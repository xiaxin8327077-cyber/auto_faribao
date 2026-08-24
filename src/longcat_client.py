import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

import requests


class LongCatError(RuntimeError):
    """Base error for safe, user-facing AI provider failures."""


class LongCatUnavailableError(LongCatError):
    pass


class LongCatResponseError(LongCatError):
    pass


@dataclass(frozen=True)
class AiProviderConfig:
    key: str
    label: str
    api_key: str
    base_url: str
    model: str
    thinking_parameter: str = "thinking"

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def display_name(self) -> str:
        return f"{self.label} {self.model}"


def _default_providers() -> dict[str, AiProviderConfig]:
    return {
        "longcat": AiProviderConfig(
            key="longcat",
            label="LongCat",
            api_key="",
            base_url="https://api.longcat.chat/openai",
            model="LongCat-2.0",
        )
    }


@dataclass
class LongCatSettings:
    enabled: bool = False
    provider: str = "longcat"
    providers: dict[str, AiProviderConfig] = field(default_factory=_default_providers)
    diagnostic_users: frozenset[str] = frozenset()
    state_path: Optional[Path] = None

    @property
    def active_provider(self) -> AiProviderConfig:
        return self.providers.get(self.provider) or self.providers["longcat"]

    @property
    def api_key(self) -> str:
        return self.active_provider.api_key

    @property
    def base_url(self) -> str:
        return self.active_provider.base_url

    @property
    def model(self) -> str:
        return self.active_provider.model

    @property
    def display_name(self) -> str:
        return self.active_provider.display_name

    @property
    def thinking_parameter(self) -> str:
        return self.active_provider.thinking_parameter

    @property
    def available_providers(self) -> tuple[str, ...]:
        return tuple(
            key for key in ("qwen", "longcat")
            if key in self.providers and self.providers[key].available
        )

    @property
    def available(self) -> bool:
        return self.enabled and self.active_provider.available

    @property
    def any_available(self) -> bool:
        return self.enabled and bool(self.available_providers)

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        state_path=None,
    ):
        values = os.environ if env is None else env
        enabled_value = values.get("AI_ENABLED", values.get("LONGCAT_ENABLED", "false"))
        enabled = str(enabled_value).strip().lower() in {
            "1", "true", "yes", "on",
        }
        diagnostic_value = values.get(
            "AI_DIAGNOSTIC_USERS",
            values.get("LONGCAT_DIAGNOSTIC_USERS", ""),
        )
        users = frozenset(
            item.strip()
            for item in str(diagnostic_value).split(",")
            if item.strip()
        )
        providers = {
            "qwen": AiProviderConfig(
                key="qwen",
                label="百炼",
                api_key=str(values.get("QWEN_API_KEY", "")).strip(),
                base_url=str(
                    values.get(
                        "QWEN_BASE_URL",
                        "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    )
                ).strip().rstrip("/"),
                model=str(values.get("QWEN_MODEL", "qwen3.7-plus")).strip(),
                thinking_parameter="enable_thinking",
            ),
            "longcat": AiProviderConfig(
                key="longcat",
                label="LongCat",
                api_key=str(values.get("LONGCAT_API_KEY", "")).strip(),
                base_url=str(
                    values.get("LONGCAT_BASE_URL", "https://api.longcat.chat/openai")
                ).strip().rstrip("/"),
                model=str(values.get("LONGCAT_MODEL", "LongCat-2.0")).strip(),
            ),
        }
        requested_provider = _normalize_provider(
            str(values.get("AI_PROVIDER", "longcat"))
        ) or "longcat"
        resolved_state_path = Path(state_path) if state_path else None
        persisted_provider = _read_persisted_provider(resolved_state_path)
        provider = persisted_provider or requested_provider
        if provider not in providers:
            provider = requested_provider if requested_provider in providers else "longcat"
        return cls(
            enabled=enabled,
            provider=provider,
            providers=providers,
            diagnostic_users=users,
            state_path=resolved_state_path,
        )

    def resolve_provider(self, value: str) -> str:
        provider = _normalize_provider(value)
        if not provider or provider not in self.providers:
            raise ValueError("不支持的AI模型，可选：百炼、LongCat")
        return provider

    def activate(self, value: str) -> AiProviderConfig:
        provider = self.resolve_provider(value)
        config = self.providers[provider]
        if not config.available:
            raise ValueError(f"{config.label} 尚未配置API密钥")
        self._persist(provider)
        self.provider = provider
        return config

    def _persist(self, provider: str) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps({"provider": provider}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, self.state_path)
        try:
            os.chmod(self.state_path, 0o600)
        except OSError:
            pass


def _normalize_provider(value: str) -> str:
    normalized = "".join(str(value or "").strip().lower().split())
    aliases = {
        "qwen": "qwen",
        "千问": "qwen",
        "通义": "qwen",
        "通义千问": "qwen",
        "百炼": "qwen",
        "阿里百炼": "qwen",
        "longcat": "longcat",
        "longcat-2.0": "longcat",
        "龙猫": "longcat",
    }
    return aliases.get(normalized, "")


def _read_persisted_provider(path: Optional[Path]) -> str:
    if not path or not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _normalize_provider(data.get("provider", "")) if isinstance(data, dict) else ""
    except (OSError, ValueError, TypeError):
        return ""


class LongCatClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.longcat.chat/openai",
        model: str = "LongCat-2.0",
        provider_name: str = "LongCat",
        thinking_parameter: str = "thinking",
        session=None,
        timeout_seconds: float = 10,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._provider_name = provider_name
        self._thinking_parameter = thinking_parameter
        self._session = session or requests.Session()
        self._timeout_seconds = timeout_seconds

    def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 800,
        temperature: float = 0.0,
        thinking: bool = False,
        request_timeout_seconds: Optional[float] = None,
    ) -> str:
        request_timeout = self._timeout_seconds
        if request_timeout_seconds is not None:
            request_timeout = max(
                0.1, min(self._timeout_seconds, float(request_timeout_seconds))
            )
        try:
            payload = {
                "model": self._model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            }
            if self._thinking_parameter == "enable_thinking":
                payload["enable_thinking"] = bool(thinking)
            else:
                payload["thinking"] = {
                    "type": "enabled" if thinking else "disabled"
                }
            response = self._session.post(
                _chat_completions_url(self._base_url),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=request_timeout,
            )
        except requests.RequestException as exc:
            raise LongCatUnavailableError("AI模型暂时不可用，请稍后再试") from exc

        if response.status_code in (401, 403):
            raise LongCatUnavailableError("AI模型鉴权失败，请检查服务配置")
        if response.status_code == 429 or response.status_code >= 500:
            raise LongCatUnavailableError("AI模型暂时不可用，请稍后再试")
        if response.status_code >= 400:
            raise LongCatResponseError("AI模型请求失败")

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LongCatResponseError("AI模型响应格式异常") from exc
        if not isinstance(content, str) or not content.strip():
            raise LongCatResponseError("AI模型响应格式异常")
        return content.strip()


def _chat_completions_url(base_url: str) -> str:
    normalized = str(base_url or "").rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"
