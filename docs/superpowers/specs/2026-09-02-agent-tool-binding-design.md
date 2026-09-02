# Agent 工具绑定可配置化（feat-agent-tool-binding）设计方案

> 日期：2026-09-02 | 状态：draft → 评审中 | 关联：`feat-agent-vocabulary`（已发）
> 前置：`feat-agent-runtime-mvp`、`feat-agent-registry`、`feat-agent-vocabulary`

## 1. 背景与目标

### 1.1 现状

Agent → Tool 的绑定关系硬编码在 `backend/app/services/agent_tools.py:287` 的
`AGENT_TOOLS` dict 里：

```python
AGENT_TOOLS: dict[str, tuple[str, ...]] = {
    "SUPPLIER_360_AGENT": ("supplier_360",),
    "SUPPLIER_RISK_AGENT": ("supplier_risk",),
    "GRAPH_REASONING_AGENT": ("graph_traverse",),
}
```

`AgentRuntimeService.run` 通过 `AGENT_TOOLS.get(agent_code)` 取工具名，
再从 `agent_tool_registry`（内存注册表）取具体工具。Admin 要新增/调整
绑定必须改代码、发版、回滚困难。

### 1.2 目标

把 `Agent → Tool` 关系从 Python 代码搬到 PostgreSQL，让 Admin 通过现有
Agent Registry UI 配置即可生效，不需发版。

### 1.3 非目标（明确不做）

- 工具**定义**（`handler/data_object/data_layers`）入 DB —— 安全关键，留代码
- 1:N 工具绑定 —— 独立排期
- Redis 缓存 —— 当前单实例够用
- Tool 自身的管理 UI —— 工具是代码资产

## 2. 已对齐的设计决策

| 维度 | 选型 | 理由 |
|---|---|---|
| 关系基数 | **1:1**（Agent ↔ Tool） | 沿用现状语义；DB 单列即可；最小改动 |
| 运行时取数 | **DB 唯一源**，启动时若空则 seed | 无二义性；启动幂等 |
| 写时一致性 | **严格 422**（agent.data_layers ⊇ tool.data_layers） | deny-by-default 提前到配置面 |
| 前端选项源 | **扩展 `/agents/options`**（加 `tools` 字段） | 复用现有 hook；零额外请求 |
| 缓存策略 | **启动预热 + 写时失效** | hot path 零 DB；admin 改后 ≤1 request 生效 |
| 写权限 | **admin only** | 与既有 Agent Registry 写权限一致 |

## 3. 数据模型

### 3.1 新增列（`agent_definition` 表）

```python
# Alembic 0035
tool_name: Mapped[str | None] = mapped_column(
    String(64), nullable=True,
    doc="绑定的工具名；None = 未绑定（不可运行）",
)
tool_name_updated_at: Mapped[datetime | None] = mapped_column(
    TIMESTAMP(timezone=True), nullable=True,
    doc="tool_name 上次更新时间（用于审计）",
)
```

### 3.2 启动 Seed 脚本（idempotent）

```python
# backend/scripts/seed_agent_tool_bindings.py
"""把已注册工具绑定到默认 3 个 Agent。
启动时通过 lifespan 调用；DB 已有 binding 则跳过。
"""
```

**约束**：commit 阶段 1 引入本脚本时，`AGENT_TOOLS` dict 还在，
dict 是 seed 的数据源；commit 阶段 6 删 dict 后，本脚本从
`agent_tool_registry` 派生。

## 4. 运行时

### 4.1 缓存层（新增模块）

```python
# backend/app/services/agent_binding_cache.py
class AgentBindingCache:
    """启动预热 + 写时失效。模块级单例。"""

    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}  # agent_code → tool_name
        self._loaded: bool = False

    async def warmUp(self, session: AsyncSession) -> None:
        rows = await session.execute(
            select(AgentDefinition.agent_code, AgentDefinition.tool_name)
            .where(AgentDefinition.status == AgentStatus.ACTIVE.value)
        )
        self._cache = {code: tool for code, tool in rows.all()}
        self._loaded = True

    def getToolName(self, agent_code: str) -> str | None:
        if not self._loaded:
            raise RuntimeError("AgentBindingCache 未 warmUp（lifespan bug）")
        return self._cache.get(agent_code)

    def invalidate(self, agent_code: str | None = None) -> None:
        """agent_code=None 全清；否则清单条。"""
        if agent_code is None:
            self._cache.clear()
        else:
            self._cache.pop(agent_code, None)

    async def refreshOne(self, session: AsyncSession, agent_code: str) -> None:
        """写后主动 reload 单条（替代 invalidate + 下次 get 走 DB 的 lazy 路径）。"""
        row = await session.execute(
            select(AgentDefinition.tool_name)
            .where(AgentDefinition.agent_code == agent_code)
        )
        self._cache[agent_code] = row.scalar_one_or_none()

agent_binding_cache = AgentBindingCache()  # 模块级单例
```

