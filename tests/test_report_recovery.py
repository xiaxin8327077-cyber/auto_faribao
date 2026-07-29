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


def test_empty_snapshot_revert_returns_false_without_touching_disk(tmp_path):
    """空快照不能被当成成功回滚，也不能改动磁盘。"""
    import src.auto_cookies_updater as updater

    config_path = tmp_path / "config.yaml"
    original = "source:\n  TOK: current\n"
    config_path.write_text(original, encoding="utf-8")

    assert updater._revert_config(str(config_path), {}) is False
    assert config_path.read_text(encoding="utf-8") == original


def test_snapshot_failure_prevents_write_and_does_not_claim_rollback(monkeypatch, tmp_path):
    """旧Cookie快照读取失败时不应写入新Cookie，也不应谎报已回滚。"""
    import yaml
    import src.auto_cookies_updater as updater
    import src.cookies_checker as checker

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "source": {"TOK": "old_val", "doc_id": "d", "scode": "s", "tab_id": "t", "view_id": "v"},
    }), encoding="utf-8")

    # 快照读取失败 → 返回 None
    monkeypatch.setattr(updater, "_get_current_cookie_values", lambda *_args: None)
    write_calls = []
    monkeypatch.setattr(updater, "update_config_cookies", lambda *_args: write_calls.append(1) or True)
    monkeypatch.setattr(checker, "check_cookies", lambda _cfg: True)
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_valid", lambda *_args: None)

    success, fields, msg = updater.update_cookies_from_wechat(str(config_path), "TOK=new_val", object())

    assert success is False
    assert write_calls == [], "快照失败时不应写入新Cookie"
    assert "快照" in msg or "读取" in msg or "无法" in msg


def test_cookie_diff_read_failure_aborts_without_write_or_validation(monkeypatch):
    """读取现有配置失败时，不得当成Cookie未变化并继续报成功。"""
    import src.auto_cookies_updater as updater
    import src.cookies_checker as checker

    write_calls = []
    validation_calls = []
    monkeypatch.setattr(updater, "_get_updated_fields", lambda *_args: None)
    monkeypatch.setattr(updater, "update_config_cookies", lambda *_args: write_calls.append(1) or True)
    monkeypatch.setattr(checker, "check_cookies", lambda *_args: validation_calls.append(1) or True)

    success, fields, msg = updater.update_cookies_from_wechat(
        "unreadable-config.yaml",
        "TOK=new_val",
        object(),
    )

    assert success is False
    assert fields == []
    assert write_calls == []
    assert validation_calls == []
    assert "读取" in msg and "未修改" in msg


def test_incomplete_cookie_snapshot_prevents_write(monkeypatch, tmp_path):
    """旧值快照未覆盖全部待更新字段时不得写盘。"""
    import src.auto_cookies_updater as updater

    write_calls = []
    monkeypatch.setattr(updater, "_get_updated_fields", lambda *_args: ["TOK", "traceid"])
    monkeypatch.setattr(updater, "_get_current_cookie_values", lambda *_args: {"TOK": "old"})
    monkeypatch.setattr(updater, "update_config_cookies", lambda *_args: write_calls.append(1) or True)

    success, fields, msg = updater.update_cookies_from_wechat(
        str(tmp_path / "config.yaml"),
        "TOK=new; traceid=new-trace",
        object(),
    )

    assert success is False
    assert fields == []
    assert write_calls == []
    assert "快照" in msg and "未修改" in msg


def test_unexpected_verification_error_rolls_back_and_reports_failure(monkeypatch, tmp_path):
    """无法确认Cookie有效的未知异常不得被当成更新成功。"""
    import yaml
    import src.auto_cookies_updater as updater
    import src.cookies_checker as checker

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "source": {"TOK": "old_val", "doc_id": "d", "scode": "s", "tab_id": "t", "view_id": "v"},
    }), encoding="utf-8")

    monkeypatch.setattr(checker, "check_cookies", lambda _cfg: (_ for _ in ()).throw(RuntimeError("browser crashed")))
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_valid", lambda *_args: None)
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_invalid", lambda *_args: None)

    success, fields, msg = updater.update_cookies_from_wechat(
        str(config_path),
        "TOK=new_val",
        object(),
    )

    disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert success is False
    assert fields == []
    assert disk["source"]["TOK"] == "old_val"
    assert "已回滚" in msg


