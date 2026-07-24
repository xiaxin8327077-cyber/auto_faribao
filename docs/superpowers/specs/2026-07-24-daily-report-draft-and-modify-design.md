# 日报草稿暂存与提交后修改设计

## 背景与根因

当前日报系统的流程是：定时任务触发 → 从智能文档提取内容 → 提交到 OA。用户无法在提交前手动编辑内容，也无法在提交后追加或修改已提交的日报。

`target.py` 中已存在 `modify_daily_report` 函数（第 270 行），可以覆盖修改已提交的 OA 日报，但从未被调用，也未暴露到企业微信指令。

本设计新增两个能力：

1. **提交前草稿暂存**：用户可手动设置或追加日报内容，存为本地草稿，定时提交时优先使用草稿。
2. **提交后修改/追加**：用户可覆盖或追加已提交到 OA 的日报内容。

## 目标

- 用户可在定时提交前设置完整日报内容或追加补充内容，存为草稿，到点自动提交。
- 用户可在日报已提交后追加新内容到末尾，或用新内容完全覆盖。
- 草稿跨重启存活，提交后自动清除。
- 支持精确指令（多行文本）和 AI 自然语言识别。
- 不改变智能文档提取逻辑、OA 提交流程、理财看板和 Cookie 检查。

## 非目标

- 不增加日报历史版本管理。
- 不增加多用户草稿隔离（当前系统单用户）。
- 不修改 OA 系统的日报数据结构。
- 不增加服务自动重启。

## 方案选择

采用"本地 JSON 草稿 + OA 直接修改"方案。

- 草稿存储为 `data/daily_report_draft.json`，复用 `pending_confirmation.py` 的本地文件模式，跨重启存活。
- 提交后修改复用已有的 `modify_daily_report`，追加模式先调用 `get_previous_report_content` 读取当前 OA 内容再拼接覆盖。
- 不使用内存存储（重启丢失）或 SQLite（过度设计）。

## 提交前：草稿暂存 + 定时提交

### 草稿文件结构

```json
{
  "content": "一、【项目A】任务名称：描述\n二、【项目B】任务名称：描述",
  "source": "manual",
  "created_at": "2026-07-24T17:30:00",
  "updated_at": "2026-07-24T17:35:00"
}
```

- `content`：日报正文，中文数字编号列表格式。
- `source`：`"manual"`（用户直接输入）或 `"smart_sheet_append"`（智能文档提取 + 用户追加）。
- `created_at` / `updated_at`：北京时间 ISO 格式。

### 指令

| 指令 | 格式 | 行为 |
|------|------|------|
| 设置日报 | `设置日报\n[完整内容]` | 整体覆盖草稿，source 为 `manual` |
| 追加日报 | `追加日报\n[追加内容]` | 有草稿→追加到末尾；无草稿→先提取智能文档再追加，source 为 `smart_sheet_append` |
| 查看草稿 | `查看草稿` | 显示当前草稿内容和来源 |
| 清除草稿 | `清除草稿` | 删除草稿文件，恢复自动提取模式 |

### 定时提交逻辑改动

`server.auto_submit_if_needed` 中，在 `precheck_cookies` 通过后、调用 `build_report_with_meta` 前，检查草稿：

```
if 草稿存在:
    report = 草稿内容
    source = 草稿 source
    meta = {"smart_doc_status": "skipped", "report_source": "draft"}
else:
    走现有 build_report_with_meta 流程
```

提交成功后清除草稿。

## 提交后：修改/追加已提交日报

`modify_daily_report` 是纯覆盖模式——传入新内容直接覆盖 OA 日报，不支持追加。追加需要先读取 OA 当前内容再拼接。

### 指令

| 指令 | 格式 | 行为 | 实现 |
|------|------|------|------|
| 修改今日日报 | `修改今日日报\n[新内容]` | 覆盖 OA 日报 | `modify_daily_report(新内容, cfg)` |
| 追加今日日报 | `追加今日日报\n[追加内容]` | 追加到 OA 日报末尾 | `get_previous_report_content(cfg)` 读取 → 拼接 → `modify_daily_report(拼接内容, cfg)` |

### 错误处理