### 4.2 Lifespan 集成

```python
# backend/app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... existing ...
    await agent_binding_cache.warmUp(session_factory())
    # ... existing ...
```

**顺序**：seed → warmUp（seed 失败 → 启动失败，fail-fast）。

### 4.3 写时失效（3 个写点）

`AgentRegistryService.createAgent / updateAgent / deleteAgent` 在 commit
之后调用 `agent_binding_cache.invalidate(agent_code=...)` 或
`refreshOne(session, agent_code)`。`deleteAgent` 走 `invalidate`（删条
目），`create/updateAgent` 走 `refreshOne`（立刻 reload 单条）。

### 4.4 写时跨字段校验（Pydantic v2）

```python
# backend/app/domain/schemas.py  AgentDefinitionCreate
from app.services.agent_tools import agent_tool_registry

@field_validator("tool_name")
@classmethod
def _validateToolName(cls, v: str | None, info) -> str | None:
    if v is None:
        return v  # 未绑定合法
    tool = agent_tool_registry.get(v)
    if tool is None:
        raise ValueError(MSG_AGENT_TOOL_UNKNOWN.format(
            name=v,
            registered=",".join(t.name for t in agent_tool_registry.all()),
        ))
    data_layers = info.data.get("data_layers") or []
    missing = [layer for layer in tool.data_layers if layer not in data_layers]
    if missing:
        raise ValueError(MSG_AGENT_TOOL_LAYER_MISMATCH.format(
            tool=v, missing=",".join(missing),
        ))
    return v
```

`AgentDefinitionUpdate` 同样实现（None 跳过校验；非 None 走完整流程）。

### 4.5 `run` 路径改造

```python
# backend/app/services/agent_runtime_service.py
async def run(self, session, agent_code, params, *, actor):
    entity = await self._agents.getAgent(session, agent_code)  # 404 unchanged
    if entity.status != AgentStatus.ACTIVE.value:
        raise ConflictError(MSG_AGENT_NOT_RUNNABLE.format(code=agent_code))

    # 【CHANGED】tool binding 改为从 DB 缓存
    tool_name = agent_binding_cache.getToolName(agent_code)
    if tool_name is None:
        raise ConflictError(MSG_AGENT_NOT_RUNNABLE_NO_TOOL.format(code=agent_code))

    tool = agent_tool_registry.get(tool_name)
    if tool is None:
        # 防御：DB 写入了未在代码注册的 tool（漂移）
        raise ConflictError(MSG_AGENT_TOOL_UNREGISTERED.format(
            code=agent_code, tool=tool_name,
        ))
    # ... rest of run() unchanged: _enforcePolicies / resolver / handler ...
```

### 4.6 `runnable` 字段服务端化

```python
# backend/app/services/agent_registry_service.py  agentToRead
read.runnable = (
    read.status == AgentStatus.ACTIVE.value
    and read.agent_code in agent_binding_cache._cache  # 改用缓存判断
)
```

前端 `AgentRegistryPage.runnable` 消费逻辑不变。

## 5. API 契约

### 5.1 `GET /api/v1/agents/options` 扩展

```python
class AgentToolOption(CamelModel):
    name: str
    description: str
    data_object: str
    data_layers: tuple[str, ...]

class AgentOptionsRead(CamelModel):
    domains: list[str]              # 已有
    layers: list[str]               # 已有
    tools: list[AgentToolOption]    # NEW
```

`agent_tool_registry` 新增 `all() -> list[AgentTool]`（按 name 排序，稳定）。

### 5.2 DTO 字段新增

```python
class AgentDefinitionCreate(CamelModel):
    # ... existing ...
    tool_name: str | None = Field(default=None, max_length=64)

class AgentDefinitionUpdate(CamelModel):
    # ... existing ...
    tool_name: str | None = Field(default=None, max_length=64)
```

### 5.3 错误消息（`error_messages.py` 追加）

