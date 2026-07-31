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
_NAV_PURCHASE_TYPES = {
    TransactionType.OPENING_POSITION,
    TransactionType.MANUAL_PURCHASE,
    TransactionType.SIP_PURCHASE,
}


def _nav_earning_shares(
    repository,
    product_id,
    previous_quote_date,
    current_quote_date,
    conn=None,
):
    projector = PositionProjector(repository)
    opening_position = projector._calculate(
        product_id,
        conn=conn,
        as_of=previous_quote_date,
        use_confirmation_date=True,
    )
    confirmed_from_previous_nav = sum(
        (
            transaction.shares or ZERO
            for transaction in repository.list_transactions(
                product_id=product_id,
                conn=conn,
            )
            if (
                transaction.status is TransactionStatus.CONFIRMED
                and transaction.transaction_type in _NAV_PURCHASE_TYPES
                and transaction.trade_date <= previous_quote_date
                and previous_quote_date
                < (transaction.confirmation_date or transaction.trade_date)
                <= current_quote_date
            )
        ),
        ZERO,
    )
    current_position = projector._calculate(
        product_id,
        conn=conn,
        as_of=current_quote_date,
        use_confirmation_date=True,
    )
    return max(
        ZERO,
        opening_position.total_shares
        + confirmed_from_previous_nav
        - current_position.locked_shares,
    )


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
    for previous, current in zip(quotes, quotes[1:]):
        shares = _nav_earning_shares(
            repository,
            product.id,
            previous.quote_date,
            current.quote_date,
            conn=conn,
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
    shares = _nav_earning_shares(
        repository,
        product.id,
        previous.quote_date,
        latest.quote_date,
        conn=conn,
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
