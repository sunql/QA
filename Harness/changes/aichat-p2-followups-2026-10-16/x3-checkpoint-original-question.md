# X3 — 检查点恢复保留本轮研究问题 ✅ PR #39

## 范围

承接 1012 cross-refs spec §P0-X3。
研究检查点恢复时保留本轮研究问题（checkpoint 自带 original_question，
恢复阶段不再 join turn.content）。

## 落地

**Worktree**：`/private/tmp/qa-fix-x3-checkpoint`
**Branch**：`fix/qa-1012-p0-x3`（base=main tip `ec0570b0`）
**Commit**：`27c091c2` fix(research): checkpoint 恢复保留本轮研究问题 (X3/1012-P0)
**PR**：**#39** https://github.com/sunql/QA/pull/39

## 问题域

旧实现：checkpoint 只挂 turn_id，恢复时 join `turn.content["question"]` 取
`originalQuestion`。

隐患：
1. `turn.content` 可能在 checkpoint 创建后被修改（agent 续写、追写清理）。
2. 序列化丢失 — m2.7 chat 透传 JSON 时偶发丢键，turn.content 落库时
   已被剥离。
3. 跨会话污染 — 同 `sessionId` 复用 turn 时（resume 链路），错误
   turn.content 会被拼接到新一轮 checkpoint。

恢复时拿到错的 question 或空字符串 → 状态机以错误问题续跑
（plan_explicit / hypothesis 用错题生成下游）。

## 修法

- **alembic 0128** `0128_research_checkpoint_original_question.py`：
  `op.add_column` String(2000) `nullable=False server_default=""`。
  - server_default 兜底：旧 checkpoint 行反查落空串，行为不破坏既有数据。
  - 长度 2000 与多步计划的 `original_question` 上限对齐。
- **ORM** `ResearchCheckpoint.original_question: Mapped[str]`
- **DTO** `ResearchCheckpointRead.originalQuestion: str = ""`
  - 顶层字段，恢复层直接读，不再 join turn.content。
- **服务层** `openCheckpoint` 增加 `originalQuestion: str = ""` 形参
  （默认空串保持向后兼容），在 `row.original_question` 落库。
- **API 读** `_checkpointRead` 直接 `row.original_question or ""` 暴露。

## 验证

- `app/tests/unit/test_research_models.py::test_checkpoint_columns`：
  断言 `original_question` 在列集合中（**守护性** — 防后续 schema 漂移
  偷偷删列）。
- `app/tests/integration/test_research_session_service.py::test_openCheckpoint_persistsOriginalQuestion`：
  - **正向断言**：传 `originalQuestion="对比 2024 和 2025 年销售额"` 后
    `cp.original_question` 等于该字符串，DB 真值断言。
  - **DTO 序列化断言**（review Minor #2）：`_checkpointRead(cp)` 后
    `dto.originalQuestion == "对比 ..."`，防 CamelModel alias_generator 漂移。
  - **反向断言**：不传时 `cp2.original_question == ""`（向后兼容，
    旧调用方不破坏）；DTO 也为空串。
- `app/tests/integration/test_research_agent_service.py::test_pause_persists_original_question_from_agent_state`
  （**review Critical 回归**）：用 `makeService` 跑真 ResearchSessionService，
  断言 `cp.original_question == QUESTION`。防退化 — 后续若 `_pauseForUser`
  又被改回去不传，本测试立刻 RED。
- 17 个 X3 相关 + 2 个新/增强用例绿；research_agent_service 整 58 个测试绿。
- `alembic upgrade head` 在生产 + 测试库 5434 均已跑过，列真存在
  `is_nullable=NO column_default=''::character varying`。

## Review 闭环（commit 568b5e24）

PR #39 派 reviewer subagent 跑 1 Critical + 2 Minor + 1 Informational。

### Critical ✅ 已修

- **Bug**：`ResearchAgentService._pauseForUser`（backend/app/services/research_agent_service.py:602）
  是 `openCheckpoint` 的**唯一**生产调用方，原 X3 commit 27c091c2 加了
  `originalQuestion` 形参但调用方没传 — pause tuple `(phase, options, prompt)`
  不含 question。原问题永远落空串，X3 防御被绕过。
- **修法**：`_pauseForUser` 加 `state: dict[str, Any] | None = None` 形参，
  `_runFrom` 调用点传 state，读 `state['question']` 作为 originalQuestion。

### Minor ✅ 已修

- **DTO 序列化断言**：原 `test_openCheckpoint_persistsOriginalQuestion` 只
  断言 ORM 列值，没走 `ResearchCheckpointRead` 序列化。补 `_checkpointRead(cp)`
  调用 + 断言 `dto.originalQuestion == "..."`，防 CamelModel alias_generator
  漂移让前端读不到字段。
- **2000 字符故意截断**（research_models.py:41）：原注释只说"与多步计划
  对齐"，没说截断是设计意图。扩写为「故意截断不抛错」「不要改成 Text 类型」
  + 「改这里前先评估多步计划侧」。

### Informational（无需修）

- alembic chain 顺序 0127→0128 正确；downgrade 对称；DTO 映射靠 CamelModel
  alias_generator=to_camel；`or ""` 防御性（nullable=False + server_default=""
  永不为 NULL）。

## 反向断言 / 防退化

- `test_checkpoint_columns` 列集合断言 — 若有人重构 `ResearchCheckpoint`
  漏掉 `original_question`，RED 立刻触发。
- `test_openCheckpoint_persistsOriginalQuestion` 不传兼容断言 — 防止
  重构时把 `originalQuestion: str = ""` 改成必填而破坏旧调用方。
- DTO 顶层字段 `originalQuestion: str = ""` 默认空串 — 防字段被设为
  None 时下游 Pydantic 序列化炸。

## 副作用 / 兼容性

- 旧 checkpoint 行 `original_question` 默认为 `''`（server_default）。
- 旧 `openCheckpoint` 调用方（不传 `originalQuestion`）— 仍工作，落空串。
- 恢复层（research.py API）继续暴露 `originalQuestion` 字段，**前端契约
  不变**（已有字段是 `prompt` / `userChoice` 等，新增字段默认不消费，
  显式读取才是新读法）。

## 阻塞 / 后续

- X4 流式 CRLF + EOF 守门（chat 已修，research 未修）— 序 3 续
- X5 会话归属 + 状态互斥 — 序 3 续
- 恢复层（resume / continue after answer）实际切换到读 `originalQuestion` 顶层
  字段 — 后续 X5 落地时一起做（属于"恢复阶段"接续改动）。

## 链接

- Spec：`docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md` §P0-X3
- 母 SSOT：`Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md` 序 3
- 平行项：X1（PR #37）/ X2（PR #38）已合
