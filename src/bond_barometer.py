"""债市晴雨表：南方基金盘中行情，保留中债日终曲线作为备用。

数据来自中国债券信息网（yield.chinabond.com.cn → 正确域名见下）公开接口，
覆盖南方基金晴雨表所用的六条曲线与期限：

- 利率债：国债 / 国开 / 进出口行 × 1/3/5/7/10 年
- 信用债：中短期票据 AAA / AA+ / AA × 1月/3月/6月/1/3/5 年

颜色口径（本系统定义，页面需注明）：与上一交易日相比，
收益率下行 → red（债市走强），上行 → green，变动不超过 0.05bp → flat。
"""
from __future__ import annotations

from datetime import date, datetime, time as clock_time, timedelta
from decimal import Decimal
import hashlib
import json
import logging
import threading
import time

import requests

from src.beijing_time import now as beijing_now
from src.southern_bond_market import fetch_barometer as fetch_live_barometer

logger = logging.getLogger(__name__)
_PUBLISH_LOCK = threading.Lock()

_BASE_URL = "https://yield.chinabond.com.cn"
_SEARCH_URL = _BASE_URL + "/cbweb-mn/yc/searchYc"
_MAIN_URL = _BASE_URL + "/cbweb-mn/yield_main"
_TIMEOUT = (10, 25)
_ATTEMPTS = 3
# 变动绝对值不超过 0.0005（0.05bp）视为持平，避免微小抖动上色。
_FLAT_THRESHOLD = Decimal("0.0005")
_SNAPSHOT_TTL_SECONDS = 60
_SNAPSHOT_RETENTION_DAYS = 30
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

_SNAPSHOT_SQL = """
CREATE TABLE IF NOT EXISTS bond_barometer_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    as_of TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    first_fetched_at TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT ''
)
"""


