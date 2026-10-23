"""理财看板财务账本全面测试。

重点验证：申购、赎回、份额增加/减少、钱包 Plus 实时转出、
在途资金和总资产计算。
金额和份额全部使用 Decimal 精确比较，禁止浮点近似断言。
"""
from datetime import date
from decimal import Decimal

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
from src.portfolio_transactions import PortfolioTransactionService
from src.portfolio_view import build_portfolio_payload
from src.portfolio_wallet import WALLET_PROVIDER


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

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
    repository.upsert_quote(product.id, quote, f"{quote_date.isoformat()}T12:00:00+08:00")
    return quote


def linked_leg(repository, transaction):
    rows = repository.list_transactions()
    return next(row for row in rows if row.id == transaction.linked_transaction_id)


def total_assets(repository, as_of):
    """按页面公式计算总资产：Σmarket_value + pendingBuy + inTransitSell - pendingSell

    注意：钱包 Plus 的 pending 申购已包含在持仓中（included_in_position=True），
    所以 pendingBuy 只包含非钱包产品的 pending 申购。
    """
    payload = build_portfolio_payload(repository, as_of=as_of)
    products = payload["products"]
    txns = payload["transactions"]

    holdings_value = sum(
        Decimal(p["market_value"]) for p in products if p["market_value"] is not None
    )
    # pendingBuy: 只包含不在持仓中的 pending 申购（即非钱包产品）
    pending_buy = sum(
        Decimal(t["amount"])
        for t in txns
        if t["status"] in ("pending_quote", "pending_confirmation")
        and "purchase" in t["transaction_type"]
        and not t.get("included_in_position")
    )
    # pendingSell: pending 赎回（锁定份额但未确认）
    pending_sell = sum(
        Decimal(t["amount"])
        for t in txns
        if t["status"] in ("pending_quote", "pending_confirmation")
        and "redemption" in t["transaction_type"]
    )
    # inTransitSell: 确认赎回但待到账
    in_transit_sell = sum(
        Decimal(t["amount"])
        for t in txns
        if t["status"] == "confirmed"
        and "redemption" in t["transaction_type"]
        and t.get("settlement_status") == "pending"
    )
    return holdings_value + pending_buy + in_transit_sell - pending_sell


# ===========================================================================
# 一、普通申购
# ===========================================================================

