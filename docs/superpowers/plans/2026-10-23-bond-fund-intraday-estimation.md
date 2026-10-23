# 通用债券基金盘中涨跌幅预估 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task when available. 本项目用户指令优先：不得擅自创建分支/worktree；这份文件是交给其他开发者的实施方案，不在本次会话执行。

**Goal:** 对当前及以后新买入的支持范围内公募债基，自动补数据、更新估值输入，提供独立于正式账本的盘中涨跌幅参考；新增基金代码不需要修改程序。

**Architecture:** 持仓动态发现 → 历史净值/分红与行情归档 → 报告画像与可选AI提取 → 久期基线和指数加权Ridge → 盘中预测及次日验证。估值状态写独立SQLite库，页面只读快照；后台采集、训练与报告提取均不阻塞现有交易、分红、推送及分钟晴雨表。Kalman作为后续竞争模型，依据时序样本外结果决定是否采用。

**Tech Stack:** 现有Python/Flask/SQLite/requests/Decimal；新增numpy、scikit-learn时锁定兼容部署环境的版本。statsmodels只在第二阶段需要时添加。PDF优先文本解析；必要时复用现有AI提供方和客户端，禁止把模型凭据写入源码。

**Spec:** `docs/bond-fund-intraday-estimation-assessment-20261023.md`。其中四只已购基金和中报数值只是研究样本，不能变成运行时白名单或固定参数表。

## Global Constraints

- 工作区当前为`D:\cherry\auto_faribao`，当前分支`master`；遵循实际工作区AGENTS.md，保留既有改动。未经用户明确指示，不新建分支、worktree、隔离检出目录。
- 2026-10-23已上线南方晴雨表、四类收蛋区间和30天快照；复用`src/southern_bond_market.py`与`src/bond_barometer.py`，保留现有功能。
- 当前部署基线提交`8a1ab26aeccef3a1d87ca92db635b5fb7fcc781a`，本次上线包含已授权的未提交源码，部署状态`source_dirty=true`；应按部署清单和文件SHA256判断真实版本，不能只看提交或git status。
- 不覆盖`quotes.unit_nav`、正式市值/收益/交易/分红记录；不触发模拟成交，不将预测输入现有结算流程。
- 金额与对外百分数使用Decimal/十进制字符串；数值训练矩阵允许有限浮点数，但写出时验证有限值、单位与精度。1bp收益率变化=0.01个百分点；1蛋=基金收益率0.01%。
- 全部时间携带北京时间或可转换的时区；分别记录`report_as_of`、`published_at`、`fetched_at`、`market_as_of`、`trained_through`，不把抓取时间当数据日期。
- 自动发现支持范围内的新基金，不按代码写if/elif；历史不足仍返回明确状态及同类市场参考。
- 不创建聊天自动化替代程序后台任务；不自动部署本模块。用户允许需要时接入AI，但不是要求必须购买新服务。
- 原有未跟踪`.zcode/`、`=`、`tests/test_portfolio_financial_ledger.py`不属于本任务，不删除或夹带提交。

## Review Focus

1. 新买入但份额尚未确认：自动进入数据准备流程；取消/失败买单不继续触发建模，清仓无在途基金退出活跃刷新。
2. A/C/D等同组合不同份额：组合关联必须有证据，净值、费率和训练标签不得串用。
3. 报告发布晚于持仓日期、净值后来修订：训练只能看到当时已公开版本，旧回测不得读取未来资料。
4. 历史日终曲线和南方盘中活跃券BP：不假装同口径；缺失、源切换、日期错配要降级，不能补零伪造有效输入。
5. 并发刷新、源时间回退、AI提取失败及外部服务超时：保留较新有效版本，单基金失败不拖垮全局任务。

## 1. 产品口径和首版边界

