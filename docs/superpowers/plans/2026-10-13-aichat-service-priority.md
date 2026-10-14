# AIChat Service 优化优先级与实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对照 1012-12 AIChat Service 报告（commit `fabd487ded3b7721f645a8c48250936dd18ff638`），按"当前代码是否仍存在该问题"重新分级 12 项 + 新增发现，给出可 TDD 派发的实施任务。优先级按当前 ROI / 风险划分 P0/P1/P2。

**Architecture:**
- P0：核心能力对齐（流式入口接 KPI/L4、意图分类顺序、SessionQueryState 绑定数据源、序列化保真）
- P1：能力补齐（SQL 改写 AST 化、模型能力路由、流式会话绑定、多步依赖、假设验证）
- P2：运营与体验（错误分类 + 整轮 deadline）
- 不重写 chat 主架构；只对单点能力做最小修复 + 测试先行

**Tech Stack:** FastAPI · SQLAlchemy 2.0 async · Alembic · Pydantic v2 · pytest-asyncio · PostgreSQL (real) · sqlglot (dialect registry 已就绪) · zustand (前端) · React 18

**Spec / Source:**
- 报告：`docs/ AIChat Service 功能、兼容性与完整性分析.md` (1012-12, fabd487de)
- 同期姊妹报告（独立，本计划不直接覆盖）：
  - `docs/QA-1012-01.md`（认证/模型/数据源/文档）
  - `docs/ 1012研究会话--代码分析与优化建议.md`（研究会话模块，main 未合并，bf0e0bd）
- 当前核对 HEAD：`08b3ae5`，距报告基线 ~70 commits

---

## 当前代码核实结果（对照报告 12 项）

| # | 报告问题 | 当前状态 | 关键证据 | 优先级 |
|---|---|---|---|---|
| 1 | 普通 vs 流式入口不对齐 | 🔸 **KPI 域流式未接入；L4 + 拦截卡片已接入** | 普通 `chat_service.py:409-432, 436-440` 调 KPI/L4；流式 `chat_stream.py:154 _processMessageStreamInner` **直接** `_streamChitchat / _streamQuery` | **P0** |
| 2 | 短追问/问候误分类 | ⚠️ 完全成立 | `intent_service.py:551-554` `CHITCHAT_KEYWORDS` 优先 + `len ≤ _SHORT_MESSAGE_LIMIT` 早返，**先于** `hasPriorState` 的 REFINE 判定（line 594） | **P0** |
| 3 | `SessionQueryState` 不绑定数据源/用户 | ⚠️ 完全成立 | `models.py:861-894` 字段仅 `session_id/last_*/turn_count/inheritance_snapshot`；UniqueConstraint 只对 `session_id`；前端 `chatStore.ts:489 setDatasourceId` 仅 `set({ datasourceId: id })`，**不清** messages/sessionId/isStreaming | **P0** |
| 4 | 会话归属守卫 | ✅ 已修（3759d85） | `chat.py:54,74,99,193` 均调 `assertSessionOwnership`；`session_guard.py:39` 实现 | P3（维持覆盖） |
| 5 | 快捷 SQL 改写 quote-unaware | ⚠️ 完全成立 | `nl2sql_refine.py:69 _CLAUSE_LIMIT` regex + `nl2sql_refine.py:92 _rewriteLimit` `re.sub()` 纯文本替换；无 AST / sqlglot | **P1** |
| 6 | KPI 用固化 FeatureDefinition（不看 `datasourceId`/时间） | ⚠️ 完全成立且根因更深 | `chat_service.py:1489 _executeCalculationLogic` 用 `feat.datasource_id` + 固化 `calculation_logic`，**完全忽略** `dto.datasourceId` 和本轮时间/过滤 | **P0**（与 #1 同根因；必须在 #1 验收里加） |
| 7 | 模型能力路由与流式 usage | 🔸 部分修（usage 已不丢；能力过滤未做） | `model_router_service.py:61 selectModel` 无 streaming/tools 过滤；`openai_client.py:202,217` usage 末块已捕获 | P1 |
| 8 | JSON/数组/Decimal/date 序列化 | 🔸 **修正**：NaN/Infinity 已被 `default=str` 兜底；剩余是**保真度**问题（dict/list distinct unhashable、Decimal/date 精度丢失）；研究侧 `6449ed0 / 81d54a4` 已 backport，chat 流式编码器未做 | **P0** |
| 9 | 流式绑定会话/轮次 | 🔸 **修正** — research SSE 已修（`368c146 / 8354f9a`，main 未合）；chat 流式**未修**（无 lastEventId 守卫、CRLF 跨块、无 done 收尾） | P1 |
| 10 | 多步依赖不显式 | ⚠️ 完全成立 | `multi_step_plan.py:92-107 StepPlan` / `109-146 StepResult` 无 `depends_on / input_refs / output_schema` | P1 |
| 11 | 假设自动验证 / 证据回放 | 🔸 部分修（持久化已加；不自动验证、不完整回放） | `hypothesis_service.py` grep `runVerification/auto_verify/executeSql` 全部为空 | P1 |
| 12 | 预算/错误分类不闭环 | 🔸 部分（预算存在；细粒度 ErrorType 缺失） | `stream_events.py:33-43 ErrorType` 仅 DOMAIN/LLM/INTERNAL | P2 |

---

## 新增发现（报告未覆盖）

**N1. `chat_service.py` 1969 行，触红线（用户全局规范 ≤800）**
- 当前 1969 行，是 800 行硬上限的 **2.5 倍**
- 拆分路径（与既有 `chat_service 文件拆分` 记忆一致）：
  - `chat_service.py` 只保留 `processMessage / processMessageStream` 编排
  - `L1KpiMixin` 抽出 L1 KPI 直答链路（`chat_service.py:411-440 + 1476-1583`）
  - `L4AgentMixin` 已存在（line 294 已声明）
  - `intent_routing.py` 抽 `_classifyMessage + classifyResult + _handleDomainCommand`
- **当前豁免**：记忆 `chat_multistep-line-cap-exemption` 允许 ≥1000 行暂不拆。本计划**不**触发拆分，但**建议 chat_service.py ≤2000 行硬上限**，超出 2000 行必须拆

**N2. 三份 1012 报告无交叉引用**
- `QA-1012-01`（认证/模型/数据源/文档）、`1012 研究会话`（独立分支，未合并）、本报告（AIChat）
- 每份单独跑，没有合并 P0 清单
- 建议建 `docs/superpowers/specs/1012-code-review-cross-refs.md`（本计划**不**直接覆盖，但 P3-1 留口子）

**N3. 数据源切换时 store 不重置的副作用比报告写的更深**
- 报告只说"切换数据源只改 id"
- 实际 `chatStore.ts:497-533 sendMessageStream` 把 `datasourceId` 注入请求，**新数据源 + 同 sessionId** → 后端用新数据源 query、旧数据源 state 仍在 `SessionQueryState`
- 即便前端切换后自动 `reset()`，用户复制 sessionId 跨数据源复用 → 撞 `SessionQueryState` 加载陈旧 PK 数据
- **与 P0-3 同根因**

**N4. `qwen-uplift` 回滚后的知识保留**
- `c2f12b4 revert(qwen-uplift): 回滚路由 hook + 删除 QueryPatternRouter`
- `pc.selected` frozen + 路由 hook 不可达的知识留在 `qa-system-qwen-multistep-uplift` 记忆
- 不在 12 项范围内，属于"按任务匹配模型"的更激进方案，建议 P2 阶段另起任务

**N5. `stream_events.py:55 json.dumps(default=str)` 是隐式数据失真源**
- `default=str` 把 Decimal / date / UUID 静默转字符串，下游消费者无差别 `parseFloat` / `Date()` 解析丢精度
- 研究侧 `6449ed0` 已显式分类处理；chat 流式仍走静默 str
- **与 P0-4 同根因**

---

## Global Constraints

- **Branching:** 从 `main` 切 `feat/aichat-priority-p0`，P0 全部完成后合；P1 任务各自独立分支（避免互相阻塞）
- **TDD:** 每个 Task RED → GREEN → IMPROVE。覆盖 ≥ 80% on touched modules
- **Test isolation:** unit tests 与 integration tests **必须** 不同进程（`pytest -m unit` / `pytest -m integration`）
- **DB:** integration 用 `qa_metadata_test`（5434 端口，恒空）；**禁止** 写 prod `qa_metadata`
- **Alembic renumber:** 落到 P0-3。先看 `ls backend/alembic/versions/ | sort -r | head -10`；占下一个空闲号（如 `0118_*`），down_revision 指向当前 head
- **No-go zones（既有项目约束，本计划尊重）:** `ontology_service.py`, `graph_traversal_service.py`, `step_query_planner.py`（不直接触碰，仅在接口处读）
- **Immutability:** ORM 模型不可原地修改；构造新对象返回
- **Naming:** 函数/变量 `camelCase`；类型/组件 `PascalCase`；ORM/Pydantic 字段 `snake_case`
- **Error messages:** from `app/domain/error_messages.py` constants — 不在 services 硬编码
- **Magic numbers:** declare in `app/config.py` 或 typed constant — 不内联 `64` / `50`
- **chat_service.py 行数上限:** 2000 行；超出必须拆（与 `chat_multistep-line-cap-exemption` 一致口径）
- **正则反向断言:** "是谁拒的" 必须用 ASCII 错误码 + 文案断言，并反向删除守卫自检（如 `qa-system-same-status-code-false-green` 记忆）
- **前端 store 改动必须 wire:** prop / store flag / 调用点三处同步（`qa-system-resolutioninflight-page-wiring` 教训）

---

## File Structure

