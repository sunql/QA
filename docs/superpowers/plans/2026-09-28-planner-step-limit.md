# A6 · Planner 步数硬限 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「拆步超限」从**静默截断**改成**显式拒收 + 固定提示**，覆盖流式与非流式两条路径，并堵上规则快路径的无上限旁路。

**Architecture:** 拆成两半，各自单一职责——
**planner 如实上报**（去掉截断、去掉 prompt 里的自限），**执行缝受不受理由**（`_executeMultiStep` / `_streamMultiStep` 入口的守门谓词）。这么分的**唯一理由**是拒收文案必须说出*真实*步数（「该问题需 N 步」）；只要 planner 还在自行截断，那个 N 就永远是 4，提示形同虚设。执行缝收口也顺带让规则快路径（`plan_explicit`）自动落到同一闸门，无需单独改。

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy 2.0 async / pytest（unit + 真实 PG integration）

## Global Constraints

- **不可变数据**：始终创建新对象，禁止原地修改（本项目核心约束 #1）。
- **Token 计量**：每次 LLM 调用必须记录 Token 与成本（核心约束 #3）。本任务**不新增任何 LLM 调用**；拒收路径必须在**任何** LLM 调用之前返回。
- **SQL 安全**：业务查询仅允许只读 SELECT（核心约束 #2）。拒收路径**不得**生成或执行任何 SQL。
- **TDD**：先写测试（RED）→ 实现（GREEN）→ 重构（IMPROVE），覆盖率 ≥ 80%。
- **小文件**：200-400 行为宜、不超 800 行；函数 < 50 行；嵌套 ≤ 4 层。
- **显式错误处理**：每个层级显式处理错误，不静默吞掉。
- **真实数据库测试**：integration 必须用真实 PostgreSQL + 完整 API 链路（见 `Harness/rules/测试规范.md`），禁止 sqlite 内存库 + 直接调 service。
- **套件分进程**：unit 与 integration **不得同进程**跑（`unit/conftest.py` 的 TRUNCATE 会抹掉 `ontology_class`）。
- **测试库用 `qa-pg-a6`（端口 5435）**：本任务实测发现**共享测试库 5433 已被乙线 B1 的分支推进到 `0096`**（`evidence` 表存在），而 `epic/v31-upgrade` 链头停在 `0094` → `alembic upgrade head` 报 `Can't locate revision identified by '0096'`，**本分支在任何测试库上跑任何用例都会挂在 `seedEngine`**。故为本 worktree 单起一个 PG（同 `qa-pg-a1` 的先例，见 memory `qa-system-dedicated-pg-a1`），不去重置别人正在用的库——重置会让 B1 下次升级重跑 `op.add_column` 撞 `column already exists`。库名必须仍叫 `qa_metadata_test`（测试内有写库闸精确匹配）。
- **命名**：服务/领域层方法用 `camelCase`（与文件内既有惯例一致，如 `_executeMultiStep`）；常量 `UPPER_SNAKE_CASE`；测试函数 `snake_case`。
- **提交格式**：`<type>: <description>`；**不加** `Co-Authored-By:` trailer。
- **禁魔数**：上限必须是命名常量，不得在代码里出现裸 `4`。
- 本任务**不动**迁移（无 alembic）、**不动** `business_db_pool.py`（属乙线）、**不动** SQL Guard 三集合口径。

---

## 背景：为什么必须改 prompt

现状两处**静默吞步**，只改一处等于没改：

1. `_plan_by_llm` 在 `step_query_planner.py:343-352` 把超限计划**截断**到 4 个数据步，用户拿到的是「12 问里的 4 个」却看不出少了什么。
2. `_STEP_PLANNER_SYSTEM_PROMPT` 自己写着「最多拆 4 个子步骤」——**模型在返回前就自行合并/丢步了**，执行缝根本看不到真实步数。

