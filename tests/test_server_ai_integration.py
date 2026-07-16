from datetime import date, datetime
import json
from types import SimpleNamespace

import pytest

from src.ai_assistant import AiAssistant, AiMessageBridge
from src.longcat_client import LongCatSettings


class FakeAssistant:
    enabled = True

    def __init__(self, route=None):
        self.next_route = route
        self.route_calls = []
        self.chat_calls = []
        self.diagnosis_calls = []
        self.pending = {}
        self.active_users = set()
        self.authorized_users = {"owner"}
        self.provider_switch_calls = []

    def route(self, text, current_date):
        self.route_calls.append((text, current_date))
        return self.next_route

    def enter_chat(self, user):
        self.active_users.add(user)

    def exit_chat(self, user):
        self.active_users.discard(user)

    def chat_active(self, user):
        return user in self.active_users

    def ask_chat(self, user, question):
        self.chat_calls.append((user, question))
        return "assistant answer"

    def save_pending(self, user, command):
        self.pending[user] = command

    def confirm_pending(self, user):
        return self.pending.pop(user, None)

    def cancel_pending(self, user):
        return self.pending.pop(user, None) is not None

    def diagnostic_authorized(self, user):
        return user in self.authorized_users

    def diagnose(self, question):
        self.diagnosis_calls.append(question)
        return "diagnostic report"

    def provider_status(self):
        return "当前主模型：百炼 qwen3.7-plus\n备用模型：LongCat LongCat-2.0"

    def switch_provider(self, provider):
        self.provider_switch_calls.append(provider)
        return "百炼 qwen3.7-plus"


def test_diagnostic_wildcard_authorizes_every_wechat_user():
    assistant = AiAssistant(
        LongCatSettings(diagnostic_users=frozenset({"*"}))
    )

    assert assistant.diagnostic_authorized("any-wechat-user") is True


def test_diagnostic_client_reserves_fifteen_seconds_for_large_evidence_context():
    assistant = AiAssistant.from_env(
        object(),
        ".",
        env={"LONGCAT_ENABLED": "true", "LONGCAT_API_KEY": "test-key"},
    )

    assert assistant._diagnostics._client._timeout_seconds == 15


def test_assistant_switches_provider_only_after_probe_and_persists(monkeypatch, tmp_path):
    created = []

    class StubProviderClient:
        def __init__(self, *args, **kwargs):
            self.model = args[2]
            self.complete_calls = []
            created.append(self)

        def complete(self, messages, **kwargs):
            self.complete_calls.append((messages, kwargs))
            return "OK"

    monkeypatch.setattr("src.ai_assistant.LongCatClient", StubProviderClient)
    assistant = AiAssistant.from_env(
        object(),
        tmp_path,
        env={
            "AI_ENABLED": "true",
            "AI_PROVIDER": "qwen",
            "QWEN_API_KEY": "qwen-secret",
            "LONGCAT_API_KEY": "longcat-secret",
        },
    )

    display_name = assistant.switch_provider("LongCat")

    state = json.loads((tmp_path / "data" / "ai_provider_state.json").read_text(encoding="utf-8"))
    assert display_name == "LongCat LongCat-2.0"
    assert assistant.settings.provider == "longcat"
    assert state == {"provider": "longcat"}
    probe = next(client for client in created if client.complete_calls)
    assert probe.model == "LongCat-2.0"
    assert probe.complete_calls[0][1]["thinking"] is False


def test_assistant_keeps_current_provider_when_target_probe_fails(monkeypatch, tmp_path):
    class FailingProviderClient:
        def __init__(self, *args, **kwargs):
            pass

        def complete(self, messages, **kwargs):
            raise RuntimeError("offline")

    monkeypatch.setattr("src.ai_assistant.LongCatClient", FailingProviderClient)
    assistant = AiAssistant.from_env(
        object(),
        tmp_path,
        env={
            "AI_ENABLED": "true",
            "AI_PROVIDER": "qwen",
            "QWEN_API_KEY": "qwen-secret",
            "LONGCAT_API_KEY": "longcat-secret",
        },
    )

    with pytest.raises(RuntimeError, match="offline"):
        assistant.switch_provider("LongCat")

    assert assistant.settings.provider == "qwen"
    assert not (tmp_path / "data" / "ai_provider_state.json").exists()


