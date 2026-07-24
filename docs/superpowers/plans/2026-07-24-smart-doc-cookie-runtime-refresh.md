# 智能文档 Cookie 运行时刷新与超时分类实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 扫码或手工更新 Cookie 后立即刷新常驻服务配置，并让智能文档网络超时经过一次重试后以网络异常处理，不再误判为 Cookie 过期。

**Architecture:** `src.config.refresh_source_config()` 只把磁盘最新 `source` 原子替换到共享 `Config` 实例，调用方随后同步调度器，保证 Flask 闭包和定时任务继续引用同一对象。`src.cookies_checker` 用 `CookiesNetworkError` 区分网络超时与真实身份失效，公开检查函数完整重试一次，调度器和日报提交入口分别发送正确通知。

**Tech Stack:** Python 3.12、Playwright、PyYAML、线程回调、pytest。

## Global Constraints

- 扫码续期或手工更新 Cookie 成功后无需重启服务。
- 只刷新 `Config.source`，不得覆盖 `scheduler`、`nav_monitor`、`wechat` 或 `target`。
- 网络超时完整重试一次；连续超时不生成二维码。
- 明确登录跳转、登录提示或无权限仍按 Cookie 失效处理。
- 不改变日报正文、上一条日报兜底、OA 提交和理财看板逻辑。
- 不提交 `config.yaml`、Cookie、日志、截图或生产数据。
- 本地实施不部署服务器。

---

## File Structure

- Modify: `src/config.py` — 从磁盘刷新共享配置对象的 `source`。
- Modify: `src/server.py` — 手动扫码、自动续期和手工 Cookie 更新后同步运行时配置。
- Modify: `src/scheduler.py` — 定时扫码完成后同步配置；区分网络异常与身份失效。
- Modify: `src/qr_login_renewer.py` — 成功文案只声明验证并写盘，不提前声明运行时已生效。
- Modify: `src/wechat_notifier.py` — 手工 Cookie 更新成功文案分成写盘和运行时同步两个阶段，并提供网络异常通知。
- Modify: `src/cookies_checker.py` — 两次完整检查、网络异常类型和身份页识别。
- Modify: `src/wechat_notifier.py` — 智能文档网络异常专用通知。
- Modify: `src/notifier.py` — 网络异常通知的配置检查包装。
- Modify: `tests/test_report_recovery.py` — 运行时刷新、回调和日报提交入口回归。
- Create: `tests/test_cookies_checker.py` — 超时重试和错误类型单元测试。
- Create: `tests/test_scheduler_cookie_recovery.py` — 定时检查分类和续期回调测试。

### Task 1: Cookie 更新后刷新共享运行时配置

**Files:**
- Modify: `src/config.py:168-188`
- Modify: `src/server.py:55-59, 663-675, 787-797, 2228-2278`
- Modify: `src/scheduler.py:248-267`
- Modify: `src/qr_login_renewer.py:188-197`
- Test: `tests/test_report_recovery.py:1-180`
- Create: `tests/test_scheduler_cookie_recovery.py`

**Interfaces:**
- Produces: `refresh_source_config(current_cfg: Config, path: str) -> Config`。
- Produces: `server._refresh_runtime_source(cfg: Config, config_path: str) -> Config`。
- Produces: `server._finish_manual_qr_with_refresh(cfg, config_path, to_user, success, message) -> None`。
- Consumes: `scheduler.update_runtime_config(cfg)`。

- [ ] **Step 1: 写入配置原地刷新测试**

在 `tests/test_report_recovery.py` 的 `_cfg()` 后加入：

```python
def test_refresh_source_config_replaces_only_source(monkeypatch):
    import src.config as config_module

    current = config_module.Config({
        "source": {"TOK": "old"},
        "scheduler": {"report_submit_hour": 17},
        "nav_monitor": {"enabled": True},
        "wechat": {"corpid": "corp"},
    })
    fresh = config_module.Config({
        "source": {"TOK": "new"},
        "scheduler": {"report_submit_hour": 20},
        "nav_monitor": {"enabled": False},
        "wechat": {"corpid": "other"},
    })
    scheduler_obj = current.scheduler
    nav_obj = current.nav_monitor
    wechat_obj = current.wechat
    monkeypatch.setattr(config_module, "load_config", lambda _path: fresh)

    result = config_module.refresh_source_config(current, "config.yaml")

    assert result is current
    assert current.source.TOK == "new"
    assert current.scheduler is scheduler_obj
    assert current.nav_monitor is nav_obj
    assert current.wechat is wechat_obj
    assert current.scheduler.report_submit_hour == 17
```