只做第 1 处：prompt 仍限 4，模型永远只报 4，拒收分支**永不触发**（A6 验收里那个「12 步问题」会一直返回 4 步的部分答案）。所以 Task 1 必须两处一起改。

**这是一次用户可见的行为变更，需明确知情**：今天「需要 12 步」的问题会拿到 4 步的部分答案（看起来完整，实际不全）；改后会被**拒收并提示**。这是 A6 的既定取舍——**宁可不答，不给残缺的答案**——但覆盖面确实会下降。上限是 `MAX_PLAN_DATA_STEPS` 一行常量，若上线后拒收过多，调它即可（未来可考虑挪进 `system_config` 让运维热调，本任务按 A6 要求保持常量）。

---

### Task 1: planner 如实上报（常量 + 去截断 + 去 prompt 自限）

**Files:**
- Modify: `backend/app/domain/multi_step_plan.py:20`
- Modify: `backend/app/services/step_query_planner.py`（import 块、`_STEP_PLANNER_SYSTEM_PROMPT`、`_plan_by_llm` 截断块）
- Test: `backend/app/tests/unit/test_step_query_planner.py:142-156`

**Interfaces:**
- Consumes: 无（本任务独立）
- Produces: `MAX_PLAN_DATA_STEPS: int`（`app.domain.multi_step_plan`），值 = `MAX_MULTI_STEP - 1` = `4`。Task 2/3 的守门谓词消费它。

- [ ] **Step 1: 写失败测试（改写既有截断测试为「不截断」契约）**

把 `backend/app/tests/unit/test_step_query_planner.py` 的 `test_steps_over_max_limit_truncated`（第 142-156 行）整体替换为：

```python
    async def test_steps_over_limit_pass_through_untruncated(self) -> None:
        """拆出 6 个数据步（>MAX_PLAN_DATA_STEPS=4）时**原样返回**，不再截断。

        截断改到执行缝（见 test_chat_multi_step 的超限拒收）。planner 必须如实
        上报步数，否则拒收文案说不出真实步数、永远只能报 4。
        """
        planner = StepQueryPlanner()
        many_steps = ', '.join(
            f'{{"description": "step{i}", "subQuestion": "step{i}问"}}'
            for i in range(6)
        )
        mock_client = _MockLlmClient(
            f'{{"isMultiStep": true, "steps": [{many_steps}], "aggregationHint": "汇总"}}'
        )
        result = await planner.plan("对比各年销售额", MagicMock(), mock_client, "gpt-4o")
        assert result.plan is not None
        non_agg = [s for s in result.plan.steps if not s.aggregation_only]
        assert len(non_agg) == 6  # 原样透传，不截断
```

并在同一文件末尾追加一条 prompt 契约测试：

```python
def test_system_prompt_does_not_cap_step_count() -> None:
    """拆步 prompt 不得自带步数上限。

    一旦模型被要求「最多拆 N 步」，它会在返回前自行合并/丢步——执行缝看到的
    步数永远到不了上限，超限拒收分支形同虚设（A6 的核心陷阱）。上限只由
    `MAX_PLAN_DATA_STEPS` 在执行缝施加，prompt 只负责如实列举。
    """
    from app.services.step_query_planner import _STEP_PLANNER_SYSTEM_PROMPT

    assert "最多拆" not in _STEP_PLANNER_SYSTEM_PROMPT
```

- [ ] **Step 2: 跑测试确认 RED**

```bash
cd backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5435/qa_metadata_test' \
  uv run pytest app/tests/unit/test_step_query_planner.py -q
```

预期：`test_steps_over_limit_pass_through_untruncated` FAIL（`assert 4 == 6`），`test_system_prompt_does_not_cap_step_count` FAIL（prompt 里仍有「最多拆」）。

- [ ] **Step 3: 加常量**

`backend/app/domain/multi_step_plan.py` 第 20 行之后插入：

