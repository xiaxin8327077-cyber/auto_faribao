from decimal import Decimal
from uuid import uuid4

from src.beijing_time import now as beijing_now
from src.portfolio_models import (
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_confirmation import (
    MissingTradingCalendarError,
    is_trading_day,
)
from src.portfolio_wallet import WALLET_PROVIDER, previous_wallet_income_date


ONE = Decimal("1")
PER_TEN_THOUSAND = Decimal("10000")


class CashIncomeService:
    def __init__(self, repository, projector):
        self.repository = repository
        self.projector = projector

    def accrue(self, product_id, quote_date) -> Transaction | None:
        idempotency_key = (
            f"income:{product_id}:{quote_date.isoformat()}"
        )
        with self.repository.database.transaction() as conn:
            product = self.repository.require_product(product_id, conn=conn)
            if product.product_type is not ProductType.CASH_MANAGEMENT:
                raise ValueError("product must be cash_management")
            is_wallet = product.provider == WALLET_PROVIDER
            if is_wallet:
                # 钱包Plus：当天只记前一交易日收益，不记账当日。
                today = beijing_now().date()
                if quote_date >= today:
                    return None
                try:
                    trading_day = is_trading_day(quote_date)
                except MissingTradingCalendarError:
                    return None
                if not trading_day:
                    return None

            existing = self.repository.get_transaction_by_idempotency(
                idempotency_key,
                conn=conn,
            )
            while (
                existing is not None
                and existing.status is TransactionStatus.REVERSED
            ):
                # 计提曾被反转（如过早计提被纠正）。原 reversed 交易不可变，
                # 改用新的幂等键重新计提；若 :reactivated 也被反转则继续追加。
                idempotency_key = f"{idempotency_key}:reactivated"
                existing = self.repository.get_transaction_by_idempotency(
                    idempotency_key,
                    conn=conn,
                )
            if existing is not None:
                return self._require_matching_accrual(
                    existing,
                    product_id,
                    quote_date,
                )

            quote = self.repository.get_quote(
                product_id,
                quote_date,
                conn=conn,
            )
            if quote is None or quote.income_per_10k is None:
                return None

            # 现金：已确认份额（含当日确认）计息，确认日当天起息。
            # 钱包Plus：按收益日前一交易日已确认份额计息（T+1 确认后，
            # 确认日当天的万份收益仍不含当日新确认份额）。
            shares_as_of = (
                previous_wallet_income_date(quote_date)
                if is_wallet
                else quote_date
            )
            opening_position = self.projector._calculate(
                product_id,
                conn,
                as_of=shares_as_of,
                use_confirmation_date=True,
            )
            current_position = self.projector._calculate(
                product_id,
                conn,
                as_of=shares_as_of,
                use_confirmation_date=True,
            )
            effective_shares = max(
                Decimal("0"),
                opening_position.total_shares - current_position.locked_shares,
            )
            income_amount = (
                effective_shares
                / PER_TEN_THOUSAND
                * quote.income_per_10k
            )
            accrual = Transaction(
                id=str(uuid4()),
                product_id=product_id,
                transaction_type=TransactionType.INCOME_ACCRUAL,
                status=TransactionStatus.CONFIRMED,
                trade_date=quote_date,
                idempotency_key=idempotency_key,
                amount=income_amount,
                shares=income_amount,
                confirmation_nav=ONE,
                confirmation_date=quote_date,
                created_by="system",
            )
            self.repository.create_transaction(accrual, conn)
            rebuilt = self.projector._calculate(product_id, conn)
            self.repository.replace_position(rebuilt, conn)
            return accrual

    @staticmethod
    def _require_matching_accrual(
        existing,
        product_id,
        quote_date,
    ) -> Transaction:
        if (
            existing.product_id != product_id
            or existing.transaction_type
            is not TransactionType.INCOME_ACCRUAL
            or existing.status
            not in {
                TransactionStatus.CONFIRMED,
                TransactionStatus.REVERSED,
            }
            or existing.trade_date != quote_date
            or existing.amount is None
            or existing.shares != existing.amount
            or existing.confirmation_nav != ONE
            or existing.confirmation_date != quote_date
        ):
            raise ValueError(
                "idempotency key conflicts with existing accrual"
            )
        return existing
