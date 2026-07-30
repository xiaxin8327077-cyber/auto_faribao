from datetime import date
from decimal import Decimal
import sqlite3
import threading

import pytest

from src.portfolio_models import (
    ProductType,
    TransactionStatus,
    TransactionType,
    decimal_text,
    optional_decimal_text,
)

from src.portfolio_db import (
    BASE_SCHEMA_SQL,
    V2_SCHEMA_SQL,
    PortfolioDatabase,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository
from src.portfolio_transactions import PortfolioTransactionService


def initialize_v1_database(db):
    with db.connection() as conn:
        conn.executescript(BASE_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_migrations(version) VALUES (1)"
        )


def schema_object_names(db):
    with db.connection() as conn:
        return {
            row["name"]
            for row in conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type IN ('table', 'index', 'trigger')"""
            )
        }


def test_portfolio_enums_use_stable_database_values():
    assert ProductType.CASH_MANAGEMENT.value == "cash_management"
    assert TransactionType.SIP_PURCHASE.value == "sip_purchase"
    assert TransactionStatus.PENDING_QUOTE.value == "pending_quote"


def test_decimal_text_is_canonical_and_never_uses_float_rounding():
    assert decimal_text("2000.000") == "2000"
    assert decimal_text(Decimal("0.00006")) == "0.00006"
    assert optional_decimal_text("") is None


def test_database_initializes_versioned_schema_with_wal_and_foreign_keys(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "products", "quotes", "transactions", "sip_plans",
            "plan_executions", "positions", "audit_logs",
            "legacy_profit_history", "schema_migrations",
        } <= tables
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_clean_populated_v1_upgrades_to_v2_atomically_and_repeat_safely(
    tmp_path,
):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    initialize_v1_database(db)
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('cash', 'test', 'cash', '现金', 'cash_management',
                       'active')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, idempotency_key, created_by)
               VALUES ('opening', 'cash', 'opening_position', 'confirmed',
                       '2026-01-01', '100', '100', 'opening-key', 'test')"""
        )

    db.initialize()
    first_objects = schema_object_names(db)
    db.initialize()

    with db.connection() as conn:
        assert {
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )
        } == {2}
        assert tuple(
            conn.execute(
                """SELECT amount, shares FROM transactions
                   WHERE id = 'opening'"""
            ).fetchone()
        ) == ("100", "100")
    assert schema_object_names(db) == first_objects
    assert {
        "idx_one_cash_leg_per_transaction",
        "idx_one_reversal_child_per_transaction",
        "prevent_unbacked_confirmed_reversal",
    } <= first_objects


def test_v1_with_already_installed_v2_objects_only_advances_version(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    initialize_v1_database(db)
    with db.connection() as conn:
        conn.executescript(V2_SCHEMA_SQL)

    db.initialize()

    with db.connection() as conn:
        assert {
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )
        } == {2}


