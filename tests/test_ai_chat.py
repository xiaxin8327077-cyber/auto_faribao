import pytest

from src.ai_chat import AiChatService, ChatSessionStore
from src.longcat_client import LongCatUnavailableError


class StubClient:
    def __init__(self, replies=None, error=None):
        self.replies = list(replies or [])
        self.error = error
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return self.replies.pop(0)


def test_chat_mode_can_be_entered_and_exited_per_user():
    store = ChatSessionStore()

    store.enter("user-a")
    assert store.is_active("user-a") is True
    assert store.is_active("user-b") is False

    store.exit("user-a")
    assert store.is_active("user-a") is False
    assert store.history("user-a") == []


def test_chat_keeps_only_latest_ten_rounds():
    store = ChatSessionStore(max_turns=10)
    client = StubClient([f"answer-{index}" for index in range(12)])
    service = AiChatService(client, store)

    for index in range(12):
        service.ask("user-a", f"question-{index}")

    history = store.history("user-a")
    assert len(history) == 20
    assert history[0] == {"role": "user", "content": "question-2"}
    assert history[-1] == {"role": "assistant", "content": "answer-11"}


def test_chat_histories_are_isolated_between_users():
    store = ChatSessionStore()
    service = AiChatService(StubClient(["a-answer", "b-answer"]), store)

    service.ask("user-a", "a-question")
    service.ask("user-b", "b-question")

    assert store.history("user-a")[0]["content"] == "a-question"
    assert store.history("user-b")[0]["content"] == "b-question"


def test_failed_model_call_does_not_pollute_history():
    store = ChatSessionStore()
    service = AiChatService(StubClient(error=RuntimeError("boom")), store)

    with pytest.raises(RuntimeError, match="boom"):
        service.ask("user-a", "private question")

    assert store.history("user-a") == []


def test_chat_request_uses_history_and_safe_generation_limits():
    store = ChatSessionStore()
    store.append_exchange("user-a", "earlier", "earlier answer")
    client = StubClient(["new answer"])

    result = AiChatService(client, store).ask("user-a", "new question")

    assert result == "new answer"
    messages, kwargs = client.calls[0]
    assert messages[0]["role"] == "system"
    assert messages[-3:] == [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "new question"},
    ]
    assert kwargs["max_tokens"] <= 1200


def test_chat_retries_one_transient_unavailable_failure():
    class FlakyClient:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise LongCatUnavailableError("temporary")
            return "retry answer"

    store = ChatSessionStore()
    client = FlakyClient()

    result = AiChatService(client, store).ask("user-a", "咪咪在吗")

    assert result == "retry answer"
    assert client.calls == 2
    assert store.history("user-a") == [
        {"role": "user", "content": "咪咪在吗"},
        {"role": "assistant", "content": "retry answer"},
    ]
