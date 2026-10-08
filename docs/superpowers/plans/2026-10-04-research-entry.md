# 研究型 Agent 入口（feat-research-entry）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增与 chat 完全并列的「研究型 Agent 入口」：ESL 三臂拆解 → 3 固定 + 4 动态 checkpoint 的 turn 状态机 → 假设验证 → 三 mode 报告归档 → 独立 SSE + 前端 4 组件。

**Architecture:** 新服务族（`ResearchAgentService` / `EnterpriseSemanticLayer` / `ReportPlanner` / `ResearchSessionService`）+ 5 张新表，只经**公开方法**复用现有服务（IntentService / OntologyService / KpiSemanticMatchService / WikiVectorService / StepQueryPlanner / Nl2SqlService 门面 / ChartService / hypothesis_service 模块级函数）。chat 域调用栈与表**零改动**。

**Tech Stack:** FastAPI + SQLAlchemy(async) + PostgreSQL(qa-pg-a1) + Alembic + pytest(asyncio) + React 18 + zustand + vitest/RTL + antd。

**设计文档（SSOT）：** `docs/superpowers/specs/2026-10-04-research-entry-design.md`；摘要：`Harness/changes/feat-research-entry/summary.md`

**对设计文档的一处修正（已确认事实）：** 设计 §4.3「复用 `_executeDataStep`」不可行——它是 `ChatService` mixin 私有方法（`chat_multistep.py:270`）。改为新建薄执行 runner（Task 4）：`StepQueryPlanner.plan()` + `Nl2SqlService.generateValidatedPlan()/generateSql()` + SQL Guard + `business_db_pool`。边界更严，功能等价。

## Global Constraints

- 测试用真实 PostgreSQL：qa-pg-a1，宿主机连接串必须用 `localhost:5434`，库名 `qa_metadata_test`，**串行，绝不并行跑两个套件**
- 测试必须写在 `backend/app/tests/`（unit / services / integration 三层，沿用现有 conftest fixtures：`client` / `dbSession` / `pg_engine` / `db_session` / `mockLlmClient`）
- **绝不手工跑 `alembic upgrade head`**（迁移文件照常写，应用由容器/CI 启动完成；本地验证靠测试容器）
- TDD RED→GREEN；新代码覆盖率 ≥ 80%
- 函数 < 50 行；文件 < 800 行；camelCase（Python 函数/变量）；**ORM/Pydantic 字段 snake_case**（项目刻意约定）；常量 UPPER_SNAKE_CASE
- Pydantic schema 基类 CamelModel 已 `extra=forbid`
- 禁硬编码密钥；admin/Admin@123 仅本地 QA 凭据
- 不可变数据：领域对象一律 frozen dataclass，禁止原地修改
- 改前端必须 `docker compose build --no-cache frontend`（npm run build ≠ 容器 bundle）；禁裸 `docker build`（tag 差 `system-` 前缀）
- 禁裸 `git stash/pop`
- 错误显式处理：每层 try/except 具体化，UI 层友好提示，服务端 log 详细上下文
- 现有 chat 域（`chat_*` / `analysis_hypothesis` / `evidence` 表 + ChatService 调用栈）**零改动**
- L4/集成测试需 `TEST_NEO4J_URI=bolt://localhost:7688`（本特性不涉 Neo4j，仅全量回归时）

---

### Task 1: ORM 模型 + Alembic 迁移 0111（5 张表）

**Files:**
- Create: `backend/app/domain/research_models.py`
- Create: `backend/alembic/versions/0111_research_entry_tables.py`
- Test: `backend/app/tests/unit/test_research_models.py`

**Interfaces:**
- Produces（后续所有任务依赖）: ORM 类 `ResearchSession` / `ResearchTurn` / `ResearchCheckpoint` / `ResearchFinding` / `ResearchReport`，注册进 `Base.metadata`
- 偏差说明：`created_by` 为普通 `BigInteger`（不加 FK）——用户表名跨域耦合无收益，设计文档 FK 条款按此放宽

- [ ] **Step 1: 写失败测试**

