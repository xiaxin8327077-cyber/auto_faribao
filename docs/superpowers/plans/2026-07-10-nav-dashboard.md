# NAV Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a private-link mobile NAV portfolio dashboard with hourly/coalesced refreshes and immutable earnings accounting from 2026-07-01.

**Architecture:** Add a self-contained `nav_dashboard` state and rendering module. Existing latest-NAV queries stay authoritative and publish their results to the dashboard as a side effect; dashboard-only refreshes use the same single-flight lock but never send WeCom messages. Flask exposes a cached read API and asynchronous manual refresh endpoint, while the existing scheduler only starts dashboard refreshes when stale and safely away from enterprise push times.

**Tech Stack:** Python 3, Flask, Decimal, JSON atomic persistence, pytest, HTML/CSS/vanilla JavaScript, Playwright browser QA.

---

### Task 1: Earnings ledger and cached dashboard model

**Files:**
- Create: `src/nav_dashboard.py`
- Create: `tests/test_nav_dashboard.py`

- [ ] **Step 1: Write failing ledger tests**

Create tests using `tmp_path`, `Config`, `NavRecord`, and `ProductNavResult` that assert:

```python
def test_initial_backfill_uses_current_shares_from_base_date(tmp_path):
    state = initialize_from_history(
        cfg_with_product(shares="10000"),
        {"P1": [record("2026-06-30", "1.0000"), record("2026-07-01", "1.0010"), record("2026-07-02", "1.0030")]},
        state_path=tmp_path / "state.json",
        discovered_at=dt("2026-07-10 10:00"),
    )
    assert Decimal(state["totals"]["cumulative_profit"]) == Decimal("30.0000")


def test_share_change_only_affects_future_nav(tmp_path):
    store = initialized_store(tmp_path, shares="10000")
    store.record_results(cfg, [result("2026-07-02", "1.0020", "2026-07-01", "1.0010")], dt("2026-07-02 10:00"))
    store.sync_portfolio(cfg_with_product(shares="20000"), dt("2026-07-02 11:00"))
    store.record_results(cfg, [result("2026-07-03", "1.0030", "2026-07-02", "1.0020")], dt("2026-07-03 10:00"))
    assert store.payload()["cumulative_profit"] == "30.0000"


def test_deleted_product_keeps_historical_profit(tmp_path):
    store = initialized_store(tmp_path, shares="10000")
    before = store.payload()["cumulative_profit"]
    store.sync_portfolio(cfg_without_products(), dt("2026-07-03 10:00"))
    assert store.payload()["cumulative_profit"] == before
    assert store.payload()["products"] == []


def test_new_product_does_not_backfill_before_add_date(tmp_path):
    store = initialized_empty_store(tmp_path)
    cfg = cfg_with_product(shares="10000")
    store.sync_portfolio(cfg, dt("2026-07-05 10:00"))
    store.record_history(cfg, {"P1": [record("2026-07-04", "1.0000"), record("2026-07-05", "1.0100"), record("2026-07-06", "1.0200")]}, dt("2026-07-06 10:00"))
    assert store.payload()["cumulative_profit"] == "100.0000"


def test_duplicate_nav_date_is_not_counted_twice(tmp_path):
    store = initialized_store(tmp_path, shares="10000")
    item = result("2026-07-02", "1.0020", "2026-07-01", "1.0010")
    store.record_results(cfg, [item], dt("2026-07-02 10:00"))
    store.record_results(cfg, [item], dt("2026-07-02 11:00"))
    assert len(store.state["profit_entries"]) == 1
```

- [ ] **Step 2: Run tests and verify RED**

Run: `pytest -q tests/test_nav_dashboard.py`

Expected: import fails because `src.nav_dashboard` does not exist.

- [ ] **Step 3: Implement atomic state, lifecycle events, and profit entries**

Implement in `src/nav_dashboard.py`:

