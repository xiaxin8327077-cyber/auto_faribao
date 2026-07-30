from datetime import date
from decimal import Decimal
import threading

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    Position,
    Product,
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository


@pytest.fixture
def repo(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    return PortfolioRepository(database)


def product(product_id):
    return Product(
        product_id,
        "test_provider",
        product_id,
        f"Product {product_id}",
        ProductType.PUBLIC_FUND,
    )


def tx(
    tx_id,
    product_id,
    kind,
    status,
    shares,
    amount="0",
    trade_date=date(2026, 7, 30),
    linked_transaction_id="",
):
    return Transaction(
        id=tx_id,
        product_id=product_id,
        transaction_type=kind,
        status=status,
        trade_date=trade_date,
        idempotency_key=f"test:{tx_id}",
        shares=Decimal(shares) if shares is not None else None,
        amount=Decimal(amount) if amount is not None else None,
        linked_transaction_id=linked_transaction_id,
    )


def test_list_transactions_filters_and_orders_by_trade_date_created_at_and_id(repo):
    repo.add_product(product("alpha"))
    repo.add_product(product("beta"))
    rows = [
        tx(
            "z-last-id",
            "alpha",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_QUOTE,
            "1",
            trade_date=date(2026, 7, 30),
        ),
        tx(
            "a-first-id",
            "alpha",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_CONFIRMATION,
            "1",
            trade_date=date(2026, 7, 30),
        ),
        tx(
            "older-created",
            "alpha",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_QUOTE,
            "1",
            trade_date=date(2026, 7, 30),
        ),
        tx(
            "older-trade",
            "alpha",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_QUOTE,
            "1",
            trade_date=date(2026, 7, 29),
        ),
        tx(
            "other-product",
            "beta",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_QUOTE,
            "1",
            trade_date=date(2026, 7, 28),
        ),
    ]
    for row in rows:
        repo.create_transaction(row)
    with repo.database.transaction() as conn:
        conn.execute(
            """UPDATE transactions
               SET created_at = CASE
                   WHEN id = 'older-created' THEN '2026-07-30 00:00:00'
                   ELSE '2026-07-30 01:00:00'
               END
               WHERE product_id = 'alpha'"""
        )

    assert [
        row.id for row in repo.list_transactions(product_id="alpha")
    ] == ["older-trade", "older-created", "a-first-id", "z-last-id"]
    assert [
        row.id
        for row in repo.list_transactions(
            statuses=[TransactionStatus.PENDING_CONFIRMATION]
        )
    ] == ["a-first-id"]
    assert repo.list_transactions(statuses=[]) == []


def test_projector_replays_confirmed_events_and_pending_locks(repo):
    repo.add_product(product("cash"))
    repo.create_transaction(
        tx(
            "open",
            "cash",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "10000",
            "10000",
        )
    )
    repo.create_transaction(
        tx(
            "pending-redemption",
            "cash",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.PENDING_CONFIRMATION,
            "1500",
            "1500",
        )
    )
    repo.create_transaction(
        tx(
            "pending-transfer",
            "cash",
            TransactionType.CASH_TRANSFER_OUT,
            TransactionStatus.PENDING_QUOTE,
            "500",
            "500",
        )
    )
    repo.create_transaction(
        tx(
            "pending-purchase",
            "cash",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_QUOTE,
            "9000",
            "9000",
        )
    )

    position = PositionProjector(repo).calculate("cash")

    assert position == Position(
        "cash",
        available_shares=Decimal("8000"),
        locked_shares=Decimal("2000"),
        total_shares=Decimal("10000"),
        cost_basis=Decimal("10000"),
    )


def test_projector_uses_average_cost_for_outflows_and_signed_adjustments(repo):
    repo.add_product(product("fund"))
    events = [
        tx(
            "01-open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "100",
            "200",
        ),
        tx(
            "02-buy",
            "fund",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.CONFIRMED,
            "100",
            "400",
        ),
        tx(
            "03-redeem",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.CONFIRMED,
            "50",
            "999",
        ),
        tx(
            "04-adjust-up",
            "fund",
            TransactionType.HOLDING_ADJUSTMENT,
            TransactionStatus.CONFIRMED,
            "30",
            "90",
        ),
        tx(
            "05-adjust-down",
            "fund",
            TransactionType.HOLDING_ADJUSTMENT,
            TransactionStatus.CONFIRMED,
            "-20",
            "999",
        ),
    ]
    for event in events:
        repo.create_transaction(event)

    position = PositionProjector(repo).calculate("fund")

    assert position.total_shares == Decimal("160")
    assert position.cost_basis == Decimal("480")


def test_projector_applies_income_and_transfer_cost_and_ignores_cash_dividend(repo):
    repo.add_product(product("cash"))
    events = [
        tx(
            "01-open",
            "cash",
            TransactionType.CASH_TRANSFER_IN,
            TransactionStatus.CONFIRMED,
            "100",
            "100",
        ),
        tx(
            "02-income",
            "cash",
            TransactionType.INCOME_ACCRUAL,
            TransactionStatus.CONFIRMED,
            "20",
            "20",
        ),
        tx(
            "03-dividend",
            "cash",
            TransactionType.CASH_DIVIDEND,
            TransactionStatus.CONFIRMED,
            "0",
            "500",
        ),
        tx(
            "04-transfer-out",
            "cash",
            TransactionType.CASH_TRANSFER_OUT,
            TransactionStatus.CONFIRMED,
            "30",
            "999",
        ),
    ]
    for event in events:
        repo.create_transaction(event)

    assert PositionProjector(repo).calculate("cash") == Position(
        "cash",
        available_shares=Decimal("90"),
        locked_shares=Decimal("0"),
        total_shares=Decimal("90"),
        cost_basis=Decimal("90"),
    )


def test_projector_replays_a_strict_linked_reversal_pair(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "01-original",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.REVERSED,
            "12.5",
            "25",
        )
    )
    repo.create_transaction(
        tx(
            "02-reversal",
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.CONFIRMED,
            "-12.5",
            "-25",
            linked_transaction_id="01-original",
        )
    )
    repo.create_transaction(
        tx(
            "03-cancelled",
            "fund",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.CANCELLED,
            "100",
            "100",
        )
    )
    repo.create_transaction(
        tx(
            "04-failed",
            "fund",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.FAILED,
            "100",
            "100",
        )
    )

    assert PositionProjector(repo).calculate("fund") == Position(
        "fund",
        available_shares=Decimal("0"),
        locked_shares=Decimal("0"),
        total_shares=Decimal("0"),
        cost_basis=Decimal("0"),
    )


