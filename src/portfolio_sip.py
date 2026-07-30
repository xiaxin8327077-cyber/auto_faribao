from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, uuid4, uuid5

from src.portfolio_models import (
    ProductType,
    SipPlan,
    SipPlanStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
)


ZERO = Decimal("0")
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
            )
            return self.repository.save_plan(plan, conn)

    def activate(self, plan_id) -> SipPlan:
        with self.repository.database.transaction() as conn:
            plan = self._require_plan(plan_id, conn)
            if not plan.source_cash_product_id:
                raise ValueError("cash source is required")
            return self.repository.save_plan(
                replace(plan, status=SipPlanStatus.ACTIVE),
                conn,
            )

    def pause(self, plan_id) -> SipPlan:
        with self.repository.database.transaction() as conn:
            plan = self._require_plan(plan_id, conn)
            if plan.status is SipPlanStatus.DRAFT:
                raise ValueError("plan is not active")
            return self.repository.save_plan(
                replace(plan, status=SipPlanStatus.PAUSED),
                conn,
            )

    def resume(self, plan_id) -> SipPlan:
        with self.repository.database.transaction() as conn:
            plan = self._require_plan(plan_id, conn)
            if plan.status is SipPlanStatus.DRAFT:
                raise ValueError("plan is not paused")
            return self.repository.save_plan(
                replace(plan, status=SipPlanStatus.ACTIVE),
                conn,
            )

    def ensure_intent(self, plan_id, intended_date) -> PlanExecution:
        notify = False
        with self.repository.database.transaction() as conn:
            existing = self.repository.get_plan_execution(
                plan_id, intended_date, conn=conn
            )
            if existing is not None:
                return existing

            plan = self._require_plan(plan_id, conn)
            reason = self._skip_reason(plan, intended_date)
            if reason:
                execution = self._new_execution(
                    plan.id, intended_date, "skipped", reason
                )
                return self.repository.save_plan_execution(execution, conn)

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
                notify = True
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

        if notify:
            self._notify(execution)
        return execution

    def settle_pending(self, as_of_date) -> list[PlanExecution]:
        settled = []
        for plan in self.repository.list_plans():
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

    def _settle_execution(
        self,
        execution_id,
        as_of_date,
    ) -> PlanExecution | None:
        with self.repository.database.transaction() as conn:
            execution = self.repository.get_plan_execution_by_id(
                execution_id, conn=conn
            )
            if (
                execution is None
                or execution.status != "pending_quote"
                or execution.intended_trade_date > as_of_date
            ):
                return None
            plan = self._require_plan(execution.plan_id, conn)
            purchase, cash = self._pending_pair(execution, plan, conn)
            quote = self.repository.get_quote(
                plan.product_id,
                execution.intended_trade_date,
                conn=conn,
            )
            if quote is not None and quote.unit_nav is not None:
                nav = self._positive_decimal(quote.unit_nav, "unit_nav")
                fee_amount = plan.daily_amount * plan.purchase_fee_rate
                shares = (plan.daily_amount - fee_amount) / nav
                shares = self._positive_decimal(shares, "shares")
                self.repository.update_pending_transaction(
                    replace(
                        purchase,
                        status=TransactionStatus.CONFIRMED,
                        shares=shares,
                        fee_amount=fee_amount,
                        confirmation_nav=nav,
                        confirmation_date=execution.intended_trade_date,
                    ),
                    conn,
                )
                self.repository.update_pending_transaction(
                    replace(
                        cash,
                        status=TransactionStatus.CONFIRMED,
                        confirmation_nav=ONE,
                        confirmation_date=execution.intended_trade_date,
                    ),
                    conn,
                )
                result = replace(execution, status="confirmed")
            else:
                later_quote = self.repository.latest_quote(
                    plan.product_id,
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
                self.repository.update_pending_transaction(
                    replace(cash, status=TransactionStatus.CANCELLED),
                    conn,
                )
                result = replace(
                    execution,
                    status="skipped",
                    reason="non_trading_day",
                )

            self.repository.save_plan_execution(result, conn)
            self._rebuild_positions(
                {
                    plan.product_id,
                    plan.source_cash_product_id,
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
            status=TransactionStatus.PENDING_QUOTE,
            trade_date=intended_date,
            idempotency_key=f"sip:{execution_key}:cash",
            amount=plan.daily_amount,
            shares=plan.daily_amount,
            linked_transaction_id=purchase_id,
            plan_id=plan.id,
            created_by="sip",
        )
        self.repository.create_transaction(purchase, conn)
        self.repository.create_transaction(cash, conn)
        purchase = replace(purchase, linked_transaction_id=cash_id)
        self.repository.update_pending_transaction(purchase, conn)
        return purchase

    def _pending_pair(self, execution, plan, conn):
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
            purchase is None
            or cash is None
            or purchase.transaction_type is not TransactionType.SIP_PURCHASE
            or cash.transaction_type is not TransactionType.CASH_TRANSFER_OUT
            or purchase.status is not TransactionStatus.PENDING_QUOTE
            or cash.status is not TransactionStatus.PENDING_QUOTE
            or purchase.product_id != plan.product_id
            or cash.product_id != plan.source_cash_product_id
            or purchase.linked_transaction_id != cash.id
            or cash.linked_transaction_id != purchase.id
            or purchase.plan_id != plan.id
            or cash.plan_id != plan.id
            or purchase.trade_date != execution.intended_trade_date
            or cash.trade_date != execution.intended_trade_date
        ):
            raise ValueError("inconsistent SIP transaction pair")
        return purchase, cash

    def _skip_reason(self, plan, intended_date):
        if intended_date < plan.start_date:
            return "before_start_date"
        if intended_date.weekday() >= 5:
            return "non_trading_day"
        if plan.status is SipPlanStatus.PAUSED:
            return "plan_paused"
        if plan.status is not SipPlanStatus.ACTIVE:
            return "plan_not_active"
        return ""

    def _require_plan(self, plan_id, conn):
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
