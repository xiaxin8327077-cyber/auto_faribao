from decimal import Decimal
from uuid import uuid4

from src.portfolio_models import (
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)


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

            position = self.projector._calculate(product_id, conn)
            effective_shares = (
                position.total_shares - position.locked_shares
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
