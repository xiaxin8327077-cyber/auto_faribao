from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3

import pytest

import src.portfolio_migration as portfolio_migration
from src.config import Config
from src.portfolio_db import PortfolioDatabase
from src.portfolio_migration import migrate_legacy_portfolio


def legacy_cfg():
    return Config({"nav_monitor": {"products": [
        {
            "provider": "nanyin_wealth",
            "code": "NYRR000007",
            "name": "日日聚宝",
            "shares": 10936.10,
        },
        {
            "provider": "citic_wealth",
            "code": "AF233276B",
            "name": "净值理财",
            "shares": 2000,
        },
    ]}})


def test_migration_dry_run_does_not_install_database(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "profit_entries": [{
            "nav_date": "2026-07-29",
            "amount": "12.34",
            "discovered_at": "2026-07-30T08:00:00",
        }],
        "manual_daily_profits": {
            "2026-07-28": {
                "amount": "9.87",
                "updated_at": "2026-07-30T09:00:00",
            },
        },
        "manual_period_profits": [{
            "period": "2026-06",
            "amount": "88.01",
            "updated_at": "2026-07-01T09:00:00",
        }],
    }), encoding="utf-8")
    target = tmp_path / "portfolio.db"

    report = migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=False
    )

    assert report.products == 2
    assert report.opening_positions == 2
    assert report.profit_rows == 3
    assert report.installed is False
    assert not target.exists()
    assert not list(tmp_path.glob("portfolio.db.migrating-*"))


def test_install_is_repeat_safe_and_preserves_original_inputs(tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"

    first = migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )
    second = migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )

    assert first.installed is True
    assert second.installed is False
    assert state.exists()
    with PortfolioDatabase(target).connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_install_upgrades_v1_target_in_place_and_preserves_portfolio_data(
    tmp_path,
    monkeypatch,
):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )
    with PortfolioDatabase(target).connection() as conn:
        transaction_ids = {
            row["id"] for row in conn.execute("SELECT id FROM transactions")
        }
        conn.execute("DELETE FROM schema_migrations")
        conn.execute("INSERT INTO schema_migrations(version) VALUES (1)")

    monkeypatch.setattr(
        portfolio_migration.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("v1 target must not be replaced")
        ),
    )

    report = migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )

    assert report.installed is False
    with PortfolioDatabase(target).connection() as conn:
        assert {
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )
        } == {2}
        assert {
            row["id"] for row in conn.execute("SELECT id FROM transactions")
        } == transaction_ids


@pytest.mark.parametrize("versions", [{3}, {1, 2}])
def test_install_rejects_unsupported_portfolio_versions_without_changes(
    tmp_path,
    versions,
):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )
    with PortfolioDatabase(target).connection() as conn:
        conn.execute("DELETE FROM schema_migrations")
        for version in versions:
            conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
    before_bytes = target.read_bytes()
    with sqlite3.connect(target) as conn:
        before_rows = conn.execute(
            "SELECT id, idempotency_key FROM transactions ORDER BY id"
        ).fetchall()

    with pytest.raises(
        ValueError,
        match="unsupported portfolio schema version",
    ):
        migrate_legacy_portfolio(
            legacy_cfg(),
            state,
            target,
            tmp_path / "backups",
            install=True,
        )

    assert target.read_bytes() == before_bytes
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT id, idempotency_key FROM transactions ORDER BY id"
        ).fetchall() == before_rows
    assert not list(tmp_path.glob("portfolio.db.migrating-*"))


def test_install_rejects_malformed_portfolio_migration_table_without_replace(
    tmp_path,
):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    with sqlite3.connect(target) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations (unexpected_column INTEGER)"
        )
        conn.execute(
            """CREATE TABLE products (
                   id TEXT PRIMARY KEY,
                   name TEXT NOT NULL
               )"""
        )
        conn.execute(
            "INSERT INTO schema_migrations VALUES (99)"
        )
        conn.execute(
            "INSERT INTO products VALUES ('sentinel', 'keep')"
        )
    before_bytes = target.read_bytes()

    with pytest.raises(
        ValueError,
        match="unsupported portfolio schema version",
    ):
        migrate_legacy_portfolio(
            legacy_cfg(),
            state,
            target,
            tmp_path / "backups",
            install=True,
        )

    assert target.read_bytes() == before_bytes
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT * FROM products"
        ).fetchall() == [("sentinel", "keep")]


def test_failed_validation_never_replaces_target(tmp_path, monkeypatch):
    target = tmp_path / "portfolio.db"
    target.write_bytes(b"existing")
    monkeypatch.setattr(
        "src.portfolio_migration._validate_candidate",
        lambda *_args: (_ for _ in ()).throw(ValueError("mismatch")),
    )

    with pytest.raises(ValueError, match="mismatch"):
        migrate_legacy_portfolio(
            legacy_cfg(),
            tmp_path / "missing.json",
            target,
            tmp_path / "backups",
            install=True,
        )

    assert target.read_bytes() == b"existing"
    assert not list(tmp_path.glob("portfolio.db.migrating-*"))


