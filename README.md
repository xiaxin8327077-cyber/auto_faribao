# 日报自动提交系统

## 项目概述

本系统部署在东京 AWS 服务器（13.158.229.204），实现 OA 日报的自动提取、提交与通知。系统由两个独立服务组成：

1. **daily-report**：日报自动提交核心服务，负责定时提取智能表格任务、提交日报到 OA 系统、发送邮件通知、自动更新 Cookies
2. **status-page**：服务器状态监控页面，同时集成了日报手动发送功能

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    东京 AWS 服务器                        │
│                                                         │
│  ┌──────────────────┐    ┌──────────────────────────┐   │
│  │  status-page     │    │  daily-report            │   │
│  │  (端口 80)       │    │  (端口 8080)             │   │
│  │                  │    │                          │   │
│  │  - 系统状态监控   │    │  - 定时任务调度          │   │
│  │  - 文件管理      │    │  - 智能表格提取          │   │
│  │  - Tailscale/Xray│    │  - OA 日报提交          │   │
│  │  - 日报发送(手动) │───>│  - 邮件通知             │   │
│  │  - 发送记录      │    │  - Cookies 自动更新      │   │
│  └──────────────────┘    └──────────────────────────┘   │
│                                                         │
│  systemd: status-page.service + daily-report.service    │
└─────────────────────────────────────────────────────────┘
```

## 目录结构

```
daily_report_project/
├── main.py                    # 日报主程序入口
├── submit_for_date.py         # 指定日期提交脚本（被 status-page 调用）
├── status_page.py             # 服务器状态页 + 日报发送集成
├── status_page_original.py    # 原始状态页备份
├── config.yaml                # 日报系统配置文件
├── requirements.txt           # Python 依赖
├── src/
│   ├── report_builder.py      # 日报内容构建器（智能表格 + 兜底逻辑）
│   ├── target.py              # OA 系统交互（Playwright 自动化）
│   ├── extractor.py           # 智能表格数据提取
│   ├── processor.py           # 任务数据格式化
│   ├── auth.py                # OA 系统登录（验证码识别）
│   ├── captcha.py             # 验证码识别（多策略）
│   ├── config.py              # 配置加载与数据类
│   ├── email_notifier.py      # 邮件通知（成功/失败/Cookies）
│   ├── email_reader.py        # 邮件读取（接收 Cookies 更新）
│   ├── auto_cookies_updater.py# Cookies 自动更新流程
│   ├── cookies_checker.py     # Cookies 有效性检查
│   ├── scheduler.py           # 定时任务调度器
│   ├── server.py              # Flask Web 服务（手动提交 API）
│   ├── beijing_time.py        # 北京时间工具
│   └── workday_calendar.py    # 工作日历（节假日判断）
└── systemd/
    ├── daily-report.service   # daily-report systemd 服务
    └── status-page.service    # status-page systemd 服务
