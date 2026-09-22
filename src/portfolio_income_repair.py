"""Preview and atomically repair wallet income while preserving calibrations.

The normal scheduler stays idempotent. This explicit maintenance operation
requires an unchanged preview and keeps immutable original ledger entries.
"""
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

from src.beijing_time import now as beijing_now
from src.portfolio_db import PortfolioDatabase, SCHEMA_VERSION
from src.portfolio_income import CashIncomeService
from src.portfolio_models import Transaction, TransactionStatus as Status, TransactionType as Kind, decimal_text
from src.portfolio_positions import PositionProjector
from src.portfolio_profit import calculate_holding_profit
from src.portfolio_reconciliation import reconciled_transaction
from src.portfolio_repository import PortfolioRepository
from src.portfolio_transactions import PortfolioTransactionService


ZERO = Decimal("0")
WALLET = "wallet-plus"
ACTOR = "ops:wallet-income-repair"
ACTION = "repair_wallet_income"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    digest = hashlib.sha256()
    for table in tables:
        safe_table = '"' + table.replace('"', '""') + '"'
        data = sorted(_json(dict(r)) for r in conn.execute(f"SELECT * FROM {safe_table}"))
        digest.update(_json([table, data]).encode("utf-8"))
    return digest.hexdigest()


def _parameters(start, end):
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError("invalid repair date range")
    if end >= beijing_now().date():
        raise ValueError("wallet income repair must end before today")
    return {"product_id": WALLET, "start": start.isoformat(), "end": end.isoformat()}


def _validate_schema(conn):
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    if versions != {SCHEMA_VERSION}:
        raise ValueError("repair requires the current database schema")


def preview_wallet_income_repair(repository, start, end):
    parameters = _parameters(start, end)
    source_uri = f"{repository.database.path.resolve().as_uri()}?mode=ro"
    with TemporaryDirectory(prefix="wallet-income-preview-") as folder:
        copy_path = Path(folder) / "portfolio.db"
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(sqlite3.connect(copy_path)) as target:
                source.backup(target)
        copy = PortfolioRepository(PortfolioDatabase(copy_path))
        with copy.database.connection() as conn:
            _validate_schema(conn)
            fingerprint = _fingerprint(conn)
        operation_id = str(uuid5(NAMESPACE_URL, _json([ACTION, parameters, fingerprint])))
        with reconciled_transaction(copy, ACTION) as conn:
            report, _entries = _apply(copy, conn, start, end, operation_id)
        return {
            "schema_version": 1, "parameters": parameters,
            "fingerprint": fingerprint, "operation_id": operation_id, "report": report,
        }


def repair_wallet_income(repository, plan):
    if plan.get("schema_version") != 1:
        raise ValueError("unsupported repair plan")
    start = date.fromisoformat(plan["parameters"]["start"])
    end = date.fromisoformat(plan["parameters"]["end"])
    parameters = _parameters(start, end)
    if plan["parameters"] != parameters:
        raise ValueError("invalid repair parameters")
    operation_id = str(uuid5(NAMESPACE_URL, _json([ACTION, parameters, plan["fingerprint"]])))
    if operation_id != plan["operation_id"]:
        raise ValueError("invalid repair operation identity")
    with reconciled_transaction(repository, ACTION) as conn:
        _validate_schema(conn)
        previous = conn.execute("SELECT after_json FROM audit_logs WHERE id=? AND action=?", (operation_id, ACTION)).fetchone()
        if previous:
            completed = json.loads(previous["after_json"])
            if completed["plan"] != plan:
                raise ValueError("repair preview does not match completed operation")
            for expected in completed["entries"]:
                actual = repository.get_transaction_by_id(expected["id"], conn=conn)
                if actual is None or _entry_state(actual) != expected:
                    raise ValueError("completed repair entries changed")
            return {"status": "already_repaired", "report": completed["plan"]["report"]}
        if _fingerprint(conn) != plan["fingerprint"]:
            raise ValueError("database changed since repair preview")
        report, entries = _apply(repository, conn, start, end, operation_id)
        if report != plan["report"]:
            raise ValueError("repair results differ from preview")
        repository.append_audit(
            operation_id, ACTION, "product", WALLET,
            before_json=_json({"fingerprint": plan["fingerprint"], "parameters": parameters}),
            after_json=_json({"plan": plan, "entries": [_entry_state(t) for t in entries]}),
            source=ACTOR, conn=conn,
        )
        return {"status": "repaired", "report": report}


