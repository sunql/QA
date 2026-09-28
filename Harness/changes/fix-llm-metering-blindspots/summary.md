# 变更：fix-llm-metering-blindspots

- **日期**：2026-09-26
- **作者**：Claude
- **Phase**：bugfix（LLM 计量盲区 + 供应商风险成本口径）
- **状态**：done（待提交）
- **关联变更**：[fix-chat-llm-keyless-and-degrade](../fix-chat-llm-keyless-and-degrade/summary.md)、[feat-multistep-global-filter](../feat-multistep-global-filter/summary.md)、[feat-follow-up-cascade](../feat-follow-up-cascade/summary.md)（同属一轮 Chat 服务治理，各自独立成篇）
- **迁移版本**：无
- **上游评估**：[`Harness/wiki/chat-service-assessment.md`](../../wiki/chat-service-assessment.md) §2.2 的 H1 / H2 / H8 / H9

---

## 1. 需求

`chat-service-assessment.md` §2.2 列出 9 处计量盲区/静默降级（H1–H9）。用户裁决「按你的建议来」，批准先做 **H1 + H2 + H8 + H9** 这一批——四者同一根因：**直调 `client.complete(...)` / `complete_with_tools(...)` 绕过计量收口**，各自违反项目核心约束 #3「每次 LLM 调用必须记录 Token 消耗与成本」。C3/C4（多步硬失败隔离 + 重试子问题）牵动多步执行主干、需配流式+非流式双路径回归，单独立项，**不在本批**。

**验收标准**

1. L4 Agent Loop 的每一次 `complete_with_tools` 都在 `session_token_usage` 留行，purpose=`l4_agent_loop`，`model_config_id` 为该次实际使用的配置。
2. L4 的成本按**该配置的 `cost_per_1k_input/output`**（USD）计算，不再用硬编码的 gpt-4o-mini 单价。
3. `doc_qa`（`/doc-qa` 与 `/doc-qa/stream`）落 token 台账，与 `wiki_qa` 同口径；检索到 chunks 但分数偏低时**保留 citations**，不再存 `[]`。
4. 供应商风险点（`supplier_risk` 意图 + `supplier_risk_agent` 工具）的成本按 config 单价（**USD**）计，不再用硬编码 CNY 0.001/0.002。
5. 供应商风险 LLM 路径的降级必须是**显式分支 + 日志**，不得再靠 `await None.complete(...)` 抛 AttributeError 被 `except Exception` 吞掉；无 LLM 调用时**不得**写 0/0 假台账行。
6. 无回归：已有 chat / doc_qa / supplier_risk / agent_run 用例全绿。

## 2. 设计评审

### 根因

四个站点都是「服务内部直调 LLM → 只有自己知道 token 数 → 但只有 `chat_service` 有落库收口 `_recordUsage` / `_recordDirectUsage`」。跨模块时这个知识丢了：

| 站点 | 丢在哪 |
|---|---|
| H1 全局过滤抽取 | `extract_global_filters` 只返回 `GlobalFilters`，token 数没出口；调用方写 0-token marker，注释声称「已计到 plan/split 路径」，实际不符 |
| H2 L4 Agent Loop | `AgentLoopResult` 只有一个 USD 数字，**无字段承载 token**；该数字还是 `_estimate_cost` 按 gpt-4o-mini 硬编码估的 |
| H8 doc_qa | `rag_qa_service` 未接 `_recordUsage`（`wiki_qa_service` 接了） |
| H9 供应商风险 | 单价硬编码在 service 内部（且是 CNY），调用方无从覆盖；`llm_factory(None)` 在生产上必然造不出客户端 |

H2 的 `_estimate_cost` 与 H9 的 CNY 常数还有一个共同后果：**同一张 `session_token_usage` 台账里混着三种成本口径**（config USD / gpt-4o-mini 估算 USD / 硬编码 CNY），会话成本报表与 `ModelRouterService` 的预算降级判断都在读这张表。

### 设计

1. **token 数随返回值上行，成本留在收口处算**。`AgentLoopResult` 增 `prompt_tokens` / `completion_tokens`，`_AgentLoopStepResult` 同步；`_usage_tokens(usage)` 从各家 SDK 的 `usage` dict 取数，`_cost_for_usage(...)` 复刻 `chat_service._costFor` 的 USD 公式（`(pt·in + ct·out)/1000`，`Decimal` 全程）。
2. **工厂与配置成对**。`_generateRiskPoints` 的降级判据从「`llm_factory` 是否为 None」改成「工厂或配置任一缺失 / `factory(cfg)` 返回 None」——三者都走**显式**降级：`logger.warning` + `fallback_template` + tokens/cost 归零。`AgentToolContext` 因此增 `llm_config`，与既有 `llm_factory` 配对传递。
3. **成本可降级契约**。`_resolveChatLlmClient` 对三个调用方（L4 / 供应商风险 / agent_run）都改为「解析不出就返回 `None`，由调用方降级」，而不是抛错——L4 → L2/L3，供应商风险 → 模板文案。

