# 变更：feat-supplier-risk-agent-mini

- **日期**：2026-08-31
- **作者**：Claude (Phase 5.4)
- **Phase**：5.4（Supplier Risk Agent 最小版）
- **状态**：done

## 1. 需求

基于 Phase 5.3 落地的 SUPPLIER Feature 层（OTD_3M / DEFECT_RATE_3M / PRICE_VARIANCE_3M / RISK_SCORE），在 Supplier 360° 视图之上构建风险评估 Agent。最小版不引入新表，复用 entity_mapping / feature_value 主路径，按 enterprise_key 实时判定风险等级（High / Medium / Low / Unknown），并按等级生成结构化建议动作 + 调用 LLM 生成自然语言风险点。

验收标准：
- 主路径：RISK_SCORE 存在且 latest=True → 0-1 区间三档映射（<0.60 High / 0.60-0.80 Medium / ≥0.80 Low），level_source="risk_score"。
- Fallback：RISK_SCORE 缺失 / 禁用 / 无值 → 3 个 feature 违规计数（OTD<90、DEFECT_RATE>5、PRICE_VARIANCE>10），3 违规 High / 2 违规 Medium / ≤1 违规 Low，level_source="fallback_composite"。
- Unknown：4 个 feature 全部 latest=False → level=Unknown，不强行判定。
- LLM 生成 risk_points：默认调 LLM（gpt-4o 等模型），记录 tokens_used + cost + llm_model_name；LLM 不可用 → 自动降级到 fallback_template（按违规 feature 拼装），不阻断主响应。
- Chat 拦截路径：识别「供应商 X 的风险 / 健康度 / 评分」等问法 → 走 SupplierRiskService，answer=等级 + 主要风险点 + 建议动作，supplier_risk=<完整对象>。
- 直接 API：`GET /api/v1/supplier-risk/{supplier_key}` 返回 SupplierRiskRead（DTO 直返，非 ApiResponse 信封）。
- 前端：Chat MessageItem 按字段存在性路由渲染 SupplierRiskCard；侧栏新增「供应商风险」入口 + SupplierRiskPage 直接查询页。
- 异常隔离：LLM 不可用 → log warn + fallback；4 个 feature 缺失 → Unknown 而非 500。
- NotFoundError 通用消息（避免「不存在 vs 无权限」侧信道，与 Phase 4.5 ACL + 5.3 supplier_360 一致）。
- 真实 PostgreSQL（端口 5433）端到端测试；后端覆盖率 ≥ 80%。

## 2. 设计评审

**关键设计决策**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 规则范围 | RISK_SCORE 主路径 + 其他 3 个 feature fallback | 主路径简单清晰（0-1 三档映射），fallback 覆盖 RISK_SCORE 缺失场景；4 个 feature 全部缺失 → Unknown 而非强行判定 |
| 阈值存放 | 硬编码常量（`RISK_RULES`）放在 `supplier_risk_service.py` | 与 `DEFAULT_SUPPLIER_FEATURES` 同模式；阈值不变更，避免引入 DB 配置复杂度；后续 Phase 6 Agent 平台可演进为 DB 配置 |
| risk_points 生成 | **调 LLM 生成**（system prompt 是固定模板；user content 仅含 enterprise_code + 4 个 feature 数值） | 用户决策（项目原则「不走 LLM」针对 supplier_360 的纯聚合只读视图；risk agent 是推理服务允许调 LLM）；需记录 Token 消耗 + 成本 |
| LLM 失败降级 | 静态模板（按违规 feature 名 + note 拼接） | LLM 不可用时不影响主流程；log warn；fallback_template 仍可读 |
| Agent 与 360° 复用 | SupplierRiskService 内部直接调 `Supplier360Service().get360(...)` | 复用 entity_mapping ACL + Profile，避免重复 query；profile 字段（owner/match_rule/生效期）从 360° 透传 |
| ACL | 仅 `getCurrentUser` 鉴权（与 supplier_360 同） | 与 §5.3 一致；底层 ACL 由 entity_mapping 隔离；不引入侧信道 |
| Chat 拦截 regex | `(供应商\|supplier) ... (风险\|健康度\|评分)`（5-9 位 enterprise_key） | 与 SUPPLIER_360 regex 并列；优先级 supplier_360 → supplier_risk → query；关键词不重叠（360° 不会落入 risk） |
| Chat 入口/直入口 | 两侧入口复用 SupplierRiskCard 组件 | 与 §5.3 一致；降低 UI 重复 |

