from datetime import date
from decimal import Decimal
from dataclasses import replace
import json

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    Product, ProductType, Transaction, TransactionType, TransactionStatus,
    MarketQuote,
)
from src.portfolio_repository import PortfolioRepository
from src.portfolio_positions import PositionProjector
from src.portfolio_transactions import PortfolioTransactionService
from src.portfolio_reconciliation import ReconciliationError
from src.portfolio_view import build_portfolio_payload
from src.portfolio_sip import SipService


@pytest.fixture
def ledger(tmp_path):
    db = PortfolioDatabase(tmp_path / "reconcile.db")
    db.initialize()
    repo = PortfolioRepository(db)
    repo.add_product(Product("wallet", "wallet_plus", "WALLETPLUS", "钱包", ProductType.CASH_MANAGEMENT))
    repo.add_product(Product("fund", "fund", "F001", "基金", ProductType.PUBLIC_FUND))
    repo.add_product(Product("cash", "cash", "C001", "现金", ProductType.CASH_MANAGEMENT))
    for pid in ("wallet", "cash", "fund"):
        repo.create_transaction(Transaction(
            id="opening:" + pid, product_id=pid,
            transaction_type=TransactionType.OPENING_POSITION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 9, 1), idempotency_key="opening:" + pid,
            amount=Decimal("10000"), shares=Decimal("10000"),
            confirmation_nav=Decimal("1"), confirmation_date=date(2026, 9, 1),
        ))
    repo.upsert_quote("fund", MarketQuote("F001", date(2026, 9, 18), "test", "nav", unit_nav=Decimal("1")), "2026-09-18")
    projector = PositionProjector(repo)
    projector.rebuild()
    return repo, PortfolioTransactionService(repo, projector)


def assets(repo):
    return Decimal(build_portfolio_payload(repo, as_of=date(2026, 9, 22))["summary"]["total_assets"])


def purchase(service, source="wallet", key="purchase", fee="0"):
    return service.record_purchase(
        "fund", Decimal("2000"), date(2026, 9, 18), key,
        source_cash_product_id=source, fee_rate=Decimal(fee),
        trade_time="2026-09-18T10:00:00",
    )


def audits(repo):
    with repo.database.connection() as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT after_json FROM audit_logs WHERE action='portfolio_reconciliation'"
        )]


@pytest.mark.parametrize("source", ["wallet", "cash"])
def test_internal_purchase_confirm_fee_cancel_and_retry_reconcile(ledger, source):
    repo, service = ledger
    before = assets(repo)
    tx = purchase(service, source, fee="0.001")
    assert assets(repo) == before
    assert len(audits(repo)) == 1
    assert purchase(service, source, fee="0.001").id == tx.id
    assert len(audits(repo)) == 1
    service.confirm_pending(tx.id, MarketQuote("F001", date(2026, 9, 18), "test", "x", unit_nav=Decimal("1")))
    assert assets(repo) == before - Decimal("2")
    report = audits(repo)[-1]
    assert Decimal(report["actual_delta"]) == Decimal("-2")
    assert Decimal(report["expected_delta"]) == Decimal("-2")


def test_pending_redemption_and_settlement_preserve_assets(ledger):
    repo, service = ledger
    before = assets(repo)
    tx = service.record_redemption(
        "fund", Decimal("2000"), date(2026, 9, 18), "redeem",
        destination_cash_product_id="wallet", settlement_date=date(2026, 9, 20),
        trade_time="2026-09-18T10:00:00",
    )
    assert assets(repo) == before
    service.confirm_pending(tx.id, MarketQuote("F001", date(2026, 9, 18), "test", "x", unit_nav=Decimal("1")))
    assert assets(repo) == before
    service.settle_redemptions(date(2026, 9, 20))
    assert assets(repo) == before
    assert len(audits(repo)) == 3


def test_destination_redemption_requires_arrival_date_before_writing(ledger):
    repo, service = ledger
    before = repo.list_transactions()
    with pytest.raises(ValueError, match="预计到账日期"):
        service.record_redemption(
            "fund", Decimal("2000"), date(2026, 9, 18), "missing-date",
            destination_cash_product_id="wallet",
        )
    assert repo.list_transactions() == before
    assert audits(repo) == []


def test_legacy_pending_redemption_without_arrival_date_cannot_confirm(ledger):
    repo, service = ledger
    tx = service.record_redemption(
        "fund", Decimal("2000"), date(2026, 9, 18), "legacy-date",
        destination_cash_product_id="wallet", settlement_date=date(2026, 9, 20),
        trade_time="2026-09-18T10:00:00",
    )
    with repo.database.transaction() as conn:
        conn.execute("UPDATE transactions SET settlement_date=NULL WHERE id=?", (tx.id,))
    before = repo.list_transactions()
    with pytest.raises(ValueError, match="预计到账日期"):
        service.confirm_pending(tx.id, MarketQuote("F001", date(2026, 9, 18), "test", "x", unit_nav=Decimal("1")))
    assert repo.list_transactions() == before
    assert len(audits(repo)) == 1