```

## 核心模块详细逻辑

### 1. main.py — 主程序入口

**功能**：解析命令行参数，根据不同模式执行对应操作。

**命令行参数**：
- `--config / -c`：配置文件路径，默认 `config.yaml`
- `--test-login`：仅测试 OA 登录和验证码识别
- `--dry-run`：填写表单但不提交，截图保存到 `dry_run_form.png`
- `--extract`：从智能表格提取当日任务并打印
- `--submit`：实际提交日报到 OA 系统
- `--check-cookies`：检查智能表格 Cookies 是否有效
- `--message / -m`：日报内容（dry-run 或 submit 模式下使用）
- `--date / -d`：指定日报日期（格式 `2026-05-29`），不指定则使用当天

**运行模式**：
1. 无参数启动：启动调度器 + Flask Web 服务
2. 指定 `--test-login`：测试登录流程
3. 指定 `--extract`：测试智能表格提取
4. 指定 `--check-cookies`：检查 Cookies 有效性
5. 指定 `--submit`：提交一次日报
6. 指定 `--dry-run`：填写但不提交

### 2. submit_for_date.py — 指定日期提交脚本

**功能**：被 status-page 的 `/api/report` 接口调用，执行指定日期的日报提交。

**执行流程**：
1. 从命令行参数获取日期（可选）
2. 调用 `build_report_with_meta()` 构建日报内容（智能表格提取 → 兜底上次日报）
3. 输出元数据标记：`Report source:`、`Smart doc status:`、`Smart doc error:`
4. 输出提取内容预览：`Extracted: {前200字符}...`
5. 调用 `submit_daily_report()` 提交到 OA
6. 成功则调用 `notify_report_success()` 发送成功邮件
7. 失败则调用 `notify_report_failure()` 发送失败邮件
8. 如果内容生成阶段就失败，输出 `FAILURE_EMAIL_SENT` 标记

**关键设计**：status-page 通过解析 stdout 中的 `Extracted:` 行获取真实日报内容，通过 `FAILURE_EMAIL_SENT` 判断是否需要兜底邮件通知。

### 3. report_builder.py — 日报内容构建器

**功能**：构建日报内容，包含智能表格提取和兜底逻辑。

**类**：
- `ReportBuildError(ValueError)`：日报构建失败异常，携带 `report_source`、`smart_doc_status`、`smart_doc_error` 属性

**核心函数**：
- `build_report_content(cfg, report_date)` → `(report, source)`：简化版，只返回内容和来源
- `build_report_with_meta(cfg, report_date)` → `(report, source, meta)`：完整版，返回元数据

**构建逻辑**：
1. **首选**：从智能表格提取任务（`extract_tasks` + `format_report`）
   - 成功 → 返回 `source="smart_sheet"`，`smart_doc_status="normal"`
2. **兜底**：智能表格提取失败或无匹配数据时，读取 OA 系统中上一次日报内容（`get_previous_report_content`）
   - 成功 → 返回 `source="previous_report"`
3. **双重失败**：智能表格和上次日报都失败 → 抛出 `ReportBuildError`

**元数据字典结构**：
```python
{
    "report_source": "smart_sheet" | "previous_report",
    "smart_doc_status": "normal" | "error",
    "smart_doc_error": "错误详情或空字符串",
}
```

### 4. target.py — OA 系统交互

**功能**：通过 Playwright 浏览器自动化与 OA 系统交互。

**核心函数**：

#### `submit_daily_report(content, cfg, dry_run=False, report_date=None)`
提交日报到 OA 系统。

**流程**：
1. 规范化日期（`_normalize_report_date`）：无日期时取北京时间今天
2. 通过 API 登录 OA（`login_with_captcha`）
3. 设置 Cookie（DQMS-Token + LoginModeKey）
4. 导航到日报页面
5. **重复检查**（`_check_existing_report`）：扫描表格行，检查指定日期是否已有日报
6. 点击"添加项目日志"按钮
7. 填写表单（`_fill_report_form`）：
   - 选择项目（`_select_project`）：搜索并选择默认项目
   - 设置日期（`_set_dates`）：开始日期和结束日期都设为 report_date
   - 设置出差为"否"（`_set_travel_no`）
   - 填写日报内容（`_fill_detail`）
8. 提交对话框（`_submit_dialog`）：点击"确定"并等待对话框关闭
9. 检查是否有错误提示（`_visible_error_text`）

#### `modify_daily_report(content, cfg)`
修改今天已提交的日报。

#### `get_previous_report_content(cfg, before_date=None)`
读取 OA 系统中指定日期之前的最近一条日报内容，用于兜底提交。

**表单选择器**：
```python
FORM_SELECTORS = {
    "project_input": '.el-dialog input[placeholder="请选择项目"]',
    "date_start": '.el-dialog input[placeholder="开始日期"]',
    "date_end": '.el-dialog input[placeholder="结束日期"]',
    "hours": '.el-dialog .el-form-item:has-text("工时") input.el-input__inner',
    "detail": '.el-dialog textarea, .el-dialog .el-textarea__inner',
}
```

### 5. extractor.py — 智能表格数据提取

**功能**：通过 Playwright + JS SDK 从企业微信智能表格提取任务数据。

**提取流程**：
1. 构建文档 URL：`https://doc.weixin.qq.com/smartsheet/{doc_id}?scode={scode}&tab={tab_id}&viewId={view_id}`
2. 设置 Cookies（`.weixin.qq.com` 域名下的 13 个 Cookie 字段）
3. 打开页面，等待 DOM 加载
4. 检查是否跳转到登录页（Cookies 过期）
5. 等待 `ContainerApp.containerSdk.smartSheetSdk.editor.getCore` 可用
6. 通过 JS 代码提取数据：
   - 获取表格核心对象
   - 构建字段映射（fieldMap）和选项映射（optionMaps，用于 select 类型字段）
   - 遍历所有记录，按条件过滤：
     - **状态过滤**：`status_field` 的值包含配置的 `status_values`（如"进行中"）
     - **人员过滤**：`person_field` 的值包含配置的 `person_names`（如"夏鑫"）
   - 提取任务名称和描述，拼接为 `任务名称：任务描述` 格式