```python
"""research_models 元数据守卫：5 张表 + 关键列必须存在（unit，不连库）。"""
from sqlalchemy.dialects import postgresql

from app.domain.research_models import (
    ResearchCheckpoint,
    ResearchFinding,
    ResearchReport,
    ResearchSession,
    ResearchTurn,
)


def test_five_tables_registered() -> None:
    assert ResearchSession.__tablename__ == "research_session"
    assert ResearchTurn.__tablename__ == "research_turn"
    assert ResearchCheckpoint.__tablename__ == "research_checkpoint"
    assert ResearchFinding.__tablename__ == "research_finding"
    assert ResearchReport.__tablename__ == "research_report"


def test_session_columns() -> None:
    cols = {c.name for c in ResearchSession.__table__.columns}
    assert {"id", "title", "mode", "status", "created_by", "input_seed", "updated_at"} <= cols


def test_report_partial_unique_index() -> None:
    idx = {i.name for i in ResearchReport.__table__.indexes}
    assert "uq_research_report_session_published" in idx
    target = next(i for i in ResearchReport.__table__.indexes
                  if i.name == "uq_research_report_session_published")
    assert target.dialect_options["postgresql"]["where"] is not None


def test_turn_content_is_jsonb() -> None:
    col = ResearchTurn.__table__.columns["content"]
    assert isinstance(col.type, postgresql.JSONB)
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `cd backend && python -m pytest app/tests/unit/test_research_models.py -v`
Expected: FAIL `ModuleNotFoundError: app.domain.research_models`

- [ ] **Step 3: 写模型**

```python
"""研究型入口 5 张表（feat-research-entry）。

与 chat 域完全隔离：不触碰 chat_session / session_message /
analysis_hypothesis / evidence。created_by 不加 FK（跨域不耦合）。
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger, DateTime, Index, Integer, Numeric, String, Text,
    text, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base

_STATUS_LEN = 20
_MODE_LEN = 20
_PHASE_LEN = 30


class ResearchSession(Base):
    __tablename__ = "research_session"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text, default="", server_default="")
    mode: Mapped[str] = mapped_column(String(_MODE_LEN), default="research", server_default="research")
    status: Mapped[str] = mapped_column(String(_STATUS_LEN), default="running", server_default="running")
    createdBy: Mapped[int | None] = mapped_column("created_by", BigInteger, nullable=True)
    inputSeed: Mapped[str] = mapped_column("input_seed", Text, default="", server_default="")
    createdAt: Mapped[object] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now())
    updatedAt: Mapped[object] = mapped_column(
        "updated_at", DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ResearchTurn(Base):
    __tablename__ = "research_turn"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sessionId: Mapped[uuid.UUID] = mapped_column(
        "session_id", UUID(as_uuid=True),
        ForeignKey := __import__("sqlalchemy").ForeignKey("research_session.id", ondelete="CASCADE"),
        nullable=False, index=True)
    turnIndex: Mapped[int] = mapped_column("turn_index", Integer, nullable=False, default=0)
    role: Mapped[str] = mapped_column(String(_STATUS_LEN), nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    createdAt: Mapped[object] = mapped_column(
        "created_at", DateTime(timezone=True), server_default=func.now())
```

> 注意：上面 `ForeignKey :=` 的写法**不许照抄**——在文件头部正常 `from sqlalchemy import ForeignKey` 后直接用。此处只是提示需要 FK；`ResearchCheckpoint` / `ResearchFinding` / `ResearchReport` 按设计文档 §3 字段清单同型补齐：checkpoint 含 `turn_id FK` / `phase` / `status` / `options JSONB` / `user_choice JSONB` / `decided_at`；finding 含 `turn_id FK` / `claim_text` / `supporting_sql` / `supporting_data JSONB` / `confidence Numeric(5,4)`；report 含 `session_id FK` / `version Integer` / `payload JSONB` / `rendered_md Text` / `status` + 索引：
>
> ```python
> __table_args__ = (
>     Index(
>         "uq_research_report_session_published", "session_id",
>         unique=True, postgresql_where=text("status = 'published'"),
>     ),
> )
> ```

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `cd backend && python -m pytest app/tests/unit/test_research_models.py -v`
Expected: 4 PASS

- [ ] **Step 5: 写迁移文件**

先读 `backend/alembic/versions/0110_audit_log_restore_action.py` 取真实 `revision` 字符串作为 `down_revision`：

```python
"""research entry: 5 tables (feat-research-entry)"""

revision = "0111"
down_revision = "<0110 文件里读到的真实 revision id>"
# op.create_table × 5，列定义与 research_models.py 一一对应；
# 部分唯一索引：
# op.create_index(
#     "uq_research_report_session_published", "research_report", ["session_id"],
#     unique=True, postgresql_where=sa.text("status = 'published'"),
# )
# downgrade: op.drop_index(...) + op.drop_table(...) 倒序 5 表
```

- [ ] **Step 6: 迁移在测试库自动验证（不手工 upgrade）**

Run: `cd backend && python -m pytest app/tests/integration/test_research_session_service.py -v`（Task 3 的测试文件先建空壳跳过即可，或直接进入 Task 3 后回来跑）
Expected: 测试容器启动时 alembic 自动应用 0111，无报错

- [ ] **Step 7: Commit**

```bash
git add backend/app/domain/research_models.py backend/alembic/versions/0111_research_entry_tables.py backend/app/tests/unit/test_research_models.py
git commit -m "feat(research): 5 张研究会话表 + 迁移 0111"
```

---

### Task 2: ESL 数据类 + EnterpriseSemanticLayer（三臂，不调 LLM）

**Files:**
- Create: `backend/app/services/enterprise_semantic_layer.py`
- Test: `backend/app/tests/unit/test_enterprise_semantic_layer.py`

**Interfaces:**
- Produces: `ESLExtraction` / `BusinessObjectRef` / `MetricRef` / `KnowledgeRef` / `ESLConflict`（frozen dataclass，字段与设计 §4.6 完全一致）；`class EnterpriseSemanticLayer` 构造注入三个**协程依赖**，`async def extract(question: str, *, intent: object | None = None) -> ESLExtraction`；异常 `EmptyResearchScopeError(ValueError)`

- [ ] **Step 1: 写失败测试（fake 注入，不连任何外部系统）**

```python
"""ESL 三臂 + 冲突/降级语义（纯 unit，fake 检索器注入）。"""
import pytest

from app.services.enterprise_semantic_layer import (
    EnterpriseSemanticLayer, EmptyResearchScopeError,
)


def _layer(*, bo=None, kpi=None, wiki=None) -> EnterpriseSemanticLayer:
    async def boSearcher(q, topK=5):
        return bo or [{"classId": 1, "className": "供应商", "sourceTable": "DIM_SUPPLIER",
                       "matchedAlias": "供应商", "confidence": 0.9}]
    async def kpiMatcher(q, topK=5):
        return kpi or [{"metricId": 7, "kpiCode": "KPI_RCPT", "displayName": "收货量",
                        "formula": "COUNT(RCV_LINE_NO)", "confidence": 0.8}]
    async def wikiSearcher(q, topK=5):
        return wiki or []
    return EnterpriseSemanticLayer(boSearcher=boSearcher, kpiMatcher=kpiMatcher, wikiSearcher=wikiSearcher)


@pytest.mark.asyncio
async def test_three_arms_populated() -> None:
    result = await _layer().extract("供应商收货量为什么下降")
    assert result.business_objects[0].sourceTable == "DIM_SUPPLIER"
    assert result.metrics[0].kpiCode == "KPI_RCPT"
    assert 0.0 <= result.confidenceByArm["business_object"] <= 1.0


@pytest.mark.asyncio
async def test_metric_ambiguous_conflict_when_close_scores() -> None:
    kpi = [
        {"metricId": 1, "kpiCode": "A", "displayName": "收货量", "formula": None, "confidence": 0.80},
        {"metricId": 2, "kpiCode": "B", "displayName": "发货量", "formula": None, "confidence": 0.75},
    ]
    result = await _layer(kpi=kpi).extract("量")
    kinds = [c.kind for c in result.conflicts]
    assert "metric_ambiguous" in kinds


@pytest.mark.asyncio
async def test_all_empty_raises() -> None:
    with pytest.raises(EmptyResearchScopeError):
        await _layer(bo=[], kpi=[], wiki=[]).extract("乱码问题")


@pytest.mark.asyncio
async def test_wiki_timeout_degrades_to_empty_arm() -> None:
    async def wikiFail(q, topK=5):
        raise TimeoutError("wiki down")
    layer = _layer(wiki=None)
    # 构造后替换 wiki 依赖为抛超时的 fake
    layer._wikiSearcher = wikiFail
    result = await layer.extract("供应商收货量")
    assert result.knowledge == []
    assert result.confidenceByArm["knowledge"] == 0.0
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `cd backend && python -m pytest app/tests/unit/test_enterprise_semantic_layer.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

```python
"""Enterprise Semantic Layer — 三臂语义拆解（feat-research-entry）。

硬约束（设计 §4.6）：
1. 不调 LLM（置信度信号来自检索器原始分数）；
2. 不写库（只读 ontology / wiki 域）；
3. 无状态（构造注入依赖，extract 纯函数式）。
后续优化钩子：LLM 二次精化层放在 ESL 之外（Checkpoint #1 之前），不进本模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import logging

logger = logging.getLogger(__name__)

_METRIC_AMBIGUOUS_GAP = 0.1   # 双源候选分差 < 0.1 ⇒ 歧义冲突
_WIKI_DISAGREE_SCORE = 0.5    # ≥2 篇 score ≥ 0.5 视为共主题
_BO_TOP_K = 5
_METRIC_TOP_K = 5
_WIKI_TOP_K = 5


@dataclass(frozen=True)
class BusinessObjectRef:
    classId: int
    className: str
    sourceTable: str
    matchedAlias: str
    confidence: float


@dataclass(frozen=True)
class MetricRef:
    metricId: int | None
    kpiCode: str | None
    displayName: str
    formula: str | None
    confidence: float


@dataclass(frozen=True)
class KnowledgeRef:
    pageId: int | None
    title: str
    snippet: str
    semanticScore: float


@dataclass(frozen=True)
class ESLConflict:
    kind: str      # metric_ambiguous | wiki_disagree | bo_join_missing
    arm: str       # business_object | metric | knowledge
    detail: str
    candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ESLExtraction:
    businessObjects: list[BusinessObjectRef]
    metrics: list[MetricRef]
    knowledge: list[KnowledgeRef]
    confidenceByArm: dict[str, float]
    conflicts: list[ESLConflict]


class EmptyResearchScopeError(ValueError):
    """三臂全空：转 Checkpoint #1 强制用户改写问题。"""


BoSearcher = Callable[..., Awaitable[list[dict[str, Any]]]]
KpiMatcher = Callable[..., Awaitable[list[dict[str, Any]]]]
WikiSearcher = Callable[..., Awaitable[list[dict[str, Any]]]]


