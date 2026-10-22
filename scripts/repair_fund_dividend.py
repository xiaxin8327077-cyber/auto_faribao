"""Preview and atomically repair the verified 015736 cash dividend.

This maintenance command keeps the original mistaken calibration and its
reversal, and sends the verified dividend through the regular event service.
It never infers or changes the fund's units from a rounded bank valuation.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import date, datetime
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
from src.portfolio_dividends import FundDividend, FundDividendService
from src.portfolio_models import TransactionStatus, TransactionType, decimal_text
from src.portfolio_positions import PositionProjector
from src.portfolio_profit import calculate_latest_profit
from src.portfolio_reconciliation import reconciled_transaction
from src.portfolio_repository import PortfolioRepository
from src.portfolio_transactions import PortfolioTransactionService
from src.portfolio_view import build_portfolio_payload


ACTION = "repair_verified_fund_dividend"
ACTOR = "ops:fund-dividend-repair"
OPERATION_ID = str(uuid5(NAMESPACE_URL, f"{ACTION}:015736:2026-10-20"))
AS_OF = date(2026, 10, 22)
EX_DATE = date(2026, 10, 20)
AMOUNT = Decimal("821.94")
OLD_ADJUSTMENT = Decimal("826.1526363201589815053708515")
WALLET_BEFORE = Decimal("41048.64")
WALLET_AFTER = Decimal("41870.58")
SOURCE_URL = "https://www.csfunds.com.cn/c/2026-10-16/233356.shtml"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(conn):
    digest = hashlib.sha256()
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        table = row[0]
        quoted = '"' + table.replace('"', '""') + '"'
        data = sorted(_json(dict(item)) for item in conn.execute(f"SELECT * FROM {quoted}"))
        digest.update(_json([table, data]).encode("utf-8"))
    return digest.hexdigest()


class BoundDatabase:
    """Make existing transaction services participate in one repair transaction."""

    def __init__(self, database):
        self.database = database
        self.path = database.path
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
        savepoint = f"dividend_repair_{self.sequence}"
        self.bound.execute(f"SAVEPOINT {savepoint}")
        try:
            yield self.bound
        except BaseException:
            self.bound.execute(f"ROLLBACK TO {savepoint}")
            self.bound.execute(f"RELEASE {savepoint}")
            raise
        else:
            self.bound.execute(f"RELEASE {savepoint}")


def _snapshot(repository, product_id, conn):
    payload = build_portfolio_payload(repository, as_of=AS_OF, conn=conn)
    row = next(item for item in payload["products"] if item["id"] == product_id)
    wallet = next(item for item in payload["products"] if item["id"] == "wallet-plus")
    product = repository.require_product(product_id, conn=conn)
    profit_date, profit = calculate_latest_profit(repository, product, EX_DATE, conn=conn)
    return {
        "wallet": wallet["shares"],
        "fund_shares": row["shares"],
        "fund_market_value": row["market_value"],
        "fund_pending_purchase": row["in_transit_amount"],
        "fund_value_including_purchase": decimal_text(
            Decimal(row["market_value"]) + Decimal(row["in_transit_amount"])
        ),
        "total_assets": payload["summary"]["total_assets"],
        "ex_dividend_profit_date": profit_date.isoformat() if profit_date else None,
        "ex_dividend_profit": decimal_text(profit) if profit is not None else None,
    }


def _validate_completed(repository, conn, report):
    """A saved success report never authorizes recreating reversed money."""
    original = repository.get_transaction_by_id(report["reversed_calibration_id"], conn=conn)
    reversal = repository.get_transaction_by_idempotency(f"{ACTION}:{OPERATION_ID}:reverse", conn=conn)
    neutralized = PositionProjector._linked_reversal_pair_ids(repository.list_transactions(conn=conn))
    event = conn.execute("SELECT * FROM fund_dividend_events WHERE id=?", (report["event_id"],)).fetchone()
    if (original is None or reversal is None or event is None
        or original.status is not TransactionStatus.REVERSED or original.id not in neutralized
        or original.transaction_type is not TransactionType.LATEST_PROFIT_ADJUSTMENT
        or original.amount != OLD_ADJUSTMENT or original.shares != Decimal("0")
        or reversal.status is not TransactionStatus.CONFIRMED
        or reversal.transaction_type is not TransactionType.REVERSAL
        or reversal.linked_transaction_id != original.id or reversal.amount != -OLD_ADJUSTMENT
        or reversal.shares != Decimal("0")
        or event["status"] != "settled" or event["mode"] != "cash"
        or event["product_id"] != report["product_id"] or event["amount"] != decimal_text(AMOUNT)
        or event["amount_override"] != decimal_text(AMOUNT) or not event["override_source"]):
        raise ValueError("completed repair ledger has changed; inspect it before any further action")
    dividend = repository.get_transaction_by_id(event["dividend_transaction_id"], conn=conn)
    cash = repository.get_transaction_by_id(event["settlement_transaction_id"], conn=conn)
    if (dividend is None or cash is None
        or dividend.status is not TransactionStatus.CONFIRMED or cash.status is not TransactionStatus.CONFIRMED
        or dividend.id in neutralized or cash.id in neutralized
        or dividend.transaction_type is not TransactionType.CASH_DIVIDEND
        or cash.transaction_type is not TransactionType.CASH_TRANSFER_IN
        or dividend.product_id != report["product_id"] or cash.product_id != "wallet-plus"
        or dividend.amount != AMOUNT or dividend.shares != Decimal("0")
        or cash.amount != AMOUNT or cash.shares != AMOUNT or cash.confirmation_nav != Decimal("1")
        or dividend.trade_date != EX_DATE or dividend.confirmation_date != EX_DATE
        or dividend.settlement_date != AS_OF
        or cash.trade_date != AS_OF or cash.confirmation_date != AS_OF
        or dividend.linked_transaction_id != cash.id or cash.linked_transaction_id != dividend.id
        or dividend.created_by != cash.created_by):
        raise ValueError("completed repair ledger has changed; inspect it before any further action")


def _apply(repository, conn):
    previous = conn.execute(
        "SELECT after_json FROM audit_logs WHERE id=? AND action=?", (OPERATION_ID, ACTION)
    ).fetchone()
    if previous:
        report = json.loads(previous[0])
        _validate_completed(repository, conn, report)
        return "already_repaired", report
    product = repository.get_product_by_provider_code("changsheng_fund", "015736", conn=conn)
    if product is None:
        raise ValueError("the expected 015736 product is missing")
    candidates = [
        tx for tx in repository.list_transactions(product_id=product.id, conn=conn)
        if tx.transaction_type is TransactionType.LATEST_PROFIT_ADJUSTMENT
        and tx.status is TransactionStatus.CONFIRMED
        and tx.trade_date == EX_DATE and tx.amount == OLD_ADJUSTMENT
    ]
    if len(candidates) != 1:
        raise ValueError("expected exactly one original 826.152636... calibration")
    before = _snapshot(repository, product.id, conn)
    if Decimal(before["wallet"]) != WALLET_BEFORE:
        raise ValueError("wallet changed from the user's verified pre-dividend balance")
    transaction_service = PortfolioTransactionService(repository, PositionProjector(repository))
    transaction_service.reverse_confirmed(
        candidates[0].id,
        "真实现金分红替代除息日错误收益校准，保留原始流水",
        f"{ACTION}:{OPERATION_ID}:reverse",
        actor=ACTOR,
    )
    service = FundDividendService(repository)
    event = FundDividend(
        record_date=EX_DATE, ex_dividend_date=EX_DATE, payment_date=AS_OF,
        per_share=Decimal("0.0082"), source_url=SOURCE_URL,
    )
    included = service.include_event(
        product.id, event, amount_override=AMOUNT, mode_override="cash",
        override_source="user-confirmed bank cash dividend:821.94",
        as_of=AS_OF, conn=conn,
    )
    event_id = included["id"] if isinstance(included, dict) else included
    service.process_known_event(event_id, AS_OF, conn=conn)
    after = _snapshot(repository, product.id, conn)
    if Decimal(after["wallet"]) != WALLET_AFTER:
        raise ValueError("dividend did not credit the verified 821.94 to the wallet")
    if before["fund_shares"] != after["fund_shares"]:
        raise ValueError("cash dividend unexpectedly changed fund units")
    if before["fund_pending_purchase"] != after["fund_pending_purchase"]:
        raise ValueError("repair unexpectedly changed pending purchases")
    if not Decimal("120.28") <= Decimal(after["ex_dividend_profit"]) < Decimal("120.29"):
        raise ValueError("ex-dividend profit differs from the independently checked positive amount")
    difference = Decimal(after["total_assets"]) - Decimal(before["total_assets"])
    if abs(difference - (AMOUNT - OLD_ADJUSTMENT)) > Decimal("0.000000000001"):
        raise ValueError("asset change differs from removing the erroneous adjustment and crediting cash")
    first_fingerprint = _fingerprint(conn)
    service.process_known_event(event_id, AS_OF, conn=conn)
    if _fingerprint(conn) != first_fingerprint:
        raise ValueError("reprocessing the same dividend was not idempotent")
    report = {
        "operation_id": OPERATION_ID, "product_id": product.id, "code": product.code,
        "event_id": event_id, "reversed_calibration_id": candidates[0].id,
        "verified_cash_dividend": decimal_text(AMOUNT),
        "verified_amount_source": "user-confirmed bank cash-dividend record",
        "official_source": SOURCE_URL, "before": before, "after": after,
        "remaining_unverified_fund_valuation_difference": "-0.10770550041572213423406764",
        "bank_ex_dividend_profit": "120.28", "fund_units_changed": False,
    }
    repository.append_audit(
        OPERATION_ID, ACTION, "product", product.id,
        before_json=_json(before), after_json=_json(report), source=ACTOR, conn=conn,
    )
    return "repaired", report


def _copy_database(source_path, target_path):
    source_uri = f"{Path(source_path).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source:
        with closing(sqlite3.connect(target_path)) as target:
            source.backup(target)


def preview(database_path):
    source_uri = f"{Path(database_path).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source:
        source.row_factory = sqlite3.Row
        fingerprint = _fingerprint(source)
    with TemporaryDirectory(prefix="fund-dividend-preview-") as folder:
        copy_path = Path(folder) / "portfolio.db"
        _copy_database(database_path, copy_path)
        database = PortfolioDatabase(copy_path)
        database.initialize()
        repository = PortfolioRepository(BoundDatabase(database))
        with reconciled_transaction(repository, ACTION) as conn:
            status, report = _apply(repository, conn)
        completed = _fingerprint_after(database)
        with reconciled_transaction(repository, ACTION) as conn:
            retry_status, retry_report = _apply(repository, conn)
        if retry_status != "already_repaired" or retry_report != report or _fingerprint_after(database) != completed:
            raise ValueError("repair retry changed the completed ledger")
        return {"schema_version": 1, "fingerprint": fingerprint, "operation_id": OPERATION_ID,
                "status": status, "report": report, "retry_verified": True}


def _fingerprint_after(database):
    with database.connection() as conn:
        return _fingerprint(conn)


def execute(database_path, plan, backup_root):
    if plan.get("schema_version") != 1 or plan.get("operation_id") != OPERATION_ID:
        raise ValueError("invalid repair plan")
    database = PortfolioDatabase(Path(database_path))
    repository = PortfolioRepository(BoundDatabase(database))
    root = Path(backup_root).resolve()
    backup_path = root / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_015736_dividend")
    if not backup_path.resolve().is_relative_to(root):
        raise ValueError("backup path escaped the requested backup root")
    with reconciled_transaction(repository, ACTION) as conn:
        done = conn.execute("SELECT 1 FROM audit_logs WHERE id=? AND action=?", (OPERATION_ID, ACTION)).fetchone()
        if done:
            status, report = _apply(repository, conn)
            return {"status": status, "report": report}
        if _fingerprint(conn) != plan["fingerprint"]:
            raise ValueError("database changed since preview; generate a fresh plan")
        backup_path.mkdir(parents=False, exist_ok=False)
        _copy_database(database_path, backup_path / "portfolio.before.db")
        (backup_path / "repair-plan.json").write_text(_json(plan), encoding="utf-8")
        status, report = _apply(repository, conn)
        if report != plan["report"]:
            raise ValueError("repair result differs from the verified preview")
    result = {"status": status, "report": report, "backup": str(backup_path)}
    (backup_path / "repair-result.json").write_text(_json(result), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description="预演并修复015736本次已核实现金分红")
    parser.add_argument("--database", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--backup-root")
    args = parser.parse_args()
    plan_path = Path(args.plan)
    if args.execute:
        if not args.backup_root:
            parser.error("执行需要 --backup-root")
        result = execute(args.database, json.loads(plan_path.read_text(encoding="utf-8")), args.backup_root)
    else:
        result = preview(args.database)
        plan_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
