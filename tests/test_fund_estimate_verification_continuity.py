"""正式组合净值与独立每日验证结果的 SQLite 回归。"""
from datetime import date, datetime, timedelta
from decimal import Decimal
import importlib
import importlib.util

import pytest

from src.beijing_time import TZ_CN
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import EstimateSnapshot, FundIdentity, NavObservation
from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import MarketQuote, Product, ProductType
from src.portfolio_repository import PortfolioRepository


NOW = datetime(2026, 10, 27, 21, 0, tzinfo=TZ_CN)
DAY = "2026-10-27"
PREVIOUS = "2026-10-26"


@pytest.fixture
def books(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    identities = []
    for index in (1, 2):
        identity = FundIdentity(f"p{index}", f"00000{index}", "eastmoney_fund", "A", None, "债券型")
        repository.add_product(Product(identity.product_id, identity.provider, identity.code,
                                       f"纯债{index}", ProductType.PUBLIC_FUND))
        identities.append(identity)
    store = FundEstimateStore(tmp_path / "estimates.db")
    store.initialize()
    return repository, store, identities


def _sync(repository, store, identities, now=NOW, **kwargs):
    assert importlib.util.find_spec("src.fund_estimate_verification") is not None, "缺少正式净值本地对账模块"
    return importlib.import_module("src.fund_estimate_verification").sync_confirmed_verifications(
        repository, store, identities, now, **kwargs)


def _quote(repository, identity, day=DAY, nav="1.001", version="v1"):
    repository.upsert_quote(identity.product_id, MarketQuote(identity.code, date.fromisoformat(day),
        "eastmoney_fund", version, Decimal(nav)), NOW.isoformat())


def _coverage(store, identity, day=DAY, nav="1.001", dividend="0", coverage="verified_none"):
    store.save_nav(NavObservation(identity.code, day, nav, dividend, NOW.isoformat(),
        "eastmoney_fund", f"{day}-{nav}-{dividend}", "ordinary", coverage))


def _snapshot(store, identity, day=DAY, predicted="0.08", hour=15, status="experimental"):
    generated = f"{day}T{hour:02d}:00:00+08:00"
    snapshot = EstimateSnapshot(identity.product_id, identity.code, day,
        "mark_to_market_total_return", status, [], PREVIOUS, "1", generated,
        None, None, None, "model-1", predicted, None, None, "none", "试验", None, generated)
    store.save_snapshot(snapshot, slot=generated[:16], source_version=f"model-{hour}")
    return snapshot


def _cached_event(repository, identity, amount="0.02", event_id="cached-dividend"):
    with repository.database.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO fund_dividend_state(product_id,enabled_date) VALUES(?,?)",
                     (identity.product_id, PREVIOUS))
        conn.execute("""INSERT INTO fund_dividend_events(id,product_id,record_date,ex_dividend_date,
            payment_date,per_share,source_url) VALUES(?,?,?,?,?,?,?)""",
            (event_id, identity.product_id, PREVIOUS, DAY, "2026-10-28", amount,
             "https://fund.example/dividend"))


def test_first_and_second_official_nav_confirm_independently_including_zero_return(books):
    repository, store, identities = books
    first, second = identities
    for identity in identities:
        _quote(repository, identity, PREVIOUS, "1")
        _snapshot(store, identity, predicted="0")
    _quote(repository, first, nav="1")
    _coverage(store, first, nav="1")
    one = _sync(repository, store, identities)
    assert one["today_confirmed_count"] == 1
    assert one["latest_available_date"] == DAY
    item = next(row for row in store.read_daily_verifications() if row["trade_date"] == DAY)
    assert item["verification_status"] == "verified"
    assert Decimal(item["realized_pct"]) == 0
    assert item["direction_ok"] is True
    _quote(repository, second)
    _coverage(store, second)
    two = _sync(repository, store, identities)
    assert two["today_confirmed_count"] == 2
    assert two["confirmed_nav_version"] != one["confirmed_nav_version"]
    assert len([row for row in store.read_daily_verifications() if row["trade_date"] == DAY]) == 2


def test_daily_results_survive_restart_without_changing_content_or_versions(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity)
    _coverage(store, identity)
    _snapshot(store, identity)
    before = _sync(repository, store, [identity])
    rows = store.read_daily_verifications()
    restarted = FundEstimateStore(store.path)
    restarted.initialize()
    after = _sync(repository, restarted, [identity], NOW + timedelta(minutes=10))
    assert restarted.read_daily_verifications() == rows
    assert after["confirmed_nav_version"] == before["confirmed_nav_version"]
    assert restarted.count_rows("daily_verifications") == 1
    assert restarted.count_rows("estimate_snapshots") == 1


