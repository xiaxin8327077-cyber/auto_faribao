import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional


@dataclass(frozen=True)
class NavProduct:
    provider: str
    code: str
    name: str


@dataclass(frozen=True)
class NavRecord:
    provider: str
    code: str
    name: str
    nav_date: date
    unit_nav: Decimal
    cumulative_nav: Optional[Decimal] = None
    source: str = ""


@dataclass(frozen=True)
class ProductCandidate:
    provider: str
    code: str
    name: str
    register_code: str = ""
    sales_code: str = ""
    latest_nav_date: Optional[date] = None
    latest_unit_nav: Optional[Decimal] = None
    latest_cumulative_nav: Optional[Decimal] = None
    nav_error: str = ""


@dataclass(frozen=True)
class DateQueryResult:
    target_date: date
    exact: bool
    record: Optional[NavRecord] = None
    previous: Optional[NavRecord] = None
    error: str = ""


@dataclass(frozen=True)
class ProductNavResult:
    product: NavProduct
    latest: Optional[NavRecord] = None
    previous: Optional[NavRecord] = None
    query_result: Optional[DateQueryResult] = None
    error: str = ""


def parse_nav_query_date(text: str, base_date: Optional[date] = None) -> Optional[date]:
    base_date = base_date or date.today()
    content = (text or "").strip()

    if "前天" in content:
        return base_date - timedelta(days=2)
    if "昨天" in content or "昨日" in content:
        return base_date - timedelta(days=1)
    if "今天" in content or "今日" in content:
        return base_date

    match = re.search(r"(?<!\d)(20\d{6})(?!\d)", content)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def calculate_change(latest: NavRecord, previous: NavRecord) -> tuple[Decimal, Optional[Decimal]]:
    delta = latest.unit_nav - previous.unit_nav
    if previous.unit_nav == 0:
        return delta, None
    return delta, delta / previous.unit_nav * Decimal("100")


def format_nav_report(
    results: list[ProductNavResult],
    title: str = "理财净值日报",
    generated_at: Optional[datetime] = None,
    target_date: Optional[date] = None,
) -> str:
    generated_at = generated_at or datetime.now()
    lines = [f"# {title}", f"查询时间：{generated_at:%Y-%m-%d %H:%M}"]
    if target_date:
        lines.append(f"查询日期：{target_date:%Y-%m-%d}")

    for result in results:
        lines.append("")
        lines.append(f"## {result.product.name or result.product.code}")
        lines.append(f"代码：{result.product.code}")

        if result.error:
            lines.append(f"状态：查询失败：{result.error}")
            continue

        if result.query_result:
            _append_date_query(lines, result.query_result)
            continue

        if not result.latest:
            lines.append("状态：暂无净值")
            continue

        lines.append(f"最新：{_format_record(result.latest)}")
        if result.previous:
            lines.append(f"上期：{_format_record(result.previous)}")
            delta, delta_pct = calculate_change(result.latest, result.previous)
            if delta_pct is None:
                lines.append(f"涨跌：{_format_decimal(delta)}（无法计算百分比）")
            else:
                lines.append(f"涨跌：{_format_decimal(delta)}（{_format_pct(delta_pct)}）")
        else:
            lines.append("状态：暂无上一条净值，无法计算涨跌")

    return "\n".join(lines)


def _append_date_query(lines: list[str], query: DateQueryResult):
    lines.append(f"查询日期：{query.target_date:%Y-%m-%d}")
    if query.error:
        lines.append(f"状态：查询失败：{query.error}")
        return
    if not query.record:
        lines.append("状态：未找到该日期之前的净值")
        return

    if query.exact:
        lines.append(f"净值：{_format_record(query.record)}")
    else:
        lines.append("状态：该日未披露")
        lines.append(f"最近披露：{_format_record(query.record)}")

    if query.previous:
        delta, delta_pct = calculate_change(query.record, query.previous)
        if delta_pct is None:
            lines.append(f"较上期：{_format_decimal(delta)}（无法计算百分比）")
        else:
            lines.append(f"较上期：{_format_decimal(delta)}（{_format_pct(delta_pct)}）")


def _format_record(record: NavRecord) -> str:
    return f"{record.nav_date:%Y-%m-%d}  {_format_decimal(record.unit_nav)}"


def _format_decimal(value) -> str:
    try:
        return f"{Decimal(str(value)).quantize(Decimal('0.000001'))}"
    except (InvalidOperation, ValueError):
        return str(value)


def _format_pct(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.0001'))}%"
