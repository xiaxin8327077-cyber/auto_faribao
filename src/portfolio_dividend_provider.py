"""Public dividend disclosures. Missing reinvestment rules remain explicit."""
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from html import unescape
from io import BytesIO
import re
import time
from urllib.parse import urljoin

import requests

_NOTICE_CACHE = {}
# 公开源从境外访问偶发超时：重试并放宽读取超时，避免一次抖动被记成抓取失败。
_FETCH_TIMEOUT = (10, 25)
_FETCH_ATTEMPTS = 3


class DividendSourceConflict(ValueError):
    """A verified disclosure disagrees with another source; stop settlement."""


@dataclass(frozen=True)
class FundDividend:
    record_date: date
    ex_dividend_date: date
    payment_date: date
    per_share: Decimal
    source_url: str
    reinvest_nav_date: date | None = None
    reinvest_confirmation_date: date | None = None
    reinvest_rule_source: str = ""


class _Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row, self.cell = None, None


def _day(text):
    return date.fromisoformat(text.strip().replace(".", "-"))


def parse_csfunds_dividends(html, source_url):
    # Official order: record date, amount per share, ex date, payment date.
    match = re.search(r'id=["\']fenhong["\']', html)
    if not match:
        raise ValueError("长盛分红表结构缺失")
    table = re.search(r"<table\b.*?</table>", html[match.end():], re.S | re.I)
    if not table:
        raise ValueError("长盛分红表结构缺失")
    parser = _Rows()
    parser.feed(table.group())
    events = []
    for cells in parser.rows:
        if len(cells) != 4 or not re.fullmatch(r"\d{4}[.-]\d{2}[.-]\d{2}", cells[0]):
            continue
        value = re.match(r"([0-9]+(?:\.[0-9]+)?)", cells[1])
        if value is None:
            raise ValueError("长盛每份分红金额缺失")
        events.append(FundDividend(_day(cells[0]), _day(cells[2]), _day(cells[3]), Decimal(value[1]), source_url))
    if not events:
        raise ValueError("长盛分红表无可核实记录")
    return events


def require_eastmoney_dividend_page(html, code):
    """Reject a dividend table when the page title is for a different fund."""
    match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", "", match.group(1)) if match else ""
    if f"({code})" not in title:
        raise DividendSourceConflict(f"分红页面与请求的基金代码不符：{code}")


def parse_eastmoney_dividends(html, source_url):
    table = re.search(r"<table\b[^>]*class=[\"'][^\"']*\bcfxq\b[^\"']*[\"'][^>]*>.*?</table>", html, re.S | re.I)
    if not table:
        raise ValueError("东方财富分红表结构缺失")
    parser = _Rows()
    parser.feed(table.group())
    events = []
    for cells in parser.rows:
        if len(cells) != 5 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[1]):
            continue
        # The disclosed amount is PER TEN shares, never per share.
        amount = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*元", cells[3])
        if amount is None:
            raise ValueError("东方财富每10份分红金额缺失")
        events.append(FundDividend(_day(cells[1]), _day(cells[2]), _day(cells[4]), Decimal(amount[1]) / 10, source_url))
    if not events and not any("暂无" in "".join(row) for row in parser.rows):
        raise ValueError("东方财富分红表无可核实记录")
    return events


def parse_csfunds_dividend_notice(text, code, source_url):
    """Parse the official PDF's class-specific amount and explicit reinvest rules."""
    normalized = re.sub(r"\s+", " ", text)
    compact = re.sub(r"\s+", "", text)
    # PDF running headers can split a sentence between pages (参 / 与).
    prefix = normalized.split("收益分配公告",1)[0].strip()
    if 2 <= len(prefix) <= 70 and "基金" in prefix:
        compact = compact.replace(re.sub(r"\s+","",prefix)+"收益分配公告", "")
    compact = re.sub(r"第\d+页共\d+页", "", compact)
    codes = re.search(r"下属分级基金的交易代码\s*((?:\d{6}\s*)+)", normalized)
    if codes is None:
        codes = re.search(r"基金主代码\s*(\d{6})", normalized)
    scheme = re.search(r"本次(?:下属分级基金分红方案|分红方案)\s*[（(]单位[：:]\s*元/10\s*份基金份额[）)]\s*((?:\d+\.\d+\s*)+)", normalized)
    if codes is None or scheme is None:
        raise ValueError("公告中分级基金代码或分红方案不完整")
    code_values = re.findall(r"\d{6}", codes[1])
    amounts = re.findall(r"\d+\.\d+", scheme[1])
    if code not in code_values or len(code_values) != len(amounts):
        raise ValueError("公告基金代码与分红方案不匹配")

    def named_day(label):
        match = re.search(label + r"(\d{4})年(\d{1,2})月(\d{1,2})日", compact)
        if match is None:
            raise ValueError("公告缺少" + label)
        return date(*map(int, match.groups()))

    record = named_day("权益登记日")
    ex_date = named_day("除息日")
    payment = named_day("现金红利发放日")
    confirmation = re.search(r"再投资所得的基金份额将于(\d{4})年(\d{1,2})月(\d{1,2})日直接计入其基金账户", compact)
    reliable = ("所得红利将按除息日的基金份额净值折算为基金份额" in compact
                and "免收再投资费用" in compact and confirmation is not None
                and "权益登记日申请申购的基金份额不参与本次收益分配" in compact
                and "权益登记日申请赎回的基金份额参与本次收益分配" in compact)
    confirmation_date = date(*map(int, confirmation.groups())) if reliable else None
    return FundDividend(record, ex_date, payment, Decimal(amounts[code_values.index(code)]) / 10,
                        source_url, ex_date if reliable else None, confirmation_date,
                        source_url if reliable else "")


