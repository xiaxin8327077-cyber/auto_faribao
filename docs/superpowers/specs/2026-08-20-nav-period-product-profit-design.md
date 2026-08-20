# 收益明细按产品展开

## 目标

在理财组合看板的「每日收益明细」弹层中，点开每日 / 每月 / 每年的任意一行，展开该时段内各产品收益。不改账本口径，不访问理财官网，不新开 API。

## 交互

- 入口不变：点总览「累计收益」打开现有底部弹层。`每日 / 每月 / 每年` 三个 Tab 的每一行都可以点。
- 点某行：产品列表插在该行正下方。再点同一行收起；点另一行则关掉上一行（同时只开一行）。
- 有产品明细的行，日期左侧显示小箭头，展开时旋转。没有明细的行保持现在的静态样式，点了无效果。
- 展开区只列出该时段收益不为 0 的产品，按金额从高到低。名称用现有 `shortName` 缩写。正收益橙红、负收益绿，金额带正负号和「元」。
- 各产品金额之和等于该行合计。当月已清仓、但仍计入该时段账本合计的产品也要出现。
- 切换 Tab 或关闭弹层时，展开状态清掉。总览「本月累计」仍只展示数字，不另做入口。

## 数据

沿用 `GET /api/nav-dashboard`。在现有 `daily_profits` / `monthly_profits` / `yearly_profits` 每一行上增加 `products`：

```json
{
  "date": "2026-08-19",
  "amount": "12.34",
  "products": [
    {"name": "产品甲", "code": "P1", "amount": "10.00"},
    {"name": "产品乙", "code": "P2", "amount": "2.34"}
  ]
}
```

月、年行用 `period` 代替 `date`，`products` 形状相同。金额格式与现有行 `amount` 一致（`Decimal` 转字符串）。

计算步骤：

1. 用现有 `calculate_latest_profit(..., bundle_non_trading_days=False)` 按产品、按披露日拆出每日收益，产品范围与当前 `_portfolio_daily_profit_totals` 相同（`list_products(active_only=True)`），日期截断仍为 `2026-08-01`。
2. 按自然日、自然月（`YYYY-MM`）、自然年（`YYYY`）汇总每个产品。金额恰好为 0 的产品不进该时段的 `products`。
3. 时段合计仍等于该时段所有产品每日收益之和，因此 `sum(products.amount) == row.amount`。
4. 每一行都带 `products` 数组。没有可拆明细时为 `[]`，前端不显示箭头、不可展开。
5. 页面首次加载已带上全部 `products`，切 Tab、展开都不发新请求。

本月排行（`monthly_positive_rankings` / `monthly_negative_rankings`）改为读取「当前自然月」那一行的 `products`，正负各取 TOP 3。不再把 8 月以来的累计误当成当月。

## 页面

改动只在 `src/nav_dashboard_page.html` 的收益弹层：

- 可展开行是按钮（或 `role="button"`），带 `aria-expanded`。
- 展开区缩进、字号略小于时段行，仍两列：产品名 | 金额。
- 现有关闭方式不变：关闭按钮、点遮罩、Escape。

## 安全与范围

- 不改收益账本、计提、净值抓取、企微日报/晚报/周期报告。
- 私密链接校验不变。
- 不把展开结果写进状态文件。
- 不做月份选择器、不做总览卡片上的本月收益、不把「本月累计」做成第二入口。

## 验证

- 单元测试：同一批产品日收益滚成日/月/年行后，每行 `products` 之和等于该行 `amount`；0 金额产品被省略；排序从高到低；当前月排行来自当月 `products` 而非 8 月以来累计。
- 页面测试：HTML 源码含展开/收起处理（`aria-expanded`、产品子行、同时只开一行）。空 `products` 的行不渲染为可展开控件。
- 跑现有 `tests/test_nav_dashboard.py`、`tests/test_nav_dashboard_page.py`、`tests/test_nav_dashboard_routes.py`，确认旧的合计与弹层结构仍然成立。