@pytest.mark.parametrize("initial_version", [None, 1])
def test_concurrent_initializers_share_one_locked_schema_transition(
    tmp_path,
    initial_version,
):
    path = tmp_path / "portfolio.db"
    if initial_version == 1:
        initialize_v1_database(PortfolioDatabase(path))
    barrier = threading.Barrier(8)
    errors = []

    def initialize():
        try:
            barrier.wait()
            PortfolioDatabase(path).initialize()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    with PortfolioDatabase(path).connection() as conn:
        assert [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )
        ] == [2]
        assert conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE name IN (
                   'idx_one_cash_leg_per_transaction',
                   'idx_one_reversal_child_per_transaction',
                   'prevent_unbacked_confirmed_reversal'
               )"""
        ).fetchone()[0] == 3


@pytest.mark.parametrize(
    "dirty_kind",
    ["cash", "reversal", "unbacked"],
)
def test_dirty_v1_upgrade_fails_without_version_object_or_data_changes(
    tmp_path,
    dirty_kind,
):
    db = PortfolioDatabase(tmp_path / f"{dirty_kind}.db")
    initialize_v1_database(db)
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('fund', 'test', 'fund', '基金', 'public_fund',
                       'active')"""
        )
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('cash', 'test', 'cash', '现金', 'cash_management',
                       'active')"""
        )
        original_status = (
            "reversed" if dirty_kind == "unbacked" else "confirmed"
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, idempotency_key, created_by)
               VALUES ('main', 'fund', 'manual_purchase', ?, '2026-01-01',
                       '100', '50', 'main-key', 'test')""",
            (original_status,),
        )
        if dirty_kind == "cash":
            for suffix in ("a", "b"):
                conn.execute(
                    """INSERT INTO transactions
                       (id, product_id, transaction_type, status, trade_date,
                        amount, shares, linked_transaction_id,
                        idempotency_key, created_by)
                       VALUES (?, 'cash', 'cash_transfer_out', 'confirmed',
                               '2026-01-01', '100', '100', 'main', ?,
                               'test')""",
                    (f"cash-{suffix}", f"cash-{suffix}-key"),
                )
        elif dirty_kind == "reversal":
            for suffix in ("a", "b"):
                conn.execute(
                    """INSERT INTO transactions
                       (id, product_id, transaction_type, status, trade_date,
                        amount, shares, linked_transaction_id,
                        idempotency_key, created_by)
                       VALUES (?, 'fund', 'reversal', 'confirmed',
                               '2026-01-01', '-100', '-50', 'main', ?,
                               'test')""",
                    (
                        f"reversal-{suffix}",
                        f"reversal-{suffix}-key",
                    ),
                )
    before_objects = schema_object_names(db)
    with db.connection() as conn:
        before_rows = [
            tuple(row)
            for row in conn.execute(
                """SELECT id, status, amount, shares, linked_transaction_id
                   FROM transactions ORDER BY id"""
            )
        ]

    with pytest.raises(
        ValueError,
        match=f"portfolio v2 migration blocked: .*{dirty_kind}",
    ):
        db.initialize()

    assert schema_object_names(db) == before_objects
    with db.connection() as conn:
        assert {
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )
        } == {1}
        assert [
            tuple(row)
            for row in conn.execute(
                """SELECT id, status, amount, shares, linked_transaction_id
                   FROM transactions ORDER BY id"""
            )
        ] == before_rows
        assert conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE name IN (
                   'idx_one_cash_leg_per_transaction',
                   'idx_one_reversal_child_per_transaction',
                   'prevent_unbacked_confirmed_reversal'
               )"""
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "graph_kind",
    ["confirmed_parent", "cancelled_parent", "cycle"],
)
def test_v1_upgrade_rejects_invalid_applied_reversal_graph_atomically(
    tmp_path,
    graph_kind,
):
    db = PortfolioDatabase(tmp_path / f"{graph_kind}.db")
    initialize_v1_database(db)
    with db.connection() as conn:
        if graph_kind == "cycle":
            conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('fund', 'test', 'fund', '基金', 'public_fund',
                       'active')"""
        )
        if graph_kind == "cycle":
            rows = [
                (
                    "a",
                    "reversal",
                    "reversed",
                    "100",
                    "50",
                    "b",
                    "a-key",
                ),
                (
                    "b",
                    "reversal",
                    "reversed",
                    "-100",
                    "-50",
                    "a",
                    "b-key",
                ),
            ]
        else:
            parent_status = graph_kind.removesuffix("_parent")
            rows = [
                (
                    "parent",
                    "manual_purchase",
                    parent_status,
                    "100",
                    "50",
                    None,
                    "parent-key",
                ),
                (
                    "child",
                    "reversal",
                    "confirmed",
                    "-100",
                    "-50",
                    "parent",
                    "child-key",
                ),
            ]
        conn.executemany(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date,
                amount, shares, linked_transaction_id, idempotency_key,
                created_by)
               VALUES (?, 'fund', ?, ?, '2026-01-01', ?, ?, ?, ?, 'test')""",
            rows,
        )
    before_objects = schema_object_names(db)
    with db.connection() as conn:
        before_rows = [
            tuple(row)
            for row in conn.execute(
                """SELECT id, transaction_type, status, amount, shares,
                          linked_transaction_id
                   FROM transactions ORDER BY id"""
            )
        ]

    with pytest.raises(
        ValueError,
        match="portfolio v2 migration blocked: invalid reversal graph",
    ):
        db.initialize()

    assert schema_object_names(db) == before_objects
    with db.connection() as conn:
        assert [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )
        ] == [1]
        assert [
            tuple(row)
            for row in conn.execute(
                """SELECT id, transaction_type, status, amount, shares,
                          linked_transaction_id
                   FROM transactions ORDER BY id"""
            )
        ] == before_rows


@pytest.mark.parametrize(
    "whitespace",
    [" ", "\t", "\r\n", "\u00a0", "\u2003"],
)
def test_v1_upgrade_normalizes_key_and_equivalent_retry_reuses_old_row(
    tmp_path,
    whitespace,
):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    initialize_v1_database(db)
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('cash', 'test', 'cash', '现金', 'cash_management',
                       'active')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date,
                amount, shares, fee_amount, fee_rate, confirmation_date,
                confirmation_nav, idempotency_key, created_by)
               VALUES ('old-purchase', 'cash', 'manual_purchase', 'confirmed',
                       '2026-07-30', '10', '10', '0', '0', '2026-07-30',
                       '1', ?, 'web')""",
            (f"{whitespace}old-key{whitespace}",),
        )

    db.initialize()
    repository = PortfolioRepository(db)
    service = PortfolioTransactionService(
        repository,
        PositionProjector(repository),
    )
    retried = service.record_purchase(
        "cash",
        Decimal("10"),
        date(2026, 7, 30),
        "old-key",
    )

    assert retried.id == "old-purchase"
    assert retried.idempotency_key == "old-key"
    assert len(repository.list_transactions(product_id="cash")) == 1


