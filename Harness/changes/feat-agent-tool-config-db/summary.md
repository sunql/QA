# feat-agent-tool-config-db

> 日期：2026-09-03 | 状态：done | Spec: docs/superpowers/specs/2026-09-03-agent-tool-config-db.md
> Plan: docs/superpowers/plans/2026-09-03-agent-tool-config-db.md

## 目标

把硬编码的工具元数据注册表（`backend/app/services/agent_tools.py` 里的
`BUILTIN_HANDLERS` / `NL2SQL_HANDLERS` / `ARG_EXTRACTORS` 等 dict 字面量）
迁到 PostgreSQL，handler 引擎函数继续留在代码里——只 metadata 上 DB。

Admin 通过 `/admin/tools` UI 管理 `dataObject` / `dataLayers` /
`handlerKind` / `handlerRef` / `enabled`，无需发版即可生效。

## 实现

### 数据模型（Alembic 0037）
- `agent_tool_config` 表（SSOT）：`id / name (PK 文本) / description /
  data_object / data_layers (JSONB list) / input_schema (JSONB dict) /
  handler_kind (BUILTIN/NL2SQL enum) / handler_ref / arg_extractor_kind /
  enabled / version (乐观锁) / created_time / updated_time`
- 唯一索引 `name`；`version` 在每次 UPDATE 时 bump（乐观锁防并发覆盖）

### DTO / 枚举
- `AgentToolHandlerKind` enum：`BUILTIN` / `NL2SQL`
- `AgentToolConfigCreate` / `AgentToolConfigUpdate` / `AgentToolConfigRead`：
  - `Create`：必填 name/dataObject/handlerKind/handlerRef，可选 description/dataLayers/inputSchema/argExtractorKind
  - `Update`：必须含 `version`；name 不可改（422）
  - `Read`：含 id/version/audit 字段

### Service + Registry
- `AgentToolConfigService`：list/get/create/update/delete/toggle，async session 包裹
- `_ToolInUseConflict(ConflictError)`：删除/禁用时若 AgentDefinition 仍引用该
  tool_name → 409 + `referencingAgents: [code, ...]`
- `AgentToolConfigRegistry`：模块级缓存，DB 是 SSOT
  - `warmUp(session)`：启动预热（lifespan 调用）
  - `reloadAll/reloadOne`：写时失效（service 写完后调用）
  - `asyncio.Lock` 防止并发 reload 竞态
  - `get(name) / getAll() / getEnabled()` 同步接口（缓存已 hot）

### Runtime 集成
- `AgentToolAssembly`：把 DB record 装成 `AgentTool`（含 handler + arg_extractor）
  - `BUILTIN` → `BUILTIN_HANDLERS[handler_ref]`
  - `NL2SQL` → `NL2SQL_HANDLERS[handler_ref]`
  - arg_extractor 从 `ARG_EXTRACTORS[arg_extractor_kind]` 派生
- `AgentRuntimeService._resolveTool(session, name)`：DB 优先 + 缓存命中，
  handler engine 调用方式不变
- `seed_agents.py._policiesFor(session, code)`：从
  `agent_tool_config_registry.get(tool_name)` 派生显式分层策略（DRY）

### REST API（`/api/v1/agent-tools`，admin-only）
- `GET /` → list（`?enabledOnly=true` 过滤）
- `GET /{name}` → detail（404 NotFound）
- `POST /` → create（409 versionConflict / 422 validation）
- `PUT /{name}` → update（需 `version`，409 versionConflict）
- `DELETE /{name}` → delete（409 referencingAgents）
- `POST /{name}/toggle` → flip `enabled`（缓存失效）
- ACL：读 getCurrentUser；写 getAdminOnlyActor

### 前端
- `frontend/src/types/agentTool.ts`：AgentToolConfig{Read,Create,Update} + AgentToolHandlerKind
- `frontend/src/api/agentTools.ts`：6 个函数（list/get/create/update/delete/toggle），prefix = `/agent-tools`
- `frontend/src/i18n/zh-CN.ts` + `en-US.ts`：`agentTools.{title,columns,actions,form,messages,errors}`
  命名空间，错误 key：`versionConflict` / `inUseByAgent` / `nameImmutable`
- `frontend/src/pages/AdminToolsPage.tsx`（新）：Table + Create/Edit Modal +
  Delete Popconfirm + Switch toggle，409 → inUseByAgent，其他错误 → 通用