### 一处必须记下的行为变更

**H9 让供应商风险的 LLM 路径在生产上第一次真正跑起来。** 生产工厂是 `createClient`，本项目没有 `openaiApiKey` 环境变量 ⇒ `createClient(None)` 返回 `None` ⇒ `await None.complete(...)` 抛 AttributeError ⇒ 被 `except Exception` 吞掉 ⇒ 风险点**永远**是 `fallback_template`。既有测试用「忽略入参、无论如何都返回客户端」的假工厂（如 `lambda cfg: _NoopLlm()`）把这个洞原样盖住了。

修复后：只要会话能解析出可用配置，风险点文案就改由 LLM 生成 ⇒ **延迟与成本都会上升**（此前恒为 0），文案也会变化。两个消费方都受影响：`supplier_risk` 意图与 `supplier_risk_agent` 工具。

## 3. 改动清单

| 文件 | 内容 |
|---|---|
| `app/services/agent_runtime_service.py` | 删 `_estimate_cost`（硬编码 $0.15/$0.6 per 1M）→ `_usage_tokens` + `_cost_for_usage`；`AgentLoopResult` / `_AgentLoopStepResult` 增 token 字段；`_runAgentLoopIteration` 在**三个** return 分支（含 `cost_cap`——那一轮的钱已经花了）都带上 token；`_runOneStep` 累加进 state；`run_agent_loop` 增 `cost_per_1k_input/output` 入参；`AgentRuntimeService.run` 增 `llm_config` 并透传 `AgentToolContext`；**失败路径收敛**（复审 HIGH，见 §5）：while 循环体包 try/except，异常按 `terminated_reason="error"` 正常返回，累计 token 不随异常丢失 |
| `app/services/chat_service.py` | `_resolveL4LlmClient` → `_resolveChatLlmClient`（返回 `(client, config)` 或 `None`）；空候选池直接返回 `None`（见「教训 2」）；`_runL4AgentLoop` 落 `purpose="l4_agent_loop"`；`_handleSupplierRisk` / `_handleAgentRun` 解析一次、成对透传 |
| `app/services/supplier_risk_service.py` | 成本改按 `llm_config.cost_per_1k_*`（USD）；降级改显式分支；`_buildFallbackRiskPoints` 路径带 `0/0/0.0/None` |
| `app/services/agent_tool_types.py` | `AgentToolContext.llm_config`（工厂+配置成对，注释写明原因） |
| `app/services/agent_tools.py` | `_supplierRiskHandler` 透传 `ctx.llm_factory` + `ctx.llm_config` |
| `app/domain/schemas.py` | `SupplierRiskRead.cost` 描述 CNY → USD |
| `app/services/rag_qa_service.py` | H8：`_PURPOSE_DOC_QA = "doc_qa_answer"` 落库；低分但有 chunks 时保留 citations；**零用量不落行**（复审 LOW，见 §5） |
| `app/services/step_query_planner.py` | H1：`extract_global_filters` 返回 token 数 |
| `app/tests/integration/test_chat_multi_step.py` | 三处 `assert sorted(r.purpose …) == [...]` 精确列举补齐 `multistep_global_filter`，并新增「该行 token > 0」断言（见 §4） |
| `app/tests/integration/test_l4_agent_loop_metering.py` | 新增 `test_l4_mid_loop_failure_still_meters_spent_tokens`（复审 HIGH 的回归闸） |
| `app/tests/unit/test_rag_qa_service.py` | 新增「低分短路持久化真实 citations」+「零用量不落台账行」两例（复审 MEDIUM / LOW） |

H1 的落库点在 `chat_service.py:1879`（`purpose="multistep_global_filter"`），与 [feat-multistep-global-filter](../feat-multistep-global-filter/summary.md) 同批完成。

## 4. 验证（TDD：先 RED 后 GREEN）