预估对象是“上一有效净值日收盘至当前行情时点的本交易日净值总收益参考”，不是对余下交易时段行情的保证。首版以人民币开放式纯债/中短债为主要模型范围；含明显股票、转债、外汇、海外交易日风险或类型不明的产品返回`unsupported`或`reference`及原因，不硬套纯债模型。ETF实时交易价与基金NAV是不同对象。

新增基金可以是已有多年历史但刚买入，也可能刚成立：前者自动回补并训练；后者先用同类型市场参考，不伪造单基金精确值。历史久期仅作基线或约束，模型日更的有效敞口不能宣传为真实持仓日更。

默认参数是本方案的工程选择，集中配置，不代表已验证精度：

| 配置 | 初始值/行为 |
| --- | --- |
| `enabled` | 可配置；测试/迁移默认不启动网络任务，运行环境明确启用 |
| `display_mode` | `reference`或`experimental`；达成验证门槛后才可显示`ready` |
| 发现持仓/计算盘中快照 | 每60秒检查；新增符合条件基金在一个调度周期内入队 |
| 正式净值 | 优先消费现有同步结果；需要补数时18:00至次日02:00每30分钟检查，成功获得新净值后触发一次训练 |
| 报告/费率 | 每日07:30全量检查；交易日08:00至23:00每60分钟增量检查；按内容指纹避免重复解析 |
| 训练窗口候选 | 60/120/250个有效交易日；指数权重半衰期30个交易日，均可配置 |
| 最低训练/验证样本 | 拟合至少60个重叠收益样本；至少20个真正样本外残差才允许标注“经验区间” |
| 行情陈旧 | 默认5分钟，仅在该源应发布时段判定；午休、节假日、停盘另按交易日历处理 |
| 历史保存 | 盘中原快照继续30天；独立日频训练集、报告/模型版本、预测验证记录至少保留3年 |

法定工作日历不自动等于债市交易日历；调休周末、节假日前后及跨午夜净值轮询必须有测试。未知公告发布日期的资料不能进入无前视回测，不能用“抓取到了”倒填历史。

## 2. 已有代码接点与新增模块

| 文件 | 本次后续实现职责 |
| --- | --- |
| `src/portfolio_models.py` | 读取`Product`身份；不把预测塞进`MarketQuote` |
| `src/portfolio_repository.py` | 读取产品、持仓及待确认买入；不改变正式账本写接口 |
| `src/portfolio_providers.py` | 复用`EastmoneyFundProvider.resolve_product/fetch_quotes`能力，注意当前净值接口每次只取第1页100条，历史回补必须分页或分日期窗口并验证完整性 |
| `src/portfolio_dividend_provider.py` | 复用公开分红原始事件；用户账本中的“是否纳入历史分红”不等于基金训练序列的分红覆盖 |
| `src/southern_bond_market.py`、`src/bond_barometer.py` | 盘中行情、单位、源时间、快照；不给单基金直接照搬四类蛋数 |
| `src/longcat_client.py` | `LongCatSettings.from_env`、`LongCatClient.complete`；已有客户端返回文本，仍需新增结构和证据校验 |
| **新增**`src/fund_estimate_types.py` | 状态、版本化数据契约 |
| **新增**`src/fund_estimate_store.py` | 独立`data/fund_estimation.db`、幂等写入和版本选择；不改正式账本SCHEMA_VERSION=7 |
| **新增**`src/fund_estimate_sources.py` | 净值/分红/日终曲线回补，固定因子定义及来源映射 |
| **新增**`src/fund_estimate_profiles.py` | 动态报告发现、文本/可选AI提取、字段证据与有效期 |
| **新增**`src/fund_estimate_models.py` | 久期基线、加权Ridge、时序回测、区间校准 |
| **新增**`src/fund_estimate_service.py` | 持仓发现、后台队列、快照读取和任务调度 |
| `main.py`、`src/server.py`、`src/scheduler.py` | 可选依赖注入；构建服务不联网，调度器仅投递后台工作 |
| `src/portfolio_api.py`、`src/nav_dashboard.py`、`src/nav_dashboard_page.html` | 带现有鉴权的只读估值API及持仓卡片独立参考；不改已有实际收益口径 |

