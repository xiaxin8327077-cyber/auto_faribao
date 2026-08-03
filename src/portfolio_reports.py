from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
import logging
import os

from src.portfolio_models import (
    ProductType,
    decimal_text,
)
from src.portfolio_profit import calculate_holding_profit, calculate_latest_profit
from src.portfolio_view import build_portfolio_payload


logger = logging.getLogger(__name__)

_PERIOD_LABELS = {
    "week": "周度",
    "month": "月度",
    "quarter": "季度",
    "half_year": "半年度",
    "year": "年度",
    "rolling_7d": "近7天",
    "rolling_1m": "近一月",
    "rolling_3m": "近三月",
    "rolling_6m": "近半年",
    "rolling_1y": "近一年",
    "rolling_2y": "近两年",
    "rolling_3y": "近三年",
}
_ZERO = Decimal("0")


def build_portfolio_query_report(
    repository,
    target_date: date | None = None,
    period: str = "",
    holdings_as_of: date | None = None,
) -> str:
    target_date = target_date or date.today()
    holdings_as_of = holdings_as_of or date.today()
    held_rows = _portfolio_position_rows(repository, as_of=holdings_as_of)
    if not held_rows:
        return "当前未配置持仓产品，请在理财看板网页中查看交易和持仓。"

    query_payload = build_portfolio_payload(repository, as_of=target_date)
    query_rows = {
        row["product_id"]: row
        for row in query_payload.get("products", [])
    }
    rows = []
    for held_row in held_rows:
        row = dict(query_rows.get(held_row["product_id"], held_row))
        # Product visibility and shares always follow the current dashboard
        # holdings projection, while quote/profit fields follow target_date.
        for field in (
            "shares",
            "available_shares",
            "locked_shares",
            "in_transit_amount",
        ):
            row[field] = held_row.get(field)
        rows.append(row)

    lines = []
    period_start = None
    if period:
        label = _PERIOD_LABELS.get(period, period)
        period_start = _period_start(period, target_date)
        lines.append(
            f"📊 理财组合{label}查询（截至 {target_date:%Y-%m-%d}）"
        )
        lines.append(f"统计起点：{period_start:%Y-%m-%d}")
    else:
        lines.append(f"📊 理财组合查询（{target_date:%Y-%m-%d}）")
    lines.append("")

    for row in rows:
        lines.append(
            _position_report(
                repository,
                row,
                target_date,
                period_start=period_start,
            )
        )
        lines.append("")

    return "\n".join(lines).rstrip()


def list_portfolio_position_products(repository, as_of: date | None = None):
    """Return products shown by the dashboard holdings projection."""
    rows = _portfolio_position_rows(repository, as_of=as_of or date.today())
    products_by_id = {
        product.id: product for product in repository.list_products()
    }
    return [
        products_by_id[row["product_id"]]
        for row in rows
        if row.get("product_id") in products_by_id
    ]


