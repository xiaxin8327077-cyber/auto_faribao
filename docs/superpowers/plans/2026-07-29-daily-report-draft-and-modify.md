# 日报草稿暂存、追加与提交后修改 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有企微日报流程上增加草稿（设置/追加/查看/清除）、提交后按准确日期修改当天 OA 日报（追加/覆盖）与二次确认，且不破坏现有提交流程。

**Architecture:** 自底向上新增两个纯逻辑模块（草稿持久化、OA 修改二次确认 + 按用户修改进行中标记），改造 `target.modify_daily_report` 为真三态查询、按日期定位、重新导航回读、提交前后异常分流、标记在浏览器锁内创建/清除，在 `server.py` 增加原始多行解析（最早分隔符、正文不 strip）、`MsgId` 去重（含在途超时）、后台线程化处理、草稿优先提交与条件清除（skipped/未知动作不误报成功）、追加日报自动分流、8 指令分派、AI 路由真实接入，正文由服务器从原始消息确定性提取。

**Tech Stack:** Python 3.11、pytest、Playwright（OA 浏览器操作）、threading 锁、JSON 文件持久化、Flask（企微回调）。

## Global Constraints

- 正文从企微到草稿/确认/OA 全链路原样保留换行、编号和空格，不经过 `_normalize_message_text()` 压平；动作后正文不做 `.strip()`（仅用 `.strip()` 判空）。
- 动作与正文的分隔符取“换行/中文冒号/英文冒号”中**最先出现**的有效分隔符。
- 所有 OA 读取/修改按北京时间当天 `YYYY-MM-DD` 定位。
- OA 今日状态查询必须真三态：**表格正常加载且遍历后无目标**才返回 `absent`；表格加载超时/异常返回 `error`；找到返回 `exists`。禁止把加载失败当未提交。
- OA 修改提交后必须重新导航、重新定位当天行后回读，禁止复用旧行对象。
- `submitted` 标志在“即将提交”（`_submit_dialog` 调用前）置 True；之后任何异常都报 `unknown`，提交前异常报 `failed`。
- OA 浏览器操作只用一种锁/登录方式：`_login_and_navigate(cfg)` + finally 释放 `_BROWSER_LOCK`，**禁止**再包 `browser_operation()`（非重入锁重复获取死锁）。
- 修改进行中标记的 `begin`/`clear` 在 `modify_daily_report` 内、浏览器锁持有期间完成；`clear` 在 `_BROWSER_LOCK.release()` **之前**执行，避免同用户第二次修改覆盖后误删。
- 耗时 OA/智能文档操作在后台线程执行，回调线程快速返回 200。
- `skipped`（已审核跳过）只发提示文本、不调 `notify_report_success`；非 `submit/overwrite/skipped` 的未知动作报“结果无法确认”并返回 False，不报成功、不清草稿。
- 通知失败不能把已写入 OA 翻成失败；草稿清除结果并入单条成功通知，成功也显示“草稿已清除”。
- 修改进行中标记按用户分文件；启动期 `peek` 只读不删，通知成功后才 `clear`。
- 不修改 `report_builder.py`、`extractor.py`、`processor.py`、`scheduler.py`、`cookies_checker.py`、`nav_dashboard.py`。
- 草稿 `<项目根>/data/daily_report_draft.json`；标记 `<项目根>/data/daily_report_modify_inprogress_<user>.json`。
- 北京时间用 `src.beijing_time`：`today_str()` 得 `YYYY-MM-DD`，`now().isoformat()` 带 `+08:00`。
- 不引入新依赖；不自动重试 OA 修改；不增加服务自动重启；不持久化待确认队列。
- 测试：`pytest` + `monkeypatch`，函数内延迟 `import`。`check_cookies` 在 `auto_submit_if_needed` 内延迟 `from src.cookies_checker import`，故 patch `src.cookies_checker.check_cookies`；`server` 顶层导入的 `submit_daily_report`/`notify_report_success` 等按需 patch `server.<name>` 或源模块。
- `conftest.py` 已把项目根加入 `sys.path`。
- 提交 commit 用中文 `feat:/fix:/test:/docs:` 前缀；每次 task 末尾 commit。

## File Structure

| 文件 | 类型 | 责任 |
|------|------|------|
| `src/daily_report_draft.py` | 新增 | Draft、严格字段与真实日期校验、原子写入、可重入锁、条件清除 |
| `src/daily_report_edit_confirmation.py` | 新增 | EditConfirmation、内容规范化与哈希、按用户 120s 单次消费（peek 过滤过期）、按用户修改进行中标记（peek 只读、clear 按用户） |
| `src/target.py` | 修改 | `modify_daily_report`（真三态查询+按日期定位+重新导航回读+提交前后异常分流+锁内标记）、`query_today_report`（exists/absent/error）、`_find_report_row_on_pages`（真三态）、`_check_pre_modify`、`_verify_after_modify`、`_modify_failure_result` |
| `src/server.py` | 修改 | 原始正文保留、`parse_daily_report_edit_command`（最早分隔符、正文不 strip）、`_extract_body_from_raw`、`MsgId` 去重（在途超时）、`_run_in_background`（成败决定 processed）、`_send_long_text`、草稿优先提交+条件清除+通知安全（skipped/未知动作不误报）、`_canonical_submit_action`、追加日报分流、8 指令分派、AI 接入、帮助菜单、启动期残留标记检查 |
| `src/ai_command_router.py` | 修改 | 提示词新增 6 动作；分类为非写风险 |
| `src/wechat_notifier.py` | 修改 | `smart_sheet_append` 文案、成功通知显示实际动作与 `draft_note` |
| `.gitignore` | 修改 | 忽略 `data/` 运行态文件 |
| `tests/test_daily_report_draft.py` | 新增 | 持久化、严格校验（含无效日期）、并发 |
| `tests/test_daily_report_edit.py` | 新增 | 二次确认、peek 过期、按用户标记、OA 修改真三态/fake-page（含翻页/超时）、标记锁内生命周期 |
| `tests/test_server_command_normalization.py` | 修改 | 解析（含混合分隔符/首尾空格保留）、AI 接入、MsgId 去重、分段、帮助“日报指令” |
| `tests/test_report_recovery.py` | 修改 | 草稿优先提交、条件清除、skipped/未知动作不误报、通知不翻转、清除失败降级 |

依赖顺序：**Task 0**（format_form 兼容性闸门，安全验证）→ Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6 → Task 7 → Task 8 → Task 9 → Task 10。

---

### Task 0: format_report 兼容性闸门（安全验证，不污染生产）

**Why first:** 草稿路径有意跳过 `processor.format_report()` 以原样保留正文。若 OA 不接受未规整正文，整个草稿提交方向需返工。`submit_daily_report()` 不是无害验证（今天有未审核日报时会先删再提交），故本 task 必须安全。

**Files:** 无代码改动（真机验证 + 记录结论）。

- [ ] **Step 1: 选定安全环境**

优先使用**测试账号 / 测试 OA 环境**。若只能用生产账号：必须选择**当天无正式日报**的时机（先 `get_report_content_by_date` 确认今天无日报），并准备备份与恢复步骤。禁止在已有正式日报时直接用测试正文覆盖。

- [ ] **Step 2: 备份（若存在日报）**

若今天已有日报，先用 `get_report_content_by_date(cfg, today)` 读取并保存原始正文到本地文件，供恢复。

- [ ] **Step 3: 提交未规整测试正文**

用一段含多行、中文编号、行内空格、尾随空白的未规整正文（不经过 `format_report`），通过 `submit_daily_report(content, cfg)` 提交。

- [ ] **Step 4: 回读比对**

用 `get_report_content_by_date(cfg, today)` 回读。用 `normalize_content_for_compare`（去每行尾随空白+统一换行）比对回读与提交正文：
- 换行、编号、行内空格必须保留；
- 尾随空白若被 OA 清理，可接受（草稿提交后校验本就用规范化比对）。

- [ ] **Step 5: 收尾（恢复或删除测试数据）**

- 若 Step 2 有备份（今天原本有日报）：用 `modify_daily_report(原正文, cfg, today)` 恢复当天正文，回读确认恢复成功。
- 若 Step 2 无日报（空白天）：用 `delete_daily_report(cfg, today)` 删除测试日报，回读确认已删除（`get_report_content_by_date` 返回空且 status=absent）。
- 若恢复或删除失败：立即人工登录 OA 清理测试数据，不得留下测试正文。

- [ ] **Step 6: 分支决策**

- OA 接受未规整正文（规范化后一致）→ 继续后续 Task。
- OA 拒绝或显著变形换行/编号 → 停止，回设计：草稿提交前需经 `format_report` 或等价规整，更新设计后再继续。

- [ ] **Step 7:** 把结论写入设计文档“草稿优先提交”step 4 的注记。无 commit（除非更新设计）。

---

### Task 1: 草稿数据模型与持久化（严格校验 + 真实日期校验）

**Files:**
- Create: `src/daily_report_draft.py`
- Test: `tests/test_daily_report_draft.py`

**Interfaces:**
- Produces: `Draft`、`DraftError`、`DraftCorruptError`、`save_draft`/`append_draft`/`load_draft`/`load_draft_for_date`/`clear_draft`，常量 `SCHEMA_VERSION=1`、`MAX_CONTENT_CHARS=10000`。`save_draft`/`append_draft` 写入前用 `datetime.strptime` 校验 `report_date` 为真实日期。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_daily_report_draft.py
import json
import threading

import pytest


def _mod():
    import src.daily_report_draft as m
    return m


