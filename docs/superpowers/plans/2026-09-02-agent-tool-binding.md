# Agent 工具绑定可配置化（feat-agent-tool-binding）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `Agent → Tool` 关系从硬编码 `AGENT_TOOLS` dict 升级到 PostgreSQL，让 Admin 通过 Agent Registry UI 配置即可生效，无需发版。

**Architecture:** 在 `agent_definition` 表加 `tool_name` 列 + 启动时幂等 seed；运行时通过 `AgentBindingCache`（启动预热 + 写时失效）取 binding；写入边界 Pydantic `field_validator("tool_name")` 跨字段校验 `data_layers ⊇ tool.data_layers`；前端在 AgentRegistryPage Create/Edit Modal 加 `toolName` Select，候选来自扩展后的 `/agents/options.tools`。

**Tech Stack:**
- Backend: Python 3.x, FastAPI, SQLAlchemy 2.0.50 async, Pydantic v2, Alembic, asyncpg, pytest, pytest-asyncio
- Frontend: TypeScript, React 18, Ant Design 5, Vite, axios + qs, vitest, @testing-library/react
- DB: PostgreSQL 16（port 5433 测试 / port 5432 生产）

**Spec:** `docs/superpowers/specs/2026-09-02-agent-tool-binding-design.md`

## Global Constraints

> 这些约束对每个 task 隐式生效，reviewer 会逐项核对。复制自 spec + 项目既有规则。

- **测试 DB URL**：`postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test`
- **生产 DB URL**：`postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata`（与测试同实例不同 DB）
- **Backend coverage gate**：`uv run pytest app/tests/ --cov=app --cov-fail-under=80`
- **Backend 测试**：必须真实 PG + 完整 API 链路 + 真实 DB 对象，禁止 sqlite 内存库
- **Frontend type-check**：`cd frontend && npx tsc --noEmit`
- **Frontend 测试**：`cd frontend && npx vitest run`
- **命名**：Python 函数/变量 `snake_case`（与 DB 列及 JSON 契约一致，刻意偏离 PEP 8）；Pydantic 字段 `snake_case` + CamelModel alias 自动转 `camelCase`；TS 字段 `camelCase`；TS 接口 `PascalCase`；TS 函数 `camelCase`
- **文件大小**：200-400 行典型，800 行上限；函数 < 50 行；嵌套 ≤ 4 层
- **不可变数据**：所有写函数返回新对象，不原地修改
- **错误信封**：业务错误抛 `NotFoundError`/`ConflictError`/`PermissionDeniedError`/`ValidationError`，由全局 handler 转 `{code, message, ...}`
- **Token 计量**：每次 LLM 调用必须记录 token + cost（本期不涉及 LLM 调用，仅 audit 字段准备）
- **Commit 格式**：`<type>: <description>`，不带 Co-Authored-By trailer
- **工作分支**：直接 main（项目约定，无 worktree）
- **Docker 重建**：前端 bundle 必须 `docker compose build --no-cache frontend && up -d frontend`，否则 bundle hash 不变
- **Security review**：含 ACL/写路径的 task 完成后必须用 `security-reviewer` 审查（DTO mass-assignment / 403 侧信道 / actor 派生 / 非 admin 集成测试）
- **现有词表 SSOT**：`backend/app/domain/agent_vocabulary.py` 的 `AGENT_DATA_DOMAINS=(PROCUREMENT,QUALITY,LOGISTICS)` + `AGENT_DATA_LAYERS=(DIM,DWD,FEATURE)`；新增词汇必须先扩展该文件
- **现有工具注册表**：`backend/app/services/agent_tools.py` 的 `agent_tool_registry` + `AGENT_TOOLS` dict（本期逐步删除）
- **既有约束**：`tool.data_layers ⊂ AGENT_DATA_LAYERS`；`agent.data_layers ⊂ AGENT_DATA_LAYERS`；`agent.data_domains ⊂ AGENT_DATA_DOMAINS`

---

## Task 1: Alembic 0035 + agent_definition.tool_name 列

**Files:**
- Create: `backend/alembic/versions/0035_agent_tool_binding.py`
- Modify: `backend/app/domain/models.py:1208-1255`（`AgentDefinition` 类体）
- Modify: `backend/app/domain/schemas.py`（`AgentDefinitionCreate` + `AgentDefinitionUpdate` + `AgentDefinitionRead` 加 `tool_name: str | None` + `tool_name_updated_at: datetime | None`）
- Test: `backend/app/tests/integration/test_agent_tool_binding_migration.py`

**Interfaces:**
- Consumes: 现有 `AgentDefinition` 模型（`agent_code`, `status`, `data_domains`, `data_layers`, `policies`）
- Produces:
  - `AgentDefinition.tool_name: str | None`（64 字符）
  - `AgentDefinition.tool_name_updated_at: datetime | None`（tz-aware）
  - Alembic 0035 up/down 可逆
  - DTO `tool_name: str | None = Field(default=None, max_length=64)` + `tool_name_updated_at: datetime | None`

- [ ] **Step 1: Write failing integration test**

```python
# backend/app/tests/integration/test_agent_tool_binding_migration.py
"""验证 Alembic 0035 + AgentDefinition.tool_name 字段就位。"""
from sqlalchemy import text
import pytest
from app.infrastructure.database import getSessionFactory
from app.domain.models import AgentDefinition

@pytest.mark.asyncio
async def test_agent_definition_has_tool_name_column():
    factory = getSessionFactory()
    async with factory() as session:
        rows = await session.execute(text("""
            SELECT column_name, data_type, is_nullable, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'agent_definition'
              AND column_name IN ('tool_name', 'tool_name_updated_at')
            ORDER BY column_name
        """))
        cols = {r.column_name: r for r in rows}
    assert "tool_name" in cols
    assert cols["tool_name"].data_type == "character varying"
    assert cols["tool_name"].is_nullable == "YES"
    assert cols["tool_name"].character_maximum_length == 64
    assert "tool_name_updated_at" in cols
    assert cols["tool_name_updated_at"].is_nullable == "YES"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_tool_binding_migration.py -v
```

Expected: FAIL with `KeyError: 'tool_name'` 或列不存在错误。

- [ ] **Step 3: Write Alembic migration 0035**

```python
# backend/alembic/versions/0035_agent_tool_binding.py
"""add tool_name + tool_name_updated_at to agent_definition

Revision ID: 0035_agent_tool_binding
Revises: 0034_entity_mapping_name_index
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0035_agent_tool_binding"
down_revision = "0034_entity_mapping_name_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_definition",
        sa.Column("tool_name", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_definition",
        sa.Column(
            "tool_name_updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_definition", "tool_name_updated_at")
    op.drop_column("agent_definition", "tool_name")
```

- [ ] **Step 4: Apply migration to test DB + verify test passes**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run alembic upgrade head
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_tool_binding_migration.py -v
```

Expected: PASS.

- [ ] **Step 5: Modify `AgentDefinition` model**

```python
# backend/app/domain/models.py  AgentDefinition 类体（line ~1208 后追加）
tool_name: Mapped[str | None] = mapped_column(
    String(64), nullable=True,
    doc="绑定的工具名；None = 未绑定（不可运行）",
)
tool_name_updated_at: Mapped[datetime | None] = mapped_column(
    TIMESTAMP(timezone=True), nullable=True,
    doc="tool_name 上次更新时间（用于审计）",
)
```

- [ ] **Step 6: Modify Pydantic DTO**

```python
# backend/app/domain/schemas.py  AgentDefinitionCreate / Update / Read 三处都加：
tool_name: str | None = Field(default=None, max_length=64)
tool_name_updated_at: datetime | None = Field(default=None)
```

- [ ] **Step 7: Run full backend regression to ensure no breakage**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```

Expected: ≥ 80% coverage；既有测试全绿；tool_name 字段为 None 时不影响任何现有断言。

- [ ] **Step 8: Commit**

```bash
git add backend/alembic/versions/0035_agent_tool_binding.py \
        backend/app/domain/models.py \
        backend/app/domain/schemas.py \
        backend/app/tests/integration/test_agent_tool_binding_migration.py
git commit -m "feat(agent): tool_name + tool_name_updated_at columns (0035 migration)

为 agent_definition 表加 tool_name 列（绑定工具名，可空）与
tool_name_updated_at（更新时间戳，可空）。零业务行为变更；为
feat-agent-tool-binding 后续 task 铺路。"
```