- [ ] **Step 2: 写入服务器共享配置同步测试**

继续加入：

```python
def test_server_refresh_runtime_source_updates_shared_config_and_scheduler(monkeypatch):
    import src.server as server

    cfg = SimpleNamespace(source=SimpleNamespace(TOK="old"))
    fresh_source = SimpleNamespace(TOK="new")
    updates = []

    monkeypatch.setattr(
        "src.config.refresh_source_config",
        lambda current, _path: setattr(current, "source", fresh_source) or current,
    )
    monkeypatch.setattr("src.scheduler.update_runtime_config", updates.append)

    result = server._refresh_runtime_source(cfg, "config.yaml")

    assert result is cfg
    assert cfg.source is fresh_source
    assert updates == [cfg]


def test_manual_qr_success_refreshes_runtime_before_releasing_command(monkeypatch):
    import src.server as server

    cfg = _cfg()
    calls = []
    monkeypatch.setattr(
        server,
        "_refresh_runtime_source",
        lambda value, path: calls.append(("refresh", value, path)) or value,
    )
    monkeypatch.setattr(
        server,
        "_send_wechat_text",
        lambda _wechat, text, user=None: calls.append(("send", text, user)),
    )
    monkeypatch.setattr(server, "_finish_manual_qr_command", lambda ok, msg: calls.append(("finish", ok, msg)))

    server._finish_manual_qr_with_refresh(
        cfg, "config.yaml", "user-1", True, "扫码续期任务完成"
    )

    assert calls[0] == ("refresh", cfg, "config.yaml")
    assert "运行时配置已同步" in calls[1][1]
    assert calls[2] == ("finish", True, "扫码续期任务完成")


def test_manual_qr_refresh_failure_warns_and_releases_command(monkeypatch):
    import src.server as server

    cfg = _cfg()
    messages = []
    finished = []
    monkeypatch.setattr(
        server,
        "_refresh_runtime_source",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("reload failed")),
    )
    monkeypatch.setattr(
        server,
        "_send_wechat_text",
        lambda _wechat, text, user=None: messages.append(text),
    )
    monkeypatch.setattr(server, "_finish_manual_qr_command", lambda ok, msg: finished.append((ok, msg)))

    server._finish_manual_qr_with_refresh(
        cfg, "config.yaml", "user-1", True, "扫码续期任务完成"
    )

    assert any("需要重启服务" in text for text in messages)
    assert finished == [(False, "运行时配置同步失败: reload failed")]
```

- [ ] **Step 3: 扩展自动日报恢复测试**

用以下完整测试替换 `test_expired_cookie_resumes_once_with_reloaded_config`：

```python
def test_expired_cookie_resumes_once_with_reloaded_config(monkeypatch):
    import src.config as config_module
    import src.cookies_checker as cookies_checker
    import src.report_builder as report_builder
    import src.server as server

    old_cfg = _cfg()
    old_cfg.source = SimpleNamespace(TOK="old")
    fresh_cfg = _cfg()
    fresh_cfg.source = SimpleNamespace(TOK="new")
    callback = {}
    builds = []
    submits = []
    runtime_updates = []

    def expired(_cfg):
        raise cookies_checker.CookiesError("expired")

    def start_renew(*args, **kwargs):
        callback["fn"] = kwargs["on_complete"]
        return True

    monkeypatch.setattr(cookies_checker, "check_cookies", expired)
    monkeypatch.setattr(server, "start_renew_cookies_by_qr", start_renew)
    monkeypatch.setattr(config_module, "load_config", lambda _path: fresh_cfg)
    monkeypatch.setattr("src.scheduler.update_runtime_config", runtime_updates.append)
    monkeypatch.setattr(
        report_builder,
        "build_report_with_meta",
        lambda value: builds.append(value) or (
            "正文",
            "smart_sheet",
            {"smart_doc_status": "normal"},
        ),
    )
    monkeypatch.setattr(
        server,
        "_submit_and_notify",
        lambda content, value, source, meta: submits.append((content, value)) or True,
    )

    assert server.auto_submit_if_needed(old_cfg) is False
    assert builds == []

    workers = [
        threading.Thread(target=callback["fn"], args=(True, "ok"))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    callback["fn"](True, "duplicate")

    assert builds == [old_cfg]
    assert submits == [("正文", old_cfg)]
    assert old_cfg.source.TOK == "new"
    assert runtime_updates == [old_cfg]
```

