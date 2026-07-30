from datetime import date
from decimal import Decimal

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
from src.portfolio_reports import (
    build_portfolio_query_report,
    format_portfolio_config,
)
from src.portfolio_repository import PortfolioRepository


@pytest.fixture
def report_repo(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()
    repo = PortfolioRepository(db)

    cash = Product(
        "cash", "nanyin_wealth", "NYRR000007", "南银理财日日聚宝15号A",
        ProductType.CASH_MANAGEMENT, registration_code="Z7003226000154",
    )
    fund = Product(
        "fund", "changsheng_fund", "003103", "长盛盛裕纯债C",
        ProductType.PUBLIC_FUND,
    )
    repo.add_product(cash)
    repo.add_product(fund)

    opening = Decimal("10000")
    repo.create_transaction(Transaction(
        id="open:cash", product_id="cash",
        transaction_type=TransactionType.OPENING_POSITION,
        status=TransactionStatus.CONFIRMED, trade_date=date(2026, 7, 1),
        idempotency_key="open:cash", amount=opening, shares=opening,
    ))
    fund_shares = Decimal("100")
    repo.create_transaction(Transaction(
        id="open:fund", product_id="fund",
        transaction_type=TransactionType.OPENING_POSITION,
        status=TransactionStatus.CONFIRMED, trade_date=date(2026, 7, 28),
        idempotency_key="open:fund", amount=fund_shares, shares=fund_shares,
    ))

    repo.upsert_quote("cash", MarketQuote(
        product_code="NYRR000007", quote_date=date(2026, 7, 30),
        source="nanyin_wealth", raw_hash="h1",
        income_per_10k=Decimal("0.4475"),
        seven_day_annualized_rate=Decimal("0.016315"),
    ), fetched_at="2026-07-30T09:00:00")
    repo.upsert_quote("fund", MarketQuote(
        product_code="003103", quote_date=date(2026, 7, 29),
        source="changsheng_fund", raw_hash="h2",
        unit_nav=Decimal("1.0321"), cumulative_nav=Decimal("1.2000"),
    ), fetched_at="2026-07-29T09:00:00")

    repo.create_transaction(Transaction(
        id="income:1", product_id="cash",
        transaction_type=TransactionType.INCOME_ACCRUAL,
        status=TransactionStatus.CONFIRMED, trade_date=date(2026, 7, 29),
        idempotency_key="income:cash:2026-07-29",
        amount=Decimal("0.50"), shares=Decimal("0.50"),
        confirmation_nav=Decimal("1"), confirmation_date=date(2026, 7, 29),
    ))
    repo.create_transaction(Transaction(
        id="income:2", product_id="cash",
        transaction_type=TransactionType.INCOME_ACCRUAL,
        status=TransactionStatus.CONFIRMED, trade_date=date(2026, 7, 30),
        idempotency_key="income:cash:2026-07-30",
        amount=Decimal("0.500025"), shares=Decimal("0.500025"),
        confirmation_nav=Decimal("1"), confirmation_date=date(2026, 7, 30),
    ))

    PositionProjector(repo).rebuild()
    return repo


def test_cash_report_displays_per_10k_and_seven_day_rate(report_repo):
    text = build_portfolio_query_report(report_repo, target_date=date(2026, 7, 30))
    assert "每万份收益：0.4475 元" in text
    assert "七日年化：1.6315%" in text
    assert "净值：1.000000" not in text
    assert "南银理财日日聚宝15号A" in text
    assert "NYRR000007" in text


def test_fund_report_uses_real_position_and_unit_nav(report_repo):
    text = build_portfolio_query_report(report_repo, target_date=date(2026, 7, 29))
    assert "003103" in text
    assert "单位净值：1.0321" in text
    assert "长盛盛裕纯债C" in text


def test_fund_report_shows_pending_when_no_quote(report_repo):
    text = build_portfolio_query_report(report_repo, target_date=date(2026, 7, 27))
    assert "最新净值：待披露" in text


def test_cash_report_shows_pending_per_10k(report_repo):
    text = build_portfolio_query_report(report_repo, target_date=date(2026, 7, 27))
    assert "最新每万份收益：待披露" in text


def test_format_portfolio_config_lists_products(report_repo):
    text = format_portfolio_config(report_repo)
    assert "NYRR000007" in text
    assert "003103" in text
    assert "现金管理" in text
    assert "公募基金" in text
    assert "理财看板网页" in text


def test_empty_portfolio_report(report_repo, monkeypatch):
    monkeypatch.setattr(report_repo, "list_products", lambda active_only=False: [])
    text = build_portfolio_query_report(report_repo, target_date=date(2026, 7, 30))
    assert "未配置" in text