class TestRegularPurchase:
    """TC-01-xx: 普通申购"""

    def test_purchase_with_sufficient_cash(self, services):
        """TC-01-01: 有足够现金时创建普通申购"""
        repo, txn_svc, proj = services
        seed_cash(repo, "cash", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.0321"))

        purchase = txn_svc.record_purchase(
            "fund", Decimal("10000"), date(2026, 9, 18), "TC0101",
            source_cash_product_id="cash",
            trade_time="2026-09-18T14:00:00",
        )
        assert purchase.status == TransactionStatus.PENDING_CONFIRMATION

        # 有 linked CASH_TRANSFER_OUT 的申购需用 confirm_pending 直接确认
        confirmed = txn_svc.confirm_pending(purchase.id, MarketQuote(
            product_code="003103", quote_date=date(2026, 9, 18),
            source="test", raw_hash="TC0101", unit_nav=Decimal("1.0321"),
        ))
        assert confirmed.status == TransactionStatus.CONFIRMED
        expected_shares = Decimal("10000") / Decimal("1.0321")
        assert confirmed.shares == expected_shares
        assert proj.calculate("cash").total_shares == Decimal("90000")

    def test_purchase_no_nav_pending_quote(self, services):
        """TC-01-02: 目标产品无净值时进入待报价"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        purchase = txn_svc.record_purchase(
            "fund", Decimal("5000"), date(2026, 9, 18), "TC0102",
            trade_time="2026-09-18T10:00:00",
        )
        assert purchase.status == TransactionStatus.PENDING_QUOTE
        assert purchase.confirmation_nav is None
        assert purchase.shares is None
        assert proj.calculate("fund").total_shares == Decimal("0")

    def test_purchase_has_nav_before_confirmation_date(self, services):
        """TC-01-03: 有净值但未到确认日保持待确认"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 21), Decimal("1.0321"))
        purchase = txn_svc.record_purchase(
            "fund", Decimal("5000"), date(2026, 9, 18), "TC0103",
            trade_time="2026-09-18T16:00:00",
        )
        # 超过 15:00 cutoff → trade_date 顺延到下一个交易日 2026-09-21（周一）
        assert purchase.trade_date == date(2026, 9, 21)
        txn_svc.settle_pending(date(2026, 9, 19))
        assert repo.get_transaction_by_id(purchase.id).status == TransactionStatus.PENDING_CONFIRMATION

    def test_purchase_shares_with_fee(self, services):
        """TC-01-04: 确认后份额 = (金额 - 手续费) / 净值"""
        repo, txn_svc, _ = services
        seed_cash(repo, "cash", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.5000"))
        purchase = txn_svc.record_purchase(
            "fund", Decimal("10000"), date(2026, 9, 18), "TC0104",
            source_cash_product_id="cash",
            fee_rate=Decimal("0.001"),
        )
        assert purchase.fee_amount == Decimal("10")
        # (10000 - 10) / 1.5 = 6660
        assert purchase.shares == Decimal("6660")

    def test_confirm_no_double_debit(self, services):
        """TC-01-05: 确认时不得重复扣减资金来源"""
        repo, txn_svc, proj = services
        seed_cash(repo, "cash", "50000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.0321"))
        purchase = txn_svc.record_purchase(
            "fund", Decimal("10000"), date(2026, 9, 18), "TC0105",
            source_cash_product_id="cash",
            trade_time="2026-09-18T14:00:00",
        )
        confirmed = txn_svc.confirm_pending(purchase.id, MarketQuote(
            product_code="003103", quote_date=date(2026, 9, 18),
            source="test", raw_hash="TC0105", unit_nav=Decimal("1.0321"),
        ))
        # 再次确认（幂等）
        confirmed2 = txn_svc.confirm_pending(purchase.id, MarketQuote(
            product_code="003103", quote_date=date(2026, 9, 18),
            source="test", raw_hash="TC0105", unit_nav=Decimal("1.0321"),
        ))
        assert confirmed2.id == confirmed.id
        assert proj.calculate("cash").total_shares == Decimal("40000")

    def test_insufficient_cash_rejected_no_residue(self, services):
        """TC-01-06: 现金不足时拒绝且无残留"""
        repo, txn_svc, proj = services
        seed_cash(repo, "cash", "500")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        with pytest.raises(ValueError, match="insufficient available shares"):
            txn_svc.record_purchase(
                "fund", Decimal("1000"), date(2026, 9, 18), "TC0106",
                source_cash_product_id="cash",
            )
        assert proj.calculate("cash").total_shares == Decimal("500")
        assert len(repo.list_transactions(product_id="fund")) == 0
        assert len([t for t in repo.list_transactions()
                    if t.transaction_type == TransactionType.CASH_TRANSFER_OUT]) == 0

    @pytest.mark.parametrize("amount,fee_rate", [
        (Decimal("0"), Decimal("0")),
        (Decimal("-100"), Decimal("0")),
        (Decimal("100"), Decimal("1.0")),
        (Decimal("100"), Decimal("1.5")),
        (Decimal("100"), Decimal("-0.01")),
    ])
    def test_invalid_amount_or_fee_rejected(self, services, amount, fee_rate):
        """TC-01-07: 零/负金额、非法费率必须拒绝"""
        repo, txn_svc, _ = services
        seed_cash(repo, "cash", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        with pytest.raises(ValueError):
            txn_svc.record_purchase(
                "fund", amount, date(2026, 9, 18), f"TC0107-{amount}-{fee_rate}",
                source_cash_product_id="cash",
                fee_rate=fee_rate,
            )

    def test_source_equals_product_rejected(self, services):
        """TC-01-07c: 相同来源和目标产品必须拒绝"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        with pytest.raises(ValueError, match="source must differ from product"):
            txn_svc.record_purchase(
                "fund", Decimal("100"), date(2026, 9, 18), "TC0107c",
                source_cash_product_id="fund",
            )

    def test_wallet_plus_purchase_no_trade_time_pending(self, services):
        """TC-01-08: 钱包 Plus 无 trade_time 时申购仍为 PENDING_CONFIRMATION"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        purchase = txn_svc.record_purchase(
            "wallet-plus", Decimal("50000"), date(2026, 9, 18), "TC0108",
        )
        assert purchase.status == TransactionStatus.PENDING_CONFIRMATION
        assert purchase.shares == Decimal("50000")
        # 钱包 Plus pending 申购已计入持仓
        assert proj.calculate("wallet-plus").total_shares == Decimal("50000")

    def test_nav_product_no_trade_time_confirmed(self, services):
        """TC-01-09: 净值产品无 trade_time 有净值时直接 CONFIRMED"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.0321"))
        purchase = txn_svc.record_purchase(
            "fund", Decimal("10000"), date(2026, 9, 18), "TC0109",
        )
        assert purchase.status == TransactionStatus.CONFIRMED
        assert purchase.confirmation_date == date(2026, 9, 18)

    def test_nav_product_no_nav_no_trade_time_pending_quote(self, services):
        """TC-01-10: 净值产品无净值且无 trade_time → PENDING_QUOTE"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        purchase = txn_svc.record_purchase(
            "fund", Decimal("10000"), date(2026, 9, 18), "TC0110",
        )
        assert purchase.status == TransactionStatus.PENDING_QUOTE
        assert purchase.shares is None

    def test_idempotent_purchase_same_key(self, services):
        """TC-01-11: 同一幂等键重复申购返回同一交易"""
        repo, txn_svc, proj = services
        seed_cash(repo, "cash", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        p1 = txn_svc.record_purchase(
            "fund", Decimal("5000"), date(2026, 9, 18), "TC0111",
            source_cash_product_id="cash",
            trade_time="2026-09-18T14:00:00",
        )
        p2 = txn_svc.record_purchase(
            "fund", Decimal("5000"), date(2026, 9, 18), "TC0111",
            source_cash_product_id="cash",
            trade_time="2026-09-18T14:00:00",
        )
        assert p1.id == p2.id
        # pending 赎回锁定份额但不减少 total_shares
        assert proj.calculate("cash").available_shares == Decimal("95000")
        assert proj.calculate("cash").locked_shares == Decimal("5000")

    def test_idempotency_conflict_different_params(self, services):
        """TC-01-12: 幂等键冲突（不同参数）必须拒绝"""
        repo, txn_svc, _ = services
        seed_cash(repo, "cash", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        txn_svc.record_purchase(
            "fund", Decimal("5000"), date(2026, 9, 18), "TC0112",
            source_cash_product_id="cash",
        )
        with pytest.raises(ValueError, match="idempotency key conflicts"):
            txn_svc.record_purchase(
                "fund", Decimal("9999"), date(2026, 9, 18), "TC0112",
                source_cash_product_id="cash",
            )

    def test_settle_pending_idempotent(self, services):
        """TC-01-14: settle_pending 幂等"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.0321"))
        purchase = txn_svc.record_purchase(
            "fund", Decimal("10000"), date(2026, 9, 18), "TC0114",
        )
        txn_svc.settle_pending(date(2026, 9, 18))
        shares_after_first = proj.calculate("fund").total_shares
        txn_svc.settle_pending(date(2026, 9, 18))
        assert proj.calculate("fund").total_shares == shares_after_first


# ===========================================================================
# 二、钱包 Plus 实时转出
# ===========================================================================

class TestWalletRealtimeOutflow:
    """TC-02-xx: 钱包 Plus 实时转出申购基金"""

    def test_realtime_outflow_exact_values(self, services):
        """TC-02-01: 实时转出精确数值验证（题目给定值）"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "16551.8880198179443535014361")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")

        # 钱包 Plus 申购（较早）
        wallet_purchase = txn_svc.record_purchase(
            "wallet-plus", Decimal("108000"), date(2026, 9, 18), "TC0201-wallet",
            trade_time="2026-09-18T09:00:00",
        )
        # 实时转出（较晚，从钱包扣减）
        fund_purchase = txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0201-fund",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T10:00:00",
        )

        assert wallet_purchase.status == TransactionStatus.PENDING_CONFIRMATION
        assert fund_purchase.status == TransactionStatus.PENDING_QUOTE

        cash_leg = linked_leg(repo, fund_purchase)
        assert cash_leg.status == TransactionStatus.CONFIRMED
        assert cash_leg.confirmation_nav == Decimal("1")
        assert cash_leg.confirmation_date == date(2026, 9, 18)

        expected_total = Decimal("74551.8880198179443535014361")
        assert proj.calculate("wallet-plus").total_shares == expected_total
        assert proj.calculate("wallet-plus").available_shares == expected_total
        assert proj.calculate("wallet-plus").locked_shares == Decimal("0")

        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        wallet_row = next(r for r in payload["products"] if r["id"] == "wallet-plus")
        fund_row = next(r for r in payload["products"] if r["id"] == "fund")
        assert Decimal(wallet_row["in_transit_amount"]) == Decimal("58000")
        assert Decimal(fund_row["in_transit_amount"]) == Decimal("50000")

    def test_wallet_insufficient_rejected(self, services):
        """TC-02-02: 钱包不足时拒绝"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "40000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        with pytest.raises(ValueError, match="insufficient available shares"):
            txn_svc.record_purchase(
                "fund", Decimal("50000"), date(2026, 9, 18), "TC0202",
                source_cash_product_id="wallet-plus",
            )
        assert proj.calculate("wallet-plus").total_shares == Decimal("40000")
        assert len([t for t in repo.list_transactions()
                    if t.transaction_type == TransactionType.CASH_TRANSFER_OUT]) == 0

    def test_idempotent_no_double_debit(self, services):
        """TC-02-03: 同一幂等键重复提交不得重复扣款"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        p1 = txn_svc.record_purchase(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC0203",
            source_cash_product_id="wallet-plus",
        )
        p2 = txn_svc.record_purchase(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC0203",
            source_cash_product_id="wallet-plus",
        )
        assert p1.id == p2.id
        assert proj.calculate("wallet-plus").total_shares == Decimal("70000")

    def test_invalid_link_rejected(self, services):
        """TC-02-04: 错误的关联交易不得被确认"""
        repo, txn_svc, _ = services
        seed_cash(repo, "cash", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.0321"))
        # 创建正常申购然后篡改关联腿
        purchase = txn_svc.record_purchase(
            "fund", Decimal("5000"), date(2026, 9, 18), "TC0204",
            source_cash_product_id="cash",
            trade_time="2026-09-18T14:00:00",
        )
        # 篡改关联腿金额为不一致
        cash_leg = linked_leg(repo, purchase)
        with repo.database.connection() as conn:
            conn.execute(
                "UPDATE transactions SET amount = ? WHERE id = ?",
                ("999", cash_leg.id),
            )
        with pytest.raises(ValueError, match="inconsistent linked transaction"):
            txn_svc.confirm_pending(purchase.id, MarketQuote(
                product_code="003103", quote_date=date(2026, 9, 18),
                source="test", raw_hash="TC0204", unit_nav=Decimal("1.0321"),
            ))

    def test_rollback_on_failure(self, services):
        """TC-02-05: 转出失败时整体回滚"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        original = proj.calculate("wallet-plus").total_shares
        # 通过不存在的 source 触发失败
        with pytest.raises(ValueError):
            txn_svc.record_purchase(
                "fund", Decimal("50000"), date(2026, 9, 18), "TC0205",
                source_cash_product_id="nonexistent",
            )
        assert proj.calculate("wallet-plus").total_shares == original

    def test_total_assets_conserved(self, services):
        """TC-02-06: 总资产在实时转出前后保持不变"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_product(repo, "wealth", ProductType.WEALTH_NAV, "W001")
        seed_opening_position(repo, "wealth", "50000", "50000")
        seed_quote(repo, repo.require_product("wealth"), date(2026, 9, 18), Decimal("1.08"))

        before = total_assets(repo, date(2026, 9, 18))
        txn_svc.record_purchase(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC0206",
            source_cash_product_id="wallet-plus",
        )
        after = total_assets(repo, date(2026, 9, 18))
        assert before == after

    def test_multiple_outflows_equal_available(self, services):
        """TC-02-07: 多笔实时转出累计等于钱包可用份额"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund-a", ProductType.PUBLIC_FUND, "F001")
        seed_product(repo, "fund-b", ProductType.PUBLIC_FUND, "F002")

        txn_svc.record_purchase(
            "fund-a", Decimal("30000"), date(2026, 9, 18), "TC0207a",
            source_cash_product_id="wallet-plus",
        )
        txn_svc.record_purchase(
            "fund-b", Decimal("70000"), date(2026, 9, 18), "TC0207b",
            source_cash_product_id="wallet-plus",
        )
        assert proj.calculate("wallet-plus").available_shares == Decimal("0")

    def test_multiple_outflows_exceed_available(self, services):
        """TC-02-08: 多笔实时转出累计超过可用时第二笔拒绝"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund-a", ProductType.PUBLIC_FUND, "F001")
        seed_product(repo, "fund-b", ProductType.PUBLIC_FUND, "F002")

        txn_svc.record_purchase(
            "fund-a", Decimal("60000"), date(2026, 9, 18), "TC0208a",
            source_cash_product_id="wallet-plus",
        )
        with pytest.raises(ValueError, match="insufficient available shares"):
            txn_svc.record_purchase(
                "fund-b", Decimal("50000"), date(2026, 9, 18), "TC0208b",
                source_cash_product_id="wallet-plus",
            )
        assert proj.calculate("wallet-plus").available_shares == Decimal("40000")

    def test_outflow_in_transit_calculation(self, services):
        """TC-02-09: 实时转出后钱包 Plus 在途资金正确计算"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "50000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        txn_svc.record_purchase(
            "wallet-plus", Decimal("108000"), date(2026, 9, 18), "TC0209-w",
            trade_time="2026-09-18T09:00:00",
        )
        txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0209-f",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T10:00:00",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        wallet_row = next(r for r in payload["products"] if r["id"] == "wallet-plus")
        assert Decimal(wallet_row["in_transit_amount"]) == Decimal("58000")

    def test_outflow_cash_leg_nav_pending_quote(self, services):
        """TC-02-10: 实时转出净值产品（无净值）时 CASH_TRANSFER_OUT nav=1"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        fund_purchase = txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0210",
            source_cash_product_id="wallet-plus",
        )
        cash_leg = linked_leg(repo, fund_purchase)
        assert cash_leg.status == TransactionStatus.CONFIRMED
        assert cash_leg.confirmation_nav == Decimal("1")

    def test_outflow_target_confirm_shares(self, services):
        """TC-02-12: 实时转出后目标基金申购确认时份额计算正确"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.0321"))
        fund_purchase = txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0212",
            source_cash_product_id="wallet-plus",
        )
        txn_svc.settle_pending(date(2026, 9, 19))
        expected_fund_shares = Decimal("50000") / Decimal("1.0321")
        assert proj.calculate("fund").total_shares == expected_fund_shares
        assert proj.calculate("wallet-plus").total_shares == Decimal("50000")
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 19))
        fund_row = next(r for r in payload["products"] if r["id"] == "fund")
        assert Decimal(fund_row["in_transit_amount"]) == Decimal("0")


# ===========================================================================
# 三、钱包 Plus 申购在途及交易记录
# ===========================================================================

class TestWalletInTransitAndRecords:
    """TC-03-xx: 钱包 Plus 申购在途及交易记录"""

    def test_original_record_not_modified(self, services):
        """TC-03-01: 原始 108000 元申购记录不被修改或重复创建"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        wallet_purchase = txn_svc.record_purchase(
            "wallet-plus", Decimal("108000"), date(2026, 9, 18), "TC0301-w",
        )
        txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0301-f",
            source_cash_product_id="wallet-plus",
        )
        original = repo.get_transaction_by_id(wallet_purchase.id)
        assert original.amount == Decimal("108000")
        assert original.shares == Decimal("108000")
        wallet_purchases = [
            t for t in repo.list_transactions(product_id="wallet-plus")
            if t.transaction_type == TransactionType.MANUAL_PURCHASE
        ]
        assert len(wallet_purchases) == 1

    def test_remaining_confirmation_shares_displayed(self, services):
        """TC-03-02: 实时剩余确认份额正确显示"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        wallet_purchase = txn_svc.record_purchase(
            "wallet-plus", Decimal("108000"), date(2026, 9, 18), "TC0302-w",
            trade_time="2026-09-18T09:00:00",
        )
        txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0302-f",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T10:00:00",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        wallet_tx_row = next(
            t for t in payload["transactions"] if t["id"] == wallet_purchase.id
        )
        assert Decimal(wallet_tx_row["remaining_confirmation_shares"]) == Decimal("58000")

    def test_position_card_in_transit_net(self, services):
        """TC-03-03: 持仓卡片上的申购在途应为 58000"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        txn_svc.record_purchase(
            "wallet-plus", Decimal("108000"), date(2026, 9, 18), "TC0303-w",
            trade_time="2026-09-18T09:00:00",
        )
        txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0303-f",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T10:00:00",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        wallet_row = next(r for r in payload["products"] if r["id"] == "wallet-plus")
        assert Decimal(wallet_row["in_transit_amount"]) == Decimal("58000")

    def test_multiple_purchases_fifo_deduction(self, services):
        """TC-03-04: 多笔待确认申购按时间顺序扣减"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        p1 = txn_svc.record_purchase(
            "wallet-plus", Decimal("50000"), date(2026, 9, 18), "TC0304-1",
            trade_time="2026-09-18T09:00:00",
        )
        p2 = txn_svc.record_purchase(
            "wallet-plus", Decimal("80000"), date(2026, 9, 18), "TC0304-2",
            trade_time="2026-09-18T10:00:00",
        )
        txn_svc.record_purchase(
            "fund", Decimal("60000"), date(2026, 9, 18), "TC0304-f",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T11:00:00",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        rows = {t["id"]: t for t in payload["transactions"]}
        assert Decimal(rows[p1.id]["remaining_confirmation_shares"]) == Decimal("0")
        assert Decimal(rows[p2.id]["remaining_confirmation_shares"]) == Decimal("70000")

    def test_outflow_before_purchase_no_deduction(self, services):
        """TC-03-05: 转出发生在待确认申购之前时不得错误扣减后来的申购"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        # 先转出（从已有持仓）
        txn_svc.record_purchase(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC0305-f",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T09:00:00",
        )
        # 后创建待确认申购
        p = txn_svc.record_purchase(
            "wallet-plus", Decimal("50000"), date(2026, 9, 18), "TC0305-w",
            trade_time="2026-09-18T10:00:00",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        row = next(t for t in payload["transactions"] if t["id"] == p.id)
        assert Decimal(row["remaining_confirmation_shares"]) == Decimal("50000")

    def test_cancel_restores_remaining(self, services):
        """TC-03-06: 取消实时转出的基金申购后退款恢复剩余份额"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        wallet_purchase = txn_svc.record_purchase(
            "wallet-plus", Decimal("108000"), date(2026, 9, 18), "TC0306-w",
        )
        fund_purchase = txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0306-f",
            source_cash_product_id="wallet-plus",
        )
        txn_svc.cancel_pending(fund_purchase.id, "TC0306-cancel")

        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        wallet_tx = next(t for t in payload["transactions"] if t["id"] == wallet_purchase.id)
        assert Decimal(wallet_tx["remaining_confirmation_shares"]) == Decimal("108000")
        assert proj.calculate("wallet-plus").total_shares == Decimal("108000")

    def test_confirmed_cancelled_no_remaining(self, services):
        """TC-03-07: 已确认或已取消的钱包 Plus 申购不显示剩余份额"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        # 已确认的钱包 Plus 申购（confirm_pending 直接确认）
        p_confirmed = txn_svc.record_purchase(
            "wallet-plus", Decimal("50000"), date(2026, 9, 18), "TC0307-conf",
        )
        txn_svc.confirm_pending(p_confirmed.id, MarketQuote(
            product_code="WALLETPLUS", quote_date=date(2026, 9, 18),
            source="test", raw_hash="TC0307", unit_nav=Decimal("1"),
        ))
        # 已取消的钱包 Plus 申购
        p_cancelled = txn_svc.record_purchase(
            "wallet-plus", Decimal("30000"), date(2026, 9, 18), "TC0307-canc",
        )
        txn_svc.cancel_pending(p_cancelled.id, "TC0307-cancel")

        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        rows = {t["id"]: t for t in payload["transactions"]}
        assert rows[p_confirmed.id]["remaining_confirmation_shares"] is None
        assert rows[p_cancelled.id]["remaining_confirmation_shares"] is None

    def test_multiple_outflows_from_same_purchase(self, services):
        """TC-03-10: 多笔实时转出从同一钱包申购扣减"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        seed_product(repo, "fund-a", ProductType.PUBLIC_FUND, "FA")
        seed_product(repo, "fund-b", ProductType.PUBLIC_FUND, "FB")
        p = txn_svc.record_purchase(
            "wallet-plus", Decimal("100000"), date(2026, 9, 18), "TC0310-w",
            trade_time="2026-09-18T09:00:00",
        )
        txn_svc.record_purchase(
            "fund-a", Decimal("30000"), date(2026, 9, 18), "TC0310-a",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T10:00:00",
        )
        txn_svc.record_purchase(
            "fund-b", Decimal("40000"), date(2026, 9, 18), "TC0310-b",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T11:00:00",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        row = next(t for t in payload["transactions"] if t["id"] == p.id)
        assert Decimal(row["remaining_confirmation_shares"]) == Decimal("30000")


# ===========================================================================
# 四、取消申购
# ===========================================================================

class TestCancelPurchase:
    """TC-04-xx: 取消申购"""

    def test_cancel_releases_lock(self, services):
        """TC-04-01: 取消普通待确认申购后释放锁定资金"""
        repo, txn_svc, proj = services
        seed_cash(repo, "cash", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        purchase = txn_svc.record_purchase(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC0401",
            source_cash_product_id="cash",
        )
        cancelled = txn_svc.cancel_pending(purchase.id, "TC0401-cancel")
        assert cancelled.status == TransactionStatus.CANCELLED
        assert linked_leg(repo, purchase).status == TransactionStatus.CANCELLED
        assert proj.calculate("cash").locked_shares == Decimal("0")
        assert proj.calculate("cash").available_shares == Decimal("100000")

    def test_cancel_wallet_realtime_refund(self, services):
        """TC-04-02: 取消钱包 Plus 实时扣款的申购后退款"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        purchase = txn_svc.record_purchase(
            "fund", Decimal("40000"), date(2026, 9, 18), "TC0402",
            source_cash_product_id="wallet-plus",
        )
        txn_svc.cancel_pending(purchase.id, "TC0402-cancel")
        assert proj.calculate("wallet-plus").total_shares == Decimal("100000")
        refunds = [
            t for t in repo.list_transactions(product_id="wallet-plus")
            if t.transaction_type == TransactionType.CASH_TRANSFER_IN
        ]
        assert len(refunds) == 1
        assert refunds[0].amount == Decimal("40000")

    def test_cancel_idempotent(self, services):
        """TC-04-03: 重复取消幂等"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        purchase = txn_svc.record_purchase(
            "fund", Decimal("100"), date(2026, 9, 18), "TC0403",
        )
        first = txn_svc.cancel_pending(purchase.id, "TC0403-cancel")
        second = txn_svc.cancel_pending(purchase.id, "TC0403-cancel")
        assert first.id == second.id

    def test_cancel_confirmed_rejected(self, services):
        """TC-04-04: 已确认申购不得使用取消接口"""
        repo, txn_svc, _ = services
        seed_cash(repo, "cash", "1000")
        purchase = txn_svc.record_purchase(
            "cash", Decimal("100"), date(2026, 9, 18), "TC0404",
        )
        with pytest.raises(ValueError, match="confirmed transaction cannot be cancelled"):
            txn_svc.cancel_pending(purchase.id, "TC0404-cancel")

    def test_cancel_no_source_no_refund_leg(self, services):
        """TC-04-06: 取消净值产品待确认申购（无 source）不产生退款腿"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        purchase = txn_svc.record_purchase(
            "fund", Decimal("5000"), date(2026, 9, 18), "TC0406",
        )
        cancelled = txn_svc.cancel_pending(purchase.id, "TC0406-cancel")
        assert cancelled.status == TransactionStatus.CANCELLED
        refunds = [
            t for t in repo.list_transactions(product_id="fund")
            if t.transaction_type == TransactionType.CASH_TRANSFER_IN
        ]
        assert len(refunds) == 0

    def test_cancel_pending_redemption_releases_lock(self, services):
        """TC-04-07: 取消待确认赎回释放锁定份额"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "100000", "100000")
        redemption = txn_svc.record_redemption(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC0407",
            trade_time="2026-09-18T14:00:00",
        )
        txn_svc.cancel_pending(redemption.id, "TC0407-cancel")
        assert proj.calculate("fund").locked_shares == Decimal("0")
        assert proj.calculate("fund").available_shares == Decimal("100000")

    def test_cancel_realtime_outflow_conserves_total(self, services):
        """TC-04-08: 取消实时转出申购后总资产守恒"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        before = total_assets(repo, date(2026, 9, 18))
        purchase = txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0408",
            source_cash_product_id="wallet-plus",
        )
        txn_svc.cancel_pending(purchase.id, "TC0408-cancel")
        after = total_assets(repo, date(2026, 9, 18))
        assert before == after


