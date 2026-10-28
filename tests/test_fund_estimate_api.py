from datetime import datetime
import json
from pathlib import Path

import pytest
from flask import Flask

from src.beijing_time import TZ_CN
from src.fund_estimate_service import FundEstimateService
from src.fund_estimate_sources import EstimateSources
from src.fund_estimate_profiles import ProfileService, MemoryDocuments
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import EstimateSnapshot
from src.nav_dashboard import get_dashboard_payload
from src.portfolio_api import create_portfolio_blueprint
from src.portfolio_db import SCHEMA_VERSION, PortfolioDatabase
from src.portfolio_models import Product, ProductType, Transaction, TransactionStatus, TransactionType
from src.portfolio_repository import PortfolioRepository
from datetime import date


def _timeout_verification_scenario(tmp_path, monkeypatch, *, profit_status='ready'):
    """Exercise the actual report and analysis caches with a timed-out model."""
    from types import SimpleNamespace
    import src.fund_estimate_analysis as analysis_module
    import src.fund_estimate_service as service_module
    import src.fund_estimate_verification as verification_module

    now = datetime(2026, 10, 28, 20, 0, tzinfo=TZ_CN)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    class TimeoutAi:
        def __init__(self):
            self.calls = 0

        def is_enabled(self):
            return True

        def complete(self, _messages, max_tokens=4096):
            self.calls += 1
            raise TimeoutError('model response timed out')

    clock = [1_800_000_000.0]
    monkeypatch.setattr(service_module, 'datetime', FixedDatetime)
    monkeypatch.setattr(analysis_module, 'time', lambda: clock[0])
    monkeypatch.setattr(service_module, 'discover_funds', lambda *_args, **_kwargs: [SimpleNamespace(product_id='p1')])
    monkeypatch.setattr(verification_module, 'read_official_nav_state', lambda *_args: {
        'latest_confirmed_by_product': {'p1': '2026-10-28'},
        'confirmed_nav_version': 'v1', 'today_confirmed_count': 1, 'errors': [],
    })
    store = FundEstimateStore(tmp_path / 'verification-retry.db')
    store.initialize()
    item = {'product_id': 'p1', 'code': '000001', 'name': '测试基金', 'trade_date': '2026-10-28',
            'predicted_pct': '0.10', 'realized_pct': '0.08', 'error_pct': '0.02',
            'direction_ok': True, 'verification_status': 'verified', 'profit_status': profit_status}
    store.save_daily_verification(item)
    store.set_watermark('official-nav-verification-meta:v1',
                        json.dumps({'processed_active_nav_version': 'v1', 'errors': []}), now.isoformat())
    store.set_watermark('official-nav-verification-generation:v1', 'ready:1', now.isoformat())
    repo = SimpleNamespace(list_quotes=lambda: [], list_products=lambda: [])
    ai = TimeoutAi()
    service = FundEstimateService(repo, store, None, None, ai=ai, bond_trading_day=lambda _day: True)
    service._verification_amounts = lambda items, _snapshots: items
    service.start_official_verification_sync = lambda *_args, **_kwargs: None
    return store, service, ai, clock, item


def _finish_analysis_worker(service):
    worker = service._analysis._worker
    if worker is not None:
        worker.join(timeout=3)
        assert not worker.is_alive()


@pytest.mark.parametrize('restart', [False, True])
def test_completed_verification_keeps_timeout_summary_after_retry_deadline(tmp_path, monkeypatch, restart):
    store, service, ai, clock, _item = _timeout_verification_scenario(tmp_path, monkeypatch)
    first = service.verification_report()
    assert first['ai_status'] == 'generating'
    _finish_analysis_worker(service)
    assert ai.calls == 1

    # User returns after the retry deadline without having polled after the timeout.
    clock[0] += 120
    if restart:
        service = FundEstimateService(service.repository, FundEstimateStore(store.path), None, None,
                                      ai=ai, bond_trading_day=lambda _day: True)
        service._verification_amounts = lambda items, _snapshots: items
        service.start_official_verification_sync = lambda *_args, **_kwargs: None
    completed = service.verification_report()
    _finish_analysis_worker(service)
    assert completed['ai_status'] == 'failed'
    assert completed['ai_analysis'] == first['ai_analysis']
    assert completed['refresh_complete'] is True
    assert completed['next_refresh_at'] == '2026-10-29T15:00:00+08:00'
    assert ai.calls == 1


