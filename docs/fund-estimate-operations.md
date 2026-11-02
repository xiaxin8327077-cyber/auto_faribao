# 债基盘中预估：运行、备份与回滚

估值写在独立库 `data/fund_estimation.db`，schema 版本 1。正式组合库仍是 `SCHEMA_VERSION = 7`。预测不写入 `quotes.unit_nav`，也不改市值、收益、分红或结算。

## 配置

`config.yaml` 可以增加可选段，不写则使用代码默认值。后台任务默认关闭，避免服务启动后自行联网。

```yaml
fund_estimate:
  enabled: false
  enable_ready: false
  min_fit_samples: 60
  min_oos_residuals: 20
  half_life_days: 30
  ridge_alpha: 1.0
  stale_minutes: 5
  max_mae_bp: null
  min_direction_accuracy: null
  min_interval_coverage: null
  min_intraday_samples: 20
```

看板「设置 → 模型设置」另有两套开关，保存在估值库而不是旧的助手环境变量：

- 启用盘中预估后台任务：打开后，调度器每分钟只把任务放进队列。
- 报告提取：填写接口地址、API Key、模型名称。配置列表只回传密钥后四位；点击「复制 Key」时才通过独立鉴权接口读取原文。测试连接不会把密钥写入日志，也不会把持仓金额发给模型。

两个开关在读取成功后才可操作，改动立即保存，失败时恢复已保存状态。编辑或复制配置时，接口地址改变后必须填写新 Key；获取模型列表、测试连接和正式保存使用同一规则。连接测试需收到有效且未截断的回答，HTTP 200 本身不代表成功。

响应校验沿用 [Chat Completions 官方响应结构](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)，同时保留兼容服务商对 `finish_reason` 的已有兼容处理。

「预估验证分析 → 累计分析」右侧提供「重新生成分析」。切换配置后点击此入口，使用当前配置重新总结同一份验证数据；仅更新点评，不改验证明细和统计。生成中按钮禁用，同一份数据与配置的重复请求合并。每次任务固定入队时的模型配置，完成前再切换不会改动该任务的模型；不同配置的生成记录分开保存。打开页面或切换模型时，同一份数据优先显示最近已完成的点评，标注保留其原模型；只有手动点击才为相同数据重新生成，新正式净值改变数据后仍自动分析。旧缓存按原数据和模型精确匹配迁移，不因缓存规则升级自动重跑。手动重新生成不受自动暂停到收盘的限制。

`enable_ready` 默认 false。即使回测指标看起来不错，界面也保持「试验」或「同类参考」，不能显示「已验证」。质量门槛要同时配置误差、方向准确率、区间覆盖和最少盘中样本，才会允许 `ready`。

## 调度

- 持仓发现和盘中快照：交易时段每 60 秒入队。
- 正式净值：18:00 至次日 02:00，每 30 分钟检查；出现新净值才训练一次。
- 公告：每天 07:30 全量，交易日 08:00–23:00 每小时增量。相同 PDF 哈希不重复调用模型。
- 法定工作日不等于债市交易日。周五夜盘可以跨到周六 02:00；调休周末要由交易日历标明。

GET `/api/portfolio/fund-estimates` 只读快照，不训练、不抓取。

## 备份与迁移

停写或等当前任务结束后复制库文件。程序也提供 SQLite backup：

```python
from src.fund_estimate_store import FundEstimateStore
FundEstimateStore("data/fund_estimation.db").backup("backups/fund_estimation.db")
```

恢复时替换 `data/fund_estimation.db` 后重启进程。不要把这个文件覆盖到正式 `portfolio.db`。估值库损坏时，看板和交易仍按原逻辑启动，本模块返回空列表。

30 天清理只删除晴雨表快照。日频净值、画像、模型和验证记录留在估值库，默认至少保留 3 年。

## 模型版本与回滚

每只份额可以同时留下 `duration_baseline` 和 `weighted_ridge_net`。当前使用的版本 `active = 1`。回滚只切换指针，不删除历史：

```python
store.activate_model("基金代码", "A", "要恢复的 model_version")
```

Ridge 用训练样本的指数权重和训练期标准化。预测必须沿用同一套均值和标准差。系数是净值收益拟合出的统计敞口，不是当天的真实持仓。正式净值已经扣过费，拟合后再扣费是错误的。

Kalman 还没有加入。要加入时，必须用同一套无前视样本和基线比样本外误差。

## 后台状态

设置页显示排队、失败和最近一次错误。也可以直接查库：

```sql
SELECT idempotency_key, job_type, code, status, attempts, error, updated_at
FROM jobs
ORDER BY updated_at DESC;
```

失败任务按 2 的幂退避，最长约 6 小时。进程重启时，遗留的 `running` 会回到 `queued`，已经 `succeeded` 的幂等键不会再执行。

## API 示例