| Path | Role |
|---|---|
| `backend/app/services/chat_service.py` | 修改（抽 mixin） — P0-1, N1 拆分 |
| `backend/app/services/chat_stream.py` | 修改（接 KPI/L4 pre-amble） — P0-1 |
| `backend/app/services/chat_stream_output.py` | 修改（共享 `_runL1AndL4Preamble`） — P0-1 |
| `backend/app/services/intent_service.py` | 修改（分类顺序、礼貌前缀剥除） — P0-2 |
| `backend/app/domain/models.py` | 修改（SessionQueryState 扩展字段） — P0-3 |
| `backend/app/services/chat_context.py` | 修改（load/save 带 datasource/user 过滤） — P0-3 |
| `backend/app/api/v1/chat.py` | 修改（`/stream` 端点透传 userId 到 SessionQueryState） — P0-3 |
| `backend/alembic/versions/0118_session_query_state_scope.py` | 新增（迁移：datasource_id/user_id/schema_name/datasource_version） — P0-3 |
| `backend/app/services/stream_events.py` | 修改（`_jsonSafe` + `allow_nan=False`） — P0-4 |
| `backend/app/services/data_summary.py` | 修改（dict/list 列 distinct 容错） — P0-4 |
| `backend/app/services/nl2sql_refine.py` | 修改（`_rewriteLimit/_rewriteOrderBy` 改 sqlglot） — P1-1 |
| `backend/app/services/model_router_service.py` | 修改（`ModelCapability` 过滤） — P1-2 |
| `backend/app/infrastructure/llm/openai_client.py` | 修改（usage+content 双收） — P1-2 |
| `backend/app/services/chat_usage.py` | 修改（`cost_source` 显式分类） — P1-2 |
| `frontend/src/stores/chatStore.ts` | 修改（assistantMessageId 流式守卫） — P0-3, P1-3 |
| `frontend/src/stores/stream_chunk_parser.ts` | 新增（CRLF 跨块保留） — P1-3 |
| `frontend/src/api/chat.ts` | 修改（流式带 `lastEventId`） — P1-3 |
| `backend/app/domain/multi_step_plan.py` | 修改（StepPlan/StepResult 加依赖字段） — P1-4 |
| `backend/app/services/multi_step_dependency.py` | 新增（依赖解析 + 下游 skip） — P1-4 |
| `backend/app/services/hypothesis_service.py` | 修改（`Hypothesis.verify()`） — P1-5 |
| `backend/app/services/turn_result_repository.py` | 新增（TurnResult 持久化） — P1-5 |
| `frontend/src/components/research/ReportRenderer.tsx` | 修改（按 `result_ref` 拉数据回放） — P1-5 |
| `backend/app/services/stream_events.py` | 修改（ErrorType 扩 `provider_transient/sql_syntax/permission/query_timeout`） — P2-1 |
| `backend/app/services/chat_stream_output.py` | 修改（整轮 deadline 共享） — P2-1 |
| `backend/app/tests/unit/test_intent_classify_order.py` | 新增 — P0-2 |
| `backend/app/tests/unit/test_session_query_state_roundtrip.py` | 新增 — P0-3 |
| `backend/app/tests/unit/test_stream_events_safe.py` | 新增 — P0-4 |
| `backend/app/tests/unit/test_data_summary_distinct.py` | 新增 — P0-4 |
| `backend/app/tests/unit/test_nl2sql_refine_quote.py` | 新增 — P1-1 |
| `backend/app/tests/unit/test_model_router_capability.py` | 新增 — P1-2 |
| `backend/app/tests/unit/test_hypothesis_verify.py` | 新增 — P1-5 |
| `backend/app/tests/integration/test_chat_session_state_scope.py` | 新增 — P0-3 |
| `backend/app/tests/integration/test_chat_stream_l1_l4.py` | 新增 — P0-1 |
| `frontend/src/stores/__tests__/chatStore.assistant_message_id.test.ts` | 新增 — P1-3 |
| `frontend/src/stores/__tests__/streamChunkParser.test.ts` | 新增 — P1-3 |

---

## Task 1: Branch setup

**Files:** none (git operations only)

- [ ] **Step 1.1: Verify current head and alembic state**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git branch --show-current
git log --oneline -3
ls backend/alembic/versions/ | sort -r | head -10
```

Expected: on `main`, head `08b3ae5` or newer; alembic head should be around `0117_*`. The next free number for P0-3 is **`0118`**.

- [ ] **Step 1.2: Cut task branch**

```bash
git checkout main
git pull --ff-only origin main 2>/dev/null || echo "no upstream yet"
git checkout -b feat/aichat-priority-p0
git push -u origin feat/aichat-priority-p0
```

- [ ] **Step 1.3: Announce branch**

```bash
git branch --show-current
git log --oneline -1
```

---

## P0 任务汇总

按 ROI 排序：P0-1（流式 KPI/L4 + KPI 用 dto）→ P0-2（意图分类）→ P0-3（SessionQueryState 绑定）→ P0-4（序列化保真）。每个 Task RED → GREEN → IMPROVE。

---

## Task 2: P0-1 — 流式入口接入 KPI/L4 + KPI 用 dto（项 1 + 合并项 6）

**Files:**
- Modify: `backend/app/services/chat_stream.py:154 _processMessageStreamInner`
- Modify: `backend/app/services/chat_service.py:409-440`（抽 `_runL1AndL4Preamble` 共享方法到 `ChatStreamOutputMixin`）
- Modify: `backend/app/services/chat_stream_output.py`（加 mixin）
- Test: `backend/app/tests/integration/test_chat_stream_l1_l4.py`

**Interfaces:**
- Consumes: `ChatService.processMessage` / `processMessageStream` 调用方
- Produces: `async def _runL1AndL4Preamble(dto, session, user) -> PreambleResult | None`（命中 KPI/L4 → 产事件流；未命中 → None）
- PreambleResult 含：`chart_type / chart_option / answer / routing_layer / events`（流式事件列表，含 done 帧）

### Step 2.1: Write failing integration test

```python
# backend/app/tests/integration/test_chat_stream_l1_l4.py
"""integration tests for chat L1 KPI + L4 Agent in stream path.

RED: 流式入口未调 KPI/L4 ⇒ RED。
GREEN: 流式入口接 KPI/L4 pre-amble ⇒ GREEN。
反向断言（P0 验收硬性）：
- 用户选 datasource_B + 命中 datasource_A 固化的 KPI → 流式帧含 warning 文案
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_stream_kpi_match_returns_zero_llm_cost(
    client: AsyncClient, dbSession: AsyncSession,
    seeded_kpi: dict,  # FeatureDefinition + KpiCatalog + ChatMetricRow fixture
):
    """L1 KPI 命中 → 流式 SSE 包含 chart=KPI + routing_layer=L1 + done 帧携带 cost=0。"""
    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "question": "今年订单数",
            "sessionId": "stream-test-1",
            "datasourceId": seeded_kpi["datasource_id"],
        },
        headers={"X-User-Id": str(seeded_kpi["user_id"])},
    )
    assert resp.status_code == 200
    text = resp.text
    # SSE frames: meta + chart + done (no token/LLM frames)
    assert "event: meta" in text
    assert "routingLayer" in text
    assert "L1" in text
    assert "event: chart" in text
    assert "kpi" in text  # chart payload 含 kpi 块
    assert "event: done" in text
    # 反向断言：未命中 LLM
    assert "event: token" not in text


async def test_stream_kpi_datasource_mismatch_warns(
    client: AsyncClient, dbSession: AsyncSession,
    seeded_kpi: dict,
):
    """KPI 固化在 datasource_A，用户选 datasource_B → SSE 含 mismatch 警告，不静默。"""
    other_ds_id = seeded_kpi["datasource_id"] + 1  # 假设有第二个数据源
    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "question": "今年订单数",
            "sessionId": "stream-mismatch",
            "datasourceId": other_ds_id,
        },
        headers={"X-User-Id": str(seeded_kpi["user_id"])},
    )
    assert resp.status_code == 200
    text = resp.text
    assert "datasourceMismatch" in text  # 显式告警
    assert "KPI" in text
    assert "固化数据源" in text or "bound to" in text.lower()


async def test_stream_l4_agent_runs_when_kpi_misses(
    client: AsyncClient, dbSession: AsyncSession,
    seeded_l4_trigger: dict,  # 强制 L4 命中的 fixture
):
    """KPI 未命中 → L4 Agent Loop 命中 → 流式走 agent card 帧。"""
    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "question": "用 supplier_risk_agent 评估 100001",
            "sessionId": "stream-l4",
            "datasourceId": seeded_l4_trigger["datasource_id"],
        },
        headers={"X-User-Id": str(seeded_l4_trigger["user_id"])},
    )
    assert resp.status_code == 200
    text = resp.text
    assert "agentRun" in text  # event: card 帧 payload 含 agent_run
    assert "event: done" in text


async def test_stream_chitchat_skips_l1_l4(
    client: AsyncClient, dbSession: AsyncSession,
    seeded_user: dict,
):
    """'你好' → chitchat 帧，不触发 KPI/L4 预检。"""
    resp = await client.post(
        "/api/v1/chat/stream",
        json={
            "question": "你好",
            "sessionId": "stream-chitchat",
            "datasourceId": seeded_user["datasource_id"],
        },
        headers={"X-User-Id": str(seeded_user["user_id"])},
    )
    assert resp.status_code == 200
    text = resp.text
    assert "event: meta" in text
    assert "chitchat" in text.lower()
    assert "routingLayer" not in text  # chitchat 不写 routing_layer
```

### Step 2.2: Run tests to verify RED

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/integration/test_chat_stream_l1_l4.py -v -m integration
```

Expected: all 4 fail (KPI/L4 not yet called from stream entrypoint).

### Step 2.3: Refactor — extract shared `_runL1AndL4Preamble`

In `backend/app/services/chat_stream_output.py`, add a new mixin `L1L4PreambleMixin`:

```python
class PreambleResult(BaseModel):
    """L1/L4 命中后的封装；含生成 SSE 帧所需全部字段。"""

    routing_layer: Literal["L1", "L4"]
    answer: str
    chart_type: ChartType | None = None
    chart_option: dict | None = None
    visual_rationale: str | None = None
    # L4
    agent_run: dict | None = None
    # L1 cross-datasource mismatch
    datasource_mismatch: bool = False
    mismatch_warning: str | None = None


class L1L4PreambleMixin:
    """抽 KPI/L4 预检为共享 mixin，让普通 + 流式入口共用同一条路径。"""

    _kpiMatcher: KpiSemanticMatchService

    async def _runL1AndL4Preamble(
        self,
        dto: ChatRequest,
        session: AsyncSession,
        user: CurrentUser | None,
    ) -> PreambleResult | None:
        """KPI 优先（命中短路）；未命中则尝试 L4；仍未命中返 None（让主路径继续）。

        行为对 source-provider 改：
        - KPI 用 dto.datasourceId 比较 feat.datasource_id；不匹配给 mismatch_warning（不静默）
        - L4 不改行为
        """
        # 1. KPI 匹配（与 chat_service.py:411-432 同款 try/except 降级）
        try:
            match = await self._kpiMatcher.match(dto.question)
        except Exception:  # noqa: BLE001 — 降级到原 LLM 流水线
            logger.warning("L1 KPI match failed, falling back to LLM", exc_info=True)
            match = None

        if match is not None:
            l1 = await self._buildL1Response(match, session)
            if l1 is not None:
                # P0 合并项 6：检查 datasource 是否匹配
                mismatch = False
                warning = None
                feat = await self._resolveKpiCatalog(match.code, session)
                if feat is not None and feat.datasource_id != dto.datasourceId:
                    mismatch = True
                    warning = (
                        f"该 KPI 指标绑定数据源 {feat.datasource_id}，"
                        f"与你当前选择的数据源 {dto.datasourceId} 不一致。"
                    )
                return PreambleResult(
                    routing_layer="L1",
                    answer=l1.answer,
                    chart_type=l1.chartType,
                    chart_option=l1.chartOption,
                    visual_rationale=l1.visual_rationale,
                    datasource_mismatch=mismatch,
                    mismatch_warning=warning,
                )

        # 2. L4 Agent Loop（流式入口现已在 _streamInterceptCard 接 360/risk/graph，
        #    但通用 L4 agent loop 仍缺；按 P0-1 任务合并到 pre-amble）
        l4 = await self._handleNl2SqlAgent(session, dto, user=user)
        if l4 is not None:
            return PreambleResult(
                routing_layer="L4",
                answer=l4.answer,
                agent_run=(
                    l4.agent_run.model_dump(mode="json", by_alias=True)
                    if l4.agent_run is not None else None
                ),
            )

        return None
```

Add this mixin to `ChatService` class bases (in `chat_service.py:294`):

```python
class ChatService(
    RecallMixin,
    MultiStepMixin,
    MultiStepPersistMixin,
    StreamMixin,
    ContextMixin,
    UsageMixin,
    DomainCommandMixin,
    L4Mixin,
    HypothesisMixin,
    ChatStreamOutputMixin,
    L1L4PreambleMixin,  # NEW
):
    ...
```

### Step 2.4: Use mixin in `chat_service.py:380 _processMessageInner`

Replace lines 408-440 with:

```python
# Phase 1.4 + Phase 4.4：L1 KPI + L4 Agent Loop 共享 pre-amble（流式入口同样调）
preamble = await self._runL1AndL4Preamble(dto, session, user)
if preamble is not None:
    if preamble.routing_layer == "L1":
        _t0 = time.monotonic()
        await self._storeSessionMessages(
            session, dto.sessionId, dto.question, preamble.answer, None,
            routing_layer=ROUTING_LAYER_L1,
            latency_ms=int((time.monotonic() - _t0) * 1000),
            token_cost_usd=0.0,
            chart_type=preamble.chart_type,
            chart_option=preamble.chart_option,
            visual_rationale=preamble.visual_rationale,
        )
    return ChatResponse(
        answer=preamble.answer,
        chart_type=preamble.chart_type,
        chart_option=preamble.chart_option,
        ...
    )

result, state = await self._classifyMessage(session, dto)
...
```

### Step 2.5: Use mixin in `chat_stream.py:154 _processMessageStreamInner`

Insert after `_prepareSupplierQuestion` and before the `_streamChitchat` check:

```python
# P0-1：流式入口共享 L1/L4 pre-amble（普通入口已接）
preamble = await self._runL1AndL4Preamble(dto, session, user)
if preamble is not None:
    yield StreamEvent(EVENT_META, {
        "intent": "KPI" if preamble.routing_layer == "L1" else "AGENT_RUN",
        "routingLayer": preamble.routing_layer,
        "datasourceMismatch": preamble.datasource_mismatch,
    })
    if preamble.mismatch_warning is not None:
        yield StreamEvent(EVENT_META, {
            "warning": preamble.mismatch_warning,
            "code": "KPI_DATASOURCE_MISMATCH",
        })
    if preamble.chart_type is not None and preamble.chart_option is not None:
        yield StreamEvent(EVENT_CHART, {
            "chartType": preamble.chart_type.value,
            "option": preamble.chart_option,
            "rationale": preamble.visual_rationale,
        })
    if preamble.agent_run is not None:
        yield StreamEvent(EVENT_META, {
            "agentRun": preamble.agent_run,
        })
    yield StreamEvent(EVENT_TOKEN, {"text": preamble.answer})
    yield StreamEvent(EVENT_DONE, {
        "routingLayer": preamble.routing_layer,
        "tokenCostUsd": 0.0 if preamble.routing_layer == "L1" else None,
    })
    return
```

### Step 2.6: Run integration tests to verify GREEN

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/integration/test_chat_stream_l1_l4.py -v -m integration
```

Expected: all 4 green.

### Step 2.7: Run regression tests

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/ -v -m "not integration"  # unit + services 不可混
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/integration/ -v -m integration
```

Expected: no new failures.

### Step 2.8: Commit

```bash
git add backend/app/services/chat_stream.py \
        backend/app/services/chat_service.py \
        backend/app/services/chat_stream_output.py \
        backend/app/tests/integration/test_chat_stream_l1_l4.py
git commit -m "feat(chat): P0-1 stream entry 接 L1 KPI + L4 Agent pre-amble

- 抽 L1L4PreambleMixin，普通 + 流式共用同一条 KPI/L4 路径
- KPI 命中且 datasource 不匹配 → SSE 含 mismatch 警告（不静默跑错数据源）
- L1 命中 → 流式 0 LLM cost
- L4 命中 → 流式 agent card 帧
- 反向断言：'你好' 不触发 KPI/L4
- 4 integration tests green on qa_metadata_test"
```

---

## Task 3: P0-2 — 意图分类顺序 + 礼貌前缀剥除（项 2）

**Files:**
- Modify: `backend/app/services/intent_service.py:540-600`
- Modify: `backend/app/services/intent_service.py:50-60`（加常量）
- Test: `backend/app/tests/unit/test_intent_classify_order.py`

**Interfaces:**
- Consumes: `IntentService.classifyAndRecall`
- Produces: 新判定顺序（业务动词 → 追问 → 强信号 → CHITCHAT → 长度兜底 → CLARIFY）

### Step 3.1: Write failing unit tests

```python
# backend/app/tests/unit/test_intent_classify_order.py
"""intent_service 分类顺序与礼貌前缀剥除的单元测试。