7. 返回任务列表

**Cookie 字段列表**：
`low_login_enable`, `utype`, `TOK`, `traceid`, `hashkey`, `tdoc_uid`, `wedoc_openid`, `wedoc_sid`, `wedoc_sids`, `wedoc_skey`, `wedoc_ticket`, `language`, `fingerprint`

### 6. processor.py — 任务数据格式化

**功能**：将提取的任务名称列表格式化为日报文本。

**格式化规则**：
- 每条任务编号：`1. 任务名称：任务描述`
- 任务间换行分隔
- 内容不足 20 字符时追加"完成今日常规开发与运维任务"
- 无任务时返回空字符串

### 7. auth.py — OA 系统登录

**功能**：处理 OA 系统的登录流程，包含验证码识别。

**登录流程**：
1. 调用 `/prod-api/captchaImage` 获取验证码图片（base64）和 uuid
2. 调用 `recognize_captcha()` 识别验证码
3. 调用 `/prod-api/login` 提交登录请求（username + password + code + uuid）
4. 返回 JWT Token
5. 验证码错误时自动重试，最多 3 次

### 8. captcha.py — 验证码识别

**功能**：多策略验证码识别。

**识别策略（按优先级）**：
1. **DdddOcrSolver**：使用 ddddocr 库本地识别
2. **DashScopeSolver**：使用阿里云 DashScope API（qwen3-vl-flash 视觉模型）识别
3. **TemplateCaptchaSolver**：基于模板匹配的识别（Hamming 距离）

**后处理**（`_normalize_digits`）：
- 去除非数字字符
- 字母→数字映射：O→0, I→1, B→8, S→5, Z→2 等
- 取最后 4 位数字

**失败处理**：所有策略都失败时，保存验证码图片到 `data/captcha/failed/` 目录。

### 9. email_notifier.py — 邮件通知

**功能**：发送各类通知邮件。

**邮件类型**：
1. `notify_report_success`：日报提交成功通知
   - 包含：发送时间、日报日期、日报类型（智能文档/上次日报）、智能文档读取状态、项目名称、工作时长、是否出差、日志类型、日报详情
2. `notify_report_failure`：日报提交失败通知
   - 包含：错误信息、建议手动提交
3. `notify_cookies_expired`：Cookies 过期提醒
   - 包含：错误详情、Cookies 更新步骤说明
4. `notify_cookies_valid`：Cookies 更新成功通知
   - 包含：更新字段列表
5. `notify_cookies_invalid`：Cookies 验证失败通知
   - 包含：失败原因、配置文件未被修改的说明

**邮件配置**：SMTP_SSL 连接，使用企业邮箱 `xiaxin@gbicc.net` 发送，收件人 `350006418@qq.com`。

**日报类型标签**：
- `smart_sheet` → "来自智能文档"
- `previous_report` → "来自上一次日报"
- `manual` → "手动填写"
- `generation_failed` → "未生成（智能文档和上一次日报均失败）"

### 10. email_reader.py — 邮件读取

**功能**：从邮箱读取 Cookies 更新邮件。

**读取逻辑**：
1. 通过 IMAP_SSL 连接邮箱
2. 从最新邮件开始倒序搜索
3. 匹配条件：主题包含"新cookies"，发件人包含 `350006418@qq.com`
4. 提取邮件正文（优先 text/plain，其次 text/html）
5. 读取后删除该邮件（标记 `\Deleted` + expunge）

