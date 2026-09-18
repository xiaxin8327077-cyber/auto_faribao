# 赎回到账与钱包 Plus 申购状态设计

## 背景

当前赎回指定钱包 Plus 为资金去向时，赎回记录与 `cash_transfer_in` 关联记录会在赎回确认日一起变为 `confirmed`。持仓投影只要看到已确认的 `cash_transfer_in` 就立即增加钱包 Plus 份额，完全没有使用赎回记录上的 `settlement_date`。

线上两笔慧盈象赎回因此被提前计入钱包 Plus：

- 100,000 份赎回确认金额 108,000 元，确认日为 2026-09-17，预计到账日为 2026-09-18。
- 50,000 份赎回确认金额 54,015 元，确认日为 2026-09-18，预计到账日为 2026-09-20。

现有交易状态还只表达行情和份额确认，没有独立表达“待到账”和“已到账”；交易列表则按最初交易时间排序，不能在交易状态变化后把该记录移动到最前面。

## 目标

1. 赎回确认只减少源产品份额，不在到账前增加钱包 Plus。
2. 赎回记录明确展示“待到账”和“已到账”。
3. 赎回到账时生成一条真正的钱包 Plus `manual_purchase` 记录。
4. 自动生成的钱包 Plus 申购完整遵循现有申购规则：到账日作为提交日，按交易日规则确定有效交易日，T+1 确认后才增加钱包 Plus 份额，并继续沿用现有收益起算口径。
5. 交易记录按最近一次状态变化时间从新到旧排序。
6. 以可回溯、幂等方式修复线上已提前入账的 108,000 元和 54,015 元。

## 非目标

- 不连接真实理财平台查询到账时刻。
- 不增加小时或分钟级到账确认。系统仍以用户填写的 `settlement_date` 为到账依据。
- 不改变钱包 Plus 手工申购、手工赎回、定投扣款和收益计提的既有规则。
- 不把赎回到账金额临时计入一个新的“现金钱包”产品。

## 核心设计

### 确认状态与到账状态分离

`status` 继续表示交易确认生命周期：

- `pending_quote`
- `pending_confirmation`
- `confirmed`
- `cancelled`
- `reversed`
- `failed`

赎回记录增加独立的 `settlement_status`：

- 空值：该交易不适用到账状态，或尚未完成赎回确认。
- `pending`：赎回金额已经确认，但尚未到 `settlement_date`。
- `settled`：系统已在 `settlement_date` 到期处理这笔到账。

到账状态不与 `status` 混在同一个枚举中。这样源产品份额能在赎回确认时正确减少，同时到账资金仍可保持在钱包 Plus 之外。

页面显示优先级如下：

1. `cancelled` 显示“已取消”。
2. `reversed` 显示“已冲正”。
3. 等待行情或确认时显示“待确认”。
4. 已确认赎回且 `settlement_status = pending` 时显示“待到账”。
5. 已确认赎回且 `settlement_status = settled` 时显示“已到账”。
6. 其他已确认交易显示“已确认”。

### 数据字段

`transactions` 表新增：

- `destination_cash_product_id TEXT REFERENCES products(id)`：赎回确认前后都能直接保存资金去向，不再依赖预建的现金转入记录反查。
- `origin_transaction_id TEXT REFERENCES transactions(id)`：自动生成的钱包 Plus 申购指向来源赎回。
- `settlement_status TEXT`：只对赎回使用，取值为空、`pending` 或 `settled`。
- `settled_at TEXT`：赎回从待到账变为已到账的实际系统处理时间。
- `status_updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP`：本条记录最近一次业务状态变化时间。

增加唯一索引，保证每笔来源赎回最多生成一条非冲正的钱包 Plus 申购：

```sql
CREATE UNIQUE INDEX idx_one_purchase_per_redemption
ON transactions(origin_transaction_id)
WHERE origin_transaction_id IS NOT NULL
  AND transaction_type = 'manual_purchase';
```

模型和仓储层同时读写 `created_at`、`destination_cash_product_id`、`origin_transaction_id`、`settlement_status`、`settled_at` 和 `status_updated_at`。

历史 `status_updated_at` 使用 `COALESCE(reversed_at, confirmed_at, created_at)` 回填。以后创建、确认、取消、冲正和到账处理都更新该字段。

### 新建赎回

新建指定钱包 Plus 为去向的赎回时：

