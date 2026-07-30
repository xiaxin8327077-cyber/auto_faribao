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


def file_snapshot(*paths):
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in paths
    }


def downgrade_target_to_v1(target):
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("DELETE FROM schema_migrations")
        conn.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        conn.commit()
    finally:
        conn.close()


def set_target_versions(target, versions):
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("DELETE FROM schema_migrations")
        for version in versions:
            conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
        conn.commit()
    finally:
        conn.close()


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
    downgrade_target_to_v1(target)

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


def test_v1_target_dry_run_validates_copy_without_modifying_target(tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )
    downgrade_target_to_v1(target)
    before_bytes = target.read_bytes()
    with sqlite3.connect(target) as conn:
        before_objects = conn.execute(
            """SELECT type, name, sql FROM sqlite_master
               ORDER BY type, name"""
        ).fetchall()
        before_rows = conn.execute(
            """SELECT id, idempotency_key FROM transactions
               ORDER BY id"""
        ).fetchall()

    report = migrate_legacy_portfolio(
        legacy_cfg(),
        state,
        target,
        tmp_path / "backups",
        install=False,
    )

    assert report.installed is False
    assert target.read_bytes() == before_bytes
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            """SELECT type, name, sql FROM sqlite_master
               ORDER BY type, name"""
        ).fetchall() == before_objects
        assert conn.execute(
            """SELECT id, idempotency_key FROM transactions
               ORDER BY id"""
        ).fetchall() == before_rows
        assert conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(1,)]
    assert not list(tmp_path.glob("portfolio.db.migrating-*"))


def test_v1_target_failed_copy_validation_never_modifies_target(tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )
    downgrade_target_to_v1(target)
    before_bytes = target.read_bytes()
    with sqlite3.connect(target) as conn:
        before_objects = conn.execute(
            """SELECT type, name, sql FROM sqlite_master
               ORDER BY type, name"""
        ).fetchall()
        before_rows = conn.execute(
            """SELECT id, idempotency_key FROM transactions
               ORDER BY id"""
        ).fetchall()
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

    assert target.read_bytes() == before_bytes
    with sqlite3.connect(target) as conn:
        assert conn.execute(
            """SELECT type, name, sql FROM sqlite_master
               ORDER BY type, name"""
        ).fetchall() == before_objects
        assert conn.execute(
            """SELECT id, idempotency_key FROM transactions
               ORDER BY id"""
        ).fetchall() == before_rows
        assert conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(1,)]
    assert not list(tmp_path.glob("portfolio.db.migrating-*"))


def test_v1_install_validates_all_legacy_state_before_locked_upgrade(
    tmp_path,
):
    state = tmp_path / "state.json"
    state.write_text(
        '{"profit_entries":[{"nav_date":"2026-07-30"}]}',
        encoding="utf-8",
    )
    target = tmp_path / "portfolio.db"
    valid_state = tmp_path / "valid-state.json"
    valid_state.write_text('{"profit_entries":[]}', encoding="utf-8")
    migrate_legacy_portfolio(
        legacy_cfg(),
        valid_state,
        target,
        tmp_path / "backups",
        install=True,
    )
    downgrade_target_to_v1(target)
    before_bytes = target.read_bytes()

    with pytest.raises(
        ValueError,
        match="automatic profit entry has no amount",
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
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(1,)]


def test_v1_install_validates_latest_target_inside_upgrade_lock(
    tmp_path,
    monkeypatch,
):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )
    downgrade_target_to_v1(target)
    original = PortfolioDatabase.upgrade_v1_validated

    def inject_latest_change(database, validator):
        conn = sqlite3.connect(database.path)
        try:
            conn.execute(
                """INSERT INTO products
                   (id, provider, code, name, product_type, status)
                   VALUES ('racer', 'test', 'racer', 'racer', 'wealth_nav',
                           'active')"""
            )
            conn.commit()
        finally:
            conn.close()
        return original(database, validator)

    monkeypatch.setattr(
        PortfolioDatabase,
        "upgrade_v1_validated",
        inject_latest_change,
    )

    with pytest.raises(
        ValueError,
        match="portfolio product count mismatch",
    ):
        migrate_legacy_portfolio(
            legacy_cfg(),
            state,
            target,
            tmp_path / "backups",
            install=True,
        )

    with sqlite3.connect(target) as conn:
        assert conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall() == [(1,)]
        assert conn.execute(
            "SELECT COUNT(*) FROM products WHERE id = 'racer'"
        ).fetchone()[0] == 1


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
    set_target_versions(target, versions)
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


