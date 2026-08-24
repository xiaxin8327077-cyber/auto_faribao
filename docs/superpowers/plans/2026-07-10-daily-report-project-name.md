# Daily Report Project Name Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prefix every smart-sheet-generated daily report task with its real linked project name.

**Architecture:** Add a configurable smart-sheet project field, preload its linked project table once during extraction, and return structured task rows from browser JavaScript. Convert those rows to the existing `list[str]` API in Python so report building, preview, submission, and OA fallback boundaries remain unchanged.

**Tech Stack:** Python 3, Playwright sync API, WeCom Smart Sheet browser SDK, pytest.

---

### Task 1: Add project configuration and deterministic task formatting

**Files:**
- Modify: `src/config.py`
- Modify: `src/extractor.py`
- Create: `tests/test_extractor.py`

- [ ] **Step 1: Write failing tests for configuration and formatting**

```python
from src.config import SourceConfig
from src.extractor import _format_task_row, _format_task_rows


def test_source_config_defaults_project_field():
    assert SourceConfig({}).project_field == "所属项目"
    assert SourceConfig({"project_field": "关联项目"}).project_field == "关联项目"


def test_formats_task_with_project_and_description():
    assert _format_task_row({
        "project": "数据管理平台实施工作",
        "name": "处理数据质量问题",
        "description": "完成规则调整",
    }) == "【数据管理平台实施工作】处理数据质量问题：完成规则调整"


def test_formats_task_with_project_without_description():
    assert _format_task_row({
        "project": "数据管理平台实施工作",
        "name": "处理数据质量问题",
        "description": "",
    }) == "【数据管理平台实施工作】处理数据质量问题"


def test_project_placeholder_and_empty_project_keep_legacy_format():
    rows = [
        {"project": "...", "name": "任务一", "description": "说明"},
        {"project": "", "name": "任务二", "description": ""},
    ]
    assert _format_task_rows(rows) == ["任务一：说明", "任务二"]


def test_format_task_rows_keeps_list_of_strings_contract():
    assert _format_task_rows([{"project": "项目甲", "name": "任务一", "description": ""}]) == [
        "【项目甲】任务一"
    ]
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/test_extractor.py`

Expected: collection fails because `_format_task_row` and `_format_task_rows` do not exist, or the project field assertion fails.

- [ ] **Step 3: Add the project field and formatting helpers**

Add to `SourceConfig.__init__` in `src/config.py`:

```python
self.project_field = data.get("project_field", "所属项目")
```

Add to `src/extractor.py`:

```python
def _format_task_row(row: dict) -> str:
    name = str(row.get("name") or "").strip()
    if not name:
        return ""
    description = str(row.get("description") or "").strip()
    project = str(row.get("project") or "").strip()
    if project == "...":
        project = ""

    task_text = f"{name}：{description}" if description else name
    return f"【{project}】{task_text}" if project else task_text


def _format_task_rows(rows: list[dict]) -> list[str]:
    tasks = [_format_task_row(row) for row in rows]
    return [task for task in tasks if task]
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `pytest -q tests/test_extractor.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the formatting unit**

```bash
git add src/config.py src/extractor.py tests/test_extractor.py
git commit -m "feat: format report tasks with project names"
```

### Task 2: Resolve real project names from the smart-sheet relation

**Files:**
- Modify: `src/extractor.py`
- Modify: `tests/test_extractor.py`

- [ ] **Step 1: Write a failing contract test for browser extraction configuration**

```python
from src.extractor import _build_extract_eval_config


def test_extract_eval_config_includes_project_field():
    cfg = SourceConfig({"project_field": "关联项目"})

    assert _build_extract_eval_config(cfg)["projectField"] == "关联项目"
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `pytest -q tests/test_extractor.py::test_extract_eval_config_includes_project_field`

Expected: test collection fails because `_build_extract_eval_config` does not exist.

- [ ] **Step 3: Make smart-sheet evaluation asynchronous and preload the linked table**

Add the evaluated browser configuration helper to `src/extractor.py`:

```python
def _build_extract_eval_config(cfg: SourceConfig) -> dict:
    return {
        "tabId": cfg.tab_id,
        "personField": cfg.person_field,
        "statusField": cfg.status_field,
        "nameField": cfg.name_field,
        "personNames": cfg.person_names,
        "statusValues": cfg.status_values,
        "descField": cfg.desc_field,
        "projectField": cfg.project_field,
    }