def test_no_estimate_keeps_latest_actual_only_without_importing_years_of_history(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, "2020-01-02", "0.8")
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity)
    _coverage(store, identity)
    _sync(repository, store, [identity])
    rows = store.read_daily_verifications()
    assert len(rows) == 1
    assert rows[0]["verification_status"] == "missing_estimate"
    assert rows[0]["predicted_pct"] is None
    assert rows[0]["direction_ok"] is None
    assert Decimal(rows[0]["realized_pct"]) == Decimal("0.1")
    assert rows[0]["snapshot_generated_at"] is None


def test_morning_verification_stops_polling_after_temporary_dividend_failure(books):
    from src.fund_estimate_verification import read_official_nav_state, verification_refresh_state
    repository, store, identities = books
    for identity in identities:
        _quote(repository, identity, PREVIOUS, '1')
        _quote(repository, identity)
        _coverage(store, identity)
        _snapshot(store, identity)
    store.save_nav(NavObservation(identities[0].code, DAY, '1.001', None,
        '2026-10-28T01:00:00+08:00', 'eastmoney_fund', 'temporary-fetch-failure', 'unknown', 'missing'))
    morning = datetime(2026, 10, 28, 10, 0, tzinfo=TZ_CN)
    _sync(repository, store, identities, morning)
    rows = store.read_daily_verifications()
    assert len(rows) == 2
    assert all(row['verification_status'] == 'verified' for row in rows)
    state = read_official_nav_state(repository, identities, morning)
    refresh = verification_refresh_state(identities, state, rows, morning,
        is_trading_day=lambda day: day.weekday() < 5, analysis_status='ready', status='ready')
    assert refresh['refresh_complete'] is True
    assert refresh['verified_fund_count'] == 2
    assert refresh['next_refresh_at'] == '2026-10-28T17:00:00+08:00'
    arguments = dict(is_trading_day=lambda day: day.weekday() < 5, analysis_status='ready', status='ready')
    before_close = verification_refresh_state(identities, state, rows,
        datetime(2026, 10, 28, 16, 59, tzinfo=TZ_CN), **arguments)
    assert before_close['refresh_complete'] is True
    assert before_close['next_refresh_at'] == '2026-10-28T17:00:00+08:00'
    resumed = verification_refresh_state(identities, state, rows,
        datetime(2026, 10, 28, 17, 0, tzinfo=TZ_CN), **arguments)
    assert resumed['refresh_complete'] is False


def test_unknown_dividend_preserves_nav_change_but_never_becomes_zero(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity)
    _snapshot(store, identity)
    _coverage(store, identity, dividend=None, coverage="missing")
    _sync(repository, store, [identity])
    row = store.read_daily_verifications()[0]
    assert row["verification_status"] == "pending_return"
    assert Decimal(row["nav_return_pct"]) == Decimal("0.1")
    assert row["unit_nav"] == "1.001"
    assert row["realized_pct"] is None
    assert row["error_pct"] is None
    assert row["direction_ok"] is None
    _coverage(store, identity)
    _sync(repository, store, [identity])
    assert store.read_daily_verifications()[0]["verification_status"] == "verified"


def test_estimate_nav_is_not_a_confirmation_source_and_mismatched_coverage_is_unknown(books):
    repository, store, identities = books
    identity = identities[0]
    _snapshot(store, identity)
    _coverage(store, identity, nav="1.010")
    empty = _sync(repository, store, [identity])
    assert empty["today_confirmed_count"] == 0
    assert store.read_daily_verifications() == []
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity)
    _sync(repository, store, [identity])
    assert store.read_daily_verifications()[0]["verification_status"] == "pending_return"


def test_last_valid_saved_estimate_is_selected_and_later_invalid_snapshot_is_ignored(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity)
    _coverage(store, identity)
    _snapshot(store, identity, predicted="0.02", hour=10)
    _snapshot(store, identity, predicted="0.09", hour=15)
    _snapshot(store, identity, predicted=None, hour=16, status="pending")
    _sync(repository, store, [identity])
    row = store.read_daily_verifications()[0]
    assert row["predicted_pct"] == "0.09"
    assert row["snapshot_generated_at"] == f"{DAY}T15:00:00+08:00"
    assert Decimal(row["error_pct"]) == Decimal("-0.01")