独立估值库用产品ID/基金代码关联正式库，但不跨库假设外键事务。产品删除/类型变化后停用相关活跃任务，历史版本保留；估值库损坏或未初始化时看板和交易仍可工作。

## 3. 数据契约

以下名称为后续实现约定，当前尚不存在；统一由`fund_estimate_types.py`定义TypedDict或dataclass，其他模块不得各写一套不同字段。

```text
EstimateStatus = Literal['pending', 'reference', 'experimental', 'ready', 'stale', 'unsupported', 'error']

# 所有数据结构均可序列化；日期ISO，时刻带时区；Decimal对外转十进制字符串。
FundIdentity = {product_id, code, provider, share_class, portfolio_group_id, fund_type, currency}
ProfileVersion = {profile_version, identity, report_as_of, published_at, fetched_at,
                  source_url, document_hash, fields, evidence, status}
NavObservation = {code, nav_date, unit_nav, cash_dividend_per_share,
                  available_at, source, source_version, event_status}
FactorObservation = {factor_set_id, source_family, as_of, published_at, fetched_at,
                     market_as_of, values_bp, calendar_days, basis_status, missing_fields}
TrainingSet = {identity, cutoff_at, rows, feature_names, source_versions, profile_version}
ModelVersion = {model_version, identity, model_kind, trained_through, available_at,
                feature_names, feature_units, coefficients, preprocessing,
                source_versions, profile_version, validation, artifact_hash}
EstimateSnapshot = {product_id, code, trade_date, target, status, reason_codes,
                    base_nav_date, base_nav, market_as_of, profile_as_of,
                    report_published_at, trained_through, model_version,
                    return_pct, lower_pct, upper_pct, interval_kind,
                    confidence_label, market_reference, generated_at}
```

`target='mark_to_market_total_return'`。`return_pct='0.03'`表示+0.03%，不是0.0003%；`base_nav='1.2345'`是正式净值，仅供计算基准。`interval_kind`取`scenario`、`empirical`或`none`；未经校准的情景上下界不能标成90%置信区间。`reference`时单基金数值可为null，`market_reference`明确保留同类参考的源时间及口径。

```json
{
  "product_id": "动态产品ID",
  "code": "动态六位代码",
  "trade_date": "2026-10-23",
  "target": "mark_to_market_total_return",
  "status": "pending",
  "reason_codes": ["history_backfill_pending"],
  "base_nav_date": null, "base_nav": null,
  "market_as_of": null, "profile_as_of": null,
  "report_published_at": null, "trained_through": null, "model_version": null,
  "return_pct": null, "lower_pct": null, "upper_pct": null,
  "interval_kind": "none", "confidence_label": "数据准备中",
  "market_reference": null, "generated_at": "2026-10-23T10:00:00+08:00"
}
```

状态原因至少包括：`history_backfill_pending`、`insufficient_history`、`profile_unavailable`、`market_missing`、`market_stale`、`nav_delayed`、`basis_unverified`、`validation_insufficient`、`unsupported_asset_mix`、`ai_extraction_failed`。AI不可用时不阻断已有数值模型；缺少适用模型才降为市场参考。

估值库至少存：原始资料与校验和、字段画像版本、净值/分红版本、日频因素、模型版本及评估指标、预测与后验真值、幂等任务状态。唯一键包含代码/份额、日期/时点、来源/版本；同日修订可追踪，禁止覆盖历史回测使用的旧版本。

## 4. 模型与自动更新规则

### 4.1 预测标签与固定因子

普通现金分红情形的训练标签：`r_pct = 100 * ((NAV_t + dividend_per_share_t) / NAV_prev - 1)`。分红缺失不当作0；拆分、转型等特殊份额事件先标记不可训练，待规则明确。不能用累计净值的简单百分比变化替代总收益。

