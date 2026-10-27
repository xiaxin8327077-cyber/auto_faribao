# Fund Verification Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 总览一只基金正式净值确认即可显示每日验证，刷新和跨日保留结果，并提供动画和精简异步点评。

**Architecture:** 正式净值触发本地幂等对账，将每日结果存入独立估值库；报告只读持久化明细及异步分析缓存。前端独立请求并保留最后成功内容。

**Tech Stack:** Python / Flask / SQLite / 原生 JavaScript / pytest / Node。

## Global Constraints

- 每基金每净值日一条每日结果；未知分红不可当零，不补造历史预测。
- 今日没有数据显示上一有效日；首只确认即可显示今日；手动历史选择保留。
- 加载使用骨架扫光与旋转圆环；失败不清空已有内容。
- AI 最多4条短句、约120–180字，独立后台生成，不阻塞明细。
- 保留现有未跟踪文件；仅提交本次交付。用户已授权提交、推送和部署。

## Task 1: 前端状态与动画

**Files:** `src/nav_dashboard_page.html`, `tests/test_nav_dashboard_page.py`。

**Interfaces:** 消费原报告及 `status`, `display_date`, `latest_available_date`, `today_confirmed_count`, `ai_status`, `data_version`。

- [ ] 新增有行为断言的回归：首次加载、失败保留、默认自动前进日期、手选日期保留、行情失败不阻断验证。
- [ ] 实现骨架扫光、旋转圆环、重试与超时；合并重叠请求；初始化和卡片可见时独立加载验证。
- [ ] 展示当日逐只确认和上一有效日提示；AI 单独生成状态；保持现有金额与累计统计布局。
- [ ] 执行相关页面测试并记录结果。

## Task 2: 正式净值本地对账及持久化

**Files:** `src/fund_estimate_store.py`, 新建 `src/fund_estimate_verification.py`, 新建 `tests/test_fund_estimate_verification_continuity.py`。

**Interfaces:** `sync_confirmed_verifications(repository, store, identities, now) -> dict`，元数据含 `confirmed_nav_version`, `today_confirmed_count`, `latest_available_date`。store 提供 `save_daily_verification(item)`, `read_daily_verifications()`。

- [ ] 用正式组合库和独立估值库写回归：首只/第二只、0涨幅、重复/重启、缺预估、分红未知、净值修订。
- [ ] 新增每日结果表，使用基金与净值日唯一键和 JSON 负载；保留原分钟记录。
- [ ] 从正式 quotes 逐只对账；选择已保存的当日最后有效预测；分红不完整仍保存正式净值确认明细，完整后才计算可比较总收益。
- [ ] 每个基金隔离失败；相同经济内容不改版本；重算依赖修订。
- [ ] 执行新测试及原 store/修订测试。

## Task 3: 精简非阻塞 AI

**Files:** 新建 `src/fund_estimate_analysis.py`, `src/fund_estimate_ai.py`, 新建 `tests/test_fund_estimate_analysis.py`。

**Interfaces:** `VerificationAnalysis(store, ai).read(items, stats, overall) -> dict` 返回 `ai_analysis`, `ai_status`；首次 read 启动有锁后台线程后立即返回，后续读取同版本持久缓存。每服务实例复用一个分析器。

- [ ] 覆盖慢 AI 不阻塞、重复请求单次生成、缓存恢复、超长压缩/失败摘要、无金额进请求。
- [ ] 新缓存版本；短评最多4条，目标120–180字；超长一次压缩，仍无效则确定性四维摘要。
- [ ] EstimateAi.complete 增加可选 max_tokens，默认保持报告提取原预算；短评单独收紧。
- [ ] 后台失败设置有限重试时间，避免每次读取启动请求；旧数据点评不混入新版本。
- [ ] 执行专门测试及已有 AI 测试。

## Task 4: 服务、接口和调度集成

**Files:** `src/fund_estimate_service.py`, `src/portfolio_api.py`, `src/scheduler.py`，必要的正式净值入口。

- [ ] 服务增加 `sync_official_verifications(now)`；调度每分钟本地补偿，正式净值同步后触发；读取报告前只启动非阻塞补偿。
- [ ] 合并旧历史与每日结果，继续每日去重；单基金金额失败只降级该字段。
- [ ] 使用持久化最后成功报告，异常明确 stale 或 HTTP503；不返回伪成功空数组。
- [ ] 集成异步分析器与日期/版本元数据；总览版本变化可触发验证。
- [ ] 验证相关 service/API/jobs 及页面整合回归。

## Task 5: 审阅与交付

- [ ] 独立审阅需求符合性、并发与收益口径；修复重要发现。
- [ ] 执行相关回归、源码与前端脚本检查、Git空白检查。
- [ ] 精确暂存本次文件，提交并推送 origin/master。
- [ ] 查阅现有部署方式，备份服务代码及估值库；部署与必要重启。
- [ ] 线上核对鉴权、历史回显、首只基金确认、错误状态、短评缓存与服务状态；记录可回滚证据。
