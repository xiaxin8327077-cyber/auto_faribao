"""按基金身份发现公告并形成带证据日期的画像。报告文本不当成指令。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from src.fund_estimate_ai import SYSTEM_PROMPT, AiUnavailable
from src.fund_estimate_store import iso_now
from src.fund_estimate_types import FundIdentity, ProfileVersion


_INSTRUCTION_MARKERS = ("忽略", "指令", "ignore previous", "系统提示", "你现在是")


@dataclass(frozen=True)
class FundDocument:
    url: str
    title: str
    published_at: str | None
    report_as_of: str | None
    text: str
    share_class: str = ""


def discover_share_class(name: str, metadata: dict | None = None) -> str:
    explicit = str((metadata or {}).get("share_class") or "").strip().upper()
    if len(explicit) == 1 and explicit.isalpha():
        return explicit
    match = re.search(r"([A-Z])类?$", str(name or "").strip())
    return match.group(1) if match else ""


def support_reason(fund_type: str, name: str) -> str | None:
    text = f"{fund_type} {name}"
    folded = text.lower()
    for word in ("股票", "混合", "转债", "可转债", "qdii", "etf", "海外", "商品"):
        if word in folded or word in text:
            return "unsupported_asset_mix"
    return None


def document_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_profile_text(text: str, share_class: str) -> tuple[dict, list]:
    """确定性提取。同一组合证据不足时不把画像写给其他份额。"""
    fields: dict = {"fee_schedule": [], "portfolio_group_id": None, "duration_nav": None,
                    "duration_nav_up": None, "duration_nav_down": None,
                    "bond_weights_nav_pct": None, "disclosed_management_fee_annual_pct": None}
    evidence = []
    group = re.search(r"同一组合证据[:：]\s*(\S+)", text)
    if group:
        fields["portfolio_group_id"] = group.group(1)
        evidence.append(_evidence("portfolio_group_id", group.group(0), None))
    report = re.search(r"报告期[:：]\s*(\d{4}-\d{2}-\d{2})", text)
    published = re.search(r"公告日期[:：]\s*(\d{4}-\d{2}-\d{2})", text)
    if report:
        fields["report_as_of"] = report.group(1)
    else:
        fields["report_as_of"] = _report_in_text(text)
    if published:
        fields["published_at"] = published.group(1)
    duration = re.search(r"净值口径久期[:：]\s*([0-9]+(?:\.[0-9]+)?)年", text)
    if duration and not _looks_like_instruction(duration.group(0)):
        fields["duration_nav"] = duration.group(1)
        evidence.append(_text_evidence("duration_nav", duration.group(0), "年", text))
    _parse_public_tables(text, fields, evidence)
    if re.search(r"份额[A-Z]", text):
        fees = _fees(_share_section(text, share_class), share_class)
    else:
        fees = _fees(text, share_class)
    fields["fee_schedule"] = fees
    for item in fees:
        evidence.append(_text_evidence("management_fee_annual_pct", item["quote"], "%/年", text))
    if not fees:
        _parse_public_fee(text, share_class, fields, evidence)
    return fields, evidence


def applied_fields(profile: ProfileVersion, on_date: str) -> dict:
    fields = dict(profile.fields)
    schedule = list(fields.get("fee_schedule") or [])
    chosen = None
    for item in schedule:
        if item.get("share_class") not in {profile.identity.share_class, ""}:
            continue
        if item.get("effective_date") and str(item["effective_date"]) <= on_date:
            if chosen is None or item["effective_date"] > chosen["effective_date"]:
                chosen = item
    fields["management_fee_annual_pct"] = None if chosen is None else chosen["management_fee_annual_pct"]
    return fields


def validate_ai_extraction(raw: str, document_text: str, share_class: str | None = None) -> tuple[dict, list]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("AI 返回的不是 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("AI 返回的不是对象")
    evidence = payload.get("evidence")
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("AI 提取缺少原文证据")
    accepted = []
    result = {"fee_schedule": [], "portfolio_group_id": None, "duration_nav": None}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "")
        key = str(item.get("field") or "")
        if not quote or quote not in document_text or _looks_like_instruction(quote):
            continue
        actual_page = _page_at(document_text, document_text.index(quote))
        if actual_page is not None and (type(item.get("page")) is not int or item["page"] != actual_page):
            continue
        if key == "duration_nav":
            match = re.search(r"久期[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)年", quote)
            if match and item.get("unit") == "年" and _number_matches(fields.get(key), match.group(1)):
                result["duration_nav"] = match.group(1)
                accepted.append(_text_evidence(key, quote, "年", document_text))
        elif key == "management_fee_annual_pct":
            if share_class is not None and re.search(r"份额[A-Z]", document_text) and quote not in _share_section(document_text, share_class):
                continue
            match = re.search(r"管理费率[:：]\s*([0-9]+(?:\.[0-9]+)?)%/年", quote)
            fee_scope = _share_section(document_text, share_class) if share_class is not None and re.search(r"份额[A-Z]", document_text) else document_text
            effective = re.search(r"费率生效日期[:：]\s*(\d{4}-\d{2}-\d{2})", fee_scope)
            if match and item.get("unit") == "%/年" and _number_matches(fields.get(key), match.group(1)):
                result["fee_schedule"].append(
                    {
                        "share_class": share_class if share_class is not None else str(item.get("share_class") or ""),
                        "management_fee_annual_pct": match.group(1),
                        "effective_date": effective.group(1) if effective else None,
                        "quote": quote,
                    }
                )
                accepted.append(_text_evidence(key, quote, "%/年", document_text))
    if not accepted:
        raise ValueError("AI 证据未能对应到原文中的数值")
    return result, accepted


class ProfileService:
    def __init__(self, store, documents, ai=None, clock=None):
        self.store = store
        self.documents = documents
        self.ai = ai
        self.clock = clock or (lambda: datetime.now())

    def discover_documents(self, identity: FundIdentity, since: datetime | None = None) -> list[dict]:
        found = self.documents.list_documents(identity, since)
        rows = []
        for item in found:
            published = item.published_at or _published_in_text(item.text)
            rows.append(
                {
                    "url": item.url,
                    "title": item.title,
                    "published_at": published,
                    "report_as_of": _report_in_text(item.text) or item.report_as_of,
                    "share_class": item.share_class or identity.share_class,
                    "document_hash": document_hash(item.text),
                }
            )
        return rows

    def refresh(self, identity: FundIdentity, now: datetime) -> ProfileVersion | None:
        stamp = iso_now(now)
        try:
            documents = self.documents.list_documents(identity, None)
        except Exception as exc:
            self.store.record_profile_failure(identity.code, identity.share_class, str(exc)[:200], stamp)
            return self.store.current_profile(identity.code, identity.share_class)
        latest = None
        failed = False
        for item in sorted(documents, key=lambda row: row.published_at or _published_in_text(row.text) or ""):
            try:
                latest = self._consume(identity, item, stamp) or latest
            except (AiUnavailable, ValueError) as exc:
                failed = True
                self.store.record_profile_failure(identity.code, identity.share_class, str(exc)[:200], stamp)
        if not failed:
            self.store.set_watermark(f"profile-checked:{identity.code}:{identity.share_class}", stamp, stamp)
        return latest or self.store.current_profile(identity.code, identity.share_class)

    def get_as_of(self, identity: FundIdentity, cutoff_at: str) -> ProfileVersion | None:
        return self.store.profile_as_of(identity.code, identity.share_class, cutoff_at)

    def _consume(self, identity: FundIdentity, item: FundDocument, stamp: str) -> ProfileVersion | None:
        text = item.text or ""
        digest = document_hash(text)
        self.store.remember_document(digest, item.url, text, stamp)
        fields, evidence = parse_profile_text(text, identity.share_class)
        cache_key = f"profile-extraction:v3:{digest}:{identity.share_class}"
        cached = self.store.get_watermark(cache_key)
        ai_complete = False
        if cached:
            extracted = json.loads(cached)
            fields, evidence = extracted["fields"], extracted["evidence"]
            ai_complete = bool(extracted.get("ai_complete"))
        published = item.published_at or fields.get("published_at")
        report_as_of = fields.get("report_as_of") or item.report_as_of
        if fields.get("duration_nav") is None and self.ai is not None and not ai_complete and getattr(self.ai, "is_enabled", lambda: True)():
            raw = self.ai.complete(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"task": "extract", "document_text": text},
                            ensure_ascii=False,
                        ),
                    },
                ]
            )
            ai_fields, ai_evidence = validate_ai_extraction(raw, text, identity.share_class)
            ai_fields["fee_schedule"] = [
                item for item in ai_fields.get("fee_schedule") or []
                if item.get("share_class") in {identity.share_class, ""}
            ]
            fields = _merge_missing(fields, ai_fields)
            evidence = evidence + ai_evidence
            ai_complete = True
        if fields.get("duration_nav") is not None:
            self.store.mark_document_extracted(digest)
        self.store.set_watermark(cache_key, json.dumps({"fields": fields, "evidence": evidence, "ai_complete":ai_complete}, ensure_ascii=False), stamp)
        fields["duration_report_as_of"] = report_as_of if fields.get("duration_nav") is not None else None
        fields["duration_published_at"] = published if fields.get("duration_nav") is not None else None
        evidence = [dict(row, source_url=item.url, report_as_of=report_as_of,
                         published_at=published, fetched_at=stamp, document_hash=digest) for row in evidence]
        current = self.store.current_profile(identity.code, identity.share_class)
        if not evidence:
            return current
        if current and published and current.published_at and current.published_at <= published:
            report_as_of = report_as_of or current.report_as_of
            inherited = {key for key, value in fields.items() if value in (None, "", [])}
            fields = _merge_missing(fields, current.fields)
            # Older sensitivity remains a dated historical constraint when a
            # newer quarterly report supplies only portfolio weights.
            evidence.extend(row for row in current.evidence if any(
                row.get("field") == key or str(row.get("field", "")).startswith(key + ".")
                for key in inherited))
            schedules = list(current.fields.get("fee_schedule") or []) + list(fields.get("fee_schedule") or [])
            fields["fee_schedule"] = list({(row.get("share_class"), row.get("effective_date")): row for row in schedules}.values())
        if not evidence:
            return current
        group_id = fields.get("portfolio_group_id") if fields.get("portfolio_group_id") else None
        profile_identity = FundIdentity(
            product_id=identity.product_id,
            code=identity.code,
            provider=identity.provider,
            share_class=identity.share_class,
            portfolio_group_id=group_id,
            fund_type=identity.fund_type,
            currency=identity.currency,
        )
        semantic = json.dumps(fields, ensure_ascii=False, sort_keys=True)
        version = hashlib.sha256(f"{identity.code}:{identity.share_class}:{digest}:{published}:{semantic}".encode()).hexdigest()[:24]
        profile = ProfileVersion(
            profile_version=version,
            identity=profile_identity,
            report_as_of=report_as_of,
            published_at=published,
            fetched_at=stamp,
            source_url=item.url,
            document_hash=digest,
            fields={key: value for key, value in fields.items() if key not in {"published_at", "report_as_of"}},
            evidence=evidence,
            status="ok",
        )
        return self.store.publish_profile(profile)


def _merge_missing(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        if merged.get(key) in (None, "", []):
            merged[key] = value
    return merged


def _share_section(text: str, share_class: str) -> str:
    if not share_class:
        return text
    pattern = re.compile(rf"份额{share_class}(.*?)(?=份额[A-Z]|$)", re.S)
    match = pattern.search(text)
    return match.group(1) if match else ""


def _fees(section: str, share_class: str) -> list[dict]:
    fees = []
    rates = list(re.finditer(r"管理费率[:：]\s*([0-9]+(?:\.[0-9]+)?)%/年", section))
    dates = list(re.finditer(r"费率生效日期[:：]\s*(\d{4}-\d{2}-\d{2})", section))
    for index, rate in enumerate(rates):
        effective = dates[index].group(1) if index < len(dates) else None
        if not effective:
            continue
        fees.append(
            {
                "share_class": share_class,
                "management_fee_annual_pct": rate.group(1),
                "effective_date": effective,
                "quote": rate.group(0),
            }
        )
    return fees


_MONEY = r"-?\d[\d,]*\.\d{2}"


def _compact(text):
    positions = [index for index, char in enumerate(text) if not char.isspace()]
    return "".join(text[index] for index in positions), positions


def _original_match(text, positions, match):
    return text[positions[match.start()]:positions[match.end() - 1] + 1]


def _parse_public_tables(text, fields, evidence):
    compact, positions = _compact(text)
    nav = re.search(rf"净资产合计(?P<value>{_MONEY})", compact)
    # A historical NAV sensitivity is already normalized by NAV, including
    # leverage. Do not multiply by bond allocation, which can exceed 100%.
    headings = list(re.finditer(r"利率风险的敏感性分析", compact))
    sensitivity = headings[-1] if headings else None
    sides = {}
    if nav and sensitivity and "人民币元" in compact[sensitivity.start():sensitivity.start() + 1000]:
        pattern = rf"市场利率(?P<side>上升|上调|下降|下调)(?:25(?:个)?基点|25[bB][pP]|0\.25[%％])(?P<value>{_MONEY})"
        for match in re.finditer(pattern, compact[sensitivity.end():sensitivity.end() + 1400]):
            side = "up" if match["side"] in {"上升", "上调"} else "down"
            amount = Decimal(match["value"].replace(",", ""))
            # Inverse price/rate response is necessary for this bond proxy.
            if (side == "up" and amount >= 0) or (side == "down" and amount <= 0):
                continue
            if side not in sides:
                start = sensitivity.end() + match.start()
                end = sensitivity.end() + match.end()
                quote = text[positions[start]:positions[end - 1] + 1]
                sides[side] = (amount, quote)
        net_assets = Decimal(nav["value"].replace(",", ""))
        if net_assets > 0 and sides:
            for side, (amount, quote) in sides.items():
                value = abs(amount) / net_assets / Decimal("0.0025")
                fields[f"duration_nav_{side}"] = format(value.quantize(Decimal("0.000001")), "f")
                evidence.append(_text_evidence(f"duration_nav_{side}", quote, "元", text))
            if {"up", "down"}.issubset(sides):
                center = (abs(sides["up"][0]) + sides["down"][0]) / net_assets / Decimal("0.005")
                fields["duration_nav"] = format(center.quantize(Decimal("0.000001")), "f")
            # One-sided reports preserve the available proxy without inventing
            # the missing side; the basis remains explicit.
            else:
                fields["duration_nav"] = next(fields[f"duration_nav_{side}"] for side in sides)
            fields["duration_basis"] = "historical_nav_sensitivity_25bp"
            fields["interest_rate_sensitivity"] = {"net_assets":str(net_assets), "shift_bp":"25",
                "up_amount":str(sides["up"][0]) if "up" in sides else None,
                "down_amount":str(sides["down"][0]) if "down" in sides else None}
            evidence.append(_text_evidence("duration_nav", _original_match(text, positions, nav), "元", text))
    # Search the actual table heading (not the dotted table of contents).
    heading = list(re.finditer(r"期末按债券品种分类的债券投资组合", compact))
    if not heading:
        return
    start = heading[-1].end()
    original_start = positions[start]
    remaining = text[original_start:]
    end_match = re.search(r"(?m)^\s*\d+\.\d+\s*期末", remaining)
    original_table = remaining[:end_match.start()] if end_match else remaining[:5000]
    table = re.sub(r"\s", "", original_table)
    if not re.search(r"占基金资产净值(?:比例|的比例)", table):
        return
    weights = {}
    labels = ("国家债券", "央行票据", "金融债券", "政策性金融债", "企业债券", "企业短期融资券", "中期票据", "可转债（可交换债）", "可转债(可交换债)", "同业存单", "其他", "合计")
    for label in labels:
        match = re.search(re.escape(label) + rf"\s+{_MONEY}\s+(?P<weight>\d+\.\d{{2}})(?!\d)", original_table)
        if not match:
            continue
        original = match.group(0)
        weights[label] = match["weight"]
        proof = _text_evidence(f"bond_weights_nav_pct.{label}", original, "%净资产", text)
        proof["page"] = _page_at(text, original_start + match.start())
        evidence.append(proof)
    if weights:
        fields["bond_weights_nav_pct"] = weights
        fields["bond_weights_basis"] = "net_assets_unscaled_policy_financial_is_subset"


def _parse_public_fee(text, share_class, fields, evidence):
    # Match fee prose, never the yuan amount in the accrued-fee table or a
    # sales-service fee belonging to one particular share class.
    changes = re.finditer(r"(?:本基金的?)?管理费(?:年)?费率(?:由|从)\s*[0-9.]+%\s*(?:调低至|降低至|调整为|调整至|变更为)\s*([0-9]+(?:\.[0-9]+)?)%", text)
    change = next((item for item in changes if _fee_share_allowed(text, item.start(), share_class)), None)
    if change:
        dates = list(re.finditer(r"自\s*(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日\s*起", text[:change.end()]))
        effective = None
        if dates:
            day = dates[-1]
            try:
                effective = datetime(int(day[1]), int(day[2]), int(day[3])).date().isoformat()
            except ValueError:
                pass
        fields["disclosed_management_fee_annual_pct"] = change[1]
        evidence.append(_text_evidence("disclosed_management_fee_annual_pct", change[0], "%/年", text))
        if effective:
            fields["fee_schedule"].append({"share_class":share_class, "management_fee_annual_pct":change[1],
                                           "effective_date":effective, "quote":change[0]})
            evidence.append(_text_evidence("management_fee_annual_pct", change[0], "%/年", text))
            evidence.append(_text_evidence("fee_effective_date", dates[-1][0], "日期", text))
        return
    pattern = r"(?:本基金的?管理费|基金管理费按|管理费按)[\s\S]{0,100}?([0-9]+(?:\.[0-9]+)?)%的?年费率"
    for match in re.finditer(pattern, text):
        quote = match.group(0)
        if not _fee_share_allowed(text, match.start(), share_class):
            continue
        fields["disclosed_management_fee_annual_pct"] = match.group(1)
        evidence.append(_text_evidence("disclosed_management_fee_annual_pct", quote, "%/年", text))
        # The report proves a disclosed rate, not the day it entered into
        # force. Only an explicit effective-date disclosure enters a schedule.
        effective = re.search(r"费率生效日期[:：]\s*(\d{4}-\d{2}-\d{2})", text)
        if effective:
            fields["fee_schedule"].append({"share_class":share_class,
                "management_fee_annual_pct":match.group(1), "effective_date":effective.group(1), "quote":quote})
        break


def _fee_share_allowed(text, offset, share_class):
    scope = text[max(0, offset - 120):offset]
    classes = re.findall(r"([A-Z])类(?:基金)?份额|份额([A-Z])", scope)
    return not classes or (classes[-1][0] or classes[-1][1]) == share_class


def _number_matches(claim, actual):
    if claim is None:
        return True
    try:
        value = Decimal(str(claim))
        return value.is_finite() and value == Decimal(str(actual))
    except (InvalidOperation, TypeError, ValueError):
        return False


def _page_at(text, offset):
    markers = list(re.finditer(r"\[第\s*(\d+)\s*页\]", text[:offset + 1]))
    return int(markers[-1].group(1)) if markers else None


def _text_evidence(field, quote, unit, text):
    result = _evidence(field, quote, unit)
    result["page"] = _page_at(text, text.find(quote))
    return result


def _evidence(field: str, quote: str, unit) -> dict:
    return {"field": field, "page": None, "quote": quote, "unit": unit}


def _looks_like_instruction(quote: str) -> bool:
    folded = quote.lower()
    return any(marker in folded or marker in quote for marker in _INSTRUCTION_MARKERS)


def _published_in_text(text: str) -> str | None:
    match = re.search(r"公告日期[:：]\s*(\d{4}-\d{2}-\d{2})", text or "")
    return None if match is None else match.group(1) + "T00:00:00+08:00"


def _report_in_text(text: str) -> str | None:
    match = re.search(r"报告期[:：]\s*(\d{4}-\d{2}-\d{2})", text or "")
    if match:
        return match.group(1)
    match = re.search(r"报告截止日\s*(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", text or "")
    return None if match is None else f"{match[1]}-{int(match[2]):02d}-{int(match[3]):02d}"


class MemoryDocuments:
    def __init__(self, items=None):
        self.items = list(items or [])

    def list_documents(self, identity: FundIdentity, since: datetime | None = None):
        rows = []
        for item in self.items:
            if item.share_class and item.share_class != identity.share_class and "份额" + identity.share_class not in item.text:
                continue
            if since is not None and item.published_at and item.published_at < iso_now(since):
                continue
            rows.append(item)
        return rows
