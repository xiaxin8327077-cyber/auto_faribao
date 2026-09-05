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


def first_sip_schedule_date(floor, frequency, schedule_day):
    """First nominal date, using month-end when the selected day is absent."""
    frequency, schedule_day = normalize_sip_schedule(frequency, schedule_day)
    if frequency is SipFrequency.WEEKLY:
        planned = floor + timedelta(days=(schedule_day - floor.isoweekday()) % 7)
    elif frequency is SipFrequency.MONTHLY:
        planned = floor.replace(day=min(schedule_day, monthrange(floor.year, floor.month)[1]))
        if planned < floor:
            next_month = (floor.replace(day=28) + timedelta(days=4)).replace(day=1)
            planned = next_month.replace(day=min(schedule_day, monthrange(next_month.year, next_month.month)[1]))
    else:
        planned = floor
    return planned


def first_sip_trade_date(floor, frequency, schedule_day):
    """Return the first scheduled deduction on or after the supplied boundary."""
    return _roll_forward(first_sip_schedule_date(floor, frequency, schedule_day))


def _schedule_floor(plan):
    """周期枚举下界：取"用户配置的生效边界 start_date"与"周期修改
    生效日"中较晚者，避免下界早于 start_date 而越界生成执行日。"""
    effective = getattr(plan, "schedule_effective_date", None)
    if effective:
        return max(plan.start_date, effective)
    return plan.start_date


def iter_sip_trade_dates(plan, through_date):
    frequency, schedule_day = normalize_sip_schedule(
        plan.frequency,
        plan.schedule_day,
    )
    floor = _schedule_floor(plan)
    if through_date < floor:
        return

    effective_dates = set()
    # An automatically calculated start may be rolled past its nominal schedule
    # day. Keep that first deduction, including month-end rolls into next month.
    anchor = getattr(plan, "schedule_effective_date", None)
    if (
        frequency is not SipFrequency.DAILY
        and anchor is not None
        and anchor < plan.start_date
        and first_sip_trade_date(anchor, frequency, schedule_day) == plan.start_date
    ):
        effective_dates.add(plan.start_date)
    if frequency is SipFrequency.DAILY:
        cursor = floor
        while cursor <= through_date:
            if is_trading_day(cursor):
                effective_dates.add(cursor)
            cursor += timedelta(days=1)
    elif frequency is SipFrequency.WEEKLY:
        cursor = floor + timedelta(
            days=(schedule_day - floor.isoweekday()) % 7
        )
        while cursor <= through_date:
            effective = _roll_forward(cursor)
            if effective <= through_date:
                effective_dates.add(effective)
            cursor += timedelta(days=7)
    else:
        year = floor.year
        month = floor.month
        while True:
            month_start = floor.replace(
                year=year,
                month=month,
                day=1,
            )
            if month_start > through_date:
                break
            planned = month_start.replace(
                day=min(schedule_day, monthrange(year, month)[1])
            )
            if planned >= floor:
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
