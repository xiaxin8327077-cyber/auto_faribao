from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    Product,
    ProductType,
    RedemptionSettlementStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repairs import (
    preview_known_wallet_outflow_repair,
    preview_known_redemption_repairs,
    repair_known_wallet_outflow,
    repair_known_redemptions,
)
from src.portfolio_repository import PortfolioRepository
from src.portfolio_transactions import PortfolioTransactionService


SOURCE_PRODUCT_ID = "721c838d-b670-58e4-9c02-f1fc95a8e80a"
WALLET_OUTFLOW_PURCHASE_ID = "df3b02a8-bccd-4f3a-9d39-51664b047e11"
WALLET_OUTFLOW_CASH_ID = "65c417e9-fdeb-4f6a-8187-137cc53cb8fb"
WALLET_OUTFLOW_TARGET_ID = "7bd7363d-bb41-5fda-8bda-9849ab26b5be"


def _build_known_anomaly_database(
    path,
    second_amount="54015",
    include_existing_purchase=True,
):
    database = PortfolioDatabase(path)
    database.initialize()
    repository = PortfolioRepository(database)
    repository.add_product(Product(
        SOURCE_PRODUCT_ID,
        "test",
        "known-wealth",
        "慧盈象固收增强一年持有期5号B",
        ProductType.WEALTH_NAV,
    ))
    repository.add_product(Product(
        "wallet-plus",
        "wallet_plus",
        "wallet-plus",
        "钱包Plus",
        ProductType.CASH_MANAGEMENT,
    ))
    repository.add_product(Product(
        "target-fund",
        "test",
        "target-fund",
        "东方添益债券",
        ProductType.PUBLIC_FUND,
    ))
    for product_id, amount in (
        (SOURCE_PRODUCT_ID, "150000"),
        ("wallet-plus", "1000"),
    ):
        repository.create_transaction(Transaction(
            id=f"opening:{product_id}",
            product_id=product_id,
            transaction_type=TransactionType.OPENING_POSITION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 9, 1),
            idempotency_key=f"opening:{product_id}",
            amount=Decimal(amount),
            shares=Decimal(amount),
        ))
    rows = (
        (
            "658e58e8-a483-4bd3-bed5-0d614f8e534b",
            "482b5541-24f8-45ce-aaab-273246980f16",
            date(2026, 9, 16),
            "2026-09-16T03:09:00",
            date(2026, 9, 17),
            "108000",
            "100000",
            "1.08",
            date(2026, 9, 18),
        ),
        (
            "eb4fc341-d594-43ef-a0ae-1bc930ea66f8",
            "46a53177-7de1-4341-859a-77c1b3efa6ee",
            date(2026, 9, 17),
            "2026-09-16T23:13:00",
            date(2026, 9, 18),
            second_amount,
            "50000",
            "1.0803",
            date(2026, 9, 20),
        ),
    )
    for (
        source_id,
        leg_id,
        trade_date,
        trade_time,
        confirmation_date,
        amount,
        shares,
        nav,
        settlement_date,
    ) in rows:
        source = repository.create_transaction(Transaction(
            id=source_id,
            product_id=SOURCE_PRODUCT_ID,
            transaction_type=TransactionType.MANUAL_REDEMPTION,
            status=TransactionStatus.PENDING_CONFIRMATION,
            trade_date=trade_date,
            trade_time=trade_time,
            confirmation_date=confirmation_date,
            idempotency_key=f"legacy:{source_id}",
            amount=Decimal(amount),
            shares=Decimal(shares),
            confirmation_nav=Decimal(nav),
            destination_cash_product_id="wallet-plus",
            settlement_date=settlement_date,
            settlement_status=RedemptionSettlementStatus.PENDING,
            created_by="web",
        ))
        repository.create_transaction(Transaction(
            id=leg_id,
            product_id="wallet-plus",
            transaction_type=TransactionType.CASH_TRANSFER_IN,
            status=TransactionStatus.CONFIRMED,
            trade_date=trade_date,
            trade_time=trade_time,
            confirmation_date=confirmation_date,
            idempotency_key=f"linked:{source_id}",
            amount=Decimal(amount),
            shares=Decimal(amount),
            confirmation_nav=Decimal("1"),
            linked_transaction_id=source_id,
            created_by="web",
        ))
        repository.update_pending_transaction(replace(
            source,
            status=TransactionStatus.CONFIRMED,
            linked_transaction_id=leg_id,
        ))
    if include_existing_purchase:
        PortfolioTransactionService(
            repository,
            PositionProjector(repository),
        ).record_purchase(
            "target-fund",
            Decimal("50000"),
            date(2026, 9, 18),
            "web:existing-fund-purchase",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T10:11:00",
        )
    PositionProjector(repository).rebuild()
    return database