def test_post_write_config_load_failure_rolls_back(monkeypatch, tmp_path):
    """写盘后重新加载配置失败时也必须回滚，不能留下未验证Cookie。"""
    import yaml
    import src.auto_cookies_updater as updater
    import src.config as config_module

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "source": {"TOK": "old_val", "doc_id": "d", "scode": "s", "tab_id": "t", "view_id": "v"},
    }), encoding="utf-8")

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda _path: (_ for _ in ()).throw(RuntimeError("cannot reload config")),
    )

    success, fields, msg = updater.update_cookies_from_wechat(
        str(config_path),
        "TOK=new_val",
        object(),
    )

    disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert success is False
    assert fields == []
    assert disk["source"]["TOK"] == "old_val"
    assert "已回滚" in msg


def test_revert_failure_message_does_not_suggest_restart(monkeypatch, tmp_path):
    """回滚失败文案不应建议重启，重启会加载未验证Cookie。"""
    import yaml
    import src.auto_cookies_updater as updater
    import src.cookies_checker as checker

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "source": {"TOK": "old_val", "doc_id": "d", "scode": "s", "tab_id": "t", "view_id": "v"},
    }), encoding="utf-8")

    monkeypatch.setattr(checker, "check_cookies", lambda _cfg: (_ for _ in ()).throw(checker.CookiesNetworkError("timeout")))
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_valid", lambda *_args: None)
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_invalid", lambda *_args: None)
    monkeypatch.setattr(updater, "_revert_config", lambda *_args: False)

    success, fields, msg = updater.update_cookies_from_wechat(str(config_path), "TOK=new_val", object())

    assert success is False
    assert "或重启" not in msg and "重启服务" not in msg, f"回滚失败不应建议重启: {msg}"
    assert "请勿重启" in msg, f"应明确告知不要重启: {msg}"
    assert "磁盘仍为新Cookie" not in msg
    assert "磁盘配置状态未知" in msg


def test_invalid_cookie_revert_failure_reports_unknown_disk_state(monkeypatch, tmp_path):
    """真实Cookie失效后的回滚失败也必须报告磁盘状态未知。"""
    import yaml
    import src.auto_cookies_updater as updater
    import src.cookies_checker as checker

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "source": {"TOK": "old_val", "doc_id": "d", "scode": "s", "tab_id": "t", "view_id": "v"},
    }), encoding="utf-8")

    monkeypatch.setattr(
        checker,
        "check_cookies",
        lambda _cfg: (_ for _ in ()).throw(checker.CookiesError("expired")),
    )
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_valid", lambda *_args: None)
    monkeypatch.setattr("src.wechat_notifier.notify_cookies_invalid", lambda *_args: None)
    monkeypatch.setattr(updater, "_revert_config", lambda *_args: False)

    success, fields, msg = updater.update_cookies_from_wechat(
        str(config_path),
        "TOK=new_val",
        object(),
    )

    assert success is False
    assert fields == []
    assert "回滚失败" in msg
    assert "磁盘配置状态未知" in msg
    assert "磁盘仍为新Cookie" not in msg
    assert "请勿重启" in msg


