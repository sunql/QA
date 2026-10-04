# 研究型 Agent 入口体验修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复研究型 Agent 入口上线后用户实测暴露的 7 项体验 / 功能缺口（模型选择、歧义可见、检查点内容可见、三按钮指向明确、三模式说明、历史删除、数据源选择）。

**Architecture:** 7 条里 5 条（2/3/4/6/7）是「能力已在、暴露缺失」——数据已随 SSE 下发或被后端接收，缺的是最后一跳的渲染/接线；1 条（5）只缺文案；只有 1 条（1 模型选择）是真正新增能力（需迁移 0113 + `extra="forbid"` 放行）。故分两批交付：批一（W1–W4）纯前端 + 一个响应字段 + 一个 DELETE 端点；批二（W5）动 DB，前后端必须同批上线。

**Tech Stack:** 后端 FastAPI + SQLAlchemy(async) + Alembic + Pydantic v2 + pytest（真实 PostgreSQL）；前端 React 18 + TypeScript + antd 5 + zustand + i18next + vitest/RTL。

**SSOT：**
- 设计稿：`docs/superpowers/specs/2026-10-05-research-entry-ux-fixes-design.md`
- Harness 变更记录：`Harness/changes/feat-research-entry-ux-fixes/summary.md`

## Global Constraints

- **不可变数据**：前端一律展开运算符 / `filter` / `map` 返回新对象，禁止原地 mutation（`state.events`、`sessions`、`selectedIds` 均如此）。
- **小文件**：200–400 行为宜、≤ 800 行；函数 < 50 行；嵌套 ≤ 4 层。
- **显式错误处理**：每个层级显式处理错误；UI 层给友好文案，服务端记详细上下文。禁止静默吞错（唯一的例外是 Task 6 的数据源清单钩子，其降级理由已在该处注释说明）。
- **命名**：前端函数/变量 `camelCase`、类型/组件 `PascalCase`、常量 `UPPER_SNAKE_CASE`；Python ORM/Pydantic 字段 `snake_case`，**接口 JSON 契约 camelCase**（刻意偏离，与前端一致）。
- **后端测试必须用真实 PostgreSQL**，禁止 sqlite 内存库、禁止直接调 service 绕过 API。测试库：`localhost:5434` / 库 `qa_metadata_test` / 用户 `qa_user` / 口令 `qa_pg_dev_2026`。
- **后端集成测试命令（两个环境变量都必须给，缺 `TEST_NEO4J_URI` 是装配期 fail-fast、不是断言失败）：**

  ```bash
  cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/<file> -q
  ```

- **后端单测命令（不需要 DB 环境变量）：** `cd backend && uv run pytest app/tests/unit/<file> -q`
- **前端测试命令：** `cd frontend && npx vitest run --testTimeout=30000 src/tests/<file>`
- **串行执行**：绝不并行跑两个测试套件；**绝不跑全量 `app/tests/integration`**（无界尾部）；`pytest-timeout` 未安装。
- **绝不手工执行 `alembic upgrade head`** —— 它默认指向**生产**元数据库。迁移由容器启动自动执行；测试库由 `app/tests/_pg_support._ensureSchema` 自动 upgrade。
- **迁移文件名 ≤ 32 字符**（`变更记录强制规范` §校验清单）。
- **禁止 `git add -A` / `git add .`**：工作区含无关的未跟踪文件与继承的未提交改动（`backend/app/services/chat_multistep.py`、`chat_service.py` 等），必须显式列出路径。
- **提交与推送只在用户明确要求时进行**；本计划每个 Task 末尾的 `git commit` 只提交该 Task 涉及的文件。
- **前端改动后验收**：`docker compose build --no-cache frontend`（裸 `docker build` 产出的 tag 少 `system-` 前缀，容器会永远拉旧镜像）。
- **覆盖率 ≥ 80%**（前端 vitest 配置已在 thresholds 里强制；后端改动模块须达标）。
- **绝不修改 `raw/`**；业务查询仅只读 SELECT；每次 LLM 调用必须记 Token 与成本。

---

# 批一：W1–W4

## Task 1: W1-a 后端检查点问句带对象（纯函数抽出）

**Files:**
- Modify: `backend/app/services/research_agent_stages.py`（新增两个纯函数 + 冲突种类标签表）
- Modify: `backend/app/services/research_agent_service.py:111`（import 补两个名字）、`:487`、`:600`（调用点）
- Test: `backend/app/tests/unit/test_research_checkpoint_prompt.py`（新建）

**Interfaces:**
- Consumes: 无（本 Task 是批一第一个 Task）
- Produces:
  - `conflictKindLabel(kind: str) -> str`
  - `ambiguityPrompt(conflicts: list[dict[str, Any]]) -> str`
  - `hypothesisPrompt(candidates: list[dict[str, Any]]) -> str`
  - 三者均从 `app.services.research_agent_stages` 导出

**背景**：`research_agent_service.py:487` 的 `"检测到语义歧义，如何处理？"` 与 `:600` 的 `"验证哪些假设？"` 是**无插值的死字面量** —— 用户只看到一句泛问句，不知道在决策什么。把它们抽成 `stages` 里的纯函数，既能带上对象（条数 / 种类），又能被纯单测覆盖而不必驱动整个状态机。

**约束**：只改 `prompt` 文案，**绝不改** checkpoint 的 `phase` / `options` 结构 —— 已落库的 `research_checkpoint.options` 兼容性依赖结构稳定。`:492`（`"三臂是否齐全？"`）与 `:531`（`"计划是否确认？"`）**保持不变**（已是具体问题）。

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/unit/test_research_checkpoint_prompt.py`：

```python
"""检查点问句构造器单测（feat-research-entry-ux-fixes W1-a）。

纯函数、零 DB、零状态机 —— 这正是把文案从 `research_agent_service.py` 抽到
`research_agent_stages.py` 的目的：文案可被钉住，且不必驱动整条研究链路。

契约：问句必须带**对象**（条数 / 冲突种类），否则用户只看到泛问句不知在决策什么；
未知冲突种类回落通用词、不抛错（ESL 未来新增 kind 时不得崩）。
"""

from app.services.research_agent_stages import (
    ambiguityPrompt,
    conflictKindLabel,
    hypothesisPrompt,
)


def test_conflict_kind_labels_are_human_readable() -> None:
    assert conflictKindLabel("metric_ambiguous") == "指标歧义"
    assert conflictKindLabel("wiki_disagree") == "知识冲突"


def test_unknown_conflict_kind_falls_back_without_raising() -> None:
    assert conflictKindLabel("brand_new_kind") == "待确认项"
    assert conflictKindLabel("") == "待确认项"


def test_ambiguity_prompt_carries_count_and_kinds() -> None:
    prompt = ambiguityPrompt(
        [{"kind": "metric_ambiguous"}, {"kind": "wiki_disagree"}]
    )
    assert prompt == "检测到 2 处语义歧义（指标歧义、知识冲突），请确认采用哪一项？"


def test_ambiguity_prompt_dedups_repeated_kinds() -> None:
    """同类多条目：种类只报一次，条数照实报。"""
    prompt = ambiguityPrompt(
        [{"kind": "metric_ambiguous"}, {"kind": "metric_ambiguous"}]
    )
    assert prompt == "检测到 2 处语义歧义（指标歧义），请确认采用哪一项？"


def test_ambiguity_prompt_falls_back_when_kind_unresolvable() -> None:
    assert ambiguityPrompt([{"kind": ""}]) == "检测到语义歧义，请确认采用哪一项？"
    assert ambiguityPrompt([]) == "检测到语义歧义，请确认采用哪一项？"


def test_hypothesis_prompt_carries_candidate_count() -> None:
    prompt = hypothesisPrompt([{"statement": "a"}, {"statement": "b"}, {"statement": "c"}])
    assert prompt == "共 3 条候选假设，请选择要验证的（可多选）："


def test_hypothesis_prompt_explains_empty_candidates() -> None:
    """降级路径（无 LLM / 解析失败）候选本就是 [] —— 必须给下一步指引，不能只报 0 条。"""
    prompt = hypothesisPrompt([])
    assert prompt == "本轮未生成候选假设（模型不可用或解析失败），可点「修改」补充研究方向。"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest app/tests/unit/test_research_checkpoint_prompt.py -q
```

Expected: FAIL —— `ImportError: cannot import name 'ambiguityPrompt' from 'app.services.research_agent_stages'`。

- [ ] **Step 3: 实现**

在 `backend/app/services/research_agent_stages.py` 末尾追加（放在 `drivers()` / `confidenceIndex()` 附近，与既有无状态构件同段）：

```python
# ---------------------------------------------------------------------------
# 检查点问句构造（feat-research-entry-ux-fixes W1-a）
# ---------------------------------------------------------------------------
#
# 动因：这两个问句原先是 `research_agent_service.py` 里**无插值的死字面量**
# （用户反馈第 2/3 条：「提示检测到语义歧义，但不知道是什么歧义」「提示验证哪些
# 假设，但没有任何提示、毫无头绪」）。抽到本模块后，文案带上对象（条数 / 种类），
# 且可被纯单测钉住 —— 不必驱动整条研究状态机去断言一句文案。
#
# 只改文案：**绝不改** checkpoint 的 phase / options 结构，已落库的
# `research_checkpoint.options` 兼容性依赖结构稳定。

# 冲突种类 → 中文标签。未知 kind 回落通用词、不抛错：ESL 侧 `_detectConflicts`
# 未来新增 kind 时，本表与本函数都不该因此崩。
_CONFLICT_KIND_LABELS: dict[str, str] = {
    "metric_ambiguous": "指标歧义",
    "wiki_disagree": "知识冲突",
}
_CONFLICT_KIND_FALLBACK = "待确认项"

# 冲突种类无法解析时的兜底问句（缺 kind 字段 / kind 为空串）。
_AMBIGUITY_PROMPT_FALLBACK = "检测到语义歧义，请确认采用哪一项？"

# 候选假设为空时的空态文案（降级路径：无 LLM / 解析失败）。
_EMPTY_CANDIDATES_PROMPT = (
    "本轮未生成候选假设（模型不可用或解析失败），可点「修改」补充研究方向。"
)


def conflictKindLabel(kind: str) -> str:
    """冲突种类的人类可读标签；未知种类回落通用词。"""
    return _CONFLICT_KIND_LABELS.get(kind, _CONFLICT_KIND_FALLBACK)


def ambiguityPrompt(conflicts: list[dict[str, Any]]) -> str:
    """`runtime_dynamic` 相位的问句：带冲突条数 + 去重后的种类清单。"""
    kinds = sorted({str(item.get("kind") or "") for item in conflicts if isinstance(item, dict)})
    labels = "、".join(conflictKindLabel(kind) for kind in kinds if kind != "")
    if labels == "":
        return _AMBIGUITY_PROMPT_FALLBACK
    return f"检测到 {len(conflicts)} 处语义歧义（{labels}），请确认采用哪一项？"


def hypothesisPrompt(candidates: list[dict[str, Any]]) -> str:
    """`hypothesis` 相位的问句：带候选条数；空候选时给下一步指引。"""
    if not candidates:
        return _EMPTY_CANDIDATES_PROMPT
    return f"共 {len(candidates)} 条候选假设，请选择要验证的（可多选）："
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && uv run pytest app/tests/unit/test_research_checkpoint_prompt.py -q
```

Expected: PASS（7 passed）。

- [ ] **Step 5: 接到两个调用点**

`backend/app/services/research_agent_service.py:111` 的 import 块改为（按名导入，字母序）：

```python
from app.services.research_agent_stages import (
    ambiguityPrompt, candidateConfidence, clientModelName, dataSummary, drivers,
    eslClasses, hypothesisPrompt, rebuildState, resumeTurnContent, rewriteState,
    selectedHypotheses,
)
```

`:487` 的返回值第三项：

```python
# 改前：                "检测到语义歧义，如何处理？",
                ambiguityPrompt(arms["conflicts"]),
```

`:600` 的返回值第三项：

```python
# 改前：            "验证哪些假设？",
            hypothesisPrompt(candidates),
```

- [ ] **Step 6: 跑既有研究相关单测 + 集成测试确认零回归**

```bash
cd backend && uv run pytest app/tests/unit -q -k research
```

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py app/tests/integration/test_research_stream.py -q
```

