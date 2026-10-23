# 基金预估修复与行情页改造交接

交接日期：2026-10-23，北京时间。用户因5小时额度不足，要求停止继续扩展，写交接文档，由其他AI继续。**当前是已落盘、部分验证、尚未最终验收的开发中状态，不是已上线成品。**

## 1. 接手第一步

- 仓库：`D:\cherry\auto_faribao`，当前分支`master`。
- HEAD：`a81e36d25982072cef34b95fd2fcf4231c230f14`（`fix: 预估涨跌幅固定显示三位小数`）。
- 本轮所有修复/新增功能都还在工作区，**没有提交、推送、部署**；必须保留现有改动继续，不能checkout/reset到HEAD重做。
- 原有未跟踪文件`=`不属于本任务，不删除、不提交。
- 用户禁止擅自创建分支、worktree、隔离检出；默认当前目录和当前分支继续。PowerShell使用`pwsh`7，中文提交说明。不要自动操作生产环境。
- 工作中的资料提取子代理已中断，其他子任务已结束；接手后无需等待旧代理继续，也不要重复派发已经落盘的任务。

建议先读本文件，再读`git diff`和新文件，不要从早期设计方案重新实现。

## 2. 用户最终要求，全部仍有效

1. 修复单基金预估四项问题：训练/实时行情口径一致；回测选择与实际训练一致；经验区间残差方向正确；真实公告报告自动获取并更新画像。
2. 已持有和有效待确认买入都自动发现，新增基金不改代码。即使份额0，待确认买入也在原“持仓涨跌幅预估”列表显示，标记“买入待确认”。取消、失败买单不继续当作活跃基金。
3. **在每只基金涨跌幅正下方**显示“预估收益 ¥…”，与百分比右对齐，红涨绿跌。用户用截图红框指定该位置。
4. 预估金额仅用已确认份额：`已确认份额 × 基准净值 × return_pct / 100`。纯待确认显示“待份额确认”；已有持仓又有待确认买入，只计算已确认部分。预测不得改正式净值、收益、市值和结算。
5. 行情页**债市晴雨表上方**增加股市行情：A股主要指数、港股、美股，以及A股主要行业板块领涨、领跌。
6. 股市按各自市场交易时段采集，午间/收盘补一次最终快照后停止持续更新；A股收盘不代表港股/美股也停止。美股日期按纽约当地日期，不能把北京时间次日冒充美股当日。
7. 优化数据更新：后台共享缓存（股市约30秒，债市/预估约60秒），行情轻量接口、ETag、不变不重绘；页面隐藏暂停轮询，回来补新；避免行情页整页每分钟重拉重算。
8. 保留既有南方晴雨表、四类收蛋预估、30天快照及原页面风格。AI可选，不能让LLM凭空编一个涨跌数，也不能把测试通过当成预测精度验证通过。

## 3. 当前实现和对应文件

### A. 模型修复已落盘，需要最终独立复核

文件：`src/fund_estimate_models.py`、`tests/test_fund_estimate_models.py`、新增`tests/test_fund_estimate_model_regressions.py`。

- 同日期前段选训练窗口和模型，后段固定选择评估；最终仅用选中窗口拟合。
- 基线和Ridge各存自己的MAE、偏差、方向和残差。
- 残差改为实际减预测；用前缀校准、后缀检验覆盖，避免同批样本自证区间。
- 跨源无可靠映射直接返回参考，不再显示中心数值；带证据映射会实际应用`slope*x+intercept_bp`。
- 日终经验区间不挪到盘中；阻断未来模型、画像、行情。
- 第二轮还补了因子版本可见时间：严格`point_in_time`与`retrospective_current_versions`分开。首次回补历史只能证明抓取时可见，允许明确标注限制的回顾探索训练，不能冒充无前视验证、经验区间或ready。
- 哈希包含基金身份、训练数据和窗口追踪，避免模型版本碰撞。
- 基线最终仍使用与回测一致的统计久期，未把当前新报告久期倒填到历史。报告画像已接通，但**不要宣称每个Ridge点值已直接使用报告久期**。

独立审查曾发现“映射参数只验不应用”和“漏检查因子修订可见时间”，第二轮代码与回归测试已经落盘，当前测试通过，尚未再次独立审查。

### B. 同源历史/盘中方案已实现并真实取到75条历史因素

