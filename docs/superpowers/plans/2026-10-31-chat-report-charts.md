# 分析计划图表进最终报告 + 多类别图表可读性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让多步分析的最终回答按步骤分节、每节末尾带该步的图，并让类别很多的柱状图按类别数增高、超限可缩放。

**Architecture:** 后端把聚合文本解析成 `text` / `chart(stepIndex)` 分块下发给前端（无数据库迁移，回放时现算）；前端按块渲染，chart 块从 `message.steps[stepIndex]` 取既有图负载。图表高度由 option 结构推断（类目轴长度），在线渲染与 PNG 导出共用同一函数；后端只在类目轴补充标签密度与 `dataZoom` 结构配置。

**Tech Stack:** FastAPI + Pydantic v2（snake_case 字段 / camelCase 别名）、SQLAlchemy async、pytest（真实 PG/Neo4j）、React 18 + TypeScript、vitest、ECharts 5。

## Global Constraints

- 所有 git 操作在 worktree `/private/tmp/qa-report-charts` 内执行；不要 `cd` 回主仓。
- 共享 stash 栈：禁止裸 `git stash` / `git stash pop`；需要时 `git stash push -u -m "<tag>"` → 记 SHA → `git stash apply <sha>` → 按 tag 找到位置后 drop。
- 后端测试每条命令都必须同时导出：
  `TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'`、
  `TEST_NEO4J_URI='bolt://127.0.0.1:7688'`、`TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'`（Bash env 不跨调用）。
- 停 pytest 用 SIGINT（Ctrl-C），不要 `kill -9`。
- 不修改 `raw/`；不硬编码任何 secret；不重新生成 SECRET_KEY。
- 服务端只发图表结构、不发颜色（`chart_renderer.py` 既有契约）；颜色只在前端主题层补。
- 所有函数保持不可变：返回新 dict / list，不原地改入参。
- 文件 200-400 行为宜、≤800 行；函数 <50 行；嵌套 ≤4 层；错误显式处理。
- 每个 Task 的 TDD 顺序固定：写失败测试 → 跑出预期失败 → 最小实现 → 转绿 → commit。
- commit 不加 attribution；格式 `<type>: <description>`。
- 本批**不做**：PDF 分节插图、Top-N 截断、计划卡改版、研究线 `EChart.tsx`、单步顶层图逻辑。

---

### Task 0: 基线取证

**Files:** 无改动。

**Interfaces:**
- Consumes: 当前分支 `feat/chat-report-charts`。
- Produces: 本批开始前的红/绿基线，供 Task 10 判定「净新增失败 = 0」。

- [ ] **Step 1: 确认工作树干净且分支正确**

Run:
```bash
git status --short
git branch --show-current
```
Expected: 无输出（干净）；`feat/chat-report-charts`。

- [ ] **Step 2: 跑后端相关套件基线**

Run:
```bash
cd backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/unit/test_step_aggregator.py \
       app/tests/unit/test_chart_renderer.py \
       app/tests/integration/test_chat_multi_step.py \
       app/tests/integration/test_chat_history_api.py -q
```
Expected: 全绿或记录既有失败（既有的 `test_chat_multi_step.py` 较慢，允许 >5 分钟；失败项逐条抄进 Task 10 的对照表）。

- [ ] **Step 3: 跑前端基线**

Run:
```bash
cd frontend
npx vitest run src/tests/chartSnapshot.test.ts src/tests/ChartRenderer.test.tsx \
  src/tests/MessageItem.test.tsx src/tests/chatStore.test.ts
npx tsc --noEmit
```
Expected: 套件绿；`tsc` 0 错误（记忆基线：tsc 为 0 错误）。

---

### Task 1: 后端报告分节纯函数

**Files:**
- Create: `backend/app/services/report_sections.py`
- Test: `backend/app/tests/unit/test_report_sections.py`

**Interfaces:**
- Consumes: 任意带 `step_index`/`chart_type`/`chart_option`/`sql`/`description` 的对象，或 camelCase 映射（`stepIndex`/`chartType`/...，历史快照形状）。
- Produces: `buildReportSections(answer: str, steps: Sequence[Any] | None) -> list[dict[str, Any]]`；
  块形状 `{"kind": "text", "content": str}` 或 `{"kind": "chart", "stepIndex": int, "title": str}`。

- [ ] **Step 1: 写失败测试**

Create `backend/app/tests/unit/test_report_sections.py`:

```python
"""报告分节解析（2026-10-31）：纯函数、无 IO、绝不抛错。"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.report_sections import buildReportSections


def _step(index: int, **overrides):
    base = {
        "stepIndex": index,
        "description": f"步骤{index + 1}描述",
        "sql": "SELECT 1",
        "chartType": "bar",
        "chartOption": {"series": []},
    }
    base.update(overrides)
    return base


def test_splits_sections_and_places_chart_after_each_section() -> None:
    answer = (
        "## 分析报告\n"
        "### 步骤 1：2024 销售额\n"
        "2024 年销售额 1000 万。\n"
        "\n"
        "### 步骤 2：2025 销售额\n"
        "2025 年销售额 1200 万。\n"
        "\n"
        "### 结论\n"
        "同比增长 20%。"
    )

    blocks = buildReportSections(answer, [_step(0), _step(1)])

    assert blocks == [
        {"kind": "text", "content": "## 分析报告\n### 步骤 1：2024 销售额\n2024 年销售额 1000 万。"},
        {"kind": "chart", "stepIndex": 0, "title": "步骤1描述"},
        {"kind": "text", "content": "### 步骤 2：2025 销售额\n2025 年销售额 1200 万。"},
        {"kind": "chart", "stepIndex": 1, "title": "步骤2描述"},
        {"kind": "text", "content": "### 结论\n同比增长 20%。"},
    ]


def test_accepts_step_keyword_fullwidth_colon_and_heading_levels() -> None:
    answer = "## 分析报告\n#### Step 2：2025 销售额\n增长 20%。"

    blocks = buildReportSections(answer, [_step(0), _step(1)])

    assert [b["kind"] for b in blocks] == ["text", "chart"]
    assert blocks[1]["stepIndex"] == 1


def test_failed_step_heading_stays_text_and_gets_no_chart() -> None:
    answer = (
        "### 步骤 1：2024 销售额\n1000 万。\n"
        "### 步骤 2：2025 销售额\n本步失败。"
    )
    failed = _step(1, sql=None, chartType=None, chartOption=None)

    blocks = buildReportSections(answer, [_step(0), failed])

    assert [b["kind"] for b in blocks] == ["text", "chart", "text"]
    assert [b.get("stepIndex") for b in blocks if b["kind"] == "chart"] == [0]


def test_duplicate_reference_inserts_chart_once() -> None:
    answer = "### 步骤 1：A\n一。\n### 步骤 1：再看 A\n二。"

    blocks = buildReportSections(answer, [_step(0)])

    assert [b["kind"] for b in blocks] == ["text", "chart", "text"]
    assert len([b for b in blocks if b["kind"] == "chart"]) == 1


def test_unreferenced_chart_steps_are_appended_under_key_charts() -> None:
    answer = "### 步骤 1：A\n一。"

    blocks = buildReportSections(answer, [_step(0), _step(1)])

    assert blocks[-2] == {"kind": "text", "content": "### 关键图表"}
    assert blocks[-1] == {"kind": "chart", "stepIndex": 1, "title": "步骤2描述"}


def test_falls_back_to_text_plus_charts_when_no_section_heading() -> None:
    blocks = buildReportSections("直接给结论。", [_step(0), _step(1)])

    assert blocks == [
        {"kind": "text", "content": "直接给结论。"},
        {"kind": "chart", "stepIndex": 0, "title": "步骤1描述"},
        {"kind": "chart", "stepIndex": 1, "title": "步骤2描述"},
    ]


def test_empty_input_or_no_chart_steps_returns_empty() -> None:
    assert buildReportSections("", [_step(0)]) == []
    assert buildReportSections("   ", [_step(0)]) == []
    assert buildReportSections("纯文字", []) == []
    assert buildReportSections("纯文字", None) == []


def test_supports_snake_case_objects() -> None:
    step = SimpleNamespace(
        step_index=0,
        description="2024 销售额",
        sql="SELECT 1",
        chart_type="bar",
        chart_option={"series": []},
    )

    blocks = buildReportSections("### 步骤 1：2024\n1000 万。", [step])

    assert blocks[1] == {"kind": "chart", "stepIndex": 0, "title": "2024 销售额"}
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd backend
pytest app/tests/unit/test_report_sections.py -q
```
Expected: `ModuleNotFoundError: No module named 'app.services.report_sections'`。

- [ ] **Step 3: 写最小实现**

Create `backend/app/services/report_sections.py`:

```python
"""多步最终报告的结构化分节（2026-10-31）。

StepAggregator 产出的汇总是**一段纯文字**（0107），每步的图只活在 steps[i]。
本模块把文字按「### 步骤 N」切成 text 块，并在对应节末尾插入 chart 块；图负载
不在这里复制，只给 stepIndex，前端从 message.steps 取。

纯函数、无 IO、绝不抛错：模型输出是外部输入，格式出错就退回「原文 + 文末图」，
不能让一次渲染把整条回答带崩（与 chart_renderer 的「绝不抛错」同一原则）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# 宽松匹配 `## 步骤 1：描述` / `#### Step 1. 描述` / 全角冒号与顿号。
_HEADING_RE = re.compile(
    r"^#{1,4}\s*(?:步骤|Step)\s*(\d+)\s*[:：.、]?\s*(.*)$",
    re.IGNORECASE,
)
_ORPHAN_TITLE = "关键图表"


def _field(step: Any, snake: str, camel: str | None = None) -> Any:
    """同时支持 ORM/DTO 对象（snake_case 属性）与回放快照（camelCase dict）。"""
    if isinstance(step, Mapping):
        if camel is not None and camel in step:
            return step[camel]
        return step.get(snake)
    return getattr(step, snake, None)


def _hasChart(step: Any) -> bool:
    return (
        _field(step, "sql") is not None
        and _field(step, "chart_type", "chartType") is not None
        and _field(step, "chart_option", "chartOption") is not None
    )


def _chartBlock(step: Any) -> dict[str, Any]:
    index = _field(step, "step_index", "stepIndex")
    title = _field(step, "description") or f"步骤 {int(index) + 1}"
    return {"kind": "chart", "stepIndex": int(index), "title": title}


def _splitSections(
    text: str, byIndex: Mapping[int, Any]
) -> tuple[list[dict[str, Any]], set[int]]:
    blocks: list[dict[str, Any]] = []
    buffer: list[str] = []
    pending: int | None = None
    referenced: set[int] = set()

    def flushText() -> None:
        content = "\n".join(buffer).strip("\n")
        if content.strip():
            blocks.append({"kind": "text", "content": content})
        buffer.clear()

    def flushChart() -> None:
        nonlocal pending
        if pending is not None:
            blocks.append(_chartBlock(byIndex[pending]))
            referenced.add(pending)
            pending = None

    for line in text.split("\n"):
        match = _HEADING_RE.match(line.strip())
        if match is None:
            buffer.append(line)
            continue
        # 只有「上一节确实要插图」时才收口：正文与图一起在节末落地。上一节无图
        # （导语 / 失败步 / 结论）则继续并入同一段 —— 否则「## 分析报告」会单独成块。
        if pending is not None:
            flushText()
            flushChart()
        buffer.append(line)
        index = int(match.group(1)) - 1
        if index in byIndex and index not in referenced:
            pending = index
    flushText()
    flushChart()
    return blocks, referenced


def buildReportSections(
    answer: str, steps: Sequence[Any] | None
) -> list[dict[str, Any]]:
    """把最终回答切成 text/chart 块；不合法输入一律降级，不抛错。"""
    text = (answer or "").strip()
    if not text:
        return []

    byIndex: dict[int, Any] = {}
    for step in steps or []:
        index = _field(step, "step_index", "stepIndex")
        if isinstance(index, int) and _hasChart(step):
            byIndex.setdefault(index, step)
    if not byIndex:
        return []

    blocks, referenced = _splitSections(text, byIndex)
    if not referenced:
        # 模型完全没按格式输出：原文 + 文末图，功能不丢。
        return [{"kind": "text", "content": text}] + [
            _chartBlock(step) for _, step in sorted(byIndex.items())
        ]

    missing = [(index, step) for index, step in sorted(byIndex.items()) if index not in referenced]
    if missing:
        blocks.append({"kind": "text", "content": f"### {_ORPHAN_TITLE}"})
        blocks.extend(_chartBlock(step) for _, step in missing)
    return blocks
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd backend
pytest app/tests/unit/test_report_sections.py -q
```
Expected: `8 passed`。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/report_sections.py backend/app/tests/unit/test_report_sections.py
git commit -m "feat: 多步最终回答分节解析纯函数"
```

---

### Task 2: 汇总提示词强制分节

**Files:**
- Modify: `backend/app/services/step_aggregator.py:19-23`
- Test: `backend/app/tests/unit/test_step_aggregator.py`

**Interfaces:**
- Consumes: 无。
- Produces: `_STEP_AGGREGATOR_SYSTEM_PROMPT` 含 `## 分析报告` / `### 步骤 N：` / `### 结论`，Task 1 的解析器因此能稳定命中。

- [ ] **Step 1: 写失败测试**

Append to `backend/app/tests/unit/test_step_aggregator.py`（文件末尾新增一个测试函数，不改动既有类）：

```python
def test_system_prompt_requires_sectioned_report() -> None:
    from app.services.step_aggregator import _STEP_AGGREGATOR_SYSTEM_PROMPT

    prompt = _STEP_AGGREGATOR_SYSTEM_PROMPT
    assert "## 分析报告" in prompt
    assert "### 步骤 N：<该步骤的描述>" in prompt
    assert "### 结论" in prompt
    # 失败步骤不成节 —— 否则解析器会给失败步找不到图，生成空节
    assert "不要单独成节" in prompt
    # 既有安全/诚实约束不能丢（本次只改结构，不改行为底线）
    assert "不得编造" in prompt
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd backend
pytest app/tests/unit/test_step_aggregator.py::test_system_prompt_requires_sectioned_report -q
```
Expected: FAIL，断言 `"## 分析报告" in prompt` 不成立。

- [ ] **Step 3: 改提示词**

Replace `_STEP_AGGREGATOR_SYSTEM_PROMPT`（`step_aggregator.py:19-23`）为：

```python
_STEP_AGGREGATOR_SYSTEM_PROMPT = (
    "你是企业数据分析助手。用户提出复合问题，系统已按子步骤执行多个查询并产出结果。\n"
    "请严格按照下面的 Markdown 结构输出，不要省略任何有数据的数据步骤：\n\n"
    "## 分析报告\n"
    "### 步骤 1：<该步骤的描述>\n"
    "<基于该步骤结果的分析，引用关键数字>\n"
    "### 步骤 2：<该步骤的描述>\n"
    "<...>\n"
    "### 结论\n"
    "<跨步骤的对比、差异与趋势>\n\n"
    "规则：\n"
    "1. 每个有数据的数据步骤单独成节，标题固定为「### 步骤 N：<描述>」，"
    "N 从 1 开始且与输入步骤号一致。\n"
    "2. 失败或无数据的步骤不要单独成节，只在「结论」中如实说明，不得编造结果。\n"
    "3. 若步骤间出现数据不一致或缺失，如实说明而非编造。\n"
    "4. 不要输出代码块、SQL 或额外的顶层标题。"
)
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd backend
pytest app/tests/unit/test_step_aggregator.py -q
```
Expected: 全绿（含既有 prompt 构造用例）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/step_aggregator.py backend/app/tests/unit/test_step_aggregator.py
git commit -m "feat: 多步汇总提示词强制分节格式"
```

---

### Task 3: 非流式响应契约 + 落库快照对齐

**Files:**
- Modify: `backend/app/domain/schemas.py:1981-1985`（`ChatResponse`）
- Modify: `backend/app/services/chat_helpers.py:137-161`（`_multiStepResponse`）
- Modify: `backend/app/services/chat_multistep.py:41-52`（import）、`:710-721`（落库）
- Test: `backend/app/tests/integration/test_chat_multi_step.py`

**Interfaces:**
- Consumes: `buildReportSections(answer, steps)`（Task 1）。
- Produces: `ChatResponse.report_sections: list[dict] | None`（JSON `reportSections`）；多步非流式消息落库含 `steps_snapshot`。

- [ ] **Step 1: 写失败测试（改共享 LLM 的汇总文案 + 加断言）**

在 `test_chat_multi_step.py` 的 `_MultiStepLlm.complete` 中，把汇总分支（`:79`）改为：

```python
        elif "企业数据分析助手" in system:
            _Resp.content = (
                "## 分析报告\n"
                "### 步骤 1：2024 年销售额\n"
                "2024 年销售额已执行。\n"
                "\n"
                "### 步骤 2：2025 年销售额\n"
                "2025 年销售额已执行。\n"
                "\n"
                "### 结论\n"
                "2025 年销售额较 2024 年增长 25%。"
            )
```

在 `TestMultiStepChatApi.test_explicit_step_request_executes_multi_step` 中，`assert "增长" in body["answer"]` 之后追加：

```python
        # 2026-10-31（分节报告）：报告按步骤分块，图块只带 stepIndex
        sections = body["reportSections"]
        assert [b["kind"] for b in sections] == ["text", "chart", "text", "chart", "text"]
        assert [b["stepIndex"] for b in sections if b["kind"] == "chart"] == [0, 1]