```python
MAX_MULTI_STEP = 5  # 硬上限：含汇总步骤最多 5 步（防无限循环）

# 数据步（非汇总）上限。planner 与执行缝都以它为准；汇总步骤是必然的最后一步，
# 不占额度。派生自 MAX_MULTI_STEP 而非另写一个 5，避免两处上限各自漂移。
MAX_PLAN_DATA_STEPS = MAX_MULTI_STEP - 1
```

- [ ] **Step 4: 改 prompt（去掉自限，改为如实列举）**

`backend/app/services/step_query_planner.py`——把 `_STEP_PLANNER_SYSTEM_PROMPT`（第 70-77 行）整体替换为：

```python
_STEP_PLANNER_SYSTEM_PROMPT = (
    "你是查询拆分器。判定用户问题是否需要拆成多个子查询。\n"
    "若需要，返回 JSON: {\"isMultiStep\": true, "
    "\"steps\": [{\"description\": \"...\", \"subQuestion\": \"...\"}], "
    "\"aggregationHint\": \"如何汇总\"}\n"
    "若不需要，返回 {\"isMultiStep\": false}。\n"
    "只拆**彼此独立、无法用一条 SQL 完成**的子问题；一次 SQL 能算完的对比/汇总不要拆。\n"
    "**如实列出全部子问题，不要因为数量多就自行合并或截断**——"
    "系统会按上限决定是否受理，你少报会让用户拿到不完整的答案。"
)
```

- [ ] **Step 5: 删掉截断块，改用新常量**

`backend/app/services/step_query_planner.py`——删除 `_plan_by_llm` 里的整段截断（第 343-352 行）：

```python
        # 硬上限保护（不含汇总步骤）
        non_agg_count = len(steps) - 1
        if non_agg_count > MAX_MULTI_STEP - 1:
            logger.warning(
                "拆步数量 %d 超过上限 %d，截断: %s",
                non_agg_count,
                MAX_MULTI_STEP - 1,
                question,
            )
            steps = steps[: MAX_MULTI_STEP - 1] + steps[-1:]
```

同时删掉紧随其后的空行，让 `return (` 直接接在 `steps.append(...)` 块之后。

再把 import 块（第 20-26 行）里的 `MAX_MULTI_STEP,` 删掉——截断块是它在本文件唯一的用处，删块后它就是未使用 import：

```python
from app.domain.multi_step_plan import (
    GlobalFilters,
    MultiStepPlan,
    StepPlan,
    _clip_text,
)
```

- [ ] **Step 6: 跑测试确认 GREEN**

```bash
cd backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5435/qa_metadata_test' \
  uv run pytest app/tests/unit/test_step_query_planner.py -q
```

预期：全绿（含新契约测试）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/domain/multi_step_plan.py backend/app/services/step_query_planner.py backend/app/tests/unit/test_step_query_planner.py
git commit -m "refactor(planner): 拆步不再静默截断，如实上报步数"
```

---

### Task 2: 非流式执行缝拒收 + 固定话术

**Files:**
- Modify: `backend/app/services/messages_zh.py`（多步降级段之后）
- Modify: `backend/app/services/chat_multistep.py`（import 块、新增两个方法、`_executeMultiStep` 入口）
- Test: `backend/app/tests/integration/test_chat_multi_step.py`

**Interfaces:**
- Consumes: `MAX_PLAN_DATA_STEPS`（Task 1）；`MSG_PLAN_TOO_MANY_STEPS`（本任务新增）
- Produces:
  - `MSG_PLAN_TOO_MANY_STEPS: str`——`str.format` 占位 `{steps}` / `{limit}`
  - `MultiStepMixin._isOversizedPlan(plan: MultiStepPlan) -> bool`（`@staticmethod`）
  - `MultiStepMixin._rejectOversizedPlan(session, dto, multiStepPlan, *, total_cost: Decimal, _t0: float) -> str`——Task 3 流式路径复用

- [ ] **Step 1: 写失败测试（集成，真实 PG + 完整 API 链路）**

在 `backend/app/tests/integration/test_chat_multi_step.py` 的 `_MultiStepLlm` 旁新增一个超限 stub，并加一个测试类：

```python
# 6 个数据步（>MAX_PLAN_DATA_STEPS=4）的拆步回复，用于超限拒收用例
_OVERSIZED_PLAN_JSON = (
    '{"isMultiStep": true, "steps": ['
    + ", ".join(
        f'{{"description": "维度{i}", "subQuestion": "维度{i}的金额"}}' for i in range(6)
    )
    + '], "aggregationHint": "综合分析"}'
)


