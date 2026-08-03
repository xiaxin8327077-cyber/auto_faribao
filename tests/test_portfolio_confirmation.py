from datetime import date, datetime, timezone
import json

import pytest

from src.portfolio_confirmation import (
    MissingTradingCalendarError,
    confirmation_schedule,
)
from src.portfolio_models import Product, ProductType, TransactionType


def fund(metadata=None):
    return Product(
        id="fund",
        provider="changsheng_fund",
        code="003103",
        name="长盛盛裕纯债C",
        product_type=ProductType.PUBLIC_FUND,
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
    )


def wallet_plus():
    return Product(
        id="wallet-plus",
        provider="fixed",
        code="WALLETPLUS",
        name="钱包Plus",
        product_type=ProductType.CASH_MANAGEMENT,
    )


def test_public_fund_before_15_cutoff_confirms_next_trading_day():
    schedule = confirmation_schedule(
        fund(),
        TransactionType.MANUAL_PURCHASE,
        datetime(2026, 7, 30, 14, 59),
    )

    assert schedule.trade_date == date(2026, 7, 30)
    assert schedule.confirmation_date == date(2026, 7, 31)


def test_public_fund_exactly_at_15_cutoff_uses_same_trade_day():
    schedule = confirmation_schedule(
        fund(),
        TransactionType.MANUAL_PURCHASE,
        datetime(2026, 7, 30, 15, 0),
    )

    assert schedule.trade_date == date(2026, 7, 30)
    assert schedule.confirmation_date == date(2026, 7, 31)


def test_public_fund_after_15_cutoff_confirms_second_next_trading_day():
    schedule = confirmation_schedule(
        fund(),
        TransactionType.MANUAL_REDEMPTION,
        datetime(2026, 7, 31, 15, 1),
    )

    assert schedule.trade_date == date(2026, 8, 3)
    assert schedule.confirmation_date == date(2026, 8, 4)


def test_wallet_plus_purchase_confirms_next_trading_day():
    schedule = confirmation_schedule(
        wallet_plus(),
        TransactionType.MANUAL_PURCHASE,
        datetime(2026, 7, 30, 10, 0),
    )

    assert schedule.trade_date == date(2026, 7, 30)
    assert schedule.confirmation_date == date(2026, 7, 31)


def test_wallet_plus_weekend_submission_uses_next_trading_day_then_t1():
    schedule = confirmation_schedule(
        wallet_plus(),
        TransactionType.MANUAL_REDEMPTION,
        datetime(2026, 8, 1, 10, 0),
    )

    assert schedule.trade_date == date(2026, 8, 3)
    assert schedule.confirmation_date == date(2026, 8, 4)


def test_aware_submission_time_is_converted_to_beijing_before_cutoff_check():
    schedule = confirmation_schedule(
        fund(),
        TransactionType.MANUAL_PURCHASE,
        datetime(2026, 7, 30, 7, 1, tzinfo=timezone.utc),
    )

    assert schedule.trade_date == date(2026, 7, 31)
    assert schedule.confirmation_date == date(2026, 8, 3)


def test_confirmation_refuses_dates_without_an_official_calendar():
    with pytest.raises(
        MissingTradingCalendarError,
        match="2027",
    ):
        confirmation_schedule(
            fund(),
            TransactionType.MANUAL_PURCHASE,
            datetime(2027, 1, 4, 10),
        )


def test_product_metadata_overrides_purchase_and_redemption_rules():
    product = fund(
        {
            "trade_rules": {
                "purchase": {
                    "cutoff_time": "14:30",
                    "confirmation_trading_days": 2,
                },
                "redemption": {
                    "cutoff_time": "16:00",
                    "confirmation_trading_days": 3,
                },
            }
        }
    )

    purchase = confirmation_schedule(
        product,
        TransactionType.MANUAL_PURCHASE,
        datetime(2026, 7, 30, 14, 31),
    )
    redemption = confirmation_schedule(
        product,
        TransactionType.MANUAL_REDEMPTION,
        datetime(2026, 7, 30, 14, 31),
    )

    assert purchase.trade_date == date(2026, 7, 31)
    assert purchase.confirmation_date == date(2026, 8, 4)
    assert redemption.trade_date == date(2026, 7, 30)
    assert redemption.confirmation_date == date(2026, 8, 4)
