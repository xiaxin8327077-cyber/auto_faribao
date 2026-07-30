# Portfolio Ledger Phase 4: Web Management, WeChat Read-Only, and Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SQLite the live portfolio source, expose secure mobile web management for products/trades/SIP, update reports for all product types, and reduce Enterprise WeChat to read-only queries and scheduled pushes.

**Architecture:** `PortfolioRuntime` controls migration state and write availability without changing existing `Config` scheduling fields. A dedicated Flask blueprint exposes typed portfolio endpoints protected by the existing private token, JSON-only writes, idempotency, rate limiting, validation, and audit. The current single-file `/nav` page becomes a four-tab mobile app, while report/query services read the same repository.

**Tech Stack:** Flask 3, vanilla HTML/CSS/JavaScript, Phase 1–3 portfolio services, existing private dashboard token, pytest Flask client, Playwright/manual browser QA.

## Global Constraints

- Prerequisites: Phase 1, Phase 2, and Phase 3 completion gates are green.
- Production reads switch to `data/portfolio.db` only after successful migration validation and backup.
- Migration failure preserves legacy inputs and disables portfolio writes; never auto-reset a damaged database.
- Keep `config.yaml` for schedules, provider connection settings, Enterprise WeChat, host, and port only.
- Product list, holdings, trades, quotes, and SIP plans come from SQLite after cutover.
- The existing `X-Nav-Dashboard-Key` token authorizes both reads and writes; no second password.
- Every write requires JSON, `X-Portfolio-Request: 1`, `Idempotency-Key`, validation, audit, and confirmation preview.
- Rate limit writes to 30 requests per token/source-address per rolling minute.
- Product addition succeeds only after the official adapter resolves identity, type, and a valid quote.
- Enterprise WeChat retains scheduled pushes and read-only queries only; no product, share, profit override, schedule, profile refresh, trade, or SIP writes.
- 企业微信仅保留收益推送与只读查询，任何组合写入都必须在网页完成。
- Mobile acceptance widths are exactly 390px and 430px.
- Do not stage `config.yaml`, `data/portfolio.db`, backups, tokens, JSON state, logs, screenshots, or unrelated changes.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/portfolio_runtime.py` | Startup migration, runtime repository/service wiring, read-only failure state |
| `src/portfolio_view.py` | Unified dashboard payload and type-aware profit summaries |
| `src/portfolio_reports.py` | Type-aware Enterprise WeChat query/push formatting |
| `src/portfolio_api.py` | Authenticated Flask portfolio blueprint, validation, idempotency, rate limiting |
| `src/main.py` | Initialize portfolio runtime before scheduler and server |
| `src/server.py` | Register blueprint and remove Enterprise WeChat write handlers/help |
| `src/nav_dashboard.py` | Delegate live payload to `portfolio_view`; retain legacy fallback only for migration failure |
| `src/nav_dashboard_page.html` | Four-tab mobile management UI |
| `src/nav_monitor.py` | Repository-backed product/query/report inputs; remove write commands |
| `src/nav_holdings.py` | Read active product catalog from the shared repository |
| `src/ai_diagnostic_tools.py` | Report repository product counts after cutover |
| `src/ai_command_router.py` | Remove NAV write commands from allowed canonical commands |
| `src/scheduler.py` | Use initialized runtime for portfolio jobs and pushes |
| `tests/test_portfolio_runtime.py` | Cutover, backup, failure read-only state |
| `tests/test_portfolio_view.py` | Three product types and historical profit preservation |
| `tests/test_portfolio_api.py` | Endpoints, auth, validation, idempotency, rate limit, audit |
| `tests/test_nav_dashboard_page.py` | Four-tab structure and client behavior contract |
| `tests/test_nav_dashboard_routes.py` | Blueprint and legacy-fallback routes |
| `tests/test_nav_monitor_commands.py` | Enterprise WeChat write-command removal |
| `tests/test_nav_holdings.py` | Shared repository catalog in holdings estimates |
| `tests/test_ai_diagnostic_tools.py` | Repository-backed product count diagnostics |
| `tests/test_ai_command_router.py` | AI cannot route removed writes |
| `tests/test_server_command_normalization.py` | Help and handler boundaries |
| `tests/test_portfolio_reports.py` | Cash, fund, and NAV read-only report formatting |

### Task 1: Initialize runtime and perform guarded cutover

**Files:**
- Create: `src/portfolio_runtime.py`
- Create: `tests/test_portfolio_runtime.py`
- Modify: `src/main.py:1-88`
- Modify: `src/scheduler.py:14-59`
- Modify: `src/server.py:1012-1047`

**Interfaces:**
- Produces: `PortfolioRuntime(database, repository, write_enabled, migration_error="")`.
- Produces: `initialize_portfolio(cfg, state_path=DEFAULT_STATE_PATH, db_path=DEFAULT_DB_PATH, backup_root=...) -> PortfolioRuntime`.
- Produces: `get_portfolio_runtime() -> PortfolioRuntime | None`.
- `create_app(..., portfolio_runtime=None)` uses the explicit runtime or initialized global; tests can pass a temporary runtime.

- [ ] **Step 1: Write failing cutover and failure-state tests**

```python
# tests/test_portfolio_runtime.py
def test_first_start_migrates_then_exposes_repository(tmp_path):
    cfg = Config({"nav_monitor": {"products": [
        {"provider": "citic_wealth", "code": "AF233276B", "name": "产品", "shares": 1000},
    ]}})
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")

    runtime = initialize_portfolio(
        cfg,
        state_path=state,
        db_path=tmp_path / "portfolio.db",
        backup_root=tmp_path / "backups",
    )

    assert runtime.write_enabled is True
    assert len(runtime.repository.list_products()) == 1
    assert runtime.repository.get_position(
        runtime.repository.list_products()[0].id
    ).total_shares == Decimal("1000")


