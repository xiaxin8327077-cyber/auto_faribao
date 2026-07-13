import pytest

from src.longcat_client import (
    LongCatClient,
    LongCatResponseError,
    LongCatSettings,
    LongCatUnavailableError,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def test_settings_are_loaded_only_from_environment_mapping():
    settings = LongCatSettings.from_env(
        {
            "LONGCAT_ENABLED": "true",
            "LONGCAT_API_KEY": "secret-key",
            "LONGCAT_BASE_URL": "https://example.invalid/openai/",
            "LONGCAT_MODEL": "LongCat-Test",
            "LONGCAT_DIAGNOSTIC_USERS": "user-a, user-b",
        }
    )

    assert settings.available is True
    assert settings.api_key == "secret-key"
    assert settings.base_url == "https://example.invalid/openai"
    assert settings.model == "LongCat-Test"
    assert settings.diagnostic_users == frozenset({"user-a", "user-b"})


def test_settings_are_unavailable_without_key():
    settings = LongCatSettings.from_env({"LONGCAT_ENABLED": "true"})

    assert settings.available is False


def test_complete_posts_bearer_request_with_thinking_disabled():
    session = FakeSession(
        FakeResponse(payload={"choices": [{"message": {"content": "ok"}}]})
    )
    client = LongCatClient(
        api_key="secret-key",
        base_url="https://api.longcat.chat/openai",
        model="LongCat-2.0",
        session=session,
        timeout_seconds=4,
    )

    result = client.complete(
        [{"role": "user", "content": "hello"}],
        max_tokens=123,
        temperature=0.0,
        thinking=False,
    )

    assert result == "ok"
    url, kwargs = session.calls[0]
    assert url == "https://api.longcat.chat/openai/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-key"
    assert kwargs["timeout"] == 4
    assert kwargs["json"] == {
        "model": "LongCat-2.0",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 123,
        "temperature": 0.0,
        "stream": False,
        "thinking": {"type": "disabled"},
    }


def test_complete_maps_rate_limit_to_safe_unavailable_error():
    client = LongCatClient(
        api_key="secret-key",
        session=FakeSession(FakeResponse(status_code=429)),
    )

    with pytest.raises(LongCatUnavailableError, match="暂时不可用") as exc:
        client.complete([{"role": "user", "content": "private prompt"}])

    assert "private prompt" not in str(exc.value)
    assert "secret-key" not in str(exc.value)


def test_complete_rejects_missing_message_content():
    client = LongCatClient(
        api_key="secret-key",
        session=FakeSession(FakeResponse(payload={"choices": []})),
    )

    with pytest.raises(LongCatResponseError, match="响应格式异常"):
        client.complete([{"role": "user", "content": "hello"}])


def test_complete_can_reduce_timeout_for_a_bounded_agent_deadline():
    session = FakeSession(
        FakeResponse(payload={"choices": [{"message": {"content": "ok"}}]})
    )
    client = LongCatClient(api_key="secret-key", session=session, timeout_seconds=10)

    client.complete(
        [{"role": "user", "content": "hello"}], request_timeout_seconds=2.5
    )

    assert session.calls[0][1]["timeout"] == 2.5
