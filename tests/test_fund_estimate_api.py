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
    assert completed['next_refresh_at'] == '2026-10-29T17:00:00+08:00'
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


@pytest.mark.parametrize('restart', [False, True])
def test_completed_summary_remains_cached_after_next_market_close(tmp_path, monkeypatch, restart):
    import src.fund_estimate_service as service_module
    import src.fund_estimate_verification as verification_module

    store, service, ai, clock, _item = _timeout_verification_scenario(tmp_path, monkeypatch)
    first = service.verification_report()
    _finish_analysis_worker(service)
    assert service.verification_report()['refresh_complete'] is True
    clock[0] += 24 * 60 * 60
    later = datetime(2026, 10, 29, 15, 1, tzinfo=TZ_CN)

    class NextDayDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return later.astimezone(tz) if tz else later.replace(tzinfo=None)

    monkeypatch.setattr(service_module, 'datetime', NextDayDatetime)
    monkeypatch.setattr(verification_module, 'read_official_nav_state', lambda *_args: {
        'latest_confirmed_by_product': {'p1': '2026-10-28'},
        'confirmed_nav_version': 'v1', 'today_confirmed_count': 0, 'errors': [],
    })
    if restart:
        service = FundEstimateService(service.repository, FundEstimateStore(store.path), None, None,
                                      ai=ai, bond_trading_day=lambda _day: True)
        service._verification_amounts = lambda items, _snapshots: items
        service.start_official_verification_sync = lambda *_args, **_kwargs: None
    # 15:00 是旧恢复点；债市收盘前仍保持完成，不重新生成同一份分析。
    still_paused = service.verification_report()
    assert still_paused['refresh_complete'] is True
    assert still_paused['next_refresh_at'] == '2026-10-29T17:00:00+08:00'
    later = datetime(2026, 10, 29, 17, 1, tzinfo=TZ_CN)
    later_report = service.verification_report()
    _finish_analysis_worker(service)
    assert later_report['status'] == 'ready'
    assert later_report['refresh_complete'] is False  # Polling resumes; the summary remains reusable.
    assert later_report['ai_status'] == 'failed'
    assert later_report['ai_analysis'] == first['ai_analysis']
    assert ai.calls == 1


def test_unfinished_verification_reuses_existing_summary_after_deadline(tmp_path, monkeypatch):
    _store, service, ai, clock, _item = _timeout_verification_scenario(
        tmp_path, monkeypatch, profit_status='pending_dividend_ledger')
    first = service.verification_report()
    _finish_analysis_worker(service)
    clock[0] += 120
    reread = service.verification_report()
    _finish_analysis_worker(service)
    assert reread['ai_status'] == 'failed'
    assert reread['ai_analysis'] == first['ai_analysis']
    assert reread['refresh_complete'] is False
    assert ai.calls == 1


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


def test_research_api_is_authenticated_read_only_and_hides_model_artifacts(tmp_path, monkeypatch):
    from src.fund_estimate_research import FundEstimateResearch
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    store.save_research_report('p918273', {'product_id': 'p918273', 'code': '918273',
        'history_count': 250, 'promotion_allowed': False, 'models': {'private_artifact': {}}}, '2026-11-02T18:00:00+08:00')
    research = FundEstimateResearch(store, None)
    service = FundEstimateService(None, store, EstimateSources(), ProfileService(store, MemoryDocuments()),
                                   research=research)
    client, _repo, _db = client_for(tmp_path, monkeypatch, service)
    assert client.get('/api/portfolio/fund-estimate-research').status_code == 403
    response = client.get('/api/portfolio/fund-estimate-research', headers={'X-Nav-Dashboard-Key': 'secret'})
    assert response.status_code == 200
    assert response.get_json()['items'][0]['history_count'] == 250
    assert 'models' not in response.get_json()['items'][0]


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