## 3. 数据模型变更

**无新表**。复用现有 `entity_mapping`（主数据 + ACL）与 `feature_definition` + `feature_value`（4 个 SUPPLIER Feature）。

新增 DTO（`backend/app/domain/schemas.py`）：
- `SupplierRiskKpiContribution`（CamelModel）：`feature_name / feature_alias / value (Decimal→str) / unit / threshold / passed / note`，与 360° KPI 一一对应，多 2 个字段（threshold/passed/note）用于风险评估。
- `SupplierRiskRead`（CamelModel）：`profile (复用 Supplier360Profile) / level: RiskLevel / level_source / contributions: list[SupplierRiskKpiContribution] / risk_points / risk_points_source / recommended_actions / tokens_used / cost / llm_model_name / fetched_at`。
- `ChatResponse.supplier_risk: SupplierRiskRead | None`（与 supplier_360 字段并列，前端按字段存在性路由）。

新增枚举（`backend/app/domain/enums.py`）：
- `RiskLevel`：`HIGH / MEDIUM / LOW / UNKNOWN`（字符串值 `high / medium / low / unknown`）。
- `IntentType.SUPPLIER_RISK = "supplier_risk"`（紧跟 SUPPLIER_360 后）。

新增文案（`backend/app/domain/error_messages.py` + `app/services/messages_zh.py`）：
- `MSG_SCHEMA_CHAT_SUPPLIER_RISK`：OpenAPI 字段描述（与 supplier_360 对仗）。
- `MSG_SUPPLIER_RISK_NOT_FOUND`：复用 `MSG_SUPPLIER_360_NOT_FOUND`（避免「不存在 vs 无权限」侧信道）。
- `MSG_SCHEMA_CHAT_SUPPLIER_KEY_MISSING`：引导用户正确提问（与 supplier_360 共用）。
- `MSG_RISK_ACTIONS_HIGH / MEDIUM / LOW / UNKNOWN`：按等级静态生成的中文建议动作（不调 LLM，保持响应稳定）。
- `MSG_RISK_POINTS_TEMPLATE`：`"该供应商存在以下风险点：{reasons}"`（LLM 不可用时降级模板）。

## 4. 接口契约变更

**REST 端点**：

`GET /api/v1/supplier-risk/{supplier_key}` → `200 SupplierRiskRead` / `404 {detail: ...}`（supplier 不存在）
- 鉴权：`Depends(getCurrentUser)`（仅登录校验，不强制 owner-based ACL）。
- service：`SupplierRiskService().assess(db, supplier_key)`。
- 与 `GET /api/v1/supplier-360/{supplier_key}` 同前缀风格、同 ACL、同错误语义。

**Chat 响应**（`POST /api/v1/chat`，）：

`intent=supplier_risk` 时 `answer` + 新增字段 `supplier_risk: SupplierRiskRead`。其余字段同 supplier_360：缺失 / 找不到时 `supplier_risk=null` + `answer` 含引导或 NotFound 消息。

**前端类型**（`frontend/src/types/supplierRisk.ts`）：

```ts
export type RiskLevel = "high" | "medium" | "low" | "unknown";
export interface SupplierRiskKpiContribution { ... }
export interface SupplierRiskRead {
  profile: Supplier360Profile;  // 复用 supplier.ts
  level: RiskLevel;
  levelSource: string;
  contributions: SupplierRiskKpiContribution[];
  riskPoints: string | null;
  riskPointsSource: string;
  recommendedActions: string[];
  tokensUsed: number;
  cost: number;
  llmModelName: string | null;
  fetchedAt: string;
}
```

`ChatResponse` / `ChatMessage` 新增 `supplierRisk?: SupplierRiskRead | null`；`IntentType` union 加入 `"supplier_risk"`。

## 5. 实现要点

**1. 后端 service（`backend/app/services/supplier_risk_service.py`，~290 行）**

