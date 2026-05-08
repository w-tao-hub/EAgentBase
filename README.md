# Agent Framework

## 项目定位

本项目是一个面向后续多智能体演进的通用智能体骨架，目前已落地单主智能体 + 子代理 Task 派发的最小可运行闭环。

当前实现以以下设计文档为依据，并结合现有代码形成实际工程结构：

- `general-agent.md`：通用智能体骨架总设计。
- `docs/superpowers/specs/2026-04-30-multi-agent-task-design.md`：多智能体 Task 派发设计。
- `docs/superpowers/specs/2026-04-30-session-storage-multi-agent-design.md`：多智能体会话存储设计。
- `docs/superpowers/specs/2026-05-06-multi-agent-context-summary-refactor-design.md`：多智能体上下文摘要重构设计。
- `docs/superpowers/plans/2026-04-30-multi-agent-task-implementation.md`：多智能体实现计划。
- `docs/superpowers/plans/2026-05-06-multi-agent-context-summary-refactor-implementation.md`：上下文摘要重构实施计划。

当前能力边界：

- 已实现：`POST /sessions`、`GET /sessions/{session_id}`、`POST /chat`、`POST /runs/{run_id}/cancel`、`GET /runs/{run_id}`、SSE 流式输出、Redis 会话持久化、会话单活跃运行锁与心跳、健康检查、日志系统、启动脚本、Tool 系统（Task 子代理派发、Task CRUD、Skill、MCP 适配、大结果持久化与查询、Python 脚本执行）、Agent Loop 多轮编排、Hook 系统、子代理执行服务（ChildAgentRunner）、子代理配置加载（默认 Plan + 自定义 md）、子代理上下文隔离、取消控制（API 取消 + SSE 断链取消 + 跨进程 Redis 广播取消）、上下文摘要压缩、用户消息元数据。

## 目录与文件职责

### 根目录

```text
.
├── AGENTS.md                                          Codex 代理协作约束、执行原则与"教训"记录。
├── README.md                                          项目整体架构说明、目录职责与运行入口说明。
├── .claude/                                           Claude 侧代理规范目录。
│   ├── CLAUDE.md                                      Claude 侧工作约束、教训记录入口。
│   └── code_rule.md                                   Claude 侧开发规范文档。
├── .codex/                                            Codex 侧规则与技能目录。
│   ├── code_rule.md                                   Codex 侧开发规范文档，内容与 Claude 规范保持一致。
│   └── skills/                                        本地 Codex skills 目录。
├── .env                                               本地开发实际环境变量文件。
├── .env.example                                       项目环境变量模板。
├── .gitignore                                         Git 忽略规则。
├── agents/                                            自定义子代理 md 配置目录；目录不存在时按空目录处理。
├── app/                                               主应用源码目录。
├── docs/                                              设计文档与执行计划目录。
├── general-agent.md                                   通用智能体骨架的总设计文档，是 v0 架构来源之一。
├── mcp_servers.json                                   MCP 服务配置文件，声明多个 MCP server。
├── mcp_servers.json.example                           MCP 服务配置模板。
├── pyproject.toml                                     Python 项目元数据、依赖列表与 pytest 配置入口。
├── skills/                                            根级 Skills 目录，支持按名称注入技能文档。
├── start.py                                           项目统一启动脚本，负责加载配置、初始化日志并启动 Uvicorn。
└── tests/                                             单元测试与集成测试目录。
```

### `app/`

项目主干分层：`interfaces/http → services → core/runtime → infra`

