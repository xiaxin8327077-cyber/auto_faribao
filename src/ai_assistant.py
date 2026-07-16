import logging
import re
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Mapping, Optional

from src.ai_chat import AiChatService, ChatSessionStore
from src.ai_command_router import AiCommandRouter, AiPendingCommandStore
from src.ai_diagnostic_agent import DiagnosticAgent, DiagnosticAgentError
from src.ai_diagnostic_tools import DiagnosticToolbox
from src.longcat_client import LongCatClient, LongCatSettings


logger = logging.getLogger(__name__)


class AiAssistant:
    def __init__(
        self,
        settings: LongCatSettings,
        *,
        router=None,
        chat=None,
        diagnostics=None,
        sessions=None,
        pending=None,
        cfg=None,
        project_root=None,
    ):
        self.settings = settings
        self.enabled = settings.any_available
        self._router = router
        self._chat = chat
        self._diagnostics = diagnostics
        self._sessions = sessions or ChatSessionStore(max_turns=10)
        self._pending = pending or AiPendingCommandStore(ttl_seconds=120)
        self._cfg = cfg
        self._project_root = Path(project_root).resolve() if project_root else None
        self._provider_lock = threading.Lock()

    @classmethod
    def from_env(cls, cfg, project_root, env: Optional[Mapping[str, str]] = None):
        state_path = Path(project_root) / "data" / "ai_provider_state.json"
        settings = LongCatSettings.from_env(env, state_path=state_path)
        assistant = cls(
            settings,
            pending=AiPendingCommandStore(ttl_seconds=120),
            cfg=cfg,
            project_root=project_root,
        )
        if settings.available:
            assistant._apply_services(
                assistant._build_services(settings.active_provider)
            )
        return assistant

    @staticmethod
    def _client(provider, timeout_seconds: float):
        return LongCatClient(
            provider.api_key,
            provider.base_url,
            provider.model,
            provider_name=provider.label,
            thinking_parameter=provider.thinking_parameter,
            timeout_seconds=timeout_seconds,
        )

    def _build_services(self, provider):
        sessions = ChatSessionStore(max_turns=10)
        toolbox = DiagnosticToolbox(self._project_root, self._cfg)
        return (
            AiCommandRouter(self._client(provider, 4)),
            AiChatService(self._client(provider, 15), sessions),
            DiagnosticAgent(
                self._client(provider, 15),
                toolbox,
                max_tool_calls=3,
                timeout_seconds=30,
            ),
            sessions,
        )

    def _apply_services(self, services) -> None:
        self._router, self._chat, self._diagnostics, self._sessions = services

    def provider_status(self) -> str:
        active = self.settings.active_provider
        standby = [
            provider.display_name
            for key, provider in self.settings.providers.items()
            if key != self.settings.provider and provider.available
        ]
        standby_text = "、".join(standby) if standby else "未配置"
        return f"当前主模型：{active.display_name}\n备用模型：{standby_text}"

    def switch_provider(self, value: str) -> str:
        with self._provider_lock:
            provider_key = self.settings.resolve_provider(value)
            provider = self.settings.providers[provider_key]
            if not provider.available:
                raise ValueError(f"{provider.label} 尚未配置API密钥")
            if provider_key == self.settings.provider:
                return provider.display_name

            probe = self._client(provider, 20)
            probe.complete(
                [{"role": "user", "content": "只回复OK"}],
                max_tokens=8,
                temperature=0.0,
                thinking=False,
            )
            services = self._build_services(provider)
            self.settings.activate(provider_key)
            self._apply_services(services)
            return provider.display_name

    def route(self, text: str, current_date: date):
        return self._router.route(text, current_date)

    def enter_chat(self, user_id: str) -> None:
        self._sessions.enter(user_id)

    def exit_chat(self, user_id: str) -> None:
        self._sessions.exit(user_id)

    def chat_active(self, user_id: str) -> bool:
        return self._sessions.is_active(user_id)

    def ask_chat(self, user_id: str, question: str) -> str:
        return self._chat.ask(user_id, question)

    def save_pending(self, user_id: str, command: str) -> None:
        self._pending.save(user_id, command)

    def confirm_pending(self, user_id: str):
        return self._pending.confirm(user_id)

    def cancel_pending(self, user_id: str) -> bool:
        return self._pending.cancel(user_id)

    def diagnostic_authorized(self, user_id: str) -> bool:
        users = self.settings.diagnostic_users
        return "*" in users or user_id in users

    def diagnose(self, question: str) -> str:
        return self._diagnostics.run(question)


@dataclass(frozen=True)
class AiPreparedMessage:
    content: str
    handled: bool = False


