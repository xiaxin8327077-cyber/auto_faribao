# 后端独立审阅：预估验证持续展示

日期：2026-10-27（北京时间）。审阅当前未提交工作区；以本次读取的文件行号为准。

范围：设计与实施计划，以及 `fund_estimate_verification.py`、`fund_estimate_store.py`、`fund_estimate_service.py`、`fund_estimate_analysis.py`、`fund_estimate_ai.py`、`portfolio_api.py`、`portfolio_market.py`、`scheduler.py`、`nav_dashboard.py`。未修改业务代码，未重复运行主代理已通过的 pytest 批次；以下探针仅在 Python 内存中运行，未连接或改动正式数据库。

## 发现

### 已解决：[P1] 正式分红恢复或冲突无法使旧的分红覆盖失效

位置：`src/fund_estimate_verification.py:74-85`，关联 `:48-71`。

`_dividend` 先接受估值库中与当前单位净值匹配的 `verified_none/verified_cash`，随后才检查正式分红缓存。若旧观测为 `verified_none=0`，后来正式缓存新增当日每份分红 0.02，结果仍是 0；正式缓存已标记冲突并返回 `None` 时同样继续采用旧的 0。单位净值未修订不代表分红口径未修订，因此旧结果继续保持 verified、计入方向与误差，违背分红修订重算及未知不当零的要求。

内存探针：当前 NAV=0.99，旧观测 `ordinary/verified_none/0`。

- 正式缓存 `{day: "0.02"}`：实际返回 `('0', 'verified_none')`。
- 正式缓存 `{day: None}`（冲突）：实际仍返回 `('0', 'verified_none')`。

应保留正式缓存“无覆盖”与“当日存在冲突”的区别；不同证据不一致时退回 pending_return，明确的新分红证据需要触发修订，不能被旧观测短路。

### 已解决：[P1] 总收益率与实际收益金额可出现相反方向并制造金额误差

位置：`src/fund_estimate_service.py:458-471`；总收益率来源为 `src/fund_estimate_verification.py:94-99`。

每日 `realized_pct` 根据已知公开每份分红计算，但金额直接采用 `calculate_latest_profit`。后者只识别正式已确认的分红流水（`portfolio_profit.py:276-279,320`）。公开分红资料已完整、正式分红流水还未处理或尚未确认时，存在有效总收益率并不足以证明金额也已采用相同分红口径。当前只检查 `item['realized_pct'] is not None`，随后还会将错误金额计入 `profit_error`。

内存探针：对应历史收益份额 1000，NAV 1→0.99，每份现金分红 0.02；预估与实际总收益率均为 +1%，正式流水仅有原持仓。

实际输出：`predicted_profit=10.00`、`realized_profit=-10.00`、`profit_error=20.00`。同一条记录的百分比误差为 0，金额误差却被制造为 20 元。

需要明确收益金额与百分比使用的同一口径及历史分红权属；正式分红尚未对账时至少将金额和金额误差留空并标明核验状态。不能以修改正式账本来绕过验证展示问题。

### 已解决：[P2] 清仓基金的既有历史不再接受修订

位置：`src/fund_estimate_service.py:153-158`，当前参与过滤在 `:69-75`。

`sync_official_verifications` 只将当前持有或待买的 `discover_funds` 传入对账。清仓后，既有 daily/legacy 结果继续被报告读取并计入累计统计，但该产品的正式净值与分红不再被读取；之后历史净值或分红修订会永久留在旧统计中。

内存探针：产品当前份额为 0、无待买，但 store 中存在历史 daily；执行 `sync_official_verifications`，`list_quotes` 的调用次数为 0，返回 `errors=[]`，历史数值保持原样。

修订范围应包含已有验证历史涉及的产品。可继续对新日期的生成范围使用当前参与规则，避免清仓基金无限产生未来记录。

### 已解决：[P2] 已有历史时，正式净值已确认但待生成的状态无法正确表达

位置：`src/fund_estimate_service.py:419-426`；总览输出在 `src/nav_dashboard.py:725` 起。

只要 `items` 非空且最近同步未记 errors，报告就返回 ready，完全不考虑官方对账线程正在处理。`today_confirmed_count` 又从已保存 daily 行计算，未使用官方已确认数量。今日第一只 NAV 已写入正式库、历史 daily 尚在重算时，接口仍会给出 yesterday + ready + 今日确认 0，页面会误提示今日净值未更新，无法显示设计要求的“当日净值已更新，正在生成验证”。

同时，总览与 `portfolio_view.py` 没有输出 `confirmed_nav_version`；前端 `refreshVerificationFromDashboard` 已读取此字段，因此无法据版本识别这类待处理更新。常规总览成功仍会调用验证读取，但请求重叠时也无法凭新版本要求补读，剩余更新依赖后续轮询。

应从正式净值生成并共享版本，区分“官方最新版本”与“daily 已处理版本”，并据此返回 updating 和官方确认数量。

