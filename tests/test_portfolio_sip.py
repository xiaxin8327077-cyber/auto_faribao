from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    MarketQuote,
    Product,
    ProductStatus,
    ProductType,
    SipPlanStatus,
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
def sip_services(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    projector = PositionProjector(repository)
    transactions = PortfolioTransactionService(repository, projector)
    return repository, SipService(repository, projector, transactions), projector


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


def seed_cash(repository, product_id, shares):
    seed_product(repository, product_id, ProductType.CASH_MANAGEMENT)
    repository.create_transaction(
        Transaction(
            id=f"opening:{product_id}",
            product_id=product_id,
            transaction_type=TransactionType.OPENING_POSITION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 1),
            idempotency_key=f"opening:{product_id}",
            amount=Decimal(shares),
            shares=Decimal(shares),
            confirmation_nav=Decimal("1"),
            confirmation_date=date(2026, 7, 1),
        )
    )


def seed_fund(repository, product_id, code):
    return seed_product(repository, product_id, ProductType.PUBLIC_FUND, code)


def seed_quote(repository, product_id, quote_date, unit_nav):
    product = repository.require_product(product_id)
    quote = MarketQuote(
        product_code=product.code,
        quote_date=quote_date,
        source="official",
        raw_hash=f"{product_id}:{quote_date.isoformat()}",
        unit_nav=Decimal(unit_nav),
    )
    repository.upsert_quote(
        product_id,
        quote,
        f"{quote_date.isoformat()}T18:00:00+08:00",
    )
    return quote


def active_plan(
    sip,
    product_id,
    source_cash_product_id,
    amount,
    fee_rate,
    start_date=date(2026, 7, 1),
):
    plan = sip.save_plan(
        product_id=product_id,
        daily_amount=Decimal(amount),
        purchase_fee_rate=Decimal(fee_rate),
        source_cash_product_id=source_cash_product_id,
        start_date=start_date,
    )
    return sip.activate(plan.id)


def plan_transactions(repository, plan_id):
    return [
        transaction
        for transaction in repository.list_transactions()
        if transaction.plan_id == plan_id
    ]


def test_015736_exact_quote_confirms_fee_adjusted_shares(sip_services):
    repo, sip, projector = sip_services
    seed_cash(repo, "cash", "3000")
    seed_fund(repo, "fund", "015736")
    plan = active_plan(sip, "fund", "cash", "2000", "0.00006")

    execution = sip.ensure_intent(plan.id, TRADE_DATE)

    assert execution.status == "pending_quote"
    assert projector.calculate("cash").locked_shares == Decimal("2000")
    seed_quote(repo, "fund", TRADE_DATE, "1.0331")
    assert sip.settle_pending(TRADE_DATE) == []
    settled = sip.settle_pending(date(2026, 7, 31))

    expected = (
        Decimal("2000")
        * (Decimal("1") - Decimal("0.00006"))
        / Decimal("1.0331")
    )
    assert settled[0].status == "confirmed"
    assert projector.calculate("fund").total_shares == expected
    assert projector.calculate("cash").total_shares == Decimal("1000")
    assert execution.id == sip.ensure_intent(plan.id, TRADE_DATE).id


def test_003103_zero_fee_uses_the_same_general_sip_mechanism(sip_services):
    repo, sip, projector = sip_services
    seed_cash(repo, "cash", "500")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0")

    pending = sip.ensure_intent(plan.id, TRADE_DATE)
    seed_quote(repo, "fund", TRADE_DATE, "1.25")
    assert sip.settle_pending(TRADE_DATE) == []
    settled = sip.settle_pending(date(2026, 7, 31))

    assert settled == [
        type(pending)(
            pending.id,
            plan.id,
            TRADE_DATE,
            "confirmed",
            transaction_id=pending.transaction_id,
        )
    ]
    assert projector.calculate("fund").total_shares == Decimal("80")


def test_plan_guards_allow_source_free_draft_but_block_activation(sip_services):
    repo, sip, _projector = sip_services
    seed_fund(repo, "fund", "003103")
    draft = sip.save_plan(
        product_id="fund",
        daily_amount=Decimal("100"),
        purchase_fee_rate=Decimal("0"),
        start_date=TRADE_DATE,
    )

    assert draft.status is SipPlanStatus.DRAFT
    assert draft.source_cash_product_id == ""
    assert repo.get_plan(draft.id) == draft
    with pytest.raises(ValueError, match="^cash source is required$"):
        sip.activate(draft.id)


