# E Agent Base

## 项目定位

企业可用的最小可快速搭建的通用智能体基座，提供基础骨架，企业可根据业务自行扩展。

> **面向开发者**：本项目不是开箱即用的产品，需要具备 Python 编程基础。使用者应能理解 FastAPI、Redis、异步编程等概念，并根据业务需求进行定制开发。

当前能力边界：

- 已实现：`POST /sessions`、`GET /sessions/{session_id}`、`POST /chat`、`POST /runs/{run_id}/cancel`、`GET /runs/{run_id}`、SSE 流式输出、Redis 会话持久化（单点与 Sentinel 双模式）、会话单活跃运行锁与心跳、健康检查、日志系统、启动脚本、Tool 系统（Task 子代理派发、Plan CRUD、Skill、MCP 适配、大结果持久化与查询、Python 脚本执行）、Agent Loop 多轮编排、Hook 系统、子代理执行服务（ChildAgentRunner，支持动态工具注入）、子代理配置加载（默认 Worker + 自定义 md）、子代理上下文隔离与 session 级命名空间隔离（plan/task）、可恢复子代理列表查询（ListResumableSubagents）、取消控制（API 取消 + SSE 断链取消 + 跨进程 Redis 广播取消）、上下文摘要压缩、用户消息元数据、Store 抽象端口层（企业可替换存储后端，默认 Redis 适配器）、多主代理静态注册（default/plan）、/sessions 主代理绑定、/chat 按 master_agent_name 显式路由、子代理按主代理挂载可见。

- 通用智能体（Worker）：默认子代理。支持主代理动态指定可用工具列表，可执行包括所有工具在内的多样化任务。采用简化提示词设计，无领域约束，适配各类开发场景。支持 resume 恢复执行，支持 session 级命名空间隔离。可自行修改 Worker 智能体除工具列表外的其余参数。

## 目录与文件职责

### 根目录

```text
.
├── README.md                                          项目整体架构说明、目录职责与运行入口说明。
├── .env                                               本地开发实际环境变量文件。
├── .env.example                                       项目环境变量模板。
├── .gitignore                                         Git 忽略规则。
├── agents/                                            自定义子代理 md 配置目录（如 Echo.md），目录不存在时按空目录处理。
├── app/                                               主应用源码目录。
├── mcp_servers.json                                   MCP 服务配置文件，声明多个 MCP server。
├── mcp_servers.json.example                           MCP 服务配置模板。
├── pyproject.toml                                     Python 项目元数据、依赖列表与 pytest 配置入口。
├── skills/                                            根级 Skills 目录，每个子目录为一个 Skill（含 SKILL.md）。
├── start.py                                           项目统一启动脚本，负责加载配置、初始化日志并启动 Uvicorn。
└── tests/                                             单元测试与集成测试目录。
```

### `app/`

项目主干分层：`interfaces/http → services → core/runtime → infra`

