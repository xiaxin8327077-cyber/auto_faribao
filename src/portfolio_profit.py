from datetime import date
from decimal import Decimal

from src.portfolio_models import (
    ProductType,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector


ZERO = Decimal("0")
_CASH_PROFIT_TYPES = {
    TransactionType.INCOME_ACCRUAL,
    TransactionType.CASH_DIVIDEND,
    TransactionType.PROFIT_ADJUSTMENT,
}
_FUND_CASH_PROFIT_TYPES = {
    TransactionType.CASH_DIVIDEND,
    TransactionType.PROFIT_ADJUSTMENT,
}


def calculate_holding_profit(
    repository,
    product,
    as_of: date,
    conn=None,
) -> Decimal:
    """Return cumulative holding profit through the requested date."""
    transactions = repository.list_transactions(
        product_id=product.id,
        conn=conn,
    )
    cash_types = (
        _CASH_PROFIT_TYPES
        if product.product_type is ProductType.CASH_MANAGEMENT
        else _FUND_CASH_PROFIT_TYPES
    )
    profit = sum(
        (
            transaction.amount or ZERO
            for transaction in transactions
            if (
                transaction.status is TransactionStatus.CONFIRMED
                and transaction.transaction_type in cash_types
                and (
                    transaction.confirmation_date
                    or transaction.trade_date
                )
                <= as_of
            )
        ),
        ZERO,
    )
    if product.product_type is ProductType.CASH_MANAGEMENT:
        return profit

    quotes = [
        quote
        for quote in repository.list_quotes(
            product.id,
            on_or_before=as_of,
            conn=conn,
        )
        if quote.unit_nav is not None
    ]
    projector = PositionProjector(repository)
    for previous, current in zip(quotes, quotes[1:]):
        shares = projector._calculate(
            product.id,
            conn=conn,
            as_of=previous.quote_date,
            use_confirmation_date=True,
        ).total_shares
        profit += shares * (current.unit_nav - previous.unit_nav)
    return profit
