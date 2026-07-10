# Daily Report Chinese Numbering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate smart-sheet daily report list numbers as Chinese numerals followed by `、`.

**Architecture:** Keep numbering inside `src/processor.py`, where generated task strings are already assembled into the OA report body. Add a small dependency-free converter for positive task indexes and leave extracted task text, manual bodies, and historical OA content untouched.

**Tech Stack:** Python 3, pytest.

---

### Task 1: Convert generated report indexes to Chinese numerals

**Files:**
- Modify: `src/processor.py`
- Create: `tests/test_processor.py`

- [ ] **Step 1: Write failing converter and report-format tests**

```python
import pytest

from src.processor import _to_chinese_number, format_report


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, "一"),
        (2, "二"),
        (10, "十"),
        (11, "十一"),
        (20, "二十"),
        (21, "二十一"),
        (101, "一百零一"),
        (1001, "一千零一"),
    ],
)
def test_converts_positive_task_indexes_to_chinese(value, expected):
    assert _to_chinese_number(value) == expected


def test_formats_generated_report_with_chinese_numbering():
    report = format_report([
        "【监管数核工具】修复生产环境已知bug：修复生产环境已知bug",
        "【监管数核工具】优化对账逻辑：优化匹配规则",
    ])

    assert report == (
        "一、【监管数核工具】修复生产环境已知bug：修复生产环境已知bug\n"
        "二、【监管数核工具】优化对账逻辑：优化匹配规则"
    )
```

- [ ] **Step 2: Run tests and verify RED**

Run: `pytest -q tests/test_processor.py`

Expected: collection fails because `_to_chinese_number` does not exist.

- [ ] **Step 3: Implement the converter and use it in `format_report()`**

Add to `src/processor.py`:

```python
_CHINESE_DIGITS = "零一二三四五六七八九"


def _to_chinese_number(value: int) -> str:
    if value <= 0 or value > 9999:
        return str(value)

    parts = []
    zero_pending = False
    remainder = value
    for divisor, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        digit, remainder = divmod(remainder, divisor)
        if digit:
            if zero_pending:
                parts.append("零")
            if not (divisor == 10 and digit == 1 and not parts):
                parts.append(_CHINESE_DIGITS[digit])
            parts.append(unit)
            zero_pending = False
        elif parts and remainder:
            zero_pending = True
    return "".join(parts)
```

Change the existing line builder to:

```python
lines.append(f"{_to_chinese_number(i)}、{name}")
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `pytest -q tests/test_processor.py`

Expected: all tests pass.

### Task 2: Verify and deploy

**Files:**
- Modify on server: `/home/ubuntu/daily_report/src/processor.py`

- [ ] **Step 1: Run local regressions**

Run:

```bash
pytest -q
python -m compileall -q src
git diff --check -- src/processor.py tests/test_processor.py
```

Expected: all tests pass, compilation succeeds, and no whitespace errors are reported.

- [ ] **Step 2: Back up and upload only the processor**

```bash
mkdir -p /home/ubuntu/daily_report/backups/20260710_daily_report_chinese_numbering/src
cp /home/ubuntu/daily_report/src/processor.py /home/ubuntu/daily_report/backups/20260710_daily_report_chinese_numbering/src/processor.py
```

Upload `src/processor.py` only. Do not upload `config.yaml`.

- [ ] **Step 3: Verify generated body on the server without submitting**

Run:

```bash
cd /home/ubuntu/daily_report
venv/bin/python - <<'PY'
from src.processor import format_report

print(format_report(["【项目甲】任务一", "【项目乙】任务二"]))
PY
```

Expected:

```text
一、【项目甲】任务一
二、【项目乙】任务二
```

- [ ] **Step 4: Restart and verify service health**

Run:

```bash
cd /home/ubuntu/daily_report
venv/bin/python -m py_compile src/processor.py
systemctl restart daily-report
systemctl is-active daily-report
tail -n 30 service.log
```

Expected: service is `active` and starts without traceback with all existing schedules intact.