## 已确认的边界

- 新每日结果以产品和净值日唯一；相同内容不更新 updated_at；旧分钟记录未删除。
- 新正式净值对账模块只读正式 portfolio；本轮未发现其写入正式持仓、交易或净值的路径。
- 验证 API 无服务或读取失败返回 503；已有最后成功报告可明确 stale 恢复。
- AI 工作线程独立于明细读取；输入采用字段白名单，份额和收益金额没有进入请求摘要；旧长文缓存版本已变更。
- 未审阅部署脚本，未触碰远端或生产数据库，本记录不代表线上部署验收。

初审结论如上；最新处理状态以以下复核记录为准。测试通过情况沿用主代理提供的证据，本审阅没有重复执行这些测试。

## 修复复核（2026-10-27）

当前结论：4 项均已解决，第二次复核结论见文末。首次复核仅复读业务代码、回归断言并执行内存探针；未重复 pytest，未修改业务代码或数据库。

- 分红证据优先级已修复：`fund_estimate_verification.py:121-135` 先判断正式事件中是否存在当日键，含冲突 None；`_cached_dividends:96-118` 保留正式状态错误、事件错误与同日重复的不可比证据。新增披露、金额修订、三类冲突回归覆盖原问题。
- 金额对账已修复：`fund_estimate_service.py:480-496` 增加 `_dividend_ledger_matches` 防护；公开分红已知但正式流水未确认时保留预估金额，实际金额与金额误差为空，并返回 `pending_dividend_ledger`。新增 API 回归覆盖原正收益率/负实际金额探针条件。
- 清仓后修订已修复：`fund_estimate_service.py:153-168` 将每日及旧分钟历史涉及的公募产品并入 identities；`fund_estimate_verification.py:189-206` 对非活动产品把生成边界限制在历史已参与日期。新增回归覆盖退出后修订及不新增退出后的日期。
- 共享版本已接通：总览 `nav_dashboard.py:769-777` 与报告使用 `read_official_nav_state` 的同一版本算法；当前日第一条新确认、已生成只数不足时，报告可返回 updating。

### 已解决（第二次复核）：[P2] 只比较今日只数会遗漏凌晨补发及数量不变的净值修订

当前位置：`src/fund_estimate_service.py:429-445`。

首次复核时，报告虽然读取了实时 `latest_confirmed_date` 与 `confirmed_nav_version`，但 status 只使用 `confirmed_today > shown_today`。当时已有历史明细时，以下两种正式版本尚未对账完成的情形仍返回 ready：

1. 凌晨补发昨日净值：正式最新日期为 2026-10-26，daily 最新日期仍为 2026-10-25。两边今日只数均为 0，报告实际返回 `status=ready, latest_available_date=2026-10-25, today_confirmed_count=0`；正式只读状态明明显示 `latest_confirmed_date=2026-10-26`。
2. 当日净值修订：今日已有一条旧 daily，正式同基金今日 NAV 已修订。两边今日只数均为 1，报告仍为 ready，并把新的 confirmed_nav_version 与旧明细一同返回。

以上已由只运行 `_build_verification_report`、无文件与数据库写入的内存探针确认。需要依据净值所属日期及已处理版本判断 updating，不能仅用北京时间“今日”的数量。若比较元数据版本，需要同时统一范围：当前 `read_official_nav_state` 在总览及报告仅使用活动基金，而 sync 元数据的版本还包含清仓历史基金，直接比较两种范围的哈希会造成持续 updating。

主代理提供整体及相关测试通过证据；审阅未重复执行。以上为第一次复核时保留的历史证据，后续修复见下文。

## 最终复核（2026-10-27，第二次）

**4 项审阅发现均已解决。本次范围内未发现仍需阻断交付的具体重要问题。**

已复读当前实现及新增断言，未运行测试：

- `fund_estimate_verification.py:218-220` 在全历史修订版本之外，独立保存 `processed_active_nav_version`，仅由活动产品的同一正式净值经济内容组成。该范围与总览、报告即时读取的活动基金版本一致，不受清仓历史产品数量影响。
- `fund_estimate_service.py:436-438` 将即时正式版本与上述已处理版本比较，版本不一致即 updating；不再依赖今日只数必须增加。因此凌晨补发昨日净值及同日数量不变的净值修订，均会在完成对账前保留明细并标记更新中。
- `test_fund_estimate_api.py:247-267` 新增回归明确覆盖“今日确认 0、版本 old→new”仍为 updating，已处理版本追平后恢复 ready。该断言覆盖上次遗留的状态条件，不要求为了覆盖此逻辑再次运行整批测试。

相关批次 **167 passed** 由主代理提供；本记录仅确认已读取的代码与断言，未独立重跑，也不代表生产部署验收。审阅只改动本审计记录，未写入正式 portfolio 或独立估值数据库。
