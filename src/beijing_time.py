from datetime import datetime, date, timezone, timedelta

TZ_CN = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(TZ_CN)


def today() -> date:
    return now().date()


def today_str(fmt: str = "%Y-%m-%d") -> str:
    return now().strftime(fmt)
