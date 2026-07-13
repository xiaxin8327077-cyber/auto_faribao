from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ai_diagnostic_tools import DiagnosticToolError, DiagnosticToolbox, redact_text


def _cfg():
    return SimpleNamespace(
        scheduler=SimpleNamespace(
            cookie_check_hour=9,
            cookie_check_minute=45,
            report_submit_hour=20,
            report_submit_minute=0,
            stats_push_hour=21,
            stats_push_minute=0,
        ),
        nav_monitor=SimpleNamespace(
            enabled=True,
            push_hour=8,
            push_minute=8,
            evening_push_enabled=True,
            evening_push_hour=23,
            evening_push_minute=30,
            estimate_enabled=True,
            estimate_hour=17,
            estimate_minute=30,
            products=[1, 2, 3],
        ),
    )


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.py").write_text(
        "line one\nScheduler nav monitor push failed\nline three\n",
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text("print('needle')\n", encoding="utf-8")
    (tmp_path / "research_login.py").write_text("print('research')\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("password: top-secret\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secret.py").write_text("needle\n", encoding="utf-8")
    return tmp_path


def test_schedule_snapshot_contains_only_operational_fields(tmp_path):
    toolbox = DiagnosticToolbox(
        _project(tmp_path),
        _cfg(),
        today_fn=lambda: date(2026, 7, 13),
        workday_fn=lambda _day: True,
    )

    result = toolbox.execute("get_schedule_snapshot", {})

    assert result["ok"] is True
    assert "2026-07-13" in result["content"]
    assert "工作日：是" in result["content"]
    assert "净值日报：08:08" in result["content"]
    assert "监控产品数：3" in result["content"]


def test_schedule_snapshot_includes_authoritative_beijing_time(tmp_path):
    toolbox = DiagnosticToolbox(
        _project(tmp_path),
        _cfg(),
        now_fn=lambda: datetime(2026, 7, 13, 17, 56),
        today_fn=lambda: date(2026, 7, 13),
        workday_fn=lambda _day: True,
    )

    result = toolbox.execute("get_schedule_snapshot", {})

    assert "当前北京时间：2026-07-13 17:56" in result["content"]
    assert "Cookies检查：09:45" in result["content"]


def test_source_search_is_plain_text_bounded_and_excludes_sensitive_dirs(tmp_path):
    toolbox = DiagnosticToolbox(_project(tmp_path), _cfg())

    result = toolbox.execute("search_source", {"query": "needle"})

    assert result["ok"] is True
    assert "main.py:1" in result["content"]
    assert "data/secret.py" not in result["content"]


def test_source_reader_allows_python_ranges_and_rejects_sensitive_paths(tmp_path):
    toolbox = DiagnosticToolbox(_project(tmp_path), _cfg())

    result = toolbox.execute(
        "read_source", {"path": "src/server.py", "start_line": 2, "end_line": 3}
    )

    assert "2: Scheduler nav monitor push failed" in result["content"]
    assert "1: line one" not in result["content"]

    for path in ("config.yaml", "data/secret.py", "../outside.py", "/etc/passwd"):
        with pytest.raises(DiagnosticToolError):
            toolbox.execute("read_source", {"path": path, "start_line": 1, "end_line": 2})
    with pytest.raises(DiagnosticToolError):
        toolbox.execute(
            "read_source", {"path": "research_login.py", "start_line": 1, "end_line": 1}
        )


def test_source_reader_rejects_more_than_two_hundred_lines(tmp_path):
    toolbox = DiagnosticToolbox(_project(tmp_path), _cfg())

    with pytest.raises(DiagnosticToolError, match="200"):
        toolbox.execute(
            "read_source", {"path": "src/server.py", "start_line": 1, "end_line": 201}
        )


def test_log_search_uses_fixed_journal_command_and_redacts_secrets(tmp_path):
    project = _project(tmp_path)
    (project / "daily_send.log").write_text(
        "2026-07-13 trigger nav\n"
        "2026-07-13 Authorization: Bearer abc123\n"
        "2026-07-13 password=plain-text\n",
        encoding="utf-8",
    )
    calls = []

    def runner(args, timeout):
        calls.append((args, timeout))
        return SimpleNamespace(returncode=0, stdout="journal trigger nav\n", stderr="")

    toolbox = DiagnosticToolbox(project, _cfg(), command_runner=runner)
    result = toolbox.execute(
        "search_logs", {"query": "", "since_minutes": 120, "limit": 120}
    )

    assert calls[0][0][:4] == ["journalctl", "-u", "daily-report", "--since"]
    assert "abc123" not in result["content"]
    assert "plain-text" not in result["content"]
    assert "[REDACTED]" in result["content"]


def test_unknown_tool_and_invalid_query_are_rejected(tmp_path):
    toolbox = DiagnosticToolbox(_project(tmp_path), _cfg())

    assert toolbox.allowed_tools() == frozenset(
        {
            "get_schedule_snapshot",
            "get_service_snapshot",
            "search_logs",
            "search_source",
            "read_source",
        }
    )

    with pytest.raises(DiagnosticToolError, match="不允许"):
        toolbox.execute("run_shell", {"command": "whoami"})
    with pytest.raises(DiagnosticToolError, match="过长"):
        toolbox.execute("search_source", {"query": "x" * 81})


def test_redaction_covers_common_configuration_secrets():
    text = "api_key: abc\ncorpsecret=def\nCookie: sid=ghi\nnormal=value"

    redacted = redact_text(text)

    assert "abc" not in redacted
    assert "def" not in redacted
    assert "ghi" not in redacted
    assert "normal=value" in redacted
