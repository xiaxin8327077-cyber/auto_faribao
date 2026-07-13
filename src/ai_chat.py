import threading
from collections import deque


class ChatSessionStore:
    def __init__(self, max_turns: int = 10):
        self._max_messages = max(1, max_turns) * 2
        self._histories: dict[str, deque] = {}
        self._active_users: set[str] = set()
        self._lock = threading.Lock()

    def enter(self, user_id: str) -> None:
        with self._lock:
            self._active_users.add(user_id)

    def exit(self, user_id: str) -> None:
        with self._lock:
            self._active_users.discard(user_id)
            self._histories.pop(user_id, None)

    def is_active(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._active_users

    def history(self, user_id: str) -> list[dict]:
        with self._lock:
            return [dict(message) for message in self._histories.get(user_id, ())]

    def append_exchange(self, user_id: str, question: str, answer: str) -> None:
        with self._lock:
            history = self._histories.setdefault(
                user_id, deque(maxlen=self._max_messages)
            )
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": answer})


class AiChatService:
    def __init__(self, client, store: ChatSessionStore):
        self._client = client
        self._store = store

    def ask(self, user_id: str, question: str) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是企业微信中的中文助手。简洁、准确地回答问题。"
                    "你不能读取系统日志、代码、配置或业务数据，除非用户另行发起线上诊断。"
                    "你不能执行、承诺执行或伪造任何系统操作。"
                ),
            },
            *self._store.history(user_id),
            {"role": "user", "content": question},
        ]
        answer = self._client.complete(
            messages,
            max_tokens=1000,
            temperature=0.3,
            thinking=False,
        )
        self._store.append_exchange(user_id, question, answer)
        return answer
