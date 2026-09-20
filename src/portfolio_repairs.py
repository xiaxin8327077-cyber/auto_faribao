from contextlib import closing
from dataclasses import replace
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
KNOWN_WALLET_OUTFLOW = {
    "purchase_id": "df3b02a8-bccd-4f3a-9d39-51664b047e11",
    "cash_transaction_id": "65c417e9-fdeb-4f6a-8187-137cc53cb8fb",
    "target_product_id": "7bd7363d-bb41-5fda-8bda-9849ab26b5be",
    "trade_date": date(2026, 9, 18),
    "trade_time": "2026-09-18T10:11:00",
    "amount": Decimal("50000"),
    "fee_rate": Decimal("0.00008"),
    "purchase_idempotency_key": "1789697532937-8c71a3b2d0bff",
}
KNOWN_WALLET_REDEMPTION = {
    "transaction_id": "7bcd665c-a888-4562-94de-da4f07d14b39",
    "product_id": WALLET_PRODUCT_ID,
    "wrong_trade_date": date(2026, 9, 21),
    "corrected_trade_date": date(2026, 9, 19),
    "trade_time": "2026-09-19T17:28:00",
    "amount": Decimal("200"),
    "shares": Decimal("200"),
    "confirmation_nav": Decimal("1"),
    "idempotency_key": "1789810095333-e9f9b14a3a2028",
    "created_at": "2026-09-19 09:28:33",
}
_WALLET_REDEMPTION_REPAIR_REASON = "修正钱包Plus周末赎回实时到账日期"


def preview_known_wallet_redemption_repair(repository) -> dict:
    database_uri = f"{repository.database.path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        _require_schema_v7(conn)
        state = _validate_known_wallet_redemption(repository, conn)
        return _wallet_redemption_report(
            "already_repaired" if state["already_repaired"] else "would_repair"
        )