def test_linked_reversal_negates_raw_outflow_fields_not_share_direction(repo):
    repo.add_product(product("fund"))
    events = [
        tx(
            "01-open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "100",
            "200",
        ),
        tx(
            "02-outflow",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.REVERSED,
            "20",
            "50",
        ),
        tx(
            "03-reversal",
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.CONFIRMED,
            "-20",
            "-50",
            linked_transaction_id="02-outflow",
        ),
    ]
    for event in events:
        repo.create_transaction(event)

    position = PositionProjector(repo).calculate("fund")

    assert position.total_shares == Decimal("100")
    assert position.cost_basis == Decimal("200")


def test_linked_reversal_restores_cost_after_a_full_outflow(repo):
    repo.add_product(product("fund"))
    events = [
        tx(
            "01-open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "3",
            "1",
        ),
        tx(
            "02-outflow",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.REVERSED,
            "3",
            "10",
        ),
        tx(
            "03-reversal",
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.CONFIRMED,
            "-3",
            "-10",
            linked_transaction_id="02-outflow",
        ),
    ]
    for event in events:
        repo.create_transaction(event)

    position = PositionProjector(repo).calculate("fund")

    assert position.total_shares == Decimal("3")
    assert position.cost_basis == Decimal("1")


def test_linked_reversal_replays_later_average_cost_outflow_without_original(repo):
    repo.add_product(product("fund"))
    events = [
        tx(
            "01-open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "100",
            "100",
            trade_date=date(2026, 7, 27),
        ),
        tx(
            "02-purchase",
            "fund",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.REVERSED,
            "100",
            "1000",
            trade_date=date(2026, 7, 28),
        ),
        tx(
            "03-redeem",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.CONFIRMED,
            "50",
            "999",
            trade_date=date(2026, 7, 29),
        ),
        tx(
            "04-reversal",
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.CONFIRMED,
            "-100",
            "-1000",
            trade_date=date(2026, 7, 30),
            linked_transaction_id="02-purchase",
        ),
    ]
    for event in events:
        repo.create_transaction(event)

    position = PositionProjector(repo).calculate("fund")

    assert position.total_shares == Decimal("50")
    assert position.cost_basis == Decimal("50")