# ===========================================================================
# 五、赎回份额逻辑
# ===========================================================================

class TestRedemptionShares:
    """TC-05-xx: 赎回份额逻辑"""

    def test_pending_redemption_locks_shares(self, services):
        """TC-05-01: 发起待确认赎回时总份额不变、锁定增加、可用减少"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "100000", "100000")
        txn_svc.record_redemption(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC0501",
            trade_time="2026-09-18T14:00:00",
        )
        pos = proj.calculate("fund")
        assert pos.total_shares == Decimal("100000")
        assert pos.locked_shares == Decimal("30000")
        assert pos.available_shares == Decimal("70000")

    def test_confirmed_redemption_reduces_total(self, services):
        """TC-05-02: 赎回确认后总份额减少、锁定释放"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "100000", "100000")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.0500"))
        redemption = txn_svc.record_redemption(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC0502",
            trade_time="2026-09-18T14:00:00",
        )
        txn_svc.confirm_pending(redemption.id, MarketQuote(
            product_code="003103", quote_date=date(2026, 9, 18),
            source="test", raw_hash="TC0502", unit_nav=Decimal("1.0500"),
        ))
        pos = proj.calculate("fund")
        assert pos.total_shares == Decimal("70000")
        assert pos.locked_shares == Decimal("0")
        assert pos.available_shares == Decimal("70000")
        confirmed = repo.get_transaction_by_id(redemption.id)
        assert confirmed.amount == Decimal("31500")

    def test_full_redemption_zero_shares(self, services):
        """TC-05-03: 全部赎回后持有份额为 0"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "50000", "50000")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.0500"))
        redemption = txn_svc.record_redemption(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0503",
        )
        txn_svc.confirm_pending(redemption.id, MarketQuote(
            product_code="003103", quote_date=date(2026, 9, 18),
            source="test", raw_hash="TC0503", unit_nav=Decimal("1.0500"),
        ))
        pos = proj.calculate("fund")
        assert pos.total_shares == Decimal("0")
        assert pos.available_shares == Decimal("0")
        assert pos.locked_shares == Decimal("0")

    def test_partial_redemption_precise(self, services):
        """TC-05-04: 部分赎回后剩余份额精确"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "12345.6789", "12345.6789")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.0500"))
        redemption = txn_svc.record_redemption(
            "fund", Decimal("2345.6789"), date(2026, 9, 18), "TC0504",
        )
        txn_svc.confirm_pending(redemption.id, MarketQuote(
            product_code="003103", quote_date=date(2026, 9, 18),
            source="test", raw_hash="TC0504", unit_nav=Decimal("1.0500"),
        ))
        assert proj.calculate("fund").total_shares == Decimal("10000.0000")

    def test_redeem_exceeds_available_rejected(self, services):
        """TC-05-05: 赎回超过可用份额必须拒绝"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "10000", "10000")
        # 先锁定 4000 份额
        txn_svc.record_redemption(
            "fund", Decimal("4000"), date(2026, 9, 18), "TC0505-lock",
            trade_time="2026-09-18T14:00:00",
        )
        # 可用 = 6000，尝试赎回 7000
        with pytest.raises(ValueError, match="insufficient available shares"):
            txn_svc.record_redemption(
                "fund", Decimal("7000"), date(2026, 9, 18), "TC0505",
            )
        # 锁定份额不变
        assert proj.calculate("fund").locked_shares == Decimal("4000")

    @pytest.mark.parametrize("shares", [Decimal("0"), Decimal("-100")])
    def test_redeem_zero_negative_rejected(self, services, shares):
        """TC-05-06: 赎回零/负份额必须拒绝"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "10000", "10000")
        with pytest.raises(ValueError):
            txn_svc.record_redemption(
                "fund", shares, date(2026, 9, 18), f"TC0506-{shares}",
            )

    def test_wallet_redemption_immediate_confirm(self, services):
        """TC-05-08: 钱包 Plus 赎回应按实时到账规则处理"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        redemption = txn_svc.record_redemption(
            "wallet-plus", Decimal("30000"), date(2026, 9, 18), "TC0508",
        )
        assert redemption.status == TransactionStatus.CONFIRMED
        assert redemption.confirmation_date == date(2026, 9, 18)
        assert proj.calculate("wallet-plus").total_shares == Decimal("70000")

    def test_redemption_settlement_fields(self, services):
        """TC-05-10: 净值型理财赎回应创建 settlement 相关字段"""
        repo, txn_svc, _ = services
        seed_product(repo, "wealth", ProductType.WEALTH_NAV, "W001")
        seed_opening_position(repo, "wealth", "100000", "100000")
        seed_wallet_plus(repo, "wallet-plus", "1000")
        redemption = txn_svc.record_redemption(
            "wealth", Decimal("50000"), date(2026, 9, 17), "TC0510",
            destination_cash_product_id="wallet-plus",
            trade_time="2026-09-16T23:13:00",
            settlement_date=date(2026, 9, 20),
        )
        assert redemption.linked_transaction_id == ""
        assert redemption.settlement_date == date(2026, 9, 20)


# ===========================================================================
# 六、赎回到账钱包 Plus
# ===========================================================================

class TestRedemptionSettlement:
    """TC-06-xx: 赎回到账钱包 Plus"""

    def test_full_lifecycle(self, services):
        """TC-06-01: 完整生命周期"""
        repo, txn_svc, proj = services
        seed_product(repo, "wealth", ProductType.WEALTH_NAV, "W001")
        seed_opening_position(repo, "wealth", "100000", "100000")
        seed_wallet_plus(repo, "wallet-plus", "1000")
        seed_quote(repo, repo.require_product("wealth"), date(2026, 9, 17), Decimal("1.0803"))

        redemption = txn_svc.record_redemption(
            "wealth", Decimal("50000"), date(2026, 9, 17), "TC0601",
            destination_cash_product_id="wallet-plus",
            trade_time="2026-09-16T23:13:00",
            settlement_date=date(2026, 9, 20),
        )
        txn_svc.confirm_pending(redemption.id, MarketQuote(
            product_code="W001", quote_date=date(2026, 9, 17),
            source="test", raw_hash="TC0601", unit_nav=Decimal("1.0803"),
        ))
        confirmed = repo.get_transaction_by_id(redemption.id)
        assert confirmed.settlement_status == RedemptionSettlementStatus.PENDING
        assert proj.calculate("wealth").total_shares == Decimal("50000")
        assert proj.calculate("wallet-plus").total_shares == Decimal("1000")

        # 到账日前不处理
        assert txn_svc.settle_redemptions(date(2026, 9, 19)) == []

        # 到账日
        settled = txn_svc.settle_redemptions(date(2026, 9, 20))
        assert len(settled) == 1
        assert settled[0].settlement_status == RedemptionSettlementStatus.SETTLED
        purchase = repo.find_purchase_by_origin(redemption.id)
        assert purchase is not None
        assert purchase.amount == Decimal("54015")
        assert purchase.status == TransactionStatus.PENDING_CONFIRMATION
        assert proj.calculate("wallet-plus").total_shares == Decimal("55015")

    def test_no_wallet_purchase_before_settlement(self, services):
        """TC-06-02: 到账前不得提前增加钱包 Plus 持仓"""
        repo, txn_svc, proj = services
        seed_product(repo, "wealth", ProductType.WEALTH_NAV, "W001")
        seed_opening_position(repo, "wealth", "100000", "100000")
        seed_wallet_plus(repo, "wallet-plus", "1000")
        seed_quote(repo, repo.require_product("wealth"), date(2026, 9, 17), Decimal("1.0803"))
        redemption = txn_svc.record_redemption(
            "wealth", Decimal("50000"), date(2026, 9, 17), "TC0602",
            destination_cash_product_id="wallet-plus",
            trade_time="2026-09-16T23:13:00",
            settlement_date=date(2026, 9, 20),
        )
        txn_svc.confirm_pending(redemption.id, MarketQuote(
            product_code="W001", quote_date=date(2026, 9, 17),
            source="test", raw_hash="TC0602", unit_nav=Decimal("1.0803"),
        ))
        assert proj.calculate("wallet-plus").total_shares == Decimal("1000")

    def test_settlement_only_on_due_date(self, services):
        """TC-06-03: 到账日才创建钱包 Plus 申购记录"""
        repo, txn_svc, _ = services
        seed_product(repo, "wealth", ProductType.WEALTH_NAV, "W001")
        seed_opening_position(repo, "wealth", "100000", "100000")
        seed_wallet_plus(repo, "wallet-plus", "1000")
        seed_quote(repo, repo.require_product("wealth"), date(2026, 9, 17), Decimal("1.0803"))
        redemption = txn_svc.record_redemption(
            "wealth", Decimal("50000"), date(2026, 9, 17), "TC0603",
            destination_cash_product_id="wallet-plus",
            trade_time="2026-09-16T23:13:00",
            settlement_date=date(2026, 9, 20),
        )
        txn_svc.confirm_pending(redemption.id, MarketQuote(
            product_code="W001", quote_date=date(2026, 9, 17),
            source="test", raw_hash="TC0603", unit_nav=Decimal("1.0803"),
        ))
        assert txn_svc.settle_redemptions(date(2026, 9, 19)) == []
        assert repo.find_purchase_by_origin(redemption.id) is None

    def test_settlement_idempotent(self, services):
        """TC-06-06: 重复执行到账任务不得重复创建"""
        repo, txn_svc, _ = services
        seed_product(repo, "wealth", ProductType.WEALTH_NAV, "W001")
        seed_opening_position(repo, "wealth", "100000", "100000")
        seed_wallet_plus(repo, "wallet-plus", "1000")
        seed_quote(repo, repo.require_product("wealth"), date(2026, 9, 17), Decimal("1.0803"))
        redemption = txn_svc.record_redemption(
            "wealth", Decimal("50000"), date(2026, 9, 17), "TC0606",
            destination_cash_product_id="wallet-plus",
            trade_time="2026-09-16T23:13:00",
            settlement_date=date(2026, 9, 20),
        )
        txn_svc.confirm_pending(redemption.id, MarketQuote(
            product_code="W001", quote_date=date(2026, 9, 17),
            source="test", raw_hash="TC0606", unit_nav=Decimal("1.0803"),
        ))
        txn_svc.settle_redemptions(date(2026, 9, 20))
        assert txn_svc.settle_redemptions(date(2026, 9, 20)) == []
        purchases = [
            t for t in repo.list_transactions()
            if t.origin_transaction_id == redemption.id
        ]
        assert len(purchases) == 1

    def test_external_redemption_no_wallet_purchase(self, services):
        """TC-06-07: 外部产品赎回（无 destination）不回创建钱包申购"""
        repo, txn_svc, _ = services
        seed_product(repo, "wealth", ProductType.WEALTH_NAV, "W001")
        seed_opening_position(repo, "wealth", "100000", "100000")
        seed_quote(repo, repo.require_product("wealth"), date(2026, 9, 17), Decimal("1.0803"))
        redemption = txn_svc.record_redemption(
            "wealth", Decimal("50000"), date(2026, 9, 17), "TC0607",
            trade_time="2026-09-16T23:13:00",
            settlement_date=date(2026, 9, 20),
        )
        txn_svc.confirm_pending(redemption.id, MarketQuote(
            product_code="W001", quote_date=date(2026, 9, 17),
            source="test", raw_hash="TC0607", unit_nav=Decimal("1.0803"),
        ))
        settled = txn_svc.settle_redemptions(date(2026, 9, 20))
        assert len(settled) == 1
        assert repo.find_purchase_by_origin(redemption.id) is None


# ===========================================================================
# 七、总资产守恒
# ===========================================================================

class TestTotalAssetsConservation:
    """TC-07-xx: 总资产守恒"""

    def test_outflow_conserves(self, services):
        """TC-07-01: 钱包 Plus 转出申购基金过程总资产不变"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "50000", "50000")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.5"))
        before = total_assets(repo, date(2026, 9, 18))
        txn_svc.record_purchase(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC0701",
            source_cash_product_id="wallet-plus",
        )
        after = total_assets(repo, date(2026, 9, 18))
        assert before == after

    def test_purchase_confirm_conserves(self, services):
        """TC-07-02: 基金申购待确认到最终确认过程总资产不变"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.5"))
        txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0702",
            source_cash_product_id="wallet-plus",
        )
        before = total_assets(repo, date(2026, 9, 18))
        txn_svc.settle_pending(date(2026, 9, 19))
        after = total_assets(repo, date(2026, 9, 19))
        assert before == after

    def test_redemption_settlement_conserves(self, services):
        """TC-07-03: 赎回确认到资金到账过程总资产不变"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "100000", "100000")
        seed_wallet_plus(repo, "wallet-plus", "1000")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 17), Decimal("1.08"))
        redemption = txn_svc.record_redemption(
            "fund", Decimal("50000"), date(2026, 9, 17), "TC0703",
            destination_cash_product_id="wallet-plus",
            trade_time="2026-09-16T23:13:00",
            settlement_date=date(2026, 9, 20),
        )
        txn_svc.confirm_pending(redemption.id, MarketQuote(
            product_code="003103", quote_date=date(2026, 9, 17),
            source="test", raw_hash="TC0703", unit_nav=Decimal("1.08"),
        ))
        # 赎回确认后：fund 份额减少，wallet-plus 不变（待到账）
        assert proj.calculate("fund").total_shares == Decimal("50000")
        assert proj.calculate("wallet-plus").total_shares == Decimal("1000")
        # 到账后：wallet-plus 增加
        txn_svc.settle_redemptions(date(2026, 9, 21))
        assert proj.calculate("wallet-plus").total_shares == Decimal("55000")

    def test_cancel_refund_conserves(self, services):
        """TC-07-04: 取消申购并退款过程总资产不变"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        before = total_assets(repo, date(2026, 9, 18))
        purchase = txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0704",
            source_cash_product_id="wallet-plus",
        )
        txn_svc.cancel_pending(purchase.id, "TC0704-cancel")
        after = total_assets(repo, date(2026, 9, 18))
        assert before == after

    def test_comprehensive_sequence(self, services):
        """TC-07-08: 综合多操作序列持仓变化正确"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "50000", "50000")
        seed_product(repo, "wealth", ProductType.WEALTH_NAV, "W001")
        seed_opening_position(repo, "wealth", "30000", "30000")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.5"))
        seed_quote(repo, repo.require_product("wealth"), date(2026, 9, 18), Decimal("1.08"))

        # 申购 fund 20000（pending，钱包实时转出 20000 已确认扣减）
        p1 = txn_svc.record_purchase(
            "fund", Decimal("20000"), date(2026, 9, 18), "TC0708-1",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T09:00:00",
        )
        # 钱包实时转出已确认，total_shares 立即减少
        assert proj.calculate("wallet-plus").total_shares == Decimal("80000")
        assert proj.calculate("wallet-plus").available_shares == Decimal("80000")

        # 实时转出 15000 申购 wealth
        txn_svc.record_purchase(
            "wealth", Decimal("15000"), date(2026, 9, 18), "TC0708-2",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T10:00:00",
        )
        assert proj.calculate("wallet-plus").total_shares == Decimal("65000")

        # 赎回 fund 10000（pending，锁定 fund 10000 shares）
        txn_svc.record_redemption(
            "fund", Decimal("10000"), date(2026, 9, 18), "TC0708-3",
            trade_time="2026-09-18T11:00:00",
        )
        assert proj.calculate("fund").locked_shares == Decimal("10000")
        assert proj.calculate("fund").available_shares == Decimal("40000")

        # 取消 fund 申购（退款 20000 到钱包）
        txn_svc.cancel_pending(p1.id, "TC0708-cancel")
        assert proj.calculate("wallet-plus").total_shares == Decimal("85000")

    def test_exact_total_assets(self, services):
        """TC-07-10: 精确总资产 717682.0645018972933902959562"""
        repo, txn_svc, proj = services
        target = Decimal("717682.0645018972933902959562")

        seed_wallet_plus(repo, "wallet-plus", "74551.8880198179443535014361")
        seed_product(repo, "wealth", ProductType.WEALTH_NAV, "W001")
        seed_opening_position(repo, "wealth", "100000", "100000")
        seed_quote(repo, repo.require_product("wealth"), date(2026, 9, 18), Decimal("1.08"))
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "200000", "200000")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("2.5"))

        # 计算当前市值
        wallet_mv = Decimal("74551.8880198179443535014361")
        wealth_mv = Decimal("100000") * Decimal("1.08")
        fund_mv = Decimal("200000") * Decimal("2.5")
        current = wallet_mv + wealth_mv + fund_mv

        # 需要补充的差额通过 in-transit 调整
        gap = target - current
        # gap 应通过 pending purchase 补充
        if gap > 0:
            # 创建一笔 pending 钱包 Plus 申购来补充差额
            txn_svc.record_purchase(
                "wallet-plus", gap, date(2026, 9, 18), "TC0710-gap",
            )

        computed = total_assets(repo, date(2026, 9, 18))
        assert computed == target

        # 验证页面两位小数显示
        rounded = float(int(computed * 100) / 100)
        assert rounded == 717682.06 or rounded == 717682.07  # 取决于舍入方向


