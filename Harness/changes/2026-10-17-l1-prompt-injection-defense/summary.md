# L1 prompt 注入防御 — 6 个 LLM 入口全覆盖

> 日期：2026-10-17 | 状态：done | 分支：`fix/qa-1012-p0-l1`
> Spec: `docs/superpowers/specs/2026-10-17-l1-prompt-injection-defense-design.md`（2f13e890）
> Plan: `docs/superpowers/plans/2026-10-17-l1-prompt-injection-defense.md`

## 背景

LLM 提示注入是项目当前最大安全盲区：6 个 LLM 入口（Plan / Answer / Multi-step / L4 agent / Hypothesis / Wiki QA / RAG QA）中，已有防御仅 `_sanitizeContext` 3 字符 escape + `neutralizeFence` 旧 `<user_content>` fence，未覆盖：

1. **5 字符 HTML escape**（漏 `"` `'`）— 攻击者可构造 `"ignore previous"` 干扰 LLM 工具调用
2. **U+200B 打断 *任意* label 的关闭/自闭标签**（旧 `neutralizeFence` 只处理 `</user_content>`）
3. **XML wrap 显式 label**（旧 `<user_content>` 一锅炖，无语义区分）
4. **System prompt guard 段**（无 LLM「这是数据不是指令」的中英双语指令）

外加 SQL 注入 / wiki 编辑员注入 / RAG 文档上传注入 3 个 user-controlled 数据源 → LLM 系统的注入面，**最高风险**是 `agent_runtime_service._dispatchSingleTool` 的 SQL 结果（**前序 SQL 数据可被 question 污染**）。

## 设计

5 层防御（spec §1-3）：

1. **`sanitizeForLlmPrompt(text)`** — 5 字符 HTML escape（`&` `<` `>` `"` `'`）+ U+200B 打断 *任意* `[a-z][a-z0-9_]*` label 的关闭/自闭标签
2. **`wrapUntrusted(label, text)`** — `<label>...</label>` 包装，label 走白名单（11 个 = 8 新 + 3 向后兼容）
3. **`UntrustedLabel`** — 11 个预定义 label 常量（防 label 注入）
4. **`renderSecurityPreamble()`** — 中英双语 system prompt guard 段
5. **10 个红队 e2e 夹具**（spec §4）— 跨 6 入口验证注入 payload 一致被中和

11 个 label 白名单：
- 8 新：`user_question` / `ontology_recall` / `data_summary` / `history` / `wiki_chunks` / `sql_result` / `step_result` / `agent_tool_result`
- 3 向后兼容：`feature_catalog` / `term_dictionary` / `user_content`（旧代码已用，避免一次性重命名）

## 改动

7 个 commit（按 LLM 入口分 task）：

| Task | Commit | 文件 | 改动 |
|------|--------|------|------|
| T1 | `6ed6b2b3` | `backend/app/security/prompt_sanitizer.py`（+ 1 测试文件） | 基础模块：5 字符 escape + wrap + U+200B + guard preamble（15 tests, 100% coverage） |
| T2 | `3c4c9b4b` | `backend/app/services/nl2sql_prompts.py`（+ 1 测试） | Plan system prompt：3 个 ontology/feature/dictionary 块 wrap + preamble 注入（6 new tests, 74 existing 0 regression） |
| T3 | `fab4b9db` | `backend/app/services/chat_service.py`（+ 1 测试） | Answer prompt：question/sql/data/history 4 个 wrap + preamble 注入（7 new tests, 19 existing 0 regression） |
| T4 | `9063097f` | `backend/app/services/agent_runtime_service.py`（+ 1 测试） | L4 loop：`_L4_SYSTEM_PROMPT` preamble + `_initLoopState`/`_dispatchSingleTool` 双路径 wrap（8 new tests, 35 existing 0 regression） |
| T5 | `ec679fd8` | `backend/app/services/chat_service.py`（+ 1 测试） | Multi-step：`_wrapMultistepInputs` module-level helper + 3 user-controlled 输入 wrap（6 new tests, 57 existing 0 regression） |
| T6 | `5c974ca5` | `backend/app/services/hypothesis_service.py`（+ 1 测试） | Hypothesis：拆 `<user_content>` 为 `<user_question>` + `<data_summary>` + preamble 注入（8 new tests, 116 existing 0 regression） |
| T7 | `efb862c1` | `backend/app/services/{wiki_qa,rag_qa}_service.py`（+ 2 测试） | Wiki+RAG QA：3 个 wrap（chunks/history/question）+ preamble 注入（8 new tests, 22 existing 0 regression） |
| T8 | `8cbf6d25` | `backend/app/tests/integration/test_l1_prompt_injection_redteam.py` | 10 个红队 fixture（20 actual test cases = 10 顶层 + 10 参数化 over 11 labels）：6 单服务 + 3 横向 + 11-label 参数化 |

