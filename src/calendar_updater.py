"""从 GitHub 开源节假日数据自动更新本地日历文件。

数据来源: https://github.com/NateScarlet/holiday-cn
每年 12 月 1 日 scheduler 自动触发，或通过企业微信指令手动更新。
"""
import json
import logging
from datetime import date
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

HOLIDAY_API = "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
CALENDAR_FILE = Path(__file__).resolve().parent.parent / "config" / "mainland_workdays.json"


def fetch_holidays_for_year(year: int) -> dict | None:
    """从 holiday-cn 仓库获取指定年份节假日。返回 calendar 格式或 None。"""
    url = HOLIDAY_API.format(year=year)
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 404:
            logger.warning(f"Holiday data not yet available for {year}")
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch holiday data: {e}")
        return None

    days = data.get("days", [])
    if not days:
        return None

    holidays = []
    makeup_workdays = []
    for d in days:
        if not d.get("date"):
            continue
        if d.get("isOffDay"):
            holidays.append(d["date"])
        else:
            makeup_workdays.append(d["date"])

    return {
        "holidays": holidays,
        "makeup_workdays": makeup_workdays,
        "source": url,
    }


def update_calendar(year: int = None) -> tuple[bool, str]:
    """更新本地日历文件。成功返回 (True, info)，失败返回 (False, reason)。"""
    if year is None:
        year = date.today().year + 1

    logger.info(f"Fetching holiday calendar for {year}...")
    rules = fetch_holidays_for_year(year)
    if not rules:
        return False, f"无法获取 {year} 年节假日数据（可能尚未发布）"

    try:
        if CALENDAR_FILE.exists():
            data = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
        else:
            data = {"years": {}}
    except Exception as e:
        return False, f"读取现有日历文件失败: {e}"

    data.setdefault("years", {})[str(year)] = rules

    CALENDAR_FILE.parent.mkdir(parents=True, exist_ok=True)
    CALENDAR_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Updated calendar with {year} holidays ({len(rules['holidays'])} off days, {len(rules['makeup_workdays'])} makeup)")
    return True, f"✅ {year} 年日历已更新\n假期 {len(rules['holidays'])} 天，调休上班 {len(rules['makeup_workdays'])} 天\n来源：GitHub holiday-cn"