def test_linked_reversal_is_causal_when_same_timestamp_id_sorts_before_original(repo):
    repo.add_product(product("fund"))
    original_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    reversal_id = "11111111-1111-1111-1111-111111111111"
    rows = [
        tx(
            "00000000-0000-0000-0000-000000000000",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.PENDING_QUOTE,
            "100",
            "100",
        ),
        tx(
            original_id,
            "fund",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.PENDING_QUOTE,
            "100",
            "1000",
        ),
        tx(
            reversal_id,
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.PENDING_QUOTE,
            "-100",
            "-1000",
            linked_transaction_id=original_id,
        ),
    ]
    for row in rows:
        repo.create_transaction(row)
    with repo.database.transaction() as conn:
        conn.execute(
            """UPDATE transactions
               SET status = 'confirmed', created_at = '2026-07-30 00:00:00'
               WHERE id = '00000000-0000-0000-0000-000000000000'"""
        )
        conn.execute(
            """UPDATE transactions
               SET status = 'reversed', created_at = '2026-07-30 01:00:00'
               WHERE id = ?""",
            (original_id,),
        )
        conn.execute(
            """UPDATE transactions
               SET status = 'confirmed', created_at = '2026-07-30 01:00:00'
               WHERE id = ?""",
            (reversal_id,),
        )

    assert [
        event.id for event in repo.list_transactions(product_id="fund")
    ] == [
        "00000000-0000-0000-0000-000000000000",
        reversal_id,
        original_id,
    ]
    assert PositionProjector(repo).calculate("fund") == Position(
        "fund",
        available_shares=Decimal("100"),
        locked_shares=Decimal("0"),
        total_shares=Decimal("100"),
        cost_basis=Decimal("100"),
    )


@pytest.mark.parametrize(
    ("reversal_shares", "reversal_amount"),
    [
        ("-99", "-1000"),
        ("-100", "-999"),
        ("-100", None),
    ],
)
def test_linked_reversal_rejects_partial_or_incomplete_values(
    repo, reversal_shares, reversal_amount
):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "original",
            "fund",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.REVERSED,
            "100",
            "1000",
        )
    )
    repo.create_transaction(
        tx(
            "reversal",
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.CONFIRMED,
            reversal_shares,
            reversal_amount,
            linked_transaction_id="original",
        )
    )

    with pytest.raises(ValueError, match="^invalid reversal values$"):
        PositionProjector(repo).calculate("fund")


def test_reversal_rejects_empty_link(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "reversal",
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.CONFIRMED,
            "-1",
            "-1",
        )
    )

    with pytest.raises(ValueError, match="^invalid reversal link$"):
        PositionProjector(repo).calculate("fund")


def test_reversal_rejects_nonempty_link_missing_from_snapshot(repo):
    repo.add_product(product("fund"))
    with repo.database.connection() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date,
                amount, shares, linked_transaction_id, idempotency_key,
                note, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "reversal",
                "fund",
                TransactionType.REVERSAL.value,
                TransactionStatus.CONFIRMED.value,
                date(2026, 7, 30).isoformat(),
                "-1",
                "-1",
                "missing",
                "test:reversal",
                "",
                "system",
            ),
        )

    with pytest.raises(ValueError, match="^invalid reversal link$"):
        PositionProjector(repo).calculate("fund")


def test_reversal_rejects_cross_product_link(repo):
    repo.add_product(product("a"))
    repo.add_product(product("b"))
    repo.create_transaction(
        tx(
            "original",
            "a",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.REVERSED,
            "1",
            "1",
        )
    )
    repo.create_transaction(
        tx(
            "reversal",
            "b",
            TransactionType.REVERSAL,
            TransactionStatus.CONFIRMED,
            "-1",
            "-1",
            linked_transaction_id="original",
        )
    )

    with pytest.raises(ValueError, match="^invalid reversal link$"):
        PositionProjector(repo).calculate("b")


