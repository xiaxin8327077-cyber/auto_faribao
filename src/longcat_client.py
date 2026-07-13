import os
from dataclasses import dataclass
from typing import Mapping, Optional

import requests


class LongCatError(RuntimeError):
    """Base error for safe, user-facing LongCat failures."""


class LongCatUnavailableError(LongCatError):
    pass


class LongCatResponseError(LongCatError):
    pass


@dataclass(frozen=True)
class LongCatSettings:
    enabled: bool = False
    api_key: str = ""
    base_url: str = "https://api.longcat.chat/openai"
    model: str = "LongCat-2.0"
    diagnostic_users: frozenset[str] = frozenset()

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None):
        values = os.environ if env is None else env
        enabled = str(values.get("LONGCAT_ENABLED", "false")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        users = frozenset(
            item.strip()
            for item in str(values.get("LONGCAT_DIAGNOSTIC_USERS", "")).split(",")
            if item.strip()
        )
        return cls(
            enabled=enabled,
            api_key=str(values.get("LONGCAT_API_KEY", "")).strip(),
            base_url=str(
                values.get("LONGCAT_BASE_URL", "https://api.longcat.chat/openai")
            ).strip().rstrip("/"),
            model=str(values.get("LONGCAT_MODEL", "LongCat-2.0")).strip(),
            diagnostic_users=users,
        )


class LongCatClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.longcat.chat/openai",
        model: str = "LongCat-2.0",
        session=None,
        timeout_seconds: float = 10,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._session = session or requests.Session()
        self._timeout_seconds = timeout_seconds

    def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 800,
        temperature: float = 0.0,
        thinking: bool = False,
    ) -> str:
        try:
            response = self._session.post(
                f"{self._base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": False,
                    "thinking": {"type": "enabled" if thinking else "disabled"},
                },
                timeout=self._timeout_seconds,
            )
        except requests.RequestException as exc:
            raise LongCatUnavailableError("LongCat 暂时不可用，请稍后再试") from exc

        if response.status_code in (401, 403):
            raise LongCatUnavailableError("LongCat 鉴权失败，请检查服务配置")
        if response.status_code == 429 or response.status_code >= 500:
            raise LongCatUnavailableError("LongCat 暂时不可用，请稍后再试")
        if response.status_code >= 400:
            raise LongCatResponseError("LongCat 请求失败")

        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LongCatResponseError("LongCat 响应格式异常") from exc
        if not isinstance(content, str) or not content.strip():
            raise LongCatResponseError("LongCat 响应格式异常")
        return content.strip()
