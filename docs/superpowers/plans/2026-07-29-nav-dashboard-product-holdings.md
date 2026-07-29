# 理财组合看板 - 产品份额与市值明细 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在手机版理财组合看板 `/nav` 产品持仓列表中新增每个产品的当前份额与估算市值，复用 `payload()` 现有 `shares` 与 `latest_nav` 字段，纯前端实现，不动后端、企微、调度。

**Architecture:** 仅修改 `src/nav_dashboard_page.html`（CSS + JS + 模板节点）与新增 `tests/test_nav_dashboard_page.py`（HTML 结构回归 + format_holding 契约）。Python 端契约函数 `format_holding()` 与 HTML 端 `formatHolding()` 行为一致，用例样本双向锁住。

**Tech Stack:** Python 3 + pytest、HTML/CSS/JavaScript（vanilla）、Decimal 精度的现有 `payload()` 字段。

---

## 文件结构

| 文件 | 类型 | 职责 |
|------|------|------|
| `src/nav_dashboard_page.html` | 修改 | 新增 `.holding` CSS、新增 `formatHolding()` JS、产品模板中插入 holding 节点 |
| `tests/test_nav_dashboard_page.py` | 新增 | HTML 结构回归（CSS 规则、模板节点引用、顶部指标未变）+ Python 端 `format_holding()` 契约函数与用例 |
| `docs/superpowers/specs/2026-07-29-nav-dashboard-product-holdings-design.md` | 新增 | 设计文档（已存在） |
| `design-qa.md` | 修改 | 末尾追加本次视觉迭代记录 |

明确不修改：`src/nav_dashboard.py`、`src/server.py`、`src/nav_monitor.py`、`src/scheduler.py`、`src/wechat_notifier.py`、`config.yaml`。

---

## Task 1：HTML 骨架（CSS 规则 + 模板节点）

**Files:**
- Modify: `src/nav_dashboard_page.html`（在 `<style>` 末尾添加 `.holding` 规则；在产品模板里插入 `<div class="holding">` 节点）
- Create: `tests/test_nav_dashboard_page.py`（HTML 结构回归测试）

- [ ] **Step 1：写失败的 HTML 结构测试**

在 `tests/test_nav_dashboard_page.py` 中：

```python
from pathlib import Path

PAGE_PATH = Path(__file__).resolve().parents[1] / "src" / "nav_dashboard_page.html"


def _read_page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")


def test_holding_css_rule_exists():
    page = _read_page()
    assert ".holding" in page
    assert "font-variant-numeric: tabular-nums" in page


def test_product_template_contains_holding_node():
    page = _read_page()
    assert '<div class="holding">' in page
    assert "holding" in page  # 模板里至少出现一次


def test_top_metrics_unchanged():
    page = _read_page()
    assert "估算市值(元)" in page
    assert "已设份额" in page
    assert "今日披露" in page
```

- [ ] **Step 2：跑测试确认失败**

Run: `cd D:/cherry/auto_faribao && python -m pytest tests/test_nav_dashboard_page.py -v`
Expected: 全部 3 个 FAIL（`.holding` CSS、`<div class="holding">`、模板引用都未找到）

- [ ] **Step 3：实现 HTML 骨架**

在 `src/nav_dashboard_page.html` 的 `<style>` 块中、紧跟现有 `.code` 规则后，新增：

```css
.holding { margin-top: 4px; color: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums; }
```

在产品模板（位于 `products.length ? products.map(...)` 内部、`<article class="product">` 字符串中）的 `.code` 节点之后，插入：

```html
<div class="holding">TBD</div>
```

模板大致结构变为：