Expected: PASS。既有测试只钉了 `"三臂是否齐全？"`（本 Task 未改），没有测试钉 `"检测到语义歧义"` / `"验证哪些假设？"`，故零回归是预期结果。**若有红，先读断言再决定，不要改测试去迁就实现。**

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/services/research_agent_stages.py backend/app/services/research_agent_service.py backend/app/tests/unit/test_research_checkpoint_prompt.py
git commit -m "feat(research): 检查点问句带对象（歧义种类/候选条数），抽为可单测的纯函数"
```

---

## Task 2: W1-b 前端检查点结构化渲染

**Files:**
- Modify: `frontend/src/components/research/CheckpointCard.tsx`（整体重写渲染层）
- Modify: `frontend/src/pages/research/ResearchSessionPage.tsx:90`（加 `key`，见 Step 6 说明）
- Modify: `frontend/src/i18n/zh-CN.ts:2909-2916`（`research.checkpoint` 块）
- Modify: `frontend/src/i18n/en-US.ts:2894-2901`（同上）
- Test: `frontend/src/tests/ResearchCheckpointCard.test.tsx`（追加用例，不删既有用例）

**Interfaces:**
- Consumes: Task 1 的后端文案（`prompt` 现在带条数与种类）——本 Task 不依赖其代码，只依赖同一个数据结构。
- Produces: 无导出新增（`CheckpointCard` 组件签名不变：`{ checkpoint, onAnswer }`）。

**背景**：`options.conflicts`（歧义清单）、`options.plan.steps`（计划步骤）、`options.candidates`（候选假设）**已随 SSE 下发并被 store 完整接收**（`researchStore.ts:63` 整包保留 `options`），但 `CheckpointCard` 只读 `options.arms.metrics`，从不读它们。本 Task 把三类内容渲染出来，并加「本次针对什么」目标行（治用户反馈第 4 条「不知道三个按钮是针对哪个事项」）。

**两个必须处理的坑：**

1. **键名混用（真 landmine）**：`options.plan.steps[].sub_question` 是 **snake_case**（后端 `normalizePlan` 产物），而 `options.stepResults[].subQuestion` 是 **camelCase**（`stepResult`）。且**历史 checkpoint 的 options 已落库** —— 不能靠改后端键名统一（会让新旧数据形态不一致）。⇒ 前端**双键回落读取**，绝不「顺手统一」后端键名。
2. **降级空态**：无 LLM / 解析失败路径下 `candidates` 与 `conflicts` 本来就是 `[]`。渲染修好后这种情况仍会空白 ⇒ **必须补显式空态文案**，否则用户会误判为「没修好」。

**字段来源（写代码前请核对，不要猜）：**
- `options.conflicts[]`：`{ kind, detail, candidates[] }`；`kind` 取值 `"metric_ambiguous"` | `"wiki_disagree"`；候选形状 `{ kpiCode, displayName, confidence }`（指标歧义）或 `{ pageId, title }`（知识冲突）。产出点 `backend/app/services/enterprise_semantic_layer.py:119-135`。
- `options.plan.steps[]`：`{ sub_question, ... }`（snake_case）。
- `options.candidates[]`：`Hypothesis` 的 `asdict` ⇒ `{ statement, driver, verificationSql }`。**勾选结果按数组下标提交**（后端 `selectedHypotheses` 消费 `choice["selectedIndexes"]`）。

- [ ] **Step 1: 写失败测试**

在 `frontend/src/tests/ResearchCheckpointCard.test.tsx` **末尾新增一个 describe 块**（既有 5 个用例一字不改 —— 它们钉的是 confirm/reject 空 choice 与 arms 摘要，本 Task 必须保持其绿）。

**动手前先看该文件头部**：本 Task 新增用例用到的 `render` / `screen` / `fireEvent` / `vi` 以及包裹用的 `ConfigProvider`，**一律沿用该文件既有用例的 import 与包裹方式**（若既有用例已包 `ConfigProvider` 就照抄那层包裹；缺哪个 import 就补哪个，不要改动既有用例的结构）。

```tsx
describe("CheckpointCard 结构化渲染（W1）", () => {
  const conflictOptions = {
    signal: "metric_ambiguous",
    resumePhase: "plan",
    arms: { metrics: [], businessObjects: [], knowledge: [], conflicts: [] },
    conflicts: [
      {
        kind: "metric_ambiguous",
        detail: "前两名 metric 分差 < 0.05",
        candidates: [
          { kpiCode: "KPI-A", displayName: "供货量", confidence: 0.71 },
          { kpiCode: "KPI-B", displayName: "收货量", confidence: 0.66 },
        ],
      },
    ],
  };

  it("目标行按 phase 显示「本次针对什么」", () => {
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({ phase: "runtime_dynamic", options: conflictOptions })}
          onAnswer={() => {}}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("本次针对")).toBeInTheDocument();
    expect(screen.getByText("确认语义歧义的处理方式")).toBeInTheDocument();
  });

  it("runtime_dynamic：渲染歧义种类、detail 与候选（名称 + 置信度）", () => {
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({ phase: "runtime_dynamic", options: conflictOptions })}
          onAnswer={() => {}}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("指标歧义")).toBeInTheDocument();
    expect(screen.getByText("前两名 metric 分差 < 0.05")).toBeInTheDocument();
    expect(screen.getByText("供货量")).toBeInTheDocument();
    expect(screen.getByText(/0\.71/)).toBeInTheDocument();
  });

  it("runtime_dynamic 但 conflicts 为空：给空态文案而非空白", () => {
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({
            phase: "runtime_dynamic",
            options: { ...conflictOptions, conflicts: [] },
          })}
          onAnswer={() => {}}
        />
      </ConfigProvider>
    );
    expect(
      screen.getByText("本次未返回歧义明细，可直接点「修改」补充说明。")
    ).toBeInTheDocument();
  });

  it("planning：双键回落读取 steps（snake_case 与 camelCase 都能取到）", () => {
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({
            phase: "planning",
            options: {
              arms: { metrics: [], conflicts: [] },
              plan: { steps: [{ sub_question: "按收货地点拆分" }, { subQuestion: "按月拆分" }] },
            },
          })}
          onAnswer={() => {}}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("按收货地点拆分")).toBeInTheDocument();
    expect(screen.getByText("按月拆分")).toBeInTheDocument();
  });

  it("hypothesis：候选渲染为可勾选项，confirm 提交选中的下标", () => {
    const onAnswer = vi.fn();
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({
            phase: "hypothesis",
            options: {
              arms: { metrics: [], conflicts: [] },
              candidates: [
                { statement: "供货量下降因供应商切换", driver: "GR_QTY" },
                { statement: "供货量下降因收货地点变化", driver: "RCV_SITE" },
              ],
            },
          })}
          onAnswer={onAnswer}
        />
      </ConfigProvider>
    );
    // 下标顺序 = candidates 数组顺序：点第 2 项 ⇒ 提交 [1]
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: /确\s*认/ }));
    expect(onAnswer).toHaveBeenCalledWith("confirm", { selectedIndexes: [1] });
  });

  it("hypothesis 但候选为空：给空态文案而非空白", () => {
    render(
      <ConfigProvider>
        <CheckpointCard
          checkpoint={makeCheckpoint({
            phase: "hypothesis",
            options: { arms: { metrics: [], conflicts: [] }, candidates: [] },
          })}
          onAnswer={() => {}}
        />
      </ConfigProvider>
    );
    expect(
      screen.getByText(
        "本轮未生成候选假设（模型不可用或解析失败），可直接点「修改」补充研究方向。"
      )
    ).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchCheckpointCard.test.tsx
```

Expected: 新增 6 个用例 FAIL（`Unable to find an element with the text: 本次针对` 等）；既有 5 个用例 PASS。

- [ ] **Step 3: 补 i18n（先补，组件渲染依赖它）**

`frontend/src/i18n/zh-CN.ts` 的 `research.checkpoint` 块（当前 `:2909-2916`）整体替换为：

```ts
    checkpoint: {
      title: "待决策检查点",
      targetLabel: "本次针对",
      targetUnknown: "确认本次决策",
      target: {
        intent: "确认研究范围",
        planning: "确认研究计划",
        hypothesis: "选择要验证的假设",
        runtime_dynamic: "确认语义歧义的处理方式",
        low_confidence_step: "处理执行失败的步骤",
      },
      conflicts: "待确认的歧义",
      conflictKind: {
        metric_ambiguous: "指标歧义",
        wiki_disagree: "知识冲突",
      },
      conflictKindFallback: "待确认项",
      confidence: "置信",
      emptyConflicts: "本次未返回歧义明细，可直接点「修改」补充说明。",
      planSteps: "研究计划步骤",
      emptyPlanSteps: "本次未返回计划步骤，可直接点「修改」补充研究方向。",
      candidates: "候选假设（可多选）",
      unnamedItem: "（未提供描述）",
      emptyCandidates: "本轮未生成候选假设（模型不可用或解析失败），可直接点「修改」补充研究方向。",
      confirm: "确认",
      modify: "修改",
      reject: "拒绝",
      modifyPlaceholder: "输入修改后的要求…",
      submitModify: "提交修改",
    },
```

`frontend/src/i18n/en-US.ts` 的 `research.checkpoint` 块（当前 `:2894-2901`）整体替换为：

```ts
    checkpoint: {
      title: "Checkpoint",
      targetLabel: "This decision concerns",
      targetUnknown: "Confirm this decision",
      target: {
        intent: "Confirm the research scope",
        planning: "Confirm the research plan",
        hypothesis: "Select hypotheses to verify",
        runtime_dynamic: "Resolve the detected ambiguity",
        low_confidence_step: "Handle the failed step",
      },
      conflicts: "Ambiguities to resolve",
      conflictKind: {
        metric_ambiguous: "Metric ambiguity",
        wiki_disagree: "Knowledge conflict",
      },
      conflictKindFallback: "Item to confirm",
      confidence: "confidence",
      emptyConflicts: "No ambiguity details were returned. Click Modify to add context.",
      planSteps: "Plan steps",
      emptyPlanSteps: "No plan steps were returned. Click Modify to add a research direction.",
      candidates: "Candidate hypotheses (multi-select)",
      unnamedItem: "(no description)",
      emptyCandidates: "No candidate hypotheses were generated this round (model unavailable or parse failure). Click Modify to add a research direction.",
      confirm: "Confirm",
      modify: "Modify",
      reject: "Reject",
      modifyPlaceholder: "Enter the revised requirement…",
      submitModify: "Submit",
    },
```

- [ ] **Step 4: 实现组件**

`frontend/src/components/research/CheckpointCard.tsx` 整体替换为：

```tsx
/** 待决策检查点卡片（feat-research-entry Task 10；feat-research-entry-ux-fixes W1 结构化渲染）。
 *
 * 纯展示 + 回调组件：读取 `checkpoint.prompt` 作为问题文本，按 `checkpoint.phase`
 * 渲染「本次针对什么」目标行与相位相关明细（歧义 / 计划步骤 / 候选假设），
 * confirm / modify / reject 三动作经 `onAnswer(action, choice)` 上抛。错误处置
 * （uiHint 类分支）由 store 负责，本组件不落任何 code 分支。
 *
 * 键名坑（W1 的关键约束）：`options.plan.steps[].sub_question` 是 **snake_case**
 * （后端 normalizePlan 产物），而 `options.stepResults[].subQuestion` 是 camelCase。
 * 历史 checkpoint 的 options 已落库，不能靠改后端键名统一 ⇒ 这里双键回落读取。
 */
import { useState } from "react";
import { Button, Card, Checkbox, Input, Space, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import type { CheckpointAction, ResearchCheckpoint } from "../../types/research";

interface CheckpointCardProps {
  checkpoint: ResearchCheckpoint;
  onAnswer: (action: CheckpointAction, choice: Record<string, unknown>) => void;
}

interface ConflictCandidateView {
  label: string;
  confidence: string;
}

interface ConflictView {
  kind: string;
  detail: string;
  candidates: ConflictCandidateView[];
}

// 已知冲突种类（后端 enterprise_semantic_layer._detectConflicts 的产出集）；
// 表外种类回落通用标签，不构造文案、不抛错。
const CONFLICT_KIND_KEYS: ReadonlySet<string> = new Set<string>([
  "metric_ambiguous",
  "wiki_disagree",
]);

// 已知检查点相位（与后端 CHECKPOINT_* 常量集一致）；表外相位回落通用目标文案。
const CHECKPOINT_PHASES: ReadonlySet<string> = new Set<string>([
  "intent",
  "planning",
  "hypothesis",
  "runtime_dynamic",
  "low_confidence_step",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// 冲突候选 / 假设候选的统一显示名：指标歧义给 displayName，知识冲突给 title，
// 假设给 statement（后端假说的 dataclass 字段）。
function candidateLabel(item: Record<string, unknown>): string {
  for (const key of ["displayName", "title", "statement"]) {
    const value = item[key];
    if (typeof value === "string" && value.trim().length > 0) return value;
  }
  return "";
}

// options.arms.metrics[] 的 displayName 摘要（逐字来自后端三臂载荷，不本地构造文案）。
function metricLabels(options: Record<string, unknown>): string[] {
  const arms = options.arms;
  if (!isRecord(arms) || !Array.isArray(arms.metrics)) return [];
  const labels: string[] = [];
  for (const item of arms.metrics) {
    if (isRecord(item) && typeof item.displayName === "string") {
      labels.push(item.displayName);
    }
  }
  return labels;
}

function conflictViews(options: Record<string, unknown>): ConflictView[] {
  const raw = options.conflicts;
  if (!Array.isArray(raw)) return [];
  const views: ConflictView[] = [];
  for (const item of raw) {
    if (!isRecord(item)) continue;
    const candidates: ConflictCandidateView[] = [];
    if (Array.isArray(item.candidates)) {
      for (const candidate of item.candidates) {
        if (!isRecord(candidate)) continue;
        candidates.push({
          label: candidateLabel(candidate),
          confidence:
            typeof candidate.confidence === "number" ? candidate.confidence.toFixed(2) : "",
        });
      }
    }
    views.push({
      kind: typeof item.kind === "string" ? item.kind : "",
      detail: typeof item.detail === "string" ? item.detail : "",
      candidates,
    });
  }
  return views;
}

// 计划步骤描述：先 camelCase 再 snake_case 回落（见文件头「键名坑」）。
function planSteps(options: Record<string, unknown>): string[] {
  const plan = options.plan;
  if (!isRecord(plan) || !Array.isArray(plan.steps)) return [];
  const steps: string[] = [];
  for (const step of plan.steps) {
    if (!isRecord(step)) continue;
    const camel = step.subQuestion;
    const snake = step.sub_question;
    if (typeof camel === "string") {
      steps.push(camel);
    } else {
      steps.push(typeof snake === "string" ? snake : "");
    }
  }
  return steps;
}

// 候选假设描述。**不按下标过滤空串** —— 下标即提交给后端的 selectedIndexes。
function hypothesisStatements(options: Record<string, unknown>): string[] {
  const raw = options.candidates;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(isRecord)
    .map((item) => (typeof item.statement === "string" ? item.statement : ""));
}

export function CheckpointCard({ checkpoint, onAnswer }: CheckpointCardProps) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [selectedIndexes, setSelectedIndexes] = useState<number[]>([]);

  const labels = metricLabels(checkpoint.options);
  const conflicts = conflictViews(checkpoint.options);
  const steps = planSteps(checkpoint.options);
  const candidates = hypothesisStatements(checkpoint.options);

  // 明细块按相位出：只有对应相位才可能承载该数据，其他相位不渲染（避免每张卡片都挂
  // 一句与本相位无关的空态文案）。
  const showConflicts = checkpoint.phase === "runtime_dynamic";
  const showPlanSteps = checkpoint.phase === "planning";
  const showCandidates = checkpoint.phase === "hypothesis";

  const targetKey = CHECKPOINT_PHASES.has(checkpoint.phase)
    ? `research.checkpoint.target.${checkpoint.phase}`
    : "research.checkpoint.targetUnknown";

  const kindKey = (kind: string): string =>
    CONFLICT_KIND_KEYS.has(kind)
      ? `research.checkpoint.conflictKind.${kind}`
      : "research.checkpoint.conflictKindFallback";

  const submitModify = () => {
    const question = draft.trim();
    if (question.length === 0) return;
    onAnswer("modify", { question });
    setEditing(false);
    setDraft("");
  };

  const submitConfirm = () => {
    // 勾了候选才带上 selectedIndexes；未勾选 = 空 choice（与既有行为一致）。
    onAnswer("confirm", selectedIndexes.length > 0 ? { selectedIndexes } : {});
  };

  const toggleCandidate = (index: number, checked: boolean) => {
    setSelectedIndexes((prev) =>
      checked ? [...prev, index] : prev.filter((item) => item !== index),
    );
  };

  return (
    <Card size="small" title={t("research.checkpoint.title")}>
      <div style={{ marginBottom: 8 }}>
        <Tag color="blue">{t("research.checkpoint.targetLabel")}</Tag>
        <Typography.Text>{t(targetKey)}</Typography.Text>
      </div>
      <p>{checkpoint.prompt}</p>
      {labels.length > 0 && (
        <Space wrap>
          {labels.map((label) => (
            <Tag key={label}>{label}</Tag>
          ))}
        </Space>
      )}

      {showConflicts && (
        <div style={{ marginTop: 8 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("research.checkpoint.conflicts")}
          </Typography.Text>
          {conflicts.length > 0 ? (
            <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
              {conflicts.map((conflict, index) => (
                <li key={index}>
                  <Tag>{t(kindKey(conflict.kind))}</Tag>
                  {conflict.detail.length > 0 && <span>{conflict.detail}</span>}
                  {conflict.candidates.length > 0 && (
                    <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
                      {conflict.candidates.map((candidate, candidateIndex) => (
                        <li key={candidateIndex}>
                          {candidate.label || t("research.checkpoint.unnamedItem")}
                          {candidate.confidence.length > 0 && (
                            <span>
                              {" "}
                              · {t("research.checkpoint.confidence")} {candidate.confidence}
                            </span>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t("research.checkpoint.emptyConflicts")}
              </Typography.Text>
            </div>
          )}
        </div>
      )}

      {showPlanSteps && (
        <div style={{ marginTop: 8 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("research.checkpoint.planSteps")}
          </Typography.Text>
          {steps.length > 0 ? (
            <ol style={{ margin: "4px 0 0", paddingLeft: 20 }}>
              {steps.map((step, index) => (
                <li key={index}>{step || t("research.checkpoint.unnamedItem")}</li>
              ))}
            </ol>
          ) : (
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t("research.checkpoint.emptyPlanSteps")}
              </Typography.Text>
            </div>
          )}
        </div>
      )}

      {showCandidates && (
        <div style={{ marginTop: 8 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("research.checkpoint.candidates")}
          </Typography.Text>
          {candidates.length > 0 ? (
            <div style={{ display: "flex", flexDirection: "column", marginTop: 4 }}>
              {candidates.map((statement, index) => (
                <Checkbox
                  key={index}
                  checked={selectedIndexes.includes(index)}
                  onChange={(event) => toggleCandidate(index, event.target.checked)}
                >
                  {statement || t("research.checkpoint.unnamedItem")}
                </Checkbox>
              ))}
            </div>
          ) : (
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t("research.checkpoint.emptyCandidates")}
              </Typography.Text>
            </div>
          )}
        </div>
      )}

      <Space style={{ marginTop: 12 }}>
        <Button type="primary" onClick={submitConfirm}>
          {t("research.checkpoint.confirm")}
        </Button>
        <Button onClick={() => setEditing((value) => !value)}>
          {t("research.checkpoint.modify")}
        </Button>
        <Button danger onClick={() => onAnswer("reject", {})}>
          {t("research.checkpoint.reject")}
        </Button>
      </Space>
      {editing && (
        <div style={{ marginTop: 12 }}>
          <Input.TextArea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={t("research.checkpoint.modifyPlaceholder")}
            autoSize={{ minRows: 2 }}
          />
          <Button
            type="primary"
            onClick={submitModify}
            style={{ marginTop: 8 }}
            disabled={draft.trim().length === 0}
          >
            {t("research.checkpoint.submitModify")}
          </Button>
        </div>
      )}
    </Card>
  );
}
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchCheckpointCard.test.tsx
```

Expected: PASS（11 passed）。

- [ ] **Step 6: 给卡片加 `key` 重置勾选态（必做，否则跨检查点串味）**

`frontend/src/pages/research/ResearchSessionPage.tsx:90` 当前是：

```tsx
        <CheckpointCard checkpoint={pendingCheckpoint} onAnswer={handleAnswer} />
```

改为：

```tsx
        <CheckpointCard
          key={pendingCheckpoint.id}
          checkpoint={pendingCheckpoint}
          onAnswer={handleAnswer}
        />
```

**为什么必须加**：`CheckpointCard` 内部有 `useState`（`selectedIndexes` / `editing` / `draft`）。store 换掉 `pendingCheckpoint` 时 React 复用同一实例，`useState` 初始值**不会重跑** ⇒ 下一个检查点会带着上一个检查点的勾选/草稿。加 `key` 强制按检查点 id 重挂载，状态自然清零（同时也修掉了既有 `draft` 串味的隐患）。

- [ ] **Step 7: 跑页面测试确认零回归**

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchSessionPage.test.tsx src/tests/ResearchTimeline.test.tsx
```

Expected: PASS（新增的目标行只是多渲染一个 Tag 与一段文本，无既有断言依赖其计数）。

- [ ] **Step 8: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/components/research/CheckpointCard.tsx frontend/src/pages/research/ResearchSessionPage.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/tests/ResearchCheckpointCard.test.tsx
git commit -m "feat(research): 检查点卡片结构化渲染歧义/计划步骤/候选假设 + 目标行 + 空态"
```

---

## Task 3: W2 三模式说明（下拉描述 + 免责说明 + 报告页模式 Tag）

**Files:**
- Modify: `frontend/src/pages/research/ResearchListPage.tsx:17-21`（`MODE_OPTIONS` 加 `descKey`）、`:82-94`（Select 渲染两行 label + 免责说明）
- Modify: `frontend/src/pages/research/ResearchReportPage.tsx`（加模式 Tag）
- Modify: `frontend/src/i18n/zh-CN.ts`（`research.list` 块）、`frontend/src/i18n/en-US.ts`（同上）
- Test: `frontend/src/tests/ResearchListPage.test.tsx`（追加用例）、`frontend/src/tests/ResearchReportPage.test.tsx`（追加用例）

**Interfaces:**
- Consumes: `ReportPayload.mode: string`（`components/research/ReportRenderer.tsx:36`，由 `parseReportPayload` 保证非空，缺省 `"research"`）、`ResearchMode` 类型。
- Produces: 无导出新增。

**背景**：`research` / `attribution` / `compare` **不是三条流水线**，而是 `research_session.mode` 上的**报告章节模板** —— 检索 / 计划 / 执行 / 假设 / 验证完全共用，唯一差异在 `report_planner._MODE_SECTIONS` 的章节顺序。界面必须诚实说明这一点，否则用户以为选了「归因」就会做归因分析。**本 Task 零后端改动**（`report_planner.py:473` 已把 `mode` 写进报告 payload）。

- [ ] **Step 1: 写失败测试**

在 `frontend/src/tests/ResearchListPage.test.tsx` 的 describe 内追加：

```tsx
  it("模式选项带副描述，且页面说明三模式只改报告章节结构", async () => {
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    // 选中项 label 是两行渲染：主标题 + 副描述（副描述在收起状态下也在 DOM 里）。
    expect(screen.getByText("执行摘要 → 数据 → 引用知识 → 方法学")).toBeInTheDocument();
    expect(
      screen.getByText("三者共用同一条研究流水线，仅改变报告的章节组织，不改变分析行为。"),
    ).toBeInTheDocument();
  });
```

在 `frontend/src/tests/ResearchReportPage.test.tsx` 的 describe 内追加：

```tsx
  it("渲染报告的模式 Tag（payload.mode）", async () => {
    // mockApi 的签名以该文件既有写法为准；只需让报告 payload 的 mode 变为 attribution。
    mockApi({ ...GOOD_PAYLOAD, mode: "attribution" });
    renderPage();

    expect(await screen.findByText("归因")).toBeInTheDocument();
  });

  it("payload.mode 未知：不渲染模式 Tag（不构造文案）", async () => {
    mockApi({ ...GOOD_PAYLOAD, mode: "brand_new_mode" });
    renderPage();

    expect(await screen.findByText("本月收货量分析报告")).toBeInTheDocument();
    expect(screen.queryByText("brand_new_mode")).not.toBeInTheDocument();
  });
```

**注意**：`mockApi` 的实际签名（几个参数、`report` 是第一层 payload 还是报告行 `{id, payload}`）**以该文件既有用例为准** —— 若既有用例传的是报告行对象，就把 mode 挂在它的 `payload` 里（`{ ...row, payload: { ...GOOD_PAYLOAD, mode: "attribution" } }`），不要为此改 helper 本身。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchListPage.test.tsx src/tests/ResearchReportPage.test.tsx
```

Expected: 新增 3 个用例 FAIL；既有用例 PASS。

- [ ] **Step 3: 补 i18n**

`zh-CN.ts` 的 `research.list` 块尾（`compare: "对比",` 之后）追加，并把 `mode` 保持原样：

```ts
      modeHint: "三者共用同一条研究流水线，仅改变报告的章节组织，不改变分析行为。",
      modeDesc: {
        research: "执行摘要 → 数据 → 引用知识 → 方法学",
        attribution: "结论 → 假设验证表 → 数据 → 备选假设",
        compare: "对比维度表 → 数据 → 差异分析",
      },
```

`en-US.ts` 的 `research.list` 块尾追加：

```ts
      modeHint: "All three share one research pipeline; they only change how the report is organised, not the analysis.",
      modeDesc: {
        research: "Executive summary → Data → Knowledge → Methodology",
        attribution: "Conclusion → Hypothesis table → Data → Alternatives",
        compare: "Comparison table → Data → Diff analysis",
      },
```

- [ ] **Step 4: 实现下拉两行 label + 免责说明**

`frontend/src/pages/research/ResearchListPage.tsx:17-21`：

```tsx
const MODE_OPTIONS: ReadonlyArray<{ value: ResearchMode; labelKey: string; descKey: string }> = [
  { value: "research", labelKey: "research.list.mode.research", descKey: "research.list.modeDesc.research" },
  { value: "attribution", labelKey: "research.list.mode.attribution", descKey: "research.list.modeDesc.attribution" },
  { value: "compare", labelKey: "research.list.mode.compare", descKey: "research.list.modeDesc.compare" },
];
```

同文件 `:82-94` 的 `<Space>` 块改为：

```tsx
          <Space>
            <Select<ResearchMode>
              value={mode}
              onChange={setMode}
              options={MODE_OPTIONS.map((option) => ({
                value: option.value,
                label: (
                  <div>
                    <div>{t(option.labelKey)}</div>
                    <div style={{ fontSize: 12, color: "#8c8c8c" }}>{t(option.descKey)}</div>
                  </div>
                ),
              }))}
            />
            <Button type="primary" loading={submitting} onClick={startResearch}>
              {t("research.list.start")}
            </Button>
          </Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("research.list.modeHint")}
          </Typography.Text>
```

- [ ] **Step 5: 实现报告页模式 Tag**

`frontend/src/pages/research/ResearchReportPage.tsx` —— 在 `parseReportPayload` 之上加类型收窄助手（`payload.mode` 是 `string`，需收窄才能走 i18n 的键类型），并在标题行渲染 Tag：

```tsx
import type { ResearchMode } from "../../types/research";

// payload.mode 由 report_planner 落库，运行时是任意 string ⇒ 收窄到已知
// 模式集合；未知值不渲染 Tag（不构造文案，也不显示原始 key）。
const MODE_KEYS: ReadonlySet<string> = new Set<string>(["research", "attribution", "compare"]);

function asResearchMode(value: string): ResearchMode | null {
  return MODE_KEYS.has(value) ? (value as ResearchMode) : null;
}
```

在组件内 `const payload: ReportPayload | null = ...` 之后加：

```tsx
  const mode = payload ? asResearchMode(payload.mode) : null;
```

在标题 `</Typography.Title>` 之后（仍在同一个 `<Space>` 内）插入：

```tsx
        {mode ? <Tag>{t(`research.list.mode.${mode}`)}</Tag> : null}
```

（`Tag` 与 `Typography` 已在 `:7` 导入，无需改 import。）

- [ ] **Step 6: 跑测试确认通过**

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchListPage.test.tsx src/tests/ResearchReportPage.test.tsx
```

Expected: PASS。**注意既有断言 `expect(screen.getAllByText("研究")).toHaveLength(2)` 必须仍然为 2** —— 两行 label 让「研究」多了 1 处（选中项的标题行），但副描述是独立文本节点且模式说明句是长句（精确匹配不命中），故计数不变。若变红，先读 `getAllByText` 的匹配结果再判断，不要直接改断言。

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/pages/research/ResearchListPage.tsx frontend/src/pages/research/ResearchReportPage.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/tests/ResearchListPage.test.tsx frontend/src/tests/ResearchReportPage.test.tsx
git commit -m "feat(research): 三模式加副描述与免责说明，报告页显示模式 Tag"
```

---

## Task 4: W3-a 后端删除会话（service + DELETE 端点）

**Files:**
- Modify: `backend/app/services/research_session_service.py`（import 加 `delete`；新增 `deleteSession`）
- Modify: `backend/app/api/v1/research.py`（新增 `DELETE /sessions/{sessionId}`）
- Test: `backend/app/tests/integration/test_research_api.py`（追加用例）

**Interfaces:**
- Consumes: `ResearchSessionService`（既有）、`_ownedSession(db, sessionId, user)`（`research.py:560-567`）、`getDb` / `_requireResearchUser`、`ResearchTurn` / `ResearchCheckpoint` / `ResearchReport` ORM、`PROMPT_KEY`（已导入）。
- Produces:
  - `ResearchSessionService.deleteSession(session, sessionId: uuid.UUID) -> int`（删除行数）
  - `DELETE /api/v1/research/sessions/{sessionId}` → `204 No Content`；归属不符 / 删除 0 行 → 404

**背景**：三层皆缺（无 endpoint / 无 store action / 无按钮），但物理删可复用既有 `ON DELETE CASCADE`（`research_models.py:43-48`，session → turn / checkpoint / finding / report），**无需迁移**。删除语义与 chat 一致（`session_history_service.py:267-309` 的硬删约定：用 `RETURNING` 计数，因为 asyncpg 的 rowcount 可能是 -1）。

- [ ] **Step 1: 写失败测试**

在 `backend/app/tests/integration/test_research_api.py` 末尾追加：

```python
# ---------------------------------------------------------------------------
# W3：删除会话（硬删 + 级联 + 归属 404）
# ---------------------------------------------------------------------------


async def _countRows(dbSession: AsyncSession, model: Any, sessionId: uuid.UUID) -> int:
    """统计某会话在子表中的行数（删后应全为 0 —— 验证 CASCADE 真的生效）。"""
    return await dbSession.scalar(
        select(func.count()).select_from(model).where(model.session_id == sessionId)
    )


async def test_delete_session_cascades_children_and_404_afterwards(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    """删除会话 → 204；turn / checkpoint / report 子行一并消失；再读 404。"""
    created = await _createSession(client, authHeaders, question="q")
    sid = uuid.UUID(created["id"])
    svc = ResearchSessionService()
    turn = await svc.appendTurn(dbSession, sessionId=sid, role="user", content={})
    await svc.openCheckpoint(
        dbSession, sessionId=sid, turnId=turn.id, phase="intent", options={}, prompt="p"
    )
    await svc.publishReport(dbSession, sessionId=sid, payload={"title": "t"}, renderedMd="# t")
    await dbSession.commit()

    resp = await client.delete(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert resp.status_code == 204, resp.text
    assert resp.content == b""

    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.status_code == 404
    assert await _countRows(dbSession, ResearchTurn, sid) == 0
    assert await _countRows(dbSession, ResearchCheckpoint, sid) == 0
    assert await _countRows(dbSession, ResearchReport, sid) == 0


async def test_delete_other_user_session_404_and_kept(
    client: AsyncClient,
    authHeaders: dict[str, str],
    secondUserHeaders: dict[str, str],
) -> None:
    """他人会话删除 → 404，且**真的没删**（不泄露存在性，也不做横向越权写）。"""
    created = await _createSession(client, authHeaders, question="q")
    resp = await client.delete(f"{_BASE}/sessions/{created['id']}", headers=secondUserHeaders)
    assert resp.status_code == 404

    kept = await client.get(f"{_BASE}/sessions/{created['id']}", headers=authHeaders)
    assert kept.status_code == 200


async def test_delete_missing_session_404(
    client: AsyncClient, authHeaders: dict[str, str]
) -> None:
    resp = await client.delete(f"{_BASE}/sessions/{uuid.uuid4()}", headers=authHeaders)
    assert resp.status_code == 404


async def test_delete_requires_auth(client: AsyncClient) -> None:
    """匿名删除 → 401（未过鉴权，不泄露会话是否存在）。"""
    resp = await client.delete(f"{_BASE}/sessions/{uuid.uuid4()}")
    assert resp.status_code == 401
```

**若 `ResearchTurn` / `ResearchCheckpoint` / `ResearchReport` / `func` 未在文件顶部导入，先补 import**（检查 `from app.domain.research_models import (...)` 与 `from sqlalchemy import func, select` 两处）。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py -q -k delete
```

Expected: FAIL —— `assert 405 == 204`（路由不存在，FastAPI 返回 405 Method Not Allowed）。

- [ ] **Step 3: 实现 service 方法**

`backend/app/services/research_session_service.py` 顶部 import 行改为（加 `delete`）：

```python
from sqlalchemy import delete, func, select, update
```

在 `createSession` 之后新增：

```python
    async def deleteSession(self, session: AsyncSession, sessionId: uuid.UUID) -> int:
        """硬删会话主行，返回删除行数（0 = 不存在）。

        级联由 DB 承担：`research_models.py:_session_fk()` 的 `ondelete="CASCADE"`
        会一并清 turn / checkpoint / finding / report —— 无需在应用层逐表删。

        用 `RETURNING` 计数而非 `result.rowcount`：asyncpg 对 DELETE 的 rowcount
        可能返回 -1（与 chat 的 `session_history_service.deleteSessionHistory`
        同一约定）。**不 commit** —— 提交由调用方（router）统一负责，与
        `createSession` 的 flush-only 风格一致。
        """
        rows = await session.scalars(
            delete(ResearchSession)
            .where(
                ResearchSession.id == sessionId,
                # 归属在 SQL 层再兜一次：调用方已校验，但删除是不可逆操作，多一道闸门。
                # （会话 id 全局唯一，故这里不需要 createdBy 参数——由 router 前置校验。）
            )
            .returning(ResearchSession.id)
        )
        return len(rows.all())
```

**注意**：上面的 `where` 只按 id（归属由 router 的 `_ownedSession` 前置校验，且删除 0 行也返回 404），这是刻意的 —— service 不持有 `CurrentUser`，把归属判断留在有身份的一层。保留注释说明这一点。

若 `ResearchSession` 未在该文件导入，补 `from app.domain.research_models import ResearchSession`。

- [ ] **Step 4: 跑 service 层相关既有测试**

```bash
cd backend && uv run pytest app/tests/unit -q -k research_session
```

Expected: PASS（新方法尚未被调用，零影响）。

- [ ] **Step 5: 实现 DELETE 端点**

`backend/app/api/v1/research.py` —— 在 `listSessions`（`:285-295`）之后、`_resolveDatasource` 附近插入：

```python
@router.delete(
    "/sessions/{sessionId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除研究会话",
)
async def deleteSession(
    sessionId: uuid.UUID,
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """硬删会话，级联清 turn / checkpoint / finding / report（复用 DB CASCADE）。

    归属不符 → 404（而非 403）：与详情 / 列表同一口径，不泄露会话存在性。
    """
    await _ownedSession(db, sessionId, user)
    removed = await _sessionService.deleteSession(db, sessionId)
    if removed == 0:
        # 归属已过仍删 0 行 = 并发下已被另一请求删掉；语义上仍是「不存在」。
        raise NotFoundError("研究会话不存在")
    await db.commit()
```

**注意**：`status_code=204` 的端点函数必须返回 `None`（不要 `return` 任何 body），否则 FastAPI 会因「204 不可带 body」报错。`NotFoundError` 已在该文件导入（`_ownedSession` 在用）。

- [ ] **Step 6: 跑测试确认通过**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py -q
```

Expected: PASS（含 4 个新用例）。**级联断言若红**：说明 DB 层 `ON DELETE CASCADE` 没生效（迁移未跑或 FK 缺失），这是真问题 —— 读 `research_models.py:43-48` 与 `alembic/versions/0111_research_entry_tables.py` 核对，**不要**退化成「在应用层逐表删」来让测试变绿。

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/services/research_session_service.py backend/app/api/v1/research.py backend/app/tests/integration/test_research_api.py
git commit -m "feat(research): 新增 DELETE /research/sessions/{id}（硬删 + CASCADE + 归属 404）"
```

---

## Task 5: W3-b 前端删除（api + store + 列表按钮 + Popconfirm）

**Files:**
- Modify: `frontend/src/api/research.ts`（新增 `deleteResearchSession`）
- Modify: `frontend/src/stores/researchStore.ts`（新增 `deleteSession` action + 接口声明）
- Modify: `frontend/src/pages/research/ResearchListPage.tsx`（列表项加删除按钮 + Popconfirm）
- Modify: `frontend/src/i18n/zh-CN.ts` / `en-US.ts`（`research.list` 块）
- Test: `frontend/src/tests/ResearchListPage.test.tsx`（追加用例）

**Interfaces:**
- Consumes: Task 4 的 `DELETE /api/v1/research/sessions/{sessionId}`（204）。
- Produces: `deleteResearchSession(sessionId: string): Promise<void>`、`ResearchState.deleteSession: (sessionId: string) => Promise<void>`。

**背景**：用户原话是「在现有的（模式）、打开、报告 基础上，增加删除」—— 研究页目前是**整页纵向列表**（**没有** chat 那种右侧可折叠历史面板，别改造成侧栏）。二次确认照 `components/chat/ChatHistoryPanel.tsx:156-174` 的 `Popconfirm` 写法。

- [ ] **Step 1: 写失败测试**

在 `frontend/src/tests/ResearchListPage.test.tsx` 的 describe 内追加（新用例用到的 `userEvent` **若该文件尚未导入则补 `import userEvent from "@testing-library/user-event";`** —— 其余 `render` / `screen` / `httpMock` 该文件已有）：

```tsx
  it("删除：二次确认后调 DELETE 并从列表移除", async () => {
    const user = userEvent.setup();
    httpMock.delete.mockResolvedValue({ data: null });
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "删除研究会话" }));
    await user.click(await screen.findByRole("button", { name: "确定" }));

    await waitFor(() =>
      expect(httpMock.delete).toHaveBeenCalledWith("/research/sessions/s1"),
    );
    await waitFor(() => expect(screen.queryByText("供应商 360°")).not.toBeInTheDocument());
    // s2 未删，仍在列表
    expect(screen.getByText("为什么下降")).toBeInTheDocument();
  });

  it("删除失败：提示错误且列表保持原样（不做乐观移除）", async () => {
    const user = userEvent.setup();
    httpMock.delete.mockRejectedValue(new Error("boom"));
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "删除研究会话" }));
    await user.click(await screen.findByRole("button", { name: "确定" }));

    expect(await screen.findByText("删除失败")).toBeInTheDocument();
    expect(screen.getByText("供应商 360°")).toBeInTheDocument();
  });
```

（`httpMock` 已含 `delete: vi.fn()`，无需改 mock 声明。）

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchListPage.test.tsx
```

Expected: 新增 2 个用例 FAIL（`Unable to find a button with the accessible name: 删除研究会话`）。

- [ ] **Step 3: 补 i18n**

`zh-CN.ts` 的 `research.list` 块追加：

```ts
      deleteConfirm: "删除该研究会话？其轮次、检查点与报告将一并清除，不可恢复。",
      deleteAriaLabel: "删除研究会话",
      deleteError: "删除失败",
```

`en-US.ts` 的 `research.list` 块追加：

```ts
      deleteConfirm: "Delete this research session? Its turns, checkpoints and reports will be removed too. This cannot be undone.",
      deleteAriaLabel: "Delete research session",
      deleteError: "Delete failed",
```

（按钮文本复用既有 `common.delete`（"删除"/"Delete"）与 `common.confirm` / `common.cancel`，不新增重复词条。）

- [ ] **Step 4: 实现 api 函数与 store action**

`frontend/src/api/research.ts`，在 `listResearchSessions` 之后插入：

```ts
export async function deleteResearchSession(sessionId: string): Promise<void> {
  await httpClient.delete(`${BASE}/sessions/${sessionId}`);
}
```

`frontend/src/stores/researchStore.ts`：

1) import 块加 `deleteResearchSession as apiDeleteResearchSession,`（按名导入，与既有别名风格一致）：

```ts
import {
  answerCheckpoint as apiAnswerCheckpoint,
  createResearchSession,
  deleteResearchSession as apiDeleteResearchSession,
  getReport as apiGetReport,
  getResearchSession,
  listReports as apiListReports,
  listResearchSessions,
  openResearchStream,
  submitTurn as apiSubmitTurn,
} from "../api/research";
```

2) `ResearchState` 接口加一行（放在 `loadSessions` 之后）：

```ts
  deleteSession: (sessionId: string) => Promise<void>;
```

3) 实现（放在 `loadSessions` 实现之后）：

```ts
  deleteSession: async (sessionId) => {
    set({ error: null });
    try {
      await apiDeleteResearchSession(sessionId);
      // 服务端确认**之后**才从本地移除（不做乐观移除）：删除失败必须让列表保持原样，
      // 否则用户会以为删掉了、刷新又回来。
      set((state) => ({
        sessions: state.sessions.filter((session) => session.id !== sessionId),
      }));
    } catch (err) {
      set({ error: errorMessage(err) });
      throw err;
    }
  },
```

- [ ] **Step 5: 实现列表页按钮**

`frontend/src/pages/research/ResearchListPage.tsx`：

1) `:9` 的 antd import 加 `Popconfirm`，并新增图标 import：

```tsx
import { App, Button, Card, Checkbox, Empty, Input, List, Popconfirm, Select, Space, Tag, Typography } from "antd";
import { DeleteOutlined } from "@ant-design/icons";
```

2) 组件内取 store action：

```tsx
  const deleteSession = useResearchStore((s) => s.deleteSession);
```

3) 新增处理函数（放在 `openCompare` 之前）：

```tsx
  const handleDelete = async (sessionId: string) => {
    try {
      await deleteSession(sessionId);
      // 已删会话不能留在多选集合里，否则「对比」会带上一个不存在的 id。
      setSelectedIds((prev) => prev.filter((item) => item !== sessionId));
    } catch {
      message.error(t("research.list.deleteError"));
    }
  };
```

4) `:105-116` 的 `actions` 数组尾部追加（**放在「报告」之后**）：

```tsx
                <Popconfirm
                  key="delete"
                  title={t("research.list.deleteConfirm")}
                  okText={t("common.confirm")}
                  cancelText={t("common.cancel")}
                  onConfirm={() => handleDelete(session.id)}
                >
                  <Button
                    type="link"
                    danger
                    icon={<DeleteOutlined />}
                    aria-label={t("research.list.deleteAriaLabel")}
                  >
                    {t("common.delete")}
                  </Button>
                </Popconfirm>,
```

- [ ] **Step 6: 跑测试确认通过**

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchListPage.test.tsx src/tests/researchStore.test.ts
```

Expected: PASS。`researchStore.test.ts` 须零回归（新 action 不改既有行为）。

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/api/research.ts frontend/src/stores/researchStore.ts frontend/src/pages/research/ResearchListPage.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/tests/ResearchListPage.test.tsx
git commit -m "feat(research): 会话列表支持删除（Popconfirm 二次确认 + 服务端确认后移除）"
```

---

## Task 6: W4 数据源选择（响应字段 + 新建选源 + 列表/会话页显示用库）

**Files:**
- Modify: `backend/app/domain/research_schemas.py:92-102`（`ResearchSessionRead` 加 `datasourceId`）
- Modify: `backend/app/api/v1/research.py:577-587`（`_sessionRead` 映射）
- Modify: `backend/app/tests/integration/test_research_api.py`（追加用例）
- Create: `frontend/src/hooks/useDatasourceOptions.ts`
- Modify: `frontend/src/types/research.ts:20-28`（`ResearchSession` 加 `datasourceId`）
- Modify: `frontend/src/api/research.ts:33-39`（`createResearchSession` 支持 `datasourceId`）
- Modify: `frontend/src/pages/research/ResearchListPage.tsx`（新建表单加 Select + 列表项源 Tag）
- Modify: `frontend/src/pages/research/ResearchSessionPage.tsx`（会话页源 Tag）
- Modify: `frontend/src/i18n/zh-CN.ts` / `en-US.ts`
- Test: `frontend/src/tests/ResearchListPage.test.tsx`（改 mock 分发 + 改 POST 断言 + 追加用例）、`frontend/src/tests/ResearchSessionPage.test.tsx`（追加用例）

**Interfaces:**
- Consumes: `DataSourceService.list/get`（既有）、`api/datasource.ts:listDataSources(activeOnly)`、`types/datasource.ts:DataSource.isDefault`、`hooks/` 目录（**若不存在则新建该目录**）。
- Produces:
  - `ResearchSessionRead.datasourceId: int | None`
  - `ResearchSession.datasourceId?: number | null`（前端类型）
  - `createResearchSession(input: { question: string; mode?: ResearchMode; datasourceId?: number | null })`
  - `useDatasourceOptions(): DataSource[]`、`datasourceName(sources: DataSource[], id: number | null | undefined): string`

**背景（诊断纠正）**：接收侧 / 落库 / 执行期读取三层**本来就已通**（请求 schema 有 `datasourceId`、迁移 0112 有列、`research_agent_execution.depsForSession` 按会话取源、缺省回落默认源），前端一行没接线；但**响应侧不回显** —— `ResearchSessionRead` 没有 `datasourceId`，所以「显示用了哪个库」**不是纯前端接线**，要补一个响应字段。

- [ ] **Step 1: 写后端失败测试**

在 `backend/app/tests/integration/test_research_api.py` 追加：

```python
async def test_session_read_echoes_datasource_id(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    """响应回显 datasourceId（W4）：创建响应与列表响应都要有（治「不知道对哪个库研究」）。"""
    expected = await _seededDatasourceId(dbSession)
    created = await _createSession(client, authHeaders, question="q")
    assert created["datasourceId"] == expected

    listed = await client.get(f"{_BASE}/sessions", headers=authHeaders)
    assert listed.status_code == 200
    assert listed.json()[0]["datasourceId"] == expected
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py -q -k echoes_datasource
```

Expected: FAIL —— `KeyError: 'datasourceId'`。

- [ ] **Step 3: 实现后端响应字段**

`backend/app/domain/research_schemas.py` 的 `ResearchSessionRead` 加字段（放在 `question` 之后）：

```python
    datasourceId: int | None = None
```

`backend/app/api/v1/research.py:577-587` 的 `_sessionRead` 加一行：

```python
        question=row.input_seed or "",
        datasourceId=row.datasource_id,
        createdAt=row.created_at,
```

- [ ] **Step 4: 跑后端测试确认通过**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py -q
```

Expected: PASS。

- [ ] **Step 5: 写前端失败测试**

**先改 `frontend/src/tests/ResearchListPage.test.tsx` 的 mock 为按 URL 分发**（现为 `mockResolvedValue` 对所有 URL 返回 SESSIONS —— 加 `/datasources` 调用后会拿到错误形状）：

1) `SESSIONS[0]` 加 `datasourceId: 1`：

```tsx
const SESSIONS = [
  {
    id: "s1", title: "供应商 360°", mode: "research", status: "succeeded",
    question: "供应商 360° 全景", datasourceId: 1,
    createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-01T00:00:00Z",
  },
  {
    id: "s2", title: "", mode: "attribution", status: "succeeded",
    question: "为什么下降", createdAt: "2026-01-02T00:00:00Z", updatedAt: "2026-01-02T00:00:00Z",
  },
];

const DATASOURCES = [
  { id: 1, name: "THBI Oracle", type: "oracle", isDefault: true, isActive: true },
  { id: 2, name: "PG 报表库", type: "postgresql", isDefault: false, isActive: true },
];

// 按 URL 分发：数据源清单走 /datasources，其余（会话列表）走 SESSIONS。
function mockGet(overrides: { sessions?: unknown; datasources?: unknown } = {}) {
  httpMock.get.mockImplementation((url: string) => {
    if (url === "/datasources") {
      return Promise.resolve({ data: overrides.datasources ?? DATASOURCES });
    }
    return Promise.resolve({ data: overrides.sessions ?? SESSIONS });
  });
}
```

2) `beforeEach` 里的 `httpMock.get.mockResolvedValue({ data: SESSIONS });` 改为 `mockGet();`；空列表用例里的 `httpMock.get.mockResolvedValue({ data: [] });` 改为 `mockGet({ sessions: [] });`。

3) 把 POST 断言改为带数据源（默认选中 `isDefault` 源 ⇒ id 1）：

```tsx
    await waitFor(() =>
      expect(httpMock.post).toHaveBeenCalledWith("/research/sessions", {
        question: "供应商 360° 全景",
        mode: "research",
        datasourceId: 1,
      }),
    );
```

4) describe 内追加：

```tsx
  it("新建表单默认选中默认数据源；列表项显示所用数据源名", async () => {
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalledWith("/datasources", { params: { activeOnly: true } }));

    // 「THBI Oracle」出现 2 处：新建表单选中项 + s1（datasourceId=1）的 Tag。
    await waitFor(() => expect(screen.getAllByText("THBI Oracle")).toHaveLength(2));
    // s2 无 datasourceId ⇒ 不渲染源 Tag，故「PG 报表库」不出现。
    expect(screen.queryByText("PG 报表库")).not.toBeInTheDocument();
  });
```

在 `frontend/src/tests/ResearchSessionPage.test.tsx` 追加：

```tsx
  it("会话页显示本次研究的数据源名", async () => {
    httpMock.get.mockImplementation((url: string) => {
      if (url === "/datasources") {
        return Promise.resolve({ data: [{ id: 7, name: "THBI Oracle", isDefault: true }] });
      }
      const detail = makeDetail();
      // 不可变构造：不改 makeDetail() 的返回值本身。
      return Promise.resolve({
        data: { ...detail.data, session: { ...detail.data.session, datasourceId: 7 } },
      });
    });

    renderPage();

    expect(await screen.findByText("THBI Oracle")).toBeInTheDocument();
  });
```

- [ ] **Step 6: 跑前端测试确认失败**

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchListPage.test.tsx src/tests/ResearchSessionPage.test.tsx
```

Expected: 新增用例 FAIL（找不到 `THBI Oracle`）；既有用例在 mock 改造后仍 PASS（`/datasources` 走 DATASOURCES、其余走 SESSIONS）。

- [ ] **Step 7: 实现前端**

`frontend/src/types/research.ts` 的 `ResearchSession` 加：

```ts
  datasourceId?: number | null;
```

`frontend/src/api/research.ts:33-39` 改为：

```ts
export async function createResearchSession(input: {
  question: string;
  mode?: ResearchMode;
  datasourceId?: number | null;
}): Promise<ResearchSession> {
  const res = await httpClient.post<ResearchSession>(`${BASE}/sessions`, input);
  return res.data;
}
```

**新建 `frontend/src/hooks/useDatasourceOptions.ts`**：

```ts
/** 启用中的业务数据源清单（feat-research-entry-ux-fixes W4）。
 *
 * 用途有二：新建研究时选源（默认选 `isDefault`），以及列表 / 会话页把会话上的
 * `datasourceId` 映射成可读名称（治「不知道对哪个库研究」）。
 *
 * 失败或形状异常一律回落空清单：数据源名只是**辅助信息**，不该成为页面可用性的
 * 前提（本页不做任何依赖它的写操作）。故此处的降级是有意的，且仅限于展示层。
 */
import { useEffect, useState } from "react";
import { listDataSources } from "../api/datasource";
import type { DataSource } from "../types/datasource";

export function useDatasourceOptions(): DataSource[] {
  const [sources, setSources] = useState<DataSource[]>([]);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const list = await listDataSources(true);
        if (alive && Array.isArray(list)) setSources(list);
      } catch {
        // 展示层降级：名称缺失即不渲染 Tag（见文件头注释）。
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return sources;
}

/** 会话上的 datasourceId → 可读名称；id 缺失返回空串，清单里查不到回落 `#id`。 */
export function datasourceName(
  sources: DataSource[],
  id: number | null | undefined,
): string {
  if (id === null || id === undefined) return "";
  return sources.find((source) => source.id === id)?.name ?? `#${id}`;
}
```

`frontend/src/pages/research/ResearchListPage.tsx`：

1) import：

```tsx
import { useDatasourceOptions, datasourceName } from "../../hooks/useDatasourceOptions";
```

2) 组件内：

```tsx
  const sources = useDatasourceOptions();
  const [datasourceId, setDatasourceId] = useState<number | null>(null);
```

3) 默认选中默认源（放在既有 `loadSessions` 的 `useEffect` 之后）：

```tsx
  useEffect(() => {
    if (datasourceId !== null) return;
    const preferred = sources.find((source) => source.isDefault) ?? sources[0];
    if (preferred) setDatasourceId(preferred.id);
  }, [sources, datasourceId]);
```

4) `startResearch` 的请求体：

```tsx
      const session = await createResearchSession({
        question: trimmed,
        mode,
        datasourceId,
      });
```

5) 新建表单里，`<Select<ResearchMode>>` 之前插入数据源 Select：

```tsx
            <Select<number>
              value={datasourceId ?? undefined}
              onChange={setDatasourceId}
              placeholder={t("research.list.datasourcePlaceholder")}
              options={sources.map((source) => ({ value: source.id, label: source.name }))}
            />
```

6) 列表项里，在模式 Tag 之前插入源 Tag：

```tsx
              {datasourceName(sources, session.datasourceId) ? (
                <Tag>{datasourceName(sources, session.datasourceId)}</Tag>
              ) : null}
```

`frontend/src/pages/research/ResearchSessionPage.tsx`：加同样的 import、`const sources = useDatasourceOptions();`，并在标题行渲染：

```tsx
        {datasourceName(sources, session?.datasourceId) ? (
          <Tag>{datasourceName(sources, session?.datasourceId)}</Tag>
        ) : null}
```

**动手前先读该页**：会话详情在这页里叫什么（`session` / `currentSession` / `detail.session`）**以该页既有写法为准** —— 用同一个来源取 `datasourceId`，**不要为此新增 store 字段**。`Tag` 若未导入则补进 antd import。

`zh-CN.ts` 的 `research.list` 块追加：

```ts
      datasourcePlaceholder: "选择数据源",
```

`en-US.ts` 同位置：

```ts
      datasourcePlaceholder: "Select a datasource",
```

- [ ] **Step 8: 跑测试确认通过**

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchListPage.test.tsx src/tests/ResearchSessionPage.test.tsx src/tests/researchStore.test.ts
```

Expected: PASS。

- [ ] **Step 9: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/domain/research_schemas.py backend/app/api/v1/research.py backend/app/tests/integration/test_research_api.py frontend/src/hooks/useDatasourceOptions.ts frontend/src/types/research.ts frontend/src/api/research.ts frontend/src/pages/research/ResearchListPage.tsx frontend/src/pages/research/ResearchSessionPage.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/tests/ResearchListPage.test.tsx frontend/src/tests/ResearchSessionPage.test.tsx
git commit -m "feat(research): 新建可选数据源（默认选默认源），列表与会话页显示所用库"
```

---

## Task 7: 批一验收（全量研究相关套件 + 前端全量 + 构建）

**Files:** 无改动（纯验收）；若发现回归则回到对应 Task 修。

**Interfaces:**
- Consumes: Task 1–6 的全部产出。
- Produces: 无。

- [ ] **Step 1: 后端研究相关集成 + 单测（串行，真实 PG 5434）**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py app/tests/integration/test_research_stream.py -q
```

```bash
cd backend && uv run pytest app/tests/unit -q -k research
```

Expected: PASS。

- [ ] **Step 2: 前端研究相关套件**

**先用 `ls src/tests | grep -i research` 核对实际文件名**（下列清单以此为准；含 Report / Compare / Timeline 前后缀的若干文件按实际存在的挑，**不要照抄不存在的路径** —— vitest 对不匹配的过滤器会报错而非静默跳过）：

```bash
cd frontend && ls src/tests | grep -i research
```

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchCheckpointCard.test.tsx src/tests/ResearchListPage.test.tsx src/tests/ResearchSessionPage.test.tsx src/tests/ResearchReportPage.test.tsx src/tests/researchStore.test.ts
```

Expected: PASS（外加 Step 2 上半步 `ls` 列出的其余 research 相关文件，一并纳入同一命令）。

- [ ] **Step 3: 前端类型检查与生产构建**

```bash
cd frontend && npx tsc --noEmit && npm run build
```

Expected: 无类型错误、构建成功。（这一步是「改完前端必须能装进镜像」的前置；容器验收见 Step 5。）

- [ ] **Step 4: 对照 HEAD 基线确认零新增失败（前端全量）**

```bash
cd frontend && npx vitest run --testTimeout=30000
```

Expected: 失败数 ≤ 基线。**已记录的基线**：3 个失败文件（`AgentRegistryPage.test.tsx` 等，均为既有红，与本次改动无关）。**若失败数 > 3，逐条读断言**，不得当作既有红放过。

- [ ] **Step 5: 容器构建与部署冒烟（前端必须走 compose build）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
docker compose build --no-cache frontend
docker compose up -d
```

真机验收清单（批一）：
1. 新建研究：能看到「数据源」下拉且**默认选中默认源**；模式下拉每一项有副描述；下方有「三者共用同一条研究流水线…」说明。
2. 会话列表：每项有「删除」，点开有二次确认；确认后该行消失，刷新后仍不存在。
3. 跑到一个语义歧义检查点：卡片顶部有「本次针对：确认语义歧义的处理方式」，列出歧义种类 + detail + 候选（名称与置信度）。
4. 跑到假设检查点：候选可勾选；勾选后点「确认」，后续验证只针对所选项。
5. 会话列表与会话页显示本次研究用的库名。
6. 报告页标题旁显示模式 Tag。

- [ ] **Step 6: 记录验收结果**

把 Step 1–5 的实际输出（通过数 / 失败数 / 冒烟结论）记下来，供 Task 11 填 Harness `§6 测试` 与 `§8 部署验证`。**不要凭记忆写「全绿」** —— 只写实际跑出来的结果。

---

# 批二：W5（动 DB，前后端必须同批上线）

## Task 8: W5-a 迁移 + schema + 创建端点落库

**Files:**
- Create: `backend/alembic/versions/0113_research_session_model.py`
- Modify: `backend/app/domain/research_models.py:51-76`（`ResearchSession` 加 `model_id`）
- Modify: `backend/app/domain/research_schemas.py`（`ResearchSessionCreate` 放行 `modelId`；`ResearchSessionRead` 回显）
- Modify: `backend/app/services/research_session_service.py`（`createSession` 加 `modelId` 参数）
- Modify: `backend/app/api/v1/research.py`（`createSession` 解析模型；`_sessionRead` 回显；新增 `_resolveModelId`）
- Modify: `backend/app/tests/integration/test_research_api.py`（追加用例 + 种模型配置的 helper）

**Interfaces:**
- Consumes: `ModelConfigService.list(session, activeOnly=False)`、`MSG_MODEL_CONFIG_UNAVAILABLE`（`app/services/messages_zh.py:231`）、`LlmConfig` ORM（`app/domain/models.py:91-120`）、`encryptApiKey`（测试里已用于数据源种子的同一个函数）。
- Produces:
  - 列 `research_session.model_id INTEGER NULL`
  - `ResearchSessionCreate.modelId: int | None = None`
  - `ResearchSessionRead.modelId: int | None`
  - `ResearchSessionService.createSession(..., modelId: int | None = None)`
  - `_resolveModelId(db, modelId) -> int | None`（research.py 私有）

**背景（D1 裁定：会话级落库）**：研究是**多轮可恢复、会话 state 不落库**的 —— 每个 turn 从 `research_session` 行 + 最新 checkpoint 重建。模型选择若只随创建请求传一次，追问与进程重启后的 resume 就失效（用户会觉得「选了没用」）。这与 `datasource_id` 当初走 0112 加列是同一条理由。

**`extra="forbid"` 耦合**：`ResearchSessionCreate` 禁止未知字段 —— 后端放行 `modelId` 之前，前端擅自传会被 **422**。这就是 W5 必须独立成批、且前后端同批上线的原因（Task 10 的前端改动与 Task 8/9 同批）。

- [ ] **Step 1: 写迁移（文件名 30 字符，≤ 32 合规）**

**先读 `backend/alembic/versions/0112_research_session_datasource.py`，逐字照它的结构与注释风格写**，文件名 `backend/alembic/versions/0113_research_session_model.py`：

```python
"""research_session.model_id：研究会话级 LLM 模型选择（feat-research-entry-ux-fixes W5）

为什么是**列**而不是请求级参数：研究是**多轮可恢复**的 —— 会话 state 不落库，每个 turn
从 research_session 行 + 最新 checkpoint 重建。模型选择若只随创建请求传一次，追问与
resume 后就失效（用户会觉得「选了没用」）。与 datasource_id（0112）同一条理由、同一风格。

无 FK 约束：与 datasource_id 一致 —— 模型配置可被删除 / 停用，但历史会话应保留「当时用
的是哪个模型」这一事实。执行期读到失效模型时**显式报错**，不静默回落（见 W5 设计）。

Revision ID: 0113
Revises: 0112
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0113"
down_revision: str | None = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("research_session", sa.Column("model_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("research_session", "model_id")
```

- [ ] **Step 2: 测试库自动升级 + 写失败测试**

测试库由 `app/tests/_pg_support._ensureSchema` 自动跑 `alembic upgrade head`（用**测试库** URL），**绝不在本地手工执行 `alembic upgrade head`**（它默认指向生产库）。

在 `backend/app/tests/integration/test_research_api.py` 追加：

```python
# ---------------------------------------------------------------------------
# W5：模型选择（会话级落库 + 显式校验）
# ---------------------------------------------------------------------------


async def _seedModel(
    dbSession: AsyncSession, name: str = "api-test-model", *, isActive: bool = True
) -> LlmConfig:
    """种一个模型配置（api_endpoint 指向不可达端口：本文件不真的调 LLM）。"""
    row = LlmConfig(
        model_name=name,
        provider="openai",
        api_endpoint="http://127.0.0.1:9/v1",
        api_key_encrypted=encryptApiKey("test-key"),
        is_active=isActive,
    )
    dbSession.add(row)
    await dbSession.commit()
    await dbSession.refresh(row)
    return row


async def test_create_session_honors_explicit_model(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    model = await _seedModel(dbSession)
    created = await _createSession(client, authHeaders, question="q", modelId=model.id)

    assert created["modelId"] == model.id
    row = await dbSession.get(ResearchSession, uuid.UUID(created["id"]))
    assert row.model_id == model.id


async def test_create_session_defaults_to_auto_routing(
    client: AsyncClient, authHeaders: dict[str, str]
) -> None:
    """不传 modelId ⇒ None（自动路由行为与今天完全一致）。"""
    created = await _createSession(client, authHeaders, question="q")
    assert created["modelId"] is None


async def test_create_session_with_unknown_model_404(
    client: AsyncClient, authHeaders: dict[str, str]
) -> None:
    resp = await client.post(
        f"{_BASE}/sessions", json={"question": "q", "modelId": 999999}, headers=authHeaders
    )
    assert resp.status_code == 404, resp.text
    assert "999999" in resp.text


async def test_create_session_with_inactive_model_404(
    client: AsyncClient, authHeaders: dict[str, str], dbSession: AsyncSession
) -> None:
    """已停用的模型：报错而非静默换模型（否则用户以为用了 A 实际用了 B）。"""
    model = await _seedModel(dbSession, "api-test-inactive-model", isActive=False)
    resp = await client.post(
        f"{_BASE}/sessions", json={"question": "q", "modelId": model.id}, headers=authHeaders
    )
    assert resp.status_code == 404, resp.text
```

（若 `LlmConfig` / `ResearchSession` / `encryptApiKey` 未在文件顶部导入，先补 import。）

- [ ] **Step 3: 跑测试确认失败**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py -q -k model
```

Expected: FAIL —— 现在 `modelId` 是未知字段，`extra="forbid"` 会让请求 422（`assert 422 == 201` 或 `assert 422 == 404`）。

- [ ] **Step 4: 实现**

1) `backend/app/domain/research_models.py` 的 `ResearchSession` 在 `datasource_id` 之后加列（照 `:69` 的写法）：

```python
    # 会话级 LLM 模型选择（W5）：NULL = 自动路由。无 FK（与 datasource_id 同风格）——
    # 模型可被停用/删除，但历史会话要保留「当时用的是哪个模型」的事实。
    model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

2) `backend/app/domain/research_schemas.py`：

