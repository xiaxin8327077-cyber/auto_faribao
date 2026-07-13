import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional


class AiRouteError(RuntimeError):
    pass


@dataclass(frozen=True)
class AiRoute:
    kind: str
    canonical_command: str = ""
    confidence: float = 0.0
    reply: str = ""
    risk: str = "none"


_READ_COMMANDS = {
    "今日状态",
    "最近记录",
    "本周统计",
    "本月统计",
    "查看配置",
    "查看定时配置",
    "读取日报",
    "获取前一天日报",
    "检查Cookies",
    "服务器状态",
    "运行服务",
    "查看日志",
}

_WRITE_COMMANDS = {
    "发送日报",
    "重新发送今日日报",
    "撤回今日日报",
    "根据前一天内容发送",
    "生成二维码",
    "更新工作日历",
}

_READ_NAV_ACTIONS = {
    "view_config",
    "view_holdings",
    "view_holdings_status",
    "estimate_holdings",
    "query_latest",
    "query_date",
    "query_period",
}

_WRITE_NAV_ACTIONS = {
    "enable",
    "disable",
    "set_time",
    "set_estimate_time",
    "delete_product",
    "set_shares",
    "set_shares_batch",
    "confirm_add",
    "cancel_add",
    "add_product",
    "update_holdings",
}


def classify_canonical_command(command: str) -> Optional[str]:
    text = " ".join((command or "").strip().split())
    if text in _READ_COMMANDS:
        return "read"
    if text in _WRITE_COMMANDS:
        return "write"
    if re.fullmatch(r"查询日报 20\d{2}-\d{2}-\d{2}", text):
        return "read"
    if re.fullmatch(
        r"设置(?:Cookies检查|统计推送|缓存清理|日报提交)时间 \d{2}:\d{2}",
        text,
    ):
        return "write"

    try:
        from src.nav_monitor import parse_nav_command

        parsed = parse_nav_command(text)
    except Exception:
        parsed = None
    if not parsed:
        return None
    if parsed.action in _READ_NAV_ACTIONS:
        return "read"
    if parsed.action in _WRITE_NAV_ACTIONS:
        return "write"
    return None


class AiCommandRouter:
    def __init__(self, client, minimum_confidence: float = 0.85):
        self._client = client
        self._minimum_confidence = minimum_confidence

    def route(self, text: str, current_date: date) -> AiRoute:
        response = self._client.complete(
            [
                {"role": "system", "content": _router_system_prompt(current_date)},
                {"role": "user", "content": text},
            ],
            max_tokens=400,
            temperature=0.0,
            thinking=False,
        )
        data = _parse_route_json(response)
        kind = data.get("kind")
        if kind not in {"command", "diagnose", "clarify"}:
            raise AiRouteError("LongCat 响应格式异常")

        try:
            confidence = float(data.get("confidence", 0))
        except (TypeError, ValueError) as exc:
            raise AiRouteError("LongCat 响应格式异常") from exc
        reply = str(data.get("reply", "")).strip()[:800]
        command = " ".join(str(data.get("canonical_command", "")).split())

        if kind == "command":
            risk = classify_canonical_command(command)
            if risk is None:
                raise AiRouteError("LongCat 返回的指令不在允许范围")
            if confidence < self._minimum_confidence:
                return AiRoute(
                    kind="clarify",
                    confidence=confidence,
                    reply=reply or "我还不能准确判断你的操作，请再说具体一点。",
                )
            return AiRoute(
                kind=kind,
                canonical_command=command,
                confidence=confidence,
                reply=reply,
                risk=risk,
            )

        if not reply:
            reply = "请再补充一点信息。" if kind == "clarify" else text[:200]
        return AiRoute(kind=kind, confidence=confidence, reply=reply)


def _parse_route_json(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise AiRouteError("LongCat 响应格式异常")
    try:
        data = json.loads(raw[start:end + 1])
    except (json.JSONDecodeError, TypeError) as exc:
        raise AiRouteError("LongCat 响应格式异常") from exc
    if not isinstance(data, dict):
        raise AiRouteError("LongCat 响应格式异常")
    return data


def _router_system_prompt(current_date: date) -> str:
    return f"""你是企业微信指令路由器，不执行任何操作。当前北京时间日期：{current_date:%Y-%m-%d}。
只输出一个JSON对象，字段固定为 kind、canonical_command、confidence、reply。
kind只能是 command、diagnose、clarify。
用户询问系统为什么未发送、为什么失败、为什么没有执行时使用diagnose。
信息不足时使用clarify。不得输出Shell、代码、路径、URL或未列出的指令。
允许的标准指令包括：日报查询/提交/撤回/定时设置；净值最新、日期、自然周期、滚动周期查询；净值产品、份额、画像和监控设置；查看配置、定时配置、Cookies、服务器状态、运行服务和日志。
禁止把重启服务、清理缓存、代理服务操作转换为command。日期参数必须使用现有格式。"""


@dataclass(frozen=True)
class _PendingCommand:
    command: str
    expires_at: float


class AiPendingCommandStore:
    def __init__(self, ttl_seconds: float = 120, clock: Callable[[], float] = time.monotonic):
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._items: dict[str, _PendingCommand] = {}
        self._lock = threading.Lock()

    def save(self, user_id: str, command: str) -> None:
        with self._lock:
            self._items[user_id] = _PendingCommand(
                command=command,
                expires_at=self._clock() + self._ttl_seconds,
            )

    def confirm(self, user_id: str) -> Optional[str]:
        with self._lock:
            item = self._items.pop(user_id, None)
        if not item or item.expires_at < self._clock():
            return None
        return item.command

    def cancel(self, user_id: str) -> bool:
        with self._lock:
            return self._items.pop(user_id, None) is not None