@pytest.mark.parametrize(
    ("target_type", "source_type", "amount", "fee", "message"),
    [
        (
            ProductType.CASH_MANAGEMENT,
            ProductType.CASH_MANAGEMENT,
            "100",
            "0",
            "target must be public_fund",
        ),
        (
            ProductType.PUBLIC_FUND,
            ProductType.PUBLIC_FUND,
            "100",
            "0",
            "source must be cash_management",
        ),
        (
            ProductType.PUBLIC_FUND,
            ProductType.CASH_MANAGEMENT,
            "0",
            "0",
            "daily_amount must be positive",
        ),
        (
            ProductType.PUBLIC_FUND,
            ProductType.CASH_MANAGEMENT,
            "100",
            "1",
            "purchase_fee_rate must be between 0 and 1",
        ),
    ],
)
def test_plan_guards_are_product_and_value_based(
    sip_services, target_type, source_type, amount, fee, message
):
    repo, sip, _projector = sip_services
    seed_product(repo, "target", target_type)
    seed_product(repo, "source", source_type)

    with pytest.raises(ValueError, match=f"^{message}$"):
        sip.save_plan(
            product_id="target",
            daily_amount=Decimal(amount),
            purchase_fee_rate=Decimal(fee),
            source_cash_product_id="source",
            start_date=TRADE_DATE,
        )


def test_insufficient_balance_skips_without_debt_or_catch_up(sip_services):
    repo, sip, projector = sip_services
    seed_cash(repo, "cash", "1999")
    seed_fund(repo, "fund", "015736")
    plan = active_plan(sip, "fund", "cash", "2000", "0.00006")

    execution = sip.ensure_intent(plan.id, TRADE_DATE)

    assert execution.status == "skipped"
    assert execution.reason == "insufficient_balance"
    assert projector.calculate("cash").total_shares == Decimal("1999")
    assert plan_transactions(repo, plan.id) == []
    assert sip.ensure_intent(plan.id, TRADE_DATE) == execution


def test_insufficient_balance_notifies_once(sip_services):
    repo, _sip, projector = sip_services
    notifications = []
    sip = SipService(
        repo,
        projector,
        PortfolioTransactionService(repo, projector),
        notifier=notifications.append,
    )
    seed_cash(repo, "cash", "99")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0")

    skipped = sip.ensure_intent(plan.id, TRADE_DATE)
    sip.ensure_intent(plan.id, TRADE_DATE)

    assert notifications == [skipped]


def test_weekend_is_skipped_without_transactions(sip_services):
    repo, sip, _projector = sip_services
    seed_cash(repo, "cash", "1000")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0")

    execution = sip.ensure_intent(plan.id, date(2026, 8, 1))

    assert execution.status == "skipped"
    assert execution.reason == "non_trading_day"
    assert plan_transactions(repo, plan.id) == []


def test_exchange_holiday_is_skipped_without_deducting_cash(sip_services):
    repo, sip, projector = sip_services
    seed_cash(repo, "cash", "1000")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0")

    execution = sip.ensure_intent(plan.id, date(2026, 10, 1))

    assert execution.status == "skipped"
    assert execution.reason == "non_trading_day"
    assert plan_transactions(repo, plan.id) == []
    assert projector.calculate("cash").available_shares == Decimal("1000")


def test_missing_calendar_year_never_deducts_sip_cash(sip_services):
    repo, sip, projector = sip_services
    seed_cash(repo, "cash", "1000")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(
        sip,
        "fund",
        "cash",
        "100",
        "0",
        start_date=date(2026, 7, 1),
    )

    execution = sip.ensure_intent(plan.id, date(2027, 1, 4))

    assert execution.status == "skipped"
    assert execution.reason == "calendar_unavailable"
    assert plan_transactions(repo, plan.id) == []
    assert projector.calculate("cash").available_shares == Decimal("1000")


def test_pause_does_not_cancel_pending_and_resume_does_not_backfill(sip_services):
    repo, sip, _projector = sip_services
    seed_cash(repo, "cash", "1000")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0")
    pending = sip.ensure_intent(plan.id, TRADE_DATE)

    sip.pause(plan.id)
    paused = sip.ensure_intent(plan.id, date(2026, 7, 31))
    assert paused.reason == "plan_paused"
    assert sip.get_execution(pending.id).status == "pending_quote"

    sip.resume(plan.id)
    assert sip.list_executions(plan.id, date(2026, 7, 31))[-1] == paused
    assert sip.ensure_intent(plan.id, date(2026, 7, 31)) == paused


def test_pending_stays_pending_until_exact_or_later_quote(sip_services):
    repo, sip, projector = sip_services
    seed_cash(repo, "cash", "1000")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0")
    pending = sip.ensure_intent(plan.id, TRADE_DATE)

    seed_quote(repo, "fund", date(2026, 7, 29), "1.01")

    assert sip.settle_pending(TRADE_DATE) == []
    assert sip.get_execution(pending.id).status == "pending_quote"
    assert projector.calculate("cash").locked_shares == Decimal("100")