class _OversizedLlm(_MultiStepLlm):
    """拆步 LLM 返回 6 步计划（超出 4 步上限）。"""

    async def complete(self, messages: list, **kwargs) -> object:
        if "查询拆分器" in messages[0].content:
            self.calls.append([(m.role, m.content) for m in messages])

            class _Resp:
                content = _OVERSIZED_PLAN_JSON
                modelName = "test-model"
                promptTokens = 10
                completionTokens = 5

            return _Resp()
        return await super().complete(messages, **kwargs)


class TestOversizedPlanRejected:
    """拆步超限 → 拒收 + 固定提示，且**不执行任何数据步**（A6 核心契约）。"""

    async def test_oversized_plan_is_rejected_with_hint(self) -> None:
        """6 步计划 → 提示里含真实步数与上限，steps 为空。"""
        from app.services.messages_zh import MSG_PLAN_TOO_MANY_STEPS

        llm = _OversizedLlm()
        async with _RouterFor(llm) as client:
            resp = await client.post("/api/v1/chat", json={
                "sessionId": "sess-oversized",
                "question": "完整分析华东销售下降所有原因",
                "datasourceId": 1,
            })
        assert resp.status_code == 200
        body = resp.json()
        assert body["answer"] == MSG_PLAN_TOO_MANY_STEPS.format(steps=6, limit=4)
        assert body["intent"] == "multi_step"
        assert body["steps"] == []

    async def test_oversized_plan_executes_no_sql_step(self) -> None:
        """拒收必须发生在**任何** LLM 生成之前：不得出现 SQL 生成调用。

        这是本任务与「截断后照常执行」的分水岭——若退化成执行前 4 步，
        用户仍会拿到不完整答案，且白烧 4 次 SQL 生成。
        """
        llm = _OversizedLlm()
        async with _RouterFor(llm) as client:
            await client.post("/api/v1/chat", json={
                "sessionId": "sess-oversized-2",
                "question": "完整分析华东销售下降所有原因",
                "datasourceId": 1,
            })
        sqlGenCalls = [
            c for c in llm.calls
            if any("生成 SQL 时必须" in content for _, content in c)
        ]
        assert sqlGenCalls == []

    async def test_rule_path_oversized_is_rejected_too(self) -> None:
        """规则快路径（「第X步」标号）同样受上限约束——它此前**完全无上限**。

        5 个标号 → 5 个数据步 > 4 → 拒收；4 个标号 → 恰好 4 → 放行（边界）。
        """
        from app.services.messages_zh import MSG_PLAN_TOO_MANY_STEPS

        llm = _MultiStepLlm()
        async with _RouterFor(llm) as client:
            resp = await client.post("/api/v1/chat", json={
                "sessionId": "sess-rule-oversized",
                "question": "第1步查华东金额，第2步查华南金额，第3步查华北金额，"
                            "第4步查西南金额，第5步查东北金额",
                "datasourceId": 1,
            })
        assert resp.status_code == 200
        assert resp.json()["answer"] == MSG_PLAN_TOO_MANY_STEPS.format(steps=5, limit=4)
```

> `_RouterFor` / `_seed` / `_StubEmbeddingService` 从 `app.tests.integration.test_chat_api` 导入（文件顶部已有该 import）。若 `_RouterFor` 不是 async context manager，照抄同文件既有用例的写法——**不要**新造一套 fixture。

- [ ] **Step 2: 跑测试确认 RED**

```bash
cd backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5435/qa_metadata_test' \
  uv run pytest app/tests/integration/test_chat_multi_step.py::TestOversizedPlanRejected -q