### 11. auto_cookies_updater.py — Cookies 自动更新

**功能**：从邮件中读取新 Cookies 并更新配置文件。

**更新流程**：
1. 调用 `read_cookies_email()` 读取邮件
2. 调用 `parse_cookies_from_text()` 解析 Cookie 键值对
   - 支持格式：`TOK=xxx;`、`TOK: xxx`、`TOK xxx`
3. 比较新旧值，确定需要更新的字段
4. 备份旧值
5. 更新 `config.yaml`
6. 调用 `check_cookies()` 验证新 Cookies
7. 验证成功 → 发送 `notify_cookies_valid` 邮件
8. 验证失败 → 回滚配置 + 发送 `notify_cookies_invalid` 邮件

**Cookie 字段列表**：
`TOK`, `traceid`, `hashkey`, `tdoc_uid`, `wedoc_openid`, `wedoc_sid`, `wedoc_sids`, `wedoc_skey`, `wedoc_ticket`, `fingerprint`

### 12. cookies_checker.py — Cookies 有效性检查

**功能**：检查智能表格 Cookies 是否有效。

**检查流程**：
1. 构建文档 URL 并设置 Cookies
2. 打开页面，等待加载
3. 检查是否跳转到登录页
4. 等待 SmartSheet SDK 加载
5. 执行 JS 代码尝试获取 SDK 核心对象
6. 检查页面文本是否包含"登录"、"无权限"等关键词

### 13. scheduler.py — 定时任务调度器

**功能**：在工作日定时执行 Cookies 检查和日报自动提交。

**调度时间（北京时间）**：
- **17:00**：Cookies 有效性检查（`_run_cookies_check`）
  - 检查 Cookies 是否过期
  - 尝试从智能表格提取任务，验证读取是否正常
  - 异常时发送 Cookies 过期邮件
- **17:30**：日报自动提交（`_run_auto_submit`）
  - 调用 `auto_submit_if_needed()` 自动提交
- **每 5 分钟**：检查邮箱是否有新 Cookies 邮件（`_run_cookies_email_check`）

**工作日判断**：使用 `workday_calendar.is_workday()` 判断，支持中国法定节假日和调休。

### 14. server.py — Flask Web 服务

**功能**：提供 Web UI 和 API 接口用于手动提交日报。

**路由**：
- `GET /`：返回 Web UI 页面
- `POST /api/submit`：接收日报提交请求
  - 参数：`message`（日报内容）、`date`（日期，可选）
  - 在后台线程中执行提交

**`auto_submit_if_needed(cfg)`**：
调度器调用的自动提交函数：
1. 调用 `build_report_with_meta()` 构建日报内容
2. 调用 `submit_daily_report()` 提交
3. 根据结果发送成功/失败邮件通知

### 15. beijing_time.py — 北京时间工具

**功能**：提供北京时间相关的工具函数。

**函数**：
- `now()` → 当前北京时间 datetime
- `today()` → 当前北京时间 date
- `today_str(fmt)` → 当前北京时间格式化字符串

**时区**：UTC+8（`timezone(timedelta(hours=8))`）

### 16. workday_calendar.py — 工作日历

**功能**：判断指定日期是否为工作日，支持中国法定节假日和调休。

**数据来源**：`config/mainland_workdays.json`

**类 `WorkdayCalendar`**：
- `from_file(path)`：从 JSON 文件加载日历数据
- `is_workday(day)`：判断是否为工作日
  - 节假日列表中的日期 → 非工作日
  - 调休工作日列表中的日期 → 工作日
  - 其他：周一至周五 → 工作日，周六周日 → 非工作日
- `previous_workday(day)`：获取指定日期之前的最近工作日

**全局函数**：
- `is_workday(day)`：判断是否为工作日
- `is_today_workday()`：判断今天是否为工作日
- `get_nearest_workday(day)`：获取 <= 指定日期的最近工作日

### 17. config.py — 配置加载

**功能**：从 YAML 配置文件加载配置。

