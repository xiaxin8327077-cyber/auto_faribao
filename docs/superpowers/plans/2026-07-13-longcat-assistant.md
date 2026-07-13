# LongCat 企业微信智能助手 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为企业微信增加 LongCat 自然语言路由、多轮聊天和受限只读线上诊断，同时保持现有业务链路和定时机制不变。

**Architecture:** 现有确定性解析始终优先；仅对未知消息调用 LongCat。AI 工作与现有命令共用进程内忙碌锁，聊天和诊断异步执行，命令路由使用短超时同步完成后交回现有处理链。诊断 Agent 通过严格白名单工具读取脱敏后的状态、日志和源码。

**Tech Stack:** Python 3.12、Flask、requests、pytest、LongCat OpenAI-compatible Chat Completions

---

### Task 1: LongCat HTTP 客户端

**Files:**
- Create: `src/longcat_client.py`
- Create: `tests/test_longcat_client.py`

- [ ] 用伪造 HTTP session 编写失败测试，覆盖环境配置、Bearer 鉴权、thinking 开关、响应解析、超时、429和非法响应。
- [ ] 运行 `pytest -q tests/test_longcat_client.py`，确认测试先失败。
- [ ] 实现 `LongCatSettings.from_env()`、`LongCatClient.complete()` 和无敏感内容日志的异常类型。
- [ ] 再次运行该测试文件，确认通过。

### Task 2: 受约束指令路由与待确认

**Files:**
- Create: `src/ai_command_router.py`
- Create: `tests/test_ai_command_router.py`

- [ ] 编写路由 JSON 解析、代码围栏清理、标准指令白名单、风险等级和非法输出拒绝测试。
- [ ] 编写 AI 写操作待确认、超时清理、取消执行和不同用户隔离测试。
- [ ] 运行测试，确认缺少实现而失败。
- [ ] 实现 `AiRoute`、`AiCommandRouter`、标准指令校验器和线程安全待确认存储。
- [ ] 运行测试，确认通过。

### Task 3: 多轮聊天

**Files:**
- Create: `src/ai_chat.py`
- Create: `tests/test_ai_chat.py`

- [ ] 编写进入、退出、最多10轮、用户隔离以及模型失败不污染历史的测试。
- [ ] 运行测试，确认先失败。
- [ ] 实现线程安全 `ChatSessionStore` 和 `AiChatService`。
- [ ] 运行测试，确认通过。

### Task 4: 只读诊断工具

**Files:**
- Create: `src/ai_diagnostic_tools.py`
- Create: `tests/test_ai_diagnostic_tools.py`

- [ ] 使用临时项目和伪造命令执行器编写状态、日志、源码搜索、分段读取和脱敏测试。
- [ ] 添加路径穿越、敏感文件、超长查询、超行数和任意命令拒绝测试。
- [ ] 运行测试，确认先失败。
- [ ] 实现固定工具注册表、参数校验、项目路径白名单和统一输出截断/脱敏。
- [ ] 运行测试，确认通过。

### Task 5: 多步诊断 Agent

**Files:**
- Create: `src/ai_diagnostic_agent.py`
- Create: `tests/test_ai_diagnostic_agent.py`

- [ ] 编写工具选择、最多8次调用、30秒截止、非法工具、非法JSON和最终证据格式测试。
- [ ] 运行测试，确认先失败。
- [ ] 实现严格 JSON 的工具循环、截止时间检查和企业微信 Markdown 格式化。
- [ ] 运行测试，确认通过。

### Task 6: 企业微信入口与忙碌互斥

**Files:**
- Modify: `src/server.py`
- Modify: `tests/test_server_command_normalization.py`
- Create: `tests/test_server_ai_integration.py`

- [ ] 编写“现有指令零模型调用”“忙碌时零模型调用”“未知指令被标准化”“AI写操作需确认”“显式聊天异步”“诊断用户授权”和“诊断异步释放锁”测试。
- [ ] 运行相关测试，确认先失败。
- [ ] 在 `create_app` 中注入可替换 AI assistant；默认从环境构建，未配置时禁用。
- [ ] 在现有命令链前增加 AI 预处理，不重写已有业务处理函数。
- [ ] 复用 `_try_start_cmd/_end_cmd`，增加仅用于显示的命令名称切换，不增加第二套并发锁。
- [ ] 更新帮助导航中的聊天和诊断示例。
- [ ] 运行相关测试，确认通过。

### Task 7: 回归、安全和部署准备

**Files:**
- Modify: `README.md`

- [ ] 记录环境变量、授权方式、指令示例、数据边界和故障回退。
- [ ] 运行 `pytest -q`，要求全部通过。
- [ ] 运行 `python -m compileall -q src tests`，要求退出码为0。
- [ ] 运行 `git diff --check`，要求无空白错误。
- [ ] 检查变更中不存在API Key、Cookie、密码、私钥或线上配置值。
- [ ] 不上传服务器；等待用户明确要求部署后再执行首尔生产部署技能的预检、备份、上传和验证流程。