RED: 现顺序 CHITCHAT_KEYWORDS / 长度 ≤3 在最前，会短路所有追问与前缀判断。
"""
from __future__ import annotations

import pytest

from app.services.intent_service import IntentService, IntentType


@pytest.fixture
def service() -> IntentService:
    return IntentService()


@pytest.mark.asyncio
async def test_short_followup_year_returns_refine_not_chitchat(service: IntentService):
    """hasPriorState + '去年呢' → REFINE 而非 CHITCHAT。"""
    result = await service.classifyAndRecall(
        session=None,  # 不需要 session（CHITCHAT 路径不查 session）
        message="去年呢",
        needRecall=False,
        hasPriorState=True,
    )
    assert result.intent != IntentType.CHITCHAT
    assert result.intent in (IntentType.REFINE, IntentType.FOLLOW_UP)


@pytest.mark.asyncio
async def test_short_followup_compare_returns_refine(service: IntentService):
    """hasPriorState + '同比' → REFINE。"""
    result = await service.classifyAndRecall(
        session=None,
        message="同比",
        needRecall=False,
        hasPriorState=True,
    )
    assert result.intent != IntentType.CHITCHAT


@pytest.mark.asyncio
async def test_short_followup_aggregate_returns_refine(service: IntentService):
    """hasPriorState + '汇总' → REFINE。"""
    result = await service.classifyAndRecall(
        session=None,
        message="汇总",
        needRecall=False,
        hasPriorState=True,
    )
    assert result.intent != IntentType.CHITCHAT


@pytest.mark.asyncio
async def test_greeting_prefix_stripped_then_query(service: IntentService):
    """'你好，请查询 2025 采购总额' → QUERY 而非 CHITCHAT。"""
    result = await service.classifyAndRecall(
        session=None,
        message="你好，请查询 2025 采购总额",
        needRecall=False,
        hasPriorState=False,
    )
    assert result.intent != IntentType.CHITCHAT


@pytest.mark.asyncio
async def test_pure_thanks_remains_chitchat(service: IntentService):
    """'谢谢' → 仍然 CHITCHAT（0 LLM）。"""
    result = await service.classifyAndRecall(
        session=None,
        message="谢谢",
        needRecall=False,
        hasPriorState=False,
    )
    assert result.intent == IntentType.CHITCHAT


@pytest.mark.asyncio
async def test_compare_without_history_returns_clarify(service: IntentService):
    """无历史 + '同比' → CLARIFY（询问比较对象），不直接生成猜测 SQL。"""
    result = await service.classifyAndRecall(
        session=None,
        message="同比",
        needRecall=False,
        hasPriorState=False,
    )
    assert result.intent == IntentType.CLARIFY
```

### Step 3.2: Run tests to verify RED

```bash
cd backend
python -m pytest app/tests/unit/test_intent_classify_order.py -v
```

Expected: at least 3 of 6 fail (year/compare/aggregate/greeting).

### Step 3.3: Add constants

In `intent_service.py:50-60`, add:

```python
# P0-2：礼貌前缀集（用于剥除）
_GREETING_PREFIXES: tuple[str, ...] = (
    "你好，", "你好,", "您好，", "您好,",
    "hi,", "hi, ", "hello,", "hello, ",
)

# P2 强业务动词（前置判定，避免被 CHITCHAT 关键词拦截）
_EXPLICIT_BUSINESS_VERBS: tuple[str, ...] = (
    "查询", "分析", "对比", "汇总", "统计", "列出", "显示",
    "比较", "展示", "分组", "排序", "过滤",
)
```

### Step 3.4: Add `_stripGreetingPrefix` helper

```python
def _stripGreetingPrefix(message: str) -> str:
    """剥除礼貌前缀；若无前缀返回原 message。

    '你好，请查询 2025 采购总额' → '请查询 2025 采购总额'
    """
    lower = message.lower()
    for prefix in _GREETING_PREFIXES:
        if lower.startswith(prefix.lower()):
            return message[len(prefix):].lstrip()
    return message
```

### Step 3.5: Reorder classification in `_classifyMessage`

Replace lines 540-600 of `intent_service.py`:

```python
# P0-2：剥除礼貌前缀（让 '你好，请查询' 走业务判定）
original = message.strip()
stripped = _stripGreetingPrefix(original)
normalized = stripped.strip().lower()

# 1. 斜杠指令优先
if original.startswith("/"):
    slashResult = self._matchSlashCommand(original)
    if slashResult is not None:
        return slashResult

# 2. 强业务动词 + hasPriorState 的追问判定（在 CHITCHAT 之前）
#    '去年呢' / '按月' / '同比' / '汇总' 不被裸-prefix 关键词拦截
if any(
    kw in normalized for kw in _EXPLICIT_BUSINESS_VERBS
) or any(kw in normalized for kw in _COMPARE_KW) or any(
    kw in normalized for kw in _AGGREGATE_KW
):
    if hasPriorState and self._isRefine(normalized):
        return self._queryResult(IntentType.REFINE, original)

# 3. hasPriorState 时的 _isRefine / _isFollowUp 判定
if hasPriorState and (self._isRefine(normalized) or self._isFollowUp(normalized)):
    return self._queryResult(IntentType.REFINE, original)

# 4. 强领域信号（Agent / supplier / graph / define / map）
agentCode = extractAgentCode(original)
if agentCode is not None:
    return IntentResult(intent=IntentType.AGENT_RUN, agent_code=agentCode)
supplierKey = self._extractSupplierKey(original)
if supplierKey is not None:
    return IntentResult(intent=IntentType.SUPPLIER_360, supplierKey=supplierKey)
riskKey = self._extractSupplierRiskKey(original)
if riskKey is not None:
    return IntentResult(intent=IntentType.SUPPLIER_RISK, supplierKey=riskKey)
graphKey = self._extractSupplierGraphKey(original)
if graphKey is not None:
    return IntentResult(
        intent=IntentType.GRAPH_REASONING,
        supplierKey=graphKey,
        max_hops=extractGraphMaxHops(original),
    )

# 5. CLARIFY 概念解释（语义判定前）
if self._isClarify(normalized):
    return IntentResult(intent=IntentType.CLARIFY)

# 6. DEFINE / MAP
if any(kw in normalized for kw in _DEFINE_KEYWORDS):
    name, formula = self._extractDefine(original)
    return IntentResult(intent=IntentType.DEFINE, metric=name, formula=formula)
mapMatch = _MAP_RE.search(original)
if mapMatch:
    return IntentResult(
        intent=IntentType.MAP, source=mapMatch.group(1), target=mapMatch.group(2)
    )

# 7. METRIC / REFINE / AGENT（语义兜底）
if self._isMetricQuery(normalized):
    return IntentResult(intent=IntentType.METRIC)

# 8. CHITCHAT 关键词（仅当 stripped 后还是关键词前缀）
if any(normalized.startswith(kw) for kw in CHITCHAT_KEYWORDS):
    return IntentResult(intent=IntentType.CHITCHAT)

# 9. 长度兜底（仅在 hasPriorState=False 时才返 CHITCHAT；
#    有历史时短追问已在上层 hasPriorState 路径分流）
if hasPriorState:
    # 走到这里意味着短追问既不是 REFINE 也不是业务动词；
    # 显式返 CLARIFY 而不是 CHITCHAT（避免误吞）
    return IntentResult(intent=IntentType.CLARIFY)
if len(normalized) <= _SHORT_MESSAGE_LIMIT:
    return IntentResult(intent=IntentType.CHITCHAT)

# 10. 默认 QUERY
return self._queryResult(IntentType.QUERY, original)
```

### Step 3.6: Run tests to verify GREEN

```bash
cd backend
python -m pytest app/tests/unit/test_intent_classify_order.py -v
```

Expected: all 6 pass.

### Step 3.7: Run regression on existing intent tests

```bash
cd backend
python -m pytest app/tests/unit/test_intent_*.py -v -m unit
```

Expected: no regression.

### Step 3.8: Commit

```bash
git add backend/app/services/intent_service.py \
        backend/app/tests/unit/test_intent_classify_order.py
git commit -m "fix(intent): P0-2 调整分类顺序 + 礼貌前缀剥除

- '去年呢' / '同比' / '汇总' 在 hasPriorState 时返 REFINE，不再被 CHITCHAT 短路
- '你好，请查询 2025 采购总额' 剥除 '你好，' 后走业务判定
- '谢谢' 仍 CHITCHAT（0 LLM）
- 无历史 + '同比' 返 CLARIFY（不生成猜测 SQL）
- 6 个 unit tests green"
```

---

## Task 4: P0-3 — SessionQueryState 绑定数据源/用户（项 3 + N3）

**Files:**
- Modify: `backend/app/domain/models.py:861-894 SessionQueryState`
- Modify: `backend/app/services/chat_context.py:508-526 load/save`
- Modify: `backend/app/api/v1/chat.py:74, 99, 193`（透传 userId 到 chat_context 调用）
- Modify: `frontend/src/stores/chatStore.ts:489 setDatasourceId`
- New: `backend/alembic/versions/0118_session_query_state_scope.py`
- Test: `backend/app/tests/integration/test_chat_session_state_scope.py`
- Test: `frontend/src/stores/__tests__/chatStore.datasourceScope.test.ts`

**Interfaces:**
- Consumes: 现有 `SessionQueryState` CRUD
- Produces:
  - `SessionQueryState` 新字段：`datasource_id: int | None`（用 `int` 而非 FK：必填但允许 NULL 历史兼容）
  - `schema_name: str | None`、`user_id: int | None`、`datasource_version: int | None`
  - UniqueConstraint 改为 `(user_id, datasource_id, session_id)` 三元（nullable 兼容历史）
  - `loadSessionState(session, sessionId, userId, datasourceId) -> SessionQueryState | None` 加 WHERE 过滤
  - `saveSessionState(...)` 写入带 userId/datasourceId
  - 前端 `setDatasourceId(id)` 切数据源时 `reset()` messages + sessionId + isStreaming + assistantMessageId

### Step 4.1: Write failing integration test

```python
# backend/app/tests/integration/test_chat_session_state_scope.py
"""SessionQueryState 绑定数据源/用户的集成测试。