@pytest.mark.parametrize("versions", [{1}, {3}, {1, 2}])
def test_migration_rejects_real_uncheckpointed_wal_without_touching_files(
    tmp_path,
    versions,
):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=True
    )
    wal_path = target.with_name(f"{target.name}-wal")
    shm_path = target.with_name(f"{target.name}-shm")

    conn = sqlite3.connect(target)
    try:
        assert conn.execute(
            "PRAGMA journal_mode = WAL"
        ).fetchone()[0].lower() == "wal"
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("DELETE FROM schema_migrations")
        for version in versions:
            conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
        conn.commit()
        assert wal_path.stat().st_size > 0
        assert shm_path.stat().st_size > 0
        before = file_snapshot(target, wal_path, shm_path)

        with pytest.raises(
            ValueError,
            match="active portfolio database sidecars",
        ):
            migrate_legacy_portfolio(
                legacy_cfg(),
                state,
                target,
                tmp_path / "backups",
                install=True,
            )

        assert file_snapshot(target, wal_path, shm_path) == before
        assert not list(tmp_path.glob("portfolio.db.migrating-*"))
    finally:
        conn.close()


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
def test_missing_main_with_nonempty_sidecar_is_unsafe_and_unchanged(
    tmp_path,
    sidecar_suffix,
):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    sidecar = Path(f"{target}{sidecar_suffix}")
    sidecar.write_bytes(b"orphan-sidecar")
    before = file_snapshot(sidecar)

    with pytest.raises(
        ValueError,
        match="active portfolio database sidecars",
    ):
        migrate_legacy_portfolio(
            legacy_cfg(),
            state,
            target,
            tmp_path / "backups",
            install=True,
        )

    assert not target.exists()
    assert file_snapshot(sidecar) == before


def test_missing_target_publish_never_overwrites_racing_file(
    tmp_path,
    monkeypatch,
):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    real_link = os.link

    def race_link(source, destination):
        if Path(destination) == target:
            target.write_bytes(b"racing-database")
        return real_link(source, destination)

    monkeypatch.setattr(portfolio_migration.os, "link", race_link)

    with pytest.raises(
        ValueError,
        match="target appeared during portfolio publication",
    ):
        migrate_legacy_portfolio(
            legacy_cfg(),
            state,
            target,
            tmp_path / "backups",
            install=True,
        )

    assert target.read_bytes() == b"racing-database"
    assert not list(tmp_path.glob("portfolio.db.migrating-*"))


@pytest.mark.parametrize(
    "checkpoint_result",
    [(1, 4, 3), (0, 4, 3)],
)
def test_candidate_stabilization_rejects_incomplete_checkpoint(
    checkpoint_result,
):
    with pytest.raises(
        RuntimeError,
        match="candidate WAL checkpoint did not complete",
    ):
        portfolio_migration._require_complete_checkpoint(
            checkpoint_result
        )


def test_candidate_stabilization_failure_cleans_candidate_without_publish(
    tmp_path,
    monkeypatch,
):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    target = tmp_path / "portfolio.db"
    monkeypatch.setattr(
        portfolio_migration,
        "_stabilize_candidate",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("forced stabilization failure")
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="forced stabilization failure",
    ):
        migrate_legacy_portfolio(
            legacy_cfg(),
            state,
            target,
            tmp_path / "backups",
            install=True,
        )

    assert not target.exists()
    assert not list(tmp_path.glob("portfolio.db.migrating-*"))


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
        match="active portfolio database sidecars",
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
