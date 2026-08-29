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

## 关键参数

- `SESSION_BUDGET`：会话累计成本上限（美元），超则降级。
- `SESSION_AFFINITY_TURNS`：会话亲和轮数。
- `cost_threshold`：单模型单次成本熔断阈值。
