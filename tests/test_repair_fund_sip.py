"""Verified partial confirmations must correct both fund and wallet legs."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    MarketQuote, Product, ProductType, Transaction, TransactionStatus as Status,
    TransactionType as Kind,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository
from src.portfolio_sip import SipService
from src.portfolio_transactions import PortfolioTransactionService


PLAN_ID = "1edaa8a0-1382-58ce-80f4-32c44c68e816"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repair_fund_sip.py"


@pytest.fixture
def ledger(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repo = PortfolioRepository(database)
    repo.add_product(Product("fund", "eastmoney_fund", "400030", "东方添益", ProductType.PUBLIC_FUND))
    repo.add_product(Product("wallet-plus", "wallet_plus", "WALLETPLUS", "钱包Plus", ProductType.CASH_MANAGEMENT))
    repo.create_transaction(Transaction(
        "opening", "wallet-plus", Kind.OPENING_POSITION, Status.CONFIRMED,
        date(2026, 10, 1), "opening", amount=Decimal("10000"),
        shares=Decimal("10000"), confirmation_nav=Decimal("1"),
        confirmation_date=date(2026, 10, 1),
    ))
    projector = PositionProjector(repo)
    projector.rebuild()
    sip = SipService(repo, projector, PortfolioTransactionService(repo, projector))
    plan = sip.save_plan("fund", "3456", "0.00008", "wallet-plus", date(2026, 10, 30))
    with database.transaction() as conn:
        conn.execute("UPDATE sip_plans SET id=? WHERE id=?", (PLAN_ID, plan.id))
    sip.activate(PLAN_ID)
    for day, nav in [(date(2026, 10, 30), "1.4601"), (date(2026, 11, 2), "1.4595"), (date(2026, 11, 3), "1.4625")]:
        repo.upsert_quote("fund", MarketQuote("400030", day, "official", str(day), unit_nav=Decimal(nav)), str(day))
    sip.ensure_intent(PLAN_ID, date(2026, 10, 30))
    sip.settle_pending(date(2026, 11, 2))
    with database.transaction() as conn:
        repo.save_plan(replace(repo.get_plan(PLAN_ID, conn=conn), daily_amount=Decimal("2222")), conn)
    sip.ensure_intent(PLAN_ID, date(2026, 11, 3))
    sip.settle_pending(date(2026, 11, 4))
    with database.transaction() as conn:
        repo.save_plan(replace(repo.get_plan(PLAN_ID, conn=conn), daily_amount=Decimal("1000"), start_date=date(2026, 11, 4)), conn)
    sip.ensure_intent(PLAN_ID, date(2026, 11, 4))
    return database, repo


def repair_module():
    return importlib.import_module("scripts.repair_fund_sip")


def test_preview_cli_preserves_source_ledger(ledger, tmp_path):
    database, repo = ledger
    before = repo.list_transactions()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--database", str(database.path), "--plan", str(tmp_path / "plan.json")],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["report"]["wallet_delta"] == "2678"
    assert repo.list_transactions() == before


def test_repair_preserves_trade_and_confirmation_dates_and_pending_order(ledger, tmp_path):
    database, repo = ledger
    repair = repair_module()
    before_plan = repo.get_plan(PLAN_ID)
    pending = repo.get_plan_execution(PLAN_ID, date(2026, 11, 4))
    pending_tx = repo.get_transaction_by_id(pending.transaction_id)
    before = PositionProjector(repo).calculate("wallet-plus").total_shares
    preview = repair.preview(database.path)
    result = repair.execute(database.path, preview, tmp_path)
    assert result["status"] == "repaired"
    assert PositionProjector(repo).calculate("wallet-plus").total_shares - before == Decimal("2678")
    for trade, confirmed, nav in [
        (date(2026, 10, 30), date(2026, 11, 2), "1.4601"),
        (date(2026, 11, 2), date(2026, 11, 3), "1.4595"),
        (date(2026, 11, 3), date(2026, 11, 4), "1.4625"),
    ]:
        execution = repo.get_plan_execution(PLAN_ID, trade)
        tx = repo.get_transaction_by_id(execution.transaction_id)
        cash = repo.get_transaction_by_id(tx.linked_transaction_id)
        assert execution.status == "confirmed"
        assert tx.transaction_type is Kind.SIP_PURCHASE
        assert tx.amount == cash.amount == cash.shares == Decimal("1000")
        assert tx.trade_date == cash.trade_date == trade
        assert tx.confirmation_date == confirmed
        assert tx.confirmation_nav == Decimal(nav)
        assert tx.fee_amount == Decimal("0.08")
        assert cash.linked_transaction_id == tx.id
        assert cash.status is tx.status is Status.CONFIRMED
    # 1000 - 0.08, divided by 11/2's 1.4595, not by the confirmation-day NAV.
    nov2 = repo.get_transaction_by_id(repo.get_plan_execution(PLAN_ID, date(2026, 11, 2)).transaction_id)
    assert nov2.shares == Decimal("685.111339499828708461801987")
    assert repo.get_plan(PLAN_ID) == before_plan
    assert repo.get_plan_execution(PLAN_ID, date(2026, 11, 4)) == pending
    assert repo.get_transaction_by_id(pending.transaction_id) == pending_tx
    assert len([tx for tx in repo.list_transactions() if tx.transaction_type is Kind.REVERSAL]) == 4
    with database.connection() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    after = repo.list_transactions()
    assert repair.execute(database.path, preview, tmp_path)["status"] == "already_repaired"
    assert repo.list_transactions() == after


@pytest.mark.parametrize("change", ["missing_quote", "unexpected_order", "stale_preview"])
def test_repair_refuses_changed_or_ambiguous_ledger_atomically(ledger, tmp_path, change):
    database, repo = ledger
    repair = repair_module()
    plan = repair.preview(database.path)
    with database.transaction() as conn:
        if change == "missing_quote":
            conn.execute("DELETE FROM quotes WHERE quote_date='2026-11-02'")
        elif change == "unexpected_order":
            repo.create_transaction(Transaction("duplicate", "fund", Kind.MANUAL_PURCHASE, Status.PENDING_QUOTE, date(2026, 11, 2), "duplicate", amount=Decimal("1000")), conn)
        else:
            conn.execute("UPDATE sip_plans SET daily_amount='999' WHERE id=?", (PLAN_ID,))
    before = repo.list_transactions()
    if change != "stale_preview":
        with pytest.raises(ValueError):
            repair.preview(database.path)
    with pytest.raises(ValueError):
        repair.execute(database.path, plan, tmp_path)
    assert repo.list_transactions() == before


def test_write_failure_rolls_back_reversals_and_replacements(ledger, tmp_path, monkeypatch):
    database, repo = ledger
    repair = repair_module()
    plan = repair.preview(database.path)
    before_transactions = repo.list_transactions()
    before_executions = repo.list_plan_executions(PLAN_ID)
    before_position = PositionProjector(repo).calculate("wallet-plus")
    real_create = repair._create_confirmed_pair

    def fail_second_pair(*args):
        real_create(*args)
        if args[2] == date(2026, 11, 2):
            raise RuntimeError("simulated interrupted repair")

    monkeypatch.setattr(repair, "_create_confirmed_pair", fail_second_pair)
    with pytest.raises(RuntimeError, match="interrupted repair"):
        repair.execute(database.path, plan, tmp_path)
    assert repo.list_transactions() == before_transactions
    assert repo.list_plan_executions(PLAN_ID) == before_executions
    assert PositionProjector(repo).calculate("wallet-plus") == before_position