def test_previous_official_nav_revision_recalculates_successor_and_preserves_prediction(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity)
    _coverage(store, identity)
    _snapshot(store, identity)
    before = _sync(repository, store, [identity])
    initial = store.read_daily_verifications()[0]
    _quote(repository, identity, PREVIOUS, "1.002", version="revised")
    after = _sync(repository, store, [identity], NOW + timedelta(minutes=1))
    revised = store.read_daily_verifications()[0]
    assert Decimal(revised["realized_pct"]) == pytest.approx(Decimal("-0.0998"), abs=Decimal("0.0001"))
    assert revised["predicted_pct"] == initial["predicted_pct"]
    assert revised["snapshot_base_nav"] == "1"
    assert revised["snapshot_base_nav_date"] == PREVIOUS
    assert revised["base_nav"] == "1.002"
    assert revised["direction_ok"] is False
    assert revised["source_version"] != initial["source_version"]
    assert after["confirmed_nav_version"] != before["confirmed_nav_version"]


def test_dividend_cash_recovery_changes_total_return_and_source_version(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity, nav="0.99")
    _snapshot(store, identity)
    _sync(repository, store, [identity])
    before = store.read_daily_verifications()[0]
    _coverage(store, identity, nav="0.99", dividend="0.02", coverage="verified_cash")
    _sync(repository, store, [identity])
    recovered = store.read_daily_verifications()[0]
    assert recovered["verification_status"] == "verified"
    assert Decimal(recovered["nav_return_pct"]) == -1
    assert Decimal(recovered["realized_pct"]) == 1
    assert recovered["source_version"] != before["source_version"]


def test_one_product_failure_does_not_hide_another_confirmed_product(books, monkeypatch):
    repository, store, identities = books
    for identity in identities:
        _quote(repository, identity)
    original = repository.list_quotes
    def read_quotes(product_id, **kwargs):
        if product_id == identities[0].product_id:
            raise RuntimeError("one broken product")
        return original(product_id, **kwargs)
    monkeypatch.setattr(repository, "list_quotes", read_quotes)
    metadata = _sync(repository, store, identities)
    assert metadata["today_confirmed_count"] == 1
    assert metadata["errors"][0]["product_id"] == identities[0].product_id
    assert store.read_daily_verifications()[0]["product_id"] == identities[1].product_id


def test_next_day_without_new_quotes_keeps_previous_valid_nav_date(books):
    repository, store, identities = books
    _quote(repository, identities[0])
    _sync(repository, store, identities)
    metadata = _sync(repository, store, identities, NOW + timedelta(days=1))
    assert metadata["today_confirmed_count"] == 0
    assert metadata["latest_available_date"] == DAY


def test_backfill_starts_at_first_snapshot_and_then_preserves_actual_only_days(books):
    repository, store, identities = books
    identity = identities[0]
    for day in ("2020-01-02", "2026-10-23", PREVIOUS, DAY):
        _quote(repository, identity, day)
        _coverage(store, identity, day)
    _snapshot(store, identity, day=PREVIOUS)
    _sync(repository, store, [identity])
    rows = store.read_daily_verifications()
    assert [row["trade_date"] for row in rows] == [PREVIOUS, DAY]
    assert rows[-1]["verification_status"] == "missing_estimate"


def test_official_dividend_event_is_local_evidence_but_empty_cache_does_not_imply_zero(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity, nav="0.99")
    _snapshot(store, identity)
    with repository.database.transaction() as conn:
        conn.execute("INSERT INTO fund_dividend_state(product_id,enabled_date) VALUES(?,?)", (identity.product_id, PREVIOUS))
    _sync(repository, store, [identity])
    assert store.read_daily_verifications()[0]["realized_pct"] is None
    with repository.database.transaction() as conn:
        conn.execute("""INSERT INTO fund_dividend_events(id,product_id,record_date,ex_dividend_date,payment_date,
            per_share,source_url) VALUES(?,?,?,?,?,?,?)""", ("div-1", identity.product_id, PREVIOUS, DAY,
            "2026-10-28", "0.02", "https://fund.example/dividend"))
    _sync(repository, store, [identity])
    assert Decimal(store.read_daily_verifications()[0]["realized_pct"]) == 1


