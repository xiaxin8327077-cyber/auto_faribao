# 理财看板份额观察时间修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让理财看板按份额变化与净值节点实际入账的先后顺序计算收益，并将已错误入账的 2026-07-22 收益幂等纠正为 133.48991108 元。

**Architecture:** 在 `NavDashboardStore` 内将产品生命周期判断与份额选择拆开：净值日期仍受既有产品加入边界约束，具体份额则由 `changed_at <= discovered_at` 决定。新增私有原地核算方法和公开幂等纠正入口，正常历史/结果观察也复用私有核算方法。

**Tech Stack:** Python 3、`datetime`、`Decimal`、JSON 状态文件、pytest。

## Global Constraints

- 份额在净值节点入账前修改时，该节点使用新份额。
- 份额在净值节点入账后修改时，已入账收益保持不变。
- 不新增状态字段，不升级状态版本。
- 继续保留 `effective_date` 和 `include_start` 以兼容已有状态；二者只维持产品首次加入的净值日期边界，不再决定后续份额变化选择。
- 纠正只更新已有流水的 `shares` 和 `amount`，不修改 ID、净值日期、发现时间、净值或差值。
- 金额计算始终使用 `Decimal`；手工日收益覆盖不参与产品流水纠正。
- 本计划只修改和验证本地代码，不上传或修改生产数据，不执行生产部署。

---

## File Structure

- Modify: `src/nav_dashboard.py` — 提供按观察时间选择份额、已有流水幂等纠正及正常观察自愈。
- Modify: `tests/test_nav_dashboard.py` — 覆盖份额变更前后、延迟披露、显式纠正、自动纠正、异常时间戳及 2026-07-22 精确金额。

### Task 1: 新流水按观察时间选择份额

**Files:**
- Modify: `src/nav_dashboard.py:299-339`
- Test: `tests/test_nav_dashboard.py:59-101`

**Interfaces:**
- Consumes: 现有 `meta["share_events"]` 中的 `changed_at`、`effective_date`、`include_start` 和 `shares`。
- Produces: `NavDashboardStore._nav_date_is_eligible(meta: dict, nav_date: date) -> bool`。
- Produces: `NavDashboardStore._shares_for_observation(meta: dict, observed_at: datetime) -> Decimal | None`。
- Changes: `NavDashboardStore._record_pair(...)` 使用 `latest.nav_date` 做生命周期判断、使用 `discovered_at` 选择份额。

- [ ] **Step 1: 写入份额变更时序回归测试**

在 `test_share_change_only_affects_future_nav` 后加入：

```python
def test_same_day_share_change_before_discovery_uses_new_shares(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    store.initialize_from_history(
        _cfg(),
        {"citic_wealth:P1": [_record("2026-07-21", "1.0010")]},
        _dt("2026-07-21 10:00"),
    )
    changed_cfg = _cfg("20000")
    store.sync_portfolio(changed_cfg, _dt("2026-07-22 14:00"))
    store.record_results(
        changed_cfg,
        [_result("2026-07-22", "1.0020", "2026-07-21", "1.0010", shares="20000")],
        _dt("2026-07-22 23:30"),
    )
    entry = store.state["profit_entries"][0]
    assert entry["shares"] == "20000"
    assert entry["amount"] == "20.0000"


def test_share_change_after_discovery_keeps_booked_profit(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    cfg = _cfg()
    item = _result("2026-07-22", "1.0020", "2026-07-21", "1.0010")
    store.initialize_from_history(
        cfg,
        {"citic_wealth:P1": [_record("2026-07-21", "1.0010")]},
        _dt("2026-07-21 10:00"),
    )
    store.record_results(cfg, [item], _dt("2026-07-22 10:00"))
    changed_cfg = _cfg("20000")
    store.sync_portfolio(changed_cfg, _dt("2026-07-22 11:00"))
    store.record_results(changed_cfg, [item], _dt("2026-07-22 12:00"))
    assert len(store.state["profit_entries"]) == 1
    assert store.state["profit_entries"][0]["shares"] == "10000"
    assert store.state["profit_entries"][0]["amount"] == "10.0000"


def test_delayed_nav_disclosure_uses_shares_at_discovery_time(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    store.initialize_from_history(
        _cfg(),
        {"citic_wealth:P1": [_record("2026-07-20", "1.0010")]},
        _dt("2026-07-20 10:00"),
    )
    changed_cfg = _cfg("20000")
    store.sync_portfolio(changed_cfg, _dt("2026-07-22 14:00"))
    store.record_results(
        changed_cfg,
        [_result("2026-07-21", "1.0020", "2026-07-20", "1.0010", shares="20000")],
        _dt("2026-07-23 09:00"),
    )
    entry = store.state["profit_entries"][0]
    assert entry["shares"] == "20000"
    assert entry["amount"] == "20.0000"
```