def test_migration_failure_returns_read_only_runtime_and_preserves_legacy(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text('{"profit_entries":[]}', encoding="utf-8")
    monkeypatch.setattr(
        "src.portfolio_runtime.migrate_legacy_portfolio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad migration")),
    )
    runtime = initialize_portfolio(
        Config({"nav_monitor": {"products": []}}),
        state_path=state,
        db_path=tmp_path / "portfolio.db",
        backup_root=tmp_path / "backups",
    )
    assert runtime.write_enabled is False
    assert runtime.migration_error == "bad migration"
    assert state.exists()
    assert not (tmp_path / "portfolio.db").exists()


def test_incompatible_existing_schema_disables_writes_without_reset(tmp_path):
    db_path = tmp_path / "portfolio.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) "
            "VALUES (99, CURRENT_TIMESTAMP)"
        )
    original = db_path.read_bytes()
    runtime = initialize_portfolio(
        Config({"nav_monitor": {"products": []}}),
        db_path=db_path,
        backup_root=tmp_path / "backups",
    )
    assert runtime.write_enabled is False
    assert "schema version 99" in runtime.migration_error
    assert db_path.read_bytes() == original
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_portfolio_runtime.py -q`

Expected: FAIL because `src.portfolio_runtime` is missing.

- [ ] **Step 3: Implement runtime initialization**

Use:

```python
@dataclass(frozen=True)
class PortfolioRuntime:
    database: Optional[PortfolioDatabase]
    repository: Optional[PortfolioRepository]
    write_enabled: bool
    migration_error: str = ""


_runtime = None


def initialize_portfolio(cfg, state_path=DEFAULT_STATE_PATH,
                         db_path=DEFAULT_DB_PATH, backup_root=None):
    global _runtime
    backup_root = Path(backup_root or Path(db_path).parent / "portfolio-backups")
    try:
        if not Path(db_path).exists():
            migrate_legacy_portfolio(
                cfg, state_path, db_path, backup_root, install=True
            )
        database = PortfolioDatabase(db_path)
        validate_existing_database(database, expected_version=SCHEMA_VERSION)
        repository = PortfolioRepository(database)
        PositionProjector(repository).rebuild()
        _runtime = PortfolioRuntime(database, repository, True)
    except Exception as exc:
        logger.exception("Portfolio initialization failed; writes disabled")
        _runtime = PortfolioRuntime(None, None, False, str(exc)[:240])
    return _runtime