@pytest.mark.parametrize('operation', ['models', 'test', 'save', 'copy'])
def test_changed_profile_endpoint_requires_an_explicit_new_key(tmp_path, monkeypatch, operation):
    from src.fund_estimate_ai import EstimateAi
    store = FundEstimateStore(tmp_path / 'fund_estimation.db'); store.initialize()
    saved = store.save_ai_profile(label='A', base_url='https://a.example/v1', model='reader',
        api_key='dummy-key-a', clear_api_key=False, updated_at='2026-11-02T10:00:00+08:00', activate=True)
    calls = []
    def poster(*args):
        calls.append(args)
        return 200, json.dumps({'choices': [{'message': {'content': 'pong'}}]})
    def lister(*args):
        calls.append(args)
        return 200, json.dumps({'data': [{'id': 'reader'}]})
    service = FundEstimateService(None, store, EstimateSources(), None,
                                 ai=EstimateAi(store, poster=poster, model_lister=lister))
    client, _, _ = client_for(tmp_path, monkeypatch, service)
    headers = {'X-Nav-Dashboard-Key': 'secret', 'Content-Type': 'application/json'}
    if operation in {'models', 'test'}:
        response = client.post('/api/portfolio/fund-estimate-model-settings/' + operation, headers=headers,
            json={'profile_id': saved['id'], 'base_url': 'https://b.example/v1', 'model': 'reader'})
        assert response.get_json()['ok'] is False
    else:
        profile = {'base_url': 'https://b.example/v1', 'model': 'reader'}
        profile['id' if operation == 'save' else 'copy_from_profile_id'] = saved['id']
        response = client.put('/api/portfolio/fund-estimate-model-settings', headers=headers,
                              json={'enabled': True, 'jobs_enabled': True, 'profile': profile})
        assert response.status_code == 400
        assert store.ai_profile_secret(saved['id'])['base_url'] == 'https://a.example/v1'
    assert calls == []
    assert 'API Key' in response.get_json()['message']


def test_manual_analysis_regeneration_is_authenticated_and_keeps_statistics(tmp_path, monkeypatch):
    from tests.test_fund_estimate_analysis import RecordingAi, sample_data
    store = FundEstimateStore(tmp_path / 'fund_estimation.db'); store.initialize()
    items, _, _ = sample_data()
    for item in items:
        store.save_daily_verification(item)
    ai = RecordingAi()
    service = FundEstimateService(None, store, EstimateSources(), None, ai=ai)
    service.verification_report()
    _finish_analysis_worker(service)
    before = service.verification_report()
    client, _, _ = client_for(tmp_path, monkeypatch, service)
    path = '/api/portfolio/fund-estimate-verifications/regenerate'
    assert client.post(path, json={}).status_code == 403
    ai.model = 'new-selected-model'
    response = client.post(path, headers={'X-Nav-Dashboard-Key': 'secret'}, json={})
    assert response.status_code == 202
    result = response.get_json()
    assert result['ai_model'] == 'new-selected-model'
    assert result['ai_status'] in {'generating', 'ready'}
    assert result['stats'] == before['stats']
    assert result['items'] == before['items']
    _finish_analysis_worker(service)
    assert service.verification_report()['ai_model'] == 'new-selected-model'
    assert len(ai.calls) == 2


def test_mixed_activation_and_flat_update_checks_actual_key_owner(tmp_path):
    from src.fund_estimate_ai import AiSettingsError
    store = FundEstimateStore(tmp_path / 'fund_estimation.db'); store.initialize()
    def add(label, active):
        return store.save_ai_profile(label=label, base_url=f'https://{label}.example/v1',
            model='reader', api_key=f'dummy-key-{label}', clear_api_key=False,
            updated_at='2026-11-02T10:00:00+08:00', activate=active)
    first, second = add('a', True), add('b', False)
    service = FundEstimateService(None, store, EstimateSources(), None)
    with pytest.raises(AiSettingsError, match='API Key'):
        service.save_model_settings({'enabled': True, 'jobs_enabled': True,
            'active_profile_id': second['id'], 'base_url': 'https://a.example/v1', 'model': 'reader'},
            datetime(2026, 11, 2, 10, 0, tzinfo=TZ_CN))
    assert store.public_ai_settings()['active_profile_id'] == first['id']
    assert store.ai_profile_secret(second['id'])['base_url'] == 'https://b.example/v1'