---

## Task 2: 启动 seed 脚本 + lifespan 集成

**Files:**
- Create: `backend/scripts/seed_agent_tool_bindings.py`
- Modify: `backend/app/main.py`（lifespan）
- Test: `backend/app/tests/integration/test_seed_agent_tool_bindings.py`

**Interfaces:**
- Consumes: 现有 `AGENT_TOOLS` dict（`agent_tools.py:287`，key 是 agent_code，value 是 tool_name tuple）
- Produces:
  - `seed_agent_tool_bindings(session: AsyncSession) -> int` 返回 seed 的行数
  - 函数幂等：DB 已有 tool_name 非空行时跳过；只处理 `tool_name IS NULL` 的行
  - lifespan startup 顺序：现有初始化 → `await seed_agent_tool_bindings(session)` → 现有后续步骤
  - 失败 fail-fast（抛异常，不静默）

- [ ] **Step 1: Write failing integration test**

```python
# backend/app/tests/integration/test_seed_agent_tool_bindings.py
"""验证 seed_agent_tool_bindings 从 AGENT_TOOLS 字典 seed 3 行 + 幂等。"""
import pytest
from sqlalchemy import select, delete
from app.infrastructure.database import getSessionFactory
from app.domain.models import AgentDefinition
from scripts.seed_agent_tool_bindings import seed_agent_tool_bindings
from app.services.agent_tools import AGENT_TOOLS


@pytest.fixture(autouse=True)
async def cleanup():
    """清空 tool_name 字段以保证测试独立性。"""
    factory = getSessionFactory()
    async with factory() as session:
        await session.execute(
            AgentDefinition.__table__.update().values(tool_name=None)
        )
        await session.commit()
    yield


@pytest.mark.asyncio
async def test_seed_inserts_three_rows_from_AGENT_TOOLS():
    factory = getSessionFactory()
    async with factory() as session:
        inserted = await seed_agent_tool_bindings(session)
    assert inserted == len(AGENT_TOOLS)  # 3

    async with factory() as session:
        rows = await session.execute(
            select(
                AgentDefinition.agent_code,
                AgentDefinition.tool_name,
                AgentDefinition.tool_name_updated_at,
            ).where(AgentDefinition.tool_name.is_not(None))
        )
        result = {r.agent_code: r for r in rows}
    assert set(result.keys()) == set(AGENT_TOOLS.keys())
    for code, tools in AGENT_TOOLS.items():
        assert result[code].tool_name == tools[0]
        assert result[code].tool_name_updated_at is not None  # 自动戳


@pytest.mark.asyncio
async def test_seed_is_idempotent():
    factory = getSessionFactory()
    async with factory() as session:
        first = await seed_agent_tool_bindings(session)
        second = await seed_agent_tool_bindings(session)
    assert first == 3
    assert second == 0  # 第二次无新增
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_seed_agent_tool_bindings.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.seed_agent_tool_bindings'`.

- [ ] **Step 3: Write seed script**

```python
# backend/scripts/seed_agent_tool_bindings.py
"""Idempotent: 把 AGENT_TOOLS dict 中的 binding seed 进 agent_definition.tool_name。

启动时通过 lifespan 调用；DB 已有 binding 的行跳过（手工配置优先）。
仅在 AGENT_TOOLS dict 存在的过渡期使用——commit 6 删 dict 后，本脚本
改从 agent_tool_registry 派生。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AgentDefinition
from app.services.agent_tools import AGENT_TOOLS

logger = logging.getLogger(__name__)


async def seed_agent_tool_bindings(session: AsyncSession) -> int:
    """Seed 所有 tool_name IS NULL 的 Agent 行。

    Returns:
        新增的行数。
    """
    if not AGENT_TOOLS:
        logger.warning("AGENT_TOOLS 为空，跳过 seed")
        return 0

    now = datetime.now(timezone.utc)
    inserted = 0
    for agent_code, tools in AGENT_TOOLS.items():
        tool_name = tools[0]  # 1:1 基数
        result = await session.execute(
            update(AgentDefinition)
            .where(AgentDefinition.agent_code == agent_code)
            .where(AgentDefinition.tool_name.is_(None))
            .values(tool_name=tool_name, tool_name_updated_at=now)
            .returning(AgentDefinition.id)
        )
        if result.scalar_one_or_none() is not None:
            inserted += 1
            logger.info("seeded binding: %s → %s", agent_code, tool_name)
    await session.commit()
    return inserted
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_seed_agent_tool_bindings.py -v
```

Expected: PASS (2 tests).

- [ ] **Step 5: Integrate into lifespan**

```python
# backend/app/main.py  lifespan 函数内
from scripts.seed_agent_tool_bindings import seed_agent_tool_bindings

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... existing setup ...
    async with session_factory() as session:
        seeded = await seed_agent_tool_bindings(session)
        if seeded:
            logger.info("agent_tool_binding seed: %d new rows", seeded)
    # ... existing continue ...
```

定位 lifespan 现有初始化代码段（应在数据库 session 工厂初始化之后），把 seed 调用插入到「既有 startup 完成、既有 shutdown 开始」之间。

- [ ] **Step 6: Run full backend regression**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```

Expected: ≥ 80% coverage；既有测试全绿；seed 在既有测试 setup 中可能产生 +3 行，但所有测试断言都用 truncate 或 fixture 隔离，互不影响。

- [ ] **Step 7: e2e verify（启动一次空 DB → 看 seed 生效）**

```bash
# 跑一个临时 truncate 制造空 binding 场景
cd backend
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  uv run python -c "
import asyncio
from app.infrastructure.database import getSessionFactory
from app.domain.models import AgentDefinition
from sqlalchemy import update
async def clear():
    async with getSessionFactory()() as s:
        await s.execute(update(AgentDefinition).values(tool_name=None))
        await s.commit()
asyncio.run(clear())
"

# 启动后端看 seed 跑没跑
docker compose -f docker/docker-compose.yml up -d --build backend
sleep 8
docker compose -f docker/docker-compose.yml logs backend | grep -i "agent_tool_binding seed"

# 期望：日志含 "agent_tool_binding seed: 3 new rows" 或类似
```

- [ ] **Step 8: Commit**

```bash
git add backend/scripts/seed_agent_tool_bindings.py \
        backend/app/main.py \
        backend/app/tests/integration/test_seed_agent_tool_bindings.py
git commit -m "feat(agent): startup seed agent_tool_bindings

把硬编码 AGENT_TOOLS dict 中的 3 行 binding 幂等 seed 到
agent_definition.tool_name 列。lifespan startup 调用，失败 fail-fast；
DB 已有 binding 的行跳过（手工配置优先）。零行为变更；为 Task 5
runtime 切换铺路。"
```

---

## Task 3: AgentBindingCache 模块 + 写时失效（暂不接入 runtime）

**Files:**
- Create: `backend/app/services/agent_binding_cache.py`
- Test: `backend/app/tests/unit/test_agent_binding_cache.py`

**Interfaces:**
- Consumes: `AgentDefinition.agent_code`, `AgentDefinition.tool_name`, `AgentStatus.ACTIVE.value`
- Produces:
  - `class AgentBindingCache` 单例：
    - `async warmUp(session: AsyncSession) -> None` 全量加载 `status=ACTIVE` 行
    - `getToolName(agent_code: str) -> str | None`（未 warmUp 抛 RuntimeError）
    - `invalidate(agent_code: str | None = None) -> None`（None = 全清）
    - `async refreshOne(session, agent_code) -> None` 单条 reload
  - 模块级实例：`agent_binding_cache = AgentBindingCache()`

- [ ] **Step 1: Write failing unit tests**

```python
# backend/app/tests/unit/test_agent_binding_cache.py
"""AgentBindingCache 模块行为测试（不接真实 DB，用 fake session）。"""
import pytest
from app.services.agent_binding_cache import AgentBindingCache


class _FakeRow:
    def __init__(self, code, tool):
        self.agent_code = code
        self.tool_name = tool


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows
    def all(self):
        return self._rows
    def scalar_one_or_none(self):
        return self._rows[0].tool_name if self._rows else None


