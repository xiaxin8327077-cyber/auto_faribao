from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from uuid import NAMESPACE_URL, uuid4, uuid5

from src.portfolio_db import PortfolioDatabase, SCHEMA_VERSION
from src.portfolio_models import (
    Product,
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
    decimal_text,
)
from src.portfolio_repository import PortfolioRepository


@dataclass(frozen=True)
class MigrationReport:
    products: int
    opening_positions: int
    profit_rows: int
    installed: bool
    backup_dir: str


class PortfolioTargetState(Enum):
    LEGACY_OR_MISSING = "legacy_or_missing"
    CURRENT = "current"
    UPGRADEABLE_V1 = "upgradeable_v1"
    ACTIVE_SIDECARS = "active_sidecars"
    UNSUPPORTED_PORTFOLIO = "unsupported_portfolio"


@dataclass(frozen=True)
class PortfolioTargetSnapshot:
    state: PortfolioTargetState
    exists: bool
    size: int = 0
    mtime_ns: int = 0
    sha256: str = ""


LEGACY_PRODUCT_TYPE_OVERRIDES = {
    ("nanyin_wealth", "NYRR000007"): ProductType.CASH_MANAGEMENT,
    ("citic_wealth", "AM264381F"): ProductType.CASH_MANAGEMENT,
}


def migrate_legacy_portfolio(
    cfg,
    state_path,
    target_path,
    backup_root,
    install=False,
) -> MigrationReport:
    state_path = Path(state_path)
    target_path = Path(target_path)
    backup_root = Path(backup_root)
    products, expected_shares = _legacy_products(cfg)

    target_state = _inspect_target_schema(target_path)
    target_snapshot = _snapshot_target(target_path, target_state)
    if target_state is PortfolioTargetState.ACTIVE_SIDECARS:
        raise ValueError("active portfolio database sidecars are unsafe")
    if target_state is PortfolioTargetState.UNSUPPORTED_PORTFOLIO:
        raise ValueError("unsupported portfolio schema version")
    if target_state is PortfolioTargetState.UPGRADEABLE_V1:
        return _handle_v1_target(
            state_path,
            target_path,
            len(products),
            expected_shares,
            install,
        )
    if target_state is PortfolioTargetState.CURRENT:
        _validate_candidate(target_path, len(products), expected_shares)
        counts = _database_counts(target_path)
        return MigrationReport(*counts, installed=False, backup_dir="")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = target_path.with_name(
        f"{target_path.name}.migrating-{uuid4()}"
    )
    try:
        state = _load_legacy_state(state_path)
        database = PortfolioDatabase(candidate)
        database.initialize()
        repository = PortfolioRepository(database)

        for product, _shares in products:
            repository.add_product(product)

        for product, shares in products:
            if shares is None:
                continue
            idempotency_key = f"migration:opening:{product.id}"
            repository.create_transaction(
                Transaction(
                    id=str(uuid5(NAMESPACE_URL, idempotency_key)),
                    product_id=product.id,
                    transaction_type=TransactionType.OPENING_POSITION,
                    status=TransactionStatus.CONFIRMED,
                    trade_date=date.today(),
                    idempotency_key=idempotency_key,
                    shares=shares,
                    note="Legacy opening position",
                    created_by="migration",
                )
            )

        _copy_legacy_profits(database, state)
        _stabilize_candidate(candidate)
        _validate_candidate(candidate, len(products), expected_shares)
        counts = _database_counts(candidate)

        if not install:
            _delete_candidate(candidate)
            return MigrationReport(*counts, installed=False, backup_dir="")

        backup_dir = _backup_legacy_inputs(
            state_path, target_path, backup_root
        )
        _publish_candidate_no_overwrite(
            candidate,
            target_path,
            target_snapshot,
        )
        return MigrationReport(
            *counts,
            installed=True,
            backup_dir=str(backup_dir),
        )
    except BaseException:
        _delete_candidate(candidate)
        raise


