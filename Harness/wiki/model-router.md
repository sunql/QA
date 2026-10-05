# 模型路由

## 路由算法（`services/model_router_service.py`）

`selectModel(configs, prompt, ctx)` 依次：

1. **过滤启用**：`is_active == True`，否则抛 `NoAvailableModelError`。
2. **预算降级**：若 `ctx.sessionCost >= SESSION_BUDGET`，直接返回最便宜模型（按 `cost_per_1k_input`）。
3. **会话亲和**：若 `ctx.sessionTurnCount < SESSION_AFFINITY_TURNS`(默认3) 且 `ctx.priorModelId` 仍启用，沿用上一轮模型。
4. **成本阈值过滤**：估算各模型 prompt token 数，`sessionCost + estPromptCost < cost_threshold` 才入候选池。
5. **加权随机**：候选按 `weight` 加权随机（权重 0 不被选中，当存在正权重候选时）。
6. **全超限降级**：候选为空时返回最便宜模型。

## 模型降级重试（5.4）

`selectFallbackModel(configs, excludeId) -> config | None`：选出可用的**最便宜**备选模型，排除刚失败的 `excludeId`；无启用候选时返回 `None`。不修改入参 configs。

`ChatService._callWithFallback(session, sessionId, configs, primary, purpose, caller)`：
1. 先用 `primary` 调用 `caller`（由 `llmFactory` 构造客户端）。
2. 抛 `LlmClientError` 时记录 **zero-token** 审计 usage（`purpose="fallback_<原purpose>"`），再降级到 `selectFallbackModel(configs, primary.id)` 重试一次。
3. 返回 `(结果, 实际服务模型)`——成功调用的 Token/成本按实际服务模型计量。
4. 无可用备选时向上抛原始 `LlmClientError`（不静默吞错）。

**应用范围**：NL2SQL 与自然语言回答两个步骤。图表步骤**不**走模型降级——`ChartService.generateChartOption` 内部捕获所有异常并回退到规则生成 option（`chart_service.py`），外部降级包装对其为死代码，故排除。

## 成本估算

```
estPromptCost = tokenCount(modelName, prompt) * cost_per_1k_input / 1000
```

预调用阶段仅能估算输入成本；实际成本由 LLM 响应的 usage（`prompt_tokens`/`completion_tokens`）经 `TokenUsageService.recordUsage` 记录。

## Token 计数策略

| Provider | 计数器 | 说明 |
|----------|--------|------|
| OpenAI / Azure / 兼容代理 | `TiktokenCounter` | tiktoken；未知模型回退 cl100k_base/o200k_base |
| Ollama | `HeuristicCounter` | CJK ~1 token/字，非 CJK ~4 字符/token，标记 `isApproximate=True` |

工厂 `infrastructure/token_counter/factory.py` 按 `ProviderType` 缓存单例。

## LLM 客户端抽象

`infrastructure/llm/base_client.py`：统一 `complete(messages) -> LlmResponse`。
- `OpenAiClient`：覆盖 OpenAI/Azure/代理，按 provider 构造 `AsyncOpenAI` 或 `AsyncAzureOpenAI`，支持 `base_url`。
- `OllamaClient`：`httpx` 调 `/api/chat`，解析 `prompt_eval_count`/`eval_count`。
- `factory.py`：按 `LlmConfig.id` 缓存客户端，API Key 优先解密配置密文，否则回退环境变量。
  **缓存失效**：`OpenAiClient` 构造时固化 model_name/api_endpoint/key，配置编辑/停用必须调
  `invalidateClient(configId)`（`model_config_service` 的 update/deactivate 已接线），否则
  编辑对运行中的进程永不生效（2026-10-03 MiniMax Connection error 根因）。

## 答案后处理：Think_Hide（2026-10-03）

推理模型（MiniMax-M3 等）把 `<think>…</think>` 思维链内联在答案正文。系统参数
`Think_Hide`（system_config，迁移 0108 补种 `'0'`）：`1` 在所有 LLM 答案出口剥离
（chat 单步/多步汇总/流式/doc_qa/wiki chat，`services/think_block.py`），`0`/缺省字节级透传。
流式用逐字符状态机增量过滤，下发 token 与落库 assistant 消息一致；读取无缓存、每次现读，
admin 改值即时对新请求生效。

⚠️ **`Think_Hide` 只管「给用户看什么」，不管「机器能读什么」**。内部 LLM 消费
（计划 JSON / SQL 生成）自 2026-10-03 起**无条件剥离** think 块，与该参数无关
（见下节）—— 这两者正交，勿把解析层的剥离"优化"成读参数。

## 关闭推理模型思维链：`disable_thinking`（2026-10-03）

`llm_config.disable_thinking`（迁移 0109，`server_default=false`）：**按模型配置单独勾选**，
由 `OpenAiClient._applyThinkingPolicy` 转成 `extra_body.thinking.type=disabled` 透传，
作用于该模型的全部调用（`complete` / `completeStream` / `complete_with_tools`）。

**为什么需要**：实测 MiniMax-M3 把 **91.3%** 的 token 花在思维链上（think 5,829 / JSON 558），
挤爆 NL2SQL 计划阶段 2048 的预算 ⇒ 回复被截断在 JSON 之前 ⇒ chat HTTP 400。
关闭后同prompt 只需 509~907 tokens（省 86-92%），解析 4/4 成功。

三个易错点（改动时勿破坏）：
1. **必须用 `extra_body`**，顶层 `thinking` kwarg 被 openai SDK 拒
   （`unexpected keyword argument`）。
2. **merge 不覆盖**调用方的 `extra_body` —— 调用方可能自带 `reasoning_split`
   （M2.7 不传可能返回空响应）；`payload.update(kwargs)` 在注入之后执行。
3. 构造期固化 + `update` 已调 `invalidateClient` ⇒ 勾选后即时对新请求生效。

**配套**：计划阶段补了截断退避（`nl2sql_plan.py`，封顶 4096）。它是**不支持关闭 thinking
的模型（M2.x）的唯一防线**——4096 < 实测需求 6387，对 M3 靠的是关闭 thinking 而非退避。

⚠️ **代价**：关闭 thinking 后计划质量可能下降（实测 M3 从「单表 + 干净 `SUPPLIER_CODE = 'B019'`」
变成「多表 + `BPSNUM_0 = 'B019' 或 BPSNAM_0 = '圣特公司'`」），更容易撞本体连通性校验失败。
这正是做成按模型勾选而非全局开关的原因：风险由用户按模型自行承担。

SSOT：`Harness/changes/fix-reasoning-model-token-budget/summary.md`

## 关键参数

- `SESSION_BUDGET`：会话累计成本上限（美元），超则降级。
- `SESSION_AFFINITY_TURNS`：会话亲和轮数。
- `cost_threshold`：单模型单次成本熔断阈值。