```js
return `<article class="product"><div class="rank">${index + 1}</div><div><div class="name" title="${esc(item.name)}">${esc(item.name)}</div><div class="code">${esc(item.code)}</div><div class="holding">TBD</div></div><div class="nav"><div>${item.latest_nav ? number(item.latest_nav).toFixed(6) : '--'}</div><div class="date">${shortDate(item.nav_date)}</div></div><div class="income ${tone(profit)}"><div>${profit ? `${signedMoney(profit)} 元` : '--'}</div><div class="change">${item.change_pct ? signedPercent(item.change_pct) : '--'}</div></div></article>`;
```

TBD 是 Task 3 替换前的占位文本，确保 Task 1 提交后 UI 至少能渲染（虽然内容不正确）。

- [ ] **Step 4：跑测试确认通过**

Run: `cd D:/cherry/auto_faribao && python -m pytest tests/test_nav_dashboard_page.py -v`
Expected: 3 passed

- [ ] **Step 5：提交**

```bash
cd D:/cherry/auto_faribao && git add src/nav_dashboard_page.html tests/test_nav_dashboard_page.py && git -c user.name=jh832 -c user.email=jh832@local commit -m "feat(nav-dashboard): 产品行新增 holding 节点骨架与 CSS"
```

---

## Task 2：Python 端 `format_holding()` 契约函数

**Files:**
- Modify: `tests/test_nav_dashboard_page.py`（追加契约函数与用例）

- [ ] **Step 1：写失败的契约测试**

在 `tests/test_nav_dashboard_page.py` 末尾追加：

```python
def format_holding(share_text, nav_text):
    """Python 端契约函数，行为必须与 src/nav_dashboard_page.html 内的
    formatHolding() 完全一致。HTML 改动时必须同步更新本函数与用例样本。
    """
    raise NotImplementedError


def test_format_holding_empty_shares_returns_empty():
    assert format_holding("", "") == ""


def test_format_holding_only_shares_when_nav_missing():
    assert format_holding("401133.95", "") == "401133.95 份"
    assert format_holding("10000", "abc") == "10000.00 份"


def test_format_holding_both_present_full_text():
    assert format_holding("401133.95", "1.001234") == "401133.95 份 · 市值 401,628.95 元"
    assert format_holding("10000", "1.000000") == "10000.00 份 · 市值 10,000.00 元"


def test_format_holding_zero_values():
    assert format_holding("0", "1.0") == "0.00 份 · 市值 0.00 元"


def test_format_holding_non_numeric_shares_treated_as_empty():
    assert format_holding("abc", "1.0") == ""
    assert format_holding(None, "1.0") == ""


def test_format_holding_non_numeric_nav_only_shares():
    assert format_holding("10000", None) == "10000.00 份"
```

- [ ] **Step 2：跑测试确认失败**

Run: `cd D:/cherry/auto_faribao && python -m pytest tests/test_nav_dashboard_page.py -v`
Expected: 6 个新测试 FAIL（NotImplementedError）

- [ ] **Step 3：实现 `format_holding()`**

替换上面 `format_holding` 函数的实现：

```python
def format_holding(share_text, nav_text):
    """Python 端契约函数，行为必须与 src/nav_dashboard_page.html 内的
    formatHolding() 完全一致。HTML 改动时必须同步更新本函数与用例样本。
    """
    if not share_text:
        return ""
    try:
        shares = float(share_text)
    except (TypeError, ValueError):
        return ""
    if shares != shares:  # NaN 检查
        return ""
    shares_text = f"{shares:.2f}"
    if not nav_text:
        return f"{shares_text} 份"
    try:
        nav = float(nav_text)
    except (TypeError, ValueError):
        return f"{shares_text} 份"
    if nav != nav:  # NaN 检查
        return f"{shares_text} 份"
    value = shares * nav
    value_text = f"{value:,.2f}"
    return f"{shares_text} 份 · 市值 {value_text} 元"
```

- [ ] **Step 4：跑测试确认通过**

Run: `cd D:/cherry/auto_faribao && python -m pytest tests/test_nav_dashboard_page.py -v`
Expected: 全部 9 个测试 PASS（3 个 HTML 结构 + 6 个契约）

