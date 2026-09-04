# Portfolio SIP Weekly and Monthly Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add user-selectable daily, weekday-only weekly, and monthly SIP schedules while preserving existing daily plans and rolling non-trading schedule dates forward.

**Architecture:** Persist a normalized frequency and optional schedule day on each plan through a v5 SQLite migration. Put calendar arithmetic in a focused `portfolio_sip_schedule` module; the SIP service consumes effective dates, while the API, view payload, and existing dashboard expose the same fields.

**Tech Stack:** Python 3, dataclasses and enums, SQLite, Flask, pytest, vanilla HTML/CSS/JavaScript.

## Global Constraints

- Frequencies are exactly `daily`, `weekly`, and `monthly`.
- Weekly `schedule_day` is one ISO weekday from 1 through 5.
- Monthly `schedule_day` is one integer from 1 through 31.
- Daily plans have `schedule_day = None`.
- Non-trading planned dates roll forward to the next trading day.
- Missing monthly dates use that month's final calendar day before roll-forward.
- `start_date` is only the activation boundary.
- Existing rows and requests without frequency remain daily.
- Paused or insufficient-balance periods are not backfilled.

---

## File Structure

- `src/portfolio_models.py`: schedule types and plan fields.
- `src/portfolio_db.py`: schema creation and v5 migration.
- `src/portfolio_repository.py`: schedule persistence.
- `src/portfolio_sip_schedule.py`: validation and effective-date calculation.
- `src/portfolio_sip.py`: plan validation and schedule execution.
- `src/portfolio_api.py`, `src/portfolio_view.py`: API and management payload.
- `src/nav_dashboard_page.html`: schedule controls and summaries.
- Existing portfolio pytest modules: focused and regression coverage.

### Task 1: Persist normalized SIP schedules

**Files:**
- Modify: `src/portfolio_models.py:44-47,139-146`
- Modify: `src/portfolio_db.py:9-10,105-116,462-568`
- Modify: `src/portfolio_repository.py:40-42,443-482,807-816`
- Test: `tests/test_portfolio_db.py`
- Test: `tests/test_portfolio_repository.py`

**Interfaces:**
- Produces: `SipFrequency(str, Enum)` with `DAILY`, `WEEKLY`, `MONTHLY`.
- Produces: `SipPlan.frequency: SipFrequency`, `SipPlan.schedule_day: int | None`.
- Produces: schema v5 columns `frequency TEXT NOT NULL DEFAULT 'daily'` and `schedule_day INTEGER`.

- [ ] **Step 1: Write failing migration and repository tests**

Create a current database, insert one plan, remove the two new columns, mark it as v4, and initialize again:

```python
def test_v4_upgrade_adds_default_daily_sip_schedule(tmp_path):
    db = PortfolioDatabase(tmp_path / "portfolio.db")
    db.initialize()
    with db.connection() as conn:
        conn.execute(
            """INSERT INTO products
               (id, provider, code, name, product_type, status)
               VALUES ('fund', 'test', '000001', '基金',
                       'public_fund', 'active')"""
        )
        conn.execute(
            """INSERT INTO sip_plans
               (id, product_id, daily_amount, purchase_fee_rate, status,
                start_date)
               VALUES ('legacy', 'fund', '100', '0', 'draft',
                       '2026-09-01')"""
        )
        conn.execute("ALTER TABLE sip_plans DROP COLUMN schedule_day")
        conn.execute("ALTER TABLE sip_plans DROP COLUMN frequency")
        conn.execute("DELETE FROM schema_migrations")
        conn.execute("INSERT INTO schema_migrations(version) VALUES (4)")

    db.initialize()

    with db.connection() as conn:
        row = conn.execute(
            "SELECT frequency, schedule_day FROM sip_plans WHERE id = 'legacy'"
        ).fetchone()
        version = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchone()[0]
    assert tuple(row) == ("daily", None)
    assert version == SCHEMA_VERSION == 5
```

Add this repository round-trip case:

```python
def test_repository_round_trips_weekly_sip_schedule(repo):
    repo.add_product(Product(
        "fund", "test", "000001", "基金", ProductType.PUBLIC_FUND,
    ))
    plan = SipPlan(
        id="weekly",
        product_id="fund",
        daily_amount=Decimal("100"),
        purchase_fee_rate=Decimal("0"),
        source_cash_product_id="",
        status=SipPlanStatus.DRAFT,
        start_date=date(2026, 9, 1),
        frequency=SipFrequency.WEEKLY,
        schedule_day=5,
    )
    repo.save_plan(plan)
    assert repo.get_plan(plan.id) == plan
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_portfolio_db.py tests/test_portfolio_repository.py -q`

Expected: FAIL because v5, `SipFrequency`, and schedule columns do not exist.

- [ ] **Step 3: Implement model and migration**

Add:

