# Agent Tool Config DB-Backed Spec

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal**: Migrate hardcoded agent tool registry (`_buildRegistry()` in `backend/app/services/agent_tools.py:202-285`) to a DB-backed SSOT, so business users can CRUD tool metadata (`data_object` / `data_layers` / `handler_kind` / `handler_ref` / etc.) via an admin UI without code changes. Handler engines (Python callables) stay in code; only metadata moves to DB.

**Architecture**: Hybrid — DB SSOT for tool **metadata** + code registry for tool **handler engines**. Two-level:
1. **DB layer** (`agent_tool_config` table + `AgentToolConfigService` + `AgentToolConfigRegistry` + admin UI) — business users manage `data_object` / `data_layers` / `handler_kind` / `handler_ref` / `input_schema` / `enabled`.
2. **Code layer** (`BUILTIN_HANDLERS` dict + `NL2SQL_HANDLERS` dict + `ARG_EXTRACTORS` dict) — Python handlers stay in code, locked at import time. New handler engine requires code change + deploy.
3. **Assembly layer** (`AgentToolAssembly.assemble(config_row) -> AgentTool`) — reads DB row + looks up code-side handler by `handler_kind` / `handler_ref`, returns the runtime `AgentTool` instance.

**Tech Stack**: FastAPI + SQLAlchemy 2.x async + Alembic + Ant Design v5 + TypeScript + React + vitest. Reuses existing patterns: `KpiCatalogService` (outbox audit), `AgentBindingCache` (warmup + invalidate), `_assert_read_only` SQL safety gate (`feature_compute_service`).

---

## 1. Background

### 1.1 Current state

`backend/app/services/agent_tools.py` holds 3 hardcoded `AgentTool` instances inside `_buildRegistry()` (lines 202-285):

| Tool | data_object | data_layers | handler (Python) |
|---|---|---|---|
| `supplier_360` | `SUPPLIER` | `(DIM, FEATURE)` | `_supplier360Handler` |
| `supplier_risk` | `SUPPLIER` | `(DIM, FEATURE)` | `_supplierRiskHandler` |
| `graph_traverse` | `SUPPLIER` | `(DIM, DWD)` | `_graphTraverseHandler` |

`_buildRegistry()` is called once at module import time, producing a frozen module-level singleton `agent_tool_registry = _buildRegistry()` (line 289). `AgentRuntimeService` reads this singleton in `_resolveTool()` (line 149-155). **There is no hot-reload today.**

`AGENT_DEFAULT_BINDINGS` (lines 293-297) is a hardcoded dict mapping agent_code → tool_name. This is consumed only by `seed_agents.py`.

### 1.2 What's wrong

- **No admin manageability**: business users cannot add a new tool, change `data_object` for an existing tool, or define a new NL2SQL query without a code change + deploy.
- **No runtime mutation**: `_buildRegistry()` runs once at import time; the registry is immutable for the process lifetime.
- **`data_object` vocabulary is code-only**: business users have no way to discover or add new `data_object` values without touching `agent_tools.py`.

### 1.3 Recent precedents (already validated)

- `KpiCatalogService` (`kpi_catalog_service.py`) — DB-backed CRUD + outbox audit + companion history table.
- `AgentBindingCache` (`agent_binding_cache.py`) — DB-backed in-memory cache with `warmUp()` + `invalidate()` + `refreshOne()` pattern.
- `_assert_read_only()` (`business_db_pool.py:85`) — SQL safety gate: verb whitelist (`SELECT` / `WITH` only), multi-statement guard, hidden-write deep scan (DML in CTE/subqueries, `INTO`, `SHARE`, forbidden functions like `NEXTVAL`).
- `feature_compute_service.py` — closest precedent: `feature.calculation_logic` (a SQL string) stored in DB, validated by `_assert_read_only`, executed by code engine.

---

## 2. Goals & Non-Goals

### 2.1 Goals (v1)

1. Persist all 3 existing tools' metadata to DB; admin can CRUD via UI.
2. Add `NL2SQL` handler_kind so business users can configure NL2SQL tools.
3. `data_object` becomes DB-managed (free string but admin-visible).
4. `AgentRuntimeService` reads tool config from DB-backed registry with **lazy reload** on cache miss.
5. All CRUD writes audited via outbox pattern (consistent with `KpiCatalogService`).
6. UI: `/admin/tools` admin page with table + create/edit/toggle/delete.

### 2.2 Non-Goals (v1)

- ❌ `EXTERNAL_HTTP` handler_kind (deferred to v1.1; needs URL allowlist, timeout, auth, SSL, CORS).
- ❌ Dynamic handler / plugin upload (RCE risk; never expose).
- ❌ Handler reference as free string (only `Literal[...]` whitelist).
- ❌ Multi-instance cache sync (single-instance deployment; defer Redis).
- ❌ Agent-definition UI changes (AgentRegistryPage drawer upgrades its `dataObject` `<Input>` to `<Select>`, but no other agent UI changes).