新增`src/fund_estimate_cfets.py`及`tests/test_fund_estimate_cfets.py`。生产装配在`src/fund_estimate_service.py`已改为该来源，`source_family='cfets_realtime_mean'`。

使用中国货币网同一个实时曲线接口，历史固定17:00，盘中取最新共同分钟；**不是17:15收盘回归曲线**。国债/国开/AAA中票三类标准曲线均值产生四因素：gov3、gov10−gov3、cdb5−gov5、AAA3−gov3。

```text
https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/RtimeYldCurv?lang=CN&qttndate=2026-10-22&qttntime=17:00&bondType=100101
```

国债`100101`、国开`230101`、AAA中票`270101`；普通UA和Referer `https://www.chinamoney.com.cn/chinese/bkcurvrty/`。`data.data`行第3列是期限，均值取第1列非空，否则第6列，跳过空占位行；均值单位百分数，差值乘100才是bp。

- 回补必须同时指定日期和时间；缺time会静默回退最新。必须检查返回日期、时间和必要点位，HTTP200不是有效数据。
- 本轮在临时估值库真实逐日回补：**75条完整日变动，2026-07-02至2026-10-22，耗时80.89秒**。临时库随后自动删除，未覆盖本地/生产数据。
- 同次盘中样本：2026-10-23 15:11，基准10/22，四因素`-0.22,+0.27,+0.53,-0.96bp`。这只是当时样本，不是后续实时值。
- 75条能训练60窗口，独立验证样本少；不能承诺120/250窗口或已验证的盘中准确率。
- `curve_level_versions`保存原曲线，`factor_observations`保存版本；时间保守记录实际抓取可见时点。
- 晴雨表和收蛋仍用南方，基金模型单独用中国货币网。不要把旧中债模型直接改名当成新来源模型，应重新训练。

### C. 真实公告、PDF、可选AI接入已落盘

新增`src/fund_estimate_documents.py`、`tests/test_fund_estimate_documents.py`、`tests/test_fund_estimate_ai.py`；改`src/fund_estimate_profiles.py`、`src/fund_estimate_ai.py`及画像测试。

- `PublicFundDocuments(store, session=None)`构造不联网，`list_documents(identity,since=None)`按任意六位基金代码获取公告，缓存PDF逐页文本。
- 真实接口：`https://api.fund.eastmoney.com/f10/JJGG?fundcode=400030&pageIndex=1&pageSize=10&type=3`；费率相关公告type2。
- JSON为`ErrCode=0,Data=[...]`，字段`FUNDCODE,TITLE,PUBLISHDATE,ATTACHTYPE,ID`。PDF地址`https://pdf.dfcfw.com/pdf/H2_<AN编号>_1.pdf`。
- 生产factory已从`_EmptyDocuments`改成`PublicFundDocuments`。只读展示辅助函数中仍有`_EmptyDocuments`占位，不运行采集，**不要误认为生产装配仍是空源**。
- `store.document_by_url(url)`已补；PDF跨实例/重启可复用。新报告保留字段证据、页码、报告期、公告日；季度继承中报久期另存`duration_report_as_of/duration_published_at`，不要把季度画像日期当成久期更新日。
- 确定性提取敏感度金额/净资产推导历史久期、债种比例、披露费率；费率生效日未知不默认1900。
- AI输出预算已增大，完整HTTP JSON不截500字符，截断输出拒绝；同PDF不同份额缓存不串费率。

无API Key、未调用真实AI的公开烟测已成功：

| 基金 | 报告ID | 本次真实提取 |
| --- | --- | --- |
| 400030 | AN202608311828753690 | 净资产26554376757.19；上/下行久期代理6.797331/5.931876，中心6.364604；中票51.51%、金融40.73%、政策6.63%、债合计135.94%；管理费披露0.70% |
| 006902 | AN202608291828684121 | 久期代理2.355625；中票64.79%、金融42.26%、政策5.36%、债合计111.89%；管理费披露0.30% |

政策债是金融债子项不可重复相加，净值口径敏感度不能再乘一次杠杆。以上是2026中报历史先验，不能说当前持仓已知。

### D. 任务/存储/金额联动已落盘

改`src/fund_estimate_store.py`、`src/fund_estimate_service.py`、`src/fund_estimate_sources.py`、`src/fund_estimate_types.py`；新增`tests/test_fund_estimate_corrections.py`。

