from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
import threading

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    MarketQuote,
    Product,
    ProductType,
    RedemptionSettlementStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository
from src.portfolio_sip import SipService
from src.portfolio_transactions import PortfolioTransactionService


TRADE_DATE = date(2026, 7, 30)


@pytest.fixture
def services(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    projector = PositionProjector(repository)
    return (
        repository,
        PortfolioTransactionService(repository, projector),
        projector,
    )


def seed_product(repository, product_id, product_type, code=None):
    product = Product(
        id=product_id,
        provider="test",
        code=code or product_id,
        name=product_id,
        product_type=product_type,
    )
    repository.add_product(product)
    return product


def seed_opening_position(repository, product_id, shares, amount=None):
    opening = Transaction(
        id=f"opening:{product_id}",
        product_id=product_id,
        transaction_type=TransactionType.OPENING_POSITION,
        status=TransactionStatus.CONFIRMED,
        trade_date=date(2026, 7, 1),
        idempotency_key=f"opening:{product_id}",
        amount=Decimal(amount if amount is not None else shares),
        shares=Decimal(shares),
        confirmation_nav=Decimal("1"),
        confirmation_date=date(2026, 7, 1),
    )
    repository.create_transaction(opening)
    PositionProjector(repository).rebuild(product_id)
    return opening


def seed_cash(repository, product_id, shares):
    seed_product(repository, product_id, ProductType.CASH_MANAGEMENT)
    return seed_opening_position(repository, product_id, shares)


def seed_wallet_plus(repository, product_id, shares):
    from src.portfolio_wallet import WALLET_PROVIDER

    repository.add_product(Product(
        id=product_id,
        provider=WALLET_PROVIDER,
        code="WALLETPLUS",
        name="钱包Plus",
        product_type=ProductType.CASH_MANAGEMENT,
    ))
    return seed_opening_position(repository, product_id, shares)


def seed_quote(repository, product, quote_date, unit_nav):
    quote = MarketQuote(
        product_code=product.code,
        quote_date=quote_date,
        source="test",
        raw_hash=f"{product.id}:{quote_date.isoformat()}",
        unit_nav=unit_nav,
    )
    repository.upsert_quote(product.id, quote, "2026-07-30T12:00:00+08:00")
    return quote


def linked_leg(repository, transaction):
    rows = repository.list_transactions()
    return next(row for row in rows if row.id == transaction.linked_transaction_id)


def test_redemption_waits_for_settlement_then_creates_t1_wallet_purchase(
    services,
):
    repository, transactions, projector = services
    wealth = seed_product(
        repository,
        "wealth",
        ProductType.WEALTH_NAV,
        "W001",
    )
    seed_opening_position(repository, wealth.id, "100000", "100000")
    seed_wallet_plus(repository, "wallet-plus", "1000")

    redemption = transactions.record_redemption(
        wealth.id,
        Decimal("50000"),
        date(2026, 9, 17),
        "web:redemption-settlement",
        destination_cash_product_id="wallet-plus",
        trade_time="2026-09-16T23:13:00",
        settlement_date=date(2026, 9, 20),
    )
    quote = seed_quote(
        repository,
        wealth,
        date(2026, 9, 17),
        Decimal("1.0803"),
    )
    confirmed = transactions.confirm_pending(
        redemption.id,
        quote,
        as_of_date=date(2026, 9, 18),
    )

    assert confirmed.status is TransactionStatus.CONFIRMED
    assert (
        confirmed.settlement_status
        is RedemptionSettlementStatus.PENDING
    )
    assert confirmed.linked_transaction_id == ""
    assert projector.calculate(wealth.id).total_shares == Decimal("50000")
    assert projector.calculate("wallet-plus").total_shares == Decimal("1000")
    assert transactions.settle_redemptions(date(2026, 9, 19)) == []

    settled = transactions.settle_redemptions(date(2026, 9, 20))
    purchase = repository.find_purchase_by_origin(redemption.id)
    assert len(settled) == 1
    assert (
        settled[0].settlement_status
        is RedemptionSettlementStatus.SETTLED
    )
    assert purchase.transaction_type is TransactionType.MANUAL_PURCHASE
    assert purchase.status is TransactionStatus.PENDING_CONFIRMATION
    assert purchase.amount == Decimal("54015")
    assert purchase.trade_date == date(2026, 9, 21)
    assert purchase.confirmation_date is None
    assert projector.calculate("wallet-plus").total_shares == Decimal("55015")
    assert transactions.settle_redemptions(date(2026, 9, 20)) == []
    assert len([
        transaction
        for transaction in repository.list_transactions()
        if transaction.origin_transaction_id == redemption.id
    ]) == 1

    transactions.settle_pending(date(2026, 9, 22))

    assert projector.calculate("wallet-plus").total_shares == Decimal("55015")


def test_pending_wallet_plus_purchase_can_fund_real_time_purchase(services):
    repository, transactions, projector = services
    seed_wallet_plus(repository, "wallet-plus", "0")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)

    wallet_purchase = transactions.record_purchase(
        "wallet-plus",
        Decimal("108000"),
        TRADE_DATE,
        "web:pending-wallet-liquidity",
    )
    fund_purchase = transactions.record_purchase(
        "fund",
        Decimal("50000"),
        TRADE_DATE,
        "web:spend-pending-wallet-liquidity",
        source_cash_product_id="wallet-plus",
    )

    assert wallet_purchase.status is TransactionStatus.PENDING_CONFIRMATION
    assert fund_purchase.status is TransactionStatus.PENDING_QUOTE
    cash_leg = linked_leg(repository, fund_purchase)
    assert cash_leg.status is TransactionStatus.CONFIRMED
    assert cash_leg.confirmation_nav == Decimal("1")
    assert cash_leg.confirmation_date == TRADE_DATE
    assert projector.calculate("wallet-plus").total_shares == Decimal("58000")
    assert projector.calculate("wallet-plus").locked_shares == Decimal("0")
    assert projector.calculate("wallet-plus").available_shares == Decimal("58000")


def test_cancelling_pending_purchase_refunds_realtime_wallet_outflow(services):
    repository, transactions, projector = services
    seed_wallet_plus(repository, "wallet-plus", "108000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    purchase = transactions.record_purchase(
        "fund",
        Decimal("50000"),
        TRADE_DATE,
        "web:wallet-realtime-cancel",
        source_cash_product_id="wallet-plus",
    )

    cancelled = transactions.cancel_pending(
        purchase.id,
        "web:cancel-wallet-realtime",
    )

    assert cancelled.status is TransactionStatus.CANCELLED
    assert linked_leg(repository, purchase).status is TransactionStatus.CONFIRMED
    assert projector.calculate("wallet-plus").total_shares == Decimal("108000")
    refunds = [
        row
        for row in repository.list_transactions(product_id="wallet-plus")
        if row.transaction_type is TransactionType.CASH_TRANSFER_IN
        and row.linked_transaction_id == purchase.linked_transaction_id
    ]
    assert len(refunds) == 1
    assert refunds[0].amount == Decimal("50000")


def _create_settled_wallet_redemption(services):
    repository, transactions, projector = services
    wealth = seed_product(
        repository,
        "wealth",
        ProductType.WEALTH_NAV,
        "W001",
    )
    seed_opening_position(repository, wealth.id, "100000", "100000")
    seed_wallet_plus(repository, "wallet-plus", "1000")
    redemption = transactions.record_redemption(
        wealth.id,
        Decimal("50000"),
        date(2026, 9, 17),
        "web:redemption-for-reversal",
        destination_cash_product_id="wallet-plus",
        trade_time="2026-09-16T23:13:00",
        settlement_date=date(2026, 9, 20),
    )
    quote = seed_quote(
        repository,
        wealth,
        date(2026, 9, 17),
        Decimal("1.0803"),
    )
    transactions.confirm_pending(
        redemption.id,
        quote,
        as_of_date=date(2026, 9, 18),
    )
    transactions.settle_redemptions(date(2026, 9, 20))
    return (
        repository,
        transactions,
        projector,
        repository.get_transaction_by_id(redemption.id),
        repository.find_purchase_by_origin(redemption.id),
    )


def test_reversing_settled_redemption_cancels_pending_wallet_purchase(
    services,
):
    repository, transactions, projector, redemption, purchase = (
        _create_settled_wallet_redemption(services)
    )

    transactions.reverse_confirmed(
        redemption.id,
        "录入错误",
        "reverse:settled-pending-wallet",
    )

    assert (
        repository.get_transaction_by_id(redemption.id).status
        is TransactionStatus.REVERSED
    )
    assert (
        repository.get_transaction_by_id(purchase.id).status
        is TransactionStatus.CANCELLED
    )
    assert projector.calculate("wealth").total_shares == Decimal("100000")
    assert projector.calculate("wallet-plus").total_shares == Decimal("1000")


def test_reversing_settled_redemption_reverses_confirmed_wallet_purchase(
    services,
):
    repository, transactions, projector, redemption, purchase = (
        _create_settled_wallet_redemption(services)
    )
    transactions.settle_pending(date(2026, 9, 22))

    transactions.reverse_confirmed(
        redemption.id,
        "录入错误",
        "reverse:settled-confirmed-wallet",
    )

    assert (
        repository.get_transaction_by_id(redemption.id).status
        is TransactionStatus.REVERSED
    )
    assert (
        repository.get_transaction_by_id(purchase.id).status
        is TransactionStatus.REVERSED
    )
    assert projector.calculate("wealth").total_shares == Decimal("100000")
    assert projector.calculate("wallet-plus").total_shares == Decimal("1000")


def test_external_redemption_settles_without_creating_wallet_purchase(
    services,
):
    repository, transactions, _projector = services
    wealth = seed_product(
        repository,
        "wealth",
        ProductType.WEALTH_NAV,
        "W001",
    )
    seed_opening_position(repository, wealth.id, "100000", "100000")
    redemption = transactions.record_redemption(
        wealth.id,
        Decimal("50000"),
        date(2026, 9, 17),
        "web:external-redemption-settlement",
        trade_time="2026-09-16T23:13:00",
        settlement_date=date(2026, 9, 20),
    )
    quote = seed_quote(
        repository,
        wealth,
        date(2026, 9, 17),
        Decimal("1.0803"),
    )
    transactions.confirm_pending(
        redemption.id,
        quote,
        as_of_date=date(2026, 9, 18),
    )

    settled = transactions.settle_redemptions(date(2026, 9, 20))

    assert len(settled) == 1
    assert (
        settled[0].settlement_status
        is RedemptionSettlementStatus.SETTLED
    )
    assert repository.find_purchase_by_origin(redemption.id) is None


def test_due_legacy_linked_redemption_is_skipped_until_repaired(
    services,
    caplog,
):
    repository, transactions, _projector = services
    seed_product(repository, "wealth", ProductType.WEALTH_NAV, "W001")
    seed_cash(repository, "wallet-plus", "0")
    source = repository.create_transaction(Transaction(
        id="legacy-redemption",
        product_id="wealth",
        transaction_type=TransactionType.MANUAL_REDEMPTION,
        status=TransactionStatus.PENDING_CONFIRMATION,
        trade_date=date(2026, 9, 17),
        idempotency_key="legacy-redemption",
        amount=Decimal("54015"),
        shares=Decimal("50000"),
        confirmation_nav=Decimal("1.0803"),
        confirmation_date=date(2026, 9, 18),
        destination_cash_product_id="wallet-plus",
        settlement_date=date(2026, 9, 20),
        settlement_status=RedemptionSettlementStatus.PENDING,
    ))
    repository.create_transaction(Transaction(
        id="legacy-cash-leg",
        product_id="wallet-plus",
        transaction_type=TransactionType.CASH_TRANSFER_IN,
        status=TransactionStatus.CONFIRMED,
        trade_date=date(2026, 9, 17),
        idempotency_key="legacy-cash-leg",
        amount=Decimal("54015"),
        shares=Decimal("54015"),
        confirmation_nav=Decimal("1"),
        confirmation_date=date(2026, 9, 18),
        linked_transaction_id=source.id,
    ))
    repository.update_pending_transaction(replace(
        source,
        status=TransactionStatus.CONFIRMED,
        linked_transaction_id="legacy-cash-leg",
    ))

    assert transactions.settle_redemptions(date(2026, 9, 20)) == []
    assert repository.find_purchase_by_origin(source.id) is None
    assert "skip legacy linked redemption until repair" in caplog.text


def reversal_children(repository, transaction_id):
    return [
        row
        for row in repository.list_transactions()
        if (
            row.transaction_type is TransactionType.REVERSAL
            and row.linked_transaction_id == transaction_id
        )
    ]


def disable_confirmed_transaction_guard(repository):
    with repository.database.connection() as conn:
        conn.execute("DROP TRIGGER prevent_confirmed_transaction_mutation")


