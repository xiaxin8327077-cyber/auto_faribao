import json
import logging
import os
import re
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from src.nav_monitor import (
    NanyinHttpClient,
    NavProduct,
    ProviderError,
    _legacy_ssl_context,
)


logger = logging.getLogger(__name__)

DEFAULT_HOLDINGS_CACHE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "nav_holdings_profiles.json"
)

CITIC_DISCLOSURE_SEARCH = "https://www.citic-wealth.com/was5/web/search"
CITIC_DOCUMENT_URL = "https://www.citic-wealth.com/was5/web/document"
CITIC_PERIODIC_CHANNEL = "267968"

NANYIN_ANNOUNCEMENT_PATH = (
    "/eportal/ui?moduleId=5&portal.url=/portlet/article-data!queryInformationDisList.portlet"
    "&TopModuelId=e7db4f2628cb40548f6101d71695ada2"
)
NANYIN_ARTICLE_DETAIL_PATH = (
    "/eportal/ui?moduleId=5&portal.url=/portlet/public-product-announcement!queryArticleDetail.portlet"
)
NANYIN_DOWNLOAD_PATH = (
    "/eportal/ui?moduleId=5&portal.url=/portlet/public-product-announcement!downloadAttach.portlet"
)


@dataclass(frozen=True)
class AssetMixRow:
    asset_class: str
    before_pct: Optional[Decimal] = None
    after_pct: Optional[Decimal] = None

    def to_dict(self) -> dict:
        return {
            "asset_class": self.asset_class,
            "before_pct": _decimal_to_str(self.before_pct),
            "after_pct": _decimal_to_str(self.after_pct),
        }

    @classmethod
    def from_any(cls, value):
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                str(value.get("asset_class") or ""),
                _optional_decimal(value.get("before_pct")),
                _optional_decimal(value.get("after_pct")),
            )
        return cls(str(value[0]), _optional_decimal(value[1]), _optional_decimal(value[2]))


@dataclass(frozen=True)
class TopAsset:
    name: str
    pct: Optional[Decimal] = None
    amount: Optional[Decimal] = None
    code: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "code": self.code,
            "amount": _decimal_to_str(self.amount),
            "pct": _decimal_to_str(self.pct),
        }

    @classmethod
    def from_any(cls, value):
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                name=str(value.get("name") or ""),
                code=str(value.get("code") or ""),
                amount=_optional_decimal(value.get("amount")),
                pct=_optional_decimal(value.get("pct")),
            )
        return cls(str(value[0]), _optional_decimal(value[1]) if len(value) > 1 else None)


@dataclass(frozen=True)
class HoldingProfile:
    provider: str
    code: str
    name: str
    report_period: str
    report_title: str
    disclosure_date: Optional[date]
    source_url: str
    updated_at: datetime
    asset_mix: tuple[AssetMixRow, ...]
    top_assets: tuple[TopAsset, ...] = ()
    status: str = "ok"
    error: str = ""

    def __post_init__(self):
        object.__setattr__(self, "asset_mix", tuple(AssetMixRow.from_any(row) for row in self.asset_mix))
        object.__setattr__(self, "top_assets", tuple(TopAsset.from_any(row) for row in self.top_assets))

    @property
    def cache_key(self) -> str:
        return cache_key(self.provider, self.code)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "code": self.code,
            "name": self.name,
            "report_period": self.report_period,
            "report_title": self.report_title,
            "disclosure_date": self.disclosure_date.isoformat() if self.disclosure_date else "",
            "source_url": self.source_url,
            "updated_at": self.updated_at.isoformat(timespec="seconds"),
            "asset_mix": [row.to_dict() for row in self.asset_mix],
            "top_assets": [row.to_dict() for row in self.top_assets],
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            provider=str(data.get("provider") or ""),
            code=str(data.get("code") or ""),
            name=str(data.get("name") or ""),
            report_period=str(data.get("report_period") or ""),
            report_title=str(data.get("report_title") or ""),
            disclosure_date=_optional_date(data.get("disclosure_date")),
            source_url=str(data.get("source_url") or ""),
            updated_at=_optional_datetime(data.get("updated_at")) or datetime.now(),
            asset_mix=tuple(data.get("asset_mix") or ()),
            top_assets=tuple(data.get("top_assets") or ()),
            status=str(data.get("status") or "ok"),
            error=str(data.get("error") or ""),
        )


@dataclass(frozen=True)
class MarketSymbol:
    name: str
    code: str
    change_pct: Decimal


@dataclass(frozen=True)
class MarketSnapshot:
    trade_date: date
    symbols: tuple[MarketSymbol, ...]

    def changes_by_name(self) -> dict[str, Decimal]:
        return {symbol.name: symbol.change_pct for symbol in self.symbols}


@dataclass(frozen=True)
class TopAssetMarketMove:
    name: str
    pct: Decimal
    category: str
    change_pct: Decimal
    symbol: str


@dataclass(frozen=True)
class ProductEstimate:
    product: NavProduct
    direction: str
    confidence: str
    score: Decimal
    reasons: tuple[str, ...]
    profile: Optional[HoldingProfile] = None
    error: str = ""
    top_asset_hits: tuple[TopAssetMarketMove, ...] = ()
    top_asset_total: int = 0
    top_asset_coverage: Decimal = Decimal("0")


def cache_key(provider: str, code: str) -> str:
    return f"{provider}:{(code or '').upper()}"


def target_quarter_for_check(day: date) -> str:
    quarter = (day.month - 1) // 3
    if quarter == 0:
        return f"{day.year - 1}Q4"
    return f"{day.year}Q{quarter}"