```

`validate_existing_database` runs `PRAGMA integrity_check`, requires result `ok`, reads the maximum `schema_migrations.version`, and requires it to equal `SCHEMA_VERSION`. It must not call `initialize()`, create tables, delete the file, or change its bytes when validation fails. Version upgrades are implemented later as explicit forward migrations, never as an automatic reset.

Call `initialize_portfolio(cfg)` in `src/main.py` before `start_scheduler(cfg)`. Pass the runtime to `start_scheduler` and `create_app`; retain optional defaults so existing tests do not create a production database.

- [ ] **Step 4: Run runtime and startup regressions**

Run: `pytest tests/test_portfolio_runtime.py tests/test_scheduler_cookie_recovery.py tests/test_nav_dashboard_routes.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit runtime cutover**

```bash
git add src/portfolio_runtime.py src/main.py src/scheduler.py src/server.py tests/test_portfolio_runtime.py
git commit -m "feat: initialize guarded portfolio runtime"
```

### Task 2: Build the unified live dashboard payload

**Files:**
- Create: `src/portfolio_view.py`
- Create: `tests/test_portfolio_view.py`
- Modify: `src/nav_dashboard.py:29-654`
- Modify: `tests/test_nav_dashboard.py`

**Interfaces:**
- Produces: `build_portfolio_payload(repository, as_of=None) -> dict`.
- Payload top level: `summary`, `products`, `transactions`, `sip_plans`, `profit_history`, `write_enabled`.
- Each product includes a type-specific `quote` object and common position/value/profit fields.

- [ ] **Step 1: Write failing three-type payload tests**

```python
# tests/test_portfolio_view.py
def test_payload_keeps_type_specific_quote_semantics(portfolio_fixture):
    repo = portfolio_fixture
    payload = build_portfolio_payload(repo, as_of=date(2026, 7, 30))
    rows = {row["code"]: row for row in payload["products"]}

    assert rows["AF233276B"]["quote"]["unit_nav"] == "1.078"
    assert "income_per_10k" not in rows["AF233276B"]["quote"]
    assert rows["NYRR000007"]["quote"]["income_per_10k"] == "0.4475"
    assert rows["NYRR000007"]["quote"]["seven_day_annualized_rate"] == "0.016315"
    assert "unit_nav" not in rows["NYRR000007"]["quote"]
    assert rows["003103"]["quote"]["unit_nav"] == "1.0321"


def test_payload_preserves_legacy_profit_rows_without_recalculation(portfolio_fixture):
    payload = build_portfolio_payload(portfolio_fixture)
    assert payload["profit_history"][0] == {
        "date": "2026-07-28",
        "amount": "9.87",
        "source": "legacy_manual",
    }
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_portfolio_view.py -q`

Expected: FAIL because `src.portfolio_view` is missing.

- [ ] **Step 3: Implement repository-backed payload**

For each product:

- Read the `positions` projection.
- Read the latest normalized quote.
- Cash-management market value is `total_shares`; daily profit is that date's `income_accrual`.
- NAV/fund market value is `total_shares × unit_nav`.
- Do not fabricate a value when the required quote is absent; return `market_value: null` and `quote_status: "pending"`.
- Merge immutable `legacy_profit_history` with post-cutover ledger profit rows by date; never recalculate legacy values.
- Serialize all decimals with `decimal_text`.

Modify `get_dashboard_payload(cfg)` so an initialized runtime delegates to `build_portfolio_payload`. When runtime is read-only because migration failed, keep the existing JSON payload and add:

```json
{
  "write_enabled": false,
  "write_disabled_reason": "portfolio_migration_failed"
}
```

- [ ] **Step 4: Run view and legacy dashboard tests**

