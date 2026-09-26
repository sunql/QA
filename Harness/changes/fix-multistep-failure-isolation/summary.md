# 变更：fix-multistep-failure-isolation

- **日期**：2026-09-26
- **作者**：Claude
- **Phase**：bugfix（多步执行失败隔离 + 重试上下文一致性）
- **状态**：done（待提交）
- **关联变更**：[fix-llm-metering-blindspots](../fix-llm-metering-blindspots/summary.md)、[feat-multistep-global-filter](../feat-multistep-global-filter/summary.md)、[feat-follow-up-cascade](../feat-follow-up-cascade/summary.md)（同属一轮 Chat 服务治理）
- **迁移版本**：无
- **上游评估**：[`Harness/wiki/chat-service-assessment.md`](../../wiki/chat-service-assessment.md) §2.1 的 **C3** / **C4**

---

## 1. 需求

`chat-service-assessment.md` §2.1（CRITICAL）最后两个未修项：

- **C3 多步失败隔离不覆盖硬异常**：只有「计划判定无法回答」这种软失败被隔离；`_planAndGenerateSql`
  抛错、或 `_runQueryWithRetry` 执行 + 回灌重试均失败时，异常会穿透整条多步序列到 API 层
  （非流式 500 / 流式 internal 错误事件），**已完成步骤的数据与已消耗 token 全部作废，无部分结果恢复**。
- **C4 多步执行错误重试用错问题**：`_runQueryWithRetry` 回灌重试时恒用 `dto.question`（原始复合问题）
  且 `priorState=None`，同时丢掉**子问题范围**与**跨步注入文本**（如第二步要引用的「前三个供应商」），
  与本步骤首次生成的口径不一致 —— 重试生成的 SQL 可能重新对齐回整个复合问题。

**验收标准**

1. 单个数据步骤硬失败（生成抛错 / 执行 + 回灌重试均失败）不再中止整条序列：HTTP 200、该步
   `error` 非空且 `sql`/`data` 为空、**其它步骤与汇总照常完成**（部分结果保留）。
2. 流式同一分支：不产生 `error` 事件、末帧仍是 `done`、失败步下发 `step_result`（error 非空）。
3. **所有**数据步骤都失败时不调用汇总 LLM（它只会基于错误行编造结论），改为走既有降级收尾，
   文案为 `MSG_MULTI_STEP_DEGRADE_FAILED`。
4. 失败步骤的 token/成本照常计入总量与 `session_token_usage` 台账（钱花了就得记账，核心约束 #3）。
5. 失败步骤不得成为下一轮追问的锚点（`last_plan/last_sql/last_data` 只由成功步骤更新）。
6. 多步重试的生成上下文 = 本步骤首次生成的上下文（子问题 + 前序步骤注入 + 主问题作 `scopeQuestion`）；
   单步重试行为完全不变。
7. 失败步骤在下游 prompt（后续步骤注入 / 汇总）中**不再渲染空数据块**，改为显式「无数据 + 不得推测」。
8. 无回归：多步 / 追问级联 / 全局过滤 / chat / 流式 / 模型降级集成套件与全量单元套件全绿
   （仅剩既有预存失败）。
9. 用户可见的步骤错误只含驱动给的原因：不含 `[SQL: ...]`（内部表/列名）与 `[parameters: ...]`
   （查询字面量可能含业务数据）；详细原因仍进服务端日志与回灌 LLM 的重试反馈。
10. **回灌重试的生成 token 也落账**（重试执行再失败时）：`session_token_usage` 出现该次
    `nl2sql` 行且 token > 0，并计入响应 `tokensUsed/cost`。

## 2. 设计评审

### 根因

两条路径（`_executeMultiStep` 非流式、`_streamMultiStep` 流式）各自复制了一份「生成 → 执行」循环，
**只隔离了软失败**：

```python
if outcome.sql is None or outcome.plan is None or outcome.plan.isUnanswerable:
    completed.append(StepResult(..., sql=None, error="无法回答（LLM 判定无有效查询计划）"))
    continue
data, final_sql, retry_tokens = await self._runQueryWithRetry(...)   # ← 这里抛错就没人接
```