def parse_holding_profile_from_text(
    product: NavProduct,
    text: str,
    report_title: str,
    disclosure_date: Optional[date],
    source_url: str,
    updated_at: Optional[datetime] = None,
) -> HoldingProfile:
    normalized = _normalize_pdf_text(text)
    asset_mix = tuple(_parse_asset_mix_rows(normalized))
    top_assets = tuple(_parse_top_assets(normalized))
    if not asset_mix:
        raise ProviderError("未能从报告中解析资产组合表")
    return HoldingProfile(
        provider=product.provider,
        code=product.code,
        name=product.name,
        report_period=_extract_report_period(report_title),
        report_title=report_title,
        disclosure_date=disclosure_date,
        source_url=source_url,
        updated_at=updated_at or datetime.now(),
        asset_mix=asset_mix,
        top_assets=top_assets,
    )


def load_profile_cache(cache_path=DEFAULT_HOLDINGS_CACHE_PATH) -> dict[str, HoldingProfile]:
    path = Path(cache_path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f) or {}
    profiles = raw.get("profiles", raw)
    return {key: HoldingProfile.from_dict(value) for key, value in profiles.items()}


def update_profile_cache(profiles: list[HoldingProfile], cache_path=DEFAULT_HOLDINGS_CACHE_PATH) -> dict[str, HoldingProfile]:
    existing = load_profile_cache(cache_path)
    for profile in profiles:
        existing[profile.cache_key] = profile
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "profiles": {key: profile.to_dict() for key, profile in sorted(existing.items())},
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return existing


def refresh_profile_for_product(
    cfg,
    product,
    target_period: str = "",
    cache_path=DEFAULT_HOLDINGS_CACHE_PATH,
    notify: bool = False,
) -> Optional[HoldingProfile]:
    nav_product = _to_nav_product(product)
    try:
        if nav_product.provider == "citic_wealth":
            profile = _fetch_citic_profile(nav_product, target_period=target_period)
        elif nav_product.provider == "nanyin_wealth":
            profile = _fetch_nanyin_profile(nav_product, target_period=target_period)
        else:
            raise ProviderError(f"暂不支持该机构持仓画像：{nav_product.provider}")
        update_profile_cache([profile], cache_path=cache_path)
        return profile
    except Exception as exc:
        logger.warning("Refresh holding profile failed for %s %s: %s", nav_product.provider, nav_product.code, exc)
        return None


def refresh_all_profiles(cfg, cache_path=DEFAULT_HOLDINGS_CACHE_PATH) -> list[HoldingProfile]:
    profiles = []
    for item in getattr(getattr(cfg, "nav_monitor", None), "products", []):
        profile = refresh_profile_for_product(cfg, item, cache_path=cache_path)
        if profile:
            profiles.append(profile)
    return profiles


def refresh_quarterly_profiles_if_due(
    cfg,
    today: Optional[date] = None,
    cache_path=DEFAULT_HOLDINGS_CACHE_PATH,
    notify: bool = False,
) -> list[HoldingProfile]:
    today = today or date.today()
    target = target_quarter_for_check(today)
    cache = load_profile_cache(cache_path)
    refreshed = []
    for item in getattr(getattr(cfg, "nav_monitor", None), "products", []):
        product = _to_nav_product(item)
        cached = cache.get(cache_key(product.provider, product.code))
        if cached and cached.report_period == target and cached.status == "ok":
            continue
        profile = refresh_profile_for_product(cfg, product, target_period=target, cache_path=cache_path, notify=notify)
        if profile:
            refreshed.append(profile)
    return refreshed


def fetch_market_snapshot(trade_date: Optional[date] = None) -> MarketSnapshot:
    if trade_date:
        try:
            return _fetch_tencent_history_snapshot(trade_date)
        except Exception as exc:
            logger.warning("Tencent history snapshot failed, falling back to Eastmoney: %s", exc)
        return _fetch_eastmoney_history_snapshot(trade_date)
    try:
        return _fetch_tencent_market_snapshot()
    except Exception as exc:
        logger.warning("Tencent market snapshot failed, falling back to Sina: %s", exc)
    try:
        return _fetch_sina_market_snapshot()
    except Exception as exc:
        logger.warning("Sina market snapshot failed, falling back to Eastmoney: %s", exc)
    return _fetch_eastmoney_market_snapshot()


def _fetch_tencent_history_snapshot(trade_date: date) -> MarketSnapshot:
    symbols = {
        "sh000300": "沪深300",
        "sz399905": "中证500",
        "sz399006": "创业板指",
        "sh511260": "十年国债ETF",
        "sh511010": "国债ETF",
        "sh511880": "银华日利",
    }
    begin = (trade_date - timedelta(days=30)).strftime("%Y-%m-%d")
    end = trade_date.strftime("%Y-%m-%d")
    market_symbols = []
    trade_dates = []

    for symbol, name in symbols.items():
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
            + urllib.parse.urlencode({"param": f"{symbol},day,{begin},{end},40,qfq"})
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://gu.qq.com/",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:
            logger.warning("Tencent history snapshot failed for %s: %s", symbol, exc)
            continue

        block = (((data or {}).get("data") or {}).get(symbol) or {})
        raw_rows = block.get("qfqday") or block.get("day") or []
        rows = []
        for raw in raw_rows:
            if not isinstance(raw, (list, tuple)) or len(raw) < 3:
                continue
            row_date = _optional_date(raw[0])
            close = _optional_decimal(raw[2])
            if row_date and close is not None and row_date <= trade_date:
                rows.append((row_date, close))
        rows.sort(key=lambda item: item[0])
        if len(rows) < 2:
            continue

        row_date, close = rows[-1]
        _, previous_close = rows[-2]
        if previous_close == 0:
            continue
        change_pct = ((close - previous_close) / previous_close * Decimal("100")).quantize(Decimal("0.01"))
        market_symbols.append(MarketSymbol(name=name, code=symbol[2:], change_pct=change_pct))
        trade_dates.append(row_date)

    if not market_symbols:
        raise ProviderError(f"腾讯历史行情未获取到 {trade_date:%Y-%m-%d} 或之前交易日的数据")
    return MarketSnapshot(trade_date=max(trade_dates), symbols=tuple(market_symbols))