class PublicFundDividendProvider:
    def __init__(self, session=None):
        self.session = session or requests.Session()

    def _get(self, url, **kwargs):
        last_error = None
        for attempt in range(_FETCH_ATTEMPTS):
            try:
                response = self.session.get(url, timeout=_FETCH_TIMEOUT, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < _FETCH_ATTEMPTS:
                    time.sleep(0.5 * (attempt + 1))
        raise last_error

    def _html(self, url):
        response = self._get(url, headers={"Referer": "https://fundf10.eastmoney.com/"})
        # Both sources declare UTF-8 in HTML but omit the HTTP charset.
        return response.content.decode("utf-8")

    def _notice(self, url, code):
        cached = _NOTICE_CACHE.get(url)
        if cached and time.monotonic()-cached[0] < 3600:
            text = cached[1]
        else:
            response = self._get(url)
            response.raise_for_status()
            from pypdf import PdfReader
            text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(response.content)).pages)
            if len(_NOTICE_CACHE) >= 32:
                _NOTICE_CACHE.pop(next(iter(_NOTICE_CACHE)))
            _NOTICE_CACHE[url] = (time.monotonic(), text)
        return parse_csfunds_dividend_notice(text,code,url)

    @staticmethod
    def _merge_rules(events, notices):
        verified = {notice.ex_dividend_date:notice for notice in notices}
        merged=[]
        for event in events:
            notice=verified.get(event.ex_dividend_date)
            if notice is not None:
                if (notice.record_date,notice.payment_date,notice.per_share)!=(event.record_date,event.payment_date,event.per_share):
                    raise DividendSourceConflict("公开分红表与基金管理人公告方案冲突")
                event=replace(event,reinvest_nav_date=notice.reinvest_nav_date,
                              reinvest_confirmation_date=notice.reinvest_confirmation_date,
                              reinvest_rule_source=notice.reinvest_rule_source)
            merged.append(event)
        return merged

    def _eastmoney_rules(self, product, events):
        if not events:
            return events
        try:
            response=self._get("https://api.fund.eastmoney.com/f10/JJGG",params={"fundcode":product.code,"pageIndex":1,"pageSize":10,"type":2},
                               headers={"Referer":"https://fundf10.eastmoney.com/"})
            response.raise_for_status()
            data=response.json()
            if data.get("ErrCode") != 0 or not isinstance(data.get("Data"),list):
                return events
            notices=[]
            for item in data["Data"][:6]:
                if "收益分配" not in item.get("TITLE","") or item.get("ATTACHTYPE") != "0" or not re.fullmatch(r"AN\d+",item.get("ID","")):
                    continue
                url=f"https://pdf.dfcfw.com/pdf/H2_{item['ID']}_1.pdf"
                try:
                    notices.append(self._notice(url,product.code))
                except (ValueError, requests.RequestException, ImportError):
                    continue
            return self._merge_rules(events,notices)
        except (requests.RequestException, ImportError):
            return events

    def fetch_dividends(self, product):
        if product.code == "015736":
            url = "https://www.csfunds.com.cn/c/2022-05-10/190157.shtml"
            html = self._html(url)
            events = parse_csfunds_dividends(html, url)
            # Follow the product's own announcement listing; never guess a future rule.
            listing = re.search(r'''href=["']([^"']*/front/content/fileList\?[^"']*Type=article[^"']*)["']''', html)
            if listing is None:
                return events
            try:
                notice_html = self._html(unescape(listing[1]))
                articles = list(dict.fromkeys(re.findall(r'''<a\b[^>]*href=["'](https://www\.csfunds\.com\.cn/c/[^"']+\.shtml)["'][^>]*title=["'][^"']*收益分配公告[^"']*["']''', notice_html)))
                target_dates = {event.ex_dividend_date for event in sorted(events,key=lambda e:e.ex_dividend_date,reverse=True)[:3]}
                verified = {}
                for article in articles[:6]:
                    article_html = self._html(article)
                    pdfs = re.findall(r'''href=["'](/upload/resources/file/[^"']+\.pdf)["']''', article_html)
                    for relative in pdfs[:1]:
                        pdf_url = urljoin(article, relative)
                        try:
                            notice = self._notice(pdf_url,product.code)
                        except (ValueError,requests.RequestException,ImportError):
                            continue
                        verified[notice.ex_dividend_date] = notice
                    if target_dates.issubset(verified):
                        break
                merged=self._merge_rules(events,verified.values())
                return merged if merged and merged[0].reinvest_rule_source else self._eastmoney_rules(product,merged)
            except (requests.RequestException, ImportError):
                # Cash dates remain verified; missing rules stay visibly receivable.
                return self._eastmoney_rules(product,events)
        if not re.fullmatch(r"\d{6}", product.code):
            raise ValueError("公募基金代码必须为6位数字")
        url = f"https://fundf10.eastmoney.com/fhsp_{product.code}.html"
        html = self._html(url)
        require_eastmoney_dividend_page(html, product.code)
        return self._eastmoney_rules(product, parse_eastmoney_dividends(html, url))