@pytest.mark.parametrize(
    "dirty_kind",
    ["blank", "trim_collision", "operation_collision"],
)
def test_v1_upgrade_rejects_unsafe_idempotency_normalization_atomically(
    tmp_path,
    dirty_kind,
):
    db = PortfolioDatabase(tmp_path / f"{dirty_kind}.db")
    initialize_v1_database(db)
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('cash', 'test', 'cash', '现金', 'cash_management',
                       'active')"""
        )
        keys = {
            "blank": ["\u2003\t\r\n\u00a0"],
            "trim_collision": ["same-key", "\u00a0same-key\t"],
            "operation_collision": [" operation-key "],
        }[dirty_kind]
        for index, key in enumerate(keys):
            conn.execute(
                """INSERT INTO transactions
                   (id, product_id, transaction_type, status, trade_date,
                    amount, shares, idempotency_key, created_by)
                   VALUES (?, 'cash', 'opening_position', 'confirmed',
                           '2026-01-01', '1', '1', ?, 'test')""",
                (f"row-{index}", key),
            )
        if dirty_kind == "operation_collision":
            operation_id = (
                PortfolioTransactionService._operation_audit_id(
                    "operation-key"
                )
            )
            conn.execute(
                """INSERT INTO audit_logs
                   (id, action, object_type, object_id, result, source)
                   VALUES (?, 'cancel_pending', 'transaction', 'other',
                           'success', 'test')""",
                (operation_id,),
            )
    before_objects = schema_object_names(db)
    with db.connection() as conn:
        before_rows = [
            tuple(row)
            for row in conn.execute(
                """SELECT id, idempotency_key FROM transactions
                   ORDER BY id"""
            )
        ]

    with pytest.raises(
        ValueError,
        match="portfolio v2 migration blocked: unsafe idempotency keys",
    ):
        db.initialize()

    assert schema_object_names(db) == before_objects
    with db.connection() as conn:
        assert [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )
        ] == [1]
        assert [
            tuple(row)
            for row in conn.execute(
                """SELECT id, idempotency_key FROM transactions
                   ORDER BY id"""
            )
        ] == before_rows


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "   ",
        " padded ",
        "\tkey\t",
        "\r\nkey\r\n",
        "\u00a0key\u00a0",
        "\u2003key\u2003",
        "\t\r\n\u00a0\u2003",
    ],
)
def test_v2_database_rejects_non_normalized_transaction_keys(
    tmp_path,
    bad_key,
):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('cash', 'test', 'cash', '现金', 'cash_management',
                       'active')"""
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="idempotency key",
        ):
            conn.execute(
                """INSERT INTO transactions
                   (id, product_id, transaction_type, status, trade_date,
                    amount, shares, idempotency_key, created_by)
                   VALUES ('bad', 'cash', 'opening_position', 'confirmed',
                           '2026-01-01', '1', '1', ?, 'test')""",
                (bad_key,),
            )


