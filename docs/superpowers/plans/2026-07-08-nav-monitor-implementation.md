# Nav Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Linux-safe wealth product NAV monitoring to `auto_faribao` without changing existing daily report behavior.

**Architecture:** Implement NAV logic in a new isolated `src/nav_monitor.py` module, with small integration points in config, scheduler, and WeChat command handling. Existing daily report submission, cookies checks, statistics, cache cleanup, and pending confirmation must keep their current triggers and files.

**Tech Stack:** Python 3, pytest, requests/urllib, PyYAML, pycryptodome, Enterprise WeChat app messages.

---

## File Map

- Create `src/nav_monitor.py`: data models, provider adapters, date parsing, NAV comparison, message formatting, config mutation helpers, and independent NAV add-product pending state.
- Create `tests/test_nav_monitor.py`: unit tests for date parsing, NAV comparison, message formatting, product candidate confirmation, and mocked provider parsing.
- Create `tests/test_nav_monitor_commands.py`: tests for command classification and non-regression against daily report commands.
- Modify `src/config.py`: add `NavMonitorConfig` and `NavProductConfig`, load/save `nav_monitor` without changing existing scheduler save behavior.
- Modify `src/scheduler.py`: add independent NAV push timing state and `_run_nav_monitor_push`; do not change daily report conditions.
- Modify `src/server.py`: route text commands containing NAV keywords to isolated handlers; add help/config display lines; do not alter existing daily report branches.
- Modify `requirements.txt`: add `pycryptodome`.
- Modify `config.yaml`: add default `nav_monitor` block for the two CITIC products.

## Task 1: Config Model

**Files:**
- Modify: `src/config.py`
- Test: `tests/test_nav_monitor.py`

- [ ] Write failing tests for default NAV config and save round-trip:

```python
from src.config import Config, save_config


def test_nav_monitor_config_defaults_to_enabled_with_0800():
    cfg = Config({})
    assert cfg.nav_monitor.enabled is True
    assert cfg.nav_monitor.push_hour == 8
    assert cfg.nav_monitor.push_minute == 0
    assert cfg.nav_monitor.products == []


def test_save_config_preserves_nav_monitor(tmp_path):
    cfg = Config({
        "nav_monitor": {
            "enabled": True,
            "push_hour": 8,
            "push_minute": 30,
            "products": [
                {"provider": "citic_wealth", "code": "AF233276B", "name": "P1"},
            ],
        }
    })
    path = tmp_path / "config.yaml"
    data = save_config(str(path), cfg, {"scheduler": {"report_submit_hour": 20}})
    assert data["nav_monitor"]["push_minute"] == 30
    assert data["nav_monitor"]["products"][0]["code"] == "AF233276B"
```

- [ ] Run `pytest tests/test_nav_monitor.py::test_nav_monitor_config_defaults_to_enabled_with_0800 tests/test_nav_monitor.py::test_save_config_preserves_nav_monitor -q`; expect failures because `nav_monitor` is missing.
- [ ] Add `NavProductConfig` and `NavMonitorConfig` classes in `src/config.py`.
- [ ] Update `Config.__init__` to set `self.nav_monitor`.
- [ ] Update `save_config` to persist `nav_monitor` while keeping existing scheduler keys unchanged.
- [ ] Re-run the two tests; expect pass.

## Task 2: Core Dates, Records, and Formatting

**Files:**
- Create/modify: `src/nav_monitor.py`
- Test: `tests/test_nav_monitor.py`

- [ ] Write failing tests for relative date parsing and compact date commands:

```python
from datetime import date
from src.nav_monitor import parse_nav_query_date


def test_parse_relative_dates():
    base = date(2026, 7, 8)
    assert parse_nav_query_date("查询昨天净值", base) == date(2026, 7, 7)
    assert parse_nav_query_date("前天净值", base) == date(2026, 7, 6)
    assert parse_nav_query_date("今天净值", base) == date(2026, 7, 8)


def test_parse_compact_query_date():
    assert parse_nav_query_date("查询净值 20260707", date(2026, 7, 8)) == date(2026, 7, 7)
```

- [ ] Run these tests; expect import/function failure.
- [ ] Implement `NavRecord`, `ProductCandidate`, `DateQueryResult`, `parse_nav_query_date`, `calculate_change`, and `format_nav_report`.
- [ ] Add tests for latest-vs-previous delta and date-miss formatting.
- [ ] Re-run `pytest tests/test_nav_monitor.py -q`; expect current tests pass.

## Task 3: Provider Adapters with Mockable HTTP

