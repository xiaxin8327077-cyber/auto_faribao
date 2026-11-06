import sqlite3
from datetime import datetime

from flask import Flask

from src.portfolio_api import create_portfolio_blueprint
from src.portfolio_db import PortfolioDatabase
from src.portfolio_repository import PortfolioRepository


def test_market_overview_is_private_cached_and_conditional(tmp_path, monkeypatch):
    monkeypatch.setattr('src.nav_dashboard.get_access_token', lambda: 'secret')
    database = PortfolioDatabase(tmp_path / 'portfolio.db'); database.initialize()
    repo = PortfolioRepository(database)
    runtime = type('Runtime', (), {'repository': repo})()
    app = Flask(__name__)
    app.register_blueprint(create_portfolio_blueprint(runtime, lambda _: None))
    client = app.test_client()
    assert client.get('/api/portfolio/market-overview').status_code == 403
    headers = {'X-Nav-Dashboard-Key': 'secret'}
    before = database.path.read_bytes()
    result = client.get('/api/portfolio/market-overview', headers=headers)
    assert result.status_code == 200
    assert 'stock_market' in result.get_json()
    assert 'bond_barometer' in result.get_json()
    assert result.headers.get('ETag')
    headers['If-None-Match'] = result.headers['ETag']
    unchanged = client.get('/api/portfolio/market-overview', headers=headers)
    assert unchanged.status_code == 304
    assert not unchanged.data
    assert database.path.read_bytes() == before


def _stamp(value):
    return int(datetime.fromisoformat(value).timestamp())


def _index_rows():
    cn = _stamp("2026-10-23T09:59:48+08:00")
    us = _stamp("2026-10-22T16:00:00-04:00")
    return [
        {"f1": 2, "f2": 3690.48, "f3": 0.89, "f4": 32.45, "f12": "000001", "f13": 1, "f14": "上证指数", "f124": cn},
        {"f1": 2, "f2": 13210.5, "f3": 1.12, "f4": 146.2, "f12": "399001", "f13": 0, "f14": "深证成指", "f124": cn},
        {"f1": 2, "f2": 2655.7, "f3": 1.45, "f4": 38.1, "f12": "399006", "f13": 0, "f14": "创业板指", "f124": cn},
        {"f1": 2, "f2": 1204.3, "f3": -0.32, "f4": -3.9, "f12": "000688", "f13": 1, "f14": "科创50", "f124": cn},
        {"f1": 2, "f2": 1088.6, "f3": 0.21, "f4": 2.3, "f12": "899050", "f13": 0, "f14": "北证50", "f124": cn},
        {"f1": 2, "f2": 26008.86, "f3": -0.41, "f4": -107.2, "f12": "HSI", "f13": 100, "f14": "恒生指数", "f124": cn},
        {"f1": 2, "f2": 4321.9, "f3": 0.63, "f4": 27.1, "f12": "HSTECH", "f13": 124, "f14": "恒生科技指数", "f124": cn},
        {"f1": 2, "f2": 43210.1, "f3": -0.12, "f4": -52.3, "f12": "DJIA", "f13": 100, "f14": "道琼斯工业", "f124": us},
        {"f1": 2, "f2": 18450.2, "f3": 0.34, "f4": 62.7, "f12": "NDX", "f13": 100, "f14": "纳斯达克", "f124": us},
        {"f1": 2, "f2": 5832.7, "f3": 0.08, "f4": 4.6, "f12": "SPX", "f13": 100, "f14": "标普500", "f124": us},
        {"f1": 2, "f2": 867.5, "f3": -0.2, "f4": -1.7, "f12": "AU9999", "f13": 118, "f14": "黄金9999", "f124": cn},
        {"f1": 2, "f2": 870.02, "f3": -1.05, "f4": -9.24, "f12": "aum", "f13": 113, "f14": "沪金主连", "f124": cn},
    ]


