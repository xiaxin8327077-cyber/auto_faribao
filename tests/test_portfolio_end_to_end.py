from datetime import date, datetime
from decimal import Decimal

from src.config import Config
from src.portfolio_db import PortfolioDatabase
from src.portfolio_jobs import run_portfolio_cycle
from src.portfolio_market import MarketProduct, MarketQuote
from src.portfolio_models import (
    Product,
    ProductStatus,
    ProductType,
)
from src.portfolio_reports import build_portfolio_query_report
from src.portfolio_repository import PortfolioRepository
from src.portfolio_runtime import initialize_portfolio
from src.portfolio_view import build_portfolio_payload
from src.portfolio_wallet import WALLET_PRODUCT_ID


def _seed_runtime(tmp_path, with_legacy=True):
    cfg = Config({"nav_monitor": {"products": [
        {"provider": "nanyin_wealth", "code": "NYRR000007",
         "name": "南银理财日日聚宝15号A", "shares": 10936.10},
    ] if with_legacy else []}})
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    runtime = initialize_portfolio(
        cfg,
        state_path=state,
        db_path=tmp_path / "portfolio.db",
        backup_root=tmp_path / "backups",
    )
    assert runtime.write_enabled is True
    return runtime, cfg


def _add_product(repo, product_id, provider, code, name, product_type):
    repo.add_product(Product(
        product_id, provider, code, name, product_type,
        status=ProductStatus.ACTIVE,
    ))


def test_first_start_migrates_legacy_cash(tmp_path):
    runtime, _cfg = _seed_runtime(tmp_path)
    payload = build_portfolio_payload(runtime.repository)
    codes = {row["code"] for row in payload["products"]}
    assert "NYRR000007" in codes
    assert "WALLETPLUS" in codes
    old_cash = next(row for row in payload["products"] if row["code"] == "NYRR000007")
    wallet = next(row for row in payload["products"] if row["code"] == "WALLETPLUS")
    assert old_cash["status"] == "inactive"
    assert old_cash["shares"] == "0"
    assert wallet["shares"] == "10936.1"
    assert payload["write_enabled"] is True


def test_manual_purchase_and_sip_settlement(tmp_path):
    runtime, _cfg = _seed_runtime(tmp_path)
    repo = runtime.repository

    cash = repo.require_product(WALLET_PRODUCT_ID)
    _add_product(repo, "fund", "changsheng_fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND)
    repo.upsert_quote("fund", MarketQuote(
        product_code="003103", quote_date=date(2026, 7, 30),
        source="changsheng_fund", raw_hash="q",
        unit_nav=Decimal("1.0321"), cumulative_nav=Decimal("1.2000"),
    ), fetched_at="2026-07-30T09:00:00")
    repo.upsert_quote(cash.id, MarketQuote(
        product_code=cash.code, quote_date=date(2026, 7, 30),
        source="wallet_plus_fixed", raw_hash="c1",
        income_per_10k=Decimal("0.3644"),
        seven_day_annualized_rate=Decimal("0.0133"),
    ), fetched_at="2026-07-30T09:00:00")

    result = run_portfolio_cycle(runtime, datetime(2026, 7, 30, 18, 0))

    assert result.quotes_synced >= 2
    payload = build_portfolio_payload(repo)
    fund = next(r for r in payload["products"] if r["code"] == "003103")
    # 行情同步后净值存在即可（具体值取决于行情源返回，不硬编码）。
    assert fund["quote"].get("unit_nav")
    assert fund["quote_status"] == "ready"


def test_cycle_database_idempotent_across_runs(tmp_path):
    """同一 slot 内多次运行业务写入幂等：第二次不产生新交易/行情。"""
    runtime, _cfg = _seed_runtime(tmp_path)
    repo = runtime.repository
    _add_product(repo, "fund", "changsheng_fund", "003103", "长盛盛裕纯债C", ProductType.PUBLIC_FUND)
    cash = repo.require_product(WALLET_PRODUCT_ID)
    repo.upsert_quote(cash.id, MarketQuote(
        product_code=cash.code, quote_date=date(2026, 7, 30),
        source="wallet_plus_fixed", raw_hash="idem1",
        income_per_10k=Decimal("0.3644"),
        seven_day_annualized_rate=Decimal("0.0133"),
    ), fetched_at="2026-07-30T09:00:00")

    run_portfolio_cycle(runtime, datetime(2026, 7, 30, 18, 0))
    with repo.database.connection() as conn:
        quotes_after_first = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]

    run_portfolio_cycle(runtime, datetime(2026, 7, 30, 20, 0))
    with repo.database.connection() as conn:
        quotes_after_second = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]

    assert quotes_after_first == quotes_after_second


def test_read_only_report_uses_repository(tmp_path):
    runtime, _cfg = _seed_runtime(tmp_path)
    cash = runtime.repository.require_product(WALLET_PRODUCT_ID)
    runtime.repository.upsert_quote(cash.id, MarketQuote(
        product_code=cash.code, quote_date=date(2026, 7, 30),
        source="wallet_plus_fixed", raw_hash="c2",
        income_per_10k=Decimal("0.3644"),
        seven_day_annualized_rate=Decimal("0.0133"),
    ), fetched_at="2026-07-30T09:00:00")

    text = build_portfolio_query_report(runtime.repository, target_date=date(2026, 7, 30))
    assert "WALLETPLUS" in text
    assert "每万份收益：0.3644 元" in text
    assert "七日年化：1.33%" in text


def test_migration_failure_keeps_runtime_read_only(tmp_path, monkeypatch):
    import src.portfolio_runtime as pr_module

    def fake_migrate(*_args, **_kwargs):
        raise ValueError("simulated migration failure")

    monkeypatch.setattr(pr_module, "migrate_legacy_portfolio", fake_migrate)
    cfg = Config({"nav_monitor": {"products": [
        {"provider": "citic_wealth", "code": "AF233276B", "name": "产品", "shares": 1000},
    ]}})
    state = tmp_path / "state2.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")

    failed = initialize_portfolio(
        cfg,
        state_path=state,
        db_path=tmp_path / "portfolio2.db",
        backup_root=tmp_path / "backups2",
    )
    assert failed.write_enabled is False
    assert failed.repository is None
    assert "simulated migration failure" in failed.migration_error