- [ ] **Step 2: 运行测试并确认旧实现暴露错误**

Run:

```powershell
python -m pytest -q tests/test_nav_dashboard.py -k "same_day_share_change_before_discovery or share_change_after_discovery or delayed_nav_disclosure"
```

Expected: 前后两个使用新份额的测试失败；入账后变更测试通过。

- [ ] **Step 3: 拆分生命周期判断和观察时间份额选择**

在 `src/nav_dashboard.py` 中用以下方法替换 `_shares_for_date()`：

```python
    def _nav_date_is_eligible(self, meta: dict, nav_date: date) -> bool:
        for event in meta.get("share_events", []):
            try:
                effective = date.fromisoformat(event["effective_date"])
            except (KeyError, ValueError):
                continue
            return nav_date > effective or (
                nav_date == effective and bool(event.get("include_start"))
            )
        return False

    def _shares_for_observation(self, meta: dict, observed_at: datetime):
        selected = None
        selected_at = None
        for event in meta.get("share_events", []):
            changed_at = _parse_dt(str(event.get("changed_at") or ""))
            if changed_at is None:
                logger.warning("Ignoring dashboard share event with invalid changed_at")
                continue
            if changed_at <= observed_at and (
                selected_at is None or changed_at >= selected_at
            ):
                selected = event
                selected_at = changed_at
        return _decimal(selected.get("shares")) if selected else None
```

在 `_record_pair()` 的现有校验后加入生命周期判断，并改为按 `discovered_at` 选择份额：

```python
        if not self._nav_date_is_eligible(meta, latest.nav_date):
            return
        if self._entry_exists(product_key, latest.nav_date):
            return
        shares = self._shares_for_observation(meta, discovered_at)
```

- [ ] **Step 4: 运行新旧生命周期测试**

Run:

```powershell
python -m pytest -q tests/test_nav_dashboard.py -k "same_day_share_change_before_discovery or share_change_after_discovery or delayed_nav_disclosure or initial_backfill or new_product_does_not_backfill or share_change_only_affects"
```

Expected: 6 tests pass，观察时间规则生效，初始回溯和新产品加入边界未改变。

- [ ] **Step 5: 提交新流水计算改动**

```powershell
git add -- src/nav_dashboard.py tests/test_nav_dashboard.py
git commit -m "fix: select dashboard shares by observation time"
```

### Task 2: 已有流水幂等纠正和正常观察自愈

**Files:**
- Modify: `src/nav_dashboard.py:317-418`
- Test: `tests/test_nav_dashboard.py`

**Interfaces:**
- Consumes: Task 1 的 `_shares_for_observation(meta: dict, observed_at: datetime) -> Decimal | None`。
- Produces: `NavDashboardStore._reconcile_profit_entries() -> int`，只改内存状态并返回纠正条数。
- Produces: `NavDashboardStore.reconcile_profit_entries() -> int`，持锁执行、仅在有变化时保存，并返回纠正条数。
- Changes: `record_history()` 和 `record_results()` 在各自现有锁与单次保存流程内调用 `_reconcile_profit_entries()`。

- [ ] **Step 1: 写入显式纠正、自动纠正和异常数据测试**

在份额时序测试后加入：