**Files:**
- Modify: `src/nav_monitor.py`
- Test: `tests/test_nav_monitor.py`

- [ ] Write failing tests using fake HTTP clients for CITIC history parsing and Nanyin encrypted response parsing boundaries.
- [ ] Implement provider interface methods:
  - `fetch_latest(product)`
  - `fetch_by_date(product, target_date)`
  - `search_products(query)`
  - `get_product_identity(code)`
- [ ] Implement `CiticWealthProvider` with signed H5 API requests.
- [ ] Implement `NanyinWealthProvider` with isolated SSL context, cookie jar, AES/RSA request wrapping, and no Windows curl dependency.
- [ ] Keep network timeouts short and catch provider errors into per-product result errors.
- [ ] Re-run provider tests; expect pass.

## Task 4: Product Add Confirmation

**Files:**
- Modify: `src/nav_monitor.py`
- Test: `tests/test_nav_monitor.py`

- [ ] Write failing tests for:
  - candidate list includes latest NAV
  - no-match response does not write config
  - `确认添加净值产品 1` writes only pending candidate
  - duplicate provider/code is rejected
- [ ] Implement NAV-specific pending file, e.g. `/tmp/nav_monitor_pending_add.json`.
- [ ] Do not use `/tmp/daily_report_pending.json`.
- [ ] Implement `build_add_product_candidates`, `save_pending_nav_add`, `load_pending_nav_add`, `confirm_pending_nav_add`, and `cancel_pending_nav_add`.
- [ ] Re-run tests; expect pass.

## Task 5: WeChat Command Handling

**Files:**
- Modify: `src/server.py`
- Test: `tests/test_nav_monitor_commands.py`

- [ ] Write failing tests for command classification:
  - `立即查询净值`
  - `查询昨天净值`
  - `查询净值 20260707`
  - `添加净值产品 信银 AF233276B`
  - `确认添加净值产品 1`
  - existing `今日状态` is not a NAV command
  - existing `设置日报提交时间 20:00` is not a NAV command
- [ ] Add small pure helper functions in `src/nav_monitor.py` for command parsing so tests do not instantiate Flask.
- [ ] In `server.py`, add NAV command branch before generic config/help branches but after cookies update and daily report pending yes/no branch.
- [ ] Start NAV manual queries in background threads and send an immediate “正在查询净值” response.
- [ ] Do not modify existing daily report branch bodies.
- [ ] Re-run command tests; expect pass.

## Task 6: Scheduler Integration

**Files:**
- Modify: `src/scheduler.py`
- Test: `tests/test_nav_monitor.py`

- [ ] Write failing tests for `_get_times` or equivalent NAV time handling:
  - existing scheduler time tuple still exposes existing times
  - NAV push uses separate enabled/hour/minute state
  - NAV exceptions do not call `notify_report_failure`
- [ ] Add separate `last_nav_push_date` in `_run`.
- [ ] Trigger `_run_nav_monitor_push(current_cfg)` when `cfg.nav_monitor.enabled` and time matches.
- [ ] Implement `_run_nav_monitor_push` to call `src.nav_monitor.push_nav_report(cfg)`.
- [ ] Catch and log exceptions locally.
- [ ] Re-run scheduler tests; expect pass.

## Task 7: Requirements and Default Config

**Files:**
- Modify: `requirements.txt`
- Modify: `config.yaml`
- Test: `tests/test_nav_monitor.py`

- [ ] Add `pycryptodome>=3.20`.
- [ ] Add default `nav_monitor` with the two CITIC products.
- [ ] Re-run config tests.

## Task 8: Full Verification

**Files:**
- No new files unless tests expose defects.

- [ ] Run `pytest -q`; expect all tests pass.
- [ ] Run a direct mocked command parse smoke test with `python -m pytest tests/test_nav_monitor_commands.py -q`.
- [ ] If dependencies are available, run a live manual query script through `python -c` that imports `src.nav_monitor` and queries the two CITIC defaults.
- [ ] Confirm `git diff` contains no unrelated changes and no `__pycache__`.
- [ ] Summarize any live network failures separately from unit test status.

## Non-Regression Checklist

- Existing `auto_submit_if_needed` code path is unchanged.
- Existing `/tmp/daily_report_pending.json` is unchanged and not reused.
- Existing report submit, cookies check, stats push, cache cleanup times are not renamed or repurposed.
- NAV provider failures never call `notify_report_failure`.
- NAV command matching requires `净值` or the explicit `确认添加净值产品`/`取消添加净值产品` phrases.