```python
BASE_DATE = date(2026, 7, 1)
DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "nav_dashboard_state.json"


class NavDashboardStore:
    def __init__(self, state_path=DEFAULT_STATE_PATH): ...
    def load(self) -> dict: ...
    def save(self, state: dict) -> None: ...
    def sync_portfolio(self, cfg, changed_at: datetime) -> dict: ...
    def initialize_from_history(self, cfg, histories: dict[str, list[NavRecord]], discovered_at: datetime) -> dict: ...
    def record_history(self, cfg, histories: dict[str, list[NavRecord]], discovered_at: datetime) -> dict: ...
    def record_results(self, cfg, results: list[ProductNavResult], discovered_at: datetime) -> dict: ...
    def payload(self, cfg=None) -> dict: ...
```

Use string serialization for `Decimal`, ISO strings for dates/times, event IDs of `provider:code:nav_date`, and `Path.with_suffix(".tmp")` plus `os.replace()` for writes. Preserve deleted products in lifecycle/event data while filtering them from current product rows.

- [ ] **Step 4: Run ledger tests and verify GREEN**

Run: `pytest -q tests/test_nav_dashboard.py`

Expected: ledger tests pass.

### Task 2: Shared query coordination and dashboard refresh policy

**Files:**
- Modify: `src/nav_monitor.py`
- Modify: `src/nav_dashboard.py`
- Modify: `tests/test_nav_dashboard.py`
- Modify: `tests/test_nav_monitor.py`

- [ ] **Step 1: Write failing coordination tests**

Add tests proving:

```python
def test_enterprise_latest_query_publishes_same_results_to_dashboard(monkeypatch):
    observed = []
    monkeypatch.setattr("src.nav_dashboard.observe_enterprise_results", lambda cfg, results, **kwargs: observed.extend(results))
    results = query_nav_products(cfg, query_source="enterprise")
    assert observed == results


def test_dashboard_query_does_not_publish_as_enterprise(monkeypatch):
    monkeypatch.setattr("src.nav_dashboard.observe_enterprise_results", lambda *args, **kwargs: pytest.fail("must not publish"))
    query_nav_products(cfg, query_source="dashboard")


def test_auto_refresh_waits_when_enterprise_push_is_within_one_hour():
    assert should_auto_refresh(stale_state(), dt("2026-07-10 07:30"), cfg_with_pushes("08:08", "23:30")) is False


def test_auto_refresh_runs_after_one_hour_away_from_enterprise_push():
    assert should_auto_refresh(stale_state(), dt("2026-07-10 10:30"), cfg_with_pushes("08:08", "23:30")) is True


def test_manual_refresh_has_global_five_minute_cooldown(tmp_path):
    first = request_manual_refresh(cfg, now=dt("2026-07-10 10:00"), state_path=tmp_path / "state.json")
    second = request_manual_refresh(cfg, now=dt("2026-07-10 10:01"), state_path=tmp_path / "state.json")
    assert first["accepted"] is True
    assert second["accepted"] is False
```

- [ ] **Step 2: Run coordination tests and verify RED**

Run: `pytest -q tests/test_nav_dashboard.py tests/test_nav_monitor.py -k "dashboard or enterprise_latest"`

Expected: missing query source, observer, and refresh policy failures.

- [ ] **Step 3: Implement single-flight query and observer**

Change signature:

```python
def query_nav_products(cfg, target_date=None, query_source="enterprise") -> list[ProductNavResult]:
```

Protect the provider loop with one module-level lock. For successful latest queries with `query_source == "enterprise"`, call `observe_enterprise_results(cfg, results)` in a guarded `try/except`; historical target-date queries and dashboard queries do not publish.

Implement in `nav_dashboard.py`:

```python
def should_auto_refresh(state, now, cfg) -> bool: ...
def request_manual_refresh(cfg, now=None, state_path=DEFAULT_STATE_PATH) -> dict: ...
def request_auto_refresh(cfg, now=None, state_path=DEFAULT_STATE_PATH) -> dict: ...
def observe_enterprise_results(cfg, results, observed_at=None, state_path=DEFAULT_STATE_PATH) -> None: ...
```

Manual and auto requests start daemon threads, never send WeCom messages, share an in-progress flag, and preserve a 300-second manual cooldown in state.

- [ ] **Step 4: Implement history initialization and incremental refresh**

Dashboard refresh obtains each configured product history through existing providers under the same query lock. Initial refresh requests from `BASE_DATE - 14 days`; later refreshes request enough history to include the last processed node. Failed products retain previous rows; at least one successful product updates `last_success_at`.

