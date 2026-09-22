from datetime import date
from decimal import Decimal
import json
import sqlite3

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    MarketQuote, Product, ProductType, Transaction,
    TransactionStatus as Status, TransactionType as Kind,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository
from src.portfolio_income_repair import preview_wallet_income_repair, repair_wallet_income
from src.portfolio_income import CashIncomeService
from src.portfolio_transactions import PortfolioTransactionService


@pytest.fixture
def repair_repository(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()
    repo = PortfolioRepository(db)
    repo.add_product(Product("wallet-plus", "wallet_plus", "WALLETPLUS", "钱包Plus", ProductType.CASH_MANAGEMENT))
    repo.create_transaction(Transaction(
        id="opening", product_id="wallet-plus", transaction_type=Kind.OPENING_POSITION,
        status=Status.CONFIRMED, trade_date=date(2026, 9, 1), idempotency_key="opening",
        amount=Decimal("10000"), shares=Decimal("10000"), confirmation_date=date(2026, 9, 1),
        created_at="2026-09-01 00:00:00",
    ))
    for day in (19, 20, 21):
        d = date(2026, 9, day)
        repo.upsert_quote("wallet-plus", MarketQuote(
            product_code="WALLETPLUS", quote_date=d, source="wallet_plus_fixed",
            raw_hash=str(day), income_per_10k=Decimal("1"),
        ), "2026-09-22T00:00:00+08:00")
        repo.create_transaction(Transaction(
            id=f"income-{day}", product_id="wallet-plus", transaction_type=Kind.INCOME_ACCRUAL,
            status=Status.CONFIRMED, trade_date=d, idempotency_key=f"income:wallet-plus:{d}",
            amount=Decimal("0"), shares=Decimal("0"), confirmation_nav=Decimal("1"), confirmation_date=d,
            created_at="2026-09-21 16:00:00",
        ))
    PositionProjector(repo).rebuild()
    return repo


def rows(repo, table):
    with sqlite3.connect(f"{repo.database.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def preview(repo):
    return preview_wallet_income_repair(repo, date(2026, 9, 19), date(2026, 9, 21))


def test_preview_is_read_only_and_repairs_compounding_in_order(repair_repository):
    repo = repair_repository
    original = {table: rows(repo, table) for table in ("transactions", "positions", "audit_logs")}
    plan = preview(repo)
    assert [r["after"] for r in plan["report"]["income"]] == ["1", "1.0001", "1.00020001"]
    assert plan["report"]["wallet_after"] == "10003.00030001"
    assert original == {table: rows(repo, table) for table in original}


def test_execute_preserves_immutable_income_and_is_idempotent(repair_repository):
    repo = repair_repository
    plan = preview(repo)
    result = repair_wallet_income(repo, plan)
    assert result["status"] == "repaired"
    for day in (19, 20, 21):
        original = repo.get_transaction_by_id(f"income-{day}")
        assert original.status is Status.REVERSED and original.amount == 0
        replacement = repo.get_transaction_by_idempotency(f"income:wallet-plus:2026-09-{day}:reactivated")
        assert replacement.status is Status.CONFIRMED and replacement.amount > 0
    before_retry = rows(repo, "transactions")
    assert repair_wallet_income(repo, plan)["status"] == "already_repaired"
    assert before_retry == rows(repo, "transactions")
    assert repo.get_position("wallet-plus").total_shares == Decimal("10003.00030001")
    transaction_count = len(rows(repo, "transactions"))
    scheduler = CashIncomeService(repo, PositionProjector(repo))
    assert scheduler.accrue("wallet-plus", date(2026, 9, 21)).amount == Decimal("1.00020001")
    assert len(rows(repo, "transactions")) == transaction_count


def test_repair_preserves_later_manual_balance_and_profit_targets(repair_repository):
    repo = repair_repository
    for kind, amount, shares, action, actual in (
        (Kind.HOLDING_ADJUSTMENT, "3", "3", "adjust_holding", "10003"),
        (Kind.PROFIT_ADJUSTMENT, "3", "0", "adjust_holding_profit", "3"),
    ):
        repo.create_transaction(Transaction(
            id=action, product_id="wallet-plus", transaction_type=kind, status=Status.CONFIRMED,
            trade_date=date(2026, 9, 21), trade_time="06:50:43", idempotency_key=action,
            amount=Decimal(amount), shares=Decimal(shares), confirmation_date=date(2026, 9, 21),
            confirmation_nav=Decimal("1") if shares != "0" else None,
            created_at="2026-09-20 22:50:43", created_by="web",
        ))
        repo.append_audit(action, action, "product", "wallet-plus", after_json=json.dumps({
            "adjustment_id": action, "effective_date": "2026-09-21",
            "actual_shares" if action == "adjust_holding" else "actual_profit": actual,
        }))
    PositionProjector(repo).rebuild()
    plan = preview(repo)
    assert [r["correction"] for r in plan["report"]["calibrations"]] == ["-2.0001", "-2.0001"]
    result = repair_wallet_income(repo, plan)
    assert result["report"]["wallet_after"] == "10004.00020001"
    assert result["report"]["holding_profit_after"] == "4.00020001"
    assert repo.get_transaction_by_id("adjust_holding").status is Status.CONFIRMED
    assert repo.get_transaction_by_id("adjust_holding_profit").status is Status.CONFIRMED


def test_changed_ledger_rejects_stale_plan_without_partial_writes(repair_repository):
    repo = repair_repository
    plan = preview(repo)
    repo.append_audit("new-action", "other_action", "product", "wallet-plus")
    before = rows(repo, "transactions")
    with pytest.raises(ValueError, match="changed"):
        repair_wallet_income(repo, plan)
    assert rows(repo, "transactions") == before


def test_mismatched_preview_results_roll_back_every_repair(repair_repository):
    repo = repair_repository
    plan = preview(repo)
    plan["report"]["wallet_after"] = "99999"
    before = {table: rows(repo, table) for table in ("transactions", "positions", "audit_logs")}
    with pytest.raises(ValueError, match="preview"):
        repair_wallet_income(repo, plan)
    assert before == {table: rows(repo, table) for table in before}


def test_refuses_truncated_repair_when_later_income_exists(repair_repository):
    with pytest.raises(ValueError, match="later income"):
        preview_wallet_income_repair(repair_repository, date(2026, 9, 19), date(2026, 9, 20))


def test_refuses_unexplained_manual_adjustment_after_repair_start(repair_repository):
    repo = repair_repository
    repo.create_transaction(Transaction(
        id="unexplained", product_id="wallet-plus", transaction_type=Kind.HOLDING_ADJUSTMENT,
        status=Status.CONFIRMED, trade_date=date(2026, 9, 21), idempotency_key="unexplained",
        amount=Decimal("3"), shares=Decimal("3"), created_by="web",
    ))
    PositionProjector(repo).rebuild()
    with pytest.raises(ValueError, match="missing calibration audit"):
        preview(repo)


def test_missing_quote_does_not_partially_reverse_income(repair_repository):
    repo = repair_repository
    plan = preview(repo)
    with repo.database.transaction() as conn:
        conn.execute("DELETE FROM quotes WHERE product_id='wallet-plus' AND quote_date='2026-09-20'")
    before = rows(repo, "transactions")
    with pytest.raises(ValueError, match="missing wallet income quote"):
        preview(repo)
    assert rows(repo, "transactions") == before


def test_same_day_already_booked_income_requires_separate_calibration_review(repair_repository):
    repo = repair_repository
    repo.create_transaction(Transaction(
        id="retrospective", product_id="wallet-plus", transaction_type=Kind.HOLDING_ADJUSTMENT,
        status=Status.CONFIRMED, trade_date=date(2026, 9, 19), idempotency_key="retrospective",
        amount=Decimal("1"), shares=Decimal("1"), created_by="web",
        created_at="2026-09-22 00:00:00",
    ))
    repo.append_audit("retrospective", "adjust_holding", "product", "wallet-plus", after_json=json.dumps({
        "adjustment_id": "retrospective", "effective_date": "2026-09-19", "actual_shares": "10001",
    }))
    PositionProjector(repo).rebuild()
    with pytest.raises(ValueError, match="same-day income"):
        preview(repo)


def test_external_connection_must_have_an_active_transaction(repair_repository):
    repo = repair_repository
    with repo.database.connection() as conn:
        with pytest.raises(ValueError, match="active transaction"):
            CashIncomeService(repo, PositionProjector(repo)).accrue("wallet-plus", date(2026, 9, 19), conn=conn)


def test_new_plan_cannot_repeat_calibration_compensation_for_repaired_dates(repair_repository):
    repo = repair_repository
    repair_wallet_income(repo, preview(repo))
    before = rows(repo, "transactions")
    with pytest.raises(ValueError, match="already repaired"):
        preview(repo)
    assert rows(repo, "transactions") == before


def test_restored_income_after_end_still_blocks_a_truncated_repair(repair_repository):
    repo = repair_repository
    service = PortfolioTransactionService(repo, PositionProjector(repo))
    first = service.reverse_confirmed("income-21", "reverse", "reverse-income")
    service.reverse_confirmed(first.id, "restore", "restore-income")
    assert repo.get_transaction_by_id("income-21").status is Status.REVERSED
    with pytest.raises(ValueError, match="later income"):
        preview_wallet_income_repair(repo, date(2026, 9, 19), date(2026, 9, 20))


def test_restored_income_inside_range_requires_separate_review(repair_repository):
    repo = repair_repository
    service = PortfolioTransactionService(repo, PositionProjector(repo))
    first = service.reverse_confirmed("income-20", "reverse", "reverse-income")
    service.reverse_confirmed(first.id, "restore", "restore-income")
    with pytest.raises(ValueError, match="restored income"):
        preview(repo)