C4 则是「同一个循环里两次生成用了不同的上下文」，而**重试路径绕过了 `_planAndGenerateSql`**，
自己直接调 `generateSql(dto.question, ..., priorState=None)`，于是子问题与注入文本一起丢失。

两者共享同一处结构问题：**步骤内没有单一的执行出口**。两份复制代码各自演化，改一处漏一处
（C1/C2、D3 都栽在这个模式上）。

### 设计

1. **抽取共享 helper `_executeDataStep`**（流式/非流式同源）：内部完成生成 → 执行 → 回灌重试、
   token/cost 累计、`_spawnEmbedding`、`_recordUsage`，并把**所有**步骤级硬失败收敛为
   `_StepRun(result=StepResult(error=..., sql=None), tokens=..., cost=..., modelName=..., plan=...)`。
   两条循环只剩「记账 + 更新锚点 + （流式）发事件」，无法再漂移。
2. **`sql is None` 作为统一失败标记**：与既有 `_finalizeMultiStepDegrade` 的
   `succeeded = [r for r in completed if r.sql is not None]` 判据同口径，因此降级文案的
   「完成 N/M 步」自动正确。新增 `_hasDataStepResult` 复用同一判据，避免两处定义漂移。
3. **全失败不进汇总**：聚合步骤分支先查 `_hasDataStepResult(completed)`，否则 `continue`
   （用 `continue` 而非 `break`：不依赖「汇总步骤一定在末尾」这一隐含契约，若后续还有数据步
   被跳过也能继续执行）。
4. **失败步骤不污染下游 prompt**：`inject_to_prompt` 与 `StepAggregator._build_prompt` 对
   `sql is None` 的步骤改渲染「无数据 / 不得推测」，不再输出 `[aggregate] []` ——
   空数据块会被 LLM 读成「该查询结果为 0」并据此生成错误的 `WHERE IN` 或对比结论。
5. **C4 用显式 kw-only 参数而非隐式全局状态**：`_runQueryWithRetry(..., question=None, prior_state=None)`，
   多步调用方传子问题 + 注入文本；`question is not None` 同时作为「多步语境」判据，
   决定是否把主问题透传为 `scopeQuestion`。单步调用方不传 ⇒ 行为逐字节不变。

### 一处刻意的行为变更

失败步骤的错误文案会进入**用户可见响应**（`steps[i].error`）：`该步骤查询生成失败：…` /
`该步骤执行失败：…` 前缀 + 截断到 200 字符（`_STEP_FAILED_ERROR_LIMIT`）。选择暴露而非隐藏，
是因为「哪一步失败了、为什么」是多步场景下用户唯一能自我纠正的信息；截断 + `_summarizeExecutionError`
（取 `exc.message` 或 `str(exc)`）避免了把整段堆栈或连接串写进响应。

### 前端连带修复：两处因「后端开始返回失败步骤」而变错的旧契约假设

失败隔离只做后端等于没做完——用户看不到「哪一步失败了」，C3 的价值就落不了地。改动前，
非流式多步响应**永远只有成功步骤**，前端据此写死了两处假设：

1. `chatStore.ts` 非流式分支硬编码 `status: "done"`（注释原文：「steps 数组均为『已完成』」）——
   C3 之后失败步骤（`sql=None, error=非空`）也会走这条路径 ⇒ 失败被渲染成**已完成**。
   改为按 `error` 判定，并抽 `stepStatusFromResult` 与流式 `onStepResult` 同源（原来两处各写一遍）。
2. 流式「所有数据步骤失败」新分支跳过了汇总步骤，而多步计划概览（`EVENT_MULTI_STEP_PLAN`）**早已把
   汇总步下发**给前端（初始「待执行」）⇒ 该步永远停在「待执行」；末帧 `steps` 只含数据步骤，不会自愈。
   改为跳过时补发一条 `step_result`（`未执行（前置数据步骤全部失败）`）作为终态事件。
   该事件**只发不进 `completed`**：既保持末帧 `steps` 与非流式一致地只含数据步骤，也避免把汇总步自己
   算进 `_finalizeMultiStepDegrade` 的「完成 N/M 步」分母。

## 3. 改动清单