- [ ] **Step 5: Run coordination and existing NAV tests**

Run: `pytest -q tests/test_nav_dashboard.py tests/test_nav_monitor.py tests/test_nav_monitor_commands.py`

Expected: all pass; existing report text and image models remain unchanged.

### Task 3: Scheduler, product synchronization, and private-link API

**Files:**
- Modify: `src/scheduler.py`
- Modify: `src/server.py`
- Modify: `tests/test_nav_dashboard.py`
- Create: `tests/test_nav_dashboard_routes.py`

- [ ] **Step 1: Write failing scheduler and route tests**

Test that scheduler calls `request_auto_refresh()` when due, `_persist_runtime_config()` invokes `sync_dashboard_portfolio()` without propagating observer failures, protected APIs reject missing/wrong keys, `GET /api/nav-dashboard` does not call a provider, and `POST /api/nav-dashboard/refresh` returns `202` or cooldown status without blocking.

```python
def test_dashboard_api_reads_cache_only(monkeypatch, tmp_path):
    monkeypatch.setattr("src.nav_dashboard.query_nav_products", lambda *args, **kwargs: pytest.fail("API must not fetch"))
    monkeypatch.setattr("src.nav_dashboard.get_access_token", lambda: "secret-key")
    app = create_app(cfg)
    response = app.test_client().get("/api/nav-dashboard", headers={"X-Nav-Dashboard-Key": "secret-key"})
    assert response.status_code == 200


def test_manual_refresh_api_returns_immediately(monkeypatch):
    monkeypatch.setattr("src.nav_dashboard.get_access_token", lambda: "secret-key")
    monkeypatch.setattr("src.nav_dashboard.request_manual_refresh", lambda cfg: {"accepted": True, "status": "refreshing"})
    response = create_app(cfg).test_client().post("/api/nav-dashboard/refresh", headers={"X-Nav-Dashboard-Key": "secret-key"})
    assert response.status_code == 202


def test_dashboard_api_rejects_missing_or_wrong_private_key(monkeypatch):
    monkeypatch.setattr("src.nav_dashboard.get_access_token", lambda: "secret-key")
    client = create_app(cfg).test_client()
    assert client.get("/api/nav-dashboard").status_code == 403
    assert client.get("/api/nav-dashboard", headers={"X-Nav-Dashboard-Key": "wrong"}).status_code == 403
```

- [ ] **Step 2: Run route tests and verify RED**

Run: `pytest -q tests/test_nav_dashboard_routes.py tests/test_nav_dashboard.py -k "scheduler or api or persist"`

Expected: routes and integration hooks are absent.

- [ ] **Step 3: Add scheduler due check**

In `scheduler._run`, call `request_auto_refresh(current_cfg, now=now)` each cycle. The function itself performs staleness, enterprise-window and in-progress checks, so the scheduler loop remains non-blocking.

- [ ] **Step 4: Add routes and configuration observer**

Add private-link helpers to `src/nav_dashboard.py`:

```python
ACCESS_TOKEN_PATH = Path(__file__).resolve().parents[1] / "data" / "nav_dashboard_access_token"


def get_access_token(path=ACCESS_TOKEN_PATH) -> str:
    path = Path(path)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    os.chmod(path, 0o600)
    return token


def request_is_authorized(flask_request) -> bool:
    supplied = flask_request.headers.get("X-Nav-Dashboard-Key", "")
    return bool(supplied) and hmac.compare_digest(supplied, get_access_token())
```

In `create_app` add:

```python
@app.get("/nav")
def nav_dashboard_page():
    from src.nav_dashboard import render_dashboard_page
    return render_dashboard_page()

@app.get("/api/nav-dashboard")
def nav_dashboard_data():
    from src.nav_dashboard import get_dashboard_payload, request_is_authorized
    if not request_is_authorized(request):
        return jsonify({"error": "private_link_invalid"}), 403
    return jsonify(get_dashboard_payload(cfg))

@app.post("/api/nav-dashboard/refresh")
def nav_dashboard_refresh():
    from src.nav_dashboard import request_is_authorized, request_manual_refresh
    if not request_is_authorized(request):
        return jsonify({"error": "private_link_invalid"}), 403
    result = request_manual_refresh(cfg)
    return jsonify(result), (202 if result.get("accepted") else 200)
```