`ResearchSessionCreate`（`:54-65`，注意 `extra="forbid"` 保留）加：

```python
    modelId: int | None = None
```

`ResearchSessionRead` 在 `datasourceId` 之后加：

```python
    modelId: int | None = None
```

3) `backend/app/services/research_session_service.py` 的 `createSession` 签名加 `modelId: int | None = None`，并把 `ResearchSession(...)` 构造改为：

```python
            datasource_id=datasourceId,
            model_id=modelId,
```

4) `backend/app/api/v1/research.py`：

import 处把 `MSG_DATASOURCE_NONE_AVAILABLE` 改为同行两个名字：

```python
from app.services.messages_zh import MSG_DATASOURCE_NONE_AVAILABLE, MSG_MODEL_CONFIG_UNAVAILABLE
```

新增 import：

```python
from app.services.model_config_service import ModelConfigService
```

`createSession`（`:250-265`）改为（**先解析模型再落行**，与数据源同一原则：解析失败不留半成品）：

```python
async def createSession(
    payload: ResearchSessionCreate,
    user: CurrentUser = Depends(_requireResearchUser),
    db: AsyncSession = Depends(getDb),
) -> ResearchSessionRead:
    # 先解析数据源与模型再建行：解析失败（无可用源 / id 不存在 / 模型已停用）不留半成品会话
    ds = await _resolveDatasource(db, payload.datasourceId)
    modelId = await _resolveModelId(db, payload.modelId)
    row = await _sessionService.createSession(
        db,
        userId=user.dbUserId,
        question=payload.question,
        mode=payload.mode,
        datasourceId=ds.id,
        modelId=modelId,
    )
    await db.commit()
    return _sessionRead(row)
```