def _industry_rows():
    stamp = _stamp("2026-10-23T09:59:48+08:00")
    values = [("a", "计算机", 4.08), ("b", "机械", 3.01), ("c", "电子", -1.2), ("d", "银行", -2.5)]
    return [{"f2": 100, "f3": pct, "f4": None, "f12": code, "f13": 90, "f14": name, "f124": stamp}
            for code, name, pct in values]


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _FakeSession:
    """传输边界替身：真实缓存落库路径不变，只替换行情 HTTP。"""

    def __init__(self, price_shift=0.0):
        self.price_shift = price_shift

    def get(self, url, *, params, timeout, headers):
        rows = _index_rows() if "/ulist." in url else _industry_rows()
        if self.price_shift:
            rows = [dict(row, f2=row["f2"] + self.price_shift, f3=row["f3"] + 0.01) for row in rows]
        return _Response({"rc": 0, "rt": 6, "full": 1, "data": {"total": len(rows), "diff": rows}})


def _client_with_stock_cache(tmp_path, monkeypatch):
    monkeypatch.setattr('src.nav_dashboard.get_access_token', lambda: 'secret')
    database = PortfolioDatabase(tmp_path / 'portfolio.db'); database.initialize()
    repo = PortfolioRepository(database)
    runtime = type('Runtime', (), {'repository': repo})()
    app = Flask(__name__)
    app.register_blueprint(create_portfolio_blueprint(runtime, lambda _: None))
    from src.stock_market import ensure_stock_market_schema, refresh_stock_market
    with sqlite3.connect(database.path) as conn:
        ensure_stock_market_schema(conn)
        refresh_stock_market(conn, now=datetime.fromisoformat("2026-10-23T10:00:00+08:00"), session=_FakeSession())
    import src.stock_market as stock_market
    real_now = stock_market._now
    clock = {"value": "2026-10-23T10:00:05+08:00"}
    monkeypatch.setattr(stock_market, '_now',
                        lambda value=None: real_now(value if value is not None else clock["value"]))
    return app.test_client(), clock


def test_market_overview_etag_ignores_age_growth_when_cache_is_unchanged(tmp_path, monkeypatch):
    client, clock = _client_with_stock_cache(tmp_path, monkeypatch)
    headers = {'X-Nav-Dashboard-Key': 'secret'}
    first = client.get('/api/portfolio/market-overview', headers=headers)
    assert first.status_code == 200
    stock = first.get_json()['stock_market']
    assert stock['available'] and stock['stale'] is False
    etag = first.headers['ETag']
    # 同一份缓存：仅 cache_age_seconds/source_age_seconds 增长，缓存内容与状态未变。
    clock["value"] = "2026-10-23T10:00:25+08:00"
    headers['If-None-Match'] = etag
    second = client.get('/api/portfolio/market-overview', headers=headers)
    assert second.status_code == 304
    assert not second.data
    # TTL 过期让 stale 变为 True 是有意义的状态变化，必须让 ETag 失效。
    clock["value"] = "2026-10-23T10:01:05+08:00"
    third = client.get('/api/portfolio/market-overview', headers=headers)
    assert third.status_code == 200
    assert third.headers['ETag'] != etag
    assert third.get_json()['stock_market']['stale'] is True


def test_market_overview_etag_changes_when_source_data_updates(tmp_path, monkeypatch):
    client, clock = _client_with_stock_cache(tmp_path, monkeypatch)
    headers = {'X-Nav-Dashboard-Key': 'secret'}
    first = client.get('/api/portfolio/market-overview', headers=headers)
    etag = first.headers['ETag']
    database = PortfolioDatabase(tmp_path / 'portfolio.db')
    from src.stock_market import refresh_stock_market
    with sqlite3.connect(database.path) as conn:
        refresh_stock_market(conn, now=datetime.fromisoformat("2026-10-23T10:00:30+08:00"),
                             session=_FakeSession(price_shift=1.0), force=True)
    clock["value"] = "2026-10-23T10:00:35+08:00"
    headers['If-None-Match'] = etag
    refreshed = client.get('/api/portfolio/market-overview', headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.headers['ETag'] != etag