```python
def _stale_profit_entry(discovered_at: str = "2026-07-22T23:30:00") -> dict:
    return {
        "id": "citic_wealth:P1:2026-07-22",
        "product_key": "citic_wealth:P1",
        "provider": "citic_wealth",
        "code": "P1",
        "name": "产品P1",
        "nav_date": "2026-07-22",
        "discovered_at": discovered_at,
        "discovered_date": "2026-07-22",
        "previous_nav": "1.0010",
        "unit_nav": "1.0020",
        "delta": "0.0010",
        "shares": "10000",
        "amount": "10.0000",
    }


def _store_with_stale_profit(tmp_path) -> NavDashboardStore:
    store = NavDashboardStore(tmp_path / "state.json")
    store.initialize_from_history(
        _cfg(),
        {"citic_wealth:P1": [_record("2026-07-21", "1.0010")]},
        _dt("2026-07-21 10:00"),
    )
    store.sync_portfolio(_cfg("20000"), _dt("2026-07-22 14:00"))
    store.state["profit_entries"] = [_stale_profit_entry()]
    return store


def test_reconcile_profit_entries_corrects_in_place_and_is_idempotent(tmp_path, monkeypatch):
    store = _store_with_stale_profit(tmp_path)
    save_calls = []
    original_save = store.save

    def tracked_save():
        save_calls.append(True)
        original_save()

    monkeypatch.setattr(store, "save", tracked_save)
    assert store.reconcile_profit_entries() == 1
    assert len(store.state["profit_entries"]) == 1
    assert store.state["profit_entries"][0]["shares"] == "20000"
    assert store.state["profit_entries"][0]["amount"] == "20.0000"
    assert len(save_calls) == 1

    state_after_first_call = store.state.copy()
    assert store.reconcile_profit_entries() == 0
    assert store.state == state_after_first_call
    assert len(save_calls) == 1


def test_reconcile_does_not_apply_share_event_after_entry_discovery(tmp_path):
    store = _store_with_stale_profit(tmp_path)
    store.state["profit_entries"][0] = _stale_profit_entry("2026-07-22T10:00:00")
    assert store.reconcile_profit_entries() == 0
    assert store.state["profit_entries"][0]["shares"] == "10000"
    assert store.state["profit_entries"][0]["amount"] == "10.0000"


def test_record_results_reconciles_existing_duplicate_entry(tmp_path):
    store = _store_with_stale_profit(tmp_path)
    changed_cfg = _cfg("20000")
    store.record_results(
        changed_cfg,
        [_result("2026-07-22", "1.0020", "2026-07-21", "1.0010", shares="20000")],
        _dt("2026-07-23 09:00"),
    )
    assert len(store.state["profit_entries"]) == 1
    assert store.state["profit_entries"][0]["shares"] == "20000"
    assert store.state["profit_entries"][0]["amount"] == "20.0000"


def test_record_history_reconciles_existing_entry(tmp_path):
    store = _store_with_stale_profit(tmp_path)
    store.record_history(_cfg("20000"), {}, _dt("2026-07-23 09:00"))
    assert store.state["profit_entries"][0]["shares"] == "20000"
    assert store.state["profit_entries"][0]["amount"] == "20.0000"


def test_reconcile_skips_entry_with_invalid_discovered_at(tmp_path, caplog):
    store = _store_with_stale_profit(tmp_path)
    store.state["profit_entries"][0]["discovered_at"] = "not-a-time"
    assert store.reconcile_profit_entries() == 0
    assert store.state["profit_entries"][0]["amount"] == "10.0000"
    assert "invalid discovered_at" in caplog.text
```

- [ ] **Step 2: 写入 2026-07-22 线上金额精确回归测试**

继续加入：

```python
def test_reconcile_2026_07_22_profit_matches_enterprise_result(tmp_path):
    store = NavDashboardStore(tmp_path / "state.json")
    changed_at = "2026-07-22T14:00:00"
    discovered_at = "2026-07-22T23:30:31"
    rows = [
        ("AF233276B", "401133.95", "0.000200", "401133.95", "80.226790"),
        ("AF212017B", "36667.07", "0.002200", "9821.43", "80.667554"),
        ("AF247389H", "39815.49", "0.001100", "10649.63", "43.797039"),
        ("A32069", "101423.76", "0.000183", "101423.76", "18.56054808"),
        ("AF233398B", "87728.31", "0.000100", "13808.34", "8.772831"),
    ]
    for code, old_shares, delta, current_shares, old_amount in rows:
        product_key = f"citic_wealth:{code}"
        share_events = [{
            "changed_at": "2026-07-01T00:00:00",
            "effective_date": "2026-07-01",
            "shares": old_shares,
            "include_start": True,
        }]
        if current_shares != old_shares:
            share_events.append({
                "changed_at": changed_at,
                "effective_date": "2026-07-22",
                "shares": current_shares,
                "include_start": False,
            })
        store.state["products"][product_key] = {"share_events": share_events}
        store.state["profit_entries"].append({
            "id": f"{product_key}:2026-07-22",
            "product_key": product_key,
            "provider": "citic_wealth",
            "code": code,
            "name": code,
            "nav_date": "2026-07-22",
            "discovered_at": discovered_at,
            "discovered_date": "2026-07-22",
            "previous_nav": "1",
            "unit_nav": str(Decimal("1") + Decimal(delta)),
            "delta": delta,
            "shares": old_shares,
            "amount": old_amount,
        })

    assert store.reconcile_profit_entries() == 3
    payload = store.payload(now=_dt("2026-07-23 08:00"))
    assert payload["latest_profit_date"] == "2026-07-22"
    assert payload["latest_profit"] == "133.48991108"
```