| 文件 | 内容 |
|---|---|
| `app/services/chat_service.py` | 新增 `_executeDataStep`（两条路径共用）+ `_StepRun` frozen dataclass + `_stepFailedError` / `_failedStepResult` / `_hasDataStepResult` / `_userFacingErrorText`（复审 MEDIUM 1）+ `_attachRetryGenTokens` / `_retryGenTokens`（复审 MEDIUM 2）+ 相关常量；两条循环各自瘦身为「记账 + 锚点 + 事件」；聚合分支加「无成功步骤则跳过 + 补发终态事件」；`_runQueryWithRetry` 增 kw-only `question` / `prior_state` / `scope_question` |
| `app/domain/multi_step_plan.py` | `inject_to_prompt`：`sql is None` 的步骤渲染为「该步骤无数据可注入（原因）」，不再输出空数据块 |
| `app/services/step_aggregator.py` | `_build_prompt`：`sql is None` 的步骤渲染为「无数据 + 不得为其推测或编造结果，也不要把它当作 0 值参与对比」 |
| `app/tests/integration/test_chat_multi_step.py` | 新增 `_DataQueryFailAdapter` / `_parseFrames` / `_spyGenerateSql` / `_SqlCall` / `_PerStepLlm`；新增 `TestStepFailureIsolation`（4 例）+ `TestStepRetryContext`（1 例）；顺带把 3 处重复的 SSE 解析收敛为 `_parseFrames` |
| `app/tests/unit/test_multi_step_plan.py` | 新增失败步骤注入渲染用例（无数据块 + 原因可见） |
| `app/tests/unit/test_step_aggregator.py` | 新增失败步骤汇总 prompt 用例（禁止推测 + 无「共 0 行」数据块） |
| **`app/tests/unit/test_chat_step_error_text.py`** | 新增（复审 MEDIUM 1/2）：`_userFacingErrorText` 剥 SQL/参数 + 兜底 + 保留 `.message`；`_stepFailedError` 前缀/截断；`_attachRetryGenTokens` 往返且不改异常类型与消息 |
| **`frontend/src/stores/chatStore.ts`** | 抽 `stepStatusFromResult(error)`（流式 `onStepResult` 与非流式 `steps` 回填共用）；非流式 `steps` 不再硬编码 `status: "done"` |
| **`frontend/src/tests/chatStore.test.ts`** | 新增「非流式失败步骤 → status error」用例 |

### 关键实现：`_executeDataStep`

```python
injection_text = ctx.inject_to_prompt(step_plan.index)
try:
    outcome = await self._planAndGenerateSql(..., sub_question=..., injection_text=...)
except Exception as exc:                       # 硬失败 1：生成阶段
    return _StepRun(result=_failedStepResult(step_plan, _stepFailedError(exc, _STEP_GEN_FAILED_PREFIX)))
tokens, cost, model_name = ...                 # 生成 token 已花掉，照常累计
...
try:
    data, final_sql, retry_tokens = await self._runQueryWithRetry(
        session, dto, pc, outcome,
        question=step_plan.sub_question, prior_state=injection_text,   # ← C4
    )
except Exception as exc:                       # 硬失败 2：执行 + 回灌重试均失败
    return _StepRun(result=_failedStepResult(step_plan, _stepFailedError(exc, _STEP_EXEC_FAILED_PREFIX)),
                    tokens=tokens, cost=cost, modelName=model_name)
```

## 4. 验证（TDD：先 RED 后 GREEN）

| 用例 | 断言要点 |
|---|---|
| `TestStepFailureIsolation::test_non_stream_step_failure_isolated` | 200 + 步骤 2 `error` 非空 / `sql`·`data` 为空 + 步骤 1 完整 + 汇总 LLM 被调用 + 台账 5 行 + `state.last_sql == steps[0].sql` |
| `…::test_stream_step_failure_isolated` | 无 `error` 事件 + 末帧 `done` + 2 条 `step_result`（0 成功 / 1 失败）+ 汇总 `step_plan` 帧存在 |
| `…::test_non_stream_all_steps_failed_skips_aggregation` | 两步均 error + 汇总 LLM **未**被调用 + `answer == MSG_MULTI_STEP_DEGRADE_FAILED` |
| `…::test_stream_all_steps_failed_skips_aggregation` | 同上（流式） |
| `TestStepRetryContext::test_retry_uses_sub_question_and_prior_injection` | `generateSql` 的重试调用：`question == 子问题 != 原复合问题`、`priorState` 含「前序步骤结果」、`scopeQuestion == 主问题` |
| `test_inject_to_prompt_marks_failed_step_without_data_block` | 「该步骤执行失败」可见 + 无 `[/aggregate]` / `[/entity_list]` |
| `test_build_prompt_failed_step_marked_not_fabricable` | 「不得为其推测」可见 + 无「共 0 行」 |
| `…::test_stream_all_steps_failed_skips_aggregation`（追加） | 汇总步无 `step_plan`（未进入执行）+ 末条 `step_result` 是汇总步且 `error` 非空（RED：`assert 2 == 3`） |
| `chatStore.test.ts::非流式多步：失败步骤标记为 error` | 步骤 2 `status: "error"`（RED：实际 `"done"`） |