请求头沿用看板：`X-Nav-Dashboard-Key`。

```http
GET /api/portfolio/fund-estimates
```

```json
{
  "generated_at": "2026-10-23T10:00:00+08:00",
  "items": [{
    "code": "动态六位代码",
    "status": "experimental",
    "return_pct": null,
    "interval_kind": "none",
    "confidence_label": "试验",
    "reason_codes": ["insufficient_history"],
    "market_as_of": null,
    "profile_as_of": null,
    "trained_through": null
  }]
}
```

`return_pct` 为 `"0.03"` 时表示 +0.03%，即 3 个基点、3 蛋。空报价不是 0。四类收蛋只出现在 `market_reference`，并注明不是本基金收益率。

```http
PUT /api/portfolio/fund-estimate-model-settings
Content-Type: application/json

{"base_url":"https://example.com/v1","model":"模型名","api_key":"密钥","enabled":true,"jobs_enabled":false}
```

```http
POST /api/portfolio/fund-estimate-model-settings/test
Content-Type: application/json

{"base_url":"https://example.com/v1","model":"模型名","api_key":"密钥"}
```

## 因素来源（2026-10-23 起）

基金模型的因素来源是 `cfets_realtime_mean`（中国货币网同一实时曲线接口），不再用旧的中债日终回归曲线：

- 国债 `100101`、国开 `230101`、AAA中票 `270101` 三类标准曲线取均值，产生四因素：gov3、gov10−gov3、cdb5−gov5、AAA3−gov3。
- 历史固定取 17:00 时点，盘中取最新共同分钟，训练与实时同口径。差异单位是 bp（百分数差 ×100）。
- 回补必须同时指定日期和时间；HTTP 200 不等于有效数据，会核对返回日期、时间与必要点位。
- 历史按约 30 天小批次推进游标 `{source_family}_before`；临时网络故障的日期进入 `{source_family}_retry_days` 可重试水位，不随游标推进被永久漏掉；来源本就无数据的日期不进水位。
- 盘中新鲜度窗口默认 15 分钟。当日完整曲线超过窗口仍可生成预估，标记 `intraday_reference`，读路径保留数值并显示“行情陈旧”；标题显示完整日期和时间。仅使用来源返回的时点，不用请求时间或抓取时间替代。跨日、未来时点和缺少必要曲线仍不估算。17:00 后保留当日收盘定格值（`basis_status=native`）；尚无最终曲线时保留最后时点并标注 `close_reference`。
- 相同日期、来源及净值的分红抓取临时失败，保留截止时点内已核验的经济版本；不同净值、新的已核验修订和首次未知仍按原规则处理。训练水位不足时，旧模型仅在当前有效历史样本重新达到门槛后继续使用。验证完成后按现有规则暂停至下一交易日 15:00。
- 晴雨表和四类收蛋仍用南方基金，与基金模型来源互不替代；跨源没有可靠映射时只显示参考，不显示中心数值。

## 本次没有当成已验证的部分

- 南方盘中矩阵从本项目开始才有本地快照，不能回填更早的分钟历史。盘中样本数按 0 计，上午精度未被收盘曲线证明。
- 已实测回补 75 条完整日变动因素（2026-07-02 至 2026-10-22）。这 75 条只能作为明确标注限制的回顾探索回测；它们证明的是抓取时刻可见性（point_in_time 以实际抓取时点保守记录），不是 75 天已保存的盘中预测。**不能宣称盘中准确率已证实**，独立验证样本仍然很少。
- 训练窗口按同日期前段选窗、后段固定评估；75 条量级只够 60 窗口，不能承诺 120/250 窗口。
- 报告久期来自 2026 中报的确定性提取（上/下行久期代理），是历史先验，不是当前持仓；季度报告继承中报久期另存 `duration_report_as_of/duration_published_at`。基线仍用与回测一致的统计久期，不要宣称每个 Ridge 点值已直接使用报告久期。
- 日终经验区间只在日终同源观测（市场时点 ≥ 17:00）使用，盘中/收盘参考不挪用日终残差区间。
- 东方财富净值接口可以翻页补历史。2026-10-23 实测该接口不返回 `TotalCount`，请求 100 条时单页大约 20 条，且第 2 页继续往更早的日期走。回补必须翻到空页或覆盖起始日；短页不能当成全部历史。分红表解析失败时覆盖状态是缺失，不是“当日无分红”。
- 公告列表若没有可核验的发布日期，画像不能进入无前视回测。
- 研究文档里的 2026-06-30 中报久期已接入报告画像（字段证据、页码、报告期、公告日齐全才应用），但仍是历史材料；015736、006902、003923、400030 不是白名单。
- 股市行情（`stock_market_cache` 独立表）按 CN/HK/US 交易时段采集并停在收盘；HK/US 只用工作日近似，精确港美节假日历尚未接入（`calendar_known=false`）。