必须区分“完整核验且当日无分红”和“分红数据未取得”：前者每份分红为0，后者为未知。盘中BP通常以昨日收盘为基准，基金`base_nav_date`必须与该基准交易日对齐；若正式净值延迟跨越多个交易日，首版返回`nav_delayed`，不把单日BP套在更早净值上。自然日应计与盘中行情分别处理，票息采用整日还是截至时点的应计须固定、注明，并在验证时使用同一规则。

初版固定低维因子集`bond_factor_v1`，每列以同一来源家庭的前一交易日收盘为基准：

- `gov_3y_bp`：3年国债收益率变化。
- `gov_slope_10y_3y_bp`：10年变化减3年变化。
- `cdb_spread_5y_bp`：5年国开变化减5年国债变化。
- `credit_aaa_spread_3y_bp`：3年AAA中短票变化减3年国债变化。
- `calendar_days`：相邻正式净值日间自然日数，用于票息/费用应计项。

缺所需点不临时换成别的期限再沿用原模型。因子族变更生成新`factor_set_id`。历史中债/中国货币网日终数据与南方盘中BP必须分来源版本保存，核验定义、昨日基准及重叠期映射；未验证时只能给`experimental/reference`。不能用今天首次保存的南方快照伪造过去分钟历史。

### 4.2 基线、Ridge与升级条件

久期基线：`r_pct ≈ -D_nav * delta_y_bp / 100 + carry_pct - fees_pct - financing_pct`。使用敏感度推导的净值口径久期时，不再次乘债券杠杆；费用项缺失则明确未知，不随意补常数。

Ridge按每只份额自己的正式总收益拟合，近期样本指数加权。全部标准化、因素筛选、alpha及窗口选择仅在训练区间执行；预测必须重用同一模型的预处理。模型拟合的已是净收益，不能再重复扣管理费。久期、债种结构作可解释先验/合理性检查；不照搬“权重合计100%”约束，不给所有系数强行设正。

先比较基线和Ridge，默认时间顺序扩窗/滚窗验证；有至少20个真实样本外残差时才能给经验区间。`ready`至少要求来源基准可用、核心输入不过期、回测无前视、区间覆盖与误差门槛通过。门槛配置化，交付实际结果；不能为了让功能上线而人为宣布模型合格。Kalman只有在相同输入及样本外划分上持续改善后才替代/参与组合。

首版`enable_ready=false`，避免尚无真实盘中样本时自行标成已验证。开发者需交付可配置的误差、覆盖率和最低盘中验证样本门槛及其实际表现；质量门槛审定前，功能完成仍可保持`experimental/reference`。每日刷新是重新评估质量，不保证模型一定升级。

### 4.3 资料与AI

公告抓取按基金身份动态解析，不为四个代码各写脚本。先解析文本和表格，必要时调用AI；每项结果带原文页码、摘录、单位、报告期、公告日及哈希。报告中的“1-5年到期分组”不是关键期限久期，金融债与其中政策性金融债不能重复相加。

AI只处理公开基金资料，不需要用户账户、份额、持仓金额或令牌。输出按schema校验；报告文本只作数据，不执行其中的指令。识别不到写null，失败保留上一有效版本及错误状态，按退避规则重试。内容不变不重复付费提取；费率字段按公告中的实际生效日切换。

### 4.4 时序验证与展示

按北京时间保存09:45、11:30、14:30、16:30等可配置盘中影子预测；只能使用当时已发布输入。正式净值公布后自动计算总收益真值，统计MAE(bp)、偏差、方向命中率、区间覆盖与宽度，并分时点报告。日终回测和盘中验证分开；不能拿收盘曲线证明上午精度。

