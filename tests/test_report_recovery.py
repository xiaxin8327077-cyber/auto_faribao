from types import SimpleNamespace
from contextlib import contextmanager
import threading

import pytest


@contextmanager
def _null_cm():
    yield


def test_qr_background_completion_reports_success(monkeypatch):
    import src.qr_login_renewer as qr

    monkeypatch.setattr(qr, "renew_cookies_by_qr", lambda *args: (True, "续期成功"))
    completed = []

    qr._run_renew_in_background(
        "config.yaml", "user-1", "test", lambda ok, message: completed.append((ok, message))
    )

    assert completed == [(True, "续期成功")]


def test_qr_start_failure_calls_completion_callback(monkeypatch):
    import src.qr_login_renewer as qr

    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start")

    monkeypatch.setattr(qr.threading, "Thread", BrokenThread)
    completed = []

    started = qr.start_renew_cookies_by_qr(
        "config.yaml", on_complete=lambda ok, message: completed.append((ok, message))
    )

    assert started is False
    assert completed == [(False, "cannot start")]


def test_manual_qr_completion_releases_command_state_when_logging_fails(monkeypatch):
    import src.server as server

    server._end_cmd()
    assert server._try_start_cmd("生成二维码") is True
    monkeypatch.setattr(server.logger, "info", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("log")))

    with pytest.raises(RuntimeError, match="log"):
        server._finish_manual_qr_command(False, "超时")

    assert server._try_start_cmd("下一条指令") is True
    server._end_cmd()


def _cfg():
    return SimpleNamespace(wechat=SimpleNamespace(to_user="user-1"))


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


def test_auto_submit_checks_cookies_then_submits(monkeypatch):
    import src.cookies_checker as cookies_checker
    import src.report_builder as report_builder
    import src.server as server

    cfg = _cfg()
    calls = []
    monkeypatch.setattr(cookies_checker, "check_cookies", lambda value: calls.append(("check", value)))
    monkeypatch.setattr(
        report_builder,
        "build_report_with_meta",
        lambda value: calls.append(("build", value)) or ("正文", "smart_sheet", {"smart_doc_status": "normal"}),
    )
    monkeypatch.setattr(
        server,
        "_submit_and_notify",
        lambda content, value, source, meta: calls.append(("submit", content, value)) or True,
    )
    monkeypatch.setattr(server, "start_renew_cookies_by_qr", lambda *args, **kwargs: pytest.fail("不应续期"))

    assert server.auto_submit_if_needed(cfg) is True
    assert calls == [("check", cfg), ("build", cfg), ("submit", "正文", cfg)]


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