Run: `pytest tests/test_portfolio_view.py tests/test_nav_dashboard.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the payload**

```bash
git add src/portfolio_view.py src/nav_dashboard.py tests/test_portfolio_view.py tests/test_nav_dashboard.py
git commit -m "feat: serve portfolio ledger dashboard payload"
```

### Task 3: Add secure portfolio read/write APIs

**Files:**
- Create: `src/portfolio_api.py`
- Create: `tests/test_portfolio_api.py`
- Modify: `src/server.py:1012-1102`
- Modify: `tests/test_nav_dashboard_routes.py`

**Interfaces:**
- Produces: `create_portfolio_blueprint(runtime, provider_factory) -> Blueprint`.
- Read routes:
  - `GET /api/portfolio`
  - `GET /api/portfolio/products`
  - `GET /api/portfolio/transactions`
  - `GET /api/portfolio/sip-plans`
- Preview routes:
  - `POST /api/portfolio/products/preview`
  - `POST /api/portfolio/transactions/preview`
- Write routes:
  - `POST /api/portfolio/products`
  - `POST /api/portfolio/transactions`
  - `POST /api/portfolio/transactions/<id>/cancel`
  - `POST /api/portfolio/transactions/<id>/reverse`
  - `POST /api/portfolio/positions/<product_id>/adjust`
  - `POST /api/portfolio/sip-plans`
  - `POST /api/portfolio/sip-plans/<id>/pause`
  - `POST /api/portfolio/sip-plans/<id>/resume`

- [ ] **Step 1: Write failing auth, idempotency, validation, and rate-limit tests**

```python
# tests/test_portfolio_api.py
def write_headers(key="secret", idem="request-1"):
    return {
        "X-Nav-Dashboard-Key": key,
        "X-Portfolio-Request": "1",
        "Idempotency-Key": idem,
        "Content-Type": "application/json",
    }


def test_write_requires_token_json_custom_header_and_idempotency(client):
    body = {"kind": "purchase", "product_id": "p1", "amount": "100"}
    assert client.post("/api/portfolio/transactions", json=body).status_code == 403
    assert client.post(
        "/api/portfolio/transactions",
        headers={"X-Nav-Dashboard-Key": "secret"},
        json=body,
    ).status_code == 400
    assert client.post(
        "/api/portfolio/transactions",
        headers={
            "X-Nav-Dashboard-Key": "secret",
            "X-Portfolio-Request": "1",
        },
        json=body,
    ).status_code == 400


def test_duplicate_write_returns_same_transaction(client):
    body = {
        "kind": "purchase", "product_id": "cash",
        "amount": "100", "trade_date": "2026-07-30",
    }
    first = client.post(
        "/api/portfolio/transactions", headers=write_headers(), json=body
    )
    second = client.post(
        "/api/portfolio/transactions", headers=write_headers(), json=body
    )
    assert first.status_code == second.status_code == 201
    assert first.get_json()["transaction"]["id"] == second.get_json()["transaction"]["id"]


def test_write_rate_limit_is_30_per_minute(client):
    for index in range(30):
        response = client.post(
            "/api/portfolio/transactions/preview",
            headers=write_headers(idem=f"preview-{index}"),
            json={"kind": "purchase", "product_id": "cash", "amount": "1"},
        )
        assert response.status_code == 200
    assert client.post(
        "/api/portfolio/transactions/preview",
        headers=write_headers(idem="preview-31"),
        json={"kind": "purchase", "product_id": "cash", "amount": "1"},
    ).status_code == 429


def test_rotating_private_token_immediately_invalidates_old_token(client, token_file):
    assert client.get(
        "/api/portfolio", headers={"X-Nav-Dashboard-Key": "secret"}
    ).status_code == 200
    token_file.write_text("rotated-secret", encoding="utf-8")
    assert client.get(
        "/api/portfolio", headers={"X-Nav-Dashboard-Key": "secret"}
    ).status_code == 403
    assert client.get(
        "/api/portfolio", headers={"X-Nav-Dashboard-Key": "rotated-secret"}
    ).status_code == 200
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_portfolio_api.py -q`

Expected: FAIL because the routes are missing.

- [ ] **Step 3: Implement blueprint guards and endpoint mapping**

Apply read guard:

```python
if not request_is_authorized(request):
    return jsonify({"error": "private_link_invalid"}), 403
```

Apply write guards in this order:

1. token;
2. `runtime.write_enabled`;
3. `request.is_json`;
4. `X-Portfolio-Request == "1"`;
5. non-empty `Idempotency-Key` no longer than 128 characters;
6. 30-per-minute limiter keyed by `(token_hash, request.remote_addr)`;
7. service validation.

`products/preview` calls `provider.resolve_product(code)` and fetches at least one valid quote. `products` repeats server-side resolution, rejects identity drift, inserts the product, syncs the quote, and audits.

`transactions/preview` returns exact normalized input, source/destination impact, fee, status prediction, and warning text. The create endpoint recomputes the preview and calls Phase 3 services; it never trusts a client-calculated share or NAV.

Every successful or rejected business write appends an audit row without recording the private token.
Continue reading the token file on each request, as the existing `get_access_token()` does; do not cache the token in the blueprint, so file rotation invalidates the old value immediately.

- [ ] **Step 4: Run API and existing route tests**

Run: `pytest tests/test_portfolio_api.py tests/test_nav_dashboard_routes.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit APIs**