def _entry_state(tx):
    return {"id": tx.id, "product_id": tx.product_id, "kind": tx.transaction_type.value,
            "status": tx.status.value, "amount": decimal_text(tx.amount or ZERO),
            "shares": decimal_text(tx.shares or ZERO), "date": tx.trade_date.isoformat(),
            "idempotency_key": tx.idempotency_key}


def _calibrations(repository, conn, start, transactions):
    by_id = {t.id: t for t in transactions}
    anchors = []
    seen = set()
    for row in conn.execute(
        "SELECT action, after_json FROM audit_logs WHERE object_type='product' AND object_id=? "
        "AND action IN ('adjust_holding','adjust_holding_profit')", (WALLET,),
    ):
        audit = json.loads(row["after_json"])
        anchor = by_id.get(audit.get("adjustment_id"))
        if anchor is None:
            raise ValueError("calibration audit references missing transaction")
        if anchor.status is not Status.CONFIRMED or anchor.trade_date < start:
            continue
        expected_kind = Kind.HOLDING_ADJUSTMENT if row["action"] == "adjust_holding" else Kind.PROFIT_ADJUSTMENT
        if anchor.transaction_type is not expected_kind or not anchor.created_at:
            raise ValueError("calibration audit does not match ledger")
        target_key = "actual_shares" if expected_kind is Kind.HOLDING_ADJUSTMENT else "actual_profit"
        target = Decimal(audit[target_key])
        if not target.is_finite() or audit["effective_date"] != anchor.trade_date.isoformat():
            raise ValueError("invalid calibration target")
        if anchor.id in seen:
            raise ValueError("duplicate calibration audit")
        seen.add(anchor.id)
        anchors.append((anchor, target))
    if any(
        t.status is Status.CONFIRMED and t.trade_date >= start
        and t.transaction_type in {Kind.HOLDING_ADJUSTMENT, Kind.PROFIT_ADJUSTMENT}
        and t.created_by != ACTOR and t.id not in seen
        for t in transactions
    ):
        raise ValueError("missing calibration audit for an adjustment in the repair range")
    return sorted(anchors, key=lambda item: (item[0].trade_date, item[0].created_at, item[0].id))