def _fetch_tencent_market_snapshot() -> MarketSnapshot:
    symbols = {
        "sh000300": "沪深300",
        "sz399905": "中证500",
        "sz399006": "创业板指",
        "sh511260": "十年国债ETF",
        "sh511010": "国债ETF",
        "sh511880": "银华日利",
    }
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        text = response.read().decode("gbk", "replace")

    rows = []
    trade_date = date.today()
    for code, fallback_name in symbols.items():
        match = re.search(rf'v_{code}="([^"]*)";', text)
        if not match:
            continue
        parts = match.group(1).split("~")
        if len(parts) < 32:
            continue
        current = _optional_decimal(parts[3])
        previous = _optional_decimal(parts[4])
        change_pct = _optional_decimal(parts[32]) if len(parts) > 32 else None
        if change_pct is None:
            if previous is None or current is None or previous == 0:
                continue
            change_pct = (current - previous) / previous * Decimal("100")
        rows.append(MarketSymbol(name=fallback_name, code=code[2:], change_pct=change_pct.quantize(Decimal("0.01"))))
        if len(parts) > 30 and len(parts[30]) >= 8:
            try:
                trade_date = datetime.strptime(parts[30][:8], "%Y%m%d").date()
            except ValueError:
                pass
    if not rows:
        raise ProviderError("腾讯行情未返回有效数据")
    return MarketSnapshot(trade_date=trade_date, symbols=tuple(rows))


def _fetch_sina_market_snapshot() -> MarketSnapshot:
    symbols = {
        "sh000300": "沪深300",
        "sz399905": "中证500",
        "sz399006": "创业板指",
        "sh511260": "十年国债ETF",
        "sh511010": "国债ETF",
        "sh511880": "银华日利",
    }
    url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        text = response.read().decode("gbk", "replace")

    rows = []
    trade_date = date.today()
    for code, fallback_name in symbols.items():
        match = re.search(rf'var hq_str_{code}="([^"]*)";', text)
        if not match:
            continue
        parts = match.group(1).split(",")
        if len(parts) < 32:
            continue
        name = parts[0] or fallback_name
        previous = _optional_decimal(parts[2])
        current = _optional_decimal(parts[3])
        if previous is None or current is None or previous == 0:
            continue
        change_pct = (current - previous) / previous * Decimal("100")
        rows.append(MarketSymbol(name=fallback_name, code=code[2:], change_pct=change_pct.quantize(Decimal("0.01"))))
        parsed_date = _optional_date(parts[30])
        if parsed_date:
            trade_date = parsed_date
    if not rows:
        raise ProviderError("新浪行情未返回有效数据")
    return MarketSnapshot(trade_date=trade_date, symbols=tuple(rows))


def _fetch_eastmoney_market_snapshot() -> MarketSnapshot:
    secids = {
        "1.000300": "沪深300",
        "0.399905": "中证500",
        "0.399006": "创业板指",
        "1.511260": "十年国债ETF",
        "1.511010": "国债ETF",
        "1.511880": "银华日利",
    }
    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get?"
        + urllib.parse.urlencode(
            {
                "fltt": "2",
                "fields": "f12,f14,f3",
                "secids": ",".join(secids),
            }
        )
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))
    rows = (((data or {}).get("data") or {}).get("diff") or [])
    symbols = []
    for row in rows:
        name = str(row.get("f14") or secids.get(row.get("f12"), row.get("f12") or ""))
        change = _optional_decimal(row.get("f3"))
        if change is not None:
            symbols.append(MarketSymbol(name=name, code=str(row.get("f12") or ""), change_pct=change))
    if not symbols:
        raise ProviderError("未获取到有效市场行情")
    return MarketSnapshot(trade_date=date.today(), symbols=tuple(symbols))


def _fetch_eastmoney_history_snapshot(trade_date: date) -> MarketSnapshot:
    secids = {
        "1.000300": "沪深300",
        "0.399905": "中证500",
        "0.399006": "创业板指",
        "1.511260": "十年国债ETF",
        "1.511010": "国债ETF",
        "1.511880": "银华日利",
    }
    begin = (trade_date - timedelta(days=20)).strftime("%Y%m%d")
    end = trade_date.strftime("%Y%m%d")
    symbols = []
    trade_dates = []
    for secid, name in secids.items():
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
            + urllib.parse.urlencode(
                {
                    "secid": secid,
                    "klt": "101",
                    "fqt": "1",
                    "beg": begin,
                    "end": end,
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                }
            )
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:
            logger.warning("Eastmoney history snapshot failed for %s: %s", secid, exc)
            continue

        klines = (((data or {}).get("data") or {}).get("klines") or [])
        selected = None
        for line in klines:
            parts = str(line).split(",")
            row_date = _optional_date(parts[0] if parts else "")
            if row_date and row_date <= trade_date:
                selected = (row_date, parts)
        if not selected:
            continue
        row_date, parts = selected
        change = _optional_decimal(parts[8] if len(parts) > 8 else None)
        if change is None:
            continue
        trade_dates.append(row_date)
        symbols.append(MarketSymbol(name=name, code=secid.split(".")[-1], change_pct=change))

    if not symbols:
        raise ProviderError(f"未获取到 {trade_date:%Y-%m-%d} 或之前交易日的历史行情")
    return MarketSnapshot(trade_date=max(trade_dates), symbols=tuple(symbols))