def _handle_v1_target(
    state_path,
    target_path,
    expected_product_count,
    expected_shares,
    install,
):
    state = _load_legacy_state(state_path)
    list(_legacy_profit_rows(state))
    if install:
        counts = PortfolioDatabase(target_path).upgrade_v1_validated(
            lambda conn: _validate_portfolio_connection(
                conn,
                {1},
                expected_product_count,
                expected_shares,
            )
        )
        return MigrationReport(
            *counts,
            installed=False,
            backup_dir="",
        )
    candidate = target_path.with_name(
        f"{target_path.name}.migrating-{uuid4()}"
    )
    try:
        shutil.copy2(target_path, candidate)
        PortfolioDatabase(candidate).initialize()
        _stabilize_candidate(candidate)
        _validate_candidate(
            candidate,
            expected_product_count,
            expected_shares,
        )
        counts = _database_counts(candidate)
        return MigrationReport(
            *counts,
            installed=False,
            backup_dir="",
        )
    finally:
        _delete_candidate(candidate)


def _stabilize_candidate(candidate):
    conn = sqlite3.connect(candidate)
    try:
        if conn.execute(
            "PRAGMA journal_mode"
        ).fetchone()[0].lower() == "wal":
            checkpoint = conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            _require_complete_checkpoint(tuple(checkpoint))
        journal_mode = conn.execute(
            "PRAGMA journal_mode = DELETE"
        ).fetchone()[0].lower()
        if journal_mode != "delete":
            raise RuntimeError(
                "candidate journal mode did not stabilize to DELETE"
            )
    finally:
        conn.close()
    if _has_active_sidecars(candidate):
        raise RuntimeError(
            "candidate database sidecars remain after stabilization"
        )


def _require_complete_checkpoint(result):
    busy, log_frames, checkpointed_frames = result
    if busy != 0 or log_frames != checkpointed_frames:
        raise RuntimeError(
            "candidate WAL checkpoint did not complete"
        )


def _legacy_products(cfg):
    products = []
    expected_shares = {}
    configured = getattr(getattr(cfg, "nav_monitor", None), "products", [])
    for item in configured:
        provider = str(getattr(item, "provider", "") or "")
        code = str(getattr(item, "code", "") or "").upper()
        if not provider or not code:
            continue
        product_id = str(uuid5(NAMESPACE_URL, f"{provider}:{code}"))
        product_type = LEGACY_PRODUCT_TYPE_OVERRIDES.get(
            (provider, code),
            ProductType.WEALTH_NAV,
        )
        product = Product(
            id=product_id,
            provider=provider,
            code=code,
            name=str(getattr(item, "name", "") or code),
            product_type=product_type,
        )
        raw_shares = getattr(item, "shares", None)
        shares = None if raw_shares in (None, "") else Decimal(str(raw_shares))
        products.append((product, shares))
        if shares is not None:
            expected_shares[product_id] = shares
    return products, expected_shares


def _load_legacy_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("legacy dashboard state must be a JSON object")
    return state


