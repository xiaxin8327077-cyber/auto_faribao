import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.nav_dashboard import DEFAULT_STATE_PATH
from src.portfolio_db import DEFAULT_DB_PATH, PortfolioDatabase, SCHEMA_VERSION
from src.portfolio_migration import migrate_legacy_portfolio
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortfolioRuntime:
    database: Optional[PortfolioDatabase]
    repository: Optional[PortfolioRepository]
    write_enabled: bool
    migration_error: str = ""


_runtime: Optional[PortfolioRuntime] = None


def get_portfolio_runtime() -> Optional[PortfolioRuntime]:
    return _runtime


def validate_existing_database(
    database: PortfolioDatabase,
    expected_version: int = SCHEMA_VERSION,
) -> None:
    with database.connection() as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(
                f"database integrity check failed: {integrity}"
            )
        versions = {
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )
        }
    if versions != {expected_version}:
        raise ValueError(
            f"schema version {sorted(versions)} is not supported "
            f"(expected {expected_version})"
        )


def initialize_portfolio(
    cfg,
    state_path=DEFAULT_STATE_PATH,
    db_path=DEFAULT_DB_PATH,
    backup_root=None,
) -> PortfolioRuntime:
    global _runtime
    db_path = Path(db_path)
    state_path = Path(state_path)
    backup_root = Path(
        backup_root or db_path.parent / "portfolio-backups"
    )
    try:
        if not db_path.exists():
            migrate_legacy_portfolio(
                cfg,
                state_path,
                db_path,
                backup_root,
                install=True,
            )
        database = PortfolioDatabase(db_path)
        validate_existing_database(database, expected_version=SCHEMA_VERSION)
        repository = PortfolioRepository(database)
        PositionProjector(repository).rebuild()
        _runtime = PortfolioRuntime(database, repository, True)
    except Exception as exc:
        logger.exception("Portfolio initialization failed; writes disabled")
        _runtime = PortfolioRuntime(None, None, False, str(exc)[:240])
    return _runtime
