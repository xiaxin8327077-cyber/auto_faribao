# 理财组合看板 - 产品份额与市值明细设计

## 文档状态

- 修订日期：2026-07-29
- 状态：已确认，待编写实施计划
- 范围：手机版理财组合看板 `/nav` 产品持仓列表新增每个产品的当前份额与估算市值

## 背景与现状

`src/nav_dashboard.py` 的 `payload()` 已经为每个产品返回 `shares`（份额）与 `latest_nav`（最新单位净值）字段，顶部"估算市值(元)"也是由这两者在后端用 `Decimal` 相乘得出。

当前产品行只展示：排名、名称、代码、最新净值、净值日期、最近一期收益、涨跌幅。份额与每产品市值是后端已知的数据，但页面上不可见。用户希望每个产品都能直观看到"持有了多少份"与"当前值多少钱"。

现有约束：

1. `/nav` 页面是单文件 HTML（`src/nav_dashboard_page.html`），通过私密链接 `http://host:port/nav#<token>` 访问，密钥走 URL Fragment 不进服务器日志。
2. 顶部三个指标卡"估算市值(元)"、"已设份额 X/Y"、"今日披露 X/Y"是用户已认可的视觉重点。
3. 现有产品行采用四列 grid（rank / name+code / nav+date / income+pct），在 390px 视口下排版已满。
4. 已有 `design-qa.md` 流程记录每次视觉迭代的截图与差异。

## 目标

- 在产品行中显示每个产品的当前份额与估算市值。
- 不改变顶部三指标卡与全局数据。
- 不修改后端 payload 输出，不影响企微日报、晚报、查询、调度。
- 在 390px 与 430px 视口下保持原有排版节奏，不溢出。

## 非目标

- 不在 daily profit bottom sheet（收益明细弹层）中展示份额或市值。
- 不修改后端 `payload()` 字段。
- 不修改企微指令、报告生成、调度、Cookie 续期等任何其他模块。
- 不引入 Decimal.js 等第三方库。
- 不允许通过网页直接修改份额（份额仍只能通过 `config.yaml` 修改并重启服务）。
- 不动顶部"估算市值(元)"、"已设份额 X/Y"、"今日披露 X/Y"三个指标卡。
- 不在网页上做数字 Decimal 精度（接受浮点亚分位误差）。

## 方案选择

采用"纯前端 + 复用 payload 现有字段 + Decimal 在后端仍是单一可信源"。

职责划分：

- `src/nav_dashboard_page.html`：新增 `formatHolding()` 函数、新增 `.holding` CSS、在产品模板中根据 `shares` 是否存在决定是否输出 holding 节点
- `src/nav_dashboard.py`：不修改
- `src/server.py`：不修改
- `src/nav_monitor.py`、`src/scheduler.py`、`src/wechat_notifier.py`：不修改

不采用以下方案：

- 不在后端 `payload()` 新增 `market_value` 字段（保持后端契约稳定；如未来出现亚分位精度反馈，再回退本设计迁移为后端方案）
- 不在 daily profit bottom sheet 中复用 holding 行（语义不一致：弹层是收益历史，不是持仓明细）
- 不修改产品行 grid 模板结构（避免破坏已验收的 5 行紧凑排版）

## 显示规则

每个产品行在现有 `<div class="code">` 节点之后新增兄弟节点 `<div class="holding">`，内容由前端 `formatHolding(shareText, navText)` 决定：

| 输入条件 | `formatHolding` 返回 | 模板行为 |
|---------|---------------------|---------|
| `shares` 为空/缺失/非数字 | `""` | 整个 `<div class="holding">` 节点不输出，避免出现 4px 空行 |
| `shares` 存在，`latest_nav` 缺失/非数字 | `"{份额} 份"` | 渲染 holding 节点，不含"· 市值" |
| `shares` 存在，`latest_nav` 存在 | `"{份额} 份 · 市值 {市值} 元"` | 完整渲染 |

格式细节：

- 份额：`Number(shareText).toFixed(2)`，固定 2 位小数，**不带千分位**（与 `config.yaml` 中 `401133.95` 的写法一致，避免与市值格式混淆）
- 市值：`(Number(shareText) * Number(navText)).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })`，千分位 + 2 位小数 + " 元"（与现有 `money()` 工具同源）
- 分隔符：`·`（U+00B7 MIDDLE DOT），与已有设计语汇一致

## 视觉样式

新增 `.holding` CSS 规则，完全沿用 `.code` 的视觉权重：

```css
.holding {
  margin-top: 4px;
  color: var(--muted);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
```

`font-variant-numeric: tabular-nums` 让份额与市值的数字等宽，避免不同位数的份额（如 `401133.95` 与 `87,728.31`）抖动宽度。

隐藏逻辑用模板三元判断 `holding ? \`<div class="holding">${esc(holding)}</div>\` : ''`，避免留下空 `<div>` 产生的 4px 空白高度。

## 精度与误差

前端用 `Number` + `toLocaleString` 走 64 位双精度浮点，可能出现亚分位（< 0.01 元）误差：

- 份额 × 6 位净值结果刚好处于 0.005 元边界时，浮点取整与 Decimal 取整可能差 0.01 元
- 大份额（如 > 2^53）时双精度会丢失个位

设计选择**接受这一误差**：

1. 用户对单产品市值做"心算"或与全局"估算市值"对照时差异不构成误读
2. 顶部"估算市值(元)"与 daily profit bottom sheet 中所有金额仍由后端 `Decimal` 精确计算
3. 与现有 `number(item.latest_nav).toFixed(6)` 在 `.nav` 节点的取舍一致