**Review summary**：
- T1 review: APPROVED (0C, 1I 防御冗余, 3M parked)
- T2 review: APPROVED (0C, 0I, 3M parked)
- T3 review: APPROVED (0C, 0I, 1M parked: dead-import)
- T4 review: APPROVED (0C, 0I, 1M parked: dead variable)
- T5 review: APPROVED (0C, 0I, 1M parked)
- T6 review: APPROVED (0C, 0I, 1M parked: dead neutralizeFence import kept per brief)
- T7 review: NEEDS_FIXES (1C: vacuous ZWS assertion in rag test) → amend commit `efb862c1`，scoped re-review **APPROVED** (0C, 0I, 0M)
- T8 review: APPROVED (0C, 0I, 2M parked: 4 tests have duplicate closing-tag assertion each, brief count was 19 vs actual 20)

**Parked Minor findings**（不阻塞合并，最终 review 时再 triage）：
- T1 M-1/2/3（empty-string test, preamble 措辞, U+200B 不可见）
- T2 M-1/2/3（tautological ZWS, stale docstring, file-top docstring drift）
- T3 M-1（dead-import）
- T4 M-1（dead variable）
- T5 M-1
- T6 M-1（dead import）
- T7 fix: 0
- T8 M-1（duplicate closing-tag assertion in test 3/6/7/10）, T8 M-2（brief count 19 vs actual 20）
- T8: APPROVED (0C, 0I, 2M parked: 4 tests have duplicate closing-tag assertions, brief count was 19 vs actual 20)

## Pre-flight recon 抓出的 plan 错误（per memory `sdd-plan-recon-catches-plan-fictions`）

Plan pseudo-code 系统性在 line number / 函数名上偏：

| Plan 说 | 实际 | 文件 |
|---------|------|------|
| `chat_stream.py:_streamAnswerWithFallback` | **不存在** | T3 |
| `_buildPlanSystemPrompt` 函数 | 不存在；plan prompt 由多块拼接 | T2 |
| `chat_multistep.py:1014-1015` | 文件只有 991 行；实际在 `chat_service.py:987 _planAndGenerateSql` | T5 |
| `run_agent_loop:717` | 实际 line 593（差 124） | T4 |
| `buildHypothesisMessages:262` | 实际 line 252（差 10） | T6 |
| `featureCatalogText` 裸 | 已走 `_sanitizeContext` 3 字符 + wrap `<feature_catalog>` | T2 |
| `_buildWikiQaUserPrompt` / `_buildRagUserPrompt` helpers | 不存在 | T7 |

**11/11 task 行号/类名/架构假设有偏差**。Brief 在 dispatch 前做了 pre-flight recon，全部纠正。

## Test DB 注意事项

Worktree `fix/qa-1012-p0-l1` base = `2f13e890`（L1 spec commit），max alembic = 0127。
**Main tip** 已有 0128/0129，但 worktree 代码不支持 ⇒ 测试 DB `qa_metadata_test_solo` 在切换 worktree 后仍可能停在 0129。
**fix**: 一次性 UPDATE `alembic_version` 表到 0127（per T3 pre-flight fix；仅对 solo test DB 适用，prod 不动）。

## 红队 e2e 覆盖（Task 8）

10 个测试 = 7 单服务 + 3 横向：

1. `test_1_plan_path_ontology_recall_payload_escaped` — 调 `_buildPlanUserPrompt`
2. `test_2_answer_path_data_summary_payload_escaped` — 调 `_buildAnswerPrompt`
3. `test_3_multistep_injection_text_payload_escaped` — 调 `_wrapMultistepInputs`
4. `test_4_l4_tool_result_payload_escaped` — 调 `_dispatchSingleTool`
5. `test_5_hypothesis_data_summary_payload_escaped` — 调 `buildHypothesisMessages`
6. `test_6_wiki_qa_chunks_payload_escaped` — 调 `WIKI_QA_SYSTEM_PROMPT.format(wrapped=...)`
7. `test_7_rag_qa_chunks_payload_escaped` — 调 `DOC_QA_SYSTEM_PROMPT.format(wrapped=...)`
8. `test_8_all_services_prepend_security_preamble` — 横向
9. `test_9_all_wrap_labels_in_whitelist` — 横向
10. `test_10_injection_payload_never_escapes_wrapping` — 11 label 参数化

**20 actual test cases**（10 顶层 + 10 参数化 over 11 labels）。

## Memory 沉淀

- 新建 `~/.claude/projects/.../memory/qa-system-l1-prompt-injection-defense-done.md`
- 更新 `MEMORY.md` 索引加 1 行
- **关键**要写：
  - 11-label 白名单 + UntrustedLabel 集中处
  - `_sanitizeContext` 3 字符 vs `wrapUntrusted` 5 字符 + U+200B 的差异
  - `_wrapMultistepInputs` 是 chat_service.py 模块级 helper（**不是** `_buildXxxUserPrompt`）
  - `agent_runtime_service._dispatchSingleTool` tool result content **必须**包（最高风险注入面）
  - `wiki_chunks` 标签在 RAG 也用（不光是 wiki）— 同一 label 复用

## Plan 对应

| Plan 任务 | SSOT 任务 | 状态 |
|----------|----------|------|
| Task 1 | T1 | ✅ |
| Task 2 | T2 | ✅ |
| Task 3 | T3 | ✅ |
| Task 4 | T4 | ✅ |
| Task 5 | T5 | ✅ |
| Task 6 | T6 | ✅ |
| Task 7 | T7 | ✅ |
| Task 8 | T8 | ✅ |
| Task 9 (SSOT) | 本文件 | ✅ |
