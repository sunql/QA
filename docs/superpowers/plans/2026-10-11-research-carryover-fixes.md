# 研究链路遗留修复（M1–M4 + 图表维度标签）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉 `feat-research-report-r3` 终评留下的 4 条遗留（M1 replanCount 双拼写 / M2 计量记错模型名 / M3 端点 nextPhase 陈旧 / M4 `ports.py` 794 行）并**真修**图表维度标签那个 parked xfail。

**Architecture:** 前三条是把已经存在的「唯一正确写法」推广到重复站点（M1 常量 SSOT、M2 复用 `clientModelName` 口径、M3 抽出端点与 `resumeTurn` 共用的纯决策函数），**不新增机制**。第四条在共享 chart 链上新增一个**可选**的「维度字段 → 显示名」注入点：报告路径注入、chat 路径不注入 ⇒ chat 渲染逐字不变。第五条是纯搬移（`ports.py:541-734` → 新模块，单向依赖，不留再导出）。

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy async（asyncpg）/ PostgreSQL 16（测试库 5434）/ pytest；前端不涉及（本计划零前端改动）。

## Global Constraints

以下约束对**每个** task 生效，逐字照抄自项目规则与本次用户裁定 —— 实现者不要自行放宽：

- **工作区**：新建 worktree `.worktrees/fix-research-carryover`，分支从 `feat-research-report-r3`（`61986a9`）切出（用户 2026-10-11 裁定）。**不得**在 `main` 或 `feat-research-report-r3` 上直接提交 —— r3 冻结在已评审的 `61986a9`。
- **禁止 `alembic`**：本计划**任何**步骤都不跑 alembic（`alembic upgrade` 在本仓打的是 prod 库）。
- **测试环境**：每次 pytest 都必须**同时**给两个 env：
  `TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test` **和** `TEST_NEO4J_URI=bolt://localhost:7688`（缺后者会 fail-fast）。
  用 `python -m pytest`，**不要** `uv run pytest`。单元 / 集成 / services 套件**不许同进程混跑**（truncate 互相污染）。
- **真实数据库**：后端测试一律真 PostgreSQL，禁止 sqlite 内存库。
- **不可变数据**：新代码不得原地修改入参对象（本计划所有新增函数都是纯函数或返回新对象）。
- **小文件**：200–400 行为宜、800 硬上限；函数 < 50 行；嵌套 ≤ 4 层。
- **命名**：函数/变量 `camelCase`，类型/组件 `PascalCase`，常量 `UPPER_SNAKE_CASE`，ORM/Pydantic 字段 `snake_case`。
- **显式错误处理**：降级路径必须留痕（logger），不静默吞错。
- **零硬编码密钥**；DB 凭据只从 env 来。
- **禁止 `git stash` / `git stash pop`**（stash 栈与主 checkout 及其他 worktree 共享）。
- **不修改 `raw/`**。
- **本计划只动这些文件**：`app/services/research_agent_phases.py`、`app/services/research_agent_service.py`、`app/services/research_agent_stages.py`、`app/services/research_agent_execution.py`、`app/services/research_agent_ports.py`、`app/services/chart_renderer.py`、`app/services/chart_integration.py`、`app/services/chart_spec_builder.py`、`app/domain/chart_spec.py`、`app/api/v1/research.py`，新增 `app/services/research_agent_model_resolution.py`，以及对应测试文件。**chat 侧**（`chat_service.py` / `chat_multistep.py` / `chat_stream.py` / `chat_helpers.py`）**逐字不改** —— r3 的分支约束在新分支上继续有效，唯一被用户显式放宽的是共享 `chart_renderer`（Task 4）。
- **每条 LLM 调用都必须记 token + cost**（核心约束 #3）—— Task 2 修的就是这条。
- **两处用户裁定（2026-10-11，不得由实现者或评审推翻）**：
  1. **图表维度标签做真修，改共享 `chart_renderer`**（r3 分支的「禁改 chat 侧文件」约束在新分支上就这一项被显式放宽；`chart_spec_builder` / `chart_spec` / `chart_integration` 属同一机制的承重件，一并授权）。放宽的**边界**是：chat 渲染负载必须逐字不变，由 Task 4 的反向护栏钉住。
  2. **退役 r3 那个 `xfail(strict=True)` 的 `test_chart_labels_are_translated`**：它原来用 2 维 fixture ⇒ 走 R13 明细表 ⇒ 无轴可挂名，断言在**那条路径上**不可达。本计划把**同样两条断言**挪到单维度聚合 fixture（报告里真实存在的形态），并新增 TABLE 契约用例把明细表路径「不做翻译」钉成契约。断言未被放宽 —— 只是判别点从不可达路径移到可达路径。评审若认为这属于「改 brief 钉死的测试」，请按 SDD 规则回报人类裁定，**不要**静默改写。
- **计划文件入档**：本文件在新分支的首个提交里一起纳入版本控制（复制到新 worktree 的 `docs/superpowers/plans/2026-10-11-research-carryover-fixes.md`）。

---

### Task 1: `replanCount` 单一事实源（M1）

**为什么**：写侧用**裸字面量** `"replanCount"`（`phases.py` 的 `buildOptions(replanCount=...)` 经 `**payload` 进 options、`service.py` 的 `state[...] =`），读侧用常量 `OPT_REPLAN_COUNT`（`stages.py:64`）。两者**只靠拼写巧合**一致。将来谁改了常量值，写侧仍写 `"replanCount"` ⇒ 计数不再往返 ⇒ **3 次 replan 上限静默永不生效**，planning 点可无限 modify（每轮一次 planner LLM 调用），正是 T4 刚封掉的循环。`research_agent_phases.py` 已经 `import` 了 `OPT_REPLAN_COUNT` 却只字未用（ruff **F401** 在报），是这条缺陷的化石证据。

**Files:**
- Modify: `backend/app/services/research_agent_phases.py:172`（读）、`:200`（写）
- Modify: `backend/app/services/research_agent_stages.py:64`（`rebuildState` 的 **state 键**也直写了字面量）
- Modify: `backend/app/services/research_agent_service.py:58-67`（import 块）、`:295`（读）、`:302`（写）
- Modify: `backend/app/tests/integration/test_research_agent_service.py:1092`、`:1126`（断言改用常量）
- Test: `backend/app/tests/unit/test_research_replan_count_ssot.py`（新建）

**Interfaces:**
- Consumes: `OPT_REPLAN_COUNT = "replanCount"`（`app/services/research_agent_ports.py:86`）
- Produces: 无新符号；只改 3 个消费者模块的写/读拼写

- [ ] **Step 1: 写失败测试（结构守卫）**

新建 `backend/app/tests/unit/test_research_replan_count_ssot.py`：

```python
"""`replanCount` 只允许经 `OPT_REPLAN_COUNT` 出现（M1 的根因是双拼写）。

常量自己的定义在 `research_agent_ports.py`（那里字面量是**必须**的），消费者模块
一律不许再写一遍引号字面量 —— 否则「改常量值」与「写侧仍写老字符串」会静默分叉，
3 次 replan 上限随之失效（用户可无限循环 modify）。
"""

from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parents[2] / "services"

CONSUMERS = (
    "research_agent_phases.py",
    "research_agent_service.py",
    "research_agent_stages.py",
    "research_agent_execution.py",
)


def test_no_bare_replan_count_literal_in_consumers() -> None:
    for name in CONSUMERS:
        text = (SERVICES_DIR / name).read_text(encoding="utf-8")
        for quote in ('"', "'"):
            literal = f"{quote}replanCount{quote}"
            assert literal not in text, (
                f"{name} 直写了 {literal}；请改用 OPT_REPLAN_COUNT（单一事实源）"
            )
```

- [ ] **Step 2: 跑测试确认它红**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/unit/test_research_replan_count_ssot.py -v
```
Expected: FAIL，报 `research_agent_phases.py 直写了 "replanCount"`（该文件 `:172`/`:200` 命中）。

- [ ] **Step 3: 改 `research_agent_phases.py` 两处 + `research_agent_stages.py` 一处**

`_stagePlan` 内，读侧（原 `:172`）：

```python
        replan_count = int(state.get(OPT_REPLAN_COUNT) or 0)