```python
MSG_AGENT_TOOL_UNKNOWN = (
    "tool_name 必须是已注册工具之一（{registered}），收到 {name}"
)
MSG_AGENT_TOOL_LAYER_MISMATCH = (
    "tool_name={tool} 要求的 data_layers 包含 {missing}，"
    "需在 Agent 的 data_layers 中显式声明"
)
MSG_AGENT_TOOL_UNREGISTERED = (
    "agent_code={code} 绑定的 tool={tool} 在当前代码中未注册（环境漂移）"
)
```

### 5.4 行为契约（不变项）

| 端点 | 行为 | 状态码 |
|---|---|---|
| `POST /agents` 含合法 `tool_name` + 覆盖 layers | 创建成功 | 201 |
| `POST /agents` 含未知 `tool_name` | 拒绝 | 422 |
| `POST /agents` 含 `tool_name` 但 layers 不全 | 拒绝 | 422 |
| `POST /agents` 不含 `tool_name` | 允许（未绑定，可注册为元数据 Agent） | 201 |
| `PUT /agents/{id}` `tool_name: null` | 清空绑定 | 200 |
| `POST /agents/{code}/run` 无 binding | 拒绝 | 409 |
| `POST /agents/{code}/run` DB binding 不在代码 | 拒绝（防御漂移） | 409 |

### 5.5 Chat 路径

`IntentType.AGENT_RUN` → `AgentRuntimeService.run` 间接享受到 DB binding
切换的好处，chat 路径无额外改动。

## 6. 前端变更

### 6.1 类型扩展

```typescript
// frontend/src/types/agentOptions.ts
export interface AgentToolOption {
  name: string;
  description: string;
  dataObject: string;
  dataLayers: string[];
}

export interface AgentOptions {
  domains: string[];
  layers: string[];
  tools: AgentToolOption[];  // NEW
}
```

### 6.2 AgentRegistryPage Create/Edit Modal

在 dataLayers 字段之后插入 `toolName` Select：

```tsx
<Form.Item name="toolName" label={t("agentRegistry.fields.toolName")}
  rules={[{
    validator: (_, value) => {
      if (!value) return Promise.resolve();
      const tool = options.tools.find((x) => x.name === value);
      if (!tool) return Promise.reject(
        new Error(t("agentRegistry.errors.toolUnknown"))
      );
      const covered = dataLayers || [];
      const missing = tool.dataLayers.filter((l) => !covered.includes(l));
      if (missing.length > 0) return Promise.reject(
        new Error(t("agentRegistry.errors.toolLayerMismatch", {
          tool: value, missing: missing.join(","),
        }))
      );
      return Promise.resolve();
    },
  }]}>
  <Select allowClear placeholder={t("agentRegistry.fields.toolNamePlaceholder")}
    options={options.tools.map((tool) => ({
      value: tool.name,
      label: `${tool.name} — ${tool.description}`,
    }))}
    showSearch optionFilterProp="label" />
</Form.Item>
```

### 6.3 i18n 新增

```
agentRegistry.fields.toolName
agentRegistry.fields.toolNamePlaceholder
agentRegistry.errors.toolUnknown
agentRegistry.errors.toolLayerMismatch
```

zh-CN / en-US 各 4 条。

### 6.4 详情 Drawer 展示

`toolName` 仅展示（不允许 Drawer 内编辑，避免与 Edit Modal 重复入口）。

```tsx
{agent.toolName && (
  <Descriptions.Item label={t("agentRegistry.fields.toolName")}>
    <Tag color="blue">{agent.toolName}</Tag>
  </Descriptions.Item>
)}
```

### 6.5 `useAgentOptions()` 无改动

复用现有 hook；`options.tools` 自然随响应下发。

## 7. 迁移计划

### 7.1 Commit 序列

| # | Commit | 范围 | 验证 |
|---|---|---|---|
| 1 | `feat(agent): tool_name column + migration 0035` | Alembic + models.py + Pydantic schema | migration up/down |
| 2 | `feat(agent): startup seed agent_tool_bindings` | seed script + lifespan + AgentBindingCache（未启用） | 启动空 DB → seed 3 行；幂等 |
| 3 | `feat(agent): write-time tool_name validation` | `_validateToolName` + 3 条 MSG | 集成测试 5 条 |
| 4 | `feat(agent): extend /agents/options with tools` | AgentOptionsRead.tools + `registry.all()` | 集成测试 + 既有 options 保持绿 |
| 5 | `feat(agent): AgentBindingCache + runtime integration` | cache 模块 + 写时失效 + `_resolveTool` 改造（dict 仍保留 fallback） | e2e：run 路径走 DB |
| 6 | `refactor(agent): delete AGENT_TOOLS dict` | 删 dict；seed 改从 registry 派生；fixtures 改 DB | 全量回归 + coverage gate |
| 7 | `feat(frontend): toolName Select in Agent Registry` | 前端 §6 全部 | vitest + tsc + bundle |
| 8 | `docs(harness): feat-agent-tool-binding summary` | summary.md + Harness/wiki 更新 + memory | 文档一致性 |