1. 只创建源产品 `manual_redemption`，并写入 `destination_cash_product_id`。
2. 不再预建 `cash_transfer_in`，因此钱包 Plus 不会因为源产品确认而提前增加。
3. 赎回待确认期间继续锁定拟赎回份额。
4. 用户可以在赎回确认前按现有规则取消交易。

### 赎回确认

取得确认净值后：

1. 按 `赎回份额 × 确认净值` 计算确认金额。
2. 源产品赎回状态变为 `confirmed`，源产品份额和成本按现有规则减少。
3. 有 `settlement_date` 时把 `settlement_status` 设为 `pending`；没有到账日期且资金去向为外部时，保持空值。
4. 不创建或确认任何钱包 Plus 入账记录。
5. `status_updated_at` 更新为确认处理时间。

若确认处理发生时 `settlement_date` 已到期，同一轮任务随后执行到账处理，而不是先把资金提前计入钱包 Plus。

### 赎回到账

新增幂等的 `settle_redemptions(as_of_date)` 服务步骤。它选择：

- `transaction_type = manual_redemption`
- `status = confirmed`
- `settlement_status = pending`
- `settlement_date <= as_of_date`

对每笔记录在一个 SQLite 事务中执行：

1. 把源赎回的 `settlement_status` 改为 `settled`。
2. 写入 `settled_at` 并更新源赎回的 `status_updated_at`。
3. 若 `destination_cash_product_id` 为空，只完成源赎回到账状态，不创建钱包 Plus 申购。
4. 若去向为钱包 Plus，创建一条 `manual_purchase`：
   - `product_id` 为钱包 Plus。
   - `amount` 为赎回确认金额。
   - `fee_rate` 为 0。
   - `origin_transaction_id` 指向源赎回。
   - `idempotency_key` 使用 `redemption-settlement:<源赎回 ID>`。
   - `created_by` 使用 `redemption_settlement`。
   - 提交时点使用 `settlement_date` 当日 00:00 北京时间；周末或节假日继续由现有 `confirmation_schedule` 推进到下一交易日。
5. 钱包 Plus 申购保持 `pending_confirmation`，到账处理本身不增加钱包 Plus 份额。

钱包 Plus 申购由现有 `settle_pending()` 在确认日处理。只有它变为 `confirmed` 后，钱包 Plus 持仓投影才增加；收益起算继续沿用现有“确认日不使用新增份额、下一收益日开始参与”的规则。

### 调度顺序

每日组合任务依次执行：

1. 获取并保存行情。
2. `settle_pending(as_of_date)` 确认到期的普通申购和赎回。
3. `settle_redemptions(as_of_date)` 处理到期赎回到账并创建钱包 Plus 申购。
4. 再次运行 `settle_pending(as_of_date)`，只用于处理此前已存在且当日应确认的钱包 Plus 申购；本轮刚由到账生成的申购由于遵循 T+1，不会同日确认。
5. 执行收益计提和其他现有任务。

各步骤保持幂等。重复运行不会重复生成申购或重复改变到账状态。

### 排序与展示

后端 payload 按 `(status_updated_at, id)` 降序输出交易。前端保持后端顺序，不再按最初提交时间重新排序。

交易详情增加：

- 源产品赎回：确认日期、预计到账日、待到账/已到账状态、钱包 Plus 去向。
- 自动钱包 Plus 申购：来源显示“<源产品名称>赎回到账”，并显示有效交易日、预计确认日和收益起算日。

内部修正与冲正事件仍按既有规则隐藏；真正的 `manual_purchase` 必须出现在交易记录中。

## 取消、冲正与异常处理

- 赎回处于待行情或待确认时仍允许取消。
- 赎回确认后不允许普通取消；录错使用冲正流程。
- 待到账赎回被冲正时，不得创建钱包 Plus 申购。
- 已到账但钱包 Plus 申购仍待确认时，冲正源赎回必须同时取消该申购并追加源产品冲正事件。
- 钱包 Plus 申购已经确认后，冲正源赎回必须同时对源赎回和钱包 Plus 申购追加冲正事件，保持两个产品的投影一致。
- 到账处理任一步骤失败时整个事务回滚，源赎回保持 `pending`，并允许后续任务重试。
- 缺少或无效的目标产品时不静默入账，保留待到账并记录错误。

## 数据库迁移

组合数据库版本从 6 升到 7：