def test_storage_rejects_endpoint_change_without_replacing_saved_key(tmp_path):
    from src.fund_estimate_ai import AiSettingsError
    store = FundEstimateStore(tmp_path / 'fund_estimation.db'); store.initialize()
    original = store.save_ai_profile(label='A', base_url='https://a.example/v1', model='reader',
        api_key='dummy-key-a', clear_api_key=False, updated_at='2026-11-02T10:00:00+08:00')
    with pytest.raises(AiSettingsError, match='API Key'):
        store.save_ai_profile(profile_id=original['id'], label='A', base_url='https://b.example/v1',
            model='reader', api_key=None, clear_api_key=False, updated_at='2026-11-02T10:01:00+08:00')
    assert store.ai_profile_secret(original['id'])['base_url'] == 'https://a.example/v1'


def test_model_profiles_support_switch_copy_discovery_and_key_read(tmp_path, monkeypatch):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    seen = {}

    def lister(settings):
        seen["url"] = settings["base_url"]
        seen["key"] = settings["api_key"]
        return 200, json.dumps({"data": [{"id": "reader-b"}, {"id": "reader-a"}]})

    from src.fund_estimate_ai import EstimateAi

    service = FundEstimateService(
        None, store, EstimateSources(), ProfileService(store, MemoryDocuments()),
        ai=EstimateAi(store, model_lister=lister),
    )
    client, repository, _db = client_for(tmp_path, monkeypatch, service)
    service.repository = repository
    headers = {"X-Nav-Dashboard-Key": "secret", "Content-Type": "application/json"}

    first = client.put(
        "/api/portfolio/fund-estimate-model-settings", headers=headers,
        data=json.dumps({
            "enabled": True, "jobs_enabled": False,
            "profile": {
                "label": "LongCat", "base_url": "https://api.longcat.chat/openai",
                "model": "LongCat-2.5-Preview", "api_key": "sk-first-secret",
            },
        }),
    )
    assert first.status_code == 200
    view = first.get_json()
    assert len(view["profiles"]) == 1
    first_id = view["profiles"][0]["id"]
    assert view["active_profile_id"] == first_id
    # 列表只给脱敏值，完整密钥不出现在常规响应里。
    assert "sk-first-secret" not in json.dumps(view)
    assert view["profiles"][0]["api_key_hint"] == "····cret"

    second = client.put(
        "/api/portfolio/fund-estimate-model-settings", headers=headers,
        data=json.dumps({
            "enabled": True, "jobs_enabled": False,
            "profile": {
                "label": "百炼", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen3.7-plus", "api_key": "sk-second-secret",
            },
        }),
    )
    view = second.get_json()
    assert [item["label"] for item in view["profiles"]] == ["LongCat", "百炼"]
    second_id = view["profiles"][1]["id"]
    assert view["active_profile_id"] == second_id

    switched = client.put(
        "/api/portfolio/fund-estimate-model-settings", headers=headers,
        data=json.dumps({"enabled": True, "jobs_enabled": False, "active_profile_id": first_id}),
    )
    assert switched.get_json()["active_profile_id"] == first_id
    assert store.secret_ai_settings()["api_key"] == "sk-first-secret"

    # 手动复制：单独接口按需返回原始密钥，且禁止缓存。
    key_response = client.get(
        f"/api/portfolio/fund-estimate-model-settings/profiles/{second_id}/key", headers=headers,
    )
    assert key_response.status_code == 200
    assert key_response.get_json()["api_key"] == "sk-second-secret"
    assert key_response.headers["Cache-Control"] == "private, no-store"
    assert client.get(
        f"/api/portfolio/fund-estimate-model-settings/profiles/{second_id}/key",
    ).status_code == 403

    options = client.post(
        "/api/portfolio/fund-estimate-model-settings/models", headers=headers,
        data=json.dumps({"base_url": "https://api.longcat.chat/openai"}),
    )
    assert options.status_code == 200
    assert options.get_json()["ok"] is True
    assert options.get_json()["models"] == ["reader-a", "reader-b"]
    assert seen == {"url": "https://api.longcat.chat/openai/v1/models", "key": "sk-first-secret"}

    # 换地址且未提供密钥时不能把已保存密钥发出去。
    seen.clear()
    blocked = client.post(
        "/api/portfolio/fund-estimate-model-settings/models", headers=headers,
        data=json.dumps({"base_url": "https://other.example.test/v1"}),
    )
    assert blocked.get_json()["ok"] is False
    assert "API Key" in blocked.get_json()["message"]
    assert seen == {}

    # 新配置可以从已有配置复制，密钥由服务端复制，不经过页面。
    copied = client.put(
        "/api/portfolio/fund-estimate-model-settings", headers=headers,
        data=json.dumps({
            "enabled": True, "jobs_enabled": False,
            "profile": {
                "label": "副本", "base_url": "https://api.longcat.chat/openai",
                "model": "LongCat-2.5-Preview", "copy_from_profile_id": first_id,
            },
        }),
    )
    assert copied.status_code == 200
    copied_view = copied.get_json()
    assert len(copied_view["profiles"]) == 3
    copied_id = copied_view["active_profile_id"]
    assert store.ai_profile_secret(copied_id)["api_key"] == "sk-first-secret"
    assert "sk-first-secret" not in json.dumps(copied_view)

    deleted = client.delete(
        f"/api/portfolio/fund-estimate-model-settings/profiles/{first_id}", headers=headers,
    )
    assert deleted.status_code == 200
    assert first_id not in [item["id"] for item in deleted.get_json()["profiles"]]