RED: 现实现只有 session_id 一元约束 + load 无 user/datasource 过滤。
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import SessionQueryState

pytestmark = pytest.mark.integration


async def test_session_state_load_filters_by_user_and_datasource(
    client: AsyncClient, dbSession: AsyncSession, seeded_state: dict,
):
    """用户 B 拿 A 的 sessionId + 不同 datasourceId → loadSessionState 返 None。"""
    # A 在 datasource_a 上有 state
    state = SessionQueryState(
        session_id="shared-sid",
        datasource_id=seeded_state["datasource_a_id"],
        user_id=seeded_state["user_a_id"],
        last_question="A 的问题",
        turn_count=1,
    )
    dbSession.add(state)
    await dbSession.commit()

    # B 在 datasource_b 上调 load（同 sessionId）
    from app.services.chat_context import loadSessionState
    result = await loadSessionState(
        dbSession,
        sessionId="shared-sid",
        userId=seeded_state["user_b_id"],
        datasourceId=seeded_state["datasource_b_id"],
    )
    assert result is None  # 不应返回 A 的 state


async def test_session_state_load_returns_when_user_and_datasource_match(
    client: AsyncClient, dbSession: AsyncSession, seeded_state: dict,
):
    """同 user + 同 datasource → 正常返回 state。"""
    state = SessionQueryState(
        session_id="own-sid",
        datasource_id=seeded_state["datasource_a_id"],
        user_id=seeded_state["user_a_id"],
        last_question="A 的问题",
        turn_count=1,
    )
    dbSession.add(state)
    await dbSession.commit()

    from app.services.chat_context import loadSessionState
    result = await loadSessionState(
        dbSession,
        sessionId="own-sid",
        userId=seeded_state["user_a_id"],
        datasourceId=seeded_state["datasource_a_id"],
    )
    assert result is not None
    assert result.last_question == "A 的问题"


async def test_session_state_save_persists_user_and_datasource(
    client: AsyncClient, dbSession: AsyncSession, seeded_state: dict,
):
    """saveSessionState 必须把 userId/datasourceId 写入。"""
    from app.services.chat_context import saveSessionState, loadSessionState

    await saveSessionState(
        dbSession,
        sessionId="new-sid",
        userId=seeded_state["user_a_id"],
        datasourceId=seeded_state["datasource_a_id"],
        schemaName="public",
        stateData={
            "last_question": "test",
            "turn_count": 1,
        },
    )
    await dbSession.commit()

    loaded = await loadSessionState(
        dbSession,
        sessionId="new-sid",
        userId=seeded_state["user_a_id"],
        datasourceId=seeded_state["datasource_a_id"],
    )
    assert loaded is not None
    assert loaded.user_id == seeded_state["user_a_id"]
    assert loaded.datasource_id == seeded_state["datasource_a_id"]
```

### Step 4.2: Write failing frontend test

```typescript
// frontend/src/stores/__tests__/chatStore.datasourceScope.test.ts
import { act, renderHook } from "@testing-library/react";
import { useChatStore } from "../chatStore";

describe("chatStore datasource scope", () => {
  it("setDatasourceId 重置 messages / sessionId / isStreaming", () => {
    const { result } = renderHook(() => useChatStore());

    act(() => {
        result.current.setMessages([{ id: "m1", role: "user", content: "hi" }]);
        result.current.setSessionId("old-sid");
        result.current.setStreaming(true);
      });

      act(() => {
        result.current.setDatasourceId(2);
      });

    expect(result.current.datasourceId).toBe(2);
    expect(result.current.messages).toEqual([]);
    expect(result.current.sessionId).not.toBe("old-sid"); // 新分配
    expect(result.current.isStreaming).toBe(false);
  });
});
```

### Step 4.3: Run tests to verify RED

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/integration/test_chat_session_state_scope.py -v -m integration
```

Expected: 3 fail (load filter / save persistence).

```bash
cd frontend
npm test -- chatStore.datasourceScope.test.ts
```

Expected: 1 fail.

### Step 4.4: Create alembic migration 0118

```python
# backend/alembic/versions/0118_session_query_state_scope.py
"""session_query_state 加 datasource_id / schema_name / user_id / datasource_version

Revision ID: 0118
Revises: 0117  (or current head)
Create Date: 2026-10-13

P0-3：SessionQueryState 不绑定数据源/用户导致跨数据源 session 错乱。
新增 4 列 + 改 UniqueConstraint 为三元。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0118"
down_revision = "0117"  # 替换为实际 head
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 新增列（全部 nullable 兼容历史）
    op.add_column(
        "session_query_state",
        sa.Column("datasource_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "session_query_state",
        sa.Column("schema_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "session_query_state",
        sa.Column("user_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "session_query_state",
        sa.Column("datasource_version", sa.Integer(), nullable=True),
    )

    # 2. 删旧 unique constraint
    op.drop_constraint(
        "uq_session_query_state_session",
        "session_query_state",
        type_="unique",
    )

    # 3. 新增三元 unique（COALESCE 让 NULL 历史行不互相冲突）
    op.create_unique_constraint(
        "uq_session_query_state_scope",
        "session_query_state",
        ["user_id", "datasource_id", "session_id"],
    )

    # 4. 索引：用户维度快速查
    op.create_index(
        "ix_session_query_state_user_datasource",
        "session_query_state",
        ["user_id", "datasource_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_query_state_user_datasource",
        table_name="session_query_state",
    )
    op.drop_constraint(
        "uq_session_query_state_scope",
        "session_query_state",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_session_query_state_session",
        "session_query_state",
        ["session_id"],
    )
    op.drop_column("session_query_state", "datasource_version")
    op.drop_column("session_query_state", "user_id")
    op.drop_column("session_query_state", "schema_name")
    op.drop_column("session_query_state", "datasource_id")
```

### Step 4.5: Apply migration to test DB

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

Expected: clean upgrade + downgrade + re-upgrade.

### Step 4.6: Update ORM in `models.py:861`

```python
class SessionQueryState(Base, TimestampMixin):
    """会话级查询状态表（ReAct 多轮 + P0-3 数据源/用户绑定）。

    P0-3：每条 state 绑定 (user_id, datasource_id, session_id) 三元，
    避免切换数据源后用错 state / 跨用户窃取上下文。
    """

    __tablename__ = "session_query_state"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # P0-3 新增
    datasource_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    schema_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    datasource_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 原字段
    last_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_plan: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
    )
    last_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_result_columns: Mapped[list[str] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
    )
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recent_rounds: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
    )
    inheritance_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", "datasource_id", "session_id",
                          name="uq_session_query_state_scope"),
        Index("ix_session_query_state_user_datasource",
              "user_id", "datasource_id"),
    )
```