1. 增加新字段与唯一索引。
2. 回填 `status_updated_at`。
3. 从尚未冲正的旧 `cash_transfer_in` 关联记录推导历史赎回的 `destination_cash_product_id`。
4. 对有 `settlement_date` 的已确认赎回回填 `settlement_status = pending`；是否已到账由迁移后的显式到账任务决定，不能仅凭旧 `cash_transfer_in` 推断。
5. 更新不可变触发器：允许已确认赎回仅变更到账辅助字段；金额、份额、净值、确认日期和来源信息仍不可修改。
6. 保持升级链 1→2→3→4→5→6→7 每一步写入真实目标版本，并验证失败时整段事务回滚。

## 线上两笔历史修正

提供一次性、幂等、可预演的修正命令，只处理已核实的两笔赎回 ID：

- `658e58e8-a483-4bd3-bed5-0d614f8e534b`（108,000 元）
- `eb4fc341-d594-43ef-a0ae-1bc930ea66f8`（54,015 元）

正式执行前备份 `/home/ubuntu/daily_report/data/portfolio.db`。每笔修正：

1. 校验产品、份额、金额、确认日期、到账日期和原关联钱包 Plus 转入记录完全匹配预期；任何一项不符都停止，不做部分修正。
2. 为原来提前确认的 `cash_transfer_in` 追加一条等额反向事件，并把原记录标记为已冲正，使钱包 Plus 投影扣回提前增加的金额。
3. 在审计日志记录原交易 ID、修正原因、反向事件 ID和执行者。
4. 清除源赎回对旧内部转入记录的业务依赖，保留审计链和 `destination_cash_product_id`。
5. 按到账日期运行新的到账流程：到期记录生成钱包 Plus 申购，未到期记录保持待到账。
6. 重建并校验慧盈象和钱包 Plus 持仓，确认没有重复申购、负份额或悬空关联。

预演只输出将处理的交易、金额、目标状态和余额变化，不写数据库。正式命令重复运行必须报告“已处理”并保持账本不变。

## 测试设计

### 数据库与仓储

- 新数据库包含版本 7 字段和索引。
- 版本 6 数据库可原子升级到版本 7。
- `status_updated_at` 按历史状态时间正确回填。
- 迁移失败不留下半升级列、索引或版本号。
- 每笔赎回最多生成一条来源申购。

### 交易服务

- 赎回确认后源产品份额减少，钱包 Plus 余额不变，状态显示待到账。
- 到账日前重复运行任务不创建申购。
- 到账日只生成一条钱包 Plus 申购，赎回显示已到账。
- 钱包 Plus 申购在到账日仍待确认、余额不变。
- 下一个交易日确认后钱包 Plus 余额增加，收益按现有规则起算。
- 周末到账生成的申购顺延到下一交易日，再按 T+1 确认。
- 外部去向赎回可变为已到账，但不生成钱包 Plus 申购。
- 取消、冲正、重复任务和事务失败保持账本与持仓一致。

### API 与页面

- payload 返回 `settlement_status`、`settled_at`、`status_updated_at` 和自动申购来源。
- 页面显示待到账、已到账以及钱包 Plus 申购记录。
- 钱包 Plus 自动申购在交易筛选中按申购展示。
- 交易列表按最近状态变化时间从新到旧排列。
- 同一时间使用交易 ID 作为稳定排序键。

### 线上修正

- 使用生产数据库副本验证预演只读。
- 正式修正副本后，钱包 Plus 立即扣除所有尚未按新申购规则确认的提前金额。
- 两笔赎回分别按 2026-09-18 和 2026-09-20 生成且仅生成一条钱包 Plus 申购。
- 第二次运行修正命令不产生任何新事件。

## 部署与验证

1. 本地完成定向测试、完整测试、Python 编译和 `git diff --check`。
2. 使用 Seoul 生产部署脚本预演，仅上传本次运行文件和修正命令，不覆盖配置或数据。
3. 正式部署前单独备份生产数据库。
4. 部署并重启 `daily-report` 后完成 HTTP 和只读 API 健康检查。
5. 先对生产数据执行修正预演，核对两笔 ID 和金额后再正式执行。
6. 验证服务为 active、钱包 Plus 不再包含未确认申购、赎回状态正确、自动申购记录存在且排序正确。
7. 保留代码部署备份、数据库备份、修正审计记录和回滚命令。
