"""只读正式净值，在独立估值库幂等保存逐基金每日验证。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json

from src.beijing_time import TZ_CN
from src.fund_estimate_sources import total_return_pct
from src.fund_estimate_store import iso_now, parse_time
from src.fund_estimate_types import decimal_text, ensure_aware


def _digest(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _number(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (ValueError, ArithmeticError):
        return None


def _official_quotes(repository, identity, now):
    return sorted(
        (quote for quote in repository.list_quotes(identity.product_id, on_or_before=now.date())
         if _number(quote.unit_nav) is not None and quote.unit_nav > 0),
        key=lambda quote: quote.quote_date,
    )


def _quote_version_component(identity, quotes):
    return [identity.product_id,
            [[quote.quote_date.isoformat(), decimal_text(quote.unit_nav)] for quote in quotes]]


def _official_version(versions):
    return _digest(sorted(versions, key=lambda pair: pair[0]))


def official_nav_version(repository, identities, now: datetime) -> str:
    """总览与对账共享的正式净值经济版本；单产品读取失败不影响其余产品。"""
    return read_official_nav_state(repository, identities, now)["confirmed_nav_version"]


def read_official_nav_state(repository, identities, now: datetime) -> dict:
    """首屏可读取已确认净值状态，无需等待每日对账或写入估值库。"""
    now = ensure_aware(now, TZ_CN).astimezone(TZ_CN)
    identities = list(identities)
    versions = []
    confirmed_today = set()
    latest_confirmed = None
    latest_by_product = {}
    errors = []
    for identity in identities:
        try:
            quotes = _official_quotes(repository, identity, now)
            if quotes:
                versions.append(_quote_version_component(identity, quotes))
                latest = quotes[-1].quote_date.isoformat()
                latest_by_product[identity.product_id] = latest
                latest_confirmed = max(latest_confirmed or latest, latest)
                if quotes[-1].quote_date == now.date():
                    confirmed_today.add(identity.product_id)
        except Exception as exc:
            errors.append({"product_id": identity.product_id, "code": identity.code, "error": str(exc)[:300]})
    return {
        "confirmed_nav_version": _official_version(versions),
        "today_confirmed_count": len(confirmed_today),
        "latest_confirmed_date": latest_confirmed,
        "latest_confirmed_by_product": latest_by_product,
        "expected_fund_count": len(identities),
        "errors": errors,
    }


def verification_refresh_state(identities, current_state, items, now: datetime, *,
                               is_trading_day, analysis_status, status,
                               version_pending=False) -> dict:
    """全部持仓验证及点评完成后，冻结至之后首个交易日收盘。"""
    identities = list(identities)
    product_ids = {identity.product_id for identity in identities}
    result = {
        "refresh_complete": False, "next_refresh_at": None,
        "expected_fund_count": len(identities), "verified_fund_count": 0,
    }
    if not identities:
        return result
    now = ensure_aware(now, TZ_CN).astimezone(TZ_CN)
    latest_by_product = current_state.get("latest_confirmed_by_product") or {}
    try:
        confirmed_days = {product_id: date.fromisoformat(latest_by_product[product_id])
                          for product_id in product_ids if latest_by_product.get(product_id)}
    except (TypeError, ValueError):
        return result
    if not confirmed_days:
        return result
    target = max(confirmed_days.values())
    if target > now.date():
        return result
    target_text = target.isoformat()
    matching_items = {product_id: [] for product_id in product_ids}
    for item in items:
        if item.get("product_id") in product_ids and item.get("trade_date") == target_text:
            matching_items[item["product_id"]].append(item)
    complete_products = {
        product_id for product_id, rows in matching_items.items()
        if confirmed_days.get(product_id) == target and rows
        and all(row.get("verification_status") in {"verified", "missing_estimate"}
                and _number(row.get("realized_pct")) is not None
                and row.get("profit_status") not in {"pending_dividend_ledger", "error"} for row in rows)
    }
    result["verified_fund_count"] = len(complete_products)
    if (len(complete_products) != len(identities)
            or current_state.get("errors") or status != "ready" or version_pending
            or analysis_status not in {"ready", "disabled", "failed"}):
        return result
    # 日历不可用时保持轮询，不自行以星期几推断交易日。
    try:
        for offset in range(1, 367):
            next_day = target + timedelta(days=offset)
            trading = is_trading_day(next_day)
            if trading is True:
                resume_at = datetime.combine(next_day, time(15), TZ_CN)
                if now < resume_at:
                    result.update(refresh_complete=True, next_refresh_at=iso_now(resume_at))
                return result
            if trading is not False:
                return result
    except Exception:
        return result
    return result


def _last_valid_snapshot(store, product_id, day, now):
    valid = []
    for snapshot in store.snapshots_for_trade_date(product_id, day):
        if _number(snapshot.get("return_pct")) is None:
            continue
        if snapshot.get("status") not in {"ready", "experimental", "stale"}:
            continue
        if snapshot.get("target") != "mark_to_market_total_return":
            continue
        try:
            generated = parse_time(snapshot.get("generated_at"))
        except (ValueError, TypeError):
            continue
        if generated.date().isoformat() == day and generated <= now:
            valid.append((generated, snapshot))
    return max(valid, key=lambda pair: pair[0])[1] if valid else None


def _cached_dividends(repository, product_id):
    """正式缓存只证明明确披露的分红，空记录没有覆盖截止日，不能证明零。"""
    database = getattr(repository, "database", None)
    if database is None:
        return {}
    with database.connection() as conn:
        state = conn.execute(
            "SELECT error FROM fund_dividend_state WHERE product_id = ?", (product_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT ex_dividend_date, per_share, error FROM fund_dividend_events WHERE product_id = ?",
            (product_id,),
        ).fetchall()
    events = {}
    cache_uncertain = state is None or bool(state["error"])
    for row in rows:
        day = row["ex_dividend_date"]
        amount = _number(row["per_share"])
        if cache_uncertain or row["error"] or amount is None or amount < 0 or day in events:
            events[day] = None
        else:
            events[day] = decimal_text(amount)
    return events


def _dividend(current, navs, cached_dividends):
    day = current.quote_date.isoformat()
    # 后来披露、修订或冲突的正式事件必须撤销旧观测中的零分红结论。
    if day in cached_dividends:
        cached = cached_dividends[day]
        return (cached, "verified_cash") if cached is not None else (None, "missing")
    observation = navs.get(day)
    if (observation is not None
            and _number(observation.unit_nav) == current.unit_nav
            and observation.event_status == "ordinary"
            and observation.dividend_coverage in {"verified_none", "verified_cash"}):
        value = _number(observation.cash_dividend_per_share)
        if value is not None and value >= 0:
            return decimal_text(value), observation.dividend_coverage
    return None, "missing"


def _daily_item(identity, name, current, previous, snapshot, navs, dividends, stamp):
    day = current.quote_date.isoformat()
    nav = decimal_text(current.unit_nav)
    base = decimal_text(previous.unit_nav) if previous is not None else None
    base_day = previous.quote_date.isoformat() if previous is not None else None
    dividend, coverage = _dividend(current, navs, dividends)
    nav_return = total_return_pct(base, nav, "0") if base is not None else None
    realized = total_return_pct(base, nav, dividend) if base is not None and dividend is not None else None
    predicted = decimal_text(snapshot["return_pct"]) if snapshot else None
    comparable = predicted is not None and realized is not None
    error = decimal_text(Decimal(predicted) - Decimal(realized)) if comparable else None
    direction = None
    if comparable:
        direction = ((Decimal(predicted) > 0) - (Decimal(predicted) < 0)
                     == (Decimal(realized) > 0) - (Decimal(realized) < 0))
    source_version = _digest([identity.product_id, day, nav, base_day, base, dividend, coverage])
    return {
        "product_id": identity.product_id, "name": name, "code": identity.code,
        "trade_date": day,
        "snapshot_generated_at": snapshot["generated_at"] if snapshot else None,
        "predicted_pct": predicted, "realized_pct": realized,
        "error_pct": error, "direction_ok": direction,
        "unit_nav": nav, "nav_return_pct": nav_return,
        "base_nav": base, "base_nav_date": base_day,
        "snapshot_base_nav": snapshot.get("base_nav") if snapshot else None,
        "snapshot_base_nav_date": snapshot.get("base_nav_date") if snapshot else None,
        "cash_dividend_per_share": dividend, "dividend_coverage": coverage,
        "verification_status": ("pending_return" if realized is None
                                else "verified" if comparable else "missing_estimate"),
        "source_version": source_version, "updated_at": stamp,
    }


def sync_confirmed_verifications(repository, store, identities, now: datetime, *, active_product_ids=None) -> dict:
    """正式 quotes 为唯一确认源；每只隔离失败且不进行联网、训练或 AI 调用。"""
    now = ensure_aware(now, TZ_CN).astimezone(TZ_CN)
    stamp = iso_now(now)
    today = now.date().isoformat()
    existing = store.read_daily_verifications()
    legacy = store.read_verifications()
    errors = []
    official_versions = []
    confirmed_today = set()
    latest_available = None
    active = set(active_product_ids) if active_product_ids is not None else None
    for identity in identities:
        try:
            quotes = _official_quotes(repository, identity, now)
            if not quotes:
                continue
            official_versions.append(_quote_version_component(identity, quotes))
            is_active = active is None or identity.product_id in active
            if is_active and quotes[-1].quote_date.isoformat() == today:
                confirmed_today.add(identity.product_id)
            snapshot_days = store.snapshot_trade_dates(identity.product_id)
            boundaries = snapshot_days + [row["trade_date"] for row in existing + legacy
                                          if row["product_id"] == identity.product_id]
            if not is_active and not boundaries:
                continue
            first_day = min(boundaries) if boundaries else quotes[-1].quote_date.isoformat()
            last_day = max(boundaries) if not is_active else today
            observations = {row.nav_date: row for row in store.nav_as_of(identity.code, stamp)}
            dividends = _cached_dividends(repository, identity.product_id)
            product = repository.get_product(identity.product_id)
            name = product.name if product is not None else identity.code
            previous = None
            for current in quotes:
                day = current.quote_date.isoformat()
                if first_day <= day <= last_day:
                    snapshot = _last_valid_snapshot(store, identity.product_id, day, now)
                    item = _daily_item(identity, name, current, previous, snapshot, observations, dividends, stamp)
                    store.save_daily_verification(item)
                    latest_available = max(latest_available or day, day)
                previous = current
        except Exception as exc:
            errors.append({"product_id": identity.product_id, "code": identity.code, "error": str(exc)[:300]})
    # 失败时保留持久化历史的日期边界，供上层明确降级。
    for row in existing:
        latest_available = max(latest_available or row["trade_date"], row["trade_date"])
    return {
        "confirmed_nav_version": _official_version(official_versions),
        "processed_active_nav_version": _official_version(
            [entry for entry in official_versions if active is None or entry[0] in active]),
        "today_confirmed_count": len(confirmed_today),
        "latest_available_date": latest_available,
        "errors": errors,
    }
