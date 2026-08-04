from datetime import date, timedelta
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
from src.portfolio_wallet import WALLET_PROVIDER


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
            if product.provider == WALLET_PROVIDER:
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

            # 已确认份额（含当日确认）才计息；未确认申购不计，确认日当天起息。
            opening_position = self.projector._calculate(
                product_id,
                conn,
                as_of=quote_date,
                use_confirmation_date=True,
            )
            current_position = self.projector._calculate(
                product_id,
                conn,
                as_of=quote_date,
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
