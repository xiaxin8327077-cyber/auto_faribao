"""Discover public fund notices and retain page-addressable PDF text.

Eastmoney mirrors public notices for arbitrary six digit fund codes. Discovery
is bounded; cached immutable attachments are reused, but failed reads are not.
"""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
import json
import re
import time

from pypdf import PdfReader
import requests

from src.beijing_time import TZ_CN
from src.fund_estimate_profiles import FundDocument, document_hash
from src.fund_estimate_store import iso_now, parse_time


class DocumentUnavailable(RuntimeError):
    pass


class PublicFundDocuments:
    API_URL = "https://api.fund.eastmoney.com/f10/JJGG"
    MAX_PDF_BYTES = 16 * 1024 * 1024
    MAX_JSON_BYTES = 1024 * 1024

    def __init__(self, store, session=None):
        self.store = store
        self.session = session or requests.Session()
        self._texts: dict[str, str] = {}

    def list_documents(self, identity, since=None) -> list[FundDocument]:
        code = str(identity.code)
        if not re.fullmatch(r"\d{6}", code):
            raise DocumentUnavailable("基金代码须为六位数字")
        notices = []
        quarter = long_report = None
        # Three pages cover over a year's periodic reports without reading all
        # past PDFs. A recent quarter and a recent audited/interim report serve
        # different fields: quarterlies usually omit sensitivity disclosures.
        for page in range(1, 4):
            rows = self._announcements(code, 3, page)
            for item in rows:
                title = str(item.get("TITLE") or "")
                if quarter is None and re.search(r"季度报告|季报", title):
                    quarter = item
                if long_report is None and re.search(r"中期报告|半年度报告|年度报告", title) and "摘要" not in title:
                    long_report = item
            if (quarter and long_report) or len(rows) < 10:
                break
        notices.extend(item for item in (quarter, long_report) if item)
        # Fee/structural changes are sparse. Bound both discovery and downloads.
        notices.extend(item for item in self._announcements(code, 2, 1)
                       if re.search(r"费率|管理费|转型|基金合同.*变更", str(item.get("TITLE") or "")))
        notices = notices[:5]
        result = []
        seen = set()
        for item in notices:
            notice_id = str(item.get("ID") or "")
            if notice_id in seen:
                continue
            if not re.fullmatch(r"AN\d{6,30}", notice_id):
                raise DocumentUnavailable("公告附件标识无效")
            seen.add(notice_id)
            published = _published(item)
            if since is not None and (not published or parse_time(published) < parse_time(iso_now(since))):
                continue
            url = f"https://pdf.dfcfw.com/pdf/H2_{notice_id}_1.pdf"
            text = self._pdf_text(url)
            result.append(FundDocument(url, str(item.get("TITLE") or ""), published,
                                       _report_date(str(item.get("TITLE") or "")), text))
        return sorted(result, key=lambda item: item.published_at or "")

    def _announcements(self, code, category, page):
        body = self._get(self.API_URL, self.MAX_JSON_BYTES,
                         params={"fundcode": code, "pageIndex": page, "pageSize": 10, "type": category})
        try:
            payload = json.loads(body.decode("utf-8-sig"))
            rows = payload["Data"]
            if payload.get("ErrCode") != 0 or not isinstance(rows, list):
                raise ValueError("公告源返回失败状态")
            if any(not isinstance(item, dict) or str(item.get("FUNDCODE")) != code for item in rows):
                raise ValueError("公告基金身份不匹配")
            return sorted(rows, key=lambda item: _published(item) or "", reverse=True)
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise DocumentUnavailable("公开基金公告响应无法解析") from exc

    def _pdf_text(self, url):
        if url in self._texts:
            return self._texts[url]
        cached = getattr(self.store, "document_by_url", lambda _url: None)(url)
        if cached and cached.get("content_text"):
            text = cached["content_text"]
        else:
            content = self._get(url, self.MAX_PDF_BYTES, pdf=True)
            try:
                reader = PdfReader(BytesIO(content))
                if reader.is_encrypted or not 1 <= len(reader.pages) <= 400:
                    raise ValueError("不支持的加密或超大PDF")
                pages = []
                size = 0
                for number, page in enumerate(reader.pages, 1):
                    value = page.extract_text() or ""
                    size += len(value)
                    if size > 2_000_000:
                        raise ValueError("PDF文本过大")
                    pages.append(f"[第 {number} 页]\n{value}")
                text = "\n".join(pages)
                if size == 0:
                    raise ValueError("PDF无可提取文本")
            except Exception as exc:
                raise DocumentUnavailable("公开基金PDF文本提取失败") from exc
            # Distinct attachment URLs can have byte-identical text. Keep each
            # source-addressed cache entry; ProfileService separately records
            # the pure text fingerprint used for extraction deduplication.
            self.store.remember_document(document_hash(url + "\n" + text), url, text, iso_now(datetime.now(TZ_CN)))
        self._texts[url] = text
        return text

    def _get(self, url, max_bytes, *, params=None, pdf=False):
        response = None
        try:
            started = time.monotonic()
            response = self.session.get(url, params=params,
                                        headers={"Referer": "https://fundf10.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
                                        timeout=(3, 15), stream=True, allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("公开基金源返回非200响应")
            if int(response.headers.get("Content-Length") or 0) > max_bytes:
                raise ValueError("公开资料响应过大")
            pieces = []
            total = 0
            for part in response.iter_content(65536):
                total += len(part)
                if total > max_bytes or time.monotonic() - started > 35:
                    raise ValueError("公开资料响应超出限制")
                pieces.append(part)
            body = b"".join(pieces)
            if pdf and ("pdf" not in response.headers.get("Content-Type", "").lower() or not body.startswith(b"%PDF-")):
                raise ValueError("公告附件不是PDF")
            return body
        except Exception as exc:
            raise DocumentUnavailable("公开基金资料获取失败：" + str(exc)[:160]) from exc
        finally:
            if response is not None:
                response.close()


def _published(item):
    value = item.get("PUBLISHDATE") or item.get("PUBLISHDATEDesc")
    if not value:
        return None
    try:
        return iso_now(parse_time(str(value)))
    except (ValueError, TypeError):
        return None


def _report_date(title):
    year = re.search(r"(20\d{2})年", title)
    if not year:
        return None
    prefix = year.group(1)
    if "中期" in title or "半年度" in title:
        return prefix + "-06-30"
    if "年度" in title:
        return prefix + "-12-31"
    quarter = re.search(r"第?([一二三四1-4])季度", title)
    if quarter:
        return prefix + {"一":"-03-31","1":"-03-31","二":"-06-30","2":"-06-30","三":"-09-30","3":"-09-30","四":"-12-31","4":"-12-31"}[quarter.group(1)]
    return None
