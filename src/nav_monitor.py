import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import ssl
import string
import textwrap
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional


logger = logging.getLogger(__name__)


class ProviderError(Exception):
    pass


PROVIDER_LABELS = {
    "citic_wealth": "信银理财",
    "nanyin_wealth": "南银理财",
}
PROVIDER_ALIASES = {
    "信银": "citic_wealth",
    "信银理财": "citic_wealth",
    "citic": "citic_wealth",
    "citic_wealth": "citic_wealth",
    "南银": "nanyin_wealth",
    "南银理财": "nanyin_wealth",
    "nanyin": "nanyin_wealth",
    "nanyin_wealth": "nanyin_wealth",
}

DEFAULT_PENDING_NAV_ADD_PATH = "/tmp/nav_monitor_pending_add.json"
PENDING_NAV_ADD_TTL_SECONDS = 300


@dataclass(frozen=True)
class NavProduct:
    provider: str
    code: str
    name: str


@dataclass(frozen=True)
class NavRecord:
    provider: str
    code: str
    name: str
    nav_date: date
    unit_nav: Decimal
    cumulative_nav: Optional[Decimal] = None
    source: str = ""


@dataclass(frozen=True)
class ProductCandidate:
    provider: str
    code: str
    name: str
    register_code: str = ""
    sales_code: str = ""
    latest_nav_date: Optional[date] = None
    latest_unit_nav: Optional[Decimal] = None
    latest_cumulative_nav: Optional[Decimal] = None
    nav_error: str = ""


@dataclass(frozen=True)
class DateQueryResult:
    target_date: date
    exact: bool
    record: Optional[NavRecord] = None
    previous: Optional[NavRecord] = None
    error: str = ""


@dataclass(frozen=True)
class ProductNavResult:
    product: NavProduct
    latest: Optional[NavRecord] = None
    previous: Optional[NavRecord] = None
    query_result: Optional[DateQueryResult] = None
    error: str = ""


@dataclass(frozen=True)
class NavCommand:
    action: str
    target_date: Optional[date] = None
    provider: str = ""
    query: str = ""
    code: str = ""
    index: int = 0
    hour: int = 0
    minute: int = 0


def parse_nav_query_date(text: str, base_date: Optional[date] = None) -> Optional[date]:
    base_date = base_date or date.today()
    content = (text or "").strip()

    if "前天" in content:
        return base_date - timedelta(days=2)
    if "昨天" in content or "昨日" in content:
        return base_date - timedelta(days=1)
    if "今天" in content or "今日" in content:
        return base_date

    match = re.search(r"(?<!\d)(20\d{6})(?!\d)", content)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def parse_nav_command(text: str, base_date: Optional[date] = None) -> Optional[NavCommand]:
    content = (text or "").strip()
    if not content:
        return None

    match = re.fullmatch(r"确认添加净值产品\s+(\d+)", content)
    if match:
        return NavCommand(action="confirm_add", index=int(match.group(1)))
    if content == "取消添加净值产品":
        return NavCommand(action="cancel_add")

    if "净值" not in content:
        return None

    if content == "查看净值配置":
        return NavCommand(action="view_config")
    if content == "开启净值监控":
        return NavCommand(action="enable")
    if content == "关闭净值监控":
        return NavCommand(action="disable")

    match = re.fullmatch(r"设置净值推送时间\s+([01]?\d|2[0-3]):([0-5]\d)", content)
    if match:
        return NavCommand(
            action="set_time",
            hour=int(match.group(1)),
            minute=int(match.group(2)),
        )

    match = re.fullmatch(r"添加净值产品\s+(\S+)\s+(.+)", content)
    if match:
        provider = _normalize_provider_alias(match.group(1))
        if not provider:
            return NavCommand(action="unknown_provider", query=match.group(2).strip())
        return NavCommand(
            action="add_product",
            provider=provider,
            query=match.group(2).strip(),
        )

    match = re.fullmatch(r"删除净值产品\s+(\S+)", content)
    if match:
        return NavCommand(action="delete_product", code=match.group(1).strip())

    target_date = parse_nav_query_date(content, base_date=base_date)
    if target_date:
        return NavCommand(action="query_date", target_date=target_date)

    if content in ("立即查询净值", "查询净值", "净值监控"):
        return NavCommand(action="query_latest")

    return None