def ensure_barometer_schema(conn):
    conn.execute(_SNAPSHOT_SQL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(bond_barometer_snapshots)")}
    if "fingerprint" not in columns:
        conn.execute("ALTER TABLE bond_barometer_snapshots ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")


def read_snapshot(conn):
    """读取最近一次落库的行情快照；无快照返回 None。"""
    if conn is None:
        return None
    row = conn.execute(
        "SELECT payload, fetched_at, first_fetched_at FROM bond_barometer_snapshots"
        " ORDER BY fetched_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    payload["snapshot_at"] = row[1]
    payload["first_snapshot_at"] = row[2] or row[1]
    return {"payload": payload, "fetched_at": row[1]}


def _fingerprint(payload) -> str:
    """数据指纹：忽略生成时间等易变字段，判断行情内容是否变化。"""
    timestamps = {"generated_at", "snapshot_at", "first_snapshot_at", "source_updated_at", "refresh_error"}
    core = {key: value for key, value in payload.items() if key not in timestamps}
    if isinstance(core.get("egg_estimates"), dict):
        core["egg_estimates"] = {key: value for key, value in core["egg_estimates"].items()
                                 if key != "source_updated_at"}
    canonical = json.dumps(core, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_snapshot(conn, payload, fetched_at):
    """写入快照；当天内容一致时仅更新最新刷新时间，保留首次时间。"""
    fingerprint = _fingerprint(payload)
    day = fetched_at.date().isoformat()
    # 保留微秒，否则同一秒 A→B→A 去重后无法区分最后一次刷新。
    stamp = fetched_at.isoformat()
    existing = conn.execute(
        "SELECT id, first_fetched_at FROM bond_barometer_snapshots"
        " WHERE substr(fetched_at, 1, 10)=? AND fingerprint=? ORDER BY id DESC LIMIT 1",
        (day, fingerprint),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE bond_barometer_snapshots SET fetched_at=?, payload=? WHERE id=?",
            (stamp, json.dumps(payload, ensure_ascii=False), existing[0]),
        )
        first = existing[1] or stamp
    else:
        conn.execute(
            "INSERT INTO bond_barometer_snapshots(payload, as_of, fetched_at, first_fetched_at, fingerprint) VALUES(?,?,?,?,?)",
            (
                json.dumps(payload, ensure_ascii=False),
                payload.get("as_of", ""),
                stamp,
                stamp,
                fingerprint,
            ),
        )
        first = stamp
    # 仅保留最近 30 天的快照。
    cutoff = (fetched_at - timedelta(days=_SNAPSHOT_RETENTION_DAYS)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM bond_barometer_snapshots WHERE fetched_at < ?", (cutoff,))
    return stamp, first


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
        "source_id": "chinabond",
        "source": "中国债券信息网 中债到期收益率曲线",
        "change_unit": "percentage_point",
        "method": "与前一日相比：收益率下行=red，上行=green，变动≤0.05bp=flat（本系统口径）",
        "rate": {"rows": rate_rows, **rate_summary},
        "credit": {"rows": credit_rows, **credit_summary},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _in_trading_window(now: datetime) -> bool:
    """工作日 9:00-17:30（北京时间）内才自动刷新。"""
    return now.weekday() < 5 and _TRADING_START <= now.time() <= _TRADING_END


def _publish_snapshot(result, current, conn):
    # 后台预热与页面强制刷新可能同时联网。只在发布时短暂互斥，
    # 重读最新快照再比较/写入，避免较慢的旧请求覆盖先完成的新请求。
    with _PUBLISH_LOCK:
        snapshot = read_snapshot(conn) if conn is not None else None
        cached = snapshot["payload"] if snapshot else {}
        if cached.get("available"):
            older_day = result.get("as_of", "") < cached.get("as_of", "")
            older_tick = (
                result.get("as_of") == cached.get("as_of")
                and result.get("source_id") == cached.get("source_id")
                and cached.get("source_updated_at") and result.get("source_updated_at")
                and result["source_updated_at"] < cached["source_updated_at"]
            )
            if older_day or older_tick:
                return {**cached, "refresh_error": "上游返回较旧行情，保留已有较新快照"}
            if result.get("is_fallback") and (not result.get("available") or cached.get("as_of", "") >= result.get("as_of", "")):
                return {**cached, "refresh_error": "盘中源暂不可用，显示上次成功获取的行情"}
            previous_eggs = cached.get("egg_estimates") or {}
            incoming_eggs = result.get("egg_estimates") or {}
            if (previous_eggs.get("available") and previous_eggs.get("as_of") == result.get("as_of")
                    and (not incoming_eggs.get("available") or
                         (incoming_eggs.get("as_of", ""), incoming_eggs.get("source_updated_at", "")) <
                         (previous_eggs.get("as_of", ""), previous_eggs.get("source_updated_at", "")))):
                result = {**result, "egg_estimates": previous_eggs,
                          "refresh_error": "收蛋预估暂未更新，保留上次较新的参考"}
        if conn is not None:
            stamp, first = write_snapshot(conn, result, current)
            result["snapshot_at"] = stamp
            result["first_snapshot_at"] = first
        return result


def latest_barometer(
    day: date | None = None,
    *,
    force: bool = False,
    now: datetime | None = None,
    conn=None,
    fetch=fetch_curves,
) -> dict:
    """库内快照入口：交易时段每分钟抓取盘中数据，其余时间直接读快照。

    *force* 供刷新按钮使用，随时强制重新抓取并覆盖快照。
    *conn* 为账本数据库连接；为 None 时退化为直接抓取（测试/命令行用）。
    周末/节假日自动回退到最近有数据的交易日。
    """
    current = now or beijing_now().replace(tzinfo=None)
    snapshot = read_snapshot(conn) if conn is not None else None
    if snapshot is not None and not force:
        try:
            fetched_at = datetime.fromisoformat(snapshot["fetched_at"])
            age = (current - fetched_at).total_seconds()
            if not _in_trading_window(current) or age < _SNAPSHOT_TTL_SECONDS:
                return snapshot["payload"]
        except (ValueError, TypeError):
            pass
    target = day or current.date()
    result = None
    try:
        result = fetch_live_barometer()
    except (requests.RequestException, ValueError, KeyError, TypeError):
        logger.warning("Intraday bond source unavailable", exc_info=True)
        snapshot = read_snapshot(conn) if conn is not None else None
        cached = snapshot["payload"] if snapshot else {}
        if (cached.get("available") and cached.get("source_id") == "southernfund"
                and cached.get("as_of", "") >= target.isoformat()):
            return {**cached, "refresh_error": "盘中源暂不可用，显示上次成功获取的行情"}
    if result is None:
        cursor = target
        try:
            for _ in range(10):
                while cursor.weekday() >= 5:
                    cursor -= timedelta(days=1)
                result = build_barometer(cursor, fetch=fetch)
                if result.get("available"):
                    break
                cursor -= timedelta(days=1)
        except (requests.RequestException, ValueError):
            snapshot = read_snapshot(conn) if conn is not None else None
            if snapshot and snapshot["payload"].get("available"):
                return {**snapshot["payload"], "refresh_error": "行情源暂不可用，显示上次成功获取的行情"}
            raise
        result["is_fallback"] = True
        result["refresh_error"] = "盘中源暂不可用，当前展示中债日终备用数据"
    # 生产环境记录联网完成时间；显式 now 保留测试/调用方的可控时钟。
    published_at = current if now is not None else beijing_now().replace(tzinfo=None)
    return _publish_snapshot(result, published_at, conn)


if __name__ == "__main__":
    import json

    print(json.dumps(latest_barometer(), ensure_ascii=False, indent=2))
