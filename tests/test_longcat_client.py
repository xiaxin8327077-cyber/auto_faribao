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


def test_settings_load_qwen_as_primary_and_keep_longcat_available(tmp_path):
    state_path = tmp_path / "ai_provider_state.json"
    settings = LongCatSettings.from_env(
        {
            "AI_ENABLED": "true",
            "AI_PROVIDER": "qwen",
            "AI_DIAGNOSTIC_USERS": "*",
            "QWEN_API_KEY": "qwen-secret",
            "QWEN_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1/",
            "QWEN_MODEL": "qwen3.7-plus",
            "LONGCAT_API_KEY": "longcat-secret",
            "LONGCAT_BASE_URL": "https://api.longcat.chat/openai",
            "LONGCAT_MODEL": "LongCat-2.0",
        },
        state_path=state_path,
    )

    assert settings.available is True
    assert settings.provider == "qwen"
    assert settings.display_name == "百炼 qwen3.7-plus"
    assert settings.api_key == "qwen-secret"
    assert settings.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert settings.model == "qwen3.7-plus"
    assert settings.diagnostic_users == frozenset({"*"})
    assert settings.available_providers == ("qwen", "longcat")


def test_persisted_provider_selection_overrides_environment_default(tmp_path):
    state_path = tmp_path / "ai_provider_state.json"
    state_path.write_text('{"provider":"longcat"}', encoding="utf-8")

    settings = LongCatSettings.from_env(
        {
            "AI_ENABLED": "true",
            "AI_PROVIDER": "qwen",
            "QWEN_API_KEY": "qwen-secret",
            "LONGCAT_API_KEY": "longcat-secret",
        },
        state_path=state_path,
    )

    assert settings.provider == "longcat"
    assert settings.api_key == "longcat-secret"


def test_provider_selection_is_written_atomically(tmp_path):
    state_path = tmp_path / "ai_provider_state.json"
    settings = LongCatSettings.from_env(
        {
            "AI_ENABLED": "true",
            "AI_PROVIDER": "qwen",
            "QWEN_API_KEY": "qwen-secret",
            "LONGCAT_API_KEY": "longcat-secret",
        },
        state_path=state_path,
    )

    settings.activate("longcat")

    assert state_path.read_text(encoding="utf-8") == '{\n  "provider": "longcat"\n}'
    assert not state_path.with_suffix(".json.tmp").exists()


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


def test_qwen_request_uses_official_thinking_parameter_and_normalized_url():
    session = FakeSession(
        FakeResponse(payload={"choices": [{"message": {"content": "ok"}}]})
    )
    client = LongCatClient(
        api_key="qwen-secret",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/",
        model="qwen3.7-plus",
        provider_name="百炼",
        thinking_parameter="enable_thinking",
        session=session,
        timeout_seconds=10,
    )

    assert client.complete(
        [{"role": "user", "content": "hello"}], thinking=False
    ) == "ok"

    url, kwargs = session.calls[0]
    assert url == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert kwargs["json"]["enable_thinking"] is False
    assert "thinking" not in kwargs["json"]


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