def _build_known_wallet_outflow_database(path):
    database = PortfolioDatabase(path)
    database.initialize()
    repository = PortfolioRepository(database)
    repository.add_product(Product(
        "wallet-plus",
        "wallet_plus",
        "WALLETPLUS",
        "钱包Plus",
        ProductType.CASH_MANAGEMENT,
    ))
    repository.add_product(Product(
        WALLET_OUTFLOW_TARGET_ID,
        "fund",
        "400030",
        "东方添益债券",
        ProductType.PUBLIC_FUND,
    ))
    repository.create_transaction(Transaction(
        id="opening:wallet-plus",
        product_id="wallet-plus",
        transaction_type=TransactionType.OPENING_POSITION,
        status=TransactionStatus.CONFIRMED,
        trade_date=date(2026, 9, 1),
        idempotency_key="opening:wallet-plus",
        amount=Decimal("124551.8880198179443535014361"),
        shares=Decimal("124551.8880198179443535014361"),
    ))
    purchase = repository.create_transaction(Transaction(
        id=WALLET_OUTFLOW_PURCHASE_ID,
        product_id=WALLET_OUTFLOW_TARGET_ID,
        transaction_type=TransactionType.MANUAL_PURCHASE,
        status=TransactionStatus.PENDING_QUOTE,
        trade_date=date(2026, 9, 18),
        trade_time="2026-09-18T10:11:00",
        idempotency_key="1789697532937-8c71a3b2d0bff",
        amount=Decimal("50000"),
        fee_rate=Decimal("0.00008"),
        created_by="web",
    ))
    repository.create_transaction(Transaction(
        id=WALLET_OUTFLOW_CASH_ID,
        product_id="wallet-plus",
        transaction_type=TransactionType.CASH_TRANSFER_OUT,
        status=TransactionStatus.PENDING_QUOTE,
        trade_date=date(2026, 9, 18),
        trade_time="2026-09-18T10:11:00",
        idempotency_key=f"linked:{WALLET_OUTFLOW_PURCHASE_ID}",
        amount=Decimal("50000"),
        shares=Decimal("50000"),
        linked_transaction_id=WALLET_OUTFLOW_PURCHASE_ID,
        created_by="web",
    ))
    repository.update_pending_transaction(replace(
        purchase,
        linked_transaction_id=WALLET_OUTFLOW_CASH_ID,
    ))
    PositionProjector(repository).rebuild()
    return database


@pytest.fixture
def known_anomaly_database(tmp_path):
    return _build_known_anomaly_database(tmp_path / "known-anomaly.db")


def test_known_wallet_outflow_preview_is_read_only(tmp_path):
    database = _build_known_wallet_outflow_database(
        tmp_path / "known-wallet-outflow.db"
    )
    repository = PortfolioRepository(database)
    before = database.path.read_bytes()

    result = preview_known_wallet_outflow_repair(repository)

    assert result == {
        "purchase_id": WALLET_OUTFLOW_PURCHASE_ID,
        "cash_transaction_id": WALLET_OUTFLOW_CASH_ID,
        "amount": "50000",
        "result": "would_repair",
    }
    assert database.path.read_bytes() == before


def test_known_wallet_outflow_repair_confirms_cash_leg_and_is_idempotent(
    tmp_path,
):
    database = _build_known_wallet_outflow_database(
        tmp_path / "known-wallet-outflow.db"
    )
    repository = PortfolioRepository(database)

    first = repair_known_wallet_outflow(repository)
    second = repair_known_wallet_outflow(repository)

    assert first["result"] == "repaired"
    assert second["result"] == "already_repaired"
    cash = repository.get_transaction_by_id(WALLET_OUTFLOW_CASH_ID)
    assert cash.status is TransactionStatus.CONFIRMED
    assert cash.confirmation_nav == Decimal("1")
    assert cash.confirmation_date == date(2026, 9, 18)
    wallet = PositionProjector(repository).calculate("wallet-plus")
    assert wallet.total_shares == Decimal(
        "74551.8880198179443535014361"
    )
    assert wallet.available_shares == wallet.total_shares
    assert wallet.locked_shares == Decimal("0")