```text
app/
├── __init__.py                                        应用包声明。
├── main.py                                            FastAPI 应用装配器；注册中间件、异常处理器、路由和依赖容器。
├── config.py                                          `Settings` 定义；统一收口运行、Redis、日志、CORS、Uvicorn、主智能体配置。
├── bootstrap/                                         组合根目录，负责依赖装配。
│   ├── __init__.py                                    包标记。
│   ├── container.py                                   唯一组合根；装配 Redis store、AgentProvider、服务层与运行时依赖。
│   └── factory.py                                     公开无参启动入口，支持 `uvicorn app.bootstrap.factory:bootstrap_app --factory`。
├── core/                                              核心领域与运行时目录。
│   ├── __init__.py                                    核心层包标记。
│   ├── hooks/                                         Hook 扩展目录。
│   │   ├── __init__.py                                统一导出 Hook 抽象、管线与守卫。
│   │   ├── base.py                                    `ModelHook`、`ToolHook` 抽象基类定义。
│   │   ├── errors.py                                  Hook 执行异常定义。
│   │   ├── guard.py                                   `StreamTextGuard` 与 `NoOpStreamTextGuard` 定义。
│   │   ├── persist_large_tool_result_hook.py          大工具结果持久化 Hook，将超大输出写入独立 Redis key。
│   │   ├── pipeline.py                                模型 Hook 与工具 Hook 的串行执行管线实现。
│   │   └── types.py                                   Hook 请求/响应载体定义。
│   ├── models/                                        领域模型、错误模型、事件模型目录。
│   │   ├── __init__.py                                统一导出 Agent、Session、Run、Event、Error 等公共模型。
│   │   ├── agent.py                                   `Agent` 领域模型与 `AgentExecutionProfile` 执行配置。
│   │   ├── error.py                                   `ErrorCode` 与 `AppError` 定义。
│   │   ├── event.py                                   SSE/运行链路中的事件模型定义。
│   │   ├── execution_context.py                       `ExecutionContext`，透传给 Hook 和 Tool 的运行上下文。
│   │   ├── llm_chunk.py                               `LLMChunk` 模型。
│   │   ├── run.py                                     `Run` 与 `RunStatus` 模型。
│   │   ├── session.py                                 `Session` 领域模型。
│   │   ├── stored_message.py                          统一消息模型，附带 `_meta` 元数据。
│   │   ├── task.py                                    `Task` 领域模型，用于 Task CRUD。
│   │   └── tool.py                                    `Tool` ABC、`ToolResult`、`ToolRegistry` 定义。
│   ├── loop/                                          Agent Loop 多轮循环编排目录。
│   │   ├── __init__.py                                导出 `AgentLoop`。
│   │   └── agent_loop.py                              Agent Loop 实现；多轮 LLM 调用与工具执行编排，含取消检测点。
│   └── runtime/                                       运行时核心目录。
│       ├── __init__.py                                导出 `ContextBuilder` 等。
│       ├── agent_runtime.py                           单次 Run 执行内核；`stream_once()` 负责单次 LLM 调用。
│       ├── context_builder.py                         构建 `system + history + current user` 的上下文消息序列。
│       ├── context_history_view.py                    历史消息视图，用于摘要压缩前的范围计算。
│       ├── context_summary_persistence.py             上下文摘要持久化与上下文摘要边界状态管理。
│       └── context_summary_planner.py                 上下文摘要压缩规划，决定哪些历史需要摘要化。
├── infra/                                             外部依赖适配层目录。
│   ├── agents/                                        智能体配置加载实现目录。
│   │   ├── __init__.py                                导出 `MasterAgentProvider`。
│   │   ├── master_agent_provider.py                   主智能体提供者。
│   │   ├── master_prompt.md                           主智能体系统提示词文件。
│   │   ├── default_sub_agents/                        默认子代理配置与 prompt 资源目录。
│   │   │   ├── __init__.py                            包标记。
│   │   │   ├── definitions.py                         默认子代理 Python 声明式配置。
│   │   │   └── plan.md                                默认 Plan 子代理系统提示词。
│   │   ├── custom_sub_agent_loader.py                 自定义 md 子代理加载器。
│   │   ├── hook_profiles.py                           子代理可引用的预注册 Hook 组。
│   │   └── profile_builder.py                         将默认/自定义配置组装成 AgentExecutionProfile。
│   ├── llm/                                           大模型调用适配目录。
│   │   ├── __init__.py                                包标记。
│   │   └── litellm_adapter.py                         LiteLLM 适配器；流式调用和 chunk 归一化，支持 tools 参数。
│   ├── logging/                                       日志系统目录。
│   │   ├── __init__.py                                导出 `setup_logging` 和 `get_logger`。
│   │   └── logger_manager.py                          日志管理器；按天+按大小轮转、单例初始化。
│   ├── skills/                                        技能索引目录。
│   │   ├── __init__.py                                包标记。
│   │   └── catalog.py                                 Skill 文档扫描与索引；支持按名称注入模型上下文。
│   ├── tools/                                         Tool 实现目录。
│   │   ├── __init__.py                                包标记。
│   │   ├── mcp_adapter.py                             MCP 协议适配器。
│   │   ├── mcp_client_manager.py                      MCP 客户端生命周期管理器。
│   │   ├── task_tool.py                               Task 派发工具，master 通过此工具同步派发子代理。
│   │   ├── task_create_tool.py                        任务创建工具。
│   │   ├── task_get_tool.py                           任务查询工具。
│   │   ├── task_list_tool.py                          任务列表工具。
│   │   ├── task_update_tool.py                        任务更新工具。
│   │   ├── skill_tool.py                              Skill 加载工具。
│   │   ├── query_tool_result_tool.py                  大工具结果分页查询工具。
│   │   └── run_python_script_tool.py                  项目内 Python 脚本执行工具。
│   └── store/                                         Redis 持久化适配目录。
│       ├── __init__.py                                统一导出全部 Store。
│       ├── redis_lock_store.py                        会话分布式锁实现（SET NX EX + 心跳续期）。
│       ├── redis_run_store.py                         Run 持久化。
│       ├── redis_session_store.py                     会话元数据、主/child 上下文消息、session 索引持久化。
│       ├── redis_task_store.py                        Task 持久化。
│       └── redis_tool_result_store.py                 大工具结果持久化。
├── interfaces/                                        接口适配层目录。
│   ├── __init__.py                                    包标记。
│   └── http/                                          HTTP 接入层目录。
│       ├── __init__.py                                包标记。
│       ├── dependencies.py                            依赖解析函数，Route 通过窄依赖获取 Service。
│       ├── exception_handlers.py                      全局 HTTP 异常处理器。
│       ├── sse.py                                     把内部事件流编码成 SSE 文本流。
│       ├── routes/                                    FastAPI 路由目录。
│       │   ├── __init__.py                            包标记。
│       │   ├── chat.py                                `POST /chat` 路由，SSE 流式响应 + SSE 断链取消。
│       │   ├── health.py                              `/health/ready`、`/health/live` 路由。
│       │   ├── runs.py                                `GET /runs/{run_id}`、`POST /runs/{run_id}/cancel` 路由。
│       │   └── sessions.py                            `POST /sessions`、`GET /sessions/{session_id}` 路由。
│       └── schemas/                                   HTTP 请求/响应模型目录。
│           ├── __init__.py                            包标记。
│           ├── chat.py                                `POST /chat` 请求体模型。
│           ├── common.py                              通用响应模型。
│           ├── run.py                                 `GetRunResponse`、`CancelRunResponse`。
│           └── session.py                             会话创建/查询响应模型。
└── services/                                          业务服务层目录。
    ├── __init__.py                                    包标记。
    ├── agent_provider.py                              `AgentProvider` 协议。
    ├── chat_event_processor.py                        聊天事件分发器，负责事件落库和 Task 结果回填标记。
    ├── chat_run_lock.py                               聊天运行锁作用域（ChatRunLockScope），含心跳续期。
    ├── chat_service.py                                聊天主链路编排服务；会话校验、锁、Run、上下文、终态持久化、取消监听。
    ├── child_agent_runner.py                          子代理执行服务；管理 child run、上下文隔离、取消传播。
    ├── run_control_service.py                         Run 查询与取消控制。
    ├── session_cleanup_service.py                     会话级联删除服务。
    ├── session_service.py                             会话创建、查询视图、消息计数。
    └── task_service.py                                Task CRUD 业务服务。
```