@pytest.mark.parametrize(
    ("original_status", "reversal_status"),
    [
        (TransactionStatus.CONFIRMED, TransactionStatus.CONFIRMED),
        (TransactionStatus.REVERSED, TransactionStatus.REVERSED),
    ],
)
def test_reversal_rejects_inconsistent_pair_statuses(
    repo, original_status, reversal_status
):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "original",
            "fund",
            TransactionType.MANUAL_PURCHASE,
            original_status,
            "1",
            "1",
        )
    )
    repo.create_transaction(
        tx(
            "reversal",
            "fund",
            TransactionType.REVERSAL,
            reversal_status,
            "-1",
            "-1",
            linked_transaction_id="original",
        )
    )

    with pytest.raises(ValueError, match="^invalid reversal status$"):
        PositionProjector(repo).calculate("fund")


def test_reversal_rejects_duplicate_children_for_one_original(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "original",
            "fund",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.REVERSED,
            "1",
            "1",
        )
    )
    for reversal_id in ("reversal-a", "reversal-b"):
        repo.create_transaction(
            tx(
                reversal_id,
                "fund",
                TransactionType.REVERSAL,
                TransactionStatus.CONFIRMED,
                "-1",
                "-1",
                linked_transaction_id="original",
            )
        )

    with pytest.raises(ValueError, match="^duplicate reversal link$"):
        PositionProjector(repo).calculate("fund")


def test_reversal_of_reversal_pairs_from_confirmed_leaf_and_restores_root(repo):
    repo.add_product(product("fund"))
    events = [
        tx(
            "a",
            "fund",
            TransactionType.MANUAL_PURCHASE,
            TransactionStatus.REVERSED,
            "100",
            "100",
        ),
        tx(
            "r1",
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.REVERSED,
            "-100",
            "-100",
            linked_transaction_id="a",
        ),
        tx(
            "r2",
            "fund",
            TransactionType.REVERSAL,
            TransactionStatus.CONFIRMED,
            "100",
            "100",
            linked_transaction_id="r1",
        ),
    ]
    for event in events:
        repo.create_transaction(event)

    assert PositionProjector(repo).calculate("fund") == Position(
        "fund",
        available_shares=Decimal("100"),
        locked_shares=Decimal("0"),
        total_shares=Decimal("100"),
        cost_basis=Decimal("100"),
    )


def test_full_average_cost_outflow_has_no_decimal_rounding_residue(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "01-open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "3",
            "1",
        )
    )
    repo.create_transaction(
        tx(
            "02-redeem",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.CONFIRMED,
            "3",
            "99",
        )
    )

    position = PositionProjector(repo).calculate("fund")

    assert position.total_shares == Decimal("0")
    assert position.cost_basis == Decimal("0")


def test_projector_rejects_negative_total_shares(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "redeem",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.CONFIRMED,
            "1",
        )
    )

    with pytest.raises(ValueError, match="^negative portfolio shares$"):
        PositionProjector(repo).calculate("fund")


def test_projector_rejects_locks_above_total_shares(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "1",
            "1",
        )
    )
    repo.create_transaction(
        tx(
            "pending",
            "fund",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.PENDING_QUOTE,
            "1.0000000000000000001",
        )
    )

    with pytest.raises(ValueError, match="^locked shares exceed total shares$"):
        PositionProjector(repo).calculate("fund")


def test_rebuild_replaces_corrupt_projection_from_ledger(repo):
    repo.add_product(product("fund"))
    repo.create_transaction(
        tx(
            "open",
            "fund",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "100",
            "100",
        )
    )
    repo.replace_position(
        Position(
            "fund",
            Decimal("9"),
            Decimal("0"),
            Decimal("9"),
            Decimal("9"),
        )
    )

    rebuilt = PositionProjector(repo).rebuild("fund")

    assert rebuilt == [
        Position(
            "fund",
            available_shares=Decimal("100"),
            locked_shares=Decimal("0"),
            total_shares=Decimal("100"),
            cost_basis=Decimal("100"),
        )
    ]
    assert repo.get_position("fund") == rebuilt[0]


