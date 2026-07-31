import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from src.config import Config
from src.portfolio_db import (
    BASE_SCHEMA_SQL,
    V2_SCHEMA_SQL,
    PortfolioDatabase,
    SCHEMA_VERSION,
)
from src.portfolio_models import (
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository
from src.portfolio_runtime import (
    PortfolioRuntime,
    get_portfolio_runtime,
    initialize_portfolio,
    validate_existing_database,
)


def _seed_v2_database(db_path):
    database = PortfolioDatabase(db_path)
    database.initialize()
    return database


def test_first_start_migrates_then_exposes_repository(tmp_path):
    cfg = Config({"nav_monitor": {"products": [
        {"provider": "citic_wealth", "code": "AF233276B", "name": "产品", "shares": 1000},
    ]}})
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")

    runtime = initialize_portfolio(
        cfg,
        state_path=state,
        db_path=tmp_path / "portfolio.db",
        backup_root=tmp_path / "backups",
    )

    assert runtime.write_enabled is True
    assert len(runtime.repository.list_products()) == 1
    assert runtime.repository.get_position(
        runtime.repository.list_products()[0].id
    ).total_shares == Decimal("1000")
    assert get_portfolio_runtime() is runtime


def test_migration_failure_returns_read_only_runtime_and_preserves_legacy(
    tmp_path, monkeypatch,
):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    monkeypatch.setattr(
        "src.portfolio_runtime.migrate_legacy_portfolio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad migration")),
    )
    runtime = initialize_portfolio(
        Config({"nav_monitor": {"products": []}}),
        state_path=state,
        db_path=tmp_path / "portfolio.db",
        backup_root=tmp_path / "backups",
    )
    assert runtime.write_enabled is False
    assert runtime.migration_error == "bad migration"
    assert state.exists()
    assert not (tmp_path / "portfolio.db").exists()


def test_incompatible_existing_schema_disables_writes_without_reset(tmp_path):
    db_path = tmp_path / "portfolio.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) "
            "VALUES (99, CURRENT_TIMESTAMP)"
        )
    runtime = initialize_portfolio(
        Config({"nav_monitor": {"products": []}}),
        db_path=db_path,
        backup_root=tmp_path / "backups",
    )
    assert runtime.write_enabled is False
    assert "schema version" in runtime.migration_error
    assert "99" in runtime.migration_error
    # 数据库未被重置：schema_migrations 仍包含 version 99，未添加任何业务表
    with sqlite3.connect(db_path) as conn:
        versions = {
            row[0] for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )
        }
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert versions == {99}
    assert tables == {"schema_migrations"}


def test_rebuild_runs_after_migration(tmp_path):
    cfg = Config({"nav_monitor": {"products": [
        {"provider": "nanyin_wealth", "code": "NYRR000007", "name": "日日聚宝", "shares": 5000},
    ]}})
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")

    runtime = initialize_portfolio(
        cfg,
        state_path=state,
        db_path=tmp_path / "portfolio.db",
        backup_root=tmp_path / "backups",
    )

    product = runtime.repository.list_products()[0]
    assert runtime.repository.get_position(product.id).total_shares == Decimal("5000")


def test_corrupt_database_disables_writes(tmp_path):
    db_path = tmp_path / "portfolio.db"
    _seed_v2_database(db_path)
    db_path.write_bytes(b"corrupt")

    runtime = initialize_portfolio(
        Config({"nav_monitor": {"products": []}}),
        db_path=db_path,
        backup_root=tmp_path / "backups",
    )
    assert runtime.write_enabled is False
    assert runtime.migration_error


def test_validate_existing_database_accepts_valid_database(tmp_path):
    db_path = tmp_path / "portfolio.db"
    _seed_v2_database(db_path)
    # 正常数据库不应抛出异常
    validate_existing_database(PortfolioDatabase(db_path))


def test_initialize_on_valid_database_loads_repository(tmp_path):
    db_path = tmp_path / "portfolio.db"
    database = _seed_v2_database(db_path)
    repository = PortfolioRepository(database)
    product_id = "p1"
    from src.portfolio_models import Product
    repository.add_product(Product(
        product_id, "changsheng_fund", "003103", "长盛盛裕纯债C",
        ProductType.PUBLIC_FUND,
    ))
    repository.create_transaction(Transaction(
        id="t1",
        product_id=product_id,
        transaction_type=TransactionType.OPENING_POSITION,
        status=TransactionStatus.CONFIRMED,
        trade_date=date(2026, 7, 30),
        idempotency_key="migration:opening:p1",
        shares=Decimal("100"),
    ))
    PositionProjector(repository).rebuild()

    runtime = initialize_portfolio(
        Config({"nav_monitor": {"products": []}}),
        db_path=db_path,
        backup_root=tmp_path / "backups",
    )

    assert runtime.write_enabled is True
    assert runtime.repository is not None
    assert len(runtime.repository.list_products()) == 1


def test_initialize_upgrades_existing_v2_database_before_validation(tmp_path):
    db_path = tmp_path / "portfolio.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(BASE_SCHEMA_SQL)
        conn.executescript(V2_SCHEMA_SQL)
        conn.execute("DELETE FROM schema_migrations")
        conn.execute(
            "INSERT INTO schema_migrations(version) VALUES (2)"
        )

    runtime = initialize_portfolio(
        Config({"nav_monitor": {"products": []}}),
        db_path=db_path,
        backup_root=tmp_path / "backups",
    )

    assert runtime.write_enabled is True
    with sqlite3.connect(db_path) as conn:
        versions = {
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )
        }
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(transactions)"
            )
        }
    assert versions == {SCHEMA_VERSION}
    assert "trade_time" in columns
