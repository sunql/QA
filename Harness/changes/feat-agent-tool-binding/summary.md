# feat-agent-tool-binding

> 日期：2026-09-02 | 状态：done | Spec: docs/superpowers/specs/2026-09-02-agent-tool-binding-design.md
> Plan: docs/superpowers/plans/2026-09-02-agent-tool-binding.md
> SDD ledger: .superpowers/sdd/2026-09-02-agent-tool-binding/progress.md

## 目标

把 Agent → Tool 关系从硬编码 `AGENT_TOOLS` dict 升级到 PostgreSQL。
Admin 通过 Agent Registry UI 配置即生效，不需发版。

## 实现

- Alembic 0035：`agent_definition` 表新增 `tool_name` + `tool_name_updated_at`
- `seed_agent_tool_bindings.py`：启动幂等 seed（从 `AGENT_DEFAULT_BINDINGS` 派生）
- `AgentBindingCache`：模块级单例，启动 `warmUp` + 写时 `refreshOne/invalidate`
- `AgentDefinitionCreate/Update` 写时跨字段校验：`tool_name` 必须在 `agent_tool_registry` 中；
  `agent.data_layers` 必须完全覆盖 `tool.data_layers`；非法 422
- `GET /agents/options` 扩展 `tools: [{name, description, dataObject, dataLayers}]`
- Runtime `_resolveTool`：DB cache 优先，无 binding → 409；DB 绑定漂移（代码未注册）→ 409
- `agentToRead.runnable` 改用 cache 判断
- 前端 `AgentRegistryPage` Create/Edit Modal 加 toolName Select（实时 layer 校验）+ Detail Drawer 展示
- i18n: zh-CN / en-US 各 4 keys
- `AGENT_TOOLS` dict 完全删除，改为 `AGENT_DEFAULT_BINDINGS` 常量（seed 数据源）

## 验证

### 单测
- `test_agent_binding_cache.py`：4 用例（warmUp/getToolName/invalidate 单条/全清）

### 集成
- `test_agent_tool_binding_migration.py`：列存在性
- `test_seed_agent_tool_bindings.py`：幂等性 + 3 行 seed
- `test_agent_tool_binding_validation.py`：5 用例（合法/未知/缺层/无 tool/清空）
- `test_agent_options_api.py`：扩展 2 用例（tools 字段 + 排序）
- `test_agent_tool_binding_runtime.py`：2 用例（cache 命中 + dict fallback 过渡期）

### 全量
- backend: `pytest app/tests/ --cov=app --cov-fail-under=80` ≥ 80% 通过
- frontend: `npx tsc --noEmit` + `npx vitest run` 全绿

### e2e
- 启动空 DB → 3 行自动 seed → run 路径 200
- Admin 改 binding → ≤1 request 生效（cache 失效验证）
- DB 绑未注册 tool → 409 + MSG_AGENT_TOOL_UNREGISTERED

## 关键决策

- 1:1 基数（`agent.tool_name` 单列；运行时不引入工具选择器）
- DB 唯一源 + 启动 seed（一次性脚本，dict 删除后从 registry 派生）
- 写时严格 422（deny-by-default 提前到配置面）
- 启动预热 + 写时失效（hot path 零 DB roundtrip）
- admin only 写权限（与既有 Agent Registry 一致）
- 工具定义（handler/data_object/data_layers）始终在代码（安全关键）

## 与既有 change 的关系

- `feat-agent-vocabulary`（已发）：复用 `AGENT_DATA_LAYERS` 词表约束
- `feat-agent-runtime-mvp`（已发）：`_resolveTool` 改造点
- `feat-acl-extension-3-entities`（已发）：写权限 admin only 模式延续

## 已知遗留

- Update toolName「未传 vs null」歧义：当前实现是「不传 = 清空」语义，前端须明确 PATCH 时是否带 toolName 字段（spec §9 风险已登记）
- 单实例缓存：未来多实例部署需切 Redis（独立 change）
- AGENT_DEFAULT_BINDINGS 是 seed 常量而非 dict 概念上的「运行时映射」

## Commits

```
285393e docs(spec): feat-agent-tool-binding design
fc59b68 docs(plan): feat-agent-tool-binding implementation plan
c232db7 feat(agent): tool_name + tool_name_updated_at columns (0035 migration)
3d4130b feat(agent): startup seed agent_tool_bindings
cb8b426 feat(agent): AgentBindingCache module + write-time invalidation
ab187a7 feat(agent): write-time tool_name validation (Pydantic field_validator)
2858ade feat(agent): extend /agents/options with tools list
8d3f6bf feat(agent): AgentBindingCache wired into runtime (AGENT_TOOLS dict retained as fallback)
6c3e803 refactor(agent): delete AGENT_TOOLS dict; runtime cache-only
127bf1f feat(frontend): toolName Select in Agent Registry
(TBD)  docs(harness): feat-agent-tool-binding change summary
```