**配置类**：
- `SourceConfig`：智能表格数据源配置
  - 文档 ID、表格 ID、视图 ID
  - 字段映射：任务名称、任务描述、启动时间、预计完成时间、负责人、任务状态
  - 过滤条件：负责人姓名列表、状态值列表
  - Cookie 字段（13 个）
- `TargetConfig`：OA 目标系统配置
  - URL、用户名、密码
  - 登录路径、日报路径
  - 默认项目名称
  - 页面超时、元素超时
- `CaptchaConfig`：验证码识别配置
  - API Key、Base URL、模型名称、识别提示词
- `EmailConfig`：邮件配置
  - SMTP/IMAP 主机、端口、发件人、密码、收件人
- `Config`：顶层配置，包含以上所有子配置 + host + port

**安全检查**：配置文件权限不是 600 时输出警告。

### 18. status_page.py — 服务器状态页 + 日报发送集成

**功能**：服务器状态监控页面，同时集成了日报手动发送功能。

**技术栈**：Python 标准库 `http.server.ThreadingHTTPServer`（非 Flask）

#### 日报发送相关新增内容

**常量**：
- `REPORT_HISTORY_FILE`：发送记录存储路径 `/var/lib/status-page/report_history.json`
- `RECLOCK`：`threading.RLock()`（使用 RLock 防止死锁，因为 `save_report_record` 内部调用 `load_report_records`，两者都需要加锁）

**函数**：
- `save_report_record(record)`：保存发送记录到 JSON 文件
  - 使用 `RECLOCK` 加锁
  - 自动生成 16 位 hex ID
- `load_report_records()`：加载所有发送记录
- `parse_report_source_from_output(text)`：从子进程输出解析 `Report source:` 行
- `parse_smart_doc_meta_from_output(text)`：从子进程输出解析 `Smart doc status:` 和 `Smart doc error:` 行
- `send_report_failure_fallback(report_date, detail, ...)`：兜底邮件通知
  - 当 submit_for_date.py 子进程失败且未自行发送邮件时调用
  - 通过内联 Python 代码执行邮件发送

**API 端点**：
- `POST /api/report`：手动发送日报
  - 请求体：`{"date": "2026-05-29"}`
  - 流程：
    1. 创建 pending 状态的记录并保存
    2. 调用 `submit_for_date.py` 子进程执行提交
    3. 解析子进程输出获取结果
    4. 更新记录状态（成功/失败）
    5. 从输出中提取日报内容（`Extracted:` 行）
    6. 失败时发送兜底邮件通知
    7. 返回结果
- `GET /api/report-history`：获取发送记录列表

**前端（日报发送 Tab）**：
- 日期选择器 + 快捷日期按钮（今天/昨天/前天/三天前）
- `setReportDate(offset)`：使用本地时间设置日期（修复了 `toISOString()` UTC 时区偏移 bug）
- `sendReport()`：发送日报请求
- `loadReportHistory()`：加载并渲染发送记录表格
  - 内容列：超过 50 字符截断显示
  - 详情列：`\n` 转 `<br>`，直接显示文字（不使用 tooltip）
  - 状态列：成功绿色、失败红色、其他黄色

**访问控制**：日报发送功能仅对"完整版"角色可见。

#### 状态页原有功能

**系统监控**：
- CPU、内存、负载、磁盘、网络流量
- CPU/内存趋势图（SVG 折线图）
- 资源仪表盘（conic-gradient 环形图）
- 近七日流量柱状图

**服务管理**：
- Tailscale：状态查看 + 启停控制
- Xray：状态查看 + 启停/重启控制

**文件管理**：
- 目录浏览、搜索、上传、下载
- 图片/文本预览
- 批量删除、新建文件夹

**访问控制**：
- 双密码体系：完整版密码 + 只读版密码
- 登录频率限制（1 分钟 12 次，10 分钟 6 次失败锁定）
- Session 管理（12 小时有效期）

**数据持久化**：
- `/var/lib/status-page/traffic_state.json`：流量统计
- `/var/lib/status-page/metric_history.json`：指标历史
- `/var/lib/status-page/auth_config.json`：密码配置
- `/var/lib/status-page/report_history.json`：日报发送记录
- `/var/lib/status-page/uploads/`：上传文件目录