class _FakeSession:
    def __init__(self, by_code):
        self.by_code = by_code  # dict[code, tool_name]
    async def execute(self, stmt):
        # 简化：根据 stmt 的 where 子句返回对应行
        s = str(stmt)
        if "WHERE agent_definition.tool_name IS NOT NULL" in s or "tool_name_updated_at" in s:
            rows = [self.by_code[c] for c in self.by_code]
            return _FakeResult(rows)
        # single refreshOne path
        return _FakeResult([])


@pytest.fixture
def cache():
    return AgentBindingCache()


@pytest.mark.asyncio
async def test_getToolName_raises_before_warmUp(cache):
    with pytest.raises(RuntimeError, match="未 warmUp"):
        cache.getToolName("ANY")


@pytest.mark.asyncio
async def test_warmUp_then_getToolName(cache):
    by_code = {
        "SUPPLIER_360_AGENT": _FakeRow("SUPPLIER_360_AGENT", "supplier_360"),
        "SUPPLIER_RISK_AGENT": _FakeRow("SUPPLIER_RISK_AGENT", "supplier_risk"),
    }
    await cache.warmUp(_FakeSession(by_code))
    assert cache.getToolName("SUPPLIER_360_AGENT") == "supplier_360"
    assert cache.getToolName("SUPPLIER_RISK_AGENT") == "supplier_risk"
    assert cache.getToolName("UNKNOWN") is None


@pytest.mark.asyncio
async def test_invalidate_single(cache):
    by_code = {"A": _FakeRow("A", "toolA")}
    await cache.warmUp(_FakeSession(by_code))
    cache.invalidate("A")
    assert cache.getToolName("A") is None


@pytest.mark.asyncio
async def test_invalidate_all(cache):
    by_code = {"A": _FakeRow("A", "toolA"), "B": _FakeRow("B", "toolB")}
    await cache.warmUp(_FakeSession(by_code))
    cache.invalidate()  # 全清
    assert cache.getToolName("A") is None
    assert cache.getToolName("B") is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit/test_agent_binding_cache.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement AgentBindingCache**

```python
# backend/app/services/agent_binding_cache.py
"""Agent → Tool 绑定缓存：启动预热 + 写时失效。

单实例部署；未来多实例切换 Redis（独立 change）。
"""
from __future__ import annotations

import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AgentDefinition
from app.domain.enums import AgentStatus

logger = logging.getLogger(__name__)


class AgentBindingCache:
    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}
        self._loaded: bool = False

    async def warmUp(self, session: AsyncSession) -> None:
        rows = await session.execute(
            select(AgentDefinition.agent_code, AgentDefinition.tool_name)
            .where(AgentDefinition.tool_name.is_not(None))
            .where(AgentDefinition.status == AgentStatus.ACTIVE.value)
        )
        self._cache = {code: tool for code, tool in rows.all()}
        self._loaded = True
        logger.info("AgentBindingCache warmed up: %d active bindings", len(self._cache))

    def getToolName(self, agent_code: str) -> str | None:
        if not self._loaded:
            raise RuntimeError("AgentBindingCache 未 warmUp（lifespan bug）")
        return self._cache.get(agent_code)

    def invalidate(self, agent_code: str | None = None) -> None:
        if agent_code is None:
            self._cache.clear()
        else:
            self._cache.pop(agent_code, None)

    async def refreshOne(self, session: AsyncSession, agent_code: str) -> None:
        if not self._loaded:
            return  # 写前若未 warmUp，跳过（lifespan 会兜底）
        row = await session.execute(
            select(AgentDefinition.tool_name)
            .where(AgentDefinition.agent_code == agent_code)
        )
        self._cache[agent_code] = row.scalar_one_or_none()


agent_binding_cache = AgentBindingCache()  # 模块级单例
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit/test_agent_binding_cache.py -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Run full regression to ensure cache module imports cleanly**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```

Expected: ≥ 80% coverage；所有既有测试绿（cache 模块未被 runtime 使用，无行为变化）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/agent_binding_cache.py \
        backend/app/tests/unit/test_agent_binding_cache.py
git commit -m "feat(agent): AgentBindingCache module + write-time invalidation

启动预热 + 写时失效的单例缓存。Task 5 接入 runtime 前置，本期不动
run 路径。模块级单例 + 写时 refreshOne/invalidate 单条或全清。
lifespan 未 warmUp 时 getToolName 抛 RuntimeError（fail-fast）。"
```

---

## Task 4: 写时跨字段校验（field_validator）

**Files:**
- Modify: `backend/app/domain/error_messages.py`（追加 3 条 MSG）
- Modify: `backend/app/domain/schemas.py`（`AgentDefinitionCreate` + `AgentDefinitionUpdate` 加 `_validateToolName`）
- Test: `backend/app/tests/integration/test_agent_tool_binding_validation.py`

**Interfaces:**
- Consumes: `agent_tool_registry.get(name)`, `agent_tool_registry.all()`（Task 5 会建，本 task 暂时假设 `all()` 存在；如未到 Task 5 阶段，先 stub 一个空实现，本 task 跑通后 Task 5 补完整）
- Produces:
  - `MSG_AGENT_TOOL_UNKNOWN`, `MSG_AGENT_TOOL_LAYER_MISMATCH`, `MSG_AGENT_TOOL_UNREGISTERED`
  - `_validateToolName(v, info) -> str | None` field_validator：
    - `v is None` → 通过
    - `tool is None` → raise ValueError(MSG_AGENT_TOOL_UNKNOWN)
    - `tool.data_layers` ⊄ `info.data["data_layers"]` → raise ValueError(MSG_AGENT_TOOL_LAYER_MISMATCH)
    - 否则通过

- [ ] **Step 1: Write failing integration test**

```python
# backend/app/tests/integration/test_agent_tool_binding_validation.py
"""验证 POST/PUT /agents 的 tool_name 写入校验。"""
import pytest
from httpx import AsyncClient
from app.main import app
from app.infrastructure.database import getSessionFactory
from app.domain.models import AgentDefinition
from sqlalchemy import delete