```python
class SipFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"

@dataclass(frozen=True)
class SipPlan:
    id: str
    product_id: str
    daily_amount: Decimal
    purchase_fee_rate: Decimal
    source_cash_product_id: str
    status: SipPlanStatus
    start_date: date
    frequency: SipFrequency = SipFrequency.DAILY
    schedule_day: Optional[int] = None
```

Set `SCHEMA_VERSION = 5`, add both columns to `BASE_SCHEMA_SQL`, and add:

```python
@staticmethod
def _upgrade_v4_to_v5(conn) -> None:
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(sip_plans)")
    }
    if "frequency" not in columns:
        conn.execute(
            "ALTER TABLE sip_plans "
            "ADD COLUMN frequency TEXT NOT NULL DEFAULT 'daily'"
        )
    if "schedule_day" not in columns:
        conn.execute("ALTER TABLE sip_plans ADD COLUMN schedule_day INTEGER")
    conn.execute("DELETE FROM schema_migrations")
    conn.execute(
        "INSERT INTO schema_migrations(version) VALUES (?)",
        (SCHEMA_VERSION,),
    )
```

Make every supported upgrade chain finish with `_upgrade_v4_to_v5`. Extend `_PLAN_COLUMNS`, the upsert, and `_plan_from_row` using `SipFrequency(row["frequency"])`.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_portfolio_db.py tests/test_portfolio_repository.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/portfolio_models.py src/portfolio_db.py src/portfolio_repository.py tests/test_portfolio_db.py tests/test_portfolio_repository.py
git commit -m "feat: 持久化定投周期配置"
```

### Task 2: Calculate and execute schedules

**Files:**
- Create: `src/portfolio_sip_schedule.py`
- Modify: `src/portfolio_sip.py:49-87,120-214,520-541`
- Test: `tests/test_portfolio_sip.py`

**Interfaces:**
- Consumes: Task 1 model fields.
- Produces: `normalize_sip_schedule(frequency, schedule_day) -> tuple[SipFrequency, int | None]`.
- Produces: `iter_sip_trade_dates(plan, through_date) -> Iterator[date]`.
- Produces: `is_sip_trade_date(plan, candidate) -> bool`.

- [ ] **Step 1: Write failing validation tests**

Extend the existing `active_plan` test helper with frequency and day parameters. Add:

```python
@pytest.mark.parametrize(
    ("frequency", "schedule_day"),
    [
        (SipFrequency.DAILY, 1),
        (SipFrequency.WEEKLY, None),
        (SipFrequency.WEEKLY, 0),
        (SipFrequency.WEEKLY, 6),
        (SipFrequency.MONTHLY, 0),
        (SipFrequency.MONTHLY, 32),
    ],
)
def test_sip_rejects_invalid_frequency_day_combinations(
    sip_services, frequency, schedule_day
):
    repo, sip, _ = sip_services
    seed_fund(repo, "fund", "003103")
    with pytest.raises(ValueError, match="^invalid SIP schedule$"):
        sip.save_plan(
            product_id="fund",
            daily_amount="100",
            purchase_fee_rate="0",
            start_date=date(2026, 9, 1),
            frequency=frequency,
            schedule_day=schedule_day,
        )
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_portfolio_sip.py::test_sip_rejects_invalid_frequency_day_combinations -q`

Expected: FAIL because `save_plan` lacks schedule arguments.

- [ ] **Step 3: Implement normalization**

Create `normalize_sip_schedule` with these exact rules:

```python
def normalize_sip_schedule(frequency, schedule_day):
    try:
        normalized = (
            frequency if isinstance(frequency, SipFrequency)
            else SipFrequency(str(frequency or "daily").strip().lower())
        )
    except ValueError as exc:
        raise ValueError("invalid SIP schedule") from exc
    if normalized is SipFrequency.DAILY:
        if schedule_day not in (None, ""):
            raise ValueError("invalid SIP schedule")
        return normalized, None
    if isinstance(schedule_day, bool):
        raise ValueError("invalid SIP schedule")
    try:
        day = int(schedule_day)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid SIP schedule") from exc
    limit = 5 if normalized is SipFrequency.WEEKLY else 31
    if str(day) != str(schedule_day).strip() or not 1 <= day <= limit:
        raise ValueError("invalid SIP schedule")
    return normalized, day
```

Update `SipService.save_plan` to normalize and store both values.

- [ ] **Step 4: Verify GREEN**

Run the focused validation test again. Expected: PASS.

- [ ] **Step 5: Write failing effective-date tests**

Add one test each for:

```python
# Weekly Wednesday from 2026-09-01 through 2026-09-10.
assert execution_dates == [date(2026, 9, 2), date(2026, 9, 9)]

# Weekly selected weekday during National Day closure.
assert execution_dates == [date(2026, 10, 9)]