def test_save_and_load_multiline(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    body = "一、【项目A】任务：描述\n二、【项目B】任务：描述"
    assert m.save_draft("2026-07-29", body, "manual").revision == 1
    assert m.load_draft().content == body


def test_append_preserves_newlines_and_origin(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    m.save_draft("2026-07-29", "一、原内容", "manual")
    draft = m.append_draft("2026-07-29", "二、追加内容")
    assert draft.content == "一、原内容\n二、追加内容" and draft.origin == "manual" and draft.revision == 2


def test_save_rejects_invalid_real_date(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    with pytest.raises(m.DraftError): m.save_draft("2026-99-99", "x", "manual")
    with pytest.raises(m.DraftError): m.save_draft("2026/07/29", "x", "manual")
    assert not p.exists()


def test_empty_and_oversize_rejected(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    with pytest.raises(m.DraftError): m.save_draft("2026-07-29", "   ", "manual")
    with pytest.raises(m.DraftError): m.save_draft("2026-07-29", "x" * (m.MAX_CONTENT_CHARS + 1), "manual")


def test_corrupt_and_bad_fields_raise(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(m.DraftCorruptError): m.load_draft()
    p.write_text(json.dumps({"schema_version": 1, "report_date": "2026-99-99", "content": "x",
        "origin": "manual", "revision": 1, "created_at": "2026-07-29T16:30:00+08:00",
        "updated_at": "2026-07-29T16:30:00+08:00"}), encoding="utf-8")
    with pytest.raises(m.DraftCorruptError): m.load_draft()
    p.write_text(json.dumps({"schema_version": 1, "report_date": "2026-07-29", "content": "x",
        "origin": "unknown", "revision": 1, "created_at": "2026-07-29T16:30:00+08:00",
        "updated_at": "2026-07-29T16:30:00+08:00"}), encoding="utf-8")
    with pytest.raises(m.DraftCorruptError): m.load_draft()
    p.write_text(json.dumps({"schema_version": 1, "report_date": "2026-07-29", "content": "x",
        "origin": "manual", "revision": True, "created_at": "2026-07-29T16:30:00+08:00",
        "updated_at": "2026-07-29T16:30:00+08:00"}), encoding="utf-8")
    with pytest.raises(m.DraftCorruptError): m.load_draft()


def test_expired_draft_returned_not_deleted(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    m.save_draft("2026-07-28", "昨日", "manual")
    loaded = m.load_draft_for_date("2026-07-29")
    assert loaded is not None and loaded.report_date == "2026-07-28" and p.exists()


def test_clear_requires_match(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    draft = m.save_draft("2026-07-29", "今日", "manual")
    assert m.clear_draft(expected_report_date="2026-07-29", expected_revision=draft.revision + 1) is False
    assert m.clear_draft(expected_report_date="2026-07-29", expected_revision=draft.revision) is True
    assert not p.exists()


def test_concurrent_append_no_loss(monkeypatch, tmp_path):
    m = _mod()
    p = tmp_path / "daily_report_draft.json"
    monkeypatch.setattr(m, "_draft_path", lambda: p)
    m.save_draft("2026-07-29", "基线", "manual")
    lines = [f"<<{i}>>line" for i in range(10)]  # 互不为子串
    def append_one(ln): m.append_draft("2026-07-29", ln)
    ts = [threading.Thread(target=append_one, args=(ln,)) for ln in lines]
    for t in ts: t.start()
    for t in ts: t.join()
    content = m.load_draft().content
    for ln in lines:
        assert ln in content
```

- [ ] **Step 2:** `python -m pytest tests/test_daily_report_draft.py -v` → 全 FAIL。
- [ ] **Step 3:** 实现草稿模块（严格校验 + 真实日期）

```python
# src/daily_report_draft.py
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
        report_date = data["report_date"]; content = data["content"]; origin = data["origin"]
        revision = data["revision"]; created_at = data["created_at"]; updated_at = data["updated_at"]
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
    return Draft(str(report_date), str(content), str(origin), int(revision), str(created_at), str(updated_at))


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
            raise DraftError("无草稿可追加，请先发送“设置日报”")
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
                path.unlink(); return True
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
            path.unlink(); return True
        except FileNotFoundError:
            return False
```

- [ ] **Step 4:** `python -m pytest tests/test_daily_report_draft.py -v` → PASS。
- [ ] **Step 5:** Commit `feat: 新增日报草稿持久化模块（严格校验+真实日期）`。

---

### Task 2: OA 修改二次确认存储 + 按用户修改进行中标记

**Files:**
- Create: `src/daily_report_edit_confirmation.py`
- Test: `tests/test_daily_report_edit.py`

**Interfaces:**
- Produces: `EditConfirmation`、`EditConfirmationStore`（`save`/`confirm`/`cancel`/`peek`，peek 过滤过期）、`normalize_content_for_compare`（仅去每行尾随空白+统一换行+首尾 strip，不改行首）、`content_hash`、`begin_modify_marker(user_id, report_date, content_hash)`、`clear_modify_marker(user_id)`、`peek_stale_modify_markers() -> list[dict]`（只读不删）。常量 `TTL_SECONDS=120.0`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_daily_report_edit.py
import pytest


def _mod():
    import src.daily_report_edit_confirmation as m
    return m


def test_save_confirm_single_consume(monkeypatch):
    m = _mod(); clk = [1000.0]
    store = m.EditConfirmationStore(clock=lambda: clk[0])
    item = m.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
        original_content_hash=m.content_hash("原正文"), original_preview="原正文", final_content="新正文",
        created_at=clk[0], expires_at=clk[0] + m.TTL_SECONDS)
    store.save(item)
    assert store.confirm("u1").final_content == "新正文"
    assert store.confirm("u1") is None


def test_peek_filters_expired(monkeypatch):
    m = _mod(); clk = [1000.0]
    store = m.EditConfirmationStore(clock=lambda: clk[0])
    item = m.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
        original_content_hash="h", original_preview="p", final_content="c",
        created_at=clk[0], expires_at=clk[0] + m.TTL_SECONDS)
    store.save(item)
    assert store.peek("u1") is not None
    clk[0] += m.TTL_SECONDS + 1
    assert store.peek("u1") is None


def test_content_hash_trailing_only_not_leading():
    m = _mod()
    assert m.content_hash("一、a\n二、b") == m.content_hash("一、a \n二、b  ")
    assert m.content_hash("一、a\n二、b") != m.content_hash("一、a\n 二、b")
    assert m.content_hash("一、a\r\n二、b") == m.content_hash("一、a\n二、b")


def test_marker_per_user_peek_no_delete(monkeypatch, tmp_path):
    m = _mod()
    monkeypatch.setattr(m, "_marker_dir", lambda: tmp_path)
    m.begin_modify_marker("u1", "2026-07-29", "hashA")
    m.begin_modify_marker("u2", "2026-07-29", "hashB")
    stale = m.peek_stale_modify_markers()
    assert {s["user_id"] for s in stale} == {"u1", "u2"}
    assert len(m.peek_stale_modify_markers()) == 2  # peek 不删除
    m.clear_modify_marker("u1")
    assert {s["user_id"] for s in m.peek_stale_modify_markers()} == {"u2"}
```

- [ ] **Step 2:** `python -m pytest tests/test_daily_report_edit.py -v` → FAIL。
- [ ] **Step 3:** 实现

```python
# src/daily_report_edit_confirmation.py
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
```

- [ ] **Step 4:** `python -m pytest tests/test_daily_report_edit.py -v` → PASS。
- [ ] **Step 5:** Commit `feat: 新增 OA 修改二次确认与按用户修改进行中标记`。

---

### Task 3: 企业微信原始消息解析器（最早分隔符 + 正文不 strip）

**Files:**
- Modify: `src/server.py`（新增 `ParsedReportCommand`、`_first_sep_index`、`_split_action_and_body`、`parse_daily_report_edit_command`、`_extract_body_from_raw`）
- Test: `tests/test_server_command_normalization.py`

**Interfaces:**
- `parse_daily_report_edit_command(raw) -> ParsedReportCommand | None`；`_extract_body_from_raw(raw) -> str`。两者取“换行/中文冒号/英文冒号”中**最先出现**的分隔符切分；**正文原样保留，不做 `.strip()`**（动作前缀才 strip）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_server_command_normalization.py  追加
def _srv():
    import src.server as s
    return s


def test_parse_mixed_colon_then_newline():
    s = _srv()
    parsed = s.parse_daily_report_edit_command("修改今日日报：一、修复问题\n二、完成测试")
    assert parsed.action == "overwrite_today"
    assert parsed.body == "一、修复问题\n二、完成测试"


def test_parse_newline_first():
    s = _srv()
    parsed = s.parse_daily_report_edit_command("设置日报\n一、完成联调\n二、测试")
    assert parsed.action == "set_today" and parsed.body == "一、完成联调\n二、测试"


def test_parse_colon_only():
    s = _srv()
    parsed = s.parse_daily_report_edit_command("追加日报：完成生产验证")
    assert parsed.action == "append" and parsed.body == "完成生产验证"


def test_body_leading_space_preserved():
    s = _srv()
    parsed = s.parse_daily_report_edit_command("追加日报： 完成验证")  # 冒号后一个空格
    assert parsed.body == " 完成验证"  # 原样保留，不 strip


def test_set_report_time_not_captured():
    s = _srv()
    assert s.parse_daily_report_edit_command("设置日报提交时间 09:00") is None
    assert s.parse_daily_report_edit_command("设置日报提交时间：09:00") is None


def test_extract_body_uses_earliest_sep():
    s = _srv()
    assert s._extract_body_from_raw("往日报后面加一条：完成生产验证") == "完成生产验证"
    assert s._extract_body_from_raw("帮我设置日报\n一、完成联调") == "一、完成联调"
    assert s._extract_body_from_raw("修改今日日报：一、修复\n二、测试") == "一、修复\n二、测试"
    assert s._extract_body_from_raw("无分隔符") == ""
```

- [ ] **Step 2:** `python -m pytest tests/test_server_command_normalization.py -k "parse_ or extract_body or body_leading" -v` → FAIL。
- [ ] **Step 3:** 实现

在 `src/server.py` 中 `_normalize_message_text`（约 165 行）下方新增：

```python
from dataclasses import dataclass


@dataclass
class ParsedReportCommand:
    action: str  # set_today | append | append_today | overwrite_today | view_draft | clear_draft
    body: str


_REPORT_EDIT_ACTIONS = {
    "设置日报": "set_today", "追加日报": "append", "追加今日日报": "append_today",
    "修改今日日报": "overwrite_today", "查看草稿": "view_draft", "清除草稿": "clear_draft",
}


def _first_sep_index(raw: str) -> int:
    """返回换行/中文冒号/英文冒号中最早出现的位置，没有返回 -1。"""
    candidates = [i for i in (raw.find("\n"), raw.find("："), raw.find(":")) if i != -1]
    return min(candidates) if candidates else -1


def _split_action_and_body(raw: str) -> tuple[str, str]:
    idx = _first_sep_index(raw)
    if idx == -1:
        return raw.strip(), ""
    return raw[:idx].strip(), raw[idx + 1:]  # 正文原样保留，不 strip


def _extract_body_from_raw(raw: str) -> str:
    """从原始消息按最早分隔符提取正文，忽略动作前缀，原样保留。"""
    if not raw:
        return ""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    idx = _first_sep_index(raw)
    if idx == -1:
        return ""
    return raw[idx + 1:]


def parse_daily_report_edit_command(raw_content):
    if not raw_content:
        return None
    raw = raw_content.replace("\r\n", "\n").replace("\r", "\n")
    if _normalize_message_text(raw).startswith("设置日报提交时间"):
        return None
    action_text, body = _split_action_and_body(raw)
    if not action_text:
        return None
    action = _REPORT_EDIT_ACTIONS.get(action_text)
    if action is None:
        return None
    if action in ("view_draft", "clear_draft"):
        return ParsedReportCommand(action, "")
    return ParsedReportCommand(action, body)
```

- [ ] **Step 4:** `python -m pytest tests/test_server_command_normalization.py -k "parse_ or extract_body or body_leading" -v` → PASS。
- [ ] **Step 5:** Commit `feat: 企微日报编辑指令解析（最早分隔符、正文不 strip）`。

---

### Task 4: target.modify_daily_report 真三态 + 按日期修改 + 重新导航回读 + 异常分流 + 锁内标记

**Files:**
- Modify: `src/target.py`：新增 `_find_report_row_on_pages`（真三态）、`_check_pre_modify`、`_verify_after_modify`、`_modify_failure_result`、`query_today_report`；重写 `modify_daily_report`（270-314）。
- Test: `tests/test_daily_report_edit.py`

**Interfaces:**
- `_find_report_row_on_pages(page, report_date, max_pages=12) -> tuple[str, object|None]`：返回 `("found",row)`/`("absent",None)`/`("error",None)`。**不复用** `_find_existing_report_row`（其超时返回 None 会被误判 absent），内联表格加载检查：`wait_for_selector` 超时即 `error`。
- `_check_pre_modify(page, actual_date, expected_hash) -> (ok, msg, current, row)`。
- `_verify_after_modify(page, actual_date, target_content, target) -> bool`。
- `_modify_failure_result(submitted, exc, actual_date) -> (ok, msg, meta)`。
- `modify_daily_report(content, cfg, report_date=None, expected_content_hash=None, user_id=None)`：`submitted` 在 `_submit_dialog` 前置 True；标记 begin/clear 在锁内、clear 在 `_BROWSER_LOCK.release()` 前。
- `query_today_report(cfg, report_date) -> (status, content)`：`exists`/`absent`/`error`。
- Consumes: `_normalize_report_date`、`_login_and_navigate`、`_BROWSER_LOCK`、`_read_report_content_from_row`、`_fill_report_form`、`_submit_dialog`、`REPORT_PAGE`、`WORK_HOURS`、`PlaywrightTimeout`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_daily_report_edit.py  追加
def test_modify_rejects_non_today(monkeypatch):
    import src.target as t
    from types import SimpleNamespace
    cfg = SimpleNamespace(target=SimpleNamespace(default_project="P", url="http://x", page_timeout=1000))
    ok, msg, meta = t.modify_daily_report("x", cfg, "2099-01-01")
    assert ok is False and meta["action"] == "failed"


def test_modify_failure_pre_submit_is_failed():
    import src.target as t
    _, _, meta = t._modify_failure_result(False, Exception("e"), "2026-07-29")
    assert meta["action"] == "failed"


def test_modify_failure_post_submit_is_unknown():
    import src.target as t
    _, _, meta = t._modify_failure_result(True, Exception("e"), "2026-07-29")
    assert meta["action"] == "unknown"


class _FakeRow:
    def __init__(self, text): self._text = text
    def inner_text(self): return self._text
    def query_selector(self, sel): return None


class _FakeNextBtn:
    def __init__(self, page): self._page = page
    def is_disabled(self): return False
    def click(self): self._page._on_page2 = True


class _FakePage:
    def __init__(self, rows1, rows2):
        self._rows1 = rows1; self._rows2 = rows2; self._on_page2 = False; self.goto_count = 0
    def goto(self, *a, **k): self.goto_count += 1
    def wait_for_selector(self, *a, **k): pass
    def wait_for_timeout(self, *a, **k): pass
    def query_selector_all(self, sel):
        return self._rows2 if self._on_page2 else self._rows1
    def query_selector(self, sel):
        if ("下一页" in sel or "btn-next" in sel) and not self._on_page2:
            return _FakeNextBtn(self)
        return None


class _NoNextPage(_FakePage):
    def __init__(self, rows1):
        super().__init__(rows1, [])
    def query_selector(self, sel): return None  # 无下一页


def test_find_row_table_timeout_is_error():
    import src.target as t
    class ErrPage:
        def wait_for_selector(self, *a, **k): raise t.PlaywrightTimeout("x")
        def wait_for_timeout(self, *a, **k): pass
        def query_selector(self, *a): return None
        def query_selector_all(self, *a): return []
    status, row = t._find_report_row_on_pages(ErrPage(), "2026-07-29")
    assert status == "error" and row is None


def test_find_row_absent_when_loaded_no_match():
    import src.target as t
    page = _NoNextPage([_FakeRow("2026-07-28")])  # 表格加载但无目标、无下一页
    status, row = t._find_report_row_on_pages(page, "2026-07-29")
    assert status == "absent"


def test_find_row_empty_table_is_absent():
    import src.target as t
    page = _NoNextPage([])  # 表格容器加载但无任何行（空表）
    status, row = t._find_report_row_on_pages(page, "2026-07-29")
    assert status == "absent"


def test_find_row_found_on_first_page():
    import src.target as t
    page = _NoNextPage([_FakeRow("2026-07-29")])
    status, row = t._find_report_row_on_pages(page, "2026-07-29")
    assert status == "found" and row is not None


def test_find_row_paginates_to_second_page():
    import src.target as t
    page = _FakePage([_FakeRow("2026-07-28")], [_FakeRow("2026-07-29")])
    status, row = t._find_report_row_on_pages(page, "2026-07-29")
    assert status == "found" and row is not None and page._on_page2 is True


def test_pre_modify_aborts_on_hash_mismatch(monkeypatch):
    import src.target as t
    from src.daily_report_edit_confirmation import content_hash
    page = _NoNextPage([_FakeRow("2026-07-29")])
    monkeypatch.setattr(t, "_read_report_content_from_row", lambda p, r: "旧正文")
    ok, msg, _, _ = t._check_pre_modify(page, "2026-07-29", content_hash("不同的"))
    assert ok is False and "发生变化" in msg


def test_pre_modify_returns_row(monkeypatch):
    import src.target as t
    from src.daily_report_edit_confirmation import content_hash
    page = _NoNextPage([_FakeRow("2026-07-29")])
    monkeypatch.setattr(t, "_read_report_content_from_row", lambda p, r: "旧正文")
    ok, msg, current, row = t._check_pre_modify(page, "2026-07-29", content_hash("旧正文"))
    assert ok is True and current == "旧正文" and row is not None


def test_verify_after_modify_re_navigates_and_compares(monkeypatch):
    import src.target as t
    from types import SimpleNamespace
    page = _NoNextPage([_FakeRow("2026-07-29")])
    target = SimpleNamespace(url="http://x", page_timeout=1000)
    monkeypatch.setattr(t, "_read_report_content_from_row", lambda p, r: "新正文")
    before = page.goto_count
    assert t._verify_after_modify(page, "2026-07-29", "新正文", target) is True
    assert page.goto_count > before


def test_modify_marker_lifecycle_under_lock(monkeypatch, tmp_path):
    import src.target as t
    import src.daily_report_edit_confirmation as ec
    import src.beijing_time as bt
    monkeypatch.setattr(bt, "today_str", lambda: "2026-07-29")
    events = []
    monkeypatch.setattr(ec, "begin_modify_marker", lambda u, d, h: events.append(("begin", u)))
    monkeypatch.setattr(ec, "clear_modify_marker", lambda u: events.append(("clear", u)))

    class FakePw:
        def stop(self): events.append(("pw_stop",))
    class FakeBrowser:
        def close(self): events.append(("browser_close",))
    class FakeBtn:
        def click(self): events.append(("edit_click",))
    class ClickableRow:
        def inner_text(self): return "2026-07-29"  # 命中当天日期
        def query_selector(self, sel): return FakeBtn()  # 返回修改按钮
    fake_page = _NoNextPage([ClickableRow()])
    monkeypatch.setattr(t, "_login_and_navigate", lambda cfg: (FakePw(), FakeBrowser(), None, fake_page))
    monkeypatch.setattr(t, "_BROWSER_LOCK", type("L", (), {"release": staticmethod(lambda: events.append(("lock_release",)))})())
    monkeypatch.setattr(t, "_read_report_content_from_row", lambda p, r: "旧正文")
    monkeypatch.setattr(t, "_fill_report_form", lambda *a, **k: events.append(("fill",)))
    monkeypatch.setattr(t, "_submit_dialog", lambda *a, **k: events.append(("submit",)))
    monkeypatch.setattr(t, "_verify_after_modify", lambda p, d, tc, target: (events.append(("verify",)), True)[1])
    from src.daily_report_edit_confirmation import content_hash
    from types import SimpleNamespace
    cfg = SimpleNamespace(target=SimpleNamespace(default_project="P", url="http://x", page_timeout=1000))
    ok, msg, meta = t.modify_daily_report("新正文", cfg, "2026-07-29",
                                          expected_content_hash=content_hash("旧正文"), user_id="u1")
    assert ok and meta["action"] == "overwrite"
    assert ("submit",) in events  # submitted 在 submit 之前/之后无所谓，但 submit 发生
    begin_i = events.index(("begin", "u1"))
    clear_i = events.index(("clear", "u1"))
    rel_i = events.index(("lock_release",))
    assert begin_i < clear_i < rel_i  # begin→clear→release
```

- [ ] **Step 2:** `python -m pytest tests/test_daily_report_edit.py -v` → FAIL。
- [ ] **Step 3:** 实现

`src/target.py` 新增（**不复用** `_find_existing_report_row`，内联三态查找）：

```python
def _find_report_row_on_pages(page, report_date: str, max_pages: int = 12):
    """返回 (status, row)。status: 'found' | 'absent' | 'error'。
    等待的是表格容器 table/.el-table；表格加载但无行（空表）算 absent。
    只有容器都没加载（wait_for_selector 超时）才 error。"""
    for _ in range(max_pages):
        try:
            page.wait_for_selector("table, .el-table", timeout=30000)
            page.wait_for_timeout(3000)
        except PlaywrightTimeout:
            return "error", None
        rows = page.query_selector_all("table tbody tr, .el-table__body tr")
        for row in rows:
            text = row.inner_text()
            if report_date in text or report_date.replace("-", "/") in text:
                return "found", row
        next_btn = page.query_selector("button:has-text('下一页'), .el-pagination .btn-next")
        if next_btn is None or next_btn.is_disabled():
            return "absent", None
        next_btn.click()
        page.wait_for_timeout(1500)
    return "absent", None


def _check_pre_modify(page, actual_date, expected_hash):
    """返回 (ok, msg, current, row)。expected_hash 为 None 时只读不校验。"""
    from src.daily_report_edit_confirmation import content_hash
    status, row = _find_report_row_on_pages(page, actual_date)
    if status == "error":
        return False, "OA 表格加载失败，无法确认今天的日报", None, None
    if status == "absent":
        return False, "今天尚未提交日报", None, None
    current = _read_report_content_from_row(page, row)
    if expected_hash is not None and content_hash(current) != expected_hash:
        return False, "内容在确认期间发生变化，已停止修改", current, row
    return True, "", current, row


def _verify_after_modify(page, actual_date, target_content, target) -> bool:
    """重新导航并重新定位当天行后回读比对。"""
    from src.daily_report_edit_confirmation import normalize_content_for_compare
    page.goto(target.url.rstrip("/") + REPORT_PAGE, timeout=target.page_timeout)
    try:
        page.wait_for_selector("table, .el-table", timeout=30000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(3000)
    status, row = _find_report_row_on_pages(page, actual_date)
    if status != "found":
        return False
    after = _read_report_content_from_row(page, row)
    return normalize_content_for_compare(after) == normalize_content_for_compare(target_content)


def _modify_failure_result(submitted: bool, exc: Exception, actual_date: str):
    if submitted:
        return False, f"修改结果未知，请手动核对：{exc}", {"report_date": actual_date, "action": "unknown"}
    return False, f"修改失败: {exc}", {"report_date": actual_date, "action": "failed"}


def query_today_report(cfg, report_date) -> tuple[str, str]:
    """返回 (status, content)。status: exists | absent | error。复用 _login_and_navigate 模式。"""
    actual_date = _normalize_report_date(report_date)
    try:
        pw, browser, context, page = _login_and_navigate(cfg)
    except Exception:
        return "error", ""
    try:
        page.goto(cfg.target.url.rstrip("/") + REPORT_PAGE, timeout=cfg.target.page_timeout)
        try:
            page.wait_for_selector("table, .el-table", timeout=30000)
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(3000)
        status, row = _find_report_row_on_pages(page, actual_date)
        if status == "error":
            return "error", ""
        if status == "absent":
            return "absent", ""
        content = _read_report_content_from_row(page, row)
        if content and content.strip():
            return "exists", content
        return "error", ""  # 有行但读不到正文
    except Exception:
        return "error", ""
    finally:
        try: browser.close()
        except Exception: pass
        try: pw.stop()
        except Exception: pass
        _BROWSER_LOCK.release()
```

重写 `modify_daily_report`（270-314）。**沿用 `get_report_content_by_date` 的 `_login_and_navigate` + finally 释放 `_BROWSER_LOCK` 模式；不包 `browser_operation()`。标记 begin/clear 在锁内、clear 在 release 前。**

```python
def modify_daily_report(content, cfg, report_date=None, expected_content_hash=None, user_id=None) -> tuple[bool, str, dict]:
    from src.beijing_time import today_str
    from src.daily_report_edit_confirmation import (
        begin_modify_marker, clear_modify_marker)

    target = cfg.target
    actual_date = _normalize_report_date(report_date) if report_date else _normalize_report_date(today_str())
    if actual_date != _normalize_report_date(today_str()):
        return False, "本期只允许修改北京时间当天的日报", {"report_date": actual_date, "action": "failed"}

    submitted = False
    try:
        pw, browser, context, page = _login_and_navigate(cfg)
    except Exception as exc:
        return _modify_failure_result(False, exc, actual_date)
    try:
        if user_id:
            begin_modify_marker(user_id, actual_date, expected_content_hash or "")
        page.goto(target.url.rstrip("/") + REPORT_PAGE, timeout=target.page_timeout)
        try:
            page.wait_for_selector("table, .el-table", timeout=30000)
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(3000)

        ok, msg, current, row = _check_pre_modify(page, actual_date, expected_content_hash)
        if not ok:
            return False, msg, {"report_date": actual_date, "action": "failed"}
        edit_btn = row.query_selector("button:has-text('修改')")
        if edit_btn is None:
            return False, "未找到修改入口", {"report_date": actual_date, "action": "failed"}
        edit_btn.click()
        page.wait_for_timeout(2000)
        _fill_report_form(page, content, target)
        submitted = True  # 即将提交；此后任何异常都视为 unknown
        _submit_dialog(page)
        if not _verify_after_modify(page, actual_date, content, target):
            return False, "修改结果未知，请手动核对", {"report_date": actual_date, "action": "unknown"}
        return True, f"日报修改成功\n> {content[:200]}", {
            "project": target.default_project, "hours": WORK_HOURS, "travel": "否",
            "log_type": "实施日志", "content": content,
            "report_date": actual_date, "action": "overwrite"}
    except Exception as exc:
        return _modify_failure_result(submitted, exc, actual_date)
    finally:
        try: browser.close()
        except Exception: pass
        try: pw.stop()
        except Exception: pass
        if user_id:
            try: clear_modify_marker(user_id)
            except Exception as e:
                logger.error(f"clear modify marker failed: {e}", exc_info=True)
        _BROWSER_LOCK.release()  # 标记清除在释放锁之前
```

实施者：打开 `target.py:270-314` 原文逐行对照。原函数若用 `browser_operation()` + 自建浏览器，**改为上面 `_login_and_navigate` 模式**。`_login_and_navigate` 是否已导航报表页以仓库现状为准；若未导航，上面的 `page.goto(... + REPORT_PAGE)` 负责导航。`logger` 用 `target.py` 现有 logger；若无则改 `import logging; logger = logging.getLogger(__name__)`。

- [ ] **Step 4:** `python -m pytest tests/test_daily_report_edit.py -v` → PASS。
- [ ] **Step 5:** Commit `feat: modify_daily_report 真三态查询、按日期定位、重新导航回读、异常分流、锁内标记`。

---

### Task 5: MsgId 有界时效去重（含在途超时清理）

**Files:**
- Modify: `src/server.py`
- Test: `tests/test_server_command_normalization.py`

**Interfaces:**
- `_msgid_should_process(msg_id, *, clock=...) -> bool`（首次置 in-flight；重复/已见返回 False）；`_msgid_mark_done(msg_id, *, processed: bool, clock=...)`（processed=True 才记 seen；False 仅清 in-flight 允许重试）。in-flight 带 TTL，超时未 mark_done 视为孤儿清理。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_server_command_normalization.py  追加
def test_msgid_first_then_seen():
    s = _srv(); clk = [1000.0]
    assert s._msgid_should_process("M1", clock=lambda: clk[0]) is True
    s._msgid_mark_done("M1", processed=True, clock=lambda: clk[0])
    assert s._msgid_should_process("M1", clock=lambda: clk[0]) is False


def test_msgid_inflight_blocks_concurrent():
    s = _srv(); clk = [1000.0]
    assert s._msgid_should_process("M2", clock=lambda: clk[0]) is True
    assert s._msgid_should_process("M2", clock=lambda: clk[0]) is False
    s._msgid_mark_done("M2", processed=False, clock=lambda: clk[0])
    assert s._msgid_should_process("M2", clock=lambda: clk[0]) is True


def test_msgid_inflight_orphan_cleared_after_ttl():
    s = _srv(); clk = [1000.0]
    assert s._msgid_should_process("M3", clock=lambda: clk[0]) is True
    clk[0] += 10_000
    assert s._msgid_should_process("M3", clock=lambda: clk[0]) is True


def test_msgid_empty_always_processes():
    s = _srv()
    assert s._msgid_should_process("", clock=lambda: 1.0) is True
```

- [ ] **Step 2:** `python -m pytest tests/test_server_command_normalization.py -k msgid -v` → FAIL。
- [ ] **Step 3:** 实现

```python
# src/server.py 顶部模块级
import threading
import time as _time

_MSGID_TTL = 300.0
_msgid_lock = threading.Lock()
_msgid_seen: dict[str, float] = {}
_msgid_inflight: dict[str, float] = {}


def _msgid_should_process(msg_id: str, *, clock=_time.monotonic) -> bool:
    if not msg_id:
        return True
    now = clock()
    with _msgid_lock:
        for k in list(_msgid_seen):
            if _msgid_seen[k] < now - _MSGID_TTL:
                _msgid_seen.pop(k, None)
        for k in list(_msgid_inflight):
            if _msgid_inflight[k] < now - _MSGID_TTL:
                _msgid_inflight.pop(k, None)  # 孤儿，允许重试
        if msg_id in _msgid_seen or msg_id in _msgid_inflight:
            return False
        _msgid_inflight[msg_id] = now
        return True


def _msgid_mark_done(msg_id: str, *, processed: bool, clock=_time.monotonic) -> None:
    if not msg_id:
        return
    with _msgid_lock:
        _msgid_inflight.pop(msg_id, None)
        if processed:
            _msgid_seen[msg_id] = clock()
```

- [ ] **Step 4:** `python -m pytest tests/test_server_command_normalization.py -k msgid -v` → PASS。
- [ ] **Step 5:** Commit `feat: 企微回调 MsgId 去重（含在途超时清理）`。

---

### Task 6: 草稿优先提交、条件清除与通知安全（skipped/未知动作不误报）

**Files:**
- Modify: `src/server.py`：`auto_submit_if_needed`（2363）开头插入草稿优先分支；重写 `_submit_and_notify`（2412）。
- Test: `tests/test_report_recovery.py`

**Interfaces:**
- `_canonical_submit_action(action) -> str`；`_submit_and_notify(content, cfg, report_source=None, report_meta=None, on_actual_write=None) -> bool`。`skipped` 只发提示、不调 `notify_report_success`；非 `submit/overwrite/skipped` 报“结果无法确认”并 return False；`on_actual_write(meta)` 只写 `meta["draft_note"]`（成功也写“草稿已清除。”）。草稿路径 `report_meta` 含 `smart_doc_status`（manual→`not_used`，smart_sheet_append→`normal`）。
- 注意：`submit_daily_report`/`notify_report_success`/`notify_report_failure` 为 `server` 顶层导入名，patch `server.<name>`；`check_cookies` 延迟导入，patch `src.cookies_checker.check_cookies`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_report_recovery.py  追加
def _srv():
    import src.server as s
    return s


def test_canonical_submit_action_maps_chinese():
    s = _srv()
    assert s._canonical_submit_action("提交") == "submit"
    assert s._canonical_submit_action("覆盖") == "overwrite"
    assert s._canonical_submit_action("skipped") == "skipped"
    assert s._canonical_submit_action("别的") == "failed"


def _patch_notifier(monkeypatch, cap):
    import src.server as s
    def _succ(cfg, content, report_info, **k):
        cap.setdefault("success", {"info": report_info, "k": k})
    monkeypatch.setattr(s, "notify_report_success", _succ)
    monkeypatch.setattr(s, "notify_report_failure", lambda *a, **k: cap.setdefault("failure", a))
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: cap.setdefault("text", a))


def test_draft_first_submit_skips_smart_doc_and_clears(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_draft as d
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    d.save_draft("2026-07-29", "草稿正文", "manual")
    calls = {"cookie": 0, "build": 0, "submit": []}
    import src.cookies_checker as cc
    monkeypatch.setattr(cc, "check_cookies", lambda c: calls.__setitem__("cookie", calls["cookie"]+1) or True)
    import src.report_builder as rb
    monkeypatch.setattr(rb, "build_report_with_meta", lambda c: calls.__setitem__("build", calls["build"]+1) or ("x","smart_sheet",{}))
    monkeypatch.setattr(s, "submit_daily_report", lambda content, cfg: (calls["submit"].append(content), (True,"ok",{"action":"提交","report_date":"2026-07-29"}))[1])
    cap = {}; _patch_notifier(monkeypatch, cap)
    from types import SimpleNamespace
    s.auto_submit_if_needed(SimpleNamespace(wechat=SimpleNamespace()), precheck_cookies=True)
    assert calls["cookie"] == 0 and calls["build"] == 0 and calls["submit"] == ["草稿正文"]
    assert d.load_draft() is None
    assert "success" in cap and "failure" not in cap
    assert cap["success"]["k"]["smart_doc_status"] == "not_used"  # 作为 kwarg 传入
    assert cap["success"]["info"].get("draft_note") == "草稿已清除。"


def test_skipped_does_not_notify_success(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_draft as d
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    d.save_draft("2026-07-29", "草稿正文", "manual")
    monkeypatch.setattr(s, "submit_daily_report", lambda content, cfg: (True, "今日日报已审核，跳过", {"action": "skipped"}))
    cap = {}; _patch_notifier(monkeypatch, cap)
    from types import SimpleNamespace
    s.auto_submit_if_needed(SimpleNamespace(wechat=SimpleNamespace()), precheck_cookies=True)
    assert "success" not in cap and "failure" not in cap
    assert "text" in cap and d.load_draft() is not None


def test_unknown_action_reports_unconfirmed(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_draft as d
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    d.save_draft("2026-07-29", "草稿正文", "manual")
    monkeypatch.setattr(s, "submit_daily_report", lambda content, cfg: (True, "ok", {"action": "weird"}))
    cap = {}; _patch_notifier(monkeypatch, cap)
    from types import SimpleNamespace
    ret = s.auto_submit_if_needed(SimpleNamespace(wechat=SimpleNamespace()), precheck_cookies=True)
    assert ret is False  # 不报成功
    assert "failure" in cap and "无法确认" in str(cap["failure"])
    assert d.load_draft() is not None  # 草稿保留


def test_clear_failure_sets_draft_note_not_failure(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_draft as d
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    d.save_draft("2026-07-29", "草稿正文", "manual")
    monkeypatch.setattr(s, "submit_daily_report", lambda content, cfg: (True, "ok", {"action": "提交"}))
    monkeypatch.setattr(d, "clear_draft", lambda **k: (_ for _ in ()).throw(d.DraftCorruptError("bad")))
    cap = {}; _patch_notifier(monkeypatch, cap)
    from types import SimpleNamespace
    s.auto_submit_if_needed(SimpleNamespace(wechat=SimpleNamespace()), precheck_cookies=True)
    assert "草稿清除失败" in cap["success"]["info"].get("draft_note", "")
    assert "failure" not in cap


def test_notify_exception_does_not_flip_to_failure(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_draft as d
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    d.save_draft("2026-07-29", "草稿正文", "manual")
    monkeypatch.setattr(s, "submit_daily_report", lambda content, cfg: (True, "ok", {"action": "提交"}))
    monkeypatch.setattr(s, "notify_report_success", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(s, "notify_report_failure", lambda *a, **k: None)
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: None)
    from types import SimpleNamespace
    ret = s.auto_submit_if_needed(SimpleNamespace(wechat=SimpleNamespace()), precheck_cookies=True)
    assert ret is True and d.load_draft() is not None  # 清除失败致草稿保留，但结果不翻失败
```

- [ ] **Step 2:** `python -m pytest tests/test_report_recovery.py -k "canonical_submit or draft_first or skipped_does or unknown_action or clear_failure or notify_exception" -v` → FAIL。
- [ ] **Step 3:** 实现

`src/server.py` 新增：

```python
def _canonical_submit_action(action: str) -> str:
    return {"提交": "submit", "覆盖": "overwrite", "skipped": "skipped"}.get(str(action), "failed")


def _safe_notify_failure(cfg, msg, report_source, report_meta, report_info=None):
    try:
        notify_report_failure(cfg, msg, report_source=report_source,
                              smart_doc_status=(report_meta or {}).get("smart_doc_status"),
                              smart_doc_error=(report_meta or {}).get("smart_doc_error"),
                              screenshot=(report_info or {}).get("screenshot"))
    except Exception as e:
        logger.error(f"notify failure failed: {e}", exc_info=True)


def _submit_and_notify(content: str, cfg: Config, report_source: str = None,
                       report_meta: dict = None, on_actual_write=None) -> bool:
    report_meta = report_meta or {}
    try:
        success, msg, report_info = submit_daily_report(content, cfg)
    except Exception as e:
        logger.error(f"Submit crashed: {e}", exc_info=True)
        _safe_notify_failure(cfg, f"日报提交流程异常: {e}", report_source, report_meta)
        return False
    if not success:
        _safe_notify_failure(cfg, msg, report_source, report_meta, report_info)
        return False

    action = _canonical_submit_action(report_info.get("action"))
    if action == "skipped":
        try:
            _send_wechat_text(cfg.wechat, msg, None)
        except Exception as e:
            logger.error(f"skip notify failed: {e}", exc_info=True)
        return True
    if action not in ("submit", "overwrite"):
        # 未知动作：不报成功、不清草稿、报告无法确认
        _safe_notify_failure(cfg, f"提交结果无法确认（动作：{report_info.get('action')}），请手动核对。",
                             report_source, report_meta, report_info)
        return False

    if on_actual_write is not None:
        try:
            on_actual_write(report_info)
        except Exception as e:
            logger.error(f"post-write callback failed: {e}", exc_info=True)
            report_info["draft_note"] = f"草稿后续处理失败：{e}"
    try:
        notify_report_success(cfg, content, report_info, report_source=report_source,
                              smart_doc_status=report_meta.get("smart_doc_status"),
                              smart_doc_error=report_meta.get("smart_doc_error"))
    except Exception as e:
        logger.error(f"notify success failed: {e}", exc_info=True)
    return True
```

修改 `auto_submit_if_needed`（2363）：最开头插入草稿优先分支，现有无草稿逻辑不变：

```python
def auto_submit_if_needed(cfg: Config, *, precheck_cookies: bool = True) -> bool:
    from src.daily_report_draft import load_draft, clear_draft, DraftCorruptError
    from src.beijing_time import today_str

    today = today_str()
    try:
        draft = load_draft()
    except DraftCorruptError as exc:
        _safe_notify_failure(cfg, f"草稿文件损坏，提交失败：{exc}", None, {})
        return False
    if draft is not None and draft.report_date != today:
        _safe_notify_failure(cfg, f"存在 {draft.report_date} 的过期草稿，请先“查看草稿”并“清除草稿”。", None, {})
        return False
    if draft is not None and draft.report_date == today:
        recorded_date, recorded_revision = draft.report_date, draft.revision
        smart_status = "not_used" if draft.origin == "manual" else "normal"

        def _on_actual_write(meta: dict):
            if _canonical_submit_action(meta.get("action")) in ("submit", "overwrite"):
                try:
                    cleared = clear_draft(expected_report_date=recorded_date, expected_revision=recorded_revision)
                except DraftCorruptError:
                    cleared = False
                meta["draft_note"] = "草稿已清除。" if cleared else "草稿清除失败，请手动发送“清除草稿”。"

        return _submit_and_notify(
            draft.content, cfg,
            report_source="manual" if draft.origin == "manual" else "smart_sheet_append",
            report_meta={"report_date": today, "origin": draft.origin, "smart_doc_status": smart_status},
            on_actual_write=_on_actual_write)

    # ↓↓↓ 以下保留原有 precheck_cookies + build_report_with_meta + _submit_and_notify 流程不变 ↓↓↓
```

- [ ] **Step 4:** `python -m pytest tests/test_report_recovery.py -k "canonical_submit or draft_first or skipped_does or unknown_action or clear_failure or notify_exception" -v` → PASS。
- [ ] **Step 5:** Commit `feat: 草稿优先提交、条件清除、skipped/未知动作不误报成功`。

---

### Task 7: 后台线程化、追加日报分流、8 指令分派、AI 接入、分段发送、启动期残留检查

**Files:**
- Modify: `src/server.py`
- Test: `tests/test_report_recovery.py`、`tests/test_server_command_normalization.py`

**Interfaces:**
- `_run_in_background(fn, *args, msg_id="", cfg_obj=None, from_user=None, **kwargs)`：handler 成败决定 `_msgid_mark_done(processed=ok)`。
- `_send_long_text(cfg, text, from_user)`：分段。
- `_dispatch_append_report(body, from_user, cfg)`：用 `query_today_report` 状态分流；无 OA 无草稿先 `check_cookies`；确认消息含目标日期+原正文摘要+修改后正文。
- `_handle_daily_report_edit_command(raw, from_user, cfg, *, action=None, body=None)`。
- `_handle_edit_confirmation(from_user, cfg)`：confirm 返回 None（已超时/被消费）时发“已超时或不存在”并返回 True；不在此 begin/clear 标记（modify 内做）；传 `user_id=item.user_id`。
- `_check_stale_modify_on_startup(cfg)`：`peek_stale_modify_markers()` 只读，通知成功才 `clear_modify_marker(user_id)`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_report_recovery.py  追加
def _setup_basic(monkeypatch, tmp_path, oa_status=("absent", "")):
    import src.server as s
    import src.daily_report_draft as d
    import src.target as t
    monkeypatch.setattr(d, "_draft_path", lambda: tmp_path / "daily_report_draft.json")
    monkeypatch.setattr(t, "query_today_report", lambda cfg, dt: oa_status)
    sent = []
    monkeypatch.setattr(s, "_send_wechat_text", lambda cfg, msg, u=None: sent.append((msg, u)))
    monkeypatch.setattr(s, "_send_long_text", lambda cfg, msg, u=None: sent.append((msg, u)))
    from types import SimpleNamespace
    return s, SimpleNamespace(wechat=SimpleNamespace()), sent


def test_append_to_oa_creates_pending_when_submitted(monkeypatch, tmp_path):
    s, cfg, sent = _setup_basic(monkeypatch, tmp_path, oa_status=("exists", "现有正文"))
    s._handle_daily_report_edit_command("追加日报\n新增条目", "u1", cfg)
    item = s._edit_confirmations.peek("u1")
    assert item is not None and item.action == "append_today"
    assert item.final_content == "现有正文\n新增条目"
    assert any("目标日期" in m[0] and "原正文摘要" in m[0] for m in sent)


def test_append_no_oa_no_draft_uses_smart_sheet_with_cookie_check(monkeypatch, tmp_path):
    s, cfg, sent = _setup_basic(monkeypatch, tmp_path, oa_status=("absent", ""))
    import src.cookies_checker as cc
    import src.report_builder as rb
    cookie = {"checked": False}
    monkeypatch.setattr(cc, "check_cookies", lambda c: cookie.__setitem__("checked", True) or True)
    monkeypatch.setattr(rb, "build_report_with_meta", lambda c: ("智能文档正文", "smart_sheet", {"smart_doc_status": "normal"}))
    s._handle_daily_report_edit_command("追加日报\n完成联调", "u1", cfg)
    assert cookie["checked"] is True
    import src.daily_report_draft as d
    loaded = d.load_draft()
    assert loaded.origin == "smart_sheet_append" and loaded.content == "智能文档正文\n完成联调"


def test_append_oa_error_does_not_create_draft(monkeypatch, tmp_path):
    s, cfg, sent = _setup_basic(monkeypatch, tmp_path, oa_status=("error", ""))
    import src.report_builder as rb
    monkeypatch.setattr(rb, "build_report_with_meta", lambda c: ("x", "smart_sheet", {}))
    s._handle_daily_report_edit_command("追加日报\nx", "u1", cfg)
    import src.daily_report_draft as d
    assert d.load_draft() is None
    assert any("失败" in m[0] for m in sent)


def test_set_today_rejected_when_oa_submitted(monkeypatch, tmp_path):
    s, cfg, sent = _setup_basic(monkeypatch, tmp_path, oa_status=("exists", "已存在"))
    s._handle_daily_report_edit_command("设置日报\n今日内容", "u1", cfg)
    assert any("修改今日日报" in m[0] for m in sent)


def test_ai_route_natural_language_executes_append(monkeypatch, tmp_path):
    s, cfg, sent = _setup_basic(monkeypatch, tmp_path, oa_status=("exists", "现有正文"))
    raw = "往日报后面加一条：完成生产验证"
    s._handle_daily_report_edit_command(raw, "u1", cfg, action="append", body=s._extract_body_from_raw(raw))
    item = s._edit_confirmations.peek("u1")
    assert item is not None and item.final_content == "现有正文\n完成生产验证"


def test_confirm_passes_user_id_to_modify(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    s._msgid_inflight.clear(); s._msgid_seen.clear()
    clk = [1000.0]
    s._edit_confirmations = ec.EditConfirmationStore(clock=lambda: clk[0])
    item = ec.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
                               original_content_hash=ec.content_hash("原"), original_preview="原",
                               final_content="新正文", created_at=clk[0], expires_at=clk[0] + 120)
    s._edit_confirmations.save(item)
    import src.target as t
    captured = {}
    monkeypatch.setattr(t, "modify_daily_report", lambda content, cfg, report_date=None, expected_content_hash=None, user_id=None:
                       captured.update(user_id=user_id) or (True, "ok", {"action": "overwrite", "report_date": report_date}))
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: True)
    from types import SimpleNamespace
    s._handle_edit_confirmation("u1", SimpleNamespace(wechat=SimpleNamespace()), msg_id="M-pass")
    assert captured["user_id"] == "u1"


def test_confirm_consumed_sends_timeout(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    s._msgid_inflight.clear(); s._msgid_seen.clear()
    clk = [1000.0]
    s._edit_confirmations = ec.EditConfirmationStore(clock=lambda: clk[0])
    sent = []
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: sent.append(a) or True)
    from types import SimpleNamespace
    # 无待确认（未 save）→ _consume_confirm_or_skip 返回 (None, False) → 发超时提示
    assert s._handle_edit_confirmation("u1", SimpleNamespace(wechat=SimpleNamespace()), msg_id="M-to") is True
    assert any("超时" in str(a) for a in sent)


def test_consume_confirm_atomic_dedup(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    s._msgid_inflight.clear(); s._msgid_seen.clear()
    clk = [1000.0]
    s._edit_confirmations = ec.EditConfirmationStore(clock=lambda: clk[0])
    item = ec.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
                               original_content_hash="h", original_preview="p", final_content="c",
                               created_at=clk[0], expires_at=clk[0] + 120)
    s._edit_confirmations.save(item)
    item1, dup1 = s._consume_confirm_or_skip("M-atom", "u1")
    assert item1 is not None and dup1 is False  # 消费成功，MsgId 在途
    item2, dup2 = s._consume_confirm_or_skip("M-atom", "u1")
    assert item2 is None and dup2 is True  # 重复 MsgId，不消费


def test_route_confirm_present_routes_oa(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    clk = [1000.0]
    s._edit_confirmations = ec.EditConfirmationStore(clock=lambda: clk[0])
    item = ec.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
                               original_content_hash="h", original_preview="p", final_content="c",
                               created_at=clk[0], expires_at=clk[0] + 120)
    s._edit_confirmations.save(item)
    assert s._route_confirm_or_cancel("确认执行", "u1") is True


def test_route_confirm_expired_falls_through(monkeypatch, tmp_path):
    # 真实回调路径：过期 → peek None → _route 返回 False → 回调回落 AI 流程
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    clk = [1000.0]
    s._edit_confirmations = ec.EditConfirmationStore(clock=lambda: clk[0])
    item = ec.EditConfirmation(user_id="u1", action="overwrite_today", report_date="2026-07-29",
                               original_content_hash="h", original_preview="p", final_content="c",
                               created_at=clk[0], expires_at=clk[0] + 1)
    s._edit_confirmations.save(item)
    clk[0] += 10  # 过期
    assert s._edit_confirmations.peek("u1") is None
    assert s._route_confirm_or_cancel("确认执行", "u1") is False  # 回落 AI 流程


def test_stale_notify_false_keeps_marker(monkeypatch, tmp_path):
    import src.server as s
    import src.daily_report_edit_confirmation as ec
    monkeypatch.setattr(ec, "_marker_dir", lambda: tmp_path)
    ec.begin_modify_marker("u1", "2026-07-29", "h")
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: False)  # 通知失败
    from types import SimpleNamespace
    s._check_stale_modify_on_startup(SimpleNamespace(wechat=SimpleNamespace()))
    assert len(ec.peek_stale_modify_markers()) == 1  # 保留，下次再提醒
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: True)  # 通知成功
    s._check_stale_modify_on_startup(SimpleNamespace(wechat=SimpleNamespace()))
    assert ec.peek_stale_modify_markers() == []  # 成功才删


def test_background_thread_marks_done_only_on_success(monkeypatch, tmp_path):
    import src.server as s
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(s, "_msgid_mark_done", lambda mid, *, processed: calls.append(processed))
    monkeypatch.setattr(s, "_send_wechat_text", lambda *a, **k: None)
    cfg_obj = SimpleNamespace(wechat=SimpleNamespace())

    def ok_handler(*a, **k): pass
    def bad_handler(*a, **k): raise RuntimeError("x")
    s._run_in_background(ok_handler, msg_id="OK", cfg_obj=cfg_obj, from_user="u")
    s._run_in_background(bad_handler, msg_id="BAD", cfg_obj=cfg_obj, from_user="u")
    import time
    time.sleep(0.2)
    assert True in calls and False in calls
```

```python
# tests/test_server_command_normalization.py  追加
def test_send_long_text_segments(monkeypatch):
    s = _srv()
    from types import SimpleNamespace
    sent = []
    monkeypatch.setattr(s, "_send_wechat_text", lambda cfg, msg, u=None: sent.append(msg))
    long = "\n".join(f"行{i}" + "x" * 600 for i in range(10))
    s._send_long_text(SimpleNamespace(wechat=SimpleNamespace()), long, "u1")
    assert len(sent) >= 2
    assert "".join(sent).replace("\n", "") == long.replace("\n", "")
```

- [ ] **Step 2:** `python -m pytest tests/test_report_recovery.py tests/test_server_command_normalization.py -k "append or set_today or ai_route or confirm_passes or confirm_consumed or expired_peek or background_thread or send_long_text" -v` → FAIL。
- [ ] **Step 3:** 实现

`src/server.py` 模块级新增：

```python
from src.daily_report_edit_confirmation import (
    EditConfirmationStore, EditConfirmation, content_hash,
    peek_stale_modify_markers, clear_modify_marker,
)
_edit_confirmations = EditConfirmationStore()
_WECHAT_TEXT_MAX = 2000
_AI_ACTION_MAP = {
    "设置日报": "set_today", "追加日报": "append", "追加今日日报": "append_today",
    "修改今日日报": "overwrite_today", "查看草稿": "view_draft", "清除草稿": "clear_draft",
}


def _send_long_text(cfg, text, from_user=None):
    if not text:
        return
    if len(text) <= _WECHAT_TEXT_MAX:
        _send_wechat_text(cfg.wechat, text, from_user)
        return
    buf = ""
    for line in text.split("\n"):
        if buf and len(buf) + len(line) + 1 > _WECHAT_TEXT_MAX:
            _send_wechat_text(cfg.wechat, buf, from_user)
            buf = ""
        while len(line) > _WECHAT_TEXT_MAX:
            _send_wechat_text(cfg.wechat, line[:_WECHAT_TEXT_MAX], from_user)
            line = line[_WECHAT_TEXT_MAX:]
        buf = (buf + "\n" + line) if buf else line
    if buf:
        _send_wechat_text(cfg.wechat, buf, from_user)


def _run_in_background(fn, *args, msg_id="", cfg_obj=None, from_user=None, **kwargs):
    def _worker():
        ok = True
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            ok = False
            if cfg_obj is not None and from_user is not None:
                try:
                    _send_wechat_text(cfg_obj.wechat, f"处理失败：{exc}", from_user)
                except Exception:
                    pass
        finally:
            if msg_id:
                _msgid_mark_done(msg_id, processed=ok)
    if msg_id and not _msgid_should_process(msg_id):
        return
    threading.Thread(target=_worker, daemon=True).start()
```

`_dispatch_append_report`（状态分流 + Cookie 预检 + 确认消息含目标日期/原摘要/修改后正文）：

```python
def _dispatch_append_report(body, from_user, cfg) -> bool:
    from src.beijing_time import today_str
    from src.target import query_today_report
    from src.daily_report_draft import load_draft, append_draft, save_draft
    from src.report_builder import build_report_with_meta

    today = today_str()
    status, existing = query_today_report(cfg, today)
    if status == "error":
        _send_wechat_text(cfg.wechat, "查询今天 OA 日报失败，未修改。", from_user)
        return True
    if status == "exists":
        final = existing.rstrip("\n") + "\n" + body
        item = EditConfirmation(user_id=from_user, action="append_today", report_date=today,
            original_content_hash=content_hash(existing), original_preview=existing[:80],
            final_content=final, created_at=_time.monotonic(), expires_at=_time.monotonic() + 120)
        _edit_confirmations.save(item)
        _send_long_text(cfg, f"目标日期：{today}\n原正文摘要：{existing[:80]}\n修改后正文：\n{final}", from_user)
        _send_wechat_text(cfg.wechat, "回复“确认执行”提交，或“取消执行”放弃。", from_user)
        return True

    draft = load_draft()
    if draft is not None and draft.report_date != today:
        _send_wechat_text(cfg.wechat, f"存在 {draft.report_date} 的过期草稿，请先“清除草稿”或“设置日报”。", from_user)
        return True
    if draft is not None and draft.report_date == today:
        new = append_draft(today, body)
        _send_wechat_text(cfg.wechat, f"已追加到今天草稿，版本 {new.revision}。", from_user)
        _send_long_text(cfg, new.content, from_user)
        return True

    # 无 OA、无今天草稿：先 Cookie 预检
    try:
        from src.cookies_checker import check_cookies
        cookie_ok = check_cookies(cfg)
    except Exception as exc:
        _send_wechat_text(cfg.wechat, f"智能文档 Cookie 检查失败，未创建追加草稿：{exc}", from_user)
        return True
    if not cookie_ok:
        _send_wechat_text(cfg.wechat, "智能文档 Cookie 失效，未创建追加草稿。", from_user)
        return True
    try:
        report, source, meta = build_report_with_meta(cfg)
    except Exception as exc:
        _send_wechat_text(cfg.wechat, f"智能文档读取失败，未创建草稿：{exc}", from_user)
        return True
    if source != "smart_sheet" or meta.get("smart_doc_status") != "normal" or not report.strip():
        _send_wechat_text(cfg.wechat, "智能文档状态异常或回退上一条日报，未创建追加草稿。", from_user)
        return True
    new = save_draft(today, report.rstrip("\n") + "\n" + body, "smart_sheet_append")
    _send_wechat_text(cfg.wechat, f"已根据智能文档生成草稿并追加，来源 智能文档加手工追加，版本 {new.revision}。将在提交时使用。", from_user)
    _send_long_text(cfg, new.content, from_user)
    return True
```

注：`check_cookies` 实际契约若失效抛 `CookiesError`（见 `server.py:2365-2383`），try/except 已兼容。

主分派：

```python
def _handle_daily_report_edit_command(raw_content, from_user, cfg, *, action=None, body=None):
    from src.beijing_time import today_str
    from src.daily_report_draft import load_draft, save_draft, clear_draft
    from src.target import query_today_report

    if action is None:
        parsed = parse_daily_report_edit_command(raw_content)
        if parsed is None:
            return False
        action, body = parsed.action, parsed.body
    today = today_str()

    if action == "view_draft":
        draft = load_draft()
        if draft is None:
            _send_wechat_text(cfg.wechat, "当前没有草稿。", from_user)
        else:
            _send_long_text(cfg, f"草稿日期：{draft.report_date}\n来源：{draft.origin}\n版本：{draft.revision}\n更新时间：{draft.updated_at}\n正文：\n{draft.content}", from_user)
        return True

    if action == "clear_draft":
        clear_draft()
        _send_wechat_text(cfg.wechat, "草稿已清除。", from_user)
        return True

    if action == "set_today":
        if not body.strip():
            _send_wechat_text(cfg.wechat, "格式：设置日报\\n[完整内容]", from_user)
            return True
        status, _ = query_today_report(cfg, today)
        if status == "error":
            _send_wechat_text(cfg.wechat, "查询今天 OA 失败，未保存草稿。", from_user)
            return True
        if status == "exists":
            _send_wechat_text(cfg.wechat, "今天已提交日报，请改用“修改今日日报”。", from_user)
            return True
        new = save_draft(today, body, "manual")
        _send_wechat_text(cfg.wechat, f"已保存今天草稿，版本 {new.revision}。", from_user)
        return True

    if action == "append":
        if not body.strip():
            _send_wechat_text(cfg.wechat, "格式：追加日报\\n[追加内容]", from_user)
            return True
        return _dispatch_append_report(body, from_user, cfg)

    if action == "append_today":
        if not body.strip():
            _send_wechat_text(cfg.wechat, "格式：追加今日日报\\n[追加内容]", from_user)
            return True
        status, existing = query_today_report(cfg, today)
        if status == "error":
            _send_wechat_text(cfg.wechat, "查询今天 OA 失败。", from_user)
            return True
        if status != "exists":
            _send_wechat_text(cfg.wechat, "今天尚未提交日报，请改用“追加日报”。", from_user)
            return True
        final = existing.rstrip("\n") + "\n" + body
        item = EditConfirmation(user_id=from_user, action="append_today", report_date=today,
            original_content_hash=content_hash(existing), original_preview=existing[:80],
            final_content=final, created_at=_time.monotonic(), expires_at=_time.monotonic() + 120)
        _edit_confirmations.save(item)
        _send_long_text(cfg, f"目标日期：{today}\n原正文摘要：{existing[:80]}\n修改后正文：\n{final}", from_user)
        _send_wechat_text(cfg.wechat, "回复“确认执行”提交，或“取消执行”放弃。", from_user)
        return True

    if action == "overwrite_today":
        if not body.strip():
            _send_wechat_text(cfg.wechat, "格式：修改今日日报\\n[完整内容]", from_user)
            return True
        status, existing = query_today_report(cfg, today)
        if status == "error":
            _send_wechat_text(cfg.wechat, "查询今天 OA 失败。", from_user)
            return True
        if status != "exists":
            _send_wechat_text(cfg.wechat, "今天尚未提交日报，请改用“设置日报”。", from_user)
            return True
        item = EditConfirmation(user_id=from_user, action="overwrite_today", report_date=today,
            original_content_hash=content_hash(existing), original_preview=existing[:80],
            final_content=body, created_at=_time.monotonic(), expires_at=_time.monotonic() + 120)
        _edit_confirmations.save(item)
        _send_long_text(cfg, f"目标日期：{today}\n原正文摘要：{existing[:80]}\n修改后正文：\n{body}", from_user)
        _send_wechat_text(cfg.wechat, "回复“确认执行”提交，或“取消执行”放弃。", from_user)
        return True

    return False
```

`确认/取消`（MsgId 去重与单次消费原子；不在此 begin/clear 标记；通知成功才删标记）：

```python
_confirm_dedup_lock = threading.Lock()


def _consume_confirm_or_skip(msg_id: str, from_user: str):
    """原子地完成 MsgId 在途标记与待确认项消费（同一临界区），满足设计 rule 6。
    返回 (item, duplicate)：item 为已消费的 EditConfirmation；duplicate=True 表示 MsgId 重复回调。"""
    with _confirm_dedup_lock:
        if msg_id and (msg_id in _msgid_seen or msg_id in _msgid_inflight):
            return None, True  # 重复回调，不消费
        if msg_id:
            _msgid_inflight[msg_id] = _time.monotonic()
        item = _edit_confirmations.confirm(from_user)  # 单次消费
    return item, False


def _route_confirm_or_cancel(stripped: str, from_user: str) -> bool:
    """是否由 OA 确认路径处理。True=OA（后台执行），False=回落 AI 流程。
    peek 已过滤过期，故过期项返回 False → 回调落入 AI 流程（真实路径）。"""
    if stripped not in ("确认执行", "取消执行"):
        return False
    return _edit_confirmations.peek(from_user) is not None


def _handle_edit_confirmation(from_user, cfg, msg_id: str = "") -> bool:
    from src.target import modify_daily_report
    item, duplicate = _consume_confirm_or_skip(msg_id, from_user)
    if item is None:
        if not duplicate:
            _send_wechat_text(cfg.wechat, "待确认的日报修改已超时或不存在，请重新发起。", from_user)
        if msg_id:
            _msgid_mark_done(msg_id, processed=True)
        return True
    try:
        ok, msg, meta = modify_daily_report(item.final_content, cfg, item.report_date,
                                             expected_content_hash=item.original_content_hash,
                                             user_id=item.user_id)
        action = meta.get("action")
        if ok and action == "overwrite":
            _send_wechat_text(cfg.wechat, f"日报已修改：{msg}", from_user)
        elif action == "unknown":
            _send_wechat_text(cfg.wechat, "修改结果未知，请手动核对 OA 当天日报。", from_user)
        else:
            _send_wechat_text(cfg.wechat, f"修改失败：{msg}", from_user)
    finally:
        if msg_id:
            _msgid_mark_done(msg_id, processed=True)  # 消费+执行后才记 seen
    return True


def _handle_edit_cancel(from_user, cfg) -> bool:
    if _edit_confirmations.cancel(from_user):
        _send_wechat_text(cfg.wechat, "已取消本次日报修改。", from_user)
        return True
    return False


def _run_confirm_in_background(fn, from_user, cfg, msg_id="", cfg_obj=None):
    """确认执行专用：不在派发时标记在途（消费时在 _consume_confirm_or_skip 内原子标记），
    避免'MsgId 在途已记录、待确认项未消费'的崩溃窗口。"""
    def _worker():
        try:
            fn(from_user, cfg, msg_id=msg_id)
        except Exception as exc:
            try:
                _send_wechat_text(cfg_obj.wechat, f"处理失败：{exc}", from_user)
            except Exception:
                pass
    threading.Thread(target=_worker, daemon=True).start()


def _check_stale_modify_on_startup(cfg):
    for stale in peek_stale_modify_markers():  # 只读不删
        user_id = stale.get("user_id")
        report_date = stale.get("report_date", "未知日期")
        try:
            sent_ok = _send_wechat_text(cfg.wechat,
                f"上次日报修改（{report_date}）可能未完成，请核对 OA 当天正文。", user_id)
        except Exception as e:
            logger.error(f"stale modify notify failed: {e}", exc_info=True)
            continue  # 保留标记，下次重启再提醒
        if sent_ok:  # send_text 返回 bool；True 才删
            clear_modify_marker(user_id)
        else:
            logger.warning("stale modify notify returned False, keep marker for next restart")
```

修改 `wechat_callback`（703-761）文本分支。原：

```python
    if msg_type == "text":
        content = extract_text_content(msg)
        content = _normalize_daily_report_command_text(content)
        from_user = msg.get("FromUserName", "")
```

改为（**只插入新分支，不删除/移动原有 `_ai_command_in_progress()` 等并发检查与原有分派块**）：

```python
    if msg_type == "text":
        raw_content = extract_text_content(msg)
        from_user = msg.get("FromUserName", "")
        msg_id = msg.get("MsgId", "")

        # 1) 精确日报编辑指令：后台线程处理，保留原始多行正文
        if parse_daily_report_edit_command(raw_content) is not None:
            _run_in_background(_handle_daily_report_edit_command, raw_content, from_user, cfg,
                               msg_id=msg_id, cfg_obj=cfg, from_user=from_user)
            return "", 200

        # 2) 确认/取消：peek 过滤过期；命中 OA 待确认才走 OA 路径，否则回落 AI 流程
        stripped = raw_content.strip()
        if _route_confirm_or_cancel(stripped, from_user):
            if stripped == "确认执行":
                _run_confirm_in_background(_handle_edit_confirmation, from_user, cfg,
                                          msg_id=msg_id, cfg_obj=cfg)
            else:
                _run_in_background(_handle_edit_cancel, from_user, cfg,
                                   msg_id=msg_id, cfg_obj=cfg, from_user=from_user)
            return "", 200

        # 3) 原有流程：标准化、_ai_command_in_progress 等并发检查、ai_bridge.prepare 等保持不动
        content = _normalize_daily_report_command_text(raw_content)
        # ↓ 保留原有 _ai_command_in_progress() 等检查与 prepared = ai_bridge.prepare(...) ↓
        prepared = ai_bridge.prepare(content, from_user, beijing_today())
        if prepared.handled:
            return "", 200
        content = prepared.content
        # 4) AI 路由命中日报动作：服务器从原始消息提取正文后后台处理
        if content in _AI_ACTION_MAP:
            body = _extract_body_from_raw(raw_content)
            if content in ("设置日报", "追加日报", "追加今日日报", "修改今日日报") and not body.strip():
                _send_wechat_text(cfg.wechat,
                    "未识别到日报正文。请用换行或冒号分隔动作与正文，例如：\n设置日报\\n一、任务内容", from_user)
                return "", 200
            _run_in_background(_handle_daily_report_edit_command, raw_content, from_user, cfg,
                               action=_AI_ACTION_MAP[content], body=body,
                               msg_id=msg_id, cfg_obj=cfg, from_user=from_user)
            return "", 200
        # 5) 其它：沿用原有分派块（不变）
```

实施者：`_ai_command_in_progress()` 等原有并发守卫若位于 `content = _normalize...` 与 `prepared = ...` 之间，保持原位不动；AI 动作分支（第 4 步）位于 `prepared` 之后、原有分派块之前，确保经过原有并发守卫。在服务启动入口（`create_app` 初始化 cfg 后或 `main` 启动时）调用一次 `_check_stale_modify_on_startup(cfg)`。

- [ ] **Step 4:** `python -m pytest tests/test_report_recovery.py tests/test_server_command_normalization.py -v` → PASS。
- [ ] **Step 5:** Commit `feat: 后台线程化、追加日报分流、8 指令分派、AI 接入、分段发送、启动期残留检查`。

---

### Task 8: AI 路由新增 6 个日报动作

**Files:**
- Modify: `src/ai_command_router.py:172-206`、`classify_canonical_command`
- Test: `tests/test_server_command_normalization.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_server_command_normalization.py  追加
def test_classify_daily_report_actions_none_risk():
    import src.ai_command_router as r
    for cmd in ("设置日报", "追加日报", "追加今日日报", "修改今日日报", "查看草稿", "清除草稿"):
        assert r.classify_canonical_command(cmd) == "none"
```

- [ ] **Step 2:** `python -m pytest tests/test_server_command_normalization.py::test_classify_daily_report_actions_none_risk -v` → FAIL。
- [ ] **Step 3:**

`src/ai_command_router.py` 系统提示词“无参数指令”列表后追加：

```
日报草稿与修改指令（AI 只返回动作名，不得在 canonical_command 中复述日报正文；正文由系统从用户原始消息提取）：
设置日报；追加日报；追加今日日报；修改今日日报；查看草稿；清除草稿。
判定规则：用户意图是设置/追加/修改今天日报或查看/清除草稿时，canonical_command 取对应动作名；不判断今天是否已提交。
```

`classify_canonical_command`：

```python
_DAILY_REPORT_ACTIONS = {"设置日报", "追加日报", "追加今日日报", "修改今日日报", "查看草稿", "清除草稿"}

def classify_canonical_command(command: str) -> str:
    if command in _DAILY_REPORT_ACTIONS:
        return "none"
    # ... 现有分类逻辑不变 ...
```

`ai_assistant.py` 无需改分支（risk=none → 现有非 write 分支返回 canonical_command）。

- [ ] **Step 4:** `python -m pytest tests/test_server_command_normalization.py -v` → PASS。
- [ ] **Step 5:** Commit `feat: AI 路由新增 6 个日报动作且不携带正文`。

---

### Task 9: 通知来源文案、实际动作与 draft_note 显示、帮助菜单

**Files:**
- Modify: `src/wechat_notifier.py`：`_report_source_label`（141-151）加 `smart_sheet_append`；`notify_report_success`（182-221）显示实际动作与 `draft_note`。
- Modify: `src/server.py`：`_daily_help_text()` 增加 8 条新指令。
- Test: `tests/test_server_command_normalization.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_server_command_normalization.py  追加
def test_smart_sheet_append_label():
    import src.wechat_notifier as n
    assert n._report_source_label("smart_sheet_append") == "智能文档加手工追加"


def test_notify_success_includes_action_and_draft_note(monkeypatch):
    import src.wechat_notifier as n
    sent = []
    monkeypatch.setattr(n, "_is_configured", lambda cfg: True)  # 绕过 corpid 等配置检查
    monkeypatch.setattr(n, "send_markdown", lambda wechat, md: sent.append(md))
    from types import SimpleNamespace
    n.notify_report_success(SimpleNamespace(wechat=SimpleNamespace()), "正文",
                            {"action": "提交", "draft_note": "草稿已清除。"}, report_source="manual")
    blob = "".join(sent)
    assert "提交" in blob and "草稿已清除" in blob


def test_help_contains_all_new_commands():
    import src.server as s
    msgs = s._build_help_messages("日报指令")
    text = msgs[0] if msgs else ""
    for cmd in ("设置日报", "追加日报", "追加今日日报", "修改今日日报", "查看草稿", "清除草稿", "确认执行", "取消执行"):
        assert cmd in text, f"帮助菜单缺少 {cmd}"
```

- [ ] **Step 2:** `python -m pytest tests/test_server_command_normalization.py -k "smart_sheet_append_label or notify_success_includes or help_contains" -v` → FAIL。
- [ ] **Step 3:**

`src/wechat_notifier.py` 的 `labels` 字典加：

```python
        "smart_sheet_append": "智能文档加手工追加",
```

`notify_report_success` 在 `md_content` 中插入“实际动作”，末尾追加 `draft_note`（替换原 204-221 的 `md_content` 构造与 `send_markdown`）：

```python
    action_label = {"submit": "提交", "overwrite": "覆盖"}.get(info.get("action"), info.get("action", ""))
    md_content = f"""## ✅ 日报提交成功

> **发送时间**：{send_time}
> **日报日期**：{report_date} {weekday_cn}
> **实际动作**：{action_label}
> **日报类型**：{source_label}
> **智能文档状态**：{smart_status_text}
> **项目名称**：{project}
> **工作时长**：{hours} 小时
> **是否出差**：{travel}
> **日志类型**：{log_type}
> **提交系统**：OA企业信息管理平台

### 日报详情
```text
{content}
```
"""
    draft_note = info.get("draft_note", "")
    if draft_note:
        md_content += f"\n> {draft_note}\n"
    send_markdown(cfg.wechat, md_content)
```

`src/server.py` 的 `_daily_help_text()` 追加：

```
设置日报
[换行后写完整内容]：今天未提交时保存/覆盖当天草稿
追加日报
[换行后写追加内容]：自动判断追加到已提交的今天 OA 或当天草稿
追加今日日报
[换行后写追加内容]：明确追加到今天已提交 OA（需确认）
修改今日日报
[换行后写完整内容]：覆盖今天已提交 OA（需确认）
查看草稿：显示当前草稿
清除草稿：删除当前草稿（含过期）
确认执行 / 取消执行：确认或放弃待执行的 OA 日报修改（120 秒内有效）
```

- [ ] **Step 4:** `python -m pytest tests/test_server_command_normalization.py -k "smart_sheet_append_label or notify_success_includes or help_contains" -v` → PASS。
- [ ] **Step 5:** Commit `feat: 草稿来源文案、实际动作与 draft_note 显示、帮助菜单`。

---

### Task 10: .gitignore 与回归

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1:** 确保 `.gitignore` 含：

```
data/daily_report_draft.json
data/daily_report_modify_inprogress_*.json
data/*.json
!data/.gitkeep
```

- [ ] **Step 2:** `python -m pytest -v` → 全部 PASS。

- [ ] **Step 3: 手动回归清单（真机，记录结果）**

- 现有日报立即发送、定时提交、上一条日报确认、撤回、查询、统计通过。
- 现有 Cookie 检查与续期通过。
- 理财净值/收益看板通过。
- 真机：`设置日报`→草稿→`发送日报`→OA 落库正文与草稿逐字一致（多行/编号/行内空格；尾随空白允许规范化）。
- 真机：今天已提交时`追加日报`→确认消息含目标日期/原摘要/修改后正文→`确认执行`→OA 当天正文被追加；重复`确认执行`不重复修改。
- 真机：`修改今日日报`→`确认执行`→OA 当天正文被覆盖，重新导航回读一致。
- 真机：`修改今日日报：一、修复\n二、测试`（冒号+换行混合）正文完整保留。
- 真机：OA 表格加载超时时“追加日报”返回 error 而非误创建草稿。
- 真机：今天已审核时`发送日报`→收到“跳过”提示而非“提交成功”。
- 真机：`查看草稿`长正文分段发送，确认入口在全文发完后给出。
- 真机：制造按用户残留标记后重启应收到“上次修改可能未完成”通知；通知失败则保留标记下次再提醒。
- 真机：AI 自然语言“往日报后面加一条：完成生产验证”→追加 OA 待确认。

- [ ] **Step 4:** Commit `chore: 忽略草稿与修改标记等运行态数据文件`。

---

## Self-Review

**Spec coverage**：草稿模型/规则→Task 1；解析（最早分隔符、不 strip）→Task 3+7；指令语义→Task 7；追加分流（真三态+Cookie 预检+过期前置）→Task 7；草稿优先提交+清除→Task 6；OA 修改（真三态+重新导航回读+异常分流+锁内标记）→Task 4；二次确认（peek 过期+按用户标记+通知成功才删）→Task 2+7；AI 识别→Task 8+7；并发/MsgId（在途超时）→Task 5+7；通知→Task 9；硬崩溃→Task 2+7 标记+启动检查；format_form 兼容→Task 0 安全闸门。

**阻断问题对照**：
1. 真三态→Task 4 `_find_report_row_on_pages` 内联 `wait_for_selector`，超时返回 `error`，不复用 `_find_existing_report_row`；测试覆盖超时=error、加载无目标=absent、命中=found、翻页第二页。
2. 提交后异常→Task 4 `submitted` 在 `_submit_dialog` 前置 True，之后异常走 `_modify_failure_result(True,...)=unknown`；测试覆盖 pre/post submit。
3. 同用户标记竞态→Task 4 begin/clear 在 `modify` 锁内、clear 在 `_BROWSER_LOCK.release()` 前；测试 `test_modify_marker_lifecycle_under_lock` 断言 begin<clear<release。启动期 `peek` 只读、通知成功才 clear（Task 7）。
4. Task 0 污染生产→改为测试账号/空白天+备份+恢复+规范化比对。

**其他问题对照**：未知动作→Task 6 报“无法确认”+return False，测试 `test_unknown_action_reports_unconfirmed`；冒号正文不 strip→Task 3，测试 `test_body_leading_space_preserved`；draft_note 成功显示“草稿已清除”→Task 6 `_on_actual_write` + Task 9 通知；过期确认真实路径→Task 2 peek 过滤 + Task 7 `test_route_confirm_expired_falls_through`、`test_confirm_consumed_sends_timeout`（race/消费）；`_ai_command_in_progress`→Task 7 注明保留原位、AI 动作分支经其之后；`smart_doc_status` 作为 kwarg→Task 6 测试 `cap["success"]["k"]["smart_doc_status"]`；通知测试 patch `_is_configured`+`send_markdown`→Task 9；`_send_long_text` 传 `SimpleNamespace(wechat=...)`→Task 7 测试；标记生命周期真测→Task 4 `test_modify_marker_lifecycle_under_lock`（不替换 modify，伪造内部，用 `ClickableRow`）。

**第五轮 6 修复对照**：(1) 空表→absent：`_find_report_row_on_pages` 等 `table, .el-table` 容器而非行，`test_find_row_empty_table_is_absent`。(2) 通知返回 False 不删标记：`_check_stale_modify_on_startup` 检查 `send_text` 返回值，`test_stale_notify_false_keeps_marker`。(3) 过期确认真实回调路径：`_route_confirm_or_cancel` 可测，过期返回 False→回落 AI，`test_route_confirm_expired_falls_through`/`test_route_confirm_present_routes_oa`。(4) MsgId 与消费原子：`_consume_confirm_or_skip` 在同一锁内完成在途标记+消费，`_run_confirm_in_background` 派发时不预标记在途，`test_consume_confirm_atomic_dedup`。(5) 标记生命周期假行对象：改用 `ClickableRow`（`query_selector` 返回修改按钮），`test_modify_marker_lifecycle_under_lock` 真测 begin<clear<release。(6) Task 0 收尾：空白天用 `delete_daily_report` 删测试日报，失败人工清理。

**仍需真机验证**：OA 弹窗长度限制（Task 0 前置）、回读规范化一致性、`check_cookies` 实际契约（已 try/except 兼容）、`_login_and_navigate` 是否已导航报表页（按现状补 goto）、`PlaywrightTimeout`/`logger` 在 `target.py` 的确切引用名按现状校正。
