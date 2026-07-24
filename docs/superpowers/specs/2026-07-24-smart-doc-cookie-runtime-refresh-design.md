# 智能文档 Cookie 运行时刷新与超时分类修复设计

## 背景与根因

2026-07-23 至 2026-07-24 的生产证据如下：

1. `daily-report` 服务进程于 2026-07-23 09:55 启动。
2. 2026-07-23 18:06 企业微信扫码续期成功，18:09 日报提交成功，证明新 Cookie 当时有效。
3. 扫码子进程只把新 Cookie 写入 `config.yaml`；父进程中的 Flask 闭包配置和调度器运行时配置没有同步刷新。
4. 2026-07-24 09:45 调度器继续使用续期前的内存配置检查智能文档，得到 `Page load timeout - network issue or cookies expired`，随后按 Cookie 过期处理并生成二维码。
5. 使用与服务相同的虚拟环境重新读取磁盘 `config.yaml` 后，Cookie 校验结果为 `VALID`。

因此本次不是磁盘中的新 Cookie 一夜失效，而是续期后运行时仍使用旧 Cookie。同时，现有检查把 Playwright 页面超时与身份失效合并为同一种 `CookiesError`，导致网络异常也会被标记为 Cookie 过期。

## 目标

- 扫码续期或手工更新 Cookie 成功后，无需重启服务，新 Cookie 立即供 Flask 请求和定时调度使用。
- 页面加载或 SDK 等待超时时自动完整重试一次。
- 连续两次超时后明确报告“智能文档访问/网络异常”，不判定 Cookie 过期，不自动生成二维码。
- 明确跳转登录页、页面提示登录或无权限时，仍判定为 Cookie 失效并进入现有扫码续期流程。
- 保持日报内容生成、上一条日报兜底、OA 提交及理财看板逻辑不变。

## 非目标

- 不改变微信智能文档 Cookie 的字段集合或获取方式。
- 不增加服务自动重启。
- 不把全部配置访问改成每次从磁盘重新读取。
- 不修改生产 `config.yaml`、状态文件或 Cookie 内容。
- 本地实现阶段不部署生产服务器。

## 方案选择

采用“凭据更新事件主动同步运行时 + 超时类型化重试”。

不采用每次操作重读全部配置，因为影响路径多，且可能覆盖尚未保存的运行时调度设置。

不采用续期后重启服务，因为会中断正在执行的命令，并把配置同步问题隐藏为运维动作。

## 运行时配置刷新

在 `src/config.py` 增加一个只刷新智能文档数据源的函数：

```python
def refresh_source_config(current_cfg: Config, path: str) -> Config
```

处理规则：

1. 使用 `load_config(path)` 读取磁盘最新配置。
2. 只执行一次原子属性替换：`current_cfg.source = fresh_cfg.source`。
3. 不替换 `scheduler`、`nav_monitor`、`wechat`、`target` 等其他运行时对象。
4. 返回原来的 `current_cfg`，保证 Flask 闭包、调度器和其他持有者仍引用同一个 `Config` 实例。
5. 刷新成功后调用 `scheduler.update_runtime_config(current_cfg)`，确保 `_runtime_cfg` 指向该共享对象。
6. 读取失败时保留原运行时配置和磁盘中的新 Cookie，停止自动恢复日报，并明确提示“写盘成功但运行时同步失败，需要重启服务”，不得宣称已经完全生效。

以下凭据更新入口必须复用该函数：

- 企业微信“生成二维码”的手动扫码完成回调。
- 定时 Cookie 检查触发的扫码完成回调。
- 日报提交前检查失败后触发的扫码完成回调。
- 企业微信手工发送 Cookie 文本并通过校验的成功路径。

二维码工作进程仍只负责抓取、验证和落盘；父进程完成回调负责刷新内存，避免子进程无法修改父进程对象的问题。工作进程成功文案只说明“已验证并写入配置”，父进程刷新成功后才发送“运行时已同步、无需重启”；刷新失败则发送上述明确警告。

## 超时重试与错误分类

在 `src/cookies_checker.py` 中增加：

```python
class CookiesNetworkError(CookiesError):
    pass
```

并将一次浏览器校验提取为私有方法。公开 `check_cookies(cfg)` 保持现有签名：

1. 第一次校验成功，立即返回 `True`。
2. 第一次遇到 Playwright 页面加载或 SDK 等待超时，关闭本次浏览器，记录重试日志并重新执行完整校验。
3. 第二次仍超时，抛出 `CookiesNetworkError`。
4. 明确跳转登录页、正文包含登录提示或无权限时，直接抛出普通 `CookiesError`，不做网络重试。
5. 其他无法证明为身份失效的连接异常按 `CookiesNetworkError` 处理，避免误触发扫码。

## 调用方处理

定时检查和日报提交前检查必须先捕获 `CookiesNetworkError`，再捕获普通 `CookiesError`：

- `CookiesNetworkError`：发送“智能文档访问异常，未判定 Cookie 失效，未自动生成二维码”的通知；本次任务停止，等待下一次检查或用户重试。
- `CookiesError`：沿用现有 Cookie 失效通知和扫码续期。

在 `src/wechat_notifier.py` 与 `src/notifier.py` 增加专用网络异常通知，避免复用“Cookie 已过期”的标题。

## 测试设计

### 运行时刷新

- `refresh_source_config()` 只替换 `source`，保留原 `Config` 对象及调度、净值、企业微信配置对象。
- 手动二维码续期成功后，Flask 闭包中的 `cfg.source` 变成磁盘新值，并调用调度器更新。
- 定时检查触发的二维码续期成功后，调度器下一次检查使用新 `source`。
- 日报提交前自动续期成功后，只恢复提交一次，并使用刷新后的共享配置。
- 手工 Cookie 文本更新成功后立即刷新运行时 `source`。
- 续期失败时不刷新运行时配置。
- 磁盘写入成功但父进程刷新失败时，不恢复日报提交，并发送需要重启的警告。

### 超时分类

- 第一次超时、第二次成功：总调用两次并返回成功。
- 连续两次超时：抛出 `CookiesNetworkError`。
- 明确登录跳转：抛出普通 `CookiesError`，不执行第二次网络重试。
- 定时检查收到 `CookiesNetworkError`：发送网络异常通知，不调用二维码续期。
- 定时检查收到普通 `CookiesError`：仍调用二维码续期。
- 日报提交前收到 `CookiesNetworkError`：不生成二维码、不读取智能文档、不提交 OA。

## 验收标准

- 扫码续期完成后不重启服务，下一次 Cookie 检查使用新 Cookie。
- 模拟旧内存 Cookie、新磁盘 Cookie 时，完成回调后共享配置的 `source` 与磁盘一致。
- 连续网络超时的企微文案不包含“Cookie 已过期”，且不发送二维码。
- 真实登录失效仍能生成二维码续期。
- `tests/test_report_recovery.py`、新增 Cookie 检查测试及完整测试集全部通过。
- 本地提交不包含 `config.yaml`、Cookie、日志、截图或生产数据。