在 `_resolveDatasource` 之后新增：

```python
async def _resolveModelId(db: AsyncSession, modelId: int | None) -> int | None:
    """解析研究会话的 LLM 模型（W5）：显式指定 → 校验存在且启用；缺省 → None（自动路由）。

    查**全量**清单（`activeOnly=False`）而非「可用池」：这样「配置存在但已停用」会报
    404，而不是被池过滤掉后误判为「不存在」—— 与 chat 的消费模式同口径
    （`services/chat_service.py:831-845`）。显式报错、**不静默回落自动路由**。
    """
    if modelId is None:
        return None
    configs = await ModelConfigService().list(db, activeOnly=False)
    target = next((config for config in configs if config.id == modelId), None)
    if target is None or not target.is_active:
        raise NotFoundError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=modelId))
    return target.id
```

`_sessionRead` 加一行：

```python
        datasourceId=row.datasource_id,
        modelId=row.model_id,
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py -q
```

Expected: PASS。**若报「`llm_config` 缺列」**：那是测试库结构漂移，属基础设施问题 —— 记下来报告，**不要**把 `_resolveModelId` 改成绕过 `ModelConfigService`（那会绕开真实校验路径）。

- [ ] **Step 6: 跑 `extra="forbid"` 的既有护栏用例**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py -q -k extra_field
```

Expected: PASS（`test_extra_field_is_422` 用的是别的未知字段，放行 `modelId` 不影响它 —— 若红，说明验的字段就是 `modelId`，需改该用例并说明）。

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/alembic/versions/0113_research_session_model.py backend/app/domain/research_models.py backend/app/domain/research_schemas.py backend/app/services/research_session_service.py backend/app/api/v1/research.py backend/app/tests/integration/test_research_api.py
git commit -m "feat(research): 会话级模型选择（迁移 0113 + modelId 放行/回显 + 显式校验）"
```