### 2.3 Out of Scope

- ontology_service UPPERCASE entity_type drift fix (separate change).
- AdminAuditPage ENTITY_TYPE_OPTIONS display values (uppercase vs backend lowercase) cleanup (separate change).

---

## 3. Global Constraints

These bind every task. Repeating them verbatim from the project's rules:

- **File size**: 200-400 lines typical, **800 lines hard cap**. (CLAUDE.md)
- **Function size**: < 50 lines. (CLAUDE.md)
- **Nesting**: ≤ 4 levels (early returns). (CLAUDE.md)
- **Immutability**: always create new objects; never mutate in-place. (CLAUDE.md)
- **Python naming**: `snake_case` (project deviation from PEP 8 — keeps DB column / JSON contract aligned with frontend `camelCase`). (CLAUDE.md)
- **Test DB**: real PostgreSQL on port **5433** (not 5432), database `qa_metadata_test`, user `qa_user`, password `qa_pg_dev_2026`. **NO sqlite**. (CLAUDE.md)
- **Coverage gate**: `pytest app/tests/ --cov=app --cov-fail-under=80`. (CLAUDE.md)
- **Commit format**: `<type>: <description>`, **NO `Co-Authored-By:` trailer**. (CLAUDE.md)
- **SQL safety**: business queries SELECT-only via `_assert_read_only`. (CLAUDE.md)
- **Token metering**: every LLM call records tokens + cost. (CLAUDE.md)
- **Audit pattern**: `audit.record()` uses `session.add(row)` only (no commit); caller commits for transaction atomicity. (feat-audit-history-api)
- **`entity_type` convention**: lowercase ORM `__tablename__`. (feat-audit-history-api cross-task finding)
- **Working on main**: no worktree (project convention).

---

## 4. Architecture

### 4.1 Component map

```
┌─── Code Layer (locked at import time, requires deploy to change) ─────────┐
│                                                                            │
│  BUILTIN_HANDLERS: dict[str, AgentHandler] = {                            │
│      "supplier_360": _supplier360Handler,                                  │
│      "supplier_risk": _supplierRiskHandler,                                │
│      "graph_traverse": _graphTraverseHandler,                              │
│  }                                                                         │
│                                                                            │
│  NL2SQL_HANDLERS: dict[str, AgentHandler] = {                             │
│      "nl2sql_default": _nl2sqlDefaultHandler,                              │
│  }                                                                         │
│                                                                            │
│  ARG_EXTRACTORS: dict[str, ArgExtractor] = {                               │
│      "supplier_key": lambda raw: _supplierKeyArgs(raw, extractSupplierKey),│
│      "supplier_risk_key": lambda raw: _supplierKeyArgs(raw, extractSupplierRiskKey),│
│      "supplier_graph_key": lambda raw: _supplierKeyArgs(raw, extractSupplierGraphKey),│
│  }                                                                         │
│                                                                            │
│  AgentToolAssembly.assemble(config_row) -> AgentTool                       │
│    • reads data_object/data_layers/name/description/input_schema from row  │
│    • looks up handler by (handler_kind, handler_ref)                       │
│    • looks up arg_extractor by arg_extractor_kind                          │
└────────────────────────────────────────────────────────────────────────────┘
                                     ▲
                                     │ assembly on warmUp / reload_one
                                     │
┌─── DB Layer (admin-manageable, no code change needed) ───────────────────┐
│                                                                            │
│  agent_tool_config table:                                                  │
│    id, name (unique, immutable), description, data_object, data_layers,   │
│    input_schema, handler_kind, handler_ref, arg_extractor_kind,            │
│    enabled, version (optimistic lock), created_time, updated_time         │
│                                                                            │
│  AgentToolConfigService (CRUD + ACL + outbox audit)                       │
│  AgentToolConfigRegistry (DB-backed in-memory cache; warmUp + invalidate)  │
│  /api/v1/agent-tools/* REST endpoints                                     │
│  /admin/tools frontend page                                                │
└────────────────────────────────────────────────────────────────────────────┘
                                     ▲
                                     │ consumed by
                                     │
┌─── Runtime Layer (existing) ─────────────────────────────────────────────┐
│                                                                            │
│  AgentRuntimeService                                                       │
│    _resolveTool(session, name) -> AgentTool                                │
│      fast path: agent_tool_config_registry.get(name)                      │
│      cache miss: registry.reload_one(session, name)                       │
│      not found: ConflictError(409) — including disabled or deleted         │
│                                                                            │
│  _enforcePolicies(entity, tool)  ← reads tool.data_object / tool.data_layers│
└────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Key design choices

1. **Hybrid storage**: DB for metadata + code for handlers. Rationale: Python handlers are not safely serializable; DB stores only what changes per business need.
2. **`handler_kind` enum + `handler_ref` whitelist**: prevents injection of arbitrary handler references via UI. Adding new handler engine requires code change.
3. **`name` immutable**: prevents orphan `AgentDefinition.tool_name` references. Enforced at service layer + UI (no edit field).
4. **Lazy reload**: write operations only call `invalidate(name)`; next `_resolveTool()` triggers `reload_one(session, name)`. Avoids write-time cache rebuild storms.
5. **CHECK constraint on `handler_kind`**: DB-level safety net in case service layer has a bug.
6. **No history companion table**: tool changes are fully captured in `audit_log` (consistent with `entity_mapping_service`); diff/rollback not needed.

---

## 5. Data Model

### 5.1 New ORM model `AgentToolConfig`

```python
class AgentToolConfig(Base):
    """Agent工具配置表（feat-agent-tool-config-db, 2026-09-03）。

    元数据 SSOT：业务人员通过 /admin/tools UI CRUD；handler 引擎在代码。
    name 与 AgentDefinition.tool_name 形成 FK-by-name 引用，**不可改**。
    """
    __tablename__ = "agent_tool_config"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False,
        doc="工具名（小写+下划线；与 AgentDefinition.tool_name 对齐；创建后不可改）")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_object: Mapped[str] = mapped_column(String(128), nullable=False,
        doc="ACL 主体；写入时 _normalizeDataObject trim+upper")
    data_layers: Mapped[list[str]] = mapped_column(postgresql.JSONB, nullable=False, default=list)
    input_schema: Mapped[dict] = mapped_column(postgresql.JSONB, nullable=False, default=dict,
        doc="JSON Schema（v1 仅展示；未来 LLM function-calling 复用）")
    handler_kind: Mapped[str] = mapped_column(String(30), nullable=False,
        doc="枚举: BUILTIN | NL2SQL（CHECK 约束兜底）")
    handler_ref: Mapped[str] = mapped_column(String(64), nullable=False,
        doc="按 handler_kind 白名单校验")
    arg_extractor_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="supplier_key")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.text("true"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
        doc="乐观锁；每次 UPDATE +1")
    created_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "handler_kind IN ('BUILTIN','NL2SQL')",
            name="ck_agent_tool_config_handler_kind",
        ),
        Index("ix_agent_tool_config_enabled", "enabled"),
        Index("ix_agent_tool_config_data_object", "data_object"),
    )

    def __repr__(self) -> str:
        return f"<AgentToolConfig id={self.id} name={self.name} handler_kind={self.handler_kind}>"