def test_qr_cookie_diff_read_failure_does_not_claim_no_change(monkeypatch, tmp_path):
    """扫码入口读取配置失败时，不得宣称Cookie一致或续期成功。"""
    import src.qr_login_renewer as qr

    class FakePage:
        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, *_args):
            return None

        def set_default_timeout(self, *_args):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

        def cookies(self):
            return [{"name": "TOK", "value": "new_val"}]

    class FakeBrowser:
        def new_context(self, **_kwargs):
            return FakeContext()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    cfg = SimpleNamespace(
        source=SimpleNamespace(doc_id="d", scode="s", tab_id="t", view_id="v"),
        wechat=object(),
    )
    messages = []
    monkeypatch.setattr(qr, "sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(qr, "launch_browser", lambda _p: FakeBrowser())
    monkeypatch.setattr(qr, "browser_operation", _null_cm)
    monkeypatch.setattr(qr, "_capture_enterprise_qr", lambda *_args: True)
    monkeypatch.setattr(qr, "_wait_login_success", lambda *_args: True)
    monkeypatch.setattr(qr, "_get_updated_fields", lambda *_args: None)
    monkeypatch.setattr(qr, "send_image", lambda *_args: True)
    monkeypatch.setattr(qr, "send_text", lambda _wechat, text, *_args: messages.append(text))

    success, msg = qr._renew_cookies_by_qr_locked(
        str(tmp_path / "config.yaml"),
        cfg,
        "user-1",
        "Cookies 已过期",
    )

    assert success is False
    assert "读取" in msg
    assert not any("无需更新" in text for text in messages)


def test_qr_snapshot_failure_prevents_write(monkeypatch, tmp_path):
    """扫码入口无法取得完整旧值快照时不得写盘。"""
    import src.qr_login_renewer as qr

    class FakePage:
        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, *_args):
            return None

        def set_default_timeout(self, *_args):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

        def cookies(self):
            return [{"name": "TOK", "value": "new_val"}]

    class FakeBrowser:
        def new_context(self, **_kwargs):
            return FakeContext()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    cfg = SimpleNamespace(
        source=SimpleNamespace(doc_id="d", scode="s", tab_id="t", view_id="v"),
        wechat=object(),
    )
    write_calls = []
    monkeypatch.setattr(qr, "sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(qr, "launch_browser", lambda _p: FakeBrowser())
    monkeypatch.setattr(qr, "browser_operation", _null_cm)
    monkeypatch.setattr(qr, "_capture_enterprise_qr", lambda *_args: True)
    monkeypatch.setattr(qr, "_wait_login_success", lambda *_args: True)
    monkeypatch.setattr(qr, "_get_updated_fields", lambda *_args: ["TOK"])
    monkeypatch.setattr(qr, "_get_current_cookie_values", lambda *_args: None)
    monkeypatch.setattr(qr, "update_config_cookies", lambda *_args: write_calls.append(1) or True)
    monkeypatch.setattr(qr, "send_image", lambda *_args: True)
    monkeypatch.setattr(qr, "send_text", lambda *_args: None)

    success, msg = qr._renew_cookies_by_qr_locked(
        str(tmp_path / "config.yaml"),
        cfg,
        "user-1",
        "Cookies 已过期",
    )

    assert success is False
    assert write_calls == []
    assert "快照" in msg


def test_qr_revert_failure_reports_unknown_disk_state(monkeypatch, tmp_path):
    """扫码验证失败且回滚失败时不得谎称已经回滚。"""
    import src.qr_login_renewer as qr

    class FakePage:
        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, *_args):
            return None

        def set_default_timeout(self, *_args):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

        def cookies(self):
            return [{"name": "TOK", "value": "new_val"}]

    class FakeBrowser:
        def new_context(self, **_kwargs):
            return FakeContext()

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    cfg = SimpleNamespace(
        source=SimpleNamespace(doc_id="d", scode="s", tab_id="t", view_id="v"),
        wechat=object(),
    )
    messages = []
    monkeypatch.setattr(qr, "sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(qr, "launch_browser", lambda _p: FakeBrowser())
    monkeypatch.setattr(qr, "browser_operation", _null_cm)
    monkeypatch.setattr(qr, "_capture_enterprise_qr", lambda *_args: True)
    monkeypatch.setattr(qr, "_wait_login_success", lambda *_args: True)
    monkeypatch.setattr(qr, "_get_updated_fields", lambda *_args: ["TOK"])
    monkeypatch.setattr(qr, "_get_current_cookie_values", lambda *_args: {"TOK": "old_val"})
    monkeypatch.setattr(qr, "update_config_cookies", lambda *_args: True)
    monkeypatch.setattr(qr, "_verify_current_page", lambda *_args: False)
    monkeypatch.setattr(qr, "_revert_config", lambda *_args: False)
    monkeypatch.setattr(qr, "send_image", lambda *_args: True)
    monkeypatch.setattr(qr, "send_text", lambda _wechat, text, *_args: messages.append(text))

    success, msg = qr._renew_cookies_by_qr_locked(
        str(tmp_path / "config.yaml"),
        cfg,
        "user-1",
        "Cookies 已过期",
    )

    assert success is False
    assert "回滚失败" in msg
    assert "磁盘配置状态未知" in msg
    assert "已回滚配置" not in msg


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


# ---- Task 6: 草稿优先提交、条件清除、通知安全 ----

def _srv():
    import src.server as s
    return s


def test_canonical_submit_action_maps_chinese():
    s = _srv()
    assert s._canonical_submit_action("提交") == "submit"
    assert s._canonical_submit_action("覆盖") == "overwrite"
    assert s._canonical_submit_action("skipped") == "skipped"
    assert s._canonical_submit_action("别的") == "failed"


def _patch_notifier(monkeypatch, cap):
    import src.server as s

    def _succ(cfg, content, report_info, **k):
        cap.setdefault("success", {"info": report_info, "k": k})

    monkeypatch.setattr(s, "notify_report_success", _succ)
    monkeypatch.setattr(s, "notify_report_failure", lambda *a, **k: cap.setdefault("failure", a))
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: cap.setdefault("text", a))