```

写侧（原 `:200`，`buildOptions(...)` 的实参）—— `buildOptions` 只收关键字参数，动态键必须展开：

```python
        return (
            CHECKPOINT_PLANNING,
            buildOptions(
                signal=SIGNAL_FIXED_PLAN, plan=plan, arms=state.get("esl"),
                resumePhase=PHASE_EXECUTE,
                **{OPT_REPLAN_COUNT: state.get(OPT_REPLAN_COUNT, 0)},
            ),
            "计划是否确认？",
        )
```

`research_agent_stages.py:64`（`rebuildState` 里 state 的**键** —— 读已走常量、键还直写；键与 options 键本就是同一个字符串）：

```python
        OPT_REPLAN_COUNT: int(options.get(OPT_REPLAN_COUNT) or 0),
```

- [ ] **Step 4: 改 `research_agent_service.py` 三处**

import 块按字母序插入（`OPT_ABORT_PHASE` 之前无同名冲突；该模块的 ports import 块在 `:58` 起）：

```python
    OPT_REPLAN_COUNT,
```

`resumeTurn` 内，读侧（原 `:295`）：

```python
            replan_count = int(state.get(OPT_REPLAN_COUNT, 0))
```

写侧（原 `:302`）：

```python
                state[OPT_REPLAN_COUNT] = replan_count + 1
```

> 注：`state["replan"] = True` / `state[OPT_REPLAN_COUNT] = ...` 这两行原地改的是 `resumeState` 刚重建出来的局部 dict（不是外部传入的对象），本次**不**改语义 —— 这一任务只换拼写，保持行为逐字不变。评审若把它当既有坏味道提出，按「未由本支引入」处理。

- [ ] **Step 5: 测试断言也改用常量**

`backend/app/tests/integration/test_research_agent_service.py`，import 块加 `OPT_REPLAN_COUNT`（该文件已从 ports 导入 `MAX_REPLAN_PER_TURN` 等），并把两处断言改成：

`~:1092`：
```python
    assert cp2.options[OPT_REPLAN_COUNT] == 1  # 迭代计数已 +1 并随 checkpoint 持久化
```

`~:1126`：
```python
        assert cp.options[OPT_REPLAN_COUNT] == i + 1, "迭代计数应随 checkpoint 持久化"
```

- [ ] **Step 6: 跑测试确认全绿**

Run（结构守卫 + replan 行为）：
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/unit/test_research_replan_count_ssot.py -v
```
Expected: 1 passed。

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/integration/test_research_agent_service.py -v -k "replan or modify"
```
Expected: `test_planning_modify_reruns_planner_with_choice`、`test_planning_modify_iterates_then_caps_at_max_replan` 均 PASS（这两条是本任务的**行为**护栏：上限在第 4 次 modify 生效、`replanCount` 随 checkpoint 持久化）。

再验 lint 面（F401 应消失、无新 F821）：
```bash
cd backend && ruff check --select F401,F821 app/services/research_agent_phases.py app/services/research_agent_service.py app/services/research_agent_stages.py
```
Expected: **只剩基线的 1 条 F821**（`research_agent_phases.py:342` 的 `"FindingStats | None"` 字符串注解 —— BASE 就有、本计划五个 task 都不认领，见「不做」清单）；`OPT_REPLAN_COUNT` 那条 **F401 必须消失**（由「导入未用」变成「已用」），且**不得新增**任何 F401/F821。
> 判据是「F401 清 0 且本任务零新增」，**不是**「无输出」—— 本仓 F821 的零容忍闸需要先钉基线（基线 30 条，含本支禁改的 chat 侧文件），那是独立任务。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/research_agent_phases.py backend/app/services/research_agent_stages.py backend/app/services/research_agent_service.py backend/app/tests/integration/test_research_agent_service.py backend/app/tests/unit/test_research_replan_count_ssot.py
git commit -m "fix(research): replanCount 写侧走 OPT_REPLAN_COUNT，消掉双拼写（M1）"
```

---

### Task 2: 计量记**配置名**而不是上游自报名（M2）

**为什么**：`MeteredClient.modelName` 取的是 **LLM 响应自报**的名字（`ports.py:429`：`self.modelName = getattr(response, "modelName", None) or self.modelName`）。网关会把名字改写成它自己的别名（真机观测到 `deepseek-flash`），该名字**不在 `llm_config`** ⇒ `LlmUsageRecorder._findConfig` 查不到 ⇒ **cost 记 0**，且 `model_config_id` 落空（`TokenUsageService.getLastModelId` 的模型亲和一并失效）。全链路只有 plan 相位是对的（`phases.py:188` 用 `clientModelName(client)` = 客户端固化的配置名，查得到价）。本任务把这条**已有的正确口径**推广到另外 4 个站点。

**Files:**
- Modify: `backend/app/services/research_agent_stages.py`（在 `clientModelName` 旁新增 `meteringModelName`）
- Modify: `backend/app/services/research_agent_phases.py:379`、`:415`（并 import 新 helper）
- Modify: `backend/app/services/research_agent_service.py:508`（并 import 新 helper）
- Modify: `backend/app/services/research_agent_execution.py:533`（并 import 新 helper）
- Test: `backend/app/tests/integration/test_research_agent_service.py`（新增 fake client + 1 条用例）

**Interfaces:**
- Consumes: `clientModelName(client) -> str | None`（`research_agent_stages.py:154`，读 `client._modelName` 再退 `client.model_name`）
- Produces: `meteringModelName(client: Any, metered: Any = None) -> str | None` —— 位置参数两个，`metered` 可省；返回「配置名优先，取不到才退回自报名」

- [ ] **Step 1: 写失败测试**

在 `backend/app/tests/integration/test_research_agent_service.py` 的 `FakeLlmClient`（`:314`）之后追加：

```python
class AliasedLlmClient(FakeLlmClient):
    """配置名与自报名**不同**的客户端：`_modelName` = 配置名（同真 `OpenAiClient`），
    响应自报网关别名。`test_hypothesis_llm_call_is_metered` 里两者同为 "fake-model"，
    双拼写同值 ⇒ 判别不出 M2；这里制造分叉，判别才成立。
    """

    def __init__(
        self,
        content: str,
        *,
        configured: str = "fake-model",
        reported: str = "gateway-alias",
    ) -> None:
        super().__init__(content)
        self._modelName = configured
        self._reported = reported

    async def complete(self, messages, **kwargs):
        response = await super().complete(messages, **kwargs)
        response.modelName = self._reported
        return response
```

在 `test_hypothesis_llm_call_is_metered`（`:691`）之后追加用例：

```python
@pytest.mark.asyncio
async def test_hypothesis_metering_records_configured_model_name(dbSession, makeService) -> None:
    """计量记**配置名**（M2）：自报名查不到 `llm_config` ⇒ 那条账 cost 记 0。

    断言点是「交给 recorder 的名字」，不是 cost 本身：本测试库 `llm_config` 结构漂移
    （缺 `disable_thinking` 列，见 `test_default_usage_recorder_writes_row_and_skips_zero_usage`
    的 docstring），任何 ORM 级 LlmConfig 读都会炸，而修漂移要跑 alembic（本计划禁止）。
    名字对了，`_findConfig` 才查得到 → 成本/`model_config_id` 才有值，这条链路由
    `test_default_usage_recorder_cost_formula_matches_chat` 覆盖。
    """
    recorder = FakeUsageRecorder()
    svc = makeService(
        llmFactory=lambda cfg: AliasedLlmClient(HYPOTHESIS_JSON),
        usageRecorder=recorder,
    )
    s = await _newSession(svc, dbSession)
    await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    await _resolve(svc, dbSession, s.id, "confirm")  # 固定 #1
    await _resolve(svc, dbSession, s.id, "confirm")  # 固定 #2 → hypothesis 阶段
    assert recorder.records, "假设相位应至少记一笔账"
    assert recorder.records[0]["modelName"] == "fake-model"
    assert recorder.records[0]["purpose"] == "research_hypothesis"
```