class EnterpriseSemanticLayer:
    """三臂编排器；依赖全部构造注入，便于 fake 测试。"""

    def __init__(self, *, boSearcher: BoSearcher, kpiMatcher: KpiMatcher,
                 wikiSearcher: WikiSearcher) -> None:
        self._boSearcher = boSearcher
        self._kpiMatcher = kpiMatcher
        self._wikiSearcher = wikiSearcher

    async def extract(self, question: str, *, intent: object | None = None) -> ESLExtraction:
        bos = await self._extractArm("business_object", self._boSearcher, question,
                                     self._toBoRef)
        metrics = await self._extractArm("metric", self._kpiMatcher, question,
                                         self._toMetricRef)
        knowledge = await self._extractArm("knowledge", self._wikiSearcher, question,
                                           self._toKnowledgeRef)
        if not bos and not metrics and not knowledge:
            raise EmptyResearchScopeError("三臂检索全空，需要用户改写问题")
        conflicts = self._detectConflicts(bos, metrics, knowledge)
        return ESLExtraction(
            businessObjects=bos, metrics=metrics, knowledge=knowledge,
            confidenceByArm={
                "business_object": bos[0].confidence if bos else 0.0,
                "metric": metrics[0].confidence if metrics else 0.0,
                "knowledge": knowledge[0].semanticScore if knowledge else 0.0,
            },
            conflicts=conflicts,
        )

    async def _extractArm(self, arm: str, searcher: Callable[..., Awaitable[list]],
                          question: str, toRef: Callable[[dict], Any]) -> list:
        try:
            raw = await searcher(question, topK=_TOP_K[arm])
        except Exception:
            logger.warning("ESL %s 臂检索失败，降级为空臂", arm, exc_info=True)
            return []
        return [toRef(item) for item in raw]

    def _detectConflicts(self, bos, metrics, knowledge) -> list[ESLConflict]:
        conflicts: list[ESLConflict] = []
        if len(metrics) >= 2 and (metrics[0].confidence - metrics[1].confidence) < _METRIC_AMBIGUOUS_GAP:
            conflicts.append(ESLConflict(
                kind="metric_ambiguous", arm="metric",
                detail=f"前两名 metric 分差 < {_METRIC_AMBIGUOUS_GAP}",
                candidates=[{"kpiCode": m.kpiCode, "displayName": m.displayName,
                             "confidence": m.confidence} for m in metrics[:2]]))
        strong = [k for k in knowledge if k.semanticScore >= _WIKI_DISAGREE_SCORE]
        if len(strong) >= 2:
            conflicts.append(ESLConflict(
                kind="wiki_disagree", arm="knowledge",
                detail=f"{len(strong)} 篇高相关 wiki 主题重叠，表述可能冲突",
                candidates=[{"pageId": k.pageId, "title": k.title} for k in strong]))
        return conflicts

    def _toBoRef(self, item: dict) -> BusinessObjectRef:
        return BusinessObjectRef(
            classId=int(item["classId"]), className=item["className"],
            sourceTable=item.get("sourceTable") or "",
            matchedAlias=item.get("matchedAlias") or "",
            confidence=float(item.get("confidence") or 0.0))

    def _toMetricRef(self, item: dict) -> MetricRef:
        return MetricRef(
            metricId=item.get("metricId"), kpiCode=item.get("kpiCode"),
            displayName=item.get("displayName") or "",
            formula=item.get("formula"), confidence=float(item.get("confidence") or 0.0))

    def _toKnowledgeRef(self, item: dict) -> KnowledgeRef:
        return KnowledgeRef(
            pageId=item.get("pageId"), title=item.get("title") or "",
            snippet=(item.get("snippet") or "")[:280],
            semanticScore=float(item.get("score") or 0.0))


_TOP_K = {"business_object": _BO_TOP_K, "metric": _METRIC_TOP_K, "knowledge": _WIKI_TOP_K}
```

> 实现者注意：`_TOP_K` 放文件底部是为可读性，实际请放到常量区（文件顶部 `_WIKI_DISAGREE_SCORE` 旁边）。字段名 camelCase（领域 dataclass），与 ORM snake_case 约定不冲突（dataclass ≠ ORM/Pydantic）。

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `cd backend && python -m pytest app/tests/unit/test_enterprise_semantic_layer.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/enterprise_semantic_layer.py backend/app/tests/unit/test_enterprise_semantic_layer.py
git commit -m "feat(research): ESL 三臂语义拆解（无 LLM / 无写库 / 无状态）"
```

---

### Task 3: ResearchSessionService（会话/turn/checkpoint/finding/report 持久化）

**Files:**
- Create: `backend/app/services/research_session_service.py`
- Test: `backend/app/tests/integration/test_research_session_service.py`（真实 PG）

**Interfaces:**
- Consumes: Task 1 的 5 个 ORM 类
- Produces: `class ResearchSessionService`（无状态，方法全部首参 `session: AsyncSession`）：
  - `async def createSession(session, *, userId: int | None, question: str, mode: str = "research") -> ResearchSession`
  - `async def appendTurn(session, *, sessionId: uuid.UUID, role: str, content: dict) -> ResearchTurn`
  - `async def openCheckpoint(session, *, sessionId, turnId, phase: str, options: dict, prompt: str) -> ResearchCheckpoint`
  - `async def resolveCheckpoint(session, *, checkpointId: uuid.UUID, status: str, userChoice: dict) -> ResearchCheckpoint`（非 pending 抛 `ValueError`）
  - `async def getPendingCheckpoint(session, sessionId) -> ResearchCheckpoint | None`
  - `async def saveFinding(session, *, sessionId, turnId, claimText: str, supportingSql: str | None, supportingData: dict, confidence: float) -> ResearchFinding`
  - `async def publishReport(session, *, sessionId, payload: dict, renderedMd: str) -> ResearchReport`（旧 published → superseded；version = max+1）
  - `async def listReports(session, sessionId) -> list[ResearchReport]`
  - `async def updateSessionStatus(session, sessionId, status: str) -> None`

- [ ] **Step 0: 确认 integration fixture 可用**

Run: `cd backend && grep -n "async def dbSession" app/tests/integration/conftest.py`
Expected: `60: async def dbSession(...)`（已核实存在，无需改动；此步只是确认环境在位）

- [ ] **Step 1: 写失败测试（真实 PG，qa_metadata_test）**

```python
"""research_session_service 持久化链路（真实 PostgreSQL，串行）。"""
import uuid

import pytest

from app.services.research_session_service import ResearchSessionService


@pytest.mark.asyncio
async def test_create_session_and_first_turn(dbSession) -> None:
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="供应商收货量为什么下降")
    assert s.status == "running" and s.mode == "research" and s.inputSeed
    turn = await svc.appendTurn(dbSession, sessionId=s.id, role="user",
                                content={"question": "供应商收货量为什么下降"})
    assert turn.turnIndex == 0 and turn.role == "user"


@pytest.mark.asyncio
async def test_checkpoint_open_resolve_and_pending_lookup(dbSession) -> None:
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=s.id, role="agent", content={})
    cp = await svc.openCheckpoint(dbSession, sessionId=s.id, turnId=turn.id,
                                  phase="intent", options={"arms": ["bo", "metric"]},
                                  prompt="三臂是否齐全？")
    assert cp.status == "pending"
    pending = await svc.getPendingCheckpoint(dbSession, s.id)
    assert pending is not None and pending.id == cp.id
    resolved = await svc.resolveCheckpoint(dbSession, checkpointId=cp.id,
                                           status="confirmed", userChoice={"action": "confirm"})
    assert resolved.status == "confirmed" and resolved.decidedAt is not None
    assert await svc.getPendingCheckpoint(dbSession, s.id) is None
    with pytest.raises(ValueError):
        await svc.resolveCheckpoint(dbSession, checkpointId=cp.id,
                                    status="confirmed", userChoice={})


@pytest.mark.asyncio
async def test_publish_report_versioning(dbSession) -> None:
    svc = ResearchSessionService()
    s = await svc.createSession(dbSession, userId=1, question="q")
    r1 = await svc.publishReport(dbSession, sessionId=s.id, payload={"v": 1}, renderedMd="# v1")
    r2 = await svc.publishReport(dbSession, sessionId=s.id, payload={"v": 2}, renderedMd="# v2")
    assert (r1.version, r2.version) == (1, 2)
    assert r1.status == "superseded" and r2.status == "published"
    reports = await svc.listReports(dbSession, s.id)
    assert {r.status for r in reports} == {"superseded", "published"}
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `cd backend && python -m pytest app/tests/integration/test_research_session_service.py -v`
Expected: FAIL `ModuleNotFoundError`（或表不存在 → Task 1 迁移未应用即先修迁移）

- [ ] **Step 3: 写实现（每个方法 < 10 行，文件 < 300 行）**

