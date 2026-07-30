from decimal import Decimal

from src.portfolio_models import (
    ProductType,
    TransactionStatus,
    TransactionType,
    decimal_text,
    optional_decimal_text,
)


def test_portfolio_enums_use_stable_database_values():
    assert ProductType.CASH_MANAGEMENT.value == "cash_management"
    assert TransactionType.SIP_PURCHASE.value == "sip_purchase"
    assert TransactionStatus.PENDING_QUOTE.value == "pending_quote"


def test_decimal_text_is_canonical_and_never_uses_float_rounding():
    assert decimal_text("2000.000") == "2000"
    assert decimal_text(Decimal("0.00006")) == "0.00006"
    assert optional_decimal_text("") is None