| 项 | RED 证据 | GREEN |
|---|---|---|
| H1 | 新增用例断言 `session_token_usage` 出现 `multistep_global_filter` 行 | 61 passed |
| H8 | 台账零行 + 低分场景 `citations=[]` | 13 passed（`test_rag_qa_service.py` + `test_doc_qa_api.py`） |
| H2 | 台账 0 行；成本 `1.2e-05`（硬编码）vs 期望 `6e-05`（config 单价） | 2 passed（新增 `test_l4_agent_loop_metering.py`，走完整 `POST /api/v1/chat`） |
| H9 | **诚实假工厂**（`cfg is None → None`，与 `createClient` 同语义）下 `riskPointsSource == "fallback_template"` | 2 passed（新增 `test_supplier_risk_llm_metering.py`）；回归 `test_supplier_risk_api.py` + `test_chat_agent_run.py` 18 passed；`test_supplier_risk_service.py` + `test_agent_runtime_service.py` 49 passed |

新增测试的设计要点：H9 的假工厂刻意**不**模仿旧的宽容假工厂——`_HonestFactory` 在 `cfg is None` 时返回 `None`，所以「`riskPointsSource == "llm"`」这一条断言本身就钉死了缺陷 2；第二条用例（`alwaysNone=True`）断言降级走的是「解析出配置了但客户端造不出来」这条显式分支（`factory.configs` 非空且全非 None），并断言 **零 LLM 调用不留台账行**。

### 集成回归：本批被隔壁套件抓出的 3 个失败

首轮集成回归（9 文件）`3 failed, 57 passed`，三个失败全在 `test_chat_multi_step.py`，同一原因：

```
assert sorted(r.purpose for r in usages) == ["answer", "nl2sql", "nl2sql", "step_plan"]
E  At index 1 diff: 'multistep_global_filter' != 'nl2sql'
```

**不是测试该改，是断言停在旧调用序**：H1 把一个**真实存在的第 5 次 LLM 调用**（B 层全局过滤抽取）纳入计量，LLM 确实调用了、token 确实花了，台账就该多这一行。三处精确列举型断言补上 `multistep_global_filter`，并加断言「该行 `prompt_tokens > 0`」把「真花钱」而不是「有个字符串」钉住。修后重跑：`12 passed`；再跑全套 9 文件：`60 passed`。

**为什么前一轮没发现**：H1 当时的绿灯只覆盖了新建的 `test_multistep_global_filter.py`（61 passed），没重跑隔壁 `test_chat_multi_step.py`。

### 全量单元套件

`2 failed, 2239 passed, 1 skipped`。两个失败**均已证明为预存**：在 HEAD（`4e84753`）的临时 worktree 中单独复跑，两者同样失败。

- `test_chat_service.py::TestSearchByKeywordAdsWeighting::test_weight_from_system_config_db_value` —— 根因：`_selectRelevantClasses` 里的 ADS 加权重排（step D）**被后续 `_rankByLayer`（feat-layer-priority）整体覆盖**，加权分只用于排序 `weightedHits`，随后 `relevant` 又被按「ADS > DWS > DWD > DIM」重排，测试断言的加权顺序不可能成立。同组另 3 条用例之所以绿，只是因为层优先恰好与加权顺序一致。**加权逻辑在生产上已是死代码**，待单独裁决（要么删测试与加分代码，要么把 ADS 加权折叠进 `_rankByLayer` 的层内排序）。
- `test_dependencies.py::test_stub_disabled_raises_permission_denied` —— `AttributeError: 'Header' object has no attribute 'lower'`（`app/dependencies.py:108`），Starlette/FastAPI 版本漂移，与本批无关。

## 5. code-reviewer 复审（2026-09-26）

审出 3 条，全部处理（0 CRITICAL / 1 HIGH / 1 MEDIUM / 1 LOW）。

### HIGH —— L4 中途失败时，已花掉的 token 凭空消失（已修）

`run_agent_loop` 的 while 体没有异常包容，任一轮（或 `_executePendingToolCalls`）抛错就穿透到
`_runL4AgentLoop` 的 `except` → `return None`，而 `_recordUsage` 在那之后——**前面若干轮累积的
token 与成本随之丢失**。与 H2 是同一类缺陷（花钱不记账），只是发生在失败路径上；`AgentLoopResult`
里那个被文档和调用方 `_maybeRunL4AgentLoop` 检查着的 `terminated_reason="error"`，其实**从未被真正
产生过**。

- RED：`test_l4_mid_loop_failure_still_meters_spent_tokens`（第 1 轮花钱并请求执行只读 SQL，第 2 轮抛
  `LlmClientError` 模拟限流）→ `AssertionError: 失败路径同样必须落台账，实际 0 行`，traceback 显示
  异常从 `agent_runtime_service.py:628` 一路穿到 `chat_service.py:728` 的 `except`。