- [ ] **Step 2: 跑测试确认它红**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/integration/test_research_agent_service.py -v -k configured_model_name
```
Expected: FAIL —— `assert 'gateway-alias' == 'fake-model'`（自报名被回填）。

- [ ] **Step 3: 新增 helper**

`backend/app/services/research_agent_stages.py`，紧接 `clientModelName`（`:154`）之后：

```python
def meteringModelName(client: Any, metered: Any = None) -> str | None:
    """计量用的模型名：**配置名优先**（与计划相位 `_stagePlan` 同口径）。

    `MeteredClient.modelName` 是**上游响应自报**的名字（网关别名，如 deepseek-flash），
    它按 `model_name` 反查不到 `llm_config` ⇒ 成本记 0、`model_config_id` 为空
    （`TokenUsageService.getLastModelId` 的模型亲和一并失效）。
    查价与归属都以**客户端固化的配置名**为准；配置名取不到（老 fake 无 `_modelName`）
    才退回自报名 —— 保证既有 fake 驱动的测试口径不变。
    """
    return clientModelName(client) or getattr(metered, "modelName", None)
```

- [ ] **Step 4: 改 5 个站点为同一写法**

`research_agent_phases.py`：把 `meteringModelName` 加进该文件对 `research_agent_stages` 的 import（该文件已有的 stages/execution 导入块可能不含它，按其字母序插入）。

`_stagePlan` 计量（原 `:188`，已是正确口径，统一成同一 idiom）：
```python
        await self._recordUsage(
            session,
            sessionId=sessionId,
            purpose=PURPOSE_PLAN,
            promptTokens=int(getattr(result, "prompt_tokens", 0) or 0),
            completionTokens=int(getattr(result, "completion_tokens", 0) or 0),
            modelName=meteringModelName(client),
        )
```

假设相位（原 `:379`，`metered = MeteredClient(client)` 在 `:370`）：
```python
            modelName=meteringModelName(client, metered),
```

报告相位（原 `:415`，`client = await self._llmClient(...)` 在 `:401`，在 `finally` 内）：
```python
            modelName=meteringModelName(client, metered),
```

`research_agent_service.py`（原 `:508`，`client` 在 `:468`、`metered = MeteredClient(client)` 在 `:471`）：
```python
        await self._recordUsage(
            session,
            sessionId=sessionId,
            purpose=PURPOSE_HYPOTHESIS,
            promptTokens=metered.promptTokens,
            completionTokens=metered.completionTokens,
            modelName=meteringModelName(client, metered),
            cachedTokens=metered.cachedTokens,
        )
```

`research_agent_execution.py`（原 `:533`，`metered = MeteredClient(client) if client is not None else None` 在 `:519`）：
```python
            modelName=meteringModelName(client, metered) if client is not None else None,
```
> 这三个文件各自把 `meteringModelName` 加进已有的 stages import 行；`execution.py` 若未导入 `clientModelName` 所在的模块，按该文件现有 import 风格补。

- [ ] **Step 5: 跑测试确认全绿**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/integration/test_research_agent_service.py -v -k "metered or metering or configured_model_name"
```
Expected: 全 PASS —— 新用例绿，且 `test_hypothesis_llm_call_is_metered`（同为 "fake-model" 的既有用例）、`test_report_llm_calls_are_metered_and_numbers_verbatim`、`test_report_metering_survives_reporter_failure`、`test_metered_client_captures_cached_tokens`、`test_compose_requires_or_meters_client` 一条不红（老 fake 无 `_modelName` ⇒ 走退回自报名分支，口径不变）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/research_agent_stages.py backend/app/services/research_agent_phases.py backend/app/services/research_agent_service.py backend/app/services/research_agent_execution.py backend/app/tests/integration/test_research_agent_service.py
git commit -m "fix(research): 计量记 llm_config 的配置名，成本不再记 0（M2）"
```

---

### Task 3: 端点与 `resumeTurn` 共用续跑决策（M3）

**为什么**：`POST /research/checkpoints/{id}/answer` 在**派后台任务之前同步**算 `nextPhase`，用的是「reject → abortPhase，否则 `nextPhaseForPhase(phase, options)`」；而机器实际跑的是 `resumeTurn` 里带 replan 感知的那套判定。planning 点 modify 时端点报 `execute`、机器却**重新暂停在 planning** ⇒ 响应说谎（今天无消费者，一旦有消费方就是 bug）。

**Files:**
- Modify: `backend/app/services/research_agent_phases.py`（`dataclass` import；新增 `rewrittenQuestion` / `ResumeDecision` / `decideResume`；ports import 补 `ACTION_MODIFY`、`MAX_REPLAN_PER_TURN`）
- Modify: `backend/app/services/research_agent_service.py:283-311`（`resumeTurn` 改用 `decideResume`）、`:323-341`（`_recordResumeTurn` 改用 `rewrittenQuestion`）
- Modify: `backend/app/api/v1/research.py:88`、`:414`
- Test: `backend/app/tests/unit/test_research_resume_decision.py`（新建）
- Test: `backend/app/tests/integration/test_research_api.py`（新增 1 条）

**Interfaces:**
- Consumes: `nextPhase(checkpoint, action) -> str`（`research_agent_phases.py:550`）、`nextPhaseForPhase(phase, options) -> str`、`OPT_REPLAN_COUNT`、`MAX_REPLAN_PER_TURN = 3`、`CHECKPOINT_PLANNING = "planning"`、`PHASE_ESL` / `PHASE_PLAN`
- Produces:
  - `rewrittenQuestion(choice: dict[str, Any] | None) -> str` —— 用户改写的问题（空串 = 未改写）；**唯一的推导点**（`_recordResumeTurn` 与 `decideResume` 都调它）
  - `ResumeDecision(phase: str, isReplan: bool)` —— `@dataclass(frozen=True)`
  - `decideResume(checkpoint: Any, action: str, choice: dict[str, Any] | None) -> ResumeDecision` —— 纯同步函数，端点与 `resumeTurn` 共用

- [ ] **Step 1: 写失败单元测试**

新建 `backend/app/tests/unit/test_research_resume_decision.py`：

```python
"""续跑决策矩阵：端点与 `resumeTurn` 必须给出同一个相位（M3）。

纯函数测试：`decideResume` 只读 checkpoint 的 `phase` / `options`，不碰 DB、不碰 LLM，
故用 `SimpleNamespace` 造桩即可（真库口径由 test_research_api.py 的端点用例覆盖）。
键名一律走常量，不写字面量 —— 本任务的意义就是消掉第二份判定。
"""

from types import SimpleNamespace

import pytest

from app.services.research_agent_ports import (
    ACTION_MODIFY,
    ACTION_REJECT,
    MAX_REPLAN_PER_TURN,
    OPT_ABORT_PHASE,
    OPT_REPLAN_COUNT,
    OPT_RESUME_PHASE,
    PHASE_ESL,
    PHASE_EXECUTE,
    PHASE_PLAN,
    PHASE_REPORT,
)
from app.services.research_agent_phases import decideResume


def _checkpoint(phase: str, **options) -> SimpleNamespace:
    return SimpleNamespace(phase=phase, options=options, session_id="s-1")


def test_rewritten_question_beats_everything() -> None:
    cp = _checkpoint("planning", **{OPT_RESUME_PHASE: PHASE_EXECUTE, OPT_REPLAN_COUNT: 0})
    decision = decideResume(cp, ACTION_MODIFY, {"question": "  换成按供应商看  "})
    assert (decision.phase, decision.isReplan) == (PHASE_ESL, False)


def test_reject_at_dynamic_checkpoint_goes_to_abort_phase() -> None:
    cp = _checkpoint("low_confidence", **{OPT_ABORT_PHASE: PHASE_REPORT})
    decision = decideResume(cp, ACTION_REJECT, None)
    assert (decision.phase, decision.isReplan) == (PHASE_REPORT, False)


