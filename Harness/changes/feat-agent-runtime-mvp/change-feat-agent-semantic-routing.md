# 变更：feat-agent-semantic-routing

- **日期**：2026-09-01
- **阶段**：Phase 7 G4（Agent Runtime 遗留缺口）
- **类型**：功能补强（Chat 未指名 Agent 语义路由）
- **提交**：`feat: chat agent semantic routing`

---

## 1. 问题

Agent Runtime 的 AGENT_RUN 意图只有两种触发方式：显式指名（`用 supplier_risk_agent
评估供应商 100001`，`_AGENT_RUN_RE`）与既有确定性意图（360°/risk/graph 拦截）。
用户若用**自然语言**表达 Agent 诉求但**不指名 Agent**（如「供应商 100001 是否可靠合规」、
「供应商 100001 表现怎么样」），当前一律走 NL2SQL 普通查询，无法享受 Agent 能力。

`feat-agent-runtime-mvp` §10 列为已知缺口；Phase 7 gap plan G4（P1）。

## 2. 设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 增量 vs 覆盖（用户确认） | **增量**：路由只在既有 360/risk/graph/METRIC/DEFINE 等确定性拦截**均未命中**后触发 | 覆盖式会劫持既有稳定意图（如「供应商 100001 的风险等级」已被 supplier_risk 拦截），破坏 Phase 5.3/5.4/6.3 行为 |
| 评分机制 | 纯关键词命中数：`confidence = min(1.0, 命中数 × 0.4)`，不调用 LLM | 复用 Phase 5.3 领域词汇，零 Token 成本；关键词静态、无注入面 |
| 置信阈值 | 高 ≥0.7 → AGENT_RUN（自动调度）；中 0.4 ≤ x < 0.7 → QUERY + `suggested_agent` 建议卡片；低 <0.4 → 无建议 | 双关键词（如「评估…风险」）才自动跑，单关键词（如「表现」）只建议，避免误调度 |
| 实体门禁 | 无供应商编码（`extractSupplierAnyKey`）→ 不路由；命中聚合量词（`_SUPPLIER_AGGREGATION_RE`）→ 数值聚合查询不路由 | 防止把 NL2SQL 聚合查询吸走（与图推理兜底同一正则，语义一致） |
| 循环依赖防护 | `AgentRoutingService` 不 import intent_service；`AgentSuggestion` 放 schemas；门禁在 intent_service 调用点施加 | 纯评分与调用方门禁解耦；schemas ← routing ← intent 单向依赖无环 |
| 路由位置 | classifyResult 中 METRIC 检查之后、`hasPriorState` 分支之前 | REFINE/METRIC 等确定性意图不被路由覆盖；FOLLOW_UP/NEW_QUERY/QUERY 均可带建议 |
| 建议透传（非流式） | processMessage 抽出 `_handleGenericQuery`，出口统一 `model_copy(update=suggested_agent)` | 单一出口附加，避免多返回路径重复拼接；不可变风格不改原响应 |
| 建议透传（流式） | `_streamQuery` 增加 `suggestion` 参数，3 个 done 帧统一带 `suggestedAgent` | 默认 UI 全走 `/chat/stream`，done 帧不带则卡片永不渲染（#207 同教训） |
| 前端 | `SuggestedAgentCard`（展示码 + 置信度 + 理由），MessageItem 按字段存在性路由 | 与 supplier360/risk/graph/agent_run 卡片同一路由模式；V1 仅展示，用户可显式指名执行 |

## 3. 数据模型变更

无 DB 迁移。新增 `AgentSuggestion` CamelModel（`recommended_agent_code` /
`confidence` / `reason`），`ChatResponse.suggested_agent` 新字段
（JSON 别名 `suggestedAgent`）。`IntentResult.suggested_agent` 为意图级承载。

## 4. 关键代码

### 后端

- `app/services/agent_routing_service.py`（新增）：
  - `AgentRoutingService.route(message)` 纯关键词评分 → `AgentSuggestion` 或 None；
  - 关键词表 `_AGENT_KEYWORDS`：SUPPLIER_360_AGENT（360/全貌/全景/概况/总览/画像/表现/视图/整体/全维度）、
    SUPPLIER_RISK_AGENT（风险/健康度/评估/靠谱/可靠/合规/信誉/资质/审核/稳定）、
    GRAPH_REASONING_AGENT（关联/链路/上下游/涉及/关系/网络/依赖/路径/物料/下一环）；
  - `HIGH_CONFIDENCE`/`MEDIUM_CONFIDENCE` 常量导出，intent_service 复用决策语义。
