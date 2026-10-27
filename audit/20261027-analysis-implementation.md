# Task 3：精简非阻塞 AI 实施记录

日期：2026-10-27；工作目录：`D:\trae\daily_report_project`。

## 实施范围与集成接口

本任务新增 `src/fund_estimate_analysis.py`、`tests/test_fund_estimate_analysis.py`，修改 `src/fund_estimate_ai.py`；服务、接口、页面、正式净值对账、提交和部署由主任务处理。本记录没有执行线上操作或 Git 提交。

服务应在实例创建时保留一个 `VerificationAnalysis(store, ai)`，每次报告读取调用 `read(items, stats, overall)`，合并其返回的 `ai_analysis` 与 `ai_status`。无需修改估值库 schema，沿用现有 `get_watermark` / `set_watermark`。

公开辅助函数 `compact_verification_summary(items, stats, overall)` 返回四句确定性摘要，空明细返回 `None`。它直接依据可比较误差、同期间基线、最大单条误差及下一净值日观察点组织内容，不复用旧版 `overall.summary` 长文案。

## 并发、持久化和失败策略

- 第一次 `read` 在实例锁内登记数据版本并启动后台线程，立即返回当前数据的确定性摘要及 `generating`。网络调用和压缩请求均在锁外执行，不等待 AI 才返回明细。
- 同版本并发读取合并为一个生成任务。每实例只有一个工作线程，不同版本排队串行；任务完成后退出，后续新版本再启动。旧版结果写入自己的缓存键，不作为新版本点评返回。
- 新键格式为 `verification-analysis:short-v1:<24位摘要>`。摘要包含全部白名单明细、统计、期间、按日/按基金聚合与诊断；数据顺序按日期和基金排序。旧 `overall-v3` 长文案键不会命中。
- 缓存记录包含版本、状态及分析正文；失败另保存尝试次数和绝对 `retry_at`。重启可恢复成功分析，也可恢复失败冷却。
- 失败后仅在后续读取且冷却期满时重试。冷却按失败次数从 60 秒开始翻倍，最大 900 秒；后台不 sleep 后自发重试，避免热循环。
- 模型停用或不存在时返回短摘要和 `disabled`，没有明细时返回 `ready` / `None`，均不发模型请求。生成/缓存异常记录日志；已得到的短摘要仍可返回。

## 短评约束与隐私

- 提示词要求四个维度：整体方向表现相对同期间简单基线、误差大小与方向、拖累基金或日期、下一净值日具体核验点；单日不得虚构跨日趋势，原因未有证据时标明待核验或假设。
- 单次输出预算为 512 tokens。`EstimateAi.complete(messages, max_tokens=4096)` 保留报告提取的 4096 默认预算，短评单独传入 512；兼容仅有 `complete(messages)` 的旧实现，使用签名检查，内部 `TypeError` 不触发重复请求。
- 输出转为四行完整短句，总长度不超过 180 字，每句不超过 70 字。目标为 120–180 字；为避免填充文案，允许有实质内容的更短输出，但拒绝不足 80 字的四句占位。
- 超长、空内容、条数或基本事实校验无效时只进行一次完整压缩/重写。仍不合格则返回确定性四维摘要并进入失败冷却，不机械截断句子、数值或单位。
- 数字校验识别公开日期、代码及名称，检查百分数、bp、百分点、倍数和计数是否可由请求数据及允许换算支持；比例转换与 `_pct` 单位分开，数值统一三位小数。常见久期、持仓、杠杆和因子原因须有假设或待核验等限定，明显无依据的原因句会被重写或降级。
- 数值校验是源数值与单位闭合检查，不等于对全部自然语言语义的形式化证明；提示词仍承担事实解释与四个观察维度的约束。确定性摘要避免未经证实的原因推断。
- 请求及缓存摘要键均使用严格字段白名单。持仓金额、份额、预估收益、实际收益、金额误差、任意调用方附加字段及原始 `overall.summary` 不进入请求或键；按日、按基金和诊断的额外字段同样过滤。
- 明细仅向模型发送最近 20 条，按日/按基金统计和诊断仍覆盖全部历史。`pending_return` 与 `missing_estimate` 不被确定性摘要当作已可比较记录；无可比较项时提示先核对收益口径、下一净值日保存有效预估，避免补造历史预测。

## TDD 与验证证据

1. 新增初始专测后先运行：15 个失败，原因分别为分析器模块尚不存在，以及 `EstimateAi.complete` 尚无 `max_tokens` 参数。
2. 完成首次实现后：专测及原 AI 测试 17 个通过。
3. 新增数值单位、虚构数字、原因限定及极短占位回归后先观察到 4 个失败；补充校验后专测及原 AI 测试 28 个通过。
4. 接口整合期间新增 `pending_returns` 白名单回归，先观察到 1 个失败，补齐字段后通过。
5. 最终命令：`python -m pytest tests/test_fund_estimate_analysis.py tests/test_fund_estimate_ai.py tests/test_fund_estimate_profiles.py -q --tb=short`，结果 **48 passed in 21.92s**，其中新专测 27 个、原 AI 2 个、profile 19 个。
6. `python -m py_compile src/fund_estimate_analysis.py src/fund_estimate_ai.py tests/test_fund_estimate_analysis.py` 退出码 0。
7. `git -c core.whitespace=cr-at-eol diff --check -- src/fund_estimate_ai.py` 退出码 0。新文件由主任务精确暂存后再纳入最终 staged 空白检查。

回归覆盖慢 AI 不阻塞、同版本 40 次并发读取只发一个请求、多版本共享一个工作线程、成功缓存重启恢复、旧长版本不命中、新数据不混入旧点评、失败冷却与重启恢复、冷却后重试、一次压缩、压缩仍无效降级、停用与空数据、隐私字段、待核验收益、旧签名兼容、内部 TypeError 不重发、百分数/bp 与原因限定，以及零收益和长标签等摘要分支。

上述验证使用本地 SQLite 和模型边界替身，原 AI 测试另使用本地 HTTP 服务器；没有调用真实外部模型服务，线上部署与全服务整合验证由主任务继续完成。