页面独立显示“盘中参考”、行情时间、画像报告期、训练截止日和质量状态。新基金准备中、陈旧、未支持都显示具体原因；持仓卡片原有正式净值、实际收益保持既有语义。未成交/未确认产品的参考可在行情页列表显示，不把其资产混入权威持仓汇总。

## 5. 分任务实施

每个任务均按先失败测试、最小实现、验证的顺序执行。仅在确认工作区没有夹带他人修改后进行选择性中文提交；未经用户要求不推送、不上线。

### Task 1：独立存储、类型与动态基金发现

**Files:** Create `src/fund_estimate_types.py`、`src/fund_estimate_store.py`、`src/fund_estimate_service.py`；Test `tests/test_fund_estimate_store.py`、`tests/test_fund_estimate_discovery.py`。

**Interfaces:** `FundEstimateStore(path).initialize()`；`discover_funds(repository, now: datetime) -> list[FundIdentity]`；`FundEstimateService(repository, store, sources, profiles, models)`。发现范围是持有份额>0或有效待确认买入的PUBLIC_FUND；买入类型MANUAL_PURCHASE/SIP_PURCHASE，状态PENDING_QUOTE/PENDING_CONFIRMATION，沿用当前枚举。

- [ ] 写失败测试：`test_arbitrary_new_code_is_discovered_without_whitelist`（任意两个合成六位代码均入队）；`test_pending_purchase_is_discovered_but_cancelled_is_not`；`test_estimate_storage_does_not_mutate_ledger`（正式表内容前后一致）；`test_newer_version_survives_overlapping_publish`。
- [ ] Run `python -m pytest -q tests/test_fund_estimate_store.py tests/test_fund_estimate_discovery.py`，确认以上行为缺失而失败。
- [ ] 实现独立库版本/唯一键/短事务、动态发现和可注入时钟；报告无变化不重复版本，失败状态不抹掉成功版本。
- [ ] 同命令全部通过；验证只用临时数据库。选择性提交，中文说明，例如`feat: 新增基金估值独立存储与动态发现`。

### Task 2：历史净值、分红与因素归档

**Files:** Create `src/fund_estimate_sources.py`；Test `tests/test_fund_estimate_sources.py`。

**Interfaces:** `EstimateSources.fetch_nav_history(identity, start: date, end: date) -> list[NavObservation]`；同类方法`fetch_daily_factors(start, end, source_family) -> list[FactorObservation]`、`read_intraday_factors(now) -> FactorObservation`。纯函数`total_return_pct(nav_prev, nav_t, cash_dividend_per_share) -> str`定义在本模块，入参为十进制字符串，仅接受分红覆盖已核验的普通份额事件。仅写估值库历史，不调用正式QuoteSyncService写预测。

- [ ] 写失败测试：`test_nav_backfill_exceeds_first_hundred_rows`（185条、分页/日期窗口去重且覆盖完整）；`test_cash_dividend_total_return`；`test_missing_dividend_is_not_silently_zero`；`test_cross_family_factors_require_basis_status`；`test_later_nav_revision_does_not_leak_into_old_cutoff`。

```python
# 价格除息下降并不代表总收益为负：1.02 + 0.01 相对1.03，总收益为0。
assert total_return_pct('1.03', '1.02', '0.01') == '0'
```

- [ ] Run `python -m pytest -q tests/test_fund_estimate_sources.py`并确认失败，再实现有限重试/限流/版本归档/固定因子定义及单位校验。
- [ ] 同命令通过；离线fixture覆盖节假日、跨年、重复记录、缺页、份额事件及NaN；公开网络少量烟测作为单独命令，不让单测依赖外网。选择性提交。

### Task 3：动态报告画像、自动刷新与可选AI

**Files:** Create `src/fund_estimate_profiles.py`、`docs/prompts/fund-risk-profile-extraction.md`；Test `tests/test_fund_estimate_profiles.py`。