- `app/domain/schemas.py`：`AgentSuggestion` + `ChatResponse.suggested_agent`。
- `app/services/intent_service.py`：
  - `_routeSupplierAgent(message)` 门禁（无 key / 聚合量词 → None）+ 调 router；
  - `IntentResult.suggested_agent` 字段；
  - classifyResult 路由器步骤：高置信 → AGENT_RUN(agent_code)；中置信 → 尾部三个
    `_queryResult` 携带 `suggestion`；
  - `_queryResult` 增 `suggestion: AgentSuggestion | None = None` 参数。
- `app/services/chat_service.py`：
  - `_handleGenericQuery(session, dto, result, state)` 抽出 processMessage 通用查询尾部
    （CLARIFY + 多步 + NL2SQL 单步），出口唯一；
  - processMessage 末尾 `model_copy(update={"suggested_agent": ...})` 附加建议；
  - `_streamQuery` 增 `suggestion` 参数，unanswerable / featureResp / 主 NL2SQL 三个
    done 帧带 `suggestedAgent`（`model_dump(mode="json", by_alias=True)`）。

### 前端

- `src/types/chat.ts`：`AgentSuggestion` + `ChatResponse.suggestedAgent` + `ChatMessage.suggestedAgent`。
- `src/api/chat.ts`：`StreamSummary.suggestedAgent`。
- `src/components/chat/SuggestedAgentCard.tsx`（新增）：antd Card，码 Tag + 置信度百分比 + 理由。
- `src/components/chat/MessageItem.tsx`：按 `message.suggestedAgent` 存在性路由渲染。
- `src/stores/chatStore.ts`：done 帧与 `sendChatMessage` 非流式分支均回填 `suggestedAgent`。
- `src/i18n/zh-CN.ts` / `en-US.ts`：`suggestedAgent.*` 键。

### 偏离计划点

- 计划把 `AgentSuggestion` 写成 `agent_routing_service` 内部 dataclass；实现放在
  schemas.py（CamelModel），因 `ChatResponse` 字段与 SSE `model_dump(by_alias)`
  都需要 Pydantic 模型，避免跨层手写 dict 序列化。
- 计划建议卡片无交互；实现增加 i18n 提示「可直接输入『用 SUPPLIER_XXX_AGENT …』
  执行」，把「点击运行」交互留作后续（YAGNI，V1 展示即达目标）。
- 流式透传是计划未显式要求的发现项：G4 调试中发现 `_streamQuery` done 帧不带
  卡片会导致默认 streaming UI 下建议永不出现，补 3 处 done 帧。

## 5. 测试

| 层 | 文件 | 结果 |
|---|---|---|
| 单元 | `test_agent_routing_service.py`（新增） | 14 全绿（评分 5 + 集成 4 + 负向护栏 5） |
| 单元 | `test_intent_service.py` / `test_graph_traversal_service.py` 回归 | 通过 |
| 集成 | `test_chat_agent_run.py`（+5：高置信 AGENT_RUN / 中置信卡片 / 低置信无卡 / 聚合不劫持 / 流式 done 卡片） | 13 passed |
| 集成回归 | `test_chat_api` / `test_chat_stream_api` / `test_chat_multi_step` / `test_chat_feature_rerouting` / `test_chat_supplier_360` / `test_chat_supplier_risk` | 36 passed |
| 集成回归 | `test_chat_service_state` / `test_chat_history_api` / `test_chat_data_quality_integration` / `test_rate_limit_chat_route` / `test_agent_runtime_api` | 54 passed |
| 集成回归 | `test_chat_multi_step`（+1：审查 MEDIUM 回归——多步 done 帧携带 suggestedAgent） | 9 passed |
| 全量单元 | `app/tests/unit/` | 1368 passed, 1 skipped |
| 前端 | `SuggestedAgentCard.test.tsx`（+3）/ `MessageItem.test.tsx`（+2） | 353 passed；tsc --noEmit 0 errors |

关键断言：
- 「供应商 100001 是否可靠合规」→ 高置信 0.8 → intent=agent_run + agentRun 卡片
  （agentCode=SUPPLIER_RISK_AGENT），且不附带 suggestedAgent（已直接调度）；
- 「供应商 100001 表现怎么样」→ 中置信 0.4 → intent=query + suggestedAgent 卡片
  （recommendedAgentCode=SUPPLIER_360_AGENT），非流式与流式均透传；