def test_planning_modify_under_cap_replans() -> None:
    cp = _checkpoint("planning", **{OPT_RESUME_PHASE: PHASE_EXECUTE, OPT_REPLAN_COUNT: 0})
    decision = decideResume(cp, ACTION_MODIFY, {"instruction": "只看华东"})
    assert (decision.phase, decision.isReplan) == (PHASE_PLAN, True)


@pytest.mark.parametrize("replanCount", [MAX_REPLAN_PER_TURN, MAX_REPLAN_PER_TURN + 1])
def test_planning_modify_at_cap_advances(replanCount: int) -> None:
    cp = _checkpoint(
        "planning", **{OPT_RESUME_PHASE: PHASE_EXECUTE, OPT_REPLAN_COUNT: replanCount}
    )
    decision = decideResume(cp, ACTION_MODIFY, {"instruction": "只看华东"})
    assert (decision.phase, decision.isReplan) == (PHASE_EXECUTE, False)


def test_planning_confirm_advances_without_replan() -> None:
    cp = _checkpoint("planning", **{OPT_RESUME_PHASE: PHASE_EXECUTE, OPT_REPLAN_COUNT: 0})
    decision = decideResume(cp, "confirm", None)
    assert (decision.phase, decision.isReplan) == (PHASE_EXECUTE, False)
```

- [ ] **Step 2: 跑测试确认它红**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/unit/test_research_resume_decision.py -v
```
Expected: collection ERROR —— `ImportError: cannot import name 'decideResume'`。

- [ ] **Step 3: 实现纯决策函数**

`research_agent_phases.py` 顶部：`from dataclasses import asdict` → `from dataclasses import asdict, dataclass`。

该文件对 `research_agent_ports` 的 import 块（字母序）：`ACTION_REJECT` 之前插 `ACTION_MODIFY`；`MAX_FINDING_ROWS` 之后、`MAX_RESEARCH_DATA_STEPS` 之前插 `MAX_REPLAN_PER_TURN`（`MAX_R<b>...` 早于 `MAX_RESEARCH`）。

紧接 `nextPhase`（`:550`）之后插入：

```python
def rewrittenQuestion(choice: dict[str, Any] | None) -> str:
    """用户改写后的问题（空串 = 未改写）。**唯一推导点**：端点的决策与落库的 turn 共用它。"""
    return str((choice or {}).get("question") or "").strip()


@dataclass(frozen=True)
class ResumeDecision:
    """续跑决策：`phase` = 从哪个相位续跑；`isReplan` = 是否走「重跑计划」分支。"""

    phase: str
    isReplan: bool


def decideResume(checkpoint: Any, action: str, choice: dict[str, Any] | None) -> ResumeDecision:
    """checkpoint 决策 → 续跑相位。**端点与 `resumeTurn` 共用这一份**（M3）。

    判定顺序与 `resumeTurn` 历史实现逐字一致：改写 > replan > （reject → abort，否则
    `nextPhaseForPhase`）。判定键 `replanCount` 读**已落库**的 checkpoint.options
    （`_stagePlan` 每次 replan 都把它写回新 checkpoint 的 options）。
    """
    if rewrittenQuestion(choice):
        return ResumeDecision(phase=PHASE_ESL, isReplan=False)
    if action == ACTION_MODIFY and checkpoint.phase == CHECKPOINT_PLANNING:
        replanCount = int((checkpoint.options or {}).get(OPT_REPLAN_COUNT) or 0)
        if replanCount < MAX_REPLAN_PER_TURN:
            return ResumeDecision(phase=PHASE_PLAN, isReplan=True)
        logger.info(
            "replan 上限已达 %s，按 confirm 推进: session=%s",
            MAX_REPLAN_PER_TURN, checkpoint.session_id,
        )
    return ResumeDecision(phase=nextPhase(checkpoint, action), isReplan=False)
```

- [ ] **Step 4: 跑单元测试确认全绿**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/unit/test_research_resume_decision.py -v
```
Expected: 6 passed（5 个用例，其中上限那条 parametrize 成 2）。

- [ ] **Step 5: `resumeTurn` 改用决策函数**

`research_agent_service.py:283-311`，把原来的 `startPhase = nextPhase(...)` + `if/elif` 整块换成：

```python
        # 会话级模型选择（W5）：checkpoint 载荷不带 modelId（模型不是落库状态），由 resumeState 补回。
        state = resumeState(row, checkpoint, choice)
        decision = decideResume(checkpoint, action, choice)
        startPhase = decision.phase
        if decision.isReplan:
            # Task 6.5-4（MEDIUM-8）：计划点的 modify 不是「确认旧计划继续跑」，而是带
            # 用户反馈**重跑 planner**；故回到 plan 相位并置 replan 标记。2026-10-11 裁定：
            # 第 1..MAX 次 modify 会**重新暂停**在计划点（用户看得到新计划）。
            # Task B1：replan 上限由 decideResume 封顶；达到上限后它返 execute、不置 isReplan。
            state["replan"] = True
            state[OPT_REPLAN_COUNT] = int(state.get(OPT_REPLAN_COUNT, 0)) + 1
        if rewritten:
            state = rewriteState(state, rewritten)
```

> 改写分支移到 replan 之后：`rewritten` 非空时 `decideResume` 已返 `PHASE_ESL`、`isReplan=False`，两块互斥 ⇒ 与历史行为等价（历史里 `if rewritten` 覆盖 `startPhase`，且不进 `elif`）。
> 原来那段 `logger.info("replan 上限已达 …")` 已挪进 `decideResume`，此处**删除**，避免两处同义日志。

同文件 `_recordResumeTurn`（`:323-341`）第一行改成走同一推导点：

```python
        rewritten = rewrittenQuestion(choice)