def test_cancelling_pending_purchase_releases_linked_cash_lock(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    seed_product(
        repository,
        "fund",
        ProductType.PUBLIC_FUND,
        "003103",
    )
    purchase = transactions.record_purchase(
        "fund",
        Decimal("2000"),
        TRADE_DATE,
        "web:p",
        source_cash_product_id="cash",
    )

    cancelled = transactions.cancel_pending(
        purchase.id,
        "web:cancel-p",
    )

    assert cancelled.status is TransactionStatus.CANCELLED
    assert linked_leg(repository, purchase).status is TransactionStatus.CANCELLED
    assert projector.calculate("cash").locked_shares == Decimal("0")


def test_confirmed_transaction_requires_reversal_not_cancel(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")
    purchase = transactions.record_purchase(
        "cash",
        Decimal("100"),
        TRADE_DATE,
        "web:confirmed",
    )

    with pytest.raises(
        ValueError,
        match="confirmed transaction cannot be cancelled",
    ):
        transactions.cancel_pending(purchase.id, "web:bad-cancel")

    reversal = transactions.reverse_confirmed(
        purchase.id,
        "录入错误",
        "web:reverse",
    )

    assert reversal.transaction_type is TransactionType.REVERSAL
    assert projector.calculate("cash").total_shares == Decimal("1000")


def test_holding_adjustment_records_only_the_difference(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")

    adjustment = transactions.adjust_holding(
        "cash",
        Decimal("998.50"),
        TRADE_DATE,
        "平台份额校准",
        "web:adjust",
    )

    assert adjustment.shares == Decimal("-1.50")
    assert projector.calculate("cash").total_shares == Decimal("998.50")


def test_holding_profit_adjustment_records_only_the_difference(services):
    repository, transactions, _projector = services
    seed_cash(repository, "cash", "1000")

    first = transactions.adjust_holding_profit(
        "cash",
        Decimal("12.34"),
        TRADE_DATE,
        "平台累计收益校准",
        "web:adjust-profit",
    )
    retried = transactions.adjust_holding_profit(
        "cash",
        Decimal("12.34"),
        TRADE_DATE,
        "平台累计收益校准",
        "web:adjust-profit",
    )

    assert retried == first
    assert first.transaction_type is TransactionType.PROFIT_ADJUSTMENT
    assert first.amount == Decimal("12.34")
    assert first.shares == Decimal("0")


def test_cancel_pending_is_idempotent_and_audited_once(services):
    repository, transactions, _ = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    purchase = transactions.record_purchase(
        "fund",
        Decimal("100"),
        TRADE_DATE,
        "web:pending-for-cancel",
    )

    first = transactions.cancel_pending(
        purchase.id,
        "web:cancel-idempotent",
        actor="operator",
    )
    retried = transactions.cancel_pending(
        purchase.id,
        "web:cancel-idempotent",
        actor="operator",
    )

    assert retried == first
    with repository.database.connection() as conn:
        audits = conn.execute(
            """SELECT action, object_type, object_id, result, source,
                      before_json, after_json
               FROM audit_logs"""
        ).fetchall()
    assert len(audits) == 1
    assert tuple(audits[0][:5]) == (
        "cancel_pending",
        "transaction",
        purchase.id,
        "success",
        "operator",
    )
    assert json.loads(audits[0]["before_json"])["status"] == "pending_quote"
    assert json.loads(audits[0]["after_json"])["status"] == "cancelled"


def test_cancel_sip_updates_execution_and_settlement_skips_it(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    sip = SipService(repository, projector, transactions)
    plan = sip.activate(
        sip.save_plan(
            product_id="fund",
            daily_amount=Decimal("100"),
            purchase_fee_rate=Decimal("0"),
            source_cash_product_id="cash",
            start_date=TRADE_DATE,
        ).id
    )
    cancelled_execution = sip.ensure_intent(plan.id, TRADE_DATE)
    other_execution = sip.ensure_intent(
        plan.id,
        date(2026, 7, 31),
    )

    cancelled = transactions.cancel_pending(
        cancelled_execution.transaction_id,
        "test:cancel-sip-execution",
    )
    retried = transactions.cancel_pending(
        cancelled_execution.transaction_id,
        "test:cancel-sip-execution",
    )
    settled = sip.settle_pending(date(2026, 7, 31))

    updated = sip.get_execution(cancelled_execution.id)
    assert updated.status == "cancelled"
    assert retried == cancelled
    assert updated.reason == "user_cancelled"
    assert sip.get_execution(other_execution.id).status == "pending_quote"
    assert settled == []
    assert projector.calculate("cash").total_shares == Decimal("900")
    plan_rows = [
        row for row in repository.list_transactions()
        if row.plan_id == plan.id
    ]
    assert len([
        row for row in plan_rows
        if row.transaction_type is TransactionType.CASH_TRANSFER_OUT
    ]) == 2
    assert len([
        row for row in plan_rows
        if row.transaction_type is TransactionType.CASH_TRANSFER_IN
    ]) == 1


def test_reverse_sip_updates_execution_across_retry_and_correction(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")
    fund = seed_product(
        repository,
        "fund",
        ProductType.PUBLIC_FUND,
        "003103",
    )
    seed_quote(repository, fund, TRADE_DATE, Decimal("1"))
    sip = SipService(repository, projector, transactions)
    plan = sip.activate(
        sip.save_plan(
            product_id="fund",
            daily_amount=Decimal("100"),
            purchase_fee_rate=Decimal("0"),
            source_cash_product_id="cash",
            start_date=TRADE_DATE,
        ).id
    )
    execution = sip.ensure_intent(plan.id, TRADE_DATE)
    sip.settle_pending(date(2026, 7, 31))

    reversal = transactions.reverse_confirmed(
        execution.transaction_id,
        "撤销定投",
        "test:reverse-sip-execution",
    )
    retried = transactions.reverse_confirmed(
        execution.transaction_id,
        "撤销定投",
        "test:reverse-sip-execution",
    )
    assert retried == reversal
    assert sip.get_execution(execution.id).status == "reversed"

    transactions.reverse_confirmed(
        reversal.id,
        "恢复定投",
        "test:correct-sip-execution",
    )
    corrected = sip.get_execution(execution.id)
    assert corrected.status == "confirmed"
    assert corrected.reason == ""


def test_cancel_retry_rejects_corrupt_linked_final_status(services):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "3000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    purchase = transactions.record_purchase(
        "fund",
        Decimal("1000"),
        TRADE_DATE,
        "web:cancel-retry-corrupt-root",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)
    transactions.cancel_pending(
        purchase.id,
        "web:cancel-retry-corrupt",
    )
    with repository.database.connection() as conn:
        conn.execute(
            """UPDATE transactions SET status = 'pending_quote'
               WHERE id = ?""",
            (cash_leg.id,),
        )

    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.cancel_pending(
            purchase.id,
            "web:cancel-retry-corrupt",
        )


@pytest.mark.parametrize(
    ("row_side", "field", "value"),
    [
        ("target", "amount", "999"),
        ("target", "trade_date", "2026-07-29"),
        ("target", "created_by", "other"),
        ("target", "confirmation_nav", "1"),
        ("linked", "amount", "999"),
        ("linked", "shares", "999"),
        ("linked", "trade_date", "2026-07-29"),
        ("linked", "created_by", "other"),
        ("linked", "confirmation_nav", "1"),
        ("linked", "transaction_type", "cash_transfer_in"),
        ("linked", "product_id", "fund"),
    ],
)
def test_cancel_retry_rejects_any_corrupt_persisted_group_semantics(
    services,
    row_side,
    field,
    value,
):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "3000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    purchase = transactions.record_purchase(
        "fund",
        Decimal("1000"),
        TRADE_DATE,
        f"web:cancel-semantic-root:{row_side}:{field}",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)
    operation_key = f"web:cancel-semantic:{row_side}:{field}"
    transactions.cancel_pending(purchase.id, operation_key)
    row_id = purchase.id if row_side == "target" else cash_leg.id
    with repository.database.connection() as conn:
        conn.execute(
            f"UPDATE transactions SET {field} = ? WHERE id = ?",
            (value, row_id),
        )

    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.cancel_pending(purchase.id, operation_key)


def test_cancel_pending_idempotency_key_rejects_conflicting_request(services):
    repository, transactions, _ = services
    seed_product(repository, "fund-a", ProductType.PUBLIC_FUND)
    seed_product(repository, "fund-b", ProductType.PUBLIC_FUND)
    purchase_a = transactions.record_purchase(
        "fund-a", Decimal("100"), TRADE_DATE, "web:pending-a"
    )
    purchase_b = transactions.record_purchase(
        "fund-b", Decimal("100"), TRADE_DATE, "web:pending-b"
    )
    transactions.cancel_pending(
        purchase_a.id,
        "web:cancel-conflict",
        actor="operator",
    )

    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.cancel_pending(
            purchase_b.id,
            "web:cancel-conflict",
            actor="operator",
        )
    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.cancel_pending(
            purchase_a.id,
            "web:cancel-conflict",
            actor="other-operator",
        )

    assert (
        repository.get_transaction_by_id(purchase_b.id).status
        is TransactionStatus.PENDING_QUOTE
    )


@pytest.mark.parametrize("record_kind", ["purchase", "redemption"])
def test_record_trade_rejects_idempotency_key_already_used_by_operation_audit(
    services,
    record_kind,
):
    repository, transactions, _ = services
    seed_product(repository, "pending", ProductType.PUBLIC_FUND)
    pending = transactions.record_purchase(
        "pending",
        Decimal("100"),
        TRADE_DATE,
        f"web:global-idempotency-pending:{record_kind}",
    )
    operation_key = f"web:global-operation-key:{record_kind}"
    transactions.cancel_pending(pending.id, operation_key)
    seed_cash(repository, "cash", "1000")

    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        if record_kind == "purchase":
            transactions.record_purchase(
                "cash",
                Decimal("10"),
                TRADE_DATE,
                operation_key,
            )
        else:
            transactions.record_redemption(
                "cash",
                Decimal("10"),
                TRADE_DATE,
                operation_key,
            )

    rows_with_key = [
        row
        for row in repository.list_transactions()
        if row.idempotency_key == operation_key
    ]
    assert rows_with_key == []


def test_whitespace_variant_cannot_cross_record_and_operation_namespaces(
    services,
):
    repository, transactions, _ = services
    seed_product(repository, "pending-a", ProductType.PUBLIC_FUND)
    pending_a = transactions.record_purchase(
        "pending-a",
        Decimal("100"),
        TRADE_DATE,
        "web:pending-a-for-normalized-global",
    )
    transactions.cancel_pending(
        pending_a.id,
        " normalized-global-key ",
    )
    seed_cash(repository, "cash", "1000")

    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.record_purchase(
            "cash",
            Decimal("10"),
            TRADE_DATE,
            "normalized-global-key",
        )

    seed_product(repository, "pending-b", ProductType.PUBLIC_FUND)
    pending_b = transactions.record_purchase(
        "pending-b",
        Decimal("100"),
        TRADE_DATE,
        "web:pending-b-for-normalized-global",
    )
    transactions.record_redemption(
        "cash",
        Decimal("10"),
        TRADE_DATE,
        "record-first-normalized-key",
    )

    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.cancel_pending(
            pending_b.id,
            " record-first-normalized-key ",
        )


def test_operation_rejects_idempotency_key_already_used_by_record_trade(
    services,
):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "1000")
    purchase = transactions.record_purchase(
        "cash",
        Decimal("10"),
        TRADE_DATE,
        "web:trade-key-used-first",
    )
    seed_product(repository, "pending", ProductType.PUBLIC_FUND)
    pending = transactions.record_purchase(
        "pending",
        Decimal("10"),
        TRADE_DATE,
        "web:pending-for-global-conflict",
    )

    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.cancel_pending(
            pending.id,
            purchase.idempotency_key,
        )

    assert (
        repository.get_transaction_by_id(pending.id).status
        is TransactionStatus.PENDING_QUOTE
    )


def test_cancel_pending_rejects_inconsistent_link_without_partial_update(
    services,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    purchase = transactions.record_purchase(
        "fund",
        Decimal("1000"),
        TRADE_DATE,
        "web:pending-corrupt-link",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE transactions SET shares = '999' WHERE id = ?",
            (cash_leg.id,),
        )

    with pytest.raises(ValueError, match="inconsistent linked transaction"):
        transactions.cancel_pending(
            purchase.id,
            "web:cancel-corrupt-link",
        )

    assert (
        repository.get_transaction_by_id(purchase.id).status
        is TransactionStatus.PENDING_QUOTE
    )
    assert projector.calculate("cash").locked_shares == Decimal("999")


def test_link_validation_rejects_extra_reverse_cash_candidate(services):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "3000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    purchase = transactions.record_purchase(
        "fund",
        Decimal("1000"),
        TRADE_DATE,
        "web:pending-extra-cash",
        source_cash_product_id="cash",
    )
    with repository.database.connection() as conn:
        conn.execute("DROP INDEX idx_one_cash_leg_per_transaction")
    repository.create_transaction(
        Transaction(
            id="extra-cash-leg",
            product_id="cash",
            transaction_type=TransactionType.CASH_TRANSFER_OUT,
            status=TransactionStatus.PENDING_QUOTE,
            trade_date=TRADE_DATE,
            idempotency_key="web:extra-cash-leg",
            amount=Decimal("1000"),
            shares=Decimal("1000"),
            linked_transaction_id=purchase.id,
            created_by="web",
        )
    )

    with pytest.raises(ValueError, match="inconsistent linked transaction"):
        transactions.cancel_pending(
            purchase.id,
            "web:cancel-extra-cash",
        )

    assert (
        repository.get_transaction_by_id(purchase.id).status
        is TransactionStatus.PENDING_QUOTE
    )


def test_cancel_pending_rolls_back_rows_and_position_when_audit_fails(
    services,
    monkeypatch,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    purchase = transactions.record_purchase(
        "fund",
        Decimal("1000"),
        TRADE_DATE,
        "web:pending-cancel-rollback",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(repository, "append_audit", fail_audit)

    with pytest.raises(RuntimeError, match="audit failed"):
        transactions.cancel_pending(
            purchase.id,
            "web:cancel-rollback",
        )

    assert (
        repository.get_transaction_by_id(purchase.id).status
        is TransactionStatus.PENDING_QUOTE
    )
    assert (
        repository.get_transaction_by_id(cash_leg.id).status
        is TransactionStatus.PENDING_QUOTE
    )
    assert projector.calculate("cash").locked_shares == Decimal("1000")


def test_cancel_audit_records_complete_target_and_linked_final_status(
    services,
):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "3000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    purchase = transactions.record_purchase(
        "fund",
        Decimal("1000"),
        TRADE_DATE,
        "web:cancel-audit-root",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)

    transactions.cancel_pending(
        purchase.id,
        "web:cancel-audit-complete",
        actor="operator",
    )

    with repository.database.connection() as conn:
        row = conn.execute(
            """SELECT after_json FROM audit_logs
               WHERE action = 'cancel_pending'"""
        ).fetchone()
    after = json.loads(row["after_json"])
    assert after["transaction_id"] == purchase.id
    assert after["target_status"] == "cancelled"
    assert after["linked_transaction_id"] == cash_leg.id
    assert after["linked_status"] == "cancelled"


def test_reverse_confirmed_negates_complete_values_and_linked_cash_leg(
    services,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_quote(repository, fund, TRADE_DATE, Decimal("2"))
    purchase = transactions.record_purchase(
        "fund",
        Decimal("2000"),
        TRADE_DATE,
        "web:confirmed-linked",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)

    reversal = transactions.reverse_confirmed(
        purchase.id,
        "录入错误",
        "web:reverse-linked",
        actor="operator",
    )

    assert reversal.product_id == purchase.product_id
    assert reversal.linked_transaction_id == purchase.id
    assert reversal.shares == -purchase.shares
    assert reversal.amount == -purchase.amount
    assert (
        repository.get_transaction_by_id(purchase.id).status
        is TransactionStatus.REVERSED
    )
    assert (
        repository.get_transaction_by_id(cash_leg.id).status
        is TransactionStatus.REVERSED
    )
    cash_reversals = [
        row
        for row in repository.list_transactions(product_id="cash")
        if row.transaction_type is TransactionType.REVERSAL
    ]
    assert len(cash_reversals) == 1
    assert cash_reversals[0].linked_transaction_id == cash_leg.id
    assert cash_reversals[0].shares == -cash_leg.shares
    assert cash_reversals[0].amount == -cash_leg.amount
    assert projector.calculate("fund").total_shares == Decimal("0")
    assert projector.calculate("cash").total_shares == Decimal("3000")


def test_reverse_audit_records_complete_group_status_timestamp_and_values(
    services,
):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "3000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_quote(repository, fund, TRADE_DATE, Decimal("2"))
    purchase = transactions.record_purchase(
        "fund",
        Decimal("2000"),
        TRADE_DATE,
        "web:reverse-audit-root",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)

    returned = transactions.reverse_confirmed(
        purchase.id,
        "审计完整性",
        "web:reverse-audit-complete",
        actor="operator",
    )

    with repository.database.connection() as conn:
        row = conn.execute(
            """SELECT after_json FROM audit_logs
               WHERE action = 'reverse_confirmed'"""
        ).fetchone()
    after = json.loads(row["after_json"])
    assert after["reversal_id"] == returned.id
    assert after["linked_reversal_id"]
    assert after["target_status"] == "reversed"
    assert after["linked_transaction_id"] == cash_leg.id
    assert after["linked_status"] == "reversed"
    assert after["target_reversed_at"]
    assert after["linked_reversed_at"]
    assert {
        (target["id"], target["status"])
        for target in after["targets"]
    } == {
        (purchase.id, "reversed"),
        (cash_leg.id, "reversed"),
    }
    assert all(target["reversed_at"] for target in after["targets"])
    assert {
        (
            reversal["original_id"],
            Decimal(reversal["shares"]),
            Decimal(reversal["amount"]),
        )
        for reversal in after["reversals"]
    } == {
        (purchase.id, -purchase.shares, -purchase.amount),
        (cash_leg.id, -cash_leg.shares, -cash_leg.amount),
    }


def test_reverse_confirmed_from_cash_leg_reverses_complete_business_group(
    services,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_quote(repository, fund, TRADE_DATE, Decimal("2"))
    purchase = transactions.record_purchase(
        "fund",
        Decimal("2000"),
        TRADE_DATE,
        "web:cash-entry-root",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)

    cash_reversal = transactions.reverse_confirmed(
        cash_leg.id,
        "现金腿入口冲正",
        "web:cash-entry-reverse",
    )

    assert cash_reversal.linked_transaction_id == cash_leg.id
    assert len(reversal_children(repository, cash_leg.id)) == 1
    assert len(reversal_children(repository, purchase.id)) == 1
    assert (
        repository.get_transaction_by_id(cash_leg.id).status
        is TransactionStatus.REVERSED
    )
    assert (
        repository.get_transaction_by_id(purchase.id).status
        is TransactionStatus.REVERSED
    )
    assert projector.calculate("fund").total_shares == Decimal("0")
    assert projector.calculate("cash").total_shares == Decimal("3000")


@pytest.mark.parametrize("entry_side", ["main", "cash"])
def test_reversing_either_first_reversal_restores_complete_business_group(
    services,
    entry_side,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_quote(repository, fund, TRADE_DATE, Decimal("2"))
    purchase = transactions.record_purchase(
        "fund",
        Decimal("2000"),
        TRADE_DATE,
        f"web:group-chain-root:{entry_side}",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)
    main_reversal = transactions.reverse_confirmed(
        purchase.id,
        "首次冲正",
        f"web:group-chain-first:{entry_side}",
    )
    cash_reversal = reversal_children(repository, cash_leg.id)[0]
    entry = main_reversal if entry_side == "main" else cash_reversal

    returned = transactions.reverse_confirmed(
        entry.id,
        "恢复业务组",
        f"web:group-chain-second:{entry_side}",
    )

    sibling = cash_reversal if entry_side == "main" else main_reversal
    assert returned.linked_transaction_id == entry.id
    assert (
        repository.get_transaction_by_id(entry.id).status
        is TransactionStatus.REVERSED
    )
    assert (
        repository.get_transaction_by_id(sibling.id).status
        is TransactionStatus.REVERSED
    )
    assert len(reversal_children(repository, entry.id)) == 1
    assert len(reversal_children(repository, sibling.id)) == 1
    assert projector.calculate("fund").total_shares == Decimal("1000")
    assert projector.calculate("cash").total_shares == Decimal("1000")


@pytest.mark.parametrize(
    "sibling_corruption",
    ["missing", "duplicate", "values"],
)
def test_reversing_group_reversal_rejects_corrupt_sibling_without_partial_write(
    services,
    sibling_corruption,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_quote(repository, fund, TRADE_DATE, Decimal("2"))
    purchase = transactions.record_purchase(
        "fund",
        Decimal("2000"),
        TRADE_DATE,
        f"web:corrupt-sibling-root:{sibling_corruption}",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)
    main_reversal = transactions.reverse_confirmed(
        purchase.id,
        "首次冲正",
        f"web:corrupt-sibling-first:{sibling_corruption}",
    )
    cash_reversal = reversal_children(repository, cash_leg.id)[0]
    with repository.database.connection() as conn:
        if sibling_corruption == "missing":
            conn.execute("DROP TRIGGER prevent_final_transaction_delete")
            conn.execute(
                "DELETE FROM transactions WHERE id = ?",
                (cash_reversal.id,),
            )
        elif sibling_corruption == "duplicate":
            conn.execute(
                "DROP INDEX idx_one_reversal_child_per_transaction"
            )
            conn.execute(
                """INSERT INTO transactions
                   (id, product_id, transaction_type, status, trade_date,
                    amount, shares, linked_transaction_id, idempotency_key,
                    note, created_by)
                   VALUES ('duplicate-cash-reversal', 'cash', 'reversal',
                           'confirmed', ?, '-2000', '-2000', ?,
                           'duplicate-cash-reversal-key', '首次冲正', 'web')""",
                (TRADE_DATE.isoformat(), cash_leg.id),
            )
        else:
            conn.execute(
                "DROP TRIGGER prevent_confirmed_transaction_mutation"
            )
            conn.execute(
                "UPDATE transactions SET shares = '-1999' WHERE id = ?",
                (cash_reversal.id,),
            )

    with pytest.raises(ValueError, match="inconsistent reversal group"):
        transactions.reverse_confirmed(
            main_reversal.id,
            "恢复业务组",
            f"web:corrupt-sibling-second:{sibling_corruption}",
        )

    assert (
        repository.get_transaction_by_id(main_reversal.id).status
        is TransactionStatus.CONFIRMED
    )
    assert reversal_children(repository, main_reversal.id) == []
    assert projector.calculate("fund").total_shares == Decimal("0")


def test_group_reversal_of_reversal_rolls_back_both_sides_when_audit_fails(
    services,
    monkeypatch,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_quote(repository, fund, TRADE_DATE, Decimal("2"))
    purchase = transactions.record_purchase(
        "fund",
        Decimal("2000"),
        TRADE_DATE,
        "web:group-rollback-root",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)
    main_reversal = transactions.reverse_confirmed(
        purchase.id,
        "首次冲正",
        "web:group-rollback-first",
    )
    cash_reversal = reversal_children(repository, cash_leg.id)[0]

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(repository, "append_audit", fail_audit)

    with pytest.raises(RuntimeError, match="audit failed"):
        transactions.reverse_confirmed(
            main_reversal.id,
            "恢复业务组",
            "web:group-rollback-second",
        )

    assert (
        repository.get_transaction_by_id(main_reversal.id).status
        is TransactionStatus.CONFIRMED
    )
    assert (
        repository.get_transaction_by_id(cash_reversal.id).status
        is TransactionStatus.CONFIRMED
    )
    assert reversal_children(repository, main_reversal.id) == []
    assert reversal_children(repository, cash_reversal.id) == []
    assert projector.calculate("fund").total_shares == Decimal("0")
    assert projector.calculate("cash").total_shares == Decimal("3000")


def test_reverse_confirmed_is_idempotent_and_rejects_conflicts(services):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "1000")
    purchase = transactions.record_purchase(
        "cash", Decimal("100"), TRADE_DATE, "web:confirmed-idempotent"
    )

    first = transactions.reverse_confirmed(
        purchase.id,
        "录入错误",
        "web:reverse-idempotent",
        actor="operator",
    )
    retried = transactions.reverse_confirmed(
        purchase.id,
        "录入错误",
        "web:reverse-idempotent",
        actor="operator",
    )

    assert retried == first
    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.reverse_confirmed(
            purchase.id,
            "不同原因",
            "web:reverse-idempotent",
            actor="operator",
        )
    with repository.database.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = 'reverse_confirmed'"
        ).fetchone()[0] == 1
    assert len(
        [
            row
            for row in repository.list_transactions(product_id="cash")
            if row.transaction_type is TransactionType.REVERSAL
        ]
    ) == 1


def test_reverse_retry_rejects_missing_group_sibling(services):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "3000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_quote(repository, fund, TRADE_DATE, Decimal("2"))
    purchase = transactions.record_purchase(
        "fund",
        Decimal("2000"),
        TRADE_DATE,
        "web:reverse-retry-corrupt-root",
        source_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, purchase)
    transactions.reverse_confirmed(
        purchase.id,
        "首次冲正",
        "web:reverse-retry-corrupt",
    )
    cash_reversal = reversal_children(repository, cash_leg.id)[0]
    with repository.database.connection() as conn:
        conn.execute("DROP TRIGGER prevent_final_transaction_delete")
        conn.execute(
            "DELETE FROM transactions WHERE id = ?",
            (cash_reversal.id,),
        )

    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.reverse_confirmed(
            purchase.id,
            "首次冲正",
            "web:reverse-retry-corrupt",
        )


def test_reverse_confirmed_supports_reversal_chain(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")
    purchase = transactions.record_purchase(
        "cash", Decimal("100"), TRADE_DATE, "web:chain-root"
    )
    first_reversal = transactions.reverse_confirmed(
        purchase.id,
        "撤销购买",
        "web:chain-r1",
    )

    second_reversal = transactions.reverse_confirmed(
        first_reversal.id,
        "恢复购买",
        "web:chain-r2",
    )

    assert second_reversal.shares == -first_reversal.shares
    assert second_reversal.amount == -first_reversal.amount
    assert (
        repository.get_transaction_by_id(first_reversal.id).status
        is TransactionStatus.REVERSED
    )
    assert projector.calculate("cash").total_shares == Decimal("1100")


def test_reverse_confirmed_rolls_back_original_reversal_and_position_on_audit_failure(
    services,
    monkeypatch,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")
    purchase = transactions.record_purchase(
        "cash", Decimal("100"), TRADE_DATE, "web:reverse-rollback-root"
    )

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(repository, "append_audit", fail_audit)

    with pytest.raises(RuntimeError, match="audit failed"):
        transactions.reverse_confirmed(
            purchase.id,
            "录入错误",
            "web:reverse-rollback",
        )

    assert (
        repository.get_transaction_by_id(purchase.id).status
        is TransactionStatus.CONFIRMED
    )
    assert repository.get_transaction_by_idempotency(
        "web:reverse-rollback"
    ) is None
    assert projector.calculate("cash").total_shares == Decimal("1100")


def test_concurrent_reversal_retries_create_one_reversal_and_one_audit(
    services,
):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "1000")
    purchase = transactions.record_purchase(
        "cash", Decimal("100"), TRADE_DATE, "web:concurrent-reverse-root"
    )
    second_service = PortfolioTransactionService(
        repository,
        PositionProjector(repository),
    )
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def reverse(service):
        try:
            barrier.wait()
            results.append(
                service.reverse_confirmed(
                    purchase.id,
                    "录入错误",
                    "web:concurrent-reverse",
                    actor="operator",
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=reverse, args=(transactions,)),
        threading.Thread(target=reverse, args=(second_service,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    with repository.database.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = 'reverse_confirmed'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "actual_shares",
    [
        Decimal("-0.01"),
        Decimal("NaN"),
        Decimal("Infinity"),
        "not-a-number",
    ],
)
def test_adjust_holding_rejects_invalid_actual_shares(
    services,
    actual_shares,
):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "1000")

    with pytest.raises(
        ValueError,
        match="actual_shares must be non-negative and finite",
    ):
        transactions.adjust_holding(
            "cash",
            actual_shares,
            TRADE_DATE,
            "平台份额校准",
            f"web:invalid-adjust:{actual_shares}",
        )


def test_adjust_holding_records_positive_and_zero_differences(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")

    increase = transactions.adjust_holding(
        "cash",
        Decimal("1002.5"),
        TRADE_DATE,
        "平台份额校准",
        "web:adjust-up",
    )
    no_change = transactions.adjust_holding(
        "cash",
        Decimal("1002.5"),
        TRADE_DATE,
        "平台份额复核",
        "web:adjust-zero",
    )

    assert increase.shares == Decimal("2.5")
    assert increase.amount == Decimal("2.5")
    assert no_change.shares == Decimal("0")
    assert no_change.amount == Decimal("0")
    assert projector.calculate("cash").total_shares == Decimal("1002.5")
    assert projector.calculate("cash").cost_basis == Decimal("1002.5")


@pytest.mark.parametrize(
    "product_type",
    [ProductType.PUBLIC_FUND, ProductType.WEALTH_NAV],
)
def test_positive_non_cash_adjustment_preserves_existing_average_cost(
    services,
    product_type,
):
    repository, transactions, projector = services
    seed_product(repository, "fund", product_type)
    seed_opening_position(repository, "fund", "100", amount="250")

    adjustment = transactions.adjust_holding(
        "fund",
        Decimal("110"),
        TRADE_DATE,
        "平台份额校准",
        f"web:adjust-average-cost:{product_type.value}",
    )

    assert adjustment.shares == Decimal("10")
    assert adjustment.amount == Decimal("25")
    position = projector.calculate("fund")
    assert position.total_shares == Decimal("110")
    assert position.cost_basis == Decimal("275")


@pytest.mark.parametrize(
    "product_type",
    [ProductType.PUBLIC_FUND, ProductType.WEALTH_NAV],
)
def test_positive_non_cash_adjustment_from_zero_uses_exact_quote_cost(
    services,
    product_type,
):
    repository, transactions, projector = services
    product = seed_product(repository, "fund", product_type)
    seed_quote(repository, product, TRADE_DATE, Decimal("2.5"))

    adjustment = transactions.adjust_holding(
        "fund",
        Decimal("10"),
        TRADE_DATE,
        "平台份额校准",
        f"web:adjust-quote-cost:{product_type.value}",
    )

    assert adjustment.shares == Decimal("10")
    assert adjustment.amount == Decimal("25")
    position = projector.calculate("fund")
    assert position.total_shares == Decimal("10")
    assert position.cost_basis == Decimal("25")


def test_positive_non_cash_adjustment_from_zero_requires_exact_quote(
    services,
):
    repository, transactions, projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)

    with pytest.raises(
        ValueError,
        match="positive adjustment from zero requires an exact quote",
    ):
        transactions.adjust_holding(
            "fund",
            Decimal("10"),
            TRADE_DATE,
            "平台份额校准",
            "web:adjust-missing-quote",
        )

    assert repository.get_transaction_by_idempotency(
        "web:adjust-missing-quote"
    ) is None
    assert projector.calculate("fund").total_shares == Decimal("0")
    with repository.database.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_logs"
        ).fetchone()[0] == 0


def test_positive_non_cash_adjustment_rejects_zero_existing_cost_basis(
    services,
):
    repository, transactions, projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_opening_position(repository, "fund", "100", amount="0")

    with pytest.raises(
        ValueError,
        match="positive adjustment requires a positive average cost",
    ):
        transactions.adjust_holding(
            "fund",
            Decimal("101"),
            TRADE_DATE,
            "平台份额校准",
            "web:adjust-zero-cost-basis",
        )

    assert repository.get_transaction_by_idempotency(
        "web:adjust-zero-cost-basis"
    ) is None
    assert projector.calculate("fund").cost_basis == Decimal("0")


def test_adjust_holding_is_idempotent_against_original_actual_request(
    services,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")

    first = transactions.adjust_holding(
        "cash",
        Decimal("998.5"),
        TRADE_DATE,
        "平台份额校准",
        "web:adjust-idempotent",
        actor="operator",
    )
    retried = transactions.adjust_holding(
        "cash",
        Decimal("998.50"),
        TRADE_DATE,
        "平台份额校准",
        "web:adjust-idempotent",
        actor="operator",
    )

    assert retried == first
    assert projector.calculate("cash").total_shares == Decimal("998.5")
    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.adjust_holding(
            "cash",
            Decimal("997"),
            TRADE_DATE,
            "平台份额校准",
            "web:adjust-idempotent",
            actor="operator",
        )
    with repository.database.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = 'adjust_holding'"
        ).fetchone()[0] == 1


def test_adjustment_services_allow_reason_to_be_omitted(services):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "1000")

    holding_adjustment = transactions.adjust_holding(
        "cash",
        Decimal("999"),
        TRADE_DATE,
        idempotency_key="web:adjust-without-reason",
    )
    profit_adjustment = transactions.adjust_holding_profit(
        "cash",
        Decimal("1"),
        TRADE_DATE,
        idempotency_key="web:adjust-profit-without-reason",
    )
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_opening_position(repository, "fund", "100")
    seed_quote(repository, fund, date(2026, 7, 29), Decimal("1"))
    seed_quote(repository, fund, TRADE_DATE, Decimal("1.1"))
    latest_profit_adjustment = transactions.adjust_latest_profit(
        "fund",
        Decimal("7.5"),
        TRADE_DATE,
        idempotency_key="web:adjust-latest-without-reason",
    )

    assert holding_adjustment.note == ""
    assert profit_adjustment.note == ""
    assert latest_profit_adjustment.note == ""


def test_cash_latest_profit_adjustment_updates_shares_and_market_value(services):
    """现金最新收益校准差额应计入份额（金额=份额），从而改变持仓市值/总资产。"""
    from src.portfolio_view import build_portfolio_payload

    repository, transactions, projector = services
    seed_cash(repository, "cash", "10000")
    repository.create_transaction(
        Transaction(
            id="income:cash:latest",
            product_id="cash",
            transaction_type=TransactionType.INCOME_ACCRUAL,
            status=TransactionStatus.CONFIRMED,
            trade_date=TRADE_DATE,
            confirmation_date=TRADE_DATE,
            idempotency_key="income:cash:latest",
            amount=Decimal("3.146"),
            shares=Decimal("3.146"),
            confirmation_nav=Decimal("1"),
        )
    )
    projector.rebuild("cash")
    before = build_portfolio_payload(repository, as_of=TRADE_DATE)
    cash_before = next(row for row in before["products"] if row["id"] == "cash")
    assert cash_before["latest_profit"] == "3.146"
    assert cash_before["shares"] == "10003.146"
    assert cash_before["market_value"] == "10003.146"

    adjustment = transactions.adjust_latest_profit(
        "cash",
        Decimal("9.44"),
        TRADE_DATE,
        "校准最新收益",
        "web:cash-latest-profit-shares",
    )

    assert adjustment.amount == Decimal("6.294")
    assert adjustment.shares == Decimal("6.294")
    after = build_portfolio_payload(repository, as_of=TRADE_DATE)
    cash_after = next(row for row in after["products"] if row["id"] == "cash")
    assert cash_after["latest_profit"] == "9.44"
    assert cash_after["shares"] == "10009.44"
    assert cash_after["market_value"] == "10009.44"
    assert after["summary"]["market_value"] == "10009.44"


def test_nav_latest_profit_adjustment_does_not_change_shares(services):
    """净值产品校准最新收益只改收益展示，不改份额/市值。"""
    from src.portfolio_view import build_portfolio_payload

    repository, transactions, projector = services
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_opening_position(repository, "fund", "100")
    seed_quote(repository, fund, date(2026, 7, 29), Decimal("1"))
    seed_quote(repository, fund, TRADE_DATE, Decimal("1.1"))
    projector.rebuild("fund")
    before = build_portfolio_payload(repository, as_of=TRADE_DATE)
    fund_before = next(row for row in before["products"] if row["id"] == "fund")

    adjustment = transactions.adjust_latest_profit(
        "fund",
        Decimal("7.5"),
        TRADE_DATE,
        "校准最新收益",
        "web:fund-latest-profit-no-shares",
    )

    assert adjustment.shares == Decimal("0")
    after = build_portfolio_payload(repository, as_of=TRADE_DATE)
    fund_after = next(row for row in after["products"] if row["id"] == "fund")
    assert fund_after["latest_profit"] == "7.5"
    assert fund_after["shares"] == fund_before["shares"]
    assert fund_after["market_value"] == fund_before["market_value"]


def test_adjust_holding_retry_rejects_corrupt_cost_payload(services):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "1000")
    adjustment = transactions.adjust_holding(
        "cash",
        Decimal("1002.5"),
        TRADE_DATE,
        "平台份额校准",
        "web:adjust-corrupt-cost-retry",
    )
    assert adjustment.amount == Decimal("2.5")
    with repository.database.connection() as conn:
        conn.execute("DROP TRIGGER prevent_confirmed_transaction_mutation")
        conn.execute(
            "UPDATE transactions SET amount = '0' WHERE id = ?",
            (adjustment.id,),
        )

    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.adjust_holding(
            "cash",
            Decimal("1002.5"),
            TRADE_DATE,
            "平台份额校准",
            "web:adjust-corrupt-cost-retry",
        )


@pytest.mark.parametrize(
    (
        "product_type",
        "opening_shares",
        "opening_amount",
        "actual_shares",
        "quote_nav",
        "corrupt_rule",
    ),
    [
        (
            ProductType.CASH_MANAGEMENT,
            "1000",
            "1000",
            "1002.5",
            None,
            "preserve_average_cost",
        ),
        (
            ProductType.PUBLIC_FUND,
            "100",
            "250",
            "110",
            None,
            "exact_quote",
        ),
        (
            ProductType.PUBLIC_FUND,
            None,
            None,
            "10",
            "2.5",
            "preserve_average_cost",
        ),
    ],
)
def test_adjust_holding_retry_rejects_cost_rule_inconsistent_with_context(
    services,
    product_type,
    opening_shares,
    opening_amount,
    actual_shares,
    quote_nav,
    corrupt_rule,
):
    repository, transactions, _ = services
    product = seed_product(repository, "product", product_type)
    if opening_shares is not None:
        seed_opening_position(
            repository,
            product.id,
            opening_shares,
            amount=opening_amount,
        )
    if quote_nav is not None:
        seed_quote(repository, product, TRADE_DATE, Decimal(quote_nav))

    key = f"web:adjust-corrupt-rule:{product_type.value}:{actual_shares}"
    transactions.adjust_holding(
        product.id,
        Decimal(actual_shares),
        TRADE_DATE,
        "平台份额校准",
        key,
    )
    with repository.database.connection() as conn:
        row = conn.execute(
            """SELECT id, after_json FROM audit_logs
               WHERE action = 'adjust_holding'"""
        ).fetchone()
        after = json.loads(row["after_json"])
        after["cost_basis_rule"] = corrupt_rule
        conn.execute(
            "UPDATE audit_logs SET after_json = ? WHERE id = ?",
            (
                json.dumps(
                    after,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                row["id"],
            ),
        )

    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.adjust_holding(
            product.id,
            Decimal(actual_shares),
            TRADE_DATE,
            "平台份额校准",
            key,
        )


def test_adjust_holding_rolls_back_ledger_and_position_when_audit_fails(
    services,
    monkeypatch,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(repository, "append_audit", fail_audit)

    with pytest.raises(RuntimeError, match="audit failed"):
        transactions.adjust_holding(
            "cash",
            Decimal("900"),
            TRADE_DATE,
            "平台份额校准",
            "web:adjust-rollback",
        )

    assert repository.get_transaction_by_idempotency(
        "web:adjust-rollback"
    ) is None
    assert projector.calculate("cash").total_shares == Decimal("1000")
    assert repository.get_position("cash").total_shares == Decimal("1000")


def test_concurrent_holding_adjustments_serialize_without_negative_holding(
    services,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")
    second_service = PortfolioTransactionService(
        repository,
        PositionProjector(repository),
    )
    barrier = threading.Barrier(2)
    errors = []

    def adjust(service, actual, key):
        try:
            barrier.wait()
            service.adjust_holding(
                "cash",
                actual,
                TRADE_DATE,
                "并发平台校准",
                key,
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(
            target=adjust,
            args=(transactions, Decimal("900"), "web:adjust-concurrent-a"),
        ),
        threading.Thread(
            target=adjust,
            args=(second_service, Decimal("800"), "web:adjust-concurrent-b"),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert projector.calculate("cash").total_shares in {
        Decimal("800"),
        Decimal("900"),
    }
    assert len(
        [
            row
            for row in repository.list_transactions(product_id="cash")
            if row.transaction_type is TransactionType.HOLDING_ADJUSTMENT
        ]
    ) == 2


@pytest.mark.parametrize(
    ("method_name", "reason", "actor", "expected_message"),
    [
        ("reverse", " ", "web", "reason must not be empty"),
        ("reverse", "录入错误", " ", "actor must not be empty"),
        ("adjust", "平台份额校准", " ", "actor must not be empty"),
    ],
)
def test_reason_and_actor_must_be_nonempty(
    services,
    method_name,
    reason,
    actor,
    expected_message,
):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "1000")
    if method_name == "reverse":
        transaction = transactions.record_purchase(
            "cash", Decimal("1"), TRADE_DATE, f"web:validate:{reason}:{actor}"
        )
        call = lambda: transactions.reverse_confirmed(
            transaction.id,
            reason,
            f"web:reverse-validate:{reason}:{actor}",
            actor=actor,
        )
    else:
        call = lambda: transactions.adjust_holding(
            "cash",
            Decimal("1000"),
            TRADE_DATE,
            reason,
            f"web:adjust-validate:{reason}:{actor}",
            actor=actor,
        )

    with pytest.raises(ValueError, match=expected_message):
        call()


def test_fund_purchase_waits_for_exact_quote_and_locks_source(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_quote(repository, fund, date(2026, 7, 29), Decimal("1.2"))

    purchase = transactions.record_purchase(
        "fund",
        Decimal("2000"),
        TRADE_DATE,
        "web:purchase-1",
        source_cash_product_id="cash",
        fee_rate=Decimal("0.00006"),
    )

    assert purchase.status is TransactionStatus.PENDING_QUOTE
    assert purchase.shares is None
    assert projector.calculate("cash").locked_shares == Decimal("2000")
    cash_leg = linked_leg(repository, purchase)
    assert cash_leg.transaction_type is TransactionType.CASH_TRANSFER_OUT
    assert cash_leg.status is purchase.status
    assert cash_leg.linked_transaction_id == purchase.id


def test_cash_purchase_with_source_confirms_both_sides_atomically(services):
    repository, transactions, projector = services
    seed_cash(repository, "source", "3000")
    seed_cash(repository, "target", "0")

    purchase = transactions.record_purchase(
        "target",
        Decimal("500"),
        TRADE_DATE,
        "web:cash-1",
        source_cash_product_id="source",
    )

    cash_leg = linked_leg(repository, purchase)
    assert purchase.status is TransactionStatus.CONFIRMED
    assert cash_leg.status is TransactionStatus.CONFIRMED
    assert cash_leg.linked_transaction_id == purchase.id
    assert projector.calculate("source").total_shares == Decimal("2500")
    assert projector.calculate("target").total_shares == Decimal("500")


def test_purchase_with_exact_quote_confirms_and_applies_fee(services):
    repository, transactions, projector = services
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_quote(repository, fund, TRADE_DATE, Decimal("2"))

    purchase = transactions.record_purchase(
        "fund",
        Decimal("1000"),
        TRADE_DATE,
        "web:exact",
        fee_rate=Decimal("0.01"),
    )

    assert purchase.status is TransactionStatus.CONFIRMED
    assert purchase.confirmation_nav == Decimal("2")
    assert purchase.confirmation_date == TRADE_DATE
    assert purchase.fee_amount == Decimal("10")
    assert purchase.shares == Decimal("495")
    assert projector.calculate("fund").total_shares == Decimal("495")


def test_purchase_retry_is_idempotent_and_does_not_lock_source_twice(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")

    first = transactions.record_purchase(
        "fund",
        Decimal("1000"),
        TRADE_DATE,
        "web:retry",
        source_cash_product_id="cash",
    )
    retry = transactions.record_purchase(
        "fund",
        Decimal("1000"),
        TRADE_DATE,
        "web:retry",
        source_cash_product_id="cash",
    )

    assert retry == first
    assert len(repository.list_transactions()) == 3
    assert projector.calculate("cash").locked_shares == Decimal("1000")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product_id", "other-fund"),
        ("amount", Decimal("1001")),
        ("trade_date", date(2026, 7, 31)),
        ("source_cash_product_id", "other-cash"),
        ("source_cash_product_id", ""),
        ("fee_rate", Decimal("0.02")),
        ("created_by", "api"),
    ],
)
def test_purchase_idempotency_key_rejects_conflicting_request(
    services, field, value
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    seed_cash(repository, "other-cash", "3000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_product(repository, "other-fund", ProductType.PUBLIC_FUND, "009999")
    request = {
        "product_id": "fund",
        "amount": Decimal("1000.00"),
        "trade_date": TRADE_DATE,
        "idempotency_key": "web:purchase-conflict",
        "source_cash_product_id": "cash",
        "fee_rate": Decimal("0.0100"),
        "created_by": "web",
    }
    original = transactions.record_purchase(**request)
    request[field] = value

    with pytest.raises(
        ValueError, match="idempotency key conflicts with existing request"
    ):
        transactions.record_purchase(**request)

    assert repository.get_transaction_by_idempotency(
        "web:purchase-conflict"
    ) == original
    assert projector.calculate("cash").locked_shares == Decimal("1000")


def test_purchase_idempotency_normalizes_equivalent_decimal_request(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "3000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    original = transactions.record_purchase(
        "fund",
        Decimal("1000.00"),
        TRADE_DATE,
        "web:purchase-normalized",
        source_cash_product_id="cash",
        fee_rate=Decimal("0.0100"),
    )

    retry = transactions.record_purchase(
        "fund",
        Decimal("1E+3"),
        TRADE_DATE,
        "web:purchase-normalized",
        source_cash_product_id="cash",
        fee_rate=Decimal("0.01"),
    )

    assert retry == original
    assert projector.calculate("cash").locked_shares == Decimal("1000")


@pytest.mark.parametrize(
    ("record_kind", "first_key", "retry_key"),
    [
        ("purchase", " normalized-record-key ", "normalized-record-key"),
        ("purchase", "normalized-record-key", " normalized-record-key "),
        ("redemption", " normalized-record-key ", "normalized-record-key"),
        ("redemption", "normalized-record-key", " normalized-record-key "),
    ],
)
def test_record_trade_normalizes_idempotency_key_before_lookup_and_insert(
    services,
    record_kind,
    first_key,
    retry_key,
):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "1000")

    if record_kind == "purchase":
        first = transactions.record_purchase(
            "cash",
            Decimal("10"),
            TRADE_DATE,
            first_key,
        )
        retried = transactions.record_purchase(
            "cash",
            Decimal("10"),
            TRADE_DATE,
            retry_key,
        )
    else:
        first = transactions.record_redemption(
            "cash",
            Decimal("10"),
            TRADE_DATE,
            first_key,
        )
        retried = transactions.record_redemption(
            "cash",
            Decimal("10"),
            TRADE_DATE,
            retry_key,
        )

    assert retried == first
    assert first.idempotency_key == "normalized-record-key"
    assert repository.get_transaction_by_idempotency(
        "normalized-record-key"
    ) == first
    assert repository.get_transaction_by_idempotency(
        " normalized-record-key "
    ) is None


@pytest.mark.parametrize("record_kind", ["purchase", "redemption"])
def test_record_trade_rejects_blank_idempotency_key(
    services,
    record_kind,
):
    repository, transactions, _ = services
    seed_cash(repository, "cash", "1000")

    with pytest.raises(
        ValueError,
        match="idempotency_key must not be empty",
    ):
        if record_kind == "purchase":
            transactions.record_purchase(
                "cash",
                Decimal("10"),
                TRADE_DATE,
                "   ",
            )
        else:
            transactions.record_redemption(
                "cash",
                Decimal("10"),
                TRADE_DATE,
                "   ",
            )


@pytest.mark.parametrize("field", ["amount", "shares"])
def test_purchase_retry_rejects_corrupt_pending_cash_out_value(
    services, field
):
    repository, transactions, _projector = services
    seed_cash(repository, "cash", "3000")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    purchase = transactions.record_purchase(
        "fund",
        Decimal("1000"),
        TRADE_DATE,
        "web:corrupt-cash-out",
        source_cash_product_id="cash",
        fee_rate=Decimal("0.01"),
    )
    cash_out = linked_leg(repository, purchase)
    with repository.database.transaction() as conn:
        conn.execute(
            f"UPDATE transactions SET {field} = ? WHERE id = ?",
            ("999", cash_out.id),
        )

    with pytest.raises(
        ValueError, match="idempotency key conflicts with existing request"
    ):
        transactions.record_purchase(
            "fund",
            Decimal("1000"),
            TRADE_DATE,
            "web:corrupt-cash-out",
            source_cash_product_id="cash",
            fee_rate=Decimal("0.01"),
        )


def test_cash_purchase_retry_rejects_corrupt_confirmation_nav(services):
    repository, transactions, _projector = services
    seed_cash(repository, "source", "100")
    seed_cash(repository, "target", "0")
    purchase = transactions.record_purchase(
        "target",
        Decimal("20"),
        TRADE_DATE,
        "web:corrupt-cash-purchase-nav",
        source_cash_product_id="source",
    )
    disable_confirmed_transaction_guard(repository)
    with repository.database.connection() as conn:
        conn.execute(
            "UPDATE transactions SET confirmation_nav = ? WHERE id = ?",
            ("2", purchase.id),
        )

    with pytest.raises(
        ValueError, match="idempotency key conflicts with existing request"
    ):
        transactions.record_purchase(
            "target",
            Decimal("20"),
            TRADE_DATE,
            "web:corrupt-cash-purchase-nav",
            source_cash_product_id="source",
        )


def test_purchase_source_must_be_cash_management(services):
    repository, transactions, _projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_product(repository, "not-cash", ProductType.WEALTH_NAV)

    with pytest.raises(ValueError, match="source must be cash_management"):
        transactions.record_purchase(
            "fund",
            Decimal("100"),
            TRADE_DATE,
            "web:bad-source",
            source_cash_product_id="not-cash",
        )

    assert repository.list_transactions() == []


def test_purchase_rejects_insufficient_available_cash(services):
    repository, transactions, _projector = services
    seed_cash(repository, "cash", "100")
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)

    with pytest.raises(ValueError, match="insufficient available shares"):
        transactions.record_purchase(
            "fund",
            Decimal("101"),
            TRADE_DATE,
            "web:too-much",
            source_cash_product_id="cash",
        )

    assert len(repository.list_transactions()) == 1


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity"])
def test_purchase_rejects_nonpositive_or_nonfinite_amount(services, amount):
    repository, transactions, _projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)

    with pytest.raises(ValueError, match="amount must be positive and finite"):
        transactions.record_purchase(
            "fund", Decimal(amount), TRADE_DATE, f"web:amount:{amount}"
        )


@pytest.mark.parametrize("fee_rate", ["-0.1", "1", "NaN", "Infinity"])
def test_purchase_rejects_invalid_fee_rate(services, fee_rate):
    repository, transactions, _projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)

    with pytest.raises(ValueError, match="fee_rate must be finite and between 0 and 1"):
        transactions.record_purchase(
            "fund",
            Decimal("100"),
            TRADE_DATE,
            f"web:fee:{fee_rate}",
            fee_rate=Decimal(fee_rate),
        )


def test_redemption_rejects_more_than_available_shares(services):
    repository, transactions, _projector = services
    seed_cash(repository, "cash", "100")

    with pytest.raises(ValueError, match="insufficient available shares"):
        transactions.record_redemption(
            "cash", Decimal("101"), TRADE_DATE, "web:redeem-1"
        )


def test_fund_redemption_waits_for_exact_quote_and_locks_shares(services):
    repository, transactions, projector = services
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_opening_position(repository, "fund", "100", "120")
    seed_quote(repository, fund, date(2026, 7, 29), Decimal("1.2"))

    redemption = transactions.record_redemption(
        "fund", Decimal("25"), TRADE_DATE, "web:pending-redemption"
    )

    assert redemption.status is TransactionStatus.PENDING_QUOTE
    assert redemption.amount is None
    assert projector.calculate("fund").locked_shares == Decimal("25")


def test_pending_manual_redemption_settles_when_trade_date_quote_arrives(
    services,
):
    repository, transactions, projector = services
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_opening_position(repository, "fund", "100", "120")
    redemption = transactions.record_redemption(
        "fund", Decimal("25"), TRADE_DATE, "web:pending-redemption"
    )
    quote = seed_quote(repository, fund, TRADE_DATE, Decimal("1.2"))

    settled = transactions.settle_pending(TRADE_DATE)

    assert [row.id for row in settled] == [redemption.id]
    assert settled[0].status is TransactionStatus.CONFIRMED
    assert settled[0].confirmation_nav == quote.unit_nav
    position = projector.calculate("fund")
    assert position.total_shares == Decimal("75")
    assert position.locked_shares == Decimal("0")
    assert position.available_shares == Decimal("75")


def test_timed_redemption_uses_cutoff_and_waits_for_confirmation_date(
    services,
):
    repository, transactions, projector = services
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_opening_position(repository, "fund", "100", "120")

    redemption = transactions.record_redemption(
        "fund",
        Decimal("25"),
        date(2026, 7, 31),
        "web:timed-redemption",
        trade_time="2026-07-31T15:01",
    )
    seed_quote(repository, fund, date(2026, 8, 3), Decimal("1.2"))

    assert redemption.trade_date == date(2026, 8, 3)
    assert redemption.trade_time == "2026-07-31T15:01:00"
    assert transactions.settle_pending(date(2026, 8, 3)) == []
    assert projector.calculate("fund").locked_shares == Decimal("25")

    settled = transactions.settle_pending(date(2026, 8, 4))

    assert len(settled) == 1
    assert settled[0].confirmation_date == date(2026, 8, 4)
    assert projector.calculate("fund").total_shares == Decimal("75")


def test_timed_purchase_with_known_nav_stays_pending_until_confirmation(
    services,
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "500")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_quote(repository, fund, TRADE_DATE, Decimal("1.25"))

    purchase = transactions.record_purchase(
        "fund",
        Decimal("100"),
        TRADE_DATE,
        "web:timed-purchase-known-nav",
        source_cash_product_id="cash",
        trade_time="2026-07-30T14:59",
    )

    assert purchase.status is TransactionStatus.PENDING_CONFIRMATION
    assert purchase.shares == Decimal("80")
    assert projector.calculate("fund").total_shares == Decimal("0")
    assert projector.calculate("cash").locked_shares == Decimal("100")
    assert transactions.settle_pending(TRADE_DATE) == []

    settled = transactions.settle_pending(date(2026, 7, 31))

    assert settled[0].confirmation_date == date(2026, 7, 31)
    assert projector.calculate("fund").total_shares == Decimal("80")
    assert projector.calculate("cash").locked_shares == Decimal("0")


def test_cash_redemption_with_destination_confirms_both_sides(services):
    repository, transactions, projector = services
    seed_cash(repository, "source", "100")
    seed_cash(repository, "destination", "10")

    redemption = transactions.record_redemption(
        "source",
        Decimal("25"),
        TRADE_DATE,
        "web:cash-redemption",
        destination_cash_product_id="destination",
    )

    cash_leg = linked_leg(repository, redemption)
    assert redemption.status is TransactionStatus.CONFIRMED
    assert cash_leg.status is TransactionStatus.CONFIRMED
    assert cash_leg.transaction_type is TransactionType.CASH_TRANSFER_IN
    assert cash_leg.amount == Decimal("25")
    assert cash_leg.shares == Decimal("25")
    assert projector.calculate("source").total_shares == Decimal("75")
    assert projector.calculate("destination").total_shares == Decimal("35")


def test_redemption_retry_is_idempotent_and_does_not_lock_twice(services):
    repository, transactions, projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_opening_position(repository, "fund", "100")
    seed_cash(repository, "cash", "0")

    original = transactions.record_redemption(
        "fund",
        Decimal("25.00"),
        TRADE_DATE,
        "web:redemption-retry",
        destination_cash_product_id="cash",
    )
    retry = transactions.record_redemption(
        "fund",
        Decimal("25"),
        TRADE_DATE,
        "web:redemption-retry",
        destination_cash_product_id="cash",
    )

    assert retry == original
    assert len(repository.list_transactions()) == 3
    assert retry.linked_transaction_id == ""
    assert retry.destination_cash_product_id == "cash"
    assert projector.calculate("fund").locked_shares == Decimal("25")


def test_cash_redemption_with_trade_time_waits_for_t1_confirmation(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "100")

    redemption = transactions.record_redemption(
        "cash",
        Decimal("25"),
        date(2026, 8, 1),
        "web:weekend-cash-redemption",
        trade_time="2026-08-01T10:30",
        settlement_date=date(2026, 8, 5),
    )

    assert redemption.trade_date == date(2026, 8, 3)
    assert redemption.status is TransactionStatus.PENDING_CONFIRMATION
    assert redemption.confirmation_date is None
    assert redemption.settlement_date == date(2026, 8, 3)
    assert projector.calculate("cash").locked_shares == Decimal("25")
    assert projector.calculate("cash").total_shares == Decimal("100")

    assert transactions.settle_pending(date(2026, 8, 3)) == []
    settled = transactions.settle_pending(date(2026, 8, 4))

    assert len(settled) == 1
    assert settled[0].status is TransactionStatus.CONFIRMED
    assert settled[0].confirmation_date == date(2026, 8, 4)
    assert projector.calculate("cash").total_shares == Decimal("75")
    assert projector.calculate("cash").locked_shares == Decimal("0")


def test_cash_purchase_with_trade_time_confirms_next_trading_day(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")

    purchase = transactions.record_purchase(
        "cash",
        Decimal("100"),
        TRADE_DATE,
        "web:timed-cash-purchase",
        trade_time="2026-07-30T10:00",
    )

    assert purchase.trade_date == TRADE_DATE
    assert purchase.status is TransactionStatus.PENDING_CONFIRMATION
    assert purchase.confirmation_date is None
    assert projector.calculate("cash").total_shares == Decimal("1000")

    assert transactions.settle_pending(TRADE_DATE) == []
    settled = transactions.settle_pending(date(2026, 7, 31))

    assert len(settled) == 1
    assert settled[0].confirmation_date == date(2026, 7, 31)
    assert projector.calculate("cash").total_shares == Decimal("1100")


def test_cash_purchase_without_trade_time_still_waits_for_t1_confirmation(
    services,
):
    """钱包Plus 申购即使未传 trade_time，也必须 T+1 确认，不能当日计息。"""
    from src.portfolio_wallet import WALLET_PROVIDER

    repository, transactions, projector = services
    repository.add_product(
        Product(
            id="wallet",
            provider=WALLET_PROVIDER,
            code="WALLETPLUS",
            name="钱包Plus",
            product_type=ProductType.CASH_MANAGEMENT,
        )
    )
    seed_opening_position(repository, "wallet", "1000")

    purchase = transactions.record_purchase(
        "wallet",
        Decimal("100"),
        TRADE_DATE,
        "web:untimed-wallet-purchase",
    )

    assert purchase.status is TransactionStatus.PENDING_CONFIRMATION
    assert purchase.confirmation_date is None
    assert projector.calculate("wallet").total_shares == Decimal("1100")
    assert transactions.settle_pending(TRADE_DATE) == []

    settled = transactions.settle_pending(date(2026, 7, 31))
    assert len(settled) == 1
    assert settled[0].status is TransactionStatus.CONFIRMED
    assert settled[0].confirmation_date == date(2026, 7, 31)
    assert projector.calculate("wallet").total_shares == Decimal("1100")


def test_cash_purchase_does_not_earn_until_day_after_confirmation(services):
    """钱包 T+1 确认后，确认日当天的收益仍按确认前份额；次日收益日起息。"""
    from src.portfolio_income import CashIncomeService
    from src.portfolio_wallet import WALLET_PROVIDER

    repository, transactions, projector = services
    repository.add_product(
        Product(
            id="wallet",
            provider=WALLET_PROVIDER,
            code="WALLETPLUS",
            name="钱包Plus",
            product_type=ProductType.CASH_MANAGEMENT,
        )
    )
    seed_opening_position(repository, "wallet", "10000")
    repository.upsert_quote(
        "wallet",
        MarketQuote(
            product_code="WALLETPLUS",
            quote_date=TRADE_DATE,
            source="fixed",
            raw_hash="h1",
            income_per_10k=Decimal("0.5"),
        ),
        f"{TRADE_DATE.isoformat()}T00:00:00+08:00",
    )
    repository.upsert_quote(
        "wallet",
        MarketQuote(
            product_code="WALLETPLUS",
            quote_date=date(2026, 7, 31),
            source="fixed",
            raw_hash="h2",
            income_per_10k=Decimal("0.5"),
        ),
        "2026-07-31T00:00:00+08:00",
    )
    repository.upsert_quote(
        "wallet",
        MarketQuote(
            product_code="WALLETPLUS",
            quote_date=date(2026, 8, 3),
            source="fixed",
            raw_hash="h3",
            income_per_10k=Decimal("0.5"),
        ),
        "2026-08-03T00:00:00+08:00",
    )

    transactions.record_purchase(
        "wallet",
        Decimal("5000"),
        TRADE_DATE,
        "web:wallet-t1-purchase",
    )
    income = CashIncomeService(repository, projector)

    # 交易日：申购未确认
    trade_day = income.accrue("wallet", TRADE_DATE)
    assert trade_day is not None
    assert trade_day.amount == Decimal("0.5")

    # T+1 确认日：收益仍按确认前一交易日份额
    transactions.settle_pending(date(2026, 7, 31))
    confirm_day = income.accrue("wallet", date(2026, 7, 31))
    assert confirm_day is not None
    assert confirm_day.amount == Decimal("0.500025")

    # 确认日后的下一交易日：新申购进入计息
    next_day = income.accrue("wallet", date(2026, 8, 3))
    assert next_day is not None
    assert next_day.amount == Decimal("0.75005000125")


def test_wallet_plus_redemption_confirms_immediately_on_trade_date(services):
    """钱包Plus 赎回当天到账，不能按申购规则挂到 T+1。"""
    from src.portfolio_wallet import WALLET_PROVIDER

    repository, transactions, projector = services
    repository.add_product(
        Product(
            id="wallet",
            provider=WALLET_PROVIDER,
            code="WALLETPLUS",
            name="钱包Plus",
            product_type=ProductType.CASH_MANAGEMENT,
        )
    )
    seed_opening_position(repository, "wallet", "1000")
    seed_cash(repository, "destination", "10")

    redemption = transactions.record_redemption(
        "wallet",
        Decimal("400"),
        date(2026, 8, 18),
        "web:wallet-plus-instant-redemption",
        destination_cash_product_id="destination",
        trade_time="2026-08-18T10:54:00",
    )

    cash_leg = linked_leg(repository, redemption)
    assert redemption.status is TransactionStatus.CONFIRMED
    assert redemption.confirmation_date == date(2026, 8, 18)
    assert redemption.settlement_date == date(2026, 8, 18)
    assert cash_leg.status is TransactionStatus.CONFIRMED
    assert cash_leg.confirmation_date == date(2026, 8, 18)
    assert projector.calculate("wallet").total_shares == Decimal("600")
    assert projector.calculate("wallet").locked_shares == Decimal("0")
    assert projector.calculate("destination").total_shares == Decimal("410")


def test_pending_wallet_plus_redemption_settles_on_trade_date(services):
    """历史上已挂起的钱包Plus 赎回，交易日当天就应确认到账。"""
    from src.portfolio_wallet import WALLET_PROVIDER

    repository, transactions, projector = services
    repository.add_product(
        Product(
            id="wallet",
            provider=WALLET_PROVIDER,
            code="WALLETPLUS",
            name="钱包Plus",
            product_type=ProductType.CASH_MANAGEMENT,
        )
    )
    seed_opening_position(repository, "wallet", "1000")
    pending = Transaction(
        id="pending-wallet-redemption",
        product_id="wallet",
        transaction_type=TransactionType.MANUAL_REDEMPTION,
        status=TransactionStatus.PENDING_CONFIRMATION,
        trade_date=date(2026, 8, 18),
        idempotency_key="pending-wallet-redemption",
        trade_time="2026-08-18T10:54:00",
        amount=Decimal("400"),
        shares=Decimal("400"),
        confirmation_nav=Decimal("1"),
        settlement_date=date(2026, 8, 18),
    )
    repository.create_transaction(pending)
    projector.rebuild("wallet")

    assert transactions.settle_pending(date(2026, 8, 18))[0].status is (
        TransactionStatus.CONFIRMED
    )
    settled = repository.get_transaction_by_id(pending.id)
    assert settled.confirmation_date == date(2026, 8, 18)
    assert projector.calculate("wallet").total_shares == Decimal("600")


def test_purchase_rejects_same_cash_product_as_source(services):
    repository, transactions, _projector = services
    seed_cash(repository, "cash", "100")

    with pytest.raises(ValueError, match="source must differ from product"):
        transactions.record_purchase(
            "cash",
            Decimal("25"),
            TRADE_DATE,
            "web:self-funded-cash-purchase",
            source_cash_product_id="cash",
        )


def test_redemption_rejects_same_cash_product_as_destination(services):
    repository, transactions, _projector = services
    seed_cash(repository, "cash", "100")

    with pytest.raises(ValueError, match="destination must differ from product"):
        transactions.record_redemption(
            "cash",
            Decimal("25"),
            TRADE_DATE,
            "web:self-destination-cash-redemption",
            destination_cash_product_id="cash",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product_id", "other-fund"),
        ("shares", Decimal("26")),
        ("trade_date", date(2026, 7, 31)),
        ("destination_cash_product_id", "other-cash"),
        ("destination_cash_product_id", ""),
        ("created_by", "api"),
    ],
)
def test_redemption_idempotency_key_rejects_conflicting_request(
    services, field, value
):
    repository, transactions, projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_opening_position(repository, "fund", "100")
    seed_product(repository, "other-fund", ProductType.PUBLIC_FUND, "009999")
    seed_opening_position(repository, "other-fund", "100")
    seed_cash(repository, "cash", "0")
    seed_cash(repository, "other-cash", "0")
    request = {
        "product_id": "fund",
        "shares": Decimal("25.00"),
        "trade_date": TRADE_DATE,
        "idempotency_key": "web:redemption-conflict",
        "destination_cash_product_id": "cash",
        "created_by": "web",
    }
    original = transactions.record_redemption(**request)
    request[field] = value

    with pytest.raises(
        ValueError, match="idempotency key conflicts with existing request"
    ):
        transactions.record_redemption(**request)

    assert repository.get_transaction_by_idempotency(
        "web:redemption-conflict"
    ) == original
    assert projector.calculate("fund").locked_shares == Decimal("25")


def test_redemption_destination_must_be_cash_management(services):
    repository, transactions, _projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_opening_position(repository, "fund", "100")
    seed_product(repository, "not-cash", ProductType.WEALTH_NAV)

    with pytest.raises(ValueError, match="destination must be cash_management"):
        transactions.record_redemption(
            "fund",
            Decimal("10"),
            TRADE_DATE,
            "web:bad-destination",
            destination_cash_product_id="not-cash",
        )

    assert len(repository.list_transactions()) == 1


def test_confirm_pending_cash_purchase_preserves_amount_as_shares(services):
    """现金产品确认时，shares 必须等于 amount（金额=份额），不受 fee/nav 影响。"""
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")
    pending = transactions.record_purchase(
        "cash",
        Decimal("500"),
        TRADE_DATE,
        "web:cash-confirm",
        fee_rate=Decimal("0.01"),
    )
    quote = MarketQuote(
        "cash", TRADE_DATE, "cash_unit_price", "cash_unit_price", unit_nav=Decimal("1")
    )

    confirmed = transactions.confirm_pending(pending.id, quote)

    assert confirmed.status is TransactionStatus.CONFIRMED
    assert confirmed.shares == Decimal("500")
    assert confirmed.amount == Decimal("500")
    assert projector.calculate("cash").total_shares == Decimal("1500")


@pytest.mark.parametrize("shares", ["0", "-1", "NaN", "Infinity"])
def test_redemption_rejects_nonpositive_or_nonfinite_shares(services, shares):
    repository, transactions, _projector = services
    seed_cash(repository, "cash", "100")

    with pytest.raises(ValueError, match="shares must be positive and finite"):
        transactions.record_redemption(
            "cash", Decimal(shares), TRADE_DATE, f"web:shares:{shares}"
        )


def test_confirm_pending_purchase_updates_linked_leg_once(services):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    pending = transactions.record_purchase(
        "fund",
        Decimal("600"),
        TRADE_DATE,
        "web:confirm-purchase",
        source_cash_product_id="cash",
        fee_rate=Decimal("0.01"),
    )
    quote = MarketQuote(
        fund.code, TRADE_DATE, "test", "confirm-purchase", unit_nav=Decimal("2")
    )

    confirmed = transactions.confirm_pending(pending.id, quote)
    retry = transactions.confirm_pending(pending.id, quote)

    assert retry == confirmed
    assert confirmed.status is TransactionStatus.CONFIRMED
    assert confirmed.fee_amount == Decimal("6")
    assert confirmed.shares == Decimal("297")
    assert confirmed.confirmation_nav == Decimal("2")
    assert confirmed.confirmation_date == TRADE_DATE
    assert linked_leg(repository, confirmed).status is TransactionStatus.CONFIRMED
    assert projector.calculate("cash").total_shares == Decimal("400")
    assert projector.calculate("cash").locked_shares == Decimal("0")
    assert projector.calculate("fund").total_shares == Decimal("297")


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("trade_date", "2026-07-29"),
        ("created_by", "api"),
        ("confirmation_nav", "1"),
        ("confirmation_date", "2026-07-30"),
        ("status", TransactionStatus.PENDING_CONFIRMATION.value),
        ("product_id", "fund"),
    ],
)
def test_confirm_pending_rejects_corrupt_cash_out_metadata(
    services, column, value
):
    repository, transactions, _projector = services
    seed_cash(repository, "cash", "1000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    pending = transactions.record_purchase(
        "fund",
        Decimal("600"),
        TRADE_DATE,
        "web:corrupt-cash-out-metadata",
        source_cash_product_id="cash",
    )
    cash_out = linked_leg(repository, pending)
    with repository.database.transaction() as conn:
        conn.execute(
            f"UPDATE transactions SET {column} = ? WHERE id = ?",
            (value, cash_out.id),
        )
    quote = MarketQuote(
        fund.code, TRADE_DATE, "test", "confirm", unit_nav=Decimal("2")
    )

    with pytest.raises(ValueError, match="inconsistent linked transaction"):
        transactions.confirm_pending(pending.id, quote)


def test_confirm_pending_rejects_orphaned_reverse_cash_link(services):
    repository, transactions, _projector = services
    seed_cash(repository, "cash", "1000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    pending = transactions.record_purchase(
        "fund",
        Decimal("600"),
        TRADE_DATE,
        "web:orphaned-reverse-link",
        source_cash_product_id="cash",
    )
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE transactions SET linked_transaction_id = NULL WHERE id = ?",
            (pending.id,),
        )
    quote = MarketQuote(
        fund.code, TRADE_DATE, "test", "confirm", unit_nav=Decimal("2")
    )

    with pytest.raises(ValueError, match="inconsistent linked transaction"):
        transactions.confirm_pending(pending.id, quote)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("fee_amount", "99"),
        ("shares", "1"),
    ],
)
def test_confirmed_purchase_rejects_corrupt_fee_or_effective_shares(
    services, column, value
):
    repository, transactions, _projector = services
    seed_cash(repository, "cash", "1000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    quote = seed_quote(repository, fund, TRADE_DATE, Decimal("2"))
    purchase = transactions.record_purchase(
        "fund",
        Decimal("600"),
        TRADE_DATE,
        "web:corrupt-purchase-financials",
        source_cash_product_id="cash",
        fee_rate=Decimal("0.01"),
    )
    disable_confirmed_transaction_guard(repository)
    with repository.database.connection() as conn:
        conn.execute(
            f"UPDATE transactions SET {column} = ? WHERE id = ?",
            (value, purchase.id),
        )

    with pytest.raises(ValueError, match="inconsistent linked transaction"):
        transactions.confirm_pending(purchase.id, quote)


@pytest.mark.parametrize(
    ("product_code", "quote_date", "unit_nav"),
    [
        ("WRONG", TRADE_DATE, Decimal("2")),
        ("003103", date(2026, 7, 29), Decimal("2")),
        ("003103", TRADE_DATE, Decimal("2.01")),
        ("003103", TRADE_DATE, None),
        ("003103", TRADE_DATE, Decimal("NaN")),
    ],
)
def test_confirmed_transaction_rejects_conflicting_confirmation_quote(
    services, product_code, quote_date, unit_nav
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    pending = transactions.record_purchase(
        "fund",
        Decimal("600"),
        TRADE_DATE,
        "web:confirmed-quote-conflict",
        source_cash_product_id="cash",
        fee_rate=Decimal("0.01"),
    )
    confirmed = transactions.confirm_pending(
        pending.id,
        MarketQuote(
            fund.code,
            TRADE_DATE,
            "test",
            "confirmed",
            unit_nav=Decimal("2"),
        ),
    )
    conflicting_quote = MarketQuote(
        product_code,
        quote_date,
        "test",
        "conflicting",
        unit_nav=unit_nav,
    )

    with pytest.raises(ValueError, match="confirmation quote conflicts"):
        transactions.confirm_pending(confirmed.id, conflicting_quote)

    assert repository.get_transaction_by_id(confirmed.id) == confirmed
    assert projector.calculate("cash").total_shares == Decimal("400")
    assert projector.calculate("fund").total_shares == Decimal("297")


def test_confirm_pending_redemption_does_not_credit_destination_before_settlement(
    services,
):
    repository, transactions, projector = services
    fund = seed_product(repository, "fund", ProductType.WEALTH_NAV, "W001")
    seed_opening_position(repository, "fund", "100", "80")
    seed_cash(repository, "cash", "10")
    pending = transactions.record_redemption(
        "fund",
        Decimal("25"),
        TRADE_DATE,
        "web:confirm-redemption",
        destination_cash_product_id="cash",
    )
    quote = MarketQuote(
        fund.code, TRADE_DATE, "test", "confirm-redemption", unit_nav=Decimal("1.2")
    )

    confirmed = transactions.confirm_pending(pending.id, quote)

    assert confirmed.amount == Decimal("30")
    assert confirmed.shares == Decimal("25")
    assert confirmed.linked_transaction_id == ""
    assert confirmed.destination_cash_product_id == "cash"
    assert confirmed.settlement_status is None
    assert projector.calculate("fund").total_shares == Decimal("75")
    assert projector.calculate("cash").total_shares == Decimal("10")


def test_confirmed_redemption_retry_rejects_different_direct_destination(
    services,
):
    repository, transactions, _projector = services
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_opening_position(repository, "fund", "100")
    seed_cash(repository, "cash", "0")
    seed_cash(repository, "other-cash", "0")
    seed_quote(repository, fund, TRADE_DATE, Decimal("1.2"))
    transactions.record_redemption(
        "fund",
        Decimal("25"),
        TRADE_DATE,
        "web:direct-redemption-destination",
        destination_cash_product_id="cash",
    )

    with pytest.raises(
        ValueError, match="idempotency key conflicts with existing request"
    ):
        transactions.record_redemption(
            "fund",
            Decimal("25"),
            TRADE_DATE,
            "web:direct-redemption-destination",
            destination_cash_product_id="other-cash",
        )


@pytest.mark.parametrize(
    ("product_code", "quote_date", "unit_nav", "message"),
    [
        ("WRONG", TRADE_DATE, Decimal("1"), "quote product does not match"),
        ("003103", date(2026, 7, 29), Decimal("1"), "quote date does not match"),
        ("003103", TRADE_DATE, None, "quote must contain a finite positive unit NAV"),
        ("003103", TRADE_DATE, Decimal("0"), "quote must contain a finite positive unit NAV"),
        ("003103", TRADE_DATE, Decimal("NaN"), "quote must contain a finite positive unit NAV"),
        ("003103", TRADE_DATE, Decimal("Infinity"), "quote must contain a finite positive unit NAV"),
    ],
)
def test_confirm_pending_rejects_mismatched_or_invalid_quote(
    services, product_code, quote_date, unit_nav, message
):
    repository, transactions, projector = services
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_opening_position(repository, "fund", "100")
    pending = transactions.record_redemption(
        "fund", Decimal("10"), TRADE_DATE, "web:quote-validation"
    )
    quote = MarketQuote(
        product_code, quote_date, "test", "bad-quote", unit_nav=unit_nav
    )

    with pytest.raises(ValueError, match=message):
        transactions.confirm_pending(pending.id, quote)

    unchanged = repository.get_transaction_by_id(pending.id)
    assert unchanged.status is TransactionStatus.PENDING_QUOTE
    assert projector.calculate("fund").locked_shares == Decimal("10")


def test_confirm_pending_rejects_unsupported_transaction_type(services):
    repository, transactions, _projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    unsupported = Transaction(
        id="sip-pending",
        product_id="fund",
        transaction_type=TransactionType.SIP_PURCHASE,
        status=TransactionStatus.PENDING_QUOTE,
        trade_date=TRADE_DATE,
        idempotency_key="sip:pending",
        amount=Decimal("100"),
    )
    repository.create_transaction(unsupported)
    quote = MarketQuote(
        "003103", TRADE_DATE, "test", "unsupported", unit_nav=Decimal("1")
    )

    with pytest.raises(ValueError, match="unsupported pending transaction type"):
        transactions.confirm_pending(unsupported.id, quote)


def test_purchase_rolls_back_ledger_and_positions_when_rebuild_fails(
    services, monkeypatch
):
    repository, transactions, _projector = services
    seed_cash(repository, "cash", "1000")
    seed_cash(repository, "target", "0")
    before_cash = repository.get_position("cash")
    before_target = repository.get_position("target")
    original_replace = repository.replace_position

    def fail_target(position, conn=None):
        if position.product_id == "target":
            raise RuntimeError("position write failed")
        return original_replace(position, conn)

    monkeypatch.setattr(repository, "replace_position", fail_target)

    with pytest.raises(RuntimeError, match="position write failed"):
        transactions.record_purchase(
            "target",
            Decimal("100"),
            TRADE_DATE,
            "web:rollback",
            source_cash_product_id="cash",
        )

    assert len(repository.list_transactions()) == 2
    assert repository.get_position("cash") == before_cash
    assert repository.get_position("target") == before_target


def test_concurrent_purchases_cannot_double_spend_one_cash_balance(services):
    repository, _transactions, projector = services
    seed_cash(repository, "source", "100")
    seed_cash(repository, "target-a", "0")
    seed_cash(repository, "target-b", "0")
    database_path = repository.database.path
    first_has_write_lock = threading.Event()
    release_first = threading.Event()
    second_attempting_transaction = threading.Event()
    second_has_write_lock = threading.Event()
    results = {}
    errors = {}

    class HoldingDatabase(PortfolioDatabase):
        @contextmanager
        def transaction(self):
            with super().transaction() as conn:
                first_has_write_lock.set()
                if not release_first.wait(5):
                    raise RuntimeError("timed out holding first write lock")
                yield conn

    class ObservedDatabase(PortfolioDatabase):
        @contextmanager
        def transaction(self):
            second_attempting_transaction.set()
            with super().transaction() as conn:
                second_has_write_lock.set()
                yield conn

    def service_for(database):
        independent_repository = PortfolioRepository(database)
        return PortfolioTransactionService(
            independent_repository,
            PositionProjector(independent_repository),
        )

    first_service = service_for(HoldingDatabase(database_path))
    second_service = service_for(ObservedDatabase(database_path))

    def purchase(label, service, target_id, idempotency_key):
        try:
            results[label] = service.record_purchase(
                target_id,
                Decimal("80"),
                TRADE_DATE,
                idempotency_key,
                source_cash_product_id="source",
            )
        except BaseException as exc:
            errors[label] = exc

    first_thread = threading.Thread(
        target=purchase,
        args=(
            "first",
            first_service,
            "target-a",
            "web:concurrent-a",
        ),
    )
    second_thread = threading.Thread(
        target=purchase,
        args=(
            "second",
            second_service,
            "target-b",
            "web:concurrent-b",
        ),
    )
    first_thread.start()
    try:
        assert first_has_write_lock.wait(5)
        second_thread.start()
        assert second_attempting_transaction.wait(5)
        assert not second_has_write_lock.wait(0.5)
    finally:
        release_first.set()

    assert second_has_write_lock.wait(5)
    for thread in (first_thread, second_thread):
        thread.join(10)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert set(results) == {"first"}
    assert set(errors) == {"second"}
    assert isinstance(errors["second"], ValueError)
    assert str(errors["second"]) == "insufficient available shares"
    assert projector.calculate("source").total_shares == Decimal("20")
    assert projector.calculate("target-a").total_shares == Decimal("80")
    assert projector.calculate("target-b").total_shares == Decimal("0")


def test_linked_row_insert_failure_rolls_back_primary_and_positions(
    services, monkeypatch
):
    repository, transactions, _projector = services
    seed_cash(repository, "source", "100")
    seed_cash(repository, "target", "0")
    before_source = repository.get_position("source")
    before_target = repository.get_position("target")
    original_create = repository.create_transaction

    def fail_linked_insert(transaction, conn=None):
        if transaction.transaction_type is TransactionType.CASH_TRANSFER_OUT:
            raise RuntimeError("linked insert failed")
        return original_create(transaction, conn)

    monkeypatch.setattr(repository, "create_transaction", fail_linked_insert)

    with pytest.raises(RuntimeError, match="linked insert failed"):
        transactions.record_purchase(
            "target",
            Decimal("20"),
            TRADE_DATE,
            "web:linked-insert-failure",
            source_cash_product_id="source",
        )

    assert len(repository.list_transactions()) == 2
    assert repository.get_position("source") == before_source
    assert repository.get_position("target") == before_target


def test_reciprocal_link_update_failure_rolls_back_both_rows(
    services, monkeypatch
):
    repository, transactions, _projector = services
    seed_cash(repository, "source", "100")
    seed_cash(repository, "target", "0")
    before_source = repository.get_position("source")
    before_target = repository.get_position("target")

    def fail_primary_link_update(transaction, conn=None):
        raise RuntimeError("primary link update failed")

    monkeypatch.setattr(
        repository, "update_pending_transaction", fail_primary_link_update
    )

    with pytest.raises(RuntimeError, match="primary link update failed"):
        transactions.record_purchase(
            "target",
            Decimal("20"),
            TRADE_DATE,
            "web:link-update-failure",
            source_cash_product_id="source",
        )

    assert len(repository.list_transactions()) == 2
    assert repository.get_position("source") == before_source
    assert repository.get_position("target") == before_target


def test_confirm_linked_update_failure_rolls_back_confirmed_primary(
    services, monkeypatch
):
    repository, transactions, projector = services
    seed_cash(repository, "cash", "1000")
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    pending = transactions.record_purchase(
        "fund",
        Decimal("600"),
        TRADE_DATE,
        "web:confirm-linked-failure",
        source_cash_product_id="cash",
    )
    pending_leg = linked_leg(repository, pending)
    original_update = repository.update_pending_transaction

    def fail_linked_update(transaction, conn=None):
        if transaction.id == pending_leg.id:
            raise RuntimeError("linked confirmation failed")
        return original_update(transaction, conn)

    monkeypatch.setattr(
        repository, "update_pending_transaction", fail_linked_update
    )
    quote = MarketQuote(
        fund.code, TRADE_DATE, "test", "confirm-failure", unit_nav=Decimal("2")
    )

    with pytest.raises(RuntimeError, match="linked confirmation failed"):
        transactions.confirm_pending(pending.id, quote)

    assert repository.get_transaction_by_id(pending.id) == pending
    assert repository.get_transaction_by_id(pending_leg.id) == pending_leg
    assert projector.calculate("cash").locked_shares == Decimal("600")
    assert projector.calculate("cash").total_shares == Decimal("1000")
    assert projector.calculate("fund").total_shares == Decimal("0")


def test_redemption_source_projector_failure_rolls_back_ledger_and_positions(
    services, monkeypatch
):
    repository, transactions, projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_opening_position(repository, "fund", "100")
    seed_cash(repository, "cash", "0")
    before_fund = repository.get_position("fund")
    before_cash = repository.get_position("cash")
    original_calculate = projector._calculate

    def fail_source_calculation(product_id, conn=None):
        if product_id == "fund":
            raise RuntimeError("source projection failed")
        return original_calculate(product_id, conn)

    monkeypatch.setattr(projector, "_calculate", fail_source_calculation)

    with pytest.raises(RuntimeError, match="source projection failed"):
        transactions.record_redemption(
            "fund",
            Decimal("20"),
            TRADE_DATE,
            "web:redemption-projection-failure",
            destination_cash_product_id="cash",
        )

    assert len(repository.list_transactions()) == 2
    assert repository.get_position("fund") == before_fund
    assert repository.get_position("cash") == before_cash


def test_repository_get_quote_is_exact_date_and_connection_aware(services):
    repository, _transactions, _projector = services
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    old = seed_quote(repository, fund, date(2026, 7, 29), Decimal("1.1"))

    assert repository.get_quote("fund", TRADE_DATE) is None
    with repository.database.transaction() as conn:
        assert repository.get_quote("fund", date(2026, 7, 29), conn=conn) == old


def test_cash_dividend_without_destination_is_external_and_share_neutral(
    services,
):
    repository, transactions, projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_opening_position(repository, "fund", "100", amount="125")
    before = projector.calculate("fund")

    dividend = transactions.record_cash_dividend(
        "fund",
        Decimal("25"),
        TRADE_DATE,
        "web:dividend-external",
        actor="operator",
    )

    assert dividend.transaction_type is TransactionType.CASH_DIVIDEND
    assert dividend.status is TransactionStatus.CONFIRMED
    assert dividend.amount == Decimal("25")
    assert dividend.shares == Decimal("0")
    assert dividend.linked_transaction_id == ""
    assert dividend.created_by == "operator"
    assert projector.calculate("fund") == before


def test_cash_dividend_destination_creates_equal_linked_cash_transfer(
    services,
):
    repository, transactions, projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_cash(repository, "cash", "1000")

    dividend = transactions.record_cash_dividend(
        "fund",
        Decimal("25"),
        TRADE_DATE,
        "web:dividend-linked",
        destination_cash_product_id="cash",
    )

    cash_leg = linked_leg(repository, dividend)
    assert cash_leg.transaction_type is TransactionType.CASH_TRANSFER_IN
    assert cash_leg.status is TransactionStatus.CONFIRMED
    assert cash_leg.product_id == "cash"
    assert cash_leg.amount == Decimal("25")
    assert cash_leg.shares == Decimal("25")
    assert cash_leg.confirmation_nav == Decimal("1")
    assert cash_leg.confirmation_date == TRADE_DATE
    assert cash_leg.linked_transaction_id == dividend.id
    assert projector.calculate("cash").total_shares == Decimal("1025")
    assert projector.calculate("fund").total_shares == Decimal("0")


@pytest.mark.parametrize("reverse_from", ["dividend", "cash_leg"])
def test_linked_cash_dividend_reversal_restores_destination_holding(
    services,
    reverse_from,
):
    repository, transactions, projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_cash(repository, "cash", "1000")
    dividend = transactions.record_cash_dividend(
        "fund",
        Decimal("25"),
        TRADE_DATE,
        f"web:dividend-to-reverse:{reverse_from}",
        destination_cash_product_id="cash",
    )
    cash_leg = linked_leg(repository, dividend)

    reversal = transactions.reverse_confirmed(
        dividend.id if reverse_from == "dividend" else cash_leg.id,
        "分红录入错误",
        f"web:reverse-dividend:{reverse_from}",
    )

    assert reversal.transaction_type is TransactionType.REVERSAL
    assert projector.calculate("fund").total_shares == Decimal("0")
    assert projector.calculate("cash").total_shares == Decimal("1000")
    assert (
        repository.get_transaction_by_id(dividend.id).status
        is TransactionStatus.REVERSED
    )
    assert (
        repository.get_transaction_by_id(cash_leg.id).status
        is TransactionStatus.REVERSED
    )


def test_external_cash_dividend_can_be_reversed_without_changing_shares(
    services,
):
    repository, transactions, projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_opening_position(repository, "fund", "100", amount="125")
    before = projector.calculate("fund")
    dividend = transactions.record_cash_dividend(
        "fund",
        Decimal("25"),
        TRADE_DATE,
        "web:external-dividend-to-reverse",
    )

    reversal = transactions.reverse_confirmed(
        dividend.id,
        "分红录入错误",
        "web:reverse-external-dividend",
    )

    assert reversal.amount == Decimal("-25")
    assert reversal.shares == Decimal("0")
    assert projector.calculate("fund") == before


def test_cash_dividend_is_idempotent_and_audited_once(services):
    repository, transactions, _projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_cash(repository, "cash", "1000")

    first = transactions.record_cash_dividend(
        "fund",
        Decimal("25"),
        TRADE_DATE,
        "web:dividend-idempotent",
        destination_cash_product_id="cash",
        actor="operator",
    )
    retried = transactions.record_cash_dividend(
        "fund",
        Decimal("25.0"),
        TRADE_DATE,
        "web:dividend-idempotent",
        destination_cash_product_id="cash",
        actor="operator",
    )

    assert retried == first
    with repository.database.connection() as conn:
        audits = conn.execute(
            """SELECT action, object_type, object_id, source,
                      before_json, after_json
               FROM audit_logs"""
        ).fetchall()
    assert len(audits) == 1
    assert tuple(audits[0][:4]) == (
        "record_cash_dividend",
        "product",
        "fund",
        "operator",
    )
    assert json.loads(audits[0]["before_json"]) == {}
    after = json.loads(audits[0]["after_json"])
    assert after["amount"] == "25"
    assert after["destination_cash_product_id"] == "cash"
    assert after["dividend_id"] == first.id
    assert after["linked_transaction_id"] == first.linked_transaction_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", Decimal("26")),
        ("dividend_date", date(2026, 7, 29)),
        ("destination_cash_product_id", ""),
        ("actor", "other"),
    ],
)
def test_cash_dividend_retry_rejects_conflicting_request(
    services,
    field,
    value,
):
    repository, transactions, _projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_cash(repository, "cash", "1000")
    request = {
        "product_id": "fund",
        "amount": Decimal("25"),
        "dividend_date": TRADE_DATE,
        "idempotency_key": "web:dividend-conflict",
        "destination_cash_product_id": "cash",
        "actor": "operator",
    }
    transactions.record_cash_dividend(**request)
    request[field] = value

    with pytest.raises(
        ValueError,
        match="idempotency key conflicts with existing request",
    ):
        transactions.record_cash_dividend(**request)


def test_cash_dividend_requires_cash_management_destination(services):
    repository, transactions, _projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_product(repository, "other-fund", ProductType.PUBLIC_FUND)

    with pytest.raises(ValueError, match="destination must be cash_management"):
        transactions.record_cash_dividend(
            "fund",
            Decimal("25"),
            TRADE_DATE,
            "web:dividend-invalid-destination",
            destination_cash_product_id="other-fund",
        )


def test_cash_dividend_rolls_back_linked_pair_audit_and_projection_together(
    services,
    monkeypatch,
):
    repository, transactions, projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND)
    seed_cash(repository, "cash", "1000")
    before = repository.get_position("cash")

    def fail_destination_calculation(product_id, conn=None):
        assert product_id == "cash"
        assert conn is not None
        raise RuntimeError("destination projection failed")

    monkeypatch.setattr(projector, "_calculate", fail_destination_calculation)

    with pytest.raises(RuntimeError, match="destination projection failed"):
        transactions.record_cash_dividend(
            "fund",
            Decimal("25"),
            TRADE_DATE,
            "web:dividend-rollback",
            destination_cash_product_id="cash",
        )

    assert repository.get_transaction_by_idempotency(
        "web:dividend-rollback"
    ) is None
    assert repository.get_position("cash") == before
    with repository.database.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == 0