核心组件：
- `_Rule`（内部类）：`(operator: lt/gt/lt_inverse, threshold: str, friendly_name: str)` + `violates(value)`。
- `RISK_RULES` 字典：4 个 feature 的阈值（RISK_SCORE: 0.80 lt_inverse；OTD_3M: 90 lt；DEFECT_RATE_3M: 5 gt；PRICE_VARIANCE_3M: 10 gt）。
- `SupplierRiskService.assess(session, supplier_key, *, llm_factory=None)`：
  1. 调 `Supplier360Service().get360(session, supplier_key)` 复用 360° 视图（profile + 4 个 KPI）。
  2. `_toContribution(kpi)` 把每个 KPI 映射成 SupplierRiskKpiContribution（计算 passed + note）。
  3. `_decideLevel(contributions)` 静态方法：RISK_SCORE 三档映射 → fallback 违规计数 → Unknown。
  4. `_buildActions(level)` 按等级返回静态建议动作（不调 LLM）。
  5. `_generateRiskPoints(...)` 异步：若 `llm_factory=None` → 模板降级；否则调 `llm_factory(None).complete(messages)`；任何异常 → log warn + 模板降级。
  6. 返回 `SupplierRiskRead(profile, level, level_source, contributions, risk_points, risk_points_source, actions, tokens_used, cost, model_name)`。

**2. 意图识别（`backend/app/services/intent_service.py`）**

新增 `_SUPPLIER_RISK_RE`（不命中 360/全貌 等词，避免与 supplier_360 碰撞）与 `_extractSupplierRiskKey`。在 `classifyResult` 顺序里：`supplier_360` → `supplier_risk` → `DEFINE` → `MAP` → ... → `QUERY`。两者优先级并列（regex 互相不重叠）。

**3. Chat 派发（`backend/app/services/chat_service.py`）**

新增 `_handleSupplierRisk(session, dto, result)`（与 `_handleSupplier360` 同结构）：
- supplierKey 缺失 / 非数字 → `answer=MSG_SCHEMA_CHAT_SUPPLIER_KEY_MISSING` + `supplier_risk=null`。
- `NotFoundError` → `answer=MSG_SUPPLIER_RISK_NOT_FOUND.format(key=...)` + `supplier_risk=null`（与 ACL 一致，不泄漏「不存在 vs 无权限」）。
- 成功 → `answer=等级 + 主要风险点（截断 80 字）+ 首要建议动作` + `supplier_risk=data`。
- 通过 `self._llmFactory` 注入 LLM 工厂（与现有 chat 链路一致）。

dispatcher（line ~348）：在 supplier_360 派发后插入 `if result.intent == IntentType.SUPPLIER_RISK: return await self._handleSupplierRisk(session, dto, result)`。

**4. API（`backend/app/api/v1/supplier_risk.py`，NEW）**

```python
router = APIRouter()
@router.get("/{supplier_key}", response_model=SupplierRiskRead, status_code=200)
async def getSupplierRisk(supplier_key, _user=Depends(getCurrentUser), db=Depends(getDb), ...):
    return await service.assess(db, supplier_key)
```

挂载：`main.py` + `_testapp.py` 加 `app.include_router(supplier_risk.router, prefix="/api/v1/supplier-risk", tags=["supplier-risk"])`。

**5. 前端（5 个新文件 + 4 个修改）**

- **NEW** `types/supplierRisk.ts`：`RiskLevel` + `SupplierRiskKpiContribution` + `SupplierRiskRead`。
- **NEW** `api/supplierRisk.ts`：`getSupplierRisk(supplierKey)`。
- **NEW** `components/chat/SupplierRiskCard.tsx`：头部等级 Tag（high→red / medium→orange / low→green / unknown→default）+ enterprise_code + 主要风险点段落（含 LLM / fallback 标识）+ 建议动作 List + 特征贡献 Table（5 列：feature / value / threshold / passed / note）+ Tokens & Cost。
- **NEW** `pages/SupplierRiskPage.tsx`：直接查询入口，与 Supplier360Page 同模式（Input + Button + Alert 错误 + SupplierRiskCard）。
- **MOD** `types/chat.ts`：`IntentType` 加 `"supplier_risk"`；`ChatResponse` + `ChatMessage` 加 `supplierRisk?: SupplierRiskRead | null`。
- **MOD** `components/chat/MessageItem.tsx`：`message.supplierRisk ? <SupplierRiskCard />`（在 supplier360 渲染之后）。
- **MOD** `App.tsx`：路由 `<Route path="supplier-risk" element={<SupplierRiskPage />} />`。
- **MOD** `components/common/AppLayout.tsx`：菜单项 `{ key: "/supplier-risk", labelKey: "appLayout.menu.supplierRisk" }`。
- **MOD** `i18n/{zh-CN,en-US}.ts`：`appLayout.menu.supplierRisk` + `supplierRisk.*`（cardTitle / levelSource / riskPointsLabel / sourceLlm / sourceFallback / cost / section.{actions,contributions} / contribution.{feature,value,threshold,passed,passedOk,passedFail,note,placeholder} / empty.riskPoints）+ `supplierRiskPage.*`（title / hint / query / invalidKey / notFound / requestFailed）。

