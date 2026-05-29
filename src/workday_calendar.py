import json
import logging
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CALENDAR_PATH = Path(__file__).resolve().parent.parent / "config" / "mainland_workdays.json"


class WorkdayCalendar:
    def __init__(self, holidays_by_year: dict, makeup_workdays_by_year: dict, sources_by_year: dict):
        self.holidays_by_year = holidays_by_year
        self.makeup_workdays_by_year = makeup_workdays_by_year
        self.sources_by_year = sources_by_year

    @classmethod
    def from_file(cls, path=None):
        calendar_path = Path(path) if path else DEFAULT_CALENDAR_PATH
        try:
            raw = json.loads(calendar_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning(f"Workday calendar file not found: {calendar_path}")
            return cls({}, {}, {})
        except json.JSONDecodeError as e:
            logger.error(f"Invalid workday calendar JSON: {e}")
            return cls({}, {}, {})

        years = raw.get("years", {})
        holidays_by_year = {}
        makeup_workdays_by_year = {}
        sources_by_year = {}

        for year_text, rules in years.items():
            year = int(year_text)
            holidays = set()
            for d in rules.get("holidays", []):
                try:
                    holidays.add(date.fromisoformat(d))
                except ValueError:
                    pass
            makeup_workdays = set()
            for d in rules.get("makeup_workdays", []):
                try:
                    makeup_workdays.add(date.fromisoformat(d))
                except ValueError:
                    pass
            holidays_by_year[year] = frozenset(holidays)
            makeup_workdays_by_year[year] = frozenset(makeup_workdays)
            sources_by_year[year] = rules.get("source", "")

        return cls(holidays_by_year, makeup_workdays_by_year, sources_by_year)

    def is_workday(self, day: date) -> bool:
        year = day.year
        if year not in self.holidays_by_year:
            logger.warning(f"No calendar data for {year}, falling back to weekday check")
            return day.weekday() < 5
        if day in self.holidays_by_year[year]:
            return False
        if day in self.makeup_workdays_by_year[year]:
            return True
        return day.weekday() < 5

    def previous_workday(self, day: date) -> date:
        cursor = day - timedelta(days=1)
        while not self.is_workday(cursor):
            cursor -= timedelta(days=1)
        return cursor


_calendar_instance = None


def get_calendar(path=None) -> WorkdayCalendar:
    global _calendar_instance
    if _calendar_instance is None:
        _calendar_instance = WorkdayCalendar.from_file(path)
    return _calendar_instance


def is_workday(day: date = None, path=None) -> bool:
    if day is None:
        from src.beijing_time import today
        day = today()
    return get_calendar(path).is_workday(day)


def is_today_workday() -> bool:
    return is_workday()


def get_nearest_workday(day: date = None, path=None) -> date:
    """Get the nearest workday that is <= the given day.
    If the given day is a workday, return it.
    If not, return the previous workday.
    """
    if day is None:
        from src.beijing_time import today
        day = today()
    calendar = get_calendar(path)
    if calendar.is_workday(day):
        return day
    return calendar.previous_workday(day)
