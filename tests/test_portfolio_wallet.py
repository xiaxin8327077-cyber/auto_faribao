from datetime import date
from decimal import Decimal

from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    Product,
    ProductStatus,
    ProductType,
    SipPlan,
    SipPlanStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_providers import get_market_provider
from src.portfolio_repository import PortfolioRepository
from src.portfolio_wallet import (
    WALLET_CODE,
    WALLET_PRODUCT_ID,
    WalletPlusProvider,
    consolidate_cash_products,
)


DAY = date(2026, 7, 31)


def _repository(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    return PortfolioRepository(database)


def _add_cash(repository, product_id, amount):
    repository.add_product(
        Product(
            id=product_id,
            provider="legacy",
            code=product_id,
            name=product_id,
            product_type=ProductType.CASH_MANAGEMENT,
        )
    )
    repository.create_transaction(
        Transaction(
            id=f"opening:{product_id}",
            product_id=product_id,
            transaction_type=TransactionType.OPENING_POSITION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 1),
            idempotency_key=f"opening:{product_id}",
            amount=Decimal(amount),
            shares=Decimal(amount),
            confirmation_nav=Decimal("1"),
            confirmation_date=date(2026, 7, 1),
        )
    )


def test_wallet_provider_returns_fixed_natural_day_yield():
    provider = get_market_provider("wallet_plus")

    assert isinstance(provider, WalletPlusProvider)
    product = provider.resolve_product(WALLET_CODE)
    quotes = provider.fetch_quotes(product, date(2026, 7, 30), DAY)

    assert product.name == "钱包Plus"
    assert product.product_type is ProductType.CASH_MANAGEMENT
    assert [quote.quote_date for quote in quotes] == [
        DAY,
        date(2026, 7, 30),
    ]
    assert {quote.income_per_10k for quote in quotes} == {Decimal("0.3644")}
    assert {quote.seven_day_annualized_rate for quote in quotes} == {
        Decimal("0.0133")
    }


def test_cash_consolidation_moves_balances_and_sip_source_once(tmp_path):
    repository = _repository(tmp_path)
    _add_cash(repository, "cash-a", "100.25")
    _add_cash(repository, "cash-b", "49.75")
    repository.add_product(
        Product(
            id="fund",
            provider="test",
            code="003103",
            name="公募基金",
            product_type=ProductType.PUBLIC_FUND,
        )
    )
    repository.save_plan(
        SipPlan(
            id="plan",
            product_id="fund",
            daily_amount=Decimal("100"),
            purchase_fee_rate=Decimal("0"),
            source_cash_product_id="cash-a",
            status=SipPlanStatus.ACTIVE,
            start_date=date(2026, 7, 1),
        )
    )

    first = consolidate_cash_products(repository, DAY)
    second = consolidate_cash_products(repository, DAY)
    PositionProjector(repository).rebuild()

    assert first.migrated_balance == Decimal("150")
    assert second.migrated_balance == Decimal("0")
    wallet = repository.require_product(WALLET_PRODUCT_ID)
    assert wallet.name == "钱包Plus"
    assert wallet.status is ProductStatus.ACTIVE
    assert PositionProjector(repository).calculate(
        WALLET_PRODUCT_ID
    ).total_shares == Decimal("150")
    assert PositionProjector(repository).calculate("cash-a").total_shares == 0
    assert PositionProjector(repository).calculate("cash-b").total_shares == 0
    assert repository.require_product("cash-a").status is ProductStatus.INACTIVE
    assert repository.require_product("cash-b").status is ProductStatus.INACTIVE
    assert repository.require_product("fund").status is ProductStatus.ACTIVE
    assert repository.get_plan("plan").source_cash_product_id == WALLET_PRODUCT_ID
    assert len(
        [
            transaction
            for transaction in repository.list_transactions()
            if transaction.created_by == "wallet_migration"
        ]
    ) == 3