```

预期：3 例 FAIL——`MSG_PLAN_TOO_MANY_STEPS` 尚不存在（ImportError），且当前会照常执行 6 步（Task 1 后不再截断，会执行全部 6 步）。

- [ ] **Step 3: 加文案常量**

`backend/app/services/messages_zh.py`——在 `MSG_MULTI_STEP_DEGRADE_FAILED` 之后、`# MSG_RATE_LIMITED ...` 之前插入：

```python
# 拆步结果超出数据步上限：执行缝拒收（非执行失败）。`{steps}` 为模型要求的**真实**
# 步数，`{limit}` 为上限。刻意报出真实步数——只说「太多了」用户无从判断该砍多少，
# 说出步数才知道要缩到什么程度。
MSG_PLAN_TOO_MANY_STEPS = (
    "该问题需要拆解为 {steps} 步，超出 {limit} 步上限，"
    "请聚焦单一维度提问（如先分析客户层面原因）。"
)
```

- [ ] **Step 4: 加守门谓词与拒收方法**

`backend/app/services/chat_multistep.py`——import 块补上新符号：

```python
from app.domain.multi_step_plan import (
    MAX_PLAN_DATA_STEPS,
    GlobalFilters,
    MultiStepPlan,
    StepExecutionContext,
    StepPlan,
    StepResult,
)
```

```python
from app.services.messages_zh import (
    MSG_MULTI_STEP_DEGRADE_FAILED,
    MSG_MULTI_STEP_DEGRADE_PARTIAL,
    MSG_PLAN_TOO_MANY_STEPS,
)
```

在 `_executeMultiStep`（第 361 行）之前插入两个方法：

```python
    @staticmethod
    def _isOversizedPlan(plan: MultiStepPlan) -> bool:
        """计划的数据步数是否超上限（汇总步不占额度）。

        执行缝的守门谓词。planner 负责**如实上报**步数，这里负责**受不受理**——
        分成两处是为了让拒收文案能说出真实步数（planner 若自行截断，N 恒为 4）。
        """
        return len(plan.data_steps) > MAX_PLAN_DATA_STEPS

    async def _rejectOversizedPlan(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        multiStepPlan: MultiStepPlan,
        *,
        total_cost: Decimal,
        _t0: float,
    ) -> str:
        """超限计划的固定友好回答：落库 + 存状态，返回文案；不做任何执行。

        与 `_finalizeMultiStepDegrade` 同构（流式/非流式共用同一入口，避免两条
        路径漂移），差别只在语义：这里是「按上限拒收」而非「执行失败」，
        故不写降级 warning，也不重试——重试只会拿到同样超限的计划。
        """
        steps = len(multiStepPlan.data_steps)
        answer = MSG_PLAN_TOO_MANY_STEPS.format(steps=steps, limit=MAX_PLAN_DATA_STEPS)
        logger.info(
            "拆步超限，按上限拒收（%d 步 > %d 步）: %s",
            steps, MAX_PLAN_DATA_STEPS, dto.question,
        )
        await self._storeSessionMessages(
            session, dto.sessionId, dto.question, answer, None,
            routing_layer="L2",
            latency_ms=int((time.monotonic() - _t0) * 1000),
            token_cost_usd=float(total_cost),
        )
        await self._saveQueryState(
            session, dto.sessionId,
            question=dto.question, plan=None, sql=None, resultColumns=[],
        )
        return answer
```

- [ ] **Step 5: `_executeMultiStep` 入口早退**

`backend/app/services/chat_multistep.py`——在 `_executeMultiStep` 的 docstring 之后、`ctx = StepExecutionContext(` 之前插入：

