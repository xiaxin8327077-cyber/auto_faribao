from calendar import monthrange
from datetime import timedelta

from src.portfolio_confirmation import is_trading_day
from src.portfolio_models import SipFrequency


def normalize_sip_schedule(frequency, schedule_day):
    try:
        normalized = (
            frequency
            if isinstance(frequency, SipFrequency)
            else SipFrequency(str(frequency or "daily").strip().lower())
        )
    except ValueError as exc:
        raise ValueError("invalid SIP schedule") from exc

    if normalized is SipFrequency.DAILY:
        if schedule_day not in (None, ""):
            raise ValueError("invalid SIP schedule")
        return normalized, None

    if isinstance(schedule_day, bool):
        raise ValueError("invalid SIP schedule")
    try:
        day = int(schedule_day)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid SIP schedule") from exc
    limit = 5 if normalized is SipFrequency.WEEKLY else 31
    if str(day) != str(schedule_day).strip() or not 1 <= day <= limit:
        raise ValueError("invalid SIP schedule")
    return normalized, day


def _roll_forward(day):
    cursor = day
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def iter_sip_trade_dates(plan, through_date):
    frequency, schedule_day = normalize_sip_schedule(
        plan.frequency,
        plan.schedule_day,
    )
    if through_date < plan.start_date:
        return

    effective_dates = set()
    if frequency is SipFrequency.DAILY:
        cursor = plan.start_date
        while cursor <= through_date:
            if is_trading_day(cursor):
                effective_dates.add(cursor)
            cursor += timedelta(days=1)
    elif frequency is SipFrequency.WEEKLY:
        cursor = plan.start_date + timedelta(
            days=(schedule_day - plan.start_date.isoweekday()) % 7
        )
        while cursor <= through_date:
            effective = _roll_forward(cursor)
            if effective <= through_date:
                effective_dates.add(effective)
            cursor += timedelta(days=7)
    else:
        year = plan.start_date.year
        month = plan.start_date.month
        while True:
            month_start = plan.start_date.replace(
                year=year,
                month=month,
                day=1,
            )
            if month_start > through_date:
                break
            planned = month_start.replace(
                day=min(schedule_day, monthrange(year, month)[1])
            )
            if planned >= plan.start_date:
                effective = _roll_forward(planned)
                if effective <= through_date:
                    effective_dates.add(effective)
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1

    yield from sorted(effective_dates)


def is_sip_trade_date(plan, candidate):
    return candidate in iter_sip_trade_dates(plan, candidate)
