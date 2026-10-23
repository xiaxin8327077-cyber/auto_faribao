from datetime import datetime
import json
from pathlib import Path

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