```text
app/
├── __init__.py                                        应用包声明。
├── main.py                                            FastAPI 应用装配器；注册中间件、异常处理器、路由和依赖容器。
├── config.py                                          `Settings` 定义；统一收口运行、Redis、日志、CORS、Uvicorn、模型参数等配置。
├── bootstrap/                                         组合根目录，负责依赖装配。
│   ├── __init__.py                                    包标记。
│   ├── container.py                                   唯一组合根；按模式装配 Redis（单点/Sentinel）、Store、StoreTransaction、RunCancelBus、AgentProvider、服务层、工具与运行时依赖。
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
│   │   ├── execution_context.py                       `ExecutionContext`，透传给 Hook/Tool 的运行上下文，含 child_id 与 plan 隔离命名空间。
│   │   ├── llm_chunk.py                               `LLMChunk` 模型。
│   │   ├── run.py                                     `Run` 与 `RunStatus` 模型。
│   │   ├── session.py                                 `Session` 领域模型。
│   │   ├── stored_message.py                          统一消息模型，附带 `_meta` 元数据。
│   │   ├── task.py                                    `Task` 领域模型，用于 Task CRUD。
│   │   └── tool.py                                    `Tool` ABC、`ToolResult`、`ToolRegistry` 定义。
│   ├── loop/                                          Agent Loop 多轮循环编排目录。
│   │   ├── __init__.py                                导出 `AgentLoop`。
│   │   └── agent_loop.py                              Agent Loop 实现；多轮 LLM 调用与工具执行编排，含取消检测点。
│   ├── ports/                                         核心存储端口目录（企业可替换存储后端的抽象接入口）。
│   │   ├── __init__.py                                统一导出全部 Store Protocol、事务端口与取消广播端口。
│   │   ├── stores.py                                  存储端口定义：SessionStore、RunStore、TaskStore、LockStore、ToolResultStore 及 DTO（ContextSummaryState 等）。
│   │   ├── transactions.py                            复合写入端口：StoreTransaction 及请求 DTO（RunCreateWrite 等 4 类复合写入）。
│   │   └── cancellation.py                            取消广播端口：RunCancelBus（跨进程 run 取消的发布与监听抽象）。
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
│   │   ├── master_agent_provider.py                   内置主代理定义、主代理 prompt 加载与多 profile 提供者。
│   │   ├── master_prompt.md                           主智能体系统提示词文件。
│   │   ├── default_sub_agents/                        默认子代理配置与 prompt 资源目录。
│   │   │   ├── __init__.py                            包标记。
│   │   │   ├── definitions.py                         默认子代理 Python 声明式配置（默认 Worker 子代理）。
│   │   │   └── worker.md                              默认 Worker 子代理系统提示词。
│   │   ├── custom_sub_agent_loader.py                 自定义 md 子代理加载器。
│   │   ├── hook_profiles.py                           子代理可引用的预注册 Hook 组。
│   │   └── profile_builder.py                         将默认/自定义配置组装成 AgentExecutionProfile，自动过滤主控工具（Task/ListResumableSubagents）。
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
│   │   ├── task_tool.py                               Task 派发工具，master 通过此工具同步派发子代理，支持动态工具注入（Worker）。
│   │   ├── plan_create_tool.py                        计划创建工具。
│   │   ├── plan_get_tool.py                           计划查询工具。
│   │   ├── plan_list_tool.py                          计划列表工具。
│   │   ├── plan_update_tool.py                        计划更新工具。
│   │   ├── list_resumable_subagents_tool.py           可恢复子代理列表查询工具。
│   │   ├── skill_tool.py                              Skill 加载工具。
│   │   ├── query_tool_result_tool.py                  大工具结果分页查询工具。
│   │   └── run_python_script_tool.py                  项目内 Python 脚本执行工具。
│   └── store/                                         Redis 持久化适配目录。
│       ├── __init__.py                                统一导出全部 Store 及适配器。
│       ├── redis_lock_store.py                        会话分布式锁实现（SET NX EX + 心跳续期）。
│       ├── redis_run_store.py                         Run 持久化。
│       ├── redis_session_store.py                     会话元数据、主/child 上下文消息、session 索引持久化（Hash 结构），含 SessionChildSummary 可恢复子代理摘要。
│       ├── redis_task_store.py                        Task 持久化。
│       ├── redis_tool_result_store.py                 大工具结果持久化。
│       ├── redis_store_transaction.py                 复合写入事务适配器（封装 Redis pipeline，实现 StoreTransaction 端口）。
│       └── redis_run_cancel_bus.py                    运行取消广播适配器（封装 Redis pubsub，实现 RunCancelBus 端口）。
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
    ├── child_agent_runner.py                          子代理执行服务；管理 child run、上下文隔离、取消传播、动态工具注入（Worker）。
    ├── run_control_service.py                         Run 查询与取消控制。
    ├── session_cleanup_service.py                     会话级联删除服务。
    ├── session_service.py                             会话创建、查询视图、消息计数。
    └── task_service.py                                Task CRUD 业务服务。
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
│   ├── test_cancel.sh                                 取消功能端到端测试。
│   ├── smoke_test_store_protocol.sh                    Store Protocol 重构全端点冒烟测试。
│   ├── test_cancel_run.sh                              取消 Run 专用冒烟测试。
│   ├── test_worker_chat.sh                            Worker 子代理对话 e2e 测试。
│   ├── test_plan_isolation.sh                         Plan 隔离命名空间 e2e 测试。
│   └── test_resumable_subagents.sh                    可恢复子代理 e2e 测试。
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
    │   ├── ports/
    │   │   └── test_store_ports.py                    Store 端口与 DTO 冒烟测试。
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
    │   │   ├── test_sub_agent_profiles.py             子代理 profile 测试。
    │   │   └── test_profile_builder.py                ProfileBuilder 测试（含主控工具过滤）。
    │   ├── llm/test_litellm_adapter.py                LiteLLM 适配器测试。
    │   ├── logging/test_logger_manager.py             日志管理器测试。
    │   ├── tools/
    │   │   ├── test_mcp_adapter.py                    MCP 适配器测试。
    │   │   ├── test_mcp_client_manager.py             MCP 管理器测试。
    │   │   ├── test_python_script_tool.py             Python 脚本工具测试。
    │   │   ├── test_query_tool_result_tool.py         大结果查询工具测试。
    │   │   ├── test_skill_tool.py                     Skill 工具测试。
    │   │   ├── test_task_tool.py                      TaskTool 测试（含动态 tools 注入）。
    │   │   ├── test_task_tools.py                     Plan CRUD 工具测试。
    │   │   ├── test_list_resumable_subagents_tool.py  ListResumableSubagentsTool 测试。
    │   │   └── test_plan_scope.py                     Plan 隔离命名空间测试。
    │   └── store/
    │       ├── test_redis_lock_store.py               锁存储测试。
    │       ├── test_redis_run_store.py                Run 存储测试。
    │       ├── test_redis_session_store.py            会话存储测试。
    │       ├── test_redis_task_store.py               Task 存储测试。
    │       ├── test_redis_tool_result_store.py        大结果存储测试。
    │       ├── test_redis_store_transaction.py        复合写入事务适配器测试。
    │       └── test_redis_run_cancel_bus.py           取消广播适配器测试。
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
-> SessionService.create_session(master_agent_name?)
-> MasterAgentProvider.get_default() 或 get_master_profile_by_name()
-> SessionStore.create_session()
```

