from contextlib import contextmanager
from datetime import date
from decimal import Decimal
import threading

import pytest

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    MarketQuote,
    Product,
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository
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


def disable_confirmed_transaction_guard(repository):
    with repository.database.connection() as conn:
        conn.execute("DROP TRIGGER prevent_confirmed_transaction_mutation")


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
    assert len(repository.list_transactions()) == 4
    assert projector.calculate("fund").locked_shares == Decimal("25")


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


def test_confirm_pending_redemption_updates_destination_amount(services):
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
    cash_leg = linked_leg(repository, confirmed)
    assert cash_leg.status is TransactionStatus.CONFIRMED
    assert cash_leg.amount == Decimal("30")
    assert cash_leg.shares == Decimal("30")
    assert projector.calculate("fund").total_shares == Decimal("75")
    assert projector.calculate("cash").total_shares == Decimal("40")


def test_confirmed_redemption_rejects_corrupt_destination_amount(services):
    repository, transactions, _projector = services
    fund = seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_opening_position(repository, "fund", "100")
    seed_cash(repository, "cash", "0")
    quote = seed_quote(repository, fund, TRADE_DATE, Decimal("1.2"))
    redemption = transactions.record_redemption(
        "fund",
        Decimal("25"),
        TRADE_DATE,
        "web:corrupt-redemption-destination",
        destination_cash_product_id="cash",
    )
    destination = linked_leg(repository, redemption)
    disable_confirmed_transaction_guard(repository)
    with repository.database.connection() as conn:
        conn.execute(
            "UPDATE transactions SET amount = ? WHERE id = ?",
            ("999", destination.id),
        )

    with pytest.raises(
        ValueError, match="idempotency key conflicts with existing request"
    ):
        transactions.record_redemption(
            "fund",
            Decimal("25"),
            TRADE_DATE,
            "web:corrupt-redemption-destination",
            destination_cash_product_id="cash",
        )
    with pytest.raises(ValueError, match="inconsistent linked transaction"):
        transactions.confirm_pending(redemption.id, quote)


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


def test_redemption_projector_failure_rolls_back_ledger_and_positions(
    services, monkeypatch
):
    repository, transactions, projector = services
    seed_product(repository, "fund", ProductType.PUBLIC_FUND, "003103")
    seed_opening_position(repository, "fund", "100")
    seed_cash(repository, "cash", "0")
    before_fund = repository.get_position("fund")
    before_cash = repository.get_position("cash")
    original_calculate = projector._calculate

    def fail_destination_calculation(product_id, conn=None):
        if product_id == "cash":
            raise RuntimeError("destination projection failed")
        return original_calculate(product_id, conn)

    monkeypatch.setattr(projector, "_calculate", fail_destination_calculation)

    with pytest.raises(RuntimeError, match="destination projection failed"):
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