### `docs/`

```text
docs/
├── claude-code-learned.md                             Claude Code 使用经验记录。
├── claude-code-work/                                  Claude Code 工作模式文档系列。
│   ├── 01-overview.md
│   ├── 02-agent-loop.md
│   ├── ...
│   └── reference.md
└── superpowers/                                       superpowers 工作流文档。
    ├── plans/                                         实施计划。
    │   ├── 2026-04-30-multi-agent-task-implementation.md
    │   ├── 2026-04-30-session-storage-refactor.md
    │   └── 2026-05-06-multi-agent-context-summary-refactor-implementation.md
    └── specs/                                         设计规格。
        ├── 2026-04-30-multi-agent-task-design.md
        ├── 2026-04-30-session-storage-multi-agent-design.md
        └── 2026-05-06-multi-agent-context-summary-refactor-design.md
```

### `tests/`

```text
tests/
├── conftest.py                                        pytest 共享夹具，提供 fakeredis 实例。
├── fakes.py                                           测试替身集合（FakeLLMAdapter、FakeAgentRuntime、FakeTool）。
├── helpers/                                           测试辅助工具。
│   └── sse.py                                         SSE 解析辅助。
├── integration/                                       集成测试。
│   ├── test_multi_agent_task_flow.py                  多智能体 Task 派发主链路。
│   ├── test_task_flow.py                              Task CRUD 完整流程。
│   ├── http/                                          HTTP 接口集成测试。
│   │   ├── test_chat_api.py                           /chat SSE 行为测试。
│   │   ├── test_health_api.py                         健康检查接口测试。
│   │   ├── test_runs_api.py                           /runs 接口测试。
│   │   └── test_sessions_api.py                       /sessions 接口测试。
│   └── mcp/                                           MCP 集成测试。
│       └── test_mcp_real_server_smoke.py              MCP 真实服务冒烟测试。
├── curl_test/                                          curl 端到端测试。
│   └── test_cancel.sh                                 取消功能端到端测试。
└── unit/                                              单元测试。
    ├── test_config.py                                 配置读取测试。
    ├── test_logging_boundary.py                       日志边界测试。
    ├── test_main.py                                   main.py 装配测试。
    ├── bootstrap/
    │   └── test_container.py                          容器装配测试。
    ├── core/
    │   ├── loop/test_agent_loop.py                    AgentLoop 测试。
    │   ├── models/
    │   │   ├── test_agent_profile.py                  Agent 与 Profile 模型测试。
    │   │   ├── test_event.py                          事件序列化测试。
    │   │   ├── test_models.py                         模型测试。
    │   │   └── test_tool.py                           ToolRegistry 测试。
    │   └── runtime/
    │       ├── test_agent_runtime.py                  AgentRuntime 测试。
    │       ├── test_context_builder.py                上下文构建测试。
    │       ├── test_context_history_view.py           历史视图测试。
    │       ├── test_context_summary_persistence.py    摘要持久化测试。
    │       └── test_context_summary_planner.py        摘要规划测试。
    ├── hooks/
    │   └── test_persist_large_tool_result_hook.py     大结果持久化 Hook 测试。
    ├── infra/
    │   ├── agents/
    │   │   ├── test_master_agent_provider.py          主智能体提供者测试。
    │   │   └── test_sub_agent_profiles.py             子代理 profile 测试。
    │   ├── llm/test_litellm_adapter.py                LiteLLM 适配器测试。
    │   ├── logging/test_logger_manager.py             日志管理器测试。
    │   ├── tools/
    │   │   ├── test_mcp_adapter.py                    MCP 适配器测试。
    │   │   ├── test_mcp_client_manager.py             MCP 管理器测试。
    │   │   ├── test_python_script_tool.py             Python 脚本工具测试。
    │   │   ├── test_query_tool_result_tool.py         大结果查询工具测试。
    │   │   ├── test_skill_tool.py                     Skill 工具测试。
    │   │   ├── test_task_tool.py                      TaskTool 测试。
    │   │   └── test_task_tools.py                     Task CRUD 工具测试。
    │   └── store/
    │       ├── test_redis_lock_store.py               锁存储测试。
    │       ├── test_redis_run_store.py                Run 存储测试。
    │       ├── test_redis_session_store.py            会话存储测试。
    │       ├── test_redis_task_store.py               Task 存储测试。
    │       └── test_redis_tool_result_store.py        大结果存储测试。
    ├── interfaces/http/routes/
    │   └── test_chat_route.py                         聊天路由测试。
    └── services/
        ├── test_chat_event_processor.py               事件分发器测试。
        ├── test_chat_run_lock.py                      运行锁测试。
        ├── test_chat_service.py                       聊天服务测试。
        ├── test_child_agent_runner.py                 子代理执行服务测试。
        ├── test_run_control_service.py                运行控制服务测试。
        ├── test_session_service.py                    会话服务测试。
        └── test_task_service.py                       Task 服务测试。
```