After `save_config()` succeeds in `_persist_runtime_config`, call `sync_dashboard_portfolio(cfg)` inside a `try/except` that logs but does not raise.

- [ ] **Step 5: Run route and scheduler tests**

Run: `pytest -q tests/test_nav_dashboard_routes.py tests/test_nav_dashboard.py tests/test_server_command_normalization.py`

Expected: all pass.

### Task 4: Implement the responsive mobile dashboard

**Files:**
- Modify: `src/nav_dashboard.py`
- Reference: `docs/nav-dashboard-mobile-preview.html`
- Test: `tests/test_nav_dashboard_routes.py`

- [ ] **Step 1: Write failing page-content test**

```python
def test_nav_page_contains_dashboard_shell_and_refresh_control():
    html = create_app(cfg).test_client().get("/nav").get_data(as_text=True)
    assert "理财组合看板" in html
    assert 'id="refreshButton"' in html
    assert "/api/nav-dashboard" in html
    assert "累计收益" in html
    assert "产品持仓" in html
```

- [ ] **Step 2: Run page test and verify RED**

Run: `pytest -q tests/test_nav_dashboard_routes.py::test_nav_page_contains_dashboard_shell_and_refresh_control`

Expected: `/nav` is missing or shell assertions fail.

- [ ] **Step 3: Build dynamic HTML from the approved preview**

Move the approved visual structure into `render_dashboard_page()` as a self-contained HTML document. Read the private key from `location.hash`, retain it in `sessionStorage`, and send it only through the `X-Nav-Dashboard-Key` API request header. Replace static product rows with JavaScript render functions fed by `GET /api/nav-dashboard`; add an icon-only refresh button with tooltip, loading spinner, invalid-private-link state, empty/error states, escaped text, tabular numerals, and 60-second cache polling. Keep the 430px mobile composition and responsive fit at 390px without horizontal overflow.

Use public JSON fields only; no secrets or provider URLs are embedded. Positive values receive `positive` orange-red classes and negative values receive `negative` green classes.

- [ ] **Step 4: Run route tests**

Run: `pytest -q tests/test_nav_dashboard_routes.py`

Expected: all route and HTML shell tests pass.

### Task 5: Full verification, browser QA, and production deployment

**Files:**
- Deploy: `src/nav_dashboard.py`, `src/nav_monitor.py`, `src/scheduler.py`, `src/server.py`

- [ ] **Step 1: Run complete local verification**

Run:

```bash
pytest -q
python -m compileall -q src
git diff --check -- src/nav_dashboard.py src/nav_monitor.py src/scheduler.py src/server.py tests/test_nav_dashboard.py tests/test_nav_dashboard_routes.py
```

Expected: zero failures and no compilation or whitespace errors.

- [ ] **Step 2: Start a local server and run browser QA**

Open `/nav#test-private-key` at 390x844 and 430x932. Capture screenshots and verify no horizontal overflow, no overlapping controls, readable values, dynamic rows, refresh states, invalid-link state, and crisp rendering. Use mocked cached state locally if provider access is unavailable.

- [ ] **Step 3: Back up affected production files and dashboard state**

Create `/home/ubuntu/daily_report/backups/20260710_nav_dashboard/`, copy all affected source files, and copy an existing `data/nav_dashboard_state.json` when present. Do not upload `config.yaml`.

- [ ] **Step 4: Upload, compile, and initialize without sending WeCom messages**

Upload only affected source files. Run a dashboard-only initialization using production config and verify five product rows, cumulative profit, latest success time, and no notifier calls.

- [ ] **Step 5: Restart and verify production**

Verify service `active`, local/remote hashes match, startup logs retain all existing schedules, `GET /nav` returns 200, unauthenticated API returns 403, private-key API returns valid JSON, and manual refresh respects cooldown.

- [ ] **Step 6: Provide the public URL**

Return the generated `http://8.213.145.226:8080/nav#随机密钥` link, test count, backup path, initialized cumulative value, and any products whose historical data was incomplete. Do not print the key in server logs.
