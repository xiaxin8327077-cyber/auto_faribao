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
- 报告提取：填写接口地址、API Key、模型名称。密钥接口只回传后四位。测试连接不会把密钥写入日志，也不会把持仓金额发给模型。

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

## 本次没有当成已验证的部分

- 南方盘中矩阵从本项目开始才有本地快照，不能回填更早的分钟历史。盘中样本数按 0 计，上午精度未被收盘曲线证明。
- 日终训练用中债收盘变动。南方盘中矩阵从本项目开始才有本地快照，不能回填更早的分钟历史，也不能在未做映射核验时替代中债日终。盘中样本数仍要从现在开始积累。上午精度不能用收盘曲线证明。
- 中债日终曲线沿用晴雨表里已经核对过的公开接口，按相邻两个有数据的交易日收盘计算 bp。来源记为 `chinabond_eod`，`basis_status=native`。它和南方盘中 BP 没有做过重叠期映射，盘中快照不会把这两套数拼成一个已验证结果。
- 东方财富净值接口可以翻页补历史。2026-10-23 实测该接口不返回 `TotalCount`，请求 100 条时单页大约 20 条，且第 2 页继续往更早的日期走。回补必须翻到空页或覆盖起始日；短页不能当成全部历史。分红表解析失败时覆盖状态是缺失，不是“当日无分红”。
- 公告列表若没有可核验的发布日期，画像不能进入无前视回测。
- 研究文档里的 2026-06-30 中报久期只是历史材料，没有写进程序。015736、006902、003923、400030 不是白名单。
