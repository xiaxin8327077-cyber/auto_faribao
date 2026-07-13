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
    def __init__(self):
        self.calls = []

    def descriptions(self):
        return "search_logs({query}); read_source({path,start_line,end_line})"

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        if name not in {"search_logs", "read_source"}:
            raise RuntimeError("not allowed")
        return {"ok": True, "tool": name, "content": f"evidence from {name}"}


def _tool(name="search_logs"):
    return (
        '{"action":"tool","tool":"%s","arguments":{"query":"nav"},'
        '"reason":"查找定时日志"}' % name
    )


def _final():
    return (
        '{"action":"final","title":"理财净值日报诊断",'
        '"cause":"08:08抓取接口超时",'
        '"evidence":["08:08:01任务启动","08:08:20抓取超时"],'
        '"recommendations":["检查数据源连通性","稍后手动查询"],'
        '"confidence":"高"}'
    )


def test_agent_calls_tools_then_formats_evidence_based_report():
    client = StubClient([_tool(), _final()])
    toolbox = StubToolbox()

    report = DiagnosticAgent(client, toolbox).run("为什么今天没发净值日报")

    assert toolbox.calls == [("search_logs", {"query": "nav"})]
    assert "🔍 **理财净值日报诊断**" in report
    assert "08:08抓取接口超时" in report
    assert "08:08:01任务启动" in report
    assert "置信度：高" in report
    assert client.calls[0][1]["thinking"] is True
    assert "evidence from search_logs" in str(client.calls[1][0])


def test_agent_stops_before_ninth_tool_call():
    client = StubClient([_tool()] * 9)

    with pytest.raises(DiagnosticAgentError, match="8次"):
        DiagnosticAgent(client, StubToolbox(), max_tool_calls=8).run("diagnose")


def test_agent_enforces_total_deadline():
    times = iter([0.0, 0.0, 31.0])
    client = StubClient([_tool(), _final()])

    with pytest.raises(DiagnosticAgentError, match="30秒"):
        DiagnosticAgent(
            client,
            StubToolbox(),
            timeout_seconds=30,
            clock=lambda: next(times),
        ).run("diagnose")


def test_agent_rejects_invalid_json_and_unknown_action():
    with pytest.raises(DiagnosticAgentError, match="响应格式异常"):
        DiagnosticAgent(StubClient(["not-json"]), StubToolbox()).run("diagnose")

    with pytest.raises(DiagnosticAgentError, match="响应格式异常"):
        DiagnosticAgent(
            StubClient(['{"action":"delete","path":"/etc/passwd"}']),
            StubToolbox(),
        ).run("diagnose")


def test_final_report_requires_cause_and_evidence():
    response = (
        '{"action":"final","title":"诊断","cause":"",'
        '"evidence":[],"recommendations":[],"confidence":"低"}'
    )

    with pytest.raises(DiagnosticAgentError, match="缺少证据"):
        DiagnosticAgent(StubClient([response]), StubToolbox()).run("diagnose")

