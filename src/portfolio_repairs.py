from contextlib import closing
from datetime import date
from decimal import Decimal
import json
import sqlite3
from uuid import NAMESPACE_URL, uuid5

from src.portfolio_models import (
    RedemptionSettlementStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
    decimal_text,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_transactions import PortfolioTransactionService


REPAIR_ACTOR = "ops:redemption-settlement-repair"
SOURCE_PRODUCT_ID = "721c838d-b670-58e4-9c02-f1fc95a8e80a"
WALLET_PRODUCT_ID = "wallet-plus"
KNOWN_REDEMPTIONS = {
    "658e58e8-a483-4bd3-bed5-0d614f8e534b": {
        "product_id": SOURCE_PRODUCT_ID,
        "legacy_cash_transaction_id": (
            "482b5541-24f8-45ce-aaab-273246980f16"
        ),
        "trade_date": date(2026, 9, 16),
        "trade_time": "2026-09-16T03:09:00",
        "shares": Decimal("100000"),
        "amount": Decimal("108000"),
        "confirmation_nav": Decimal("1.08"),
        "confirmation_date": date(2026, 9, 17),
        "settlement_date": date(2026, 9, 18),
    },
    "eb4fc341-d594-43ef-a0ae-1bc930ea66f8": {
        "product_id": SOURCE_PRODUCT_ID,
        "legacy_cash_transaction_id": (
            "46a53177-7de1-4341-859a-77c1b3efa6ee"
        ),
        "trade_date": date(2026, 9, 17),
        "trade_time": "2026-09-16T23:13:00",
        "shares": Decimal("50000"),
        "amount": Decimal("54015"),
        "confirmation_nav": Decimal("1.0803"),
        "confirmation_date": date(2026, 9, 18),
        "settlement_date": date(2026, 9, 20),
    },
}


def preview_known_redemption_repairs(repository, as_of_date) -> list[dict]:
    database_uri = f"{repository.database.path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        version_row = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchone()
        if version_row is None or version_row[0] != 7:
            raise ValueError("portfolio database must be schema version 7")
        states = _validated_states(repository, conn)
        return [
            _report_row(
                source_id,
                state,
                as_of_date,
                result=(
                    "already_repaired"
                    if state["already_repaired"]
                    else "would_repair"
                ),
            )
            for source_id, state in states.items()
        ]


def repair_known_redemptions(repository, as_of_date) -> list[dict]:
    projector = PositionProjector(repository)
    with repository.database.transaction() as conn:
        states = _validated_states(repository, conn)
        results = []
        changed = False
        for source_id, state in states.items():
            if state["already_repaired"]:
                result = "already_repaired"
            else:
                _repair_one(
                    repository,
                    source_id,
                    state,
                    conn,
                )
                result = "repaired"
                changed = True
            results.append(
                _report_row(
                    source_id,
                    state,
                    as_of_date,
                    result=result,
                )
            )
        if changed:
            for product_id in (SOURCE_PRODUCT_ID, WALLET_PRODUCT_ID):
                repository.replace_position(
                    projector._calculate(product_id, conn),
                    conn,
                )

    transaction_service = PortfolioTransactionService(repository, projector)
    transaction_service.settle_redemptions(as_of_date)
    return results


def _validated_states(repository, conn) -> dict[str, dict]:
    return {
        source_id: _validate_state(repository, source_id, expected, conn)
        for source_id, expected in KNOWN_REDEMPTIONS.items()
    }


def _validate_state(repository, source_id, expected, conn) -> dict:
    source = repository.get_transaction_by_id(source_id, conn=conn)
    leg = repository.get_transaction_by_id(
        expected["legacy_cash_transaction_id"],
        conn=conn,
    )
    if source is None or leg is None:
        _mismatch(source_id)
    source_expected = {
        "product_id": expected["product_id"],
        "transaction_type": TransactionType.MANUAL_REDEMPTION,
        "status": TransactionStatus.CONFIRMED,
        "trade_date": expected["trade_date"],
        "trade_time": expected["trade_time"],
        "confirmation_date": expected["confirmation_date"],
        "amount": expected["amount"],
        "shares": expected["shares"],
        "confirmation_nav": expected["confirmation_nav"],
        "settlement_date": expected["settlement_date"],
        "destination_cash_product_id": WALLET_PRODUCT_ID,
        "created_by": "web",
    }
    leg_expected = {
        "product_id": WALLET_PRODUCT_ID,
        "transaction_type": TransactionType.CASH_TRANSFER_IN,
        "trade_date": expected["trade_date"],
        "trade_time": expected["trade_time"],
        "confirmation_date": expected["confirmation_date"],
        "amount": expected["amount"],
        "shares": expected["amount"],
        "confirmation_nav": Decimal("1"),
        "linked_transaction_id": source_id,
        "created_by": "web",
    }
    if any(
        getattr(source, name) != value
        for name, value in source_expected.items()
    ) or any(
        getattr(leg, name) != value
        for name, value in leg_expected.items()
    ):
        _mismatch(source_id)
    if source.settlement_status not in {
        RedemptionSettlementStatus.PENDING,
        RedemptionSettlementStatus.SETTLED,
    }:
        _mismatch(source_id)

    if (
        source.linked_transaction_id
        == expected["legacy_cash_transaction_id"]
        and leg.status is TransactionStatus.CONFIRMED
    ):
        if repository.find_purchase_by_origin(source_id, conn=conn) is not None:
            _mismatch(source_id)
        return {
            "already_repaired": False,
            "source": source,
            "leg": leg,
            "expected": expected,
        }

    if (
        source.linked_transaction_id == ""
        and leg.status is TransactionStatus.REVERSED
    ):
        reversal = _reversal_child(repository, leg.id, conn)
        if (
            reversal is None
            or reversal.status is not TransactionStatus.CONFIRMED
            or reversal.product_id != WALLET_PRODUCT_ID
            or reversal.amount != -expected["amount"]
            or reversal.shares != -expected["amount"]
        ):
            _mismatch(source_id)
        return {
            "already_repaired": True,
            "source": source,
            "leg": leg,
            "expected": expected,
        }
    _mismatch(source_id)


def _repair_one(repository, source_id, state, conn) -> None:
    source = state["source"]
    leg = state["leg"]
    expected = state["expected"]
    reversal = Transaction(
        id=str(uuid5(NAMESPACE_URL, f"repair-redemption-wallet:{leg.id}")),
        product_id=leg.product_id,
        transaction_type=TransactionType.REVERSAL,
        status=TransactionStatus.CONFIRMED,
        trade_date=leg.trade_date,
        idempotency_key=f"repair:redemption-wallet:{source_id}",
        amount=-leg.amount,
        shares=-leg.shares,
        confirmation_nav=leg.confirmation_nav,
        confirmation_date=leg.confirmation_date,
        linked_transaction_id=leg.id,
        note="冲正赎回提前计入钱包Plus的资金",
        created_by=REPAIR_ACTOR,
    )
    reversal = repository.create_transaction(reversal, conn)
    conn.execute(
        """UPDATE transactions
           SET status = 'reversed',
               reversed_at = CURRENT_TIMESTAMP,
               status_updated_at = CURRENT_TIMESTAMP
           WHERE id = ? AND status = 'confirmed'""",
        (leg.id,),
    )
    cursor = conn.execute(
        """UPDATE transactions
           SET linked_transaction_id = NULL,
               destination_cash_product_id = ?,
               settlement_status = 'pending',
               status_updated_at = CURRENT_TIMESTAMP
           WHERE id = ?
             AND status = 'confirmed'
             AND transaction_type = 'manual_redemption'""",
        (WALLET_PRODUCT_ID, source.id),
    )
    if cursor.rowcount != 1:
        raise ValueError("known redemption repair update failed")
    audit_id = str(
        uuid5(NAMESPACE_URL, f"repair-redemption-settlement:{source_id}")
    )
    repository.append_audit(
        audit_id,
        "repair_redemption_wallet_settlement",
        "transaction",
        source_id,
        before_json=json.dumps(
            {
                "legacy_cash_transaction_id": leg.id,
                "legacy_cash_status": leg.status.value,
                "linked_transaction_id": source.linked_transaction_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        after_json=json.dumps(
            {
                "destination_cash_product_id": WALLET_PRODUCT_ID,
                "legacy_cash_status": TransactionStatus.REVERSED.value,
                "reversal_id": reversal.id,
                "settlement_status": (
                    RedemptionSettlementStatus.PENDING.value
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        source=REPAIR_ACTOR,
        conn=conn,
    )


def _reversal_child(repository, transaction_id, conn):
    children = [
        transaction
        for transaction in repository.list_transactions(conn=conn)
        if (
            transaction.transaction_type is TransactionType.REVERSAL
            and transaction.linked_transaction_id == transaction_id
        )
    ]
    return children[0] if len(children) == 1 else None


def _report_row(source_id, state, as_of_date, *, result) -> dict:
    expected = state["expected"]
    due = expected["settlement_date"] <= as_of_date
    return {
        "redemption_id": source_id,
        "legacy_cash_transaction_id": expected[
            "legacy_cash_transaction_id"
        ],
        "amount": decimal_text(expected["amount"]),
        "settlement_date": expected["settlement_date"].isoformat(),
        "target": (
            "wallet_purchase_pending_confirmation"
            if due
            else "pending_settlement"
        ),
        "result": result,
    }


def _mismatch(source_id):
    raise ValueError(
        f"redemption {source_id} does not match expected production data"
    )
