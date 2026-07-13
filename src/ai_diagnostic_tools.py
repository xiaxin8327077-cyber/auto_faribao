import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Callable, Optional


class DiagnosticToolError(RuntimeError):
    pass


_BLOCKED_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "backups",
    "config",
    "data",
    "runtime",
    "venv",
}
_MAX_OUTPUT_CHARS = 12000
_ALLOWED_TOP_LEVEL_SOURCE = {"main.py", "status_page.py", "submit_for_date.py"}


def redact_text(text: str) -> str:
    result = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer [REDACTED]", text or "")
    secret_names = (
        "api_key|corpsecret|password|cookie|token|tok|secret|aes_key|"
        "wedoc_sid|wedoc_skey|wedoc_ticket|hashkey"
    )
    result = re.sub(
        rf"(?im)(\b(?:{secret_names})\s*[:=]\s*)[^\r\n]*",
        lambda match: f"{match.group(1)}[REDACTED]",
        result,
    )
    return result


class DiagnosticToolbox:
    _TOOL_METHODS = {
        "get_schedule_snapshot": "_get_schedule_snapshot",
        "get_service_snapshot": "_get_service_snapshot",
        "search_logs": "_search_logs",
        "search_source": "_search_source",
        "read_source": "_read_source",
    }

    def __init__(
        self,
        project_root,
        cfg,
        *,
        command_runner: Optional[Callable] = None,
        now_fn: Optional[Callable] = None,
        today_fn: Optional[Callable[[], date]] = None,
        workday_fn: Optional[Callable[[date], bool]] = None,
    ):
        self._root = Path(project_root).resolve()
        self._cfg = cfg
        self._command_runner = command_runner or self._run_command
        self._now_fn = now_fn or self._beijing_now
        self._today_fn = today_fn or self._beijing_today
        self._workday_fn = workday_fn or self._is_workday

    def descriptions(self) -> str:
        return (
            "get_schedule_snapshot({}); get_service_snapshot({}); "
            "search_logs({query, since_minutes, limit}); "
            "search_source({query}); "
            "read_source({path, start_line, end_line})"
        )

    def allowed_tools(self) -> frozenset[str]:
        return frozenset(self._TOOL_METHODS)

    def execute(self, name: str, arguments: dict) -> dict:
        method_name = self._TOOL_METHODS.get(name)
        if method_name is None:
            raise DiagnosticToolError("该诊断工具不允许调用")
        if not isinstance(arguments, dict):
            raise DiagnosticToolError("诊断工具参数格式错误")
        tool = getattr(self, method_name)
        content = redact_text(str(tool(arguments)))[:_MAX_OUTPUT_CHARS]
        return {"ok": True, "tool": name, "content": content}

    def _get_schedule_snapshot(self, _arguments: dict) -> str:
        current = self._now_fn()
        day = self._today_fn()
        scheduler = self._cfg.scheduler
        nav = self._cfg.nav_monitor
        return "\n".join(
            [
                f"当前北京时间：{current:%Y-%m-%d %H:%M}",
                f"日期：{day:%Y-%m-%d}",
                f"工作日：{'是' if self._workday_fn(day) else '否'}",
                f"Cookies检查：{scheduler.cookie_check_hour:02d}:{scheduler.cookie_check_minute:02d}",
                f"日报提交：{scheduler.report_submit_hour:02d}:{scheduler.report_submit_minute:02d}",
                f"日报统计：{scheduler.stats_push_hour:02d}:{scheduler.stats_push_minute:02d}",
                f"净值日报：{nav.push_hour:02d}:{nav.push_minute:02d}，{'开启' if nav.enabled else '关闭'}",
                f"净值晚间补发：{nav.evening_push_hour:02d}:{nav.evening_push_minute:02d}，{'开启' if nav.evening_push_enabled else '关闭'}",
                f"理财收益预估：{nav.estimate_hour:02d}:{nav.estimate_minute:02d}，{'开启' if nav.estimate_enabled else '关闭'}",
                f"监控产品数：{len(nav.products)}",
            ]
        )

    def _get_service_snapshot(self, _arguments: dict) -> str:
        commands = [
            ["systemctl", "show", "daily-report", "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "MemoryCurrent", "-p", "MemoryPeak"],
            ["uptime", "-p"],
            ["free", "-h"],
            ["df", "-h", "/"],
        ]
        sections = []
        for command in commands:
            try:
                result = self._command_runner(command, timeout=5)
                output = (result.stdout or result.stderr or "").strip()
            except Exception as exc:
                output = f"读取失败：{type(exc).__name__}"
            sections.append(f"$ {' '.join(command)}\n{output}")
        return "\n\n".join(sections)

    def _search_logs(self, arguments: dict) -> str:
        query = str(arguments.get("query", "")).strip()
        if len(query) > 80:
            raise DiagnosticToolError("日志查询关键词过长")
        since_minutes = _bounded_int(arguments.get("since_minutes", 1440), 1, 10080, "时间范围")
        limit = _bounded_int(arguments.get("limit", 80), 1, 120, "日志行数")

        lines = []
        log_path = self._root / "daily_send.log"
        if log_path.is_file():
            lines.extend(_tail_text_file(log_path).splitlines()[-500:])

        try:
            result = self._command_runner(
                [
                    "journalctl",
                    "-u",
                    "daily-report",
                    "--since",
                    f"{since_minutes} minutes ago",
                    "--no-pager",
                    "-n",
                    "500",
                ],
                timeout=8,
            )
            lines.extend((result.stdout or "").splitlines())
        except Exception as exc:
            lines.append(f"journalctl读取失败：{type(exc).__name__}")

        if query:
            lowered = query.lower()
            lines = [line for line in lines if lowered in line.lower()]
        if not lines:
            return "未找到匹配日志"
        return "\n".join(lines[-limit:])

    def _search_source(self, arguments: dict) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise DiagnosticToolError("源码查询关键词不能为空")
        if len(query) > 80:
            raise DiagnosticToolError("源码查询关键词过长")
        lowered = query.lower()
        matches = []
        for path in self._source_files():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, 1):
                if lowered in line.lower():
                    relative = path.relative_to(self._root).as_posix()
                    matches.append(f"{relative}:{line_number}: {line.strip()}")
                    if len(matches) >= 50:
                        return "\n".join(matches)
        return "\n".join(matches) if matches else "未找到匹配源码"

    def _read_source(self, arguments: dict) -> str:
        raw_path = str(arguments.get("path", "")).strip()
        relative = Path(raw_path)
        if not raw_path or relative.is_absolute() or ".." in relative.parts:
            raise DiagnosticToolError("源码路径不在允许范围")
        if (
            relative.suffix.lower() != ".py"
            or any(part in _BLOCKED_PARTS for part in relative.parts)
            or not _source_path_allowed(relative)
        ):
            raise DiagnosticToolError("源码路径不在允许范围")
        path = (self._root / relative).resolve()
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise DiagnosticToolError("源码路径不在允许范围") from exc
        if not path.is_file():
            raise DiagnosticToolError("源码文件不存在")

        start = _bounded_int(arguments.get("start_line", 1), 1, 1_000_000, "起始行")
        end = _bounded_int(arguments.get("end_line", start + 99), start, 1_000_000, "结束行")
        if end - start + 1 > 200:
            raise DiagnosticToolError("单次最多读取200行源码")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start - 1:end]
        return "\n".join(
            f"{number}: {line}" for number, line in enumerate(selected, start=start)
        ) or "指定范围没有内容"

    def _source_files(self):
        candidates = []
        src_root = self._root / "src"
        if src_root.is_dir():
            candidates.extend(src_root.rglob("*.py"))
        candidates.extend(
            self._root / name
            for name in _ALLOWED_TOP_LEVEL_SOURCE
            if (self._root / name).is_file()
        )
        for path in sorted(candidates):
            try:
                relative = path.relative_to(self._root)
            except ValueError:
                continue
            if any(part in _BLOCKED_PARTS for part in relative.parts):
                continue
            if not _source_path_allowed(relative):
                continue
            yield path

    @staticmethod
    def _run_command(args, timeout):
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    @staticmethod
    def _beijing_now():
        from src.beijing_time import now

        return now()

    @staticmethod
    def _beijing_today():
        from src.beijing_time import today

        return today()

    @staticmethod
    def _is_workday(day):
        from src.workday_calendar import is_workday

        return is_workday(day)


def _bounded_int(value, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DiagnosticToolError(f"{label}格式错误") from exc
    if parsed < minimum or parsed > maximum:
        raise DiagnosticToolError(f"{label}超出允许范围")
    return parsed


def _source_path_allowed(relative: Path) -> bool:
    return (
        len(relative.parts) >= 2 and relative.parts[0] == "src"
    ) or (
        len(relative.parts) == 1 and relative.name in _ALLOWED_TOP_LEVEL_SOURCE
    )


def _tail_text_file(path: Path, max_bytes: int = 1024 * 1024) -> str:
    with path.open("rb") as file_handle:
        file_handle.seek(0, 2)
        size = file_handle.tell()
        file_handle.seek(max(0, size - max_bytes))
        data = file_handle.read(max_bytes)
    return data.decode("utf-8", errors="replace")