- failed任务到退避时间后可领取；同日净值修订/分红覆盖补齐触发训练和再验证。
- 分红覆盖纳入净值经济内容版本，保留早期missing观测，不被`INSERT OR IGNORE`永久卡住；验证真值可更新。
- 使用现有`is_trading_day`日历，不再只看星期。
- 新来源使用v2回补幂等键，曲线版本改变会触发共享重训。历史按小批回补，避免每分钟拉5年历史。
- `FactorObservation.mapping_evidence`为末尾可选字段；估值库初始化给factor表增列并建curve缓存表，正式账本schema仍7。
- `read_snapshots`给尚无预测的新待确认基金生成pending展示，附name、holding_status、pending_purchase。
- `estimate_holding_amount`用Decimal计算已确认份额对应金额，保留两位小数；未知预测/0份额不显示伪造的¥0.00。
- 后台网络完成后用来源时钟取得计算时刻，避免刚抓到数据因晚于任务开始时间而误判未来。

### E. 股市后端与页面已整合，尚缺浏览器最终验收

新增`src/stock_market.py`、`tests/test_stock_market.py`。指数/一级行业均来自东方财富公开push2，股票模块独立缓存表`stock_market_cache`，不改正式收益。

接口：`ensure_stock_market_schema(conn)`、`refresh_stock_market(conn,now=None,session=None,force=False)`、`read_stock_market(conn,now=None)`、`stock_market_due(now=None,snapshot=None,trading_calendars=None)`。

- 10指数：上证、深证、创业板、科创50、北证50、恒生、恒生科技、道指、纳斯达克综合、标普500。
- 恒生科技secid为`124.HSTECH`；该接口`100.NDX`实际是纳斯达克综合，不能误标纳斯达克100。
- `fltt=2`时`f3=0.89`即0.89%；`f124`为Unix更新时间。美股转纽约时间含DST。
- 行业使用一级`fs=m:90+s:2+f:!50`（31类），不要用混496层级的`t:2`。正值前5领涨、负值最低5领跌，只有3个下跌就只显示3个。
- 按CN/HK/US分别交易时段；午间、收盘收尾采集一次，之后包括force也不持续联网。首次空缓存允许bootstrap取最近收盘。
- CN用现有节假日日历；HK/US默认仅工作日时段，支持注入交易日历，`calendar_known=False`，**精确港美节假日历尚未接入**。
- 真实烟测10指数、31行业均可用，约1.625秒；没有备用源。

`src/scheduler.py`每30秒后台尝试股市采集，模块内部判断交易时段和TTL。调用普通`repo.database.connection()`，不能放在正式账本写事务或外部组合锁里。

`src/portfolio_api.py`新增带既有鉴权的`GET /api/portfolio/market-overview`，返回`stock_market/bond_barometer/fund_estimates`，只读缓存，使用SQLite `mode=ro`读市场库；ETag/304和private缓存已加。新增`tests/test_market_overview_api.py`。

`src/nav_dashboard_page.html`已加入股市卡片（债市上方）、金额行、待确认标记、状态/时间；行情页30秒轻量轮询，其他页60秒原组合刷新；页面隐藏暂停、恢复补新、304不重绘。尚未做最终手机/桌面截图验收。

## 4. 验证记录：哪些是真的完成，哪些不能宣称

接手前基线全套：1185通过。开发过程中某代理跑过1266通过/1失败，但当时其他文件正在修改，失败股票收尾用例后来已修，**该过程结果不能当本轮最终全绿**。

停止前主线程新跑的两批检查均通过：

```powershell
# 128 passed in 2.53s
python -X utf8 -m pytest -q tests/test_nav_dashboard_page.py tests/test_market_overview_api.py tests/test_fund_estimate_corrections.py tests/test_fund_estimate_api.py tests/test_fund_estimate_cfets.py tests/test_fund_estimate_jobs.py

# 96 passed in 3.60s，包含模型第二轮改动和报告久期日期字段
python -X utf8 -m pytest -q tests/test_fund_estimate_models.py tests/test_fund_estimate_model_regressions.py tests/test_fund_estimate_ai.py tests/test_fund_estimate_documents.py tests/test_fund_estimate_profiles.py tests/test_stock_market.py

# 已通过；仓库既有CRLF，只有行尾转换提示，不要全仓换行整理
git -c core.whitespace=cr-at-eol diff --check
```

