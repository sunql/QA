# 变更：chat_service.py 拆分为 8 文件（§2.4 LOW 文件拆分 Phase 1.2）

- **日期**：2026-09-28
- **作者**：Claude (with user direction 启琳)
- **Phase**：§2.4 LOW 文件拆分（Phase 1.2）
- **状态**：done
- **关联**：姊妹批 `chore-nl2sql-service-file-split`（Phase 1.1，门面+re-export 模式）、
  `chore-magic-number-governance-spec`（Phase 2 规范已落地、实施待执行）、
  `Harness/wiki/chat-service-assessment.md` §2.4 LOW 行
- **迁移版本**：无（纯模块搬迁，无 DB schema 变更）

---

## 1. 需求

`backend/app/services/chat_service.py` 原 **4916 行**，远超 800 行软上限。用户拍板
「重评估类拆分（像 nl2sql 那样）」。与 nl2sql 不同，`ChatService` 是**有状态类**
（~105 方法、~20 个 `self._xxx` 注入依赖），故采用 **mixin 分解**而非方法体→模块函数。

## 2. 设计评审（关键洞察）

**机制与 nl2sql 的本质差异**：
- `Nl2SqlService` 无状态 → 方法体→模块函数是纯机械替换。
- `ChatService` 方法大量读写 `self._xxx` 注入依赖 → 用 mixin：方法仍是方法，
  `self._xxx` 经 MRO 在合并后的类上照常解析；内部 `self.method()` 跨 mixin 调用、
  外部 `service.method()`、测试 monkeypatch 实例方法**全部零改动**。

**风险管控（与 nl2sql 的关键差异）**：
1. `test_chat_service.py` 有 **36 个既有失败**，无法用「全绿」判回归 → 每个 mixin 落地后
   用 `git stash` 对比拆前失败集**完全不变**（55 核心失败 + 9 领域集成失败逐一 diff）。
2. **MRO 冲突**：mixin 间方法名不得重复（Python 静默前者遮蔽后者）→ 合并后断言
   `ChatService.__mro__` + `dir(ChatService)` 无重名（实测 0 重名）。
3. **`__globals__` 陷阱**：被移方法引用的裸名（非 `self.`）必须 import 进新模块，否则
   `NameError`（本次踩过两例：`_looks_like_compound_question`、`Any`）。
4. **monkeypatch 常量陷阱**：测试 `monkeypatch.setattr(module, CONST)` 只打目标模块
   `__dict__`；被移常量须 patch 新模块（`test_chat_service.py` 改 patch `chat_context`）。

## 3. 模块布局（实测行数，全部平铺 `app/services/`）