```

Change the `page.evaluate` callback in `src/extractor.py` to `async (cfg) => { ... }`. Resolve the configured project field and preload its relation data once:

```javascript
const projectField = cfg.projectField
    ? fields.find(function(field) { return field.title === cfg.projectField; })
    : null;

if (projectField && typeof projectField.preloadLocalLinkTable === 'function') {
    try {
        await projectField.preloadLocalLinkTable();
    } catch (e) {
        // Project lookup is optional; task extraction must continue.
    }
}
```

Inside the existing filtered record loop, return structured rows and resolve relation display text from the standard cell:

```javascript
let projectText = '';
if (projectField) {
    try {
        const standardCell = projectField.getStandardCell(rid);
        const projectItems = standardCell && Array.isArray(standardCell.data)
            ? standardCell.data
            : [];
        projectText = projectItems
            .map(function(item) { return item && item.text ? item.text.trim() : ''; })
            .filter(function(text) { return text && text !== '...'; })
            .join('、');
    } catch (e) {
        projectText = '';
    }
}

if (taskName) {
    tasks.push({
        project: projectText,
        name: taskName,
        description: descText,
    });
}
```

Replace the inline evaluation configuration dictionary with `_build_extract_eval_config(cfg)`. Replace direct `task_list` return with:

```python
task_rows = tasks.get("tasks", [])
task_list = _format_task_rows(task_rows)
```

- [ ] **Step 4: Run extractor and report regression tests**

Run: `pytest -q tests/test_extractor.py tests/test_report_recovery.py tests/test_server_command_normalization.py`

Expected: all tests pass and `extract_tasks()` still returns `list[str]`.

- [ ] **Step 5: Commit linked-project extraction**

```bash
git add src/extractor.py tests/test_extractor.py
git commit -m "feat: resolve smart sheet project relations"
```

### Task 3: Verify locally and deploy safely

**Files:**
- Modify on server: `/home/ubuntu/daily_report/src/config.py`
- Modify on server: `/home/ubuntu/daily_report/src/extractor.py`

- [ ] **Step 1: Run complete local verification**

Run:

```bash
pytest -q
python -m compileall -q src
git diff --check
```

Expected: zero test failures, compilation exit code 0, and no whitespace errors.

- [ ] **Step 2: Back up only the affected production files**

```bash
mkdir -p /home/ubuntu/daily_report/backups/20260710_daily_report_project_name/src
cp /home/ubuntu/daily_report/src/config.py /home/ubuntu/daily_report/backups/20260710_daily_report_project_name/src/config.py
cp /home/ubuntu/daily_report/src/extractor.py /home/ubuntu/daily_report/backups/20260710_daily_report_project_name/src/extractor.py
```

- [ ] **Step 3: Upload code without overwriting production configuration**

Upload only `src/config.py` and `src/extractor.py`. Do not upload `config.yaml`.

- [ ] **Step 4: Run a read-only production extraction preview**

Run with the production virtual environment:

```bash
cd /home/ubuntu/daily_report
venv/bin/python - <<'PY'
from src.config import load_config
from src.extractor import extract_tasks

tasks = extract_tasks(load_config("config.yaml").source)
print(type(tasks).__name__)
for task in tasks:
    print(task)
PY
```

Expected: output type is `list`; each task with a readable project starts with `【真实项目名称】`; no OA report is submitted.

- [ ] **Step 5: Compile, restart, and verify service health**

```bash
cd /home/ubuntu/daily_report
venv/bin/python -m py_compile src/config.py src/extractor.py
systemctl restart daily-report
systemctl is-active daily-report
tail -n 50 service.log
```

Expected: service is `active`, startup completes without traceback, and existing scheduled features remain configured.

- [ ] **Step 6: Record final verification evidence**

Report the full pytest count, production preview examples, service status, and backup path to the user.