```

在同文件 `TestReplayPlanCardSnapshot.test_multistep_summary_persists_steps_snapshot` 之后新增：

```python
    async def test_non_stream_multistep_summary_persists_steps_snapshot(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """非流式汇总落库与流式对齐：不落快照 ⇒ 回放的新报告缺图。"""
        config, ds = await _seed(dbSession)
        _install(monkeypatch, config, _MultiStepLlm(), _OkAdapter())

        resp = await client.post(
            "/api/v1/chat",
            json=_payload("请分步查询 2024 和 2025 年的销售额并对比", ds.id),
        )
        assert resp.status_code == 200, resp.text

        msgs = list(
            (await dbSession.execute(select(SessionMessage).order_by(SessionMessage.id)))
            .scalars()
            .all()
        )
        snapshot = msgs[1].steps_snapshot
        assert isinstance(snapshot, list)
        assert [s["stepIndex"] for s in snapshot] == [0, 1, 2]
        assert snapshot[0]["sql"] is not None
        assert snapshot[2]["sql"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/integration/test_chat_multi_step.py::TestMultiStepChatApi::test_explicit_step_request_executes_multi_step \
       app/tests/integration/test_chat_multi_step.py::TestReplayPlanCardSnapshot::test_non_stream_multistep_summary_persists_steps_snapshot -q
```
Expected: 前者 `KeyError: 'reportSections'`；后者 `assert None is list`（或 `steps_snapshot` 为 None）。

- [ ] **Step 3: 加 schema 字段**

在 `ChatResponse.steps`（`schemas.py:1981-1985`）之后插入：

```python
    # 2026-10-31（分节报告）：多步最终回答的结构化分块。chart 块只带 stepIndex，
    # 图负载复用 steps[i]，不重复下发。仅多步聚合/降级收尾填充；其余意图为 None。
    report_sections: list[dict] | None = Field(
        default=None,
        description="多步报告分块：{kind:'text',content} | {kind:'chart',stepIndex,title}",
    )
```

- [ ] **Step 4: 响应里现算**

在 `chat_helpers.py` 顶部 import 区加入：

```python
from app.services.report_sections import buildReportSections
```

把 `_multiStepResponse` 的 `return ChatResponse(` 段改为（注意先构造一次 `stepReads`，避免两次转换）：

```python
    stepReads = [_step_result_to_read(s, s.query_plan) for s in completed]
    return ChatResponse(
        answer=answer,
        intent="multi_step",
        chartType=None,
        chartOption=None,
        visual_rationale=visualRationale,
        data=data,
        steps=stepReads,
        # 2026-10-31：分节块由最终文本 + 每步结果现算，不落库。
        report_sections=buildReportSections(answer, stepReads),
        tokensUsed=tokensUsed,
        cost=float(cost),
        latency_ms=int((time.monotonic() - t0) * 1000),
        modelName=modelName,
        affinityStatus=affinityStatus,
        classRecall=classRecall,
        hypotheses=hypotheses,
        queryPlan=queryPlan.to_dict() if queryPlan else None,
    )
```

- [ ] **Step 5: 非流式汇总补 steps_snapshot**

在 `chat_multistep.py` 的 `chat_helpers` import 块（`:41-52`）加入 `buildStepsSnapshot`：

```python
from app.services.chat_helpers import (
    _MSG_STEP_UNANSWERABLE,
    _STEP_EXEC_FAILED_PREFIX,
    _STEP_GEN_FAILED_PREFIX,
    _failedStepResult,
    _hasDataStepResult,
    _multiStepResponse,
    _PipelineContext,
    _step_result_to_read,
    _stepFailedError,
    _StepRun,
    buildStepsSnapshot,
)
```

在 `:710-721` 的 `_storeSessionMessages(...)` 里、`visual_rationale=summaryTextOnlyRationale().to_dict(),` 之后追加：

```python
                    # 2026-10-31：与流式对齐 —— 非流式多步也要能回放分节图。
                    steps_snapshot=buildStepsSnapshot(
                        plan=multiStepPlan,
                        stepsByIdx=stepsByIdx,
                        completed=completed,
                        runId=str(run.id) if run is not None else None,
                    ),
```

- [ ] **Step 6: 跑测试确认通过**

Run: 同 Step 2 的命令。
Expected: `2 passed`。

- [ ] **Step 7: Commit**

```bash
git add backend/app/domain/schemas.py backend/app/services/chat_helpers.py \
        backend/app/services/chat_multistep.py backend/app/tests/integration/test_chat_multi_step.py
git commit -m "feat: 非流式多步响应下发 reportSections 并落回放快照"
```

---

### Task 4: 流式 done 帧下发 reportSections

**Files:**
- Modify: `backend/app/services/chat_stream_multistep.py:37-44`（import）、`:295-316`（done 帧）
- Test: `backend/app/tests/integration/test_chat_multi_step.py`

**Interfaces:**
- Consumes: `buildReportSections`（Task 1）、`StepResultRead` 列表。
- Produces: `EVENT_DONE` payload 的 `reportSections`（camelCase，list[dict]）。

- [ ] **Step 1: 写失败测试**

在 `TestMultiStepChatStreamApi.test_stream_explicit_step_emits_step_events` 的 `done = frames[-1][1]` 断言区追加：

```python
        # 2026-10-31（分节报告）：done 帧带分块，前端据此替换纯 markdown 渲染
        sections = done["reportSections"]
        assert [b["kind"] for b in sections] == ["text", "chart", "text", "chart", "text"]
        assert [b["stepIndex"] for b in sections if b["kind"] == "chart"] == [0, 1]
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/integration/test_chat_multi_step.py::TestMultiStepChatStreamApi::test_stream_explicit_step_emits_step_events -q
```
Expected: `KeyError: 'reportSections'`。

- [ ] **Step 3: 实现**

在 `chat_stream_multistep.py` 的 `chat_helpers` import 块加入：

```python
from app.services.report_sections import buildReportSections
```

在 `yield StreamEvent(EVENT_TOKEN, {"content": agg_content})` 之前构造：

```python
                stepReads = [_step_result_to_read(s) for s in completed]
```

把 `EVENT_DONE` 的 payload 改为：

```python
                yield StreamEvent(
                    EVENT_DONE,
                    {
                        "tokensUsed": total_tokens,
                        "cost": float(total_cost),
                        "modelName": last_model_name,
                        "latency_ms": int((time.monotonic() - _ms_t0) * 1000),
                        "affinityStatus": affinity_payload,
                        "steps": [s.model_dump(by_alias=True) for s in stepReads],
                        # 2026-10-31：分节块与最终文本同源现算；图负载不重复下发。
                        "reportSections": buildReportSections(agg_content, stepReads),
                        # 汇总步不发 step_result（纯文字），SUMMARY_TEXT_ONLY 只能经 done 帧抵达前端
                        "visualRationale": summaryTextOnlyRationale().to_dict(),
                        "suggestedAgent": suggestion.model_dump(mode="json", by_alias=True)
                        if suggestion is not None else None,
                    },
                )
```

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 的命令。
Expected: `1 passed`。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat_stream_multistep.py backend/app/tests/integration/test_chat_multi_step.py
git commit -m "feat: 流式多步 done 帧下发 reportSections"
```

---

### Task 5: 历史回放现算 reportSections

**Files:**
- Modify: `backend/app/domain/schemas.py:2196-2199`（`ChatMessageRead`）
- Modify: `backend/app/services/session_history_service.py:230-272`
- Test: `backend/app/tests/integration/test_chat_history_api.py`

**Interfaces:**
- Consumes: `buildReportSections(content, steps_snapshot)`（Task 1，dict 输入）。
- Produces: `ChatMessageRead.report_sections`（JSON `reportSections`）；无快照/非 assistant 行 → None。

- [ ] **Step 1: 写失败测试**

在 `test_chat_history_api.py` 的 `_seedMessage` 增加参数（`:32-44`）：

```python
    stepsSnapshot: list[dict] | None = None,
```

并在 `SessionMessage(...)` 构造里加：

```python
        steps_snapshot=stepsSnapshot,
```

在 `TestChatHistoryMessages` 类中新增两个用例：

```python
    async def test_messages_computes_report_sections_from_snapshot(
        self, client, dbSession
    ) -> None:
        """回放不落 reportSections：由 content + steps_snapshot 现算（同一纯函数）。"""
        content = (
            "## 分析报告\n"
            "### 步骤 1：2024 销售额\n"
            "1000 万。\n"
            "\n"
            "### 步骤 2：2025 销售额\n"
            "1200 万。\n"
            "\n"
            "### 结论\n"
            "增长 20%。"
        )
        snapshot = [
            {"stepIndex": 0, "description": "2024 销售额", "sql": "SELECT 1",
             "chartType": "bar", "chartOption": {"series": []}},
            {"stepIndex": 1, "description": "2025 销售额", "sql": "SELECT 2",
             "chartType": "hbar", "chartOption": {"series": []}},
        ]
        await _seedMessage(
            dbSession, sessionId="s1", role="assistant", content=content,
            stepsSnapshot=snapshot,
        )

        resp = await client.get("/api/v1/sessions/s1/messages")
        assert resp.status_code == 200, resp.text
        sections = resp.json()["messages"][0]["reportSections"]

        assert [b["kind"] for b in sections] == ["text", "chart", "text", "chart", "text"]
        assert [b["stepIndex"] for b in sections if b["kind"] == "chart"] == [0, 1]

    async def test_messages_without_snapshot_have_no_report_sections(
        self, client, dbSession
    ) -> None:
        """存量行没有快照 → None，前端走旧渲染（零回归）。"""
        await _seedMessage(
            dbSession, sessionId="s1", role="assistant",
            content="### 步骤 1：A\n纯文本老消息",
        )

        resp = await client.get("/api/v1/sessions/s1/messages")
        assert resp.json()["messages"][0]["reportSections"] is None

    async def test_single_step_snapshot_has_no_report_sections(
        self, client, dbSession
    ) -> None:
        """单步消息也落 1 元素快照（回放计划卡用）—— 但单步不是「分节报告」。

        不挡住会在回放里画出**两张**一模一样的图（顶层 chartType 一张 + 报告块一张）。
        """
        await _seedMessage(
            dbSession, sessionId="s1", role="assistant",
            content="各供应商的收货数量汇总：共 3 家。",
            stepsSnapshot=[
                {"stepIndex": 0, "description": "各供应商的收货数量", "sql": "SELECT 1",
                 "chartType": "hbar", "chartOption": {"series": []}},
            ],
        )

        resp = await client.get("/api/v1/sessions/s1/messages")
        assert resp.json()["messages"][0]["reportSections"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/integration/test_chat_history_api.py::TestChatHistoryMessages::test_messages_computes_report_sections_from_snapshot -q
```
Expected: `KeyError: 'reportSections'`（或 `_seedMessage` 不接受 `stepsSnapshot`）。

- [ ] **Step 3: 加 schema 字段**

在 `ChatMessageRead.steps_snapshot`（`schemas.py:2196-2199`）之后插入：

```python
    # 2026-10-31（分节报告）：回放时由 content + steps_snapshot 现算，不落库。
    # 存量行、非多步行、无可图步骤 → None（前端走旧渲染）。
    report_sections: list[dict] | None = Field(
        default=None,
        description="多步最终回答的分节块（历史回放现算）",
    )
```

- [ ] **Step 4: 回放时现算**

在 `session_history_service.py` 顶部 import 区加入：

```python
from app.services.report_sections import buildReportSections
```

在 `ChatMessageRead(...)` 构造里、`steps_snapshot=r.steps_snapshot,` 之后追加：

```python
                # 2026-10-31：与实时路径调同一个纯函数，回放与屏幕同源。
                # 只对**多步**消息算：单步消息也落 1 元素快照（回放计划卡用），
                # 若也算，回放会同时画顶层图与报告块图 —— 同一张图出现两次。
                report_sections=(
                    buildReportSections(r.content or "", r.steps_snapshot)
                    if r.role == "assistant"
                    and isinstance(r.steps_snapshot, list)
                    and len(r.steps_snapshot) > 1
                    else None
                ) or None,
```

- [ ] **Step 5: 跑测试确认通过**

Run: 同 Step 2 的命令，改为跑本类全部用例（含新增 3 条）。
Expected: `3 passed`（`test_messages_computes_report_sections_from_snapshot`、
`test_messages_without_snapshot_have_no_report_sections`、
`test_single_step_snapshot_has_no_report_sections`）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/domain/schemas.py backend/app/services/session_history_service.py \
        backend/app/tests/integration/test_chat_history_api.py
git commit -m "feat: 历史回放现算 reportSections"
```

---

### Task 6: 后端类目轴可读性（标签密度 + dataZoom）

**Files:**
- Modify: `backend/app/services/chart_renderer.py:1-40`（常量）、`:191-234`（`_buildAxisOption`）
- Test: `backend/app/tests/unit/test_chart_renderer.py`

**Interfaces:**
- Consumes: `renderChartOption(spec, rows)`（既有）。
- Produces（option 结构契约）：
  - hbar 且类别 > 12：`yAxis.axisLabel = {interval: 0, overflow: "truncate", width: 160}`；
  - 纵向 bar 且类别 > 12：`xAxis.axisLabel = {rotate: 45, interval: 0, hideOverlap: true}`（line 不旋转）；
  - 类别 > 40：类目轴方向 `dataZoom: [inside, slider]`，`grid.bottom` +34，有图例时 `legend.bottom = 24`。

- [ ] **Step 1: 写失败测试**

在 `backend/app/tests/unit/test_chart_renderer.py` 新增（放在 `TestLegendPlacement` 之前或文件末尾均可）：

```python
def _many_rows(count: int) -> list[dict]:
    return [
        {"SUPPLIER_NAME": f"S{i:02d}", "RCV_QTY_PUU": count - i}
        for i in range(count)
    ]


class TestCategoryReadability:
    """2026-10-31：类别一多，图和标签被等比压扁；用高度（前端）+ 轴配置（后端）解决。"""

    def test_hbar_many_categories_shows_every_label_truncated(self) -> None:
        option = renderChartOption(_spec(ChartType.HBAR), _many_rows(20))

        assert option["yAxis"]["axisLabel"] == {
            "interval": 0,
            "overflow": "truncate",
            "width": 160,
        }

    def test_hbar_few_categories_keep_bare_axis(self) -> None:
        option = renderChartOption(_spec(ChartType.HBAR), _ROWS)

        assert "axisLabel" not in option["yAxis"]

    def test_vertical_bar_many_categories_rotates_labels(self) -> None:
        option = renderChartOption(_spec(ChartType.BAR), _many_rows(20))

        assert option["xAxis"]["axisLabel"] == {
            "rotate": 45,
            "interval": 0,
            "hideOverlap": True,
        }

    def test_line_many_categories_keeps_auto_interval(self) -> None:
        """折线不旋转：斜着的日期标签比稀疏标签更难读。"""
        spec = _spec(
            ChartType.LINE,
            columns=("MONTH", "LINE_AMT"),
            dimensions=(_dim("MONTH", "月份"),),
            measures=(_measure("LINE_AMT", "金额"),),
            series=(SpecSeries(name="金额", kind="line", dimension="MONTH", measure="LINE_AMT"),),
        )
        rows = [{"MONTH": f"M{i:02d}", "LINE_AMT": i} for i in range(50)]

        option = renderChartOption(spec, rows)

        assert "axisLabel" not in option["xAxis"]
        assert "dataZoom" not in option

    def test_hbar_over_threshold_adds_datazoom_and_grid_room(self) -> None:
        option = renderChartOption(_spec(ChartType.HBAR), _many_rows(50))

        assert option["dataZoom"] == [
            {"type": "inside", "yAxisIndex": 0},
            {"type": "slider", "yAxisIndex": 0, "height": 20, "bottom": 0},
        ]
        assert option["grid"]["bottom"] == 66  # 32 + 34
        assert "legend" not in option

    def test_vertical_bar_over_threshold_zooms_x_axis(self) -> None:
        option = renderChartOption(_spec(ChartType.BAR), _many_rows(50))

        assert option["dataZoom"] == [
            {"type": "inside", "xAxisIndex": 0},
            {"type": "slider", "xAxisIndex": 0, "height": 20, "bottom": 0},
        ]

    def test_datazoom_and_legend_share_the_bottom_band(self) -> None:
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"),
            measures=(_measure("RCV_QTY_PUU"), _measure("LINE_AMT", "金额")),
            series=(
                SpecSeries(name="供货量", kind="bar", dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU"),
                SpecSeries(name="金额", kind="bar", dimension="SUPPLIER_NAME", measure="LINE_AMT"),
            ),
        )

        option = renderChartOption(spec, _many_rows(50))

        assert option["grid"]["bottom"] == 90  # 56 + 34
        assert option["legend"]["bottom"] == 24  # 滑块 20 + 4
        assert option["legend"]["type"] == "scroll"

    def test_threshold_is_strictly_greater_than_40(self) -> None:
        option = renderChartOption(_spec(ChartType.HBAR), _many_rows(40))

        assert "dataZoom" not in option
        assert option["grid"]["bottom"] == 32
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd backend
pytest app/tests/unit/test_chart_renderer.py::TestCategoryReadability -q
```
Expected: 多个 FAIL（`KeyError: 'dataZoom'`、`axisLabel` 不存在）。

- [ ] **Step 3: 加常量并改 `_buildAxisOption`**

在 `chart_renderer.py` 模块头常量区（`_GRID_BOTTOM_DEFAULT` 附近）加入：

```python
# 2026-10-31：类目轴可读性（结构边界，不做数据治理）。
# 标签 > 12 个时强制逐条显示：横条图截断超长名称，纵向柱状图旋转 45°。
_CATEGORY_LABEL_DENSE_THRESHOLD = 12
_AXIS_LABEL_MAX_WIDTH = 160
# 类别 > 40 时绘图区已塞不下，打开缩放滑块（inside 滚轮 + slider 拖拽）。
_DATAZOOM_THRESHOLD = 40
_DATAZOOM_SLIDER_HEIGHT = 20
_DATAZOOM_BAND_HEIGHT = 34
_DATAZOOM_LEGEND_BOTTOM = 24
```

把 `_buildAxisOption`（`:191-234`）改为：

```python
def _denseLabel(kind: ChartType, horizontal: bool) -> dict[str, Any] | None:
    """类别多到必须逐条显示时，标签怎么摆；line/combo 保持 ECharts 自动稀疏。

    斜着的日期标签比稀疏标签更难读，所以只有纵向柱状图旋转。
    """
    if horizontal:
        return {"interval": 0, "overflow": "truncate", "width": _AXIS_LABEL_MAX_WIDTH}
    if kind is ChartType.BAR:
        return {"rotate": 45, "interval": 0, "hideOverlap": True}
    return None


def _buildCategoryAxis(
    spec: ChartSpec, categories: list[str], catField: str | None, kind: ChartType
) -> dict[str, Any]:
    """类目轴：Top N 翻转 + 标签密度 + （可选的）轴名。两种朝向共用这一份。"""
    horizontal = kind is ChartType.HBAR
    axis: dict[str, Any] = {"type": "category", "data": categories}
    if horizontal:
        # Top N 的语义是「最大的在最上面」：类目轴默认自下而上，故翻转。
        axis["inverse"] = True
    if len(categories) > _CATEGORY_LABEL_DENSE_THRESHOLD:
        label = _denseLabel(kind, horizontal)
        if label is not None:
            axis["axisLabel"] = label
    # 轴名只此一处。无注入 ⇒ 原对象原样返回。
    return _namedAxis(axis, _axisNameOf(spec, catField))


def _dataZoomOf(horizontal: bool) -> list[dict[str, Any]]:
    """缩放滑块：inside 负责滚轮/拖拽，slider 负责可见的把手。"""
    axisIndex = "yAxisIndex" if horizontal else "xAxisIndex"
    return [
        {"type": "inside", axisIndex: 0},
        {"type": "slider", axisIndex: 0, "height": _DATAZOOM_SLIDER_HEIGHT, "bottom": 0},
    ]


def _buildAxisOption(spec: ChartSpec, data: list[dict], kind: ChartType) -> dict[str, Any]:
    """折线/柱状/横柱/组合共用一套笛卡尔装配。

    - 第一个维度作类目轴；若还有第二个维度，则摊成多系列（分组柱状）。
    - 多个指标 = 多条系列。
    """
    catField = _dimensionOf(spec, 0)
    groupField = _dimensionOf(spec, 1)
    categories = _categories(data, catField) if catField else []
    series = _cartesianSeries(
        spec, data, categories, catField, groupField, withAxisIndex=kind is ChartType.COMBO
    )

    horizontal = kind is ChartType.HBAR
    categoryAxis = _buildCategoryAxis(spec, categories, catField, kind)
    valueAxis: dict[str, Any] = {"type": "value"}
    legend = _legend(spec, len(series))

    zoomed = len(categories) > _DATAZOOM_THRESHOLD
    gridBottom = (_GRID_BOTTOM_WITH_LEGEND if legend else _GRID_BOTTOM_DEFAULT) + (
        _DATAZOOM_BAND_HEIGHT if zoomed else 0
    )
    if zoomed and legend:
        # 图例让位给滑块：滑块贴底 0，图例抬到它上面 4px。
        legend = {**legend, "bottom": _DATAZOOM_LEGEND_BOTTOM}
    option: dict[str, Any] = {
        **_title(spec),
        "tooltip": {"trigger": "axis"},
        **legend,
        # 有图例时底部多留一截给滚动条 + 刻度标签（32/56 常数见模块头）。
        "grid": {
            "left": 48,
            "right": 48,
            "bottom": gridBottom,
            "top": 40,
            "containLabel": True,
        },
        "series": series,
    }
    if zoomed:
        option["dataZoom"] = _dataZoomOf(horizontal)
    if horizontal:
        option["yAxis"] = categoryAxis
        option["xAxis"] = valueAxis
        return option
    option["xAxis"] = categoryAxis
    option["yAxis"] = _valueAxes(spec, series) if kind is ChartType.COMBO else valueAxis
    return option
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd backend
pytest app/tests/unit/test_chart_renderer.py -q
```
Expected: 全绿。重点反向守卫：`test_many_grouped_series_legend_stays_one_scrollable_row`
用 **31 类 + 图例**逐字钉住 `legend == {"show":True,"type":"scroll","bottom":0}` 与
`grid.bottom == 56` —— 31 已过标签阈值（会加 `axisLabel`）但未过 dataZoom 阈值，
正好锁住「标签密度不影响图例/网格」「阈值是严格 >40」两件事。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chart_renderer.py backend/app/tests/unit/test_chart_renderer.py
git commit -m "feat: 多类别图表的轴标签密度与 dataZoom"
```

---

### Task 7: 前端动态高度（在线 + PNG 同高）

**Files:**
- Create: `frontend/src/utils/chartLayout.ts`
- Modify: `frontend/src/components/chat/ChartRenderer.tsx:104-112`（hook）、`:205`（style）
- Modify: `frontend/src/utils/chartSnapshot.ts:22-40, 56-67`
- Test: `frontend/src/tests/chartLayout.test.ts`、`frontend/src/tests/ChartRenderer.test.tsx`、`frontend/src/tests/chartSnapshot.test.ts`

**Interfaces:**
- Consumes: ECharts option（服务端结构）。
- Produces: `chartHeightOf(option: Record<string, unknown> | null | undefined): number`。
  高度只看 option：`yAxis` 是 category 且 series[0] 不是 heatmap → 横向条形；
  series[0].type === "heatmap" → 热力图；其余 320。

- [ ] **Step 1: 写失败测试**

Create `frontend/src/tests/chartLayout.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { chartHeightOf } from "../utils/chartLayout";

function hbarOption(count: number, withLegend = false) {
  return {
    yAxis: {
      type: "category",
      data: Array.from({ length: count }, (_, i) => `S${i}`),
    },
    xAxis: { type: "value" },
    series: [{ type: "bar", data: count }],
    ...(withLegend ? { legend: { show: true, type: "scroll" } } : {}),
  };
}

function heatmapOption(rowCount: number) {
  return {
    xAxis: { type: "category", data: ["A", "B"] },
    yAxis: {
      type: "category",
      data: Array.from({ length: rowCount }, (_, i) => `R${i}`),
    },
    series: [{ type: "heatmap" }],
  };
}

describe("chartHeightOf", () => {
  it("类别很少时保持 320 基线", () => {
    expect(chartHeightOf(hbarOption(3))).toBe(320);
    expect(chartHeightOf(hbarOption(8))).toBe(320);
  });

  it("横向条形图按类别数增高（96 + 28/类）", () => {
    expect(chartHeightOf(hbarOption(20))).toBe(656);
    expect(chartHeightOf(hbarOption(30))).toBe(936);
  });

  it("有图例时额外留 32px", () => {
    expect(chartHeightOf(hbarOption(20, true))).toBe(688);
  });

  it("超过 960 封顶（靠 dataZoom 看剩余类别）", () => {
    expect(chartHeightOf(hbarOption(60))).toBe(960);
  });

  it("热力图按行数增高（120 + 26/行）", () => {
    expect(chartHeightOf(heatmapOption(10))).toBe(380);
    expect(chartHeightOf(heatmapOption(40))).toBe(960);
  });

  it("纵向柱状图/折线/饼图保持 320", () => {
    expect(
      chartHeightOf({
        xAxis: { type: "category", data: ["A", "B", "C"] },
        yAxis: { type: "value" },
        series: [{ type: "bar" }],
      })
    ).toBe(320);
  });

  it("没有 option / 没有类目轴时回落 320（反向守卫）", () => {
    expect(chartHeightOf(null)).toBe(320);
    expect(chartHeightOf(undefined)).toBe(320);
    expect(chartHeightOf({})).toBe(320);
    expect(chartHeightOf({ series: [] })).toBe(320);
    expect(chartHeightOf(hbarOption(0))).toBe(320);
  });
});
```

Modify `frontend/src/tests/ChartRenderer.test.tsx` 的 echarts mock（`:42-55`）以暴露高度：

```tsx
vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: React.forwardRef(function EChartsMock(
    props: { option?: Record<string, unknown>; style?: { height?: number } },
    ref: React.ForwardedRef<unknown>
  ) {
    React.useImperativeHandle(ref, () => ({
      getEchartsInstance: () => echartsInstance,
    }));
    return (
      <div
        data-testid="echarts-mock"
        data-height={String(props.style?.height ?? "")}
      >
        {JSON.stringify(props.option)}
      </div>
    );
  }),
}));
```

并在 "BAR 类型渲染 echarts 组件并透传 option" 附近新增：

```tsx
  it("横向条形图按类别数增高（30 类 → 936px）", () => {
    render(
      <ChartRenderer
        chartType="hbar"
        chartOption={{
          yAxis: {
            type: "category",
            data: Array.from({ length: 30 }, (_, i) => `S${i}`),
          },
          xAxis: { type: "value" },
          series: [{ type: "bar", data: 30 }],
        }}
      />
    );

    expect(screen.getByTestId("echarts-mock").getAttribute("data-height")).toBe("936");
  });
```

Modify `frontend/src/tests/chartSnapshot.test.ts` 既有断言（`"420px"` → `"320px"`）：

```ts
  it("离屏容器带显式宽高（ECharts 在零尺寸容器里会拿到 0×0 画布）", async () => {
    await renderChartPng({ series: [] });

    const host = echartsMock.init.mock.calls[0][0] as unknown as HTMLElement;
    expect(host.style.width).toBe("800px");
    expect(host.style.height).toBe("320px");
  });
```

并新增：

```ts
  it("离屏高度与在线一致：30 类横向条形图 936px", async () => {
    await renderChartPng({
      yAxis: {
        type: "category",
        data: Array.from({ length: 30 }, (_, i) => `S${i}`),
      },
      xAxis: { type: "value" },
      series: [{ type: "bar" }],
    });

    const host = echartsMock.init.mock.calls[0][0] as unknown as HTMLElement;
    expect(host.style.height).toBe("936px");
    expect((echartsMock.init.mock.calls[0][2] as { height: number }).height).toBe(936);
  });
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd frontend
npx vitest run src/tests/chartLayout.test.ts src/tests/ChartRenderer.test.tsx src/tests/chartSnapshot.test.ts
```
Expected: `chartLayout.test.ts` 找不到模块；ChartRenderer 新用例 `data-height` 为空；chartSnapshot 高度仍 `420px`。

- [ ] **Step 3: 实现 `chartLayout.ts`**

Create `frontend/src/utils/chartLayout.ts`:

```ts
/**
 * 图表高度（2026-10-31）：类别一多，整张图连同坐标轴被等比压扁。
 *
 * 高度只从 **option 结构**推断，不依赖后端新字段 —— 在线渲染、PNG 导出、
 * 历史回放拿到的都是同一份服务端 option，因此三处天然一致。
 * - 类目轴挂在 yAxis = 横向条形图（决策引擎 >15 行时的形态）
 * - heatmap 按行数增长，单行更矮
 * - 其余 320 基线（纵向柱/折线/饼的类别数由宽度吸收）
 */
const CHART_BASE_HEIGHT = 320;
const CHART_MAX_HEIGHT = 960;
const HBAR_PADDING = 96;
const HBAR_ROW_HEIGHT = 28;
const CHART_LEGEND_HEIGHT = 32;
const HEATMAP_PADDING = 120;
const HEATMAP_ROW_HEIGHT = 26;

type OptionRecord = Record<string, unknown>;

function asRecord(value: unknown): OptionRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as OptionRecord)
    : null;
}

function categoryCountOf(axis: unknown): number {
  const record = asRecord(axis);
  if (!record || record.type !== "category" || !Array.isArray(record.data)) {
    return 0;
  }
  return record.data.length;
}

function firstSeriesType(option: OptionRecord): string | null {
  const first = Array.isArray(option.series) ? option.series[0] : option.series;
  const record = asRecord(first);
  return typeof record?.type === "string" ? record.type : null;
}

function hasLegend(option: OptionRecord): boolean {
  const legend = option.legend;
  if (Array.isArray(legend)) return legend.length > 0;
  const record = asRecord(legend);
  return record !== null && record.show !== false;
}

function clamp(height: number): number {
  return Math.min(CHART_MAX_HEIGHT, Math.max(CHART_BASE_HEIGHT, Math.round(height)));
}

export function chartHeightOf(
  option: OptionRecord | null | undefined
): number {
  const record = asRecord(option);
  if (!record) return CHART_BASE_HEIGHT;

  const rowCount = categoryCountOf(record.yAxis);
  if (rowCount === 0) return CHART_BASE_HEIGHT;

  if (firstSeriesType(record) === "heatmap") {
    return clamp(HEATMAP_PADDING + rowCount * HEATMAP_ROW_HEIGHT);
  }
  const legendExtra = hasLegend(record) ? CHART_LEGEND_HEIGHT : 0;
  return clamp(HBAR_PADDING + rowCount * HBAR_ROW_HEIGHT + legendExtra);
}
```

- [ ] **Step 4: ChartRenderer 使用动态高度**

在 `ChartRenderer.tsx` 顶部 import 区加：

```ts
import { chartHeightOf } from "../../utils/chartLayout";
```

在 `kpiPayload` 的 `useMemo` 之后（**必须在 `if (isKpi && !kpiPayload)` 早退之前**）加：

```ts
  // 高度随类别数增长：在线与 PNG 导出共用同一函数（见 utils/chartLayout.ts）。
  const chartHeight = useMemo(() => chartHeightOf(chartOption), [chartOption]);
```

把既有那一行（`:205`，外层 `chart-container` div 已在，不要重复包一层）：

```tsx
            <ReactECharts ref={chartRef} option={themedOption} style={{ height: 320 }} notMerge />
```

改为：

```tsx
            <ReactECharts
              ref={chartRef}
              option={themedOption}
              style={{ height: chartHeight }}
              notMerge
            />
```

- [ ] **Step 5: chartSnapshot 使用同一高度**

在 `chartSnapshot.ts` 顶部 import：

```ts
import { chartHeightOf } from "./chartLayout";
```

将 `SNAPSHOT_HEIGHT` 常量与 `createOffscreenHost` 改为参数化：

```ts
/** 截图画布宽度（2 倍像素密度由 getDataURL 的 pixelRatio 负责，这里给 CSS 尺寸）。 */
const SNAPSHOT_WIDTH = 800;

/** 离屏容器：必须在文档流里（ECharts 依赖 getBoundingClientRect），但不可见。 */
function createOffscreenHost(height: number): HTMLDivElement {
  const host = document.createElement("div");
  host.style.position = "fixed";
  host.style.left = "-99999px";
  host.style.top = "0";
  host.style.width = `${SNAPSHOT_WIDTH}px`;
  host.style.height = `${height}px`;
  host.style.pointerEvents = "none";
  host.setAttribute("aria-hidden", "true");
  document.body.appendChild(host);
  return host;
}
```

`renderChartPng` 内改为：

```ts
export async function renderChartPng(
  chartOption: Record<string, unknown>
): Promise<string | null> {
  if (typeof document === "undefined") return null;
  const height = chartHeightOf(chartOption);
  const host = createOffscreenHost(height);
  let chart: echarts.ECharts | null = null;
  try {
    chart = echarts.init(host, undefined, {
      width: SNAPSHOT_WIDTH,
      height,
      renderer: "canvas",
    });
```

其余逻辑不变。

- [ ] **Step 6: 跑测试确认通过**

Run:
```bash
cd frontend
npx vitest run src/tests/chartLayout.test.ts src/tests/ChartRenderer.test.tsx src/tests/chartSnapshot.test.ts
npx tsc --noEmit
```
Expected: 三套件全绿；`tsc` 0 错误。

- [ ] **Step 7: Commit**

```bash
git add frontend/src/utils/chartLayout.ts frontend/src/components/chat/ChartRenderer.tsx \
        frontend/src/utils/chartSnapshot.ts frontend/src/tests/chartLayout.test.ts \
        frontend/src/tests/ChartRenderer.test.tsx frontend/src/tests/chartSnapshot.test.ts
git commit -m "feat: 图表高度随类别数增长且在线与导出一致"
```

---

### Task 8: 前端契约流转（类型 / 校验 / API / store）

**Files:**
- Modify: `frontend/src/types/chat.ts:122-139, 176-223, 266-340`
- Modify: `frontend/src/types/chatHistory.ts:14-40`
- Create: `frontend/src/utils/reportSections.ts`
- Modify: `frontend/src/api/chat.ts:208-227, 371-389`
- Modify: `frontend/src/stores/chatStore.ts:194-216, 468-507, 815-850, 883-920`
- Test: `frontend/src/tests/reportSections.test.ts`、`frontend/src/tests/chatStore.test.ts`

**Interfaces:**
- Consumes: 后端 `reportSections` JSON（系统边界原始值）。
- Produces: `ReportBlock` 类型、`asReportSections(unknown) -> ReportBlock[] | null`；
  `ChatMessage.reportSections` 由 done 帧 / 非流式响应 / 历史回放三处写入。

- [ ] **Step 1: 写失败测试**

Create `frontend/src/tests/reportSections.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { asReportSections } from "../utils/reportSections";

describe("asReportSections", () => {
  it("保留合法 text / chart 块", () => {
    const blocks = asReportSections([
      { kind: "text", content: "## 分析报告" },
      { kind: "chart", stepIndex: 0, title: "2024 销售额" },
    ]);

    expect(blocks).toEqual([
      { kind: "text", content: "## 分析报告" },
      { kind: "chart", stepIndex: 0, title: "2024 销售额" },
    ]);
  });

  it("丢弃形状不合法的块，保留其余（不冒未知形状进渲染层）", () => {
    const blocks = asReportSections([
      { kind: "text", content: "正文" },
      { kind: "text", content: 1 },
      { kind: "chart", stepIndex: "0" },
      { kind: "unknown", content: "x" },
      null,
    ]);

    expect(blocks).toEqual([{ kind: "text", content: "正文" }]);
  });

  it("非数组 / 空数组 / 全部非法 → null（走旧渲染）", () => {
    expect(asReportSections(undefined)).toBeNull();
    expect(asReportSections(null)).toBeNull();
    expect(asReportSections("text")).toBeNull();
    expect(asReportSections([])).toBeNull();
    expect(asReportSections([{ kind: "chart" }])).toBeNull();
  });
});
```

Append to `frontend/src/tests/chatStore.test.ts`（放在流式输出 describe 内）：

```ts
  it("流式 done 帧写入 reportSections", async () => {
    chatApi.sendMessageStream.mockImplementation(async (_payload, handlers) => {
      handlers.onToken?.("## 分析报告");
      handlers.onDone?.({
        tokensUsed: 10,
        cost: 0.00001,
        reportSections: [
          { kind: "text", content: "## 分析报告" },
          { kind: "chart", stepIndex: 0, title: "2024 销售额" },
        ],
      });
    });
    useChatStore.getState().setDatasourceId(1);

    await useChatStore.getState().sendMessage("分步查询", true);

    expect(useChatStore.getState().messages[1].reportSections).toEqual([
      { kind: "text", content: "## 分析报告" },
      { kind: "chart", stepIndex: 0, title: "2024 销售额" },
    ]);
  });

  it("非流式响应写入 reportSections", async () => {
    chatApi.sendMessage.mockResolvedValue({
      answer: "## 分析报告",
      intent: "multi_step",
      tokensUsed: 10,
      cost: 0.00001,
      reportSections: [{ kind: "text", content: "## 分析报告" }],
    });
    useChatStore.getState().setDatasourceId(1);

    await useChatStore.getState().sendMessage("分步查询");

    expect(useChatStore.getState().messages[1].reportSections).toEqual([
      { kind: "text", content: "## 分析报告" },
    ]);
  });

  it("历史回放还原 reportSections", async () => {
    historyApi.loadSessionMessages.mockResolvedValue({
      sessionId: "s-history",
      messages: [
        {
          id: 1, role: "assistant", content: "## 分析报告", question: null,
          sql: null, createdTime: "2026-01-01T00:00:00Z", interrupted: false,
          reportSections: [{ kind: "text", content: "## 分析报告" }],
        },
      ],
    });

    await useChatStore.getState().loadSessionMessages("s-history");

    expect(useChatStore.getState().messages[0].reportSections).toEqual([
      { kind: "text", content: "## 分析报告" },
    ]);
  });
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd frontend
npx vitest run src/tests/reportSections.test.ts src/tests/chatStore.test.ts
```
Expected: 模块缺失；store 用例得到 `undefined`。

- [ ] **Step 3: 加类型**

`frontend/src/types/chat.ts` 在 `ChatResponse` 之前加入：

```ts
// 多步最终报告的分块（2026-10-31）：text 块是 Markdown 正文，chart 块只带
// stepIndex，图负载复用 steps[stepIndex]（不重复下发）。
export type ReportBlock =
  | { kind: "text"; content: string }
  | { kind: "chart"; stepIndex: number; title?: string | null };
```

`ChatResponse` 内、`steps?` 之后加：

```ts
  // 多步报告分块（仅多步聚合/降级收尾；其余意图为 null/缺省）
  reportSections?: ReportBlock[] | null;
```

`ChatMessage` 内、`steps?: MultiStepStep[];` 之后加：

```ts
  // 多步报告分块：done 帧 / 非流式响应 / 历史回放回填；缺省走旧纯 Markdown 渲染
  reportSections?: ReportBlock[];
```

`frontend/src/types/chatHistory.ts` 的 `ChatMessageRead` 末尾加（系统边界原始值，由 chatStore 收窄）：

```ts
  // 2026-10-31：后端回放时现算的分节块；同是系统边界原始 JSON
  reportSections?: unknown;
```

- [ ] **Step 4: 加边界校验**

Create `frontend/src/utils/reportSections.ts`:

```ts
import type { ReportBlock } from "../types/chat";

/**
 * 系统边界收窄：后端/历史回放的 reportSections 是原始 JSON，形状可能来自
 * 更早版本。逐项校验并丢弃非法块；没有合法块时返回 null（= 走旧渲染）。
 */
function isReportBlock(value: unknown): value is ReportBlock {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  if (record.kind === "text") {
    return typeof record.content === "string";
  }
  if (record.kind === "chart") {
    return typeof record.stepIndex === "number" && Number.isInteger(record.stepIndex);
  }
  return false;
}

export function asReportSections(value: unknown): ReportBlock[] | null {
  if (!Array.isArray(value)) return null;
  const blocks = value.filter(isReportBlock);
  return blocks.length > 0 ? blocks : null;
}
```

- [ ] **Step 5: API done 帧透传**

`frontend/src/api/chat.ts` 顶部 import 区加：

```ts
import { asReportSections } from "../utils/reportSections";
```

`StreamSummary` 的 `visualRationale` 之后加：

```ts
  // 2026-10-31：多步报告分块（done 帧）；
  // 注意：本批**不**顺带补 done 帧的 queryPlan 缺口（既有行为，改动会影响卡片出现时机）。
  reportSections?: import("../types/chat").ReportBlock[] | null;
```

`dispatchFrame` 的 `case "done"` payload 加：

```ts
        reportSections: asReportSections(d.reportSections),
```

- [ ] **Step 6: store 三处写入**

`chatStore.ts` 顶部 import 区加：

```ts
import { asReportSections } from "../utils/reportSections";
```

`toChatMessage` 的 `steps: asReplaySteps(read.stepsSnapshot),` 之后加：

```ts
    reportSections: asReportSections(read.reportSections) ?? undefined,
```

首个流 `onDone` 的解构参数加 `reportSections`，并在 `visualRationale` 的展开之后加：

```ts
            ...(reportSections ? { reportSections } : {}),
```

续跑 `onDone` 同样处理（解构加 `reportSections`，patch 里加同一行展开）。

非流式分支 patch 里、`queryPlan: response.queryPlan ?? null,` 之后加：

```ts
              reportSections: asReportSections(response.reportSections) ?? undefined,
```

- [ ] **Step 7: 跑测试确认通过**

Run:
```bash
cd frontend
npx vitest run src/tests/reportSections.test.ts src/tests/chatStore.test.ts
npx tsc --noEmit
```
Expected: 全绿；`tsc` 0 错误。

- [ ] **Step 8: Commit**

```bash
git add frontend/src/types/chat.ts frontend/src/types/chatHistory.ts \
        frontend/src/utils/reportSections.ts frontend/src/api/chat.ts \
        frontend/src/stores/chatStore.ts frontend/src/tests/reportSections.test.ts \
        frontend/src/tests/chatStore.test.ts
git commit -m "feat: 前端 reportSections 契约流转与边界收窄"
```

---

### Task 9: MessageItem 按块渲染分节报告

**Files:**
- Modify: `frontend/src/components/chat/MessageItem.tsx:18`（import）、`:66-104`（helper）、`:189-199`（正文渲染）
- Test: `frontend/src/tests/MessageItem.test.tsx`

**Interfaces:**
- Consumes: `message.reportSections`（Task 8）、`message.steps`（既有）。
- Produces: 非流式消息在有分块时按 text → chart → text 顺序渲染；chart 块从 `steps[stepIndex]` 取图；取不到则跳过该块；无分块走旧 Markdown。

- [ ] **Step 1: 写失败测试**

把 `MessageItem.test.tsx` 的 ChartRenderer mock（`:34-37`）改为暴露 chartType：

```tsx
vi.mock("../components/chat/ChartRenderer", () => ({
  __esModule: true,
  default: (props: { chartType?: string | null }) => (
    <div data-testid="chart-renderer" data-chart-type={props.chartType ?? ""} />
  ),
}));
```

在文件末尾新增：

```tsx
describe("MessageItem 分节报告（2026-10-31）", () => {
  const steps = [
    {
      stepIndex: 0,
      description: "2024 销售额",
      subQuestion: "2024 年销售额是多少",
      aggregationOnly: false,
      status: "done" as const,
      chartType: "hbar" as const,
      chartOption: { series: [] },
    },
  ];

  it("按块顺序渲染正文与每步图", () => {
    // 断言文案刻意与 step.description 不同：MultiStepPlanCard 也会渲染步骤描述，
    // 用同一串文本会让 getByText 命中多个元素（那是测试写法问题，不是被测行为）。
    renderItem({
      content: "## 分析报告\n### 步骤 1：年度对比\n### 结论\n增长 20%。",
      reportSections: [
        { kind: "text", content: "## 分析报告\n### 步骤 1：年度对比" },
        { kind: "chart", stepIndex: 0, title: "2024 销售额" },
        { kind: "text", content: "### 结论\n增长 20%。" },
      ],
      steps,
    });

    expect(screen.getByText("年度对比")).toBeInTheDocument();
    const charts = screen.getAllByTestId("chart-renderer");
    expect(charts).toHaveLength(1);
    expect(charts[0].getAttribute("data-chart-type")).toBe("hbar");
    // 图在结论之前：分节顺序不能被卡片区（计划卡/查询计划）打乱
    const conclusion = screen.getByText("增长 20%。");
    expect(
      charts[0].compareDocumentPosition(conclusion) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });

  it("chart 块取不到对应步骤时跳过（不崩、不画空图）", () => {
    renderItem({
      content: "### 步骤 1：A",
      reportSections: [
        { kind: "text", content: "### 步骤 1：A" },
        { kind: "chart", stepIndex: 9, title: "缺失" },
      ],
      steps,
    });

    expect(screen.queryByTestId("chart-renderer")).not.toBeInTheDocument();
  });

  it("reportSections 缺失时走旧 Markdown 渲染（反向守卫）", () => {
    renderItem({ content: "纯文本回答", steps });

    expect(screen.getByText("纯文本回答")).toBeInTheDocument();
    expect(screen.queryByTestId("chart-renderer")).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run:
```bash
cd frontend
npx vitest run src/tests/MessageItem.test.tsx
```
Expected: 新用例找不到第二段文本 / 无 chart-renderer（当前忽略 `reportSections`，且 `steps` 会渲染计划卡）。

- [ ] **Step 3: 实现**

`MessageItem.tsx` 的 type import 改为：

```ts
import type { ChatMessage, MultiStepStep } from "../../types/chat";
```

在 `MessageItem` 组件之前加 helper：

```tsx
/** 分节报告里的一个图块：负载从 steps[stepIndex] 取（不重复下发），取不到整块跳过。 */
function ReportChartBlock({ step }: { step?: MultiStepStep }) {
  if (!step?.chartType) return null;
  return (
    <div style={{ marginTop: 8 }}>
      <ChartRenderer
        chartType={step.chartType}
        chartOption={step.chartOption}
        tableOption={step.tableOption}
        visualRationale={step.visualRationale}
      />
    </div>
  );
}
```

把正文渲染的三元分支（`:189-199`）改为：

```tsx
            {message.isStreaming ? (
              // 流式期间行高与 markdown 渲染对齐（1.7），完成后切换渲染无视觉跳动（审查 LOW-2）
              <Paragraph style={{ margin: 0, whiteSpace: "pre-wrap", lineHeight: 1.7 }}>
                {message.content}
                <span className="typing-cursor" aria-label={t("messageItem.typing")} />
              </Paragraph>
            ) : message.reportSections?.length ? (
              // 2026-10-31：多步报告按「正文 → 该步图 → 下一节」渲染；图负载复用
              // message.steps，缺失时跳过该块（老消息/半截数据不崩）。
              <div className="markdown-body">
                {message.reportSections.map((block, index) =>
                  block.kind === "text" ? (
                    <ReactMarkdown key={`text-${index}`} remarkPlugins={[remarkGfm]}>
                      {block.content}
                    </ReactMarkdown>
                  ) : (
                    <ReportChartBlock
                      key={`chart-${index}`}
                      step={message.steps?.find((step) => step.stepIndex === block.stepIndex)}
                    />
                  )
                )}
              </div>
            ) : (
              <div className="markdown-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
              </div>
            )}
```

- [ ] **Step 4: 跑测试确认通过**

Run:
```bash
cd frontend
npx vitest run src/tests/MessageItem.test.tsx
npx tsc --noEmit
```
Expected: 全绿；`tsc` 0 错误。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/chat/MessageItem.tsx frontend/src/tests/MessageItem.test.tsx
git commit -m "feat: 多步报告按分块渲染每步图"
```

---

### Task 10: 文档、全量回归与净新增失败取证

**Files:**
- Modify: `Harness/wiki/chart-rendering.md`
- Modify: `Harness/changes/2026-10-31-chat-report-charts/summary.md`（状态、实际签名、测试证据）
- No test files added.

**Interfaces:**
- Consumes: Task 1-9 全部产出。
- Produces: 与代码一致的 Wiki 契约；SSOT 收尾记录。

- [ ] **Step 1: 更新 Wiki 契约**

在 `Harness/wiki/chart-rendering.md` 的图表渲染章节追加：

```markdown
## 多步报告分节（2026-10-31）

多步最终回答由 `StepAggregator` 按固定结构产出：`## 分析报告` → 每步
`### 步骤 N：<描述>` + 正文 → `### 结论`。后端 `buildReportSections`
（`services/report_sections.py`）把文本解析成 `text` / `chart(stepIndex)` 块：

- 非流式：`ChatResponse.reportSections`；
- 流式：`done` 帧 `reportSections`；
- 历史回放：`ChatMessageRead.reportSections`（由 `content + steps_snapshot` 现算）。

图负载不重复下发，前端从 `message.steps[stepIndex]` 取。解析失败（模型没按格式）
回退为「原文 + 文末图」，图绝不静默丢失。

## 图表高度与多类别可读性（2026-10-31）

- 高度由 option 结构推断：横向条形 `clamp(320, 96 + 类别数×28 + 图例32, 960)`；
  热力图 `clamp(320, 120 + 行数×26, 960)`；其余 320。在线渲染与 PNG 导出共用
  `utils/chartLayout.ts::chartHeightOf`。
- 后端轴结构：hbar >12 类 `axisLabel {interval:0, overflow:"truncate", width:160}`；
  纵向 bar >12 类旋转 45°；类别 >40 加 `dataZoom`（inside + slider），
  `grid.bottom` +34，有图例时 `legend.bottom=24`。line 保持自动稀疏。
```

同时把 `summary.md` §4.4 的 `chartHeightOf(chartType, option)` 改为
`chartHeightOf(option)`（实现签名以本计划 Task 7 为准），并把状态改为「已实施」。

- [ ] **Step 2: 跑后端相关套件**

Run:
```bash
cd backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/unit/test_report_sections.py \
       app/tests/unit/test_step_aggregator.py \
       app/tests/unit/test_chart_renderer.py \
       app/tests/integration/test_chat_multi_step.py \
       app/tests/integration/test_chat_history_api.py -q
```
Expected: 全绿。若有红，逐条与 Task 0 基线对照；**净新增失败必须为 0**，否则修复后重跑。

- [ ] **Step 3: 跑前端相关套件 + 类型检查**

Run:
```bash
cd frontend
npx vitest run src/tests/chartLayout.test.ts src/tests/chartSnapshot.test.ts \
  src/tests/ChartRenderer.test.tsx src/tests/MessageItem.test.tsx \
  src/tests/chatStore.test.ts src/tests/reportSections.test.ts
npx tsc --noEmit
```
Expected: 全绿；`tsc` 0 错误。

- [ ] **Step 4: 自查交付物清单**

对照 `summary.md` §9 的 15 个文件逐项确认已改/已新增；漏项当场补。特别确认：

```bash
cd /private/tmp/qa-report-charts
git diff --stat origin/main
```
Expected: 文件清单与 §9 一致；无意外文件（尤其无 `.env`、无 `raw/`）。

- [ ] **Step 5: 提交文档**

```bash
git add Harness/wiki/chart-rendering.md \
        Harness/changes/2026-10-31-chat-report-charts/summary.md
git commit -m "docs: 分节报告与图表高度契约"
```

- [ ] **Step 6: 交付**

报告给用户：分支、提交列表、两套测试结果、净新增失败对照、未做项（PDF 下一批）。
不自动 push / 不自动建 PR —— 等用户选择（推送前需 `HTTPS_PROXY=http://127.0.0.1:12450`）。

---

## Self-Review

**1. Spec coverage**

| 设计条目 | 对应 Task |
|---|---|
| 4.1 报告格式契约 | Task 1（解析）、Task 2（提示词） |
| 4.2 `report_sections.py` 规则 1-6 | Task 1 |
| 4.2 非流式 `ChatResponse.reportSections` | Task 3 |
| 4.2 流式 done 帧 | Task 4 |
| 4.2 历史回放现算 | Task 5 |
| 4.2 非流式补 `steps_snapshot` | Task 3 |
| 4.3 前端类型/store/MessageItem | Task 8、Task 9 |
| 4.4 高度公式 | Task 7 |
| 4.4 后端轴配置 + dataZoom | Task 6 |
| §5 非目标 | Global Constraints 明确排除 |
| §7 测试计划 | Task 1/2/3/4/5/6/7/8/9 各自的 RED 用例 |
| §9 文档 | Task 10 |

**2. Placeholder scan**：无 TBD/TODO；所有代码步骤均给出可粘贴的实现与测试。

**3. Type consistency**：
- 后端块字段 `kind/content/stepIndex/title` 在 Task 1、3、4、5 一致；
- 前端 `ReportBlock` 与 `asReportSections` 在 Task 8、9 一致；
- `chartHeightOf(option)` 在 Task 7 的 ChartRenderer 与 chartSnapshot 两处调用一致；
- Task 3 的 `stepReads` 与 Task 4 的 `stepReads` 同名同义（各自函数内局部变量）。
