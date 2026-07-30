import json

import pytest

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
        "profit_entries": [{"nav_date": "2026-07-29", "profit": "12.34"}],
        "manual_daily_profits": {"2026-07-28": "9.87"},
    }), encoding="utf-8")
    target = tmp_path / "portfolio.db"

    report = migrate_legacy_portfolio(
        legacy_cfg(), state, target, tmp_path / "backups", install=False
    )

    assert report.products == 2
    assert report.opening_positions == 2
    assert report.profit_rows == 2
    assert report.installed is False
    assert not target.exists()


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