### RED → GREEN 证据

首轮 RED（修复前，用「第二步 SQL 带专属标记」的假 LLM 构造失败）：非流式 2 例以
`RuntimeError: ORA-00942` 从 API 层穿出，流式 2 例断言 `'error' not in [...]` / `'error' == 'done'` 失败，
C4 例断言 `'请分步查询 2024 和 2025 年的销售额并对比' == '2025年的销售额是多少'`（重试确实用了复合问题）。
两个单元用例同样先红（失败步骤渲染成 `[aggregate] []`）。

修复后 17 passed（`test_chat_multi_step.py` 全文件）。

前端连带修复：`chatStore.test.ts` **30 passed**（含新增 1 例），`tsc --noEmit` 干净。

### 变异验证（比「跑在 HEAD 上」更强的证据）

HEAD 工作树不含本轮之前若干未提交批次，且失败「标记假 LLM」的构造方式与最终用例不同，
故改用**定向变异**逐条证明用例是真闸（在 `/tmp` 的 worktree 副本里改，不动工作树）：

| 变异 | 结果 |
|---|---|
| `_executeDataStep` 的 `except` 改回 `raise`（还原 C3 穿透） | 4 个隔离用例全红（`RuntimeError` 穿出 / 流式出 `error` 帧），C4 用例仍绿 → 证明 C3 用例与 C4 解耦 |
| `_executeDataStep` 不再向 `_runQueryWithRetry` 传 `question`/`prior_state`（还原 C4） | 仅 C4 用例红，4 个隔离用例仍绿 → 证明 C4 用例与 C3 解耦 |
| 还原 `inject_to_prompt` 的失败步骤分支 | 对应单元用例红（渲染出 `[aggregate] []`） |
| 还原 `_build_prompt` 的失败步骤分支 | 对应单元用例红（出现「共 0 行」数据块） |
| `_userFacingErrorText` 改回直接返回 `_summarizeExecutionError`（复审 MEDIUM 1） | `test_chat_step_error_text.py` 3 条红（含「剥完为空要有兜底」），其余 5 条绿 |
| 去掉 `_attachRetryGenTokens` 调用（复审 MEDIUM 2） | `test_non_stream_step_failure_isolated` 红（`nl2sql` 行 2 ≠ 3）——即修复前的 RED 证据 |

### 回归

- 相邻集成套件（9 文件：多步 / 追问级联 / 全局过滤 / 模型降级 / doc_qa / chat / chat 流式 /
  L4 计量 / 供应商风险计量）：**66 passed**（含前端连带修复后的复跑）
- 全量单元套件：**2243 passed, 2 failed**，两个失败均为既有预存（`test_chat_service.py` 的 ADS
  加权重排死代码断言；`test_dependencies.py` 的 Starlette `Header.lower` 版本漂移），本批未触碰。

## 5. code-reviewer 复审

审查范围限定本批 6 个文件的 C3/C4 hunks（工作树含其它未提交前序批次，明确排除）。
结论：**APPROVE**，0 CRITICAL / 0 HIGH / 2 MEDIUM / 2 LOW。