def calculate_change(latest: NavRecord, previous: NavRecord) -> tuple[Decimal, Optional[Decimal]]:
    delta = latest.unit_nav - previous.unit_nav
    if previous.unit_nav == 0:
        return delta, None
    return delta, delta / previous.unit_nav * Decimal("100")


def _normalize_provider_alias(value: str) -> str:
    return PROVIDER_ALIASES.get((value or "").strip().lower()) or PROVIDER_ALIASES.get((value or "").strip())


def format_nav_report(
    results: list[ProductNavResult],
    title: str = "理财净值日报",
    generated_at: Optional[datetime] = None,
    target_date: Optional[date] = None,
) -> str:
    generated_at = generated_at or datetime.now()
    lines = [f"# {title}", f"查询时间：{generated_at:%Y-%m-%d %H:%M}"]
    if target_date:
        lines.append(f"查询日期：{target_date:%Y-%m-%d}")

    for result in results:
        lines.append("")
        lines.append(f"## {result.product.name or result.product.code}")
        lines.append(f"代码：{result.product.code}")

        if result.error:
            lines.append(f"状态：查询失败：{result.error}")
            continue

        if result.query_result:
            _append_date_query(lines, result.query_result)
            continue

        if not result.latest:
            lines.append("状态：暂无净值")
            continue

        lines.append(f"最新：{_format_record(result.latest)}")
        if result.previous:
            lines.append(f"上期：{_format_record(result.previous)}")
            delta, delta_pct = calculate_change(result.latest, result.previous)
            if delta_pct is None:
                lines.append(f"涨跌：{_format_decimal(delta)}（无法计算百分比）")
            else:
                lines.append(f"涨跌：{_format_decimal(delta)}（{_format_pct(delta_pct)}）")
        else:
            lines.append("状态：暂无上一条净值，无法计算涨跌")

    return "\n".join(lines)


def _append_date_query(lines: list[str], query: DateQueryResult):
    lines.append(f"查询日期：{query.target_date:%Y-%m-%d}")
    if query.error:
        lines.append(f"状态：查询失败：{query.error}")
        return
    if not query.record:
        lines.append("状态：未找到该日期之前的净值")
        return

    if query.exact:
        lines.append(f"净值：{_format_record(query.record)}")
    else:
        lines.append("状态：该日未披露")
        lines.append(f"最近披露：{_format_record(query.record)}")

    if query.previous:
        delta, delta_pct = calculate_change(query.record, query.previous)
        if delta_pct is None:
            lines.append(f"较上期：{_format_decimal(delta)}（无法计算百分比）")
        else:
            lines.append(f"较上期：{_format_decimal(delta)}（{_format_pct(delta_pct)}）")


def _format_record(record: NavRecord) -> str:
    return f"{record.nav_date:%Y-%m-%d}  {_format_decimal(record.unit_nav)}"


def _format_decimal(value) -> str:
    try:
        return f"{Decimal(str(value)).quantize(Decimal('0.000001'))}"
    except (InvalidOperation, ValueError):
        return str(value)