def build_estimate_report(
    cfg,
    snapshot: Optional[MarketSnapshot] = None,
    cache_path=DEFAULT_HOLDINGS_CACHE_PATH,
    generated_at: Optional[datetime] = None,
    market_date: Optional[date] = None,
) -> str:
    generated_at = generated_at or datetime.now()
    market_error = ""
    if snapshot is None:
        try:
            snapshot = fetch_market_snapshot(market_date)
        except Exception as exc:
            market_error = str(exc)
            snapshot = MarketSnapshot(trade_date=market_date or date.today(), symbols=())
    cache = load_profile_cache(cache_path)
    estimates = []
    for item in getattr(getattr(cfg, "nav_monitor", None), "products", []):
        product = _to_nav_product(item)
        profile = cache.get(cache_key(product.provider, product.code))
        top_asset_moves = ()
        if not market_error and profile and profile.top_assets:
            try:
                top_asset_moves = _fetch_top_asset_market_moves(profile.top_assets[:10], snapshot.trade_date)
            except Exception as exc:
                logger.warning(
                    "Fetch top asset market moves failed for %s %s: %s",
                    product.provider,
                    product.code,
                    exc,
                )
        estimates.append(_estimate_product(product, profile, snapshot, top_asset_moves=top_asset_moves))

    if market_error:
        lines = [
            "## 📈 理财涨跌方向参考",
            "",
            f"查询时间：{generated_at:%Y-%m-%d %H:%M}",
            f"行情日期：{snapshot.trade_date:%Y-%m-%d}",
            "总体判断：无法预估",
            "置信度：低",
            f"状态：行情获取失败，暂不输出方向判断。{market_error}",
        ]
        return "\n".join(lines)

    valid_scores = [estimate.score for estimate in estimates if not estimate.error]
    overall_score = sum(valid_scores, Decimal("0")) / Decimal(len(valid_scores)) if valid_scores else Decimal("0")
    overall = _direction_from_score(overall_score)
    confidence = _overall_confidence(estimates)

    lines = [
        "## 📈 理财涨跌方向参考",
        "",
        f"查询时间：{generated_at:%Y-%m-%d %H:%M}",
        f"行情日期：{snapshot.trade_date:%Y-%m-%d}",
        f"总体判断：{overall}",
        f"置信度：{confidence}",
        "口径：资产画像 + 前十可识别资产真实行情 + 市场代理指标，非完整持仓估值",
        "",
    ]
    if not estimates:
        lines.append("暂无净值产品，请先添加产品。")
        return "\n".join(lines)

    for index, estimate in enumerate(estimates, 1):
        title = f"{estimate.product.name or estimate.product.code}（{estimate.product.code}）"
        lines.append(f"### {index}. {title}")
        if estimate.error:
            lines.append(f"状态：无法预估，{estimate.error}")
            lines.append("")
            continue
        lines.append(
            f"判断：<font color=\"{_direction_color(estimate.direction)}\">{estimate.direction}</font>　"
            f"置信度：{estimate.confidence}"
        )
        if estimate.profile:
            lines.append(f"画像：{estimate.profile.report_period}（披露 {estimate.profile.disclosure_date or '-'}）")
        if estimate.reasons:
            lines.append("原因：" + "；".join(estimate.reasons[:4]))
        lines.append("")

    return "\n".join(lines).strip()


def push_estimate_report(
    cfg,
    to_user: str = None,
    cache_path=DEFAULT_HOLDINGS_CACHE_PATH,
    market_date: Optional[date] = None,
) -> str:
    from src.wechat_notifier import send_markdown

    report = build_estimate_report(cfg, cache_path=cache_path, market_date=market_date)
    send_markdown(cfg.wechat, report, to_user)
    return report


def format_holdings_profiles(code: str = "", cache_path=DEFAULT_HOLDINGS_CACHE_PATH) -> str:
    profiles = list(load_profile_cache(cache_path).values())
    if code:
        code_upper = code.upper()
        profiles = [profile for profile in profiles if profile.code.upper() == code_upper]
    if not profiles:
        return "## 🧭 持仓画像\n\n暂无持仓画像缓存，可发送：更新持仓画像"

    lines = ["## 🧭 持仓画像", ""]
    for profile in sorted(profiles, key=lambda item: item.code):
        lines.append(f"### {profile.name or profile.code}（{profile.code}）")
        lines.append(f"报告期：{profile.report_period or '-'}")
        lines.append(f"披露日期：{profile.disclosure_date or '-'}")
        rows = sorted(
            [row for row in profile.asset_mix if row.after_pct is not None],
            key=lambda row: row.after_pct or Decimal("0"),
            reverse=True,
        )
        if rows:
            lines.append("资产占比：" + "，".join(f"{row.asset_class}：{_format_pct(row.after_pct)}" for row in rows[:6]))
        if profile.top_assets:
            top_assets = profile.top_assets[:10]
            lines.append(f"前十资产（{len(top_assets)}项）：")
            for index, asset in enumerate(top_assets, start=1):
                label = asset.name
                if asset.code:
                    label = f"{asset.code} {label}"
                pct = f"（{_format_pct(asset.pct)}）" if asset.pct is not None else ""
                lines.append(f"{index}. {label}{pct}")
        lines.append("")
    return "\n".join(lines).strip()


def format_holdings_status(cache_path=DEFAULT_HOLDINGS_CACHE_PATH) -> str:
    profiles = sorted(load_profile_cache(cache_path).values(), key=lambda item: item.code)
    if not profiles:
        return "## 🧭 持仓画像状态\n\n暂无缓存。"
    lines = ["## 🧭 持仓画像状态", ""]
    for profile in profiles:
        status = "正常" if profile.status == "ok" else "异常"
        lines.append(f"- {profile.code}：{status}，{profile.report_period or '-'}，更新 {profile.updated_at:%Y-%m-%d %H:%M}")
    return "\n".join(lines)


def format_profile_update_result(profiles: list[HoldingProfile]) -> str:
    if not profiles:
        return "本次未发现可更新的持仓画像，可能官网尚未披露最新季度报告。"
    lines = ["## ✅ 持仓画像已更新", ""]
    for profile in profiles:
        lines.append(f"- {profile.name or profile.code}（{profile.code}）：{profile.report_period}")
    return "\n".join(lines)