| 级别 | 问题 | 处理 |
|---|---|---|
| MEDIUM | **步骤级硬异常原文进用户可见响应**：`_stepFailedError` 用 `_summarizeExecutionError`（= `str(exc)`），而 SQLAlchemy 语句异常的 `str()` 会在驱动原因后追加 `[SQL: ...]`（内部表/列名）与 `[parameters: ...]`（查询字面量，可能含业务数据）；该字符串经 `_step_result_to_read` / `_stepResultEvent` 直接进响应并由前端渲染。违反「UI 层友好提示 / 错误信息不泄漏敏感数据」。 | 新增 `_userFacingErrorText`：只保留驱动给的首段（`[SQL:` / `[parameters:` 起全部剥掉），剥空时兜底 `执行失败（详见服务端日志）`；详细原因仍留在服务端日志（`exc_info=True`）与**回灌 LLM 的重试反馈**（`_summarizeExecutionError`，LLM 需要细节）。用例：集成 `test_step_error_text_hides_sql_and_parameters`（用真实 `ProgrammingError`，RED 时响应里可见 SQL 全文与 `{'n': 'ACME-机密客户'}`）+ 5 条纯函数单测（新增 `test_chat_step_error_text.py`） |
| MEDIUM | **重试生成的 token 在「重试执行也失败」时丢账**：`_runQueryWithRetry` 重试生成成功、重试执行再失败时 `raise firstErr`，`retryResult` 的 token 既不返回也不落库 ⇒ 那次生成花了钱却不进台账/总量，C3「token 不再作废」只兑现了一半。原用例的 `purpose` 精确列举断言恰好**固化了这个缺口**（只期望 2 个 `nl2sql` 行）。 | 新增 `_attachRetryGenTokens` / `_retryGenTokens`：把 token 挂到**上抛的原始异常**的私有属性上（复用 `_consumedTokens` 对 `Nl2SqlError.tokens` 的同一思路，**不改异常类型/消息**——API 层按类型映射 HTTP 状态）；`_executeDataStep` 的 except 分支取出后落账并计入 `run.tokens/cost`。用例：`test_non_stream_step_failure_isolated` 改为期望 **3** 个 `nl2sql` 行且 prompt_tokens > 0 |
| LOW | `_hasDataStepResult` 的语义是「截止汇总步为止有没有成功」而非「计划里所有数据步都失败」；汇总步不在末尾的畸形计划下两者不等价 | 非 bug（`StepQueryPlanner` 两处构造点汇总步均在末尾，该形态生产不可达；且 `continue` 比 `break` 更保守，不编造结论）。按建议在 docstring 写明语义边界与「真要支持该形态需对 `data_steps` 全集判断」，不改行为 |
| LOW | C4 用「是否传入 `question`」隐式推断「是否多步」，未来单步调用方若顺手传 `question` 会静默改变 `scopeQuestion` 口径 | 改为显式 kw-only `scope_question`，多步调用方显式传 `dto.question`，单步调用方不传 ⇒ 维持 `None`（与旧行为逐项一致），隐式契约消除 |

reviewer 逐项核对**无需修改**的点（保留备查）：`_executeDataStep` 抽取后的 token/cost/锚点/`_spawnEmbedding`/`_recordUsage` 与原两条路径逐项等价；原非流式的
`sqlConfig is None → Decimal("0")` 分支是死代码（多步恒 `NEW_QUERY`，REFINE 捷径不可达；即便可达 `_costForSql` 也给 0）；
`except Exception` 不误捕 `asyncio.CancelledError`（BaseException），流式取消不会被静默吞掉；
`inject_to_prompt` / `_build_prompt` 的失败步骤渲染正确且 error 文本已转义；
C4 用例的 spy 断言的是真实行为（委托真实实现），`_DataQueryFailAdapter` 的序号判定在固定 LLM stub 下确定性成立。

### 复审带出的连带修复之后的复跑

`test_chat_multi_step.py` **18 passed**（17 + 脱敏用例）；新增单测 `test_chat_step_error_text.py` **8 passed**；
相邻集成 9 文件 **67 passed**；全量单元 **2251 passed, 2 failed**（仍是那两个预存失败）；
前端 `chatStore.test.ts` **30 passed**、全量 vitest **1186 passed, 1 failed**（`EntityMappingPage.test.tsx`，
与本批无关的预存失败——该文件未在本批改动且未在工作树修改列表中）。

## 6. 遗留