def test_install_preserves_exact_products_shares_profits_and_backups(tmp_path):
    cfg = Config({"nav_monitor": {"products": [
        {
            "provider": "nanyin_wealth",
            "code": "NYRR000007",
            "name": "日日聚宝",
            "shares": "10936.10",
        },
        {
            "provider": "citic_wealth",
            "code": "AM264381F",
            "name": "天天利",
            "shares": "234.50",
        },
        {
            "provider": "citic_wealth",
            "code": "AF233276B",
            "name": "净值理财",
            "shares": "2000",
        },
    ]}})
    automatic = {
        "nav_date": "2026-07-29",
        "amount": "12.3400",
        "discovered_at": "2026-07-30T08:00:00",
    }
    manual_daily = {
        "amount": "9.870",
        "updated_at": "2026-07-30T09:00:00",
    }
    manual_period = {
        "period": "2026-06",
        "amount": "88.010",
        "updated_at": "2026-07-01T09:00:00",
    }
    state_data = {
        "profit_entries": [automatic],
        "manual_daily_profits": {"2026-07-28": manual_daily},
        "manual_period_profits": [manual_period],
    }
    state = tmp_path / "state.json"
    state_bytes = json.dumps(
        state_data,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    state.write_bytes(state_bytes)
    target = tmp_path / "portfolio.db"
    old_target_bytes = b"legacy target bytes"
    target.write_bytes(old_target_bytes)

    report = migrate_legacy_portfolio(
        cfg, state, target, tmp_path / "backups", install=True
    )

    assert report.products == 3
    assert report.opening_positions == 3
    assert report.profit_rows == 3
    assert report.installed is True
    backup_dir = Path(report.backup_dir)
    assert backup_dir.parent == tmp_path / "backups"
    assert (
        len(backup_dir.name) == 15
        and backup_dir.name[8] == "-"
        and backup_dir.name.replace("-", "").isdigit()
    )
    assert (backup_dir / state.name).read_bytes() == state_bytes
    assert (backup_dir / target.name).read_bytes() == old_target_bytes
    assert state.read_bytes() == state_bytes
    assert not list(tmp_path.glob("portfolio.db.migrating-*"))

    with sqlite3.connect(target) as conn:
        conn.row_factory = sqlite3.Row
        product_types = {
            row["code"]: row["product_type"]
            for row in conn.execute(
                "SELECT code, product_type FROM products"
            )
        }
        shares = {
            row["code"]: Decimal(row["shares"])
            for row in conn.execute(
                """SELECT products.code, transactions.shares
                   FROM transactions
                   JOIN products ON products.id = transactions.product_id
                   WHERE transactions.transaction_type = 'opening_position'"""
            )
        }
        profits = {
            row["source_kind"]: (
                row["profit_date"],
                Decimal(row["amount"]),
                row["source_json"],
            )
            for row in conn.execute(
                """SELECT profit_date, amount, source_kind, source_json
                   FROM legacy_profit_history"""
            )
        }

    assert product_types == {
        "NYRR000007": "cash_management",
        "AM264381F": "cash_management",
        "AF233276B": "wealth_nav",
    }
    assert shares == {
        "NYRR000007": Decimal("10936.10"),
        "AM264381F": Decimal("234.50"),
        "AF233276B": Decimal("2000"),
    }
    assert profits == {
        "automatic": (
            "2026-07-29",
            Decimal("12.3400"),
            json.dumps(automatic, ensure_ascii=False, separators=(",", ":")),
        ),
        "manual_daily": (
            "2026-07-28",
            Decimal("9.870"),
            json.dumps(manual_daily, ensure_ascii=False, separators=(",", ":")),
        ),
        "manual_period": (
            "2026-06",
            Decimal("88.010"),
            json.dumps(manual_period, ensure_ascii=False, separators=(",", ":")),
        ),
    }


def test_existing_schema_validation_does_not_construct_portfolio_database(
    tmp_path, monkeypatch
):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )

    def fail_if_constructed(*_args, **_kwargs):
        raise AssertionError("existing target validation must be read-only")

    monkeypatch.setattr(
        portfolio_migration,
        "PortfolioDatabase",
        fail_if_constructed,
    )

    report = migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )

    assert report.installed is False


def test_current_schema_detection_requires_exact_version_set(tmp_path):
    target = tmp_path / "portfolio.db"
    database = PortfolioDatabase(target)
    database.initialize()
    assert portfolio_migration._contains_current_schema(target) is True

    with database.connection() as conn:
        conn.execute(
            "INSERT INTO schema_migrations(version) VALUES (1)"
        )

    assert portfolio_migration._contains_current_schema(target) is False


def test_failed_existing_schema_validation_preserves_database_and_sidecars(
    tmp_path
):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )
    with sqlite3.connect(target) as conn:
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"

    wal_path = target.with_name(f"{target.name}-wal")
    shm_path = target.with_name(f"{target.name}-shm")
    wal_path.write_bytes(b"pre-existing wal sentinel")
    shm_path.write_bytes(b"pre-existing shm sentinel")
    fixed_ns = 1_700_000_000_000_000_000
    for path in (target, wal_path, shm_path):
        os.utime(path, ns=(fixed_ns, fixed_ns))
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (target, wal_path, shm_path)
    }
    before_names = {path.name for path in tmp_path.iterdir()}
    changed_cfg = legacy_cfg()
    changed_cfg.nav_monitor.products[0].shares = "1"

    with pytest.raises(
        ValueError,
        match="opening-position share totals mismatch",
    ):
        migrate_legacy_portfolio(
            changed_cfg,
            state,
            target,
            tmp_path / "backups",
            install=True,
        )

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (target, wal_path, shm_path)
    }
    assert after == before
    assert {path.name for path in tmp_path.iterdir()} == before_names
    assert not list(tmp_path.glob("portfolio.db.migrating-*"))
