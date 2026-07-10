# 指定日期日报查询 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 支持企业微信自然语言查询指定日期日报，并只回复精确匹配的 OA 日报正文。

**Architecture:** `src.server` 将自然语言日期归一为内部指令并异步回复；`src.target` 负责跨 OA 分页精确定位、复用现有详情读取策略。日期解析和分页定位均提取为纯辅助函数，便于无网络单测。

**Tech Stack:** Python 3.11, Flask, Playwright sync API, pytest, 企业微信自建应用。

---

### Task 1: 自然语言日期归一化

**Files:**
- Modify: `src/server.py:69-118`
- Modify: `tests/test_server_command_normalization.py`

- [ ] **Step 1: Write failing normalization tests**

```python
from datetime import date

from src.server import _normalize_daily_report_command_text


def test_normalizes_explicit_daily_report_query_dates():
    assert _normalize_daily_report_command_text("查询2026-07-08日报") == "查询日报 2026-07-08"
    assert _normalize_daily_report_command_text("查20260708日报") == "查询日报 2026-07-08"
    assert _normalize_daily_report_command_text("帮我看 7月8日的日报", reference_date=date(2026, 7, 10)) == "查询日报 2026-07-08"


def test_normalizes_relative_daily_report_query_dates():
    assert _normalize_daily_report_command_text("查昨天日报", reference_date=date(2026, 7, 10)) == "查询日报 2026-07-09"
    assert _normalize_daily_report_command_text("看前天日报", reference_date=date(2026, 7, 10)) == "查询日报 2026-07-08"


def test_keeps_non_query_daily_report_commands_unchanged():
    assert _normalize_daily_report_command_text("发送日报") == "发送日报"
    assert _normalize_daily_report_command_text("撤回今日日报") == "撤回今日日报"
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_server_command_normalization.py -q`

Expected: FAIL because the function has no `reference_date` argument and compact/natural date query expressions are not normalized.

- [ ] **Step 3: Implement date-query parsing**

Add a private helper that accepts normalized text and an optional `date` reference:

```python
def _parse_daily_report_query_date(content: str, reference_date: date) -> date | None:
    if "日报" not in content or not _contains_any(content, ("查询", "查", "看", "查看", "读取")):
        return None
    # Parse YYYYMMDD, YYYY-MM-DD, M月D日, 今天、昨天、前天.
```

Change the normalizer signature to `reference_date: date | None = None`; when omitted, import `src.beijing_time.today`. If the helper returns a date, return `查询日报 {date.isoformat()}` before existing generic “读取日报” logic. Preserve all existing send, delete, statistics and NAV exclusions.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_server_command_normalization.py -q`

Expected: PASS.

### Task 2: OA 跨页精确读取正文

**Files:**
- Modify: `src/target.py:1071-1121`
- Modify: `tests/test_report_recovery.py`

- [ ] **Step 1: Write failing pagination tests**

```python
def test_finds_report_content_on_later_page(monkeypatch):
    from src import target

    page = SimpleNamespace(wait_for_timeout=lambda *_: None)
    rows = [None, object()]
    monkeypatch.setattr(target, "_find_existing_report_row", lambda _page, _date: rows.pop(0))
    monkeypatch.setattr(target, "_go_to_next_page", lambda _page: bool(rows))
    monkeypatch.setattr(target, "_read_report_content_from_row", lambda _page, _row: "目标日报正文")

    assert target._find_report_content_on_pages(page, "2026-07-08") == "目标日报正文"


def test_returns_empty_when_matching_row_has_no_readable_content(monkeypatch):
    from src import target

    page = SimpleNamespace(wait_for_timeout=lambda *_: None)
    monkeypatch.setattr(target, "_find_existing_report_row", lambda _page, _date: object())
    monkeypatch.setattr(target, "_read_report_content_from_row", lambda _page, _row: "")

    assert target._find_report_content_on_pages(page, "2026-07-08") == ""
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_report_recovery.py -q`

Expected: FAIL because `_find_report_content_on_pages` and `_read_report_content_from_row` do not exist.

- [ ] **Step 3: Implement reusable OA helpers**

```python
def _read_report_content_from_row(page: Page, row) -> str:
    for reader in (_read_content_by_expand_row, _read_content_from_row, _read_content_by_open_dialog):
        content = reader(page, row) if reader is not _read_content_from_row else reader(row)
        if content:
            return content
    return ""


def _find_report_content_on_pages(page: Page, report_date: str, max_pages: int = 12) -> str:
    for _ in range(max_pages):
        row = _find_existing_report_row(page, report_date)
        if row:
            return _read_report_content_from_row(page, row)
        if not _go_to_next_page(page):
            return ""
        page.wait_for_timeout(1000)
    return ""
```

Add `get_report_content_by_date(cfg, report_date)` using `_login_and_navigate`, `_normalize_report_date`, `_find_report_content_on_pages`, and the same browser cleanup used by `get_report_status`.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_report_recovery.py -q`

Expected: PASS.

### Task 3: 企业微信只回复正文

**Files:**
- Modify: `src/server.py:1457-1482`
- Modify: `tests/test_server_command_normalization.py`

- [ ] **Step 1: Write failing routing test**

```python
def test_daily_report_query_command_is_normalized_before_generic_reading():
    assert _normalize_daily_report_command_text("查询2026-07-08日报") == "查询日报 2026-07-08"
```

- [ ] **Step 2: Implement the exact-query handler**

Insert a branch before `获取前一天日报`:

```python
match = re.fullmatch(r"查询日报 (\d{4}-\d{2}-\d{2})", content)
if match:
    report_date = match.group(1)
    # Acquire _try_start_cmd("查询指定日期日报"), then start a daemon thread.
    # get_report_content_by_date(cfg, report_date) returns the body.
    # Send only the body; no title/status metadata.
    # If empty, send “ℹ️ 未找到 {report_date} 的日报正文”.
    # Always call _end_cmd() in finally.
```

- [ ] **Step 3: Verify GREEN and regressions**

Run: `python -m pytest tests/test_server_command_normalization.py tests/test_report_recovery.py -q`

Expected: PASS.

Run: `python -m pytest -q`

Expected: all existing and new tests PASS.

### Task 4: Deploy and verify

**Files:**
- Deploy: `src/server.py`
- Deploy: `src/target.py`

- [ ] **Step 1: Back up production files**

Run:

```powershell
ssh -i 'C:\Users\jsitc\.ssh\shouer_aliyun.pem' -p 2222 root@8.213.145.226 "mkdir -p /home/ubuntu/daily_report/backups/20260710_daily_report_query/src && cp /home/ubuntu/daily_report/src/server.py /home/ubuntu/daily_report/src/target.py /home/ubuntu/daily_report/backups/20260710_daily_report_query/src/"
```

- [ ] **Step 2: Upload, compile, restart**

Run:

```powershell
scp -i 'C:\Users\jsitc\.ssh\shouer_aliyun.pem' -P 2222 src/server.py src/target.py root@8.213.145.226:/home/ubuntu/daily_report/src/
ssh -i 'C:\Users\jsitc\.ssh\shouer_aliyun.pem' -p 2222 root@8.213.145.226 "cd /home/ubuntu/daily_report && venv/bin/python -m py_compile src/server.py src/target.py && systemctl restart daily-report && systemctl is-active daily-report"
```

Expected: `active`.

- [ ] **Step 3: Verify in Enterprise WeChat**

Send `查询2026-07-08日报`.

Expected: reply contains only the OA report body for 2026-07-08.