- OA 读取失败（今日无日报）：追加今日日报时报"今日尚未提交日报，请先发送日报"。
- `modify_daily_report` 失败：回复错误信息，不改变 OA 内容。
- 浏览器超时：报网络异常，不重试不覆盖。

## AI 自然语言识别

AI 路由器 prompt 新增指令模板和示例：

- 指令列表新增：`设置日报 日报内容`；`追加日报 追加内容`；`修改今日日报 日报内容`；`追加今日日报 追加内容`。
- 示例：
  - "帮我设置今天日报：一、【项目A】..." → `设置日报 一、【项目A】...`
  - "往日报后面加一条：..." → `追加今日日报 ...`（已提交）或 `追加日报 ...`（未提交）
  - "把日报改成：..." → `修改今日日报 ...`
- 风险等级：设置日报、追加日报为 `write`；修改今日日报、追加今日日报为 `write`（需二次确认）。

## 改动文件清单

| 文件 | 改动类型 | 内容 |
|------|---------|------|
| `src/daily_report_draft.py` | 新增 | 草稿 CRUD：`save_draft` / `load_draft` / `append_draft` / `clear_draft` / `has_draft` |
| `src/server.py` | 修改 | 新增 6 个企微指令分支；`auto_submit_if_needed` 检查草稿优先；帮助菜单新增草稿和修改指令 |
| `src/report_builder.py` | 修改 | `build_report_with_meta` 检查草稿优先 |
| `src/ai_command_router.py` | 修改 | prompt 新增指令模板和示例；指令分类新增 |
| `tests/test_daily_report_draft.py` | 新增 | 草稿 CRUD 单元测试 |
| `tests/test_report_recovery.py` | 修改 | 新增草稿优先提交、追加草稿、清除草稿测试 |

### 不改动

- `src/target.py` — `modify_daily_report` 和 `submit_daily_report` 不改动
- `src/extractor.py` — 智能文档提取逻辑不改动
- `src/processor.py` — 日报格式化不改动
- `src/scheduler.py` — 调 `auto_submit_if_needed` 即可，无需改动
- `src/cookies_checker.py` — 不改动
- `src/nav_dashboard.py` — 不改动

## 测试设计

### 草稿 CRUD

- `save_draft` 写入后 `load_draft` 能读回相同内容。
- `append_draft` 追加到已有草稿末尾。
- `append_draft` 无草稿时返回空（调用方决定是否先提取智能文档）。
- `clear_draft` 删除后 `has_draft` 返回 False。
- 草稿文件不存在时 `load_draft` 返回 None，不抛异常。

### 定时提交优先草稿

- 有草稿时 `auto_submit_if_needed` 用草稿内容提交，提交后草稿被清除。
- 无草稿时走现有 `build_report_with_meta` 流程。
- 草稿内容为空时不提交，报错。

### 提交后修改/追加

- `修改今日日报` 调用 `modify_daily_report` 覆盖。
- `追加今日日报` 先读取 OA 当前内容，拼接后调用 `modify_daily_report`。
- OA 无今日日报时追加报错。
- `modify_daily_report` 失败时回复错误，不改变 OA 内容。

### 指令解析

- `设置日报\n多行内容` 正确解析为草稿设置。
- `追加日报\n多行内容` 正确解析为追加。
- `修改今日日报\n多行内容` 正确解析为覆盖修改。
- `追加今日日报\n多行内容` 正确解析为追加修改。
- `查看草稿` 和 `清除草稿` 正确解析。

### AI 路由

- `classify_canonical_command` 正确分类新指令为 `write`。
- AI prompt 包含新指令模板和示例。

## 验收标准

- 用户可通过企微设置日报草稿，定时提交时使用草稿内容。
- 用户可通过企微追加内容到草稿末尾。
- 草稿跨重启存活，提交后自动清除。
- 用户可在日报提交后追加或覆盖修改。
- 追加今日日报时若 OA 无今日日报，报错提示先提交。
- AI 自然语言能识别"设置日报""追加""修改"意图。
- 帮助菜单包含所有新指令。
- 完整测试集通过。
- 本地提交不包含 `config.yaml`、Cookie、日志或生产数据。