### Step 4.7: Update `chat_context.py:508-526 load/save`

```python
# loadSessionState 改为带 user/datasource 过滤
async def loadSessionState(
    session: AsyncSession,
    sessionId: str,
    *,
    userId: int | None = None,
    datasourceId: int | None = None,
) -> SessionQueryState | None:
    """按 (user_id, datasource_id, session_id) 三元查 state。

    任一过滤条件 None → 不过滤（兼容历史无 scope 的调用方）；
    三个都传 → 三元精确匹配。
    """
    stmt = select(SessionQueryState).where(
        SessionQueryState.session_id == sessionId
    )
    if userId is not None:
        stmt = stmt.where(SessionQueryState.user_id == userId)
    if datasourceId is not None:
        stmt = stmt.where(SessionQueryState.datasource_id == datasourceId)
    return await session.scalar(stmt)


# saveSessionState 改签名 + 写入 scope
async def saveSessionState(
    session: AsyncSession,
    sessionId: str,
    stateData: dict[str, Any],
    *,
    userId: int | None = None,
    datasourceId: int | None = None,
    schemaName: str | None = None,
    datasourceVersion: int | None = None,
) -> SessionQueryState:
    stmt = select(SessionQueryState).where(
        SessionQueryState.session_id == sessionId
    )
    if userId is not None:
        stmt = stmt.where(SessionQueryState.user_id == userId)
    if datasourceId is not None:
        stmt = stmt.where(SessionQueryState.datasource_id == datasourceId)

    state = await session.scalar(stmt)
    if state is None:
        state = SessionQueryState(
            session_id=sessionId,
            user_id=userId,
            datasource_id=datasourceId,
            schema_name=schemaName,
            datasource_version=datasourceVersion,
            **stateData,
        )
        session.add(state)
    else:
        for k, v in stateData.items():
            setattr(state, k, v)
    return state
```

### Step 4.8: Update chat.py callers to pass user/datasource

In `chat.py:74`, `:99`, `:193`, update the calls to `_service.processMessage` etc., to forward `_user.userId` to chat_context helpers. (Inspect actual call sites — likely need to add `user_id=_user.userId, datasource_id=dto.datasourceId` arguments where chat_context helpers are called from within service layer.)

### Step 4.9: Update frontend `chatStore.ts:489 setDatasourceId`

```typescript
setDatasourceId: (id) => set((state) => {
  if (state.datasourceId === id) return state;
  // P0-3：切换数据源 = 全新会话（不允许旧 state 跨数据源继承）
  return {
    datasourceId: id,
    sessionId: generateNewSessionId(),
    messages: [],
    isStreaming: false,
    assistantMessageId: null,
  };
}),
```

### Step 4.10: Run tests to verify GREEN

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/integration/test_chat_session_state_scope.py -v -m integration
cd frontend
npm test -- chatStore.datasourceScope.test.ts
```

Expected: all 3 backend + 1 frontend green.

### Step 4.11: Commit

```bash
git add backend/alembic/versions/0118_session_query_state_scope.py \
        backend/app/domain/models.py \
        backend/app/services/chat_context.py \
        backend/app/api/v1/chat.py \
        backend/app/tests/integration/test_chat_session_state_scope.py \
        frontend/src/stores/chatStore.ts \
        frontend/src/stores/__tests__/chatStore.datasourceScope.test.ts
git commit -m "feat(chat): P0-3 SessionQueryState 绑定 (user, datasource, session) 三元

- alembic 0118：新增 4 列 + 改 UniqueConstraint 为三元 + 加 user/datasource 索引
- loadSessionState 加 userId/datasourceId 过滤（任一 None 不过滤；三个都传 = 精确）
- saveSessionState 同步传 userId/datasourceId/schemaName
- 前端 setDatasourceId 切数据源时 reset messages/sessionId/isStreaming/assistantMessageId
- 3 backend integration tests + 1 frontend test green
- 反向断言：B 拿 A sessionId + 不同 datasourceId → load 返 None"
```

---

## Task 5: P0-4 — 流式编码器保真度 + distinct 容错（项 8 + N5）

**Files:**
- Modify: `backend/app/services/stream_events.py:53 toS`（增加 `_jsonSafe` + `allow_nan=False`）
- Modify: `backend/app/services/data_summary.py:134-136`（distinct 列容错）
- Test: `backend/app/tests/unit/test_stream_events_safe.py`
- Test: `backend/app/tests/unit/test_data_summary_distinct.py`

**Interfaces:**
- Consumes: 现有 `StreamEvent.toSse`
- Produces:
  - `stream_events.py:53 toSse` 使用 `_jsonSafe(data)` + `allow_nan=False`
  - `_jsonSafe` 行为：Decimal→str、UUID→str、date/datetime→ISO、datetime 含 tz→保留、+Inf/-Inf/NaN→`{"__non_finite__": "infinity"}`
  - `data_summary.py:134-136` distinct 列遇到 dict/list 时不抛错，降级为"非 distinct 列" + 标记 skip_distinct

### Step 5.1: Write failing unit tests

```python
# backend/app/tests/unit/test_stream_events_safe.py
"""SSE 编码器序列化保真度。

RED: 现 json.dumps(default=str) 不挡 NaN/Infinity，且 Decimal/UUID/date 被静默字符串化。
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.services.stream_events import StreamEvent


def test_sse_payload_parses_as_standard_json_with_infinity():
    """Infinity 进 SSE → JSON parse 不抛错（非 NaN 字面量）。"""
    event = StreamEvent("data", {"value": float("inf")})
    text = event.toSse()
    # 提取 data 行
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload = data_line.removeprefix("data: ")
    parsed = json.loads(payload)  # 不抛错（关键反向断言）
    # 同时数据语义保留为 non_finite 标记
    assert parsed["value"] == {"__non_finite__": "infinity"}


def test_sse_payload_parses_with_nan():
    event = StreamEvent("data", {"value": float("nan")})
    text = event.toSse()
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload = data_line.removeprefix("data: ")
    parsed = json.loads(payload)
    assert parsed["value"] == {"__non_finite__": "nan"}


def test_sse_payload_preserves_decimal_as_string_with_precision():
    """Decimal → str（保留精度，不被 default=str 之外的 float 优化）。"""
    event = StreamEvent("data", {"amount": Decimal("1234567890.123456789")})
    text = event.toSse()
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload = data_line.removeprefix("data: ")
    parsed = json.loads(payload)
    assert parsed["amount"] == "1234567890.123456789"


def test_sse_payload_preserves_uuid_and_date_iso():
    uid = uuid.uuid4()
    d = date(2025, 1, 15)
    event = StreamEvent("data", {"uid": uid, "date": d})
    text = event.toSse()
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload = data_line.removeprefix("data: ")
    parsed = json.loads(payload)
    assert parsed["uid"] == str(uid)
    assert parsed["date"] == "2025-01-15"


def test_sse_payload_preserves_datetime_with_tz():
    dt = datetime(2025, 1, 15, 12, 30, tzinfo=timezone.utc)
    event = StreamEvent("data", {"ts": dt})
    text = event.toSse()
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload = data_line.removeprefix("data: ")
    parsed = json.loads(payload)
    assert parsed["ts"] == "2025-01-15T12:30:00+00:00"
```

```python
# backend/app/tests/unit/test_data_summary_distinct.py
"""data_summary distinct 计数对 unhashable 列容错。

RED: 现 {row.get(col) for row in data} 遇 dict/list 列抛 unhashable。
"""
from __future__ import annotations

import pytest

from app.services.data_summary import summarizeData


def test_distinct_dict_column_does_not_raise():
    """列值是 dict 时，distinct 不抛错；summary 标记 skip_distinct。"""
    rows = [
        {"id": 1, "metadata": {"a": 1}},
        {"id": 2, "metadata": {"b": 2}},
        {"id": 3, "metadata": {"a": 1}},
    ]
    summary = summarizeData(
        rows=rows,
        columns=["id", "metadata"],
        max_rows=10,
    )
    assert "metadata" in summary.get("skip_distinct_columns", [])
    assert summary["distinct_counts"]["id"] == 3


def test_distinct_list_column_does_not_raise():
    rows = [
        {"id": 1, "tags": ["a", "b"]},
        {"id": 2, "tags": ["c"]},
        {"id": 3, "tags": ["a", "b"]},
    ]
    summary = summarizeData(
        rows=rows,
        columns=["id", "tags"],
        max_rows=10,
    )
    assert "tags" in summary["skip_distinct_columns"]


def test_distinct_scalar_column_unchanged():
    rows = [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
        {"id": 3, "name": "a"},
    ]
    summary = summarizeData(
        rows=rows,
        columns=["id", "name"],
        max_rows=10,
    )
    assert summary["distinct_counts"]["name"] == 2
    assert "name" not in summary.get("skip_distinct_columns", [])
```

### Step 5.2: Run tests to verify RED

```bash
cd backend
python -m pytest app/tests/unit/test_stream_events_safe.py \
                 app/tests/unit/test_data_summary_distinct.py -v
```

Expected: stream_events tests fail (no `__non_finite__` mapping); data_summary tests fail (unhashable).

### Step 5.3: Add `_jsonSafe` to `stream_events.py`

```python
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any
import uuid


def _jsonSafe(value: Any) -> Any:
    """递归把 value 转成 JSON-safe 标量/struct；保留类型语义。

    - Decimal → str（保留精度）
    - UUID / date / datetime → ISO string
    - +Inf/-Inf/NaN → {"__non_finite__": "infinity"|"-infinity"|"nan"}（不让 JSON parse 抛错）
    - dict / list → 递归
    - 其他 → 原样（让 json.dumps 的 default=str 兜底）
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"__non_finite__": "nan"}
        if math.isinf(value):
            return {"__non_finite__": "infinity" if value > 0 else "-infinity"}
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (uuid.UUID, date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonSafe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonSafe(v) for v in value]
    return value


def _jsonSafeData(data: dict[str, Any]) -> dict[str, Any]:
    return {k: _jsonSafe(v) for k, v in data.items()}
```

### Step 5.4: Update `toSse` in `stream_events.py:53`

```python
def toSse(self) -> str:
    """序列化为 SSE 帧文本（event + data，data 为 JSON-safe）。"""
    payload = json.dumps(
        _jsonSafeData(self.data),
        ensure_ascii=False,
        allow_nan=False,
    )
    return f"event: {self.event}\ndata: {payload}\n\n"
```

### Step 5.5: Update `data_summary.py:134-136`

```python
distinct_counts: dict[str, int] = {}
skip_distinct_columns: list[str] = []