- [ ] **Step 4: 写入定时扫码完成后的刷新测试**

创建 `tests/test_scheduler_cookie_recovery.py`：

```python
from types import SimpleNamespace


def _cfg():
    return SimpleNamespace(
        source=SimpleNamespace(TOK="old"),
        wechat=SimpleNamespace(to_user="user-1"),
    )


def test_scheduled_qr_success_refreshes_runtime(monkeypatch):
    import src.scheduler as scheduler

    cfg = _cfg()
    callback = {}
    refreshed = []
    updates = []
    messages = []

    monkeypatch.setattr(
        "src.qr_login_renewer.start_renew_cookies_by_qr",
        lambda *args, **kwargs: callback.setdefault("fn", kwargs["on_complete"]) or True,
    )
    monkeypatch.setattr(
        "src.config.refresh_source_config",
        lambda value, path: refreshed.append((value, path)) or value,
    )
    monkeypatch.setattr(scheduler, "update_runtime_config", updates.append)
    monkeypatch.setattr(
        "src.wechat_notifier.send_text",
        lambda _wechat, text, user=None: messages.append(text),
    )

    scheduler._start_qr_renew(cfg, "expired")
    callback["fn"](True, "扫码续期任务完成")

    assert refreshed and refreshed[0][0] is cfg
    assert updates == [cfg]
    assert any("运行时配置已同步" in text for text in messages)


def test_scheduled_qr_failure_does_not_refresh_runtime(monkeypatch):
    import src.scheduler as scheduler

    cfg = _cfg()
    callback = {}
    monkeypatch.setattr(
        "src.qr_login_renewer.start_renew_cookies_by_qr",
        lambda *args, **kwargs: callback.setdefault("fn", kwargs["on_complete"]) or True,
    )
    monkeypatch.setattr(
        "src.config.refresh_source_config",
        lambda *_args: (_ for _ in ()).throw(AssertionError("不应刷新")),
    )

    scheduler._start_qr_renew(cfg, "expired")
    callback["fn"](False, "扫码超时")
```

- [ ] **Step 5: 运行测试并确认红灯**

Run:

```powershell
python -m pytest -q tests/test_report_recovery.py tests/test_scheduler_cookie_recovery.py -k "refresh_source or qr_success or qr_refresh or expired_cookie_resumes"
```

Expected: 因 `refresh_source_config`、`_refresh_runtime_source`、`_finish_manual_qr_with_refresh` 不存在以及定时续期未提供回调而失败。

- [ ] **Step 6: 实现配置原地刷新**

在 `src/config.py` 的 `load_config()` 后加入：

```python
def refresh_source_config(current_cfg: Config, path: str) -> Config:
    fresh_cfg = load_config(path)
    current_cfg.source = fresh_cfg.source
    return current_cfg
```

- [ ] **Step 7: 实现服务器刷新与手动扫码完成函数**

在 `src/server.py` 的 `_finish_manual_qr_command()` 后加入：

```python
def _refresh_runtime_source(cfg: Config, config_path: str) -> Config:
    from src.config import refresh_source_config
    from src.scheduler import update_runtime_config

    refresh_source_config(cfg, config_path)
    update_runtime_config(cfg)
    return cfg


def _finish_manual_qr_with_refresh(
    cfg: Config,
    config_path: str,
    to_user: str,
    success: bool,
    message: str,
) -> None:
    final_success = success
    final_message = message
    try:
        if success:
            _refresh_runtime_source(cfg, config_path)
            _send_wechat_text(
                cfg.wechat,
                "✅ Cookies 运行时配置已同步，无需重启服务。",
                to_user,
            )
    except Exception as exc:
        final_success = False
        final_message = f"运行时配置同步失败: {exc}"
        logger.error(final_message, exc_info=True)
        _send_wechat_text(
            cfg.wechat,
            f"⚠️ Cookies 已验证并写入配置，但运行时同步失败，需要重启服务。\n{exc}",
            to_user,
        )
    finally:
        _finish_manual_qr_command(final_success, final_message)
```