```

### 5.2 New enum `AgentToolHandlerKind`

In `backend/app/domain/enums.py`:

```python
class AgentToolHandlerKind(str, Enum):
    BUILTIN = "BUILTIN"
    NL2SQL = "NL2SQL"
```

### 5.3 Alembic migration `0037_agent_tool_config`

`down_revision = "0036_audit_actor_index"` (linear chain).

```python
op.create_table(
    "agent_tool_config",
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column("name", sa.String(length=64), nullable=False),
    sa.Column("description", sa.Text(), nullable=True),
    sa.Column("data_object", sa.String(length=128), nullable=False),
    sa.Column("data_layers", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("input_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("handler_kind", sa.String(length=30), nullable=False),
    sa.Column("handler_ref", sa.String(length=64), nullable=False),
    sa.Column("arg_extractor_kind", sa.String(length=64), nullable=False, server_default="supplier_key"),
    sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("created_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    sa.Column("updated_time", sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("name", name="uq_agent_tool_config_name"),
    sa.CheckConstraint("handler_kind IN ('BUILTIN','NL2SQL')", name="ck_agent_tool_config_handler_kind"),
)
op.create_index("ix_agent_tool_config_enabled", "agent_tool_config", ["enabled"])
op.create_index("ix_agent_tool_config_data_object", "agent_tool_config", ["data_object"])
```

Downgrade: `op.drop_table("agent_tool_config")` (no external dependencies; safe).

---

## 6. Service Layer

### 6.1 `AgentToolConfigService` (`backend/app/services/agent_tool_config_service.py`)

Constructor: `__init__(self, acl: AclService | None = None, outbox: OutboxService | None = None)` — DI-compatible for testing.

Methods (all write methods take `actor: CurrentUser`):

| Method | Signature | Errors | Audit |
|---|---|---|---|
| `listTools` | `(session, *, enabledOnly=False) -> list[AgentToolConfig]` | — | — |
| `getTool` | `(session, name) -> AgentToolConfig` | `NotFoundError(404)` | — |
| `createTool` | `(session, dto: AgentToolConfigCreate, actor) -> AgentToolConfig` | `ConflictError(409)` on name dup; `ValidationError(422)` on bad handler_kind/ref | outbox `agent_tool_created` |
| `updateTool` | `(session, name, dto: AgentToolConfigUpdate, actor) -> AgentToolConfig` | `NotFoundError`; `ConflictError(409)` on version mismatch | outbox `agent_tool_updated` |
| `deleteTool` | `(session, name, actor) -> None` | `NotFoundError`; `ConflictError(409)` if referenced by AgentDefinition | outbox `agent_tool_deleted` |
| `toggleEnabled` | `(session, name, enabled, actor) -> AgentToolConfig` | `NotFoundError` | outbox `agent_tool_toggled` |
| `upsertSeed` | `(session, name, fields) -> AgentToolConfig` | — (internal; bypasses ACL) | — (seed only) |

**Validation rules (service layer)**:

- `name`: pattern `^[a-z][a-z0-9_]*$`; max 64 chars; **immutable** (Update DTO has no `name` field).
- `data_object`: `len 1-128`; service calls `_normalizeDataObject(value)` (strip + upper).
- `data_layers`: each element must be in `AGENT_DATA_LAYERS` (`("DIM","DWD","FEATURE")`); empty list allowed (layer-agnostic tool, legacy compat).
- `input_schema`: must be a JSON object (Pydantic `dict` validator).
- `handler_kind + handler_ref` combo: validated against `_VALID_HANDLER_REFS`:
  - `BUILTIN` → `{"supplier_360", "supplier_risk", "graph_traverse"}`
  - `NL2SQL` → `{"nl2sql_default"}`
- `arg_extractor_kind`: must be in `ARG_EXTRACTORS` (v1: `supplier_key` / `supplier_risk_key` / `supplier_graph_key`).
- `version` in `AgentToolConfigUpdate`: required, must match current row's version → otherwise `ConflictError(409)`.

**ACL**:

- Write operations (`createTool` / `updateTool` / `deleteTool` / `toggleEnabled`): require admin role (use `getAdminOnlyActor` at API layer; service trusts upstream ACL).
- `upsertSeed`: bypass ACL (seed scripts only).

**Audit pattern (per task, write path)**:

```python
# Inside createTool, AFTER session.flush(), BEFORE commit:
row = AgentToolConfig(name=..., ...)
session.add(row)
await session.flush()  # populate row.id

await self._outbox.enqueue(
    session,
    event_type="agent_tool_created",
    entity_type="agent_tool_config",  # lowercase ORM tablename
    entity_id=row.id,
    actor=actor.userId,
    actor_departments=tuple(actor.departments or []),
    payload={"before": None, "after": _configRowToDict(row)},
)
# Caller commits (transaction atomicity).
```

### 6.2 `AgentToolConfigRegistry` (`backend/app/services/agent_tool_config_registry.py`)

```python
class AgentToolConfigRegistry:
    """DB-backed 工具注册表（替代硬编码 agent_tool_registry）。

    warmUp：lifespan 调用，加载所有 enabled=True 行并装配为 AgentTool。
    get(name)：快路径；cache miss 时调用方需 reload_one(session, name)。
    invalidate(name)：写时失效，下次 _resolveTool 触发 reload_one。
    """
```

Methods:

| Method | Signature | Behavior |
|---|---|---|
| `warmUp` | `async (session) -> None` | Lock-protected bulk load; assemble all enabled rows |
| `get` | `(name: str) -> AgentTool \| None` | Sync fast path; raises `RuntimeError` if not warmed |
| `has` | `(name: str) -> bool` | |
| `all` | `() -> list[AgentTool]` | Sorted by name |
| `listEnabled` | `() -> list[AgentTool]` | Only enabled=True (warmUp already filters) |
| `invalidate` | `(name: str \| None = None) -> None` | Write-time; clears single or all |
| `reload_one` | `async (session, name) -> None` | Lock-protected single-row reload; re-assembles |

Module-level singleton: `agent_tool_config_registry = AgentToolConfigRegistry()`.

### 6.3 `AgentToolAssembly.assemble`

Static helper (in `agent_tools.py`):

```python
class AgentToolAssembly:
    @staticmethod
    def assemble(config_row: AgentToolConfig) -> AgentTool:
        # 1. handler_kind + handler_ref 校验
        # 2. 查 BUILTIN_HANDLERS / NL2SQL_HANDLERS
        # 3. 查 ARG_EXTRACTORS
        # 4. _normalizeDataObject + _normalizeDataLayer 归一化
        # 5. 构造 AgentTool(...)
```

### 6.4 Code-side handler dictionaries

In `backend/app/services/agent_tools.py`:

```python
BUILTIN_HANDLERS: dict[str, AgentHandler] = {
    "supplier_360": _supplier360Handler,
    "supplier_risk": _supplierRiskHandler,
    "graph_traverse": _graphTraverseHandler,
}

NL2SQL_HANDLERS: dict[str, AgentHandler] = {
    "nl2sql_default": _nl2sqlDefaultHandler,
}

ARG_EXTRACTORS: dict[str, ArgExtractor] = {
    "supplier_key": lambda raw: _supplierKeyArgs(raw, extractSupplierKey),
    "supplier_risk_key": lambda raw: _supplierKeyArgs(raw, extractSupplierRiskKey),
    "supplier_graph_key": lambda raw: _supplierKeyArgs(raw, extractSupplierGraphKey),
}

_VALID_HANDLER_REFS: dict[str, frozenset[str]] = {
    AgentToolHandlerKind.BUILTIN.value: frozenset(BUILTIN_HANDLERS.keys()),
    AgentToolHandlerKind.NL2SQL.value: frozenset(NL2SQL_HANDLERS.keys()),
}
```

### 6.5 New handler `_nl2sqlDefaultHandler`

```python
async def _nl2sqlDefaultHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """NL2SQL handler：业务人员配的自然语言查询经 NL2SqlService 翻译执行。

    v1 复用 NL2SqlService.translate + _assert_read_only SQL 安全网关
    （与 feature_compute_service 同模式）。
    """
    question = args["question"]
    result = await NL2SqlService().translate(
        session, question=question, user_id=ctx.actor, llm_factory=ctx.llm_factory
    )
    return ToolResult(
        data=result.model_dump(mode="json", by_alias=True),
        answer=result.answer or "查询完成",
        tokens_used=result.tokens_used,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost=result.cost,
        llm_model_name=result.llm_model_name,
    )
```

### 6.6 New helper `_normalizeDataObject`

In `backend/app/domain/schemas.py` (alongside `_normalizeDataLayer`):

```python
def _normalizeDataObject(value: str | None) -> str:
    """data_object 归一化：strip + upper。空串 → ValueError（422）。"""
    if value is None:
        raise ValueError("data_object 不能为空")
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("data_object 不能为空字符串")
    return normalized
```

---

## 7. Runtime Layer

### 7.1 `AgentRuntimeService` changes

- `__init__`: replace `registry: AgentToolRegistry | None = None` with `registry: AgentToolConfigRegistry | None = None` (default: `agent_tool_config_registry`).
- `_resolveTool`: convert to `async (session, name) -> AgentTool`:
  ```python
  async def _resolveTool(self, session, name):
      tool = self._registry.get(name)  # sync fast path
      if tool is None:
          await self._registry.reload_one(session, name)  # cache miss → reload
          tool = self._registry.get(name)
      if tool is None:
          raise ConflictError(MSG_AGENT_NOT_RUNNABLE.format(
              code=name, status="tool_unbound_or_disabled"))
      return tool
  ```
- `run()`: pass `session` to `_resolveTool`.

### 7.2 Lifespan integration

In `backend/app/main.py` `lifespan()` (after line 105):

```python
from app.services.agent_tool_config_registry import agent_tool_config_registry
from scripts.seed_agent_tool_configs import seedAgentToolConfigs

async with session_factory() as session:
    await seedAgentToolConfigs(session)  # idempotent upsert
    await agent_binding_cache.warmUp(session)
    await agent_tool_config_registry.warmUp(session)  # NEW
```

### 7.3 Files removed (post-migration)

In `backend/app/services/agent_tools.py`:

- `_buildRegistry()` function (lines 202-285) — DELETE entirely.
- `agent_tool_registry = _buildRegistry()` module singleton (line 289) — DELETE.
- `AGENT_DEFAULT_BINDINGS` dict (lines 293-297) — DELETE (semantics now in `AgentDefinition.tool_name` + `AgentBindingCache`).

**Retained**:

- `AgentTool`, `AgentToolContext`, `ToolResult`, `AgentToolRegistry`, `AgentHandler`, `ArgExtractor` type definitions.
- `_supplier360Handler`, `_supplierRiskHandler`, `_graphTraverseHandler`, `_supplierKeyArgs`, `_keyOrNone` functions (moved into module-level scope, no longer nested in `_buildRegistry`).
- New `_nl2sqlDefaultHandler`, `BUILTIN_HANDLERS`, `NL2SQL_HANDLERS`, `ARG_EXTRACTORS`, `_VALID_HANDLER_REFS`, `AgentToolAssembly`.

---

## 8. Seed

### 8.1 New `backend/scripts/seed_agent_tool_configs.py`

Idempotent upsert of 3 built-in tools. Runs at every lifespan startup.

```python
TOOL_SEEDS = [
    {
        "name": "supplier_360",
        "description": "查询单供应商 360° 视图（主数据 + 交付/质量/价格表现 + 跨系统编码）",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "FEATURE"],
        "handler_kind": "BUILTIN",
        "handler_ref": "supplier_360",
        "arg_extractor_kind": "supplier_key",
        "enabled": True,
    },
    {
        "name": "supplier_risk",
        "description": "评估单供应商风险等级（RISK_SCORE 主路径 + LLM 风险点）",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "FEATURE"],
        "handler_kind": "BUILTIN",
        "handler_ref": "supplier_risk",
        "arg_extractor_kind": "supplier_risk_key",
        "enabled": True,
    },
    {
        "name": "graph_traverse",
        "description": "供应链链路推理：从供应商出发的多跳可达业务实体",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "DWD"],
        "handler_kind": "BUILTIN",
        "handler_ref": "graph_traverse",
        "arg_extractor_kind": "supplier_graph_key",
        "enabled": True,
    },
]