def _format_pct(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.0001'))}%"


def build_add_product_candidates(provider: "WealthProvider", query: str, limit: int = 5) -> list[ProductCandidate]:
    return provider.search_products(query)[:limit]


def format_product_candidates(candidates: list[ProductCandidate]) -> str:
    if not candidates:
        return "未找到该产品，请改用产品代码、销售代码、登记编码或更完整的产品名称重试。"

    lines = ["找到以下候选产品，请确认：", ""]
    for index, candidate in enumerate(candidates, 1):
        label = PROVIDER_LABELS.get(candidate.provider, candidate.provider)
        lines.append(f"{index}. {label}")
        lines.append(f"名称：{candidate.name or '-'}")
        lines.append(f"代码：{candidate.code or '-'}")
        if candidate.register_code:
            lines.append(f"登记编码：{candidate.register_code}")
        if candidate.sales_code and candidate.sales_code != candidate.code:
            lines.append(f"销售代码：{candidate.sales_code}")

        if candidate.latest_nav_date and candidate.latest_unit_nav is not None:
            lines.append(
                "最新净值："
                f"{candidate.latest_nav_date:%Y-%m-%d}  {_format_decimal(candidate.latest_unit_nav)}"
            )
            if candidate.latest_cumulative_nav is not None:
                lines.append(f"累计净值：{_format_decimal(candidate.latest_cumulative_nav)}")
        else:
            reason = candidate.nav_error or "接口未返回最新净值"
            lines.append(f"最新净值：暂不可用（{reason}）")
        lines.append("")

    lines.append("回复：确认添加净值产品 1")
    lines.append("回复：取消添加净值产品 可放弃本次添加")
    return "\n".join(lines).rstrip()


def save_pending_nav_add(
    candidates: list[ProductCandidate],
    pending_path: str = DEFAULT_PENDING_NAV_ADD_PATH,
    now: Optional[datetime] = None,
):
    data = {
        "created_at": (now or datetime.now()).timestamp(),
        "candidates": [_candidate_to_dict(candidate) for candidate in candidates],
    }
    with open(pending_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_pending_nav_add(
    pending_path: str = DEFAULT_PENDING_NAV_ADD_PATH,
    now: Optional[datetime] = None,
) -> list[ProductCandidate]:
    if not os.path.exists(pending_path):
        return []
    try:
        with open(pending_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        clear_pending_nav_add(pending_path)
        return []

    created_at = float(data.get("created_at") or 0)
    current = (now or datetime.now()).timestamp()
    if current - created_at > PENDING_NAV_ADD_TTL_SECONDS:
        clear_pending_nav_add(pending_path)
        return []
    return [_candidate_from_dict(item) for item in data.get("candidates", [])]


def clear_pending_nav_add(pending_path: str = DEFAULT_PENDING_NAV_ADD_PATH):
    try:
        if os.path.exists(pending_path):
            os.remove(pending_path)
    except OSError:
        logger.warning("Failed to remove NAV pending add file: %s", pending_path)


def cancel_pending_nav_add(pending_path: str = DEFAULT_PENDING_NAV_ADD_PATH) -> str:
    clear_pending_nav_add(pending_path)
    return "已取消添加净值产品。"


def confirm_pending_nav_add(
    cfg,
    index: int,
    pending_path: str = DEFAULT_PENDING_NAV_ADD_PATH,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    candidates = load_pending_nav_add(pending_path, now=now)
    if not candidates:
        return False, "当前没有待确认的净值产品，或确认已超时，请重新发送添加命令。"
    if index < 1 or index > len(candidates):
        return False, f"序号无效，请回复 1 到 {len(candidates)} 之间的序号。"

    candidate = candidates[index - 1]
    products = getattr(getattr(cfg, "nav_monitor", None), "products", [])
    for product in products:
        if (
            getattr(product, "provider", "").lower() == candidate.provider.lower()
            and getattr(product, "code", "").upper() == candidate.code.upper()
        ):
            clear_pending_nav_add(pending_path)
            return False, f"净值产品已存在：{candidate.name or candidate.code}"

    from src.config import NavProductConfig

    products.append(
        NavProductConfig(
            {
                "provider": candidate.provider,
                "code": candidate.code,
                "name": candidate.name,
            }
        )
    )
    clear_pending_nav_add(pending_path)
    return True, f"已添加净值产品：{candidate.name or candidate.code}"


def query_nav_products(cfg, target_date: Optional[date] = None) -> list[ProductNavResult]:
    products = getattr(getattr(cfg, "nav_monitor", None), "products", [])
    results = []
    for item in products:
        product = NavProduct(
            provider=getattr(item, "provider", ""),
            code=getattr(item, "code", ""),
            name=getattr(item, "name", "") or getattr(item, "code", ""),
        )
        try:
            provider = get_provider(product.provider)
            if target_date:
                query_result = provider.fetch_by_date(product, target_date)
                results.append(ProductNavResult(product=product, query_result=query_result))
            else:
                records = provider.fetch_latest(product)
                latest = records[0] if records else None
                previous = records[1] if len(records) > 1 else None
                results.append(ProductNavResult(product=product, latest=latest, previous=previous))
        except Exception as exc:
            logger.error("NAV query failed for %s %s: %s", product.provider, product.code, exc, exc_info=True)
            results.append(ProductNavResult(product=product, error=str(exc)))
    return results


def build_nav_report(
    cfg,
    target_date: Optional[date] = None,
    generated_at: Optional[datetime] = None,
    title: str = None,
) -> str:
    products = getattr(getattr(cfg, "nav_monitor", None), "products", [])
    if not products:
        return "当前未配置净值产品，请先发送：添加净值产品 信银 AF233276B"
    if not title:
        title = "理财净值查询" if target_date else "理财净值日报"
    return format_nav_report(
        query_nav_products(cfg, target_date=target_date),
        title=title,
        generated_at=generated_at,
        target_date=target_date,
    )


def push_nav_report(cfg, target_date: Optional[date] = None, to_user: str = None) -> str:
    from src.wechat_notifier import send_markdown

    report = build_nav_report(cfg, target_date=target_date)
    send_markdown(cfg.wechat, report, to_user)
    return report


def format_nav_config(cfg) -> str:
    nav = cfg.nav_monitor
    status = "开启" if nav.enabled else "关闭"
    lines = [
        "# 净值监控配置",
        f"状态：{status}",
        f"推送时间：{nav.push_hour:02d}:{nav.push_minute:02d}",
        "",
        "产品：",
    ]
    if not nav.products:
        lines.append("- 暂无")
    for index, product in enumerate(nav.products, 1):
        label = PROVIDER_LABELS.get(product.provider, product.provider)
        lines.append(f"{index}. {label} {product.code} {product.name}")
    return "\n".join(lines)


def delete_nav_product(cfg, code: str) -> tuple[bool, str]:
    code_upper = (code or "").strip().upper()
    products = getattr(cfg.nav_monitor, "products", [])
    for index, product in enumerate(list(products)):
        if getattr(product, "code", "").upper() == code_upper:
            removed = products.pop(index)
            return True, f"已删除净值产品：{getattr(removed, 'name', '') or code_upper}"
    return False, f"未找到净值产品：{code}"


def _candidate_to_dict(candidate: ProductCandidate) -> dict:
    return {
        "provider": candidate.provider,
        "code": candidate.code,
        "name": candidate.name,
        "register_code": candidate.register_code,
        "sales_code": candidate.sales_code,
        "latest_nav_date": candidate.latest_nav_date.isoformat() if candidate.latest_nav_date else "",
        "latest_unit_nav": str(candidate.latest_unit_nav) if candidate.latest_unit_nav is not None else "",
        "latest_cumulative_nav": (
            str(candidate.latest_cumulative_nav)
            if candidate.latest_cumulative_nav is not None
            else ""
        ),
        "nav_error": candidate.nav_error,
    }


def _candidate_from_dict(data: dict) -> ProductCandidate:
    return ProductCandidate(
        provider=str(data.get("provider") or ""),
        code=str(data.get("code") or ""),
        name=str(data.get("name") or ""),
        register_code=str(data.get("register_code") or ""),
        sales_code=str(data.get("sales_code") or ""),
        latest_nav_date=_optional_nav_date(data.get("latest_nav_date")),
        latest_unit_nav=_optional_decimal(data.get("latest_unit_nav")),
        latest_cumulative_nav=_optional_decimal(data.get("latest_cumulative_nav")),
        nav_error=str(data.get("nav_error") or ""),
    )


class WealthProvider:
    provider = ""

    def fetch_latest(self, product: NavProduct, **kwargs) -> list[NavRecord]:
        raise NotImplementedError

    def search_products(self, query: str) -> list[ProductCandidate]:
        raise NotImplementedError

    def get_product_identity(self, code: str) -> Optional[ProductCandidate]:
        candidates = self.search_products(code)
        for candidate in candidates:
            if candidate.code.upper() == code.upper():
                return candidate
        return candidates[0] if candidates else None

    def fetch_by_date(self, product: NavProduct, target_date: date) -> DateQueryResult:
        records = self.fetch_latest(product, as_of=target_date)
        eligible = [record for record in records if record.nav_date <= target_date]
        if not eligible:
            return DateQueryResult(target_date=target_date, exact=False, record=None)
        record = eligible[0]
        previous = next(
            (item for item in records if item.nav_date < record.nav_date),
            None,
        )
        return DateQueryResult(
            target_date=target_date,
            exact=record.nav_date == target_date,
            record=record,
            previous=previous,
        )


class CiticWealthProvider(WealthProvider):
    provider = "citic_wealth"
    SEARCH_PATH = "/cms.product/api/custom/productInfo/search"
    DETAIL_PATH = "/cms.product/api/custom/productInfo/getTAProductDetail"
    NAV_PATH = "/cms.product/api/custom/productInfo/getTAProductNav"

    def __init__(self, client=None):
        self.client = client or CiticHttpClient()

    def fetch_latest(self, product: NavProduct, **kwargs) -> list[NavRecord]:
        data = _unwrap_provider_response(
            self.client.get_json(self.NAV_PATH, {"prodCode": product.code, "queryUnit": 1})
        )
        items = _first_list(data, "productNavPic", "list", "rows", "data")
        records = [
            self._record_from_item(item, product)
            for item in items
            if item
        ]
        return sorted(records, key=lambda item: item.nav_date, reverse=True)

    def search_products(self, query: str) -> list[ProductCandidate]:
        data = _unwrap_provider_response(
            self.client.get_json(self.SEARCH_PATH, {"key": query})
        )
        items = data if isinstance(data, list) else _first_list(data, "list", "rows", "records", "data")
        return [
            self._candidate_from_item(item)
            for item in items[:5]
            if item
        ]

    def get_product_identity(self, code: str) -> Optional[ProductCandidate]:
        exact = [
            candidate for candidate in self.search_products(code)
            if candidate.code.upper() == code.upper()
        ]
        if exact:
            return exact[0]

        data = _unwrap_provider_response(
            self.client.get_json(self.DETAIL_PATH, {"prodCode": code, "prodType": 2})
        )
        return self._candidate_from_item(data) if data else None

    def _record_from_item(self, item: dict, product: NavProduct) -> NavRecord:
        return NavRecord(
            provider=self.provider,
            code=str(item.get("prodCode") or product.code),
            name=str(item.get("prodNameShort") or item.get("prodName") or product.name),
            nav_date=_parse_nav_date(item.get("navDate") or item.get("navDateStr")),
            unit_nav=_parse_decimal(item.get("nav") or item.get("navStr")),
            cumulative_nav=_optional_decimal(item.get("totalNav") or item.get("totalNavStr")),
            source="citic_wealth",
        )

    def _candidate_from_item(self, item: dict) -> ProductCandidate:
        return ProductCandidate(
            provider=self.provider,
            code=str(item.get("prodCode") or item.get("code") or ""),
            name=str(item.get("prodNameShort") or item.get("prodName") or item.get("name") or ""),
            register_code=str(item.get("registCode") or item.get("registerCode") or ""),
            sales_code=str(item.get("prodCode") or ""),
            latest_nav_date=_optional_nav_date(item.get("navDate") or item.get("navDateStr")),
            latest_unit_nav=_optional_decimal(item.get("nav") or item.get("navStr")),
            latest_cumulative_nav=_optional_decimal(item.get("totalNav") or item.get("totalNavStr")),
        )


class NanyinWealthProvider(WealthProvider):
    provider = "nanyin_wealth"
    DETAIL_PATH = "/eportal/ui?moduleId=5&portal.url=/portlet/article-data!queryProductDetail.portlet&TopModuelId=e7db4f2628cb40548f6101d71695ada2"
    NAV_PATH = "/eportal/ui?moduleId=5&portal.url=/portlet/article-data!queryNetValueList.portlet&TopModuelId=e7db4f2628cb40548f6101d71695ada2"

    def __init__(self, client=None):
        self.client = client or NanyinHttpClient()

    def fetch_latest(self, product: NavProduct, as_of: Optional[date] = None, **kwargs) -> list[NavRecord]:
        end_date = as_of or date.today()
        start_date = end_date - timedelta(days=120)
        payload = {
            "productCode": product.code,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "currentPage": 1,
        }
        data = self.client.post_encrypted_json(self.NAV_PATH, payload)
        items = _first_list(data, "aaData", "result", "list", "rows", "data")
        records = [
            self._record_from_item(item, product)
            for item in items
            if isinstance(item, dict) and "$ref" not in item
        ]
        return sorted(records, key=lambda item: item.nav_date, reverse=True)

    def search_products(self, query: str) -> list[ProductCandidate]:
        candidate = self.get_product_identity(query)
        return [candidate] if candidate else []

    def get_product_identity(self, code: str) -> Optional[ProductCandidate]:
        data = self.client.post_encrypted_json(self.DETAIL_PATH, {"productCode": code})
        if not data:
            return None
        candidate = ProductCandidate(
            provider=self.provider,
            code=str(data.get("salesCode") or data.get("productCode") or code),
            name=str(data.get("title") or data.get("productName") or ""),
            register_code=str(data.get("financingRegisterCode") or data.get("registerCode") or ""),
            sales_code=str(data.get("salesCode") or ""),
        )
        return self._with_latest_nav(candidate)

    def _with_latest_nav(self, candidate: ProductCandidate) -> ProductCandidate:
        try:
            product = NavProduct(candidate.provider, candidate.code, candidate.name)
            records = self.fetch_latest(product)
            if not records:
                return ProductCandidate(**{**candidate.__dict__, "nav_error": "暂无净值记录"})
            latest = records[0]
            return ProductCandidate(
                **{
                    **candidate.__dict__,
                    "latest_nav_date": latest.nav_date,
                    "latest_unit_nav": latest.unit_nav,
                    "latest_cumulative_nav": latest.cumulative_nav,
                }
            )
        except Exception as exc:
            return ProductCandidate(**{**candidate.__dict__, "nav_error": str(exc)[:120]})

    def _record_from_item(self, item: dict, product: NavProduct) -> NavRecord:
        return NavRecord(
            provider=self.provider,
            code=str(item.get("productCode") or product.code),
            name=product.name,
            nav_date=_parse_nav_date(item.get("date") or item.get("navDate")),
            unit_nav=_parse_decimal(item.get("netValue") or item.get("netAssetValue")),
            cumulative_nav=_optional_decimal(item.get("cumulativeNetValue")),
            source="nanyin_wealth",
        )


class CiticHttpClient:
    BASE_URL = "https://wechat.citic-wealth.com"
    SIGN_KEY = b"49bd72694d7e406fbcf738244e2b0803"

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_legacy_ssl_context())
        )

    def get_json(self, path: str, params: dict = None) -> dict:
        url = self.BASE_URL + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return _read_json(self.opener, url, self._headers(), timeout=self.timeout)

    def _headers(self) -> dict:
        timestamp = str(int(time.time() * 1000))
        nonce = secrets.token_hex(16)
        message = f"api:request:signature:{timestamp}:{nonce}".encode("utf-8")
        return {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "channel": "h5_trade_service",
            "X-Api-Request-Timestamp": timestamp,
            "X-Api-Request-Nonce": nonce,
            "X-Api-Request-Signature": hmac.new(
                self.SIGN_KEY,
                message,
                hashlib.sha256,
            ).hexdigest(),
        }


class NanyinHttpClient:
    BASE_URL = "https://www.nanyinwealth.com"
    PAGE_PATH = "/nanyinwealth/lccp/cpjz/index.html?id=NYZY000022"
    PUBLIC_KEY_PATH = "/eportal/ui?moduleId=5&portal.url=/portlet/login-handle!publicKey.portlet&TopModuelId=e7db4f2628vb40548f6101d71695ada2&_=1672801141869"

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.cookie_jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_legacy_ssl_context()),
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
        )
        self.server_public_key = ""

    def post_encrypted_json(self, path: str, payload: dict) -> dict:
        self._ensure_session()
        aes_key = _random_aes_key()
        body = {
            "data": _aes_encrypt(payload, aes_key),
            "aesKey": _rsa_encrypt(aes_key, self.server_public_key),
            "timeStamp": _aes_encrypt(str(int(time.time() * 1000)), aes_key),
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        response = _read_json(
            self.opener,
            self.BASE_URL + path,
            self._headers("application/json;charset=utf-8"),
            data=data,
            timeout=self.timeout,
        )
        if response.get("errorMessage"):
            raise ProviderError(response["errorMessage"])
        encrypted = ((response.get("data") or {}).get("data") if isinstance(response.get("data"), dict) else "")
        if not encrypted:
            return response.get("data") or response
        plain = _aes_decrypt(encrypted, aes_key)
        try:
            return json.loads(plain)
        except json.JSONDecodeError:
            raise ProviderError("南银理财响应解密后不是有效 JSON")

    def _ensure_session(self):
        if self.server_public_key:
            return
        _read_text(
            self.opener,
            self.BASE_URL + self.PAGE_PATH,
            self._headers("text/html"),
            timeout=self.timeout,
        )
        response = _read_json(
            self.opener,
            self.BASE_URL + self.PUBLIC_KEY_PATH,
            self._headers("application/json"),
            timeout=self.timeout,
        )
        key = response.get("data")
        if not key:
            raise ProviderError("南银理财未返回服务端公钥")
        self.server_public_key = key

    def _headers(self, content_type: str = "") -> dict:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/javascript,*/*;q=0.01",
            "Referer": self.BASE_URL + self.PAGE_PATH,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers


def get_provider(provider: str) -> WealthProvider:
    if provider == "citic_wealth":
        return CiticWealthProvider()
    if provider == "nanyin_wealth":
        return NanyinWealthProvider()
    raise ProviderError(f"不支持的理财机构：{provider}")


def _unwrap_provider_response(response: dict):
    if not isinstance(response, dict):
        return response
    code = response.get("code")
    if code not in (None, "0000", 0, "0"):
        raise ProviderError(str(response.get("msg") or response.get("message") or code))
    return response.get("data", response)


def _first_list(data, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _parse_nav_date(value) -> date:
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        raise ProviderError("净值日期为空")
    raw = raw.replace(".", "-").replace("/", "-")
    if re.fullmatch(r"\d{8}", raw):
        return datetime.strptime(raw, "%Y%m%d").date()
    return date.fromisoformat(raw[:10])


def _optional_nav_date(value) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return _parse_nav_date(value)
    except Exception:
        return None


def _parse_decimal(value) -> Decimal:
    if value in (None, ""):
        raise ProviderError("净值为空")
    return Decimal(str(value).replace(",", "").strip())


def _optional_decimal(value) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return _parse_decimal(value)
    except Exception:
        return None


def _legacy_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    return context


def _read_json(opener, url: str, headers: dict, data: bytes = None, timeout: int = 10) -> dict:
    text = _read_text(opener, url, headers, data=data, timeout=timeout)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"接口返回不是有效 JSON：{exc}") from exc


def _read_text(opener, url: str, headers: dict, data: bytes = None, timeout: int = 10) -> str:
    method = "POST" if data is not None else "GET"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with opener.open(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace")


def _random_aes_key() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _aes_encrypt(payload, key: str) -> str:
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
    except Exception as exc:
        raise ProviderError("缺少 pycryptodome，无法访问南银理财接口") from exc

    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(payload.encode("utf-8"), 16))).decode("ascii")


def _aes_decrypt(payload: str, key: str) -> str:
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except Exception as exc:
        raise ProviderError("缺少 pycryptodome，无法访问南银理财接口") from exc

    cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
    return unpad(cipher.decrypt(base64.b64decode(payload)), 16).decode("utf-8", "replace")


def _rsa_encrypt(payload: str, public_key: str) -> str:
    try:
        from Crypto.Cipher import PKCS1_v1_5
        from Crypto.PublicKey import RSA
    except Exception as exc:
        raise ProviderError("缺少 pycryptodome，无法访问南银理财接口") from exc

    if "BEGIN PUBLIC KEY" not in public_key:
        public_key = (
            "-----BEGIN PUBLIC KEY-----\n"
            + "\n".join(textwrap.wrap(public_key, 64))
            + "\n-----END PUBLIC KEY-----"
        )
    cipher = PKCS1_v1_5.new(RSA.import_key(public_key))
    return base64.b64encode(cipher.encrypt(payload.encode("utf-8"))).decode("ascii")
