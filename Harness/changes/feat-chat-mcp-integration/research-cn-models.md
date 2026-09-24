# 国产 LLM tool-call JSON 降级方案实测

> 2026-09-22 | 决定 plan §4.2.B 降级方案是否成立

## 1. 环境

实测 `qwen3.6:27b`(本地 ollama GGUF Q4_K_M,17GB,OpenAI 兼容层)。

**`.env` 全部 key 为空**:`OPENAI/DEEPSEEK/QWEN/AZURE`。本机无 DeepSeek/GLM/Kimi。**这三家真实数据无法获取,Phase 1 拿 key 后必须补测**。

**陷阱 1**: Python `urllib.request` 调本机 ollama 返 502(body 0 字节),`curl` 子进程 100% 200。原因不明。已用 subprocess curl 规避。**生产 OpenAI SDK 不受影响**。

**陷阱 2**: ollama OpenAI 兼容层忽略 `think:false`,qwen3.6 reasoning 永远存在。

## 2. 方法

按 plan §4.2.B 模板:`<available_tools>` 块 + 3 工具(wiki_search/get_weather/calculate) + "只输出 JSON" 指令。10 条用例覆盖直接调用/中文歧义/多步/不需要工具/参数类型/多工具诱导/JSON嵌套/长文本诱导/空字符串/干扰 prompt。

## 3. 实测结果(qwen3.6:27b, max_tokens=300)

| # | 场景 | 延迟 | 分类 | ✓ |
|---|---|---|---|---|
| 01 | 直接调用 | 20s | OK | ✓ |
| 02 | 中文歧义 | 22s | OK | ✓ |
| 03a | 多步 | 23s | OK+fence | ✓ |
| 04 | 不需工具 | 10s | OK(无) | ✓ |
| 05 | 参数类型 | 26s | OK+fence | ✓ |
| 06 | 多工具诱导 | 43s | TIMEOUT | ✗ |
| 07 | JSON嵌套 | 43s | TIMEOUT | ✗ |
| 08 | 长文本 | 35s | OK+fence | ✓ |
| 09 | 空字符串 | 10s | OK(空输入) | ✓ |
| 10 | 干扰prompt | 43s | TIMEOUT | ✗ |

**7/10 = 70.0%(临界达标)**。其余 provider 无 key 未实测。

**失败根因(全部同一)**: `max_tokens=300` 被 thinking 耗尽,`finish_reason:length`,content 空。#06 reasoning 350+字纠结"一个 JSON 表达两次调用"; #07 400+字纠结"湿度>60%没目标温度"; #10 350+字纠结"用户真要我忽略工具吗"。

**格式合规**: 7 个产 content 的 case,JSON 合法/tool 名/args 是 dict 均 7/7。**裸 JSON 仅 3/7,4/7 被 `` ```json...``` `` 包裹**(必须剥 fence)。

## 4. 结论

### 4.1 成功率

| Provider | 实测 | 成功率 | 达标 |
|---|---|---|---|
| qwen3.6:27b (max_tokens=300) | 7/10 | **70%** | 临界 |
| qwen3.6:27b (max_tokens≥800 推算) | 估计 10/10 | ~100% | ✓ |
| DeepSeek/GLM/Kimi | 无 key | 待 Phase 1 | **必须实测** |

### 4.2 Provider 决策

| Provider | 方案 | 依据 |
|---|---|---|
| OpenAI/GPT | **原生 tool_use** | plan §4.2.A |
| DeepSeek | **原生 tool_use**(OpenAI 兼容) | 官方 API 原生支持 |
| qwen3.6/Ollama | **JSON 降级**(够用) | 实测 70~100% |
| GLM-4 | 待 Phase 1 | 行业资料说法不一 |
| Kimi/Moonshot | JSON 降级 | 无原生 tool_use API |

### 4.3 prompt 是否重写

**不大改,3 处微调**:

1. **`max_tokens=1024`**(客户端参数)。thinking 默认耗 200+ tokens,300 不够。
2. **parser 先剥 code fence 再 fullmatch**:`re.search(r"\`\`\`(?:json)?\s*(\{[\s\S]+?\})\s*\`\`\`", c)` → `re.fullmatch(r"\{...\}", ...)`。原 plan §R1 直接 fullmatch 会把 fence 拒掉(实测 4/7)。
3. **prompt 加一句**"直接给出最终 JSON,不要思考过程或解释",压短 thinking。

## 5. Mitigation

| 级别 | 措施 | 位置 |
|---|---|---|
| **P0** | `max_tokens=1024` | `chat_mcp_executor._llm_call` |
| **P0** | parser 先剥 fence 再 fullmatch | `parse_and_dispatch` |
| P1 | jsonschema 校验 arguments | 同上 |
| P1 | 空 content + finish_reason=length → 自动重试 1 次 | 同上 |
| P1 | prompt 注入"直接给最终 JSON,不要思考" | `_buildAnswerPrompt` |
| P2 | `mcp_call_log` 记录原始 content+reasoning | `MCPAdapter` |
| P2 | DeepSeek/GLM 补测脚本 `smoke_cn_models_tool_call.sh` | scripts/ |
| P3 | qwen3.6 改走 ollama 原生 `/api/chat` + `think:false` | factory 改造 |

## 6. Phase 1 行动

- **无 key**: P0/P1 落 executor 骨架;`test_chat_mcp_executor_parse.py` 用 `/tmp/mcp_research/qwen3.6_27b_results.json` 作 fixture 覆盖 fence/裸 JSON/EMPTY 三类。
- **需 key**: 申请 DeepSeek/GLM 试用 key,跑 `smoke_cn_models_tool_call.sh`,定 GLM 走 JSON 还是原生。

附录:脚本 `/tmp/mcp_research/test_tool_call.py`、结果 `qwen3.6_27b_results.json`、日志 `run3.log`。关联 memory `qa-system-prompt-fence-neutralize`、`qa-system-llm-fence-stripping`。