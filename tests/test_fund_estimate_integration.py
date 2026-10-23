"""临时估值库 + 模拟组合的全链路整合：数据准备→训练→快照→缓存API，不动正式账本。

网络只在传输边界替换（公告 JSON/PDF、曲线 HTTP），其余组件走生产实现：
PublicFundDocuments、CfetsCurveSource、ProfileService、train_models、
read_snapshots、estimate_holding_amount 与只读缓存 API。
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
import json

from flask import Flask
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, NameObject, DictionaryObject, TextStringObject, NumberObject
import requests

from src.beijing_time import TZ_CN
from src.fund_estimate_cfets import CFETS_REALTIME, CfetsCurveSource
from src.fund_estimate_documents import PublicFundDocuments
from src.fund_estimate_profiles import ProfileService
from src.fund_estimate_service import FundEstimateService
from src.fund_estimate_sources import EstimateSources
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import FundIdentity
from src.portfolio_api import create_portfolio_blueprint
from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    Position,
    Product,
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_repository import PortfolioRepository

NOW = datetime(2026, 10, 23, 19, 0, tzinfo=TZ_CN)
HELD = FundIdentity("p-held", "918273", "eastmoney_fund", "A", None, "债券型")
PENDING = FundIdentity("p-pending", "400030", "eastmoney_fund", "A", None, "债券型")
REPORT_TEXT = "报告期：2026-06-30\n公告日期：2026-08-28\n净值口径久期：3.240年\n"


def _response(body, content_type="application/json", status=200):
    result = requests.Response()
    result.status_code = status
    result.headers["Content-Type"] = content_type
    result._content = body
    result.raw = BytesIO(body)
    result.encoding = "utf-8"
    return result


def _pdf_bytes(text):
    writer = PdfWriter()
    page = writer.add_blank_page(400, 400)
    cmap = DecodedStreamObject()
    chars = sorted(set(text.replace("\n", "")))
    mapping = "\n".join(f"<{ord(char):04x}> <{ord(char):04x}>" for char in chars)
    cmap.set_data((f"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def /CMapName /Fixture def /CMapType 2 def 1 begincodespacerange <0000> <FFFF> endcodespacerange {len(chars)} beginbfchar\n{mapping}\nendbfchar endcmap CMapName currentdict /CMap defineresource pop end end").encode())
    descendant = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/CIDFontType2"), NameObject("/BaseFont"): NameObject("/Fixture"), NameObject("/CIDSystemInfo"): DictionaryObject({NameObject("/Registry"): TextStringObject("Adobe"), NameObject("/Ordering"): TextStringObject("UCS"), NameObject("/Supplement"): NumberObject(0)})})
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type0"), NameObject("/BaseFont"): NameObject("/Fixture"), NameObject("/Encoding"): NameObject("/Identity-H"), NameObject("/ToUnicode"): writer._add_object(cmap), NameObject("/DescendantFonts"): ArrayObject([writer._add_object(descendant)])})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    lines = " T* ".join(f"<{line.encode('utf-16-be').hex()}> Tj" for line in text.split("\n"))
    stream.set_data(f"BT /F1 8 Tf 30 360 Td 12 TL {lines} ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class _DocSession:
    """公开公告接口替身：JJGG 列表与 PDF 下载都在传输边界。"""

    def __init__(self):
        self.pdf_downloads = []

    def get(self, url, **kwargs):
        if "JJGG" in url:
            code = kwargs["params"]["fundcode"]
            an = "AN202608311" if code == "918273" else "AN202608312"
            row = {"FUNDCODE": code, "TITLE": "公开债券2026年中期报告", "ShortTitle": "中报", "NEWCATEGORY": "3",
                   "PUBLISHDATE": "2026-08-31T00:00:00", "PUBLISHDATEDesc": "2026-08-31", "ATTACHTYPE": "0", "ID": an}
            body = json.dumps({"Data": [row], "ErrCode": 0, "ErrMsg": None, "TotalCount": 1,
                               "Expansion": 3, "PageSize": 10, "PageIndex": 1}, ensure_ascii=False).encode()
            return _response(body)
        self.pdf_downloads.append(url)
        return _response(_pdf_bytes(REPORT_TEXT), "application/pdf")


def _curve(day, quote_time):
    level = Decimal("1.2") + Decimal(str((day.toordinal() % 13) - 6)) * Decimal("0.005")
    rows = [['', '', '', str(term), 'true', '', '', ''] for term in (3, 5, 10)]
    rows += [['', '', '', str(term), '', '', str(level + Decimal(term) * Decimal("0.02")), ''] for term in (3, 5, 10)]
    return {'head': {'rep_code': '200'}, 'data': {'date': day.isoformat(), 'time': quote_time, 'data': rows}}


def _nav_fetcher(code, start, end, page, page_size):
    rows = []
    day = min(end, date(2026, 10, 22))  # 今天的净值尚未公布，只给到前一交易日
    while day >= start and len(rows) < 63:
        if day.weekday() < 5:
            nav = Decimal("1.4") + Decimal(str((day.toordinal() % 11) - 5)) * Decimal("0.0004")
            rows.append({"nav_date": day.isoformat(), "unit_nav": str(nav),
                         "available_at": f"{day.isoformat()}T17:05:00+08:00",
                         "source": "eastmoney_fund", "source_version": f"nav-{day.isoformat()}"})
        day -= timedelta(days=1)
    return {"total": len(rows), "rows": rows}


def _build(tmp_path, monkeypatch):
    monkeypatch.setattr("src.nav_dashboard.get_access_token", lambda: "secret")
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repo = PortfolioRepository(database)
    repo.add_product(Product("p-held", "eastmoney_fund", "918273", "稳健纯债A", ProductType.PUBLIC_FUND,
                             metadata_json='{"fund_type":"债券型"}'))
    repo.add_product(Product("p-pending", "eastmoney_fund", "400030", "新买入纯债A", ProductType.PUBLIC_FUND,
                             metadata_json='{"fund_type":"债券型"}'))
    repo.replace_position(Position("p-held", Decimal("10000"), Decimal("0"), Decimal("10000"), Decimal("14000")))
    repo.create_transaction(Transaction("buy-pending", "p-pending", TransactionType.MANUAL_PURCHASE,
                                        TransactionStatus.PENDING_CONFIRMATION, date(2026, 10, 23), "买入待确认"))

    store = FundEstimateStore(tmp_path / "estimates.db")
    store.initialize()
    clock = lambda: NOW
    curve_source = CfetsCurveSource(store, fetcher=lambda kind, day, quote_time: _curve(day, quote_time),
                                    clock=clock, calendar=lambda d: d.weekday() < 5)
    doc_session = _DocSession()
    documents = PublicFundDocuments(store, session=doc_session)
    profiles = ProfileService(store, documents)
    sources = EstimateSources(
        nav_fetcher=_nav_fetcher,
        dividend_fetcher=lambda identity, start, end: {"complete": True, "events": []},
        factor_fetcher=curve_source.fetch_daily_factors,
        intraday_reader=curve_source.read_intraday_factors,
        transient_reader=curve_source.take_transient_failures,
        clock=clock,
    )
    service = FundEstimateService(repo, store, sources, profiles)
    runtime = type("R", (), {"repository": repo, "write_enabled": True, "migration_error": ""})()
    app = Flask(__name__)
    app.register_blueprint(create_portfolio_blueprint(runtime, lambda _name: None, estimate_service=service))
    return service, store, database, app.test_client(), doc_session


def _backfill_history(service):
    # 生产按日推进批次游标，每天只回补一个约30天批次；这里模拟多次调度补齐训练窗口。
    for _ in range(4):
        service._backfill(HELD, NOW, enqueue_train=True)
    service.run_pending(NOW, max_jobs=10)


def test_full_chain_trains_and_reports_held_and_pending_funds(tmp_path, monkeypatch):
    service, store, database, client, doc_session = _build(tmp_path, monkeypatch)
    headers = {"X-Nav-Dashboard-Key": "secret"}
    ledger_before = database.path.read_bytes()

    service.profiles.refresh(HELD, NOW)
    service.profiles.refresh(PENDING, NOW)
    _backfill_history(service)
    service._snapshot(HELD, NOW)
    payloads = service.read_snapshots(None, NOW)

    held = payloads[HELD.product_id]
    assert held["return_pct"] not in (None, "")
    assert held["status"] != "pending"
    assert held["holding_status"] == "held"
    expected = (Decimal("10000") * Decimal(held["base_nav"]) * Decimal(held["return_pct"]) / 100).quantize(Decimal("0.01"))
    assert Decimal(held["estimated_profit"]) == expected
    assert held["market_as_of"].startswith("2026-10-23")

    pending = payloads[PENDING.product_id]
    assert pending["estimated_profit"] is None
    assert pending["profit_reason"] == "awaiting_confirmed_shares"
    assert pending["holding_status"] == "pending_purchase"
    assert pending["pending_purchase"] is True

    report = service.store.current_profile("918273", "A")
    assert report is not None and report.report_as_of == "2026-06-30"

    items = client.get("/api/portfolio/fund-estimates", headers=headers).get_json()["items"]
    by_code = {item["code"]: item for item in items}
    assert by_code["918273"]["return_pct"] == held["return_pct"]
    assert by_code["400030"]["profit_reason"] == "awaiting_confirmed_shares"

    overview = client.get("/api/portfolio/market-overview", headers=headers).get_json()
    assert {"918273", "400030"} <= {item["code"] for item in overview["fund_estimates"].values()}
    # 全链路只写临时估值库与公开接口读取，模拟组合的正式账本保持只读。
    assert database.path.read_bytes() == ledger_before
    assert doc_session.pdf_downloads


def test_close_time_snapshot_uses_close_factors_not_missing(tmp_path, monkeypatch):
    service, store, database, client, _docs = _build(tmp_path, monkeypatch)
    _backfill_history(service)
    service._snapshot(HELD, NOW)
    saved = store.latest_snapshots([HELD.product_id])[HELD.product_id]
    # 19:00 已过收盘：快照必须由 17:00 收盘因素生成，而不是被新鲜度门槛清成缺失。
    assert saved["market_as_of"] == "2026-10-23T17:00:00+08:00"
    assert "market_missing" not in (saved.get("reason_codes") or [])
    assert saved["return_pct"] not in (None, "")