## 关键调用链

### 1. 创建会话

```text
POST /sessions
-> app/interfaces/http/routes/sessions.py
-> app/interfaces/http/dependencies.py
-> SessionService.create_session()
-> MasterAgentProvider.get_default()
-> RedisSessionStore.create_session()
```

产物是一个绑定主智能体的 `Session` 元数据记录。

### 2. 发起聊天（含取消链路）

```text
POST /chat
-> app/interfaces/http/routes/chat.py
   -> 创建 cancel_event + 启动 SSE 断链监控
   -> encode_sse(event_iterator)
-> app/interfaces/http/dependencies.py
-> ChatService.stream_chat()
   -> ChatRunLockScope.acquire()                    # 会话锁 + 心跳续期
   -> RedisRunStore.create_run()                    # Run 建档
   -> RedisSessionStore.append_main_message(user)
   -> ContextBuilder.build_llm_messages()            # 构建上下文
   -> AgentLoop.run()                               # 多轮循环编排
     -> AgentRuntime.stream_once()                  # 单次 LLM 调用
       -> LiteLLMAdapter.stream_completion()         # 流式调用 LLM
         cancel_event check                         # ← 取消检测点：chunk 输出中
       cancel_event check                           # ← 取消检测点：LLM 调用前
     -> ToolRegistry.get() / tool.call()            # 工具查找与执行
       cancel_event check                           # ← 取消检测点：工具执行前
     -> ToolUseStartedEvent / ToolUseCompletedEvent  # 工具事件
     -> (循环继续直到无 tool_calls 或达到 max_turns)
   -> _persist_terminal_state()                     # 终态持久化
   -> ChatRunLockScope.release()                    # 释放锁
-> event_iterator (async generator)
   -> RunStartedEvent / MessageDeltaEvent / ToolUseStartedEvent / ToolUseCompletedEvent
   -> RunCompletedEvent / RunFailedEvent / RunCancelledEvent
-> encode_sse(event_iterator)
-> _wrapped_sse_stream()                            # 含 SSE 异常兜底
-> StreamingResponse

取消触发：
- POST /runs/{run_id}/cancel
  -> RunControlService.cancel_run()
  -> ChatService.cancel_run()
  -> cancel_event.set() 或 Redis PUBLISH run_cancel:{run_id}
- SSE 断链
  -> _disconnect_monitor() 检测 http.disconnect
  -> cancel_event.set()
- Redis run_cancel 广播（跨进程）
  -> _listen_cancel_messages() 收到 pmessage
  -> cancel_event.set()
```

