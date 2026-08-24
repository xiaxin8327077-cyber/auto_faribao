# Report Renewal and Fallback Bugfix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure QR renewal always releases command state, scheduled/manual report submission checks Cookie validity first and resumes automatically after successful renewal, and approved historical OA reports can be read as fallback content.

**Architecture:** Extend the existing QR background launcher with a result callback and keep QR locking inside `src.qr_login_renewer`. Orchestrate Cookie preflight and one-shot post-renewal resume in `src.server`, reloading `config.yaml` before resuming. Fix OA fallback at the source in `src.target` by enforcing a “before today” cutoff and reading the structured expanded-row detail field.

**Tech Stack:** Python 3.11, Flask, Playwright sync API, pytest, Enterprise WeChat app messaging.

---

## Workspace Note

`src/server.py` already contains large uncommitted NAV-monitor changes that are running in production. Execute this plan in the current workspace, preserve those changes, and do not stage or commit the whole file. Use `apply_patch` for every source edit and inspect only the bugfix hunks before deployment.

### Task 1: Release command state when QR renewal finishes

**Files:**
- Modify: `src/qr_login_renewer.py:70-86`
- Modify: `src/server.py:559-571`
- Create: `tests/test_report_recovery.py`

- [ ] **Step 1: Write failing QR lifecycle tests**

```python
import pytest


def test_qr_background_runner_reports_success(monkeypatch):
    import src.qr_login_renewer as qr

    monkeypatch.setattr(qr, "renew_cookies_by_qr", lambda *args: (True, "扫码续期成功"))
    completed = []

    qr._run_renew_in_background(
        "config.yaml", "user-1", "test", lambda ok, msg: completed.append((ok, msg))
    )

    assert completed == [(True, "扫码续期成功")]


def test_qr_background_runner_reports_unhandled_exception(monkeypatch):
    import src.qr_login_renewer as qr

    def crash(*args):
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(qr, "renew_cookies_by_qr", crash)
    completed = []

    qr._run_renew_in_background(
        "config.yaml", "user-1", "test", lambda ok, msg: completed.append((ok, msg))
    )

    assert completed == [(False, "worker crashed")]


def test_manual_qr_completion_releases_command_state():
    import src.server as server

    server._end_cmd()
    assert server._try_start_cmd("生成二维码") is True

    server._finish_manual_qr_command(False, "等待扫码登录超时")

    assert server._try_start_cmd("下一条指令") is True
    server._end_cmd()
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m pytest tests/test_report_recovery.py -v`

Expected: FAIL because `_run_renew_in_background` and `_finish_manual_qr_command` do not exist.

- [ ] **Step 3: Add a QR background completion callback**

Add to `src/qr_login_renewer.py` and route `start_renew_cookies_by_qr` through it:

```python
from collections.abc import Callable

RenewCompleteCallback = Callable[[bool, str], None]


def _run_renew_in_background(
    config_path: str,
    to_user: str | None,
    reason: str,
    on_complete: RenewCompleteCallback | None = None,
):
    ok = False
    message = "二维码续期任务未完成"
    try:
        ok, message = renew_cookies_by_qr(config_path, to_user, reason)
    except Exception as exc:
        message = str(exc)
        logger.error("QR renew background task crashed: %s", exc, exc_info=True)
    finally:
        if on_complete:
            try:
                on_complete(ok, message)
            except Exception:
                logger.error("QR renew completion callback failed", exc_info=True)


def start_renew_cookies_by_qr(
    config_path: str,
    to_user: str = None,
    reason: str = "Cookies 已过期",
    on_complete: RenewCompleteCallback | None = None,
) -> bool:
    if RENEW_LOCK.locked():
        try:
            cfg = load_config(config_path)
            send_text(cfg.wechat, "ℹ️ 已有二维码登录续期任务正在进行中，请先完成当前扫码。", to_user)
        except Exception:
            pass
        return False

    thread = threading.Thread(
        target=_run_renew_in_background,
        args=(config_path, to_user, reason, on_complete),
        daemon=True,
        name="qr-cookie-renew",
    )
    thread.start()
    return True
```

- [ ] **Step 4: Release the server command state from the callback**

Add to `src/server.py`:

```python
def _finish_manual_qr_command(success: bool, message: str):
    logger.info("Manual QR command finished: success=%s, message=%s", success, message)
    _end_cmd()
```

Change the manual QR call to:

```python
started = start_renew_cookies_by_qr(
    config_path,
    from_user_id,
    "收到手动扫码登录指令",
    on_complete=_finish_manual_qr_command,
)
```

Keep the existing immediate `_end_cmd()` when `started` is false.

- [ ] **Step 5: Run the QR lifecycle tests and verify GREEN**

Run: `python -m pytest tests/test_report_recovery.py -v`