def _estimate_product(
    product: NavProduct,
    profile: Optional[HoldingProfile],
    snapshot: MarketSnapshot,
    top_asset_moves: tuple[TopAssetMarketMove, ...] = (),
) -> ProductEstimate:
    if not profile:
        return ProductEstimate(
            product=product,
            direction="无法预估",
            confidence="低",
            score=Decimal("0"),
            reasons=(),
            error="缺少持仓画像",
        )
    changes = snapshot.changes_by_name()
    equity_move = _avg_change(changes, ("沪深300", "中证500", "创业板"))
    bond_move = _avg_change(changes, ("十年国债", "国债ETF"))
    cash_move = _avg_change(changes, ("银华日利",))

    exposures, top_asset_weights = _estimate_exposures(profile)
    equity_weight = exposures["equity"]
    bond_weight = exposures["bond"]
    cash_weight = exposures["cash"]
    unknown_weight = exposures["unknown"]
    equity_beta = _equity_beta_for_product(equity_weight, bond_weight, cash_weight)
    top_asset_moves = tuple(top_asset_moves or ())
    top_asset_total = len(profile.top_assets[:10])
    top_asset_coverage = sum((move.pct for move in top_asset_moves), Decimal("0"))
    top_asset_score = sum(
        (move.pct / Decimal("100")) * move.change_pct
        for move in top_asset_moves
        if move.pct is not None
    )
    top_asset_hit_weights = _top_asset_market_weights(top_asset_moves)
    residual_equity_weight = _positive_decimal(equity_weight - top_asset_hit_weights.get("equity", Decimal("0")))
    residual_bond_weight = _positive_decimal(bond_weight - top_asset_hit_weights.get("bond", Decimal("0")))
    residual_cash_weight = _positive_decimal(cash_weight - top_asset_hit_weights.get("cash", Decimal("0")))
    score = (
        top_asset_score
        + residual_equity_weight * equity_move * equity_beta
        + residual_bond_weight * bond_move * Decimal("0.65")
        + residual_cash_weight * cash_move * Decimal("0.20")
    )

    reasons = []
    top_asset_market_reason = _format_top_asset_market_reason(top_asset_moves, top_asset_total)
    if top_asset_market_reason:
        reasons.append(top_asset_market_reason)
    if equity_weight and equity_move > 0:
        if equity_weight < Decimal("0.15"):
            reasons.append(f"权益约{_format_pct(equity_weight * Decimal('100'))}，上涨贡献有限")
        else:
            reasons.append(f"权益约{_format_pct(equity_weight * Decimal('100'))}，权益偏强")
    elif equity_weight and equity_move < 0:
        if equity_weight < Decimal("0.15"):
            reasons.append(f"权益约{_format_pct(equity_weight * Decimal('100'))}，下跌拖累有限")
        else:
            reasons.append(f"权益约{_format_pct(equity_weight * Decimal('100'))}，权益拖累")
    if bond_weight and bond_move > 0:
        reasons.append(f"固收/债券约{_format_pct(bond_weight * Decimal('100'))}，债券偏强")
    elif bond_weight and bond_move < 0:
        reasons.append(f"固收/债券约{_format_pct(bond_weight * Decimal('100'))}，债券偏弱")
    if cash_weight:
        reasons.append(f"现金/存款约{_format_pct(cash_weight * Decimal('100'))}，贡献稳定")
    top_asset_reason = _format_top_asset_reason(top_asset_weights)
    if top_asset_reason:
        reasons.append(top_asset_reason)
    if unknown_weight > Decimal("0.10"):
        reasons.append(f"未映射资产约{_format_pct(unknown_weight * Decimal('100'))}，置信度下调")

    confidence = "中"
    if unknown_weight > Decimal("0.15") or not profile.asset_mix:
        confidence = "低"
    if _profile_age_days(profile) > 150:
        confidence = "低"
        reasons.append("画像披露时间偏久")

    return ProductEstimate(
        product=product,
        direction=_direction_from_score(score),
        confidence=confidence,
        score=score,
        reasons=tuple(reasons or ("市场代理指标整体平稳",)),
        profile=profile,
        top_asset_hits=top_asset_moves,
        top_asset_total=top_asset_total,
        top_asset_coverage=top_asset_coverage,
    )


def _estimate_exposures(profile: HoldingProfile) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    exposures = {
        "equity": Decimal("0"),
        "bond": Decimal("0"),
        "cash": Decimal("0"),
        "unknown": Decimal("0"),
    }
    fund_top_weights = _classify_fund_like_top_assets(profile.top_assets)
    fund_top_total = sum(fund_top_weights.values(), Decimal("0"))
    has_after_pct = any(row.after_pct is not None for row in profile.asset_mix)

    for row in profile.asset_mix:
        pct = row.after_pct if has_after_pct else row.before_pct
        pct = pct or Decimal("0")
        weight = pct / Decimal("100")
        if weight <= 0:
            continue
        asset_class = row.asset_class
        if _contains_any(asset_class, ("公募基金", "混合", "资产管理产品", "委外投资")):
            scale = Decimal("1")
            if fund_top_total > weight and fund_top_total:
                scale = weight / fund_top_total
            allocated = Decimal("0")
            for category, top_weight in fund_top_weights.items():
                amount = top_weight * scale
                exposures[category] += amount
                allocated += amount
            if weight > allocated:
                exposures["unknown"] += weight - allocated
        elif _contains_any(asset_class, ("权益", "股票")):
            exposures["equity"] += weight
        elif _contains_any(asset_class, ("债券", "债权", "固定收益", "存单", "拆放", "买入返售")):
            exposures["bond"] += weight
        elif _contains_any(asset_class, ("现金", "银行存款", "存款")):
            exposures["cash"] += weight
        else:
            exposures["unknown"] += weight
    return exposures, fund_top_weights


def _equity_beta_for_product(equity_weight: Decimal, bond_weight: Decimal, cash_weight: Decimal) -> Decimal:
    defensive_weight = bond_weight + cash_weight
    if defensive_weight >= Decimal("0.70") and equity_weight <= Decimal("0.12"):
        return Decimal("0.08")
    if defensive_weight >= Decimal("0.60") and equity_weight <= Decimal("0.18"):
        return Decimal("0.15")
    if equity_weight <= Decimal("0.25"):
        return Decimal("0.30")
    return Decimal("0.60")