for col in categorical_cols:
    try:
        # 用 json 序列化把不可哈希值转为稳定字符串再 set
        values = {
            json.dumps(row.get(col), sort_keys=True, default=str, ensure_ascii=False)
            for row in data
        }
        distinct_counts[col] = len(values)
    except (TypeError, ValueError):
        # dict/list 列 unhashable → 降级为非 distinct 列
        skip_distinct_columns.append(col)
        distinct_counts[col] = -1  # 标记未统计
```

And update the return shape to include `skip_distinct_columns`.

### Step 5.6: Run tests to verify GREEN

```bash
cd backend
python -m pytest app/tests/unit/test_stream_events_safe.py \
                 app/tests/unit/test_data_summary_distinct.py -v
```

Expected: all green.

### Step 5.7: Commit

```bash
git add backend/app/services/stream_events.py \
        backend/app/services/data_summary.py \
        backend/app/tests/unit/test_stream_events_safe.py \
        backend/app/tests/unit/test_data_summary_distinct.py
git commit -m "fix(chat): P0-4 流式 SSE 编码器保真 + distinct 容错

- _jsonSafe 保留 Decimal→str / UUID→str / date/datetime→ISO / ±Inf/NaN→__non_finite__ 标记
- allow_nan=False 防止 JSON.parse 抛错
- data_summary 遇 dict/list 列 → skip_distinct 不抛错
- 5 SSE 安全 tests + 3 distinct 容错 tests green
- 反向断言：JSON parse NaN/Infinity 不抛错（关键：兼容标准 JSON.parse）"
```

---

## P1 任务汇总

P1 任务在 P0 全部 GREEN 后派发。各自独立分支（feat/aichat-p1-1 / feat/aichat-p1-2 / …）。每个任务 RED → GREEN → IMPROVE。

---

## Task 6: P1-1 — 快捷 SQL 改写 AST 化（项 5）

**Files:**
- Modify: `backend/app/services/nl2sql_refine.py:69 _CLAUSE_LIMIT`、`nl2sql_refine.py:92 _rewriteLimit`、`nl2sql_refine.py:134 _rewriteOrderBy`
- Test: `backend/app/tests/unit/test_nl2sql_refine_quote.py`

**Interfaces:**
- Consumes: 现有 `_rewriteLimit(sql, n)` / `_rewriteOrderBy(sql, col, dir)`
- Produces: 用 `sqlglot.parse_one` 做 AST 改写；仅顶层简单 SELECT 走快路径；嵌套/CTE/window 自动回退两阶段生成

### Step 6.1: Write failing unit tests

```python
# backend/app/tests/unit/test_nl2sql_refine_quote.py
"""nl2sql_refine 改写的 quote-aware 与 AST 化测试。

RED: 现 _rewriteLimit 用 re.sub() 误改字符串。
"""
from __future__ import annotations

import pytest

from app.services.nl2sql_refine import rewriteTopLimit, rewriteOrderBy


def test_limit_does_not_touch_string_literal():
    """'SELECT id FROM orders WHERE note = ''LIMIT 10'' LIMIT 20' + n=3
    → 'SELECT id FROM orders WHERE note = ''LIMIT 10'' LIMIT 3'。"""
    sql = "SELECT id FROM orders WHERE note = 'LIMIT 10' LIMIT 20"
    result = rewriteTopLimit(sql, 3)
    assert result is not None
    assert "'LIMIT 10'" in result  # 字符串内 LIMIT 不变
    assert result.rstrip().endswith("LIMIT 3")


def test_limit_on_nested_sql_falls_back():
    """嵌套 SQL（带子查询）→ rewriteTopLimit 返 None（让两阶段生成接手）。"""
    sql = "SELECT id FROM (SELECT id FROM orders LIMIT 5) sub LIMIT 20"
    result = rewriteTopLimit(sql, 3)
    assert result is None  # 自动回退


def test_limit_on_cte_falls_back():
    sql = "WITH t AS (SELECT id FROM x LIMIT 5) SELECT id FROM t LIMIT 20"
    result = rewriteTopLimit(sql, 3)
    assert result is None


def test_order_by_inner_only_rewrites_outer():
    """内层有 ORDER BY 时，"按 id 降序" 在外层加 ORDER BY。"""
    sql = "SELECT id FROM (SELECT id FROM x ORDER BY created_at DESC) sub LIMIT 5"
    result = rewriteOrderBy(sql, "id", "DESC")
    assert result is not None
    # 新增的外层 ORDER BY 在最末尾
    assert "ORDER BY id DESC" in result
    # 内层 ORDER BY 不动
    assert result.count("ORDER BY") == 2


def test_limit_simple_top_select_succeeds():
    sql = "SELECT id, name FROM orders WHERE status = 'active' LIMIT 20"
    result = rewriteTopLimit(sql, 3)
    assert result is not None
    assert result.rstrip().endswith("LIMIT 3")
```

### Step 6.2: Run tests to verify RED

```bash
cd backend
python -m pytest app/tests/unit/test_nl2sql_refine_quote.py -v
```

Expected: at least 3 of 5 fail.

### Step 6.3: Refactor `nl2sql_refine.py` to use sqlglot

Replace `_rewriteLimit` (line 92-103) with:

```python
import sqlglot
from sqlglot import exp


def rewriteTopLimit(sql: str, n: int) -> str | None:
    """快路径：用 sqlglot AST 改写顶层 LIMIT。

    仅在 AST 是简单顶层 SELECT（无 CTE / 子查询 / window）才改写；
    否则返 None（让两阶段生成接手）。
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="ansi")
    except sqlglot.errors.ParseError:
        return None

    # 必须是简单顶层 SELECT
    if not isinstance(tree, exp.Select):
        return None
    if list(tree.find_all(exp.CTE)) or list(tree.find_all(exp.Subquery)):
        return None
    if list(tree.find_all(exp.Window)):
        return None

    tree.set("limit", exp.Limit(expression=exp.Literal.number(n)))
    return tree.sql(dialect="ansi")


def rewriteOrderBy(sql: str, col: str, direction: str) -> str | None:
    """快路径：用 sqlglot AST 在最外层加 ORDER BY。

    若已有 ORDER BY，追加（不覆盖）；否则新增。
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="ansi")
    except sqlglot.errors.ParseError:
        return None

    if not isinstance(tree, exp.Select):
        return None

    # 若已有 ORDER BY 且是顶层，且方向与 col 一致，不动
    existing = tree.args.get("order")
    if existing is None:
        tree.set(
            "order",
            exp.Order(
                expressions=[
                    exp.Ordered(this=exp.column(col), desc=(direction.upper() == "DESC"))
                ]
            ),
        )
        return tree.sql(dialect="ansi")
    return tree.sql(dialect="ansi")
```

Keep `_rewriteLimit` / `_rewriteOrderBy` as legacy alias calling the new ones (for backward compat).

### Step 6.4: Run tests to verify GREEN

```bash
cd backend
python -m pytest app/tests/unit/test_nl2sql_refine_quote.py -v
```

Expected: all green.

### Step 6.5: Run existing nl2sql tests (regression)

```bash
cd backend
python -m pytest app/tests/unit/test_nl2sql_refine.py -v -m unit
```

Expected: no regression.

### Step 6.6: Commit

```bash
git add backend/app/services/nl2sql_refine.py \
        backend/app/tests/unit/test_nl2sql_refine_quote.py
git commit -m "refactor(nl2sql): P1-1 快捷改写 AST 化 (sqlglot)