async def seedAgentToolConfigs(session) -> int:
    """幂等 upsert：name 命中 → 更新元数据；未命中 → 插入。返回改动行数。"""
    service = AgentToolConfigService()
    changed = 0
    for seed in TOOL_SEEDS:
        existing = await session.execute(
            select(AgentToolConfig).where(AgentToolConfig.name == seed["name"])
        )
        row = existing.scalar_one_or_none()
        if row is None:
            await service.upsertSeed(session, seed["name"], seed)
            changed += 1
        elif _needsUpdate(row, seed):
            await service.upsertSeed(session, seed["name"], seed)
            changed += 1
    return changed
```

### 8.2 Old `AGENT_DEFAULT_BINDINGS`

Migration impact: existing `seed_agents.py` references `AGENT_DEFAULT_BINDINGS` to determine which agents get policies (`_policiesFor`). After this change:

- `AGENT_DEFAULT_BINDINGS` is deleted from `agent_tools.py`.
- `seed_agents.py` must be updated to **read from DB**: query `AgentToolConfig.data_object` / `data_layers` for each agent's bound tool via the registry.

Concretely, `_policiesFor(code)` in `seed_agents.py` becomes:

```python
async def _policiesFor(session, code) -> list[AgentAccessPolicyCreate]:
    service = AgentRegistryService()
    agent = await service.getAgent(session, code)
    if agent.tool_name is None:
        return list(_DEFAULT_POLICIES)  # 元数据 Agent
    tool = agent_tool_config_registry.get(agent.tool_name)
    if tool is None:
        # tool 未启用或被删 → 回退通配（保持原行为）
        return list(_DEFAULT_POLICIES)
    if not tool.data_layers:
        return list(_DEFAULT_POLICIES)
    return [
        AgentAccessPolicyCreate(
            data_object=tool.data_object,
            permission=AgentPermission.READ,
            data_layer=layer,
            notes="显式分层（最小权限；与运行时对齐）",
        )
        for layer in tool.data_layers
    ]