| 模块 | 行数 | mixin/职责 | 成员要点 |
|---|---|---|---|
| `chat_helpers.py` | 416 | 共享纯函数/数据类（阶段 0，被 3+ 模块共用） | `_clipText`/`_fitPartsToBudget`/`_summarizeExecutionError`/`_statePlan`/`_snapshotRound`/`_looks_like_compound_question`/`_speakerFor`/`_step_result_to_read`/`_logEmbeddingTaskFailure`/`_userFacingErrorText`/`_stepFailedError` + `_RetryFailure`/`_RetryGenUsage`/`_PipelineContext`/`_SqlOutcome`/`_StepRun`/`StreamPersistState` + `_attachRetryFailure`/`_retryFailure`/`attachStreamPersistState`/`streamPersistStateOf` |
| `chat_recall.py` | 538 | `RecallMixin` — 类召回/过滤/排序 | `_getClassFilterMaxClasses`/`_getAdsRecallWeight`/`_rankByLayer`/`_fetchAllDimClasses`/`_selectRelevantClasses`/`_fallbackRecall`/`_expandByJoinNeighbors`/`_buildFewShot` + `_CLASS_FILTER_*`/`_FEW_SHOT_*`/`_ADS_RECALL_WEIGHT_DEFAULT` |
| `chat_multistep.py` | 558 | `MultiStepMixin` — 多步/追问/步骤错误 | `_detectMultiStep`/`_resolveExplicitMultiStep`/`_isFollowUpRetryCandidate`/`_rewriteFollowUpQuestion`/`_prepareFollowUpMultiStep`/`_resolveGlobalFilters`/`_executeDataStep`/`_executeMultiStep`/`_finalizeMultiStepDegrade`/`_summarizeStepData` |
| `chat_context.py` | 253 | `ContextMixin` — 上下文/历史/状态 | `_buildContextPrompt`/`_loadRecentRounds`/`_roundsFromClientHistory`/`_loadQueryState`/`_saveQueryState`/`_buildStatePrompt`/`_storeSessionMessages` + `_CONTEXT_*`/`_STATE_HISTORY_FIELD_LIMIT` |
| `chat_usage.py` | 332 | `UsageMixin` — 查询执行/用量/成本/图表 | `_runQuery`/`_runQueryWithRetry`/`_accountRetryGenUsage`/`_recordUsage`/`_recordDirectUsage`/`_recordChartUsage`/`_recordAnswerUsage`/`_summarizeUsage`/`_costForSql`/`_costFor`/`_consumedTokens`/`_columns`/`_chartStep`/`_generateAnswer` |
| `chat_stream.py` | 918 | `StreamMixin` — 流式输出 | `processMessageStream`/`_streamChitchat`/`_chitchatStreamEvents`/`_streamClarify`/`_streamInterceptCard`/`_streamQuery`/`_streamMultiStep`/`_stepResultEvent`/`_singleStepOverview`/`_singleStepStart`/`persistInterruptedStream` |
| `chat_domain.py` | 659 | `DomainCommandMixin` — 领域命令/Agent/供应商/图推理 | `_handleClarify`/`_handleDomainCommand`/`_handleSupplier360`/`_handleSupplierRisk`/`_handleAgentRun`/`_handleGraphReasoning`/`_handleDefineMetric`/`_handleDefineClass`/`_handleShowMetric`/`_handleMapProperty`/`_streamDomainCommand`/`_entitiesFor` |
| `chat_l4.py` | 207 | `L4Mixin` — L4 Agent Loop | `_handleNl2SqlAgent`/`_runL4AgentLoop`/`_resolveChatLlmClient`/`_buildL4ChatResponse`/`_isL4AgentLoopEnabled` + `_L4_EXPLORATORY_KEYWORDS` |
| `chat_service.py`（基类） | 1443 | 真编排 + 交叉小特性 | `__init__`/`processMessage`/`_classifyMessage`/`_buildPipelineContext`/`_handleGenericQuery`/`_planAndGenerateSql`/`_tryRefineDirect`/`_twoStageGenerate`/`_prepareSupplierQuestion`/`_usableModelConfigs`/`_buildDriftWarning`/L1/Feature 响应/亲和性/badge/`_listModelConfigs`/`_callWithFallback`/`_spawnEmbedding` |

最终类声明：

```python
class ChatService(RecallMixin, MultiStepMixin, StreamMixin, ContextMixin,
                  UsageMixin, DomainCommandMixin, L4Mixin, ChatStreamOutputMixin):
    def __init__(self, *, ...): ...   # 仍唯一集中注入全部 self._xxx
```

## 4. re-export（既有测试 import 零改动）

chat_service.py 底部显式 re-export 被测试/生产直接 import 的私有名（与 nl2sql 门面同模式）：

- **chat_helpers**：`_statePlan`/`_looks_like_compound_question`/`_attachRetryFailure`/
  `_retryFailure`/`_stepFailedError`/`_userFacingErrorText`/…（chat_service 顶部 `from chat_helpers import` 即为 re-export）
- **chat_recall**：`_getClassLayer`/`_isDimensionHint`/`_isExplicitOdsRequest`/`_LAYER_RANK`/
  `_CLASS_FILTER_TOP_K`/`_FEW_SHOT_*`/…（`test_chat_service.py` 直接 import）
- **chat_context**：`_CONTEXT_PROMPT_CHAR_BUDGET`/`_RECENT_ROUNDS_LIMIT`/`_STATE_HISTORY_FIELD_LIMIT`
  （`test_chat_service.py` / `test_chat_service_state.py` 读这些常量断言）
- **llm_retry_policy**：`_attachRetryGenTokens`/`_retryGenTokens`（`test_chat_step_error_text.py`）