---

## Task 9: W5-b 执行期直选（绕过路由，失效显式报错）

**Files:**
- Modify: `backend/app/services/research_agent_ports.py`（新增 `PreferredModelUnavailableError`；`resolveModelConfig` / `resolveClient` 加 `preferredModelId`）
- Modify: `backend/app/services/research_agent_service.py`（`runTurn` / `resumeTurn` 取会话上的 `model_id` 存入 `state["modelId"]`；`_resolveClient` 转发）
- Test: `backend/app/tests/unit/test_research_model_selection.py`（新建）

**Interfaces:**
- Consumes: Task 8 的 `research_session.model_id`；`research_agent_service._loadSession`（`:793-798`）、`rebuildState(row, checkpoint)`（`research_agent_stages.py`）、`_resolveClient`（`research_agent_service.py:743-761`）。
- Produces:
  - `PreferredModelUnavailableError(RuntimeError)`（从 `app.services.research_agent_ports` 导出）
  - `resolveModelConfig(..., preferredModelId: int | None = None)`
  - `resolveClient(..., preferredModelId: int | None = None)`
  - `state["modelId"]` 键（int 或 None）

**背景**：`resolveModelConfig` 现在恒走 `activeOnly=True` 的可用池 + router。选定模型后必须**直选并跳过 router**；而「选定的模型失效了」必须**显式报错**，绝不静默换成别的模型 —— 静默替换正是要修掉的「以为用了 A 实际用了 B」误判。