```bash
git add src/portfolio_api.py src/server.py tests/test_portfolio_api.py tests/test_nav_dashboard_routes.py
git commit -m "feat: add secure portfolio management api"
```

### Task 4: Convert `/nav` into the four-tab mobile management page

**Files:**
- Modify: `src/nav_dashboard_page.html`
- Modify: `tests/test_nav_dashboard_page.py`
- Modify: `tests/test_nav_dashboard_routes.py`

**Interfaces:**
- Tabs and IDs:
  - `overviewTab` / `overviewPanel`
  - `positionsTab` / `positionsPanel`
  - `transactionsTab` / `transactionsPanel`
  - `sipTab` / `sipPanel`
- Quick action: `quickTradeButton`.
- Dialogs: `productDialog`, `tradeDialog`, `confirmDialog`, `sipDialog`, `adjustmentDialog`.
- Client sends the existing fragment token in `X-Nav-Dashboard-Key`.

- [ ] **Step 1: Write failing page contract tests**

```python
def test_page_has_four_management_tabs_and_quick_trade():
    html = PAGE_PATH.read_text(encoding="utf-8")
    for element_id in (
        "overviewTab", "positionsTab", "transactionsTab", "sipTab",
        "overviewPanel", "positionsPanel", "transactionsPanel", "sipPanel",
        "quickTradeButton", "productDialog", "tradeDialog",
        "confirmDialog", "sipDialog", "adjustmentDialog",
    ):
        assert f'id="{element_id}"' in html


def test_write_fetch_uses_private_token_idempotency_and_custom_header():
    html = PAGE_PATH.read_text(encoding="utf-8")
    assert '"X-Nav-Dashboard-Key": token' in html
    assert '"X-Portfolio-Request": "1"' in html
    assert '"Idempotency-Key": idempotencyKey' in html
    assert "/api/portfolio/transactions/preview" in html
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_nav_dashboard_page.py tests/test_nav_dashboard_routes.py -q`

Expected: FAIL because the four-tab IDs and write client do not exist.

- [ ] **Step 3: Implement the approved mobile layout**

Keep the page dependency-free and use one state object:

```javascript
const state = {
  data: null,
  activeTab: "overview",
  pendingAction: null,
  writeEnabled: false,
};
```

Required behavior:

- 总览: total market value, latest profit, cumulative profit, type summaries, pending status, `+交易`.
- 持仓: group by `wealth_nav`, `cash_management`, `public_fund`; show unit NAV only where applicable; show per-10k and seven-day yield for cash products; add/disable/view/calibrate controls.
- 交易: filter pending/confirmed/cancelled/reversed, create purchase/redemption, cancel pending, reverse confirmed.
- 定投: show amount, fee, source, state, last execution, skip reason; edit draft, pause, resume.
- Product and trade forms first call a preview endpoint, render the server result in `confirmDialog`, then submit using one generated idempotency key.
- Timeout recovery repeats the same key and displays the returned existing result.
- When `write_enabled` is false, hide or disable every write control and show the server reason.
- Escape every server-supplied string before inserting HTML.

- [ ] **Step 4: Run page and route tests**