def test_rebuild_all_is_deterministic_and_persists_only_after_all_calculations(repo):
    repo.add_product(product("a-valid"))
    repo.add_product(product("b-invalid"))
    repo.create_transaction(
        tx(
            "valid-open",
            "a-valid",
            TransactionType.OPENING_POSITION,
            TransactionStatus.CONFIRMED,
            "10",
            "20",
        )
    )
    repo.create_transaction(
        tx(
            "invalid-redeem",
            "b-invalid",
            TransactionType.MANUAL_REDEMPTION,
            TransactionStatus.CONFIRMED,
            "1",
        )
    )
    corrupt = Position(
        "a-valid",
        available_shares=Decimal("9"),
        locked_shares=Decimal("0"),
        total_shares=Decimal("9"),
        cost_basis=Decimal("9"),
    )
    repo.replace_position(corrupt)

    with pytest.raises(ValueError, match="^negative portfolio shares$"):
        PositionProjector(repo).rebuild()

    assert repo.get_position("a-valid") == corrupt


def test_rebuild_rolls_back_if_second_position_write_fails(repo, monkeypatch):
    repo.add_product(product("a"))
    repo.add_product(product("b"))
    for product_id in ("a", "b"):
        repo.create_transaction(
            tx(
                f"{product_id}-open",
                product_id,
                TransactionType.OPENING_POSITION,
                TransactionStatus.CONFIRMED,
                "10",
                "20",
            )
        )
        repo.replace_position(
            Position(
                product_id,
                available_shares=Decimal("9"),
                locked_shares=Decimal("0"),
                total_shares=Decimal("9"),
                cost_basis=Decimal("9"),
            )
        )

    original_replace = repo.replace_position
    writes = 0

    def fail_second_write(position, conn=None):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("second position write failed")
        return original_replace(position, conn)

    monkeypatch.setattr(repo, "replace_position", fail_second_write)

    with pytest.raises(RuntimeError, match="^second position write failed$"):
        PositionProjector(repo).rebuild()

    assert repo.get_position("a").total_shares == Decimal("9")
    assert repo.get_position("b").total_shares == Decimal("9")


def test_rebuild_reads_one_protected_snapshot_before_atomic_persistence(
    repo, monkeypatch
):
    repo.add_product(product("a"))
    repo.add_product(product("b"))
    for product_id in ("a", "b"):
        repo.create_transaction(
            tx(
                f"{product_id}-open",
                product_id,
                TransactionType.OPENING_POSITION,
                TransactionStatus.CONFIRMED,
                "10",
                "10",
            )
        )

    first_product_read = threading.Event()
    release_rebuild = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    thread_errors = []
    original_list_transactions = repo.list_transactions

    def pause_after_first_product_read(
        product_id=None,
        statuses=None,
        conn=None,
    ):
        if conn is None:
            transactions = original_list_transactions(product_id, statuses)
        else:
            transactions = original_list_transactions(
                product_id, statuses, conn=conn
            )
        if product_id == "a" and not first_product_read.is_set():
            first_product_read.set()
            if not release_rebuild.wait(5):
                raise RuntimeError("timed out waiting to release rebuild")
        return transactions

    monkeypatch.setattr(
        repo, "list_transactions", pause_after_first_product_read
    )

    def rebuild_positions():
        try:
            PositionProjector(repo).rebuild()
        except BaseException as exc:
            thread_errors.append(exc)

    def append_concurrent_transaction():
        writer_started.set()
        try:
            repo.create_transaction(
                tx(
                    "b-concurrent",
                    "b",
                    TransactionType.MANUAL_PURCHASE,
                    TransactionStatus.CONFIRMED,
                    "1",
                    "1",
                )
            )
        except BaseException as exc:
            thread_errors.append(exc)
        finally:
            writer_done.set()

    rebuild_thread = threading.Thread(target=rebuild_positions)
    rebuild_thread.start()
    assert first_product_read.wait(5)

    writer_thread = threading.Thread(target=append_concurrent_transaction)
    writer_thread.start()
    assert writer_started.wait(5)
    writer_done.wait(0.5)
    release_rebuild.set()

    rebuild_thread.join(5)
    writer_thread.join(5)

    assert not rebuild_thread.is_alive()
    assert not writer_thread.is_alive()
    assert thread_errors == []
    assert repo.get_position("a").total_shares == Decimal("10")
    assert repo.get_position("b").total_shares == Decimal("10")
    assert PositionProjector(repo).calculate("b").total_shares == Decimal("11")