**为什么抛异常是安全的**：`_guardedRun`（`research_agent_service.py:331-361`）捕获 `Exception` → 发 `research.error{turn_failed}`（**终态**）→ `markFailed` → 重抛。故新增异常**无需改 `ERROR_SPECS`**，自动落到既有的终态失败语义。

- [ ] **Step 1: 写失败测试**

**先看 Task 1 已落地的 `backend/app/tests/unit/test_research_checkpoint_prompt.py` 文件头**：本仓 `app/tests/unit/conftest.py` 有 autouse 的 DB fixtures（`dbSession` / `seedEngine` / `warmBusinessObjectRegistry`），**纯函数单测必须在本文件内用 no-op 覆写它们**，否则每个用例都会走 `seedEngine._truncateAll` 清空整个测试库（既慢又抹掉集成测试依赖的迁移种子）。照抄 Task 1 那段覆写（`dbSession`→None、`seedEngine`→None、`warmBusinessObjectRegistry`→no-op），这是本仓既有约定（另见 `test_prior_cte_contract.py` / `test_kpi_catalog_api.py`）。

新建 `backend/app/tests/unit/test_research_model_selection.py`：

```python
"""执行期模型直选单测（feat-research-entry-ux-fixes W5-b）。

纯单测、零 DB：`resolveModelConfig` 需要的 session 只用于 `begin_nested()` 与
`buildRoutingContext`（后者在 tokenUsage=None 时提前返回，不碰 DB）——
故用两个极简替身即可覆盖全部分支。

契约要点（对应用户反馈第 1 条）：
- 指定模型 ⇒ **直选**，不进路由器；
- 指定模型按**全量**清单找（存在但停用 ⇒ 报错，而不是被可用池过滤后误判为不存在）；
- 指定模型 key 缺失 / 不存在 ⇒ **显式报错，绝不静默换模型**；
- 未指定 ⇒ 现有自动路由行为逐字不变。
"""

from typing import Any

import pytest

from app.services.research_agent_ports import (
    PreferredModelUnavailableError,
    resolveModelConfig,
)


class _FakeConfig:
    def __init__(self, configId: int, *, isActive: bool = True, hasKey: bool = True) -> None:
        self.id = configId
        self.is_active = isActive
        self._hasKey = hasKey


class _FakeConfigs:
    """按 activeOnly 返回不同清单（显式选择走全量，自动路由走可用池）。"""

    def __init__(self, configs: list[_FakeConfig]) -> None:
        self._configs = configs
        self.calls: list[bool] = []

    async def list(self, session: Any, *, activeOnly: bool = False) -> list[_FakeConfig]:
        self.calls.append(activeOnly)
        if activeOnly:
            return [config for config in self._configs if config.is_active]
        return list(self._configs)


def _factory(config: Any) -> Any:
    """`buildClient` 的替身：有 key 的配置返回哨兵客户端，无 key 返回 None。"""
    return object() if getattr(config, "_hasKey", False) else None


class _FakeRouter:
    def __init__(self) -> None:
        self.calls = 0

    def selectModel(self, configs: list[Any], prompt: str, ctx: Any) -> Any:
        self.calls += 1
        return configs[0]


class _FakeNested:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeSession:
    """`resolveModelConfig` 用 `session.begin_nested()` 隔离配置读失败；单测无需真库。"""

    def begin_nested(self) -> _FakeNested:
        return _FakeNested()


async def test_preferred_model_is_selected_directly_without_router() -> None:
    configs = _FakeConfigs([_FakeConfig(1), _FakeConfig(2)])
    router = _FakeRouter()

    picked = await resolveModelConfig(
        configs, router, _factory, _FakeSession(),
        question="q", sessionId="s", preferredModelId=2,
    )

    assert picked.id == 2
    assert router.calls == 0


async def test_preferred_model_reads_full_list_not_usable_pool() -> None:
    """存在但已停用 ⇒ 报错，而不是被「可用池」过滤后误判为不存在。"""
    configs = _FakeConfigs([_FakeConfig(1, isActive=False)])

    with pytest.raises(PreferredModelUnavailableError):
        await resolveModelConfig(
            configs, _FakeRouter(), _factory, _FakeSession(),
            question="q", sessionId="s", preferredModelId=1,
        )

    assert configs.calls == [False]


async def test_preferred_model_missing_raises() -> None:
    with pytest.raises(PreferredModelUnavailableError):
        await resolveModelConfig(
            _FakeConfigs([_FakeConfig(1)]), _FakeRouter(), _factory, _FakeSession(),
            question="q", sessionId="s", preferredModelId=99,
        )


async def test_preferred_model_without_key_raises_instead_of_substituting() -> None:
    """选中的模型无 API key：显式报错，绝不静默换模型。"""
    with pytest.raises(PreferredModelUnavailableError):
        await resolveModelConfig(
            _FakeConfigs([_FakeConfig(1, hasKey=False)]), _FakeRouter(), _factory,
            _FakeSession(), question="q", sessionId="s", preferredModelId=1,
        )


async def test_no_preference_still_uses_router() -> None:
    """未指定 ⇒ 自动路由行为逐字不变。"""
    configs = _FakeConfigs([_FakeConfig(1), _FakeConfig(2)])
    router = _FakeRouter()

    picked = await resolveModelConfig(
        configs, router, _factory, _FakeSession(), question="q", sessionId="s"
    )

    assert picked.id == 1
    assert router.calls == 1
    assert configs.calls == [True]


async def test_no_preference_and_no_usable_config_returns_none() -> None:
    configs = _FakeConfigs([_FakeConfig(1, hasKey=False)])

    assert (
        await resolveModelConfig(
            configs, _FakeRouter(), _factory, _FakeSession(), question="q", sessionId="s"
        )
        is None
    )
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest app/tests/unit/test_research_model_selection.py -q
```