Run: `pytest tests/test_nav_dashboard_page.py tests/test_nav_dashboard_routes.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit the mobile UI**

```bash
git add src/nav_dashboard_page.html tests/test_nav_dashboard_page.py tests/test_nav_dashboard_routes.py
git commit -m "feat: add mobile portfolio management tabs"
```

### Task 5: Make reports and Enterprise WeChat read-only

**Files:**
- Create: `src/portfolio_reports.py`
- Create: `tests/test_portfolio_reports.py`
- Modify: `src/nav_monitor.py:178-588`
- Modify: `src/nav_monitor.py:613-966`
- Modify: `src/nav_monitor.py:1131-1534`
- Modify: `src/nav_holdings.py:258-307`
- Modify: `src/nav_holdings.py:582-679`
- Modify: `src/ai_diagnostic_tools.py:90-108`
- Modify: `src/server.py:680-860`
- Modify: `src/server.py:2320-2580`
- Modify: `src/ai_command_router.py:60-216`
- Modify: `tests/test_nav_monitor_commands.py`
- Modify: `tests/test_ai_command_router.py`
- Modify: `tests/test_server_command_normalization.py`

**Interfaces:**
- Produces: `build_portfolio_query_report(repository, target_date=None, period="") -> str`.
- Produces: `format_portfolio_config(repository) -> str`.
- Existing push entry points delegate to repository-backed reporting when runtime is available.
- `nav_holdings` and diagnostic product counts consume the same active repository catalog.

- [ ] **Step 1: Write failing type-aware report and write-removal tests**

```python
# tests/test_portfolio_reports.py
def test_cash_report_displays_per_10k_and_seven_day_rate(report_repo):
    text = build_portfolio_query_report(report_repo, target_date=date(2026, 7, 30))
    assert "每万份收益：0.4475 元" in text
    assert "七日年化：1.6315%" in text
    assert "净值：1.000000" not in text


def test_fund_report_uses_real_position_and_unit_nav(report_repo):
    text = build_portfolio_query_report(report_repo, target_date=date(2026, 7, 29))
    assert "003103" in text
    assert "单位净值：1.0321" in text
```

Add command tests:

```python
@pytest.mark.parametrize("text", [
    "添加净值产品 信银 AF233276B",
    "删除净值产品 AF233276B",
    "设置净值份额 AF233276B 10000",
    "设置收益 2026-07-21 150",
    "开启净值监控",
    "关闭净值监控",
    "设置净值推送时间 08:00",
    "设置收益预估时间 17:30",
    "更新持仓画像",
])
def test_wechat_nav_writes_are_not_commands(text):
    assert parse_nav_command(text) is None
    assert classify_canonical_command(text) is None
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_portfolio_reports.py tests/test_nav_monitor_commands.py tests/test_ai_command_router.py tests/test_server_command_normalization.py -q`

Expected: FAIL because cash reports are missing and current write commands still parse.

- [ ] **Step 3: Implement reports and remove every WeChat write path**

Reporting rules:

- `wealth_nav` and `public_fund`: show exact/latest unit NAV, shares, market value, and ledger-derived profit.
- `cash_management`: show effective amount, per-10k income, seven-day annualized yield, latest income, and cumulative ledger income.
- Missing target-date quote says “待披露”; do not substitute an older quote for transaction confirmation.
- Existing scheduled report functions keep their names and call the new report builder.
- Replace every `cfg.nav_monitor.products` consumer in `nav_monitor.py`, `nav_holdings.py`, `nav_dashboard.py`, `server.py`, and `ai_diagnostic_tools.py` with the shared repository catalog after successful cutover. `config.yaml` products remain migration input only.

Write removal:

- Remove add/confirm/cancel/delete product, set shares, batch shares, manual profit, enable/disable, schedule changes, and update-holdings actions from `parse_nav_command`.
- Remove those actions from `_WRITE_NAV_ACTIONS`, `_router_system_prompt`, help text, `_is_existing_command`, and `_handle_nav_command`.
- Keep immediate/date/period NAV queries, holdings/profile viewing, configuration viewing, and scheduled pushes.
- If an old explicit write phrase reaches the callback, reply once: `请在理财看板网页中完成该操作。` without mutating state.

- [ ] **Step 4: Run report and command tests**

Run: `pytest tests/test_portfolio_reports.py tests/test_nav_monitor_commands.py tests/test_ai_command_router.py tests/test_server_command_normalization.py tests/test_nav_monitor.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit WeChat boundary**

```bash
git add src/portfolio_reports.py src/nav_monitor.py src/nav_holdings.py src/ai_diagnostic_tools.py src/server.py src/ai_command_router.py tests/test_portfolio_reports.py tests/test_nav_holdings.py tests/test_nav_monitor_commands.py tests/test_ai_command_router.py tests/test_ai_diagnostic_tools.py tests/test_server_command_normalization.py
git commit -m "refactor: keep portfolio writes on web only"
```

