from datetime import date

import pytest

from src.ai_command_router import (
    AiCommandRouter,
    AiPendingCommandStore,
    AiRouteError,
    classify_canonical_command,
)


class StubClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.response


def test_router_parses_fenced_command_json_and_disables_thinking():
    client = StubClient(
        '```json\n{"kind":"command","canonical_command":"查询净值 20260707",'
        '"confidence":0.98,"reply":""}\n```'
    )
    router = AiCommandRouter(client)

    route = router.route("帮我看看七月七号净值", date(2026, 7, 13))

    assert route.kind == "command"
    assert route.canonical_command == "查询净值 20260707"
    assert route.risk == "read"
    assert client.calls[0][1]["thinking"] is False
    assert client.calls[0][1]["max_tokens"] <= 500


def test_router_prompt_lists_exact_canonical_templates_and_examples():
    client = StubClient(
        '{"kind":"clarify","canonical_command":"","confidence":0.4,'
        '"reply":"请补充信息"}'
    )

    AiCommandRouter(client).route("说得不够清楚", date(2026, 7, 13))

    prompt = client.calls[0][0][0]["content"]
    assert "今日状态" in prompt
    assert "设置日报提交时间 HH:MM" in prompt
    assert "查询净值 YYYYMMDD" in prompt
    assert "OA今天交上去了吗" in prompt
    assert "设置日报提交时间 20:30" in prompt
    assert "为什么今天没有自动发送理财净值日报" in prompt
    assert '"kind":"diagnose"' in prompt
    assert "为什么台风没有影响南京" in prompt
    assert '"kind":"chat"' in prompt
    assert "缺少产品代码" in prompt
    assert '"kind":"clarify"' in prompt


def test_router_marks_model_routed_write_command_for_confirmation():
    client = StubClient(
        '{"kind":"command","canonical_command":"设置净值份额 AF233276B 10000",'
        '"confidence":0.96,"reply":""}'
    )

    route = AiCommandRouter(client).route("份额帮我改成一万", date(2026, 7, 13))

    assert route.risk == "write"


@pytest.mark.parametrize(
    "command",
    [
        "重启服务",
        "清理缓存",
        "停止Xray",
        "执行 rm -rf /",
        "读取 /etc/shadow",
    ],
)
def test_router_rejects_operational_or_non_allowlisted_commands(command):
    client = StubClient(
        '{"kind":"command","canonical_command":%r,"confidence":0.99,"reply":""}'
        % command
    )
    client.response = client.response.replace("'", '"')

    with pytest.raises(AiRouteError, match="不在允许范围"):
        AiCommandRouter(client).route("do something", date(2026, 7, 13))


def test_router_accepts_diagnosis_and_clarification_results():
    diagnosis = AiCommandRouter(
        StubClient(
            '{"kind":"diagnose","canonical_command":"","confidence":0.95,'
            '"reply":"排查净值日报未发送"}'
        )
    ).route("为什么今天没自动发送理财净值日报", date(2026, 7, 13))
    clarification = AiCommandRouter(
        StubClient(
            '{"kind":"clarify","canonical_command":"","confidence":0.4,'
            '"reply":"你想查询日报还是净值日报？"}'
        )
    ).route("看看日报", date(2026, 7, 13))

    assert diagnosis.kind == "diagnose"
    assert clarification.reply == "你想查询日报还是净值日报？"


def test_router_accepts_chat_for_an_ordinary_question():
    route = AiCommandRouter(
        StubClient(
            '{"kind":"chat","canonical_command":"","confidence":0.97,'
            '"reply":""}'
        )
    ).route("为什么台风没有影响南京？", date(2026, 7, 13))

    assert route.kind == "chat"
    assert route.canonical_command == ""
    assert route.risk == "none"


def test_invalid_json_is_rejected_without_guessing():
    with pytest.raises(AiRouteError, match="响应格式异常"):
        AiCommandRouter(StubClient("查询昨天净值")).route(
            "昨天那个", date(2026, 7, 13)
        )


def test_classifies_supported_daily_commands_and_rejects_ops():
    assert classify_canonical_command("查询日报 2026-07-08") == "read"
    assert classify_canonical_command("发送日报") == "write"
    assert classify_canonical_command("设置日报提交时间 20:00") == "write"
    assert classify_canonical_command("重启服务") is None


def test_pending_commands_are_isolated_cancelled_and_expire():
    now = [100.0]
    store = AiPendingCommandStore(ttl_seconds=60, clock=lambda: now[0])

    store.save("user-a", "发送日报")
    store.save("user-b", "设置净值份额 AF233276B 10000")
    assert store.confirm("user-a") == "发送日报"
    assert store.confirm("user-a") is None
    assert store.cancel("user-b") is True
    assert store.confirm("user-b") is None

    store.save("user-a", "发送日报")
    now[0] = 161.0
    assert store.confirm("user-a") is None


def test_classifies_set_daily_profit_as_write():
    assert classify_canonical_command("设置收益 2026-07-21 150.5") == "write"
    assert classify_canonical_command("设置收益 2026-07-22 -80") == "write"


def test_router_prompt_includes_set_daily_profit_template():
    client = StubClient(
        '{"kind":"clarify","canonical_command":"","confidence":0.4,'
        '"reply":"请补充信息"}'
    )

    AiCommandRouter(client).route("说得不够清楚", date(2026, 7, 13))

    prompt = client.calls[0][0][0]["content"]
    assert "设置收益 YYYY-MM-DD 金额" in prompt
    assert "把最新收益改成150块" in prompt
    assert "收益改成-50" in prompt