def _fetch_top_asset_market_moves(top_assets: tuple[TopAsset, ...], trade_date: date) -> tuple[TopAssetMarketMove, ...]:
    moves = []
    for asset in top_assets:
        if asset.pct is None or asset.pct <= 0:
            continue
        resolved = _resolve_top_asset_symbol(asset)
        if not resolved:
            continue
        symbol, category = resolved
        try:
            change_pct = _fetch_tencent_symbol_history_change(symbol, trade_date)
        except Exception as exc:
            logger.warning("Fetch top asset quote failed for %s %s: %s", asset.name, symbol, exc)
            continue
        moves.append(
            TopAssetMarketMove(
                name=asset.name,
                pct=asset.pct,
                category=category,
                change_pct=change_pct,
                symbol=symbol,
            )
        )
    return tuple(moves)


def _resolve_top_asset_symbol(asset: TopAsset) -> Optional[tuple[str, str]]:
    name = asset.name or ""
    text = name.upper()
    aliases = (
        ("东方盛虹", "sz000301", "equity"),
        ("中证A500ETF国泰", "sz159338", "equity"),
    )
    for keyword, symbol, category in aliases:
        if keyword.upper() in text:
            return symbol, category

    code = re.sub(r"\D", "", asset.code or "")
    if not re.fullmatch(r"\d{6}", code):
        return None
    if _looks_like_open_end_fund(asset):
        return None
    market = _exchange_prefix_for_security_code(code)
    if not market:
        return None
    return market + code, _quote_category_for_asset(asset)


def _exchange_prefix_for_security_code(code: str) -> str:
    if code.startswith(("600", "601", "603", "605", "688", "689", "510", "511", "512", "513", "515", "516", "517", "518", "519", "520", "560", "561", "562", "563", "588", "589")):
        return "sh"
    if code.startswith(("000", "001", "002", "003", "300", "301", "159")):
        return "sz"
    return ""


def _looks_like_open_end_fund(asset: TopAsset) -> bool:
    name = asset.name or ""
    code = re.sub(r"\D", "", asset.code or "")
    if _contains_any(name, ("联接", "ETF联接")):
        return True
    if code.startswith(("00", "01", "02", "03", "04")) and _contains_any(name, ("基金", "纯债", "债券", "混合", "指数")):
        return True
    return False


def _quote_category_for_asset(asset: TopAsset) -> str:
    text = (asset.name or "").upper()
    if _contains_any(text, ("货币", "现金", "日利")):
        return "cash"
    if _contains_any(text, ("纯债", "短债", "短融", "债券", "信用债", "利率债", "国债", "政金债", "债ETF")):
        return "bond"
    if _contains_any(text, ("黄金", "GOLD")):
        return "unknown"
    return "equity"


def _fetch_tencent_symbol_history_change(symbol: str, trade_date: date) -> Decimal:
    begin = (trade_date - timedelta(days=30)).strftime("%Y-%m-%d")
    end = trade_date.strftime("%Y-%m-%d")
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        + urllib.parse.urlencode({"param": f"{symbol},day,{begin},{end},40,qfq"})
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8", "replace"))

    block = (((data or {}).get("data") or {}).get(symbol) or {})
    raw_rows = block.get("qfqday") or block.get("day") or []
    rows = []
    for raw in raw_rows:
        if not isinstance(raw, (list, tuple)) or len(raw) < 3:
            continue
        row_date = _optional_date(raw[0])
        close = _optional_decimal(raw[2])
        if row_date and close is not None and row_date <= trade_date:
            rows.append((row_date, close))
    rows.sort(key=lambda item: item[0])
    if len(rows) < 2:
        raise ProviderError(f"腾讯历史行情未获取到 {symbol} {trade_date:%Y-%m-%d} 或之前交易日数据")
    _, close = rows[-1]
    _, previous_close = rows[-2]
    if previous_close == 0:
        raise ProviderError(f"腾讯历史行情 {symbol} 前收盘价为 0")
    return ((close - previous_close) / previous_close * Decimal("100")).quantize(Decimal("0.01"))


def _top_asset_market_weights(moves: tuple[TopAssetMarketMove, ...]) -> dict[str, Decimal]:
    weights = {
        "equity": Decimal("0"),
        "bond": Decimal("0"),
        "cash": Decimal("0"),
    }
    for move in moves:
        if move.pct is None or move.category not in weights:
            continue
        weights[move.category] += move.pct / Decimal("100")
    return weights


def _format_top_asset_market_reason(moves: tuple[TopAssetMarketMove, ...], total: int) -> str:
    if total <= 0:
        return ""
    if not moves:
        return f"前十资产真实行情：命中0/{total}，继续使用画像代理"
    coverage = sum((move.pct for move in moves if move.pct is not None), Decimal("0"))
    highlights = "，".join(
        f"{move.name}{_format_signed_pct(move.change_pct)}"
        for move in sorted(moves, key=lambda item: item.pct or Decimal("0"), reverse=True)[:3]
    )
    return f"前十资产真实行情：命中{len(moves)}/{total}，覆盖{_format_pct(coverage)}，{highlights}"


def _positive_decimal(value: Decimal) -> Decimal:
    return value if value > 0 else Decimal("0")


def _classify_fund_like_top_assets(top_assets: tuple[TopAsset, ...]) -> dict[str, Decimal]:
    weights = {
        "equity": Decimal("0"),
        "bond": Decimal("0"),
        "cash": Decimal("0"),
        "unknown": Decimal("0"),
    }
    for asset in top_assets:
        if asset.pct is None:
            continue
        category = _classify_fund_like_asset(asset.name)
        if not category:
            continue
        weights[category] += asset.pct / Decimal("100")
    return {key: value for key, value in weights.items() if value > 0}