```

并把 `decideResume` / `rewrittenQuestion` 加进该文件对 `research_agent_phases` 的 import 行。

- [ ] **Step 6: 写端点级失败测试**

`backend/app/tests/integration/test_research_api.py`，紧随 `test_checkpoint_answer_and_double_submit_409` 之后追加（同一套 fixture / monkeypatch 写法）：

```python
async def test_checkpoint_answer_at_planning_modify_reports_replan_phase(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession, monkeypatch
) -> None:
    """planning 点 modify ⇒ 机器重新暂停在 **plan**，响应不得报 execute（M3）。

    端点同步算这个值（后台任务之前），所以响应体与本断言不受后台执行影响；被判别的
    是「端点用的判定」而不是「机器实际跑成什么」，后者由
    test_planning_modify_reruns_planner_with_choice 覆盖。
    """
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fastService)
    svc = ResearchSessionService()
    userId = await _userId(dbSession, "research-user-a")
    row = await svc.createSession(
        dbSession, userId=userId, question="q", datasourceId=await _seededDatasourceId(dbSession)
    )
    turn = await svc.appendTurn(dbSession, sessionId=row.id, role="user", content={})
    cp = await svc.openCheckpoint(
        dbSession,
        sessionId=row.id,
        turnId=turn.id,
        phase="planning",
        options={"signal": "fixed_plan", "resumePhase": "execute", "replanCount": 0},
        prompt="计划是否确认？",
    )
    await dbSession.commit()

    resp = await client.post(
        f"{_BASE}/checkpoints/{cp.id}/answer",
        json={"action": "modify", "choice": {"instruction": "只看华东"}},
        headers=authHeaders,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sessionStatus": "running", "nextPhase": "plan"}
```

- [ ] **Step 7: 跑端点测试确认它红**（此时端点还是旧实现，这是本任务唯一的 RED 证据）

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/integration/test_research_api.py -v -k "checkpoint_answer"
```
Expected: `test_checkpoint_answer_at_planning_modify_reports_replan_phase` **FAIL**，实际 `nextPhase == "execute"`（旧实现读 `options["resumePhase"]`）；其余两条 `checkpoint_answer` 用例 PASS。
如果这条新用例此时就 PASS，**停下来**：说明你没测到 `resumeTurn` 走 replan 的那条路（检查 `options` 里是否真的写了 `replanCount: 0` 与 `resumePhase: "execute"`）。

- [ ] **Step 8: 端点改用决策函数**

`app/api/v1/research.py:88`：

```python
from app.services.research_agent_phases import decideResume
```

`:414`（`nextPhaseValue = nextPhase(checkpoint, payload.action)` 那行）：

```python
    nextPhaseValue = decideResume(checkpoint, payload.action, dict(payload.choice or {})).phase
```

- [ ] **Step 9: 跑端点测试确认绿（含反向护栏）**

同 Step 7 的命令。Expected: 3 条 `checkpoint_answer` 用例全 PASS（含既有的 `..._and_double_submit_409`、`..._invalid_action_422`，二者是**反向护栏**：确认既有语义没被决策函数改写）。

- [ ] **Step 10: 跑本任务的全部回归面**

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/unit/test_research_resume_decision.py app/tests/unit/test_research_checkpoint_prompt.py -v
```
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/integration/test_research_agent_service.py -v -k "resume or replan or modify or checkpoint"
```
Expected: 全 PASS（`test_empty_scope_resume_with_rewritten_question_reruns_esl`、`test_intent_checkpoint_confirm_still_resumes_at_plan`、两条 modify 用例是改写/推进分支的护栏）。

- [ ] **Step 11: 提交**

```bash
git add backend/app/services/research_agent_phases.py backend/app/services/research_agent_service.py backend/app/api/v1/research.py backend/app/tests/unit/test_research_resume_decision.py backend/app/tests/integration/test_research_api.py
git commit -m "fix(research): 端点与 resumeTurn 共用 decideResume，nextPhase 不再陈旧（M3）"
```

---

### Task 4: 图表维度标签真渲染 + 退役 parked xfail

**为什么**：`chart_renderer` 在任何 kind 下都**不输出维度显示名** —— 类目轴装的是维度**取值**，轴/图例没有任何地方写维度名。r3 里 `test_chart_labels_are_translated`（`xfail(strict=True)`）只能停在「brief 钉死的断言在报告路径上不可达」的备案状态，因为它的 fixture（2 维 × 2 行）走 `R13_RAW_DETAIL_TABLE` ⇒ TABLE 负载逐字回显 `spec.columns`（`site_code` 必然出现、`基地` 必然不出现）。

**本次真修口径（用户 2026-10-11 裁定「做真修：改共享 renderer」）**：给共享渲染器加一个**可选的**「维度字段 → 显示名」注入点，轴上渲染该显示名。

- **报告路径**注入（它本来就有 `aliasOverrides`：列名 → 显示名）。
- **chat 路径**不注入 ⇒ `axisLabels` 为空 ⇒ **渲染负载逐字不变**（由 Step 6 的回归测试钉住）。

设计取舍（写进实现注释，评审要看到）：显示名走**新增的 `ChartSpec.axisLabels` 映射**，不读 `SpecDimension.label` —— 因为该 label 在 chat 形态下是 `_prettify(field)` 兜底（英文列名美化），照读会把英文轴名强行加到 chat 图上。另外**恒等映射必须屏蔽**（`label != field` 才渲染）：否则某上游 alias 原样返回列名时，原始 `site_code` 会从轴名漏进 option（这正是 R4 那次「恒等短路」的同类坑）。

**Files:**
- Modify: `backend/app/domain/chart_spec.py`（`ChartSpec` 增 `axisLabels` 字段）
- Modify: `backend/app/services/chart_spec_builder.py`（`buildSpec` 增关键字参数并落到 spec）
- Modify: `backend/app/services/chart_renderer.py`（新增 `_axisNameOf`；`_buildAxisOption` / `_buildHeatmap` / `_buildWaterfall` 挂轴名）
- Modify: `backend/app/services/chart_integration.py`（把已有的 `aliasOverrides` 当 `axisLabels` 传下去）
- Test: `backend/app/tests/unit/test_report_planner.py`（退役 xfail + 新增 TABLE 契约用例）
- Test: `backend/app/tests/unit/test_chart_renderer.py`（新增 chat 零变更回归）
- Test: `backend/app/tests/unit/test_chart_spec_builder.py`（新增 axisLabels 落 spec 的判别）

**Interfaces:**
- Consumes: `aliasOverrides: Mapping[str, str] | None`（`chart_integration.buildReportChart` 的入参，`chart_spec_builder.buildSpec` 的既有参数）
- Produces:
  - `ChartSpec.axisLabels: Mapping[str, str]`（默认空 map；键 = 维度字段，值 = 显示名）
  - `buildSpec(..., *, aliasOverrides=None, axisLabels=None)`（`axisLabels` 为新增关键字参数）

- [ ] **Step 1: 写失败测试 —— builder 落 spec**

`backend/app/tests/unit/test_chart_spec_builder.py`，加进 `TestAliasOverrides` 类（`~:378`，紧接 `test_labels_stay_prettified_when_no_overrides` 之后），沿用该文件已有的 `_build` / `buildSpec` 直调两种既有写法：

```python
    def test_axisLabels_land_on_spec_when_injected(self) -> None:
        """报告形态：注入「维度字段 → 显示名」⇒ 落进 spec.axisLabels。"""
        signals = buildChartSignals(None, ("site_code", "bill_cnt"), _TRANSLATED_ROWS, "按基地分")
        decision = decideChartKind(signals, _THRESHOLDS)
        spec = buildSpec(
            decision,
            signals,
            _TRANSLATED_ROWS,
            None,
            aliasOverrides={"site_code": "基地", "bill_cnt": "账单数"},
            axisLabels={"site_code": "基地"},
        )
        assert spec.axisLabels == {"site_code": "基地"}

    def test_axisLabels_default_to_empty_for_chat_shape(self) -> None:
        """chat 形态（不注入）⇒ 空 map，渲染层据此不挂任何轴名。"""
        spec, _ = _build(["site_code", "bill_cnt"], _TRANSLATED_ROWS)
        assert spec.axisLabels == {}
```

- [ ] **Step 2: 跑测试确认它红**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/unit/test_chart_spec_builder.py -v -k axis_labels
```
Expected: FAIL —— `TypeError: buildSpec() got an unexpected keyword argument 'axisLabels'`（或 `AttributeError: 'ChartSpec' object has no attribute 'axisLabels'`）。

- [ ] **Step 3: `ChartSpec` 增字段**

`backend/app/domain/chart_spec.py`：文件顶部补 `from collections.abc import Mapping`（`dataclass` / `field` 已在 `:18` 导入，不要重复），`ChartSpec` 的**最后一个字段之后**（`specVersion: int = field(default=SPEC_VERSION)` 之下）追加：

```python
    # 维度字段 → 显示名（轴名用）。报告路径注入；chat 形态为空 map ⇒ 渲染负载逐字不变。
    axisLabels: Mapping[str, str] = field(default_factory=dict)
```

> 该 dataclass 的每个字段都已有默认值，追加带默认值的新字段不会破坏位置参数（`tableSpec` 等以关键字构造）。

> 该 dataclass 是 `frozen=True`。若加了这个可变默认字段后有测试因**不可哈希**而红（`frozen=True` 会生成 `__hash__`），把它改成 `field(default_factory=dict, compare=False)` 并保留其余语义 —— 这是唯一授权的退路，不要改用别的字段类型。

- [ ] **Step 4: `buildSpec` 落参数**

`backend/app/services/chart_spec_builder.py`，签名原样（`:236-243`，**同步函数**、第四个参数名是 `data` 不是 `rows`）只**新增**一个关键字参数，既有参数一个不动：

```python
def buildSpec(
    decision: ChartDecision,
    signals: ChartSignals,
    data: list[dict],
    plan: QueryPlan | None = None,
    *,
    aliasOverrides: Mapping[str, str] | None = None,
    axisLabels: Mapping[str, str] | None = None,
) -> ChartSpec:
```

构造 `ChartSpec(...)` 处（`:260` 起）追加一行：

```python
        axisLabels=dict(axisLabels or {}),
```

- [ ] **Step 5: 渲染器挂轴名**

`backend/app/services/chart_renderer.py`，紧接 `_dimensionOf`（`:115`）之后新增：

```python
def _axisNameOf(spec: ChartSpec, field: str | None) -> str | None:
    """类目轴的轴名 = 该维度的**显示名**；无注入或**恒等映射**时返 None（不挂轴名）。

    恒等映射必须屏蔽：上游 alias 原样返列名时，原始列名会经轴名漏进 option。
    """
    if not field:
        return None
    label = spec.axisLabels.get(field)
    return label if label and label != field else None


def _namedAxis(axis: dict[str, Any], name: str | None) -> dict[str, Any]:
    """给轴挂显示名；无名字时**原样返回**（chat 形态的负载因此逐字不变）。"""
    if not name:
        return axis
    return {**axis, "name": name}
```

`_buildAxisOption`：在 `horizontal` 分支之后、`option: dict[str, Any] = {...}` 之前插入一行（两种朝向共用同一个 `categoryAxis` 对象，故只此一处）：

```python
    categoryAxis = _namedAxis(categoryAxis, _axisNameOf(spec, catField))
```

`_buildHeatmap`：x 轴取 `dimensions[1]`、y 轴取 `dimensions[0]`：

```python
        "xAxis": _namedAxis(
            {"type": "category", "data": cols, "splitArea": {"show": True}},
            _axisNameOf(spec, colField),
        ),
        "yAxis": _namedAxis(
            {"type": "category", "data": rows, "splitArea": {"show": True}},
            _axisNameOf(spec, rowField),
        ),
```

`_buildWaterfall`：

```python
        "xAxis": _namedAxis(
            {"type": "category", "data": _categories(data, catField) if catField else []},
            _axisNameOf(spec, catField),
        ),
```

> `_buildPieLike` **不动**：饼图没有轴、图例已装类目，维度名没有既定槽位，加了即臆造 UI（YAGNI）。维度**取值**的翻译（C1 → 基地 C1）是另一件事 —— R4 已裁定「轴的维度取值翻译另开任务」，本任务不做。

- [ ] **Step 6: 报告路径注入 + chat 零变更回归测试**

`backend/app/services/chart_integration.py:50-51`：

```python
        spec: ChartSpec = buildSpec(
            decision, signals, rows, plan,
            aliasOverrides=aliasOverrides, axisLabels=aliasOverrides,
        )
        echartsOption = renderChartOption(spec, rows)
```

`backend/app/tests/unit/test_chart_renderer.py`，加进 `TestCartesian` 类（沿用该类现有的 `_spec(...)` + `_ROWS` 写法）：

```python
    def test_chat_shape_renders_no_axis_name(self) -> None:
        """chat 形态（spec.axisLabels 空）⇒ 类目轴不带 name 键，负载与既有行为一致。

        这是本任务对 chat 侧的**反向护栏**：共享渲染器被改了，但没注入就没轴名。
        """
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU"),
            dimensions=(_dim("SUPPLIER_NAME"),),
            measures=(_measure("RCV_QTY_PUU"),),
            series=(
                SpecSeries(
                    name="供货量", kind="bar",
                    dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU",
                ),
            ),
        )

        option = renderChartOption(spec, _ROWS)

        assert "name" not in option["xAxis"]
        assert option["xAxis"]["data"] == _CATS

    def test_injected_axis_labels_render_dimension_name_and_never_raw_field(self) -> None:
        """注入后：轴名 = 显示名；**恒等映射**不渲染（原始列名不得漏进 option）。"""
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU"),
            dimensions=(_dim("SUPPLIER_NAME"),),
            measures=(_measure("RCV_QTY_PUU"),),
            series=(
                SpecSeries(
                    name="供货量", kind="bar",
                    dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU",
                ),
            ),
            axisLabels={"SUPPLIER_NAME": "供应商", "RCV_QTY_PUU": "RCV_QTY_PUU"},
        )

        option = renderChartOption(spec, _ROWS)

        assert option["xAxis"]["name"] == "供应商"   # 显示名上了轴
        assert option["xAxis"]["data"] == _CATS       # 取值仍是数据键，不被翻译
