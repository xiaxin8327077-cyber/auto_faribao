from datetime import date, datetime
from decimal import Decimal
import json

from src.beijing_time import TZ_CN
from src.bond_barometer import ensure_barometer_schema, write_snapshot
from src.fund_estimate_models import train_models, walk_forward_validate, build_training_set
from src.fund_estimate_profiles import FundDocument, MemoryDocuments, ProfileService
from src.fund_estimate_service import FundEstimateService, discover_funds
from src.fund_estimate_sources import EstimateSources
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import FACTOR_NAMES, FactorObservation, FundIdentity, NavObservation
from src.portfolio_db import SCHEMA_VERSION, PortfolioDatabase
from src.portfolio_models import Position, Product, ProductType, Transaction, TransactionStatus, TransactionType
from src.portfolio_repository import PortfolioRepository


def test_new_fund_cold_start_to_prediction_without_code_edit(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    before = _dump(database)
    repository.add_product(Product(
        "pending-new", "eastmoney_fund", "918273", "待确认纯债A", ProductType.PUBLIC_FUND,
        metadata_json='{"fund_type":"债券型"}',
    ))
    repository.create_transaction(Transaction(
        "buy", "pending-new", TransactionType.MANUAL_PURCHASE, TransactionStatus.PENDING_CONFIRMATION,
        date(2026, 10, 23), "buy-918273",
    ))
    repository.add_product(Product(
        "held-new", "eastmoney_fund", "564738", "持有中短债C", ProductType.PUBLIC_FUND,
        metadata_json='{"fund_type":"中短债"}',
    ))
    repository.replace_position(Position("held-new", Decimal("8"), Decimal("0"), Decimal("8"), Decimal("8")))
    seeded = _dump(database)
    codes = {item.code for item in discover_funds(repository, datetime(2026, 10, 23, tzinfo=TZ_CN))}
    assert codes == {"918273", "564738"}

    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    identity = next(item for item in discover_funds(repository) if item.code == "564738")
    _seed_history(store, identity.code)
    text = "公告日期：2026-08-28\n报告期：2026-06-30\n净值口径久期：2.200年\n份额C\n管理费率：0.30%/年\n费率生效日期：2020-01-01\n"
    profiles = ProfileService(store, MemoryDocuments([
        FundDocument("https://example.test/564738", "中报", "2026-08-28T00:00:00+08:00", "2026-06-30", text, "C")
    ]))
    now = datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN)
    profiles.refresh(identity, now)
    trained = train_models(identity, store, now, {"min_fit_samples": 60, "enable_ready": False, "ridge_alpha": 1.0})
    assert trained["trained"] is True
    assert trained["validation"]["ready"] is False
    navs = store.nav_as_of(identity.code, "2026-10-23T12:00:00+08:00")
    base = navs[-1]
    values = {name: "0" for name in FACTOR_NAMES[:4]}
    values["gov_3y_bp"] = "1"
    live = FactorObservation(
        "bond_factor_v1", "daily_close", "2026-10-23", "2026-10-23T10:00:00+08:00",
        "2026-10-23T10:00:00+08:00", "2026-10-23T10:00:00+08:00",
        values, 1, "native", [], base.nav_date, "live",
    )
    from src.fund_estimate_models import project_snapshot

    shown = project_snapshot(identity, store, live, base, now, {"min_fit_samples": 60, "enable_ready": False}, {"note": "同类市场参考"})
    assert shown.status == "experimental"
    assert shown.return_pct is not None
    assert shown.status != "ready"
    assert _dump(database) == seeded
    assert SCHEMA_VERSION == 7


def test_barometer_retention_does_not_delete_estimate_history(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    store.set_watermark("training:old", "keep", "2020-01-01T00:00:00+08:00")
    with database.connection() as conn:
        ensure_barometer_schema(conn)
        write_snapshot(conn, {"available": True, "as_of": "2026-10-23"}, datetime(2026, 10, 23, 10, 0))
    assert store.get_watermark("training:old") == "keep"


def test_validation_report_does_not_invent_intraday_accuracy(tmp_path):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    identity = FundIdentity("held-new", "564738", "eastmoney_fund", "C", None, "中短债")
    _seed_history(store, identity.code)
    training = build_training_set(identity, "2026-10-23T12:00:00+08:00", store)
    report = walk_forward_validate(training, {"min_fit_samples": 60, "training_windows": (60,)})
    assert report["intraday_sample_count"] == 0
    assert report["ready"] is False
    assert report["oos_count"] >= 1
    assert report["mae_bp"] is not None


def _seed_history(store, code):
    previous_day = date(2026, 1, 1)
    previous_nav = "1.0000"
    store.save_nav(NavObservation(code, previous_day.isoformat(), previous_nav, "0", "2026-01-01T20:00:00+08:00", "fixture", "v0", "ordinary", "verified_none"))
    value = Decimal("1.0000")
    for offset in range(1, 81):
        current = date(2026, 1, 1).fromordinal(previous_day.toordinal() + 1)
        value += Decimal("0.0001")
        gov = "1" if offset % 2 else "-1"
        store.save_nav(NavObservation(
            code, current.isoformat(), format(value, "f"), "0",
            f"{current.isoformat()}T20:00:00+08:00", "fixture", f"v{offset}", "ordinary", "verified_none",
        ))
        values = {name: "0" for name in FACTOR_NAMES[:4]}
        values["gov_3y_bp"] = gov
        store.save_factor(FactorObservation(
            "bond_factor_v1", "daily_close", current.isoformat(), f"{current.isoformat()}T20:00:00+08:00",
            f"{current.isoformat()}T20:00:00+08:00", f"{current.isoformat()}T20:00:00+08:00",
            values, 1, "native", [], previous_day.isoformat(), f"daily-{offset}",
        ))
        previous_day = current


def _dump(database):
    with database.connection() as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")]
        return {
            name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")]
            for name in tables
            if name != "bond_barometer_snapshots"
        }