def test_auto_submit_network_error_does_not_start_qr_or_submit(monkeypatch):
    import src.cookies_checker as cookies_checker
    import src.report_builder as report_builder
    import src.server as server

    cfg = _cfg()
    failures = []
    monkeypatch.setattr(
        cookies_checker,
        "check_cookies",
        lambda _cfg: (_ for _ in ()).throw(cookies_checker.CookiesNetworkError("timeout")),
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


def test_expired_cookie_failure_does_not_build_or_submit(monkeypatch):
    import src.cookies_checker as cookies_checker
    import src.report_builder as report_builder
    import src.server as server

    cfg = _cfg()
    callback = {}
    messages = []
    monkeypatch.setattr(
        cookies_checker,
        "check_cookies",
        lambda _cfg: (_ for _ in ()).throw(cookies_checker.CookiesError("expired")),
    )
    monkeypatch.setattr(
        server,
        "start_renew_cookies_by_qr",
        lambda *args, **kwargs: callback.setdefault("fn", kwargs["on_complete"]) and True,
    )
    monkeypatch.setattr(report_builder, "build_report_with_meta", lambda _cfg: pytest.fail("不应读取文档"))
    monkeypatch.setattr(server, "_submit_and_notify", lambda *args: pytest.fail("不应提交"))
    monkeypatch.setattr(server, "_send_wechat_text", lambda _wechat, text, *args: messages.append(text))

    assert server.auto_submit_if_needed(cfg) is False
    callback["fn"](False, "扫码超时")

    assert any("今日日报尚未提交" in text for text in messages)


def test_existing_renewal_guidance_is_not_sent_after_synchronous_failure(monkeypatch):
    import src.cookies_checker as cookies_checker
    import src.server as server

    cfg = _cfg()
    messages = []
    monkeypatch.setattr(
        cookies_checker,
        "check_cookies",
        lambda _cfg: (_ for _ in ()).throw(cookies_checker.CookiesError("expired")),
    )

    def start_failure(*args, **kwargs):
        kwargs["on_complete"](False, "cannot start")
        return False

    monkeypatch.setattr(server, "start_renew_cookies_by_qr", start_failure)
    monkeypatch.setattr(server, "_send_wechat_text", lambda _wechat, text, *args: messages.append(text))

    assert server.auto_submit_if_needed(cfg) is False
    assert any("今日日报尚未提交" in text for text in messages)
    assert not any("当前续期任务完成后" in text for text in messages)


class _TextNode:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class _FormItem:
    def __init__(self, label, content):
        self.label = _TextNode(label)
        self.content = _TextNode(content)

    def query_selector(self, selector):
        if selector == ".el-form-item__label":
            return self.label
        if selector == ".el-form-item__content":
            return self.content
        return None


class _ExpandedElement:
    def __init__(self, items=(), text=""):
        self.items = list(items)
        self.text = text

    def query_selector_all(self, selector):
        return self.items if selector == ".el-form-item" else []

    def inner_text(self):
        return self.text


def test_approved_expanded_row_reads_only_structured_detail():
    from src.target import _extract_detail_from_expanded_row

    element = _ExpandedElement([
        _FormItem("详情：", "1. 修复生产问题\n2. 完成回归验证"),
        _FormItem("审核说明", "审核通过"),
        _FormItem("审核时间", "2026-07-08 00:00:00"),
    ])

    assert _extract_detail_from_expanded_row(element) == "1. 修复生产问题\n2. 完成回归验证"


def test_previous_report_cutoff_excludes_today_and_legacy_parser_remains_available(monkeypatch):
    import src.beijing_time
    from src.target import _extract_detail_from_expanded_row, _is_previous_report_date, _previous_report_cutoff

    monkeypatch.setattr(src.beijing_time, "today_str", lambda: "2026-07-10")
    assert _previous_report_cutoff(None) == "2026-07-10"
    assert _is_previous_report_date("2026-07-09", "2026-07-10") is True
    assert _is_previous_report_date("2026-07-10", "2026-07-10") is False

    legacy = _ExpandedElement(text="详情\n1. 旧版日报正文足够长\n审核说明\n已通过\n审核时间\n2026-07-08")
    assert _extract_detail_from_expanded_row(legacy) == "1. 旧版日报正文足够长\n已通过\n2026-07-08"


def test_finds_report_content_on_later_oa_page(monkeypatch):
    from src import target

    page = SimpleNamespace(wait_for_timeout=lambda *_: None)
    rows = [None, object()]
    monkeypatch.setattr(target, "_find_existing_report_row", lambda _page, _date: rows.pop(0))
    monkeypatch.setattr(target, "_go_to_next_page", lambda _page: bool(rows))
    monkeypatch.setattr(target, "_read_report_content_from_row", lambda _page, _row: "目标日报正文")

    assert target._find_report_content_on_pages(page, "2026-07-08") == "目标日报正文"


def test_returns_empty_when_matching_oa_row_has_no_readable_content(monkeypatch):
    from src import target

    page = SimpleNamespace(wait_for_timeout=lambda *_: None)
    monkeypatch.setattr(target, "_find_existing_report_row", lambda _page, _date: object())
    monkeypatch.setattr(target, "_read_report_content_from_row", lambda _page, _row: "")

    assert target._find_report_content_on_pages(page, "2026-07-08") == ""


def test_same_cookie_after_network_timeout_does_not_sync_runtime(monkeypatch, tmp_path):
    """网络超时后Cookie留盘，再发相同Cookie不应跳过验证同步运行时。"""
    import yaml
    import src.auto_cookies_updater as updater
    import src.cookies_checker as checker

    config_path = tmp_path / "config.yaml"
    config_data = {
        "source": {"TOK": "xxx", "doc_id": "d", "scode": "s", "tab_id": "t", "view_id": "v"},
    }
    config_path.write_text(yaml.dump(config_data), encoding="utf-8")

    # 磁盘已有相同Cookie → _get_updated_fields 返回 []
    # 验证时抛网络超时
    monkeypatch.setattr(checker, "check_cookies", lambda _cfg: (_ for _ in ()).throw(checker.CookiesNetworkError("timeout")))
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_valid", lambda *_args: None)
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_invalid", lambda *_args: None)

    success, fields, msg = updater.update_cookies_from_wechat(str(config_path), "TOK=xxx", object())

    assert success is False
    assert fields == []
    assert "网络" in msg or "超时" in msg


def test_same_cookie_verified_refreshes_runtime(monkeypatch, tmp_path):
    """同值Cookie验证成功后应刷新运行时配置。"""
    import yaml
    import src.server as server

    config_path = tmp_path / "config.yaml"
    config_data = {
        "source": {"TOK": "xxx", "doc_id": "d", "scode": "s", "tab_id": "t", "view_id": "v"},
    }
    config_path.write_text(yaml.dump(config_data), encoding="utf-8")

    cfg = _cfg()
    messages = []
    refreshed = []
    monkeypatch.setattr(server, "update_cookies_from_wechat", lambda *_args: (True, [], ""))
    monkeypatch.setattr(server, "_refresh_runtime_source", lambda c, p: refreshed.append((c, p)) or c)
    monkeypatch.setattr(server, "_send_wechat_text", lambda _w, text, *args: messages.append(text))
    monkeypatch.setattr(server, "_end_cmd", lambda: None)

    server._process_cookies_update(cfg, str(config_path), "TOK=xxx", "user-1")

    assert len(refreshed) == 1
    assert refreshed[0][0] is cfg
    assert any("运行时配置已同步" in m for m in messages)


def test_cookies_update_failure_sends_error_msg_to_user(monkeypatch, tmp_path):
    """update_cookies_from_wechat 返回 False 时用户应通过真实入口收到 error_msg。"""
    import src.server as server

    cfg = _cfg()
    messages = []
    monkeypatch.setattr(
        server,
        "update_cookies_from_wechat",
        lambda *_args: (False, [], "Cookies无效，已回滚配置"),
    )
    monkeypatch.setattr(server, "_send_wechat_text", lambda _w, text, *args: messages.append(text))
    monkeypatch.setattr(server, "_end_cmd", lambda: None)

    server._process_cookies_update(cfg, str(tmp_path / "config.yaml"), "TOK=xxx", "user-1")

    assert any("Cookies 更新失败" in m and "已回滚" in m for m in messages)


def test_network_timeout_reverts_disk_cookies(monkeypatch, tmp_path):
    """网络超时后磁盘Cookie应回滚，重启不会加载未验证值。"""
    import yaml
    import src.auto_cookies_updater as updater
    import src.cookies_checker as checker

    config_path = tmp_path / "config.yaml"
    old_data = {
        "source": {"TOK": "old_val", "doc_id": "d", "scode": "s", "tab_id": "t", "view_id": "v"},
    }
    config_path.write_text(yaml.dump(old_data), encoding="utf-8")

    monkeypatch.setattr(checker, "check_cookies", lambda _cfg: (_ for _ in ()).throw(checker.CookiesNetworkError("timeout")))
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_valid", lambda *_args: None)
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_invalid", lambda *_args: None)

    success, fields, msg = updater.update_cookies_from_wechat(str(config_path), "TOK=new_val", object())

    assert success is False
    assert "已回滚" in msg

    # 磁盘应恢复旧值
    disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert disk["source"]["TOK"] == "old_val"


def test_revert_failure_does_not_claim_rolled_back(monkeypatch, tmp_path):
    """_revert_config 回滚失败时，不应宣称已回滚。"""
    import yaml
    import src.auto_cookies_updater as updater
    import src.cookies_checker as checker

    config_path = tmp_path / "config.yaml"
    old_data = {
        "source": {"TOK": "old_val", "doc_id": "d", "scode": "s", "tab_id": "t", "view_id": "v"},
    }
    config_path.write_text(yaml.dump(old_data), encoding="utf-8")

    # check_cookies 抛网络超时触发回滚
    monkeypatch.setattr(checker, "check_cookies", lambda _cfg: (_ for _ in ()).throw(checker.CookiesNetworkError("timeout")))
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_valid", lambda *_args: None)
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_invalid", lambda *_args: None)
    # 注入回滚失败（模拟真实_revert_config内部异常被捕获后返回False）
    monkeypatch.setattr(updater, "_revert_config", lambda *_args: False)

    success, fields, msg = updater.update_cookies_from_wechat(str(config_path), "TOK=new_val", object())

    assert success is False
    assert "已回滚" not in msg
    assert "回滚失败" in msg or "回滚" in msg


def test_browser_close_exception_does_not_mask_login_failure(monkeypatch):
    """browser.close() 抛异常时不应覆盖已识别的登录失效。"""
    import src.cookies_checker as checker

    raised_exceptions = []

    class FakeBrowser:
        def new_context(self, **kwargs):
            raise checker.CookiesError("Cookies expired: redirected to login page")

        def close(self):
            raise RuntimeError("browser already crashed")

    class FakePlaywright:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @property
        def chromium(self):
            return self

        def launch(self, **kwargs):
            return FakeBrowser()

    monkeypatch.setattr(checker, "browser_operation", lambda: _null_cm())
    monkeypatch.setattr(checker, "sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(checker, "launch_browser", lambda p: p.chromium.launch())

    cfg = SimpleNamespace(
        source=SimpleNamespace(doc_id="d", scode="s", tab_id="t", view_id="v", TOK="x")
    )

    try:
        checker._check_cookies_once(cfg)
    except checker.CookiesError as e:
        raised_exceptions.append(e)
    except checker.CookiesNetworkError as e:
        raised_exceptions.append(e)
    except Exception as e:
        raised_exceptions.append(e)

    assert len(raised_exceptions) == 1
    assert isinstance(raised_exceptions[0], checker.CookiesError)
    assert "login" in str(raised_exceptions[0]).lower()