- 「供应商 100001 关联的采购订单总金额」→ 聚合量词兜底 → 纯查询无卡片；
- 「供应商 100001 的风险等级」仍为 supplier_risk（既有意图不被覆盖）；
- `_NoopLlm` 测试 fake 增 `completeStream`（G4 流式测试走 `_streamQuery` 需要）。

## 6. 安全审查

`security-reviewer` **APPROVE（0 CRITICAL / 0 HIGH）**：
- 授权边界确认：只有硬编码 Agent 编码能进入 `result.agent_code`（关键词表静态、无注入面），
  `_handleAgentRun` 的注册存在性 / READ 策略 / DRAFT 不可运行三重校验对**自动路由**与
  **显式指名**的 Agent 一视同仁——路由只负责「选谁」，放行与否仍由原授权链裁决。
- 无新 DB 访问 / 无新外部调用 / 无 secrets；`suggested_agent` 仅透传，不携带敏感数据。
- 上限护栏：`confidence = min(1.0, 命中数 × 0.4)` 有界；路由纯本地、零 LLM 成本。

`code-reviewer` 双审结果：
- **HIGH → 已修复**：前端 SSE `handleFrame` done 分支此前只回填 4 个字段
  （tokensUsed/cost/modelName/affinityStatus），把所有拦截类卡片对象（agentRun /
  supplier360 / supplierRisk / graphTraversal / suggestedAgent）一并丢弃——不仅是 G4
  建议卡片，Phase 6.4 的 agent_run/360/risk/graph 卡片在默认 streaming UI 也从未渲染。
  `src/api/chat.ts` done 分支改为逐字段转发 5 个卡片字段（与后端 `model_dump(by_alias)`
  形状对齐）。同坑已注入 SSOT 教训：「done 帧必须带卡片，前端按存在性渲染」。
- **MEDIUM → 已修复**：`_streamMultiStep` 的两个 done 帧（聚合成功路径 / 异常降级路径）
  未透传 `suggestedAgent`，与 `_streamQuery` 口径不一致——多步场景下建议卡片不渲染。
  新增 `suggestion` 参数 + 3 处委托点透传 + 2 处 done 帧补发 + 回归测试
  `test_stream_multi_step_done_carries_suggested_agent`。
- **LOW → 记录不修（含理由）**：
  1. 否定语义不匹配：「供应商 100001 不稳定」命中「稳定」关键词 → 0.4 建议卡。
     关键词路由无否定意识，V1 接受（误报仅是建议卡，不自动调度；负向精确化留 NLP 升级）。
  2. 聚合兜底不完全：「供应商 100001 上下游物料库存」中「库存」不在聚合量词表，
     若同时含图关键词可能误路由——实测「上下游物料」已命中图关键词会被图拦截先于路由，
     属既有行为，非 G4 引入；聚合量词表可后续补充。
  3. `SuggestedAgentCard` 置信度 `>= 0.5 ? orange : default` 颜色分支是死代码
     （卡片只会收到 0.4），已移除分支保留文案。
  4. INFO：高置信自动调度无频控，与 Agent Runtime 既有 AGENT_RUN 同频控（LOW#2 的
     auto-run 限流是运行时后续项，不阻塞本 change）。

## 7. 验证

```bash
cd backend
uv run pytest app/tests/unit/test_agent_routing_service.py \
  app/tests/unit/test_intent_service.py app/tests/unit/test_graph_traversal_service.py -q
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_chat_agent_run.py -q
cd ../frontend
npx tsc --noEmit && npx vitest run
```

期望：单元 110 全绿；集成 13 全绿；前端 353 + tsc 0。

## 8. 关键文件清单

- `backend/app/services/agent_routing_service.py`（新增）
- `backend/app/services/intent_service.py`
- `backend/app/services/chat_service.py`
- `backend/app/domain/schemas.py`
- `backend/app/tests/unit/test_agent_routing_service.py`（新增）
- `backend/app/tests/integration/test_chat_agent_run.py`
- `frontend/src/components/chat/SuggestedAgentCard.tsx`（新增）
- `frontend/src/components/chat/MessageItem.tsx`
- `frontend/src/stores/chatStore.ts`
- `frontend/src/types/chat.ts`
- `frontend/src/api/chat.ts`
- `frontend/src/i18n/zh-CN.ts` / `en-US.ts`
- `frontend/src/tests/SuggestedAgentCard.test.tsx`（新增）
- `frontend/src/tests/MessageItem.test.tsx`
