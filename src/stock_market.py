"""东方财富公开指数与一级行业行情的独立、有限容量缓存。

启动时调用 ``ensure_stock_market_schema``；后台用普通（未开启写事务的）
portfolio connection 调用 refresh。HTTP 在数据库事务和发布锁之外执行，
read 只查询缓存。每个市场和行业只保留最近成功数据及最近采集状态。

HK/US 默认仅按工作日和常规时段安排采集，并不据此断言真实开市；可通过
trading_calendars={"HK": date_to_bool, "US": date_to_bool} 注入交易日历。
"""
from __future__ import annotations

from datetime import date, datetime, time as clock_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

import requests

from src.portfolio_confirmation import MissingTradingCalendarError, is_trading_day
from src.stock_market_trends import attach_intraday_trend, tencent_trend_payload, yahoo_trend_payload


_SOURCE = "公开市场行情"
_SOURCE_URL = "https://quote.eastmoney.com/center/"
_INDICES_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
_INDUSTRIES_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_TRENDS_URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
_FIELDS = "f1,f2,f3,f4,f12,f13,f14,f124"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
_TTL_SECONDS = 30
# Leave room for the short SQLite publication transaction within the 30s caller budget.
_NETWORK_BUDGET_SECONDS = 25.0
_CACHE_LOCK = threading.Lock()
_LAST_ATTEMPT_ORDER = 0
_MARKETS = {
    "CN": {"name": "A股", "timezone": "Asia/Shanghai", "morning_end": "11:30", "close": "15:00"},
    "HK": {"name": "港股", "timezone": "Asia/Hong_Kong", "morning_end": "12:00", "close": "16:00"},
    "US": {"name": "美股", "timezone": "America/New_York", "morning_end": None, "close": "16:00"},
}
_INDICES = (
    ("sse", "上证指数", "CN", "1.000001"),
    ("szse", "深证成指", "CN", "0.399001"),
    ("chinext", "创业板指", "CN", "0.399006"),
    ("star50", "科创50", "CN", "1.000688"),
    ("bse50", "北证50", "CN", "0.899050"),
    ("hsi", "恒生指数", "HK", "100.HSI"),
    ("hstech", "恒生科技", "HK", "124.HSTECH"),
    ("djia", "道琼斯工业", "US", "100.DJIA"),
    # In this particular Eastmoney endpoint, 100.NDX is the Nasdaq Composite.
    ("nasdaq", "纳斯达克综合", "US", "100.NDX"),
    ("sp500", "标普500", "US", "100.SPX"),
)
_TENCENT_URL = "https://qt.gtimg.cn/q="
_TENCENT_CODES = {
    "sh000001": (1, "000001"), "sz399001": (0, "399001"), "sz399006": (0, "399006"),
    "sh000688": (1, "000688"), "bj899050": (0, "899050"), "hkHSI": (100, "HSI"),
    "hkHSTECH": (124, "HSTECH"), "usDJI": (100, "DJIA"), "usIXIC": (100, "NDX"),
    "usINX": (100, "SPX"),
}


