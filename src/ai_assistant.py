import logging
import threading
from dataclasses import dataclass
from datetime import date
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
    ):
        self.settings = settings
        self.enabled = settings.available
        self._router = router
        self._chat = chat
        self._diagnostics = diagnostics
        self._sessions = sessions or ChatSessionStore(max_turns=10)
        self._pending = pending or AiPendingCommandStore(ttl_seconds=120)

    @classmethod
    def from_env(cls, cfg, project_root, env: Optional[Mapping[str, str]] = None):
        settings = LongCatSettings.from_env(env)
        if not settings.available:
            return cls(settings)

        router_client = LongCatClient(
            settings.api_key,
            settings.base_url,
            settings.model,
            timeout_seconds=4,
        )
        chat_client = LongCatClient(
            settings.api_key,
            settings.base_url,
            settings.model,
            timeout_seconds=15,
        )
        diagnostic_client = LongCatClient(
            settings.api_key,
            settings.base_url,
            settings.model,
            timeout_seconds=15,
        )
        sessions = ChatSessionStore(max_turns=10)
        toolbox = DiagnosticToolbox(project_root, cfg)
        return cls(
            settings,
            router=AiCommandRouter(router_client),
            chat=AiChatService(chat_client, sessions),
            diagnostics=DiagnosticAgent(
                diagnostic_client,
                toolbox,
                max_tool_calls=3,
                timeout_seconds=30,
            ),
            sessions=sessions,
            pending=AiPendingCommandStore(ttl_seconds=120),
        )

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
            logger.error("LongCat command routing failed", exc_info=True)
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
                    logger.error("LongCat diagnosis failed", exc_info=True)
                    self._send_text("❌ 线上诊断暂时不可用，请稍后再试。", user_id)
                finally:
                    self._end_cmd()

            try:
                self._run_async(run_diagnosis)
            except Exception:
                logger.error("Failed to start LongCat diagnostic worker", exc_info=True)
                self._end_cmd()
                self._send_text("❌ 线上诊断启动失败，命令锁已释放，请稍后再试。", user_id)
            return AiPreparedMessage(content, handled=True)

        self._end_cmd()
        self._send_text("请再补充一点信息。", user_id)
        return AiPreparedMessage(content, handled=True)

    def _launch_chat(
        self, content: str, user_id: str, question: str
    ) -> AiPreparedMessage:
        self._send_text("⏳ LongCat 正在思考，请稍等...", user_id)

        def run_chat():
            try:
                answer = self._assistant.ask_chat(user_id, question)
                self._send_text(answer, user_id)
            except Exception:
                logger.error("LongCat chat failed", exc_info=True)
                self._send_text("❌ AI助手暂时无法回答，请稍后再试。", user_id)
            finally:
                self._end_cmd()

        try:
            self._run_async(run_chat)
        except Exception:
            logger.error("Failed to start LongCat chat worker", exc_info=True)
            self._end_cmd()
            self._send_text("❌ AI助手启动失败，命令锁已释放，请稍后再试。", user_id)
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
        threading.Thread(target=fn, daemon=True, name="longcat-worker").start()
