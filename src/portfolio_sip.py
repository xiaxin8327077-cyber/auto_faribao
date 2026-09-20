from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from src.portfolio_reconciliation import reconciled_transaction
from uuid import NAMESPACE_URL, uuid4, uuid5

from src.beijing_time import TZ_CN, today as beijing_today

from src.portfolio_models import (
    ProductStatus,
    ProductType,
    SipFrequency,
    SipPlan,
    SipPlanStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_confirmation import (
    MissingTradingCalendarError,
    confirmation_schedule,
    is_trading_day,
)
from src.portfolio_sip_schedule import (
    is_sip_trade_date,
    iter_sip_trade_dates,
    normalize_sip_schedule,
)


ZERO = Decimal("0")


def _utc_timestamp_to_beijing_date(value):
    """把 SQLite ``CURRENT_TIMESTAMP``（UTC 无时区串）转成北京自然日。"""
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(TZ_CN).date()
ONE = Decimal("1")


@dataclass(frozen=True)
class PlanExecution:
    id: str
    plan_id: str
    intended_trade_date: date
    status: str
    reason: str = ""
    transaction_id: str = ""


class SipService:
    def __init__(
        self,
        repository,
        projector,
        transaction_service,
        notifier=None,
    ):
        self.repository = repository
        self.projector = projector
        self.transaction_service = transaction_service
        self.notifier = notifier

    def save_plan(
        self,
        product_id,
        daily_amount,
        purchase_fee_rate,
        source_cash_product_id="",
        start_date=None,
        frequency=SipFrequency.DAILY,
        schedule_day=None,
    ) -> SipPlan:
        daily_amount = self._decimal(daily_amount, "daily_amount")
        if daily_amount <= ZERO:
            raise ValueError("daily_amount must be positive")
        purchase_fee_rate = self._decimal(
            purchase_fee_rate, "purchase_fee_rate"
        )
        if not ZERO <= purchase_fee_rate < ONE:
            raise ValueError("purchase_fee_rate must be between 0 and 1")
        if not isinstance(start_date, date):
            raise ValueError("start_date is required")
        frequency, schedule_day = normalize_sip_schedule(
            frequency,
            schedule_day,
        )

        with self.repository.database.transaction() as conn:
            product = self.repository.require_product(product_id, conn=conn)
            if product.product_type is not ProductType.PUBLIC_FUND:
                raise ValueError("target must be public_fund")
            if source_cash_product_id:
                source = self.repository.require_product(
                    source_cash_product_id, conn=conn
                )
                if source.product_type is not ProductType.CASH_MANAGEMENT:
                    raise ValueError("source must be cash_management")
            plan = SipPlan(
                id=str(uuid4()),
                product_id=product.id,
                daily_amount=daily_amount,
                purchase_fee_rate=purchase_fee_rate,
                source_cash_product_id=source_cash_product_id or "",
                status=SipPlanStatus.DRAFT,
                start_date=start_date,
                frequency=frequency,
                schedule_day=schedule_day,
            )
            return self.repository.save_plan(plan, conn)

    def activate(self, plan_id) -> SipPlan:
        with self.repository.database.transaction() as conn:
            plan = self._require_plan(plan_id, conn)
            if plan.status is SipPlanStatus.PAUSED:
                raise ValueError("plan is paused")
            if not plan.source_cash_product_id:
                raise ValueError("cash source is required")
            self._require_active_plan_products(plan, conn)
            if plan.status is SipPlanStatus.ACTIVE:
                return plan
            return self.repository.save_plan(
                replace(plan, status=SipPlanStatus.ACTIVE),
                conn,
            )

    def pause(self, plan_id, conn=None) -> SipPlan:
        def _apply(active_conn):
            plan = self._require_plan(plan_id, active_conn)
            if plan.status is SipPlanStatus.DRAFT:
                raise ValueError("plan is not active")
            if plan.status is SipPlanStatus.PAUSED:
                return plan
            return self.repository.save_plan(
                replace(plan, status=SipPlanStatus.PAUSED),
                active_conn,
            )

        if conn is not None:
            return _apply(conn)
        with self.repository.database.transaction() as owned:
            return _apply(owned)

    def resume(self, plan_id, conn=None, resume_day=None) -> SipPlan:
        """恢复定投：在切换为 ACTIVE 之前，先把暂停区间内已到期
        的真实执行日幂等记录为 ``plan_paused`` 终态跳过，确保即使暂停
        期间定时任务一次都没运行，恢复后也不会补扣，且恢复当天不扣。
        """
        target_day = resume_day or beijing_today()

        def _apply(active_conn):
            plan = self._require_plan(plan_id, active_conn)
            if plan.status is SipPlanStatus.DRAFT:
                raise ValueError("plan is not paused")
            if plan.status is SipPlanStatus.ACTIVE:
                return plan
            self._require_active_plan_products(plan, active_conn)
            # paused_at 会在切换 ACTIVE 时被清空，必须在此之前读取。
            paused_at = self.repository.get_plan_paused_at(
                plan.id, conn=active_conn
            )
            window_start = _utc_timestamp_to_beijing_date(paused_at)
            if window_start is None:
                # 无法确定暂停边界时拒绝恢复，保持 PAUSED，
                # 既不关闭任何历史，也不沿用可能补扣的旧行为。
                raise ValueError(
                    "暂停起始时间缺失，无法安全恢复；计划保持暂停，"
                    "请检查 paused_at 后重试"
                )
            self._close_pause_window(plan, window_start, target_day, active_conn)
            return self.repository.save_plan(
                replace(plan, status=SipPlanStatus.ACTIVE),
                active_conn,
            )

        if conn is not None:
            return _apply(conn)
        with self.repository.database.transaction() as owned:
            return _apply(owned)

    def _close_pause_window(self, plan, window_start, resume_day, conn):
        for intended_date in iter_sip_trade_dates(plan, resume_day):
            if intended_date > resume_day:
                break
            if intended_date < window_start:
                continue
            if self.repository.get_plan_execution(
                plan.id, intended_date, conn=conn
            ) is not None:
                continue
            self.repository.save_plan_execution(
                self._new_execution(
                    plan.id, intended_date, "skipped", "plan_paused"
                ),
                conn,
            )

    def ensure_intent(
        self,
        plan_id,
        intended_date,
        *,
        recheck_schedule=False,
        _scheduled_dates=None,
    ) -> PlanExecution:
        should_notify = False
        with reconciled_transaction(self.repository, "sip_deduction") as conn:
            plan = self._require_plan(plan_id, conn)
            reason = self._skip_reason(
                plan, intended_date, _scheduled_dates
            )
            existing = self.repository.get_plan_execution(
                plan_id, intended_date, conn=conn
            )
            retryable_schedule_skip = (
                recheck_schedule
                and existing is not None
                and existing.status == "skipped"
                and existing.reason
                in {
                    "before_start_date",
                    "plan_not_active",
                }
                and not reason
            )
            if existing is not None and not retryable_schedule_skip:
                return existing

            if reason:
                execution = self._new_execution(
                    plan.id, intended_date, "skipped", reason
                )
                return self.repository.save_plan_execution(execution, conn)

            inactive_reason = self._inactive_product_reason(plan, conn)
            if inactive_reason:
                execution = self._new_execution(
                    plan.id,
                    intended_date,
                    "skipped",
                    inactive_reason,
                )
                execution = self.repository.save_plan_execution(
                    execution, conn
                )
                self.repository.enqueue_plan_execution_notification(
                    execution, conn
                )
                should_notify = True
            else:
                source_position = self.projector._calculate(
                    plan.source_cash_product_id, conn
                )
                if source_position.available_shares < plan.daily_amount:
                    execution = self._new_execution(
                        plan.id,
                        intended_date,
                        "skipped",
                        "insufficient_balance",
                    )
                    execution = self.repository.save_plan_execution(
                        execution, conn
                    )
                    self.repository.enqueue_plan_execution_notification(
                        execution, conn
                    )
                    should_notify = True
                else:
                    purchase = self._create_pending_transactions(
                        plan, intended_date, conn
                    )
                    execution = self._new_execution(
                        plan.id,
                        intended_date,
                        "pending_quote",
                        transaction_id=purchase.id,
                    )
                    execution = self.repository.save_plan_execution(
                        execution, conn
                    )
                    self._rebuild_positions(
                        {
                            plan.product_id,
                            plan.source_cash_product_id,
                        },
                        conn,
                    )

        if should_notify:
            self._deliver_notification(execution)
        return execution

    def backfill_plan(
        self,
        plan_id,
        through_date,
        *,
        settle=True,
    ) -> list[PlanExecution]:
        if not isinstance(through_date, date):
            raise ValueError("through_date is required")
        plan = self._require_plan(plan_id)
        if plan.status is not SipPlanStatus.ACTIVE:
            return self.list_executions(plan.id, through_date)

        # 一次性算出周期内全部真实执行日，避免每个日期再从头枚举，
        # 使补跑复杂度从 O(n^2) 降到 O(n)。
        scheduled = set(iter_sip_trade_dates(plan, through_date))
        for intended_date in sorted(scheduled):
            self.ensure_intent(
                plan.id,
                intended_date,
                recheck_schedule=True,
                _scheduled_dates=scheduled,
            )
        if settle:
            self.settle_pending(through_date)
        return self.list_executions(plan.id, through_date)

    def settle_pending(self, as_of_date) -> list[PlanExecution]:
        settled = []
        for plan in self.repository.list_plans(include_deleted=True):
            for execution in self.repository.list_plan_executions(plan.id):
                if (
                    execution.status != "pending_quote"
                    or execution.intended_trade_date > as_of_date
                ):
                    continue
                result = self._settle_execution(execution.id, as_of_date)
                if result is not None:
                    settled.append(result)
        return settled

    def get_execution(self, execution_id) -> PlanExecution:
        execution = self.repository.get_plan_execution_by_id(execution_id)
        if execution is None:
            raise ValueError("plan execution not found")
        return execution

    def list_executions(
        self,
        plan_id,
        on_or_before=None,
    ) -> list[PlanExecution]:
        executions = self.repository.list_plan_executions(plan_id)
        if on_or_before is None:
            return executions
        return [
            execution
            for execution in executions
            if execution.intended_trade_date <= on_or_before
        ]

    def retry_notifications(self) -> list[PlanExecution]:
        delivered = []
        for execution in (
            self.repository.list_pending_plan_execution_notifications()
        ):
            if self._deliver_notification(execution):
                delivered.append(execution)
        return delivered

    def _settle_execution(
        self,
        execution_id,
        as_of_date,
    ) -> PlanExecution | None:
        with reconciled_transaction(self.repository, "sip_confirmation") as conn:
            execution = self.repository.get_plan_execution_by_id(
                execution_id, conn=conn
            )
            if (
                execution is None
                or execution.status != "pending_quote"
                or execution.intended_trade_date > as_of_date
            ):
                return None
            purchase, cash = self._pending_pair(execution, conn)
            product = self.repository.require_product(
                purchase.product_id, conn=conn
            )
            confirmed_on = confirmation_schedule(
                product,
                TransactionType.SIP_PURCHASE,
                datetime.combine(execution.intended_trade_date, time.min),
            ).confirmation_date
            if as_of_date < confirmed_on:
                return None
            quote = self.repository.get_quote(
                purchase.product_id,
                execution.intended_trade_date,
                conn=conn,
            )
            if quote is not None and quote.unit_nav is not None:
                nav = self._positive_decimal(quote.unit_nav, "unit_nav")
                amount = self._positive_decimal(purchase.amount, "amount")
                fee_rate = self._fee_rate(purchase.fee_rate)
                fee_amount = amount * fee_rate
                shares = (amount - fee_amount) / nav
                shares = self._positive_decimal(shares, "shares")
                self.repository.update_pending_transaction(
                    replace(
                        purchase,
                        status=TransactionStatus.CONFIRMED,
                        shares=shares,
                        fee_amount=fee_amount,
                        confirmation_nav=nav,
                        confirmation_date=confirmed_on,
                    ),
                    conn,
                )
                result = replace(execution, status="confirmed")
            else:
                later_quote = self.repository.latest_quote(
                    purchase.product_id,
                    on_or_before=as_of_date,
                    conn=conn,
                )
                if (
                    later_quote is None
                    or later_quote.quote_date
                    <= execution.intended_trade_date
                ):
                    return None
                self.repository.update_pending_transaction(
                    replace(purchase, status=TransactionStatus.CANCELLED),
                    conn,
                )
                self._refund_cash_deduction(cash, conn)
                result = replace(
                    execution,
                    status="skipped",
                    reason="non_trading_day",
                )

            self.repository.save_plan_execution(result, conn)
            self._rebuild_positions(
                {
                    purchase.product_id,
                    cash.product_id,
                },
                conn,
            )
            return result

    def _create_pending_transactions(self, plan, intended_date, conn):
        execution_key = f"{plan.id}:{intended_date.isoformat()}"
        purchase_id = str(
            uuid5(NAMESPACE_URL, f"portfolio-sip-purchase:{execution_key}")
        )
        cash_id = str(
            uuid5(NAMESPACE_URL, f"portfolio-sip-cash:{execution_key}")
        )
        purchase = Transaction(
            id=purchase_id,
            product_id=plan.product_id,
            transaction_type=TransactionType.SIP_PURCHASE,
            status=TransactionStatus.PENDING_QUOTE,
            trade_date=intended_date,
            idempotency_key=f"sip:{execution_key}:purchase",
            amount=plan.daily_amount,
            fee_rate=plan.purchase_fee_rate,
            plan_id=plan.id,
            created_by="sip",
        )
        cash = Transaction(
            id=cash_id,
            product_id=plan.source_cash_product_id,
            transaction_type=TransactionType.CASH_TRANSFER_OUT,
            status=TransactionStatus.CONFIRMED,
            trade_date=intended_date,
            idempotency_key=f"sip:{execution_key}:cash",
            amount=plan.daily_amount,
            shares=plan.daily_amount,
            confirmation_nav=ONE,
            confirmation_date=intended_date,
            linked_transaction_id=purchase_id,
            plan_id=plan.id,
            created_by="sip",
        )
        self.repository.create_transaction(purchase, conn)
        self.repository.create_transaction(cash, conn)
        purchase = replace(purchase, linked_transaction_id=cash_id)
        self.repository.update_pending_transaction(purchase, conn)
        return purchase

    def _pending_pair(self, execution, conn):
        purchase = self.repository.get_transaction_by_id(
            execution.transaction_id, conn=conn
        )
        cash = (
            self.repository.get_transaction_by_id(
                purchase.linked_transaction_id, conn=conn
            )
            if purchase is not None
            else None
        )
        if (
            cash is not None
            and cash.transaction_type is TransactionType.CASH_TRANSFER_OUT
            and cash.status is TransactionStatus.PENDING_QUOTE
            and cash.confirmation_nav is None
            and cash.confirmation_date is None
        ):
            cash = replace(
                cash,
                status=TransactionStatus.CONFIRMED,
                confirmation_nav=ONE,
                confirmation_date=cash.trade_date,
            )
            self.repository.update_pending_transaction(cash, conn)
            self._rebuild_positions({cash.product_id}, conn)
        execution_key = (
            f"{execution.plan_id}:"
            f"{execution.intended_trade_date.isoformat()}"
        )
        expected_purchase_id = str(
            uuid5(
                NAMESPACE_URL,
                f"portfolio-sip-purchase:{execution_key}",
            )
        )
        expected_cash_id = str(
            uuid5(NAMESPACE_URL, f"portfolio-sip-cash:{execution_key}")
        )
        purchase_amount_valid = (
            purchase is not None
            and self._is_finite_positive(purchase.amount)
        )
        fee_rate_valid = (
            purchase is not None
            and self._is_valid_fee_rate(purchase.fee_rate)
        )
        target = (
            self.repository.get_product(purchase.product_id, conn=conn)
            if purchase is not None
            else None
        )
        source = (
            self.repository.get_product(cash.product_id, conn=conn)
            if cash is not None
            else None
        )
        if (
            purchase is None
            or cash is None
            or purchase.id != expected_purchase_id
            or cash.id != expected_cash_id
            or purchase.transaction_type is not TransactionType.SIP_PURCHASE
            or cash.transaction_type is not TransactionType.CASH_TRANSFER_OUT
            or purchase.status is not TransactionStatus.PENDING_QUOTE
            or cash.status is not TransactionStatus.CONFIRMED
            or purchase.linked_transaction_id != cash.id
            or cash.linked_transaction_id != purchase.id
            or purchase.plan_id != execution.plan_id
            or cash.plan_id != execution.plan_id
            or purchase.trade_date != execution.intended_trade_date
            or cash.trade_date != execution.intended_trade_date
            or purchase.idempotency_key
            != f"sip:{execution_key}:purchase"
            or cash.idempotency_key != f"sip:{execution_key}:cash"
            or not purchase_amount_valid
            or not fee_rate_valid
            or purchase.shares is not None
            or purchase.fee_amount is not None
            or purchase.confirmation_nav is not None
            or purchase.confirmation_date is not None
            or cash.amount != purchase.amount
            or cash.shares != purchase.amount
            or cash.fee_amount is not None
            or cash.fee_rate is not None
            or cash.confirmation_nav != ONE
            or cash.confirmation_date != execution.intended_trade_date
            or purchase.created_by != "sip"
            or cash.created_by != purchase.created_by
            or target is None
            or target.product_type is not ProductType.PUBLIC_FUND
            or source is None
            or source.product_type is not ProductType.CASH_MANAGEMENT
        ):
            raise ValueError("inconsistent SIP transaction pair")
        return purchase, cash

    def _refund_cash_deduction(self, cash, conn):
        return self.transaction_service._refund_confirmed_sip_cash(
            cash,
            conn,
        )

    def _skip_reason(self, plan, intended_date, _scheduled_dates=None):
        if intended_date < plan.start_date:
            return "before_start_date"
        try:
            trading_day = is_trading_day(intended_date)
            if _scheduled_dates is None:
                scheduled_day = is_sip_trade_date(plan, intended_date)
            else:
                scheduled_day = intended_date in _scheduled_dates
        except MissingTradingCalendarError:
            return "calendar_unavailable"
        if not trading_day:
            return "non_trading_day"
        if not scheduled_day:
            return "not_scheduled_day"
        if plan.status is SipPlanStatus.PAUSED:
            return "plan_paused"
        if plan.status is not SipPlanStatus.ACTIVE:
            return "plan_not_active"
        return ""

    def _require_active_plan_products(self, plan, conn):
        target = self.repository.require_product(
            plan.product_id, conn=conn
        )
        if target.product_type is not ProductType.PUBLIC_FUND:
            raise ValueError("target must be public_fund")
        if target.status is not ProductStatus.ACTIVE:
            raise ValueError("target product is inactive")
        source = self.repository.require_product(
            plan.source_cash_product_id, conn=conn
        )
        if source.product_type is not ProductType.CASH_MANAGEMENT:
            raise ValueError("source must be cash_management")
        if source.status is not ProductStatus.ACTIVE:
            raise ValueError("cash source is inactive")
        return target, source

    def _inactive_product_reason(self, plan, conn):
        target = self.repository.require_product(
            plan.product_id, conn=conn
        )
        if target.product_type is not ProductType.PUBLIC_FUND:
            raise ValueError("target must be public_fund")
        source = self.repository.require_product(
            plan.source_cash_product_id, conn=conn
        )
        if source.product_type is not ProductType.CASH_MANAGEMENT:
            raise ValueError("source must be cash_management")
        if target.status is not ProductStatus.ACTIVE:
            return "target_inactive"
        if source.status is not ProductStatus.ACTIVE:
            return "source_inactive"
        return ""

    def _require_plan(self, plan_id, conn=None):
        plan = self.repository.get_plan(plan_id, conn=conn)
        if plan is None:
            raise ValueError("plan not found")
        return plan

    @staticmethod
    def _new_execution(
        plan_id,
        intended_date,
        status,
        reason="",
        transaction_id="",
    ):
        execution_id = str(
            uuid5(
                NAMESPACE_URL,
                f"portfolio-sip-execution:{plan_id}:{intended_date.isoformat()}",
            )
        )
        return PlanExecution(
            execution_id,
            plan_id,
            intended_date,
            status,
            reason,
            transaction_id,
        )

    def _rebuild_positions(self, product_ids, conn):
        for product_id in sorted(product_ids):
            self.repository.replace_position(
                self.projector._calculate(product_id, conn),
                conn,
            )

    def _notify(self, execution):
        if self.notifier is None:
            return
        if callable(self.notifier):
            self.notifier(execution)
            return
        notify = getattr(self.notifier, "notify", None)
        if notify is None:
            raise TypeError("notifier must be callable")
        notify(execution)

    def _deliver_notification(self, execution):
        if self.notifier is None:
            return False
        try:
            self._notify(execution)
        except Exception:
            return False
        self.repository.mark_plan_execution_notification_sent(execution.id)
        return True

    @staticmethod
    def _decimal(value, name):
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"{name} must be a finite decimal") from exc
        if not number.is_finite():
            raise ValueError(f"{name} must be a finite decimal")
        return number

    @classmethod
    def _positive_decimal(cls, value, name):
        number = cls._decimal(value, name)
        if number <= ZERO:
            raise ValueError(f"{name} must be positive")
        return number

    @classmethod
    def _fee_rate(cls, value):
        number = cls._decimal(value, "fee_rate")
        if not ZERO <= number < ONE:
            raise ValueError("fee_rate must be between 0 and 1")
        return number

    @staticmethod
    def _is_finite_positive(value):
        return (
            isinstance(value, Decimal)
            and value.is_finite()
            and value > ZERO
        )

    @staticmethod
    def _is_valid_fee_rate(value):
        return (
            isinstance(value, Decimal)
            and value.is_finite()
            and ZERO <= value < ONE
        )
