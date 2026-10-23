from datetime import date, datetime, timedelta
from decimal import Decimal
import time

from src.beijing_time import TZ_CN
from src.fund_estimate_profiles import FundDocument, MemoryDocuments, ProfileService
from src.fund_estimate_service import (
    FundEstimateService,
    create_fund_estimate_service,
    intraday_session,
    nav_poll_session,
    weekday_bond_day,
)
from src.fund_estimate_sources import EstimateSources
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import FundIdentity, NavObservation
from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import Position, Product, ProductType, Transaction, TransactionStatus, TransactionType
from src.portfolio_repository import PortfolioRepository
from src.server import create_app
from src.config import Config


def repository(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    return PortfolioRepository(database)


def pending_fund(repo, code="918273"):
    repo.add_product(Product("p-" + code, "eastmoney_fund", code, "新买入纯债A", ProductType.PUBLIC_FUND, metadata_json='{"fund_type":"债券型"}'))
    repo.create_transaction(Transaction(
        "buy-" + code, "p-" + code, TransactionType.MANUAL_PURCHASE, TransactionStatus.PENDING_CONFIRMATION,
        date(2026, 10, 23), "idem-" + code,
    ))


def build(tmp_path, repo, sources=None, documents=None):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    profiles = ProfileService(store, MemoryDocuments(documents or []))
    service = FundEstimateService(
        repo, store, sources or EstimateSources(nav_fetcher=lambda *a: {"rows": [], "total": 0}),
        profiles, {"enabled": True, "min_fit_samples": 60},
    )
    return service


def test_new_purchase_is_queued_within_next_tick(tmp_path):
    repo = repository(tmp_path)
    pending_fund(repo)
    calls = []
    sources = EstimateSources(nav_fetcher=lambda *a: calls.append("nav") or {"rows": [], "total": 0})
    service = build(tmp_path, repo, sources)
    service.tick(datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN))
    jobs = service.store.list_jobs("918273")
    assert any(item["job_type"] == "backfill" and item["status"] == "queued" for item in jobs)
    assert calls == []


def test_new_nav_schedules_one_training_job(tmp_path):
    repo = repository(tmp_path)
    pending_fund(repo, "564738")
    def fetcher(*_args):
        return {"total": 1, "rows": [{"nav_date": "2026-10-22", "unit_nav": "1.2", "source": "fixture", "source_version": "v1", "available_at": "2026-10-22T21:00:00+08:00"}]}
    sources = EstimateSources(nav_fetcher=fetcher, dividend_fetcher=lambda *a: {"complete": True, "events": []}, factor_fetcher=lambda *a: [])
    service = build(tmp_path, repo, sources)
    now = datetime(2026, 10, 23, 19, 0, tzinfo=TZ_CN)
    service.tick(now)
    service.run_pending(now, max_jobs=5)
    training = [item for item in service.store.list_jobs("564738") if item["job_type"] == "train"]
    assert len(training) == 1
    service.tick(now)
    service.run_pending(now, max_jobs=5)
    training = [item for item in service.store.list_jobs("564738") if item["job_type"] == "train"]
    assert len(training) == 1


def test_model_refit_follows_new_report_without_code_change(tmp_path):
    repo = repository(tmp_path)
    pending_fund(repo, "102938")
    text = "公告日期：2026-08-28\n报告期：2026-06-30\n净值口径久期：2.100年\n"
    documents = [FundDocument("https://example.test/new", "中报", "2026-08-28T08:00:00+08:00", "2026-06-30", text, "A")]
    service = build(tmp_path, repo, documents=documents)
    now = datetime(2026, 10, 23, 9, 0, tzinfo=TZ_CN)
    service.tick(now)
    service.run_pending(now, max_jobs=8)
    keys = [item["idempotency_key"] for item in service.store.list_jobs("102938") if item["job_type"] == "train"]
    assert any("profile:" in key for key in keys)
    assert "102938" not in open("src/fund_estimate_service.py", encoding="utf-8").read()