def _tencent_indices(client, deadline=None):
    """备用源：腾讯行情。转成东财同结构行供解析管道复用，东财失败时兜底指数。"""
    remaining = deadline - time.monotonic() if deadline else 8.0
    if remaining <= 0:
        raise requests.Timeout('stock-market refresh deadline exceeded')
    response = client.get(_TENCENT_URL + ",".join(_TENCENT_CODES), params={},
                          headers={"User-Agent": "Mozilla/5.0"},
                          timeout=(min(3.0, remaining / 2), min(5.0, remaining / 2)))
    response.raise_for_status()
    rows = []
    for line in getattr(response, "text", "").splitlines():
        if '="' not in line:
            continue
        variable, _, rest = line.partition('="')
        key = variable[2:] if variable.startswith("v_") else ""
        if key not in _TENCENT_CODES:
            continue
        parts = rest.rstrip('";').split("~")
        if len(parts) < 33:
            continue
        f13, f12 = _TENCENT_CODES[key]
        market = 'US' if key.startswith('us') else 'HK' if key.startswith('hk') else 'CN'
        stamp = None
        for pattern in ('%Y%m%d%H%M%S', '%Y/%m/%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
            try:
                stamp = datetime.strptime(parts[30], pattern).replace(tzinfo=ZoneInfo(_MARKETS[market]['timezone']))
                break
            except ValueError:
                continue
        if stamp is None:
            continue
        rows.append({"f12": f12, "f13": f13, "f14": parts[1],
                     "f2": parts[3], "f3": parts[32], "f4": parts[31],
                     "f124": int(stamp.timestamp())})
    if not rows:
        raise ValueError("tencent fallback returned no quotes")
    return {"rc": 0, "data": {"diff": rows}}
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stock_market_cache (
    component TEXT PRIMARY KEY,
    payload TEXT,
    fetched_at TEXT,
    attempted_at TEXT NOT NULL,
    attempt_order INTEGER NOT NULL,
    refresh_error TEXT,
    session_slot TEXT NOT NULL
)
"""


def ensure_stock_market_schema(conn):
    """Create only the independent stock cache; leave transaction ownership to caller."""
    conn.execute(_SCHEMA_SQL)


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value))
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return value.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat(timespec="seconds")


def _parse_datetime(value):
    try:
        return _now(datetime.fromisoformat(value)) if value else None
    except (TypeError, ValueError):
        return None


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
        return format(number, "f") if number.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _trading_day(market, day, calendars):
    if day.weekday() >= 5:
        return False, market == "CN" or market in calendars
    if market in calendars:
        return bool(calendars[market](day)), True
    if market == "CN":
        try:
            return is_trading_day(day), True
        except MissingTradingCalendarError:
            return True, False
    return True, False


def _market_slot(market, at, calendars=None):
    calendars = calendars or {}
    spec = _MARKETS[market]
    local = at.astimezone(ZoneInfo(spec["timezone"]))
    day = local.date()
    trading, known = _trading_day(market, day, calendars)
    local_time = local.time()
    if not trading or local_time < clock_time(9, 30):
        # The previous session id survives midnight/weekends, so an already collected
        # close is not repeatedly fetched on each new non-trading calendar date.
        cursor = day - timedelta(days=1)
        for _ in range(370):
            if _trading_day(market, cursor, calendars)[0]:
                break
            cursor -= timedelta(days=1)
        return {"slot": f"{cursor.isoformat()}:close", "date": cursor.isoformat(),
                "phase": "closed", "active": False, "calendar_known": known}
    if local_time >= clock_time.fromisoformat(spec["close"]):
        phase, active = "close", False
    elif spec["morning_end"] and clock_time.fromisoformat(spec["morning_end"]) <= local_time < clock_time(13):
        phase, active = "lunch", False
    else:
        phase, active = ("afternoon" if local_time >= clock_time(13) else "morning"), True
    return {"slot": f"{day.isoformat()}:{phase}", "date": day.isoformat(),
            "phase": phase, "active": active, "calendar_known": known}


def _is_due(status, slot, at, force=False):
    if not status or not status.get("attempted_at"):
        return True  # A single bootstrap is allowed outside collection windows.
    if not slot["active"]:
        return status.get("session_slot") != slot["slot"]
    if status.get("session_slot") != slot["slot"]:
        return True
    attempted = _parse_datetime(status.get("attempted_at"))
    return force or attempted is None or (at - attempted).total_seconds() >= _TTL_SECONDS


def stock_market_due(now=None, snapshot=None, *, trading_calendars=None) -> bool:
    """Whether any market needs bootstrap, a scheduled-window update, or one close check."""
    at = _now(now)
    if snapshot is None:
        return True
    for market in _MARKETS:
        slot = _market_slot(market, at, trading_calendars)
        if _is_due(snapshot.get("indices_status", {}).get(market), slot, at):
            return True
        if any(item.get('market_id') == market and item.get('available') and 'trend' not in item
               for item in snapshot.get('indices', [])):
            return True
    return _is_due(snapshot.get("industries"), _market_slot("CN", at, trading_calendars), at)


def stock_market_session(now=None, *, trading_calendars=None) -> dict:
    """供浏览器休眠至下次开盘；沿用采集交易日历及各市场时区。"""
    at = _now(now)
    trading_calendars = trading_calendars or {}
    active = any(_market_slot(market, at, trading_calendars)['active'] for market in _MARKETS)
    openings, transitions = [], []
    for market, spec in _MARKETS.items():
        zone = ZoneInfo(spec['timezone'])
        day = at.astimezone(zone).date()
        for offset in range(370):
            candidate_day = day + timedelta(days=offset)
            if not _trading_day(market, candidate_day, trading_calendars)[0]:
                continue
            clocks = [('09:30', True), (spec['close'], False)]
            if spec['morning_end']:
                clocks += [(spec['morning_end'], False), ('13:00', True)]
            future_openings = []
            for text, is_open in clocks:
                candidate = datetime.combine(candidate_day, clock_time.fromisoformat(text), zone).astimezone(timezone.utc)
                if candidate > at:
                    transitions.append(candidate)
                    if is_open:
                        future_openings.append(candidate)
            if future_openings:
                openings.extend(future_openings)
                break
    return {'as_of': _iso(at), 'any_active': active,
            'next_open_at': _iso(min(openings)) if openings else None,
            'next_transition_at': _iso(min(transitions)) if transitions else None}


def _rows(payload):
    if not isinstance(payload, dict) or payload.get("rc") not in (None, 0):
        raise ValueError("public quote response rejected")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("public quote response has no data")
    rows = data.get("diff")
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list) or not rows:
        raise ValueError("public quote response has no quotes")
    return [row for row in rows if isinstance(row, dict)]


def _source_time(value, timezone_name):
    number = _decimal(value)
    if number is None:
        return None
    try:
        seconds = Decimal(number)
        if seconds <= 0:
            return None
        return datetime.fromtimestamp(float(seconds), ZoneInfo(timezone_name))
    except (ValueError, OverflowError, OSError):
        return None


def _quote(raw, *, key, name, market, secid):
    zone = _MARKETS[market]["timezone"]
    stamp = _source_time(raw.get("f124"), zone)
    price = _decimal(raw.get("f2"))
    pct = _decimal(raw.get("f3"))
    return {
        "key": key, "name": name, "market": _MARKETS[market]["name"], "market_id": market,
        "timezone": zone, "secid": secid, "quote_date": stamp.date().isoformat() if stamp else None,
        "quote_time": _iso(stamp) if stamp else None,
        "source_updated_at": _iso(stamp) if stamp else None,
        "price": price, "change_pct": pct, "change_points": _decimal(raw.get("f4")),
        "available": price is not None and pct is not None and stamp is not None,
    }


def _parse_indices(payload, markets):
    raw = {}
    for row in _rows(payload):
        secid = f"{row.get('f13')}.{row.get('f12')}"
        old = raw.get(secid)
        stamp = _source_time(row.get("f124"), "UTC")
        previous = _source_time(old.get("f124"), "UTC") if old else None
        if old is None or (stamp is not None and (previous is None or stamp > previous)):
            raw[secid] = row
    groups = {}
    for market in markets:
        groups[market] = [_quote(raw.get(secid, {}), key=key, name=name, market=kind, secid=secid)
                          for key, name, kind, secid in _INDICES if kind == market]
    return groups


def _parse_industries(payload):
    raw = {}
    for row in _rows(payload):
        code = str(row.get("f12") or "").strip()
        if not code:
            continue
        current = raw.get(code)
        stamp = _source_time(row.get("f124"), "Asia/Shanghai")
        previous = _source_time(current.get("f124"), "Asia/Shanghai") if current else None
        if current is None or (stamp is not None and (previous is None or stamp > previous)):
            raw[code] = row
    items = [_quote(row, key=code, name=str(row.get("f14") or code), market="CN", secid=f"90.{code}")
             for code, row in raw.items()]
    valid = [item for item in items if item["available"]]
    if not valid:
        raise ValueError("no usable primary-industry quotes")
    quote_date = max(item["quote_date"] for item in valid)
    current = [item for item in valid if item["quote_date"] == quote_date]
    positive = [item for item in current if Decimal(item["change_pct"]) > 0]
    negative = [item for item in current if Decimal(item["change_pct"]) < 0]
    return {
        "available": True, "classification": "东方财富一级行业", "count": len(items),
        "items": items,
        "leaders": sorted(positive, key=lambda row: (-Decimal(row["change_pct"]), row["key"]))[:5],
        "laggards": sorted(negative, key=lambda row: (Decimal(row["change_pct"]), row["key"]))[:5],
        "quote_date": quote_date, "source_updated_at": max(item["source_updated_at"] for item in current),
    }


def _read_records(conn):
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT component,payload,fetched_at,attempted_at,attempt_order,refresh_error,session_slot"
            " FROM stock_market_cache"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {}
        raise
    records = {}
    for row in rows:
        try:
            payload = json.loads(row[1]) if row[1] else None
        except (TypeError, ValueError):
            payload = None
        records[row[0]] = {"payload": payload, "fetched_at": row[2], "attempted_at": row[3],
                           "attempt_order": row[4], "refresh_error": row[5], "session_slot": row[6]}
    return records


def _status(record, slot, at):
    record = record or {}
    fetched = _parse_datetime(record.get("fetched_at"))
    age = max(0, int((at - fetched).total_seconds())) if fetched else None
    return {"fetched_at": record.get("fetched_at"), "attempted_at": record.get("attempted_at"),
            "refresh_error": record.get("refresh_error"), "session_slot": record.get("session_slot"),
            "schedule_slot": slot["slot"], "collection_phase": slot["phase"],
            "calendar_known": slot["calendar_known"], "cache_age_seconds": age,
            "stale": age is None or age >= _TTL_SECONDS or bool(record.get("refresh_error")),
            "refresh_due": _is_due(record, slot, at)}


def _decorate_quote(quote, status, slot, at):
    result = {**quote, "fetched_at": status["fetched_at"], "refresh_error": status["refresh_error"]}
    stamp = _parse_datetime(quote.get("source_updated_at"))
    delay = max(0, int((at - stamp).total_seconds())) if stamp else None
    result["source_age_seconds"] = delay
    result["source_delayed"] = bool(slot["active"] and (delay is None or delay > 15 * 60))
    result["stale"] = (status["stale"] or not quote.get("available") or result["source_delayed"]
                       or bool(quote.get("quote_date") and quote["quote_date"] < slot["date"]))
    return result


def _snapshot(records, at, calendars=None):
    if not records:
        return None
    indices = []
    statuses = {}
    for market in _MARKETS:
        slot = _market_slot(market, at, calendars)
        record = records.get(market)
        status = statuses[market] = _status(record, slot, at)
        quotes = (record or {}).get("payload") or []
        by_key = {quote["key"]: quote for quote in quotes if isinstance(quote, dict)}
        for key, name, kind, secid in _INDICES:
            if kind == market:
                quote = by_key.get(key) or _quote({}, key=key, name=name, market=kind, secid=secid)
                indices.append(_decorate_quote(quote, status, slot, at))
    sector_slot = _market_slot("CN", at, calendars)
    sector_record = records.get("industries") or {}
    industries = {"available": False, "items": [], "leaders": [], "laggards": [],
                  "quote_date": None, "source_updated_at": None,
                  **(sector_record.get("payload") or {}), **_status(sector_record, sector_slot, at)}
    for field in ("items", "leaders", "laggards"):
        industries[field] = [_decorate_quote(item, industries, sector_slot, at) for item in industries[field]]
    industries["stale"] = (industries["stale"] or not industries["available"]
                           or bool(industries["quote_date"] and industries["quote_date"] < sector_slot["date"]))
    errors = [f"{name}: {record['refresh_error']}" for name, record in records.items() if record.get("refresh_error")]
    fetched = [record["fetched_at"] for record in records.values() if record.get("fetched_at")]
    attempted = [record["attempted_at"] for record in records.values() if record.get("attempted_at")]
    return {
        "available": any(item["available"] for item in indices) or industries["available"],
        "source": _SOURCE, "source_url": _SOURCE_URL,
        "fetched_at": max(fetched) if fetched else None,
        "attempted_at": max(attempted) if attempted else None,
        "stale": any(item["stale"] for item in indices) or industries["stale"],
        "refresh_error": "; ".join(errors) or None,
        "indices": indices, "indices_status": statuses, "industries": industries,
        "change_unit": "%", "cache_ttl_seconds": _TTL_SECONDS,
        "live_session": stock_market_session(at, trading_calendars=calendars),
    }


def read_stock_market(conn, now=None) -> dict | None:
    """Return cache only, including explicit expiry; never DDL, writes, or HTTP."""
    return _snapshot(_read_records(conn), _now(now))


def _request(client, url, params, deadline, *, trend=False):
    last_error = None
    for _ in range(1 if trend else 2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout("stock-market refresh deadline exceeded")
        try:
            response = client.get(url, params=params, headers=_HEADERS,
                                  timeout=(min(3.0, remaining / 2), min(5.0, remaining / 2)))
            response.raise_for_status()
            payload = response.json()
            if trend:
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict) or not payload['data'].get('trends'):
                    raise ValueError('intraday trend response has no points')
            else:
                _rows(payload)
            return payload
        except (requests.RequestException, ValueError, TypeError) as exc:
            last_error = exc
    raise last_error


def _fallback_trend(client, key, deadline):
    _, _, market, secid = next(row for row in _INDICES if row[0] == key)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise requests.Timeout('stock-market refresh deadline exceeded')
    timeout = (min(3.0, remaining / 2), min(5.0, remaining / 2))
    if market == 'US':
        symbol = {'djia': '^DJI', 'nasdaq': '^IXIC', 'sp500': '^GSPC'}[key]
        response = client.get('https://query1.finance.yahoo.com/v8/finance/chart/' + requests.utils.quote(symbol, safe=''),
                              params={'interval': '1m', 'range': '1d'}, headers={'User-Agent': 'Mozilla/5.0'}, timeout=timeout)
        response.raise_for_status()
        return yahoo_trend_payload(response.json(), symbol=symbol, secid=secid)
    code = next(code for code, (market_id, ticker) in _TENCENT_CODES.items() if f'{market_id}.{ticker}' == secid)
    response = client.get('https://web.ifzq.gtimg.cn/appstock/app/minute/query', params={'code': code},
                          headers={'User-Agent': 'Mozilla/5.0'}, timeout=timeout)
    response.raise_for_status()
    return tencent_trend_payload(response.json(), code=code, secid=secid, market=market)


def _fetch(markets, industries_due, session):
    """Transport threads never receive a connection or publish after the caller returns."""
    deadline = time.monotonic() + _NETWORK_BUDGET_SECONDS
    outcomes = {}
    result_lock = threading.Lock()
    jobs = []
    if markets:
        params = {"fltt": 2, "fields": _FIELDS,
                  "secids": ",".join(secid for _, _, market, secid in _INDICES if market in markets)}
        jobs.append(("indices", _INDICES_URL, params))
        for key, _, market, secid in _INDICES:
            if market in markets:
                jobs.append((f'trend:{key}', _TRENDS_URL,
                             {'secid': secid, 'fields1': 'f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11',
                              'fields2': 'f51,f53,f54,f55,f56,f57,f58', 'ndays': 1, 'iscr': 0, 'iscca': 0}))
    if industries_due:
        jobs.append(("industries", _INDUSTRIES_URL,
                     {"pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                      "fid": "f3", "fs": "m:90+s:2+f:!50", "fields": _FIELDS}))

    def run(name, url, params):
        client = session
        try:
            client = client if client is not None else requests.Session()
            result = _request(client, url, params, deadline, trend=name.startswith('trend:'))
            if name == "industries":
                collected = _rows(result)
                total = int(result["data"].get("total", len(collected)))
                page = params["pn"]
                while len(collected) < total:
                    page += 1
                    next_page = _request(client, url, {**params, "pn": page}, deadline)
                    collected.extend(_rows(next_page))
                result = {**result, "data": {**result["data"], "diff": collected}}
            outcome = (result, None)
        except Exception as exc:  # Each public source can fail independently.
            if name == "indices":
                try:
                    outcome = (_tencent_indices(client, deadline), None)
                except Exception:
                    outcome = (None, f"{type(exc).__name__}: {str(exc)[:180]}")
            elif name.startswith('trend:'):
                try:
                    outcome = (_fallback_trend(client, name.split(':', 1)[1], deadline), None)
                except Exception:
                    outcome = (None, f"{type(exc).__name__}: {str(exc)[:180]}")
            else:
                outcome = (None, f"{type(exc).__name__}: {str(exc)[:180]}")
        finally:
            if session is None and client is not None:
                client.close()
        with result_lock:
            outcomes[name] = outcome

    workers = []
    for name, url, params in jobs:
        worker = threading.Thread(target=run, args=(name, url, params), name=f"stock-market-{name}", daemon=True)
        worker.start()
        workers.append(worker)
    for worker in workers:
        worker.join(max(0, deadline - time.monotonic()))
    with result_lock:
        # Copying detaches the accepted outcomes from any late network completion.
        return {name: outcomes.get(name, (None, "Timeout: stock-market refresh deadline exceeded"))
                for name, _, _ in jobs}


def _regressed(previous, candidate, component):
    if not previous:
        return False
    old_items = previous.get("items", []) if component == "industries" else previous
    new_items = candidate.get("items", []) if component == "industries" else candidate
    old_by_key = {item["key"]: item for item in old_items}
    for item in new_items:
        old = old_by_key.get(item["key"])
        old_time = _parse_datetime((old or {}).get("source_updated_at"))
        new_time = _parse_datetime(item.get("source_updated_at"))
        if old_time and new_time and new_time < old_time:
            return True
    return False


def _publish(conn, updates, attempt_order, at):
    # Callers own any ledger transaction. Refresh must not open network requests
    # while holding one, and this transaction writes only stock_market_cache.
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.execute("PRAGMA busy_timeout=750")
    try:
        with _CACHE_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = _read_records(conn)
                for component, (candidate, error, slot) in updates.items():
                    old = current.get(component) or {}
                    if old.get("attempt_order", -1) >= attempt_order:
                        continue
                    if candidate is not None and _regressed(old.get("payload"), candidate, component):
                        candidate, error = None, "source timestamp moved backwards; retained last successful quotes"
                    payload = candidate if candidate is not None else old.get("payload")
                    fetched_at = _iso(at) if candidate is not None else old.get("fetched_at")
                    conn.execute(
                        "INSERT INTO stock_market_cache"
                        " (component,payload,fetched_at,attempted_at,attempt_order,refresh_error,session_slot)"
                        " VALUES(?,?,?,?,?,?,?) ON CONFLICT(component) DO UPDATE SET"
                        " payload=excluded.payload,fetched_at=excluded.fetched_at,attempted_at=excluded.attempted_at,"
                        " attempt_order=excluded.attempt_order,refresh_error=excluded.refresh_error,session_slot=excluded.session_slot"
                        " WHERE excluded.attempt_order > stock_market_cache.attempt_order",
                        (component, json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                         fetched_at, _iso(at), attempt_order, error, slot),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    finally:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout)}")


def refresh_stock_market(conn, now=None, *, session=None, force=False, trading_calendars=None) -> dict:
    """Refresh due groups in the background; closed groups get at most one final check.

    The caller must initialize schema and use a connection outside a ledger write
    transaction. force bypasses active-window TTL, never a completed close check.
    A stalled transport is detached at the total network deadline and cannot write
    to SQLite; its underlying request retains its own finite socket timeouts.
    """
    if conn.in_transaction:
        raise ValueError("stock-market refresh requires a connection outside a write transaction")
    at = _now(now)
    records = _read_records(conn)
    slots = {market: _market_slot(market, at, trading_calendars) for market in _MARKETS}
    markets = [market for market, slot in slots.items()
               if _is_due(records.get(market), slot, at, force)
               or any(item.get('available') and 'trend' not in item
                      for item in ((records.get(market) or {}).get('payload') or []))]
    industries_due = _is_due(records.get("industries"), slots["CN"], at, force)
    if not markets and not industries_due:
        return _snapshot(records, at, trading_calendars)
    global _LAST_ATTEMPT_ORDER
    with _CACHE_LOCK:
        _LAST_ATTEMPT_ORDER = max(time.time_ns(), _LAST_ATTEMPT_ORDER + 1)
        attempt_order = _LAST_ATTEMPT_ORDER
    outcomes = _fetch(markets, industries_due, session)
    updates = {}
    if markets:
        raw, error = outcomes["indices"]
        try:
            groups = _parse_indices(raw, markets) if not error else {}
        except (ValueError, TypeError, KeyError) as exc:
            groups, error = {}, f"invalid index response: {str(exc)[:180]}"
        for market in markets:
            candidate = groups.get(market)
            market_error = error
            if candidate is not None and not any(item["available"] for item in candidate):
                candidate, market_error = None, "no usable index quotes for market"
            if candidate is not None:
                previous = {item['key']: item for item in ((records.get(market) or {}).get('payload') or [])}
                candidate = [attach_intraday_trend(item, outcomes.get(f"trend:{item['key']}", (None, None))[0],
                                                   previous.get(item['key'])) for item in candidate]
            updates[market] = (candidate, market_error, slots[market]["slot"])
    if industries_due:
        raw, error = outcomes["industries"]
        try:
            candidate = _parse_industries(raw) if not error else None
        except (ValueError, TypeError, KeyError) as exc:
            candidate, error = None, f"invalid primary-industry response: {str(exc)[:180]}"
        updates["industries"] = (candidate, error, slots["CN"]["slot"])
    _publish(conn, updates, attempt_order, at)
    return _snapshot(_read_records(conn), at, trading_calendars)