- [ ] **Step 5：提交**

```bash
cd D:/cherry/auto_faribao && git add tests/test_nav_dashboard_page.py && git -c user.name=jh832 -c user.email=jh832@local commit -m "test(nav-dashboard): format_holding Python 契约与用例样本"
```

---

## Task 3：HTML 端 `formatHolding()` 实现 + 模板调用

**Files:**
- Modify: `src/nav_dashboard_page.html`（在 JS 工具区添加 `formatHolding()`；把产品模板中 `TBD` 替换为 `formatHolding()` 调用，并按 falsy 决定是否输出整节点）

- [ ] **Step 1：在 HTML 内添加 `formatHolding()` 函数**

在 `nav_dashboard_page.html` 的 JS 工具区（在 `signedPercent` 与 `tone` 之间或紧邻 `money`/`signedMoney` 一组），新增：

```js
const formatHolding = (shareText, navText) => {
  const shares = Number(shareText);
  if (!shareText || isNaN(shares)) return '';
  const sharesText = shares.toFixed(2);
  const nav = Number(navText);
  if (!navText || isNaN(nav)) return `${sharesText} 份`;
  const value = shares * nav;
  const valueText = value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${sharesText} 份 · 市值 ${valueText} 元`;
};
```

- [ ] **Step 2：替换产品模板中 `TBD` 为 `formatHolding()` 调用 + 整节点条件输出**

定位产品模板中 `<div class="holding">TBD</div>` 这一行（Task 1 引入），替换为三元判断：

```js
const holding = formatHolding(item.shares, item.latest_nav);
const holdingLine = holding ? `<div class="holding">${esc(holding)}</div>` : '';
```

把 `<div class="holding">TBD</div>` 改为 `${holdingLine}`。最终模板大致结构：

```js
return `<article class="product"><div class="rank">${index + 1}</div><div><div class="name" title="${esc(item.name)}">${esc(item.name)}</div><div class="code">${esc(item.code)}</div>${holdingLine}</div><div class="nav"><div>${item.latest_nav ? number(item.latest_nav).toFixed(6) : '--'}</div><div class="date">${shortDate(item.nav_date)}</div></div><div class="income ${tone(profit)}"><div>${profit ? `${signedMoney(profit)} 元` : '--'}</div><div class="change">${item.change_pct ? signedPercent(item.change_pct) : '--'}</div></div></article>`;
```

- [ ] **Step 3：人工对照 Python 用例样本**

打开 `src/nav_dashboard_page.html`，定位 `formatHolding` 函数，对照 `tests/test_nav_dashboard_page.py` 中 `test_format_holding_*` 9 个用例样本（含 Task 2 的 6 个），在浏览器 Console（DevTools）逐条执行 JS 版 `formatHolding` 输入，确保输出与 Python 端完全一致。

- [ ] **Step 4：跑现有测试确认无回归**

Run: `cd D:/cherry/auto_faribao && python -m pytest tests/test_nav_dashboard_page.py -v`
Expected: 9 passed

- [ ] **Step 5：提交**

```bash
cd D:/cherry/auto_faribao && git add src/nav_dashboard_page.html && git -c user.name=jh832 -c user.email=jh832@local commit -m "feat(nav-dashboard): formatHolding JS 实现 + 产品模板调用"
```

---

## Task 4：视觉验收 + design-qa.md 更新

**Files:**
- Modify: `design-qa.md`（末尾追加本次迭代记录）

- [ ] **Step 1：启动本地服务**

```bash
cd D:/cherry/auto_faribao && python main.py
```

Expected: 服务监听 `0.0.0.0:8080`（参见 `config.yaml`）

- [ ] **Step 2：获取私密链接 token**

```bash
cd D:/cherry/auto_faribao && python -c "from src.nav_dashboard import get_access_token; print(get_access_token())"
```

把输出的 token 拼到 `http://localhost:8080/nav#<token>`。