**最终完整pytest、最终编译、最终独立审查、浏览器验收、部署都未完成。** 不要把两个批次或代理报告拼成“完整全测已过”。

原生代理协作记录：[Codex / gpt-6.1-sol / 思考强度：xhigh]实现模型/报告/股票；[Codex / gpt-6.1-sol / 思考强度：high]研究源与独立模型审查。模型代理第二轮因容量报错中止，但它的修改和回归已落盘；本次96项已验证其可运行，不要误判为完全没做。

## 5. 接下来必须继续的工作（优先级顺序）

1. **先修轻量接口ETag实际失效的整合问题。** 当前canonical只排除`generated_at`；股票read响应含`cache_age_seconds/source_age_seconds`，每次读取都会增长，有真实缓存时ETag可能次次变化。现有API测试只有空缓存，未覆盖。加“有真实股票缓存且源数据没变”的304行为测试，canonical排除纯年龄字段，同时保留stale/phase等有意义状态变化；不必删API中供判断的年龄元数据。
2. 核对`CfetsCurveSource`历史回补失败与游标：网络异常现在可能被当缺曲线跳过，而service仍推进批次游标；最近14天外临时失败的洞不能永久漏掉。区分来源本就无历史/短暂网络错误，留可重试水位并测恢复。
3. 核对债市17:00后的保留行为：`read_intraday_factors`当前5分钟新鲜度门槛可能在17:06起把合法收盘值变成missing，而估值调度还到17:30。应保留/明确标收盘参考，避免收盘后清空结果；不能把昨天数据冒充今天。
4. 给最新模型修复做独立复审，尤其来源版本可见性、同日期修订、选窗和真正实盘一致、映射实际应用、旧模型迁移降级。当前75条历史是回补版本探索，不是75天已保存的盘中预测。
5. 完成真实整合：用**临时估值库和模拟组合**跑PublicFundDocuments + 同源曲线/NAV + train + snapshot + cached API，覆盖一个0份额待确认基金，确认数据准备→可算值/诚实降级闭环；不要动生产账本。
6. 浏览器手机390px/桌面验收：股市顺序、港美日期、行业列表、无横向溢出、百分比下方金额、pending标签、XSS转义；验证隐藏/切页/恢复/304不重绘。已有Node单测仅局部函数，不是整页验收。
7. 检查行情手动刷新和自动轻量更新是否重复请求；检查主看板首次render与轻量响应竞争是否覆盖较新状态。设置页5秒状态轮询在隐藏页也应停止/跳过。
8. 检查后台可关闭/失败隔离、重复入队、清仓/取消买入退出刷新、估值库迁移（含旧数据库无mapping列）及长期历史保留。HK/US公休日历需补齐或明确限制，不能宣称精确交易日判断已完成。
9. 运行完整`python -X utf8 -m pytest -q`、`python -m compileall -q src main.py`、差异检查。补运行/模型限制文档，给用户报告完成与未完成、真实数据覆盖、测试及预测质量限制。
10. 用户另行要求上线时才执行首尔部署技能；必须按实际未提交源码哈希核对文件清单，不能只部署HEAD。先预演再执行，备份配置/数据边界不变，不上传本地库、令牌、config或密钥。

## 6. 其他文档及生产背景

- 本轮实施记录：`docs/superpowers/plans/2026-10-23-fund-estimate-corrections.md`。
- 原设计：`docs/superpowers/plans/2026-10-23-bond-fund-intraday-estimation.md`。
- 原研究：`docs/bond-fund-intraday-estimation-assessment-20261023.md`。
- 原运行文档：`docs/fund-estimate-operations.md`（**仍描述旧中债训练方案，需要更新**）。
- 公共接口原始样本在本机临时目录`faribao-chinamoney-research-20261023`及`faribao-stock-research-20261023`，只供辅助，接手代码不依赖它们。
- 首尔生产：`/home/ubuntu/daily_report`、`daily-report`服务、8080端口；部署技能`C:\Users\jsitc\.codex\skills\auto-faribao-seoul-prod-deploy\SKILL.md`。本轮没有上传，线上仍是修复前的开发者版本。
- 更早南方晴雨表备份`/home/ubuntu/daily_report/backups/20261023_bond_intraday_eggs_01`只对应当时四文件，**不能误当作当前整套基金功能的完整回滚包**。

## 7. 可直接复制给接手AI的提示词