```

- [ ] **Step 7: 退役 parked xfail，换成可达 fixture 上的真判别**

`backend/app/tests/unit/test_report_planner.py`：删除 `test_chart_labels_are_translated` 整个函数**及其 `@pytest.mark.xfail(...)` 装饰器**（`:684-713`），原位置替换为以下两条（第一条复用文件里**已存在**的 `fakeReportDepsWithSqlOnlyOneDim` fixture，它就在同一区域；第二条用 2 维 fixture 把 TABLE 契约钉住）：

```python
@pytest.mark.asyncio
async def test_chart_dimension_label_is_translated(fakeReportDepsWithSqlOnlyOneDim) -> None:
    """维度显示名真的进了 echartsOption（类目轴 name = 注入的显示名）。

    与 test_chart_measure_label_is_translated 互补：那条钉指标标签（图例系列名），这条钉
    维度标签（轴名）。两条此前都不可达 —— 渲染器从不输出维度名；本任务给共享渲染器加了
    「维度字段 → 显示名」注入点后，轴名可达。原用例（2 维 fixture）走 R13 明细表，
    TABLE 负载无轴可挂名，故判别点为单维度聚合形态（报告里真实存在）。
    """
    planner, sessionId, turnId = fakeReportDepsWithSqlOnlyOneDim
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    chart = next(
        b for s in payload["sections"] if s["kind"] == "data"
        for b in s["blocks"] if b["type"] == "chart"
    )
    assert chart["content"]["chartType"] == "bar"
    dumped = json.dumps(chart["content"]["echartsOption"], ensure_ascii=False)
    assert "基地" in dumped          # 维度显示名（轴名）
    assert "site_code" not in dumped  # 原始维度列名不得出现


@pytest.mark.asyncio
async def test_chart_table_payload_keeps_raw_columns_as_keys(fakeReportDepsWithSqlOnly) -> None:
    """TABLE 契约冻结：columns 是**行取值键**（前端 DataTable 按它取值），必须是原始列名。

    这条是上面那条「退役 xfail」的配套：明细表路径不做标签翻译，是契约决定的，
    不是缺陷 —— 前端契约更换前不得在此路径注入显示名。
    """
    planner, sessionId, turnId = fakeReportDepsWithSqlOnly
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    chart = next(
        b for s in payload["sections"] if s["kind"] == "data"
        for b in s["blocks"] if b["type"] == "chart"
    )
    assert chart["content"]["chartType"] == "table"
    assert "site_code" in chart["content"]["columns"]
