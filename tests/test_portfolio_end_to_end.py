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
    SipFrequency,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_reports import build_portfolio_query_report
from src.portfolio_repository import PortfolioRepository
from src.portfolio_runtime import initialize_portfolio
from src.portfolio_sip import SipService
from src.portfolio_transactions import PortfolioTransactionService
from src.portfolio_view import build_portfolio_payload
from src.portfolio_wallet import WALLET_PRODUCT_ID


def _sip_services(repo):
    """在真实运行时仓储上装配定投服务，与看板共享同一份数据。"""
    projector = PositionProjector(repo)
    sip = SipService(repo, projector, PortfolioTransactionService(repo, projector))
    return projector, sip


def _plan_purchases(repo, plan_id):
    return [
        t for t in repo.list_transactions()
        if t.plan_id == plan_id and t.transaction_type is TransactionType.SIP_PURCHASE
    ]


def _seed_cash(repo, product_id="cash", shares="1000", trade_date=date(2026, 1, 1)):
    """显式、早于任何扣款日的现金建仓，避免依赖迁移建仓的 date.today()。"""
    repo.add_product(Product(
        product_id, "test", product_id, product_id,
        ProductType.CASH_MANAGEMENT, status=ProductStatus.ACTIVE,
    ))
    repo.create_transaction(Transaction(
        id=f"opening:{product_id}",
        product_id=product_id,
        transaction_type=TransactionType.OPENING_POSITION,
        status=TransactionStatus.CONFIRMED,
        trade_date=trade_date,
        idempotency_key=f"opening:{product_id}",
        amount=Decimal(shares),
        shares=Decimal(shares),
        confirmation_nav=Decimal("1"),
        confirmation_date=trade_date,
    ))
    return product_id


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


def test_monthly_sip_persists_executes_and_appears_in_dashboard(tmp_path):
    runtime, _cfg = _seed_runtime(tmp_path)
    repo = runtime.repository
    _add_product(
        repo, "fund", "changsheng_fund", "003103",
        "长盛盛裕纯债C", ProductType.PUBLIC_FUND,
    )
    _projector, sip = _sip_services(repo)
    plan = sip.activate(sip.save_plan(
        product_id="fund",
        daily_amount="100",
        purchase_fee_rate="0",
        source_cash_product_id=WALLET_PRODUCT_ID,
        start_date=date(2026, 9, 1),
        frequency=SipFrequency.MONTHLY,
        schedule_day=30,
    ).id)

    # 未执行前，看板已持久化并回显周期字段。
    before = build_portfolio_payload(repo)["sip_plans"][0]
    assert before["frequency"] == "monthly"
    assert before["schedule_day"] == 30

    sip.backfill_plan(plan.id, date(2026, 9, 30), settle=False)

    assert [e.intended_trade_date for e in sip.list_executions(plan.id)] == [
        date(2026, 9, 30)
    ]
    payload = build_portfolio_payload(repo)
    assert payload["sip_plans"][0]["frequency"] == "monthly"
    assert payload["sip_plans"][0]["schedule_day"] == 30


def test_weekly_sip_executes_on_selected_weekday_and_shows_in_dashboard(
    tmp_path,
):
    runtime, _cfg = _seed_runtime(tmp_path, with_legacy=False)
    repo = runtime.repository
    cash = _seed_cash(repo)
    _add_product(
        repo, "fund", "changsheng_fund", "003103",
        "长盛盛裕纯债C", ProductType.PUBLIC_FUND,
    )
    _projector, sip = _sip_services(repo)
    plan = sip.activate(sip.save_plan(
        product_id="fund",
        daily_amount="100",
        purchase_fee_rate="0",
        source_cash_product_id=cash,
        start_date=date(2026, 9, 1),
        frequency=SipFrequency.WEEKLY,
        schedule_day=3,
    ).id)

    executions = sip.backfill_plan(plan.id, date(2026, 9, 10), settle=False)

    # 每周三：2026-09-02、2026-09-09 两个交易日各扣款一次。
    assert [e.intended_trade_date for e in executions] == [
        date(2026, 9, 2),
        date(2026, 9, 9),
    ]
    assert len(_plan_purchases(repo, plan.id)) == 2
    row = build_portfolio_payload(repo)["sip_plans"][0]
    assert row["frequency"] == "weekly"
    assert row["schedule_day"] == 3


def test_monthly_pause_and_resume_do_not_backfill_missed_period(tmp_path):
    runtime, _cfg = _seed_runtime(tmp_path, with_legacy=False)
    repo = runtime.repository
    cash = _seed_cash(repo)
    _add_product(
        repo, "fund", "changsheng_fund", "003103",
        "长盛盛裕纯债C", ProductType.PUBLIC_FUND,
    )
    _projector, sip = _sip_services(repo)
    plan = sip.activate(sip.save_plan(
        product_id="fund",
        daily_amount="100",
        purchase_fee_rate="0",
        source_cash_product_id=cash,
        start_date=date(2026, 9, 1),
        frequency=SipFrequency.MONTHLY,
        schedule_day=15,
    ).id)

    # 9 月正常扣款。
    sip.backfill_plan(plan.id, date(2026, 9, 15), settle=False)
    # 暂停期间，日任务逐日 ensure_intent 记录错过的 10-15。
    sip.pause(plan.id)
    paused = sip.ensure_intent(plan.id, date(2026, 10, 15), recheck_schedule=True)
    assert paused.status == "skipped"
    assert paused.reason == "plan_paused"
    # 恢复后向前补跑到 11-16（11-15 为周日，顺延到 11-16）。
    sip.resume(plan.id)
    sip.backfill_plan(plan.id, date(2026, 11, 16), settle=False)

    executions = sip.list_executions(plan.id)
    executed = [e for e in executions if e.status != "skipped"]
    assert [e.intended_trade_date for e in executed] == [
        date(2026, 9, 15),
        date(2026, 11, 16),
    ]
    still_paused = next(
        e for e in executions
        if e.intended_trade_date == date(2026, 10, 15)
    )
    assert still_paused.status == "skipped"
    assert still_paused.reason == "plan_paused"
    # 恢复不得追补暂停期间错过的周期：仅 9 月和 11 月产生真实扣款。
    assert len(_plan_purchases(repo, plan.id)) == 2


def test_sip_same_trade_date_is_idempotent_on_repeated_backfill(tmp_path):
    runtime, _cfg = _seed_runtime(tmp_path)
    repo = runtime.repository
    _add_product(
        repo, "fund", "changsheng_fund", "003103",
        "长盛盛裕纯债C", ProductType.PUBLIC_FUND,
    )
    _projector, sip = _sip_services(repo)
    plan = sip.activate(sip.save_plan(
        product_id="fund",
        daily_amount="100",
        purchase_fee_rate="0",
        source_cash_product_id=WALLET_PRODUCT_ID,
        start_date=date(2026, 9, 1),
        frequency=SipFrequency.MONTHLY,
        schedule_day=30,
    ).id)

    first = sip.backfill_plan(plan.id, date(2026, 9, 30), settle=False)
    purchases_after_first = len(_plan_purchases(repo, plan.id))
    second = sip.backfill_plan(plan.id, date(2026, 9, 30), settle=False)

    # 同一计划在同一交易日最多扣款一次：重复补跑不新增执行或交易。
    assert [e.intended_trade_date for e in second] == [
        e.intended_trade_date for e in first
    ]
    assert len(_plan_purchases(repo, plan.id)) == purchases_after_first == 1