**Interfaces:** `ProfileService.discover_documents(identity, since: datetime | None) -> list[dict]`；`refresh(identity, now) -> ProfileVersion`；`get_as_of(identity, cutoff_at) -> ProfileVersion | None`。`evidence`包含字段键、页码、原文、数值单位；同组合份额关联必须可追溯。

- [ ] 写失败测试：`test_new_generic_fund_discovers_report`；`test_report_is_visible_only_after_publication`；`test_share_class_fee_does_not_leak_to_sibling`；`test_fee_uses_effective_date`；`test_ai_invalid_json_or_missing_evidence_keeps_last_good_profile`；`test_unchanged_document_is_not_sent_to_ai_again`。
- [ ] Run `python -m pytest -q tests/test_fund_estimate_profiles.py`，确认失败后实现通用公告发现、文本优先提取、可选AI、证据核验和定时刷新水位。
- [ ] 记录`last_checked_at/next_check_at/error/retry_at`；自动任务发现新公告后一个成功轮询周期内生成新画像；失败可重试且不抹旧版本。
- [ ] 同命令通过，模拟AI关闭/超时及恶意报告指令，不发送真实密钥或用户资产信息。选择性提交。

### Task 4：基线、Ridge与无前视回测

**Files:** Create `src/fund_estimate_models.py`；Test `tests/test_fund_estimate_models.py`；按需修改`requirements.txt`。

**Interfaces:** `build_training_set(identity, cutoff_at, store) -> TrainingSet`；`fit_baseline(training, profile) -> ModelVersion`；`fit_ridge(training, config) -> ModelVersion`；`walk_forward_validate(training, config) -> dict`；`predict(model, factors, base_nav: NavObservation, profile: ProfileVersion | None, now) -> EstimateSnapshot`。纯函数`duration_price_return_pct(duration_nav, delta_y_bp) -> str`定义在本模块，入参为十进制字符串。训练集构造使用Task 2/3的时点版本，模型载入须校验特征名称、顺序和单位。

```python
# 收益率上行1bp，净值口径久期3年的单独价格贡献为-0.03%。
assert duration_price_return_pct('3', '1') == '-0.03'
```

- [ ] 写失败测试：`test_synthetic_duration_sign_and_scale`；`test_preprocessing_fits_training_only`；`test_post_cutoff_report_is_not_used`；`test_missing_or_reordered_factor_is_rejected`；`test_delayed_nav_is_not_paired_with_single_day_bp`；`test_trained_net_return_does_not_deduct_fees_twice`；`test_uncalibrated_interval_is_not_labelled_empirical`；`test_insufficient_history_returns_reference`。
- [ ] Run `python -m pytest -q tests/test_fund_estimate_models.py`，确认失败后实现小维度基线/加权Ridge及时间顺序验证，参数与假设集中配置。
- [ ] 用合成已知敞口检查方向和数量级，再生成四只现有基金的真实覆盖/回测报告；抓取不到的时间段必须列为缺口，不能编造数据或误差。第一阶段不同时引入Kalman/深度模型。
- [ ] 同命令通过，序列化/重启加载结果一致；选择性提交。模型质量不足时保留reference/experimental状态，不伪装ready。

### Task 5：后台调度、冷启动、自动学习与验证任务

**Files:** Extend `src/fund_estimate_service.py`；Modify `main.py`、`src/scheduler.py`、`src/server.py`；Test `tests/test_fund_estimate_jobs.py`。

**Interfaces:** `create_fund_estimate_service(portfolio_runtime, config) -> FundEstimateService | None`，只构建不联网；`service.tick(now) -> None`只幂等入队；`service.read_snapshots(product_ids, now) -> dict[str, EstimateSnapshot]`只读。`scheduler.start(cfg, portfolio_runtime=None, estimate_service=None)`、`server.create_app(..., estimate_service=None)`增加可选参数，保留旧调用兼容；`main.py`构建一次后注入两处。

