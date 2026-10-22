"""债市晴雨表数据源：中债到期收益率曲线的逐日变动。

数据来自中国债券信息网（yield.chinabond.com.cn → 正确域名见下）公开接口，
覆盖南方基金晴雨表所用的六条曲线与期限：

- 利率债：国债 / 国开 / 进出口行 × 1/3/5/7/10 年
- 信用债：中短期票据 AAA / AA+ / AA × 1月/3月/6月/1/3/5 年

颜色口径（本系统定义，页面需注明）：与上一交易日相比，
收益率下行 → red（债市走强），上行 → green，变动不超过 0.05bp → flat。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from decimal import Decimal
import threading
import time

import requests

from src.beijing_time import now as beijing_now

_BASE_URL = "https://yield.chinabond.com.cn"
_SEARCH_URL = _BASE_URL + "/cbweb-mn/yc/searchYc"
_MAIN_URL = _BASE_URL + "/cbweb-mn/yield_main"
_TIMEOUT = (10, 25)
_ATTEMPTS = 3
# 变动绝对值不超过 0.0005（0.05bp）视为持平，避免微小抖动上色。
_FLAT_THRESHOLD = Decimal("0.0005")
_CACHE_TTL_SECONDS = 1800
# 仅银行间交易时段（工作日 9:00-17:30）内自动刷新；其余时间沿用缓存。
_TRADING_START = clock_time(9, 0)
_TRADING_END = clock_time(17, 30)

_CURVE_IDS = {
    "rate_guozhai": "2c9081e50a2f9606010a3068cae70001",
    "rate_guokai": "8a8b2ca037a7ca910137bfaa94fa5057",
    "rate_jinchukou": "8a8b2ca0567e033b01567ea9c1d96af8",
    "credit_aaa": "2c9081880fa9d507010fb8505b393fe7",
    "credit_aa_plus": "2c90818811d3f4ae0111e8b341f560e0",
    "credit_aa": "2c9081e50a2f9606010a30acdae40176",
}
_CURVE_IDS_PARAM = ",".join(_CURVE_IDS.values()) + ","

_RATE_TENORS = ((1.0, "1年"), (3.0, "3年"), (5.0, "5年"), (7.0, "7年"), (10.0, "10年"))
_CREDIT_TENORS = (
    (0.0833, "1月"),
    (0.25, "3月"),
    (0.5, "6月"),
    (1.0, "1年"),
    (3.0, "3年"),
    (5.0, "5年"),
)
_RATE_ROW_LABELS = {
    "rate_guozhai": "国债",
    "rate_guokai": "国开",
    "rate_jinchukou": "口行",
}
_CREDIT_ROW_LABELS = {
    "credit_aaa": "AAA",
    "credit_aaa_plus": "AAA+",
    "credit_aa_plus": "AA+",
    "credit_aa": "AA",
}

_cache_lock = threading.Lock()
_cache = {}


def _curve_key(name: str) -> str | None:
    if "AAA＋" in name:
        return "credit_aaa_plus"
    if "AAA" in name:
        return "credit_aaa"
    if "AA＋" in name:
        return "credit_aa_plus"
    if "AA" in name and "AAA" not in name:
        return "credit_aa"
    if "国债" in name:
        return "rate_guozhai"
    if "国开" in name:
        return "rate_guokai"
    if "进出口" in name:
        return "rate_jinchukou"
    return None


def _tenor_key(value: float) -> float:
    return round(float(value), 4)


def parse_curves(payload) -> dict[str, dict[float, Decimal]]:
    """把接口返回的曲线数组解析为 {内部键: {期限: 收益率}}。"""
    curves: dict[str, dict[float, Decimal]] = {}
    for item in payload or []:
        key = _curve_key(str(item.get("ycDefName") or ""))
        if key is None:
            continue
        series = {}
        for point in item.get("seriesData") or []:
            try:
                tenor = _tenor_key(point[0])
                series[tenor] = Decimal(str(point[1]))
            except (IndexError, TypeError, ValueError):
                continue
        curves[key] = series
    return curves


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; auto-faribao/1.0)",
            "Referer": _MAIN_URL,
        }
    )
    session.get(_MAIN_URL, timeout=_TIMEOUT)
    return session


def fetch_curves(day: date, session: requests.Session | None = None) -> dict[str, dict[float, Decimal]]:
    """抓取某一交易日的六条曲线；返回空 dict 表示该日无数据。"""
    url = (
        f"{_SEARCH_URL}?xyzSelect=txy&&workTimes={day.isoformat()}&&dxbj=0"
        f"&&qxll=0,&&yqqxN=N&&yqqxK=K&&ycDefIds={_CURVE_IDS_PARAM}"
        "&&wrjxCBFlag=0&&locale="
    )
    own_session = session is None
    client = session or _session()
    last_error = None
    try:
        for attempt in range(_ATTEMPTS):
            try:
                response = client.post(url, timeout=_TIMEOUT)
                response.raise_for_status()
                return parse_curves(response.json())
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt + 1 < _ATTEMPTS:
                    time.sleep(0.5 * (attempt + 1))
        raise last_error
    finally:
        if own_session:
            client.close()


def _previous_curve_date(day: date, fetch) -> tuple[date, dict] | tuple[None, dict]:
    cursor = day - timedelta(days=1)
    for _ in range(14):
        while cursor.weekday() >= 5:
            cursor -= timedelta(days=1)
        curves = fetch(cursor)
        if curves:
            return cursor, curves
        cursor -= timedelta(days=1)
    return None, {}


def _color(current: Decimal | None, previous: Decimal | None) -> str:
    if current is None or previous is None:
        return "flat"
    change = current - previous
    if change < -_FLAT_THRESHOLD:
        return "red"
    if change > _FLAT_THRESHOLD:
        return "green"
    return "flat"


def _weather(counts: dict[str, int]) -> str:
    colored = counts["red"] + counts["green"]
    if colored == 0:
        return "flat"
    ratio = counts["red"] / colored
    if ratio >= 0.8:
        return "sunny"
    if ratio >= 0.6:
        return "sun_cloud"
    if ratio >= 0.4:
        return "cloud"
    return "rain"


def _rows(curves, prev_curves, keys, tenors, labels):
    rows = []
    counts = {"red": 0, "green": 0, "flat": 0}
    for key in keys:
        if key not in curves:
            continue
        cells = []
        for tenor, label in tenors:
            current = curves[key].get(tenor)
            previous = (prev_curves.get(key) or {}).get(tenor)
            color = _color(current, previous)
            counts[color] += 1
            cells.append(
                {
                    "tenor": label,
                    "yield": str(current) if current is not None else None,
                    "change": (
                        str(current - previous)
                        if current is not None and previous is not None
                        else None
                    ),
                    "color": color,
                }
            )
        rows.append({"label": labels[key], "cells": cells})
    return rows, {"counts": counts, "weather": _weather(counts)}


def build_barometer(day: date, *, fetch=fetch_curves) -> dict:
    """生成指定交易日的晴雨表；数据缺失时返回 available=False。"""
    curves = fetch(day)
    if not curves:
        return {"as_of": day.isoformat(), "available": False}
    previous_day, prev_curves = _previous_curve_date(day, fetch=fetch)
    rate_rows, rate_summary = _rows(curves, prev_curves, _RATE_ROW_LABELS.keys(), _RATE_TENORS, _RATE_ROW_LABELS)
    credit_rows, credit_summary = _rows(curves, prev_curves, _CREDIT_ROW_LABELS.keys(), _CREDIT_TENORS, _CREDIT_ROW_LABELS)
    return {
        "as_of": day.isoformat(),
        "previous_as_of": previous_day.isoformat() if previous_day else None,
        "available": True,
        "source": "中国债券信息网 中债到期收益率曲线",
        "method": "与前一日相比：收益率下行=red，上行=green，变动≤0.05bp=flat（本系统口径）",
        "rate": {"rows": rate_rows, **rate_summary},
        "credit": {"rows": credit_rows, **credit_summary},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _in_trading_window(now: datetime) -> bool:
    """工作日 9:00-17:30（北京时间）内才自动刷新。"""
    return now.weekday() < 5 and _TRADING_START <= now.time() <= _TRADING_END


def latest_barometer(
    day: date | None = None,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> dict:
    """带缓存的入口：交易时段内每 30 分钟刷新，非交易时段沿用缓存。

    *force* 供页面“更新”按钮使用，随时强制重新抓取。
    周末/节假日自动回退到最近有数据的交易日。
    """
    current = now or beijing_now().replace(tzinfo=None)
    target = day or current.date()
    key = target.isoformat()
    started = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and not force:
            fresh = started - cached[0] < _CACHE_TTL_SECONDS
            if fresh or not _in_trading_window(current):
                return cached[1]
    cursor = target
    for _ in range(10):
        while cursor.weekday() >= 5:
            cursor -= timedelta(days=1)
        result = build_barometer(cursor)
        if result.get("available"):
            break
        cursor -= timedelta(days=1)
    with _cache_lock:
        _cache[key] = (started, result)
        if len(_cache) > 8:
            _cache.pop(next(iter(_cache)))
    return result


if __name__ == "__main__":
    import json

    print(json.dumps(latest_barometer(), ensure_ascii=False, indent=2))