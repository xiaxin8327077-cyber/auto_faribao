# 正式净值每日验证持久化（Task 2）

日期：2026-10-27，北京时间。

## 实施范围

- `src/fund_estimate_store.py`：增加 `daily_verifications`，以 `product_id + trade_date` 唯一，JSON 保存完整展示条目；相同内容不更新 `updated_at`。保留原 `estimate_snapshots` 与 `verifications`。
- 新建 `src/fund_estimate_verification.py`：实现 `sync_confirmed_verifications(repository, store, identities, now)`；只读取正式组合库和本地估值数据，不联网、不训练、不调用 AI。
- 新建 `tests/test_fund_estimate_verification_continuity.py`：15 项真实正式库与独立估值库 SQLite 回归。

## 对账口径

1. 正式 `quotes` 的有效单位净值是唯一确认源。零涨幅有效；按净值所属日期归档，并按北京时间计算今日确认数量。
2. 从首次已保存估值或已有每日/分钟验证日期开始补齐；完全没有历史估值及验证时，只收录最近正式净值日。保留更早正式净值作为计算前值，不产生多年无预测明细。
3. 每日选择当日最后一条已保存的、数值有效的总收益预测。忽略缺数值、无效状态、其他目标及未来生成的快照；不补造预测。
4. 缺预测仍保存实际净值。缺前值或分红覆盖时保存 `nav_return_pct`（有前值时）并设置 `realized_pct=None`、`verification_status=pending_return`，不生成误差及方向判断。
5. 正式当日分红事件优先，明确冲突（包括 None）撤销旧可比较状态；没有当日事件时，才使用与正式单位净值匹配的 `nav_observations` 完整覆盖。缓存没有覆盖截止日期，空缓存不能推断零分红。
6. 正式单位净值修订后重新计算依赖它的后继日期。当前日期单位净值修订后，原来不匹配的分红覆盖不再采信；预测值继续来自原始快照。
   `base_nav/base_nav_date` 保存正式收益前值，`snapshot_base_nav/snapshot_base_nav_date` 独立保存原预测基准，避免正式前值修订后改写原预测金额。
7. `confirmed_nav_version` 按正式产品、净值日期及规范化单位净值生成；重复抓取、净值小数格式或原始抓取哈希变化不改变经济版本。每日 `source_version` 包含前值与分红依赖。
8. 单只基金错误进入返回的 `errors`，其他基金继续对账；原每日历史保留。返回元数据含 `confirmed_nav_version`、`today_confirmed_count`、`latest_available_date`。

## 回归证据

- 实现前执行新回归：12 项全部因缺少正式净值对账模块而明确断言失败。
- 新增旧分钟验证边界回归后，确认原实现仅回补最后一日而失败；加入旧验证边界后通过。
- 新回归捕获同一秒内分红覆盖恢复排序不确定的问题：`nav_as_of` 显式按插入 ID 排序，时间相同的经济修订采用后写入版本。
- `python -m pytest tests/test_fund_estimate_verification_continuity.py tests/test_fund_estimate_store.py -q`：**17 passed in 1.69s**。
- 合跑原 `test_fund_estimate_corrections.py` 时，服务集成代理正在增加对尚未创建的 `fund_estimate_analysis` 的导入，导致临时 `ModuleNotFoundError`；未修改其他代理文件规避问题。待异步分析模块落盘后复跑并补充结果。
- 异步分析模块落盘后复跑上述三文件：**35 passed / 3 failed**。三个失败均为旧服务 AI 同步或长文案断言（关闭 AI 时返回 None、首个读取即同步得到 AI、提示词要求四个自然段），已交由集成代理按新需求调整；新 15 项、原 store 2 项及原修订逻辑均通过。

## 集成只读审阅

已向集成代理报告：金额输入字典中每日条目覆盖旧快照导致原预测基准丢失；事件在工作线程忙或五秒节流期间可能被丢弃；部分基金同步错误未反映报告状态；重启后空响应可能覆盖持久成功缓存；产品发现阶段的单只异常会阻断整体对账。此节记录审阅发现，不代表上述问题已全部修复。

## 最终审阅修复

新增正式分红后来披露、修订、冲突及清仓历史修订回归；提供 `read_official_nav_state` 和 `official_nav_version`。清仓产品仅重算既有日期，不生成清仓后新日期。原预测基准独立保留；同一秒覆盖以最终入库版本为准。

相关每日对账与原 store 回归最终 **25 passed**。集成阶段旧 AI 同步断言已按异步短评更新；后续 API、修订和页面批次均通过。前述集成审阅发现已修复，见最终后端审阅记录。生产部署证据单独记录。

## 全部完成后的刷新冻结

追加 `read_official_nav_state` 元数据：`latest_confirmed_by_product` 保存各持仓基金最新正式净值日，`expected_fund_count` 保存传入持仓数量。

新增纯计算函数 `verification_refresh_state`。按当前正式净值最大日期判断完成：所有 active 产品均确认同一目标日，且该日明细全部为 `verified` 或 `missing_estimate`、实际收益有效、没有待分红入账状态；报告必须 `ready`、正式状态无错误、版本无待处理，分析状态为已结束的 `ready/disabled/failed`。满足条件后冻结到目标净值日之后首个交易日北京时间 15:00。跨午夜继续冻结；到达该时刻即恢复轮询，旧结果仍可展示。没有 active 产品、部分确认、分析生成中、错误、过期、版本待处理或分红未入账时均不冻结。

下一交易日完全依赖服务传入的交易日历，不使用工作日兜底。日历异常、返回未知值或在有限搜索范围内找不到交易日时保持轮询。

本次追加按根代理最新开发约束，仅实施并做静态/逻辑审阅，**未新增或运行自动化测试**；前文 25 项通过记录属于前一轮分红与清仓修复，不作为本次冻结逻辑的执行证据。服务/API 与页面冻结接入由其他代理负责。