def build_portfolio_daily_image_results(
    repository,
    target_date: date | None = None,
    holdings_as_of: date | None = None,
    disclosed_on: date | None = None,
):
    """Build image rows from dashboard holdings and ledger quotes."""
    from src.nav_monitor import NavProduct

    target_date = target_date or date.today()
    holdings_as_of = holdings_as_of or date.today()
    products_by_id = {
        product.id: product for product in repository.list_products()
    }
    results = []
    for row in _portfolio_position_rows(repository, as_of=holdings_as_of):
        product = products_by_id.get(row["product_id"])
        if product is None:
            continue
        shares = Decimal(str(row.get("shares") or "0"))
        nav_product = NavProduct(product.provider, product.code, product.name)
        if product.product_type is ProductType.CASH_MANAGEMENT:
            quote = repository.latest_quote(
                product.id,
                on_or_before=target_date,
            )
            if disclosed_on is not None and (
                quote is None or quote.quote_date != disclosed_on
            ):
                continue
            profit_date, profit = calculate_latest_profit(
                repository,
                product,
                target_date,
            )
            results.append(
                SimpleNamespace(
                    product=nav_product,
                    shares=shares,
                    latest=None,
                    previous=None,
                    query_result=None,
                    error="",
                    cash_income_per_10k=(
                        quote.income_per_10k if quote else None
                    ),
                    cash_seven_day_rate=(
                        quote.seven_day_annualized_rate if quote else None
                    ),
                    cash_quote_date=(
                        quote.quote_date if quote else None
                    ),
                    cash_profit=profit,
                    cash_profit_date=profit_date,
                )
            )
            continue

        latest_quote = repository.latest_quote(
            product.id,
            on_or_before=target_date,
        )
        if latest_quote is None or latest_quote.unit_nav is None:
            latest_quote = None
        previous_quote = None
        if latest_quote is not None:
            previous_quote = repository.latest_quote(
                product.id,
                on_or_before=latest_quote.quote_date - timedelta(days=1),
            )
            if previous_quote is not None and previous_quote.unit_nav is None:
                previous_quote = None
        if disclosed_on is not None and (
            latest_quote is None or latest_quote.quote_date != disclosed_on
        ):
            continue
        profit_date, profit = calculate_latest_profit(
            repository,
            product,
            target_date,
        )
        latest = (
            _quote_to_nav_record(nav_product, latest_quote)
            if latest_quote
            else None
        )
        previous = (
            _quote_to_nav_record(nav_product, previous_quote)
            if previous_quote
            else None
        )
        results.append(
            SimpleNamespace(
                product=nav_product,
                shares=shares,
                latest=latest,
                previous=previous,
                query_result=None,
                error="" if latest else "暂无净值",
                latest_profit=profit,
                latest_profit_date=profit_date,
                cash_income_per_10k=None,
                cash_seven_day_rate=None,
                cash_quote_date=None,
                cash_profit=None,
                cash_profit_date=None,
            )
        )
    return results


def build_portfolio_period_image_results(
    repository,
    period: str,
    base_date: date | None = None,
    holdings_as_of: date | None = None,
):
    """Build period image rows from dashboard holdings and ledger quotes."""
    from src.nav_monitor import NavProduct

    base_date = base_date or date.today()
    holdings_as_of = holdings_as_of or date.today()
    period_start = _period_start(period, base_date)
    previous_day = period_start - timedelta(days=1)
    products_by_id = {
        product.id: product for product in repository.list_products()
    }
    results = []
    for row in _portfolio_position_rows(repository, as_of=holdings_as_of):
        product = products_by_id.get(row["product_id"])
        if product is None:
            continue
        shares = Decimal(str(row.get("shares") or "0"))
        nav_product = NavProduct(product.provider, product.code, product.name)
        period_profit = (
            calculate_holding_profit(repository, product, base_date)
            - calculate_holding_profit(repository, product, previous_day)
        )
        if product.product_type is ProductType.CASH_MANAGEMENT:
            end_quote = repository.latest_quote(
                product.id,
                on_or_before=base_date,
            )
            results.append(
                SimpleNamespace(
                    product=nav_product,
                    shares=shares,
                    latest=None,
                    baseline=None,
                    error="",
                    period_profit=period_profit,
                    cash_income_per_10k=(
                        end_quote.income_per_10k if end_quote else None
                    ),
                    cash_seven_day_rate=(
                        end_quote.seven_day_annualized_rate
                        if end_quote
                        else None
                    ),
                    cash_quote_date=(
                        end_quote.quote_date if end_quote else None
                    ),
                    cash_profit=period_profit,
                    cash_profit_date=base_date,
                    cash_period_start=period_start,
                )
            )
            continue

        latest_quote = repository.latest_quote(
            product.id,
            on_or_before=base_date,
        )
        if latest_quote is not None and latest_quote.unit_nav is None:
            latest_quote = None
        baseline_quote = _period_baseline_quote(
            repository,
            product.id,
            period_start,
            base_date,
            include_target=period in _ROLLING_PERIODS,
        )
        results.append(
            SimpleNamespace(
                product=nav_product,
                shares=shares,
                latest=(
                    _quote_to_nav_record(nav_product, latest_quote)
                    if latest_quote
                    else None
                ),
                baseline=(
                    _quote_to_nav_record(nav_product, baseline_quote)
                    if baseline_quote
                    else None
                ),
                error="" if latest_quote else "暂无净值",
                period_profit=period_profit,
                cash_income_per_10k=None,
                cash_seven_day_rate=None,
                cash_quote_date=None,
                cash_profit=None,
                cash_profit_date=None,
                cash_period_start=None,
            )
        )
    return results