按 Interfaces 清单逐方法实现；要点：
- `appendTurn` 的 `turnIndex = SELECT max(turn_index)+1`（并发低，研究会话单人串行，无需锁）
- `resolveCheckpoint` 先查 pending，非 pending 显式抛 `ValueError`（幂等保护）
- `publishReport` 单事务内：`UPDATE ... SET status='superseded' WHERE session_id=? AND status='published'` → `SELECT coalesce(max(version),0)+1` → INSERT
- 所有写操作由调用方（FastAPI 依赖注入的 session）统一 commit，service 不自行 commit（与现有 service 一致；实现前 grep 一个现有 service 确认 commit 归属，例如 `grep -n "commit" app/services/ontology_service.py | head -3`）

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `cd backend && python -m pytest app/tests/integration/test_research_session_service.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/research_session_service.py backend/app/tests/integration/test_research_session_service.py
git commit -m "feat(research): 会话/轮次/checkpoint/finding/report 持久化服务"
```

---

### Task 4: 假设适配层 + 薄执行 runner（公开面复用，不穿透 chat 私有）

**Files:**
- Create: `backend/app/services/research_hypothesis_adapter.py`
- Create: `backend/app/services/research_sql_runner.py`
- Test: `backend/app/tests/unit/test_research_hypothesis_adapter.py`
- Test: `backend/app/tests/integration/test_research_sql_runner.py`

**Interfaces:**
- Consumes（已核实的现有签名）:
  - `hypothesis_service.buildHypothesisMessages(question: str, dataSummary: str, drivers: list[str]) -> list[LlmMessage]`（`hypothesis_service.py:193`）
  - `hypothesis_service.parseHypotheses(raw: str) -> list[Hypothesis]`（`:149`）
  - `hypothesis_service.isValidVerificationSql(sql: str | None) -> bool`（`:113`）
  - `Nl2SqlService.generateValidatedPlan(...)`（`nl2sql_service.py:485`）/ `generateSql(...)`（`:623`）
- Produces:
  - `async def generateHypotheses(llmClient, question: str, dataSummary: str, drivers: list[str]) -> list[Hypothesis]`（过滤 `isValidVerificationSql` 为 False 的条目）
  - `class ResearchSqlRunner`：`async def executeReadonlySql(session: AsyncSession, sql: str) -> list[dict]`（SQL Guard 校验 + `business_db_pool` 执行，只读）；`async def runVerification(session, hypothesis) -> dict`（返回 `{"rows": [...], "error": None | str}`，失败不抛——finding 允许「验证失败」结论）

- [ ] **Step 0: 核对两个执行面签名（各一条 grep，结果写进实现注释）**

```bash
cd backend && grep -nE "class |def " app/infrastructure/business_db_pool.py | head -8
cd backend && grep -rnE "def (validate|check|guard)" app/services/sql_guard*.py app/infrastructure/sql_guard*.py 2>/dev/null | head -5
```

若 SQL Guard 模块名与预期不同，以 grep 实际结果为准接入（`_runQueryWithRetry` 的 chat 内部实现**不参考**——那是私有路径）。

- [ ] **Step 1: 写失败测试**