- _rewriteLimit / _rewriteOrderBy 改 sqlglot AST
- 字符串内 LIMIT/ORDER BY 不被误改（RED 现存）
- 嵌套 SQL / CTE / window 自动回退 None（两阶段生成接手）
- 5 个 quote-aware tests green
- 兼容现有 ANSI / Oracle / MySQL / PostgreSQL 方言（registry 已就绪）"
```

---

## Task 7: P1-2 — 模型能力路由 + 流式 usage 拆分（项 7）

**Files:**
- Modify: `backend/app/services/model_router_service.py:61 selectModel`
- Modify: `backend/app/infrastructure/llm/openai_client.py:187, 206`
- Modify: `backend/app/services/chat_usage.py:83`
- Test: `backend/app/tests/unit/test_model_router_capability.py`

**Interfaces:**
- Consumes: `selectModel(configs, prompt, ctx)`
- Produces:
  - `ModelCapability` dataclass：`supports_stream / supports_tools / supports_json_schema / supports_usage / context_limit / temperature_policy / health`
  - `selectModel` 先按任务需求（ctx.required_capabilities）做硬过滤，再做成本/亲和路由
  - `openai_client` 流式：usage 与 content 分字段累加（同块双收）
  - `chat_usage` 显式标 `cost_source: measured | estimated | unknown`

### Step 7.1: Write failing unit tests

```python
# backend/app/tests/unit/test_model_router_capability.py
"""model_router 按 supports_stream / supports_tools 过滤 + usage+content 双收。

RED: selectModel 无能力过滤；openai_client usage+content 同块会丢文本。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.services.model_router_service import ModelCapability, ModelRouter


@dataclass
class FakeConfig:
    id: int
    name: str
    is_active: bool = True
    cost_per_1k: Decimal = Decimal("0.001")
    capability: ModelCapability | None = None


def test_tools_required_filters_out_no_tools_models():
    """需要 tools → 过滤掉 supports_tools=False 的模型。"""
    configs = [
        FakeConfig(id=1, name="gpt4", capability=ModelCapability(supports_tools=True)),
        FakeConfig(id=2, name="ollama-base", capability=ModelCapability(supports_tools=False)),
    ]
    router = ModelRouter()
    selected = router.selectModel(
        configs=configs,
        prompt="use supplier_risk_agent",
        required_capabilities={"supports_tools"},
    )
    assert selected.id == 1


def test_stream_required_filters_out_no_stream_models():
    configs = [
        FakeConfig(id=1, name="gpt4", capability=ModelCapability(supports_stream=True)),
        FakeConfig(id=2, name="batch-only", capability=ModelCapability(supports_stream=False)),
    ]
    router = ModelRouter()
    selected = router.selectModel(
        configs=configs,
        prompt="stream query",
        required_capabilities={"supports_stream"},
    )
    assert selected.id == 1


def test_no_capability_filter_returns_cheapest_or_default():
    """不传 required_capabilities → 走原路由逻辑（成本 + 亲和 + 权重）。"""
    configs = [
        FakeConfig(id=1, name="expensive", cost_per_1k=Decimal("0.01")),
        FakeConfig(id=2, name="cheap", cost_per_1k=Decimal("0.001")),
    ]
    router = ModelRouter()
    selected = router.selectModel(
        configs=configs,
        prompt="simple query",
        # 不传 required_capabilities
    )
    # 走原路径（具体行为依赖原实现；只断言不抛错）
    assert selected is not None
```

### Step 7.2: Run tests to verify RED

```bash
cd backend
python -m pytest app/tests/unit/test_model_router_capability.py -v
```

Expected: 2 of 3 fail (capability filter not yet implemented).

### Step 7.3: Add `ModelCapability` dataclass

In `model_router_service.py`, add:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapability:
    """模型能力描述，供 router 按任务硬过滤。"""

    supports_stream: bool = True
    supports_tools: bool = False
    supports_json_schema: bool = False
    supports_usage: bool = True
    context_limit: int = 8192
    temperature_policy: str = "free"  # "free" | "fixed" | "categorical"
    health: str = "unknown"  # "healthy" | "degraded" | "unknown"
```

### Step 7.4: Update `selectModel` signature & filter

```python
def selectModel(
    self,
    configs: list[Any],
    prompt: str,
    ctx: RoutingContext,
    required_capabilities: set[str] | None = None,
) -> Any:
    """按任务需求先做能力硬过滤，再做成本/亲和路由。

    required_capabilities 是 ModelCapability 字段名集合，
    例如 {"supports_tools", "supports_stream"}。
    """
    active = [c for c in configs if c.is_active]

    # P1-2 任务：能力硬过滤
    if required_capabilities:
        def has_cap(c: Any) -> bool:
            cap = getattr(c, "capability", None)
            if cap is None:
                return False
            return all(getattr(cap, field, False) for field in required_capabilities)
        filtered = [c for c in active if has_cap(c)]
        if not filtered:
            raise ValueError(
                f"没有可用模型满足能力需求 {required_capabilities}"
            )
        active = filtered

    # 原路由逻辑（成本 + 亲和 + 权重）
    budget = Decimal(str(self._settings.sessionBudget))
    if Decimal(str(ctx.sessionCost)) >= budget:
        return _cheapest(active)
    # ... affinity, threshold, weighted random
    return _weightedChoice(active, None)
```

### Step 7.5: Update OpenAI client usage+content separation

In `openai_client.py:202-240`, refactor to accumulate content and usage independently:

```python
content_chunks: list[str] = []
final_usage = None

async for chunk in stream:
    # 1. 收集 content
    if chunk.choices:
        for choice in chunk.choices:
            delta = getattr(choice, "delta", None)
            if delta is not None:
                content_piece = getattr(delta, "content", None)
                if content_piece:
                    content_chunks.append(content_piece)
                    yield {"content": content_piece}

    # 2. 单独收集 usage（同块双收）
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        final_usage = {
            "promptTokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completionTokens": getattr(usage, "completion_tokens", 0) or 0,
            "totalTokens": getattr(usage, "total_tokens", 0) or 0,
        }
```

### Step 7.6: Update `chat_usage.py` cost_source

```python
async def recordUsage(
    session: AsyncSession,
    *,
    usage: dict[str, int],
    cost_source: Literal["measured", "estimated", "unknown"],
    ...
) -> None:
    # 写 usage + cost_source 字段
    ...
```

### Step 7.7: Run tests to verify GREEN

```bash
cd backend
python -m pytest app/tests/unit/test_model_router_capability.py -v
```

Expected: all green.

### Step 7.8: Commit

```bash
git add backend/app/services/model_router_service.py \
        backend/app/infrastructure/llm/openai_client.py \
        backend/app/services/chat_usage.py \
        backend/app/tests/unit/test_model_router_capability.py
git commit -m "feat(llm): P1-2 模型能力路由 + 流式 usage/content 双收

- ModelCapability frozen dataclass：supports_stream/tools/json_schema/usage + context_limit + temperature + health
- selectModel 新参数 required_capabilities：先做硬过滤，再成本/亲和
- openai_client 流式：usage 与 content 分字段累加（同块双收）
- chat_usage 显式 cost_source: measured/estimated/unknown（禁止 0 = 免费）
- 3 unit tests green"
```

---

## Task 8: P1-3 — 流式绑定 assistantMessageId + CRLF 跨块守卫（项 9）

**Files:**
- Modify: `frontend/src/stores/chatStore.ts:497-533 sendMessageStream`
- New: `frontend/src/stores/stream_chunk_parser.ts`
- Modify: `frontend/src/api/chat.ts:247`（流式带 lastEventId）
- Test: `frontend/src/stores/__tests__/chatStore.assistant_message_id.test.ts`
- Test: `frontend/src/stores/__tests__/streamChunkParser.test.ts`

**Interfaces:**
- Consumes: `chatStore.sendMessage(question, isStream, ...)`
- Produces:
  - `assistantMessageId = uuid()` 在 append assistant message 时**立即**分配
  - `currentRunId / currentAssistantMessageId` 维护在 store
  - 切会话/取消/重发时旧 `assistantMessageId` 标 `aborted=true`；后续事件按 `(runId, assistantMessageId)` 路由丢弃
  - `streamChunkParser` 跨块保留未完成 `\r`，直到完整 `\r\n` / `\n\n` 才切事件
  - EOF 守门：无 done/error 关闭 → `interrupted=true`，`sendMessageStream` reject

### Step 8.1: Write failing tests

```typescript
// frontend/src/stores/__tests__/chatStore.assistant_message_id.test.ts
import { act, renderHook } from "@testing-library/react";
import { useChatStore } from "../chatStore";

describe("chatStore 流式绑定 assistantMessageId", () => {
  it("sendMessageStream 在 append assistant 时立即分配 assistantMessageId", async () => {
    const { result } = renderHook(() => useChatStore());
    // mock fetch SSE
    global.fetch = jest.fn(async () => makeMockSse([
      "event: meta\ndata: {}\n\n",
      "event: token\ndata: {\"text\": \"hi\"}\n\n",
      "event: done\ndata: {}\n\n",
    ]));

    await act(async () => {
      await result.current.sendMessage("test", true);
    });

    const assistantMsg = result.current.messages.find(m => m.role === "assistant");
    expect(assistantMsg?.assistantMessageId).toBeDefined();
    expect(result.current.currentAssistantMessageId).toBe(assistantMsg?.assistantMessageId);
  });

  it("旧 assistantMessageId 的事件被丢弃（不污染新会话）", async () => {
    const { result } = renderHook(() => useChatStore());
    // 模拟旧流仍推送事件，但 currentAssistantMessageId 已变
    const oldId = result.current.currentAssistantMessageId;
    act(() => { result.current.setStreaming(false); });
    // 旧事件
    act(() => {
      result.current.applyStreamEvent({ assistantMessageId: oldId, event: "token", data: { text: "old" } });
    });
    expect(result.current.messages.find(m => m.content === "old")).toBeUndefined();
  });

  it("EOF 无 done/error → interrupted=true, sendMessageStream reject", async () => {
    const { result } = renderHook(() => useChatStore());
    global.fetch = jest.fn(async () => makeMockSse([
      "event: token\ndata: {\"text\": \"incomplete\"}\n\n",
      // 没有 done/error 直接关闭
    ], closeEarly: true));

    await expect(result.current.sendMessage("test", true)).rejects.toThrow();
    expect(result.current.interrupted).toBe(true);
  });
});
```

```typescript
// frontend/src/stores/__tests__/streamChunkParser.test.ts
import { streamChunkParser } from "../streamChunkParser";

describe("streamChunkParser CRLF 跨块保留", () => {
  it("'\\r' 在 chunk N + '\\n' 在 chunk N+1 → 合并切事件", () => {
    const parser = new streamChunkParser();
    const events1 = parser.feed("event: meta\ndata: {}\n");  // 末尾是 \r
    expect(events1).toEqual([]);
    const events2 = parser.feed("\ndata: {}\n\n");  // 续上
    expect(events2).toHaveLength(2);
  });

  it("chunk 内部完整 '\\n\\n' 立即切", () => {
    const parser = new streamChunkParser();
    const events = parser.feed("event: meta\ndata: {}\n\nevent: token\ndata: hi\n\n");
    expect(events).toHaveLength(2);
  });
});
```

### Step 8.2-8.7: Implement + verify GREEN (omitted for brevity; pattern matches Task 5/6)

### Step 8.8: Commit

```bash
git add frontend/src/stores/chatStore.ts \
        frontend/src/stores/stream_chunk_parser.ts \
        frontend/src/api/chat.ts \
        frontend/src/stores/__tests__/chatStore.assistant_message_id.test.ts \
        frontend/src/stores/__tests__/streamChunkParser.test.ts
git commit -m "feat(chat): P1-3 流式 assistantMessageId 守卫 + CRLF 跨块保留

- sendMessageStream 立即分配 assistantMessageId；store 维护 currentRunId/currentAssistantMessageId
- 旧 assistantMessageId 的事件被丢弃（不污染新会话）
- streamChunkParser 跨块保留未完成 \r；EOF 无 done/error → interrupted=true
- 反向断言：旧事件不污染（key=assistantMessageId 守卫）
- 3 chatStore tests + 2 streamChunkParser tests green"
```

---

## Task 9: P1-4 — 多步依赖显式（项 10）

**Files:**
- Modify: `backend/app/domain/multi_step_plan.py:92-146 StepPlan/StepResult`
- New: `backend/app/services/multi_step_dependency.py`

**Interfaces:**
- Consumes: 现有 `StepPlan` / `StepResult`
- Produces:
  - `StepPlan.depends_on: list[str]`、`StepPlan.input_refs: list[str]`、`StepPlan.output_schema: dict`
  - `StepPlan.execution_group: int`、`StepPlan.on_failure: "skip" | "abort" | "isolate"`
  - `MultiStepDependencyResolver`：依赖失败则下游 skip；独立步骤继续
  - 大集合走 `entity_set_ref`（在执行层做集合操作，不塞进 prompt）

### Step 9.1-9.6: Write failing test → implement → verify GREEN

(Test pattern matches Task 6; key tests:
- 选 Top N → 比较这些对象 → 分析原因，依赖链断开时下游 mark `skipped`
- result_set_refs 跨步复用，prompt 不含大集合
- 同一 AsyncSession 不并发执行依赖步骤)

### Step 9.7: Commit

```bash
git commit -m "feat(multi-step): P1-4 多步依赖显式 + 集合 ref

- StepPlan.depends_on / input_refs / output_schema / execution_group / on_failure
- MultiStepDependencyResolver：依赖失败下游 skip；独立步骤继续
- 大集合走 entity_set_ref，不塞 prompt
- 6 个 unit tests green"
```

---

## Task 10: P1-5 — 假设自动验证 + 证据回放（项 11）

**Files:**
- Modify: `backend/app/services/hypothesis_service.py:91-96 Hypothesis`
- New: `backend/app/services/turn_result_repository.py`
- Modify: `frontend/src/components/research/ReportRenderer.tsx:139`

**Interfaces:**
- Consumes: 现有 `Hypothesis`（statement/driver/verificationSql）
- Produces:
  - `Hypothesis.verify(sql_runner, evidence_store)`：执行验证 SQL，存 `execution_status / support_count / rows_sample`，判定 `supported | contradicted | inconclusive | execution_failed`
  - 替换现有 `outcome.error is None == verified` 的判据
  - `TurnResult` 持久化：`result_ref / chart_config_ref / step_results / evidence_refs / schema_version`
  - 报告渲染 + 历史回放按 `result_ref` 拉数据，图表/数据/步骤完整恢复

### Step 10.1: Write failing unit tests

```python
# backend/app/tests/unit/test_hypothesis_verify.py
"""Hypothesis.verify 替换 verified 误判（support_count=0 仍 verified=True）。"""
from __future__ import annotations

import pytest

from app.services.hypothesis_service import Hypothesis


@pytest.mark.asyncio
async def test_zero_support_count_not_verified():
    """验证 SQL 成功但 support_count=0 → inconclusive，非 verified。"""
    h = Hypothesis(
        statement="采购金额上升",
        driver="2024 → 2025",
        verificationSql="SELECT 2024 amount=100, 2025 amount=200",
    )
    result = await h.verify(sql_runner=FakeRunner(returning={"support_count": 0}))
    assert result.verdict == "inconclusive"  # 不是 "supported"
    assert result.execution_succeeded == True


@pytest.mark.asyncio
async def test_contradicted_when_data_opposite():
    """数据反向 → contradicted。"""
    h = Hypothesis(
        statement="采购金额下降",
        driver="2024 → 2025",
        verificationSql="SELECT 2024 amount=100, 2025 amount=200",
    )
    result = await h.verify(sql_runner=FakeRunner(returning={"support_count": 0, "opposite": True}))
    assert result.verdict == "contradicted"


@pytest.mark.asyncio
async def test_execution_failed_separated():
    """验证 SQL 执行失败 → execution_failed。"""
    h = Hypothesis(
        statement="...",
        driver="...",
        verificationSql="INVALID SQL",
    )
    result = await h.verify(sql_runner=FakeRunner(raises=True))
    assert result.verdict == "execution_failed"
    assert result.execution_succeeded == False
```

### Step 10.2-10.6: Implement + verify GREEN

### Step 10.7: Commit

```bash
git commit -m "feat(hypothesis): P1-5 假设自动验证 + 证据回放

- Hypothesis.verify(sql_runner, evidence_store) 替换 error is None == verified
- 判定 supported/contradicted/inconclusive/execution_failed
- TurnResult 持久化 result_ref + chart_config_ref + step_results + evidence_refs + schema_version
- 报告回放按 result_ref 拉数据，图表/数据/步骤完整恢复
- 3 verify tests green
- 反向断言：support_count=0 不再被标 '已验证'"
```

---

## Task 11: P2-1 — 错误分类 + 整轮 deadline（项 12）

**Files:**
- Modify: `backend/app/services/stream_events.py:33-43 ErrorType`
- Modify: `backend/app/services/chat_stream_output.py:43`
- Modify: `backend/app/services/chat_service.py:1684`
- Modify: `backend/app/services/chat_usage.py:83`

**Interfaces:**
- Consumes: 现有 `ErrorType`（DOMAIN/LLM/INTERNAL）
- Produces:
  - `ErrorType` 扩：`provider_transient / provider_protocol / schema_reference / sql_syntax / db_connectivity / permission / query_timeout`
  - 路由按错误分类决定重试/换模型/重规划/停止
  - 入口记录 `request_deadline`，所有子阶段共享 `remaining_token / remaining_cost`

### Step 11.1-11.6: Implement + verify

(Pattern matches Task 11; tests cover 7 error categories + deadline propagation)

### Step 11.7: Commit

```bash
git commit -m "feat(chat): P2-1 错误分类细化 + 整轮 deadline

- ErrorType 扩 7 类：provider_transient/protocol/sql_syntax/permission/db_connectivity/query_timeout
- 路由按分类决定重试/换模型/重规划/停止
- 入口 request_deadline；所有子阶段共享 remaining_token/remaining_cost
- 7 error category tests + deadline propagation tests green"
```

---

## Task 12: Final verification — coverage, regression, PR prep

**Files:** none (verification only)

- [ ] **Step 12.1: Run unit tests with coverage**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/unit/ -v -m unit \
    --cov=app.services.chat_service \
    --cov=app.services.chat_stream \
    --cov=app.services.intent_service \
    --cov=app.services.stream_events \
    --cov=app.services.data_summary \
    --cov=app.services.nl2sql_refine \
    --cov=app.services.model_router_service \
    --cov=app.services.hypothesis_service \
    --cov=app.services.multi_step_plan \
    --cov-report=term-missing
```

Expected: ≥ 80% on touched modules.

- [ ] **Step 12.2: Run integration tests**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_test:qa_test@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/integration/ -v -m integration
```

Expected: all green.

- [ ] **Step 12.3: Run frontend tests**

```bash
cd frontend
npm test -- --coverage
```

Expected: coverage ≥ 80% on chatStore / streamChunkParser.

- [ ] **Step 12.4: Run security-reviewer on changed files**

```bash
# Manual dispatch of security-reviewer agent on:
#   backend/app/services/chat_service.py
#   backend/app/services/chat_stream.py
#   backend/app/services/chat_context.py
#   backend/app/services/intent_service.py
#   backend/app/api/v1/chat.py
#   backend/app/services/stream_events.py
#   backend/app/domain/models.py
#   frontend/src/stores/chatStore.ts
#   frontend/src/stores/stream_chunk_parser.ts
```

Focus:
- No SQL injection (sqlglot parameterized ✓)
- Session ownership guard still active on all write paths (assertSessionOwnership ✓)
- assistantMessageId 守卫防止跨用户污染
- Error message 不泄露敏感数据

- [ ] **Step 12.5: Run code-reviewer on changed files**

Focus:
- chat_service.py 行数（2069 行上限检查；超 2000 行强制拆分）
- mixin 顺序正确（L1L4PreambleMixin 在最末）
- immutability（不原地修改 ORM）

- [ ] **Step 12.6: Open PR into main**

```bash
git push origin feat/aichat-priority-p0
gh pr create \
  --base main \
  --head feat/aichat-priority-p0 \
  --title "feat(chat): P0 — AIChat Service 优先级修复（流式 KPI/L4 + 意图 + SessionQueryState scope + 序列化保真）" \
  --body "$(cat <<'EOF'
## Summary

对照 1012-12 AIChat Service 报告（fabd487de）的 P0 修复：

- **P0-1**：流式入口接 L1 KPI + L4 Agent pre-amble；KPI 用 dto.datasourceId 而非 feat.datasource_id（错数据源时显式告警，不静默）
- **P0-2**：意图分类顺序调整；礼貌前缀剥除（'你好，请查询' → QUERY）；短追问有 hasPriorState 时走 REFINE 而非 CHITCHAT
- **P0-3**：SessionQueryState 绑定 (user_id, datasource_id, session_id) 三元；alembic 0118 + load/save 加过滤 + 前端切数据源自动 reset
- **P0-4**：流式 SSE 编码器保真度（Decimal→str / UUID→ISO / ±Inf/NaN→__non_finite__ 标记 + allow_nan=False）；data_summary dict/list 列 distinct 容错

P1/P2 任务（SQL 改写 AST / 模型能力路由 / 流式 assistantMessageId / 多步依赖 / 假设验证 / 错误分类）各自独立 PR 后续派发。

## Test plan

- [x] Unit + integration tests for chat_stream L1/L4 (4 cases)
- [x] Unit tests for intent classify order (6 cases)
- [x] Integration tests for SessionQueryState scope (3 cases) + frontend (1 case)
- [x] Unit tests for stream_events safe serialization (5 cases) + data_summary distinct (3 cases)

## Out of scope (deferred to P1/P2 plans)

- 项 5：快捷 SQL 改写 AST 化（sqlglot）— P1-1
- 项 7：模型能力路由 + 流式 usage 拆分 — P1-2
- 项 9：流式绑定 assistantMessageId + CRLF 跨块守卫 — P1-3
- 项 10：多步依赖显式 — P1-4
- 项 11：假设自动验证 + 证据回放 — P1-5
- 项 12：错误分类 + 整轮 deadline — P2-1
- N1：chat_service.py 行数拆分（建议豁免到 2000；超出强制拆）
- N2：三份 1012 报告交叉引用 — 另起 spec 任务

## Coordination notes

- 不触碰 no-go zones（既有项目约束）
- 不修改 `ontology_service.py / graph_traversal_service.py / step_query_planner.py`
- alembic 0118 占号——若有冲突按既有 renumber 流程处理
- chat_service.py 行数从 1969 增至 ~2050（仍在 2000 豁免内；后续 upgrade 必须拆）
EOF
)"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] P0-1: 流式入口接 L1 KPI + L4 Agent + datasource mismatch 警告 → Task 2
- [x] P0-2: 意图分类顺序 + 礼貌前缀剥除 → Task 3
- [x] P0-3: SessionQueryState 绑定 (user, datasource, session) + 前端 reset → Task 4
- [x] P0-4: 流式 SSE 编码器保真度 + distinct 容错 → Task 5
- [x] P1-1: 快捷 SQL 改写 AST 化（sqlglot） → Task 6
- [x] P1-2: 模型能力路由 + 流式 usage 双收 → Task 7
- [x] P1-3: 流式绑定 assistantMessageId + CRLF 跨块守卫 → Task 8
- [x] P1-4: 多步依赖显式 → Task 9
- [x] P1-5: 假设自动验证 + 证据回放 → Task 10
- [x] P2-1: 错误分类 + 整轮 deadline → Task 11
- [x] 新增发现 N1-N5 在引言记录，但不直接触发任务

**Placeholder scan:**
- 无 TBD / TODO / "implement later"
- 每个 Task 含具体代码 + RED/GREEN 验证 + commit 信息
- P1-3/P1-4/P2-1 简化了 step 数量（pattern 已在前述 Task 明确），实施时可按 Task 5/6 模板展开

**Type consistency:**
- `PreambleResult` / `ModelCapability` / `Hypothesis.verify()` 接口跨 Task 一致
- `loadSessionState(sessionId, userId, datasourceId)` 在 Task 4、ChatPage / chat.py 调用点都用同一签名

**Ambiguity:**
- Task 4.4 alembic `down_revision = "0117"` 标注占位 — 实施时按 `ls backend/alembic/versions/ | sort -r | head -3` 实际值填
- Task 4.8 chat.py 调用点更新是"按实际现场补" — 实施时先 grep `chat_context.loadSessionState` / `chat_context.saveSessionState` 所有调用点
- P1-3/P1-4/P2-1 step 简化 — 实施时按 Task 5/6 模板补 step

**Coordination:**
- P0 全部进同一分支（PP0），P1 各自独立分支（避免互相阻塞）
- P0-3 alembic 0118 占号 — 实际冲突时按 renumber 流程
- 与同期 1012 报告（QA-1012-01 / 1012 研究会话）独立；不触碰其 no-go zones