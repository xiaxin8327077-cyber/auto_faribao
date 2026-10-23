"""按基金身份发现公告并形成带证据日期的画像。报告文本不当成指令。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
    fields: dict = {"fee_schedule": [], "portfolio_group_id": None, "duration_nav": None}
    evidence = []
    group = re.search(r"同一组合证据[:：]\s*(\S+)", text)
    if group:
        fields["portfolio_group_id"] = group.group(1)
        evidence.append(_evidence("portfolio_group_id", group.group(0), None))
    report = re.search(r"报告期[:：]\s*(\d{4}-\d{2}-\d{2})", text)
    published = re.search(r"公告日期[:：]\s*(\d{4}-\d{2}-\d{2})", text)
    if report:
        fields["report_as_of"] = report.group(1)
    if published:
        fields["published_at"] = published.group(1)
    duration = re.search(r"净值口径久期[:：]\s*([0-9]+(?:\.[0-9]+)?)年", text)
    if duration and not _looks_like_instruction(duration.group(0)):
        fields["duration_nav"] = duration.group(1)
        evidence.append(_evidence("duration_nav", duration.group(0), "年"))
    if re.search(r"份额[A-Z]", text):
        fees = _fees(_share_section(text, share_class), share_class)
    else:
        fees = _fees(text, share_class)
    fields["fee_schedule"] = fees
    for item in fees:
        evidence.append(_evidence("management_fee_annual_pct", item["quote"], "%/年"))
    return fields, evidence


def applied_fields(profile: ProfileVersion, on_date: str) -> dict:
    fields = dict(profile.fields)
    schedule = list(fields.get("fee_schedule") or [])
    chosen = None
    for item in schedule:
        if item.get("share_class") not in {profile.identity.share_class, ""}:
            continue
        if str(item.get("effective_date") or "") <= on_date:
            if chosen is None or item["effective_date"] > chosen["effective_date"]:
                chosen = item
    fields["management_fee_annual_pct"] = None if chosen is None else chosen["management_fee_annual_pct"]
    return fields


def validate_ai_extraction(raw: str, document_text: str) -> tuple[dict, list]:
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
        if key == "duration_nav":
            match = re.search(r"久期[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)年", quote)
            if match:
                result["duration_nav"] = match.group(1)
                accepted.append(_evidence(key, quote, item.get("unit") or "年"))
        elif key == "management_fee_annual_pct":
            match = re.search(r"管理费率[:：]\s*([0-9]+(?:\.[0-9]+)?)%/年", quote)
            effective = re.search(r"费率生效日期[:：]\s*(\d{4}-\d{2}-\d{2})", document_text)
            if match:
                result["fee_schedule"].append(
                    {
                        "share_class": str(item.get("share_class") or ""),
                        "management_fee_annual_pct": match.group(1),
                        "effective_date": effective.group(1) if effective else "1900-01-01",
                        "quote": quote,
                    }
                )
                accepted.append(_evidence(key, quote, "%/年"))
    if not accepted:
        raise ValueError("AI 证据未能对应到原文中的数值")
    result.update({key: value for key, value in fields.items() if key in {"report_as_of"} and value})
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
                    "report_as_of": item.report_as_of or _report_in_text(item.text),
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
        for item in documents:
            latest = self._consume(identity, item, stamp) or latest
        self.store.set_watermark(f"profile-checked:{identity.code}:{identity.share_class}", stamp, stamp)
        return latest or self.store.current_profile(identity.code, identity.share_class)

    def get_as_of(self, identity: FundIdentity, cutoff_at: str) -> ProfileVersion | None:
        return self.store.profile_as_of(identity.code, identity.share_class, cutoff_at)

    def _consume(self, identity: FundIdentity, item: FundDocument, stamp: str) -> ProfileVersion | None:
        text = item.text or ""
        digest = document_hash(text)
        already = self.store.remember_document(digest, item.url, text, stamp)
        fields, evidence = parse_profile_text(text, identity.share_class)
        if already and fields.get("duration_nav") is None:
            return self.store.current_profile(identity.code, identity.share_class)
        published = item.published_at or fields.get("published_at")
        report_as_of = item.report_as_of or fields.get("report_as_of")
        if fields.get("duration_nav") is None and self.ai is not None and not already:
            try:
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
                ai_fields, ai_evidence = validate_ai_extraction(raw, text)
                ai_fields["fee_schedule"] = [
                    item for item in ai_fields.get("fee_schedule") or []
                    if item.get("share_class") in {identity.share_class, ""}
                ]
                fields = _merge_missing(fields, ai_fields)
                evidence = evidence + ai_evidence
                self.store.mark_document_extracted(digest)
            except (AiUnavailable, ValueError) as exc:
                self.store.record_profile_failure(identity.code, identity.share_class, str(exc)[:200], stamp)
                return self.store.current_profile(identity.code, identity.share_class)
        elif fields.get("duration_nav") is not None:
            self.store.mark_document_extracted(digest)
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
        version = hashlib.sha256(f"{identity.code}:{identity.share_class}:{digest}:{published}".encode()).hexdigest()[:24]
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
    return None if match is None else match.group(1)


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