产物是一个绑定主智能体的 `Session` 元数据记录。

### 2. 发起聊天（含取消链路）

```text
POST /chat
-> ChatRequest(master_agent_name 必填)
-> ChatService.stream_chat()
   -> MasterAgentProvider.get_master_profile_by_name(master_agent_name)
   -> 校验 session.agent_id == profile.agent_id
   -> ChatRunLockScope.acquire()                    # 会话锁 + 心跳续期
   -> SessionStore.append_main_message(user)
   -> StoreTransaction.create_run_and_index_session()
   -> ContextBuilder.build_llm_messages()            # 构建上下文
   -> AgentLoop.run(profile)                        # 多轮循环编排
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
  -> cancel_event.set() 或 RunCancelBus.publish_cancel(run_id)
- SSE 断链
  -> _disconnect_monitor() 检测 http.disconnect
  -> cancel_event.set()
- RunCancelBus 广播（跨进程）
  -> _listen_cancel_messages() 收到取消 run_id
  -> cancel_event.set()
```

### 3. 查询会话 / 运行 / 取消运行

```text
GET /sessions/{session_id}
-> app/interfaces/http/routes/sessions.py
-> SessionService.get_session_view()
-> SessionStore + LockStore

GET /runs/{run_id}
-> app/interfaces/http/routes/runs.py
-> RunControlService.get_run()
-> RunStore.get_run()

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
   -> 可选：动态构建 Worker 子代理工具列表（tools 参数）
   -> 生成 child_id 或复用 resume 中的 child_id
-> ChildAgentRunner.run_child()
   -> 校验 resume 一致性（subagent_type 与存储匹配）
   -> 创建 child Run（run_type=child, parent_run_id, child_id）
   -> 通用子代理（Worker）支持动态工具注入（_build_dynamic_profile）
   -> 构建 child 上下文（从 child_context_messages 读取历史）
   -> AgentLoop.run() (child profile)
     -> ContextBuilder.build (child 上下文隔离，含 child_id)
     -> plan/task 存储通过 ExecutionContext.resolve_plan_session_id() 隔离
   -> 结果写入 child_context_messages + upsert child 摘要
   -> 更新 child run 终态
-> 返回 ToolResult 给 master
```