## 5. 提交序列（每 mixin 独立 commit，逐次验证）

```
ddabb53 refactor: 抽出 ChatService 共享纯函数/数据类到 chat_helpers.py   （阶段 0）
4e17144 refactor: 抽出 RecallMixin（类召回/过滤/排序）到 chat_recall.py
da79a5a refactor: 抽出 MultiStepMixin（多步执行/追问级联）到 chat_multistep.py
0cafe13 refactor: 抽出 ContextMixin（会话上下文/历史/查询状态）到 chat_context.py
7f14f53 refactor: 抽出 UsageMixin（查询执行/用量/成本/图表）到 chat_usage.py
c6562a1 refactor: 抽出 StreamMixin（流式输出/断连兜底）到 chat_stream.py
ee3ac85 refactor: 抽出 DomainCommandMixin（领域命令/Agent/供应商/图推理）到 chat_domain.py
2c8bce7 refactor: 抽出 L4Mixin（L4 Agent Loop）到 chat_l4.py
```

## 6. 测试

- **验收口径**：拆前既有失败集不变（非全绿）。每个 mixin 落地后 `git stash` 对比：
  - 核心 3 文件（`test_chat_service.py` + `test_chat_service_stream.py` +
    `test_chat_service_state.py`）＝ **55 失败**，逐行 diff **IDENTICAL**
  - 领域集成 5 文件（agent_run / agent_run_audit / supplier_360 / supplier_risk /
    supplier_name）＝ **9 失败**，逐行 diff **IDENTICAL**
  - `test_l4_agent_loop_metering.py` = **3 passed**（当前）
  - `test_supplier_risk_llm_metering.py` = passed（`_resolveChatLlmClient` 经 MRO 正常）
- **import smoke**：`python -c "from app.services.chat_service import ChatService"` 通过；
  `ChatService.__mro__` 含全部 8 个 mixin；`dir(ChatService)` 0 重名
- **静态分析**：每 mixin `ast` 扫描裸名引用，仅 `chat_l4.py` 漏 `Any`（已补 `from typing import Any`）
- **全量收集**：`pytest --collect-only app/tests/unit/` = **2527 tests collected，0 收集错误**

## 7. 诚实结论

- 4916 → 基类 1443 行（~70% 削减），但基类与 `chat_stream.py`（918 行）仍未达 800 硬限。
  这是**有意接受**：基类是「真编排 + 交叉小特性」不可再拆；`chat_stream.py` 的
  `processMessageStream` 是 500+ 行的整体编排。每个 mixin 职责单一、可独立测试，
  符合拆分目标（利于后续扩展），而非强求逐文件 <800。
- 拆分期间**未改任何方法名/签名/逻辑**，仅换模块位置（除必要的 import 补充）。

## 8. 关联

- **完整计划**：`/Users/sunql/.claude/plans/rosy-beaming-phoenix.md`（§2.4 LOW 3 阶段）
- **Phase 2 规范**：[`Harness/rules/魔数治理.md`](../../rules/魔数治理.md) +
  [`chore-magic-number-governance-spec`](../chore-magic-number-governance-spec/summary.md)
  （14 项常量候选已 SSOT 化；本拆分后常量已随 mixin 落位，Phase 2 在各自模块内新增 `_getXxx(session)`）
- **Phase 1.1 姊妹批**：[`chore-nl2sql-service-file-split`](../chore-nl2sql-service-file-split/summary.md)
- **memory 待登记**：`qa-system-chat-service-file-split`（Phase 1.2 完成；Phase 2 魔数治理实施待执行）

## SSOT 校验清单

- [x] 8 个模块（7 mixin + 1 helper）落地，方法名/签名零改动
- [x] 基类唯一集中注入 `self._xxx`，MRO 无重名冲突
- [x] re-export 补齐（既有测试 import 零改动）
- [x] 55 核心 + 9 领域集成失败集逐行 IDENTICAL（无回归）
- [x] import smoke + MRO + 静态裸名分析 + 全量收集 0 错误
- [x] Phase 2（魔数治理实施）→ 下次执行；Phase 3（§15 残差）→ 后续