- GREEN：while 循环体包 try/except，异常收敛为 `terminated_reason="error"` 后正常返回累计值。
  **行为不变**（error → `answer_text` 为 None → 照旧降级 L2/L3），只是账不再丢。3 passed。

### MEDIUM —— doc_qa 低分短路的 citations 落库无测试（已补闸，行为保留）

H8 把短路分支的落库从 `[]` 改成真实 `citations`（对齐 `wiki_qa_service.py:157`），但没有任何用例断言
**落库**后的 citations（既有用例只断言 SSE 事件里的）。

- 补 `test_answer_stream_low_score_persists_real_citations`：在 HEAD worktree 上复跑**失败**
  （`assert None is not None`——旧代码 `[] or None` 落成 None），确认它是真闸而非摆设。
- 保留该行为而非回退，理由有二：SSE 事件里的 citations 本来就在阈值判断**之前**发给了前端（第 4 步），
  只在落库时抹成空会让历史消息与前端看到的依据不一致；且 `wiki_qa` 在同一分支就是持久化真实 citations。
- ⚠️ 留一个**产品层面**的待裁：模板文案是「未在已上传文档中找到相关依据」，却挂着低分 citations。这个
  矛盾在 `wiki_qa` 同样存在（属既有设计），不是本批引入；若产品决定「无依据就不该显示依据」，应两个
  service 一起改。

### LOW —— doc_qa 台账写 0/0 假行（已修）

`rag_qa_service` 的 `recordUsage` 无条件调用：客户端整条流都没报 usage（退化流）时会写
`total_tokens=0, cost=0` 的行，把 `total_requests` 灌水，也让「台账有行」不再等价于「真的调过 LLM」。
其余三处计量点（`_runL4AgentLoop` / `_resolveGlobalFilters` / `_recordDirectUsage`）都守了零用量。

- RED：`test_answer_stream_zero_usage_writes_no_ledger_row` → `assert [<SessionTokenUsage … total=0
  cost=0.000>] == []`。
- GREEN：`if total_pt or total_ct:` 包住落库。9 passed（全文件）。

## 6. 遗留

1. **供应商风险台账行的 `model_config_id` 仍为 `NULL`**。`_handleSupplierRisk` 走的是 `_recordDirectUsage`（该助手按设计写 `model_config_id=None`），而此刻 `llm_config` 已在手上。改走 `_recordUsage` 会更一致，但 `_recordUsage` **不跳过 0 token**——降级路径会写出 0/0 假行，正是 H9 第二条用例明确禁止的。需先给 `_recordUsage` 加零用量守卫，故本批不动。
2. **C3 / C4** 按用户裁决单独立项。
3. C 路径（`_isFollowUpRetryCandidate` 兜底）的 `global_filters` 对称缺口仍未补：非流式 `chat_service.py:955`、流式 `:3233`。

## 7. 教训

1. **跨模块 LLM 调用的返回值必须承载 token 数。** 只要 token 数没有出口，调用方就只能写 0-token marker，而注释会立刻开始说谎（H1）。签名里没有 token 字段的 LLM 封装，等价于一个计量漏洞。
2. **「可降级」契约要在最外层写，不能靠异常兜。** `_handleAgentRun` 改成先解析 LLM 再查 Agent 后，`selectModel([])` 抛出的 `NoAvailableModelError` 把 4 条本来返回友好 400 的集成用例打红了——空候选池必须在解析处就返回 `None`，让「没有模型配置」走降级而不是变成请求级错误。这是被回归测试抓住的，不是设计时想到的。
3. **宽容的假工厂会盖住真实缺陷。** H9 的缺陷 2 藏了这么久，直接原因是既有测试的假工厂「无论传什么都返回客户端」——它比生产工厂更宽容，于是生产上必然失败的路径在测试里永远成功。**假替身必须比生产实现更严格，而不是更宽容。**
4. **改完一处计量，必须重跑隔壁的集成套件。** 新增一次 LLM 调用必然打破精确列举型断言（`sorted(purpose) == [...]`），这类断言本身是好事——它逼你看清新增的调用有没有被计量。但如果只跑新写的那一个文件，绿灯就是假的：本次 3 个失败全都出在没被重跑的邻居文件里。
5. **失败路径也是计量路径。** H2 修好了「正常返回时记账」，却没管「中途抛错时记账」——异常一穿出 `run_agent_loop`，前面几轮的钱就跟着没了。更值得记的是那处**征兆**：`terminated_reason="error"` 被文档写着、被调用方检查着，却从来没有被产生过——**一个「声明了但永不发生」的分支，就是它旁边那条异常旁路的证据**。复核时要专挑这种死声明看。