每步独立可编译、可回滚；最后一步删 dict 是单向门（前面 5 步都有 fallback）。

### 7.2 启动顺序保证

```
lifespan startup:
  1. await seed_agent_tool_bindings(session)   # 幂等 seed
  2. await agent_binding_cache.warmUp(session) # 启动预热
```

任一失败 → 启动失败（fail-fast）。

### 7.3 回滚预案

- commit 1-5：直接 `git revert`（dict 还在，回滚路径完整）
- commit 6：dict 已删，回滚需要回填 `agent_definition.tool_name` 数据
  （destructive migration 防护：删 dict 前必须验证 seed 已成功）

## 8. 测试覆盖

### 8.1 Unit

- `AgentBindingCache.warmUp / getToolName / invalidate / refreshOne`
- `_validateToolName` 5 路径（合法 / None / 未知 / 层缺失 / Update 跳过 None）
- `agent_tool_registry.all()` 排序稳定性

### 8.2 Integration

- `POST /agents` 5 个 case（合法 / 未知 tool / 缺 layer / 清空 / Update None）
- `GET /agents/options` 含 tools 字段
- `POST /agents/{code}/run` 路径走 DB 缓存
- `POST /agents/{code}/run` DB 绑了但代码未注册 → 409（防御漂移）

### 8.3 E2E

- 启动空 DB → 3 行自动 seed → run 路径 200
- Admin 改 binding → ≤1 request 生效（cache 失效验证）
- 改 binding 为非法 → 422 → 旧 binding 不受影响

### 8.4 Security Review（强制）

按 `acl-security-review-pattern` 4 项审查：
- **DTO mass-assignment**：tool_name 必须是 Create/Update 显式字段
- **403/409 侧信道**：MSG_AGENT_NOT_RUNNABLE_NO_TOOL vs MSG_AGENT_TOOL_UNREGISTERED 是否泄露内部状态
- **actor 派生**：写权限仅 admin（与 ACL extension 一致）
- **非 admin 集成测试**：buyer 角色 `PUT /agents/{id}` 改 tool_name → 403

## 9. 风险登记

| 风险 | 缓解 |
|---|---|
| 启动顺序：seed 在 cache warmUp 之前失败 | lifespan 严格顺序：seed → warmUp；seed 失败 → 启动失败 |
| seed 脚本与 `AGENT_TOOLS` dict 双源漂移 | commit 6 删 dict；脚本改从 `agent_tool_registry` 派生 |
| DB tool_name 与代码 tool 漂移 | runtime 409 + 集成测试守护 |
| 多实例缓存不一致 | 当前单实例；未来多实例切 Redis（独立 change） |
| Pydantic field 校验顺序 | v2 按声明顺序；model_config 中显式 ordering 保证 data_layers 在 tool_name 之前 |
| **Update toolName 未传 vs null 歧义** | Pydantic v2 partial update 无法区分「字段未传」与「字段为 null」；当前 `Update.tool_name: str \| None = None` 会让前端 PATCH 不传 toolName 时静默清空 binding。**plans 阶段决定**：sentinel `UnsetType` vs `model_validator(mode="before")` vs 前端必传三种之一 |

## 10. 与既有 change 的耦合

| 既有 change | 关系 |
|---|---|
| `feat-agent-vocabulary`（已发） | `tool.data_layers` ⊂ `AGENT_DATA_LAYERS`；本期复用 |
| `feat-agent-runtime-mvp`（已发） | `_resolveTool` 改造点；其他逻辑不动 |
| `feat-acl-extension-3-entities`（已发） | 写权限 admin only，绕过 entity_mapping ACL |
| `feat-agent-registry`（已发） | Create/Update Modal / Detail Drawer 复用 |

## 11. 未来演进（非本期）

- 1:N 工具绑定（中间表 + 工具选择器）
- 工具 `description/data_object/data_layers` 入 DB（与本期解耦；需先解决 handler 入库的安全审核）
- Redis 共享缓存（多实例部署）
- Tool 自身的管理 UI（工具清单可视化）
