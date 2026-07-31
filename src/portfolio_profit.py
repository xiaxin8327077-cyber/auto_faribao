from datetime import date, timedelta
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
    TransactionType.LATEST_PROFIT_ADJUSTMENT,
}
_FUND_CASH_PROFIT_TYPES = {
    TransactionType.CASH_DIVIDEND,
    TransactionType.PROFIT_ADJUSTMENT,
    TransactionType.LATEST_PROFIT_ADJUSTMENT,
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
        opening_position = projector._calculate(
            product.id,
            conn=conn,
            as_of=current.quote_date - timedelta(days=1),
            use_confirmation_date=True,
        )
        current_position = projector._calculate(
            product.id,
            conn=conn,
            as_of=current.quote_date,
            use_confirmation_date=True,
        )
        shares = max(
            ZERO,
            opening_position.total_shares - current_position.locked_shares,
        )
        profit += shares * (current.unit_nav - previous.unit_nav)
    return profit


def calculate_latest_profit(repository, product, as_of: date, conn=None):
    """Return the product's latest disclosed profit date and adjusted amount."""
    transactions = repository.list_transactions(
        product_id=product.id,
        conn=conn,
    )
    if product.product_type is ProductType.CASH_MANAGEMENT:
        income_dates = [
            transaction.trade_date
            for transaction in transactions
            if (
                transaction.status is TransactionStatus.CONFIRMED
                and transaction.transaction_type
                is TransactionType.INCOME_ACCRUAL
                and transaction.trade_date <= as_of
            )
        ]
        if not income_dates:
            return None, None
        profit_date = max(income_dates)
        profit = sum(
            (
                transaction.amount or ZERO
                for transaction in transactions
                if (
                    transaction.status is TransactionStatus.CONFIRMED
                    and transaction.transaction_type
                    in {
                        TransactionType.INCOME_ACCRUAL,
                        TransactionType.LATEST_PROFIT_ADJUSTMENT,
                    }
                    and transaction.trade_date == profit_date
                )
            ),
            ZERO,
        )
        return profit_date, profit

    latest = repository.latest_quote(
        product.id,
        on_or_before=as_of,
        conn=conn,
    )
    if latest is None or latest.unit_nav is None:
        return None, None
    previous = repository.latest_quote(
        product.id,
        on_or_before=latest.quote_date - timedelta(days=1),
        conn=conn,
    )
    if previous is None or previous.unit_nav is None:
        return latest.quote_date, None
    projector = PositionProjector(repository)
    opening_position = projector._calculate(
        product.id,
        conn=conn,
        as_of=latest.quote_date - timedelta(days=1),
        use_confirmation_date=True,
    )
    current_position = projector._calculate(
        product.id,
        conn=conn,
        as_of=latest.quote_date,
        use_confirmation_date=True,
    )
    shares = max(
        ZERO,
        opening_position.total_shares - current_position.locked_shares,
    )
    adjustment = sum(
        (
            transaction.amount or ZERO
            for transaction in transactions
            if (
                transaction.status is TransactionStatus.CONFIRMED
                and transaction.transaction_type
                is TransactionType.LATEST_PROFIT_ADJUSTMENT
                and transaction.trade_date == latest.quote_date
            )
        ),
        ZERO,
    )
    return (
        latest.quote_date,
        shares * (latest.unit_nav - previous.unit_nav) + adjustment,
    )