def test_completed_summary_is_not_restarted_during_official_sync(tmp_path, monkeypatch):
    store, service, ai, clock, _item = _timeout_verification_scenario(tmp_path, monkeypatch)
    service.verification_report()
    _finish_analysis_worker(service)
    completed = service.verification_report()
    assert completed['refresh_complete'] is True
    clock[0] += 120
    store.set_watermark('official-nav-verification-generation:v1', 'running:2', '2026-10-28')

    first_update = service.verification_report()
    second_update = service.verification_report()
    _finish_analysis_worker(service)
    assert first_update['status'] == second_update['status'] == 'updating'
    assert first_update['ai_status'] == second_update['ai_status'] == 'failed'
    assert first_update['ai_analysis'] == second_update['ai_analysis'] == completed['ai_analysis']
    assert ai.calls == 1


def test_unfinished_verification_may_retry_failed_ai_after_deadline(tmp_path, monkeypatch):
    _store, service, ai, clock, _item = _timeout_verification_scenario(
        tmp_path, monkeypatch, profit_status='pending_dividend_ledger')
    service.verification_report()
    _finish_analysis_worker(service)
    clock[0] += 120
    retry = service.verification_report()
    _finish_analysis_worker(service)
    assert retry['ai_status'] == 'generating'
    assert retry['refresh_complete'] is False
    assert ai.calls == 2


def test_changed_verification_data_gets_new_analysis_after_completion(tmp_path, monkeypatch):
    store, service, ai, _clock, item = _timeout_verification_scenario(tmp_path, monkeypatch)
    service.verification_report()
    _finish_analysis_worker(service)
    assert service.verification_report()['refresh_complete'] is True

    store.save_daily_verification({**item, 'predicted_pct': '0.30', 'error_pct': '0.22'})
    changed = service.verification_report()
    _finish_analysis_worker(service)
    assert changed['ai_status'] == 'generating'
    assert changed['refresh_complete'] is False
    assert ai.calls == 2


def client_for(tmp_path, monkeypatch, service):
    path = tmp_path / "token"
    path.write_text("secret", encoding="utf-8")
    monkeypatch.setattr("src.nav_dashboard.get_access_token", lambda: "secret")
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    repository.add_product(Product("p918273", "eastmoney_fund", "918273", "新买入纯债A", ProductType.PUBLIC_FUND, metadata_json='{"fund_type":"债券型"}'))
    repository.create_transaction(Transaction(
        "buy", "p918273", TransactionType.SIP_PURCHASE, TransactionStatus.PENDING_QUOTE,
        date(2026, 10, 23), "sip-buy",
    ))
    runtime = type("R", (), {"repository": repository, "write_enabled": True, "migration_error": ""})()
    app = Flask(__name__)
    app.register_blueprint(create_portfolio_blueprint(runtime, lambda _name: None, estimate_service=service))
    return app.test_client(), repository, database


def snapshot():
    return EstimateSnapshot(
        "p918273", "918273", "2026-10-23", "mark_to_market_total_return", "experimental",
        ["<script>alert(1)</script>"], "2026-10-22", "1.0000", "2099-01-01T00:00:00+08:00",
        "2026-06-30", "2026-08-28T00:00:00+08:00", "2026-10-22", "model-1",
        "0.0300", None, None, "scenario", "试验，情景范围不是概率置信区间",
        {"note": "市场参考<img>"}, "2026-10-23T10:00:00+08:00",
    )


def test_estimate_api_requires_dashboard_key(tmp_path, monkeypatch):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    service = FundEstimateService(None, store, EstimateSources(), ProfileService(store, MemoryDocuments()))
    client, _repo, _db = client_for(tmp_path, monkeypatch, service)
    assert client.get("/api/portfolio/fund-estimates").status_code == 403
    assert client.get("/api/portfolio/fund-estimate-model-settings").status_code == 403


def test_get_only_reads_cached_estimates(tmp_path, monkeypatch):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    store.save_snapshot(snapshot(), slot="2026-10-23T10:00", source_version="v1")
    calls = []
    service = FundEstimateService(None, store, EstimateSources(), ProfileService(store, MemoryDocuments()))
    service.run_pending = lambda *args, **kwargs: calls.append("run")
    service.tick = lambda *args, **kwargs: calls.append("tick")
    client, repository, _db = client_for(tmp_path, monkeypatch, service)
    service.repository = repository
    response = client.get("/api/portfolio/fund-estimates", headers={"X-Nav-Dashboard-Key": "secret"})
    assert response.status_code == 200
    body = response.get_json()
    assert calls == []
    assert body["items"][0]["return_pct"] == "0.0300"
    assert body["items"][0]["status"] == "experimental"