def test_later_official_quote_marks_non_trading_day_and_releases_cash(
    sip_services,
):
    repo, sip, projector = sip_services
    seed_cash(repo, "cash", "1000")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0")
    pending = sip.ensure_intent(plan.id, TRADE_DATE)
    seed_quote(repo, "fund", date(2026, 7, 31), "1.01")

    settled = sip.settle_pending(date(2026, 7, 31))

    assert settled[0].status == "skipped"
    assert settled[0].reason == "non_trading_day"
    assert projector.calculate("cash").locked_shares == Decimal("0")
    assert {
        transaction.status for transaction in plan_transactions(repo, plan.id)
    } == {TransactionStatus.CANCELLED}
    assert sip.get_execution(pending.id) == settled[0]


def test_intent_creation_is_atomic_and_unique_under_concurrency(sip_services):
    repo, sip, projector = sip_services
    seed_cash(repo, "cash", "100")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0")

    with ThreadPoolExecutor(max_workers=2) as pool:
        executions = list(
            pool.map(
                lambda _index: sip.ensure_intent(plan.id, TRADE_DATE),
                range(2),
            )
        )

    assert executions[0] == executions[1]
    assert len(sip.list_executions(plan.id)) == 1
    assert len(plan_transactions(repo, plan.id)) == 2
    assert projector.calculate("cash").locked_shares == Decimal("100")


def test_settlement_uses_persisted_transaction_snapshot_after_plan_edit(
    sip_services,
):
    repo, sip, projector = sip_services
    seed_cash(repo, "original-cash", "1000")
    seed_cash(repo, "new-cash", "1000")
    seed_fund(repo, "original-fund", "003103")
    seed_fund(repo, "new-fund", "015736")
    plan = active_plan(
        sip,
        "original-fund",
        "original-cash",
        "100",
        "0.01",
    )
    pending = sip.ensure_intent(plan.id, TRADE_DATE)
    repo.save_plan(
        replace(
            plan,
            product_id="new-fund",
            source_cash_product_id="new-cash",
            daily_amount=Decimal("900"),
            purchase_fee_rate=Decimal("0.5"),
        )
    )
    seed_quote(repo, "original-fund", TRADE_DATE, "1.1")

    settled = sip.settle_pending(date(2026, 7, 31))

    assert settled[0].id == pending.id
    assert settled[0].status == "confirmed"
    assert projector.calculate("original-fund").total_shares == (
        Decimal("100") * Decimal("0.99") / Decimal("1.1")
    )
    assert projector.calculate("new-fund").total_shares == Decimal("0")
    assert projector.calculate("original-cash").total_shares == Decimal("900")
    assert projector.calculate("new-cash").total_shares == Decimal("1000")


@pytest.mark.parametrize(
    ("inactive_product_id", "message"),
    [
        ("fund", "target product is inactive"),
        ("cash", "cash source is inactive"),
    ],
)
def test_activation_rejects_inactive_products_in_its_write_transaction(
    sip_services, inactive_product_id, message
):
    repo, sip, _projector = sip_services
    seed_cash(repo, "cash", "1000")
    seed_fund(repo, "fund", "003103")
    draft = sip.save_plan(
        product_id="fund",
        daily_amount=Decimal("100"),
        purchase_fee_rate=Decimal("0"),
        source_cash_product_id="cash",
        start_date=TRADE_DATE,
    )
    with repo.database.transaction() as conn:
        conn.execute(
            "UPDATE products SET status = ? WHERE id = ?",
            (ProductStatus.INACTIVE.value, inactive_product_id),
        )

    with pytest.raises(ValueError, match=f"^{message}$"):
        sip.activate(draft.id)

    assert repo.get_plan(draft.id).status is SipPlanStatus.DRAFT


@pytest.mark.parametrize(
    ("inactive_product_id", "reason"),
    [
        ("fund", "target_inactive"),
        ("cash", "source_inactive"),
    ],
)
def test_inactive_product_skips_intent_notifies_and_never_locks_cash(
    sip_services, inactive_product_id, reason
):
    repo, _sip, projector = sip_services
    notifications = []
    sip = SipService(
        repo,
        projector,
        PortfolioTransactionService(repo, projector),
        notifier=notifications.append,
    )
    seed_cash(repo, "cash", "1000")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0")
    with repo.database.transaction() as conn:
        conn.execute(
            "UPDATE products SET status = ? WHERE id = ?",
            (ProductStatus.INACTIVE.value, inactive_product_id),
        )

    execution = sip.ensure_intent(plan.id, TRADE_DATE)

    assert execution.status == "skipped"
    assert execution.reason == reason
    assert notifications == [execution]
    assert plan_transactions(repo, plan.id) == []
    assert projector.calculate("cash").locked_shares == Decimal("0")
    assert projector.calculate("cash").total_shares == Decimal("1000")