把手动二维码入口的 `on_complete` 改为：

```python
                        on_complete=lambda success, message: _finish_manual_qr_with_refresh(
                            cfg,
                            config_path,
                            from_user_id,
                            success,
                            message,
                        ),
```

- [ ] **Step 8: 让自动日报恢复和手工 Cookie 更新复用刷新函数**

在 `_start_report_cookie_renewal()` 的成功回调中，用以下逻辑替换“加载 `notify_cfg` 后只恢复一次提交”的代码：

```python
        try:
            _refresh_runtime_source(cfg, config_path)
            auto_submit_if_needed(cfg, precheck_cookies=False)
        except Exception as exc:
            logger.error(
                "Cookie renewal succeeded but runtime refresh failed: %s",
                exc,
                exc_info=True,
            )
            _send_wechat_text(
                cfg.wechat,
                f"⚠️ Cookies 已写入配置，但运行时同步失败，需要重启服务。\n今日日报尚未提交。\n{exc}",
            )
```

在企业微信文本 Cookie 更新成功且 `updated_fields` 非空后加入：

```python
                            if success and updated_fields:
                                try:
                                    _refresh_runtime_source(cfg, config_path)
                                    _send_wechat_text(
                                        cfg.wechat,
                                        "✅ Cookies 运行时配置已同步，无需重启服务。",
                                        from_user_id,
                                    )
                                except Exception as exc:
                                    _send_wechat_text(
                                        cfg.wechat,
                                        f"⚠️ Cookies 已写入配置，但运行时同步失败，需要重启服务。\n{exc}",
                                        from_user_id,
                                    )
```

- [ ] **Step 9: 给定时二维码续期增加完成回调**

在 `scheduler._start_qr_renew()` 内传入：

```python
        def on_complete(success: bool, message: str) -> None:
            if not success:
                return
            try:
                from src.config import refresh_source_config
                from src.wechat_notifier import send_text

                refresh_source_config(cfg, config_path)
                update_runtime_config(cfg)
                send_text(
                    cfg.wechat,
                    "✅ Cookies 运行时配置已同步，无需重启服务。",
                    getattr(cfg.wechat, "to_user", None),
                )
            except Exception as exc:
                logger.error("Refresh runtime source after QR renewal failed: %s", exc, exc_info=True)
                from src.wechat_notifier import send_text
                send_text(
                    cfg.wechat,
                    f"⚠️ Cookies 已写入配置，但运行时同步失败，需要重启服务。\n{exc}",
                    getattr(cfg.wechat, "to_user", None),
                )

        start_renew_cookies_by_qr(
            config_path,
            getattr(cfg.wechat, "to_user", None),
            reason,
            on_complete=on_complete,
        )
```

- [ ] **Step 10: 修正二维码工作进程成功文案**

将 `src/qr_login_renewer.py` 成功消息末行改为：

```python
新 Cookies 已通过智能文档验证并写入配置，正在同步服务运行时配置。""",
```

同时把 `src/wechat_notifier.py::notify_cookies_valid()` 的末行改为：

```python
新 Cookies 已通过验证并写入配置，正在同步服务运行时配置。
```

- [ ] **Step 11: 运行 Task 1 测试**

Run:

```powershell
python -m pytest -q tests/test_report_recovery.py tests/test_scheduler_cookie_recovery.py
```

Expected: 全部通过；并发完成回调只恢复一次日报。

- [ ] **Step 12: 提交运行时刷新修复**

```powershell
git add -- src/config.py src/server.py src/scheduler.py src/qr_login_renewer.py tests/test_report_recovery.py tests/test_scheduler_cookie_recovery.py
git commit -m "fix: refresh runtime cookies after renewal"
```

### Task 2: 网络超时重试并与 Cookie 失效分流