def _apply(repository, conn, start, end, operation_id):
    for row in conn.execute("SELECT after_json FROM audit_logs WHERE action=? AND object_id=?", (ACTION, WALLET)):
        completed = json.loads(row["after_json"])["plan"]["parameters"]
        if date.fromisoformat(completed["start"]) <= end and start <= date.fromisoformat(completed["end"]):
            raise ValueError("income range already repaired; reuse the recorded plan for an idempotent retry")
    product = repository.require_product(WALLET, conn=conn)
    if product.provider != "wallet_plus":
        raise ValueError("repair product is not Wallet Plus")
    projector = PositionProjector(repository)
    before = projector._calculate(WALLET, conn)
    transactions = repository.list_transactions(WALLET, conn=conn)
    neutralized = projector._linked_reversal_pair_ids(transactions)
    incomes = [
        t for t in transactions
        if t.transaction_type is Kind.INCOME_ACCRUAL
        and t.status in {Status.CONFIRMED, Status.REVERSED}
        and t.id not in neutralized
    ]
    if any(t.trade_date > end for t in incomes):
        raise ValueError("later income exists; extend the repair through the latest income date")
    if any(start <= t.trade_date <= end and t.status is not Status.CONFIRMED for t in incomes):
        raise ValueError("restored income in repair range requires separate review")
    anchors = _calibrations(repository, conn, start, transactions)
    if any(
        income.trade_date == anchor.trade_date and income.created_at
        and income.created_at <= anchor.created_at
        for anchor, _ in anchors for income in incomes
    ):
        raise ValueError("same-day income before a calibration requires a separate review")
    if any(t.trade_date > beijing_now().date() for t, _ in anchors):
        raise ValueError("future calibration requires a separate review")
    original_by_day = {}
    for income in incomes:
        if start <= income.trade_date <= end:
            if income.trade_date in original_by_day:
                raise ValueError("duplicate confirmed income for repair date")
            CashIncomeService._require_matching_accrual(income, WALLET, income.trade_date)
            original_by_day[income.trade_date] = income
    events = [(start + timedelta(days=i), 1, None) for i in range((end - start).days + 1)]
    events += [(anchor.trade_date, 0, (anchor, target)) for anchor, target in anchors]
    events.sort(key=lambda item: (item[0], item[1], item[2][0].created_at if item[2] else ""))
    income_service = CashIncomeService(repository, projector)
    report = {"income": [], "calibrations": [], "wallet_before": decimal_text(before.total_shares)}
    new_amounts = {}
    corrections = {Kind.HOLDING_ADJUSTMENT: ZERO, Kind.PROFIT_ADJUSTMENT: ZERO}
    entries = []
    for day, _priority, anchor_item in events:
        if anchor_item:
            anchor, target = anchor_item
            difference = ZERO
            for income_day, new_amount in new_amounts.items():
                original = original_by_day.get(income_day)
                # The broken backfill happened after the user's calibration.
                # Only subtract old income already visible at that instant.
                old_visible = (
                    original.amount if original is not None and original.created_at
                    and original.created_at <= anchor.created_at else ZERO
                )
                difference += new_amount - old_visible
            correction = -(difference + corrections[anchor.transaction_type])
            if correction:
                key = f"{ACTION}:{operation_id}:calibration:{anchor.id}"
                tx = Transaction(
                    id=str(uuid5(NAMESPACE_URL, key)), product_id=WALLET,
                    transaction_type=anchor.transaction_type, status=Status.CONFIRMED,
                    trade_date=anchor.trade_date, trade_time=anchor.trade_time,
                    idempotency_key=key, amount=(max(correction, ZERO) if anchor.transaction_type is Kind.HOLDING_ADJUSTMENT else correction),
                    shares=correction if anchor.transaction_type is Kind.HOLDING_ADJUSTMENT else ZERO,
                    confirmation_date=anchor.confirmation_date,
                    confirmation_nav=Decimal("1") if anchor.transaction_type is Kind.HOLDING_ADJUSTMENT else None,
                    created_at=anchor.created_at, created_by=ACTOR,
                    note=f"收益重算后保留人工校准目标 {decimal_text(target)}；原记录 {anchor.id}",
                )
                entries.append(repository.create_transaction(tx, conn))
                corrections[anchor.transaction_type] += correction
            report["calibrations"].append({
                "original_id": anchor.id, "kind": anchor.transaction_type.value,
                "date": day.isoformat(), "target": decimal_text(target), "correction": decimal_text(correction),
            })
            continue
        original = original_by_day.get(day)
        if original:
            key = f"{ACTION}:{operation_id}:reverse:{original.id}"
            entries.append(repository.create_transaction(Transaction(
                id=str(uuid5(NAMESPACE_URL, key)), product_id=WALLET,
                transaction_type=Kind.REVERSAL, status=Status.CONFIRMED,
                trade_date=day, idempotency_key=key, amount=-original.amount, shares=-original.shares,
                confirmation_date=day, confirmation_nav=original.confirmation_nav,
                linked_transaction_id=original.id, created_by=ACTOR, note="修正钱包待确认资金扣减导致的收益异常",
            ), conn))
            PortfolioTransactionService._mark_reversed(original.id, conn)
        replacement = income_service.accrue(WALLET, day, conn=conn)
        if replacement is None:
            raise ValueError(f"missing wallet income quote for {day}")
        if original and replacement.id == original.id:
            raise ValueError("repair did not replace the original income")
        entries.append(replacement)
        new_amounts[day] = replacement.amount
        report["income"].append({
            "date": day.isoformat(), "before": decimal_text(original.amount if original else ZERO),
            "after": decimal_text(replacement.amount),
        })
    after = projector._calculate(WALLET, conn)
    repository.replace_position(after, conn)
    report["wallet_after"] = decimal_text(after.total_shares)
    report["wallet_change"] = decimal_text(after.total_shares - before.total_shares)
    report["holding_profit_after"] = decimal_text(calculate_holding_profit(repository, product, beijing_now().date(), conn=conn))
    return report, entries
