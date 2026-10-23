"""南方基金公开债市晴雨表：盘中 BP 矩阵及四类债基收蛋参考。

使用公开 H5 的正常无登录请求协议；行情、预估分别保留上游时间。
https://m.nffund.com/cache/minipro/bondMarket/index.html
"""
from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import logging
import secrets
import string
import time

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from src.beijing_time import TZ_CN, now as beijing_now

logger = logging.getLogger(__name__)
_BASE_URL = "https://m.nffund.com/nffund/nfEStationNewWechat/"
_SOURCE_URL = "https://m.nffund.com/cache/minipro/bondMarket/index.html"
# 公开网页请求封装中的协议常量，不是账号密钥或登录凭据。
_REQUEST_KEY = b"tkeynfjj08282016"
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://m.nffund.com/new/index.html",
    "Accept": "application/json, text/plain, */*",
}
_WEATHER = {"1": "rain", "2": "cloud", "3": "sun_cloud", "4": "sunny"}
_TENORS = {
    "0": (("1Y", "1年"), ("3Y", "3年"), ("5Y", "5年"), ("7Y", "7年"), ("10Y", "10年")),
    "1": (("1M", "1月"), ("3M", "3月"), ("6M", "6月"), ("1Y", "1年"), ("3Y", "3年"), ("5Y", "5年")),
}
_LABELS = {"0": ("国债", "国开", "口行"), "1": ("AAA", "AA+", "AA")}
_EGG_FIELDS = (
    ("rate_short", "利率债·中短期", "llz_short_price"),
    ("rate_long", "利率债·中长期", "llz_long_price"),
    ("credit_short", "信用债·中短期", "yxz_short_price"),
    ("credit_long", "信用债·中长期", "yxz_long_price"),
)


def _decimal(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _request(session, function, **params):
    for attempt in range(2):
        # 上游有重放保护，每个请求（包括重试）均须使用新的随机串。
        nonce = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))
        token = base64.b64encode(
            AES.new(_REQUEST_KEY, AES.MODE_CBC, iv=_REQUEST_KEY).encrypt(pad(nonce.encode(), 16))
        ).decode()
        body = {
            "function": function, "channel": "mweb", "platform": "mweb",
            "clientversion": "76894", "pn": "MicroPage",
            "reqrandom": nonce, "reqtoken": token, **params,
        }
        try:
            response = session.post(
                _BASE_URL + function, data=body, headers=_HEADERS, timeout=(5, 12)
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != "ETS-5BP0000":
                code = payload.get("code", "invalid_payload") if isinstance(payload, dict) else "invalid_payload"
                raise ValueError(f"Southern {function}: {code}")
            return payload
        except (requests.RequestException, ValueError):
            if attempt == 1:
                raise
            time.sleep(0.2)


def parse_barometer(payload):
    as_of = date.fromisoformat(str(payload.get("trade_date") or "")).isoformat()
    updated = datetime.fromisoformat(f"{as_of}T{payload.get('deadline_time') or ''}")
    source_rows = payload.get("barometerRateItemList") or []
    blocks = {}
    for kind, block_name in (("0", "rate"), ("1", "credit")):
        source = {
            row.get("sub_type"): row for row in source_rows
            if isinstance(row, dict) and str(row.get("type")) == kind
        }
        counts = {"red": 0, "green": 0, "flat": 0, "missing": 0}
        rows = []
        weather = "flat"
        for label in _LABELS[kind]:
            raw = source.get(label, {})
            if weather == "flat":
                weather = _WEATHER.get(str(raw.get("sub_weather")), "flat")
            points = {point.get("age_limit"): point.get("bp")
                      for point in raw.get("bpDataList") or [] if isinstance(point, dict)}
            cells = []
            for tenor, tenor_label in _TENORS[kind]:
                bp = _decimal(points.get(tenor))
                color = "missing" if bp is None else "red" if bp < 0 else "green" if bp > 0 else "flat"
                counts[color] += 1
                cells.append({"tenor": tenor_label, "yield": None,
                              "change": str(bp) if bp is not None else None, "color": color})
            rows.append({"label": label, "cells": cells})
        blocks[block_name] = {"rows": rows, "counts": counts, "weather": weather}
    if not any(block["counts"][c] for block in blocks.values() for c in ("red", "green", "flat")):
        raise ValueError("Southern matrix has no quotes")
    return {
        "available": True, "as_of": as_of, "source_id": "southernfund",
        "source": "南方基金债市晴雨表（盘中）", "source_url": _SOURCE_URL,
        "source_updated_at": updated.isoformat(timespec="seconds"), "change_unit": "bp",
        "method": "南方基金口径：红=收益率下行、绿=上行、灰=持平，斜纹=暂无报价；变动单位为bp。",
        "generated_at": beijing_now().replace(tzinfo=None).isoformat(timespec="seconds"),
        **blocks,
    }


def parse_egg_estimates(payload):
    as_of = datetime.strptime(str(payload.get("trade_date") or ""), "%Y%m%d").date().isoformat()
    updated = datetime.fromtimestamp(int(payload["deadline_time"]) / 1000, TZ_CN)
    items = []
    for key, label, field in _EGG_FIELDS:
        value = _decimal(payload.get(field))
        if value is None:
            display, color = "暂无预估", "flat"
        elif value < 0:
            display, color = "预计碎蛋", "green"
        else:
            color = "red" if value > 0 else "flat"
            if value < Decimal("0.03"):
                display = "预计收蛋0–3个"
            elif value < Decimal("0.06"):
                display = "预计收蛋3–6个"
            elif value < Decimal("0.10"):
                display = "预计收蛋6–10个"
            else:
                display = "预计收蛋超10个"
        items.append({"key": key, "label": label, "return_pct": str(value) if value is not None else None,
                      "display": display, "color": color})
    return {
        "available": any(item["return_pct"] is not None for item in items),
        "as_of": as_of, "source_updated_at": updated.replace(tzinfo=None).isoformat(timespec="seconds"),
        "items": items,
    }


def fetch_barometer(*, session=None):
    own_session = session is None
    client = session or requests.Session()
    try:
        result = parse_barometer(_request(client, "OC050"))
        try:
            result["egg_estimates"] = parse_egg_estimates(
                _request(client, "OC049", queryTacticsProduct="1", bond_channel="1")
            )
        except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError):
            logger.warning("Southern egg estimates unavailable", exc_info=True)
            result["egg_estimates"] = {"available": False, "items": []}
        return result
    finally:
        if own_session:
            client.close()