def _copy_legacy_profits(database: PortfolioDatabase, state: dict) -> None:
    rows = list(_legacy_profit_rows(state))
    if not rows:
        return
    with database.transaction() as conn:
        for profit_date, amount, source_kind, source_json in rows:
            row_key = (
                f"migration:profit:{source_kind}:{profit_date}:{source_json}"
            )
            conn.execute(
                """INSERT INTO legacy_profit_history
                   (id, profit_date, amount, source_kind, source_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    str(uuid5(NAMESPACE_URL, row_key)),
                    profit_date,
                    decimal_text(amount),
                    source_kind,
                    source_json,
                ),
            )


def _legacy_profit_rows(state: dict):
    automatic = state.get("profit_entries", [])
    if not isinstance(automatic, list):
        raise ValueError("legacy profit_entries must be a list")
    for entry in automatic:
        if not isinstance(entry, dict):
            raise ValueError("legacy automatic profit entry must be an object")
        amount = entry.get("profit", entry.get("amount"))
        if amount in (None, ""):
            raise ValueError("legacy automatic profit entry has no amount")
        profit_date = str(entry.get("nav_date") or "")
        if not profit_date:
            raise ValueError("legacy automatic profit entry has no nav_date")
        yield profit_date, amount, "automatic", _source_json(entry)

    periods = state.get("manual_period_profits", [])
    if not isinstance(periods, list):
        raise ValueError("legacy manual_period_profits must be a list")
    for entry in periods:
        if not isinstance(entry, dict):
            raise ValueError("legacy manual period profit entry must be an object")
        amount = entry.get("amount")
        if amount in (None, ""):
            raise ValueError("legacy manual period profit entry has no amount")
        period = str(entry.get("period") or "")
        if not period:
            raise ValueError("legacy manual period profit entry has no period")
        yield period, amount, "manual_period", _source_json(entry)

    manual = state.get("manual_daily_profits", {})
    if not isinstance(manual, dict):
        raise ValueError("legacy manual_daily_profits must be an object")
    for profit_date, source in manual.items():
        if isinstance(source, dict):
            amount = source.get("amount", source.get("profit"))
        else:
            amount = source
        if amount in (None, ""):
            raise ValueError(
                f"legacy manual profit entry has no amount: {profit_date}"
            )
        yield str(profit_date), amount, "manual_daily", _source_json(source)


def _source_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _contains_current_schema(path: Path) -> bool:
    return _inspect_target_schema(path) is PortfolioTargetState.CURRENT


def _inspect_target_schema(path: Path) -> PortfolioTargetState:
    if _has_active_sidecars(path):
        return PortfolioTargetState.ACTIVE_SIDECARS
    if not path.is_file():
        return PortfolioTargetState.LEGACY_OR_MISSING
    try:
        with _read_only_connection(path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type = 'table'"""
                )
            }
            if "schema_migrations" not in tables:
                portfolio_tables = {
                    "products",
                    "transactions",
                    "positions",
                    "quotes",
                    "audit_logs",
                    "legacy_profit_history",
                }
                if tables & portfolio_tables:
                    return PortfolioTargetState.UNSUPPORTED_PORTFOLIO
                return PortfolioTargetState.LEGACY_OR_MISSING
            try:
                versions = {
                    row[0]
                    for row in conn.execute(
                        "SELECT version FROM schema_migrations"
                    )
                }
            except sqlite3.DatabaseError:
                return PortfolioTargetState.UNSUPPORTED_PORTFOLIO
    except sqlite3.DatabaseError:
        try:
            with path.open("rb") as source:
                if source.read(16) == b"SQLite format 3\x00":
                    return PortfolioTargetState.UNSUPPORTED_PORTFOLIO
        except OSError:
            pass
        return PortfolioTargetState.LEGACY_OR_MISSING
    if versions == {SCHEMA_VERSION}:
        return PortfolioTargetState.CURRENT
    if versions == {1}:
        return PortfolioTargetState.UPGRADEABLE_V1
    return PortfolioTargetState.UNSUPPORTED_PORTFOLIO


def _has_active_sidecars(path):
    sidecars = (
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    )
    try:
        return any(
            sidecar.is_file() and sidecar.stat().st_size
            for sidecar in sidecars
        )
    except OSError:
        return True


def _validate_candidate(
    path: Path,
    expected_product_count: int,
    expected_shares: dict[str, Decimal],
) -> None:
    with _read_only_connection(path) as conn:
        _validate_portfolio_connection(
            conn,
            {SCHEMA_VERSION},
            expected_product_count,
            expected_shares,
        )


def _validate_portfolio_connection(
    conn,
    expected_versions,
    expected_product_count,
    expected_shares,
):
    versions = {
        row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations"
        )
    }
    if versions != expected_versions:
        raise ValueError("portfolio schema version mismatch")

    product_count = conn.execute(
        "SELECT COUNT(*) FROM products"
    ).fetchone()[0]
    if product_count != expected_product_count:
        raise ValueError(
            "portfolio product count mismatch: "
            f"expected {expected_product_count}, got {product_count}"
        )

    rows = conn.execute(
        """SELECT product_id, shares
           FROM transactions
           WHERE transaction_type = 'opening_position'
             AND status = 'confirmed'"""
    ).fetchall()
    if len(rows) != len(expected_shares):
        raise ValueError(
            "portfolio opening-position count mismatch: "
            f"expected {len(expected_shares)}, got {len(rows)}"
        )

    actual_shares = {}
    for row in rows:
        product_id = row["product_id"]
        shares = Decimal(row["shares"])
        actual_shares[product_id] = (
            actual_shares.get(product_id, Decimal("0")) + shares
        )
    if actual_shares != expected_shares:
        raise ValueError("portfolio opening-position share totals mismatch")
    return (
        product_count,
        len(rows),
        conn.execute(
            "SELECT COUNT(*) FROM legacy_profit_history"
        ).fetchone()[0],
    )


