from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

TTL_SECONDS = 120.0


def normalize_content_for_compare(content: str) -> str:
    """规范化正文用于比对：统一换行符、去除每行尾随空白、首尾 strip。不改行首空白。"""
    if not content:
        return ""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in content.split("\n")).strip()


def content_hash(content: str) -> str:
    return hashlib.sha256(normalize_content_for_compare(content).encode("utf-8")).hexdigest()[:16]


@dataclass
class EditConfirmation:
    user_id: str
    action: str  # append_today | overwrite_today
    report_date: str
    original_content_hash: str
    original_preview: str
    final_content: str
    created_at: float
    expires_at: float

    def is_expired(self, now: float) -> bool:
        return self.expires_at < now


class EditConfirmationStore:
    def __init__(self, ttl_seconds: float = TTL_SECONDS, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._items: dict[str, EditConfirmation] = {}
        self._lock = threading.Lock()

    def save(self, item: EditConfirmation) -> None:
        with self._lock:
            self._items[item.user_id] = item

    def confirm(self, user_id: str) -> Optional[EditConfirmation]:
        with self._lock:
            item = self._items.pop(user_id, None)
        if item is None or item.is_expired(self._clock()):
            return None
        return item

    def cancel(self, user_id: str) -> bool:
        with self._lock:
            return self._items.pop(user_id, None) is not None

    def peek(self, user_id: str) -> Optional[EditConfirmation]:
        with self._lock:
            item = self._items.get(user_id)
        if item is None or item.is_expired(self._clock()):
            return None
        return item


# ---- 按用户的修改进行中标记（仅崩溃感知，不自动恢复）----
_marker_lock = threading.Lock()
_MARKER_GLOB = "daily_report_modify_inprogress_*.json"


def _marker_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _safe_user(user_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", user_id or "unknown")


def _marker_path(user_id: str) -> Path:
    return _marker_dir() / f"daily_report_modify_inprogress_{_safe_user(user_id)}.json"


def begin_modify_marker(user_id: str, report_date: str, content_hash: str) -> None:
    path = _marker_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"user_id": user_id, "report_date": report_date,
            "content_hash": content_hash, "started_at": time.time()}
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _marker_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def clear_modify_marker(user_id: str) -> None:
    with _marker_lock:
        try:
            _marker_path(user_id).unlink()
        except FileNotFoundError:
            pass


def peek_stale_modify_markers() -> list:
    """只读全部残留标记，不删除。调用方通知成功后再 clear_modify_marker。"""
    results = []
    with _marker_lock:
        for p in _marker_dir().glob(_MARKER_GLOB):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            if data:
                results.append(data)
    return results
