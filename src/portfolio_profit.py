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
_NAV_REDEMPTION_TYPES = {
    TransactionType.MANUAL_REDEMPTION,
    TransactionType.CASH_TRANSFER_OUT,
}


def _nav_earning_shares(
    repository,
    product_id,
    previous_quote_date,
    current_quote_date,
    conn=None,
    *,
    product=None,
    transactions=None,
):
    """Shares that earn the NAV change between two disclosure dates.

    Start from confirmed holdings as of the previous NAV date, then:
    - add purchases traded by that date but confirmed in (prev, curr]
    - subtract redemptions / transfer-outs confirmed in (prev, curr]

    A redemption confirmed on the current NAV date therefore does not earn
    that disclosure segment. *product* / *transactions* allow callers on the
    dashboard hot path to reuse preloaded rows instead of re-querying.
    """
    projector = PositionProjector(repository)
    opening_position = projector._calculate(
        product_id,
        conn=conn,
        as_of=previous_quote_date,
        use_confirmation_date=True,
        product=product,
        transactions=transactions,
    )
    if transactions is None:
        transactions = repository.list_transactions(
            product_id=product_id,
            conn=conn,
        )
    confirmed_from_previous_nav = sum(
        (
            transaction.shares or ZERO
            for transaction in transactions
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
    redeemed_before_or_on_current_nav = sum(
        (
            transaction.shares or ZERO
            for transaction in transactions
            if (
                transaction.status is TransactionStatus.CONFIRMED
                and transaction.transaction_type in _NAV_REDEMPTION_TYPES
                and previous_quote_date
                < (transaction.confirmation_date or transaction.trade_date)
                <= current_quote_date
            )
        ),
        ZERO,
    )
    return max(
        ZERO,
        opening_position.total_shares
        + confirmed_from_previous_nav
        - redeemed_before_or_on_current_nav,
    )


def calculate_holding_profit(
    repository,
    product,
    as_of: date,
    conn=None,
    since: date | None = None,
) -> Decimal:
    """Return cumulative holding profit through the requested date.

    If *since* is given, only count changes on or after that date (inclusive).
    The first quote at/after *since* becomes the baseline: its NAV is treated
    as the cost basis for the period, so earlier gains are excluded.
    """
    transactions = repository.list_transactions(
        product_id=product.id,
        conn=conn,
    )
    cash_types = (
        _CASH_PROFIT_TYPES
        if product.product_type is ProductType.CASH_MANAGEMENT
        else _FUND_CASH_PROFIT_TYPES
    )
    def tx_in_range(transaction):
        tx_date = transaction.confirmation_date or transaction.trade_date
        if tx_date > as_of:
            return False
        if since is not None and tx_date < since:
            return False
        return True
    profit = sum(
        (
            transaction.amount or ZERO
            for transaction in transactions
            if (
                transaction.status is TransactionStatus.CONFIRMED
                and transaction.transaction_type in cash_types
                and tx_in_range(transaction)
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
    if since is not None:
        quotes = [q for q in quotes if q.quote_date >= since]
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


def calculate_latest_profit(
    repository,
    product,
    as_of: date,
    conn=None,
    *,
    bundle_non_trading_days: bool = True,
    transactions=None,
    quotes=None,
):
    """Return the product's latest disclosed profit date and adjusted amount.

    For cash products, weekend income is disclosed together: when the latest
    income day is Sunday, the amount is Fri+Sat+Sun (仍标注为周日日期).
    Pass bundle_non_trading_days=False for per-day history / cumulative sums.

    *transactions* / *quotes* may carry preloaded rows (the dashboard hot
    path reuses one load per product instead of re-querying per quote day);
    lookups are then resolved in memory with identical semantics.
    """
    if transactions is None:
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
        start_date = profit_date
        if bundle_non_trading_days:
            start_date = _last_trading_day_on_or_before(profit_date)
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
                    and start_date <= transaction.trade_date <= profit_date
                )
            ),
            ZERO,
        )
        return profit_date, profit

    if quotes is not None:
        visible = [
            quote for quote in quotes if quote.quote_date <= as_of
        ]
        latest = visible[-1] if visible else None
    else:
        latest = repository.latest_quote(
            product.id,
            on_or_before=as_of,
            conn=conn,
        )
    if latest is None or latest.unit_nav is None:
        return None, None
    if quotes is not None:
        earlier = [
            quote
            for quote in quotes
            if quote.quote_date < latest.quote_date
        ]
        previous = earlier[-1] if earlier else None
    else:
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
        product=product,
        transactions=transactions,
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


def _last_trading_day_on_or_before(day: date) -> date:
    from src.portfolio_confirmation import (
        MissingTradingCalendarError,
        is_trading_day,
    )

    cursor = day
    for _ in range(14):
        try:
            if is_trading_day(cursor):
                return cursor
        except MissingTradingCalendarError:
            return cursor
        cursor -= timedelta(days=1)
    return day


def product_daily_profit_series(
    repository,
    product,
    as_of: date,
    conn=None,
    *,
    transactions=None,
    quotes=None,
) -> list[tuple[date, Decimal]]:
    """该产品逐个披露日的当日收益（bundle=False 口径）。

    与持仓卡片"累计持有收益"及看板每日收益明细使用同一口径：
    每个行情披露日 d 记一笔 calculate_latest_profit(d, bundle=False)，
    仅当披露日恰好等于 d 且收益非空时收录。
    """
    if quotes is None:
        if conn is not None:
            quotes = repository.list_quotes(
                product.id,
                on_or_before=as_of,
                conn=conn,
            )
        else:
            quotes = repository.list_quotes(
                product.id,
                on_or_before=as_of,
            )
    if transactions is None:
        transactions = repository.list_transactions(
            product_id=product.id,
            conn=conn,
        )
    series = []
    for quote_date in sorted({quote.quote_date for quote in quotes}):
        profit_date, profit = calculate_latest_profit(
            repository,
            product,
            quote_date,
            conn=conn,
            bundle_non_trading_days=False,
            transactions=transactions,
            quotes=quotes,
        )
        if profit_date == quote_date and profit is not None:
            series.append((quote_date, profit))
    return series


def daily_product_profits(
    repository,
    as_of: date,
    conn=None,
    *,
    products=None,
    transactions_by_product=None,
    quotes_by_product=None,
) -> dict[str, list[tuple[date, Decimal]]]:
    """单遍计算全部产品的逐日收益序列，供看板多处视图共享。

    返回 {product_id: [(披露日, 当日收益), ...]}。调用方传入预载的
    products / transactions_by_product / quotes_by_product 可避免重复查询；
    缺省时在本函数内各查一次。
    """
    if products is None:
        if conn is not None:
            products = repository.list_products(conn=conn)
        else:
            products = repository.list_products()
    series_by_product: dict[str, list[tuple[date, Decimal]]] = {}
    for product in products:
        quotes = (
            quotes_by_product.get(product.id)
            if quotes_by_product is not None
            else None
        )
        transactions = (
            transactions_by_product.get(product.id)
            if transactions_by_product is not None
            else None
        )
        series_by_product[product.id] = product_daily_profit_series(
            repository,
            product,
            as_of,
            conn=conn,
            transactions=transactions,
            quotes=quotes,
        )
    return series_by_product