### 7. 文件之间的关键关系

```text
主智能体配置链:
app/config.py(.env) + app/infra/agents/master_prompt.md（等各 profile prompt 文件）
-> app/infra/agents/master_agent_provider.py（多 profile 提供者：default/plan）
-> app/services/agent_provider.py
-> SessionService / ChatService（均通过 get_master_profile_by_name() 获取对应 profile）

持久化链:
SessionService / ChatService / RunControlService
-> SessionStore / RunStore / LockStore / TaskStore / ToolResultStore
-> StoreTransaction / RunCancelBus
-> Redis 默认适配器

启动链:
start.py / uvicorn --factory
-> app/bootstrap/factory.py
-> app/config.py + app/infra/logging/logger_manager.py
-> app/bootstrap/container.py
-> app/main.py
```

## 部署指南

### 前置要求

- Python 3.12+
- Redis 服务（本地或远程均可）
- Git

### 快速部署

```bash
# 1. 克隆项目
git clone https://github.com/w-tao-hub/EAgentBase.git
cd EAgentBase

# 2. 创建虚拟环境
python3.12 -m venv .venv

# 3. 安装项目依赖
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

# 4. 配置环境变量
cp .env.example .env

# 5. 启动服务
.venv/bin/python start.py
```

服务默认运行在 `http://localhost:8000`，访问 `/docs` 可查看 Swagger API 文档。

### Redis 配置模式

项目支持两种 Redis 连接模式，通过 `.env` 中的 `REDIS_MODE` 配置项切换。

#### 单点模式（默认）

适用于开发环境或单 Redis 实例部署。配置项：

```bash
REDIS_MODE=single
REDIS_URL=redis://:your_password@127.0.0.1:6379/0
REDIS_KEY_PREFIX=agent
```

其中 `REDIS_URL` 格式为 `redis://[:密码@]主机:端口/库号`。本地无密码时可省略 `:your_password@`。

#### Sentinel 模式（生产推荐）

适用于 Redis 高可用集群部署，应用通过 Sentinel 自动发现当前 master 节点，支持故障切换。配置项：

```bash
REDIS_MODE=sentinel
REDIS_SENTINEL_NODES=127.0.0.1:26379,127.0.0.2:26379,127.0.0.3:26379
REDIS_SENTINEL_MASTER_NAME=mymaster
REDIS_KEY_PREFIX=agent
REDIS_DB=0
REDIS_USERNAME=
REDIS_PASSWORD=your_password
```

| 配置项 | 必填 | 说明 |
|--------|------|------|
| `REDIS_MODE` | 是 | 设为 `sentinel` |
| `REDIS_SENTINEL_NODES` | 是 | Sentinel 节点列表，逗号分隔 `host:port`，支持 JSON 数组格式 |
| `REDIS_SENTINEL_MASTER_NAME` | 是 | Sentinel 监控的 master service name |
| `REDIS_DB` | 否 | master 逻辑库编号，默认 `0` |
| `REDIS_USERNAME` | 否 | Redis 认证用户名（Sentinel 与 master 共用） |
| `REDIS_PASSWORD` | 否 | Redis 认证密码（Sentinel 与 master 共用） |

**连接说明：**

- 应用启动时会创建两个独立的 Redis 连接客户端：主连接（连接池 50，服务业务读写）和 pubsub 连接（连接池 2，专用于跨进程取消广播监听）。
- 两个客户端在 Sentinel 模式下均通过 `sentinel.master_for()` 发现当前 master，上层 Store 和 Service 对连接模式无感知。
- Sentinel 与 master 共用同一套 `REDIS_USERNAME` / `REDIS_PASSWORD` 认证凭据。

## 使用指南

本项目为骨架式设计，企业可根据业务需求进行扩展。推荐以下开发路径：

### 第一步：开发定制化 Tool

Tool 是智能体的"双手"，负责执行具体动作（查数据库、调用外部 API、操作文件等）。

> 提示：在 `app/infra/tools/` 下新建文件，继承 `Tool` 基类，实现 `call()` 方法，然后在 `ToolRegistry` 中注册。参考 `task_tool.py` 的实现风格。