```

`seedAgents` (the orchestrator) must now `async` call `_policiesFor(session, code)`.

---

## 9. API Layer

### 9.1 New `backend/app/api/v1/agent_tools.py`

| Method | Path | ACL | Purpose |
|---|---|---|---|
| GET | `/api/v1/agent-tools` | `getCurrentUser` | List (supports `?enabledOnly=true`) |
| GET | `/api/v1/agent-tools/{name}` | `getCurrentUser` | Detail |
| POST | `/api/v1/agent-tools` | `getAdminOnlyActor` | Create |
| PUT | `/api/v1/agent-tools/{name}` | `getAdminOnlyActor` | Update (with optimistic lock via `If-Match: version=N` header or `version` in body) |
| DELETE | `/api/v1/agent-tools/{name}` | `getAdminOnlyActor` | Delete (409 if used by AgentDefinition) |
| POST | `/api/v1/agent-tools/{name}/toggle` | `getAdminOnlyActor` | Toggle enabled |

Register in `backend/app/main.py` alongside existing `agents` router.

### 9.2 DTO schemas

In `backend/app/domain/schemas.py`:

```python
class AgentToolConfigBase(CamelModel):
    description: str | None = Field(default=None, max_length=2000)
    data_object: str = Field(min_length=1, max_length=128)
    data_layers: list[str] = Field(default_factory=list)
    input_schema: dict = Field(default_factory=dict)
    handler_kind: AgentToolHandlerKind
    handler_ref: str = Field(min_length=1, max_length=64)
    arg_extractor_kind: str = Field(default="supplier_key", max_length=64)