def test_old_minute_verification_sets_backfill_boundary_and_is_preserved(books):
    repository, store, identities = books
    identity = identities[0]
    for day in ("2020-01-02", "2026-10-23", PREVIOUS, DAY):
        _quote(repository, identity, day)
    store.save_verification(product_id=identity.product_id, code=identity.code,
        trade_date=PREVIOUS, snapshot_generated_at=f"{PREVIOUS}T15:00:00+08:00",
        realized_return_pct="0", recorded_at=NOW.isoformat())
    _sync(repository, store, [identity])
    assert [row["trade_date"] for row in store.read_daily_verifications()] == [PREVIOUS, DAY]
    assert store.count_rows("verifications") == 1


def test_equivalent_official_refetch_does_not_change_economic_versions(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1.0000")
    _quote(repository, identity, nav="1.0010")
    _coverage(store, identity)
    _snapshot(store, identity)
    first = _sync(repository, store, [identity])
    rows = store.read_daily_verifications()
    _quote(repository, identity, nav="1.001", version="different-raw-hash")
    second = _sync(repository, store, [identity], NOW + timedelta(minutes=2))
    assert first["confirmed_nav_version"] == second["confirmed_nav_version"]
    assert store.read_daily_verifications() == rows


def test_revision_of_current_nav_requires_matching_dividend_coverage_again(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity)
    _coverage(store, identity)
    _snapshot(store, identity)
    _sync(repository, store, [identity])
    assert store.read_daily_verifications()[0]["verification_status"] == "verified"
    _quote(repository, identity, nav="1.002", version="current-revised")
    _sync(repository, store, [identity])
    revised = store.read_daily_verifications()[0]
    assert Decimal(revised["nav_return_pct"]) == Decimal("0.2")
    assert revised["verification_status"] == "pending_return"
    assert revised["realized_pct"] is None


def test_later_official_dividend_disclosure_replaces_old_verified_zero_observation(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity, nav="0.99")
    _coverage(store, identity, nav="0.99")
    _snapshot(store, identity)
    _sync(repository, store, [identity])
    initial = store.read_daily_verifications()[0]
    assert Decimal(initial["realized_pct"]) == -1
    _cached_event(repository, identity)
    _sync(repository, store, [identity], NOW + timedelta(minutes=1))
    revised = store.read_daily_verifications()[0]
    assert Decimal(revised["realized_pct"]) == 1
    assert revised["cash_dividend_per_share"] == "0.02"
    assert revised["source_version"] != initial["source_version"]
    assert revised["predicted_pct"] == initial["predicted_pct"]


def test_official_dividend_amount_revision_recomputes_old_comparison(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity, nav="0.99")
    _coverage(store, identity, nav="0.99", dividend="0.02", coverage="verified_cash")
    _snapshot(store, identity)
    _cached_event(repository, identity)
    _sync(repository, store, [identity])
    initial = store.read_daily_verifications()[0]
    with repository.database.transaction() as conn:
        conn.execute("UPDATE fund_dividend_events SET per_share='0.03' WHERE product_id=?",
                     (identity.product_id,))
    _sync(repository, store, [identity], NOW + timedelta(minutes=1))
    revised = store.read_daily_verifications()[0]
    assert Decimal(revised["realized_pct"]) == 2
    assert revised["source_version"] != initial["source_version"]


@pytest.mark.parametrize("conflict", ["event_error", "state_error", "duplicate_event"])
def test_official_dividend_conflict_withdraws_previously_verified_comparison(books, conflict):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity, nav="0.99")
    _coverage(store, identity, nav="0.99", dividend="0.02", coverage="verified_cash")
    _snapshot(store, identity)
    _cached_event(repository, identity)
    _sync(repository, store, [identity])
    initial = store.read_daily_verifications()[0]
    assert initial["verification_status"] == "verified"
    if conflict == "duplicate_event":
        _cached_event(repository, identity, amount="0.03", event_id="contradicting-event")
    else:
        table = "fund_dividend_events" if conflict == "event_error" else "fund_dividend_state"
        with repository.database.transaction() as conn:
            conn.execute(f"UPDATE {table} SET error='公开来源分红方案冲突，需核实' WHERE product_id=?",
                         (identity.product_id,))
    _sync(repository, store, [identity], NOW + timedelta(minutes=1))
    revised = store.read_daily_verifications()[0]
    assert revised["verification_status"] == "pending_return"
    assert revised["realized_pct"] is None
    assert revised["error_pct"] is None
    assert revised["direction_ok"] is None
    assert revised["source_version"] != initial["source_version"]
    assert Decimal(revised["nav_return_pct"]) == -1