```python
"""假设适配层：LLM 输出 → 合法假设列表（fake llmClient）。"""
import pytest

from app.services.research_hypothesis_adapter import generateHypotheses


class _FakeLlm:
    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.calls: list[list] = []

    async def complete(self, messages, **kwargs):  # 与 BaseLlmClient 契约对齐
        self.calls.append(messages)
        return self._raw


@pytest.mark.asyncio
async def test_parses_and_filters_illegal_sql() -> None:
    raw = ('```json\n[{"statement": "供应商A供货减少", "driver": "SUPPLIER_NAME", '
           '"verification_sql": "SELECT 1 FROM DUAL"},'
           '{"statement": "坏假设", "driver": null, '
           '"verification_sql": "DELETE FROM T"}]\n```')
    result = await generateHypotheses(_FakeLlm(raw), "为什么下降", "数据摘要", ["SUPPLIER_NAME"])
    assert len(result) == 1
    assert result[0].statement == "供应商A供货减少"
    assert result[0].verificationSql.upper().startswith("SELECT")


@pytest.mark.asyncio
async def test_garbage_output_returns_empty_not_raise() -> None:
    result = await generateHypotheses(_FakeLlm("完全不是 JSON"), "q", "s", [])
    assert result == []
```

```python
"""ResearchSqlRunner：只读执行 + 验证失败不抛（真实 PG）。"""
import pytest

from app.services.research_sql_runner import ResearchSqlRunner


@pytest.mark.asyncio
async def test_execute_readonly_select(dbSession) -> None:
    runner = ResearchSqlRunner()
    rows = await runner.executeReadonlySql(dbSession, "SELECT 1 AS ONE FROM (SELECT 1) T")
    assert rows and list(rows[0].values())[0] == 1


@pytest.mark.asyncio
async def test_rejects_dml(dbSession) -> None:
    runner = ResearchSqlRunner()
    with pytest.raises(ValueError):
        await runner.executeReadonlySql(dbSession, "DELETE FROM research_session")


@pytest.mark.asyncio
async def test_run_verification_returns_error_dict_not_raise(dbSession) -> None:
    from app.services.hypothesis_service import Hypothesis
    runner = ResearchSqlRunner()
    bad = Hypothesis(statement="s", driver=None, verificationSql="SELECT * FROM 不存在的表")
    outcome = await runner.runVerification(dbSession, bad)
    assert outcome["error"] is not None and outcome["rows"] == []
```

- [ ] **Step 2: 跑测试确认 RED**

Run: `cd backend && python -m pytest app/tests/unit/test_research_hypothesis_adapter.py app/tests/integration/test_research_sql_runner.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

`research_hypothesis_adapter.py` 核心（< 50 行）：

```python
async def generateHypotheses(llmClient, question: str, dataSummary: str,
                             drivers: list[str]) -> list[Hypothesis]:
    messages = buildHypothesisMessages(question, dataSummary, drivers)
    raw = await llmClient.complete(messages)
    text = stripJsonFence(getattr(raw, "content", None) or str(raw))
    hypotheses = [h for h in parseHypotheses(text) if isValidVerificationSql(h.verificationSql)]
    dropped = len(parseHypotheses(text)) - len(hypotheses)
    if dropped:
        logger.warning("假设适配层过滤 %d 条非法 verification_sql", dropped)
    return hypotheses
```

`research_sql_runner.py` 核心：`executeReadonlySql` 先过 SQL Guard（Step 0 grep 到的真实入口），再经 `business_db_pool` 取连接执行、`dict(row)` 序列化；`runVerification` 把 `_HYPOTHESIS_SQL_FORBIDDEN_RE` 同级防御交给 SQL Guard 后 try/except 全部异常 → `{"rows": [], "error": str(exc)}`。

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `cd backend && python -m pytest app/tests/unit/test_research_hypothesis_adapter.py app/tests/integration/test_research_sql_runner.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/research_hypothesis_adapter.py backend/app/services/research_sql_runner.py backend/app/tests/unit/test_research_hypothesis_adapter.py backend/app/tests/integration/test_research_sql_runner.py
git commit -m "feat(research): 假设生成适配层 + 只读验证 runner（公开面复用）"
```

---

### Task 5: ResearchAgentService turn 状态机（12 阶段 + 3 固定 + 4 动态 checkpoint）

**Files:**
- Create: `backend/app/services/research_agent_service.py`
- Test: `backend/app/tests/integration/test_research_agent_service.py`

**Interfaces:**
- Consumes: Task 2 ESL（`extract`）、Task 3 ResearchSessionService、Task 4 两件、现有 `IntentService` / `StepQueryPlanner.plan()` / `ChartService.buildChart()`
- Produces:
  - `PHASES = ("intent", "esl", "plan", "execute", "hypothesis", "verify", "report")`（UPPER_SNAKE 常量）
  - `class ResearchAgentService`（构造注入 `esl` / `sessionService` / `planner` / `runner` / `chartService` / `llmFactory` / `autoConfirm: bool = False`——autoConfirm 仅供测试与「免确认」模式）
  - `async def startTurn(session, *, sessionId: uuid.UUID, question: str, userId: int | None, emit: Callable[[str, dict], Awaitable[None]] | None = None) -> str` 返回终态：`"awaiting_user"` 或 `"done"`
  - `async def resumeTurn(session, *, checkpointId: uuid.UUID, action: str, choice: dict, emit=None) -> str`（action ∈ confirm / modify / reject）
  - 每阶段一个私有方法 `_stageIntent` / `_stageEsl` / `_stagePlan` / `_stageExecute` / `_stageHypothesis` / `_stageVerify` / `_stageReport`，各 < 50 行；checkpoint 触发即 return，恢复点存 `research_turn.content["phase"]`
  - **Token 计量（核心约束 #3）**：构造注入 `usageRecorder`（Task 5 Step 0 `grep -nE "class |def " app/services/chat_usage.py | head` 确认现有公开方法签名后接线）；每次 LLM 调用（假设生成 / ReportPlanner 文本块）后记 `(sessionId, phase, promptTokens, completionTokens, modelName)`，落库路径与 chat 同口径
  - 动态信号：ESL `conflicts` 非空 → plan 前插 `runtime_dynamic` checkpoint；单步执行 `StepResult.error` 非空或 data 空 → `low_confidence_step` checkpoint；`metric_ambiguous` 同 ESL 冲突通道

- [ ] **Step 1: 写失败测试（fake 全部外部依赖，真实 PG 存状态）**

```python
"""研究状态机：暂停/恢复/动态 checkpoint/终态（真实 PG + fake 依赖）。"""
import uuid

import pytest

from app.services.research_agent_service import ResearchAgentService
from app.services.research_session_service import ResearchSessionService


class FakeEsl:
    def __init__(self, conflicts=()) -> None:
        self._conflicts = conflicts

    async def extract(self, question, *, intent=None):
        from app.services.enterprise_semantic_layer import (
            BusinessObjectRef, ESLExtraction, MetricRef)
        return ESLExtraction(
            businessObjects=[BusinessObjectRef(1, "供应商", "DIM_SUPPLIER", "供应商", 0.9)],
            metrics=[MetricRef(None, "K1", "收货量", None, 0.8)],
            knowledge=[], confidenceByArm={"business_object": 0.9, "metric": 0.8, "knowledge": 0.0},
            conflicts=list(self._conflicts))


class FakePlanner:
    async def plan(self, *args, **kwargs):
        from app.domain.multi_step_plan import GlobalFilters, MultiStepPlan, StepPlan
        step = StepPlan(index=1, description="收货量趋势", sub_question="近12月收货量", sql=None, chartPlan=None)
        return MultiStepPlan(steps=[step], globalFilters=GlobalFilters(filters=[]))


class FakeRunner:
    async def executeReadonlySql(self, session, sql):
        return [{"month": "2026-01", "cnt": 100}]


@pytest.fixture
def makeService(dbSession):
    def _make(**kwargs) -> ResearchAgentService:
        return ResearchAgentService(
            esl=kwargs.get("esl", FakeEsl()), sessionService=ResearchSessionService(),
            planner=kwargs.get("planner", FakePlanner()), runner=FakeRunner(),
            llmFactory=kwargs.get("llmFactory"), autoConfirm=kwargs.get("autoConfirm", False))
    return _make


@pytest.mark.asyncio
async def test_turn_pauses_at_checkpoint1(dbSession, makeService) -> None:
    svc = makeService()
    s = await svc.sessionService.createSession(dbSession, userId=1, question="供应商收货量为什么下降")
    status = await svc.startTurn(dbSession, sessionId=s.id, question="供应商收货量为什么下降", userId=1)
    assert status == "awaiting_user"
    assert (await svc.sessionService.getPendingCheckpoint(dbSession, s.id)).phase == "intent"


@pytest.mark.asyncio
async def test_esl_conflict_inserts_dynamic_checkpoint_before_plan(dbSession, makeService) -> None:
    from app.services.enterprise_semantic_layer import ESLConflict
    svc = makeService(esl=FakeEsl(conflicts=[ESLConflict(
        kind="metric_ambiguous", arm="metric", detail="歧义", candidates=[])]))
    s = await svc.sessionService.createSession(dbSession, userId=1, question="量")
    status = await svc.startTurn(dbSession, sessionId=s.id, question="量", userId=1)
    assert status == "awaiting_user"
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp.phase == "runtime_dynamic"   # 冲突先于固定 #1 之后第一个动态点


@pytest.mark.asyncio
async def test_auto_confirm_runs_to_done_and_publishes(dbSession, makeService) -> None:
    svc = makeService(autoConfirm=True)
    s = await svc.sessionService.createSession(dbSession, userId=1, question="供应商收货量为什么下降")
    status = await svc.startTurn(dbSession, sessionId=s.id, question="供应商收货量为什么下降", userId=1)
    assert status == "done"
    reports = await svc.sessionService.listReports(dbSession, s.id)
    assert reports and reports[-1].status == "published"
```

> 注：`FakeEsl/FakePlanner` 与真实 dataclass 字段如对不上，**以 Task 2/现有 `MultiStepPlan` 真实字段为准修 fake**（fake 是测试资产，不许改产品代码迁就 fake）。`StepPlan` 真实字段见 `app/domain/multi_step_plan.py`（写实现前先读）。

- [ ] **Step 2: 跑测试确认 RED**

Run: `cd backend && python -m pytest app/tests/integration/test_research_agent_service.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

骨架（每方法 < 50 行；状态恢复读 `research_turn.content["nextPhase"]`）：

```python
_PHASE_ORDER = ["intent", "esl", "plan", "execute", "hypothesis", "verify", "report"]
_FIXED_CHECKPOINT_AFTER = {"esl": "intent", "plan": "planning", "hypothesis": "hypothesis"}


class ResearchAgentService:
    def __init__(self, *, esl, sessionService, planner, runner, llmFactory=None,
                 autoConfirm: bool = False) -> None:
        self._esl = esl
        self._sessions = sessionService
        self._planner = planner
        self._runner = runner
        self._llmFactory = llmFactory
        self._autoConfirm = autoConfirm

    async def startTurn(self, session, *, sessionId, question, userId, emit=None) -> str:
        turn = await self._sessions.appendTurn(session, sessionId=sessionId,
                                               role="user", content={"question": question})
        await self._sessions.updateSessionStatus(session, sessionId, "running")
        return await self._runFrom(session, sessionId=s_id(sessionId), turnId=turn.id,
                                   phase="intent", emit=emit)

    async def _runFrom(self, session, *, sessionId, turnId, phase, emit) -> str:
        phases = _PHASE_ORDER[_PHASE_ORDER.index(phase):]
        for current in phases:
            pause = await self._dispatchStage(session, sessionId=sessionId, turnId=turnId,
                                              stage=current, emit=emit)
            if pause is not None:      # 返回 (checkpointPhase, options, prompt) ⇒ 暂停
                await self._pauseForUser(session, sessionId=sessionId, turnId=turnId,
                                         checkpointPhase=pause[0], options=pause[1], prompt=pause[2])
                return "awaiting_user"
        await self._sessions.updateSessionStatus(session, sessionId, "done")
        return "done"
```

`_dispatchStage` 返回 `tuple | None`：
- `_stageIntent` → 意图分类（复用 IntentService；无暂停）
- `_stageEsl` → `await self._esl.extract(question)`；**固定 #1**：autoConfirm 为 False 时返回 `("intent", armsPayload, "三臂是否齐全？")`
- `_stagePlan` → 冲突非空且非 autoConfirm → 返回 `("runtime_dynamic", conflictsPayload, "检测到语义歧义，如何处理？")`；否则 `planner.plan(...)`；**固定 #2** 同上返回 `("planning", planPayload, "计划是否确认？")`
- `_stageExecute` → 逐步 `runner.executeReadonlySql` + `chartService.buildChart`；步失败且非 autoConfirm → `("runtime_dynamic", stepPayload, "步骤失败，跳过还是终止？")`
- `_stageHypothesis` → `generateHypotheses(...)`（Task 4）；**固定 #3** 返回 `("hypothesis", candidatesPayload, "验证哪些假设？")`
- `_stageVerify` → 对选中假设 `runner.runVerification`；`saveFinding`（confidence 用候选分 × 验证成功与否 0.9/0.3）
- `_stageReport` → ReportPlanner（Task 6，此处先以注入 `reporter` 依赖占位，接口签名与 Task 6 一致：`async compose(session, *, sessionId, turnId, mode, llmClient=None) -> tuple[dict, str]`，fake 注入；真实实现 Task 6 完成后接线）

`resumeTurn`：读 checkpoint → `resolveCheckpoint` → 按 `checkpoint.phase` 映射回 `nextPhase`（intent→plan / planning→execute / hypothesis→verify / runtime_dynamic→按 options 里存的 `resumePhase`）→ `_runFrom`。

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `cd backend && python -m pytest app/tests/integration/test_research_agent_service.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/research_agent_service.py backend/app/tests/integration/test_research_agent_service.py
git commit -m "feat(research): turn 状态机（3 固定 + 动态 checkpoint，暂停/恢复落库）"
```

---

### Task 6: ReportPlanner（三 mode + MD 渲染 + 版本归档）

**Files:**
- Create: `backend/app/services/report_planner.py`
- Test: `backend/app/tests/unit/test_report_planner.py`

**Interfaces:**
- Consumes: Task 3 `publishReport` / findings 查询、`mockLlmClient`（conftest `:138`）
- Produces:
  - `@dataclass(frozen=True) class ReportSection / ReportBlock / SourceRef`（设计 §4.7 字段）
  - `class ReportPlanner`：构造注入 `sessionService` + `llmClientFactory`；`async def compose(session, *, sessionId: uuid.UUID, turnId: uuid.UUID, mode: str = "research", llmClient=None) -> tuple[dict, str]` 返回 `(payload, renderedMd)`
  - mode ∈ `{"research", "attribution", "compare"}`；未知 mode 抛 `ValueError`
  - LLM 仅用于 text 块；每块单独调用；LLM 异常 → 模板降级文案（`"（自动摘要生成失败，以下为结构化摘要）"` + step.summary 列表）
  - chart/table 块数据值**只来自** step 结果/finding 数据，LLM 文本不参与数字

- [ ] **Step 1: 写失败测试（fake LLM，纯逻辑）**

```python
"""ReportPlanner：mode 模板 / LLM 失败降级 / 数据值不被 LLM 改写。"""
import pytest

from app.services.report_planner import ReportPlanner


class FakeLlm:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def complete(self, messages, **kwargs):
        if self._fail:
            raise RuntimeError("llm down")
        return type("R", (), {"content": "解读：指标下降明显。"})()


@pytest.mark.asyncio
async def test_research_mode_section_order(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    payload, md = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    kinds = [s["kind"] for s in payload["sections"]]
    assert kinds[0] == "executive_summary" and kinds[-1] == "methodology"
    assert any(b["type"] == "chart" for s in payload["sections"] for b in s["blocks"])
    assert "# " in md  # rendered_md 非


@pytest.mark.asyncio
async def test_chart_data_not_llm_touched(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    chart_blocks = [b for s in payload["sections"] for b in s["blocks"] if b["type"] == "chart"]
    assert chart_blocks[0]["content"]["rows"][0]["cnt"] == 100   # 与 fake step data 逐字一致


@pytest.mark.asyncio
async def test_llm_failure_degrades_to_template(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    planner._llmClientFactory = lambda: FakeLlm(fail=True)
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    summary = payload["sections"][0]["blocks"][0]["content"]
    assert "结构化摘要" in summary


@pytest.mark.asyncio
async def test_unknown_mode_raises(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    with pytest.raises(ValueError):
        await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="nope")
```

`conftest` 级 fixture `fakeReportDeps`（放本测试文件内即可）：向真实 PG 写 1 session + 1 turn + 1 finding（`ResearchSessionService`），返回 `(ReportPlanner(sessionService=…, llmClientFactory=lambda: FakeLlm()), sessionId, turnId)`。

- [ ] **Step 2: 跑测试确认 RED**

Run: `cd backend && python -m pytest app/tests/unit/test_report_planner.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 写实现**

- 段落模板常量：

```python
_MODE_SECTIONS: dict[str, list[str]] = {
    "research": ["executive_summary", "data", "knowledge", "methodology"],
    "attribution": ["conclusion", "hypothesis_table", "data", "alternative", "methodology"],
    "compare": ["comparison_table", "data", "diff_analysis", "methodology"],
}
```

- `compose` < 50 行：查 session/finding/step 数据 → 按 mode 取模板 → 每段 `_buildSection(kind, ctx)` 分派 → LLM 文本块 → MD 渲染（纯 string 模板，chart 块渲染为表格 + 提示「图表见 payload」，table 块 markdown table）→ `sessionService.publishReport`
- text 块数字防改写：LLM 只收到 step.summary / finding.claim_text（**不含原始行数据**），原始数字只出现在 table/chart 块——结构上免疫改数，无需渲染期 diff

- [ ] **Step 4: 跑测试确认 GREEN**

Run: `cd backend && python -m pytest app/tests/unit/test_report_planner.py -v`
Expected: 4 PASS

- [ ] **Step 5: 接线 Task 5**

把 `research_agent_service._stageReport` 的占位 reporter 换成真实 `ReportPlanner`，`test_auto_confirm_runs_to_done_and_publishes` 改走真实 compose（该测试从 fake reporter 升级）。

Run: `cd backend && python -m pytest app/tests/integration/test_research_agent_service.py -v`
Expected: 3 PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/report_planner.py backend/app/tests/unit/test_report_planner.py backend/app/services/research_agent_service.py
git commit -m "feat(research): ReportPlanner 三 mode 报告 + MD 渲染 + 版本归档"
```

---

### Task 7: REST API `/api/v1/research/*`

**Files:**
- Create: `backend/app/api/v1/research.py`
- Create: `backend/app/domain/research_schemas.py`（CamelModel，独立小文件，不动 1700 行的 schemas.py）
- Modify: `backend/app/main.py`（router 注册处——实现前 `grep -n "includeRouter\|include_router" app/main.py | head` 确认真实注册方式）
- Test: `backend/app/tests/integration/test_research_api.py`

**Interfaces:**
- Produces（HTTP 契约，CamelModel 自动 camelCase JSON）:
  - `POST /api/v1/research/sessions` `{question, mode?}` → `ResearchSessionRead`（201）
  - `GET /api/v1/research/sessions` → `list[ResearchSessionRead]`（当前用户的）
  - `GET /api/v1/research/sessions/{sessionId}` → `{session, turns, pendingCheckpoint}`
  - `POST /api/v1/research/sessions/{sessionId}/turns` `{question}` → 202 `{sessionId, turnId, status}`（状态机异步跑，进度走 SSE）
  - `POST /api/v1/research/checkpoints/{checkpointId}/answer` `{action, choice?}` → `{sessionStatus, nextPhase}`
  - `GET /api/v1/research/sessions/{sessionId}/report` `?version=N` → 当前 published（或指定版）
  - `GET /api/v1/research/sessions/{sessionId}/reports` → 版本列表
- 鉴权：`router = APIRouter(prefix="/research", dependencies=[Depends(getCurrentUser)])`（router-auth 强制，read-only 也要）
- `X-User-Id` 越权防护：所有 session 读写校验 `created_by == currentUserId`，不匹配 404（不泄露存在性）

- [ ] **Step 1: 写失败测试**

```python
"""research REST API（真实 PG + HTTP 链路）。"""
import pytest

pytestmark = pytest.mark.asyncio


async def test_requires_auth(client):
    resp = await client.get("/api/v1/research/sessions")
    assert resp.status_code == 401


async def test_create_and_get_session(client, authHeaders):
    resp = await client.post("/api/v1/research/sessions",
                             json={"question": "供应商收货量为什么下降"},
                             headers=authHeaders)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "running" and body["question"] if False else True
    sid = body["id"]
    detail = await client.get(f"/api/v1/research/sessions/{sid}", headers=authHeaders)
    assert detail.status_code == 200
    assert detail.json()["session"]["id"] == sid


async def test_other_user_session_404(client, authHeaders, secondUserHeaders):
    resp = await client.post("/api/v1/research/sessions",
                             json={"question": "q"}, headers=authHeaders)
    sid = resp.json()["id"]
    resp2 = await client.get(f"/api/v1/research/sessions/{sid}", headers=secondUserHeaders)
    assert resp2.status_code == 404
```

> fixture 名 `authHeaders` / `secondUserHeaders`：实现前先看 `app/tests/integration/` 现有鉴权 fixture（`grep -rn "authHeaders\|secondUser" app/tests/integration/conftest.py app/tests/integration/*.py | head`）。没有就按现有 login 流程（`POST /api/v1/auth/login`）在文件内造，参照任一现有带鉴权的集成测试。

- [ ] **Step 2: 跑测试确认 RED** → **Step 3: 写实现**（route handlers 全部 < 30 行，委托 service）→ **Step 4: GREEN**（4 PASS）

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/research.py backend/app/domain/research_schemas.py backend/app/main.py backend/app/tests/integration/test_research_api.py
git commit -m "feat(research): REST API sessions/turns/checkpoints/report（含越权防护）"
```

---

### Task 8: SSE 端点 `/api/v1/research/stream` + 事件总线

**Files:**
- Create: `backend/app/services/research_event_bus.py`
- Modify: `backend/app/api/v1/research.py`（追加 stream 路由）
- Test: `backend/app/tests/integration/test_research_stream.py`

**Interfaces:**
- Produces:
  - `class ResearchEventBus`：进程内 per-session 广播。`subscribe(sessionId) -> asyncio.Queue` / `unsubscribe(sessionId, queue)` / `async publish(sessionId, event: str, payload: dict)`；队列上限 200，满则丢最旧并 log warning（背压策略显式化）
  - `GET /api/v1/research/stream?sessionId=...` → `StreamingResponse(media_type="text/event-stream")`，事件名 = `research.<stage>`（设计 §4.5 全集），首事件 `research.connected`，心跳 `: ping` 每 15s
  - Task 5 的 `startTurn/resumeTurn` 的 `emit` 参数在本任务接线为 `bus.publish` 的 partial
- 事件序列契约（测试断言用）：`research.connected → (research.intent → research.esl → research.checkpoint) | (… → research.plan → research.step.* → … → research.done)`；错误 → `research.error` 后关流

- [ ] **Step 1: 写失败测试**

```python
"""SSE：事件顺序 + connected 首事件 + checkpoint 暂停事件（真实 PG）。"""
import json

import pytest

pytestmark = pytest.mark.asyncio


async def test_stream_emits_connected_and_stage_events(client, authHeaders, makeService, monkeypatch):
    # 建 session → 经 API 起 turn（autoConfirm fake 管线注入 app state）
    create = await client.post("/api/v1/research/sessions",
                               json={"question": "供应商收货量"}, headers=authHeaders)
    sid = create.json()["id"]
    events: list[str] = []
    async with client.stream("GET", f"/api/v1/research/stream?sessionId={sid}",
                             headers=authHeaders) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: "))
            if "research.done" in events or len(events) > 12:
                break
    assert events[0] == "research.connected"
    assert any(e.startswith("research.") for e in events[1:])
```

- [ ] **Step 2: RED** → **Step 3: 写实现**（bus < 100 行；stream 路由 < 40 行：生成队列 → async generator yield `event: {name}\ndata: {json}\n\n`，客户端断连 finally unsubscribe）→ **Step 4: GREEN**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/research_event_bus.py backend/app/api/v1/research.py backend/app/tests/integration/test_research_stream.py
git commit -m "feat(research): 独立 SSE 端点 + 进程内事件总线（research.* 事件族）"
```

---

### Task 9: 前端 api client + researchStore

**Files:**
- Create: `frontend/src/api/research.ts`
- Create: `frontend/src/stores/researchStore.ts`
- Test: `frontend/src/stores/researchStore.test.ts`

**Interfaces:**
- Produces:
  - `research.ts`：`createResearchSession` / `listResearchSessions` / `getResearchSession` / `submitTurn` / `answerCheckpoint` / `getReport` / `listReports` / `openResearchStream(sessionId, onEvent)`（SSE 用 fetch + `authHeaders` 注入 Bearer——沿用 `api/authHeaders.ts`，见记忆 qa-system-frontend-sse-auth-header）
  - `researchStore.ts`（zustand）：`{sessions, currentSession, events, pendingCheckpoint, report, loadSessions, openSession, sendQuestion, answer, connectStream}`；不可变更新（展开运算符，禁止 push 原数组）

- [ ] **Step 1: 写失败测试**（vitest，fake fetch；断言 answer 后 pendingCheckpoint 清空、events 只追加不修改旧数组、SSE event 分发到 store）→ **Step 2: RED** → **Step 3: 实现** → **Step 4: GREEN**

Run: `cd frontend && npx vitest run src/stores/researchStore.test.ts`

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/research.ts frontend/src/stores/researchStore.ts frontend/src/stores/researchStore.test.ts
git commit -m "feat(research): 前端 API client + researchStore（SSE 鉴权接入）"
```

---

### Task 10: 前端 4 组件 + 路由 + i18n

**Files:**
- Create: `frontend/src/pages/research/ResearchListPage.tsx`、`ResearchSessionPage.tsx`、`ResearchReportPage.tsx`
- Create: `frontend/src/components/research/ResearchTimeline.tsx`、`CheckpointCard.tsx`、`ReportRenderer.tsx`、`ResearchCompareView.tsx`（compare 组件本任务先落空壳 props 契约，Task 12 填充）
- Modify: `frontend/src/App.tsx`（路由 `/research/*`）、`frontend/src/i18n/zh-CN.ts`、`frontend/src/i18n/en-US.ts`（`research.*` 命名空间）
- Test: `frontend/src/components/research/__tests__/CheckpointCard.test.tsx`、`ReportRenderer.test.tsx`、`ResearchTimeline.test.tsx`

**Interfaces:**
- Consumes: Task 9 store 与 api
- Produces:
  - `<CheckpointCard checkpoint={…} onAnswer={(action, choice) => void}>`：渲染 options 卡片，confirm/modify/reject 三按钮
  - `<ReportRenderer payload={ReportPayload}>`：按 sections[].blocks[] 顺序渲染 text/chart(table fallback)/table/bullet_list
  - `<ResearchTimeline turns={…} checkpoints={…} onJump={(anchor) => void}>`
- i18n：所有用户可见文案走 `research.*` key（zh/en 双份；漏 key 会被 `i18n.test.ts` 现有守卫抓住）

- [ ] **Step 1: 写失败测试**

```tsx
// CheckpointCard.test.tsx（RTL）
import { render, screen, fireEvent } from "@testing-library/react";
import { CheckpointCard } from "../CheckpointCard";

const checkpoint = {
  id: "cp-1", phase: "intent", status: "pending",
  options: { arms: [{ arm: "metric", displayName: "收货量", confidence: 0.8 }] },
  prompt: "三臂是否齐全？",
};

test("renders options and emits answer on confirm", () => {
  const onAnswer = vi.fn();
  render(<CheckpointCard checkpoint={checkpoint} onAnswer={onAnswer} />);
  expect(screen.getByText("三臂是否齐全？")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /confirm/i }));
  expect(onAnswer).toHaveBeenCalledWith("confirm", expect.anything());
});
```

ReportRenderer.test.tsx：给定 text/table/chart/bullet 四块，断言渲染顺序与表格行数；ResearchTimeline.test.tsx：3 turn + 1 checkpoint 节点渲染 + onJump 回调。

- [ ] **Step 2: RED**（`cd frontend && npx vitest run src/components/research`）→ **Step 3: 实现**（每组件 < 150 行；chart 块 v1 用 table 呈现 + ECharts 接入留 Task 12 后统一）→ **Step 4: GREEN**

- [ ] **Step 5: 路由 + i18n + 类型检查**

Run: `cd frontend && npm run build && npx vitest run`
Expected: build 0 error；i18n.test.ts 守卫通过

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/research frontend/src/components/research frontend/src/App.tsx frontend/src/i18n
git commit -m "feat(research): 前端三页面 + Timeline/CheckpointCard/ReportRenderer + i18n"
```

---

### Task 11: 菜单 seed 注入（menu-config-seed，不硬编）

**Files:**
- Modify: `backend/scripts/seed_menu_config.py`（SECTIONS 追加 + ITEMS 追加）
- Test: `backend/app/tests/unit/test_seed_menu_research.py`

**Interfaces:**
- Consumes: seed 现有模式（INSERT-if-absent；`sort_order` 百位区间 240 已被 wiki 占用 → research section 用 **250**，置于 enterpriseWiki(240) 与 bizConfig(300) 之间）
- Produces: section `section.research`（label_key `menu.section.research`，icon `experiment`）+ 叶子项 `research.session`（path `/research`，sort_order 2510）+ `research.compare`（path `/research/compare`，sort_order 2520）

- [ ] **Step 1: 写失败测试**

```python
"""seed_menu_config：research section/item 存在且不破坏幂等契约。"""
import importlib.util, pathlib


def _loadSeedModule():
    path = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "seed_menu_config.py"
    spec = importlib.util.spec_from_file_location("seed_menu_config", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_research_section_and_items_present() -> None:
    mod = _loadSeedModule()
    sectionCodes = {s["code"] for s in mod.SECTIONS}
    assert "section.research" in sectionCodes
    itemPaths = {i["path"] for i in mod.ITEMS}
    assert "/research" in itemPaths and "/research/compare" in itemPaths


def test_sort_order_spacing() -> None:
    mod = _loadSeedModule()
    research = [i for i in mod.ITEMS if str(i["path"]).startswith("/research")]
    assert all(i["sort_order"] % 10 == 0 for i in research)
```

- [ ] **Step 2: RED** → **Step 3: 改 seed（只追加，不改既有行）** → **Step 4: GREEN** → **Step 5: Commit**

```bash
git add backend/scripts/seed_menu_config.py backend/app/tests/unit/test_seed_menu_research.py
git commit -m "feat(research): 菜单 seed 注入研究入口（section.research + 2 叶子项）"
```

---

### Task 12: 侧边栏勾选对比视图

**Files:**
- Modify: `frontend/src/components/research/ResearchCompareView.tsx`（填充 Task 10 的空壳）
- Modify: `frontend/src/pages/research/ResearchListPage.tsx`（checkbox 多选 + 「对比」按钮 → `/research/compare?ids=a,b`)
- Modify: `frontend/src/App.tsx`（`/research/compare` 路由）
- Test: `frontend/src/components/research/__tests__/ResearchCompareView.test.tsx`

**Interfaces:**
- Produces: `<ResearchCompareView sessionIds={string[]}>`：并行拉各 session 最新 published report，并排渲染「标题 / executive_summary / 关键 findings（claim + confidence）/ 方法学」四列对齐；某 session 无报告显示「未生成报告」占位
- 后端零改动（`GET /reports` 已够）

- [ ] **Step 1: 失败测试**（fake store：2 session 有报告 + 1 无报告；断言 4 行对齐结构与占位文案）→ **Step 2: RED** → **Step 3: 实现** → **Step 4: GREEN**（`npx vitest run src/components/research`）→ **Step 5: Commit**

```bash
git add frontend/src/components/research/ResearchCompareView.tsx frontend/src/pages/research/ResearchListPage.tsx frontend/src/App.tsx frontend/src/components/research/__tests__/ResearchCompareView.test.tsx
git commit -m "feat(research): 侧边栏勾选对比视图"
```

---

### Task 13: 全回归 + 真机验收 + SSOT

**Files:**
- Modify: `Harness/changes/feat-research-entry/summary.md`（补实施记录）
- Modify: `docs/superpowers/specs/2026-10-04-research-entry-design.md`（回写 §4.3 修正：_executeDataStep → 薄 runner，已在本计划头部声明）

- [ ] **Step 1: 后端回归（串行！绝不并行两个套件）**

```bash
cd backend
python -m pytest app/tests/unit -x -q            # 对照既有 48 红基线，零新增红
python -m pytest app/tests/integration/test_research_session_service.py app/tests/integration/test_research_agent_service.py app/tests/integration/test_research_api.py app/tests/integration/test_research_stream.py app/tests/integration/test_research_sql_runner.py -v
python -m pytest app/tests/integration -q -x     # 全量集成，零新增红（chat 套件不得受影响）
```

- [ ] **Step 2: 覆盖率 ≥ 80%（新代码）**

```bash
cd backend && python -m pytest app/tests/unit app/tests/integration/test_research_session_service.py app/tests/integration/test_research_agent_service.py app/tests/integration/test_research_api.py app/tests/integration/test_research_stream.py --cov=app/services/research_sql_runner --cov=app/services/enterprise_semantic_layer --cov=app/services/research_hypothesis_adapter --cov=app/services/report_planner --cov=app/services/research_agent_service --cov=app/services/research_session_service --cov=app/services/research_event_bus --cov-report=term | tail -15
```

Expected: 每个新模块 ≥ 80%

- [ ] **Step 3: 前端构建 + 测试 + 重建镜像**

```bash
cd frontend && npm run build && npx vitest run
cd .. && docker compose build --no-cache frontend && docker compose build backend && docker compose up -d
```

- [ ] **Step 4: 真机验收（Admin/Admin@123 本地凭据）**

1. 登录 → 侧边栏出现「研究」菜单（seed 注入）
2. 新建研究：「供应商收货量为什么下降」→ SSE 流出 `research.intent/esl/checkpoint`
3. Checkpoint #1 确认 → plan → Checkpoint #2 确认 → step 流 → hypothesis → Checkpoint #3 挑选 → findings → `research.done`
4. 报告页渲染 + 下载 MD；重跑 → version=2、v1 superseded
5. 对比视图勾选 ≥2 session 并排渲染
6. 既有 chat 页跑一问：**行为与改造前完全一致**（回归口径）

- [ ] **Step 5: SSOT 回写 + Commit**

```bash
git add Harness/changes/feat-research-entry/summary.md docs/superpowers/specs/2026-10-04-research-entry-design.md
git commit -m "docs(research): 实施记录回写 SSOT（含 _executeDataStep→薄 runner 修正）"
```

---

## 任务依赖

| Task | 依赖 | 产出 |
|---|---|---|
| 1 | — | 5 表 ORM + 迁移 0111 |
| 2 | — | ESL（无依赖，可与 1 并行开发但提交顺序 1→2）|
| 3 | 1 | 持久化服务 |
| 4 | — | 假设适配 + SQL runner |
| 5 | 2,3,4 | 状态机 |
| 6 | 3 | ReportPlanner（Step 5 接线回 5）|
| 7 | 3,5 | REST API |
| 8 | 5,7 | SSE |
| 9 | 7,8 | 前端 client/store |
| 10 | 9 | 前端组件 |
| 11 | — | 菜单 seed（可早做）|
| 12 | 10 | 对比视图 |
| 13 | 全部 | 回归 + 真机 + SSOT |

## 已知风险与缓解

| 风险 | 缓解 |
|---|---|
| `business_db_pool` / SQL Guard 真实签名与预期不符 | Task 4 Step 0 显式 grep 核对，按实际接入；禁止参照 chat 私有路径 |
| 现有 fixture（authHeaders 等）不存在预期名字 | Task 7 Step 1 注释给出 grep 定位 + 现有 login 流程兜底 |
| `MultiStepPlan`/`StepPlan` 字段与 fake 不一致 | 以 `app/domain/multi_step_plan.py` 真实字段修 fake，不许改产品代码迁就 fake |
| SSE 经 nginx 重现 `ERR_INCOMPLETE_CHUNKED_ENCODING`（既有未决问题） | 真机验收直接命中；若复现，root-cause 单独立项，不混入本计划 |
| LLM 慢（本地 27B ≈ 98s/轮） | SSE 心跳 15s + 事件驱动 UI；timeout 只在 HTTP 层调大（300s，与 nginx 现有配置一致） |