@pytest.fixture(autouse=True)
async def clean_test_agents():
    """每个测试前清空测试 agent_code。"""
    factory = getSessionFactory()
    async with factory() as session:
        await session.execute(
            delete(AgentDefinition).where(
                AgentDefinition.agent_code.like("BIND_TEST_%")
            )
        )
        await session.commit()
    yield
    async with factory() as session:
        await session.execute(
            delete(AgentDefinition).where(
                AgentDefinition.agent_code.like("BIND_TEST_%")
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_create_with_valid_tool_and_full_layers_succeeds():
    async with AsyncClient(app=app, base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/agents",
            headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
            json={
                "agentCode": "BIND_TEST_OK",
                "agentName": "bind ok",
                "dataDomains": ["PROCUREMENT"],
                "dataLayers": ["DIM", "FEATURE"],   # 完全覆盖 supplier_360 的 (DIM, FEATURE)
                "toolName": "supplier_360",
            },
        )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_create_with_unknown_tool_rejected_422():
    async with AsyncClient(app=app, base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/agents",
            headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
            json={
                "agentCode": "BIND_TEST_BAD_TOOL",
                "agentName": "bad tool",
                "dataDomains": ["PROCUREMENT"],
                "dataLayers": ["DIM"],
                "toolName": "fake_tool_not_registered",
            },
        )
    assert r.status_code == 422
    assert "fake_tool_not_registered" in r.text


@pytest.mark.asyncio
async def test_create_with_tool_but_missing_layer_rejected_422():
    async with AsyncClient(app=app, base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/agents",
            headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
            json={
                "agentCode": "BIND_TEST_MISS_LAYER",
                "agentName": "miss layer",
                "dataDomains": ["PROCUREMENT"],
                "dataLayers": ["DIM"],   # 缺 FEATURE
                "toolName": "supplier_360",
            },
        )
    assert r.status_code == 422
    assert "FEATURE" in r.text


@pytest.mark.asyncio
async def test_create_without_tool_succeeds():
    """toolName 字段未传 = 未绑定 = 元数据 Agent（合法）。"""
    async with AsyncClient(app=app, base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/agents",
            headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
            json={
                "agentCode": "BIND_TEST_NO_TOOL",
                "agentName": "no tool",
                "dataDomains": ["PROCUREMENT"],
                "dataLayers": ["DIM"],
            },
        )
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_update_tool_to_null_clears_binding():
    """PUT toolName: null 应清空 binding（spec §5.4 行为）。"""
    # 1. 先创建一个有 binding 的 Agent
    async with AsyncClient(app=app, base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/agents",
            headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
            json={
                "agentCode": "BIND_TEST_CLEAR",
                "agentName": "clear",
                "dataDomains": ["PROCUREMENT"],
                "dataLayers": ["DIM", "FEATURE"],
                "toolName": "supplier_360",
            },
        )
        assert r.status_code == 201
        agent_id = r.json()["id"]

        # 2. 改 toolName 为 null
        r2 = await ac.put(
            f"/api/v1/agents/{agent_id}",
            headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
            json={
                "agentCode": "BIND_TEST_CLEAR",
                "agentName": "clear",
                "dataDomains": ["PROCUREMENT"],
                "dataLayers": ["DIM", "FEATURE"],
                "toolName": None,
            },
        )
    assert r2.status_code == 200
    assert r2.json()["toolName"] is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_tool_binding_validation.py -v
```

Expected: FAIL with `toolName` 字段未在 DTO 中或校验未生效。

- [ ] **Step 3: Append error messages**

在 `backend/app/domain/error_messages.py` 文件末尾追加（沿用既有常量命名风格）：

```python
# agent tool binding 校验
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

- [ ] **Step 4: Stub `agent_tool_registry.all()` (临时)**

Task 5 才正式实现 `all()`。本 task 先 stub 一个最小可用版本，避免循环依赖：

```python
# backend/app/services/agent_tools.py  在 AgentToolRegistry 类内追加
def all(self) -> list["AgentTool"]:
    """所有已注册工具，按 name 排序。"""
    return [self._tools[k] for k in sorted(self._tools.keys())]
```

如果 Task 5 已完成则跳过此步；否则现在补上。

- [ ] **Step 5: Add `_validateToolName` field_validator**

```python
# backend/app/domain/schemas.py  AgentDefinitionCreate 类内
from app.domain.error_messages import (
    MSG_AGENT_TOOL_UNKNOWN,
    MSG_AGENT_TOOL_LAYER_MISMATCH,
)
from app.services.agent_tools import agent_tool_registry

@field_validator("tool_name")
@classmethod
def _validateToolName(cls, v: str | None, info) -> str | None:
    if v is None:
        return v
    tool = agent_tool_registry.get(v)
    if tool is None:
        registered = ",".join(t.name for t in agent_tool_registry.all())
        raise ValueError(MSG_AGENT_TOOL_UNKNOWN.format(
            name=v, registered=registered,
        ))
    data_layers = info.data.get("data_layers") or []
    missing = [layer for layer in tool.data_layers if layer not in data_layers]
    if missing:
        raise ValueError(MSG_AGENT_TOOL_LAYER_MISMATCH.format(
            tool=v, missing=",".join(missing),
        ))
    return v
```

**注意**：`AgentDefinitionUpdate` 同样添加该 validator（None 跳过；非 None 走完整流程）。两者可以共享 helper：

```python
# 在 schemas.py 顶部（导入后）：
def _validateToolNameShared(v: str | None, info) -> str | None:
    """Create/Update 共用逻辑。"""
    if v is None:
        return v
    tool = agent_tool_registry.get(v)
    if tool is None:
        registered = ",".join(t.name for t in agent_tool_registry.all())
        raise ValueError(MSG_AGENT_TOOL_UNKNOWN.format(
            name=v, registered=registered,
        ))
    data_layers = info.data.get("data_layers") or []
    missing = [layer for layer in tool.data_layers if layer not in data_layers]
    if missing:
        raise ValueError(MSG_AGENT_TOOL_LAYER_MISMATCH.format(
            tool=v, missing=",".join(missing),
        ))
    return v
```

然后 Create 和 Update 类各加：
```python
@field_validator("tool_name")
@classmethod
def _validateToolName(cls, v, info):
    return _validateToolNameShared(v, info)
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_tool_binding_validation.py -v
```

Expected: PASS (5 tests).

- [ ] **Step 7: Run full regression**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```

Expected: ≥ 80% coverage；既有测试全绿。

- [ ] **Step 8: Commit**

```bash
git add backend/app/domain/error_messages.py \
        backend/app/domain/schemas.py \
        backend/app/services/agent_tools.py \
        backend/app/tests/integration/test_agent_tool_binding_validation.py
git commit -m "feat(agent): write-time tool_name validation (Pydantic field_validator)

跨字段校验：tool_name 必须在 agent_tool_registry 中，且 agent.data_layers
必须完全覆盖 tool.data_layers。非法值 Pydantic 422 拒绝。
AgentDefinitionCreate/Update 共用 _validateToolNameShared helper。
三条 MSG_AGENT_TOOL_* 错误消息同步追加。"
```

---

## Task 5: `/agents/options` 扩展 + `agent_tool_registry.all()` 完整实现

**Files:**
- Modify: `backend/app/domain/schemas.py`（`AgentOptionsRead` 加 `tools: list[AgentToolOption]` + 新增 `AgentToolOption` CamelModel）
- Modify: `backend/app/api/v1/agents.py`（`/options` 端点）
- Test: `backend/app/tests/integration/test_agent_options_api.py`（已有文件，扩展断言）

**Interfaces:**
- Consumes: 现有 `/agents/options` 端点、`agent_tool_registry.all()`（Task 4 已 stub，本 task 确认实现完整）
- Produces:
  - `AgentToolOption(name, description, data_object, data_layers)` CamelModel
  - `AgentOptionsRead.tools: list[AgentToolOption]`
  - 端点响应：`{domains, layers, tools: [{name, description, dataObject, dataLayers}]}`
  - 既有测试（domain/layers 字段）保持绿

- [ ] **Step 1: 确认 `agent_tool_registry.all()` 已完整**

读 `backend/app/services/agent_tools.py`，确认 Task 4 stub 已实现 `all()` 方法。如果还没，参考 Task 4 Step 4 实现。

- [ ] **Step 2: 扩展既有集成测试**

```python
# backend/app/tests/integration/test_agent_options_api.py  追加：
@pytest.mark.asyncio
async def test_options_includes_tools_with_all_fields():
    async with AsyncClient(app=app, base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/agents/options",
            headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
        )
    assert r.status_code == 200
    body = r.json()
    assert "tools" in body
    assert isinstance(body["tools"], list)
    assert len(body["tools"]) >= 3  # supplier_360 / supplier_risk / graph_traverse
    by_name = {t["name"]: t for t in body["tools"]}
    assert "supplier_360" in by_name
    assert by_name["supplier_360"]["dataObject"] == "SUPPLIER"
    assert set(by_name["supplier_360"]["dataLayers"]) == {"DIM", "FEATURE"}
    assert by_name["supplier_360"]["description"]  # 非空


@pytest.mark.asyncio
async def test_options_tools_sorted_by_name():
    async with AsyncClient(app=app, base_url="http://t") as ac:
        r = await ac.get(
            "/api/v1/agents/options",
            headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
        )
    names = [t["name"] for t in r.json()["tools"]]
    assert names == sorted(names)
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_options_api.py -v
```

Expected: 既有 2 测试 PASS；新 2 测试 FAIL（`tools` 字段缺失）。

- [ ] **Step 4: Add `AgentToolOption` + extend `AgentOptionsRead`**

```python
# backend/app/domain/schemas.py  追加（保持 CamelModel alias）：
class AgentToolOption(CamelModel):
    name: str
    description: str
    data_object: str
    data_layers: list[str]


class AgentOptionsRead(CamelModel):
    domains: list[str]
    layers: list[str]
    tools: list[AgentToolOption] = Field(default_factory=list)
```

- [ ] **Step 5: Update `/options` endpoint**

```python
# backend/app/api/v1/agents.py  getAgentOptions 端点
from app.domain.schemas import AgentOptionsRead, AgentToolOption
from app.services.agent_tools import agent_tool_registry


@router.get("/options", response_model=AgentOptionsRead)
async def getAgentOptions(_user: CurrentUser = Depends(getCurrentUser)):
    return AgentOptionsRead(
        domains=list(AGENT_DATA_DOMAINS),
        layers=list(AGENT_DATA_LAYERS),
        tools=[
            AgentToolOption(
                name=t.name,
                description=t.description,
                data_object=t.data_object,
                data_layers=list(t.data_layers),
            )
            for t in agent_tool_registry.all()
        ],
    )
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_options_api.py -v
```

Expected: PASS (4 tests = 2 既有 + 2 新增).

- [ ] **Step 7: Run full regression**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```

Expected: ≥ 80% coverage；全绿。

- [ ] **Step 8: Commit**

```bash
git add backend/app/domain/schemas.py \
        backend/app/api/v1/agents.py \
        backend/app/tests/integration/test_agent_options_api.py
git commit -m "feat(agent): extend /agents/options with tools list

返回 {domains, layers, tools:[{name,description,dataObject,dataLayers}]}。
前端 useAgentOptions() 复用现有 hook，零额外请求。
registry.all() 按 name 排序（稳定输出）。"
```

---

## Task 6: Runtime 集成（AgentBindingCache 接入 + dict 保留 fallback）

**Files:**
- Modify: `backend/app/main.py`（lifespan 加 `warmUp`）
- Modify: `backend/app/services/agent_registry_service.py`（`createAgent/updateAgent/deleteAgent` 加 cache invalidate/refreshOne）
- Modify: `backend/app/services/agent_runtime_service.py`（`_resolveTool` 改造 + `runnable` 服务端化）
- Test: `backend/app/tests/integration/test_agent_tool_binding_runtime.py`

**Interfaces:**
- Consumes: 现有 `AGENT_TOOLS` dict（保留作为 fallback）+ `agent_binding_cache`
- Produces:
  - lifespan：`seed_agent_tool_bindings` → `agent_binding_cache.warmUp`
  - `run()` 内 `_resolveTool`：先查 `agent_binding_cache`，命中且 tool 已注册 → 用 DB binding；未命中 → 回退 `AGENT_TOOLS` dict；两者都无 → 409
  - `agentToRead.runnable` 改用 `agent_binding_cache.getToolName(...)` 判断
  - `createAgent/updateAgent`：commit 后 `await agent_binding_cache.refreshOne(session, agent_code)`
  - `deleteAgent`：commit 后 `agent_binding_cache.invalidate(agent_code)`

- [ ] **Step 1: Write failing integration test**

```python
# backend/app/tests/integration/test_agent_tool_binding_runtime.py
"""验证 runtime 路径：cache 命中 / dict fallback / 漂移防御。"""
import pytest
from httpx import AsyncClient
from app.main import app
from app.infrastructure.database import getSessionFactory
from app.domain.models import AgentDefinition
from app.services.agent_binding_cache import agent_binding_cache
from sqlalchemy import delete, update


@pytest.fixture(autouse=True)
async def reset_cache_and_seed_state():
    """每个测试前重置 cache + DB binding。"""
    agent_binding_cache.invalidate()  # 全清
    factory = getSessionFactory()
    async with factory() as session:
        # 把 SUPPLIER_360_AGENT 的 tool_name 设回 supplier_360
        await session.execute(
            update(AgentDefinition)
            .where(AgentDefinition.agent_code == "SUPPLIER_360_AGENT")
            .values(tool_name="supplier_360")
        )
        await session.commit()
    yield
    agent_binding_cache.invalidate()


@pytest.mark.asyncio
async def test_run_path_uses_db_binding_via_cache():
    """正常路径：DB 有 binding → cache 命中 → 200。"""
    async with getSessionFactory()() as session:
        await agent_binding_cache.warmUp(session)
    async with AsyncClient(app=app, base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/agents/SUPPLIER_360_AGENT/run",
            headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
            json={"input": "10105"},
        )
    assert r.status_code in (200, 422)  # 422 也 OK（input 解析失败但走完了 run 路径）
    # 关键：不是 404 / 409 → binding 路径打通


@pytest.mark.asyncio
async def test_run_409_when_db_binding_missing_dict_fallback_works():
    """DB binding 清空 → dict fallback 兜底 → 200（过渡期行为）。"""
    factory = getSessionFactory()
    async with factory() as session:
        await session.execute(
            update(AgentDefinition)
            .where(AgentDefinition.agent_code == "SUPPLIER_360_AGENT")
            .values(tool_name=None)
        )
        await session.commit()
    async with factory() as session:
        await agent_binding_cache.warmUp(session)  # 重载反映新 DB 状态
    async with AsyncClient(app=app, base_url="http://t") as ac:
        r = await ac.post(
            "/api/v1/agents/SUPPLIER_360_AGENT/run",
            headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
            json={"input": "10105"},
        )
    assert r.status_code in (200, 422)  # dict fallback 兜底，不是 409
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_tool_binding_runtime.py -v
```

Expected: FAIL（runtime 还没接 cache）。

- [ ] **Step 3: Wire lifespan with warmUp**

```python
# backend/app/main.py  lifespan startup
from app.services.agent_binding_cache import agent_binding_cache

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... existing setup ...
    async with session_factory() as session:
        await seed_agent_tool_bindings(session)
        await agent_binding_cache.warmUp(session)
    # ... existing continue ...
```

- [ ] **Step 4: Add cache invalidation in registry service**

```python
# backend/app/services/agent_registry_service.py  createAgent / updateAgent / deleteAgent
from app.services.agent_binding_cache import agent_binding_cache

async def createAgent(self, session, payload):
    # ... existing logic ...
    await session.commit()
    await agent_binding_cache.refreshOne(session, entity.agent_code)
    return entity

async def updateAgent(self, session, agent_id, payload):
    # ... existing logic ...
    await session.commit()
    await agent_binding_cache.refreshOne(session, entity.agent_code)
    return entity

async def deleteAgent(self, session, agent_id):
    # ... existing logic（先取 agent_code 再删）...
    code = entity.agent_code
    await session.delete(entity)
    await session.commit()
    agent_binding_cache.invalidate(code)
```

- [ ] **Step 5: Modify `run()` to use cache with dict fallback**

```python
# backend/app/services/agent_runtime_service.py
from app.services.agent_tools import AGENT_TOOLS, agent_tool_registry
from app.domain.error_messages import MSG_AGENT_TOOL_UNREGISTERED
from app.services.agent_binding_cache import agent_binding_cache

async def run(self, session, agent_code, params, *, actor):
    entity = await self._agents.getAgent(session, agent_code)
    if entity.status != AgentStatus.ACTIVE.value:
        raise ConflictError(MSG_AGENT_NOT_RUNNABLE.format(code=agent_code))

    # 【CHANGED】DB cache 优先，dict fallback
    tool_name = agent_binding_cache.getToolName(agent_code)
    if tool_name is None:
        # 过渡期 fallback：dict 还在
        fallback = AGENT_TOOLS.get(agent_code)
        if fallback:
            tool_name = fallback[0]
    if tool_name is None:
        raise ConflictError(MSG_AGENT_NOT_RUNNABLE_NO_TOOL.format(code=agent_code))

    tool = agent_tool_registry.get(tool_name)
    if tool is None:
        # 防御：DB 写入了未在代码注册的 tool（漂移）
        raise ConflictError(MSG_AGENT_TOOL_UNREGISTERED.format(
            code=agent_code, tool=tool_name,
        ))
    # ... rest unchanged ...
```

- [ ] **Step 6: Update `agentToRead.runnable` to use cache**

```python
# backend/app/services/agent_registry_service.py  agentToRead
from app.services.agent_binding_cache import agent_binding_cache

def agentToRead(entity):
    read = CamelModel.from_attributes(entity)  # 既有逻辑
    # ...
    read.runnable = (
        read.status == AgentStatus.ACTIVE.value
        and agent_binding_cache.getToolName(read.agent_code) is not None
    )
    return read
```

注意：`agent_binding_cache.getToolName` 在未 warmUp 时会抛 RuntimeError。需在测试 setup 中先 warmUp，或在 `agentToRead` 内 try/except 兜底：

```python
try:
    tool = agent_binding_cache.getToolName(read.agent_code)
except RuntimeError:
    tool = None  # 未 warmUp 视为不可运行
read.runnable = (
    read.status == AgentStatus.ACTIVE.value and tool is not None
)
```

- [ ] **Step 7: Run test to verify it passes**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_tool_binding_runtime.py -v
```

Expected: PASS (2 tests).

- [ ] **Step 8: Run full regression**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```

Expected: ≥ 80% coverage；全绿。

- [ ] **Step 9: e2e verify**

```bash
docker compose -f docker/docker-compose.yml up -d --build backend
sleep 8
curl -s http://localhost:8000/api/v1/agents/options \
  -H "X-User-Id: admin" -H "X-User-Roles: admin" | python3 -m json.tool
# 期望：含 "tools" 字段，3 个工具

curl -s -X POST http://localhost:8000/api/v1/agents/SUPPLIER_360_AGENT/run \
  -H "X-User-Id: admin" -H "X-User-Roles": admin" \
  -H "Content-Type: application/json" \
  -d '{"input":"10105"}' -w "\n%{http_code}\n"
# 期望：200 或 422（input 路径），不是 404 / 409
```

- [ ] **Step 10: Commit**

```bash
git add backend/app/main.py \
        backend/app/services/agent_registry_service.py \
        backend/app/services/agent_runtime_service.py \
        backend/app/tests/integration/test_agent_tool_binding_runtime.py
git commit -m "feat(agent): AgentBindingCache wired into runtime (AGENT_TOOLS dict retained as fallback)

lifespan: seed_agent_tool_bindings → warmUp；registry 写点加 refreshOne /
invalidate；runtime._resolveTool 改 DB cache 优先 + dict fallback；
agentToRead.runnable 改用 cache 判定。AGENT_TOOLS dict 仍保留为过渡期
fallback；Task 7 删除。"
```

---

## Task 7: 删除 `AGENT_TOOLS` dict + seed 改从 registry 派生

**Files:**
- Modify: `backend/app/services/agent_tools.py`（删 `AGENT_TOOLS` dict 行 287 附近）
- Modify: `backend/scripts/seed_agent_tool_bindings.py`（不再依赖 AGENT_TOOLS）
- Modify: `backend/app/services/agent_runtime_service.py`（删 fallback 分支）
- Modify: `backend/app/services/agent_registry_service.py`（agentToRead 等不再依赖 dict）
- Test: 所有既有 + Task 6 测试保持绿

**Interfaces:**
- Consumes: `agent_tool_registry` 已实现 `all()` 方法
- Produces:
  - `AGENT_TOOLS` dict 完全删除（无 import 残留）
  - seed 脚本遍历 `agent_tool_registry.all()` 但需「Agent 默认绑定」规则——这意味着需要新建 `AGENT_DEFAULT_BINDINGS` 常量或类似
  - runtime 路径只剩 cache + registry，无 dict

- [ ] **Step 1: Audit `AGENT_TOOLS` 所有 import 点**

```bash
cd backend
grep -rn "AGENT_TOOLS" app/ scripts/ --include="*.py"
```

预期：当前 usage 主要在 `agent_tools.py:287` 定义 + `agent_runtime_service.py:97` + `agent_registry_service.py:65-68` + `scripts/seed_agent_tool_bindings.py`。逐一清理。

- [ ] **Step 2: Add `AGENT_DEFAULT_BINDINGS` to seed-time**

为了 seed 脚本不再依赖 dict，但启动时仍能 seed 3 行——在 `agent_tools.py` 中新增：

```python
# backend/app/services/agent_tools.py  替代被删除的 AGENT_TOOLS
AGENT_DEFAULT_BINDINGS: dict[str, str] = {
    "SUPPLIER_360_AGENT": "supplier_360",
    "SUPPLIER_RISK_AGENT": "supplier_risk",
    "GRAPH_REASONING_AGENT": "graph_traverse",
}
```

这是「默认绑定」声明（与 dict 等价语义，但只是常量）。

- [ ] **Step 3: Update seed script**

```python
# backend/scripts/seed_agent_tool_bindings.py
from app.services.agent_tools import AGENT_DEFAULT_BINDINGS

async def seed_agent_tool_bindings(session: AsyncSession) -> int:
    if not AGENT_DEFAULT_BINDINGS:
        logger.warning("AGENT_DEFAULT_BINDINGS 为空，跳过 seed")
        return 0
    # ... rest of logic uses AGENT_DEFAULT_BINDINGS instead of AGENT_TOOLS ...
```

- [ ] **Step 4: Remove fallback from runtime**

```python
# backend/app/services/agent_runtime_service.py  _resolveTool 简化：
async def run(self, session, agent_code, params, *, actor):
    entity = await self._agents.getAgent(session, agent_code)
    if entity.status != AgentStatus.ACTIVE.value:
        raise ConflictError(MSG_AGENT_NOT_RUNNABLE.format(code=agent_code))

    tool_name = agent_binding_cache.getToolName(agent_code)
    if tool_name is None:
        raise ConflictError(MSG_AGENT_NOT_RUNNABLE_NO_TOOL.format(code=agent_code))

    tool = agent_tool_registry.get(tool_name)
    if tool is None:
        raise ConflictError(MSG_AGENT_TOOL_UNREGISTERED.format(
            code=agent_code, tool=tool_name,
        ))
    # ... rest unchanged ...
```

删除 `AGENT_TOOLS` 的 import。

- [ ] **Step 5: Update `agentToRead.runnable`**

```python
# backend/app/services/agent_registry_service.py  删除 AGENT_TOOLS import
# agentToRead.runnable 已经在 Task 6 改为 cache 判断，无需改动
```

- [ ] **Step 6: Run full regression + coverage gate**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```

Expected: ≥ 80% coverage；全绿。

- [ ] **Step 7: 确认 AGENT_TOOLS 无残留**

```bash
cd backend
grep -rn "AGENT_TOOLS" app/ scripts/ --include="*.py"
# 期望：空（dict 完全删除）
```

- [ ] **Step 8: e2e verify 完整体验**

```bash
docker compose -f docker/docker-compose.yml up -d --build backend
sleep 8

# 1. 清空 tool_name 模拟「DB 无 binding」
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  uv run python -c "
import asyncio
from app.infrastructure.database import getSessionFactory
from app.domain.models import AgentDefinition
from sqlalchemy import update
async def clear():
    async with getSessionFactory()() as s:
        await s.execute(update(AgentDefinition).values(tool_name=None))
        await s.commit()
asyncio.run(clear())
"
docker compose -f docker/docker-compose.yml restart backend
sleep 8

# 2. 启动 seed 应自动恢复 3 行
curl -s -X POST http://localhost:8000/api/v1/agents/SUPPLIER_360_AGENT/run \
  -H "X-User-Id: admin" -H "X-User-Roles: admin" \
  -H "Content-Type: application/json" \
  -d '{"input":"10105"}' -w "\n%{http_code}\n"
# 期望：200 或 422（run 路径通了）

# 3. 改 binding 为非法（清空 + 绑未知 tool）
DATABASE_URL=... uv run python -c "
import asyncio
from app.infrastructure.database import getSessionFactory
from app.domain.models import AgentDefinition
from sqlalchemy import update
async def break_binding():
    async with getSessionFactory()() as s:
        await s.execute(
            update(AgentDefinition)
            .where(AgentDefinition.agent_code == 'SUPPLIER_360_AGENT')
            .values(tool_name='fake_tool_not_in_registry')
        )
        await s.commit()
asyncio.run(break_binding())
"
docker compose -f docker/docker-compose.yml restart backend
sleep 8

curl -s -X POST http://localhost:8000/api/v1/agents/SUPPLIER_360_AGENT/run \
  -H "X-User-Id: admin" -H "X-User-Roles: admin" \
  -H "Content-Type: application/json" \
  -d '{"input":"10105"}' -w "\n%{http_code}\n"
# 期望：409 + MSG_AGENT_TOOL_UNREGISTERED
```

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/agent_tools.py \
        backend/app/services/agent_runtime_service.py \
        backend/app/services/agent_registry_service.py \
        backend/scripts/seed_agent_tool_bindings.py
git commit -m "refactor(agent): delete AGENT_TOOLS dict; default bindings from registry

AGENT_TOOLS dict 完全删除；AGENT_DEFAULT_BINDINGS 作为 seed 数据源
（语义等价但显式标记为「默认绑定常量」）；runtime 路径只剩 cache +
registry，无 fallback。漂移防御：DB 绑定未注册工具 → 409
MSG_AGENT_TOOL_UNREGISTERED。"
```

---

## Task 8: 前端 — Agent Registry ToolName Select + i18n + Drawer 展示

**Files:**
- Modify: `frontend/src/types/agentOptions.ts`（加 `AgentToolOption`）
- Modify: `frontend/src/pages/AgentRegistryPage.tsx`（Create/Edit Modal 加 toolName Select + 校验 + Detail Drawer 加展示）
- Modify: `frontend/src/i18n/zh-CN.ts` + `en-US.ts`（加 4 keys）
- Test: `frontend/src/tests/AgentRegistryPage.test.tsx`（已有文件，扩展）

**Interfaces:**
- Consumes: 既有 `useAgentOptions()` hook（已返回 options.tools 字段，Task 5）
- Produces:
  - `AgentOptions.tools: AgentToolOption[]`
  - Create/Edit Modal 加 `toolName` 字段，含实时 layer 覆盖校验
  - Detail Drawer 加 `toolName` 展示（只读）
  - i18n 4 keys × 2 语言 = 8 条

- [ ] **Step 1: Write failing frontend test**

```typescript
// frontend/src/tests/AgentRegistryPage.test.tsx  追加 describe
describe("AgentRegistryPage - toolName binding", () => {
  it("displays toolName in detail drawer when present", async () => {
    mockAgentOptions.tools = [
      { name: "supplier_360", description: "x", dataObject: "SUPPLIER", dataLayers: ["DIM", "FEATURE"] },
    ];
    // ... 渲染 Drawer，断言 ToolName Tag 出现
  });

  it("shows validation error when tool layers not covered by agent dataLayers", async () => {
    mockAgentOptions.tools = [
      { name: "supplier_360", description: "x", dataObject: "SUPPLIER", dataLayers: ["DIM", "FEATURE"] },
    ];
    // ... 打开 Create Modal，选 toolName=supplier_360，但 dataLayers 只选 DIM
    // 断言：红色错误提示 + "FEATURE" 字样
  });

  it("submits successfully when toolName and dataLayers are compatible", async () => {
    // ... 选 supplier_360 + dataLayers=[DIM, FEATURE] → 提交成功
  });
});
```

完整 mock setup 参考既有 `AgentRegistryPage.test.tsx`（已用 `vi.mock` mock `useAgentOptions`）。

- [ ] **Step 2: Run test to verify it fails**

```bash
cd frontend
npx vitest run src/tests/AgentRegistryPage.test.tsx
```

Expected: FAIL（`options.tools` 未在 mock 中或表单无 toolName 字段）。

- [ ] **Step 3: Extend `AgentOptions` type**

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

- [ ] **Step 4: Modify AgentRegistryPage**

```tsx
// frontend/src/pages/AgentRegistryPage.tsx  Create/Edit Modal
// 在 dataLayers Form.Item 之后追加：

<Form.Item
  name="toolName"
  label={t("agentRegistry.fields.toolName")}
  rules={[
    {
      validator: (_: unknown, value: string | undefined) => {
        if (!value) return Promise.resolve();
        const tool = options.tools.find((x) => x.name === value);
        if (!tool) {
          return Promise.reject(
            new Error(t("agentRegistry.errors.toolUnknown"))
          );
        }
        const covered = form.getFieldValue("dataLayers") || [];
        const missing = tool.dataLayers.filter(
          (layer) => !covered.includes(layer)
        );
        if (missing.length > 0) {
          return Promise.reject(
            new Error(
              t("agentRegistry.errors.toolLayerMismatch", {
                tool: value,
                missing: missing.join(","),
              })
            )
          );
        }
        return Promise.resolve();
      },
    },
  ]}
>
  <Select
    allowClear
    placeholder={t("agentRegistry.fields.toolNamePlaceholder")}
    options={options.tools.map((tool) => ({
      value: tool.name,
      label: `${tool.name} — ${tool.description}`,
    }))}
    showSearch
    optionFilterProp="label"
    onChange={() => form.validateFields(["dataLayers"])}  // 双向联动
  />
</Form.Item>
```

注意：`onChange` 反向触发 `dataLayers` 校验，否则只在 toolName 字段触发，user 体验割裂。

- [ ] **Step 5: Detail Drawer 加展示**

```tsx
// frontend/src/pages/AgentRegistryPage.tsx  Detail Drawer Descriptions
{selectedAgent?.toolName && (
  <Descriptions.Item
    label={t("agentRegistry.fields.toolName")}
  >
    <Tag color="blue">{selectedAgent.toolName}</Tag>
  </Descriptions.Item>
)}
```

- [ ] **Step 6: i18n 新增**

`frontend/src/i18n/zh-CN.ts`:
```typescript
agentRegistry: {
  // ... existing ...
  fields: {
    // ... existing ...
    toolName: "绑定工具",
    toolNamePlaceholder: "选择工具（可空，留空为元数据 Agent）",
  },
  errors: {
    // ... existing ...
    toolUnknown: "未知工具，请刷新页面重试",
    toolLayerMismatch: "工具 {tool} 要求 data_layers 含 {missing}，请先在数据层字段补齐",
  },
},
```

`frontend/src/i18n/en-US.ts`:
```typescript
agentRegistry: {
  // ... existing ...
  fields: {
    // ... existing ...
    toolName: "Bound Tool",
    toolNamePlaceholder: "Select tool (optional; empty = metadata-only Agent)",
  },
  errors: {
    // ... existing ...
    toolUnknown: "Unknown tool, please refresh the page and retry",
    toolLayerMismatch: "Tool {tool} requires data_layers to include {missing}; please add them first",
  },
},
```

- [ ] **Step 7: Run frontend tests**

```bash
cd frontend
npx vitest run
```

Expected: 全绿（既有 + 3 新增）。

- [ ] **Step 8: Type-check**

```bash
cd frontend
npx tsc --noEmit
```

Expected: 0 errors.

- [ ] **Step 9: Build + Docker rebuild**

```bash
cd frontend
npm run build  # 验证本地构建
# 然后 Docker 重建（必须 --no-cache 才能更新 bundle hash）
docker compose -f docker/docker-compose.yml build --no-cache frontend
docker compose -f docker/docker-compose.yml up -d frontend
sleep 5

# 验证新 bundle 含 toolName 字段字符串
curl -s http://localhost:5173/ | grep -o 'assets/index-[^"]*\.js' | head -1
# 然后：
ASSET=$(curl -s http://localhost:5173/ | grep -o 'assets/index-[^"]*\.js' | head -1)
curl -s "http://localhost:5173/$ASSET" | grep -c "toolName\|绑定工具\|Bound Tool"
# 期望：≥ 2 命中
```

- [ ] **Step 10: Commit**

```bash
git add frontend/src/types/agentOptions.ts \
        frontend/src/pages/AgentRegistryPage.tsx \
        frontend/src/i18n/zh-CN.ts \
        frontend/src/i18n/en-US.ts \
        frontend/src/tests/AgentRegistryPage.test.tsx
git commit -m "feat(frontend): toolName Select in Agent Registry

Create/Edit Modal 加 toolName 字段（候选来自 useAgentOptions().tools）；
实时校验：agent.dataLayers 必须覆盖 tool.dataLayers；Detail Drawer
加 toolName Tag 展示（只读）。i18n zh-CN + en-US 各 4 keys。
前后端字段契约：tool_name ↔ toolName (camelCase)。"
```

---

## Task 9: Harness docs + memory

**Files:**
- Create: `Harness/changes/feat-agent-tool-binding/summary.md`
- Modify: `Harness/wiki/business-domain.md`（§「Agent Runtime」章节补充「工具绑定可配置化」小节）
- Create: `~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-agent-tool-binding.md`
- Modify: `~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/MEMORY.md`

**Interfaces:**
- Consumes: 已 commit 的 8 个 commits
- Produces: SSOT 文档（spec / plan / summary）+ wiki 标注 + 跨会话 memory

- [ ] **Step 1: Create Harness change summary**

```markdown
# Harness/changes/feat-agent-tool-binding/summary.md

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
[本 plan 8 个 commit + harness docs]
```
```

（最终 commit hash 列表在 commit 后回填）

- [ ] **Step 2: Update `Harness/wiki/business-domain.md`**

在 §「Agent Runtime（Phase 6.4）」章节追加「工具绑定可配置化（Phase 7 feat-agent-tool-binding）」小节：

```markdown
### 工具绑定可配置化（Phase 7 feat-agent-tool-binding）

2026-09-02 完成（[[Harness/changes/feat-agent-tool-binding/summary.md]]）。

- `agent_definition.tool_name` 列（Alembic 0035）+ 启动 seed 脚本（`AGENT_DEFAULT_BINDINGS` 派生，幂等）
- `AgentBindingCache`：模块级单例，启动 `warmUp` 全量加载 `status=ACTIVE` 行；写时 `refreshOne/invalidate`
- 写时跨字段校验（`field_validator("tool_name")`）：tool_name 必须在 `agent_tool_registry` 中；
  `agent.data_layers` 必须 ⊇ `tool.data_layers`；非法值 Pydantic 422
- `GET /agents/options` 扩展 `tools` 字段（前端 `useAgentOptions()` 复用零额外请求）
- Runtime `_resolveTool` 改造：DB cache 优先；DB 无 binding → 409；DB 绑定漂移（代码未注册）→ 409
- 前端 `AgentRegistryPage` Create/Edit Modal 加 `toolName` Select（实时 layer 覆盖校验）+ Detail Drawer 展示
- `AGENT_TOOLS` dict 完全删除（仅留 `AGENT_DEFAULT_BINDINGS` 作 seed 数据源）
- **与 `LineageLayer` / `AGENT_DATA_LAYERS` 边界**：本期复用既有 3 层词表约束，不引入新层
- **工具定义（`handler/data_object/data_layers`）始终在代码**——handler 与分层授权是 P0 安全防线，不入 DB
- **未来演进**：1:N 工具绑定（中间表 + 工具选择器）、Redis 共享缓存（多实例）、Tool 自身管理 UI（独立排期）
```

- [ ] **Step 3: Write memory file**

Write to `/Users/sunql/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-agent-tool-binding.md`:

```markdown
---
name: qa-system-agent-tool-binding
description: Agent→Tool 关系 DB 化：agent_definition.tool_name 列 + AgentBindingCache（启动预热 + 写时失效）+ 写时跨字段校验
metadata:
  type: project
---

`agent_definition.tool_name: str | None` 列（Alembic 0035）是 Agent→Tool 绑定的 SSOT；
`AGENT_DEFAULT_BINDINGS`（`agent_tools.py`）是启动 seed 的派生源，dict 已删除。

**AgentBindingCache**（`app/services/agent_binding_cache.py`）模块级单例：
- `warmUp(session)` 启动时全量加载 `status=ACTIVE` 行
- `getToolName(agent_code)` 未 warmUp 抛 RuntimeError（fail-fast）
- `invalidate(agent_code=None)` 单条或全清；写路径用 `refreshOne` 主动 reload
- lifespan 顺序：seed → warmUp

**写时严格校验**（`schemas._validateToolName`）：
- `tool_name is None` 放行（元数据 Agent）
- tool_name 不在 `agent_tool_registry` → 422 MSG_AGENT_TOOL_UNKNOWN
- `agent.data_layers` 不完全覆盖 `tool.data_layers` → 422 MSG_AGENT_TOOL_LAYER_MISMATCH

**Runtime `_resolveTool`**：DB cache 优先；cache 未命中 → 409 MSG_AGENT_NOT_RUNNABLE_NO_TOOL；
DB 绑定但代码未注册（漂移）→ 409 MSG_AGENT_TOOL_UNREGISTERED。

**前端**：`AgentRegistryPage` Create/Edit Modal 加 `toolName` Select，候选来自扩展后的
`/agents/options.tools`（按 name 排序）；实时校验 tool.dataLayers ⊆ agent.dataLayers。

**How to apply:**
- 新增工具：在 `agent_tools.py` 注册 `AgentTool` + 加入 `AGENT_DEFAULT_BINDINGS`（仅 seed 用）
- 新增 Agent 默认绑定：在 `AGENT_DEFAULT_BINDINGS` 加一行；启动自动 seed；Admin 可在 UI 覆盖
- 排查 binding 相关 409：先查 cache（`agent_binding_cache._cache`），再查 DB binding，最后查代码 `agent_tool_registry`
- 漂移修复：DB 绑了未注册的 tool → 删 DB 行或重新发布含该 tool 的代码

关联 [[qa-system-agent-vocabulary]]（词表约束）、[[acl-security-review-pattern]]（写路径审查）、
[[qa-system-runbook-quirks]]（docker 重建 + PG 端口）。
```

- [ ] **Step 4: Add memory index entry**

在 `/Users/sunql/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/MEMORY.md` 追加：

```markdown
- [Agent 工具绑定 DB 化](qa-system-agent-tool-binding.md) — agent_definition.tool_name + AgentBindingCache + 写时跨字段校验 + AGENT_TOOLS dict 删除
```

- [ ] **Step 5: Commit Harness docs (in-repo)**

```bash
git add Harness/changes/feat-agent-tool-binding/summary.md \
        Harness/wiki/business-domain.md
git commit -m "docs(harness): feat-agent-tool-binding change summary

记录实现细节、验证结果、关键决策、与既有 change 关系、已知遗留。
Harness/wiki/business-domain.md Agent Runtime 章节补充工具绑定可配置化小节。"
```

- [ ] **Step 6: Memory file (out-of-repo, manual)**

Memory 文件在仓外（`~/.claude/projects/.../memory/`），不通过 git commit。
只需 Write 工具写入即可（Step 3-4 已覆盖）。

- [ ] **Step 7: Final SDD ledger update**

更新 `.superpowers/sdd/2026-09-02-agent-tool-binding/progress.md`：
- 列出 8 commits（含 harness docs）
- 标记 plan complete
- 标注 final review verdict（由 subagent-driven-development skill 在最终 whole-branch review 后填）

---

## Self-Review

**1. Spec coverage:**
- §1 背景与目标 → 整体驱动 Task 1-9
- §2 决策 6 项 → Task 2 (1:1)、Task 2/6 (DB seed)、Task 4 (写时校验)、Task 5 (options 扩展)、Task 6 (cache)、Task 6/8 (admin only)
- §3 数据模型 → Task 1 (列) + Task 2 (seed)
- §4.1-4.2 缓存 → Task 3
- §4.3 写时失效 → Task 6
- §4.4 Pydantic 校验 → Task 4
- §4.5 run 改造 → Task 6
- §4.6 runnable 服务端化 → Task 6
- §5.1 options 扩展 → Task 5
- §5.2 DTO → Task 1
- §5.3 错误消息 → Task 4
- §5.4 行为契约 → Task 4 (POST/PUT) + Task 6 (run 路径)
- §5.5 chat 路径 → 既有，无需任务
- §6 前端 → Task 8
- §7 迁移计划 8 commit → Task 1-8 一对一映射
- §8 测试覆盖 → Task 1-8 每个都含单测/集成/e2e
- §9 风险（Update 歧义）→ spec 登记；plan Task 6 Step 4 走 refreshOne 实现隐含语义（不传=null=清空）
- §10 与既有耦合 → spec 描述，无需专门任务
- §11 未来演进 → spec 描述

无 gap。

**2. Placeholder scan:** ✅ 无 TBD/TODO/"类似"/"待补"。所有代码块含完整可运行内容。

**3. Type consistency:**
- `AgentBindingCache` 接口：Task 3 定义（warmUp/getToolName/invalidate/refreshOne）→ Task 6 引用 → Task 7 删除 fallback 时仍用 ✅
- `agent_tool_registry.all()`：Task 4 stub → Task 5 完整实现 → Task 8 前端 type 引用 ✅
- `agent_binding_cache` 模块级单例：Task 3 → Task 6 ✅
- `_validateToolNameShared`：Task 4 定义 → Create/Update 共用 ✅
- `tool_name_updated_at`：Task 1 模型 + DTO + Task 2 seed 自动戳 ✅
- `AGENT_TOOLS` → `AGENT_DEFAULT_BINDINGS`：Task 7 改名 + 引用更新 ✅
- `MSG_AGENT_TOOL_*`：Task 4 定义 → Task 6 引用 ✅
- 字段命名：DB `tool_name` ↔ JSON `toolName` ↔ TS `toolName` ↔ form `toolName` 全链一致 ✅