如果未来出现用户对亚分位误差的明确反馈，可按本设计"非目标"的回退路径，将 `market_value` 计算下沉到后端 `payload()` 新增 `market_value` 字段（迁移成本：HTML 内 `formatHolding()` 改读 `item.market_value` 即可，模板结构不变）。

## 改动文件

| 文件 | 类型 | 内容 |
|------|------|------|
| `src/nav_dashboard_page.html` | 修改 | 新增 `.holding` CSS；新增 `formatHolding()` JS 函数；产品模板中插入 holding 节点；空字符串时整节点不输出 |
| `tests/test_nav_dashboard_page.py` | 新增 | HTML 结构回归（CSS 规则、模板节点引用、顶部指标未变）+ 格式化逻辑契约测试（Python 等价函数 + 锁住用例样本） |
| `docs/superpowers/specs/2026-07-29-nav-dashboard-product-holdings-design.md` | 新增 | 本设计文档 |
| `design-qa.md` | 修改 | 在末尾"按需"小节追加本次视觉迭代记录 |

明确不修改：

- `src/nav_dashboard.py`：payload 字段不变
- `src/server.py`：路由不变
- `src/nav_monitor.py`、`src/scheduler.py`、`src/wechat_notifier.py`、`src/extractor.py`、`src/target.py`、`src/cookies_checker.py`、`src/ai_*`、`src/daily_report_*`、`src/pending_confirmation.py` 等
- `config.yaml`：份额配置入口不变

## 测试设计

### HTML 结构回归

加载 `src/nav_dashboard_page.html` 文本，断言：

- 存在 `.holding` CSS 规则（包含 `var(--muted)` 与 `font-variant-numeric: tabular-nums`）
- 产品模板（`products.map(...)` 内的 `<article class="product">` 字符串）包含 `<div class="holding">` 节点引用
- 顶部三个指标卡标签文本未变：`估算市值(元)`、`已设份额`、`今日披露`
- 现有 `.code` 规则未变（防止被合并改坏）
- 现有 `.product` grid 模板未引入新列（防回归）

### 格式化逻辑契约测试

在 `tests/test_nav_dashboard_page.py` 中复刻一份与 HTML 内 `formatHolding()` 行为完全等价的 Python 函数 `format_holding(share_text, nav_text) -> str`。在函数 docstring 中明确标注："必须与 `src/nav_dashboard_page.html` 内 `formatHolding()` 保持一致；HTML 改动时同步更新本函数。"

用例样本（与 HTML 用例同步）：

- `shares=""` → `""`
- `shares="401133.95"`，`nav=""` → `"401133.95 份"`
- `shares="401133.95"`，`nav="1.001234"` → `"401133.95 份 · 市值 401,628.95 元"`
- `shares="0"`，`nav="1.0"` → `"0.00 份 · 市值 0.00 元"`
- `shares="10000"`，`nav="1.000000"` → `"10000.00 份 · 市值 10,000.00 元"`
- `shares="abc"`，`nav="1.0"` → `""`（非数字视为未设置份额）
- `shares="10000"`，`nav="abc"` → `"10000.00 份"`（仅份额，市值缺失）

实现侧约束：HTML 的 `formatHolding()` 必须先做 falsy 与 `isNaN` 检查，再做 `toFixed` / `toLocaleString`，避免 `Number("")` 返回 0 导致"空字符串被当作 0 份"误渲染：

```js
function formatHolding(shareText, navText) {
  const shares = Number(shareText);
  if (!shareText || isNaN(shares)) return "";
  const sharesText = shares.toFixed(2);
  const nav = Number(navText);
  if (!navText || isNaN(nav)) return `${sharesText} 份`;
  const value = shares * nav;
  const valueText = value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${sharesText} 份 · 市值 ${valueText} 元`;
}
```

Python 契约函数 `format_holding()` 等价地先做 `if not share_text: return ""`，再做数值计算（保留两位 + 千分位）。

锁住这些样本后，HTML 端与 Python 端任一侧修改都会让测试失败，强制同步。

### 视觉验收

按 `design-qa.md` 既有流程：

- 启动本地 `python main.py`，访问 `http://localhost:8080/nav#<token>`
- 在 390x844 与 430x932 视口分别截图
- 确认 5 个产品中设置了份额的均显示 holding 行
- 确认未设份额的产品行不出现 holding 节点（不留 4px 空白）
- 确认 `scrollWidth <= innerWidth`（无横向溢出）
- 确认与现有 `.code` 视觉权重一致
- 把截图追加到 `design-qa.md` 末尾"按需"小节

### 回归

- `tests/test_nav_dashboard.py`（后端 payload 测试）应**无新增、无改动**
- `tests/test_nav_dashboard_routes.py`（路由测试）应无改动
- 跑完整 pytest，确认现有 300+ 用例无回归
- 检查 `git diff` 不包含 `config.yaml`、`data/nav_dashboard_state.json`、`data/nav_dashboard_access_token`、日志或截图

## 验收标准

- 5 个产品中设置了份额的均显示 `{份额} 份 · 市值 {市值} 元` 行
- 未设置份额的产品不显示新行，不留 4px 空白
- 390px 与 430px 视口下无横向溢出
- 新行视觉权重与 `.code` 完全一致
- 顶部"估算市值(元)"、"已设份额 X/Y"、"今日披露 X/Y"三个指标卡未变
- 后端 payload 字段未变（接口契约、企微、调度全部不受影响）
- `tests/test_nav_dashboard_page.py` 新测试通过
- 完整 pytest 测试集通过
- `design-qa.md` 已更新本次视觉迭代记录
- `git diff` 不包含 `config.yaml`、运行态数据文件、日志或截图