def test_v2_external_sqlite_writer_without_canonical_function_fails_safe(
    tmp_path,
):
    path = tmp_path / "portfolio.db"
    db = PortfolioDatabase(path)
    db.initialize()
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('cash', 'test', 'cash', '现金', 'cash_management',
                       'active')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date,
                amount, shares, idempotency_key, created_by)
               VALUES ('external', 'cash', 'opening_position',
                       'pending_quote', '2026-01-01', '1', '1',
                       'external-key', 'test')"""
        )

    with sqlite3.connect(path) as conn:
        with pytest.raises(
            sqlite3.OperationalError,
            match="canonical_idempotency_key",
        ):
            conn.execute(
                """UPDATE transactions
                   SET idempotency_key = 'external-key-2'
                   WHERE id = 'external'"""
            )


def test_v2_database_rejects_key_de_normalization_on_update(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('cash', 'test', 'cash', '现金', 'cash_management',
                       'active')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date,
                amount, shares, idempotency_key, created_by)
               VALUES ('valid', 'cash', 'opening_position', 'pending_quote',
                       '2026-01-01', '1', '1', 'valid-key', 'test')"""
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="idempotency key",
        ):
            conn.execute(
                """UPDATE transactions SET idempotency_key = ' valid-key '
                   WHERE id = 'valid'"""
            )


def test_v1_upgrade_ddl_failure_rolls_back_all_v2_objects_and_version(
    tmp_path,
):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    initialize_v1_database(db)
    with db.connection() as conn:
        conn.execute(
            """CREATE TABLE idx_one_reversal_child_per_transaction (
                   sentinel TEXT
               )"""
        )
        conn.execute(
            """INSERT INTO idx_one_reversal_child_per_transaction
               VALUES ('keep')"""
        )
    before_objects = schema_object_names(db)

    with pytest.raises(sqlite3.DatabaseError):
        db.initialize()

    assert schema_object_names(db) == before_objects
    with db.connection() as conn:
        assert {
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            )
        } == {1}
        assert conn.execute(
            """SELECT sentinel
               FROM idx_one_reversal_child_per_transaction"""
        ).fetchone()[0] == "keep"
        assert conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE name = 'idx_one_cash_leg_per_transaction'"""
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE name = 'prevent_unbacked_confirmed_reversal'"""
        ).fetchone()[0] == 0


