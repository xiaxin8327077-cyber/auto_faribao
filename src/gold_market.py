"""上金所黄金9999与沪金主连的交易时段、报价和分时适配。

夜盘属于下一交易日。周五夜盘延续到周六凌晨，周日晚上不开盘。
法定假日前的最后一个交易日晚上没有夜盘，节后第一个交易日只有日盘。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

GOLD_MARKETS = {
    "SGE": {
        "name": "黄金",
        "timezone": "Asia/Shanghai",
        "calendar": "CN",
        "night_start": "20:00",
        "night_end": "02:30",
        "day_segments": (("09:00", "15:30"),),
    },
    "SHFE": {
        "name": "黄金",
        "timezone": "Asia/Shanghai",
        "calendar": "CN",
        "night_start": "21:00",
        "night_end": "02:30",
        "day_segments": (("09:00", "10:15"), ("10:30", "11:30"), ("13:30", "15:00")),
    },
}

GOLD_INSTRUMENTS = (
    ("au9999", "黄金9999", "SGE", "118.AU9999"),
    ("shfe_au", "沪金主连", "SHFE", "113.aum"),
)

SINA_QUOTE_CODES = {"au9999": "gds_AU9999", "shfe_au": "nf_AU0"}
SINA_MINUTE_CODES = {"shfe_au": "AU0"}
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
SINA_QUOTE_URL = "https://hq.sinajs.cn/list="
SINA_MINUTE_URL = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_AU0=/InnerFuturesNewService.getFewMinLine"


def _clock(value):
    return time.fromisoformat(value)


def _combine(day, text):
    return datetime.combine(day, _clock(text), SHANGHAI)


def _next_trading_day(day, trading):
    cursor = day + timedelta(days=1)
    for _ in range(14):
        if trading(cursor):
            return cursor
        cursor += timedelta(days=1)
    return None


def night_trading_date(evening, trading):
    """Return the trading date that owns this evening's night session."""
    if not trading(evening):
        return None
    owner = _next_trading_day(evening, trading)
    if owner is None:
        return None
    cursor = evening + timedelta(days=1)
    while cursor < owner:
        if cursor.weekday() < 5:
            return None
        cursor += timedelta(days=1)
    return owner


def session_segments(market, trading_date, trading):
    """Absolute Shanghai intervals for one exchange trading date."""
    spec = GOLD_MARKETS[market]
    if not trading(trading_date):
        return []
    evening = trading_date - timedelta(days=1)
    while evening > trading_date - timedelta(days=8) and night_trading_date(evening, trading) != trading_date:
        evening -= timedelta(days=1)
    segments = []
    if night_trading_date(evening, trading) == trading_date:
        start = _combine(evening, spec["night_start"])
        end_day = evening + timedelta(days=1)
        segments.append((start, _combine(end_day, spec["night_end"])))
    for start_text, end_text in spec["day_segments"]:
        segments.append((_combine(trading_date, start_text), _combine(trading_date, end_text)))
    return segments


def segment_payload(market, trading_date, trading):
    return [
        {"start": start.isoformat(timespec="seconds"), "end": end.isoformat(timespec="seconds")}
        for start, end in session_segments(market, trading_date, trading)
    ]


def _contains(segments, moment):
    return any(start <= moment <= end for start, end in segments)


def trading_date_of(market, moment, trading):
    local = moment.astimezone(SHANGHAI)
    candidates = [local.date()]
    tonight = night_trading_date(local.date(), trading)
    if tonight is not None:
        candidates.append(tonight)
    if local.time() <= _clock("02:30"):
        carried = night_trading_date(local.date() - timedelta(days=1), trading)
        if carried is not None:
            candidates.append(carried)
    for candidate in candidates:
        if _contains(session_segments(market, candidate, trading), moment):
            return candidate
    return None


def _phase(spec, moment):
    clock = moment.time()
    if clock >= _clock(spec["night_start"]) or clock <= _clock(spec["night_end"]):
        return "night"
    if clock < _clock("12:00"):
        return "morning"
    return "afternoon"


