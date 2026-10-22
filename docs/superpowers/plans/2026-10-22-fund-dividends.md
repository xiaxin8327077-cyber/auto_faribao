# 公募基金自动分红 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. 用户已逐项明确功能和UI，直接按当前目录执行，不创建隔离分支或worktree。

**Goal:** 自动处理公募基金现金/再投资分红，并修复015736本次错误校准和到账。

**Architecture:** 独立分红事件服务连接公开数据、交易账本、组合周期及面板；存量API和同日分红兼容，事件按稳定身份防重。

**Tech Stack:** Python/Flask/SQLite/Decimal，原生HTML/CSS/JavaScript。

**Spec:** docs/superpowers/specs/2026-10-22-fund-dividends-design.md

## Global Constraints

- 不创建分支/worktree；保留既有未跟踪文件；不改配置/密钥。
- 现金默认入钱包Plus；仅public_fund有分红设置和按钮。
- 每笔分红收益和资产仅计算一次；未核实0.11元差异保留。

## Review Focus

- 登记日当日申购/赎回与未来确认份额的权益边界。
- 除息后至派现前的应收资产，以及收益/总资产不重复。
- 已处理事件/历史人工校准/模式更改及调度重启的幂等性。
- 不完整公告、未知再投确认规则、净值迟到及来源冲突的等待状态。
- UI鉴权、写失败恢复、只有基金显示和移动端红点/列表。

### Task 1: 分红财务服务及周期

**Files:** src/portfolio_dividends.py, src/portfolio_db.py, src/portfolio_transactions.py, src/portfolio_profit.py, src/portfolio_reconciliation.py, src/portfolio_jobs.py, src/portfolio_view.py, 分红数据provider及相应tests。

**Interfaces:** 实现spec中的FundDividendService/dividend_records及payload字段；record_cash_dividend增加可选payment_date。

- [x] 先编写并运行真实账本行为失败测试：跨日现金、再投、登记权益、应收、重试、模式锁定和错误等待。
- [x] 最小实现，可靠抓取公开分红日期与方案，保存事件/快照，接组合周期及收益/资产。
- [x] 运行相关测试并报告红绿证据，独立复核无财务阻断。

### Task 2: 设置API与分红面板

**Files:** src/portfolio_api.py, src/nav_dashboard_page.html, tests/test_portfolio_api.py及必要页面测试。

**Interfaces:** 使用spec规定mode endpoint与records endpoint；兼容后端新增产品字段。

- [x] 先验证非法产品/模式、鉴权和设置持久化的失败测试。
- [x] 接口仅public_fund可写，合并metadata、保存幂等和审计；GET只读。
- [x] 基金卡片详情后增加分红按钮/红点，面板设置模式并显示完成/未完成记录，移动端核验。

### Task 3: 生产修账与交付

**Files:** runtime/fund-dividend-repair-20261022/操作脚本与证据、audit/20261022-fund-dividend-repair.md。

- [x] 在现有生产一致副本预演冲销旧校准并处理本次真实821.94分红；现金41870.58，基金原始份额不动。
- [x] 全量pytest、编译、diff检查、独立代码审查与生产快照回放。1102 passed；两项旧定投测试改用固定起始日期现金夹具，消除机器当前日期依赖，业务断言不变。
- [ ] 生产数据先备份，在一个事务内按相同计划执行，校验并重跑证明防重复。
- [ ] 核验真实HTTP payload、日期收益、分红记录、模式选择和红点，提供结果与剩余差异。