def test_preview_is_read_only_and_reports_exact_known_repairs(
    known_anomaly_database,
):
    repository = PortfolioRepository(known_anomaly_database)
    before = known_anomaly_database.path.read_bytes()

    preview = preview_known_redemption_repairs(
        repository,
        date(2026, 9, 18),
    )

    assert [
        (row["redemption_id"], row["amount"])
        for row in preview
    ] == [
        ("658e58e8-a483-4bd3-bed5-0d614f8e534b", "108000"),
        ("eb4fc341-d594-43ef-a0ae-1bc930ea66f8", "54015"),
    ]
    assert known_anomaly_database.path.read_bytes() == before


def test_repair_reverses_premature_wallet_credit_and_is_idempotent(
    known_anomaly_database,
):
    repository = PortfolioRepository(known_anomaly_database)
    projector = PositionProjector(repository)
    wallet_before = projector.calculate("wallet-plus").total_shares

    first = repair_known_redemptions(repository, date(2026, 9, 18))
    second = repair_known_redemptions(repository, date(2026, 9, 18))

    wallet = projector.calculate("wallet-plus")
    assert all(row["result"] == "repaired" for row in first)
    assert all(row["result"] == "already_repaired" for row in second)
    assert wallet.total_shares == wallet_before - Decimal("54015")
    assert wallet.locked_shares == Decimal("0")
    assert wallet.available_shares == Decimal("59000")
    assert (
        repository.find_purchase_by_origin(
            "658e58e8-a483-4bd3-bed5-0d614f8e534b"
        ).status
        is TransactionStatus.PENDING_CONFIRMATION
    )
    assert repository.find_purchase_by_origin(
        "eb4fc341-d594-43ef-a0ae-1bc930ea66f8"
    ) is None


def test_repair_aborts_all_rows_when_one_expected_amount_mismatches(tmp_path):
    database = _build_known_anomaly_database(
        tmp_path / "mismatch.db",
        second_amount="54016",
    )
    repository = PortfolioRepository(database)
    before = database.path.read_bytes()

    with pytest.raises(ValueError, match="does not match expected production data"):
        repair_known_redemptions(repository, date(2026, 9, 18))

    assert database.path.read_bytes() == before
    assert not [
        transaction
        for transaction in repository.list_transactions()
        if transaction.transaction_type is TransactionType.REVERSAL
    ]


def test_repair_cli_defaults_to_read_only_preview(known_anomaly_database):
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "repair_redemption_wallet_settlement.py"
    )
    before = known_anomaly_database.path.read_bytes()

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--database",
            str(known_anomaly_database.path),
            "--as-of",
            "2026-09-18",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert len(payload) == 2
    assert all(row["result"] == "would_repair" for row in payload)
    assert known_anomaly_database.path.read_bytes() == before


def test_repaired_redemption_can_reverse_with_pending_wallet_purchase(
    tmp_path,
):
    database = _build_known_anomaly_database(
        tmp_path / "unspent-anomaly.db",
        include_existing_purchase=False,
    )
    repository = PortfolioRepository(database)
    projector = PositionProjector(repository)
    repair_known_redemptions(repository, date(2026, 9, 18))
    transactions = PortfolioTransactionService(repository, projector)
    source_id = "658e58e8-a483-4bd3-bed5-0d614f8e534b"
    purchase = repository.find_purchase_by_origin(source_id)

    transactions.reverse_confirmed(
        source_id,
        "修正后冲正验证",
        "test:reverse-repaired-redemption",
    )

    assert (
        repository.get_transaction_by_id(source_id).status
        is TransactionStatus.REVERSED
    )
    assert (
        repository.get_transaction_by_id(purchase.id).status
        is TransactionStatus.CANCELLED
    )


def test_repaired_redemption_cannot_reverse_after_wallet_funds_are_used(
    known_anomaly_database,
):
    repository = PortfolioRepository(known_anomaly_database)
    projector = PositionProjector(repository)
    repair_known_redemptions(repository, date(2026, 9, 18))
    transactions = PortfolioTransactionService(repository, projector)
    source_id = "658e58e8-a483-4bd3-bed5-0d614f8e534b"
    purchase = repository.find_purchase_by_origin(source_id)

    with pytest.raises(ValueError, match="negative portfolio shares"):
        transactions.reverse_confirmed(
            source_id,
            "已使用资金不应直接冲正",
            "test:reject-used-redemption-reversal",
        )

    assert (
        repository.get_transaction_by_id(source_id).status
        is TransactionStatus.CONFIRMED
    )
    assert (
        repository.get_transaction_by_id(purchase.id).status
        is TransactionStatus.PENDING_CONFIRMATION
    )