def gold_slot(market, at, trading, known=True):
    """Same shape as a stock market slot. Inactive breaks are fetched once."""
    spec = GOLD_MARKETS[market]
    local = at.astimezone(SHANGHAI)
    owner = trading_date_of(market, local, trading)
    if owner is not None:
        phase = _phase(spec, local)
        return {"slot": f"{owner.isoformat()}:{phase}", "date": owner.isoformat(),
                "phase": phase, "active": True, "calendar_known": known}
    if trading(local.date()):
        day_segments = [(_combine(local.date(), start), _combine(local.date(), end))
                        for start, end in spec["day_segments"]]
        if day_segments and day_segments[0][0] <= local <= day_segments[-1][1]:
            return {"slot": f"{local.date().isoformat()}:break", "date": local.date().isoformat(),
                    "phase": "lunch", "active": False, "calendar_known": known}
        if _clock(spec["night_end"]) < local.time() < _clock("09:00"):
            return {"slot": f"{local.date().isoformat()}:pause", "date": local.date().isoformat(),
                    "phase": "closed", "active": False, "calendar_known": known}
        if local.time() > day_segments[-1][1].time() if day_segments else local.time() >= _clock("15:00"):
            return {"slot": f"{local.date().isoformat()}:close", "date": local.date().isoformat(),
                    "phase": "closed", "active": False, "calendar_known": known}
    cursor = local.date() - timedelta(days=1 if local.time() < _clock("09:00") else 0)
    for _ in range(14):
        if trading(cursor):
            return {"slot": f"{cursor.isoformat()}:close", "date": cursor.isoformat(),
                    "phase": "closed", "active": False, "calendar_known": known}
        cursor -= timedelta(days=1)
    return {"slot": f"{local.date().isoformat()}:close", "date": local.date().isoformat(),
            "phase": "closed", "active": False, "calendar_known": known}


def upcoming_boundaries(market, at, trading):
    """Future segment edges as UTC datetimes: (openings, transitions)."""
    local = at.astimezone(SHANGHAI)
    openings, transitions = [], []
    for offset in range(0, 12):
        day = local.date() + timedelta(days=offset)
        owner = _next_trading_day(day - timedelta(days=1), trading)
        dates = {day}
        if owner:
            dates.add(owner)
        for trading_date in sorted(dates):
            for start, end in session_segments(market, trading_date, trading):
                for edge, is_open in ((start, True), (end, False)):
                    if edge.astimezone(ZoneInfo("UTC")) > at:
                        moment = edge.astimezone(ZoneInfo("UTC"))
                        transitions.append(moment)
                        if is_open:
                            openings.append(moment)
        if openings:
            break
    return openings, transitions


def _number(value):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return None
    return number if number.is_finite() else None


def parse_sina_gold_quotes(text):
    """Sina hq strings for 沪金主连 and 上金所黄金9999, shaped like Eastmoney rows."""
    rows = []
    for line in text.splitlines():
        if '="' not in line:
            continue
        key, _, rest = line.partition('="')
        symbol = key.removeprefix("var hq_str_")
        parts = rest.rstrip('";').split(",")
        if symbol == "nf_AU0" and len(parts) > 17:
            price, previous = _number(parts[8]), _number(parts[10])
            clock, day = parts[1], parts[17]
            stamp = datetime.strptime(day + clock, "%Y-%m-%d%H%M%S").replace(tzinfo=SHANGHAI)
            code, market = "aum", 113
        elif symbol == "gds_AU9999" and len(parts) > 12:
            price, previous = _number(parts[0]), _number(parts[7])
            stamp = datetime.strptime(parts[12] + " " + parts[6], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
            code, market = "AU9999", 118
        else:
            continue
        if price is None or previous is None or previous == 0:
            continue
        change = price - previous
        rows.append({
            "f12": code, "f13": market, "f14": "黄金",
            "f2": format(price, "f"), "f3": format(change / previous * 100, "f"),
            "f4": format(change, "f"), "f124": int(stamp.timestamp()),
        })
    if not rows:
        raise ValueError("sina gold quotes are empty")
    return {"rc": 0, "data": {"diff": rows}}


def kline_trend_payload(payload, secid):
    """Eastmoney 1-minute kline uses klines, not the trends2 field."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("gold minute response has no data")
    market, code = secid.split(".", 1)
    if str(data.get("market")) != market or str(data.get("code")).lower() != code.lower():
        raise ValueError("gold minute security mismatch")
    points = []
    for row in data.get("klines") or []:
        moment, _, price = str(row).partition(",")
        if _number(price) is None or _number(price) <= 0:
            continue
        points.append(f"{moment},{format(_number(price), 'f')}")
    if not points:
        raise ValueError("gold minute response has no points")
    pre_close = data.get("prePrice")
    return {"rc": 0, "data": {"market": int(market), "code": code, "trends": points, "preClose": pre_close},
            "_trend_source": "东方财富分钟行情"}


def parse_sina_minute_payload(text, secid):
    match = re.search(r"(\[.*\])", text, re.S)
    if match is None:
        raise ValueError("sina gold minute response has no points")
    rows = json.loads(match.group(1))
    points = []
    for row in rows:
        moment = datetime.strptime(row["d"], "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M")
        price = _number(row.get("c"))
        if price is None or price <= 0:
            continue
        points.append(f"{moment},{format(price, 'f')}")
    if not points:
        raise ValueError("sina gold minute response has no points")
    market, code = secid.split(".", 1)
    return {"rc": 0, "data": {"market": int(market), "code": code, "trends": points, "preClose": None},
            "_trend_source": "新浪分钟行情"}