## 定时任务时间线（北京时间，工作日）

```
17:00  Cookies 有效性检查（scheduler.py → cookies_checker.py）
       ├── Cookies 有效 → 尝试提取智能表格任务验证
       └── Cookies 过期 → 发送过期提醒邮件

17:00-17:30  每 5 分钟检查邮箱是否有新 Cookies 邮件
             ├── 有新邮件 → 解析 Cookies → 更新 config.yaml → 验证
             │   ├── 验证通过 → 发送成功邮件
             │   └── 验证失败 → 回滚配置 + 发送失败邮件
             └── 无新邮件 → 跳过

17:30  日报自动提交（scheduler.py → server.py → report_builder.py → target.py）
       ├── 智能表格提取成功 → 提交日报 → 发送成功邮件
       ├── 智能表格提取失败 → 读取上次日报 → 提交 → 发送成功邮件
       └── 两者都失败 → 发送失败邮件
```

## 手动发送日报流程（通过 status-page）

```
用户访问 http://13.158.229.204 → 登录（完整版密码）
→ 点击"日报发送"菜单 → 选择日期 → 点击"发送日报"
→ status-page 调用 submit_for_date.py 子进程
→ 智能表格提取 → OA 提交 → 邮件通知
→ 页面显示发送结果 + 更新历史记录
```

## systemd 服务配置

### daily-report.service
- **工作目录**：`/home/ubuntu/daily_report`
- **执行命令**：`/home/ubuntu/daily_report/venv/bin/python /home/ubuntu/daily_report/main.py`
- **日志**：`/home/ubuntu/daily_report/service.log`
- **自动重启**：30 秒后重启

### status-page.service
- **工作目录**：`/opt/status-page`
- **执行命令**：`/usr/bin/python3 /opt/status-page/status_page.py`
- **端口**：80
- **自动重启**：3 秒后重启
- **环境变量**：
  - `STATUS_PAGE_HOST=0.0.0.0`
  - `STATUS_PAGE_PORT=80`
  - `STATUS_PAGE_REFRESH=8`
  - `STATUS_PAGE_SAMPLE_INTERVAL=60`
  - `STATUS_PAGE_STATE_DIR=/var/lib/status-page`

## 关键 Bug 修复记录

### 1. 死锁问题（threading.Lock → RLock）
- **问题**：`save_report_record` 内部调用 `load_report_records`，两者都使用 `with RECLOCK`（普通 Lock），导致同一线程重复获取锁，产生死锁，整个服务卡死
- **修复**：`threading.Lock()` → `threading.RLock()`（可重入锁）

### 2. JS 时区偏移
- **问题**：`toISOString().split('T')[0]` 返回 UTC 日期，北京时间晚上 8 点后"今天"会显示为"昨天"
- **修复**：改用 `getFullYear()` + `getMonth()` + `getDate()` 拼接本地日期

### 3. 页面全部"加载中"
- **问题**：在 `bindTabMenu()` 后直接调用 `setReportDate(0)`，该函数在定义之前执行，导致 JS 整体崩溃
- **修复**：仅在切换到日报 Tab 时调用 `setReportDate(0)`

### 4. 历史记录内容显示
- **问题**：内容列显示"(自动从智能表格提取)"而非真实内容
- **修复**：从 submit_for_date.py 的 stdout 解析 `Extracted:` 行获取真实内容

### 5. 失败详情显示
- **问题**：详情列使用 tooltip/overflow:hidden 显示，用户无法直接看到
- **修复**：去掉 `overflow:hidden` + `title` 属性，改为 `word-break:break-word` 直接显示文字，`\n` 转 `<br>`

## 依赖

```
flask>=3.0
requests>=2.28
pyyaml>=6.0
playwright>=1.40
openai>=1.0
pytest>=7.0
```

额外运行时依赖（可选）：
- `ddddocr`：本地验证码识别
- `Pillow`：模板验证码识别
- `zoneinfo`（Python 3.9+ 内置）：时区支持