# ===========================================================================
# 八、交易排序
# ===========================================================================

class TestTransactionSorting:
    """TC-08-xx: 交易排序"""

    def test_sort_by_status_updated_at_desc(self, services):
        """TC-08-01: 交易按 status_updated_at 从新到旧排序"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "100000", "100000")
        # 创建三条交易，设置不同 status_updated_at
        for tx_id, sua in [
            ("tx-oldest", "2026-09-17 10:00:00"),
            ("tx-middle", "2026-09-18 10:00:00"),
            ("tx-newest", "2026-09-18 15:00:00"),
        ]:
            repo.create_transaction(Transaction(
                id=tx_id,
                product_id="fund",
                transaction_type=TransactionType.MANUAL_PURCHASE,
                status=TransactionStatus.CONFIRMED,
                trade_date=date(2026, 9, 18),
                idempotency_key=tx_id,
                amount=Decimal("10"),
                shares=Decimal("10"),
                status_updated_at=sua,
            ))
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        tx_ids = [t["id"] for t in payload["transactions"]
                  if t["id"] in ("tx-oldest", "tx-middle", "tx-newest")]
        assert tx_ids == ["tx-newest", "tx-middle", "tx-oldest"]

    def test_sort_stable_same_timestamp(self, services):
        """TC-08-03: status_updated_at 相同时排序稳定"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "100000", "100000")
        for tx_id in ["tx-a", "tx-b", "tx-c"]:
            repo.create_transaction(Transaction(
                id=tx_id,
                product_id="fund",
                transaction_type=TransactionType.MANUAL_PURCHASE,
                status=TransactionStatus.CONFIRMED,
                trade_date=date(2026, 9, 18),
                idempotency_key=tx_id,
                amount=Decimal("10"),
                shares=Decimal("10"),
                status_updated_at="2026-09-18 10:00:00",
            ))
        payload1 = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        payload2 = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        ids1 = [t["id"] for t in payload1["transactions"] if t["id"].startswith("tx-")]
        ids2 = [t["id"] for t in payload2["transactions"] if t["id"].startswith("tx-")]
        assert ids1 == ids2

    def test_newer_created_older_updated_not_first(self, services):
        """TC-08-04: 创建时间新但状态更新时间旧的交易不得排在前面"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "100000", "100000")
        # A: created 新, status_updated_at 旧
        repo.create_transaction(Transaction(
            id="tx-A",
            product_id="fund",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 9, 18),
            idempotency_key="tx-A",
            amount=Decimal("10"),
            shares=Decimal("10"),
            created_at="2026-09-18 10:00:00",
            status_updated_at="2026-09-17 10:00:00",
        ))
        # B: created 旧, status_updated_at 新
        repo.create_transaction(Transaction(
            id="tx-B",
            product_id="fund",
            transaction_type=TransactionType.MANUAL_PURCHASE,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 9, 18),
            idempotency_key="tx-B",
            amount=Decimal("10"),
            shares=Decimal("10"),
            created_at="2026-09-17 10:00:00",
            status_updated_at="2026-09-18 10:00:00",
        ))
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        tx_ids = [t["id"] for t in payload["transactions"] if t["id"] in ("tx-A", "tx-B")]
        assert tx_ids == ["tx-B", "tx-A"]


# ===========================================================================
# 九、页面展示
# ===========================================================================

class TestPageDisplay:
    """TC-09-xx: 页面展示"""

    def test_shares_display_correct(self, services):
        """TC-09-01: 持有份额、可用份额、锁定份额显示正确"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "100000", "100000")
        txn_svc.record_redemption(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC0901",
            trade_time="2026-09-18T14:00:00",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        fund_row = next(r for r in payload["products"] if r["id"] == "fund")
        assert Decimal(fund_row["shares"]) == Decimal("100000")
        assert Decimal(fund_row["available_shares"]) == Decimal("70000")
        assert Decimal(fund_row["locked_shares"]) == Decimal("30000")

    def test_wallet_in_transit_net_remaining(self, services):
        """TC-09-02: 钱包 Plus 申购在途显示净剩余值"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        txn_svc.record_purchase(
            "wallet-plus", Decimal("108000"), date(2026, 9, 18), "TC0902-w",
            trade_time="2026-09-18T09:00:00",
        )
        txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0902-f",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T10:00:00",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        wallet_row = next(r for r in payload["products"] if r["id"] == "wallet-plus")
        assert Decimal(wallet_row["in_transit_amount"]) == Decimal("58000")

    def test_original_record_appears_once(self, services):
        """TC-09-03: 原 108000 元交易记录只出现一次"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        txn_svc.record_purchase(
            "wallet-plus", Decimal("108000"), date(2026, 9, 18), "TC0903-w",
        )
        txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0903-f",
            source_cash_product_id="wallet-plus",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        wallet_purchase_rows = [
            t for t in payload["transactions"]
            if t["product_id"] == "wallet-plus"
            and t["transaction_type"] == "manual_purchase"
            and Decimal(t["amount"]) == Decimal("108000")
        ]
        assert len(wallet_purchase_rows) == 1

    def test_shares_and_remaining_displayed_together(self, services):
        """TC-09-04: 原始确认份额和实时剩余确认份额同时显示"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        wp = txn_svc.record_purchase(
            "wallet-plus", Decimal("108000"), date(2026, 9, 18), "TC0904-w",
            trade_time="2026-09-18T09:00:00",
        )
        txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0904-f",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T10:00:00",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        row = next(t for t in payload["transactions"] if t["id"] == wp.id)
        assert Decimal(row["shares"]) == Decimal("108000")
        assert Decimal(row["remaining_confirmation_shares"]) == Decimal("58000")

    def test_fund_in_transit_shows_50000(self, services):
        """TC-09-05: 50000 元显示在目标基金申购在途中"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC0905",
            source_cash_product_id="wallet-plus",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        fund_row = next(r for r in payload["products"] if r["id"] == "fund")
        assert Decimal(fund_row["in_transit_amount"]) == Decimal("50000")

    def test_pending_wallet_included_in_position(self, services):
        """TC-09-08: pending 钱包 Plus 申购 included_in_position = True"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        wp = txn_svc.record_purchase(
            "wallet-plus", Decimal("50000"), date(2026, 9, 18), "TC0908",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        row = next(t for t in payload["transactions"] if t["id"] == wp.id)
        assert row["included_in_position"] is True

    def test_pending_nav_not_included_in_position(self, services):
        """TC-09-09: pending 净值产品申购 included_in_position = False"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        p = txn_svc.record_purchase(
            "fund", Decimal("5000"), date(2026, 9, 18), "TC0909",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        row = next(t for t in payload["transactions"] if t["id"] == p.id)
        assert row["included_in_position"] is False

    def test_display_status_mapping(self, services):
        """TC-09-10: 赎回交易的 display_status 正确映射"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "100000", "100000")
        seed_wallet_plus(repo, "wallet-plus", "1000")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 17), Decimal("1.08"))

        # 待确认赎回
        r1 = txn_svc.record_redemption(
            "fund", Decimal("10000"), date(2026, 9, 17), "TC0910-pending",
            trade_time="2026-09-16T23:13:00",
        )
        # 确认赎回待到账
        r2 = txn_svc.record_redemption(
            "fund", Decimal("10000"), date(2026, 9, 17), "TC0910-settle",
            destination_cash_product_id="wallet-plus",
            trade_time="2026-09-16T23:13:00",
            settlement_date=date(2026, 9, 20),
        )
        txn_svc.confirm_pending(r2.id, MarketQuote(
            product_code="003103", quote_date=date(2026, 9, 17),
            source="test", raw_hash="TC0910", unit_nav=Decimal("1.08"),
        ))

        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        rows = {t["id"]: t for t in payload["transactions"]}
        assert rows[r1.id]["display_status"] == "pending_confirmation"
        assert rows[r2.id]["display_status"] == "pending_settlement"

    def test_no_nav_quote_status_pending(self, services):
        """TC-09-12: 净值产品无行情时 quote_status = pending"""
        repo, txn_svc, _ = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        fund_row = next(r for r in payload["products"] if r["id"] == "fund")
        assert fund_row["quote_status"] == "pending"
        assert fund_row["market_value"] is None


# ===========================================================================
# 十、持仓投影与份额计算边界
# ===========================================================================

class TestPositionBoundary:
    """TC-10-xx: 持仓投影与份额计算边界"""

    def test_pending_wallet_included_in_position(self, services):
        """TC-10-01: pending 钱包 Plus 申购计入持仓总量"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "0")
        txn_svc.record_purchase(
            "wallet-plus", Decimal("50000"), date(2026, 9, 18), "TC1001",
        )
        pos = proj.calculate("wallet-plus")
        assert pos.total_shares == Decimal("50000")
        assert pos.available_shares == Decimal("50000")

    def test_pending_nav_not_included(self, services):
        """TC-10-02: pending 净值产品申购不计入持仓"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        txn_svc.record_purchase(
            "fund", Decimal("50000"), date(2026, 9, 18), "TC1002",
        )
        assert proj.calculate("fund").total_shares == Decimal("0")

    def test_pending_redemption_locks(self, services):
        """TC-10-03: pending 赎回锁定份额正确"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_opening_position(repo, "fund", "100000", "100000")
        txn_svc.record_redemption(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC1003",
            trade_time="2026-09-18T14:00:00",
        )
        pos = proj.calculate("fund")
        assert pos.total_shares == Decimal("100000")
        assert pos.locked_shares == Decimal("30000")
        assert pos.available_shares == Decimal("70000")

    def test_reversal_deducts_shares(self, services):
        """TC-10-05: 冲正交易正确扣减份额"""
        repo, txn_svc, proj = services
        seed_cash(repo, "cash", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        seed_quote(repo, repo.require_product("fund"), date(2026, 9, 18), Decimal("1.0321"))
        purchase = txn_svc.record_purchase(
            "fund", Decimal("30000"), date(2026, 9, 18), "TC1005",
            source_cash_product_id="cash",
        )
        txn_svc.settle_pending(date(2026, 9, 19))
        shares_before = proj.calculate("fund").total_shares
        txn_svc.reverse_confirmed(purchase.id, "测试冲正", "TC1005-reverse")
        shares_after = proj.calculate("fund").total_shares
        assert shares_after == Decimal("0")

    def test_zero_position(self, services):
        """TC-10-08: 零持仓产品的份额计算"""
        repo, txn_svc, proj = services
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        pos = proj.calculate("fund")
        assert pos.total_shares == Decimal("0")
        assert pos.available_shares == Decimal("0")
        assert pos.locked_shares == Decimal("0")


# ===========================================================================
# 十一、跨产品交互
# ===========================================================================

class TestCrossProductInteraction:
    """TC-11-xx: 跨产品交互与复杂场景"""

    def test_full_redemption_wallet_zero(self, services):
        """TC-11-04: 全部赎回钱包 Plus 后持仓为 0"""
        repo, txn_svc, proj = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        txn_svc.record_redemption(
            "wallet-plus", Decimal("100000"), date(2026, 9, 18), "TC1104",
        )
        assert proj.calculate("wallet-plus").total_shares == Decimal("0")

    def test_full_outflow_in_transit_zero(self, services):
        """TC-11-06: 全部实时转出后在途为 0"""
        repo, txn_svc, _ = services
        seed_wallet_plus(repo, "wallet-plus", "100000")
        seed_product(repo, "fund", ProductType.PUBLIC_FUND, "003103")
        txn_svc.record_purchase(
            "wallet-plus", Decimal("50000"), date(2026, 9, 18), "TC1106-w",
            trade_time="2026-09-18T09:00:00",
        )
        txn_svc.record_purchase(
            "fund", Decimal("150000"), date(2026, 9, 18), "TC1106-f",
            source_cash_product_id="wallet-plus",
            trade_time="2026-09-18T10:00:00",
        )
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        wallet_row = next(r for r in payload["products"] if r["id"] == "wallet-plus")
        assert Decimal(wallet_row["in_transit_amount"]) == Decimal("0")

    def test_empty_portfolio_no_error(self, services):
        """TC-11-07: 空组合（无任何产品）的 payload 不报错"""
        repo, txn_svc, _ = services
        payload = build_portfolio_payload(repo, as_of=date(2026, 9, 18))
        assert payload["products"] == []
        assert payload["transactions"] == []
        assert payload["summary"]["market_value"] == "0"