class AgentToolConfigCreate(AgentToolConfigBase):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")

class AgentToolConfigUpdate(CamelModel):
    description: UnsetType | str | None  # UnsetType distinguishes "not provided"
    data_object: UnsetType | str
    data_layers: UnsetType | list[str]
    input_schema: UnsetType | dict
    handler_kind: UnsetType | AgentToolHandlerKind
    handler_ref: UnsetType | str
    arg_extractor_kind: UnsetType | str
    enabled: UnsetType | bool
    version: int  # required for optimistic lock

class AgentToolConfigRead(AgentToolConfigBase):
    id: int
    name: str
    version: int
    enabled: bool
    created_time: datetime
    updated_time: datetime | None
```

### 9.3 Existing `GET /agents/options` change

`backend/app/api/v1/agents.py:84-108` already consumes `agent_tool_registry.all()`. After this change:

```python
tools=[
    AgentToolOption(
        name=t.name,
        description=t.description,
        data_object=t.data_object,
        data_layers=list(t.data_layers),
    )
    for t in agent_tool_config_registry.all()  # was: agent_tool_registry.all()
]
```

---

## 10. Frontend Layer

### 10.1 New `frontend/src/pages/AdminToolsPage.tsx`

Layout (Ant Design v5):

- Top bar: `Button` (新建) + `Input` (搜索) + `Button` (刷新).
- Table columns:
  - `name` (link to detail/edit)
  - `description` (truncated 50 chars)
  - `data_object` (Tag)
  - `data_layers` (list of Tag, max 3 visible)
  - `handler_kind` (Tag, color by kind)
  - `handler_ref`
  - `enabled` (Switch with Popconfirm)
  - `updated_time` (relative time)
  - Actions: 编辑 / 切换 / 删除
- Create / edit `Modal` + `Form`:
  - `name`: Input (disabled in edit mode)
  - `description`: TextArea (max 2000 chars)
  - `data_object`: Select (options derived from `useAgentOptions().tools.map(t => t.dataObject)` deduplicated, sorted, with `<Select.Option value="__custom__">自定义…</Select.Option>` for free input via `showSearch`)
  - `data_layers`: multi-Select (options: `AGENT_DATA_LAYERS`)
  - `input_schema`: `Input.TextArea` (JSON, validated client-side)
  - `handler_kind`: Select (BUILTIN / NL2SQL)
  - `handler_ref`: Select (filtered by handler_kind)
  - `arg_extractor_kind`: Select (filtered by handler_kind; default `supplier_key`)
- Delete: `Popconfirm` + custom message ("该工具被 Agent X 引用，无法删除" handled by 409 error display).

### 10.2 New API client `frontend/src/api/agentTools.ts`

```typescript
export async function listAgentTools(params: { enabledOnly?: boolean } = {}): Promise<AgentToolConfig[]>
export async function getAgentTool(name: string): Promise<AgentToolConfig>
export async function createAgentTool(payload: AgentToolConfigCreate): Promise<AgentToolConfig>
export async function updateAgentTool(name: string, payload: AgentToolConfigUpdate): Promise<AgentToolConfig>
export async function deleteAgentTool(name: string): Promise<void>
export async function toggleAgentTool(name: string, enabled: boolean): Promise<AgentToolConfig>
```

### 10.3 New types `frontend/src/types/agentTool.ts`

Mirrors backend CamelModel fields exactly.

### 10.4 i18n keys

In `frontend/src/i18n/zh-CN.ts` and `en-US.ts`, add `agentTools.*` namespace:

```ts
agentTools: {
  title: "工具配置管理",
  columns: { name, description, dataObject, dataLayers, handlerKind, handlerRef, enabled, updatedTime },
  actions: { create, edit, delete, toggle, refresh },
  form: { ... },  // field labels and placeholders
  messages: { created, updated, deleted, toggled, failed },
  errors: {
    versionConflict: "版本冲突，请刷新后重试",
    inUseByAgent: "该工具被 Agent {agentCodes} 引用，无法删除",
    nameImmutable: "工具名创建后不可修改",
  },
}
```

### 10.5 Route registration

In `frontend/src/App.tsx`, add adjacent to `admin/audit`:

```tsx
<Route path="admin/tools" element={<AdminToolsPage />} />
```

### 10.6 AgentRegistryPage upgrade

`AgentRegistryPage.tsx:670-680` currently uses `<Input>` for `dataObject`. Replace with `<Select>` (sourced from `useAgentOptions().tools.map(t => t.dataObject)` deduplicated). Single file edit, ~10 lines.

### 10.7 AdminAuditPage adapt

In `frontend/src/pages/AdminAuditPage.tsx:65-76`, add `{value: "agent_tool_config", label: "AGENT_TOOL_CONFIG"}` to `ENTITY_TYPE_OPTIONS`.

---

## 11. Testing Strategy

### 11.1 Unit tests

| File | Coverage |
|---|---|
| `tests/unit/test_agent_tool_config_service.py` | CRUD signatures, validation (name pattern, handler_kind/ref combo, data_layers vocabulary, version optimistic lock) |
| `tests/unit/test_agent_tool_config_registry.py` | warmUp / get / invalidate / reload_one / has / all / listEnabled; concurrent reload_one locking |
| `tests/unit/test_agent_tool_assembly.py` | assemble() correctly maps config_row to AgentTool; rejects bad handler_kind/ref |

### 11.2 Integration tests (real PostgreSQL on 5433)

| File | Cases |
|---|---|
| `tests/integration/test_agent_tool_config_api.py` | 6 endpoints × 2-3 case = ~18: happy path, ACL deny, version conflict, agent-reference check on delete |
| `tests/integration/test_agent_tool_config_audit.py` | 6 cases: CREATE / UPDATE / DELETE / TOGGLE → audit_log has correct row (drainOutbox + assert) |
| `tests/integration/test_agent_tool_runtime_db_driven.py` | 8 cases: runtime uses DB config; enabled=false → 409; new tool effective after next call; NL2SQL handler end-to-end (sk mocked) |

### 11.3 Frontend tests

| File | Cases |
|---|---|
| `frontend/src/tests/AdminToolsPage.test.tsx` | 5 cases: list render, create, edit (incl. optimistic-lock conflict handling), delete, toggle enabled |

### 11.4 Coverage gate

`pytest app/tests/ --cov=app --cov-fail-under=80` must pass.

---

## 12. Risks

| # | Risk | Level | Mitigation |
|---|---|---|---|
| 1 | `description` becomes prompt injection if routed to LLM | MEDIUM | v1: `description` only used for display. Spec explicitly states "**never** inject `description` into any LLM prompt". Frontend warns in i18n. |
| 2 | NL2SQL handler SQL injection | MEDIUM | Reuse `_assert_read_only()` (validated in `feature_compute_service`). Service-layer defensive re-check. |
| 3 | Hot reload races concurrent `run()` calls | LOW | `agent_tool_config_registry._lock` (asyncio.Lock) on reload_one; write only invalidates, doesn't push. |
| 4 | `name` rename → orphan `AgentDefinition.tool_name` references | LOW | Service rejects `UPDATE name` with 422; UI has no name edit field. |
| 5 | Delete referenced tool → dangling `AgentDefinition.tool_name` | MEDIUM | DELETE checks `SELECT 1 FROM agent_definition WHERE tool_name = :name LIMIT 1`; non-empty → 409. |
| 6 | DB/code drift between seed and code registries | LOW | Seed script runs every startup; idempotent upsert. |
| 7 | `_nl2sqlDefaultHandler` reuses NL2SqlService; new LLM cost surface | LOW | Only enabled if admin explicitly creates a tool with handler_kind=NL2SQL. |

---

## 13. Deployment & Rollback

### 13.1 Deploy steps

```bash
cd backend
alembic upgrade head              # → 0037_agent_tool_config
python scripts/seed_agent_tool_configs.py  # idempotent upsert (3 tools)
docker compose restart backend    # or kubectl rollout
# log: AgentToolConfigRegistry warmed up: 3 tools
```

### 13.2 Smoke test

```bash
curl -H "X-User-Id: admin" -H "X-User-Roles: admin" \
  http://localhost:8000/api/v1/agent-tools | jq '. | length'