def test_draft_first_submit_skips_smart_doc_and_clears(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_draft as d
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    d.save_draft("2026-07-29", "草稿正文", "manual")
    calls = {"cookie": 0, "build": 0, "submit": []}
    import src.cookies_checker as cc
    monkeypatch.setattr(cc, "check_cookies", lambda c: calls.__setitem__("cookie", calls["cookie"] + 1) or True)
    import src.report_builder as rb
    monkeypatch.setattr(rb, "build_report_with_meta",
                        lambda c: calls.__setitem__("build", calls["build"] + 1) or ("x", "smart_sheet", {}))
    monkeypatch.setattr(s, "submit_daily_report",
                        lambda content, cfg: (calls["submit"].append(content),
                                             (True, "ok", {"action": "提交", "report_date": "2026-07-29"}))[1])
    cap = {}
    _patch_notifier(monkeypatch, cap)
    from types import SimpleNamespace
    s.auto_submit_if_needed(SimpleNamespace(wechat=SimpleNamespace()), precheck_cookies=True)
    assert calls["cookie"] == 0 and calls["build"] == 0 and calls["submit"] == ["草稿正文"]
    assert d.load_draft() is None
    assert "success" in cap and "failure" not in cap
    assert cap["success"]["k"]["smart_doc_status"] == "not_used"
    assert cap["success"]["info"].get("draft_note") == "草稿已清除。"


def test_skipped_does_not_notify_success(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_draft as d
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    d.save_draft("2026-07-29", "草稿正文", "manual")
    monkeypatch.setattr(s, "submit_daily_report",
                        lambda content, cfg: (True, "今日日报已审核，跳过", {"action": "skipped"}))
    cap = {}
    _patch_notifier(monkeypatch, cap)
    from types import SimpleNamespace
    s.auto_submit_if_needed(SimpleNamespace(wechat=SimpleNamespace()), precheck_cookies=True)
    assert "success" not in cap and "failure" not in cap
    assert "text" in cap and d.load_draft() is not None


def test_unknown_action_reports_unconfirmed(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_draft as d
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    d.save_draft("2026-07-29", "草稿正文", "manual")
    monkeypatch.setattr(s, "submit_daily_report", lambda content, cfg: (True, "ok", {"action": "weird"}))
    cap = {}
    _patch_notifier(monkeypatch, cap)
    from types import SimpleNamespace
    ret = s.auto_submit_if_needed(SimpleNamespace(wechat=SimpleNamespace()), precheck_cookies=True)
    assert ret is False
    assert "failure" in cap and "无法确认" in str(cap["failure"])
    assert d.load_draft() is not None


def test_clear_failure_sets_draft_note_not_failure(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_draft as d
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    d.save_draft("2026-07-29", "草稿正文", "manual")
    monkeypatch.setattr(s, "submit_daily_report", lambda content, cfg: (True, "ok", {"action": "提交"}))
    monkeypatch.setattr(d, "clear_draft", lambda **k: (_ for _ in ()).throw(d.DraftCorruptError("bad")))
    cap = {}
    _patch_notifier(monkeypatch, cap)
    from types import SimpleNamespace
    s.auto_submit_if_needed(SimpleNamespace(wechat=SimpleNamespace()), precheck_cookies=True)
    assert "草稿清除失败" in cap["success"]["info"].get("draft_note", "")
    assert "failure" not in cap


def test_notify_exception_does_not_flip_to_failure(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_draft as d
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    d.save_draft("2026-07-29", "草稿正文", "manual")
    monkeypatch.setattr(s, "submit_daily_report", lambda content, cfg: (True, "ok", {"action": "提交"}))
    monkeypatch.setattr(s, "notify_report_success",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(s, "notify_report_failure", lambda *a, **k: None)
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: None)
    from types import SimpleNamespace
    ret = s.auto_submit_if_needed(SimpleNamespace(wechat=SimpleNamespace()), precheck_cookies=True)
    # 通知失败不翻结果；草稿已在 OA 写入成功后清除（按计划：通知失败不改变 OA 写入结论）
    assert ret is True and d.load_draft() is None


# ---- Task 7: 后台线程化、追加日报分流、确认/取消、启动期检查 ----

def _setup_basic(monkeypatch, tmp_path, oa_status=("absent", "")):
    import src.server as s
    import src.daily_report_draft as d
    import src.target as t
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    monkeypatch.setattr(t, "query_today_report", lambda cfg, dt: oa_status)
    sent = []
    monkeypatch.setattr(s, "_send_wechat_text", lambda cfg, msg, u=None: sent.append((msg, u)))
    monkeypatch.setattr(s, "_send_long_text", lambda cfg, msg, u=None: sent.append((msg, u)))
    from types import SimpleNamespace
    return s, SimpleNamespace(wechat=SimpleNamespace()), sent


def test_append_to_oa_creates_pending_when_submitted(monkeypatch, tmp_path):
    s, cfg, sent = _setup_basic(monkeypatch, tmp_path, oa_status=("exists", "现有正文"))
    s._handle_daily_report_edit_command("追加日报\n新增条目", "u1", cfg)
    item = s._edit_confirmations.peek("u1")
    assert item is not None and item.action == "append_today"
    assert item.final_content == "现有正文\n新增条目"
    assert any("目标日期" in m[0] and "原正文摘要" in m[0] for m in sent)


def test_append_no_oa_no_draft_uses_smart_sheet_with_cookie_check(monkeypatch, tmp_path):
    s, cfg, sent = _setup_basic(monkeypatch, tmp_path, oa_status=("absent", ""))
    import src.cookies_checker as cc
    import src.report_builder as rb
    cookie = {"checked": False}
    monkeypatch.setattr(cc, "check_cookies", lambda c: cookie.__setitem__("checked", True) or True)
    monkeypatch.setattr(rb, "build_report_with_meta",
                        lambda c: ("智能文档正文", "smart_sheet", {"smart_doc_status": "normal"}))
    s._handle_daily_report_edit_command("追加日报\n完成联调", "u1", cfg)
    assert cookie["checked"] is True
    import src.daily_report_draft as d
    loaded = d.load_draft()
    assert loaded.origin == "smart_sheet_append" and loaded.content == "智能文档正文\n完成联调"


def test_append_oa_error_does_not_create_draft(monkeypatch, tmp_path):
    s, cfg, sent = _setup_basic(monkeypatch, tmp_path, oa_status=("error", ""))
    import src.report_builder as rb
    monkeypatch.setattr(rb, "build_report_with_meta", lambda c: ("x", "smart_sheet", {}))
    s._handle_daily_report_edit_command("追加日报\nx", "u1", cfg)
    import src.daily_report_draft as d
    assert d.load_draft() is None
    assert any("失败" in m[0] for m in sent)


def test_set_today_rejected_when_oa_submitted(monkeypatch, tmp_path):
    s, cfg, sent = _setup_basic(monkeypatch, tmp_path, oa_status=("exists", "已存在"))
    s._handle_daily_report_edit_command("设置日报\n今日内容", "u1", cfg)
    assert any("修改今日日报" in m[0] for m in sent)


def test_ai_route_natural_language_executes_append(monkeypatch, tmp_path):
    s, cfg, sent = _setup_basic(monkeypatch, tmp_path, oa_status=("exists", "现有正文"))
    raw = "往日报后面加一条：完成生产验证"
    s._handle_daily_report_edit_command(raw, "u1", cfg, action="append", body=s._extract_body_from_raw(raw))
    item = s._edit_confirmations.peek("u1")
    assert item is not None and item.final_content == "现有正文\n完成生产验证"


def test_confirm_passes_user_id_to_modify(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    s._msgid_inflight.clear()
    s._msgid_seen.clear()
    clk = [1000.0]
    s._edit_confirmations = ec.EditConfirmationStore(clock=lambda: clk[0])
    item = ec.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
                               original_content_hash=ec.content_hash("原"), original_preview="原",
                               final_content="新正文", created_at=clk[0], expires_at=clk[0] + 120)
    s._edit_confirmations.save(item)
    import src.target as t
    captured = {}
    monkeypatch.setattr(t, "modify_daily_report",
                        lambda content, cfg, report_date=None, expected_content_hash=None, user_id=None:
                        captured.update(user_id=user_id) or (True, "ok", {"action": "overwrite", "report_date": report_date}))
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: True)
    from types import SimpleNamespace
    s._handle_edit_confirmation("u1", SimpleNamespace(wechat=SimpleNamespace()), msg_id="M-pass")
    assert captured["user_id"] == "u1"


def test_confirm_consumed_sends_timeout(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    s._msgid_inflight.clear()
    s._msgid_seen.clear()
    clk = [1000.0]
    s._edit_confirmations = ec.EditConfirmationStore(clock=lambda: clk[0])
    sent = []
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: sent.append(a) or True)
    from types import SimpleNamespace
    assert s._handle_edit_confirmation("u1", SimpleNamespace(wechat=SimpleNamespace()), msg_id="M-to") is True
    assert any("超时" in str(a) for a in sent)


def test_consume_confirm_atomic_dedup(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    s._msgid_inflight.clear()
    s._msgid_seen.clear()
    clk = [1000.0]
    s._edit_confirmations = ec.EditConfirmationStore(clock=lambda: clk[0])
    item = ec.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
                               original_content_hash="h", original_preview="p", final_content="c",
                               created_at=clk[0], expires_at=clk[0] + 120)
    s._edit_confirmations.save(item)
    item1, dup1 = s._consume_confirm_or_skip("M-atom", "u1")
    assert item1 is not None and dup1 is False
    item2, dup2 = s._consume_confirm_or_skip("M-atom", "u1")
    assert item2 is None and dup2 is True


def test_route_confirm_present_routes_oa(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    clk = [1000.0]
    s._edit_confirmations = ec.EditConfirmationStore(clock=lambda: clk[0])
    item = ec.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
                               original_content_hash="h", original_preview="p", final_content="c",
                               created_at=clk[0], expires_at=clk[0] + 120)
    s._edit_confirmations.save(item)
    assert s._route_confirm_or_cancel("确认执行", "u1") is True


def test_route_confirm_expired_falls_through(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    clk = [1000.0]
    s._edit_confirmations = ec.EditConfirmationStore(clock=lambda: clk[0])
    item = ec.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
                               original_content_hash="h", original_preview="p", final_content="c",
                               created_at=clk[0], expires_at=clk[0] + 1)
    s._edit_confirmations.save(item)
    clk[0] += 10
    assert s._edit_confirmations.peek("u1") is None
    assert s._route_confirm_or_cancel("确认执行", "u1") is False


def test_stale_notify_false_keeps_marker(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    monkeypatch.setattr(ec, "_marker_dir", lambda: tmp_path)
    ec.begin_modify_marker("u1", "2026-07-29", "h")
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: False)
    from types import SimpleNamespace
    s._check_stale_modify_on_startup(SimpleNamespace(wechat=SimpleNamespace()))
    assert len(ec.peek_stale_modify_markers()) == 1
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: True)
    s._check_stale_modify_on_startup(SimpleNamespace(wechat=SimpleNamespace()))
    assert ec.peek_stale_modify_markers() == []


def test_background_thread_marks_done_only_on_success(monkeypatch, tmp_path):
    import src.server as s
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(s, "_msgid_mark_done", lambda mid, *, processed: calls.append(processed))
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: None)
    cfg_obj = SimpleNamespace(wechat=SimpleNamespace())

    def ok_handler(*a, **k):
        pass

    def bad_handler(*a, **k):
        raise RuntimeError("x")

    s._run_in_background(ok_handler, msg_id="OK", cfg_obj=cfg_obj, from_user="u")
    s._run_in_background(bad_handler, msg_id="BAD", cfg_obj=cfg_obj, from_user="u")
    import time
    time.sleep(0.2)
    assert True in calls and False in calls