def test_failed_notification_is_persisted_and_can_be_retried(sip_services):
    repo, _sip, projector = sip_services
    attempts = []

    def flaky_notifier(execution):
        attempts.append(execution)
        if len(attempts) == 1:
            raise RuntimeError("notification unavailable")

    sip = SipService(
        repo,
        projector,
        PortfolioTransactionService(repo, projector),
        notifier=flaky_notifier,
    )
    seed_cash(repo, "cash", "99")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0")

    skipped = sip.ensure_intent(plan.id, TRADE_DATE)

    assert skipped.reason == "insufficient_balance"
    assert attempts == [skipped]
    assert sip.retry_notifications() == [skipped]
    assert attempts == [skipped, skipped]
    assert sip.retry_notifications() == []


def test_repeated_lifecycle_calls_are_idempotent_and_transitions_are_strict(
    sip_services,
):
    repo, sip, _projector = sip_services
    seed_cash(repo, "cash", "1000")
    seed_fund(repo, "fund", "003103")
    draft = sip.save_plan(
        product_id="fund",
        daily_amount=Decimal("100"),
        purchase_fee_rate=Decimal("0"),
        source_cash_product_id="cash",
        start_date=TRADE_DATE,
    )
    active = sip.activate(draft.id)
    with repo.database.transaction() as conn:
        conn.execute(
            "UPDATE sip_plans SET updated_at = '2000-01-01 00:00:00' "
            "WHERE id = ?",
            (active.id,),
        )
    assert sip.activate(active.id) == active
    with repo.database.connection() as conn:
        assert conn.execute(
            "SELECT updated_at FROM sip_plans WHERE id = ?", (active.id,)
        ).fetchone()[0] == "2000-01-01 00:00:00"

    paused = sip.pause(active.id)
    with pytest.raises(ValueError, match="^plan is paused$"):
        sip.activate(paused.id)
    with repo.database.transaction() as conn:
        conn.execute(
            """UPDATE sip_plans
               SET updated_at = '2000-01-02 00:00:00',
                   paused_at = '2000-01-02 00:00:00'
               WHERE id = ?""",
            (paused.id,),
        )
    assert sip.pause(paused.id) == paused
    with repo.database.connection() as conn:
        row = conn.execute(
            "SELECT updated_at, paused_at FROM sip_plans WHERE id = ?",
            (paused.id,),
        ).fetchone()
    assert tuple(row) == (
        "2000-01-02 00:00:00",
        "2000-01-02 00:00:00",
    )

    resumed = sip.resume(paused.id)
    with repo.database.transaction() as conn:
        conn.execute(
            "UPDATE sip_plans SET updated_at = '2000-01-03 00:00:00' "
            "WHERE id = ?",
            (resumed.id,),
        )
    assert sip.resume(resumed.id) == resumed
    with repo.database.connection() as conn:
        assert conn.execute(
            "SELECT updated_at FROM sip_plans WHERE id = ?", (resumed.id,)
        ).fetchone()[0] == "2000-01-03 00:00:00"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("amount", "101"),
        ("shares", "101"),
        ("fee_rate", "1"),
        ("created_by", "operator"),
    ],
)
def test_settlement_rejects_semantically_inconsistent_pending_pair(
    sip_services, column, value
):
    repo, sip, projector = sip_services
    seed_cash(repo, "cash", "1000")
    seed_fund(repo, "fund", "003103")
    plan = active_plan(sip, "fund", "cash", "100", "0.01")
    pending = sip.ensure_intent(plan.id, TRADE_DATE)
    purchase = repo.get_transaction_by_id(pending.transaction_id)
    cash = repo.get_transaction_by_id(purchase.linked_transaction_id)
    target_id = purchase.id if column in {"fee_rate", "created_by"} else cash.id
    with repo.database.transaction() as conn:
        conn.execute(
            f"UPDATE transactions SET {column} = ? WHERE id = ?",
            (value, target_id),
        )
    seed_quote(repo, "fund", TRADE_DATE, "1")

    with pytest.raises(ValueError, match="^inconsistent SIP transaction pair$"):
        sip.settle_pending(TRADE_DATE)

    assert repo.get_transaction_by_id(purchase.id).status is (
        TransactionStatus.PENDING_QUOTE
    )
    assert repo.get_transaction_by_id(cash.id).status is (
        TransactionStatus.PENDING_QUOTE
    )
    assert projector.calculate("cash").locked_shares == Decimal(value) if (
        column == "shares"
    ) else Decimal("100")