- [ ] **Step 3: 运行新增测试并确认缺少纠正接口**

Run:

```powershell
python -m pytest -q tests/test_nav_dashboard.py -k "reconcile or record_results_reconciles or record_history_reconciles"
```

Expected: 新测试失败，主要错误为 `AttributeError: 'NavDashboardStore' object has no attribute 'reconcile_profit_entries'`；自动纠正测试保留旧金额。

- [ ] **Step 4: 实现私有与公开幂等纠正入口**

在 `_entry_exists()` 前加入：

```python
    def _reconcile_profit_entries(self) -> int:
        corrected = 0
        for entry in self.state.get("profit_entries", []):
            discovered_at = _parse_dt(str(entry.get("discovered_at") or ""))
            if discovered_at is None:
                logger.warning(
                    "Skipping dashboard profit entry %s with invalid discovered_at",
                    entry.get("id", ""),
                )
                continue
            meta = self.state.get("products", {}).get(entry.get("product_key"))
            if not meta:
                continue
            shares = self._shares_for_observation(meta, discovered_at)
            delta = _decimal(entry.get("delta"))
            if shares is None or delta is None:
                continue
            amount = delta * shares
            if (
                _decimal(entry.get("shares")) == shares
                and _decimal(entry.get("amount")) == amount
            ):
                continue
            entry["shares"] = _decimal_text(shares)
            entry["amount"] = _decimal_text(amount)
            corrected += 1
        return corrected

    def reconcile_profit_entries(self) -> int:
        with _STATE_LOCK:
            corrected = self._reconcile_profit_entries()
            if corrected:
                self.save()
            return corrected
```

- [ ] **Step 5: 将正常历史和结果观察接入同一私有核算方法**

在 `record_history()` 中，完成全部 `_record_product_history()` 调用后加入：

```python
            self._reconcile_profit_entries()
```

在 `record_results()` 中，完成结果循环后、设置 `initialized` 前加入同一行。两个调用必须使用私有方法，以复用外层锁和最后一次 `save()`。

- [ ] **Step 6: 运行纠正测试**

Run:

```powershell
python -m pytest -q tests/test_nav_dashboard.py -k "reconcile or record_results_reconciles or record_history_reconciles"
```

Expected: 6 tests pass；2026-07-22 精确回归测试断言最新收益为 `133.48991108`。

- [ ] **Step 7: 提交已有流水纠正改动**

```powershell
git add -- src/nav_dashboard.py tests/test_nav_dashboard.py
git commit -m "fix: reconcile dashboard profit shares"
```

### Task 3: 完整验证与交付核对

**Files:**
- Verify: `src/nav_dashboard.py`
- Verify: `tests/test_nav_dashboard.py`
- Verify: `docs/superpowers/specs/2026-07-23-nav-dashboard-share-observation-time-fix-design.md`

**Interfaces:**
- Consumes: Task 1 和 Task 2 的全部代码及测试。
- Produces: 可部署的本地修复提交和完整测试证据；不产生生产数据变更。

- [ ] **Step 1: 运行看板专项测试**

Run: `python -m pytest -q tests/test_nav_dashboard.py`

Expected: 文件内全部测试通过，无新增异常 warning。

- [ ] **Step 2: 运行完整测试集**

Run: `python -m pytest -q`

Expected: 退出码为 0，所有测试通过。

- [ ] **Step 3: 检查差异完整性和工作区边界**

Run:

```powershell
git diff --check HEAD~2..HEAD
git status --short
git log -3 --oneline
```

Expected: `git diff --check` 无输出；最近两条实现提交分别为观察时间选择和已有流水纠正；原有未跟踪图片、PDF 和 `__pycache__` 未被加入提交。

- [ ] **Step 4: 核对 7 月 22 日验收断言**

Run: `python -m pytest -q tests/test_nav_dashboard.py::test_reconcile_2026_07_22_profit_matches_enterprise_result`

Expected: 1 test passes，`latest_profit == "133.48991108"` 成立。

- [ ] **Step 5: 报告本地修复结果并等待部署授权**

交付报告必须明确：新流水按 `changed_at <= discovered_at` 选份额；晚于入账的份额变更不回改收益；2026-07-22 回归值为 133.48991108 元；生产尚未修改。部署前必须备份 `data/nav_dashboard_state.json`，再显式执行一次 `reconcile_profit_entries()` 并读取 payload 验收。

不要在没有用户明确部署授权时上传代码、重启服务或执行线上纠正。