def _harness(assistant, *, busy=False, known=False, run_async=None, now_fn=None):
    sent_text = []
    sent_markdown = []
    ended = []
    names = []
    starts = []

    def try_start(name):
        starts.append(name)
        return not busy

    bridge = AiMessageBridge(
        assistant,
        send_text=lambda text, user: sent_text.append((text, user)),
        send_markdown=lambda text, user: sent_markdown.append((text, user)),
        try_start_cmd=try_start,
        end_cmd=lambda: ended.append(True),
        rename_cmd=lambda name: names.append(name),
        busy_reply=lambda: "BUSY",
        is_known_command=lambda _text: known,
        run_async=run_async or (lambda fn: fn()),
        now_fn=now_fn,
    )
    return bridge, sent_text, sent_markdown, starts, ended, names


def test_existing_command_never_calls_model():
    assistant = FakeAssistant()
    bridge, *_ = _harness(assistant, known=True)

    result = bridge.prepare("立即查询净值", "owner", date(2026, 7, 13))

    assert result.handled is False
    assert result.content == "立即查询净值"
    assert assistant.route_calls == []


def test_ai_provider_status_is_answered_without_calling_model():
    assistant = FakeAssistant()
    bridge, sent, _, starts, ended, _ = _harness(assistant)

    result = bridge.prepare("查看AI模型", "owner", date(2026, 7, 13))

    assert result.handled is True
    assert sent == [
        ("🤖 AI模型配置\n\n当前主模型：百炼 qwen3.7-plus\n备用模型：LongCat LongCat-2.0", "owner")
    ]
    assert assistant.route_calls == []
    assert starts == []
    assert ended == []


def test_ai_provider_switch_probes_then_applies_and_releases_lock():
    assistant = FakeAssistant()
    bridge, sent, _, starts, ended, _ = _harness(assistant)

    result = bridge.prepare("切换AI模型 百炼", "owner", date(2026, 7, 13))

    assert result.handled is True
    assert assistant.provider_switch_calls == ["百炼"]
    assert starts == ["AI模型切换"]
    assert "正在检测百炼" in sent[0][0]
    assert "已切换" in sent[-1][0]
    assert "百炼 qwen3.7-plus" in sent[-1][0]
    assert ended == [True]


def test_ai_provider_switch_does_not_run_while_another_command_is_busy():
    assistant = FakeAssistant()
    bridge, sent, _, starts, ended, _ = _harness(assistant, busy=True)

    result = bridge.prepare("换成LongCat", "owner", date(2026, 7, 13))

    assert result.handled is True
    assert assistant.provider_switch_calls == []
    assert starts == ["AI模型切换"]
    assert sent == [("BUSY", "owner")]
    assert ended == []


def test_busy_command_prevents_longcat_call():
    assistant = FakeAssistant()
    bridge, sent, _, starts, ended, _ = _harness(assistant, busy=True)

    result = bridge.prepare("帮我看看那个收益", "owner", date(2026, 7, 13))

    assert result.handled is True
    assert assistant.route_calls == []
    assert starts == ["AI指令识别"]
    assert sent == [("BUSY", "owner")]
    assert ended == []


def test_unknown_read_command_is_normalized_for_existing_dispatch():
    route = SimpleNamespace(
        kind="command", canonical_command="查询近一月净值", risk="read", reply=""
    )
    assistant = FakeAssistant(route)
    bridge, _, _, _, ended, _ = _harness(assistant)

    result = bridge.prepare("看看最近一个月那个理财", "owner", date(2026, 7, 13))

    assert result.handled is False
    assert result.content == "查询近一月净值"
    assert ended == [True]


def test_router_chat_reuses_the_held_command_lock_and_answers():
    route = SimpleNamespace(
        kind="chat", canonical_command="", risk="none", reply=""
    )
    assistant = FakeAssistant(route)
    bridge, sent, _, starts, ended, names = _harness(assistant)

    result = bridge.prepare(
        "为什么台风没有影响南京？", "owner", date(2026, 7, 13)
    )

    assert result.handled is True
    assert assistant.chat_calls == [("owner", "为什么台风没有影响南京？")]
    assert starts == ["AI指令识别"]
    assert sent[-1] == ("assistant answer", "owner")
    assert ended == [True]
    assert names == ["AI聊天"]


def test_casual_presence_greetings_bypass_command_router():
    for text in ("咪咪在吗", "在嘛，咪咪"):
        assistant = FakeAssistant()
        bridge, sent, _, starts, ended, _ = _harness(assistant)

        result = bridge.prepare(text, "owner", date(2026, 7, 13))

        assert result.handled is True
        assert assistant.route_calls == []
        assert assistant.chat_calls == [("owner", text)]
        assert starts == ["AI聊天"]
        assert sent[-1] == ("assistant answer", "owner")
        assert ended == [True]


