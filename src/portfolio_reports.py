from datetime import date
from decimal import Decimal

from src.portfolio_models import (
    ProductType,
    TransactionStatus,
    TransactionType,
    decimal_text,
)


_ZERO = Decimal("0")
_ONE = Decimal("1")
_INCOME_TYPES = {TransactionType.INCOME_ACCRUAL, TransactionType.CASH_DIVIDEND}


def build_portfolio_query_report(
    repository,
    target_date: date | None = None,
    period: str = "",
) -> str:
    target_date = target_date or date.today()
    products = repository.list_products(active_only=True)
    if not products:
        return "当前未配置任何产品，请在理财看板网页中添加产品。"

    lines = []
    if period:
        lines.append(f"📊 理财组合期间查询（{period}）")
    else:
        lines.append(f"📊 理财组合查询（{target_date:%Y-%m-%d}）")
    lines.append("")

    for product in products:
        lines.append(_product_report(repository, product, target_date))
        lines.append("")

    return "\n".join(lines).rstrip()


def format_portfolio_config(repository) -> str:
    products = repository.list_products()
    if not products:
        return "当前未配置任何产品，请在理财看板网页中添加产品。"

    lines = ["📋 理财产品配置", ""]
    type_labels = {
        ProductType.WEALTH_NAV: "净值理财",
        ProductType.CASH_MANAGEMENT: "现金管理",
        ProductType.PUBLIC_FUND: "公募基金",
    }
    for product in products:
        label = type_labels.get(product.product_type, product.product_type.value)
        status = "启用" if product.status.value == "active" else "停用"
        lines.append(
            f"• {product.name}（{product.code}）\n"
            f"  类型：{label}  状态：{status}"
        )
        if product.registration_code:
            lines.append(f"  登记编码：{product.registration_code}")
    lines.append("")
    lines.append("写操作（添加/停用/交易/定投）请在理财看板网页中完成。")
    return "\n".join(lines)


def _product_report(repository, product, target_date):
    position = repository.get_position(product.id)
    if product.product_type is ProductType.CASH_MANAGEMENT:
        return _cash_report(repository, product, position, target_date)
    return _nav_report(repository, product, position, target_date)


def _cash_report(repository, product, position, target_date):
    quote = repository.latest_quote(product.id, on_or_before=target_date)
    latest_income = _daily_cash_income(repository, product.id, target_date)
    cumulative = _cumulative_cash_income(repository, product.id)

    lines = [
        f"💰 {product.name}（{product.code}）",
        f"  持仓金额：{_fmt_amount(position.total_shares)} 元",
    ]
    if quote is not None and quote.income_per_10k is not None:
        lines.append(
            f"  每万份收益：{quote.income_per_10k} 元"
        )
    if quote is not None and quote.seven_day_annualized_rate is not None:
        lines.append(
            f"  七日年化：{_fmt_percent(quote.seven_day_annualized_rate)}"
        )
    if latest_income is not None:
        lines.append(f"  当日收益：{_fmt_amount(latest_income)} 元")
    lines.append(f"  累计收益：{_fmt_amount(cumulative)} 元")
    if quote is None or quote.income_per_10k is None:
        lines.append("  最新每万份收益：待披露")
    return "\n".join(lines)


def _nav_report(repository, product, position, target_date):
    quote = repository.latest_quote(product.id, on_or_before=target_date)
    cost = position.cost_basis
    lines = [f"📈 {product.name}（{product.code}）"]
    lines.append(f"  持仓份额：{_fmt_amount(position.total_shares)}")

    if quote is not None and quote.unit_nav is not None:
        market_value = position.total_shares * quote.unit_nav
        lines.append(f"  单位净值：{_fmt_nav(quote.unit_nav)}")
        if quote.cumulative_nav is not None:
            lines.append(f"  累计净值：{_fmt_nav(quote.cumulative_nav)}")
        lines.append(f"  市值：{_fmt_amount(market_value)} 元")
        lines.append(
            f"  未实现收益：{_fmt_amount(market_value - cost)} 元"
        )
    else:
        lines.append("  最新净值：待披露")
        lines.append(f"  成本：{_fmt_amount(cost)} 元")

    if target_date is not None:
        daily = _daily_nav_change_income(repository, product.id, target_date)
        if daily is not None:
            lines.append(f"  当日行情变动：{_fmt_amount(daily)} 元")
    return "\n".join(lines)


def _daily_cash_income(repository, product_id, target_date):
    entries = [
        transaction.amount
        for transaction in repository.list_transactions(product_id=product_id)
        if (
            transaction.transaction_type is TransactionType.INCOME_ACCRUAL
            and transaction.status is TransactionStatus.CONFIRMED
            and transaction.trade_date == target_date
            and transaction.amount is not None
        )
    ]
    return sum(entries, _ZERO) if entries else None


def _cumulative_cash_income(repository, product_id):
    return sum(
        (
            transaction.amount or _ZERO
            for transaction in repository.list_transactions(product_id=product_id)
            if (
                transaction.status is TransactionStatus.CONFIRMED
                and transaction.transaction_type in _INCOME_TYPES
            )
        ),
        _ZERO,
    )


def _daily_nav_change_income(repository, product_id, target_date):
    """当日行情变动 = 当日确认的 cash_dividend（ NAV 产品当日一般无行情变动台账）。"""
    entries = [
        transaction.amount
        for transaction in repository.list_transactions(product_id=product_id)
        if (
            transaction.transaction_type is TransactionType.CASH_DIVIDEND
            and transaction.status is TransactionStatus.CONFIRMED
            and transaction.trade_date == target_date
            and transaction.amount is not None
        )
    ]
    return sum(entries, _ZERO) if entries else None


def _fmt_amount(value):
    number = Decimal(str(value))
    return format(number.normalize(), "f")


def _fmt_nav(value):
    return decimal_text(Decimal(str(value)))


def _fmt_percent(rate):
    percent = Decimal(str(rate)) * Decimal("100")
    return format(percent.normalize(), "f") + "%"
