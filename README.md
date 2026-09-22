# daily_report

生产部署目录：`/home/ubuntu/daily_report`  
服务器：阿里云 `8.213.145.226`  
仓库：`git@gitee.com:xiaxin8327077-cyber/daily_report.git`（默认分支 **master**）

这台机器上的个人自动化服务。名字还叫日报，实际包含三块：

1. **OA 日报**：从企业微信智能表格抽任务，用 Playwright 提交到 OA，结果走企业微信
2. **理财净值 / 组合账本**：拉取信银、南银等净值，SQLite 记账，看板在 `/nav`
3. **企业微信控制台 + AI**：文字指令操作日报/净值/运维；未命中指令时走 LongCat 助手

通知只走企业微信，不再发邮件。

## 架构

```
阿里云 /home/ubuntu/daily_report
└── daily-report.service     端口 8080（当前唯一在跑的服务）
    ├── Flask：Web UI / 企微回调 / 净值看板 / 组合 API
    ├── 调度器：日报、Cookies、净值推送、账本同步
    └── Playwright：OA 登录提交、智能表格提取（浏览器互斥锁）
```

仓库里还有一份 `status_page.py`，只是历史副本。本机没有 `status-page.service`，`/opt/status-page` 也不存在，80 端口未监听。运维看服务器状态改走企业微信指令。

无参数启动 `main.py` 会同时拉起调度器和 Flask。systemd 工作目录就是本路径：

```
/home/ubuntu/daily_report/venv/bin/python /home/ubuntu/daily_report/main.py
```

日志：`service.log`、`daily_send.log`。

## 当前定时（北京时间，以 config.yaml 为准）

| 时间 | 条件 | 任务 |
|------|------|------|
| 08:08 | 工作日 | 净值早间推送 |
| 09:45 | 工作日 | 智能表格 Cookies 检查 |
| 17:30 | 工作日 | 净值预估 |
| 17:56 | 工作日 | 自动提交日报 |
| 18:10 | 周日或月末 | 日报周报/月报 |
| 23:30 | 工作日 | 净值晚间检查 |
| 约每 30 分钟 | 持续 | 组合账本：行情 / 在途确认 / 现金收益 |
| 每月 1 日 04:00 | — | 缓存清理 |
| 每年 12 月 1 日 | — | 更新下一年工作日历 |

可用企微指令改调度时间，写入 `config.yaml` 后立即生效。

日报内容：先读智能表格（负责人「夏鑫」、状态「进行中」）；失败则沿用 OA 上一条；Cookies 过期会在企微里确认是否继续。

## HTTP

| 路径 | 说明 |
|------|------|
| `GET /` | 日报手动提交页 |
| `POST /api/submit` | 提交日报 |
| `GET /nav` | 理财看板（需访问令牌） |
| `GET /api/nav-dashboard` | 看板数据 |
| `POST /api/nav-dashboard/refresh` | 手动刷新看板（有冷却） |
| `/api/portfolio/*` | 组合产品 / 交易 / 持仓 / 定投 |
| `GET|POST /api/wechat/callback` | 企业微信回调 |

## 企业微信

在应用里发文字即可。常见分组：

- **日报**：发送日报、根据前一天内容发送、今日状态、本周/本月统计、草稿与修改、撤回
- **净值**：立即查询净值、按日期/周期查询、持仓画像；交易操作走 `/nav` 网页
- **系统**：查看配置、改定时、检查/更新 Cookies、扫码续期
- **运维**：服务器状态、清理缓存、查看日志、重启服务
- **帮助**：`指令` / `帮助` / 数字菜单 `0`–`5`
- **AI**：未命中固定指令时，由 LongCat 聊天或诊断路由处理

直接发送 Cookies 字符串会校验并写入配置；失败回滚。

## 目录

```
/home/ubuntu/daily_report
├── main.py                 入口：调度器 + Flask
├── submit_for_date.py      指定日期提交（命令行）
├── status_page.py          旧状态页源码（服务已下线，未再部署）
├── config.yaml             运行配置（含密钥，权限应为 600，勿提交）
├── requirements.txt
├── config/mainland_workdays.json
├── data/                   运行时：portfolio.db、看板状态、令牌（勿提交）
├── src/
│   ├── 日报：report_builder / extractor / processor / target
│   │         daily_report_draft / daily_report_edit_confirmation
│   ├── 登录：auth / captcha / captcha_worker / qr_login_renewer
│   │         cookies_checker / auto_cookies_updater
│   ├── 调度通知：scheduler / server / notifier / wechat_*
│   │            browser_lock / pending_confirmation
│   ├── 净值：nav_monitor / nav_dashboard / nav_holdings / nav_report_image
│   ├── 账本：portfolio_*（SQLite schema v7）
│   └── AI：ai_assistant / ai_chat / ai_command_router
│          ai_diagnostic_* / longcat_client
└── tests/
```

本地忽略：`venv/`、`backups/`、`runtime/`、日志、`config.yaml`、`data/`。仓库历史里可能仍有旧副本，新增密钥不要再提交。

## 命令

```bash
source venv/bin/activate
python main.py                  # 生产模式：调度器 + Web
python main.py --extract        # 只抽智能表格
python main.py --check-cookies
python main.py --dry-run -m "..." -d 2026-08-20
python main.py --submit -d 2026-08-20
```

OA 验证码：ddddocr 子进程 → 阿里云视觉模型 → 模板匹配。浏览器任务串行，避免和 Cookies 检查抢 Chromium。

## systemd

**daily-report.service**（本机唯一相关服务）

- 工作目录：`/home/ubuntu/daily_report`
- 命令：`venv/bin/python main.py`
- 失败 30 秒后重启

`status-page.service` 已不存在，无需启动。

## 依赖

```
flask>=3.0
requests>=2.28
pyyaml>=6.0
playwright>=1.40
openai>=1.0
pytest>=7.0
pycryptodome>=3.20
pillow>=10.0
pypdf>=5.0
```

可选：`ddddocr`（本地验证码，用完即释放）。