- `frontend/src/App.tsx`：注册 `/admin/tools` 路由
- `AgentRegistryPage` 详情抽屉的 policy 表单 `dataObject` 字段升级为
  `<Select showSearch allowClear>`，选项从 `useAgentOptions().tools` 派生
  （去重 + 排序）；避免硬编码 SUPPLIER/GRAPH 等数据对象名
- `AdminAuditPage` 审计 `entity_type` 过滤下拉新增 `AGENT_TOOL_CONFIG`，
  让管理员可按该 entity_type 过滤 AgentToolConfig 的 CRUD 审计日志

### Seed（启动幂等）
- `scripts/seed_agent_tool_configs.py`：启动 lifespan 调用，
  从代码侧的 `BUILTIN_HANDLERS` + `NL2SQL_HANDLERS` + `ARG_EXTRACTORS`
  元数据派生 3 条记录：`supplier_360` / `supplier_risk` / `graph_traverse`。
  已存在则跳过（按 name 幂等）

## 验证

### 单测
- `test_agent_tool_handler_kind.py`：`AgentToolHandlerKind` enum + `_normalizeDataObject`
- `test_agent_tool_config_dto.py`：Create/Update/Read 字段裁剪、name 不可改、
  version 必填、`UnsetType` 处理

### 集成（真实 PG，TRUNCATE 隔离）
- `test_agent_tool_config_service.py`：5 用例（CRUD + toggle + version 乐观锁）
- `test_agent_tool_config_api.py`：17 用例（list/get/create/update/delete/
  toggle 全部 endpoint + ACL + 409 referencing + 409 versionConflict + 422 validation）
- `test_agent_tool_runtime_db_driven.py`：7 用例（registry SSOT + cache 失效 +
  handler dispatch + enabled=false 拒绝 + arg_extractor 派生）
- `test_agent_tool_config_audit.py`：5 用例（OutboxService 写审计 CREATE/
  UPDATE/DELETE/TOGGLE + version bump + enabled flag 变更）
- `test_agent_scheduler_api.py`：补 `tool_name` 传参 + 自动派生 `data_layers`

### 覆盖率门槛
- `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
   uv run pytest app/tests/ --cov=app --cov-fail-under=80`

### 前端
- `frontend/src/tests/T16.toolConfigIntegration.test.tsx`（新）：
  - AdminAuditPage `ENTITY_TYPE_OPTIONS` 含 `AGENT_TOOL_CONFIG`（契约性源码断言）
  - AgentRegistryPage policy form dataObject Select 选项含 SUPPLIER / GRAPH
- `frontend/src/tests/AdminToolsPage.test.tsx`：5 用例（render / 表格列 / 按钮 / 删除 /
  toggle 调用），所有 i18n 中文按钮 `.replace(/\s+/g, "")` 折叠 antd inline 空白
- `frontend/src/tests/agentTools.test.ts`：types + API client 6 函数

## 关键设计决策

- **DB 仅存元数据**：handler 函数 + nl2sql 查询 + arg_extractor 留在 Python
  代码（`agent_tools.Assembly`），编译期保证不存在，DB 不存可执行对象；
  避免 SQL 注入 / 任意代码执行风险
- **乐观锁**：`version` 字段每次 UPDATE 时 `+1`，写接口要求客户端传 version，
  不匹配 → 409 versionConflict
- **handler_ref 与 name 解耦**：DB 存 `handler_ref`（代码侧 handler 字典 key），
  改名（DB 的 name）不影响代码侧 dispatch
- **`enabled=false` 立即生效**：toggle endpoint → `reloadOne` 缓存失效 +
  OutboxService 写审计；下次 runtime 调用重新读 DB
- **写时跨字段校验**：`enabled=false` 不影响已绑定的 AgentDefinition（避免
  AgentDefinition 突然 409 未绑定工具）
- **注册表 + 字典双轨**：DB 是 SSOT 但冷启动靠 `seed_agent_tool_configs.py`，
  如果 DB 数据被误删，启动时自动恢复

## 关联变更

- 前置：`feat-agent-tool-binding`（AgentDefinition.tool_name 已建好）
- 前置：`feat-agent-runtime-mvp`（AgentTool / AgentHandler 抽象已建好）
- 后续（潜在）：AdminToolsPage 增「关联 Agent 列表」面板（直接拉
  AgentDefinition 中 tool_name == this.name 的记录）