def _classify_fund_like_asset(name: str) -> str:
    text = (name or "").upper()
    if not text:
        return ""
    if _contains_any(text, ("黄金", "GOLD")):
        return "unknown"
    if _contains_any(text, ("纯债", "短债", "短融", "债券", "信用债", "利率债", "国债", "政金债", "债ETF", "债E")):
        return "bond"
    if _contains_any(text, ("货币", "现金", "日利")):
        return "cash"
    if _contains_any(text, ("ETF", "联接", "沪深", "中证", "A500", "创业板", "科创", "标普", "纳指", "NASDAQ", "股票", "红利")):
        return "equity"
    return ""


def _format_top_asset_reason(weights: dict[str, Decimal]) -> str:
    parts = []
    if weights.get("equity"):
        parts.append(f"权益约{_format_pct(weights['equity'] * Decimal('100'))}")
    if weights.get("bond"):
        parts.append(f"固收/债券约{_format_pct(weights['bond'] * Decimal('100'))}")
    if weights.get("cash"):
        parts.append(f"现金约{_format_pct(weights['cash'] * Decimal('100'))}")
    if weights.get("unknown"):
        parts.append(f"未映射约{_format_pct(weights['unknown'] * Decimal('100'))}")
    return "前十资产识别：" + "，".join(parts) if parts else ""


def _fetch_citic_profile(product: NavProduct, target_period: str = "") -> HoldingProfile:
    base_code = _citic_base_code(product.code)
    rows = _citic_search_reports(base_code)
    report = _select_quarterly_report(rows, target_period=target_period)
    if not report:
        raise ProviderError(f"未找到 {target_period or '最新季度'} 运行公告")
    source_url = _citic_document_url(report["url"])
    pdf_bytes = _download_url(source_url)
    text = _extract_pdf_text(pdf_bytes)
    return parse_holding_profile_from_text(
        product,
        text,
        report_title=report["title"],
        disclosure_date=_optional_date(report.get("date")),
        source_url=source_url,
    )


def _fetch_nanyin_profile(product: NavProduct, target_period: str = "") -> HoldingProfile:
    client = NanyinHttpClient(timeout=20)
    rows = []
    current_page = 1
    while current_page <= 5:
        data = client.post_encrypted_json(
            NANYIN_ANNOUNCEMENT_PATH,
            {"productCode": product.code, "productType": "CPGG", "currentPage": current_page},
        )
        rows.extend(data.get("aaData") or [])
        if not data.get("hasNext"):
            break
        current_page += 1
    report = _select_quarterly_report(rows, target_period=target_period)
    if not report:
        raise ProviderError(f"未找到 {target_period or '最新季度'} 定期报告")

    detail = client.post_encrypted_json(NANYIN_ARTICLE_DETAIL_PATH, {"articleId": report["id"]})
    attachments = detail.get("attachmentList") or []
    if not attachments:
        raise ProviderError("南银报告无 PDF 附件")
    attachment_id = attachments[0]["id"]
    source_url = client.BASE_URL + NANYIN_DOWNLOAD_PATH + "&attachmentId=" + urllib.parse.quote(attachment_id)
    request = urllib.request.Request(source_url, headers=client._headers(""))
    with client.opener.open(request, timeout=30) as response:
        pdf_bytes = response.read()
    text = _extract_pdf_text(pdf_bytes)
    return parse_holding_profile_from_text(
        product,
        text,
        report_title=str(detail.get("title") or report.get("title") or ""),
        disclosure_date=_optional_date(detail.get("showDate") or report.get("createDate")),
        source_url=source_url,
    )