> 请继续`D:\cherry\auto_faribao`当前master的未提交开发，先完整读取`docs/handoffs/2026-10-23-fund-estimation-and-market-handoff.md`并检查git diff。用户要求债基预估四项修复、待确认基金在同一列表显示、涨跌幅正下方显示已确认份额的预估收益、债市上方加A/港/美股和一级行业、按市场休市停止采集、轻量缓存更新。大量实现已经落盘，两个相关测试批次128/96通过，但最终全测/独立审查/浏览器验收/上线未完成。优先修本交接第5节ETag年龄字段、同源回补失败水位和债市收盘保留问题，再完成真实临时库整合、独立复审和完整验证。不得重置/覆盖已有修改、删除无关`=`、自建分支/worktree、自动推送或部署；不得修改正式净值或收益账本。保留已有南方晴雨表/收蛋，基金模型采用已实测75条历史的cfets_realtime_mean同口径来源重训，历史回补数据只作有明确限制的探索回测，不能宣称盘中准确性已证实。例行实现自行决定，不反复询问，最终如实报告剩余限制。

## 8. 2026-10-23 第二轮接手执行记录（同日追加）

第5节优先项 1/2/3/5/9 已完成，均先写失败测试再修：

1. **ETag 年龄字段**：`src/portfolio_api.py` canonical 排除 `generated_at/cache_age_seconds/source_age_seconds`（纯年龄），保留 stale/phase/refresh_due 等状态字段。`tests/test_market_overview_api.py` 新增：真实股票缓存下仅年龄增长→304；TTL 过期 stale 翻转→ETag 变化；源数据更新→ETag 变化。
2. **回补失败水位**：`src/fund_estimate_cfets.py` 的 `CurveUnavailable` 增加 `transient` 分类（网络/服务端临时故障 vs 来源无数据），`_failed` 退避缓存记录失败分类；`fetch_daily_factors` 记录临时失败日，`take_transient_failures()` 供查询；`EstimateSources.transient_reader` 注入；`_sync_daily_factors` 新增 `{family}_retry_days` 可重试水位（当日缺口由近14天窗口滚动重试，成功/永久缺失退出水位），批次游标照常推进但洞不再永久漏掉。测试覆盖：网络失败留水位+恢复后入库清空、永久缺失不进水位、退避缓存命中仍算临时失败。
3. **债市收盘保留**：`read_intraday_factors` 的 5 分钟新鲜度门槛只约束盘中；17:00 后保留收盘定格值（native），收盘后接口未出 17:00 最终曲线时保留最后时点并标 `close_reference`（market_as_of 仍精确到实际时点，不冒充 17:00）；盘中陈旧/日期落后仍 missing。
5. **真实临时库整合**：新增 `tests/test_fund_estimate_integration.py`——临时估值库+模拟组合（一只持仓 10000 份、一只 0 份额待确认买入），PublicFundDocuments（公告 JSON/PDF 传输边界替身）→画像（2026-06-30 中报）→NAV+同源曲线多批回补→train_models 选窗→快照→read_snapshots→缓存 API；持仓基金预估收益金额与 `份额×基准净值×return_pct/100` 精确一致，待确认基金显示 `awaiting_confirmed_shares` 无伪造金额；正式组合库全程只读。另做真实网络抽查：真实曲线 2026-10-22 17:00 历史与 2026-10-23 盘中解析成功（16:09 盘中样本 basis_status=native）。
9. **完整验证**：`python -X utf8 -m pytest -q` 全套 **1290 passed**；`python -m compileall -q src main.py` 通过；`git -c core.whitespace=cr-at-eol diff --check` 通过（仅行尾提示）。运行文档 `docs/fund-estimate-operations.md` 已更新：因素来源改为 cfets_realtime_mean、回补游标/重试水位/收盘保留行为、模型与数据限制（75 条仅探索回测，不宣称盘中准确率）。

仍未完成（下轮继续）：第5节第4项独立复审结论待落实、第6项浏览器手机390px/桌面验收、第7项刷新请求重复/渲染竞争/隐藏页轮询检查、第8项后台失败隔离与迁移抽查、第10项部署（需用户明确要求）。`tests/test_fund_estimate_integration.py` 的 PDF/公告/曲线均为传输边界替身，真实公告提取的烟测证据见本文第 3 节 C。

## 9. 独立复审结论与落实（同日追加）