```python
        if self._isOversizedPlan(multiStepPlan):
            # 在**任何** LLM 调用之前返回：拒收不是「执行失败」，更不该先烧掉
            # 前 4 步的 SQL 生成再报错（那正是本任务要消灭的形态）。
            answer = await self._rejectOversizedPlan(
                session, dto, multiStepPlan,
                total_cost=initial_cost, _t0=_t0,
            )
            return ChatResponse(
                answer=answer,
                intent="multi_step",
                steps=[],
                tokensUsed=initial_tokens,
                cost=float(initial_cost),
                latency_ms=int((time.monotonic() - _t0) * 1000),
                modelName=None,
            )
```

- [ ] **Step 6: 跑测试确认 GREEN**

```bash
cd backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5435/qa_metadata_test' \
  uv run pytest app/tests/integration/test_chat_multi_step.py -q
```

预期：新增 3 例全绿，且**本文件既有用例不回归**（含「明确要求分步 → 多步执行」的 2 步用例——它 ≤4 步，不受影响）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/messages_zh.py backend/app/services/chat_multistep.py backend/app/tests/integration/test_chat_multi_step.py
git commit -m "feat(planner): 拆步超限在执行缝拒收并给出拆解提示"
```

---

### Task 3: 流式路径同口径

**Files:**
- Modify: `backend/app/services/chat_stream.py:706` 附近（`_streamMultiStep` 入口）
- Test: `backend/app/tests/integration/test_chat_multi_step.py`（流式用例）

**Interfaces:**
- Consumes: `_isOversizedPlan` / `_rejectOversizedPlan`（Task 2）、`MSG_PLAN_TOO_MANY_STEPS`
- Produces: 无新符号（流式走既有事件类型）

- [ ] **Step 1: 写失败测试（流式 SSE 事件序列）**

在 `backend/app/tests/integration/test_chat_multi_step.py` 的 `TestOversizedPlanRejected` 内追加。**照抄本文件既有流式用例的事件解析写法**（见 `EVENT_STEP_PLAN` / `EVENT_TOKEN` / `EVENT_DONE` 的既有断言），只替换断言内容：

```python
    async def test_streaming_oversized_yields_hint_without_step_events(self) -> None:
        """流式：只下发固定提示 + done，**不出现任何 step_plan / step_result**。

        与非流式同口径（`_rejectOversizedPlan` 共用），否则前端在流式下会渲染
        出 6 个空的步骤卡片。
        """
        from app.services.stream_events import EVENT_STEP_PLAN, EVENT_STEP_RESULT, EVENT_TOKEN
        from app.services.messages_zh import MSG_PLAN_TOO_MANY_STEPS

        llm = _OversizedLlm()
        events = await _collect_stream_events(llm, {
            "sessionId": "sess-oversized-stream",
            "question": "完整分析华东销售下降所有原因",
            "datasourceId": 1,
        })
        assert not [e for e in events if e.event in (EVENT_STEP_PLAN, EVENT_STEP_RESULT)]
        tokens = [e for e in events if e.event == EVENT_TOKEN]
        assert tokens and tokens[-1].data["content"] == MSG_PLAN_TOO_MANY_STEPS.format(
            steps=6, limit=4,
        )
```

> 若本文件没有 `_collect_stream_events` 之类的现成 helper，就用既有流式用例用的那个（可能是 `_RouterFor` + 逐个 parse SSE 行）。**不要**新造一套与既有用例不同的流式驱动方式。

- [ ] **Step 2: 跑测试确认 RED**

```bash
cd backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5435/qa_metadata_test' \
  uv run pytest app/tests/integration/test_chat_multi_step.py::TestOversizedPlanRejected::test_streaming_oversized_yields_hint_without_step_events -q
```

预期：FAIL——当前流式会为 6 个步骤各下发 `EVENT_STEP_PLAN`。

- [ ] **Step 3: 流式入口早退**

`backend/app/services/chat_stream.py`——在 `_streamMultiStep` 的

```python
        _ms_t0 = _t0 if _t0 is not None else time.monotonic()