# Monthly 29, 30, or 31 in February 2026 clamps to Feb 28,
# then rolls to the next official trading day.
assert execution_dates == [date(2026, 3, 2)]
```

Also assert a direct `ensure_intent` on a non-scheduled trading day returns a skipped execution with reason `not_scheduled_day`, without transactions.

- [ ] **Step 6: Verify RED**

Run: `python -m pytest tests/test_portfolio_sip.py -q`

Expected: FAIL because frequency is ignored and backfill visits every date.

- [ ] **Step 7: Implement date generation**

Use `calendar.monthrange`, `timedelta`, and existing `is_trading_day`. The core shape is:

```python
def _roll_forward(day):
    cursor = day
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor

def iter_sip_trade_dates(plan, through_date):
    frequency, schedule_day = normalize_sip_schedule(
        plan.frequency, plan.schedule_day
    )
    effective_dates = set()
    # daily: yield each trading day
    # weekly: first selected ISO weekday on/after start_date, then +7 days
    # monthly: clamp schedule_day with monthrange, then advance month
    # roll weekly/monthly planned dates before adding them
    # include only dates <= through_date
    yield from sorted(effective_dates)

def is_sip_trade_date(plan, candidate):
    return candidate in iter_sip_trade_dates(plan, candidate)
```

Replace the calendar-day loop in `backfill_plan` with `iter_sip_trade_dates`. In `_skip_reason`, preserve existing start/status/calendar reasons and use `not_scheduled_day` for a valid but non-due date. Propagate missing official-calendar errors into the existing `calendar_unavailable` no-debit path.

- [ ] **Step 8: Verify GREEN**

Run: `python -m pytest tests/test_portfolio_sip.py tests/test_portfolio_jobs.py -q`

Expected: PASS, including daily compatibility.

- [ ] **Step 9: Commit**

```powershell
git add src/portfolio_sip_schedule.py src/portfolio_sip.py tests/test_portfolio_sip.py tests/test_portfolio_jobs.py
git commit -m "feat: 执行每周每月定投计划"
```

### Task 3: Round-trip schedule fields through API and view

**Files:**
- Modify: `src/portfolio_api.py:869-999,1740-1855`
- Modify: `src/portfolio_view.py:539-619`
- Test: `tests/test_portfolio_api.py`
- Test: `tests/test_portfolio_view.py`

**Interfaces:**
- Consumes: schedule normalization and model fields.
- Produces: request, preview, write, and read fields `frequency` and `schedule_day`.

- [ ] **Step 1: Write failing API tests**

Create and preview a weekly plan with `frequency="weekly"`, `schedule_day=5`; assert both responses return those values and the repository plan uses `SipFrequency.WEEKLY`.

Parametrize invalid bodies:

```python
[
    {"frequency": "weekly", "schedule_day": 6},
    {"frequency": "monthly", "schedule_day": 32},
    {"frequency": "daily", "schedule_day": 1},
]
```

Assert HTTP 400 and `error == "invalid SIP schedule"`. Add a view test asserting an omitted frequency returns `daily` and `None`.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_portfolio_api.py tests/test_portfolio_view.py -q`

Expected: FAIL because the API and payload omit schedule fields.

- [ ] **Step 3: Implement API normalization and payload fields**

In `sip_preview`:

```python
frequency, schedule_day = normalize_sip_schedule(
    body.get("frequency", "daily"),
    body.get("schedule_day"),
)
```

Return:

```python
"frequency": frequency.value,
"schedule_day": schedule_day,
```

Pass them into `SipPlan`:

```python
frequency=SipFrequency(preview["frequency"]),
schedule_day=preview["schedule_day"],
```

In `_sip_plan_row`, return:

```python
"frequency": plan.frequency.value,
"schedule_day": plan.schedule_day,
```

- [ ] **Step 4: Verify GREEN**