def test_public_official_nav_version_uses_same_economic_content_as_sync(books):
    repository, store, identities = books
    module = importlib.import_module("src.fund_estimate_verification")
    assert hasattr(module, "official_nav_version"), "缺少共享正式净值版本接口"
    first, second = identities
    _quote(repository, first, PREVIOUS, "1")
    _quote(repository, first)
    _quote(repository, second, PREVIOUS, "2")
    before = _sync(repository, store, identities)
    assert module.official_nav_version(repository, list(reversed(identities)), NOW) == before["confirmed_nav_version"]
    _quote(repository, first, nav="1.0010", version="refetched")
    assert module.official_nav_version(repository, identities, NOW) == before["confirmed_nav_version"]
    _quote(repository, first, PREVIOUS, "1.002", version="revised")
    after = _sync(repository, store, identities)
    assert module.official_nav_version(repository, identities, NOW) == after["confirmed_nav_version"]
    assert after["confirmed_nav_version"] != before["confirmed_nav_version"]
    _quote(repository, first, "2026-10-28", "1.003")
    assert module.official_nav_version(repository, identities, NOW) == after["confirmed_nav_version"]


def test_inactive_fund_revises_existing_days_without_creating_new_post_exit_days(books):
    repository, store, identities = books
    identity = identities[0]
    _quote(repository, identity, PREVIOUS, "1")
    _quote(repository, identity)
    _coverage(store, identity)
    _snapshot(store, identity)
    _sync(repository, store, [identity])
    _quote(repository, identity, PREVIOUS, "1.002", version="revised-after-exit")
    _quote(repository, identity, "2026-10-28", "1.005")
    metadata = _sync(repository, store, [identity], NOW + timedelta(days=1), active_product_ids=set())
    rows = store.read_daily_verifications()
    assert [row["trade_date"] for row in rows] == [DAY]
    assert Decimal(rows[0]["realized_pct"]) == pytest.approx(Decimal("-0.0998"), abs=Decimal("0.0001"))
    assert metadata["today_confirmed_count"] == 0
    assert metadata["latest_available_date"] == DAY


def test_public_official_nav_state_reports_real_confirmations_before_daily_sync(books, monkeypatch):
    repository, store, identities = books
    module = importlib.import_module("src.fund_estimate_verification")
    assert hasattr(module, "read_official_nav_state"), "缺少正式净值首屏状态接口"
    empty = module.read_official_nav_state(repository, identities, NOW)
    assert empty["today_confirmed_count"] == 0
    assert empty["latest_confirmed_date"] is None
    assert empty["errors"] == []
    first, second = identities
    _quote(repository, first, PREVIOUS, "1")
    _quote(repository, first, nav="1")
    _quote(repository, second, PREVIOUS, "2")
    state = module.read_official_nav_state(repository, identities, NOW)
    assert store.read_daily_verifications() == []
    assert state["today_confirmed_count"] == 1
    assert state["latest_confirmed_date"] == DAY
    assert state["confirmed_nav_version"] == module.official_nav_version(repository, identities, NOW)
    assert state["confirmed_nav_version"] == _sync(repository, store, identities)["confirmed_nav_version"]
    _quote(repository, second, nav="2.001")
    assert module.read_official_nav_state(repository, identities, NOW)["today_confirmed_count"] == 2
    tomorrow = module.read_official_nav_state(repository, identities, NOW + timedelta(days=1))
    assert tomorrow["today_confirmed_count"] == 0
    assert tomorrow["latest_confirmed_date"] == DAY
    original = repository.list_quotes
    def read_quotes(product_id, **kwargs):
        if product_id == first.product_id:
            raise RuntimeError("broken official quote read")
        return original(product_id, **kwargs)
    monkeypatch.setattr(repository, "list_quotes", read_quotes)
    failed = module.read_official_nav_state(repository, identities, NOW)
    metadata = _sync(repository, store, identities)
    assert failed["today_confirmed_count"] == 1
    assert failed["errors"][0]["product_id"] == first.product_id
    assert failed["confirmed_nav_version"] == metadata["confirmed_nav_version"]