**Files:**
- Modify: `src/cookies_checker.py`
- Modify: `src/scheduler.py:235-257`
- Modify: `src/server.py:2280-2318`
- Modify: `src/wechat_notifier.py:258-302`
- Modify: `src/notifier.py:1-46`
- Create: `tests/test_cookies_checker.py`
- Modify: `tests/test_scheduler_cookie_recovery.py`
- Modify: `tests/test_report_recovery.py`

**Interfaces:**
- Produces: `CookiesNetworkError(CookiesError)`。
- Produces: `cookies_checker._check_cookies_once(cfg) -> bool`。
- Keeps: `cookies_checker.check_cookies(cfg) -> bool`，签名不变，最多执行两次完整检查。
- Produces: `notify_cookies_network_error(cfg, error: str) -> None`。

- [ ] **Step 1: 写入检查器重试与异常类型测试**

创建 `tests/test_cookies_checker.py`：

```python
import pytest


def test_network_timeout_retries_once_then_succeeds(monkeypatch):
    import src.cookies_checker as checker

    calls = []

    def check_once(cfg):
        calls.append(cfg)
        if len(calls) == 1:
            raise checker.CookiesNetworkError("timeout")
        return True

    monkeypatch.setattr(checker, "_check_cookies_once", check_once)
    cfg = object()

    assert checker.check_cookies(cfg) is True
    assert calls == [cfg, cfg]


def test_two_network_timeouts_raise_network_error(monkeypatch):
    import src.cookies_checker as checker

    calls = []

    def check_once(cfg):
        calls.append(cfg)
        raise checker.CookiesNetworkError("timeout")

    monkeypatch.setattr(checker, "_check_cookies_once", check_once)

    with pytest.raises(checker.CookiesNetworkError, match="timeout"):
        checker.check_cookies(object())

    assert len(calls) == 2


def test_auth_failure_does_not_retry(monkeypatch):
    import src.cookies_checker as checker

    calls = []

    def check_once(cfg):
        calls.append(cfg)
        raise checker.CookiesError("redirected to login page")

    monkeypatch.setattr(checker, "_check_cookies_once", check_once)

    with pytest.raises(checker.CookiesError, match="login"):
        checker.check_cookies(object())

    assert len(calls) == 1
```

- [ ] **Step 2: 写入调度器网络异常分流测试**

在 `tests/test_scheduler_cookie_recovery.py` 继续加入：

```python
def test_scheduled_network_error_does_not_start_qr(monkeypatch):
    import src.cookies_checker as checker
    import src.scheduler as scheduler

    cfg = _cfg()
    notices = []
    monkeypatch.setattr(
        checker,
        "check_cookies",
        lambda _cfg: (_ for _ in ()).throw(checker.CookiesNetworkError("timeout")),
    )
    monkeypatch.setattr(
        "src.notifier.notify_cookies_network_error",
        lambda value, error: notices.append((value, error)),
    )
    monkeypatch.setattr(
        scheduler,
        "_start_qr_renew",
        lambda *_args: (_ for _ in ()).throw(AssertionError("网络异常不应生成二维码")),
    )

    scheduler._run_cookies_check(cfg)

    assert notices == [(cfg, "timeout")]


def test_scheduled_auth_error_still_starts_qr(monkeypatch):
    import src.cookies_checker as checker
    import src.scheduler as scheduler

    cfg = _cfg()
    renewals = []
    monkeypatch.setattr(
        checker,
        "check_cookies",
        lambda _cfg: (_ for _ in ()).throw(checker.CookiesError("login required")),
    )
    monkeypatch.setattr("src.notifier.notify_cookies_expired", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_start_qr_renew", lambda *args: renewals.append(args))

    scheduler._run_cookies_check(cfg)

    assert len(renewals) == 1
    assert renewals[0][0] is cfg
```

- [ ] **Step 3: 写入日报提交前网络异常测试**

在 `tests/test_report_recovery.py` 加入：

