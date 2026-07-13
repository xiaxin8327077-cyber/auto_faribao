import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
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
        if kind not in {"command", "diagnose", "chat", "clarify"}:
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

        if kind == "chat":
            return AiRoute(kind=kind, confidence=confidence)

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
kind只能是 command、diagnose、chat、clarify。
用户询问系统为什么未发送、为什么失败、为什么没有执行时使用diagnose。
普通知识问答、天气、生活、工作讨论和闲聊使用chat，canonical_command和reply均留空。
只有系统操作意图明确但缺少必要参数或存在冲突时才使用clarify。
不得输出Shell、代码、路径、URL或未列出的指令。

kind为command时，canonical_command必须严格使用下列指令或参数模板，不得自行概括或改名：
无参数指令：今日状态、最近记录、本周统计、本月统计、查看配置、查看定时配置、读取日报、获取前一天日报、检查Cookies、服务器状态、运行服务、查看日志、发送日报、重新发送今日日报、撤回今日日报、根据前一天内容发送、生成二维码、更新工作日历。
日报参数指令：查询日报 YYYY-MM-DD；设置Cookies检查时间 HH:MM；设置统计推送时间 HH:MM；设置缓存清理时间 HH:MM；设置日报提交时间 HH:MM。
净值查询指令：立即查询净值；查询净值 YYYYMMDD；查询周度净值；查询月度净值；查询季度净值；查询半年度净值；查询年度净值；查询近7天净值；查询近一月净值；查询近三月净值；查询近半年净值；查询近一年净值；查询近两年净值；查询近三年净值。
净值配置指令：查看净值配置；开启净值监控；关闭净值监控；设置净值推送时间 HH:MM；设置收益预估时间 HH:MM；添加净值产品 机构 产品代码；删除净值产品 产品代码；设置净值份额 产品代码 数值；查看持仓画像；查看 产品代码 持仓画像；查看画像状态；更新持仓画像；预估理财涨跌。
相对日期必须根据当前日期换算：日报使用YYYY-MM-DD，净值使用YYYYMMDD。中文时间要换算为24小时HH:MM。

示例：
用户：OA今天交上去了吗
输出：{{"kind":"command","canonical_command":"今日状态","confidence":0.98,"reply":""}}
用户：把日报提交时间改到晚上八点半
输出：{{"kind":"command","canonical_command":"设置日报提交时间 20:30","confidence":0.98,"reply":""}}
用户：帮我查询昨天的理财净值
输出：{{"kind":"command","canonical_command":"查询净值 {current_date - timedelta(days=1):%Y%m%d}","confidence":0.98,"reply":""}}
用户：为什么今天没有自动发送理财净值日报
输出：{{"kind":"diagnose","canonical_command":"","confidence":0.98,"reply":"排查理财净值日报未发送原因"}}
用户：为什么台风没有影响南京，一开始不是说很强吗
输出：{{"kind":"chat","canonical_command":"","confidence":0.98,"reply":""}}
用户：帮我删除那个理财产品，但缺少产品代码
输出：{{"kind":"clarify","canonical_command":"","confidence":0.55,"reply":"请提供要删除的产品代码。"}}

禁止把重启服务、清理缓存、启动或停止代理服务转换为command；遇到这些请求使用clarify并说明不支持。"""


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
