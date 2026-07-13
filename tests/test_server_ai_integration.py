from datetime import date, datetime
from types import SimpleNamespace

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