def test_transaction_rolls_back_every_write_on_error(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                """INSERT INTO products
                   (id, provider, code, name, product_type, status)
                   VALUES ('p1', 'test', '001', '产品', 'wealth_nav', 'active')"""
            )
            raise RuntimeError("stop")

    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def test_connection_restores_wal_after_another_connection_changes_journal_mode(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with sqlite3.connect(db.path) as external_conn:
        assert external_conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0].lower() == "delete"

    with db.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_database_enforces_canonical_decimal_text_for_every_accounting_field(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('p1', 'test', '001', '产品', 'wealth_nav', 'active')"""
        )
        conn.execute(
            """INSERT INTO quotes
               (id, product_id, quote_date, unit_nav, cumulative_nav,
                income_per_10k, seven_day_annualized_rate, source, raw_hash,
                fetched_at)
               VALUES ('q1', 'p1', '2026-01-01', '2', '2', '2', '2', 'test',
                       'hash', '2026-01-01T00:00:00')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, fee_amount, fee_rate, confirmation_nav, idempotency_key,
                created_by)
               VALUES ('t1', 'p1', 'manual_purchase', 'pending_confirmation',
                       '2026-01-01', '2', '2', '2', '2', '2', 'tx-1', 'test')"""
        )
        conn.execute(
            """INSERT INTO sip_plans
               (id, product_id, daily_amount, purchase_fee_rate, status, start_date)
               VALUES ('s1', 'p1', '2', '2', 'active', '2026-01-01')"""
        )
        conn.execute(
            """INSERT INTO positions
               (product_id, available_shares, locked_shares, total_shares, cost_basis)
               VALUES ('p1', '2', '2', '2', '2')"""
        )
        conn.execute(
            """INSERT INTO legacy_profit_history
               (id, profit_date, amount, source_kind, source_json)
               VALUES ('l1', '2026-01-01', '2', 'test', '{}')"""
        )

        for statement in (
            "UPDATE quotes SET unit_nav = '2.00' WHERE id = 'q1'",
            "UPDATE quotes SET cumulative_nav = '2.00' WHERE id = 'q1'",
            "UPDATE quotes SET income_per_10k = '2.00' WHERE id = 'q1'",
            "UPDATE quotes SET seven_day_annualized_rate = '2.00' WHERE id = 'q1'",
            "UPDATE transactions SET amount = '2.00' WHERE id = 't1'",
            "UPDATE transactions SET shares = '2.00' WHERE id = 't1'",
            "UPDATE transactions SET fee_amount = '2.00' WHERE id = 't1'",
            "UPDATE transactions SET fee_rate = '2.00' WHERE id = 't1'",
            "UPDATE transactions SET confirmation_nav = '2.00' WHERE id = 't1'",
            "UPDATE sip_plans SET daily_amount = '2.00' WHERE id = 's1'",
            "UPDATE sip_plans SET purchase_fee_rate = '2.00' WHERE id = 's1'",
            "UPDATE positions SET available_shares = '2.00' WHERE product_id = 'p1'",
            "UPDATE positions SET locked_shares = '2.00' WHERE product_id = 'p1'",
            "UPDATE positions SET total_shares = '2.00' WHERE product_id = 'p1'",
            "UPDATE positions SET cost_basis = '2.00' WHERE product_id = 'p1'",
            "UPDATE legacy_profit_history SET amount = '2.00' WHERE id = 'l1'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE quotes SET unit_nav = 'NaN' WHERE id = 'q1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE quotes SET unit_nav = 'not-a-number' WHERE id = 'q1'")


def test_confirmed_transaction_is_immutable_except_for_controlled_reversal(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('p1', 'test', '001', '产品', 'wealth_nav', 'active')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, idempotency_key, note, created_by, confirmed_at)
               VALUES ('t1', 'p1', 'manual_purchase', 'confirmed', '2026-01-01',
                       '2', '2', 'tx-1', 'original', 'test',
                       '2026-01-02T00:00:00')"""
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE transactions SET note = 'changed' WHERE id = 't1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM transactions WHERE id = 't1'")

        with pytest.raises(
            sqlite3.IntegrityError,
            match="confirmed transaction reversal requires a valid child",
        ):
            conn.execute(
                """UPDATE transactions
                   SET status = 'reversed',
                       reversed_at = '2026-01-03T00:00:00'
                   WHERE id = 't1'"""
            )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, note,
                created_by)
               VALUES ('r1', 'p1', 'reversal', 'confirmed', '2026-01-01',
                       '-2', '-2', 't1', 'reverse-1', 'correction', 'test')"""
        )
        conn.execute(
            """UPDATE transactions
               SET status = 'reversed', reversed_at = '2026-01-03T00:00:00'
               WHERE id = 't1'"""
        )
        row = conn.execute("SELECT * FROM transactions WHERE id = 't1'").fetchone()
        assert row["status"] == "reversed"
        assert row["reversed_at"] == "2026-01-03T00:00:00"
        assert row["note"] == "original"
        assert row["amount"] == "2"
        assert row["confirmed_at"] == "2026-01-02T00:00:00"

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE transactions SET note = 'changed' WHERE id = 't1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM transactions WHERE id = 't1'")


def test_database_rejects_second_cash_leg_but_allows_reversal_child(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('fund', 'test', '001', '基金', 'public_fund', 'active')"""
        )
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('cash', 'test', 'cash', '现金', 'cash_management',
                       'active')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, idempotency_key, created_by)
               VALUES ('main', 'fund', 'manual_purchase', 'confirmed',
                       '2026-01-01', '100', '50', 'main-key', 'test')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, created_by)
               VALUES ('cash-1', 'cash', 'cash_transfer_out', 'confirmed',
                       '2026-01-01', '100', '100', 'main', 'cash-1-key',
                       'test')"""
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO transactions
                   (id, product_id, transaction_type, status, trade_date,
                    amount, shares, linked_transaction_id, idempotency_key,
                    created_by)
                   VALUES ('cash-2', 'cash', 'cash_transfer_out', 'confirmed',
                           '2026-01-01', '100', '100', 'main', 'cash-2-key',
                           'test')"""
            )

        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, created_by)
               VALUES ('reversal-1', 'fund', 'reversal', 'confirmed',
                       '2026-01-01', '-100', '-50', 'main', 'reverse-1-key',
                       'test')"""
        )


def test_database_rejects_second_reversal_child_but_allows_cash_leg(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('fund', 'test', '001', '基金', 'public_fund', 'active')"""
        )
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('cash', 'test', 'cash', '现金', 'cash_management',
                       'active')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, idempotency_key, created_by)
               VALUES ('main', 'fund', 'manual_purchase', 'confirmed',
                       '2026-01-01', '100', '50', 'main-key', 'test')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, created_by)
               VALUES ('cash-1', 'cash', 'cash_transfer_out', 'confirmed',
                       '2026-01-01', '100', '100', 'main', 'cash-1-key',
                       'test')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, created_by)
               VALUES ('reversal-1', 'fund', 'reversal', 'confirmed',
                       '2026-01-01', '-100', '-50', 'main', 'reverse-1-key',
                       'test')"""
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO transactions
                   (id, product_id, transaction_type, status, trade_date,
                    amount, shares, linked_transaction_id, idempotency_key,
                    created_by)
                   VALUES ('reversal-2', 'fund', 'reversal', 'confirmed',
                           '2026-01-01', '-100', '-50', 'main',
                           'reverse-2-key', 'test')"""
            )