```python
def test_auto_submit_network_error_does_not_start_qr_or_submit(monkeypatch):
    import src.cookies_checker as checker
    import src.report_builder as report_builder
    import src.server as server

    cfg = _cfg()
    failures = []
    monkeypatch.setattr(
        checker,
        "check_cookies",
        lambda _cfg: (_ for _ in ()).throw(checker.CookiesNetworkError("timeout")),
    )
    monkeypatch.setattr(
        server,
        "start_renew_cookies_by_qr",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应生成二维码")),
    )
    monkeypatch.setattr(
        report_builder,
        "build_report_with_meta",
        lambda _cfg: (_ for _ in ()).throw(AssertionError("不应读取文档")),
    )
    monkeypatch.setattr(server, "notify_report_failure", lambda *args, **kwargs: failures.append((args, kwargs)))

    assert server.auto_submit_if_needed(cfg) is False
    assert len(failures) == 1
    assert "网络异常" in failures[0][0][1]
    assert "未判定 Cookies 失效" in failures[0][0][1]
```

- [ ] **Step 4: 运行测试并确认红灯**

Run:

```powershell
python -m pytest -q tests/test_cookies_checker.py tests/test_scheduler_cookie_recovery.py tests/test_report_recovery.py -k "network or auth_failure"
```

Expected: `CookiesNetworkError` 和 `_check_cookies_once` 不存在，且现有调用方仍把超时走入二维码分支。

- [ ] **Step 5: 拆分单次检查并实现一次重试**

在 `src/cookies_checker.py` 中把现有 `check_cookies()` 函数体改名为 `_check_cookies_once()`，并加入：

```python
class CookiesError(Exception):
    pass


class CookiesNetworkError(CookiesError):
    pass


def check_cookies(cfg: Config) -> bool:
    for attempt in range(2):
        try:
            return _check_cookies_once(cfg)
        except CookiesNetworkError:
            if attempt == 1:
                raise
            logger.warning("Smart sheet network check timed out; retrying once")
    return False
```

在 `_check_cookies_once()` 页面加载三秒后、等待 SDK 前加入身份页检查：

```python
                page_text = page.inner_text("body")[:500]
                if "login" in page.url.lower() or "登录" in page_text:
                    raise CookiesError("Cookies expired: redirected to login page")
                if "无权限" in page_text or "没有权限" in page_text:
                    raise CookiesError("Cookies expired: no permission to access document")
```

删除函数后部重复读取正文的代码；保留 `result.get("ok")` 成功判断。

将异常处理替换为：

```python
            except PlaywrightTimeout as exc:
                raise CookiesNetworkError(
                    "Smart sheet page or SDK load timed out"
                ) from exc
            except CookiesError:
                raise
            except Exception as exc:
                raise CookiesNetworkError(
                    f"Smart sheet access failed: {exc}"
                ) from exc
```

- [ ] **Step 6: 增加网络异常专用企微通知**

在 `src/wechat_notifier.py` 的 `notify_cookies_expired()` 前加入：

```python
def notify_cookies_network_error(cfg, error: str):
    if not _is_configured(cfg):
        return
    now = beijing_now()
    send_time = now.strftime("%Y-%m-%d %H:%M:%S")
    md_content = f"""## ⚠️ 智能文档访问异常

> **检测状态**：网络或页面加载异常
> **检测时间**：{send_time}
> **Cookie 判断**：未判定失效
> **自动续期**：未启动

### 错误详情
```text
{error}
```

系统已自动重试一次。请稍后再次检查；只有明确登录失效时才会生成扫码二维码。
"""
    send_markdown(cfg.wechat, md_content)
```

在 `src/notifier.py` 导入并包装：

```python
    notify_cookies_network_error as wechat_notify_cookies_network_error,
```

```python
def notify_cookies_network_error(cfg, error: str):
    if _has_wechat_config(cfg):
        wechat_notify_cookies_network_error(cfg, error)
```

- [ ] **Step 7: 调度器先捕获网络异常**

将 `_run_cookies_check()` 的导入和异常顺序改为：

```python
    from src.cookies_checker import check_cookies, CookiesError, CookiesNetworkError
    from src.notifier import notify_cookies_expired, notify_cookies_network_error

    try:
        check_cookies(cfg)
        logger.info("Cookies check passed")
    except CookiesNetworkError as exc:
        logger.error("Smart sheet network check failed after retry: %s", exc)
        notify_cookies_network_error(cfg, str(exc))
        return
    except CookiesError as exc:
        logger.error("Cookies check failed: %s", exc)
        notify_cookies_expired(cfg, f"Cookies 过期或无效: {exc}")
        _start_qr_renew(cfg, f"自动检测到 Cookies 过期或无效: {exc}")
        return
```