@pytest.mark.parametrize("fault", ["amount", "status"])
def test_corrupt_cash_leg_rolls_back_purchase(ledger, monkeypatch, fault):
    repo, service = ledger
    before = repo.list_transactions()
    original_create = repo.create_transaction

    def corrupt_cash_leg(tx, conn=None):
        if tx.transaction_type is TransactionType.CASH_TRANSFER_OUT:
            if fault == "amount":
                tx = replace(tx, amount=tx.amount - Decimal("0.01"), shares=tx.shares - Decimal("0.01"))
            else:
                tx = replace(tx, status=TransactionStatus.PENDING_QUOTE)
        return original_create(tx, conn=conn)

    monkeypatch.setattr(repo, "create_transaction", corrupt_cash_leg)
    with pytest.raises(ReconciliationError):
        purchase(service)
    assert repo.list_transactions() == before
    assert repo.get_position("wallet").total_shares == Decimal("10000")
    assert audits(repo) == []


def test_sip_pending_cash_debit_is_rejected_even_when_total_assets_balance(ledger, monkeypatch):
    repo, service = ledger
    sip = SipService(repo, PositionProjector(repo), service)
    plan = sip.save_plan("fund", Decimal("100"), Decimal("0"), "cash", date(2026, 9, 18))
    sip.activate(plan.id)
    before = repo.list_transactions()
    original_create = repo.create_transaction

    def corrupt_sip_status(tx, conn=None):
        if tx.transaction_type is TransactionType.CASH_TRANSFER_OUT:
            tx = replace(tx, status=TransactionStatus.PENDING_QUOTE)
        return original_create(tx, conn=conn)

    monkeypatch.setattr(repo, "create_transaction", corrupt_sip_status)
    with pytest.raises(ReconciliationError, match="状态或来源"):
        sip.ensure_intent(plan.id, date(2026, 9, 18))
    assert repo.list_transactions() == before
    assert repo.get_plan_execution(plan.id, date(2026, 9, 18)) is None
    assert audits(repo) == []


def test_stale_position_cannot_be_silently_absorbed_by_next_trade(ledger):
    repo, service = ledger
    with repo.database.transaction() as conn:
        conn.execute("UPDATE positions SET total_shares='9999', available_shares='9999' WHERE product_id='wallet'")
    before = repo.list_transactions()
    with pytest.raises(ReconciliationError, match="资产变动不平衡"):
        purchase(service)
    assert repo.list_transactions() == before
    assert repo.get_position("wallet").total_shares == Decimal("9999")
    assert audits(repo) == []


@pytest.mark.parametrize("kind", ["dividend", "refund", "cash_redemption"])
def test_incorrect_receipt_date_rejected_even_when_amount_balances(ledger, monkeypatch, kind):
    repo, service = ledger
    purchase_tx = purchase(service) if kind == "refund" else None
    before = repo.list_transactions()
    before_assets = assets(repo)
    original_create = repo.create_transaction

    def corrupt_receipt_date(tx, conn=None):
        if tx.transaction_type is TransactionType.CASH_TRANSFER_IN:
            tx = replace(tx, confirmation_date=date(2026, 1, 1))
        return original_create(tx, conn=conn)

    monkeypatch.setattr(repo, "create_transaction", corrupt_receipt_date)
    with pytest.raises(ReconciliationError, match="到账流水"):
        if kind == "dividend":
            service.record_cash_dividend("fund", Decimal("50"), date(2026, 9, 18), "dividend", destination_cash_product_id="wallet")
        elif kind == "refund":
            service.cancel_pending(purchase_tx.id, "cancel")
        else:
            service.record_redemption("wallet", Decimal("50"), date(2026, 9, 18), "cash-redeem", destination_cash_product_id="cash")
    assert repo.list_transactions() == before
    assert assets(repo) == before_assets


def test_missing_position_update_rolls_back_whole_purchase(ledger, monkeypatch):
    repo, service = ledger
    before = repo.list_transactions()
    monkeypatch.setattr(service, "_rebuild_positions", lambda *args: None)
    with pytest.raises(ReconciliationError, match="对账"):
        purchase(service)
    assert repo.list_transactions() == before
    assert repo.get_position("wallet").total_shares == Decimal("10000")
    assert audits(repo) == []


def test_missing_settlement_position_update_rolls_back_arrival(ledger, monkeypatch):
    repo, service = ledger
    tx = service.record_redemption(
        "fund", Decimal("2000"), date(2026, 9, 18), "redeem",
        destination_cash_product_id="wallet", settlement_date=date(2026, 9, 20),
    )
    before = repo.list_transactions()
    monkeypatch.setattr(service, "_rebuild_positions", lambda *args: None)
    with pytest.raises(ReconciliationError):
        service.settle_redemptions(date(2026, 9, 20))
    assert repo.list_transactions() == before
    assert repo.find_purchase_by_origin(tx.id) is None


def test_missing_refund_is_rejected_even_if_positions_match_ledger(ledger, monkeypatch):
    repo, service = ledger
    tx = purchase(service)
    before = repo.list_transactions()
    monkeypatch.setattr(service, "_refund_confirmed_purchase_cash", lambda *args: None)
    with pytest.raises(ReconciliationError):
        service.cancel_pending(tx.id, "cancel")
    assert repo.list_transactions() == before


def test_external_flows_and_adjustments_are_valid_changes(ledger):
    repo, service = ledger
    before = assets(repo)
    service.record_purchase("wallet", Decimal("100"), date(2026, 9, 18), "deposit")
    assert assets(repo) == before + Decimal("100")
    service.record_redemption("wallet", Decimal("20"), date(2026, 9, 19), "withdraw", trade_time="2026-09-19T10:00:00")
    assert assets(repo) == before + Decimal("80")
    service.adjust_holding("wallet", Decimal("10200"), date(2026, 9, 19), "校准", "adjust")
    assert assets(repo) == before + Decimal("200")
    assert len(audits(repo)) == 3
