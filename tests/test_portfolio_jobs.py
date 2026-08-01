from datetime import datetime
from types import SimpleNamespace

import pytest

from src.portfolio_jobs import (
    PortfolioJobs,
    PortfolioCycleResult,
    run_portfolio_cycle,
)
from src.portfolio_models import ProductType


def test_cycle_runs_network_sync_before_database_settlement():
    calls = []
    jobs = PortfolioJobs(
        sync_quotes=lambda _day: calls.append("quotes"),
        create_intents=lambda _day: calls.append("intents"),
        settle_pending=lambda _day: calls.append("settle"),
        accrue_income=lambda _day: calls.append("income"),
    )
    jobs.run_cycle(datetime(2026, 7, 30, 18, 0))
    assert calls == ["quotes", "intents", "settle", "income"]


def test_cycle_is_idempotent_within_same_thirty_minute_slot():
    calls = []
    jobs = PortfolioJobs(
        sync_quotes=lambda _day: calls.append("quotes"),
        create_intents=lambda _day: calls.append("intents"),
        settle_pending=lambda _day: calls.append("settle"),
        accrue_income=lambda _day: calls.append("income"),
    )
    first = jobs.run_cycle(datetime(2026, 7, 30, 18, 0))
    second = jobs.run_cycle(datetime(2026, 7, 30, 18, 15))
    assert first == PortfolioCycleResult(0, 0, 0, 0)
    assert second == PortfolioCycleResult(0, 0, 0, 0)
    assert calls == ["quotes", "intents", "settle", "income"]


def test_cycle_runs_again_in_next_thirty_minute_slot():
    calls = []
    jobs = PortfolioJobs(
        sync_quotes=lambda _day: calls.append("quotes"),
        create_intents=lambda _day: calls.append("intents"),
        settle_pending=lambda _day: calls.append("settle"),
        accrue_income=lambda _day: calls.append("income"),
    )
    jobs.run_cycle(datetime(2026, 7, 30, 18, 29))
    jobs.run_cycle(datetime(2026, 7, 30, 18, 30))
    assert calls.count("quotes") == 2
    assert calls.count("intents") == 2
    assert calls.count("settle") == 2
    assert calls.count("income") == 2


def test_cycle_records_returned_counts():
    jobs = PortfolioJobs(
        sync_quotes=lambda _day: 3,
        create_intents=lambda _day: 1,
        settle_pending=lambda _day: 2,
        accrue_income=lambda _day: 4,
    )
    result = jobs.run_cycle(datetime(2026, 7, 30, 9, 0))
    assert result == PortfolioCycleResult(3, 1, 2, 4)


def test_strict_cycle_propagates_single_product_quote_failure(monkeypatch):
    product = SimpleNamespace(
        provider="nanyin_wealth",
        code="A32069",
        name="测试产品",
        product_type=ProductType.WEALTH_NAV,
        registration_code="",
        metadata_json="",
    )
    repository = SimpleNamespace(
        list_products=lambda active_only=False: [product],
    )
    runtime = SimpleNamespace(write_enabled=True, repository=repository)
    monkeypatch.setattr(
        "src.portfolio_jobs.QuoteSyncService.sync_product",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("network")),
    )

    with pytest.raises(RuntimeError, match="network"):
        run_portfolio_cycle(
            runtime,
            datetime(2026, 7, 30, 9, 0),
            raise_on_error=True,
        )