def test_current_time_questions_reply_locally_without_longcat():
    for text in ("现在几点钟", "当前时间", "几点了"):
        assistant = FakeAssistant()
        bridge, sent, _, starts, ended, _ = _harness(
            assistant,
            now_fn=lambda: datetime(2026, 7, 13, 18, 20, 35),
        )

        result = bridge.prepare(text, "owner", date(2026, 7, 13))

        assert result.handled is True
        assert assistant.route_calls == []
        assert assistant.chat_calls == []
        assert starts == []
        assert ended == []
        assert sent == [("🕒 当前北京时间：2026-07-13 18:20:35", "owner")]


def test_router_chat_start_failure_releases_the_held_command_lock():
    def fail_to_start(_fn):
        raise RuntimeError("thread unavailable")

    route = SimpleNamespace(
        kind="chat", canonical_command="", risk="none", reply=""
    )
    assistant = FakeAssistant(route)
    bridge, sent, _, starts, ended, _ = _harness(
        assistant, run_async=fail_to_start
    )

    result = bridge.prepare("普通问题", "owner", date(2026, 7, 13))

    assert result.handled is True
    assert starts == ["AI指令识别"]
    assert ended == [True]
    assert "启动失败" in sent[-1][0]


def test_model_routed_write_command_requires_confirmation():
    route = SimpleNamespace(
        kind="command", canonical_command="设置净值份额 AF233276B 10000", risk="write", reply=""
    )
    assistant = FakeAssistant(route)
    bridge, sent, _, _, ended, _ = _harness(assistant)

    result = bridge.prepare("这个份额帮我改成一万", "owner", date(2026, 7, 13))

    assert result.handled is True
    assert assistant.pending["owner"] == "设置净值份额 AF233276B 10000"
    assert "确认执行" in sent[0][0]
    assert ended == [True]

    confirmed = bridge.prepare("确认执行", "owner", date(2026, 7, 13))
    assert confirmed.handled is False
    assert confirmed.content == "设置净值份额 AF233276B 10000"


def test_explicit_chat_runs_asynchronously_and_releases_command_lock():
    assistant = FakeAssistant()
    bridge, sent, _, starts, ended, _ = _harness(assistant)

    result = bridge.prepare("问助手 你好", "owner", date(2026, 7, 13))

    assert result.handled is True
    assert assistant.chat_calls == [("owner", "你好")]
    assert starts == ["AI聊天"]
    assert sent[-1] == ("assistant answer", "owner")
    assert ended == [True]


def test_chat_mode_is_local_and_does_not_call_model_until_question():
    assistant = FakeAssistant()
    bridge, sent, *_ = _harness(assistant)

    entered = bridge.prepare("进入助手模式", "owner", date(2026, 7, 13))
    exited = bridge.prepare("退出助手模式", "owner", date(2026, 7, 13))

    assert entered.handled is True
    assert exited.handled is True
    assert assistant.chat_calls == []
    assert "已进入" in sent[0][0]
    assert "已退出" in sent[1][0]


def test_diagnosis_requires_authorization_and_uses_markdown_when_allowed():
    route = SimpleNamespace(kind="diagnose", canonical_command="", risk="none", reply="")

    denied = FakeAssistant(route)
    bridge, sent, markdown, _, ended, _ = _harness(denied)
    bridge.prepare("为什么今天没发", "other", date(2026, 7, 13))
    assert "没有线上诊断权限" in sent[-1][0]
    assert denied.diagnosis_calls == []
    assert ended == [True]

    allowed = FakeAssistant(route)
    bridge, _, markdown, _, ended, names = _harness(allowed)
    bridge.prepare("为什么今天没发", "owner", date(2026, 7, 13))
    assert allowed.diagnosis_calls == ["为什么今天没发"]
    assert markdown == [("diagnostic report", "owner")]
    assert names == ["AI线上诊断"]
    assert ended == [True]


def test_async_start_failure_releases_chat_and_diagnosis_locks():
    def fail_to_start(_fn):
        raise RuntimeError("thread unavailable")

    chat = FakeAssistant()
    bridge, sent, _, _, ended, _ = _harness(chat, run_async=fail_to_start)
    result = bridge.prepare("问助手 你好", "owner", date(2026, 7, 13))
    assert result.handled is True
    assert ended == [True]
    assert "启动失败" in sent[-1][0]

    route = SimpleNamespace(kind="diagnose", canonical_command="", risk="none", reply="")
    diagnosis = FakeAssistant(route)
    bridge, sent, _, _, ended, _ = _harness(diagnosis, run_async=fail_to_start)
    result = bridge.prepare("为什么今天没发", "owner", date(2026, 7, 13))
    assert result.handled is True
    assert ended == [True]
    assert "启动失败" in sent[-1][0]