### 第二步：开发定制化 Hook

Hook 允许你在 LLM 调用前后、工具执行前后插入自定义逻辑（日志、监控、数据脱敏等）。

> 提示：在 `app/core/hooks/` 下新建文件，继承 `ModelHook` 或 `ToolHook` 基类，然后注册到 Hook 管线中。参考 `persist_large_tool_result_hook.py` 的实现。
>
> **Hook 执行顺序**（基于 `agent_runtime.py` + `agent_loop.py` 编排）：
>
> **模型 Hook**（ModelHookPipeline，按注册顺序串行执行）
> ```
> 1. ModelHook.before_model(request, context)  → 改写发往 LLM 的消息和参数
> 2. LLM.stream_completion()                   → 流式调用 LLM（实际模型请求）
> 3. ModelHook.after_model(response, context)  → 改写 LLM 返回的 tool_calls/usage
> ```
>
> **工具 Hook**（ToolHookPipeline，按注册顺序串行执行）
> ```
> 1. ToolHook.before_tool(request, context)    → 改写工具调用的名称和参数
> 2. tool.call(tool_input, context)            → 实际执行工具（所有工具通过 asyncio.gather 并行）
> 3. ToolHook.after_tool(response, context)    → 改写工具执行结果
> ```
>
> **完整一轮对话编排**：
> ```
> ① 模型 before_model(1→2→3) → ② LLM 调用 → ③ 模型 after_model(1→2→3) → ④ 若返回 tool_calls：每个工具 before_tool(1→2→3) → 工具 call → after_tool(1→2→3) → ⑤ 工具结果送回下一轮 LLM 调用（重复 ①→②→③→④→⑤→...）
> ```
>
> 同一管线内的 Hook 按 `tool_hook_profiles` / `model_hook_profiles` 列表顺序依次串行执行。某个 Hook 抛出异常时：若 `fail_open=True` 则跳过继续；否则中断当前管线并上报 `HookExecutionError`。`fail_open` 在自定义 Hook 构造函数中通过 `super().__init__(fail_open=True)` 设置，默认 `False`。

### 第三步：配置 MCP 与 Skill

MCP 用于接入外部工具服务（数据库、文件系统、Web 等），Skill 用于注入领域知识和技能文档。

> **MCP 配置**：编辑 `mcp_servers.json`，按 `mcp_servers.json.example` 的格式添加 MCP 服务端配置，重启后自动生效。
>
> **Skill 添加**：在 `skills/` 目录下放置领域知识文档（Markdown 格式），框架会自动扫描索引，智能体按需加载。

### 第四步：定制子代理

针对特定场景配置专用子代理，分配不同的模型、提示词和 Hook 组合。

> 提示：在 `agents/` 目录下创建 `.md` 文件定义子代理配置，或直接在 `app/infra/agents/default_sub_agents/definitions.py` 中添加声明式配置。

> **子代理挂载到主代理**：子代理通过 `mount_master_agents` 字段控制能被哪些主代理调用。缺省时只挂载到 `default`。主代理的 tool/hook/skill 挂载在 `container.py` 的三个字典中配置，子代理挂载关系与主代理挂载关系相互独立。示例：
> ```yaml
> ---
> name: CodeReviewer
> description: "代码审查子代理"
> mount_master_agents:
>   - default
>   - review
> ---
> ```

> **Hook 配置说明**：子代理支持 `tool_hook_profiles` 和 `model_hook_profiles` 两个字段，值为 Hook 名称列表。名称必须与 `app/bootstrap/container.py` 中 `HookRegistry` 注册的 key 对应。当前内置 Hook 及对应名称：
> - `persist_large_result` — 大工具结果持久化（ToolHook）
> 添加自定义 Hook 时，先在 `container.py` 的 `HookRegistry(tool_hooks={...}, model_hooks={...})` 中注册，然后在子代理配置中引用注册的名称即可。

### 第五步：调整系统提示词 + 调整工具描述提示词

修改主智能体的行为风格、约束规则等。
修改工具描述以及工具参数描述规则。

