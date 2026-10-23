from datetime import datetime
from io import BytesIO
import json

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, NameObject, DictionaryObject, TextStringObject, NumberObject
import pytest
import requests

from src.beijing_time import TZ_CN
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import FundIdentity


def pdf_bytes(texts=None):
    writer = PdfWriter()
    values = texts or ("First page", "Second page")
    for value in values:
        page = writer.add_blank_page(400,400)
        # A small, self-contained Unicode font mapping allows pypdf to read
        # real Chinese fixture text without a host-installed font dependency.
        cmap = DecodedStreamObject()
        chars = sorted(set(value.replace("\n", "")))
        mapping = "\n".join(f"<{ord(char):04x}> <{ord(char):04x}>" for char in chars)
        cmap.set_data((f"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def /CMapName /Fixture def /CMapType 2 def 1 begincodespacerange <0000> <FFFF> endcodespacerange {len(chars)} beginbfchar\n{mapping}\nendbfchar endcmap CMapName currentdict /CMap defineresource pop end end").encode())
        descendant = DictionaryObject({NameObject("/Type"):NameObject("/Font"),NameObject("/Subtype"):NameObject("/CIDFontType2"),NameObject("/BaseFont"):NameObject("/Fixture"),NameObject("/CIDSystemInfo"):DictionaryObject({NameObject("/Registry"):TextStringObject("Adobe"),NameObject("/Ordering"):TextStringObject("Identity"),NameObject("/Supplement"):NumberObject(0)})})
        font = DictionaryObject({NameObject("/Type"):NameObject("/Font"),NameObject("/Subtype"):NameObject("/Type0"),NameObject("/BaseFont"):NameObject("/Fixture"),NameObject("/Encoding"):NameObject("/Identity-H"),NameObject("/ToUnicode"):writer._add_object(cmap),NameObject("/DescendantFonts"):ArrayObject([writer._add_object(descendant)])})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):writer._add_object(font)})})
        stream = DecodedStreamObject()
        lines = " T* ".join(f"<{line.encode('utf-16-be').hex()}> Tj" for line in value.split("\n"))
        stream.set_data(f"BT /F1 8 Tf 30 360 Td 12 TL {lines} ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def response(body, content_type="application/json", status=200):
    result = requests.Response()
    result.status_code = status
    result.headers["Content-Type"] = content_type
    result._content = body
    result.raw = BytesIO(body)
    result.encoding = "utf-8"
    return result


def row(code, title, id, published):
    return {"FUNDCODE":code,"TITLE":title,"ShortTitle":"公开债券","NEWCATEGORY":"3","PUBLISHDATE":published,"PUBLISHDATEDesc":published[:10],"ATTACHTYPE":"0","ID":id}


class Source:
    def __init__(self, code):
        self.code = code
        self.pdf_downloads = []
        self.fail_pdf = False
        self.pages = None
        self.rows = [row(code,"公开债券2026年第3季度报告","AN202610201","2026-10-20T00:00:00"),row(code,"公开债券2026年中期报告","AN202608311","2026-08-31T00:00:00"),row(code,"公开债券2025年年度报告","AN202603311","2026-03-31T00:00:00")]
    def get(self, url, **kwargs):
        if "JJGG" in url:
            assert kwargs["params"]["fundcode"] == self.code
            data = self.rows if kwargs["params"]["type"] == 3 else []
            return response(json.dumps({"Data":data,"ErrCode":0,"ErrMsg":None,"TotalCount":len(data),"Expansion":3,"PageSize":10,"PageIndex":1},ensure_ascii=False).encode())
        self.pdf_downloads.append(url)
        if self.fail_pdf:
            return response(b"temporary", "text/html",503)
        return response(pdf_bytes(self.pages), "application/pdf")


@pytest.mark.parametrize("code",["400030","876543"])
def test_generic_source_discovers_recent_quarter_and_interim_with_pdf_pages(tmp_path,code):
    from src.fund_estimate_documents import PublicFundDocuments
    store = FundEstimateStore(tmp_path / "fund.db")
    store.initialize()
    source = Source(code)
    documents = PublicFundDocuments(store, session=source)
    assert not source.pdf_downloads  # construction never performs network I/O
    found = documents.list_documents(FundIdentity("p",code,"eastmoney_fund","A",None,"债券型"))
    assert {x.report_as_of for x in found} == {"2026-09-30","2026-06-30"}
    assert {x.published_at for x in found} == {"2026-10-20T00:00:00+08:00","2026-08-31T00:00:00+08:00"}
    assert all("[第 1 页]" in x.text and "[第 2 页]" in x.text and "Second page" in x.text for x in found)
    assert all(x.url.startswith("https://pdf.dfcfw.com/pdf/H2_AN") for x in found)
    assert len(source.pdf_downloads) == 2
    documents.list_documents(FundIdentity("p",code,"eastmoney_fund","A",None,"债券型"))
    assert len(source.pdf_downloads) == 2


def test_pdf_failure_is_raised_and_retried_instead_of_cached_as_empty(tmp_path):
    from src.fund_estimate_documents import PublicFundDocuments
    store = FundEstimateStore(tmp_path / "fund.db")
    store.initialize()
    source = Source("876543")
    documents = PublicFundDocuments(store, session=source)
    fund = FundIdentity("p","876543","eastmoney_fund","A",None,"债券型")
    source.fail_pdf = True
    with pytest.raises(Exception):
        documents.list_documents(fund)
    source.fail_pdf = False
    assert len(documents.list_documents(fund)) == 2


def test_source_error_does_not_become_successful_empty_discovery(tmp_path):
    from src.fund_estimate_documents import PublicFundDocuments
    class Broken(Source):
        def get(self, url, **kwargs):
            return response(b'{"Data":null,"ErrCode":500,"ErrMsg":"retry later"}')
    store = FundEstimateStore(tmp_path / "fund.db")
    store.initialize()
    documents = PublicFundDocuments(store, session=Broken("876543"))
    with pytest.raises(Exception):
        documents.list_documents(FundIdentity("p","876543","eastmoney_fund","A",None,"债券型"))


def test_new_instance_reuses_persisted_attachment_text(tmp_path):
    from src.fund_estimate_documents import PublicFundDocuments
    store = FundEstimateStore(tmp_path / "fund.db")
    store.initialize()
    fund = FundIdentity("p","876543","eastmoney_fund","A",None,"债券型")
    first = Source("876543")
    PublicFundDocuments(store, session=first).list_documents(fund)
    second = Source("876543")
    second.fail_pdf = True
    found = PublicFundDocuments(store, session=second).list_documents(fund)
    assert len(found) == 2
    assert not second.pdf_downloads


@pytest.mark.parametrize("code", ["400030", "876543"])
def test_generic_code_runs_from_http_listing_and_pdf_to_deterministic_profile(tmp_path, code):
    from src.fund_estimate_documents import PublicFundDocuments
    from src.fund_estimate_profiles import ProfileService
    from src.fund_estimate_ai import EstimateAi
    store = FundEstimateStore(tmp_path / "fund.db")
    store.initialize()
    source = Source(code)
    source.pages = ["净资产合计 1,000,000.00 900,000.00\n报告截止日 2026年6月30日\n本基金的管理费每日按前一日基金资产净值的 0.30%年费率计提。",
                    "利率风险的敏感性分析\n影响金额（单位：人民币元）\n市场利率上升25个基点 -10,000.00 -9,000.00\n市场利率下降25个基点 10,000.00 9,000.00\n7.5 期末按债券品种分类的债券投资组合\n占基金资产净值比例（%）\n1 中期票据 1,200,000.00 120.00\n2 合计 1,200,000.00 120.00\n7.6 期末前五名债券"]
    fund = FundIdentity("p", code, "eastmoney_fund", "A", None, "债券型")
    profiles = ProfileService(store, PublicFundDocuments(store, session=source), ai=EstimateAi(store))
    saved = profiles.refresh(fund, datetime(2026,10,23,tzinfo=TZ_CN))
    assert saved is not None
    assert saved.fields["duration_nav"] == "4.000000"
    assert saved.fields["bond_weights_nav_pct"]["合计"] == "120.00"
    assert saved.fields["disclosed_management_fee_annual_pct"] == "0.30"
    assert saved.report_as_of == "2026-06-30"
    assert all(item["source_url"] == saved.source_url and item["page"] in {1,2} for item in saved.evidence)