# Expected: 3
```

### 13.3 Rollback

```bash
alembic downgrade -1              # drops agent_tool_config
git revert <merge_commit>         # restores _buildRegistry()
docker compose restart backend
```

No data migration needed (table is new, empty before seed).

---

## 14. File-by-File Change List

### Backend (new files)

1. `backend/alembic/versions/0037_agent_tool_config.py`
2. `backend/app/services/agent_tool_config_service.py`
3. `backend/app/services/agent_tool_config_registry.py`
4. `backend/app/api/v1/agent_tools.py`
5. `backend/scripts/seed_agent_tool_configs.py`

### Backend (modified files)

6. `backend/app/domain/models.py` — add `AgentToolConfig` ORM class
7. `backend/app/domain/enums.py` — add `AgentToolHandlerKind`
8. `backend/app/domain/schemas.py` — add `AgentToolConfigBase/Create/Update/Read` + `_normalizeDataObject`
9. `backend/app/services/agent_tools.py` — refactor: add BUILTIN_HANDLERS / NL2SQL_HANDLERS / ARG_EXTRACTORS / _nl2sqlDefaultHandler / AgentToolAssembly; delete `_buildRegistry()` / `agent_tool_registry` / `AGENT_DEFAULT_BINDINGS`
10. `backend/app/services/agent_runtime_service.py` — update `_resolveTool` to async + session + new registry
11. `backend/app/api/v1/agents.py` — line 106 `agent_tool_registry.all()` → `agent_tool_config_registry.all()`
12. `backend/app/main.py` — lifespan: call `seedAgentToolConfigs` + `agent_tool_config_registry.warmUp`
13. `backend/scripts/seed_agents.py` — refactor `_policiesFor` to async + DB-driven

### Frontend (new files)

14. `frontend/src/pages/AdminToolsPage.tsx`
15. `frontend/src/api/agentTools.ts`
16. `frontend/src/types/agentTool.ts`
17. `frontend/src/tests/AdminToolsPage.test.tsx`

### Frontend (modified files)

18. `frontend/src/App.tsx` — add `/admin/tools` route
19. `frontend/src/pages/AgentRegistryPage.tsx` — line 676 `<Input>` → `<Select>`
20. `frontend/src/pages/AdminAuditPage.tsx` — add `agent_tool_config` to `ENTITY_TYPE_OPTIONS`
21. `frontend/src/i18n/zh-CN.ts` + `en-US.ts` — add `agentTools.*` namespace

### Total: ~21 files, ~1500-2000 lines.

---

## 15. Out-of-Scope Followups (Parked)

- ontology_service UPPERCASE `entity_type` drift (T5 audit-history-api parked inconsistency).
- AdminAuditPage `ENTITY_TYPE_OPTIONS` display values (uppercase vs backend lowercase) cleanup.
- v1.1: `EXTERNAL_HTTP` handler_kind with URL allowlist + timeout + auth.
- v1.1: Multi-instance cache sync via Redis.
- v1.x: Externalize handler assembly to plugin loader (only after a documented threat model).