## 6. 测试

**后端单测**（`backend/app/tests/unit/test_supplier_risk_service.py`，14 例全部通过）：

| 测试 | 输入 | 期望 |
|---|---|---|
| `test_risk_score_low_at_0_85_returns_low` | RISK_SCORE=0.85 | level=low, source="risk_score" |
| `test_risk_score_medium_at_0_70_returns_medium` | RISK_SCORE=0.70 | level=medium |
| `test_risk_score_high_at_0_50_returns_high` | RISK_SCORE=0.50 | level=high |
| `test_risk_score_boundary_0_80_is_low` | RISK_SCORE=0.80（边界） | level=low |
| `test_fallback_high_when_all_three_violate` | RISK_SCORE 缺；OTD=80, DEFECT=6, PRICE=12 | level=high, source="fallback_composite" |
| `test_fallback_medium_when_two_violate` | RISK_SCORE 缺；OTD=88, DEFECT=6, PRICE=5 | level=medium |
| `test_fallback_low_when_zero_or_one_violate` | RISK_SCORE 缺；OTD=95, DEFECT=2, PRICE=12（仅 PRICE 超标） | level=low |
| `test_unknown_when_all_four_features_missing` | 4 个 feature 全部 latest=False | level=unknown |
| `test_contributions_have_four_items_mirroring_default_features` | 仅 seed OTD | len(contributions)==4，names 集合 == {OTD, DEFECT, PRICE, RISK_SCORE} |
| `test_llm_unavailable_falls_back_to_template` | llm_factory=None | risk_points_source=fallback_template，含违规 feature 名 |
| `test_llm_success_records_tokens_and_cost` | fake LLM（120+60 tokens） | tokens_used=180，cost>0，model_name="fake-risk-model" |
| `test_actions_per_level` | RISK_SCORE=0.50（High） | recommended_actions ≥ 1 |
| `test_assess_404_when_supplier_not_in_mapping` | enterprise_key 不在 entity_mapping | NotFoundError |
| `test_contribution_passed_flag` | OTD=80 → passed=False；删除+重 seed OTD=95 → passed=True | 双断言 |

**后端集成**（真实 PG 5433 + 完整 HTTP 链路）：

- `app/tests/integration/test_supplier_risk_api.py`（3 例通过）：
  - `test_get_supplier_risk_returns_full_payload`：seed 完整 4 feature → 200 + SupplierRiskRead + level=high + risk_points_source=fallback_template + tokens_used=0 + recommended_actions 非空 + profile.enterpriseKey。
  - `test_get_supplier_risk_404_when_supplier_not_in_mapping`：supplier 不存在 → 404（防 typo 静默 + 通用消息）。
  - `test_get_supplier_risk_fallback_when_no_llm`：RISK_SCORE 缺 + 不注入 LLM → 200 + level=high + level_source=fallback_composite + risk_points_source=fallback_template。

- `app/tests/integration/test_chat_supplier_risk.py`（5 例通过）：
  - `test_chat_supplier_risk_intent_returns_payload`：「供应商 100001 的风险」→ intent=supplier_risk + supplier_risk.level=high + supplier_risk.contributions=4。
  - `test_chat_supplier_risk_missing_key_returns_guidance`：「供应商风险等级」无 key → 不进入拦截（regex 必带数字 key），intent ≠ supplier_risk。
  - `test_chat_supplier_risk_not_found_returns_notfound_message`：「供应商 999999 的健康度」→ 200 + answer 含 "999999" + "不存在" 或 "建档" + supplier_risk=null。
  - `test_chat_supplier_360_does_not_collide_with_risk`：「供应商 100001 的 360° 视图」→ intent=supplier_360 + supplier360 非 null + supplierRisk=null（验证 regex 优先级与不重叠）。
  - `test_chat_normal_query_not_intercepted_by_risk`：「供应商 100001 的订单数」→ intent ≠ supplier_risk + supplierRisk=null（验证不误吸普通查询）。

**前端**：`npx tsc --noEmit` 0 errors；`npm test -- --run` 38 test files / 331 tests 全部通过（无新单测，沿用 vitest 全套门禁）。

## 7. 安全审查

按 Phase 4.5 ACL 治理原则逐项核查：