Expected: FAIL —— `ImportError: cannot import name 'PreferredModelUnavailableError'`。

- [ ] **Step 3: 实现 ports 侧**

`backend/app/services/research_agent_ports.py` —— 在 `resolveModelConfig` **之前**新增异常类：

```python
class PreferredModelUnavailableError(RuntimeError):
    """会话显式选定的模型不可用（不存在 / 已停用 / key 缺失）。

    **绝不静默回落自动路由**：静默替换成别的模型正是要修掉的「以为用了 A 实际用了 B」
    误判（用户反馈第 1 条）。本异常由 `_guardedRun` 捕获 → 发
    `research.error{turn_failed}`（终态）→ markFailed ⇒ 无需新增 error code。
    """
```

`resolveModelConfig` 签名加参数（放在 `tokenUsage` 之后）：

```python
    tokenUsage: Any = None,
    preferredModelId: int | None = None,
```

**在 `if modelConfigs is None: return None` 之后、既有 `activeOnly=True` 读配置之前**插入直选分支：

```python
    if preferredModelId is not None:
        # 显式直选：读**全量**清单（activeOnly=False），不走 router —— 用户选了哪个就用哪个。
        try:
            async with session.begin_nested():
                configs = await modelConfigs.list(session, activeOnly=False)
        except Exception:  # noqa: BLE001 —— 配置读取失败：显式报错（不能假装选中了）
            logger.warning(
                "读取模型配置失败（会话已指定模型）: session=%s", sessionId, exc_info=True
            )
            raise PreferredModelUnavailableError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=preferredModelId))
        target = next(
            (config for config in configs if config.id == preferredModelId), None
        )
        if target is None or not target.is_active:
            raise PreferredModelUnavailableError(
                MSG_MODEL_CONFIG_UNAVAILABLE.format(id=preferredModelId)
            )
        if buildClient(factory, target) is None:
            # 配置在、但 key 缺失 / 客户端建不起来：同样显式报错，不换模型。
            raise PreferredModelUnavailableError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=preferredModelId))
        return target
```

import 补 `from app.services.messages_zh import MSG_MODEL_CONFIG_UNAVAILABLE`（**先确认该模块是否已被本文件 import** —— 本文件已用 `LLM_UNAVAILABLE_MESSAGE`，若那是本地常量而非从 `messages_zh` 引入，就新增这一行 import；不得重复定义同名消息）。

`resolveClient` 签名加 `preferredModelId: int | None = None`，并在调用 `resolveModelConfig` 时透传：

```python
    config = await resolveModelConfig(
        modelConfigs,
        modelRouter,
        factory,
        session,
        question=str(state.get("question") or ""),
        sessionId=sessionId,
        tokenUsage=tokenUsage,
        preferredModelId=preferredModelId,
    )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && uv run pytest app/tests/unit/test_research_model_selection.py -q
```

Expected: PASS（6 passed）。

- [ ] **Step 5: 把会话上的 `model_id` 接进执行期**

**行数红线（Task 1 实测）**：`backend/app/services/research_agent_service.py` 现在是 **799 行**，距项目 800 行上限只剩 1 行，而本步要往里加行。**动手前先 `wc -l` 确认**；若加上本步改动会超 800，**先把等价的纯函数/无状态构件抽到 `research_agent_stages.py`**（该文件是本模块既定的抽出目标，依赖方向单向：service → stages），再回填本步。不要为了塞下改动去改 `alembic`/结构或压缩可读性。

`backend/app/services/research_agent_service.py`：

1) `runTurn`（`:227-256`）当前**不调 `_loadSession`**，需在构造 `state` 前加载会话行并带上 `modelId`。在 `state` 字面量里加：

```python
            "modelId": row.model_id if row is not None else None,
```

并在构造 `state` 之前加载（若函数里已有 session 行变量则复用它，不要重复查）：

```python
        # 会话级模型选择（W5）：每轮从会话行取，保证追问也沿用同一模型。
        row = await self._loadSession(session, sessionId)
```

2) `resumeTurn`（`:258-305`）：在 `:279` 的 `_loadSession` 之后、`:283` 的 `rebuildState` 之后加：

```python
        state["modelId"] = row.model_id
```

3) `_resolveClient`（`:743-761`）转发（读 `state` 而非参数，保持调用点不变）：

```python
            preferredModelId=state.get("modelId"),
```

**注意**：`rebuildState` **不**携带 modelId（它是落库状态的重建，模型不在 checkpoint 里）—— 这正是必须在上面的调用点显式赋值的原因；不要试图改 `rebuildState` 的契约。

- [ ] **Step 6: 跑研究相关套件确认零回归**

```bash
cd backend && uv run pytest app/tests/unit -q -k research
```

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py app/tests/integration/test_research_stream.py -q
```

Expected: PASS（未指定模型的既有用例走 `preferredModelId=None` 分支，行为逐字不变）。

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/services/research_agent_ports.py backend/app/services/research_agent_service.py backend/app/tests/unit/test_research_model_selection.py
git commit -m "feat(research): 执行期按会话直选模型；失效显式报错不静默替换"
```

---

## Task 10: W5-c 前端模型选择（默认「自动」）

**Files:**
- Modify: `frontend/src/types/research.ts`（`ResearchSession` 加 `modelId`）
- Modify: `frontend/src/api/research.ts`（`createResearchSession` 支持 `modelId`）
- Modify: `frontend/src/pages/research/ResearchListPage.tsx`（新建表单加模型 Select）
- Modify: `frontend/src/i18n/zh-CN.ts` / `en-US.ts`
- Test: `frontend/src/tests/ResearchListPage.test.tsx`（追加用例）

**Interfaces:**
- Consumes: `api/modelConfig.ts:listModels(activeOnly)`、`types/modelConfig.ts:ModelConfig.{id, modelName, isActive}`、Task 8 的后端 `modelId` 放行、Task 6 的 `mockGet`（需要扩展 `/models` 分发）。
- Produces: 无导出新增。

**背景**：默认「自动」（`null`）—— **不选 = 现有自动路由行为逐字不变**。默认项用 `null` 表示，且**只有非 null 才把键放进请求体**（条件展开），以保持「不选」时请求体与今天完全一致。

- [ ] **Step 1: 写失败测试**

`frontend/src/tests/ResearchListPage.test.tsx`（新用例用到 `userEvent`；若该文件尚未导入则补 `import userEvent from "@testing-library/user-event";`）：

1) `mockGet` 扩展 `/models` 分发：

```tsx
const MODELS = [
  { id: 1, modelName: "deepseek-chat", isActive: true },
  { id: 2, modelName: "minimax-m1", isActive: true },
];

function mockGet(
  overrides: { sessions?: unknown; datasources?: unknown; models?: unknown } = {},
) {
  httpMock.get.mockImplementation((url: string) => {
    if (url === "/datasources") {
      return Promise.resolve({ data: overrides.datasources ?? DATASOURCES });
    }
    if (url === "/models") {
      return Promise.resolve({ data: overrides.models ?? MODELS });
    }
    return Promise.resolve({ data: overrides.sessions ?? SESSIONS });
  });
}
```

2) 既有 POST 断言追加 `modelId` 缺省不出现 —— 保持为：

```tsx
      expect(httpMock.post).toHaveBeenCalledWith("/research/sessions", {
        question: "供应商 360° 全景",
        mode: "research",
        datasourceId: 1,
      }),
```

（**不传 `modelId` 时请求体里不应有这个键** —— 这条断言就是它的护栏。）

3) describe 内追加：