def _citic_search_reports(base_code: str) -> list[dict]:
    url = CITIC_DISCLOSURE_SEARCH + "?" + urllib.parse.urlencode(
        {"channelid": CITIC_PERIODIC_CHANNEL, "searchword": base_code, "page": 1}
    )
    request = urllib.request.Request(
        url,
        data=b"",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    with urllib.request.urlopen(request, context=_legacy_ssl_context(), timeout=20) as response:
        return (json.loads(response.read().decode("utf-8", "replace")) or {}).get("data") or []


def _select_quarterly_report(rows: list[dict], target_period: str = "") -> Optional[dict]:
    candidates = []
    for row in rows:
        title = str(row.get("title") or "")
        period = _extract_report_period(title)
        if not period or "Q" not in period:
            continue
        if target_period and period != target_period:
            continue
        if not _contains_any(title, ("季度", "季报", "季度报告", "季度运行公告")):
            continue
        candidates.append((period, _optional_date(row.get("date") or row.get("createDate")) or date.min, row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise ProviderError("缺少 pypdf，无法解析持仓画像 PDF") from exc

    path = Path(tempfile.gettempdir()) / f"nav_holdings_{os.getpid()}_{datetime.now().timestamp()}.pdf"
    path.write_bytes(pdf_bytes)
    try:
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _download_url(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, context=_legacy_ssl_context(), timeout=30) as response:
        return response.read()


def _citic_document_url(channel_fragment: str) -> str:
    return (
        CITIC_DOCUMENT_URL
        + "?columnname=file_path_new&multino=1&downloadtype=open&channelid="
        + str(channel_fragment)
    )


def _citic_base_code(code: str) -> str:
    code = (code or "").upper().strip()
    if len(code) > 2 and code[-1].isalpha() and code[-2].isdigit():
        return code[:-1]
    return code


def _parse_asset_mix_rows(text: str) -> list[AssetMixRow]:
    rows = []
    for asset_class in _asset_class_names():
        line = _line_containing_asset(text, asset_class)
        if not line:
            continue
        tokens = _number_or_dash_tokens(line[line.find(asset_class) + len(asset_class):])
        if "%" in line and len(tokens) >= 2 and asset_class in ("固定收益类", "权益类", "商品及金融衍生品类", "混合类"):
            before_pct = _optional_decimal(tokens[0])
            after_pct = _optional_decimal(tokens[1])
        elif len(tokens) >= 4:
            before_pct = _optional_decimal(tokens[1])
            after_pct = _optional_decimal(tokens[3])
        else:
            continue
        rows.append(AssetMixRow(asset_class, before_pct, after_pct))
    return rows


def _parse_top_assets(text: str) -> list[TopAsset]:
    section = _after_first(text, ("前十项资产", "前十名资产"))
    if not section:
        return []
    section = section[:2500]
    assets = []
    coded = re.compile(
        r"(?m)^\s*\d+\s+([A-Za-z0-9]+)\s+(.+?)\s+([0-9][0-9,]*\.\d{2})\s+([0-9]+(?:\.\d+)?)\s*$"
    )
    for match in coded.finditer(section):
        assets.append(
            TopAsset(
                code=match.group(1),
                name=_clean_asset_name(match.group(2)),
                amount=_optional_decimal(match.group(3)),
                pct=_optional_decimal(match.group(4)),
            )
        )
        if len(assets) >= 10:
            return assets

    uncoded = re.compile(r"(?m)^\s*\d+\s+(.+?)\s+([0-9][0-9,]*\.\d{2})\s+([0-9]+(?:\.\d+)?)\s*$")
    for match in uncoded.finditer(section):
        name = _clean_asset_name(match.group(1))
        if name and not name.startswith(("资产名称", "代码", "序号")):
            assets.append(TopAsset(name=name, amount=_optional_decimal(match.group(2)), pct=_optional_decimal(match.group(3))))
        if len(assets) >= 10:
            break
    return assets


def _normalize_pdf_text(text: str) -> str:
    text = (text or "").replace("\r", "\n")
    replacements = {
        "拆放同业及买入\n返售": "拆放同业及买入返售",
        "非标准化债权类\n资产": "非标准化债权类资产",
        "委外投资--协议\n方式": "委外投资--协议方式",
        "商品及金融衍\n生品类": "商品及金融衍生品类",
        "资产管理产\n品": "资产管理产品",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"[ \t]+", " ", text)


def _line_containing_asset(text: str, asset_class: str) -> str:
    pattern = re.compile(rf"(?m)^\s*\d+\s+{re.escape(asset_class)}\s+.+$")
    match = pattern.search(text)
    return match.group(0) if match else ""


def _asset_class_names() -> tuple[str, ...]:
    return (
        "现金及银行存款",
        "同业存单",
        "拆放同业及买入返售",
        "债券",
        "非标准化债权类资产",
        "权益类投资",
        "金融衍生品",
        "境外资产",
        "商品类资产",
        "另类资产",
        "公募基金",
        "私募基金",
        "资产管理产品",
        "委外投资--协议方式",
        "其他资产",
        "固定收益类",
        "权益类",
        "商品及金融衍生品类",
        "混合类",
    )


def _extract_report_period(title: str) -> str:
    title = title or ""
    match = re.search(r"(20\d{2})年(?:第)?([1-4])季度", title)
    if not match:
        match = re.search(r"(20\d{2})年([1-4])季度", title)
    if not match:
        match = re.search(r"(20\d{2})年([1-4])季", title)
    if match:
        return f"{match.group(1)}Q{match.group(2)}"
    match = re.search(r"(20\d{2})年年度", title)
    if match:
        return f"{match.group(1)}Y"
    match = re.search(r"(20\d{2})年半年度", title)
    if match:
        return f"{match.group(1)}H1"
    return ""


def _after_first(text: str, markers: tuple[str, ...]) -> str:
    positions = [text.find(marker) for marker in markers if text.find(marker) >= 0]
    if not positions:
        return ""
    return text[min(positions):]


def _number_or_dash_tokens(value: str) -> list[str]:
    return re.findall(r"-|[0-9][0-9,]*(?:\.[0-9]+)?", value or "")


def _clean_asset_name(value: str) -> str:
    return re.sub(r"\s+", "", value or "").strip()


def _to_nav_product(item) -> NavProduct:
    if isinstance(item, NavProduct):
        return item
    return NavProduct(
        provider=getattr(item, "provider", ""),
        code=getattr(item, "code", ""),
        name=getattr(item, "name", "") or getattr(item, "code", ""),
    )


def _avg_change(changes: dict[str, Decimal], keywords: tuple[str, ...]) -> Decimal:
    values = [
        value
        for name, value in changes.items()
        if any(keyword in name for keyword in keywords)
    ]
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _direction_from_score(score: Decimal) -> str:
    if score > Decimal("0.03"):
        return "预计上涨"
    if score < Decimal("-0.03"):
        return "预计下跌"
    return "偏平"


def _direction_color(direction: str) -> str:
    if direction == "预计上涨":
        return "warning"
    if direction == "预计下跌":
        return "info"
    return "comment"


def _overall_confidence(estimates: list[ProductEstimate]) -> str:
    if not estimates or any(estimate.error for estimate in estimates):
        return "低"
    if any(estimate.confidence == "低" for estimate in estimates):
        return "低"
    if any(estimate.confidence == "中" for estimate in estimates):
        return "中"
    return "高"


def _profile_age_days(profile: HoldingProfile) -> int:
    if not profile.disclosure_date:
        return 999
    return (date.today() - profile.disclosure_date).days


def _contains_any(content: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in (content or "") for keyword in keywords)


def _optional_decimal(value) -> Optional[Decimal]:
    if value is None or value == "" or value == "-":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("%", ""))
    except (InvalidOperation, ValueError):
        return None


def _decimal_to_str(value: Optional[Decimal]) -> str:
    return "" if value is None else str(value)


def _optional_date(value) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not value:
        return None
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _optional_datetime(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _format_pct(value: Optional[Decimal]) -> str:
    if value is None:
        return "--"
    return f"{value:.2f}%"


def _format_signed_pct(value: Optional[Decimal]) -> str:
    if value is None:
        return "--"
    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.2f}%"