def test_model_settings_round_trip_hides_key_and_tests_connection(tmp_path, monkeypatch):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    seen = {}

    def poster(settings, messages, max_tokens):
        seen["key"] = settings["api_key"]
        seen["url"] = settings["base_url"]
        if settings["api_key"] == "super-secret-key":
            raise RuntimeError("failed for super-secret-key")
        return 200, json.dumps({"choices": [{"message": {"content": "pong"}}]})

    from src.fund_estimate_ai import EstimateAi

    service = FundEstimateService(None, store, EstimateSources(), ProfileService(store, MemoryDocuments()), ai=EstimateAi(store, poster=poster))
    client, repository, _db = client_for(tmp_path, monkeypatch, service)
    service.repository = repository
    saved = client.put(
        "/api/portfolio/fund-estimate-model-settings",
        headers={"X-Nav-Dashboard-Key": "secret", "Content-Type": "application/json"},
        data=json.dumps({"base_url": "https://models.example.test/v1", "model": "fund-reader", "api_key": "super-secret-key", "enabled": True, "jobs_enabled": False}),
    )
    assert saved.status_code == 200
    view = saved.get_json()
    assert "super-secret-key" not in json.dumps(view)
    assert view["api_key_configured"] is True
    assert view["base_url"] == "https://models.example.test/v1"
    assert view["model"] == "fund-reader"
    tested = client.post(
        "/api/portfolio/fund-estimate-model-settings/test",
        headers={"X-Nav-Dashboard-Key": "secret", "Content-Type": "application/json"},
        data=json.dumps({"base_url": "https://models.example.test/v1", "model": "fund-reader", "api_key": "super-secret-key"}),
    )
    assert tested.status_code == 200
    assert tested.get_json()["ok"] is False
    assert "super-secret-key" not in tested.get_json()["message"]
    source = Path("src/fund_estimate_ai.py").read_text(encoding="utf-8")
    assert "longcat_client" not in source
    assert "QWEN_API_KEY" not in source


def test_estimate_fields_do_not_change_confirmed_profit(tmp_path, monkeypatch):
    assert SCHEMA_VERSION == 7
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    store.save_snapshot(snapshot(), slot="slot", source_version="v")
    client, repository, database = client_for(tmp_path, monkeypatch, FundEstimateService(None, store, EstimateSources(), ProfileService(store, MemoryDocuments())))
    before = database.path.read_bytes()
    response = client.get("/api/portfolio/fund-estimates", headers={"X-Nav-Dashboard-Key": "secret"})
    assert response.status_code == 200
    assert database.path.read_bytes() == before
    assert repository.get_product("p918273").name == "新买入纯债A"


def test_verification_endpoint_requires_key_and_returns_report(tmp_path, monkeypatch):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    service = FundEstimateService(None, store, EstimateSources(), ProfileService(store, MemoryDocuments()))
    client, _repo, _db = client_for(tmp_path, monkeypatch, service)
    assert client.get("/api/portfolio/fund-estimate-verifications").status_code == 403
    response = client.get("/api/portfolio/fund-estimate-verifications", headers={"X-Nav-Dashboard-Key": "secret"})
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["items"] == []
    assert payload["stats"]["pairs"] == 0
    assert 'snapshot_items' not in payload  # 页面只消费每日结果，分钟记录不随轮询传输。


def test_verification_failure_is_not_disguised_as_empty_success(tmp_path, monkeypatch):
    class BrokenService:
        def verification_report(self):
            raise RuntimeError("database temporarily unavailable")

    client, _repo, _db = client_for(tmp_path, monkeypatch, BrokenService())
    response = client.get("/api/portfolio/fund-estimate-verifications",
                          headers={"X-Nav-Dashboard-Key": "secret"})
    assert response.status_code == 503
    assert response.get_json()["status"] == "error"


def test_verification_service_unavailable_is_explicit(tmp_path, monkeypatch):
    client, _repo, _db = client_for(tmp_path, monkeypatch, None)
    response = client.get("/api/portfolio/fund-estimate-verifications",
                          headers={"X-Nav-Dashboard-Key": "secret"})
    assert response.status_code == 503
    assert response.get_json()["status"] == "error"


def test_verification_recovers_last_successful_report_after_read_failure(tmp_path, monkeypatch):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    saved = snapshot()
    store.save_snapshot(saved, slot="final", source_version="v1")
    store.save_verification(product_id=saved.product_id, code=saved.code,
                            trade_date=saved.trade_date,
                            snapshot_generated_at=saved.generated_at,
                            realized_return_pct="0.04", recorded_at=saved.generated_at)
    service = FundEstimateService(None, store, EstimateSources(), None)
    first = service.verification_report()
    assert len(first["items"]) == 1
    # A fresh service must recover from durable storage, not browser/process memory.
    restarted = FundEstimateService(None, store, EstimateSources(), None)
    monkeypatch.setattr(store, "read_verifications", lambda: (_ for _ in ()).throw(RuntimeError("busy")))
    recovered = restarted.verification_report()
    assert recovered["status"] == "stale"
    assert recovered["items"] == first["items"]


