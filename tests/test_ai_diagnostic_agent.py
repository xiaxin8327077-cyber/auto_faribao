import pytest

from src.ai_diagnostic_agent import DiagnosticAgent, DiagnosticAgentError


class StubClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((list(messages), kwargs))
        return self.replies.pop(0)


class StubToolbox:
    TOOL_NAMES = frozenset(
        {
            "get_schedule_snapshot",
            "get_service_snapshot",
            "search_logs",
            "search_source",
            "read_source",
        }
    )

    def __init__(self, failures=None):
        self.calls = []
        self.failures = set(failures or ())

    def descriptions(self):
        return "search_logs({query}); read_source({path,start_line,end_line})"

    def allowed_tools(self):
        return self.TOOL_NAMES

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.failures:
            raise RuntimeError("private failure detail")
        return {"ok": True, "tool": name, "content": f"evidence from {name}"}


def _plan(*tools):
    items = ",".join(
        '{"tool":"%s","arguments":%s}' % (name, arguments)
        for name, arguments in tools
    )
    return '{"tools":[%s]}' % items


def _report(cause="08:08任务未触发"):
    return (
        "🔍 **理财净值日报诊断**\n\n"
        f"**原因**\n{cause}\n\n"
        "**诊断依据**\n• 定时配置为08:08\n• 当日日志没有触发记录\n\n"
        "**建议处理**\n1. 检查工作日配置\n\n"
        "置信度：中"
    )


def test_agent_uses_one_plan_call_and_one_markdown_report_call():
    client = StubClient(
        [
            _plan(
                ("get_schedule_snapshot", "{}"),
                (
                    "search_logs",
                    '{"query":"nav monitor","since_minutes":1440,"limit":80}',
                ),
            ),
            _report(),
        ]
    )
    toolbox = StubToolbox()

    report = DiagnosticAgent(client, toolbox).run("为什么今天没发净值日报")

    assert toolbox.calls == [
        ("get_schedule_snapshot", {}),
        (
            "search_logs",
            {"query": "nav monitor", "since_minutes": 1440, "limit": 80},
        ),
    ]
    assert len(client.calls) == 2
    assert client.calls[0][1]["thinking"] is True
    assert client.calls[1][1]["thinking"] is False
    assert "evidence from search_logs" in str(client.calls[1][0])
    assert "**诊断依据**" in report


def test_plan_deduplicates_calls_and_executes_at_most_three_tools():
    client = StubClient(
        [
            _plan(
                ("get_schedule_snapshot", "{}"),
                ("get_schedule_snapshot", "{}"),
                ("get_service_snapshot", "{}"),
                ("search_logs", '{"query":"nav"}'),
                ("search_source", '{"query":"scheduler"}'),
            ),
            _report(),
        ]
    )
    toolbox = StubToolbox()

    DiagnosticAgent(client, toolbox).run("diagnose")

    assert toolbox.calls == [
        ("get_schedule_snapshot", {}),
        ("get_service_snapshot", {}),
        ("search_logs", {"query": "nav"}),
    ]


@pytest.mark.parametrize(
    "plan",
    [
        "not-json",
        '{"tools":"search_logs"}',
        _plan(("run_shell", '{"command":"whoami"}')),
        _plan(("search_logs", "[]")),
    ],
)
def test_invalid_or_unauthorized_plan_executes_no_tools(plan):
    toolbox = StubToolbox()

    with pytest.raises(DiagnosticAgentError):
        DiagnosticAgent(StubClient([plan]), toolbox).run("diagnose")

    assert toolbox.calls == []


def test_tool_failure_is_safely_included_for_final_analysis():
    client = StubClient(
        [_plan(("search_logs", '{"query":"nav"}')), _report("日志读取失败")]
    )

    report = DiagnosticAgent(
        client, StubToolbox(failures={"search_logs"})
    ).run("diagnose")

    final_messages = str(client.calls[1][0])
    assert "RuntimeError" in final_messages
    assert "private failure detail" not in final_messages
    assert "日志读取失败" in report


def test_agent_enforces_total_deadline_before_report_call():
    times = iter([0.0, 0.0, 1.0, 31.0])
    client = StubClient([_plan(("search_logs", '{"query":"nav"}'))])

    with pytest.raises(DiagnosticAgentError, match="30秒"):
        DiagnosticAgent(
            client,
            StubToolbox(),
            timeout_seconds=30,
            clock=lambda: next(times),
        ).run("diagnose")

    assert len(client.calls) == 1


def test_final_report_requires_sections_and_is_redacted():
    missing_evidence = "**原因**\n未知\n\n**建议处理**\n稍后再试\n\n置信度：低"
    with pytest.raises(DiagnosticAgentError, match="缺少"):
        DiagnosticAgent(
            StubClient([_plan(("search_logs", "{}")), missing_evidence]),
            StubToolbox(),
        ).run("diagnose")

    report = DiagnosticAgent(
        StubClient(
            [
                _plan(("search_logs", "{}")),
                _report("password=secret-value，Authorization: Bearer abc123"),
            ]
        ),
        StubToolbox(),
    ).run("diagnose")

    assert "secret-value" not in report
    assert "abc123" not in report
    assert "[REDACTED]" in report
