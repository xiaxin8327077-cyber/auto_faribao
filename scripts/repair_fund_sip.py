"""Repair only the three user-verified 400030 SIP confirmations (2026-11-04).

Default: preview on a SQLite backup. Execution requires the unchanged preview,
backs up the live database, and retains original orders with paired reversals.
This maintenance operation does not infer bank confirmations from public NAVs.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import Transaction, TransactionStatus as Status, TransactionType as Kind, decimal_text
from src.portfolio_positions import PositionProjector
from src.portfolio_reconciliation import reconciled_transaction
from src.portfolio_repository import PortfolioRepository
from src.portfolio_sip import SipService
from src.portfolio_transactions import PortfolioTransactionService


ACTION = "repair_verified_400030_sip_20261104"
ACTOR = "ops:verified-bank-sip"
OPERATION_ID = str(uuid5(NAMESPACE_URL, ACTION))
PLAN_ID = "1edaa8a0-1382-58ce-80f4-32c44c68e816"
AMOUNT = Decimal("1000")
FEE_RATE = Decimal("0.00008")
# Trade date, bank confirmation date, original ledger amount, trade-day NAV.
ROWS = (
    (date(2026, 10, 30), date(2026, 11, 2), Decimal("3456"), Decimal("1.4601")),
    (date(2026, 11, 2), date(2026, 11, 3), None, Decimal("1.4595")),
    (date(2026, 11, 3), date(2026, 11, 4), Decimal("2222"), Decimal("1.4625")),
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(day, leg):
    return str(uuid5(NAMESPACE_URL, f"{ACTION}:{day.isoformat()}:{leg}"))


def _fingerprint(conn):
    digest = hashlib.sha256()
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        name = row[0]
        quoted = '"' + name.replace('"', '""') + '"'
        rows = sorted(_json(dict(item)) for item in conn.execute(f"SELECT * FROM {quoted}"))
        digest.update(_json([name, rows]).encode("utf-8"))
    return digest.hexdigest()


def _copy_database(source_path, target_path):
    uri = f"{Path(source_path).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source:
        with closing(sqlite3.connect(target_path)) as target:
            source.backup(target)


class BoundDatabase:
    """Keep service reversals and replacements in one atomic transaction."""

    def __init__(self, database):
        self.database = database
        self.bound = None
        self.sequence = 0

    @contextmanager
    def connection(self):
        if self.bound is not None:
            yield self.bound
        else:
            with self.database.connection() as conn:
                yield conn

    @contextmanager
    def transaction(self):
        if self.bound is None:
            with self.database.transaction() as conn:
                self.bound = conn
                try:
                    yield conn
                finally:
                    self.bound = None
            return
        self.sequence += 1
        name = f"verified_sip_{self.sequence}"
        self.bound.execute(f"SAVEPOINT {name}")
        try:
            yield self.bound
        except BaseException:
            self.bound.execute(f"ROLLBACK TO {name}")
            self.bound.execute(f"RELEASE {name}")
            raise
        else:
            self.bound.execute(f"RELEASE {name}")


def _snapshot(repo, product_id, conn):
    projector = PositionProjector(repo)
    fund = projector._calculate(product_id, conn)
    wallet = projector._calculate("wallet-plus", conn)
    return {"wallet": decimal_text(wallet.total_shares), "fund_shares": decimal_text(fund.total_shares),
            "fund_cost_basis": decimal_text(fund.cost_basis)}


def _validate_completed(repo, report, conn):
    neutralized = PositionProjector._linked_reversal_pair_ids(repo.list_transactions(conn=conn))
    for day, confirmed, old_amount, nav in ROWS:
        execution = repo.get_plan_execution(PLAN_ID, day, conn=conn)
        buy = repo.get_transaction_by_id(_id(day, "purchase"), conn=conn)
        cash = repo.get_transaction_by_id(_id(day, "cash"), conn=conn)
        if (buy is None or cash is None or execution is None
            or execution.status != "confirmed" or execution.transaction_id != buy.id
            or buy.transaction_type is not Kind.SIP_PURCHASE or buy.status is not Status.CONFIRMED
            or buy.product_id != report["product_id"] or buy.plan_id != PLAN_ID
            or buy.trade_date != day or buy.confirmation_date != confirmed or buy.confirmation_nav != nav
            or buy.amount != AMOUNT or buy.fee_rate != FEE_RATE or buy.fee_amount != Decimal("0.08")
            or buy.shares != (AMOUNT - Decimal("0.08")) / nav
            or cash.status is not Status.CONFIRMED or cash.transaction_type is not Kind.CASH_TRANSFER_OUT
            or cash.product_id != "wallet-plus" or cash.amount != AMOUNT or cash.shares != AMOUNT
            or cash.trade_date != day or cash.confirmation_date != day or cash.confirmation_nav != Decimal("1")
            or cash.plan_id != PLAN_ID or buy.linked_transaction_id != cash.id or cash.linked_transaction_id != buy.id):
            raise ValueError("completed repair records have changed; refusing to recreate money")
    for tx_id in report["reversed_original_ids"]:
        original = repo.get_transaction_by_id(tx_id, conn=conn)
        if original is None or original.status is not Status.REVERSED or tx_id not in neutralized:
            raise ValueError("repair reversal is missing or no longer neutralized")


def _create_confirmed_pair(repo, plan, day, confirmed, nav, conn):
    buy_id, cash_id = _id(day, "purchase"), _id(day, "cash")
    note = "用户核实银行实际成交1000元；2026-11-04修复定投记录"
    purchase = repo.create_transaction(Transaction(
        id=buy_id, product_id=plan.product_id, transaction_type=Kind.SIP_PURCHASE,
        status=Status.PENDING_QUOTE, trade_date=day, idempotency_key=f"{ACTION}:{day}:purchase",
        amount=AMOUNT, fee_rate=FEE_RATE, plan_id=plan.id, note=note, created_by="sip",
    ), conn)
    repo.create_transaction(Transaction(
        id=cash_id, product_id="wallet-plus", transaction_type=Kind.CASH_TRANSFER_OUT,
        status=Status.CONFIRMED, trade_date=day, idempotency_key=f"{ACTION}:{day}:cash",
        amount=AMOUNT, shares=AMOUNT, confirmation_nav=Decimal("1"), confirmation_date=day,
        linked_transaction_id=buy_id, plan_id=plan.id, note=note, created_by="sip",
    ), conn)
    repo.update_pending_transaction(replace(
        purchase, status=Status.CONFIRMED, linked_transaction_id=cash_id,
        shares=(AMOUNT - Decimal("0.08")) / nav, fee_amount=Decimal("0.08"),
        confirmation_nav=nav, confirmation_date=confirmed,
    ), conn)
    repo.save_plan_execution(SipService._new_execution(
        plan.id, day, "confirmed", transaction_id=buy_id,
    ), conn)


def _apply(repo, conn):
    done = conn.execute("SELECT after_json FROM audit_logs WHERE id=? AND action=?", (OPERATION_ID, ACTION)).fetchone()
    if done is not None:
        report = json.loads(done[0])
        _validate_completed(repo, report, conn)
        return "already_repaired", report
    plan = repo.get_plan(PLAN_ID, conn=conn)
    if plan is None or plan.source_cash_product_id != "wallet-plus" or plan.daily_amount != AMOUNT or plan.purchase_fee_rate != FEE_RATE:
        raise ValueError("current plan differs from the verified 1000-yuan plan")
    product = repo.require_product(plan.product_id, conn=conn)
    if product.code != "400030" or product.provider != "eastmoney_fund":
        raise ValueError("plan is not the verified 400030 fund")
    originals = {}
    for day, confirmed, old_amount, nav in ROWS:
        quote = repo.get_quote(product.id, day, conn=conn)
        if quote is None or quote.unit_nav != nav:
            raise ValueError(f"missing or changed trade-day NAV: {day}")
        execution = repo.get_plan_execution(plan.id, day, conn=conn)
        purchases = [tx for tx in repo.list_transactions(product.id, conn=conn)
                     if tx.trade_date == day and tx.transaction_type in {Kind.SIP_PURCHASE, Kind.MANUAL_PURCHASE}]
        if old_amount is None:
            if execution is not None or purchases:
                raise ValueError("November 2 already has an execution or purchase; refusing duplicate")
            continue
        if execution is None or execution.status != "confirmed" or len(purchases) != 1:
            raise ValueError(f"unexpected existing purchase/execution: {day}")
        original = purchases[0]
        if (execution.transaction_id != original.id or original.plan_id != plan.id
            or original.transaction_type is not Kind.SIP_PURCHASE or original.status is not Status.CONFIRMED
            or original.amount != old_amount or original.fee_rate != FEE_RATE
            or original.fee_amount != old_amount * FEE_RATE or original.confirmation_nav != nav
            or original.confirmation_date != confirmed or original.shares != (old_amount - old_amount * FEE_RATE) / nav):
            raise ValueError(f"original purchase differs from the verified ledger: {day}")
        originals[day] = original
    before = _snapshot(repo, product.id, conn)
    tx_service = PortfolioTransactionService(repo, PositionProjector(repo))
    reversed_ids = []
    for day, confirmed, old_amount, nav in ROWS:
        if day in originals:
            original = originals[day]
            reversed_ids.extend([original.id, original.linked_transaction_id])
            tx_service.reverse_confirmed(original.id, "用户核实实际仅成交1000元，超额部分未成交退回钱包", f"{ACTION}:{day}:reverse", actor=ACTOR)
        _create_confirmed_pair(repo, plan, day, confirmed, nav, conn)
        for pid in (product.id, "wallet-plus"):
            repo.replace_position(PositionProjector(repo)._calculate(pid, conn), conn)
    after = _snapshot(repo, product.id, conn)
    delta = Decimal(after["wallet"]) - Decimal(before["wallet"])
    if delta != Decimal("2678"):
        raise ValueError("wallet change differs from verified refunds less the missing purchase")
    report = {"operation_id": OPERATION_ID, "code": product.code, "product_id": product.id,
              "plan_id": plan.id, "source": "user-confirmed bank partial fills and November 2 trade / November 3 confirmation",
              "wallet_delta": decimal_text(delta), "refunds": "3678", "missing_purchase": "1000",
              "before": before, "after": after, "reversed_original_ids": reversed_ids,
              "records": [{"trade_date": d.isoformat(), "confirmation_date": c.isoformat(), "amount": "1000", "nav": decimal_text(n),
                           "purchase_id": _id(d, "purchase")} for d, c, _, n in ROWS]}
    _validate_completed(repo, report, conn)
    repo.append_audit(OPERATION_ID, ACTION, "sip_plan", plan.id, before_json=_json(before), after_json=_json(report), source=ACTOR, conn=conn)
    return "repaired", report


def preview(database_path):
    with TemporaryDirectory(prefix="verified-sip-preview-") as folder:
        copied = Path(folder) / "portfolio.db"
        _copy_database(database_path, copied)
        repo = PortfolioRepository(BoundDatabase(PortfolioDatabase(copied)))
        with repo.database.connection() as conn:
            fingerprint = _fingerprint(conn)
        with reconciled_transaction(repo, ACTION) as conn:
            status, report = _apply(repo, conn)
        with repo.database.connection() as conn:
            completed = _fingerprint(conn)
        with reconciled_transaction(repo, ACTION) as conn:
            retry_status, retry_report = _apply(repo, conn)
        with repo.database.connection() as conn:
            if retry_status != "already_repaired" or retry_report != report or _fingerprint(conn) != completed:
                raise ValueError("repair retry is not idempotent")
        return {"schema_version": 1, "operation_id": OPERATION_ID, "fingerprint": fingerprint,
                "status": status, "report": report, "retry_verified": True}


def execute(database_path, plan, backup_root):
    if plan.get("schema_version") != 1 or plan.get("operation_id") != OPERATION_ID:
        raise ValueError("invalid repair preview")
    repo = PortfolioRepository(BoundDatabase(PortfolioDatabase(database_path)))
    with reconciled_transaction(repo, ACTION) as conn:
        done = conn.execute("SELECT 1 FROM audit_logs WHERE id=? AND action=?", (OPERATION_ID, ACTION)).fetchone()
        if done:
            status, report = _apply(repo, conn)
            return {"status": status, "report": report}
        if _fingerprint(conn) != plan["fingerprint"]:
            raise ValueError("database changed since preview; generate a fresh preview")
        backup = Path(backup_root).resolve() / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_400030_sip")
        backup.mkdir(parents=False, exist_ok=False)
        _copy_database(database_path, backup / "portfolio.before.db")
        (backup / "repair-plan.json").write_text(_json(plan), encoding="utf-8")
        status, report = _apply(repo, conn)
        if report != plan["report"]:
            raise ValueError("repair differs from verified preview")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("database integrity check failed")
    result = {"status": status, "report": report, "backup": str(backup)}
    (backup / "repair-result.json").write_text(_json(result), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description="预演并修复用户核实的400030三笔定投")
    parser.add_argument("--database", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--backup-root")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    plan_path = Path(args.plan)
    if args.execute:
        if not args.backup_root:
            parser.error("execute requires --backup-root")
        result = execute(args.database, json.loads(plan_path.read_text(encoding="utf-8")), args.backup_root)
    else:
        result = preview(args.database)
        plan_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