def test_connection_uses_selected_profile_key(tmp_path, monkeypatch):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    seen = {}

    def poster(settings, messages, max_tokens):
        seen["key"] = settings["api_key"]
        seen["url"] = settings["base_url"]
        return 200, json.dumps({"choices": [{"message": {"content": "pong"}}]})

    from src.fund_estimate_ai import EstimateAi

    service = FundEstimateService(
        None, store, EstimateSources(), ProfileService(store, MemoryDocuments()),
        ai=EstimateAi(store, poster=poster),
    )
    client, repository, _db = client_for(tmp_path, monkeypatch, service)
    service.repository = repository
    headers = {"X-Nav-Dashboard-Key": "secret", "Content-Type": "application/json"}
    first = client.put(
        "/api/portfolio/fund-estimate-model-settings", headers=headers,
        data=json.dumps({"profile": {"label": "A", "base_url": "https://a.example.test/v1", "model": "m-a", "api_key": "key-a"}}),
    ).get_json()
    first_id = first["profiles"][0]["id"]
    second = client.put(
        "/api/portfolio/fund-estimate-model-settings", headers=headers,
        data=json.dumps({"profile": {"label": "B", "base_url": "https://b.example.test/v1", "model": "m-b", "api_key": "key-b"}}),
    ).get_json()
    second_id = second["profiles"][1]["id"]
    client.put(
        "/api/portfolio/fund-estimate-model-settings", headers=headers,
        data=json.dumps({"active_profile_id": first_id}),
    )
    tested = client.post(
        "/api/portfolio/fund-estimate-model-settings/test", headers=headers,
        data=json.dumps({"base_url": "https://b.example.test/v1", "model": "m-b", "profile_id": second_id}),
    )
    assert tested.get_json()["ok"] is True
    # 编辑非激活配置时，测试连接必须用该配置自己的密钥。
    assert seen == {"key": "key-b", "url": "https://b.example.test/v1/chat/completions"}


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