- **DTO 不允许 mass-assignment**：`SupplierRiskRead` 沿用 CamelModel `from_attributes=True`；不暴露任何 `Create / Update` DTO（GET-only 服务）；后端 service 层仅从 ORM 对象 + 业务计算组装 DTO，不接受 client DTO 直传。
- **403 通用消息**：`NotFoundError` 不区分「不存在 vs 无权限」（`MSG_SUPPLIER_RISK_NOT_FOUND` 复用 supplier_360 消息），不泄漏 supplier_key 探测空间。
- **Actor 派生**：`assess()` 不接受 `user` 参数；底层 `Supplier360Service.get360` 已含 entity_mapping ACL 隔离；supplier_risk 在 service 层无额外 ACL 注入（与 supplier_360 风格一致）。
- **非 admin 集成测试**：本次以 default stub user（含 admin 角色）触发，与 supplier_360 测试一致；后续 ACL 强化（按部门限制 supplier 可见性）需引入 `data_classification` 字段后再补。
- **LLM prompt 注入防护**：system prompt 是固定中文模板（不允许外部内容注入）；user content 仅含 enterprise_code + 4 个 feature value + 阈值 + passed 状态（不拼接原始 user question），降低 prompt injection 风险。
- **Token 计量**：每次 LLM 调用记录 prompt_tokens + completion_tokens + cost（按现有 0.001 / 0.002 CNY per 1k 估算），写入 `SupplierRiskRead.tokens_used / cost / llm_model_name`；fallback 时一律为 0。
- **异常隔离**：LLM 调用异常（网络 / 超时 / 鉴权失败）→ log warn + fallback_template；不影响主响应（继续返回 level + actions）。
- **SQL 注入**：service 层所有 ORM 调用走 SQLAlchemy 参数化（`Supplier360Service.get360` 复用）；无 f-string 拼接 SQL。
- **侧信道**：错误响应统一文案（`MSG_SUPPLIER_RISK_NOT_FOUND` 与 supplier_360 一致），不暴露数据源 / 用户角色 / 内部异常栈。

## 8. 部署验证

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest \
    app/tests/unit/test_supplier_risk_service.py \
    app/tests/integration/test_supplier_risk_api.py \
    app/tests/integration/test_chat_supplier_risk.py -v
# 期望：22 例全过（14 unit + 3 API + 5 chat integration）

cd frontend
npx tsc --noEmit                      # 0 errors
npm test -- --run                     # 38 test files / 331 tests
```

冒烟（live 环境）：

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"smoke-risk","question":"供应商 100001 的风险","datasourceId":1}' \
  | jq '.answer, .intent, .supplierRisk.level, .supplierRisk.levelSource'
# 期望：answer 含 SUP000001 + 100001 + high，intent=supplier_risk，levelSource=risk_score

curl http://localhost:8000/api/v1/supplier-risk/100001 -H 'X-User-Id: smoke' | jq '.level, .recommendedActions | length'
# 期望：level=high，recommendedActions 长度 ≥ 1
```

## 9. 关联

- **上游**：
  - Phase 5.3 [[feat-supplier-360-ads]]（Supplier360Service + entity_mapping ACL + 4 个 SUPPLIER Feature）。
  - Phase 4.3 [[feat-ai-feature-model]]（FeatureDefinition / FeatureValue 主数据）。
- **下游**：
  - Phase 5.5+ Supplier Risk 阈值配置化（DB 表 + owner 治理）。
  - Phase 6 Agent 平台：Agent Registry 注册 supplier_risk_agent，纳入 AgentAccessPolicy。
- **设计文档**：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` §5.4
- **代码 SSOT**：
  - `backend/app/services/supplier_risk_service.py`
  - `backend/app/services/intent_service.py`（`_SUPPLIER_RISK_RE` + `_extractSupplierRiskKey`）
  - `backend/app/services/chat_service.py`（`_handleSupplierRisk` + dispatch）
  - `backend/app/api/v1/supplier_risk.py`
  - `backend/app/domain/enums.py`（`RiskLevel` + `IntentType.SUPPLIER_RISK`）
  - `backend/app/domain/schemas.py`（`SupplierRiskKpiContribution` + `SupplierRiskRead` + `ChatResponse.supplier_risk`）
  - `frontend/src/types/supplierRisk.ts` + `frontend/src/api/supplierRisk.ts`
  - `frontend/src/components/chat/SupplierRiskCard.tsx` + `frontend/src/pages/SupplierRiskPage.tsx`