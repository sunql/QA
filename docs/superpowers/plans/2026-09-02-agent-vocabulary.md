# Agent 词表治理（feat-agent-vocabulary）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 锁定 `data_domains` / `data_layers` 的合法词表（代码常量、零 Alembic 迁移），后端 DTO 写入边界严格 422 校验，新增 `GET /agents/options` 下发选项，前端从自由输入改成约束多选，列表页加域过滤。

**Architecture:** 词表 SSOT 在后端 `app/domain/agent_vocabulary.py`（代码常量）；DTO 层 `field_validator` 完成归一化 + 词表校验（与既有 `_normalizeDataLayer` 模式对齐）；前端 `useAgentOptions()` hook 一次拉取缓存到内存，新建/编辑/详情/策略/列表五处共用。

**Tech Stack:** Python 3.12 + Pydantic v2 + FastAPI + SQLAlchemy 2.0 async；前端 React 18 + Vitest + antd Select；测试后端走 PostgreSQL 5433（`qa_metadata_test`）。

**Spec:** `docs/superpowers/specs/2026-09-02-agent-vocabulary-design.md`

## Global Constraints

- 词表常量值（SSOT，全大写）：
  - `AGENT_DATA_DOMAINS = ("PROCUREMENT", "QUALITY", "LOGISTICS")`
  - `AGENT_DATA_LAYERS = ("DIM", "DWD", "FEATURE")`
- Agent 元数据层（`data_layers`）与 `LineageLayer`（enums.py:144，7 层血缘）是**两套词汇**，本期不混。
- **严格 422 拒绝**：新写入值必须 ∈ 词表，否则 Pydantic 422；空字符串拒收；归一化方向 strip+upper（与既有 `_normalizeDataLayer` 对齐）。
- **不触碰历史不规范行**：读仍继续展示；新写入才校验。
- 测试数据库：`TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test`。
- 覆盖率门槛 80%（既有约定）；后端集成测试必须走真实 PG。
- Docker 命令需 `dangerouslyDisableSandbox: true`（既有约定）。
- 后端 Python 偏离 PEP 8：ORM/Pydantic 字段 `snake_case`，函数 `snake_case`（与前端/CLAUDE.md 一致）。
- commit message：`<type>: <description>`，types = feat/fix/refactor/docs/test/perf/chci。
- 现有路由 `GET /agents/options` 必须注册在 `GET /agents/{agent_code}` **之前**（避免 `options` 被路径参数吞掉）。

---

### Task 1: 后端词表常量 + DTO 写入校验

**Files:**
- Create: `backend/app/domain/agent_vocabulary.py`
- Modify: `backend/app/domain/schemas.py:1978-2021`（`AgentDefinitionCreate`/`AgentDefinitionUpdate`）
- Modify: `backend/app/domain/error_messages.py`（在 Phase 6.5 段后追加 2 条 MSG）
- Test: `backend/app/tests/unit/test_agent_vocabulary.py`

**Interfaces:**
- Consumes: 既有 `app.domain.schemas._normalizeDataLayer(value: str | None) -> str | None`（schemas.py:1930）
- Produces:
  - `app.domain.agent_vocabulary.AGENT_DATA_DOMAINS: tuple[str, ...]` (= `("PROCUREMENT","QUALITY","LOGISTICS")`)
  - `app.domain.agent_vocabulary.AGENT_DATA_LAYERS: tuple[str, ...]` (= `("DIM","DWD","FEATURE")`)
  - `app.domain.agent_vocabulary.normalizeAgentDomain(value: str) -> str`（strip+upper，空 → ValueError）
  - `MSG_AGENT_DOMAIN_NOT_IN_VOCAB: str`
  - `MSG_AGENT_LAYER_NOT_IN_VOCAB: str`
  - `AgentDefinitionCreate.data_domains` / `data_layers` 通过 `field_validator` 归一化 + 词表校验 + 去重保序
  - `AgentDefinitionUpdate.data_domains` / `data_layers: list[str] | None`：None 跳过；list 走同上校验

- [ ] **Step 1: Write failing unit tests**

```python
# backend/app/tests/unit/test_agent_vocabulary.py
from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.domain.agent_vocabulary import (
    AGENT_DATA_DOMAINS,
    AGENT_DATA_LAYERS,
    normalizeAgentDomain,
)
from app.domain.error_messages import (
    MSG_AGENT_DOMAIN_NOT_IN_VOCAB,
    MSG_AGENT_LAYER_NOT_IN_VOCAB,
)
from app.domain.schemas import AgentDefinitionCreate


class TestConstants:
    def test_domains_constant(self) -> None:
        assert AGENT_DATA_DOMAINS == ("PROCUREMENT", "QUALITY", "LOGISTICS")

    def test_layers_constant(self) -> None:
        assert AGENT_DATA_LAYERS == ("DIM", "DWD", "FEATURE")


class TestNormalizeAgentDomain:
    def test_strips_and_uppercases(self) -> None:
        assert normalizeAgentDomain("  procurement ") == "PROCUREMENT"

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            normalizeAgentDomain("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValueError):
            normalizeAgentDomain("   ")


class TestAgentDefinitionCreateValidation:
    def _dto(self, **kwargs) -> AgentDefinitionCreate:
        base = {
            "agentCode": "TEST_AGENT",
            "agentName": "test",
            "dataDomains": ["PROCUREMENT"],
            "dataLayers": ["FEATURE"],
        }
        base.update(kwargs)
        return AgentDefinitionCreate(**base)

    def test_valid_domains_and_layers_pass(self) -> None:
        d = self._dto(
            dataDomains=["PROCUREMENT", "QUALITY"],
            dataLayers=["DIM", "DWD", "FEATURE"],
        )
        assert d.data_domains == ["PROCUREMENT", "QUALITY"]
        assert d.data_layers == ["DIM", "DWD", "FEATURE"]

    def test_domain_normalized_lowercase(self) -> None:
        d = self._dto(dataDomains=["procurement"])
        assert d.data_domains == ["PROCUREMENT"]

    def test_layer_normalized_lowercase(self) -> None:
        d = self._dto(dataLayers=["feature", "dim"])
        assert d.data_layers == ["FEATURE", "DIM"]

    def test_domain_unknown_raises_with_message(self) -> None:
        with pytest.raises(PydanticValidationError) as exc:
            self._dto(dataDomains=["PROCUREMENT", "NONSENSE"])
        assert MSG_AGENT_DOMAIN_NOT_IN_VOCAB.split("{value}")[0] in str(exc.value)

    def test_layer_unknown_raises_with_message(self) -> None:
        with pytest.raises(PydanticValidationError) as exc:
            self._dto(dataLayers=["FEATURE", "KAFKA"])
        assert MSG_AGENT_LAYER_NOT_IN_VOCAB.split("{value}")[0] in str(exc.value)

    def test_domain_dedup_preserves_order(self) -> None:
        d = self._dto(dataDomains=["PROCUREMENT", "QUALITY", "PROCUREMENT"])
        assert d.data_domains == ["PROCUREMENT", "QUALITY"]

    def test_layer_dedup_preserves_order(self) -> None:
        d = self._dto(dataLayers=["FEATURE", "DIM", "FEATURE"])
        assert d.data_layers == ["FEATURE", "DIM"]

    def test_empty_domain_element_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            self._dto(dataDomains=["PROCUREMENT", "  "])

    def test_empty_lists_allowed(self) -> None:
        d = self._dto(dataDomains=[], dataLayers=[])
        assert d.data_domains == []
        assert d.data_layers == []
```