```tsx
  it("模型下拉默认「自动」；不选时请求体不带 modelId 键", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() =>
      expect(httpMock.get).toHaveBeenCalledWith("/models", { params: { activeOnly: true } }),
    );

    expect(await screen.findByText("自动（智能路由）")).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("输入你的研究问题…"), "供应商 360° 全景");
    await user.click(screen.getByRole("button", { name: "开始研究" }));

    await waitFor(() => {
      const [, body] = httpMock.post.mock.calls[0];
      expect(body).not.toHaveProperty("modelId");
    });
  });

  it("选定模型后请求体带 modelId", async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());

    await user.click(screen.getByText("自动（智能路由）"));
    await user.click(await screen.findByText("minimax-m1"));

    await user.type(screen.getByPlaceholderText("输入你的研究问题…"), "供应商 360° 全景");
    await user.click(screen.getByRole("button", { name: "开始研究" }));

    await waitFor(() =>
      expect(httpMock.post).toHaveBeenCalledWith("/research/sessions", {
        question: "供应商 360° 全景",
        mode: "research",
        datasourceId: 1,
        modelId: 2,
      }),
    );
  });
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchListPage.test.tsx
```

Expected: 新增 2 个用例 FAIL（找不到「自动（智能路由）」）。

- [ ] **Step 3: 补 i18n**

`zh-CN.ts` 的 `research.list` 块追加：

```ts
      modelPlaceholder: "选择模型",
      modelAuto: "自动（智能路由）",
```

`en-US.ts` 同位置：

```ts
      modelPlaceholder: "Select a model",
      modelAuto: "Auto (smart routing)",
```

- [ ] **Step 4: 实现**

`frontend/src/types/research.ts` 的 `ResearchSession` 加：

```ts
  modelId?: number | null;
```

`frontend/src/api/research.ts` 的 `createResearchSession` 输入类型加 `modelId?: number | null;`。

`frontend/src/pages/research/ResearchListPage.tsx`：

1) import：

```tsx
import { listModels } from "../../api/modelConfig";
import type { ModelConfig } from "../../types/modelConfig";
```

2) 状态 + 加载（与数据源同一模式；失败回落空清单 —— 模型下拉是可选增强，不阻断建会话）：

```tsx
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [modelId, setModelId] = useState<number | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const list = await listModels(true);
        if (Array.isArray(list)) setModels(list);
      } catch {
        // 展示层降级：清单缺失时下拉为空，用户仍可「自动」建会话。
      }
    })();
  }, []);
```

3) `startResearch` 的请求体（**条件展开：不选就不放键**，保证「自动」与今天逐字一致）：

```tsx
      const session = await createResearchSession({
        question: trimmed,
        mode,
        datasourceId,
        ...(modelId === null ? {} : { modelId }),
      });
```

4) 数据源 Select 之后插入模型 Select：

```tsx
            <Select<number>
              value={modelId ?? undefined}
              onChange={(value) => setModelId(value ?? null)}
              allowClear
              placeholder={t("research.list.modelPlaceholder")}
              options={models.map((model) => ({ value: model.id, label: model.modelName }))}
            />
```

（`allowClear` 让用户能退回「自动」= `undefined` ⇒ `setModelId(null)` ⇒ 请求体不带键。占位符文案在 `modelId === null` 时显示，这正是「自动」的可见表达 —— 与测试断言 `getByText("自动（智能路由）")` 对应：**把 `modelAuto` 用起来**，见下一步。）

5) 让「自动」在界面可见（否则用户不知道不选会发生什么）：把占位符替换为显式的「自动」项 —— 在下拉选项首位插入一个 `value` 为 `0` 的哨兵项，并把 `0` 视作「自动」：

```tsx
            <Select<number>
              value={modelId ?? 0}
              onChange={(value) => setModelId(value === 0 ? null : value)}
              placeholder={t("research.list.modelPlaceholder")}
              options={[
                { value: 0, label: t("research.list.modelAuto") },
                ...models.map((model) => ({ value: model.id, label: model.modelName })),
              ]}
            />
```

**为什么用 `0` 哨兵**：antd `Select` 的 `value` 为 `undefined` 时显示 placeholder 而非某个选项 —— 用户就看不出「默认是自动」。用 `0`（真实模型 id 从 1 起，`0` 不可能冲突）作为「自动」的显式选项，语义与外观都到位。请求体仍靠 `modelId === null` 判断是否带键（`0` 永不进请求体）。

`testing` 断言因此改为点击「自动（智能路由）」→ 选 `minimax-m1`（Step 1 的用例已如此写）。

- [ ] **Step 5: 跑测试确认通过**

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchListPage.test.tsx
```

Expected: PASS。

- [ ] **Step 6: 前端类型检查**

```bash
cd frontend && npx tsc --noEmit
```

Expected: 无错误。

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add frontend/src/types/research.ts frontend/src/api/research.ts frontend/src/pages/research/ResearchListPage.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/tests/ResearchListPage.test.tsx
git commit -m "feat(research): 新建会话可选模型（默认自动，不选时请求体不带 modelId）"
```

---

## Task 11: 批二验收 + Harness SSOT 收尾

**Files:**
- Modify: `Harness/changes/feat-research-entry-ux-fixes/summary.md`（§6 / §7 / §8 + 状态）
- Modify: `Harness/wiki/frontend.md`（新增研究入口章节）
- Modify: `docs/superpowers/specs/2026-10-05-research-entry-ux-fixes-design.md`（若实施中与设计有偏差，回填）

**Interfaces:**
- Consumes: Task 1–10 的全部产出与 Task 7 记录的验收结果。
- Produces: 无代码产出。

- [ ] **Step 1: 后端全量研究相关套件 + 单测**

**先 `ls app/tests/integration | grep research` 核对实际文件名**，把存在的 research 集成文件一并纳入（不要照抄不存在的路径）：

```bash
cd backend && ls app/tests/integration | grep -i research
```

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py app/tests/integration/test_research_stream.py -q
```

```bash
cd backend && uv run pytest app/tests/unit -q -k research
```

Expected: PASS。

- [ ] **Step 2: 覆盖率（改动模块 ≥ 80%）**

```bash
cd backend && TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/qa_metadata_test' TEST_NEO4J_URI='bolt://localhost:7688' uv run pytest app/tests/integration/test_research_api.py app/tests/integration/test_research_stream.py --cov=app/api/v1/research --cov=app/services/research_session_service --cov=app/services/research_agent_ports --cov=app/services/research_agent_stages --cov-report=term-missing -q
```

Expected: 四个模块均 ≥ 80%。**低于 80% 就补测试**，不要调低门槛。

```bash
cd frontend && npx vitest run --testTimeout=30000 src/tests/ResearchListPage.test.tsx src/tests/ResearchCheckpointCard.test.tsx src/tests/ResearchSessionPage.test.tsx --coverage
```

Expected: 通过 vitest.config.ts 里已配置的 80% thresholds。

- [ ] **Step 3: 安全审查（Harness 强制 §7）**

触发条件**已成立**：本变更新增 `DELETE` 端点（Task 4）并引入用户输入字段 `modelId`（Task 8）。派 `security-reviewer` agent，审查范围：

- `backend/app/api/v1/research.py` 的 `deleteSession` / `_resolveModelId` / `createSession`
- `backend/app/services/research_session_service.py` 的 `deleteSession`
- `backend/app/services/research_agent_ports.py` 的直选分支

**必查项（写进给 reviewer 的指令里）：**
1. `DELETE /sessions/{sessionId}` 是否**只**能删本人的会话（越权 → 404，不泄露存在性）；有没有绕过 `_ownedSession` 的路径。
2. 删除是否**不可逆**且确实级联（是否可能留下孤儿子行 → 数据残留）。
3. `modelId` 是否被用于任何拼接/注入点（应只做 `int` 比较，绝不进 SQL 字符串）。
4. `_resolveModelId` 是否会因「查全量清单」而泄露他人/停用模型的信息（预期：仅回显数字 id，不返回模型详情 —— 核对响应体）。
5. 错误信息是否泄露敏感数据（如模型 api_key / 内部路径）。

把 reviewer 的结论（至少 1 项，含等级）**逐字**填进 Harness `§7`。

- [ ] **Step 4: 部署验证（Harness 强制 §8）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
docker compose build --no-cache frontend
docker compose up -d
docker compose exec backend alembic current
```

Expected: `alembic current` 显示 `0113`（迁移由容器启动自动执行；**不手工 upgrade**）。

端点核对：

```bash
curl -s http://localhost:8000/openapi.json | python3 -c "import json,sys; p=json.load(sys.stdin)['paths']; print([k for k in p if 'research' in k])"
```

Expected: 列表里含 `/api/v1/research/sessions/{sessionId}` 且 methods 含 `delete`。

真机链路（批二）：
1. 新建会话：模型下拉默认「自动（智能路由）」；选 `minimax-m1` 建会话 → 列表/会话页正常。
2. 后续追问：仍用所选模型（容器日志里该轮的模型名与所选一致）。
3. 重启 backend 容器后打开该会话并追问：模型仍是所选的那个（验证落库，不是进程内存）。
4. 把所选模型在模型配置页停用，再对旧会话追问 → **明确的终态错误提示**，不是静默换模型出结果。

```bash
docker compose logs --tail=80 backend
```

- [ ] **Step 5: 填 Harness SSOT**

`Harness/changes/feat-research-entry-ux-fixes/summary.md`：

- §6 测试：填 **Step 1 / Step 2 的实际输出**（通过数、覆盖率数字）。禁止写「全绿」而不给数字。
- §7 安全审查：填 Step 3 的 reviewer 结论（含等级）。
- §8 部署验证：填 Step 4 的实测结果（`alembic current` 输出、openapi 核对、4 条真机链路结论）。
- 头部的 `**状态**：draft` 改为 `**状态**：in-review`；同时删除「阶段说明」里「§6/§7/§8 须在实现完成后填齐」的待办措辞（改为已填齐的陈述）。`draft` 状态**不允许合并** —— 本步是合并的前置条件。

- [ ] **Step 6: 补 Wiki 章节**

`Harness/wiki/frontend.md` 新增一节（放在前端页面/组件相关章节之后）：

```markdown
## 研究型 Agent 入口（research）

- 页面：`pages/research/ResearchListPage.tsx`（列表 + 新建）、`ResearchSessionPage.tsx`（会话 + 检查点）、`ResearchReportPage.tsx`（报告）。
- 新建表单三要素：研究问题、**数据源**（默认选 `isDefault` 源）、**模型**（默认「自动（智能路由）」，`0` 哨兵项映射为 `null`，请求体不带 `modelId` 键）、模式（每项带副描述 + 页面一句免责说明：三者共用同一条流水线，只改报告章节组织）。
- `CheckpointCard`：按 `phase` 渲染「本次针对」目标行 + 相位明细（`runtime_dynamic` → `options.conflicts`；`planning` → `options.plan.steps`；`hypothesis` → `options.candidates` 可勾选，勾选结果按**数组下标**经 `choice.selectedIndexes` 提交）。空明细必给显式空态文案（降级路径下候选本就是 `[]`）。
- **键名坑**：`options.plan.steps[].sub_question` 是 snake_case（后端 `normalizePlan`），`options.stepResults[].subQuestion` 是 camelCase；历史 checkpoint 已落库 ⇒ 前端**双键回落读取**，绝不改后端键名。
- 调用点必须给 `<CheckpointCard key={checkpoint.id} …>`：组件内有 `useState`（勾选/草稿），换检查点时不重挂载就会串味。
- 删除：`DELETE /api/v1/research/sessions/{id}`（硬删 + DB CASCADE + 归属不符 404）；前端 Popconfirm 二次确认，**服务端确认后**才从列表移除。
- 数据源名映射：`hooks/useDatasourceOptions.ts`（失败回落空清单 —— 仅展示层降级，不阻断页面）。
```

- [ ] **Step 7: 更新 memory**

更新 `~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-research-entry-ux-gaps.md`：把「待办」性质的描述改为「已落地」，并补一句实施期新发现（`CheckpointCard` 缺 `key` 导致状态串味；`modelId` 用 `0` 哨兵表达「自动」以避开 antd `value=undefined` 只显示 placeholder 的问题）。`MEMORY.md` 的一行索引若描述已不准，同步改。

- [ ] **Step 8: 提交 SSOT**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add Harness/changes/feat-research-entry-ux-fixes/summary.md Harness/wiki/frontend.md docs/superpowers/specs/2026-10-05-research-entry-ux-fixes-design.md
git commit -m "docs(research): 研究入口体验修复 SSOT 收口（§6 测试/§7 安全/§8 部署 + wiki 章节）"
```

---

# 交付边界与依赖

| 依赖 | 说明 |
|---|---|
| Task 2 → Task 1 | 无代码依赖，但二者改同一份 `options` 数据的**两端**（后端问句文案 / 前端明细渲染）；先做 Task 1 让 `prompt` 先带上对象，Task 2 的卡片才有完整可读性 |
| Task 5 → Task 4 | 前端删除按钮依赖后端端点先存在 |
| Task 10 → Task 8 | `extra="forbid"`：前端传 `modelId` 前，后端必须已放行（**同批上线**，故二者同属批二） |
| Task 9 → Task 8 | 执行期直选读的是 Task 8 加的列 |
| Task 6 / Task 10 | 都改 `ResearchListPage.tsx` 的新建表单与 `mockGet`；**串行执行**（Task 6 先），不要并行 |
| Task 11 → Task 1–10 | 收尾依赖全部产出与 Task 7 的实测记录 |

# 明确不做（YAGNI）

- 不把研究列表改造成 chat 那种右侧可折叠历史面板（用户表述是「在现有基础上加删除」）。
- 不让 `mode` 影响检索 / 计划 / 执行 / 假设（D2 裁定：只加说明）。
- 不统一后端 `sub_question` / `subQuestion` 键名（会让已落库的历史 checkpoint 形态不一致）。
- 不引入软删除（D4 裁定：与 chat 一致走硬删）。
- 不做多用户协同 / ACL；不做 ESL 后置 LLM 精化层。
- 不新增 `research.error` 的 error code（选定模型失效走既有 `turn_failed` 终态路径）。