_ROLLING_PERIODS = {
    "rolling_7d",
    "rolling_1m",
    "rolling_3m",
    "rolling_6m",
    "rolling_1y",
    "rolling_2y",
    "rolling_3y",
}


def _period_baseline_quote(
    repository,
    product_id,
    period_start: date,
    base_date: date,
    *,
    include_target: bool,
):
    """Match nav_monitor baseline: prefer pre-period quote, else first in-period quote."""
    quotes = [
        quote
        for quote in repository.list_quotes(
            product_id,
            on_or_before=base_date,
        )
        if quote.unit_nav is not None
    ]
    before = [
        quote
        for quote in quotes
        if (
            quote.quote_date <= period_start
            if include_target
            else quote.quote_date < period_start
        )
    ]
    if before:
        return max(before, key=lambda quote: quote.quote_date)
    after = [
        quote
        for quote in quotes
        if (
            quote.quote_date > period_start
            if include_target
            else quote.quote_date >= period_start
        )
    ]
    if not after:
        return None
    return min(after, key=lambda quote: quote.quote_date)


def _portfolio_position_rows(repository, as_of: date):
    rows = build_portfolio_payload(repository, as_of=as_of).get("positions", [])
    # The dashboard intentionally keeps zero-share cards visible while a trade
    # is pending or a redemption is settling. WeChat profit reports have a
    # stricter contract: only products with actual held shares are included.
    return [row for row in rows if Decimal(str(row.get("shares") or "0")) > 0]


def push_portfolio_report(
    cfg,
    repository,
    target_date: date | None = None,
    period: str = "",
    to_user: str | None = None,
    holdings_as_of: date | None = None,
    disclosed_on: date | None = None,
    image_output_dir=None,
) -> str:
    from src.wechat_notifier import send_image, send_markdown
    from src.nav_report_image import (
        render_nav_period_report_image,
        render_nav_report_image,
    )
    from src.nav_monitor import PERIOD_LABELS, PERIOD_REPORT_TITLES

    target_date = target_date or date.today()
    holdings_as_of = holdings_as_of or date.today()
    report = build_portfolio_query_report(
        repository,
        target_date=target_date,
        period=period,
        holdings_as_of=holdings_as_of,
    )

    image_path = ""
    image_sent = False
    try:
        if period:
            results = build_portfolio_period_image_results(
                repository,
                period,
                base_date=target_date,
                holdings_as_of=holdings_as_of,
            )
            if not results:
                send_markdown(cfg.wechat, report, to_user)
                return report
            image_path = render_nav_period_report_image(
                results,
                title=PERIOD_REPORT_TITLES.get(period, "理财净值统计"),
                period_label=PERIOD_LABELS.get(
                    period,
                    _PERIOD_LABELS.get(period, period),
                ),
                start_date=_period_start(period, target_date),
                generated_at=datetime.now(),
                output_dir=image_output_dir,
            )
        else:
            results = build_portfolio_daily_image_results(
                repository,
                target_date=target_date,
                holdings_as_of=holdings_as_of,
                disclosed_on=disclosed_on,
            )
            if not results:
                if disclosed_on is not None:
                    logger.info(
                        "Portfolio evening image skipped: no disclosed quotes on %s",
                        disclosed_on,
                    )
                    return ""
                send_markdown(cfg.wechat, report, to_user)
                return report
            title = (
                "理财净值晚报"
                if disclosed_on is not None
                else (
                    "理财净值查询"
                    if target_date != date.today()
                    else "理财净值日报"
                )
            )
            image_path = render_nav_report_image(
                results,
                title=title,
                generated_at=datetime.now(),
                target_date=(
                    target_date if target_date != date.today() else None
                ),
                output_dir=image_output_dir,
            )
        image_sent = send_image(cfg.wechat, image_path, to_user)
    except Exception as exc:
        logger.error(
            "Portfolio report image send failed, falling back to markdown: %s",
            exc,
            exc_info=True,
        )
    finally:
        if image_path:
            try:
                os.remove(image_path)
            except OSError:
                logger.warning(
                    "Failed to remove portfolio report image: %s",
                    image_path,
                )

    if not image_sent:
        send_markdown(cfg.wechat, report, to_user)
    return report