def repair_known_wallet_redemption(repository) -> dict:
    projector = PositionProjector(repository)
    expected = KNOWN_WALLET_REDEMPTION
    original_id = expected["transaction_id"]
    reversal_id = str(
        uuid5(NAMESPACE_URL, f"repair-wallet-redemption-reversal:{original_id}")
    )
    corrected_id = str(
        uuid5(NAMESPACE_URL, f"repair-wallet-redemption-corrected:{original_id}")
    )
    with repository.database.transaction() as conn:
        state = _validate_known_wallet_redemption(repository, conn)
        if state["already_repaired"]:
            return _wallet_redemption_report("already_repaired")
        original = state["original"]
        reversal = repository.create_transaction(
            Transaction(
                id=reversal_id,
                product_id=original.product_id,
                transaction_type=TransactionType.REVERSAL,
                status=TransactionStatus.CONFIRMED,
                trade_date=original.trade_date,
                idempotency_key=f"repair-reversal:{original.id}",
                amount=-original.amount,
                shares=-original.shares,
                confirmation_nav=original.confirmation_nav,
                confirmation_date=original.confirmation_date,
                linked_transaction_id=original.id,
                note=_WALLET_REDEMPTION_REPAIR_REASON,
                created_by=REPAIR_ACTOR,
            ),
            conn,
        )
        if not _known_wallet_redemption_reversal_matches(
            reversal,
            original,
        ):
            raise ValueError(
                "known wallet redemption does not match production data"
            )
        PortfolioTransactionService._mark_reversed(original.id, conn)
        corrected = repository.create_transaction(
            Transaction(
                id=corrected_id,
                product_id=original.product_id,
                transaction_type=TransactionType.MANUAL_REDEMPTION,
                status=TransactionStatus.CONFIRMED,
                trade_date=expected["corrected_trade_date"],
                trade_time=expected["trade_time"],
                idempotency_key=(
                    f"repair:wallet-plus-redemption:{original.id}"
                ),
                amount=expected["amount"],
                shares=expected["shares"],
                confirmation_nav=expected["confirmation_nav"],
                confirmation_date=expected["corrected_trade_date"],
                settlement_date=expected["corrected_trade_date"],
                settlement_status=RedemptionSettlementStatus.SETTLED,
                settled_at=expected["created_at"],
                note=original.note,
                created_by=original.created_by,
            ),
            conn,
        )
        if not _known_wallet_redemption_corrected_matches(corrected):
            raise ValueError(
                "known wallet redemption does not match production data"
            )
        repository.replace_position(
            projector._calculate(WALLET_PRODUCT_ID, conn),
            conn,
        )
        repository.append_audit(
            str(uuid5(NAMESPACE_URL, f"repair-wallet-redemption:{original.id}")),
            "repair_wallet_realtime_redemption",
            "transaction",
            original.id,
            before_json=json.dumps(
                {
                    "confirmation_date": original.confirmation_date.isoformat(),
                    "settlement_date": original.settlement_date.isoformat(),
                    "settlement_status": None,
                    "status": original.status.value,
                    "trade_date": original.trade_date.isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            after_json=json.dumps(
                {
                    "corrected_transaction_id": corrected_id,
                    "reversal_transaction_id": reversal_id,
                    "settlement_date": expected[
                        "corrected_trade_date"
                    ].isoformat(),
                    "settlement_status": (
                        RedemptionSettlementStatus.SETTLED.value
                    ),
                    "trade_date": expected[
                        "corrected_trade_date"
                    ].isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            source=REPAIR_ACTOR,
            conn=conn,
        )
    return _wallet_redemption_report("repaired")


def _validate_known_wallet_redemption(repository, conn) -> dict:
    expected = KNOWN_WALLET_REDEMPTION
    original_id = expected["transaction_id"]
    original = repository.get_transaction_by_id(original_id, conn=conn)
    reversal_id = str(
        uuid5(
            NAMESPACE_URL,
            f"repair-wallet-redemption-reversal:{original_id}",
        )
    )
    corrected_id = str(
        uuid5(
            NAMESPACE_URL,
            f"repair-wallet-redemption-corrected:{original_id}",
        )
    )
    audit_id = str(
        uuid5(NAMESPACE_URL, f"repair-wallet-redemption:{original_id}")
    )
    reversal = repository.get_transaction_by_id(
        reversal_id,
        conn=conn,
    )
    reversal_by_key = repository.get_transaction_by_idempotency(
        f"repair-reversal:{original_id}",
        conn=conn,
    )
    corrected = repository.get_transaction_by_id(
        corrected_id,
        conn=conn,
    )
    corrected_by_key = repository.get_transaction_by_idempotency(
        f"repair:wallet-plus-redemption:{original_id}",
        conn=conn,
    )
    audit = conn.execute(
        """SELECT id, action, object_type, object_id, before_json,
                  after_json, result, source
           FROM audit_logs
           WHERE id = ?""",
        (audit_id,),
    ).fetchone()
    base_expected = {
        "product_id": expected["product_id"],
        "transaction_type": TransactionType.MANUAL_REDEMPTION,
        "trade_date": expected["wrong_trade_date"],
        "trade_time": expected["trade_time"],
        "amount": expected["amount"],
        "shares": expected["shares"],
        "fee_amount": None,
        "fee_rate": None,
        "confirmation_nav": expected["confirmation_nav"],
        "confirmation_date": expected["wrong_trade_date"],
        "linked_transaction_id": "",
        "plan_id": "",
        "idempotency_key": expected["idempotency_key"],
        "note": "",
        "created_by": "web",
        "settlement_date": expected["wrong_trade_date"],
        "destination_cash_product_id": "",
        "origin_transaction_id": "",
        "settlement_status": None,
        "settled_at": None,
        "created_at": expected["created_at"],
    }
    if original is None or any(
        getattr(original, name) != value
        for name, value in base_expected.items()
    ):
        raise ValueError(
            "known wallet redemption does not match production data"
        )
    if (
        original.status is TransactionStatus.CONFIRMED
        and reversal is None
        and reversal_by_key is None
        and corrected is None
        and corrected_by_key is None
        and audit is None
    ):
        return {"already_repaired": False, "original": original}
    if (
        original.status is not TransactionStatus.REVERSED
        or reversal is None
        or reversal_by_key is None
        or reversal.id != reversal_by_key.id
        or corrected is None
        or corrected_by_key is None
        or corrected.id != corrected_by_key.id
        or not _known_wallet_redemption_reversal_matches(
            reversal,
            original,
        )
        or not _known_wallet_redemption_corrected_matches(corrected)
        or not _known_wallet_redemption_audit_matches(audit)
    ):
        raise ValueError(
            "known wallet redemption does not match production data"
        )
    return {"already_repaired": True, "original": original}


def _known_wallet_redemption_reversal_matches(reversal, original) -> bool:
    expected = KNOWN_WALLET_REDEMPTION
    expected_id = str(
        uuid5(
            NAMESPACE_URL,
            f"repair-wallet-redemption-reversal:{original.id}",
        )
    )
    return (
        reversal.id == expected_id
        and reversal.product_id == expected["product_id"]
        and reversal.transaction_type is TransactionType.REVERSAL
        and reversal.status is TransactionStatus.CONFIRMED
        and reversal.trade_date == expected["wrong_trade_date"]
        and reversal.amount == -expected["amount"]
        and reversal.shares == -expected["shares"]
        and reversal.confirmation_nav == expected["confirmation_nav"]
        and reversal.confirmation_date == expected["wrong_trade_date"]
        and reversal.linked_transaction_id == original.id
        and reversal.idempotency_key == f"repair-reversal:{original.id}"
        and reversal.note == _WALLET_REDEMPTION_REPAIR_REASON
        and reversal.created_by == REPAIR_ACTOR
    )


def _known_wallet_redemption_corrected_matches(corrected) -> bool:
    expected = KNOWN_WALLET_REDEMPTION
    original_id = expected["transaction_id"]
    expected_id = str(
        uuid5(
            NAMESPACE_URL,
            f"repair-wallet-redemption-corrected:{original_id}",
        )
    )
    return (
        corrected.id == expected_id
        and corrected.product_id == expected["product_id"]
        and corrected.transaction_type
        is TransactionType.MANUAL_REDEMPTION
        and corrected.status is TransactionStatus.CONFIRMED
        and corrected.trade_date == expected["corrected_trade_date"]
        and corrected.trade_time == expected["trade_time"]
        and corrected.amount == expected["amount"]
        and corrected.shares == expected["shares"]
        and corrected.fee_amount is None
        and corrected.fee_rate is None
        and corrected.confirmation_nav == expected["confirmation_nav"]
        and corrected.confirmation_date == expected["corrected_trade_date"]
        and corrected.settlement_date == expected["corrected_trade_date"]
        and corrected.settlement_status
        is RedemptionSettlementStatus.SETTLED
        and corrected.settled_at == expected["created_at"]
        and corrected.idempotency_key
        == f"repair:wallet-plus-redemption:{original_id}"
        and corrected.linked_transaction_id == ""
        and corrected.plan_id == ""
        and corrected.note == ""
        and corrected.created_by == "web"
        and corrected.destination_cash_product_id == ""
        and corrected.origin_transaction_id == ""
    )


def _known_wallet_redemption_audit_matches(audit) -> bool:
    if audit is None:
        return False
    expected = KNOWN_WALLET_REDEMPTION
    original_id = expected["transaction_id"]
    reversal_id = str(
        uuid5(
            NAMESPACE_URL,
            f"repair-wallet-redemption-reversal:{original_id}",
        )
    )
    corrected_id = str(
        uuid5(
            NAMESPACE_URL,
            f"repair-wallet-redemption-corrected:{original_id}",
        )
    )
    try:
        before = json.loads(audit["before_json"])
        after = json.loads(audit["after_json"])
    except (json.JSONDecodeError, TypeError):
        return False
    return (
        audit["id"]
        == str(uuid5(NAMESPACE_URL, f"repair-wallet-redemption:{original_id}"))
        and audit["action"] == "repair_wallet_realtime_redemption"
        and audit["object_type"] == "transaction"
        and audit["object_id"] == original_id
        and audit["result"] == "success"
        and audit["source"] == REPAIR_ACTOR
        and before
        == {
            "confirmation_date": expected["wrong_trade_date"].isoformat(),
            "settlement_date": expected["wrong_trade_date"].isoformat(),
            "settlement_status": None,
            "status": TransactionStatus.CONFIRMED.value,
            "trade_date": expected["wrong_trade_date"].isoformat(),
        }
        and after
        == {
            "corrected_transaction_id": corrected_id,
            "reversal_transaction_id": reversal_id,
            "settlement_date": expected["corrected_trade_date"].isoformat(),
            "settlement_status": RedemptionSettlementStatus.SETTLED.value,
            "trade_date": expected["corrected_trade_date"].isoformat(),
        }
    )


def _wallet_redemption_report(result) -> dict:
    return {
        "original_transaction_id": KNOWN_WALLET_REDEMPTION["transaction_id"],
        "amount": decimal_text(KNOWN_WALLET_REDEMPTION["amount"]),
        "corrected_trade_date": KNOWN_WALLET_REDEMPTION[
            "corrected_trade_date"
        ].isoformat(),
        "result": result,
    }


def preview_known_wallet_outflow_repair(repository) -> dict:
    database_uri = f"{repository.database.path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        _require_schema_v7(conn)
        state = _validate_known_wallet_outflow(repository, conn)
        return _wallet_outflow_report(
            "already_repaired" if state["already_repaired"] else "would_repair"
        )


def repair_known_wallet_outflow(repository) -> dict:
    projector = PositionProjector(repository)
    with repository.database.transaction() as conn:
        state = _validate_known_wallet_outflow(repository, conn)
        if state["already_repaired"]:
            return _wallet_outflow_report("already_repaired")
        cash = state["cash"]
        repository.update_pending_transaction(
            replace(
                cash,
                status=TransactionStatus.CONFIRMED,
                confirmation_nav=Decimal("1"),
                confirmation_date=cash.trade_date,
            ),
            conn,
        )
        repository.replace_position(
            projector._calculate(WALLET_PRODUCT_ID, conn),
            conn,
        )
        repository.append_audit(
            str(uuid5(NAMESPACE_URL, "repair-wallet-realtime-outflow-v1")),
            "repair_wallet_realtime_outflow",
            "transaction",
            cash.id,
            before_json=json.dumps(
                {
                    "confirmation_date": None,
                    "confirmation_nav": None,
                    "status": TransactionStatus.PENDING_QUOTE.value,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            after_json=json.dumps(
                {
                    "confirmation_date": cash.trade_date.isoformat(),
                    "confirmation_nav": "1",
                    "status": TransactionStatus.CONFIRMED.value,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            source=REPAIR_ACTOR,
            conn=conn,
        )
    return _wallet_outflow_report("repaired")


def _require_schema_v7(conn) -> None:
    version_row = conn.execute(
        "SELECT version FROM schema_migrations"
    ).fetchone()
    if version_row is None or version_row[0] != 7:
        raise ValueError("portfolio database must be schema version 7")


def _validate_known_wallet_outflow(repository, conn) -> dict:
    expected = KNOWN_WALLET_OUTFLOW
    purchase = repository.get_transaction_by_id(
        expected["purchase_id"],
        conn=conn,
    )
    cash = repository.get_transaction_by_id(
        expected["cash_transaction_id"],
        conn=conn,
    )
    if purchase is None or cash is None:
        raise ValueError("known wallet outflow does not match production data")
    purchase_expected = {
        "product_id": expected["target_product_id"],
        "transaction_type": TransactionType.MANUAL_PURCHASE,
        "status": TransactionStatus.PENDING_QUOTE,
        "trade_date": expected["trade_date"],
        "trade_time": expected["trade_time"],
        "amount": expected["amount"],
        "shares": None,
        "fee_amount": None,
        "fee_rate": expected["fee_rate"],
        "confirmation_nav": None,
        "confirmation_date": None,
        "linked_transaction_id": expected["cash_transaction_id"],
        "plan_id": "",
        "idempotency_key": expected["purchase_idempotency_key"],
        "created_by": "web",
    }
    cash_expected = {
        "product_id": WALLET_PRODUCT_ID,
        "transaction_type": TransactionType.CASH_TRANSFER_OUT,
        "trade_date": expected["trade_date"],
        "trade_time": expected["trade_time"],
        "amount": expected["amount"],
        "shares": expected["amount"],
        "fee_amount": None,
        "fee_rate": None,
        "linked_transaction_id": expected["purchase_id"],
        "plan_id": "",
        "idempotency_key": f"linked:{expected['purchase_id']}",
        "created_by": "web",
    }
    if any(
        getattr(purchase, name) != value
        for name, value in purchase_expected.items()
    ) or any(
        getattr(cash, name) != value
        for name, value in cash_expected.items()
    ):
        raise ValueError("known wallet outflow does not match production data")
    if (
        cash.status is TransactionStatus.PENDING_QUOTE
        and cash.confirmation_nav is None
        and cash.confirmation_date is None
    ):
        already_repaired = False
    elif (
        cash.status is TransactionStatus.CONFIRMED
        and cash.confirmation_nav == Decimal("1")
        and cash.confirmation_date == cash.trade_date
    ):
        already_repaired = True
    else:
        raise ValueError("known wallet outflow does not match production data")
    return {
        "already_repaired": already_repaired,
        "cash": cash,
        "purchase": purchase,
    }


def _wallet_outflow_report(result) -> dict:
    return {
        "purchase_id": KNOWN_WALLET_OUTFLOW["purchase_id"],
        "cash_transaction_id": KNOWN_WALLET_OUTFLOW[
            "cash_transaction_id"
        ],
        "amount": decimal_text(KNOWN_WALLET_OUTFLOW["amount"]),
        "result": result,
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
    transaction_service = PortfolioTransactionService(repository, projector)
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
        settled = False
        for source_id in states:
            source = repository.get_transaction_by_id(source_id, conn=conn)
            if (
                source.settlement_status
                is RedemptionSettlementStatus.PENDING
                and source.settlement_date is not None
                and source.settlement_date <= as_of_date
            ):
                transaction_service._settle_redemption_in_transaction(
                    source_id,
                    as_of_date,
                    conn,
                )
                settled = True
        if changed or settled:
            for product_id in (SOURCE_PRODUCT_ID, WALLET_PRODUCT_ID):
                repository.replace_position(
                    projector._calculate(product_id, conn),
                    conn,
                )

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