Run the API and view test modules again. Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/portfolio_api.py src/portfolio_view.py tests/test_portfolio_api.py tests/test_portfolio_view.py
git commit -m "feat: 开放定投周期接口"
```

### Task 4: Add dashboard schedule controls

**Files:**
- Modify: `src/nav_dashboard_page.html:751-755,1689-1710,2128-2146,2435-2464,2846-2860`
- Test: `tests/test_nav_dashboard_page.py`

**Interfaces:**
- Consumes: `frequency` and `schedule_day`.
- Produces: `sipFrequency`, `sipWeeklyDay`, `sipMonthlyDay`.
- Produces: `sipScheduleLabel(row)`.

- [ ] **Step 1: Write failing static page tests**

Assert the page contains daily/weekly/monthly options, weekly values 1 through 5 only, monthly control, conditional sync, edit refill, request normalization, preview labels, card summary, and the visible label `开始日期`.

```python
assert 'name="frequency" id="sipFrequency"' in html
assert '<option value="5">周五</option>' in html
assert 'value="6">周六' not in html
assert 'id="sipMonthlyDay"' in html
assert "function syncSipScheduleFields()" in html
assert "function sipScheduleLabel(row)" in html
assert "'frequency': '定投周期'" in html
assert "'schedule_day': '扣款日'" in html
assert ">开始日期<" in html
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_nav_dashboard_page.py -q`

Expected: FAIL because the controls do not exist.

- [ ] **Step 3: Implement conditional fields and display**

Use disabled hidden inputs to prevent stale `FormData` values:

```javascript
function syncSipScheduleFields() {
  const frequency = $('sipFrequency').value;
  const weekly = frequency === 'weekly';
  const monthly = frequency === 'monthly';
  $('sipWeeklyDayField').hidden = !weekly;
  $('sipWeeklyDay').disabled = !weekly;
  $('sipMonthlyDayField').hidden = !monthly;
  $('sipMonthlyDay').disabled = !monthly;
}
```

Create weekly options for Monday through Friday and monthly options 1 through 31. On submit, set `schedule_day` to the selected number or `null`. On edit, refill frequency and day, then sync controls. Rename “首次扣款日” to “开始日期”.

Render cards with:

```javascript
function sipScheduleLabel(row) {
  const frequency = String(rowValue(row, 'frequency') || 'daily');
  const day = number(rowValue(row, 'schedule_day'));
  if (frequency === 'weekly') {
    return '每周' + (['', '一', '二', '三', '四', '五'][day] || '--');
  }
  if (frequency === 'monthly') return '每月 ' + day + ' 日';
  return '每日';
}
```

Add Chinese preview labels for both fields.

- [ ] **Step 4: Verify GREEN**

Run the page tests again. Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/nav_dashboard_page.html tests/test_nav_dashboard_page.py
git commit -m "feat: 增加定投周期选择界面"
```

### Task 5: End-to-end and regression verification

**Files:**
- Test: `tests/test_portfolio_end_to_end.py`
- Modify only if exposed: a source file already named in Tasks 1 through 4.

**Interfaces:**
- Consumes: all prior task outputs.
- Produces: verified persistence-to-execution-to-view behavior.

- [ ] **Step 1: Write the end-to-end case**

Add the required imports and this real-runtime test:

```python
from src.portfolio_models import SipFrequency
from src.portfolio_positions import PositionProjector
from src.portfolio_sip import SipService
from src.portfolio_transactions import PortfolioTransactionService


def test_monthly_sip_persists_executes_and_appears_in_dashboard(tmp_path):
    runtime, _cfg = _seed_runtime(tmp_path)
    repo = runtime.repository
    _add_product(
        repo, "fund", "changsheng_fund", "003103",
        "长盛盛裕纯债C", ProductType.PUBLIC_FUND,
    )
    projector = PositionProjector(repo)
    sip = SipService(
        repo, projector, PortfolioTransactionService(repo, projector)
    )
    plan = sip.activate(sip.save_plan(
        product_id="fund",
        daily_amount="100",
        purchase_fee_rate="0",
        source_cash_product_id=WALLET_PRODUCT_ID,
        start_date=date(2026, 9, 1),
        frequency=SipFrequency.MONTHLY,
        schedule_day=30,
    ).id)

    sip.backfill_plan(plan.id, date(2026, 9, 30), settle=False)
    payload = build_portfolio_payload(repo)

assert [row.intended_trade_date for row in sip.list_executions(plan.id)] == [
    date(2026, 9, 30)
]
assert payload["sip_plans"][0]["frequency"] == "monthly"
assert payload["sip_plans"][0]["schedule_day"] == 30
```

- [ ] **Step 2: Run the end-to-end test**

Run: `python -m pytest tests/test_portfolio_end_to_end.py -q`

Expected: PASS if Tasks 1 through 4 are fully integrated. A failure must identify an omitted boundary field, not change the agreed behavior.

- [ ] **Step 3: Fix only exposed integration gaps**

Pass missing `frequency` or `schedule_day` values through overlooked constructors or serializers. Do not add schedule types, multi-select, or alternate holiday handling.

- [ ] **Step 4: Run the portfolio regression suite**

Run:

```powershell
python -m pytest tests/test_portfolio_db.py tests/test_portfolio_repository.py tests/test_portfolio_sip.py tests/test_portfolio_jobs.py tests/test_portfolio_api.py tests/test_portfolio_view.py tests/test_portfolio_transactions.py tests/test_portfolio_positions.py tests/test_portfolio_end_to_end.py tests/test_nav_dashboard_page.py -q
```

Expected: PASS with no new warnings.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`

Expected: PASS.

- [ ] **Step 6: Review and commit integration changes**

Run:

```powershell
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors and only planned files changed. If Task 3 required final corrections:

```powershell
git add src tests
git commit -m "test: 验证定投周期端到端行为"
```