```

之后、`cacheHitMultiplier = await _readFloatConfig(` 之前插入（放在读配置**之前**：拒收路径不需要 cache multiplier，省一次 DB 读）：

```python
        if self._isOversizedPlan(multiStepPlan):
            # 与非流式同口径共用 _rejectOversizedPlan；事件序列模仿本文件的
            # 「单步统一展示」前缀（multi_step_plan → step_plan）→ token → done，
            # 前端 handler 无需区分单步/多步。
            answer = await self._rejectOversizedPlan(
                session, dto, multiStepPlan,
                total_cost=initial_cost, _t0=_ms_t0,
            )
            yield self._singleStepOverview("超出步数上限", dto.question)
            yield self._singleStepStart("超出步数上限", dto.question)
            yield StreamEvent(EVENT_TOKEN, {"content": answer})
            yield StreamEvent(
                EVENT_DONE,
                {
                    "tokensUsed": initial_tokens,
                    "cost": float(initial_cost),
                    "modelName": None,
                    "latency_ms": int((time.monotonic() - _ms_t0) * 1000),
                    "affinityStatus": None,
                    "steps": [],
                    "suggestedAgent": suggestion.model_dump(mode="json", by_alias=True)
                    if suggestion is not None else None,
                },
            )
            return
```

- [ ] **Step 4: 跑测试确认 GREEN**

```bash
cd backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5435/qa_metadata_test' \
  uv run pytest app/tests/integration/test_chat_multi_step.py -q
```

预期：全绿。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/chat_stream.py backend/app/tests/integration/test_chat_multi_step.py
git commit -m "feat(planner): 流式路径同口径拒收超限拆步"
```

---

### Task 4: 全量回归 + 覆盖率 + 变更记录

**Files:**
- Create: `Harness/changes/2026-09-28-planner-step-limit/summary.md`
- Test: 既有套件（不再新增用例）

**Interfaces:**
- Consumes: Task 1-3 的全部产物
- Produces: 无代码产物

- [ ] **Step 1: unit 与 integration 分开进程跑（TRUNCATE 陷阱）**

```bash
cd backend
COVERAGE_FILE=/tmp/.cov_a6 TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5435/qa_metadata_test' \
  uv run pytest app/tests/unit -q --cov=app --cov-report=term-missing
```

```bash
cd backend
COVERAGE_FILE=/tmp/.cov_a6 TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5435/qa_metadata_test' \
  uv run pytest app/tests/integration -q --cov=app --cov-append --cov-report=term-missing
```

> 必须分两条命令：同进程混跑会让 `unit/conftest.py` 的 TRUNCATE 抹掉 `ontology_class`。
> 已知无关失败：`test_rbac_api.py` 稳定 11 例（`_mkUser` 未传已改必填的 `password`），与本任务无关，别去查。

- [ ] **Step 2: 核对本任务改动文件的覆盖率 ≥ 80%**

重点看 `app/services/step_query_planner.py`、`app/services/chat_multistep.py`、`app/services/chat_stream.py` 三行的命中率。拒收分支必须被 integration 用例**实测命中**（不是靠 unit 的 mock 覆盖）——若 `_rejectOversizedPlan` 显示未覆盖，说明流式那条断言没真的走到，回去看 Step 3 的插入位置是否在 `return` 之前。

- [ ] **Step 3: 写变更记录**

创建 `Harness/changes/2026-09-28-planner-step-limit/summary.md`，按 `Harness/changes/_template` 的结构写。必须写清的四点：

1. **需求**：A6（架构升级 v3.1 甲线 M2a）——planner 步数硬限。
2. **取舍**：为什么把「截断」改成「拒收」；**明确记下这是用户可见覆盖面的下降**（原先拿到 4 步部分答案的问题，现在会被拒收），以及上限是一行常量可调。
3. **设计**：为什么限制放在执行缝而不是 planner（拒收文案要真实步数）；规则快路径此前**完全无上限**，本任务顺带堵上。
4. **未做**：A6 原计划的第三条「分级落地（§5.3：2-3 步走模板校验）」**刻意未做**——它是 planner 成本优化，不改步限，也不在 A6 验收范围内；待 Planner 有独立需求时另立变更。

- [ ] **Step 4: 提交**

```bash
git add Harness/changes/2026-09-28-planner-step-limit/summary.md
git commit -m "docs(planner): 补 A6 变更记录"
```

---

## 自审（Self-Review）

**1. 规格覆盖**（对照 `plan-person-a.md` 的 A6 三条）：

| A6 要求 | 落在哪 |
|---|---|
| `MAX_PLAN_STEPS = 5`（constants，禁魔数） | Task 1 Step 3 —— 以 `MAX_PLAN_DATA_STEPS = MAX_MULTI_STEP - 1` 落地。**刻意不复用 A6 的名字**：A6 说「5」，但 `MAX_MULTI_STEP=5` 是**含汇总步**的口径，数据步上限是 4。直接写 `5` 会让 5 个数据步通过、与既有 `MAX_MULTI_STEP` 撞口径。名字与值都取数据步口径，避免两个「5」语义不同 |
| 超限 → PlanDrop 式拒绝 + 话术 | Task 2/3 —— 复用既有「固定友好回答」形态（`_UNANSWERABLE_ANSWER` / `_finalizeMultiStepDegrade` 同款），**未**引入 `PlanDrop`：`PlanDrop` 是 `QueryPlan.from_dict` 的**解析丢弃**记录（类型损坏诊断），与本任务的「计划合法但超额度」不同类，硬套会让日志语义混淆 |
| 分级落地（§5.3） | **刻意未做**，Task 4 Step 3 记入变更记录 |
| 验收：12 步问题返回拆解提示 | Task 2 Step 1 的 `test_oversized_plan_is_rejected_with_hint` + 规则路径用例 |

**2. 占位符扫描**：无 TBD / TODO；Task 3 的流式驱动 helper 与 Task 2 的 `_RouterFor` 用法都写明「照抄本文件既有用例，不要新造」——这是**既存事实**（`test_chat_multi_step.py` 顶部已 import `_RouterFor`），不是留给实现者的填空。

**3. 类型一致性**：`MAX_PLAN_DATA_STEPS`（Task 1 定义 → Task 2/3 消费）、`_isOversizedPlan`（Task 2 定义 → Task 3 消费）、`_rejectOversizedPlan`（Task 2 定义 → Task 3 消费，签名一致：`session, dto, multiStepPlan, *, total_cost, _t0`）、`MSG_PLAN_TOO_MANY_STEPS`（Task 2 定义 → Task 3 消费，占位符 `{steps}`/`{limit}` 一致）。全文无第二处新定义。

## 已知风险

1. **覆盖面下降（有意为之）**：需要 5+ 步的问题从「拿到 4 步部分答案」变成「被拒收」。这是 A6 的既定取舍，但若线上拒收过多，`MAX_PLAN_DATA_STEPS` 是一行常量。上线后建议观察 `拆步超限，按上限拒收` 这条 info 日志的频次。
2. **prompt 放宽可能提高拆步数**：去掉「最多拆 4 步」后，模型对同一问题可能报出更多步 → 更多问题落入拒收。这正是 Task 1 Step 1 那条 prompt 契约测试要守住的方向；若实测拒收率过高，优先回来调 prompt 的「只拆彼此独立的子问题」措辞，而不是直接抬上限。
3. **`messages_zh.py` 与 A1 的合并**：A1（`feat/evidence-sql-metric`）也往该文件末尾追加了 18 行（id-mapping 文案）。两边都是**末尾追加**，合并时应为可忽略的空冲突；已在 A1 的合并方案里列为「预期冲突（文件末尾追加段）」同类。A6 的常量按语义插在**多步降级段**之后、而非文件最末，正是为了与之错开。