def format_portfolio_config(repository, as_of: date | None = None) -> str:
    products = list_portfolio_position_products(
        repository,
        as_of=as_of or date.today(),
    )
    if not products:
        return "当前看板暂无实际持有份额的产品。"

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


def _quote_to_nav_record(nav_product, quote):
    from src.nav_monitor import NavRecord

    return NavRecord(
        provider=nav_product.provider,
        code=nav_product.code,
        name=nav_product.name,
        nav_date=quote.quote_date,
        unit_nav=quote.unit_nav,
        cumulative_nav=quote.cumulative_nav,
        source=quote.source,
    )


def _position_report(repository, row, target_date, period_start=None):
    product = repository.require_product(row["product_id"])
    is_cash = product.product_type is ProductType.CASH_MANAGEMENT
    icon = "💰" if is_cash else "📈"
    lines = [f"{icon} {row['name']}（{row['code']}）"]
    holding_label = "持仓金额" if is_cash else "持仓份额"
    suffix = " 元" if is_cash else ""
    lines.append(
        f"  {holding_label}：{_fmt_amount(row.get('shares') or 0)}{suffix}"
    )

    quote = row.get("quote") or {}
    if is_cash:
        income_per_10k = quote.get("income_per_10k")
        seven_day_yield = quote.get("seven_day_annualized_rate")
        if income_per_10k is not None:
            lines.append(f"  每万份收益：{_fmt_nav(income_per_10k)} 元")
        else:
            lines.append("  最新每万份收益：待披露")
        if seven_day_yield is not None:
            lines.append(f"  七日年化：{_fmt_percent(seven_day_yield)}")
    else:
        unit_nav = quote.get("unit_nav")
        if unit_nav is None:
            lines.append("  最新净值：待披露")
            lines.append(f"  成本：{_fmt_amount(row.get('cost_basis') or 0)} 元")
        else:
            lines.append(f"  单位净值：{_fmt_nav(unit_nav)}")
            if quote.get("cumulative_nav") is not None:
                lines.append(
                    f"  累计净值：{_fmt_nav(quote['cumulative_nav'])}"
                )
            if row.get("market_value") is not None:
                lines.append(
                    f"  市值：{_fmt_amount(row['market_value'])} 元"
                )

    latest_profit = row.get("latest_profit")
    latest_profit_date = row.get("latest_profit_date")
    if latest_profit is not None and latest_profit_date:
        lines.append(
            f"  最新收益（{latest_profit_date}）："
            f"{_fmt_amount(latest_profit)} 元"
        )
    if row.get("holding_profit") is not None:
        lines.append(
            f"  累计持有收益：{_fmt_amount(row['holding_profit'])} 元"
        )

    if period_start is not None:
        previous_day = period_start - timedelta(days=1)
        period_profit = (
            calculate_holding_profit(repository, product, target_date)
            - calculate_holding_profit(repository, product, previous_day)
        )
        lines.append(f"  期间收益：{_fmt_amount(period_profit)} 元")
    return "\n".join(lines)


def _period_start(period: str, base_date: date) -> date:
    rolling_days = {
        "rolling_7d": 7,
        "rolling_1m": 30,
        "rolling_3m": 90,
        "rolling_6m": 180,
        "rolling_1y": 365,
        "rolling_2y": 730,
        "rolling_3y": 1095,
    }
    if period in rolling_days:
        return base_date - timedelta(days=rolling_days[period])
    if period == "week":
        return base_date - timedelta(days=base_date.weekday())
    if period == "month":
        return date(base_date.year, base_date.month, 1)
    if period == "quarter":
        quarter_month = ((base_date.month - 1) // 3) * 3 + 1
        return date(base_date.year, quarter_month, 1)
    if period == "half_year":
        return date(base_date.year, 1 if base_date.month <= 6 else 7, 1)
    if period == "year":
        return date(base_date.year, 1, 1)
    raise ValueError(f"unsupported period: {period}")


def _fmt_amount(value) -> str:
    number = Decimal(str(value)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return format(number, "f")


def _fmt_nav(value) -> str:
    return decimal_text(value)


def _fmt_percent(value) -> str:
    number = Decimal(str(value)) * Decimal("100")
    return f"{format(number.normalize(), 'f')}%"