1. ~~**重试生成 token 在「重试执行也失败」时仍会丢账**~~ ✅ **本批已修**（复审 MEDIUM 2，
   见 §5）：`_attachRetryGenTokens` 把 token 挂在上抛的原始异常上，`_executeDataStep` 落账。
   **仍存在的边角**：单步路径（`chat_service.py` 两处 `_runQueryWithRetry` 调用）拿到异常后直接
   抛给 API 层，未取 `_retryGenTokens(exc)` 记账 —— 单步失败即整轮失败，影响小于多步，
   但要走「直调收口」的统一口径仍需在那两处补一次；**重试生成本身抛错**时（`Nl2SqlError`
   携带 tokens）仍未在此路径记账，且要小心与 `_callWithFallback` 已写的 `fallback_sql` 行重复计数。
2. **`_callWithFallback` 全模型失败时的浪费 token** 同属预存缺口（`fallback_*` 行只在能拿到
   `Nl2SqlError.tokens` 时记录）。
3. **C 路径（`_isFollowUpRetryCandidate` 兜底）的 `global_filters` 对称缺口** 仍未补：
   非流式 `chat_service.py:955`、流式 `:3233`（前一批遗留，与本批无关）。
4. 单步流水线（非多步）的硬失败仍直接抛给 API 层 —— 那是有意的：单步失败会先回退多步拆解，
   两条都失败才对外报错，C3 只承诺「多步序列内部的失败隔离」。

## 7. 教训

1. **「隔离失败」的判据要能一眼看出成败**：本批把 `sql is None` 确认为唯一失败标记，于是
   「是否还有成功步骤」(`_hasDataStepResult`) 与「降级文案 N/M」(`_finalizeMultiStepDegrade`)
   自动同源。此前软失败已经这么做了，硬失败没跟上 —— 同一个循环里两套成败定义就是缺陷温床。
2. **复制粘贴的双路径必然漂移**（本项目第三次踩）：C1/C2、D3 都靠「抽共享 helper」收口，
   本批把整个数据步骤的执行体抽成 `_executeDataStep`，循环里只剩记账与发事件 ——
   结构上让「只改一条路径」不再可能。
3. **空数据块与「查询结果为 0」在下游 LLM 眼里无法区分**：失败步骤若照常渲染 `[aggregate] []`，
   汇总会给出「2025 年销售额为 0」这种看似合理、实则编造的结论。失败必须显式声明，
   否则「失败隔离」只是把 500 换成了更隐蔽的错答案。
4. **测试的失败构造方式要与被测缺陷解耦**：第一版 C3 用例靠「第二步 SQL 含专属标记」构造失败，
   而重试会**重新生成** SQL（内容随 C4 是否修复而变），于是 C3 用例实际上被 C4 绑住 ——
   变异验证会立刻暴露这种耦合（还原 C3 时 C4 用例不该红、反之亦然）。改用「第 N 个数据查询起失败」
   的序号判定后，两个缺陷各自独立成闸。
5. **「给用户看」与「给 LLM/日志看」必须是两条出口**：`_summarizeExecutionError` 是喂给 LLM
   修正 SQL 的（越详细越好），却被复用成了用户可见文案 —— 于是 DB 驱动的 `[SQL: ...]`
   与 `[parameters: ...]`（含业务数据）直接进了响应。**一次异常同时服务两个受众时，
   先问「谁会读到它」**；脱敏要在用户出口做，且必须配「细节仍在 LLM 反馈里」的正向对照断言，
   否则容易退化成「一刀切截断」而丢掉 LLM 的自愈能力。
6. **失败路径也是计量路径**：「重试生成成功、重试执行失败」是一次只发生在异常分支上的真实
   花费（`adapter.failed == 2` 已证明它发生了），而原用例的 `purpose` 精确列举断言把它
   **固化成了期望**——断言写对了行为、写错了期望值。改动计量时，精确列举型断言要么补行、
   要么加「该行 token > 0」把它钉死，否则缺口会以「测试全绿」的形式长期驻留。
7. **后端新增「失败也返回 200」的契约时，必须同时检查前端的旧契约假设**（本批连带修复了
   `chatStore.ts` 两处：非流式 `status` 硬编码 `done`、流式跳过汇总步导致该步永远「待执行」）。
   只改后端等于只兑现了一半——失败可见性最终由前端呈现决定。