- [ ] **Step 3：用浏览器在 390x844 视口截图**

用 in-app browser 或外部浏览器访问上述 URL，DevTools 切到 390x844 视口，截图保存为 `docs/nav-dashboard-390-product-holdings.png`。

- [ ] **Step 4：用浏览器在 430x932 视口截图**

同上，截图保存为 `docs/nav-dashboard-430-product-holdings.png`。

- [ ] **Step 5：人工验证清单**

对照 `docs/superpowers/specs/2026-07-29-nav-dashboard-product-holdings-design.md` 验收标准小节：

- [ ] 5 个产品中设置了份额的均显示 `{份额} 份 · 市值 {市值} 元` 行
- [ ] 未设置份额的产品不显示新行（不留 4px 空白高度）
- [ ] 390px 视口下 `scrollWidth <= 390`
- [ ] 430px 视口下 `scrollWidth <= 430`
- [ ] 新行视觉权重（字号、颜色、margin-top）与 `.code` 完全一致
- [ ] 顶部三指标卡文字未变
- [ ] daily profit bottom sheet 仍可正常打开

任一不通过：回到 Task 3 调整 CSS 或模板，通过则继续。

- [ ] **Step 6：追加 design-qa.md 记录**

在 `design-qa.md` 末尾追加：

```markdown
## Product holding line iteration

- Implementation screenshots: `docs/nav-dashboard-390-product-holdings.png`, `docs/nav-dashboard-430-product-holdings.png`
- Viewports: 390x844 and 430x932
- Change: per-product row gains a third sub-line under the existing `.code` line, showing `{shares} 份 · 市值 {market_value} 元`; hidden entirely when `shares` is empty.
- Visual: reuses `.code` weight (`color: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums`).
- Verified: all five configured products show the new line; no horizontal overflow at either viewport; existing metrics and bottom sheet unchanged.

final result: passed
```

- [ ] **Step 7：提交**

```bash
cd D:/cherry/auto_faribao && git add docs/nav-dashboard-390-product-holdings.png docs/nav-dashboard-430-product-holdings.png design-qa.md && git -c user.name=jh832 -c user.email=jh832@local commit -m "docs(nav-dashboard): 视觉验收截图与 design-qa 记录"
```

---

## Task 5：全量回归 + 差异校验

**Files:** 无（验证步骤）

- [ ] **Step 1：跑完整 pytest**

Run: `cd D:/cherry/auto_faribao && python -m pytest -q`
Expected: 所有测试通过（包括 9 个 `test_nav_dashboard_page` 新增测试 + 现有 300+ 用例）

- [ ] **Step 2：检查 git 工作区状态**

Run: `cd D:/cherry/auto_faribao && git status --short`
Expected: 工作区干净；若仍有未提交内容则根据需要追加提交（不允许是运行态数据）

- [ ] **Step 3：检查 git diff 不含运行态数据**

Run: `cd D:/cherry/auto_faribao && git diff HEAD~5 HEAD --stat`
Expected: 改动文件仅限 `src/nav_dashboard_page.html`、`tests/test_nav_dashboard_page.py`、`docs/superpowers/specs/2026-07-29-nav-dashboard-product-holdings-design.md`、`design-qa.md`、`docs/nav-dashboard-390-product-holdings.png`、`docs/nav-dashboard-430-product-holdings.png`。**不应**出现 `config.yaml`、`data/nav_dashboard_state.json`、`data/nav_dashboard_access_token`、`*.log`、Cookie 文件、临时截图（除本次验收保留的两张外）等。

- [ ] **Step 4：完成报告**

在 commit 信息或会话最终回复中说明：

- 已完成任务列表（1-5 全部勾选）
- 截图保存路径
- 完整 pytest 通过的最终输出（用例数）
- git diff 范围（仅目标文件）
- 是否已推送（默认不推，等用户指示）