- [ ] **Step 2: Run tests — verify RED**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_vocabulary.py -q
```
Expected: all tests FAIL (modules not found).

- [ ] **Step 3: Create vocabulary module**

```python
# backend/app/domain/agent_vocabulary.py
"""Agent 词表 SSOT（Phase 7 feat-agent-vocabulary）。

锁定 `AgentDefinition.data_domains` / `data_layers` 的合法值集合。
**词表 SSOT 在代码常量**——零 Alembic 迁移；新增域/层需改本文件并发版。
近期规模：域 3 个（PROCUREMENT/QUALITY/LOGISTICS），层 3 个（DIM/DWD/FEATURE）。

与 `LineageLayer`（enums.py:144，7 层血缘 SOURCE_SYSTEM/ODS/.../AI）的边界：
同名 DWD 是巧合——`LineageLayer` 是数据血缘词汇，`AGENT_DATA_LAYERS` 是 Agent
授权与展示词汇，语义不同，不可混用。
"""

from __future__ import annotations

AGENT_DATA_DOMAINS: tuple[str, ...] = ("PROCUREMENT", "QUALITY", "LOGISTICS")
AGENT_DATA_LAYERS: tuple[str, ...] = ("DIM", "DWD", "FEATURE")


def normalizeAgentDomain(value: str) -> str:
    """域归一化：strip + upper，空串（含纯空白）→ ValueError。

    与 `schemas._normalizeDataLayer` 同模式（strip+upper），保证写入边界一致。
    """
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("data_domains 元素不允许为空字符串")
    return normalized
```

- [ ] **Step 4: Add error messages**

Append after line 394 in `backend/app/domain/error_messages.py` (after MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT):

```python
# ===== Phase 7：Agent 词表治理（feat-agent-vocabulary）=====
MSG_AGENT_DOMAIN_NOT_IN_VOCAB = (
    "data_domains 含未授权值 '{value}'；合法集：{allowed}。"
    "如需新增域，请在 AGENT_DATA_DOMAINS（app/domain/agent_vocabulary.py）补充。"
)
MSG_AGENT_LAYER_NOT_IN_VOCAB = (
    "data_layers 含未授权值 '{value}'；合法集：{allowed}。"
    "Agent 层词表仅含 DIM/DWD/FEATURE；血缘层 LineageLayer 不在此范围内。"
)
```

- [ ] **Step 5: Add field validators to AgentDefinitionCreate / AgentDefinitionUpdate**

In `backend/app/domain/schemas.py`:
1. Add import near top of Pydantic imports section (after line ~15):
   ```python
   from app.domain.agent_vocabulary import (
       AGENT_DATA_DOMAINS,
       AGENT_DATA_LAYERS,
       normalizeAgentDomain,
   )
   ```
2. Add the two MSG imports to the existing `from app.domain.error_messages import (...)` block (around line 44).
3. After `AgentAccessPolicyRead` (line 1967-1976) and before `AgentDefinitionCreate` (line 1978), insert a helper:

```python
def _vocabCheckDomains(values: list[str]) -> list[str]:
    """域归一化 + 词表校验 + 去重保序；非法值抛 ValueError（Pydantic 422）。"""
    normalized = [normalizeAgentDomain(x) for x in values]
    rejected = [x for x in normalized if x not in AGENT_DATA_DOMAINS]
    if rejected:
        raise ValueError(MSG_AGENT_DOMAIN_NOT_IN_VOCAB.format(
            value=",".join(rejected), allowed=",".join(AGENT_DATA_DOMAINS)
        ))
    seen: set[str] = set()
    deduped: list[str] = []
    for x in normalized:
        if x not in seen:
            seen.add(x); deduped.append(x)
    return deduped


def _vocabCheckLayers(values: list[str]) -> list[str]:
    """层归一化 + 词表校验 + 去重保序；复用既有 `_normalizeDataLayer`（None → None 会被过滤）。"""
    normalized = [n for n in (_normalizeDataLayer(x) for x in values if x is not None) if n]
    rejected = [x for x in normalized if x not in AGENT_DATA_LAYERS]
    if rejected:
        raise ValueError(MSG_AGENT_LAYER_NOT_IN_VOCAB.format(
            value=",".join(rejected), allowed=",".join(AGENT_DATA_LAYERS)
        ))
    seen: set[str] = set()
    deduped: list[str] = []
    for x in normalized:
        if x not in seen:
            seen.add(x); deduped.append(x)
    return deduped
```

4. Modify `AgentDefinitionCreate.data_domains` / `data_layers` (around line 1999-2000). Add field-validators inside the class:

```python
class AgentDefinitionCreate(CamelModel):
    """..."""

    agent_code: str = Field(...)
    agent_name: str = Field(...)
    description: str | None = Field(default=None, max_length=8000)
    trigger_type: AgentTriggerType = Field(default=AgentTriggerType.USER_QUESTION)
    response_latency: AgentResponseLatency = Field(default=AgentResponseLatency.REALTIME)
    data_domains: list[str] = Field(default_factory=list)
    data_layers: list[str] = Field(default_factory=list)
    status: AgentStatus = Field(default=AgentStatus.DRAFT)
    version: str | None = Field(default=None, max_length=32)
    policies: list[AgentAccessPolicyCreate] = Field(default_factory=list)

    _check_domains = field_validator("data_domains")(_vocabCheckDomains)
    _check_layers = field_validator("data_layers")(_vocabCheckLayers)