def test_restart_empty_read_does_not_overwrite_persistent_success(tmp_path, monkeypatch):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    saved = snapshot()
    store.save_snapshot(saved, slot="final", source_version="v1")
    store.save_verification(product_id=saved.product_id, code=saved.code,
                            trade_date=saved.trade_date, snapshot_generated_at=saved.generated_at,
                            realized_return_pct="0.04", recorded_at=saved.generated_at)
    first = FundEstimateService(None, store, EstimateSources(), None).verification_report()
    restarted = FundEstimateService(None, store, EstimateSources(), None)
    monkeypatch.setattr(store, 'read_verifications', lambda: [])
    recovered = restarted.verification_report()
    assert recovered['status'] == 'stale'
    assert recovered['items'] == first['items']


def test_official_notification_while_worker_busy_runs_another_pass(tmp_path):
    from threading import Event
    from types import SimpleNamespace
    store = FundEstimateStore(tmp_path / 'fund_estimation.db')
    store.initialize()
    entered, release, second = Event(), Event(), Event()
    service = FundEstimateService(SimpleNamespace(list_quotes=lambda: []), store, EstimateSources(), None)
    calls = []

    def sync(_now=None):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        else:
            second.set()

    service.sync_official_verifications = sync
    service.start_official_verification_sync(force=True)
    assert entered.wait(3)
    service.start_official_verification_sync(force=True)
    release.set()
    assert second.wait(3)
    assert len(calls) >= 2


def test_declared_dividend_does_not_compare_against_unconfirmed_ledger_money(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from decimal import Decimal
    store = FundEstimateStore(tmp_path / 'fund_estimation.db')
    store.initialize()
    day = date(2026, 10, 27)
    previous = SimpleNamespace(quote_date=date(2026, 10, 26), unit_nav=Decimal('1'))
    current = SimpleNamespace(quote_date=day, unit_nav=Decimal('.99'))
    repo = SimpleNamespace(get_product=lambda _: SimpleNamespace(currency='CNY'),
        list_transactions=lambda **_: [], list_quotes=lambda _: [previous, current])
    monkeypatch.setattr('src.portfolio_profit.calculate_latest_profit', lambda *a, **k: (day, Decimal('-10')))
    monkeypatch.setattr('src.portfolio_profit._nav_earning_shares', lambda *a, **k: Decimal('1000'))
    item = {'product_id': 'p', 'trade_date': day.isoformat(), 'snapshot_generated_at': 'final',
            'predicted_pct': '1', 'realized_pct': '1', 'cash_dividend_per_share': '.02',
            'snapshot_base_nav': '1'}
    result = FundEstimateService(repo, store, EstimateSources(), None)._verification_amounts([item], [item])[0]
    assert result['predicted_profit'] == '10.00'
    assert result['realized_profit'] is None
    assert result['profit_error'] is None
    assert result['profit_status'] == 'pending_dividend_ledger'


def test_revised_official_version_is_updating_even_when_today_count_unchanged(tmp_path, monkeypatch):
    from types import SimpleNamespace
    store = FundEstimateStore(tmp_path / 'fund_estimation.db')
    store.initialize()
    store.save_daily_verification({'product_id':'p', 'code':'1', 'name':'fund',
        'trade_date':'2026-10-26', 'snapshot_generated_at':None, 'predicted_pct':None,
        'realized_pct':None, 'error_pct':None, 'direction_ok':None})
    store.set_watermark('official-nav-verification-meta:v1',
        json.dumps({'processed_active_nav_version':'old', 'errors':[]}), '2026-10-27')
    repo = SimpleNamespace(list_quotes=lambda:[], list_products=lambda:[])
    service = FundEstimateService(repo, store, EstimateSources(), None)
    monkeypatch.setattr(service, 'start_official_verification_sync', lambda:None)
    monkeypatch.setattr('src.fund_estimate_verification.read_official_nav_state',
        lambda *a: {'confirmed_nav_version':'new', 'today_confirmed_count':0, 'errors':[]})
    report = service.verification_report()
    assert report['status'] == 'updating'
    assert report['today_confirmed_count'] == 0
    assert len(report['items']) == 1
    store.set_watermark('official-nav-verification-meta:v1',
        json.dumps({'processed_active_nav_version':'new', 'errors':[]}), '2026-10-27')
    assert service.verification_report()['status'] == 'ready'
