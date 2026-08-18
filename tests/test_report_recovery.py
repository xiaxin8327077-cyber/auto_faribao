from types import SimpleNamespace
import threading

import pytest

from src.config import Config


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
    return Config(
        {
            "wechat": {
                "to_user": "user-1",
                "corpid": "corp",
                "corpsecret": "secret",
                "agentid": 1,
            },
            "source": {"doc_id": "doc", "person_names": ["tester"]},
        }
    )


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
    fresh_cfg = _cfg()
    callback = {}
    builds = []
    submits = []

    def expired(_cfg):
        raise cookies_checker.CookiesError("expired")

    def start_renew(*args, **kwargs):
        callback["fn"] = kwargs["on_complete"]
        return True

    monkeypatch.setattr(cookies_checker, "check_cookies", expired)
    monkeypatch.setattr(server, "start_renew_cookies_by_qr", start_renew)
    monkeypatch.setattr(config_module, "load_config", lambda path: fresh_cfg)
    monkeypatch.setattr(
        report_builder,
        "build_report_with_meta",
        lambda value: builds.append(value) or ("正文", "smart_sheet", {"smart_doc_status": "normal"}),
    )
    monkeypatch.setattr(
        server,
        "_submit_and_notify",
        lambda content, value, source, meta: submits.append((content, value)) or True,
    )

    assert server.auto_submit_if_needed(old_cfg) is False
    assert builds == []

    workers = [threading.Thread(target=callback["fn"], args=(True, "ok")) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    callback["fn"](True, "duplicate")

    assert builds == [old_cfg]
    assert old_cfg.source is fresh_cfg.source
    assert submits == [("正文", old_cfg)]


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
