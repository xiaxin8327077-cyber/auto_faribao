"""净值、分红与因素归档。历史日终曲线和南方盘中矩阵分来源保存。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from urllib import parse, request

from src.beijing_time import TZ_CN
from src.fund_estimate_store import iso_now, parse_time
from src.fund_estimate_types import (
    FACTOR_NAMES,
    FACTOR_SET_ID,
    FactorObservation,
    FundIdentity,
    MissingDividendCoverage,
    NavObservation,
    decimal_text,
    pct_text,
)


NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
PAGE_SIZE = 100
CHINABOND_EOD = "chinabond_eod"
# 中债曲线点位是百分比。0.01 个百分点 = 1bp。
_BP_PER_PERCENTAGE_POINT = Decimal("100")


def total_return_pct(nav_prev, nav_t, cash_dividend_per_share) -> str:
    """普通现金分红的总收益百分数。分红未核验时拒绝，不当成 0。"""
    if cash_dividend_per_share is None:
        raise MissingDividendCoverage("分红数据缺失，不能按 0 计算总收益")
    previous = Decimal(decimal_text(nav_prev))
    current = Decimal(decimal_text(nav_t))
    dividend = Decimal(decimal_text(cash_dividend_per_share))
    if previous <= 0:
        raise ValueError("前一日净值必须大于 0")
    return pct_text((current + dividend) / previous * Decimal(100) - Decimal(100))


def parse_bp(value):
    """空报价是缺失，不是 0。"""
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    return decimal_text(text)


def visible_nav(observations: list[NavObservation], cutoff_at: str) -> list[NavObservation]:
    cutoff = parse_time(cutoff_at)
    latest: dict[str, NavObservation] = {}
    for item in observations:
        if parse_time(item.available_at) > cutoff:
            continue
        current = latest.get(item.nav_date)
        if current is None or parse_time(item.available_at) >= parse_time(current.available_at):
            latest[item.nav_date] = item
    return [latest[key] for key in sorted(latest)]


def combine_factor_families(observations: list[FactorObservation]) -> FactorObservation:
    """不同来源家庭未核验映射时，不拼接成已验证因子。"""
    if not observations:
        raise ValueError("没有因素观测")
    families = {item.source_family for item in observations}
    first = observations[0]
    if len(families) == 1:
        return first
    if all(item.basis_status == "verified_mapping" for item in observations):
        values = {}
        missing = []
        for name in FACTOR_NAMES:
            present = [item.values_bp.get(name) for item in observations if item.values_bp.get(name) is not None]
            if len(present) == 1:
                values[name] = present[0]
            else:
                values[name] = None
                missing.append(name)
        return FactorObservation(
            factor_set_id=FACTOR_SET_ID,
            source_family="mapped_mix",
            as_of=first.as_of,
            published_at=first.published_at,
            fetched_at=first.fetched_at,
            market_as_of=first.market_as_of,
            values_bp=values,
            calendar_days=first.calendar_days,
            basis_status="verified_mapping",
            missing_fields=missing,
            basis_date=first.basis_date,
            source_version="mapped",
        )
    return FactorObservation(
        factor_set_id=FACTOR_SET_ID,
        source_family="unverified_mix",
        as_of=first.as_of,
        published_at=first.published_at,
        fetched_at=first.fetched_at,
        market_as_of=first.market_as_of,
        values_bp={name: None for name in FACTOR_NAMES},
        calendar_days=first.calendar_days,
        basis_status="unverified",
        missing_fields=list(FACTOR_NAMES),
        basis_date=None,
        source_version="unverified",
    )


def _curve_point(curves, key, tenor):
    series = curves.get(key) if isinstance(curves, dict) else None
    if not isinstance(series, dict):
        return None
    value = series.get(tenor)
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    return number if number.is_finite() else None


def _bp_change(current, previous):
    if current is None or previous is None:
        return None
    return decimal_text((current - previous) * _BP_PER_PERCENTAGE_POINT)


def factors_from_chinabond_levels(start: date, end: date, fetch_day, *, fetched_at: str | None = None) -> list[FactorObservation]:
    """用中债到期收益率的相邻收盘点计算 bp。周末不请求。口径保持 native，不映射南方盘中。"""
    if end < start:
        return []
    stamp = fetched_at or iso_now(datetime.now(TZ_CN))
    levels = {}
    day = start
    while day <= end:
        if day.weekday() < 5:
            curves = fetch_day(day) or {}
            if curves:
                levels[day] = curves
        day += timedelta(days=1)
    if levels:
        cursor = min(levels) - timedelta(days=1)
        for _ in range(14):
            while cursor.weekday() >= 5:
                cursor -= timedelta(days=1)
            if cursor in levels:
                break
            curves = fetch_day(cursor) or {}
            if curves:
                levels[cursor] = curves
                break
            cursor -= timedelta(days=1)
    ordered = sorted(levels)
    rows = []
    for previous_day, current_day in zip(ordered, ordered[1:]):
        if current_day < start or current_day > end:
            continue
        current = levels[current_day]
        previous = levels[previous_day]
        gov3 = _bp_change(_curve_point(current, "rate_guozhai", 3.0), _curve_point(previous, "rate_guozhai", 3.0))
        gov5 = _bp_change(_curve_point(current, "rate_guozhai", 5.0), _curve_point(previous, "rate_guozhai", 5.0))
        gov10 = _bp_change(_curve_point(current, "rate_guozhai", 10.0), _curve_point(previous, "rate_guozhai", 10.0))
        cdb5 = _bp_change(_curve_point(current, "rate_guokai", 5.0), _curve_point(previous, "rate_guokai", 5.0))
        aaa3 = _bp_change(_curve_point(current, "credit_aaa", 3.0), _curve_point(previous, "credit_aaa", 3.0))
        slope = None if gov10 is None or gov3 is None else decimal_text(Decimal(gov10) - Decimal(gov3))
        cdb_spread = None if cdb5 is None or gov5 is None else decimal_text(Decimal(cdb5) - Decimal(gov5))
        credit = None if aaa3 is None or gov3 is None else decimal_text(Decimal(aaa3) - Decimal(gov3))
        values = {
            "gov_3y_bp": gov3,
            "gov_slope_10y_3y_bp": slope,
            "cdb_spread_5y_bp": cdb_spread,
            "credit_aaa_spread_3y_bp": credit,
            "calendar_days": str((current_day - previous_day).days),
        }
        missing = [name for name, value in values.items() if value is None]
        close_stamp = f"{current_day.isoformat()}T18:00:00+08:00"
        rows.append(
            FactorObservation(
                factor_set_id=FACTOR_SET_ID,
                source_family=CHINABOND_EOD,
                as_of=current_day.isoformat(),
                published_at=close_stamp,
                fetched_at=stamp,
                market_as_of=close_stamp,
                values_bp=values,
                calendar_days=(current_day - previous_day).days,
                basis_status="native",
                missing_fields=missing,
                basis_date=previous_day.isoformat(),
                source_version=_hash({
                    "as_of": current_day.isoformat(),
                    "basis": previous_day.isoformat(),
                    "values": values,
                }),
            )
        )
    return rows


def live_chinabond_factors(start: date, end: date, source_family: str) -> list[FactorObservation]:
    """只在任务执行时联网。中债日终与南方盘中不是已验证的同一口径。"""
    if source_family != CHINABOND_EOD:
        raise IncompleteNavHistory(f"{source_family} 不能用中债日终曲线代替")
    from src.bond_barometer import _session, fetch_curves

    session = _session()
    try:
        return factors_from_chinabond_levels(start, end, lambda day: fetch_curves(day, session))
    finally:
        session.close()


def factors_from_southern_barometer(payload: dict, *, fetched_at: str) -> FactorObservation:
    """从已保存的南方盘中矩阵取 BP。空单元格保持缺失，不借用收蛋区间。"""
    as_of = str(payload.get("as_of") or "")
    market_as_of = payload.get("source_updated_at")
    rate_rows = ((payload.get("rate") or {}).get("rows") or [])
    credit_rows = ((payload.get("credit") or {}).get("rows") or [])

    def cell(rows, label, tenor):
        for row in rows:
            if row.get("label") != label:
                continue
            for item in row.get("cells") or []:
                if item.get("tenor") == tenor:
                    return parse_bp(item.get("change"))
        return None

    gov_3y = cell(rate_rows, "国债", "3年")
    gov_10y = cell(rate_rows, "国债", "10年")
    gov_5y = cell(rate_rows, "国债", "5年")
    cdb_5y = cell(rate_rows, "国开", "5年")
    aaa_3y = cell(credit_rows, "AAA", "3年")
    values = {
        "gov_3y_bp": gov_3y,
        "gov_slope_10y_3y_bp": None if gov_10y is None or gov_3y is None else decimal_text(Decimal(gov_10y) - Decimal(gov_3y)),
        "cdb_spread_5y_bp": None if cdb_5y is None or gov_5y is None else decimal_text(Decimal(cdb_5y) - Decimal(gov_5y)),
        "credit_aaa_spread_3y_bp": None if aaa_3y is None or gov_3y is None else decimal_text(Decimal(aaa_3y) - Decimal(gov_3y)),
        "calendar_days": None,
    }
    missing = [name for name, value in values.items() if value is None]
    eggs = payload.get("egg_estimates") if isinstance(payload.get("egg_estimates"), dict) else {}
    return FactorObservation(
        factor_set_id=FACTOR_SET_ID,
        source_family="southern_intraday",
        as_of=as_of,
        published_at=market_as_of if isinstance(market_as_of, str) else None,
        fetched_at=fetched_at,
        market_as_of=market_as_of if isinstance(market_as_of, str) else None,
        values_bp=values,
        calendar_days=None,
        basis_status="native",
        missing_fields=missing,
        basis_date=None,
        source_version=_hash(payload),
    )


def category_market_reference(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    eggs = payload.get("egg_estimates")
    if not isinstance(eggs, dict):
        return None
    return {
        "kind": "category_range",
        "source": "southern_egg_estimates",
        "source_updated_at": eggs.get("source_updated_at"),
        "note": "四类债基收蛋区间是市场参考，不是本基金收益率",
        "items": eggs.get("items") or [],
    }


class IncompleteNavHistory(RuntimeError):
    pass


class EstimateSources:
    def __init__(self, nav_fetcher=None, dividend_fetcher=None, factor_fetcher=None, intraday_reader=None, sleeper=None):
        self.nav_fetcher = nav_fetcher or _live_nav_page
        self.dividend_fetcher = dividend_fetcher
        self.factor_fetcher = factor_fetcher
        self.intraday_reader = intraday_reader
        self.sleeper = sleeper or (lambda _seconds: None)
        self.fetch_calls = 0

    def fetch_nav_history(self, identity: FundIdentity, start: date, end: date) -> list[NavObservation]:
        self.fetch_calls += 1
        pages = []
        page = 1
        total = None
        seen_dates = set()
        while page <= 80:
            payload = self._fetch_page(identity.code, start, end, page)
            rows = payload.get("rows") or []
            if total is None and payload.get("total") is not None:
                total = int(payload["total"])
            if not rows:
                break
            fresh = [row for row in rows if row["nav_date"] not in seen_dates]
            if not fresh and page > 1:
                raise IncompleteNavHistory("净值分页重复，不能把第一页当成全部历史")
            pages.extend(fresh)
            seen_dates.update(row["nav_date"] for row in fresh)
            if total is not None and len(seen_dates) >= total:
                break
            if total is not None and len(rows) < PAGE_SIZE and len(seen_dates) < total:
                raise IncompleteNavHistory("净值页数不足，拒绝把不完整历史当成已回补")
            # 公开接口常常不给总条数，而且单页少于请求的 pageSize。
            # 只有空页或没有新日期时才能停止，不能把短页当成全部历史。
            if total is None and len(rows) < PAGE_SIZE and fresh and min(row["nav_date"] for row in fresh) <= start.isoformat():
                page += 1
                continue
            page += 1
        else:
            raise IncompleteNavHistory("净值翻页达到上限，覆盖范围仍未结束")
        if total is not None and len(seen_dates) < total:
            raise IncompleteNavHistory("净值日期覆盖少于来源声明的总条数")
        coverage = self._dividend_coverage(identity, start, end)
        fetched_at = iso_now(datetime.now(TZ_CN))
        observations = []
        for row in pages:
            nav_day = date.fromisoformat(row["nav_date"])
            if nav_day < start or nav_day > end:
                continue
            dividend, event_status, dividend_coverage = _dividend_for(nav_day, coverage)
            observations.append(
                NavObservation(
                    code=identity.code,
                    nav_date=row["nav_date"],
                    unit_nav=decimal_text(row["unit_nav"]),
                    cash_dividend_per_share=dividend,
                    available_at=row.get("available_at") or fetched_at,
                    source=row.get("source") or "eastmoney_fund",
                    source_version=row.get("source_version") or _hash(row),
                    event_status=event_status,
                    dividend_coverage=dividend_coverage,
                )
            )
        deduped = {}
        for item in observations:
            deduped[(item.nav_date, item.source, item.source_version)] = item
        return [deduped[key] for key in sorted(deduped)]

    def _fetch_page(self, code, start, end, page):
        delay = 0.2
        last_error = None
        for _attempt in range(3):
            try:
                return self.nav_fetcher(code, start, end, page, PAGE_SIZE)
            except Exception as exc:  # 有限退避，不在导入时访问网络
                last_error = exc
                self.sleeper(delay)
                delay *= 2
        raise IncompleteNavHistory("净值回补失败") from last_error

    def _dividend_coverage(self, identity, start, end):
        if self.dividend_fetcher is None:
            return {"complete": False, "events": []}
        try:
            coverage = self.dividend_fetcher(identity, start, end)
        except Exception:
            return {"complete": False, "events": []}
        if not isinstance(coverage, dict) or not coverage.get("complete"):
            return {"complete": False, "events": list((coverage or {}).get("events") or [])}
        return coverage

    def fetch_daily_factors(self, start: date, end: date, source_family: str) -> list[FactorObservation]:
        if self.factor_fetcher is None:
            raise IncompleteNavHistory(
                f"{source_family} 没有已核验的日终历史接口，不能用南方盘中快照回填过去"
            )
        self.fetch_calls += 1
        observations = list(self.factor_fetcher(start, end, source_family))
        for item in observations:
            if item.source_family != source_family:
                raise ValueError("因素来源家庭与请求不一致")
            _reject_zero_filled_missing(item)
        return observations

    def read_intraday_factors(self, now: datetime) -> FactorObservation:
        self.fetch_calls += 1
        if self.intraday_reader is None:
            return FactorObservation(
                factor_set_id=FACTOR_SET_ID,
                source_family="southern_intraday",
                as_of=ensure_date(now),
                published_at=None,
                fetched_at=iso_now(now),
                market_as_of=None,
                values_bp={name: None for name in FACTOR_NAMES},
                calendar_days=None,
                basis_status="missing",
                missing_fields=list(FACTOR_NAMES),
                basis_date=None,
                source_version="missing",
            )
        observation = self.intraday_reader(now)
        _reject_zero_filled_missing(observation)
        return observation


def ensure_date(now: datetime) -> str:
    aware = now if now.tzinfo else now.replace(tzinfo=TZ_CN)
    return aware.astimezone(TZ_CN).date().isoformat()


def _dividend_for(nav_day: date, coverage: dict):
    events = {date.fromisoformat(item["ex_date"]): item for item in coverage.get("events") or []}
    event = events.get(nav_day)
    if event is not None:
        kind = event.get("kind") or "cash"
        if kind != "cash":
            return None, "special", "special_event"
        return decimal_text(event["per_share"]), "ordinary", "verified_cash"
    if coverage.get("complete"):
        return "0", "ordinary", "verified_none"
    return None, "unknown", "missing"


def _reject_zero_filled_missing(observation: FactorObservation) -> None:
    for name, value in observation.values_bp.items():
        if value is None:
            continue
        decimal_text(value)
    if observation.missing_fields:
        for name in observation.missing_fields:
            if observation.values_bp.get(name) not in (None, ""):
                raise ValueError("缺失因子不能同时带有数值")


def _hash(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _live_nav_page(code, start, end, page, page_size):
    """沿用已在行情模块使用的公开净值接口，并读取总条数以翻页。"""
    query = parse.urlencode(
        {
            "fundCode": code,
            "pageIndex": page,
            "pageSize": page_size,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        }
    )
    req = request.Request(
        f"{NAV_URL}?{query}",
        headers={"Referer": "https://fundf10.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
    )
    with request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict) or data.get("ErrCode") != 0:
        raise IncompleteNavHistory("公开基金净值接口状态异常")
    payload = data.get("Data") if isinstance(data.get("Data"), dict) else {}
    rows = []
    for item in payload.get("LSJZList") or []:
        if not isinstance(item, dict) or not item.get("FSRQ") or item.get("DWJZ") in (None, ""):
            continue
        rows.append(
            {
                "nav_date": str(item["FSRQ"]),
                "unit_nav": str(item["DWJZ"]),
                "source": "eastmoney_fund",
                "source_version": _hash(item),
            }
        )
    total = payload.get("TotalCount")
    return {"rows": rows, "total": int(total) if str(total or "").isdigit() else None}


def live_dividend_coverage(identity: FundIdentity, start: date, end: date) -> dict:
    """复用已有公开分红解析。解析失败视为覆盖缺失，而不是无分红。"""
    from src.portfolio_dividend_provider import PublicFundDividendProvider
    from src.portfolio_models import MarketProduct, ProductType

    product = MarketProduct(identity.provider or "eastmoney_fund", identity.code, identity.code, ProductType.PUBLIC_FUND)
    try:
        events = PublicFundDividendProvider().fetch_dividends(product)
    except Exception as exc:
        raise IncompleteNavHistory("分红覆盖未核验") from exc
    return {
        "complete": True,
        "events": [
            {
                "ex_date": event.ex_dividend_date.isoformat(),
                "per_share": decimal_text(event.per_share),
                "kind": "cash",
            }
            for event in events
            if start <= event.ex_dividend_date <= end
        ],
    }


def default_history_start(today: date) -> date:
    return today - timedelta(days=365 * 5)