> 提示：编辑 `app/infra/agents/master_prompt.md`（default 主代理）或 `app/infra/agents/plan_master_prompt.md`（plan 主代理），调整对应主代理的 system prompt。新增主代理时需创建对应的 prompt 文件，并在 `app/bootstrap/container.py` 的三个挂载字典中配置 tool/hook/skill。

### 主代理路由

系统内置 `default` 和 `plan` 两个主代理。创建会话时可通过 `master_agent_name` 绑定主代理，未传时默认绑定 `default`。调用 `/chat` 时必须显式传入 `master_agent_name`，且必须与会话绑定的主代理一致。

子代理 md frontmatter 可通过 `mount_master_agents` 控制挂载关系。未配置时只挂载到 `default`。

#### 主代理 tool/hook/skill 挂载配置

每个主代理的可用工具、Hook 管线、Skill 通过 `app/bootstrap/container.py` 顶部的三个字典控制：

```python
# 按主代理名称控制 tool 挂载。不在字典中 = 不挂载任何固定 tool。
_MASTER_TOOL_MOUNTS: dict[str, tuple[str, ...]] = {
    "default": ("plan_create", "plan_get", "plan_update", "plan_list",
                "skill", "query_tool_result", "run_python_script"),
}

# 按主代理名称控制 tool_hook 挂载。不在字典中 = 空管线。
_MASTER_HOOK_MOUNTS: dict[str, tuple[str, ...]] = {
    "default": ("persist_large_result",),
}

# 按主代理名称控制 skill 挂载。不在字典中 = 不加载任何 skill。
_MASTER_SKILL_MOUNTS: dict[str, tuple[str, ...]] = {
    "default": ("test-skill",),
}
```

**配置规则：**

| 项目 | 不在字典中 | 显式空元组 `()` | 有值 |
|------|----------|--------------|------|
| tool | 无固定 tool | 无固定 tool | 只挂载列表中的 tool |
| hook | 空管线 | 空管线 | 按名称装配独立管线 |
| skill | 无 skill | 无 skill | 按名称加载 skill 内容 |

> MCP 动态工具不受 `_MASTER_TOOL_MOUNTS` 控制，始终全部注册。

**新增主代理示例：**

```python
# 1. 在 master_agent_provider.py 中加定义
MASTER_AGENT_DEFINITIONS = (
    ...,
    MasterAgentDefinition(agent_id="review", name="review", prompt_file="review_master_prompt.md"),
)

# 2. 创建 review_master_prompt.md

# 3. 在 container.py 三个字典中加挂载配置
_MASTER_TOOL_MOUNTS = {
    "default": (...),
    "review":  ("plan_create", "plan_get", "skill"),
}
_MASTER_HOOK_MOUNTS = {
    "default": ("persist_large_result",),
    "review":  ("persist_large_result",),
}
_MASTER_SKILL_MOUNTS = {
    "default": ("test-skill",),
    "review":  ("test-skill",),
}
```

### 第六步：替换 Store 后端（可选）

默认实现使用 Redis。企业如需替换为 PostgreSQL、MongoDB 或自研存储，不需要修改服务层主链路，应在组合根中注入自定义端口实现。

需要实现的核心端口位于 `app/core/ports/`：

- `SessionStore`：会话元数据、主上下文、child 上下文、child 摘要索引。
- `RunStore`：Run 创建、查询、终态更新与删除。
- `TaskStore`：Plan/Task CRUD 存储。
- `LockStore`：session 单活跃 run 锁。
- `ToolResultStore`：大工具结果持久化与查询。
- `StoreTransaction`：主 run 创建、主 run 终态、child 首条输入与摘要、child 终态等复合写入。
- `RunCancelBus`：跨进程 run 取消广播与监听。

项目只提供 Redis 默认适配器，不内置 PostgreSQL/MongoDB 实现。替换方式是在 `app/bootstrap/container.py` 中把默认 Redis 实例替换为企业自己的实现。

约束说明：

- 不要求替代后端模拟 Redis pipeline，复合写入由 `StoreTransaction` 表达业务语义。
- 不要求替代后端模拟 Redis pubsub，取消广播由 `RunCancelBus` 表达业务语义。
- 替代实现应自行说明事务一致性、锁租约和取消广播的可靠性边界。```