```

- [ ] **Step 8: 跑本任务的测试面**

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/unit/test_chart_renderer.py app/tests/unit/test_chart_spec_builder.py app/tests/unit/test_chart_spec.py app/tests/unit/test_chart_decision.py -v
```
Expected: 全 PASS（含两条新回归；`test_chart_spec.py` 若因 `axisLabels` 字段的默认值/哈希而红，按 Step 3 的退路处理）。

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/unit/test_report_planner.py -v
```
Expected: 全 PASS，**且 `xfail` 计数从 1 变成 0**（`1 xfailed` 消失 —— 这是本任务的验收信号）。

- [ ] **Step 9: 提交**

```bash
git add backend/app/domain/chart_spec.py backend/app/services/chart_spec_builder.py backend/app/services/chart_renderer.py backend/app/services/chart_integration.py backend/app/tests/unit/test_chart_renderer.py backend/app/tests/unit/test_chart_spec_builder.py backend/app/tests/unit/test_report_planner.py
git commit -m "fix(chart): 共享渲染器支持维度显示名轴标签，报告路径注入（退役 r3 parked xfail）"
```

---

### Task 5: 抽出 `ports.py` 的模型解析接缝（M4）

**为什么**：`research_agent_ports.py` 已 **794/800 行**，下一次编辑即越界。抽出 Seam A（`:541-734`：`buildClient` → `resolveClient`，约 194 行）后落回 ~600 行。本任务**纯搬移**：不改一行逻辑、不改签名、不改行为。

**Files:**
- Create: `backend/app/services/research_agent_model_resolution.py`
- Modify: `backend/app/services/research_agent_ports.py`（删除被搬走的块；**不留再导出**）
- Modify: `backend/app/services/research_agent_service.py`（import 改指新家）
- Modify: `backend/app/tests/unit/test_research_model_selection.py`、`backend/app/tests/integration/test_research_agent_service.py`（同上）
- Modify + Test: `backend/app/tests/unit/test_research_agent_ports.py`（既有 import 块改指新家；新增 1 条「单一老家」用例）

**Interfaces:**
- Consumes: `research_agent_ports` 的 `emitEvent` / `errorPayload` / `ERROR_LLM_UNAVAILABLE` / `LLM_UNAVAILABLE_MESSAGE`（被搬走的块要用它们，故**留**在 ports.py）
- Produces: `app.services.research_agent_model_resolution.{buildClient, buildRoutingContext, PreferredModelUnavailableError, resolveModelConfig, markLlmUnavailable, resolveClient}` —— 新家是**唯一**老家

> **为什么不留再导出 shim**（r3 worktree 里核对后的裁定，实现者不得自行加回）：
> 被搬走的块（`markLlmUnavailable`）调 `ports.emitEvent` / `ports.errorPayload`，
> 所以新模块**必须** `import research_agent_ports`。若 ports.py 再反向 import 新模块，就是
> 一个**双向 import 环** —— 只在「shim 恰好在文件末尾」时靠执行顺序侥幸成立，
> 谁把 shim 挪到头部（本能的写法）就在启动时 `cannot import name 'emitEvent'`。
> 单向（新家 → ports）+ 改 4 个引用点，比一个靠顺序成立的环更小、更稳。

- [ ] **Step 1: 先确认接缝是闭合的（决定搬移边界与引用面）**

Run:
```bash
cd backend && grep -n "buildClient\|buildRoutingContext\|PreferredModelUnavailableError\|resolveModelConfig\|markLlmUnavailable\|_readConfigs\|_resolvePreferredConfig\|resolveClient" app/services/research_agent_ports.py
```
判据：**每一个命中行都必须落在 541–734 之内**；若某行在范围外（例如 `recordUsageQuietly` 之后仍引用了接缝符号），把该符号一并纳入搬移名单（并在报告里点名）。

同时确认外部引用面（**这是本任务唯一需要改引用点的地方，必须自己复核一遍**）：

> ⚠️ **不要用「`from app.services.research_agent_ports import` 那一行 + `-E` 过滤符号名」的写法**：本仓从 ports 取符号的地方**全是多行括号块**，符号名在后续行上，行内过滤会**一律漏掉**（实测输出为空）。必须**按符号名**扫：

```bash
cd backend && grep -rn "\bresolveClient\b\|\bresolveModelConfig\b\|\bPreferredModelUnavailableError\b\|\bbuildClient\b\|\bbuildRoutingContext\b\|\bmarkLlmUnavailable\b" app/ --include='*.py' | grep -v "^app/services/research_agent_ports.py"
```

Expected：**4 个文件、7 处 import**（控制者在开工前已按此命令复核过；多一处或措辞不同都必须在报告里点名）：

| 文件 | 从 ports 取走的符号 |
|---|---|
| `app/services/research_agent_service.py` | `resolveClient` |
| `app/tests/unit/test_research_model_selection.py` | `PreferredModelUnavailableError`、`resolveModelConfig` |
| `app/tests/unit/test_research_agent_ports.py` | `buildClient`、`buildRoutingContext`、`resolveModelConfig` |
| `app/tests/integration/test_research_agent_service.py` | `resolveModelConfig` |

该命令还会命中一批**不是 import** 的同名提及，**不要改**它们：`resolveClient` 作**形参名/注入点**（`research_agent_execution.py` 的 `deps.resolveClient`、各测试里的 `resolveClient=probe`）、以及 docstring 里的说明文字。

控制者已另行核对：`_readConfigs` / `_resolvePreferredConfig` 在 `ports.py` 之外**零引用**（随接缝一起搬走，无需改任何引用点）；`markLlmUnavailable` 在 ports 之外也**零引用**（只被 `resolveClient` 内部调用）。

被搬走的块自身还用到 ports 的 `emitEvent` / `errorPayload` / `ERROR_LLM_UNAVAILABLE` / `LLM_UNAVAILABLE_MESSAGE`（这些**留在** ports.py），新模块要 `from app.services.research_agent_ports import ...` 取它们 —— 这是单向依赖，不能反向。

- [ ] **Step 2: 写失败测试（单一老家契约）**

`backend/app/tests/unit/test_research_agent_ports.py` 追加：

```python
def test_model_resolution_seam_lives_only_in_new_module() -> None:
    """接缝搬走后，新家有、老家**没有**（M4：不留再导出，避免双向 import 环）。

    前半钉住搬移真的发生了（否则测试对「什么都没做」也绿）；后半钉住
    「只有一个老家」—— ports.py 若又长出同名定义/再导出，两条路径会各自演化，
    调用方 monkeypatch 哪一边都不确定的假绿就回来了。
    """
    from app.services import research_agent_model_resolution as newHome
    from app.services import research_agent_ports as oldPath

    for name in (
        "buildClient",
        "buildRoutingContext",
        "PreferredModelUnavailableError",
        "resolveModelConfig",
        "markLlmUnavailable",
        "resolveClient",
    ):
        assert hasattr(newHome, name), f"新家缺 {name}"
        assert not hasattr(oldPath, name), f"ports.py 仍暴露 {name}：单一老家被破坏"
```

- [ ] **Step 3: 跑测试确认它红**

Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/unit/test_research_agent_ports.py -v -k model_resolution_seam
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.services.research_agent_model_resolution'`。

> `-k` 的匹配串必须与 Step 2 的**函数名**对得上（`test_model_resolution_seam_lives_only_in_new_module`）。
> 若 `-k` 选中 0 条，pytest 会以「no tests ran」+ **退出码 5** 结束 —— 那不是本步骤要的 RED 证据
> （RED 必须是 ModuleNotFoundError）。看到「no tests ran」就说明你的 `-k` 串写错了，改串重跑。

- [ ] **Step 4: 建新模块，**逐字**搬移**

新建 `backend/app/services/research_agent_model_resolution.py`：

```python
"""研究链路的 LLM 模型解析接缝（Task 5 从 research_agent_ports 抽出，保持该文件 ≤ 800 行）。

内容为**逐字搬移**：`buildClient` / `buildRoutingContext` / `PreferredModelUnavailableError`
/ `resolveModelConfig` / `_readConfigs` / `_resolvePreferredConfig` / `markLlmUnavailable`
/ `resolveClient`（原 `research_agent_ports.py:541-734`）。**不改一行逻辑、不改签名**。

依赖方向是单向的：本模块 import `research_agent_ports` 取 `emitEvent` / `errorPayload` /
`ERROR_LLM_UNAVAILABLE` / `LLM_UNAVAILABLE_MESSAGE`（这四个留在 ports.py）；
`research_agent_ports` **不** import 本模块（无环）。
"""
```

然后把 `research_agent_ports.py:541-734` 整段**原样**贴到 docstring 之后，再按 ruff / ImportError 补齐该文件需要的 import（`emitEvent`、`errorPayload`、两个错误常量从 `research_agent_ports` 取；ORM / 标准库 import 按需）。**不要**顺手重命名、重排、注释、拆函数。

- [ ] **Step 5: 改 4 个引用点（不留再导出）**

`research_agent_ports.py`：把 541–734 整段删掉，**不要**加任何再导出（理由见本任务 Interfaces 下的裁定框）。

四处 import 改指新家（新模块名 `research_agent_model_resolution` 按字母序**排在** `research_agent_ports` 之前，拆出来的 import 块要放在 ports 块**上面**，否则 ruff I001 会红）：

