from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.beijing_time import now as _now

SCHEMA_VERSION = 1
MAX_CONTENT_CHARS = 10000
_DRAFT_FILENAME = "daily_report_draft.json"
_lock = threading.RLock()
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$")
_VALID_ORIGINS = ("manual", "smart_sheet_append")


def _draft_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / _DRAFT_FILENAME


def _validate_date(report_date: str) -> None:
    if not isinstance(report_date, str):
        raise DraftError("report_date 非字符串")
    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError as exc:
        raise DraftError(f"report_date 非真实日期: {report_date!r}") from exc


@dataclass
class Draft:
    report_date: str
    content: str
    origin: str
    revision: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "report_date": self.report_date,
                "content": self.content, "origin": self.origin, "revision": self.revision,
                "created_at": self.created_at, "updated_at": self.updated_at}


class DraftError(Exception):
    pass


class DraftCorruptError(DraftError):
    pass


def _now_iso() -> str:
    return _now().isoformat()


def _validate_content(content: str) -> None:
    if not content or not content.strip():
        raise DraftError("正文不能为空")
    if len(content) > MAX_CONTENT_CHARS:
        raise DraftError(f"正文超过 {MAX_CONTENT_CHARS} 个字符，实际 {len(content)}，已拒绝保存")


def _parse_draft(data: object) -> Draft:
    if not isinstance(data, dict):
        raise DraftCorruptError("草稿格式损坏")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise DraftCorruptError(f"不支持的草稿版本: {data.get('schema_version')}")
    try:
        report_date = data["report_date"]
        content = data["content"]
        origin = data["origin"]
        revision = data["revision"]
        created_at = data["created_at"]
        updated_at = data["updated_at"]
    except KeyError as exc:
        raise DraftCorruptError(f"草稿字段缺失: {exc}") from exc
    try:
        datetime.strptime(str(report_date), "%Y-%m-%d")
    except (ValueError, TypeError) as exc:
        raise DraftCorruptError(f"report_date 非真实日期: {report_date!r}") from exc
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_CONTENT_CHARS:
        raise DraftCorruptError("content 非字符串、为空或超长")
    if not isinstance(origin, str) or origin not in _VALID_ORIGINS:
        raise DraftCorruptError(f"origin 非法: {origin!r}")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise DraftCorruptError(f"revision 非法: {revision!r}")
    if not isinstance(created_at, str) or not _ISO_RE.match(created_at):
        raise DraftCorruptError("created_at 格式错误")
    if not isinstance(updated_at, str) or not _ISO_RE.match(updated_at):
        raise DraftCorruptError("updated_at 格式错误")
    return Draft(str(report_date), str(content), str(origin),
                  int(revision), str(created_at), str(updated_at))


def _load_unlocked(path: Path) -> Optional[Draft]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise DraftCorruptError(f"草稿文件损坏: {exc}") from exc
    return _parse_draft(data)


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def save_draft(report_date: str, content: str, origin: str) -> Draft:
    if origin not in _VALID_ORIGINS:
        raise DraftError(f"未知 origin: {origin}")
    _validate_date(report_date)
    _validate_content(content)
    with _lock:
        path = _draft_path()
        existing = _load_unlocked(path)
        now_iso = _now_iso()
        if existing and existing.report_date == report_date:
            created_at, revision = existing.created_at, existing.revision + 1
        else:
            created_at, revision = now_iso, 1
        draft = Draft(report_date, content, origin, revision, created_at, now_iso)
        _write_atomic(path, draft.to_dict())
        return draft


def append_draft(report_date: str, content: str) -> Draft:
    _validate_date(report_date)
    _validate_content(content)
    with _lock:
        path = _draft_path()
        existing = _load_unlocked(path)
        if existing is None:
            raise DraftError('无草稿可追加，请先发送"设置日报"')
        if existing.report_date != report_date:
            raise DraftError("草稿日期与今天不匹配，无法追加")
        new_content = existing.content + "\n" + content
        _validate_content(new_content)
        draft = Draft(existing.report_date, new_content, existing.origin,
                      existing.revision + 1, existing.created_at, _now_iso())
        _write_atomic(path, draft.to_dict())
        return draft


def load_draft() -> Optional[Draft]:
    with _lock:
        return _load_unlocked(_draft_path())


def load_draft_for_date(report_date: str) -> Optional[Draft]:
    return load_draft()


def clear_draft(expected_report_date: Optional[str] = None,
               expected_revision: Optional[int] = None) -> bool:
    with _lock:
        path = _draft_path()
        if not path.exists():
            return False
        if expected_report_date is None and expected_revision is None:
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False
        existing = _load_unlocked(path)
        if existing is None:
            return False
        if expected_report_date is not None and existing.report_date != expected_report_date:
            return False
        if expected_revision is not None and existing.revision != expected_revision:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