Expected: the three QR lifecycle tests PASS.

### Task 2: Check Cookies before report submission and resume after renewal

**Files:**
- Modify: `src/server.py:1918-1944`
- Test: `tests/test_report_recovery.py`

- [ ] **Step 1: Write failing preflight and resume tests**

```python
from types import SimpleNamespace


def test_auto_submit_defers_then_resumes_with_reloaded_config(monkeypatch):
    import src.cookies_checker as cookies_checker
    import src.report_builder as report_builder
    import src.server as server

    old_cfg = SimpleNamespace(wechat=SimpleNamespace(to_user="user-1"))
    fresh_cfg = SimpleNamespace(wechat=SimpleNamespace(to_user="user-1"))
    callback_holder = {}
    build_calls = []
    submit_calls = []

    def expired(_cfg):
        raise cookies_checker.CookiesError("Cookies expired")

    def start_renew(*args, **kwargs):
        callback_holder["callback"] = kwargs["on_complete"]
        return True

    monkeypatch.setattr(cookies_checker, "check_cookies", expired)
    monkeypatch.setattr(server, "start_renew_cookies_by_qr", start_renew)
    monkeypatch.setattr("src.config.load_config", lambda _path: fresh_cfg)
    monkeypatch.setattr(
        report_builder,
        "build_report_with_meta",
        lambda cfg: build_calls.append(cfg) or (
            "日报正文", "smart_sheet", {"smart_doc_status": "normal"}
        ),
    )
    monkeypatch.setattr(
        server,
        "_submit_and_notify",
        lambda content, cfg, source, meta: submit_calls.append((content, cfg)) or True,
    )

    assert server.auto_submit_if_needed(old_cfg) is False
    assert build_calls == []
    assert submit_calls == []

    callback_holder["callback"](True, "扫码续期成功")

    assert build_calls == [fresh_cfg]
    assert submit_calls == [("日报正文", fresh_cfg)]


def test_auto_submit_does_not_resume_when_renewal_fails(monkeypatch):
    import src.cookies_checker as cookies_checker
    import src.report_builder as report_builder
    import src.server as server

    cfg = SimpleNamespace(wechat=SimpleNamespace(to_user="user-1"))
    callback_holder = {}
    build_calls = []

    def start_renew(*args, **kwargs):
        callback_holder["callback"] = kwargs["on_complete"]
        return True

    monkeypatch.setattr(
        cookies_checker,
        "check_cookies",
        lambda _cfg: (_ for _ in ()).throw(cookies_checker.CookiesError("expired")),
    )
    monkeypatch.setattr(server, "start_renew_cookies_by_qr", start_renew)
    monkeypatch.setattr(
        report_builder,
        "build_report_with_meta",
        lambda cfg: build_calls.append(cfg) or ("正文", "smart_sheet", {}),
    )
    monkeypatch.setattr(server, "_send_wechat_text", lambda *args, **kwargs: True)

    assert server.auto_submit_if_needed(cfg) is False
    callback_holder["callback"](False, "等待扫码登录超时")

    assert build_calls == []


def test_auto_submit_with_valid_cookies_submits_normally(monkeypatch):
    import src.cookies_checker as cookies_checker
    import src.report_builder as report_builder
    import src.server as server

    cfg = SimpleNamespace(wechat=SimpleNamespace(to_user="user-1"))
    monkeypatch.setattr(cookies_checker, "check_cookies", lambda _cfg: True)
    monkeypatch.setattr(
        report_builder,
        "build_report_with_meta",
        lambda _cfg: ("正文", "smart_sheet", {"smart_doc_status": "normal"}),
    )
    submitted = []
    monkeypatch.setattr(
        server,
        "_submit_and_notify",
        lambda *args: submitted.append(args) or True,
    )

    assert server.auto_submit_if_needed(cfg) is True
    assert len(submitted) == 1
```

- [ ] **Step 2: Run the preflight tests and verify RED**

Run: `python -m pytest tests/test_report_recovery.py -v`

Expected: FAIL because `auto_submit_if_needed` does not perform Cookie preflight, does not return a result, and does not resume from a QR callback.

- [ ] **Step 3: Add report Cookie renewal orchestration**

Add these helpers to `src/server.py`:

```python
def _runtime_config_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml",
    )


def _start_report_cookie_renewal(cfg: Config, error: str) -> bool:
    config_path = _runtime_config_path()

    def on_complete(success: bool, message: str):
        if not success:
            _send_wechat_text(
                cfg.wechat,
                f"❌ Cookies 续期未完成\n{message}\n\n当日日报尚未提交，请重新生成二维码后再试。",
                getattr(cfg.wechat, "to_user", None),
            )
            return
        try:
            from src.config import load_config

            fresh_cfg = load_config(config_path)
            logger.info("Cookies renewed; resuming today's report with reloaded config")
            auto_submit_if_needed(fresh_cfg, precheck_cookies=False)
        except Exception as exc:
            logger.error("Resume report after QR renewal failed: %s", exc, exc_info=True)
            notify_report_failure(
                cfg,
                f"Cookies 续期成功，但自动继续提交失败: {exc}",
                report_source="unknown",
                smart_doc_status="unknown",
            )

    started = start_renew_cookies_by_qr(
        config_path,
        getattr(cfg.wechat, "to_user", None),
        f"日报提交前检测到 Cookies 失效: {error}",
        on_complete=on_complete,
    )
    if not started:
        _send_wechat_text(
            cfg.wechat,
            "ℹ️ Cookies 续期任务已在进行中，当日日报暂未提交。续期完成后请再次发送“发送日报”。",
            getattr(cfg.wechat, "to_user", None),
        )
    return started
```

- [ ] **Step 4: Add preflight to the report entry point**

Change `auto_submit_if_needed` to:

```python
def auto_submit_if_needed(cfg: Config, *, precheck_cookies: bool = True) -> bool:
    if precheck_cookies:
        from src.cookies_checker import check_cookies, CookiesError

        try:
            check_cookies(cfg)
        except CookiesError as exc:
            logger.warning("Report Cookie preflight failed: %s", exc)
            _start_report_cookie_renewal(cfg, str(exc))
            return False

    logger.info("No manual report today — extracting from smart sheet...")
    try:
        from src.report_builder import build_report_with_meta

        report, source, meta = build_report_with_meta(cfg)
        logger.info("Auto report content source: %s", source)
    except Exception as exc:
        logger.error("Auto-extraction failed: %s", exc)
        notify_report_failure(
            cfg,
            f"自动提取失败: {exc}",
            report_source=getattr(exc, "report_source", "generation_failed"),
            smart_doc_status=getattr(exc, "smart_doc_status", "unknown"),
            smart_doc_error=getattr(exc, "smart_doc_error", ""),
        )
        return False

    if source == "previous_report" and meta.get("smart_doc_status") == "error":
        smart_error = meta.get("smart_doc_error", "")
        if _is_cookies_expiry_error(smart_error):
            save_pending(report, source, meta)
            _send_pending_confirm_message(cfg, smart_error)
            return False

    return _submit_and_notify(report, cfg, source, meta)
```

- [ ] **Step 5: Run the preflight tests and verify GREEN**

Run: `python -m pytest tests/test_report_recovery.py -v`

Expected: all QR lifecycle and report preflight tests PASS.

### Task 3: Read approved OA report detail and exclude today's report

**Files:**
- Modify: `src/target.py:309-376`
- Modify: `src/target.py:467-514`
- Test: `tests/test_report_recovery.py`

- [ ] **Step 1: Write failing approved-row extraction and cutoff tests**

```python
class FakeTextNode:
    def __init__(self, text):
        self._text = text

    def inner_text(self):
        return self._text


class FakeFormItem:
    def __init__(self, label, content):
        self.label = FakeTextNode(label)
        self.content = FakeTextNode(content)

    def query_selector(self, selector):
        if selector == ".el-form-item__label":
            return self.label
        if selector == ".el-form-item__content":
            return self.content
        return None


class FakeExpandedElement:
    def __init__(self, items):
        self.items = items

    def query_selector_all(self, selector):
        return self.items if selector == ".el-form-item" else []

    def inner_text(self):
        return "详情与正文黏连，旧文本策略无法识别"


def test_expanded_approved_row_reads_structured_detail_only():
    from src.target import _extract_detail_from_expanded_row

    element = FakeExpandedElement([
        FakeFormItem("详情", "1. 修复生产问题\n2. 完成回归验证"),
        FakeFormItem("审核说明", "审核通过"),
        FakeFormItem("审核时间", "2026-07-08 00:00:00"),
    ])

    assert _extract_detail_from_expanded_row(element) == "1. 修复生产问题\n2. 完成回归验证"


def test_previous_report_cutoff_defaults_to_today(monkeypatch):
    import src.beijing_time
    from src.target import _previous_report_cutoff

    monkeypatch.setattr(src.beijing_time, "today_str", lambda: "2026-07-10")

    assert _previous_report_cutoff(None) == "2026-07-10"
    assert _previous_report_cutoff("2026-07-09") == "2026-07-09"
```

- [ ] **Step 2: Run the OA fallback tests and verify RED**

Run: `python -m pytest tests/test_report_recovery.py -v`

Expected: FAIL because `_previous_report_cutoff` does not exist and the current parser cannot separate an inline “详情” label from its content.

- [ ] **Step 3: Add a default historical cutoff**