### 3. 查询会话 / 运行 / 取消运行

```text
GET /sessions/{session_id}
-> app/interfaces/http/routes/sessions.py
-> SessionService.get_session_view()
-> RedisSessionStore + RedisLockStore

GET /runs/{run_id}
-> app/interfaces/http/routes/runs.py
-> RunControlService.get_run()
-> RedisRunStore.get_run()

POST /runs/{run_id}/cancel
-> app/interfaces/http/routes/runs.py
-> RunControlService.cancel_run()
-> ChatService.cancel_run()                         # 本地或广播取消
```

### 4. 健康检查

```text
GET /health/ready
-> app/interfaces/http/routes/health.py
-> Container.ping_readiness()

GET /health/live
-> app/interfaces/http/routes/health.py
```

### 5. 启动链

```text
start.py
-> app/bootstrap/factory.py (load_settings / initialize_runtime)
   -> app/config.py (Settings)
   -> app/infra/logging/logger_manager.py
-> app/bootstrap/factory.py (bootstrap_app)
   -> app/bootstrap/container.py (Container.create)
   -> app/main.py (create_app)

或 uvicorn 工厂模式：
uvicorn app.bootstrap.factory:bootstrap_app --factory
```

### 6. 子代理派发链

```text
AgentLoop 工具执行阶段
-> TaskTool.call()
-> ChildAgentRunner.run_child()
   -> 创建 child Run（run_type=child, parent_run_id, child_id）
   -> 构建 child 上下文（从 child_context_messages 读取历史）
   -> AgentLoop.run() (child profile)
     -> ContextBuilder.build (child 上下文隔离)
   -> 结果写入 child_context_messages
   -> 更新 child run 终态
-> 返回 ToolResult 给 master
```

### 7. 文件之间的关键关系

```text
主智能体配置链:
app/config.py(.env) + app/infra/agents/master_prompt.md
-> app/infra/agents/master_agent_provider.py
-> app/services/agent_provider.py
-> SessionService / ChatService

持久化链:
SessionService / ChatService / RunControlService
-> RedisSessionStore / RedisRunStore / RedisLockStore / RedisTaskStore / RedisToolResultStore
-> Redis

启动链:
start.py / uvicorn --factory
-> app/bootstrap/factory.py
-> app/config.py + app/infra/logging/logger_manager.py
-> app/bootstrap/container.py
-> app/main.py
```
