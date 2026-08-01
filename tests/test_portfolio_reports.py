from datetime import date
from decimal import Decimal

import pytest

from src.config import Config
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
    push_portfolio_report,
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
        product_code="003103", quote_date=date(2026, 7, 28),
        source="changsheng_fund", raw_hash="h0",
        unit_nav=Decimal("1.0300"), cumulative_nav=Decimal("1.1979"),
    ), fetched_at="2026-07-28T09:00:00")
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
    assert "最新收益（2026-07-29）：0.21 元" in text


def test_fund_report_shows_pending_when_no_quote(report_repo):
    text = build_portfolio_query_report(report_repo, target_date=date(2026, 7, 27))
    assert "最新净值：待披露" in text


def test_cash_report_shows_pending_per_10k(report_repo):
    text = build_portfolio_query_report(report_repo, target_date=date(2026, 7, 27))
    assert "最新每万份收益：待披露" in text


def test_format_portfolio_config_lists_products(report_repo):
    report_repo.add_product(Product(
        "config-zero",
        "nanyin_wealth",
        "CONFIGZERO",
        "配置中但无份额产品",
        ProductType.WEALTH_NAV,
    ))

    text = format_portfolio_config(report_repo, as_of=date(2026, 7, 30))

    assert "NYRR000007" in text
    assert "003103" in text
    assert "CONFIGZERO" not in text
    assert "现金管理" in text
    assert "公募基金" in text
    assert "理财看板网页" in text


def test_empty_portfolio_report(report_repo, monkeypatch):
    monkeypatch.setattr(report_repo, "list_products", lambda active_only=False: [])
    text = build_portfolio_query_report(report_repo, target_date=date(2026, 7, 30))
    assert "未配置" in text


def test_portfolio_report_uses_same_visible_positions_as_dashboard(report_repo):
    redeemed = Product(
        "redeemed",
        "nanyin_wealth",
        "A32069",
        "已全部赎回产品",
        ProductType.WEALTH_NAV,
    )
    report_repo.add_product(redeemed)

    text = build_portfolio_query_report(
        report_repo,
        target_date=date(2026, 7, 30),
    )

    assert "NYRR000007" in text
    assert "003103" in text
    assert "A32069" not in text


def test_historical_report_still_uses_current_dashboard_holdings(report_repo):
    redeemed = Product(
        "redeemed-history",
        "nanyin_wealth",
        "A32070",
        "历史查询前已清仓产品",
        ProductType.WEALTH_NAV,
    )
    report_repo.add_product(redeemed)
    report_repo.create_transaction(Transaction(
        id="open:redeemed-history",
        product_id=redeemed.id,
        transaction_type=TransactionType.OPENING_POSITION,
        status=TransactionStatus.CONFIRMED,
        trade_date=date(2026, 7, 1),
        idempotency_key="open:redeemed-history",
        amount=Decimal("50"),
        shares=Decimal("50"),
    ))
    report_repo.create_transaction(Transaction(
        id="redeem:redeemed-history",
        product_id=redeemed.id,
        transaction_type=TransactionType.MANUAL_REDEMPTION,
        status=TransactionStatus.CONFIRMED,
        trade_date=date(2026, 7, 29),
        confirmation_date=date(2026, 7, 30),
        settlement_date=date(2026, 7, 30),
        idempotency_key="redeem:redeemed-history",
        shares=Decimal("50"),
    ))
    PositionProjector(report_repo).rebuild()

    text = build_portfolio_query_report(
        report_repo,
        target_date=date(2026, 7, 29),
        holdings_as_of=date(2026, 8, 1),
    )

    assert "A32070" not in text


def test_wechat_push_sends_only_dashboard_position_products(
    report_repo,
    monkeypatch,
):
    report_repo.add_product(Product(
        "zero-position",
        "nanyin_wealth",
        "ZERO001",
        "零持仓产品",
        ProductType.WEALTH_NAV,
    ))
    sent = []
    monkeypatch.setattr(
        "src.wechat_notifier.send_markdown",
        lambda _wechat, text, to_user=None: sent.append((text, to_user)),
    )

    report = push_portfolio_report(
        Config({"wechat": {}}),
        report_repo,
        target_date=date(2026, 7, 30),
        holdings_as_of=date(2026, 7, 30),
        to_user="user1",
    )

    assert sent == [(report, "user1")]
    assert "NYRR000007" in report
    assert "003103" in report
    assert "ZERO001" not in report


def test_wechat_report_excludes_pending_purchase_without_held_shares(report_repo):
    pending = Product(
        "pending-purchase",
        "changsheng_fund",
        "PENDING001",
        "仅有在途申购产品",
        ProductType.PUBLIC_FUND,
    )
    report_repo.add_product(pending)
    report_repo.create_transaction(Transaction(
        id="pending:purchase",
        product_id=pending.id,
        transaction_type=TransactionType.MANUAL_PURCHASE,
        status=TransactionStatus.PENDING_QUOTE,
        trade_date=date(2026, 7, 30),
        idempotency_key="pending:purchase",
        amount=Decimal("1000"),
    ))

    report = build_portfolio_query_report(
        report_repo,
        target_date=date(2026, 7, 30),
        holdings_as_of=date(2026, 7, 30),
    )

    assert "PENDING001" not in report


def test_wechat_report_excludes_zero_shares_on_redemption_settlement_day(report_repo):
    redeemed = Product(
        "settling-redemption",
        "nanyin_wealth",
        "SETTLED001",
        "当日全部赎回产品",
        ProductType.WEALTH_NAV,
    )
    report_repo.add_product(redeemed)
    report_repo.create_transaction(Transaction(
        id="open:settling-redemption",
        product_id=redeemed.id,
        transaction_type=TransactionType.OPENING_POSITION,
        status=TransactionStatus.CONFIRMED,
        trade_date=date(2026, 7, 1),
        idempotency_key="open:settling-redemption",
        amount=Decimal("50"),
        shares=Decimal("50"),
    ))
    report_repo.create_transaction(Transaction(
        id="redeem:settling-redemption",
        product_id=redeemed.id,
        transaction_type=TransactionType.MANUAL_REDEMPTION,
        status=TransactionStatus.CONFIRMED,
        trade_date=date(2026, 7, 29),
        confirmation_date=date(2026, 7, 30),
        settlement_date=date(2026, 7, 30),
        idempotency_key="redeem:settling-redemption",
        shares=Decimal("50"),
    ))
    PositionProjector(report_repo).rebuild()

    report = build_portfolio_query_report(
        report_repo,
        target_date=date(2026, 7, 30),
        holdings_as_of=date(2026, 7, 30),
    )

    assert "SETTLED001" not in report


def test_period_report_uses_readable_chinese_period_label(report_repo):
    report = build_portfolio_query_report(
        report_repo,
        target_date=date(2026, 7, 30),
        period="week",
        holdings_as_of=date(2026, 7, 30),
    )

    assert "理财组合周度查询" in report
    assert "截至 2026-07-30" in report
    assert "统计起点：2026-07-27" in report
    assert "期间收益：1.00 元" in report
    assert "期间收益：0.21 元" in report
    assert "（week）" not in report