独立复审（只读子代理，git diff 全量比对+逐文件通读）结论：8 项模型修复目标均落实且有测试证明，无"严重"问题；测试断言质量高、含真反例（同批自证 coverage=0 构造、三类前视拦截、8 种坏映射等）。以下发现已当场修复并补测试：

1. **[重要] 净值修订只重算被改日期真值**：`_backfill` 现在把每个被改净值日的后一净值日也纳入重算（realized_return(D) 依赖 nav_{D-1}，修订 D-1 必须更新 D 的验证真值）；`save_verification` 的 ON CONFLICT DO UPDATE 语义由 `test_previous_day_nav_revision_updates_next_day_verification` 验证。
2. **close_reference 不能借用日终经验区间**：`_interval_matches_observation` 排除 `basis_status=close_reference`（时点晚于 17:00 的收盘参考不是日终定格输入），`test_close_reference_curve_cannot_borrow_daily_close_interval` 覆盖。
3. **失败分类细化**：`parse_curve` 的"同一期限冲突均值"改为 transient（坏数据可重试）；"日期/时间与请求不一致"（接口回退）保持永久——它是"该时点无数据"的信号，若进水位会让 5 年回补积累上千个永久失败日。
4. **模型哈希幂等**：cutoff_at 截断到日期进哈希，同数据同日重复训练复用同一 model_version（不再灌大模型表），`test_retraining_same_data_same_day_reuses_model_version` 覆盖。
5. **水位持久化防丢**：`_sync_daily_factors` 把 `{family}_retry_days` 写入放 finally，半途异常不丢已收集的临时失败日。
6. **死代码**：删除无调用方的 `_expanding_metrics`。
7. **测试缺口**：补 read_snapshots 的 basis_unverified 读路径降级测试、旧估值库无 mapping 列的增列迁移测试。

复审建议中明确接受为已知限制（如实报告、未改行为）：`min_selection_predictions/min_validation_predictions` 缺省 1（改缺省会改变现有训练行为，仅 75 条数据量级下选窗统计脆弱）；`mapping_evidence` 无生产写入方（带证据映射是已实现未接通的能力，生产跨源一律参考态，方向安全）；`factors_as_of` 每 (family, as_of) 只留最新版本（方向保守、不引入泄漏）；`EstimateInputError` 在快照调用链未捕获（模型契约未变，现实风险低）。

最终验证（复审落实后）：`python -X utf8 -m pytest -q` 全套 **1297 passed**；`python -m compileall -q src main.py` 通过；`git -c core.whitespace=cr-at-eol diff --check` 无实质问题。真实接口抽查：中国货币网曲线历史/盘中解析成功（2026-10-23 16:09 盘中样本）；东方财富股市接口本次被服务端断连（curl 同样断、货币网正常，属网络/反爬环境问题，上一轮 10 指数 31 行业真实烟测记录见第 3 节 E）。

## 10. 部署记录（2026-10-23 16:48 追加，用户明确要求上线）

- deploy.ps1 的 [2/7] 备份引号 bug（记忆已记录）再次失败，按已验证手动流程完成部署。
- 备份：`/home/ubuntu/daily_report/backups/20261023_1648_fund_estimate/`（16 个旧文件 + 3 个新文件缺失标记 + deploy-manifest-prev.json + deploy-manifest.json）。
- 部署 19 个文件：本次功能 13 个（fund_estimate_* 全套、nav_dashboard_page.html、portfolio_api.py、scheduler.py、stock_market.py）+ 依赖闭包 6 个（portfolio_market、portfolio_transactions、portfolio_providers、nav_holdings、portfolio_jobs、portfolio_income——它们是已提交但从未部署的真实差异，为保证新版调用方与被调方一致同步上传）。无关漂移 4 个（daily_report_draft、daily_report_edit_confirmation、nav_report_image、portfolio_migration）未动。
- 验证：线上 compileall 通过、daily-report 重启、`/` `/nav` `/nav-assets/refresh-cw.svg` 鉴权 `/api/nav-dashboard` `/api/portfolio/market-overview` 全 200、SHA256 19/19 一致、服务 active；market-overview 实测返回 stock_market（线上东财可达）/bond_barometer/fund_estimates（4 只基金）。
- 状态文件已更新（commit=a81e36d、**source_dirty=true**——本次部署含未提交工作区源码；本地 git 未提交未推送）。回滚命令见部署报告。