class AiMessageBridge:
    def __init__(
        self,
        assistant,
        *,
        send_text: Callable[[str, str], None],
        send_markdown: Callable[[str, str], None],
        try_start_cmd: Callable[[str], bool],
        end_cmd: Callable[[], None],
        rename_cmd: Callable[[str], None],
        busy_reply: Callable[[], str],
        is_known_command: Callable[[str], bool],
        run_async: Optional[Callable[[Callable], None]] = None,
        now_fn: Optional[Callable] = None,
    ):
        self._assistant = assistant
        self._send_text = send_text
        self._send_markdown = send_markdown
        self._try_start_cmd = try_start_cmd
        self._end_cmd = end_cmd
        self._rename_cmd = rename_cmd
        self._busy_reply = busy_reply
        self._is_known_command = is_known_command
        self._run_async = run_async or self._start_thread
        self._now_fn = now_fn or self._beijing_now

    def prepare(self, content: str, user_id: str, current_date: date) -> AiPreparedMessage:
        if not self._assistant or not self._assistant.enabled:
            return AiPreparedMessage(content)

        stripped = (content or "").strip()
        provider_command = _parse_provider_command(stripped)
        if provider_command:
            action, provider = provider_command
            if action == "status":
                self._send_text(
                    f"🤖 AI模型配置\n\n{self._assistant.provider_status()}",
                    user_id,
                )
                return AiPreparedMessage(content, handled=True)
            if not self._try_start_cmd("AI模型切换"):
                self._send_text(self._busy_reply(), user_id)
                return AiPreparedMessage(content, handled=True)
            return self._launch_provider_switch(content, user_id, provider)

        if stripped == "确认执行":
            command = self._assistant.confirm_pending(user_id)
            if command:
                self._send_text(f"✅ 已确认：{command}", user_id)
                return AiPreparedMessage(command)
            self._send_text("ℹ️ 没有待确认的智能指令，或确认已超时。", user_id)
            return AiPreparedMessage(content, handled=True)
        if stripped == "取消执行":
            cancelled = self._assistant.cancel_pending(user_id)
            self._send_text("✅ 已取消待执行指令" if cancelled else "ℹ️ 没有待取消的智能指令", user_id)
            return AiPreparedMessage(content, handled=True)

        if stripped == "进入助手模式":
            self._assistant.enter_chat(user_id)
            self._send_text("✅ 已进入助手模式，发送「退出助手模式」即可退出。", user_id)
            return AiPreparedMessage(content, handled=True)
        if stripped == "退出助手模式":
            self._assistant.exit_chat(user_id)
            self._send_text("✅ 已退出助手模式，并清除本次聊天上下文。", user_id)
            return AiPreparedMessage(content, handled=True)

        if self._looks_like_current_time_question(stripped):
            current = self._now_fn()
            self._send_text(f"🕒 当前北京时间：{current:%Y-%m-%d %H:%M:%S}", user_id)
            return AiPreparedMessage(content, handled=True)

        question = self._chat_question(stripped, user_id)
        if question is not None:
            if not question:
                self._send_text("请在「问助手」后面写上想聊的内容。", user_id)
                return AiPreparedMessage(content, handled=True)
            if not self._try_start_cmd("AI聊天"):
                self._send_text(self._busy_reply(), user_id)
                return AiPreparedMessage(content, handled=True)
            return self._launch_chat(content, user_id, question)

        if self._is_known_command(stripped):
            return AiPreparedMessage(content)
        if self._looks_like_casual_chat(stripped):
            if not self._try_start_cmd("AI聊天"):
                self._send_text(self._busy_reply(), user_id)
                return AiPreparedMessage(content, handled=True)
            return self._launch_chat(content, user_id, stripped)
        if not self._try_start_cmd("AI指令识别"):
            self._send_text(self._busy_reply(), user_id)
            return AiPreparedMessage(content, handled=True)

        try:
            route = self._assistant.route(stripped, current_date)
        except Exception:
            logger.error("AI command routing failed", exc_info=True)
            self._end_cmd()
            self._send_text("❌ AI暂时无法理解这条消息。发送「帮助」可查看现有指令。", user_id)
            return AiPreparedMessage(content, handled=True)

        if route.kind == "command":
            self._end_cmd()
            if route.risk == "write":
                self._assistant.save_pending(user_id, route.canonical_command)
                self._send_text(
                    f"我理解为：{route.canonical_command}\n\n回复「确认执行」继续，或回复「取消执行」。",
                    user_id,
                )
                return AiPreparedMessage(content, handled=True)
            return AiPreparedMessage(route.canonical_command)

        if route.kind == "clarify":
            self._end_cmd()
            self._send_text(route.reply, user_id)
            return AiPreparedMessage(content, handled=True)

        if route.kind == "chat":
            self._rename_cmd("AI聊天")
            return self._launch_chat(content, user_id, stripped)

        if route.kind == "diagnose":
            if not self._assistant.diagnostic_authorized(user_id):
                self._end_cmd()
                self._send_text("❌ 当前账号没有线上诊断权限。", user_id)
                return AiPreparedMessage(content, handled=True)
            self._rename_cmd("AI线上诊断")
            self._send_text("⏳ 正在只读检查任务状态、日志和代码，请稍等...", user_id)

            def run_diagnosis():
                try:
                    report = self._assistant.diagnose(stripped)
                    self._send_markdown(report, user_id)
                except DiagnosticAgentError as exc:
                    self._send_text(f"❌ 线上诊断未完成\n{exc}", user_id)
                except Exception:
                    logger.error("AI diagnosis failed", exc_info=True)
                    self._send_text("❌ 线上诊断暂时不可用，请稍后再试。", user_id)
                finally:
                    self._end_cmd()

            try:
                self._run_async(run_diagnosis)
            except Exception:
                logger.error("Failed to start AI diagnostic worker", exc_info=True)
                self._end_cmd()
                self._send_text("❌ 线上诊断启动失败，命令锁已释放，请稍后再试。", user_id)
            return AiPreparedMessage(content, handled=True)

        self._end_cmd()
        self._send_text("请再补充一点信息。", user_id)
        return AiPreparedMessage(content, handled=True)

    def _launch_chat(
        self, content: str, user_id: str, question: str
    ) -> AiPreparedMessage:
        settings = getattr(self._assistant, "settings", None)
        provider = getattr(settings, "active_provider", None)
        label = getattr(provider, "label", "AI")
        self._send_text(f"⏳ {label} 正在思考，请稍等...", user_id)

        def run_chat():
            try:
                answer = self._assistant.ask_chat(user_id, question)
                self._send_text(answer, user_id)
            except Exception:
                logger.error("AI chat failed", exc_info=True)
                self._send_text("❌ AI助手暂时无法回答，请稍后再试。", user_id)
            finally:
                self._end_cmd()

        try:
            self._run_async(run_chat)
        except Exception:
            logger.error("Failed to start AI chat worker", exc_info=True)
            self._end_cmd()
            self._send_text("❌ AI助手启动失败，命令锁已释放，请稍后再试。", user_id)
        return AiPreparedMessage(content, handled=True)

    def _launch_provider_switch(
        self, content: str, user_id: str, provider: str
    ) -> AiPreparedMessage:
        self._send_text(f"⏳ 正在检测{provider}模型，请稍等...", user_id)

        def run_switch():
            try:
                display_name = self._assistant.switch_provider(provider)
                self._send_text(
                    f"✅ AI主模型已切换\n\n当前主模型：{display_name}",
                    user_id,
                )
            except ValueError as exc:
                self._send_text(f"❌ 无法切换AI模型\n{exc}", user_id)
            except Exception:
                logger.error("AI provider switch failed", exc_info=True)
                self._send_text(
                    f"❌ {provider}模型检测失败，仍使用原主模型。",
                    user_id,
                )
            finally:
                self._end_cmd()

        try:
            self._run_async(run_switch)
        except Exception:
            logger.error("Failed to start AI provider switch worker", exc_info=True)
            self._end_cmd()
            self._send_text("❌ AI模型切换启动失败，请稍后再试。", user_id)
        return AiPreparedMessage(content, handled=True)

    def _chat_question(self, content: str, user_id: str):
        for prefix in ("问助手", "问问助手"):
            if content.startswith(prefix):
                return content[len(prefix):].strip(" ：:")
        if self._assistant.chat_active(user_id):
            return content
        return None

    @staticmethod
    def _looks_like_casual_chat(content: str) -> bool:
        normalized = "".join((content or "").split()).lower()
        for mark in "，,。.!！?？~～":
            normalized = normalized.replace(mark, "")
        return normalized in {
            "咪咪",
            "咪咪在吗",
            "咪咪在嘛",
            "咪咪在不在",
            "咪咪你好",
            "在吗",
            "在嘛",
            "在不在",
            "在吗咪咪",
            "在嘛咪咪",
            "在不在咪咪",
            "你好",
            "您好",
            "嗨",
            "哈喽",
            "hello",
            "hi",
        }

    @staticmethod
    def _looks_like_current_time_question(content: str) -> bool:
        normalized = "".join((content or "").split()).lower()
        for mark in "，,。.!！?？~～":
            normalized = normalized.replace(mark, "")
        return normalized in {
            "现在几点",
            "现在几点钟",
            "现在几点了",
            "现在是几点",
            "现在是几点钟",
            "现在时间",
            "现在时间是多少",
            "现在是什么时间",
            "当前时间",
            "当前几点",
            "几点了",
            "几点钟了",
            "北京时间",
            "当前北京时间",
            "系统时间",
            "服务器时间",
        }

    @staticmethod
    def _beijing_now():
        from src.beijing_time import now

        return now()

    @staticmethod
    def _start_thread(fn):
        threading.Thread(target=fn, daemon=True, name="ai-worker").start()


def _parse_provider_command(content: str):
    normalized = re.sub(r"\s+", "", str(content or "")).lower()
    if normalized in {
        "查看ai模型", "查询ai模型", "当前ai模型", "ai模型", "查看大模型",
    }:
        return "status", ""
    if not any(marker in normalized for marker in ("切换", "换成", "改用", "使用")):
        return None
    for alias in ("阿里百炼", "百炼", "通义千问", "千问", "qwen"):
        if alias in normalized:
            return "switch", "百炼"
    for alias in ("longcat", "龙猫"):
        if alias in normalized:
            return "switch", "LongCat"
    if "ai模型" in normalized or "大模型" in normalized:
        return "status", ""
    return None