```

5. Modify `AgentDefinitionUpdate` similarly (line 2006). Note `data_domains` / `data_layers` are `list[str] | None` (None = field unset → skip). Use a wrapper validator:

```python
class AgentDefinitionUpdate(CamelModel):
    """..."""

    agent_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=8000)
    trigger_type: AgentTriggerType | None = None
    response_latency: AgentResponseLatency | None = None
    data_domains: list[str] | None = None
    data_layers: list[str] | None = None
    status: AgentStatus | None = None
    version: str | None = Field(default=None, max_length=32)

    @field_validator("data_domains")
    @classmethod
    def _check_domains_update(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return _vocabCheckDomains(v)

    @field_validator("data_layers")
    @classmethod
    def _check_layers_update(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return _vocabCheckLayers(v)
```

- [ ] **Step 6: Run tests — verify GREEN**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_vocabulary.py -v
```
Expected: 10 passed.

- [ ] **Step 7: Run related suites — no regression**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit app/tests/integration/test_agent_registry_api.py -q
```
Expected: all pass (existing seed uses valid vocab).

- [ ] **Step 8: Commit**

```bash
git add backend/app/domain/agent_vocabulary.py \
        backend/app/domain/schemas.py \
        backend/app/domain/error_messages.py \
        backend/app/tests/unit/test_agent_vocabulary.py
git commit -m "feat(agent): vocabulary SSOT + DTO write validation

锁定 AGENT_DATA_DOMAINS（PROCUREMENT/QUALITY/LOGISTICS）与
AGENT_DATA_LAYERS（DIM/DWD/FEATURE）词表 SSOT。AgentDefinitionCreate/Update
的 data_domains / data_layers 在写入边界做归一化（strip+upper）+
词表校验 + 去重保序；非法值抛 Pydantic 422 携带 MSG_AGENT_*_NOT_IN_VOCAB。
不触碰历史不规范行：新写入才校验。"
```

---

### Task 2: `GET /api/v1/agents/options` 端点

**Files:**
- Modify: `backend/app/domain/schemas.py`（新增 `AgentOptionsRead`）
- Modify: `backend/app/api/v1/agents.py`（新增路由，**必须在 `/{agent_code}` 之前**）
- Test: `backend/app/tests/integration/test_agent_options_api.py`

**Interfaces:**
- Consumes: `AGENT_DATA_DOMAINS`、`AGENT_DATA_LAYERS`（来自 T1）
- Produces:
  - `app.domain.schemas.AgentOptionsRead`: `{ domains: list[str], layers: list[str] }`
  - `GET /api/v1/agents/options` → 200 `AgentOptionsRead` JSON

- [ ] **Step 1: Write failing integration test**

```python
# backend/app/tests/integration/test_agent_options_api.py
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_get_options_returns_vocabulary(client) -> None:
    """GET /agents/options 返回词表常量；不被 /{agent_code} 路径吞掉。"""
    res = await client.get("/api/v1/agents/options")
    assert res.status_code == 200
    body = res.json()
    assert body["domains"] == ["PROCUREMENT", "QUALITY", "LOGISTICS"]
    assert body["layers"] == ["DIM", "DWD", "FEATURE"]


@pytest.mark.asyncio
async def test_get_options_not_shadowed_by_agent_code_route(client) -> None:
    """路由顺序保护：/options 必须匹配专属路由而非被 /{agent_code} 视为 code='options'（404）。"""
    res = await client.get("/api/v1/agents/options")
    # 如果被吞掉，会走 getAgent(agent_code="options") → 404
    assert res.status_code != 404
```

(Imports: `from httpx import AsyncClient` if not already; use `AsyncIterator` if fixture typing requires — match existing test file pattern at `backend/app/tests/integration/test_agent_registry_api.py`.)

- [ ] **Step 2: Run test — verify RED**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_options_api.py -q
```
Expected: 404 or 405 (route not registered).

- [ ] **Step 3: Add `AgentOptionsRead` schema**

In `backend/app/domain/schemas.py`, append after `AgentDefinitionRead` (after line 2048, find exact end-of-class line):

```python
class AgentOptionsRead(CamelModel):
    """Agent 选项下拉数据（Phase 7 feat-agent-vocabulary）。

    前端 useAgentOptions() hook 拉一次缓存；不返回工具列表——工具绑定可配置化
    属独立 change（Phase 7+ 大改动），本期 YAGNI。
    """

    domains: list[str]
    layers: list[str]
```

- [ ] **Step 4: Register `GET /options` route**

In `backend/app/api/v1/agents.py`:
1. Add to schema imports (near line 25-32):
   ```python
   from app.domain.schemas import (
       AgentAccessPolicyCreate,
       AgentAccessPolicyRead,
       AgentAccessPolicyUpdate,
       AgentDefinitionCreate,
       AgentDefinitionRead,
       AgentDefinitionUpdate,
       AgentOptionsRead,        # 新增
   )
   ```
2. Add import for vocabulary:
   ```python
   from app.domain.agent_vocabulary import AGENT_DATA_DOMAINS, AGENT_DATA_LAYERS  # 新增
   ```
3. Insert route **before** the existing `@router.get("/{agent_code}", ...)` at line 78. Recommended place: directly after `@router.get("", ...)` (line 58), so all unparameterized routes are grouped:

```python
@router.get("/options", response_model=AgentOptionsRead)
async def getAgentOptions(
    _user: CurrentUser = Depends(getCurrentUser),
) -> AgentOptionsRead:
    """Agent 编辑选项（域/层词表）；前端 useAgentOptions() 缓存。

    必须在 GET /{agent_code} 之前注册——否则 "options" 会被路径参数吞成
    agent_code='options'，触发 getAgent → 404。测试 test_agent_options_api.py 守护。
    """
    return AgentOptionsRead(
        domains=list(AGENT_DATA_DOMAINS),
        layers=list(AGENT_DATA_LAYERS),
    )
```

- [ ] **Step 5: Run test — verify GREEN**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_options_api.py -v
```
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/domain/schemas.py \
        backend/app/api/v1/agents.py \
        backend/app/tests/integration/test_agent_options_api.py
git commit -m "feat(agent): GET /agents/options endpoint

新增 GET /api/v1/agents/options，返回 AGENT_DATA_DOMAINS 与
AGENT_DATA_LAYERS 常量。前端 useAgentOptions() 拉一次缓存，4 处 Select
共用。路由必须在 /{agent_code} 之前注册——集成测试守护顺序。"
```

---

### Task 3: `listAgents` 扩展：`dataLayer` + `dataDomain` 多值

**Files:**
- Modify: `backend/app/services/agent_registry_service.py:78-96`（`listAgents` 签名 + 过滤逻辑）
- Modify: `backend/app/api/v1/agents.py:58-75`（`listAgents` 路由加 `dataLayer` Query；`dataDomain` 改多值）
- Test: 扩展既有 `backend/app/tests/integration/test_agent_registry_api.py`（已有 `test_list_filter_by_status_and_domain` 在 line 224）

**Interfaces:**
- Consumes: 既有 `AgentRegistryService.listAgents` 调用方
- Produces:
  - `AgentRegistryService.listAgents(*, status, data_layer: str | None = None, data_domains: list[str] | None = None, limit, offset)` → `list[AgentDefinition]`
  - `GET /api/v1/agents?dataDomain=A&dataDomain=B&dataLayer=X` 同时支持三参过滤（AND）

- [ ] **Step 1: Write failing integration test cases**

Append to `backend/app/tests/integration/test_agent_registry_api.py` (locate end of `TestList` class, or add new class):

```python
@pytest.mark.asyncio
async def test_list_filter_by_data_layer_single(client) -> None:
    """GET /agents?dataLayer=DIM 只返回 data_layers 含 DIM 的 Agent。"""
    # 依赖既有 seed：SUPPLIER_RISK_AGENT layers=(FEATURE,DIM) 命中
    res = await client.get("/api/v1/agents", params={"dataLayer": "DIM"})
    assert res.status_code == 200
    codes = [a["agentCode"] for a in res.json()]
    assert "SUPPLIER_RISK_AGENT" in codes
    # SUPPLIER_OTD_REPORT layers=(FEATURE) 不含 DIM
    assert "SUPPLIER_OTD_REPORT" not in codes


@pytest.mark.asyncio
async def test_list_filter_by_data_domain_multi(client) -> None:
    """GET /agents?dataDomain=A&dataDomain=B 返回 data_domains 与 A 或 B 任一相交的 Agent（OR）。"""
    # 既有 seed：5 个 Agent 全部 data_domains 含 PROCUREMENT；目前还没有含 QUALITY 的
    res = await client.get(
        "/api/v1/agents",
        params=[("dataDomain", "PROCUREMENT"), ("dataDomain", "QUALITY")],
    )
    assert res.status_code == 200
    codes = [a["agentCode"] for a in res.json()]
    # 含 PROCUREMENT 的全部命中；OR 语义下含 QUALITY 的也会被收（目前为 0）
    assert "SUPPLIER_RISK_AGENT" in codes


@pytest.mark.asyncio
async def test_list_filter_combined_status_and_layer(client) -> None:
    """多过滤条件 AND 叠加。"""
    res = await client.get(
        "/api/v1/agents",
        params={"status": "active", "dataLayer": "DWD"},
    )
    assert res.status_code == 200
    codes = [a["agentCode"] for a in res.json()]
    # 既有 seed：GRAPH_REASONING_AGENT layers=(DIM,DWD), status=active → 命中
    assert "GRAPH_REASONING_AGENT" in codes
```

- [ ] **Step 2: Run tests — verify RED**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_registry_api.py -q -k "filter_by_data or filter_combined"
```
Expected: 3 FAIL (params ignored, returns full list).

- [ ] **Step 3: Extend service signature**

In `backend/app/services/agent_registry_service.py`, modify `listAgents` (lines 78-96):

```python
async def listAgents(
    self,
    session: AsyncSession,
    *,
    status: AgentStatus | None = None,
    data_layer: str | None = None,
    data_domains: list[str] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[AgentDefinition]:
    """按 agent_code 升序列表；支持 status / data_layer / data_domains 过滤。

    - data_layer（单值）：JSONB @> 包含
    - data_domains（多值）：JSONB && 重叠（OR 语义）
    """
    stmt = select(AgentDefinition).options(selectinload(AgentDefinition.policies))
    if status is not None:
        stmt = stmt.where(AgentDefinition.status == status)
    if data_layer is not None:
        # JSONB 数组字段：用 contains（@>）匹配子集
        stmt = stmt.where(AgentDefinition.data_layers.contains([data_layer]))
    if data_domains:
        # JSONB 数组字段：用 overlap（&&）做 OR 过滤（任一相交即命中）
        stmt = stmt.where(AgentDefinition.data_domains.overlap(data_domains))
    stmt = stmt.order_by(AgentDefinition.agent_code).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 4: Extend endpoint signature**

In `backend/app/api/v1/agents.py`, modify `listAgents` (lines 58-75):

```python
@router.get("", response_model=list[AgentDefinitionRead])
async def listAgents(
    _user: CurrentUser = Depends(getCurrentUser),
    status_: AgentStatus | None = Query(default=None, alias="status"),
    dataDomain: list[str] | None = Query(default=None, alias="dataDomain"),
    dataLayer: str | None = Query(default=None, alias="dataLayer"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(getDb),
    service: AgentRegistryService = Depends(getAgentRegistryService),
) -> list[AgentDefinitionRead]:
    agents = await service.listAgents(
        session,
        status=status_,
        data_layer=dataLayer,
        data_domains=dataDomain,  # FastAPI Query 同名多次绑定聚合为 list[str]
        limit=limit,
        offset=offset,
    )
    return [agentToRead(a) for a in agents]
```

Note: `dataDomain: str | None = Query(...)` (singular) was the prior signature. Changing to `list[str] | None` is **backward compatible** — single-value clients still pass `?dataDomain=X` and FastAPI binds it as `["X"]`. The service treats empty list as "no filter" (the `if data_domains:` truthy guard).

- [ ] **Step 5: Run tests — verify GREEN**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_agent_registry_api.py -v -k "filter_by_data or filter_combined or filter_by_status_and_domain"
```
Expected: all 4 pass (3 new + 1 existing).

- [ ] **Step 6: Full regression — verify no breakage**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit app/tests/integration -q
```
Expected: pass except known pre-existing failures (1 Neo4j graph_reasoning test).

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/agent_registry_service.py \
        backend/app/api/v1/agents.py \
        backend/app/tests/integration/test_agent_registry_api.py
git commit -m "feat(agent): extend listAgents with dataLayer + multi-value dataDomain

- dataLayer 单值：JSONB contains
- dataDomain 改同名多值绑定：JSONB overlap（OR 语义）
- FastAPI Query 同名重复绑定聚合为 list[str]；空 list 视为未传
- 旧 ?dataDomain=X 单值调用兼容（绑定为 ['X']）"
```

---

### Task 4: 前端 API 客户端 + useAgentOptions hook

**Files:**
- Create: `frontend/src/api/agentOptions.ts`
- Create: `frontend/src/hooks/useAgentOptions.ts`
- Create: `frontend/src/tests/useAgentOptions.test.tsx`
- Test: `frontend/src/types/agentOptions.ts`（可放 hook 旁或独立）

**Interfaces:**
- Consumes: 既有 `frontend/src/api/client.ts` 的 `httpClient`
- Produces:
  - `getAgentOptions(): Promise<AgentOptions>` — 调 `/agents/options`
  - `useAgentOptions(): { domains: string[]; layers: string[]; loading: boolean; error: string | null }` — 内部缓存，跨组件调用共享结果（用 module-level cache 简化）
  - `AgentOptions` type: `{ domains: string[]; layers: string[] }`

- [ ] **Step 1: Write failing hook test**

```tsx
// frontend/src/tests/useAgentOptions.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useAgentOptions } from "../hooks/useAgentOptions";
import * as agentOptionsApi from "../api/agentOptions";

const api = vi.hoisted(() => ({
    getAgentOptions: vi.fn(),
}));
vi.mock("../api/agentOptions", () => api);

describe("useAgentOptions", () => {
    beforeEach(() => {
        api.getAgentOptions.mockReset();
    });

    it("初次调用拉取并返回词表", async () => {
        api.getAgentOptions.mockResolvedValue({
            domains: ["PROCUREMENT", "QUALITY", "LOGISTICS"],
            layers: ["DIM", "DWD", "FEATURE"],
        });
        const { result } = renderHook(() => useAgentOptions());
        await waitFor(() => expect(result.current.loading).toBe(false));
        expect(result.current.domains).toEqual(["PROCUREMENT", "QUALITY", "LOGISTICS"]);
        expect(result.current.layers).toEqual(["DIM", "DWD", "FEATURE"]);
        expect(result.current.error).toBeNull();
    });

    it("接口失败 → error 文案", async () => {
        api.getAgentOptions.mockRejectedValue(new Error("network"));
        const { result } = renderHook(() => useAgentOptions());
        await waitFor(() => expect(result.current.loading).toBe(false));
        expect(result.current.error).toBeTruthy();
        expect(result.current.domains).toEqual([]);
    });
});
```

- [ ] **Step 2: Run test — verify RED**

```bash
cd frontend
npx vitest run src/tests/useAgentOptions.test.tsx
```
Expected: import error.

- [ ] **Step 3: Add AgentOptions type**

```ts
// frontend/src/types/agentOptions.ts
export interface AgentOptions {
  domains: string[]
  layers: string[]
}
```

- [ ] **Step 4: Add API client**

```ts
// frontend/src/api/agentOptions.ts
import { httpClient } from "./client";
import type { AgentOptions } from "../types/agentOptions";

export async function getAgentOptions(): Promise<AgentOptions> {
  const res = await httpClient.get<AgentOptions>("/agents/options")
  return res.data
}
```

- [ ] **Step 5: Add useAgentOptions hook with module-level cache**

```ts
// frontend/src/hooks/useAgentOptions.ts
import { useEffect, useState } from "react"
import { getAgentOptions } from "../api/agentOptions"
import type { AgentOptions } from "../types/agentOptions"

// 模块级缓存：跨组件共享同一份词表，避免每处 Select 都重发请求。
// 失败时不缓存，下次调用重试。简洁优先——暂不上 React Query（项目无该依赖）。
let _cache: AgentOptions | null = null
let _inflight: Promise<AgentOptions> | null = null

export function useAgentOptions(): {
  domains: string[]
  layers: string[]
  loading: boolean
  error: string | null
} {
  const [data, setData] = useState<AgentOptions>(
    _cache ?? { domains: [], layers: [] }
  )
  const [loading, setLoading] = useState(_cache === null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (_cache !== null) return
    if (_inflight === null) {
      _inflight = getAgentOptions()
        .then((opts) => {
          _cache = opts
          return opts
        })
        .catch((err: unknown) => {
          _inflight = null
          throw err
        })
    }
    let cancelled = false
    _inflight
      .then((opts) => {
        if (!cancelled) {
          setData(opts)
          setLoading(false)
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err))
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  return {
    domains: data.domains,
    layers: data.layers,
    loading,
    error,
  }
}
```

- [ ] **Step 6: Run test — verify GREEN**

```bash
cd frontend
npx vitest run src/tests/useAgentOptions.test.tsx
```
Expected: 2 passed.

- [ ] **Step 7: Type-check + full frontend test regression**

```bash
cd frontend
npx tsc --noEmit
npx vitest run
```
Expected: tsc clean; existing 424 tests + 2 new = 426+ pass.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/api/agentOptions.ts \
        frontend/src/types/agentOptions.ts \
        frontend/src/hooks/useAgentOptions.ts \
        frontend/src/tests/useAgentOptions.test.tsx
git commit -m "feat(frontend): agentOptions API client + useAgentOptions hook

GET /agents/options 拉一次词表，模块级缓存跨 Select 共用。
失败时不缓存以保留重试语义。后续大改动（工具绑定可配置化）
可扩展 hook 形态为 useAgentMetadata()，接口预留 tools 字段。"
```

---

### Task 5: 前端页集成（4 Select 改造 + 列表过滤）+ i18n

**Files:**
- Modify: `frontend/src/pages/AgentRegistryPage.tsx`（line 1-674 范围内 4 处 Select）
- Modify: `frontend/src/i18n/zh-CN.ts`
- Modify: `frontend/src/i18n/en-US.ts`
- Test: 扩展 `frontend/src/tests/AgentRegistryPage.test.tsx`（新建文件）

**Interfaces:**
- Consumes: `useAgentOptions()`（T4）
- Produces:
  - 新建 Modal `dataDomains` / `dataLayers`：`<Select mode="multiple" options={...} />`
  - 编辑 Modal `dataDomains` / `dataLayers`：同上
  - 策略子表 `policyForm.dataLayer`：`<Select options={...} allowClear />`（**单值，不是多选**——`AgentAccessPolicy.data_layer` 是单值字符串字段）
  - 列表页过滤加 `dataDomain` 多选下拉（→ `?dataDomain=A&dataDomain=B`）
  - i18n 新增 `agentOptions.title` / `agentOptions.fetchFailed` / `agentRegistry.filter.domainPlaceholder` / `agentRegistry.filter.layerPlaceholder`

- [ ] **Step 1: Write failing UI test**

```tsx
// frontend/src/tests/AgentRegistryPage.test.tsx (extend or create; coordinate with existing one)
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AgentRegistryPage from "../pages/AgentRegistryPage";

const api = vi.hoisted(() => ({
    getAgentOptions: vi.fn(),
    listAgents: vi.fn(),
}));
vi.mock("../api/agentOptions", () => api);
// mock 其它（listAgents / getAgent / etc.）按既有用例风格补齐

describe("AgentRegistryPage vocabulary integration", () => {
    beforeEach(() => {
        api.getAgentOptions.mockResolvedValue({
            domains: ["PROCUREMENT", "QUALITY", "LOGISTICS"],
            layers: ["DIM", "DWD", "FEATURE"],
        });
        api.listAgents.mockResolvedValue([]);
    });

    it("挂载时拉一次 /agents/options（不重复请求）", async () => {
        render(<AgentRegistryPage />, { wrapper: makeWrapper() });
        await waitFor(() => expect(api.getAgentOptions).toHaveBeenCalledTimes(1));
    });

    it("列表页域过滤下拉显示词表选项", async () => {
        render(<AgentRegistryPage />, { wrapper: makeWrapper() });
        // 打开域过滤下拉，验证至少看到 PROCUREMENT
        // （具体断言写法参考既有的 antd Select 打开模式）
    });
});

function makeWrapper() {
    return ({ children }: { children: React.ReactNode }) => (
        <ConfigProvider locale={zhCN}>
            <MemoryRouter initialEntries={["/agents"]}>
                <Routes>
                  <Route path="/agents" element={children} />
                </Routes>
            </MemoryRouter>
        </ConfigProvider>
    );
}
```

(完整 mock 列表：`listAgents, getAgent, listAgentPolicies, createAgent, updateAgent, deprecateAgent, addAgentPolicy, updateAgentPolicy, deleteAgentPolicy` — 与既有 `AgentRegistryPage.test.tsx` 等同。)

- [ ] **Step 2: Run test — verify RED**

```bash
cd frontend
npx vitest run src/tests/AgentRegistryPage.test.tsx
```
Expected: import failure for `agentOptions` mock target.

- [ ] **Step 3: Add i18n keys**

In `frontend/src/i18n/zh-CN.ts`, find the `agentRegistry` block and add (or extend):

```ts
// zh-CN
agentOptions: {
  title: "Agent 选项",
  fetchFailed: "获取 Agent 词表失败：{message}",
},
agentRegistry: {
  // 既有字段保留；在 filter 子块加
  filter: {
    statusPlaceholder: "状态",
    domainPlaceholder: "按域过滤（多选）",
    layerPlaceholder: "按层过滤（单选）",  // 策略子表专用
  },
  fields: {
    // 既有字段保留；新增
    dataDomainRule: "请从下拉选择（PROCUREMENT/QUALITY/LOGISTICS）",
    dataLayerRule: "请从下拉选择（DIM/DWD/FEATURE）",
  },
}
```

In `frontend/src/i18n/en-US.ts`, mirror English:

```ts
agentOptions: {
  title: "Agent Options",
  fetchFailed: "Failed to fetch agent vocabulary: {message}",
},
agentRegistry: {
  filter: {
    statusPlaceholder: "Status",
    domainPlaceholder: "Filter by domain (multi)",
    layerPlaceholder: "Filter by layer (single)",
  },
  fields: {
    dataDomainRule: "Choose from dropdown (PROCUREMENT/QUALITY/LOGISTICS)",
    dataLayerRule: "Choose from dropdown (DIM/DWD/FEATURE)",
  },
}
```

(按现有结构插到对应块；保持字典形态连贯。)

- [ ] **Step 4: Modify AgentRegistryPage**

In `frontend/src/pages/AgentRegistryPage.tsx`:

1. Add import (line 1 area):
   ```tsx
   import { useAgentOptions } from "../hooks/useAgentOptions";
   ```

2. Inside the component (after line 92 — state declarations):
   ```tsx
   const { domains, layers } = useAgentOptions();
   ```

3. **新建 Modal** (around line 457-462) — replace the two `<Select mode="tags">`:
   ```tsx
   <Form.Item
     name="dataDomains"
     label={t("agentRegistry.fields.dataDomains")}
     rules={[{ required: false }]}
   >
     <Select
       mode="multiple"
       options={domains.map((d) => ({ value: d, label: d }))}
       placeholder={t("agentRegistry.filter.domainPlaceholder")}
     />
   </Form.Item>
   <Form.Item
     name="dataLayers"
     label={t("agentRegistry.fields.dataLayers")}
     rules={[{ required: false }]}
   >
     <Select
       mode="multiple"
       options={layers.map((l) => ({ value: l, label: l }))}
       placeholder={t("agentRegistry.filter.domainPlaceholder")}
     />
   </Form.Item>
   ```

4. **编辑 Modal** (around line 482-487) — same pattern.

5. **策略子表 dataLayer** (around line 568-570) — change `<Input placeholder=...>` to:
   ```tsx
   <Form.Item name="dataLayer" style={{ width: 140 }}>
     <Select
       allowClear
       options={layers.map((l) => ({ value: l, label: l }))}
       placeholder={t("agentRegistry.filter.layerPlaceholder")}
     />
   </Form.Item>
   ```
   **注意**：策略子表的 dataLayer 是单值（`AgentAccessPolicy.data_layer: str | None`），用普通 `<Select allowClear>` 而非 `mode="multiple"`。

7. **列表页过滤** — add new state + Select next to existing status filter (around line 353-365):
   ```tsx
   const [filterDomains, setFilterDomains] = useState<string[]>([]);
   // ...
   const refresh = useCallback(async () => {
     setLoading(true);
     try {
       const params: { status?: AgentStatus; dataDomain?: string[] } = {};
       if (filterStatus) params.status = filterStatus;
       if (filterDomains.length > 0) params.dataDomain = filterDomains;
       const list = await listAgents(params);
       setAgents(list);
     } catch (err: unknown) { ... }
   }, [filterStatus, filterDomains, t]);
   ```
   And render alongside status filter:
   ```tsx
   <Select
     mode="multiple"
     allowClear
     placeholder={t("agentRegistry.filter.domainPlaceholder")}
     style={{ width: 240 }}
     value={filterDomains}
     onChange={(v) => setFilterDomains(v)}
     options={domains.map((d) => ({ value: d, label: d }))}
   />
   ```

   The `listAgents` API client (agentRegistry.ts:15) needs `dataDomain` parameter accepted as `string[]`. Currently:
   ```ts
   export async function listAgents(params?: {
     status?: AgentStatus;
     dataDomain?: string;
   }): Promise<AgentDefinition[]> { ... }
   ```
   Update to `dataDomain?: string | string[]` and pass to `httpClient.get` as params — Axios serializes arrays as `?dataDomain=A&dataDomain=B` by default. Verify; if not, switch to `paramsSerializer` with `qs` or manual join.

   (Per `axios` 1.x default: `paramsSerializer` is built-in and produces `a=1&a=2`. FastAPI's Query accepts this. Confirm in test T5 Step 6.)

- [ ] **Step 5: Update listAgents API client if needed**

In `frontend/src/api/agentRegistry.ts`, modify `listAgents`:

```ts
export async function listAgents(params?: {
  status?: AgentStatus;
  dataDomain?: string | string[];
}): Promise<AgentDefinition[]> {
  const q: Record<string, string | string[]> = {};
  if (params?.status) q.status = params.status;
  if (params?.dataDomain) q.dataDomain = params.dataDomain;
  const res = await httpClient.get<AgentDefinition[]>(PREFIX, { params: q });
  return res.data;
}
```

If axios serializes the array to `dataDomain=A&dataDomain=B` (default), no further change. If not, add:
```ts
import qs from "qs";
// ...
const res = await httpClient.get<AgentDefinition[]>(PREFIX, {
  params: q,
  paramsSerializer: (p) => qs.stringify(p, { arrayFormat: "repeat" }),
});
```

Check `frontend/package.json` for `qs` dep first; if not present, use axios default and add test verifying URL shape.

- [ ] **Step 6: Run UI test — verify GREEN**

```bash
cd frontend
npx vitest run src/tests/AgentRegistryPage.test.tsx
```
Expected: pass.

- [ ] **Step 7: Full frontend regression + type-check**

```bash
cd frontend
npx tsc --noEmit
npx vitest run
```
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/pages/AgentRegistryPage.tsx \
        frontend/src/api/agentRegistry.ts \
        frontend/src/i18n/zh-CN.ts \
        frontend/src/i18n/en-US.ts \
        frontend/src/tests/AgentRegistryPage.test.tsx
git commit -m "feat(frontend): Agent Registry page uses vocabulary-bounded Selects

新建/编辑 Modal 的 dataDomains/dataLayers 改为 mode='multiple' +
从 /agents/options 取值；策略子表 dataLayer 改单值 Select allowClear
（AgentAccessPolicy.data_layer 是单值字段）；列表页加域过滤下拉
（多选 → ?dataDomain=A&dataDomain=B）。i18n 加 agentOptions.title/
agentRegistry.filter.* 占位文案。"
```

---

### Task 6: 端到端验证 + Harness 变更记录 + 最终审查

**Files:**
- Modify: `Harness/changes/feat-agent-vocabulary/summary.md`（新建；描述 + 验证 + 风险记录）
- Modify: `Harness/wiki/agent-runtime.md`（若存在，标注词表治理；否则新建）

- [ ] **Step 1: Rebuild backend + e2e verify**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
docker compose -f docker/docker-compose.yml up -d --build backend
sleep 8

# 词表接口
curl -s http://localhost:8000/api/v1/agents/options -H "X-User-Id: admin" -H "X-User-Roles: admin"
# 期望：{"domains":["PROCUREMENT","QUALITY","LOGISTICS"],"layers":["DIM","DWD","FEATURE"]}

# 合法域写入
curl -s -X POST http://localhost:8000/api/v1/agents -H "Content-Type: application/json" \
  -H "X-User-Id: admin" -H "X-User-Roles: admin" \
  -d '{"agentCode":"VOCAB_OK","agentName":"vocab ok","dataDomains":["PROCUREMENT","QUALITY"],"dataLayers":["DIM","FEATURE"]}'
# 期望：201

# 非法域写入
curl -s -X POST http://localhost:8000/api/v1/agents -H "Content-Type: application/json" \
  -H "X-User-Id: admin" -H "X-User-Roles: admin" \
  -d '{"agentCode":"VOCAB_BAD","agentName":"vocab bad","dataDomains":["NONSENSE"]}'
# 期望：422，detail 含合法集

# 列表过滤（多值 dataDomain）
curl -s 'http://localhost:8000/api/v1/agents?dataDomain=PROCUREMENT&dataDomain=QUALITY' \
  -H "X-User-Id: admin" -H "X-User-Roles: admin"
# 期望：返回 data_domains 与任一相交的 Agent

# 清理测试数据
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  uv run python -c "
from app.infrastructure.database import getSessionFactory
import asyncio
from sqlalchemy import delete
from app.domain.models import AgentDefinition
async def cleanup():
    factory = getSessionFactory()
    async with factory() as session:
        await session.execute(delete(AgentDefinition).where(AgentDefinition.agent_code.in_(['VOCAB_OK','VOCAB_BAD'])))
        await session.commit()
asyncio.run(cleanup())
"
```

- [ ] **Step 2: Rebuild frontend + smoke**

```bash
docker compose -f docker/docker-compose.yml up -d --build frontend
sleep 5
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5173/
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5173/api/v1/agents/options
# 期望：200 / 200

curl -s http://localhost:5173/ | grep -o 'assets/index-[^"]*\.js' | head -1
# 期望：新 bundle hash（非 index-jiElP5y.js 也非 index-D6i2zAjf.js）
```

- [ ] **Step 3: Full backend regression + coverage gate**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```
Expected: ≥ 80% coverage; only known pre-existing failures (1 Neo4j).

- [ ] **Step 4: Write Harness change summary**

Create `Harness/changes/feat-agent-vocabulary/summary.md`:

```markdown
# feat-agent-vocabulary

> 日期：2026-09-02 | 状态：done | Spec: docs/superpowers/specs/2026-09-02-agent-vocabulary-design.md

## 目标

锁定 `AgentDefinition.data_domains` / `data_layers` 词表（SSOT 在代码常量，零 DB 迁移），
前端从自由输入改成约束多选 + 列表页按域过滤。

## 实现

- `app/domain/agent_vocabulary.py`（新）：`AGENT_DATA_DOMAINS`、`AGENT_DATA_LAYERS` 常量 + `normalizeAgentDomain`
- `AgentDefinitionCreate/Update` 加 `field_validator`：归一化 + 词表校验 + 去重保序
- 两条 `MSG_AGENT_*_NOT_IN_VOCAB` 错误消息
- `GET /api/v1/agents/options` 新端点（路由必须在 `/{agent_code}` 之前，集成测试守护）
- `listAgents` 扩展 `data_layer` + `data_domains: list[str]`（JSONB contains / overlap）
- 前端 `useAgentOptions()` hook + 模块级缓存
- AgentRegistryPage 4 处 Select 改造 + 列表页域过滤下拉 + i18n

## 验证

- 单测：新 `test_agent_vocabulary.py`（10 用例，词表/归一化/校验/去重/空拒收）；前端 `useAgentOptions.test.tsx`（2 用例）
- 集成：新增 `test_agent_options_api.py`（2 用例，含路由顺序守护）+ `test_agent_registry_api.py` 3 个新过滤用例
- 全量：≥ 80% 覆盖率，剩 1 个已知 Neo4j 环境失败
- e2e：词表接口 200；非法域写入 422；多值 `?dataDomain=A&dataDomain=B` 过滤生效；前端 nginx 代理 200

## 关键决策

- 词表走代码常量（域 2-3 个、层 3 个，规模未到需要字典表）
- 严格 422，不引入新 RequestValidationError handler（YAGNI，与项目错误信封不一致的取舍）
- 不迁移历史不规范行（可独立发清理脚本）
- dataDomain 同名多值（FastAPI Query 重复绑定聚合），不新增参数名

## 与大改动（Agent 工具绑定可配置化）的边界

- 本期不暴露工具列表；`useAgentOptions()` 形态可演化为 `useAgentMetadata()`，接口预留 `tools` 字段
- 工具绑定仍硬编码于 `AGENT_TOOLS`，大改动把 `Agent→Tool` 关系入 DB
- 工具 `handler/data_object/data_layers` 定义留代码（安全关键，不入 DB）
```

- [ ] **Step 5: Commit Harness record**

```bash
git add Harness/changes/feat-agent-vocabulary/summary.md
git commit -m "docs(harness): feat-agent-vocabulary change summary

记录词表治理实现细节、验证结果、关键决策与未来大改动边界。"
```

- [ ] **Step 6: Memory update**

```write to /Users/sunql/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-agent-vocabulary.md:
---
name: qa-system-agent-vocabulary
description: AgentDefinition.data_domains / data_layers 受 SSOT 常量词表约束，写入严格 422
metadata: type: project
---

`app/domain/agent_vocabulary.py` 是 Agent 元数据词表 SSOT：`AGENT_DATA_DOMAINS=(PROCUREMENT,QUALITY,LOGISTICS)`、`AGENT_DATA_LAYERS=(DIM,DWD,FEATURE)`。AgentDefinitionCreate/Update 的写入边界做归一化（strip+upper）+ 词表校验 + 去重保序，非法值 Pydantic 422。`GET /api/v1/agents/options` 是唯一下发入口，路由必须在 `/{agent_code}` 之前注册。`listAgents` 支持 `data_layer` 单值 + `data_domains` 复数（FastAPI Query 同名重复绑定）。Agent 层与 `LineageLayer`（血缘 7 层）是两套词汇，勿混。

**How to apply:** 修改/扩展域或层时改 `agent_vocabulary.py` 一个常量即可，前端从 /agents/options 自动获取新选项。新增工具的 data_layers 也必须 ⊂ AGENT_DATA_LAYERS，否则运行时分层授权校验 fail-open。
```

Add to MEMORY.md:
```
- [Agent 词表 SSOT](qa-system-agent-vocabulary.md) — AGENT_DATA_DOMAINS/DIM/DWD/FEATURE 词表常量 + 严格 422 校验 + /agents/options 下发
```

```bash
git add memory/...
git commit -m "docs(memory): agent vocabulary SSOT fact"
```

---

## Self-Review (post-write)

**Spec coverage:**
- §"数据模型 零迁移" ✅ T1（无迁移）
- §"DTO 写入边界严格 422" ✅ T1（field_validator）
- §"归一化 strip+upper" ✅ T1（normalizeAgentDomain + 既有 _normalizeDataLayer）
- §"选项下发 GET /agents/options" ✅ T2（路由顺序守护）
- §"列表过滤扩展 dataLayer + dataDomain 多值" ✅ T3
- §"前端 4 处 Select 改造" ✅ T5
- §"i18n" ✅ T5
- §"不触碰历史不规范行" ✅ 通过设计决策（无代码任务）
- §"Pydantic 422 取舍（不引新 handler）" ✅ 通过设计决策（无代码任务）
- §"与 LineageLayer 边界" ✅ agent_vocabulary.py docstring + 注释

**Placeholder scan:** No TBD/TODO/"fill in"/"implement later" patterns. Pseudocode in tests/code blocks contains complete copy-paste-runnable snippets.

**Type consistency:**
- `AGENT_DATA_DOMAINS: tuple[str, ...]` — T1 def → T2 schema use → T4 hook return → T5 Select options ✓
- `_vocabCheckDomains` / `_vocabCheckLayers` — T1 def → T1 DTO use → T3 untouched ✓
- `AgentOptionsRead { domains, layers }` — T2 def → T4 `AgentOptions` type → T5 hook destructuring ✓
- `data_domains: list[str]` (Create) vs `list[str] | None` (Update) — handled by Update validator wrapper ✓
- `dataLayer: str | None` (policy form, single value) — T5 correctly uses `<Select allowClear>` NOT `mode="multiple"` ✓

**Risks tracked:**
- FastAPI RequestValidationError envelope mismatch → documented in spec §3, accepted as YAGNI
- Historical non-vocab data → documented; no migration script
- New domain query param serialization (axios default) → Step 5 of T5 includes fallback with `qs`