`app/services/research_agent_service.py`：在它从 ports 的 import 块里把 `resolveClient` 那行删掉，另起一行新模块 import：
```python
from app.services.research_agent_model_resolution import resolveClient
```
`app/tests/unit/test_research_model_selection.py`：把 import 块里的 `resolveModelConfig`、`PreferredModelUnavailableError` 两行删掉，另起：
```python
from app.services.research_agent_model_resolution import (
    PreferredModelUnavailableError,
    resolveModelConfig,
)
```
`app/tests/unit/test_research_agent_ports.py`：它的 ports 块里 `LlmUsageRecorder` / `MeteredClient` / `selectClassesForTables` **留在**原块，只把 `buildClient` / `buildRoutingContext` / `resolveModelConfig` 三行拆出去：
```python
from app.services.research_agent_model_resolution import (
    buildClient,
    buildRoutingContext,
    resolveModelConfig,
)
from app.services.research_agent_ports import (
    LlmUsageRecorder,
    MeteredClient,
    selectClassesForTables,
)
```
`app/tests/integration/test_research_agent_service.py`：`resolveModelConfig` 同样改指新模块（该文件从 ports 还取 `MAX_REPLAN_PER_TURN` / `OPT_REPLAN_COUNT` / `STEP_MISSING_SQL` / `LlmUsageRecorder`，这些留在原块）。

> `resolveClient` 作为**形参名**出现的地方（`research_agent_execution.py:103/118/132`、
> 各测试里的 `resolveClient=probe`）**不要动** —— 那是注入的可调用，与 import 无关。
>
> `app/tests/unit/test_research_model_selection.py:221/235` 的
> `monkeypatch.setattr(research_agent_service, "resolveClient", ...)` 也**不要动** ——
> 它打的是**被 import 进 service 模块的绑定**，Step 5 改完 import 后该属性依然存在。

- [ ] **Step 6: 跑测试确认全绿 + 行数达标**

```bash
cd backend && wc -l app/services/research_agent_ports.py app/services/research_agent_model_resolution.py
```
Expected: `ports.py` ≈ 600（**必须 < 800**），新模块 ≈ 195。

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/unit/test_research_agent_ports.py app/tests/unit/test_research_model_selection.py -v
```
Expected: 全 PASS（含 Step 2 的「单一老家」用例；`test_research_model_selection.py` 的 import 已在 Step 5 改指新家，若这里 `ImportError` 说明漏改）。

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/integration/test_research_agent_service.py -v
```
Expected: 与搬移前**同一套**结果（搬移是行为中性的，出现新红即为搬移错误，须修 import 而不是改逻辑）。

```bash
cd backend && ruff check --select F401,F821 app/services/research_agent_ports.py app/services/research_agent_model_resolution.py
```
Expected: 无输出（无未用 import、无未定义名）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/research_agent_ports.py backend/app/services/research_agent_model_resolution.py backend/app/services/research_agent_service.py backend/app/tests/unit/test_research_agent_ports.py backend/app/tests/unit/test_research_model_selection.py backend/app/tests/integration/test_research_agent_service.py
git commit -m "refactor(research): 抽出模型解析接缝，ports.py 回到 600 行内（M4）"
```

---

## 收尾：整支验证与变更记录

- [ ] **Step 1: 分进程跑全部研究面测试**（**不许**同进程混跑 unit 与 integration）

```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/unit/test_research_agent_execution.py app/tests/unit/test_research_agent_ports.py app/tests/unit/test_research_checkpoint_prompt.py app/tests/unit/test_research_event_bus.py app/tests/unit/test_research_finding_aggregator.py app/tests/unit/test_research_hypothesis_adapter.py app/tests/unit/test_research_hypothesis_rewrite.py app/tests/unit/test_research_model_selection.py app/tests/unit/test_research_models.py app/tests/unit/test_research_schema_reflection.py app/tests/unit/test_research_sql_runner.py app/tests/unit/test_seed_menu_research.py app/tests/unit/test_research_replan_count_ssot.py app/tests/unit/test_research_resume_decision.py app/tests/unit/test_report_planner.py app/tests/unit/test_chart_renderer.py app/tests/unit/test_chart_spec_builder.py -v
```
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test TEST_NEO4J_URI=bolt://localhost:7688 python -m pytest app/tests/integration/test_research_adapters.py app/tests/integration/test_research_agent_service.py app/tests/integration/test_research_api.py app/tests/integration/test_research_hypothesis_rewrite_wiring.py app/tests/integration/test_research_session_service.py app/tests/integration/test_research_sql_runner.py app/tests/integration/test_research_stream.py -v
```
Expected: 与本计划开工前**同一套红/绿**，唯一变化是新增用例全绿、`test_report_planner.py` 的 `1 xfailed` 归零。
**已知继承债（不是本支引入，必须在报告里点名）**：`test_research_event_bus.py`、`test_research_hypothesis_rewrite_wiring.py`、`test_research_session_service.py` 有 4 处红，其涉及文件在本支 0 次改动，属于 `feat-research-report-analysis` 的旧债（陈旧 frozen code-list / 陈旧 fake double）。判别命令：`git rev-list --count <base>..HEAD -- <file>`。

- [ ] **Step 2: 更新变更记录（SSOT）**

在 `Harness/changes/` 下为本分支新建/更新一份记录，写清：M1–M4 各自「改前症状 → 改法 → 判别测试」、图表维度标签的口径裁定（新注入点、chat 零变更、TABLE 契约冻结、维度**取值**翻译仍另开任务）、以及本支的 base 与继承红灯清单。

- [ ] **Step 3: 汇报「不做」的事**（写进最终报告，不要静默）

- **不进本计划的**：仓库级 `ruff check --select F821` 闸（基线 30 条，全为 `from __future__ import annotations` 模块的注解位名字，且基线含禁改的 chat 侧文件 ⇒ 上闸必须钉基线，是独立议题）。
- **仍开放的**：维度**取值**翻译（C1 → 基地 C1）、`r3 → feat-research-report-analysis` 的合并决定（r3 冻结在 `61986a9`）、`llm_config` 测试库结构漂移（缺 `disable_thinking`，修它要 alembic）。

## Self-Review

- **来源覆盖**：M1 → Task 1；M2 → Task 2；M3 → Task 3；M4 → Task 5；parked xfail（用户裁定「做真修」）→ Task 4。终评列的 L 级观察项不在修复面（L1–L3 已裁定「无需再改代码」）。
- **无占位符**：每个代码步骤都给了可粘贴的实现或**逐字搬移**的精确行区间 + 判定命令；`_buildSpecFor` / `_barSpec` 两处引用了测试文件的既有 helper，并明确「用现场的 helper 名，不要新写工厂」（DRY）。
- **类型/命名一致**：`meteringModelName(client, metered=None)`、`decideResume(checkpoint, action, choice) -> ResumeDecision(phase, isReplan)`、`rewrittenQuestion(choice)`、`ChartSpec.axisLabels: Mapping[str, str]`、`buildSpec(..., axisLabels=None)`、`_axisNameOf` / `_namedAxis` 在各 task 间同名同形。
- **TDD 诚实性**：Task 1 是行为中性重构，其判别是**结构守卫**（源码扫描）+ 既有 replan 行为测试；Task 5 是纯搬移，其判别是 **新家有/老家没有**（单一老家）+ 既有套件；这两处不假装有行为型 RED —— 其余三个 task 都有真 RED（Task 2 自报名/配置名分叉、Task 3 端点多报 execute、Task 4 无轴名）。
- **反向护栏**：Task 3 保留 `..._invalid_action_422` 与 `..._double_submit_409`（确认既有语义没被决策函数改写）；Task 4 的 chat 零变更用例 + TABLE 契约用例是「改了共享件但不许外溢」的反向断言。
- **口径一致性**：M2 让 5 个计量站点同写 `meteringModelName(...)`（plan 相位原本就正确）；M1 让读写两侧都走 `OPT_REPLAN_COUNT`；M3 让端点与机器共用 `decideResume` —— 三条都是「消灭第二份真相」，没有引入新模式。