Add and use this helper in `src/target.py`:

```python
def _previous_report_cutoff(before_date: str = None) -> str:
    return _normalize_report_date(before_date)
```

Change `get_previous_report_content` to:

```python
cutoff = _previous_report_cutoff(before_date)
```

Keep the existing filter:

```python
if cutoff and row_date and row_date >= cutoff:
    continue
```

- [ ] **Step 4: Parse the structured expanded-row detail field first**

Add to the beginning of `_extract_detail_from_expanded_row`:

```python
form_items = element.query_selector_all(".el-form-item")
for item in form_items:
    label = item.query_selector(".el-form-item__label")
    content = item.query_selector(".el-form-item__content")
    if not label or not content:
        continue
    label_text = (label.inner_text() or "").strip().rstrip("：:")
    if label_text not in ("详情", "工作内容", "日志内容", "内容", "工作详情"):
        continue
    detail = (content.inner_text() or "").strip()
    if len(detail) >= 5:
        return detail
```

Leave the existing text-line parser after this block for older OA layouts.

- [ ] **Step 5: Run the OA fallback tests and verify GREEN**

Run: `python -m pytest tests/test_report_recovery.py -v`

Expected: all tests in `tests/test_report_recovery.py` PASS.

### Task 4: Regression verification

**Files:**
- Verify: `src/qr_login_renewer.py`
- Verify: `src/server.py`
- Verify: `src/target.py`
- Verify: `tests/test_report_recovery.py`

- [ ] **Step 1: Run focused report recovery tests**

Run: `python -m pytest tests/test_report_recovery.py -q`

Expected: all report recovery tests PASS with no warnings or errors.

- [ ] **Step 2: Run the complete test suite**

Run: `python -m pytest -q`

Expected: existing tests plus the new recovery tests all PASS.

- [ ] **Step 3: Run syntax and diff checks**

Run: `python -m compileall -q src tests`

Expected: exit code 0.

Run: `git diff --check -- src/qr_login_renewer.py src/server.py src/target.py tests/test_report_recovery.py`

Expected: no whitespace errors.

- [ ] **Step 4: Inspect scope without staging unrelated work**

Run: `git diff -- src/qr_login_renewer.py src/server.py src/target.py tests/test_report_recovery.py`

Expected: only QR completion handling, report Cookie preflight/resume, historical cutoff/detail extraction, and their tests are new. Do not run `git add src/server.py` because it would stage pre-existing NAV changes.

### Task 5: Backup, deploy, and smoke-check production

**Files:**
- Deploy: `src/qr_login_renewer.py`
- Deploy: `src/server.py`
- Deploy: `src/target.py`

- [ ] **Step 1: Back up the three production files**

Run:

```powershell
ssh -i 'C:\Users\jsitc\.ssh\shouer_aliyun.pem' -p 2222 root@8.213.145.226 "mkdir -p /home/ubuntu/daily_report/backups/20260710_report_recovery/src && cp /home/ubuntu/daily_report/src/qr_login_renewer.py /home/ubuntu/daily_report/src/server.py /home/ubuntu/daily_report/src/target.py /home/ubuntu/daily_report/backups/20260710_report_recovery/src/"
```

Expected: exit code 0 and backup directory contains all three files.

- [ ] **Step 2: Upload only the repaired source files**

Run:

```powershell
scp -i 'C:\Users\jsitc\.ssh\shouer_aliyun.pem' -P 2222 src/qr_login_renewer.py src/server.py src/target.py root@8.213.145.226:/home/ubuntu/daily_report/src/
```

Expected: all three transfers complete successfully. Do not upload `config.yaml`.

- [ ] **Step 3: Compile remotely and restart the service**

Run:

```powershell
ssh -i 'C:\Users\jsitc\.ssh\shouer_aliyun.pem' -p 2222 root@8.213.145.226 "cd /home/ubuntu/daily_report && venv/bin/python -m py_compile src/qr_login_renewer.py src/server.py src/target.py && systemctl restart daily-report && systemctl is-active daily-report"
```

Expected: `active`.

- [ ] **Step 4: Verify startup and run a read-only historical fallback probe**

Run the remote service log check and then call `get_previous_report_content` with today's cutoff in a one-off read-only Python process.

Expected: service starts without traceback; the fallback probe returns the 2026-07-09 or latest earlier approved report body instead of `No previous report content found`.

- [ ] **Step 5: Report manual acceptance commands**

Ask the user to verify in Enterprise WeChat:

```text
生成二维码
```

After either timeout or success, send:

```text
今日状态
```

Expected: the second command is accepted instead of returning “当前正在执行：生成二维码”. For the full scheduled flow, an expired Cookie causes a QR prompt and a successful scan automatically continues the current day's report submission.