def _database_counts(path: Path) -> tuple[int, int, int]:
    with _read_only_connection(path) as conn:
        products = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        opening_positions = conn.execute(
            """SELECT COUNT(*) FROM transactions
               WHERE transaction_type = 'opening_position'
                 AND status = 'confirmed'"""
        ).fetchone()[0]
        profit_rows = conn.execute(
            "SELECT COUNT(*) FROM legacy_profit_history"
        ).fetchone()[0]
    return products, opening_positions, profit_rows


@contextmanager
def _read_only_connection(path: Path):
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()


def _backup_legacy_inputs(
    state_path: Path,
    target_path: Path,
    backup_root: Path,
) -> Path:
    timestamp = datetime.now().replace(microsecond=0)
    backup_dir = backup_root / timestamp.strftime("%Y%m%d-%H%M%S")
    while backup_dir.exists():
        timestamp += timedelta(seconds=1)
        backup_dir = backup_root / timestamp.strftime("%Y%m%d-%H%M%S")
    backup_dir.mkdir(parents=True)

    for source in (state_path, target_path):
        if not source.is_file():
            continue
        destination = backup_dir / source.name
        shutil.copy2(source, destination)
        with destination.open("rb+") as copied:
            os.fsync(copied.fileno())
    return backup_dir


def _snapshot_target(path, state=None):
    state = state or _inspect_target_schema(path)
    if not path.is_file():
        return PortfolioTargetSnapshot(state=state, exists=False)
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    final_stat = path.stat()
    if (
        stat.st_size != final_stat.st_size
        or stat.st_mtime_ns != final_stat.st_mtime_ns
    ):
        raise ValueError("target changed during portfolio inspection")
    return PortfolioTargetSnapshot(
        state=state,
        exists=True,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=digest.hexdigest(),
    )


def _publish_candidate_no_overwrite(candidate, target, expected):
    if _snapshot_target(target) != expected:
        raise ValueError("target changed during portfolio publication")
    if _has_active_sidecars(target):
        raise ValueError("active portfolio database sidecars are unsafe")
    displaced = None
    published = False
    if target.is_file():
        displaced = target.with_name(
            f"{target.name}.displaced-{uuid4()}"
        )
        os.link(target, displaced)
        if (
            _snapshot_target(target) != expected
            or
            _has_active_sidecars(target)
            or not target.is_file()
            or not os.path.samefile(target, displaced)
        ):
            displaced.unlink(missing_ok=True)
            raise ValueError(
                "target changed during portfolio publication"
            )
        target.unlink()
    try:
        if _has_active_sidecars(target):
            raise ValueError(
                "active portfolio database sidecars are unsafe"
            )
        try:
            os.link(candidate, target)
        except FileExistsError as exc:
            raise ValueError(
                "target appeared during portfolio publication"
            ) from exc
        published = True
        if _has_active_sidecars(target):
            raise ValueError(
                "active portfolio database sidecars are unsafe"
            )
        if not os.path.samefile(candidate, target):
            raise ValueError(
                "target changed during portfolio publication"
            )
        candidate.unlink()
        published = False
    except BaseException:
        if (
            published
            and candidate.exists()
            and target.is_file()
            and os.path.samefile(candidate, target)
        ):
            target.unlink()
        if displaced is not None and not target.exists():
            try:
                os.link(displaced, target)
            except FileExistsError:
                pass
        raise
    finally:
        if displaced is not None:
            displaced.unlink(missing_ok=True)


def _delete_candidate(candidate: Path) -> None:
    for path in (
        candidate,
        Path(f"{candidate}-wal"),
        Path(f"{candidate}-shm"),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