@pytest.mark.parametrize(
    ("child_product", "child_amount", "child_shares", "child_status"),
    [
        ("other", "-100", "-50", "confirmed"),
        ("fund", "-99", "-50", "confirmed"),
        ("fund", "-100", "-49", "confirmed"),
        ("fund", "-100", "-50", "cancelled"),
    ],
)
def test_database_rejects_reversed_status_without_exact_confirmed_child(
    tmp_path,
    child_product,
    child_amount,
    child_shares,
    child_status,
):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()

    with db.connection() as conn:
        for product_id in ("fund", "other"):
            conn.execute(
                """INSERT INTO products
                   (id, provider, code, name, product_type, status)
                   VALUES (?, 'test', ?, ?, 'public_fund', 'active')""",
                (product_id, product_id, product_id),
            )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, idempotency_key, created_by)
               VALUES ('main', 'fund', 'manual_purchase', 'confirmed',
                       '2026-01-01', '100', '50', 'main-key', 'test')"""
        )
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date, amount,
                shares, linked_transaction_id, idempotency_key, created_by)
               VALUES ('child', ?, 'reversal', ?, '2026-01-01', ?, ?, 'main',
                       'child-key', 'test')""",
            (
                child_product,
                child_status,
                child_amount,
                child_shares,
            ),
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="confirmed transaction reversal requires a valid child",
        ):
            conn.execute(
                """UPDATE transactions
                   SET status = 'reversed', reversed_at = CURRENT_TIMESTAMP
                   WHERE id = 'main'"""
            )
