from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
from zoneinfo import ZoneInfo

from src.portfolio_models import ProductType, TransactionType
from src.workday_calendar import has_workday_calendar_year, is_workday


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class MissingTradingCalendarError(ValueError):
    pass


@dataclass(frozen=True)
class ConfirmationSchedule:
    trade_date: date
    confirmation_date: date
    cutoff_time: time
    confirmation_trading_days: int


_DEFAULT_RULES = {
    ProductType.PUBLIC_FUND: {
        "cutoff_time": "15:00",
        "confirmation_trading_days": 1,
    },
    ProductType.WEALTH_NAV: {
        "cutoff_time": "15:00",
        "confirmation_trading_days": 1,
    },
    ProductType.CASH_MANAGEMENT: {
        "cutoff_time": "23:59",
        "confirmation_trading_days": 1,
    },
}


def confirmation_schedule(
    product,
    transaction_type,
    submitted_at: datetime,
) -> ConfirmationSchedule:
    submitted_at = normalize_market_datetime(submitted_at)
    kind = _transaction_kind(transaction_type)
    rule = dict(_DEFAULT_RULES[product.product_type])
    metadata = _metadata(product)
    configured = metadata.get("trade_rules", {}).get(kind, {})
    if isinstance(configured, dict):
        rule.update(configured)

    cutoff = _parse_cutoff(rule.get("cutoff_time"))
    confirmation_days = _confirmation_days(
        rule.get("confirmation_trading_days")
    )
    effective = submitted_at.date()
    if not is_trading_day(effective) or submitted_at.time() > cutoff:
        effective = _next_trading_day(effective)
    confirmed = _add_trading_days(effective, confirmation_days)
    return ConfirmationSchedule(
        trade_date=effective,
        confirmation_date=confirmed,
        cutoff_time=cutoff,
        confirmation_trading_days=confirmation_days,
    )


def _transaction_kind(transaction_type) -> str:
    if transaction_type in {
        TransactionType.MANUAL_PURCHASE,
        TransactionType.SIP_PURCHASE,
    }:
        return "purchase"
    if transaction_type is TransactionType.MANUAL_REDEMPTION:
        return "redemption"
    raise ValueError("unsupported confirmation transaction type")


def _metadata(product) -> dict:
    try:
        value = json.loads(product.metadata_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_cutoff(value) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("invalid product cutoff_time") from exc


def _confirmation_days(value) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid confirmation_trading_days")
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid confirmation_trading_days") from exc
    if days < 0 or days > 30:
        raise ValueError("invalid confirmation_trading_days")
    return days


def is_trading_day(day: date) -> bool:
    if not has_workday_calendar_year(day.year):
        raise MissingTradingCalendarError(
            f"missing official trading calendar for {day.year}"
        )
    return day.weekday() < 5 and is_workday(day)


def normalize_market_datetime(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "trade_time must be a valid date and time"
            ) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(BEIJING_TZ).replace(tzinfo=None)
    return parsed


def _next_trading_day(day: date) -> date:
    cursor = day + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def _add_trading_days(day: date, count: int) -> date:
    cursor = day
    for _ in range(count):
        cursor = _next_trading_day(cursor)
    return cursor