def test_trade_window_holiday_and_cross_midnight_nav_poll():
    def calendar(day):
        if day == date(2026, 10, 23):
            return False
        if day == date(2026, 10, 25):
            return True
        return weekday_bond_day(day)

    friday_evening = datetime(2026, 10, 23, 18, 30, tzinfo=TZ_CN)
    assert nav_poll_session(friday_evening, calendar) is False
    assert intraday_session(datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN), calendar) is False
    assert nav_poll_session(datetime(2026, 10, 24, 1, 30, tzinfo=TZ_CN), weekday_bond_day) is True
    assert nav_poll_session(datetime(2026, 10, 24, 10, 0, tzinfo=TZ_CN), weekday_bond_day) is False
    assert intraday_session(datetime(2026, 10, 25, 10, 0, tzinfo=TZ_CN), calendar) is True


def test_snapshot_queue_keeps_only_the_latest_minute(tmp_path):
    repo = repository(tmp_path)
    pending_fund(repo)
    service = build(tmp_path, repo)
    service.tick(datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN))
    service.tick(datetime(2026, 10, 23, 10, 5, tzinfo=TZ_CN))
    queued = [
        item for item in service.store.list_jobs("918273")
        if item["job_type"] == "intraday_snapshot" and item["status"] == "queued"
    ]
    assert len(queued) == 1
    assert queued[0]["idempotency_key"].endswith("2026-10-23T10:05")


def test_slow_ai_does_not_block_barometer_or_portfolio_cycle(tmp_path):
    repo = repository(tmp_path)
    pending_fund(repo)

    class Slow:
        def list_documents(self, identity, since=None):
            time.sleep(2)
            return []

        def refresh(self, identity, now):
            time.sleep(2)
            return None

    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    service = FundEstimateService(repo, store, EstimateSources(), Slow(), {"enabled": True})
    started = time.perf_counter()
    service.tick(datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN))
    assert time.perf_counter() - started < 0.5


def test_restart_resumes_jobs_without_duplicate_work(tmp_path):
    repo = repository(tmp_path)
    pending_fund(repo, "675849")
    calls = {"n": 0}

    def fetcher(*_args):
        calls["n"] += 1
        return {"total": 1, "rows": [{"nav_date": "2026-10-22", "unit_nav": "1", "source": "fixture", "source_version": "v", "available_at": "2026-10-22T21:00:00+08:00"}]}

    sources = EstimateSources(nav_fetcher=fetcher, dividend_fetcher=lambda *a: {"complete": True, "events": []}, factor_fetcher=lambda *a: [])
    service = build(tmp_path, repo, sources)
    now = datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN)
    service.tick(now)
    claimed = service.store.claim_jobs(now.isoformat(), 1)
    assert claimed and claimed[0]["status"] == "queued" or service.store.list_jobs("675849")[0]["status"] == "running"
    restarted = FundEstimateService(repo, service.store, sources, service.profiles, {"enabled": True})
    restarted.tick(now + timedelta(minutes=1))
    restarted.run_pending(now, max_jobs=3)
    restarted.run_pending(now, max_jobs=3)
    assert calls["n"] == 1


def test_create_service_does_not_use_network_and_failure_is_isolated(tmp_path, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("启动时不应访问网络")

    monkeypatch.setattr("src.fund_estimate_sources._live_nav_page", explode)
    repo = repository(tmp_path)
    service = create_fund_estimate_service(type("R", (), {"repository": repo})(), Config({"fund_estimate": {"enabled": False}}), path=tmp_path / "fund_estimation.db")
    assert service is not None
    assert service.jobs_enabled() is False
    blocked = tmp_path / "not-dir"
    blocked.write_text("x", encoding="utf-8")
    assert create_fund_estimate_service(type("R", (), {"repository": repo})(), None, path=blocked / "fund.db") is None
    app = create_app(Config({}), portfolio_runtime=type("R", (), {"repository": repo, "write_enabled": False})(), estimate_service=None)
    assert app is not None