普通未知异常使用以下分支，不得生成二维码：

```python
    except Exception as exc:
        logger.error("Cookies check error: %s", exc, exc_info=True)
        notify_cookies_network_error(cfg, f"Cookies 检查异常: {exc}")
        return
```

- [ ] **Step 8: 日报提交前先捕获网络异常**

在 `server.auto_submit_if_needed()` 的 Cookie 预检中改为：

```python
        from src.cookies_checker import CookiesError, CookiesNetworkError, check_cookies

        try:
            check_cookies(cfg)
        except CookiesNetworkError as exc:
            logger.warning("Report submission smart sheet network check failed: %s", exc)
            notify_report_failure(
                cfg,
                f"智能文档网络异常，系统已重试一次；未判定 Cookies 失效，未生成二维码。错误：{exc}",
                report_source="generation_failed",
                smart_doc_status="error",
                smart_doc_error=str(exc),
            )
            return False
        except CookiesError as exc:
            logger.warning("Report submission Cookie preflight failed: %s", exc)
            _start_report_cookie_renewal(cfg, exc)
            return False
```

- [ ] **Step 9: 运行 Task 2 测试**

Run:

```powershell
python -m pytest -q tests/test_cookies_checker.py tests/test_scheduler_cookie_recovery.py tests/test_report_recovery.py
```

Expected: 全部通过，网络异常路径没有二维码调用。

- [ ] **Step 10: 提交超时分类修复**

```powershell
git add -- src/cookies_checker.py src/scheduler.py src/server.py src/wechat_notifier.py src/notifier.py tests/test_cookies_checker.py tests/test_scheduler_cookie_recovery.py tests/test_report_recovery.py
git commit -m "fix: separate smart doc network timeouts"
```

### Task 3: 完整验证与交付

**Files:**
- Verify: `src/config.py`
- Verify: `src/server.py`
- Verify: `src/scheduler.py`
- Verify: `src/qr_login_renewer.py`
- Verify: `src/cookies_checker.py`
- Verify: `src/notifier.py`
- Verify: `src/wechat_notifier.py`
- Verify: `tests/test_report_recovery.py`
- Verify: `tests/test_cookies_checker.py`
- Verify: `tests/test_scheduler_cookie_recovery.py`

**Interfaces:**
- Consumes: Task 1 和 Task 2 的全部提交。
- Produces: 本地可部署修复及测试证据，不修改生产环境。

- [ ] **Step 1: 运行专项测试**

Run:

```powershell
python -m pytest -q tests/test_report_recovery.py tests/test_cookies_checker.py tests/test_scheduler_cookie_recovery.py
```

Expected: 全部通过。

- [ ] **Step 2: 运行完整测试集**

Run:

```powershell
python -m pytest -q
```

Expected: 退出码 0，无失败。

- [ ] **Step 3: 编译变更模块**

Run:

```powershell
python -m py_compile src/config.py src/server.py src/scheduler.py src/qr_login_renewer.py src/cookies_checker.py src/notifier.py src/wechat_notifier.py
```

Expected: 退出码 0，无输出。

- [ ] **Step 4: 检查差异与提交边界**

Run:

```powershell
git diff --check HEAD~2..HEAD
git status --short
git log -3 --oneline
```

Expected: 差异检查无输出；最近两条实现提交分别为运行时刷新和网络超时分类；原有未跟踪缓存、图片、PDF 和日志未进入提交。

- [ ] **Step 5: 报告本地结果并等待部署授权**

报告必须明确：

```text
续期后：新 Cookie 已同步 Flask 与调度器，无需重启。
网络超时：完整重试一次，仍失败只发网络异常通知，不生成二维码。
真实失效：登录跳转或无权限仍进入扫码续期。
生产环境：尚未修改；部署需单独授权。
```

不得在没有用户明确部署授权时上传文件、重启服务或执行线上 Cookie 操作。