### Task 6: End-to-end migration, mobile QA, and release checks

**Files:**
- Create: `tests/test_portfolio_end_to_end.py`
- Modify: `design-qa.md`
- Modify: `docs/superpowers/specs/2026-07-30-portfolio-ledger-cash-management-fund-sip-design.md` only if implementation reveals a factual discrepancy; document the reason in the commit.

**Interfaces:**
- End-to-end test exercises migration → product add → quote sync → manual trade → SIP → dashboard/report.

- [ ] **Step 1: Write the end-to-end acceptance test**

```python
# tests/test_portfolio_end_to_end.py
def test_portfolio_cutover_and_first_trading_day(tmp_path, fake_providers):
    runtime = migrated_runtime_with_legacy_cash(tmp_path, fake_providers)
    add_supported_product(runtime, "changsheng_fund", "003103")
    plan = create_active_plan(
        runtime, code="003103", amount="100", fee_rate="0",
        source_code="NYRR000007",
    )
    fake_providers.publish_fund_nav("003103", "2026-07-30", "1.0321")
    run_portfolio_cycle(runtime, datetime(2026, 7, 30, 20, 0))

    payload = build_portfolio_payload(runtime.repository)
    fund = next(row for row in payload["products"] if row["code"] == "003103")
    cash = next(row for row in payload["products"] if row["code"] == "NYRR000007")
    assert fund["position"]["total_shares"] == decimal_text(
        Decimal("100") / Decimal("1.0321")
    )
    assert cash["position"]["total_shares"] == "10836.1"
    assert runtime.repository.get_plan_execution(
        plan.id, date(2026, 7, 30)
    ).status == "confirmed"
```

Add a second scenario for `015736`, amount `2000`, fee `0.00006`, insufficient balance skip, pause, and resume.

- [ ] **Step 2: Run the end-to-end test**

Run: `pytest tests/test_portfolio_end_to_end.py -q`

Expected: PASS.

- [ ] **Step 3: Run complete automated verification**

Run:

```bash
pytest -q
git diff --check
```

Expected: all tests PASS and `git diff --check` emits no errors.

- [ ] **Step 4: Perform mobile browser QA**

Start the service against a copied production dataset, not live production files:

```bash
python main.py --config config.qa.yaml
```

At both 390×844 and 430×932:

- Open `/nav#<qa-token>`.
- Verify all four tabs and `+交易`.
- Add one supported product through preview and confirmation.
- Record one purchase and one redemption.
- Cancel one pending transaction and reverse one confirmed transaction.
- Save a draft SIP without source and verify activation is blocked.
- Set a source, activate, pause, and resume.
- Verify no horizontal overflow: `document.documentElement.scrollWidth <= window.innerWidth`.
- Verify private token never appears in the query string or network response body.
- Append screenshots, viewport sizes, actions, and results to `design-qa.md`; do not commit screenshot binaries.

- [ ] **Step 5: Rehearse production migration and commit acceptance coverage**

On a copy of current `config.yaml` and `data/nav_dashboard_state.json`:

- Run migration dry-run and record product/opening/profit counts.
- Install into a temporary target and compare each product's exact `Decimal` shares.
- Compare pre/post estimated total market value using the same quote date.
- Restore the backup into a clean temporary directory and rebuild positions.
- Confirm duplicate migration does not add opening rows.

Then:

```bash
git add tests/test_portfolio_end_to_end.py design-qa.md
git commit -m "test: verify portfolio ledger cutover"
```

## Phase 4 Completion Gate

- Run every new test module from Phases 1–4.
- Run `pytest -q` with zero failures.
- Run `git diff --check`.
- Verify 390px and 430px screenshots and no horizontal overflow.
- Verify only the web exposes writes; Enterprise WeChat and AI routing expose read-only portfolio commands.
- Verify the private token authorizes writes but is never logged or returned.
- Verify production migration on copied data, backup restore, duplicate-run safety, and exact share totals.
- Verify the initial catalog supports `NYRR000007`, `AM264381F`, `003103`, and `015736`.
- Create the two fund plans with amounts 100 and 2,000, rates 0 and `0.00006`, and leave them as drafts until the user selects each cash-management source.
- Do not activate either SIP plan during deployment without the user's source selection.