- [ ] 写失败测试：`test_new_purchase_is_queued_within_next_tick`；`test_new_nav_schedules_one_training_job`；`test_model_refit_follows_new_report_without_code_change`；`test_trade_window_holiday_and_cross_midnight_nav_poll`；`test_slow_ai_does_not_block_barometer_or_portfolio_cycle`；`test_restart_resumes_jobs_without_duplicate_work`。
- [ ] Run `python -m pytest -q tests/test_fund_estimate_jobs.py`并确认失败，随后实现持久化幂等键、单基金互斥、网络锁外执行、退避/重试和调度水位。不要每分钟全量重拉全部历史或重读同一PDF。
- [ ] 每只基金有独立失败状态；已清仓且无在途产品停止高频任务，保留历史。净值次日到达后给已存预测追加验证真值，而不是重算并覆盖昨天的预测。
- [ ] 同命令通过；关闭模块/损坏估值库不影响正式系统启动及现有任务。选择性提交。

### Task 6：只读API、页面和交付验收

**Files:** Modify `src/portfolio_api.py`、`src/nav_dashboard.py`、`src/nav_dashboard_page.html`、`src/server.py`；Test `tests/test_fund_estimate_api.py`、`tests/test_nav_dashboard_page.py`、`tests/test_fund_estimate_end_to_end.py`。

**Interfaces:** `create_portfolio_blueprint(runtime, provider_factory, estimate_service=None)`兼容现有调用；新增`GET /api/portfolio/fund-estimates`复用现有`read_guard`，返回`{generated_at, items:[EstimateSnapshot]}`。主看板可附加`fund_estimates`映射，页面按product_id合并独立参考，不改变原正式字段；GET不启动采集/训练/AI。

- [ ] 写失败测试：`test_estimate_api_requires_dashboard_key`；`test_get_only_reads_cached_estimates`；`test_estimate_fields_do_not_change_confirmed_profit`；`test_new_fund_cold_start_to_prediction_without_code_edit`；`test_stale_or_unsupported_state_is_visible_and_escaped`；`test_cleared_fund_is_not_reintroduced_into_holdings_totals`。
- [ ] Run `python -m pytest -q tests/test_fund_estimate_api.py tests/test_fund_estimate_end_to_end.py tests/test_nav_dashboard_page.py`，确认失败后实现只读映射与移动端展示，保留当前页面风格、晴雨表和收蛋预估。
- [ ] 验证390px及桌面宽度、错误状态、来源/时点、样本不足和新增任意基金流程。检查正式账本数据前后完全一致。
- [ ] Run `python -X utf8 -m pytest -q`、源码编译、`git -c core.whitespace=cr-at-eol diff --check`；修复本次问题，并按名称报告任何历史失败。选择性提交，不自动部署。

## 6. 最终交接验收与交付物

- 从两个未在源码出现的合成/新基金代码开始，验证动态发现→补数→画像→基线/训练→快照→页面；资料不足也完成状态闭环，不能只让四只样本走通。
- 交付完整测试输出、真实数据覆盖、模型与基线的时序样本外比较、参数版本和限制。合成数据测试通过不等于真实预测精度通过。
- 保留市场30天快照，并证明长期日频训练/报告/模型记录不被30天清理影响。
- 交付估值库初始化/迁移/备份说明、配置项、AI关闭与故障降级、后台任务检查方法、API样例和模型回滚方法。
- 给出运行文件部署候选清单和数据库方案；由用户另行授权上线，沿用项目首尔部署技能，禁止把本地数据/配置/令牌上传覆盖线上。
- 最终报告明确：已开发能力、已实测来源、验证样本数、尚未验证的盘中精度、仍然不可观测的当前持仓风险。

参考依据见Spec；方法资料：[Ridge](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.Ridge.html)、[TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)、[时变回归示例](https://www.statsmodels.org/stable/examples/notebooks/generated/statespace_custom_models.html)。模型选型是本方案的可检验建议，不是精度承诺。
