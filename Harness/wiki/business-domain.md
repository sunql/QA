# 业务域

## Demo 数据源

Demo 业务库使用 RuoYi WMS MySQL schema（`/Users/sunql/Prejectcode/Rouyi/sql/`），含仓储管理域表：

| 实体 | 表 |
|------|----|
| 仓库 | `wms_warehouse` |
| 库区 | `wms_zone` |
| 库位 | `wms_location` |
| 物料 | `wms_material` |
| 批次 | `wms_batch` |
| 供应商 | `wms_supplier` |
| 客户 | `wms_customer` |
| 库存 | `wms_inventory` |
| 库存流水 | `wms_inventory_log` |
| 入库单 | `wms_inbound_order` / `_detail` |
| 出库单 | `wms_outbound_order` / `_detail` |
| 分配(FIFO) | `wms_allocation_record` |
| 盘点 | `wms_check_order` / `_detail` |
| 采购申请 | `wms_purchase_requisition` / `_detail` |
| 采购订单 | `wms_purchase_order` / `_detail` |

## 本体映射示例

Phase 2 预置本体：
- 类 `库存` -> `wms_inventory`，属性：仓库、物料、数量。
- 类 `入库单` -> `wms_inbound_order`。
- 指标 `库存总量` = `SUM(wms_inventory.quantity)`。
- 指标 `入库金额` = `SUM(wms_inbound_order_detail.total_amount)`。

## 扩展数据源

支持动态注册（`POST /api/v1/datasource`），可接入 SRM（供应商/采购订单）等其它业务库。

## 采购域业务对象目录（Phase 3.3）

`seed_ontology.py` 现有 27 个类，覆盖采购域核心单据与主数据。Phase 3.3 补齐 3 个缺失业务对象（6 个类），源表与关键属性基于 ZJTH 真实库（`all_tab_columns` 实证）：

| 对象 | 类 | source_table | 关键属性 |
|---|---|---|---|
| QUOT（报价/询价） | `Quotation` / `QuotationDetail` | `PQUOTAT` / `PQUOTATD` | 报价单号、报价日期、响应期限、受邀/响应供应商数；明细：物料、数量、提前期、来源请购行 |
| INV（采购发票） | `PurchaseInvoice` / `PurchaseInvoiceDetail` | `PINVOICE` / `PINVOICED` | 发票号、供应商、含税/不含税金额、到期日、状态；明细：物料、数量、金额、三向匹配关联 |
| PAY（付款） | `Payment` / `PaymentDetail` | `PAYMENTH` / `PAYMENTD` | 付款单号、付款类型、付款金额、付款/到期日期、状态；明细：科目、供应商、被支付凭证 |

### 关键映射与业务流转

- **QUOT 映射修正**：采购域「报价」对应 Sage X3 **采购报价 `PQUOTAT`**（`SQUOTE` 是销售报价——含销售员 `REP_0`、销售订单 `SOHNUM_0`，不属采购域）。`PQUOTAT` 一张单据覆盖「询价 → 报价」两端：表头 `BPSNBR_0`（受邀供应商数）/`RSPNBR_0`（响应供应商数），明细 `PSHNUM_0`/`PSDLIN_0` 引用来源请购单（`PREQUISD`）。当前表内 0 行（结构性建模，供后续数据加载）。
- **INV 三向匹配**：`PINVOICED` 明细经 `POHNUM_0`+`POPLIN_0` → `PORDERQ`（订单）、`PTHNUM_0`+`PTDLIN_0` → `PRECEIPTD`（收货）、`PNHNUM_0`+`PNDLIN_0` → `PAYMENTD`（付款），支撑「订单/收货/发票/付款」四单对账。数据规模：发票 38K 行 / 明细 2.4M 行。
- **PAY 与发票核销**：`PAYMENTD` 的 `VCRNUM_0`/`VCRTYP_0` 为被支付凭证（发票）；发票明细的 `PNHNUM_0`/`PNDLIN_0` 反指付款行。
- **新 KPI**：`KPI_INVOICE_AMT`（发票金额）、`KPI_PAYMENT_AMT`（付款金额）、`KPI_QUOTATION_QTY`（询价数量）。

### 已覆盖 / 已知缺口

| 目录对象 | 状态 | 说明 |
|---|---|---|
| ASN（发运通知） | ✅ 已建模 | = 到货单 `ArrivalNotice`（`YPRECEIPT`），`XSRMID_0` 即 ASN 号 |
| DELIVERY（交付） | ✅ 已覆盖 | 采购交付无独立表，由到货单 `YPRECEIPT`/收货单 `PRECEIPT` 全流程覆盖（`SDELIVERY` 为销售发货，不属采购域） |
| RFQ（询价） | ✅ 并入 QUOT | `PQUOTAT` 即询价/报价单据（与 QUOT 同表两端） |
| CONTRACT（采购合同） | ⚠️ 已知缺口 | ZJTH 无合同主数据表；合同条款可存于 `DocumentCatalog`（Phase 5） |
| NCR（不合格处理） | ⚠️ 已知缺口 | 属 QMS 域，当前业务库无 NCR 表 |
| SUP_PERF（供应商绩效） | ⚠️ 已知缺口 | 为派生指标域（OTD/质量/价格），由 Phase 4 Feature/KPI 计算，无源表 |

NL2SQL 引用新类经 `_validateOntologyAgainstSchema` 校验：`PQUOTAT`/`PINVOICE`/`PAYMENTH` 等表均存在于 ZJTH schema 缓存，无漂移告警。见 [[Supplier 收货模式 YPTHFLGM_0]]、[[nl2sql-engine]]。

## 供应商域（Supplier）

业务表：`BPSUPPLIER`（供应商主档）→ `PORDER`/`PORDERQ`（采购订单及明细）→ `YPRECEIPT`/`YPRECEIPTD`（到货单及明细）→ `PRECEIPT`/`PRECEIPTD`（收货单/入库单及明细）。

### 关键分类字段：`BPSUPPLIER.YPTHFLGM_0`（收货模式）

`BPSUPPLIER.YPTHFLGM_0` 决定整条 SRM → WMS 收料链路的形态，**订单完成率必须按此字段拆分两组口径统计**，混算会同时虚增 / 虚减两侧：

| 取值 | 语义 | 典型代表 |
|------|------|----------|
| `1` | **非零库存供应商** | 通用外购物料 / 长周期供应商 |
| `2` | **零库存供应商** | JIT 直送车间 / 寄售 / VMI 等无独立仓库环节 |

业务别名（`business_aliases`）：「零库存标志」、「到货模式」、「收货管理方式」。见 [[Supplier 收货模式 YPTHFLGM_0]]。

### 两种模式的业务流程

#### 非零库存供应商（`YPTHFLGM_0 = 1`）：到货 → 质检 → 收货

```
采购订单 PORDER/PORDERQ
    │
    ▼ 创建到货单（草稿，供应商发货时填写）
YPRECEIPT / YPRECEIPTD        [状态：草稿 draft]
    │   货物到达工厂，改为「到货」
    ▼
YPRECEIPT                     [状态：到货 arrived]
    │   推质检（QC）
    ▼
    │   质检完毕，输入合格数量 → 单据变为「质检完毕」
YPRECEIPT                     [状态：质检完毕 qualified]
    │   以合格数量为依据，创建收货单
    ▼
PRECEIPT / PRECEIPTD          [状态：收货 receipt]
    │   收货数量 = 合格数量；不合格品另走退货流程
    ▼
入库到仓库（库存 +1）
```

#### 零库存供应商（`YPTHFLGM_0 = 2`）：采购单直通收货，自动出入库

```
采购订单 PORDER/PORDERQ
    │   无独立到货 / 质检环节，订单直接驱动收货单
    ▼
PRECEIPT / PRECEIPTD          [状态：收货 receipt]
    │
    ▼ 推到车间后，由生产领用系统自动执行「入库 → 出库」
    │   物资先虚拟入线边仓（即时生成入库记录），再按生产工单领料出库
    ▼
车间消耗，整单无独立仓库库存积压
```

### NL2SQL 路由要点

涉及「订单完成率 / 收货完成率 / 到货及时率」等指标时，Prompt 须注入 **BPSUPPLIER.YPTHFLGM_0** 维度并显式要求按 `1` / `2` 分组；面向零库存供应商的查询应略过 `YPRECEIPT`，直接走 `PRECEIPT`。

详见 [[供应商收货流程（零库存 vs 非零库存）]]。

## Supplier 360° ADS 视图（Phase 5.3）

单供应商全维度数据聚合，作为采购域首个 AI 落地样板。

### 数据流（实时聚合，不建新表）

```
entity_mapping（主数据 + 跨系统编码 + owner）
        │
        ▼
Supplier360Service.get360(supplier_key)
        ├─→ profile  ─→ enterprise_code + owner + match_rule + 生效期
        ├─→ entity_codes  ─→ 多源 system/code（ERP/SRM/QMS/...）
        └─→ kpis  ─→ Feature 默认 4 项
                  ├─ SUPPLIER_OTD_3M        （准时交付率，3 月窗口）
                  ├─ SUPPLIER_DEFECT_RATE_3M（缺陷率，3 月窗口）
                  ├─ SUPPLIER_PRICE_VARIANCE_3M（价格偏差率，3 月窗口）
                  └─ SUPPLIER_RISK_SCORE    （综合风险评分）
```

### KPI 三态语义

| 状态 | 含义 | 前端展示 |
|---|---|---|
| **latest=True** | feature 定义启用 + status=ACTIVE + 有最新 value | Statistic 数字 + unit + validAt |
| **latest=False** | feature 定义存在但 disabled / DRAFT / 无最新 value | placeholder Tag「暂无数据」 |
| **不显示** | feature 定义在 DB 中不存在 | 不在 kpis 列表中（不污染展示） |

### Chat 拦截与路由

- intent_service 正则：`供应商 X 的 360° / 全貌 / 整体` + 5-9 位 BIGINT → SUPPLIER_360
- 优先级：supplier_360 > REFINE > METRIC > QUERY（防误吸普通「供应商 X 的订单数」）
- chat_service `_handleSupplier360`：
  - supplierKey 缺失 → ChatResponse(answer=引导文案, supplier360=None)
  - NotFoundError → ChatResponse(answer=通用 404 消息, supplier360=None)（Phase 4.5 ACL 原则）
  - 成功 → ChatResponse(answer=中文摘要, supplier360=完整对象)
- 前端 MessageItem 按 `message.supplier360` 存在性路由渲染 Supplier360Card

### 异常隔离原则

任意子模块（entity_codes / kpis / 单个 feature 查询）失败 → `log.warn` + 该字段返回空值，绝不阻断整体响应（与 Plan §5.3「异常隔离」一致）。

### 入口

- 直接 API：`GET /api/v1/supplier-360/{supplier_key}`（侧栏「供应商 360°」入口）
- Chat：AIChatService 中问「供应商 X 的 360° 视图」

详见 [[Harness/changes/feat-supplier-360-ads/summary.md]]。

## Supplier Risk Agent（Phase 5.4）

基于 Phase 5.3 的 SUPPLIER Feature 层（4 项 KPI）构建最小版风险评估 Agent，按 enterprise_key 实时判定等级（High / Medium / Low / Unknown）并生成结构化建议动作 + LLM 自然语言风险点。**不引入新表**，复用 `entity_mapping`（ACL）+ `feature_definition` + `feature_value` 主路径。

### 等级决策双路径

```
SupplierRiskService.assess(session, supplier_key)
   ├─→ Supplier360Service.get360()  ← 复用 Profile + 4 KPI
   ├─→ contributions = 4 项 KPI → SupplierRiskKpiContribution（+ threshold / passed / note）
   └─→ _decideLevel(contributions)
         ├─ 主路径：RISK_SCORE latest=True
         │     ├─ score < 0.60 → High（level_source="risk_score"）
         │     ├─ 0.60 ≤ score < 0.80 → Medium
         │     └─ score ≥ 0.80 → Low
         ├─ Fallback：RISK_SCORE 缺失 / 禁用 / 无值
         │     ├─ OTD<90 / DEFECT>5 / PRICE>10 任一为违规
         │     ├─ 违规=3 → High（level_source="fallback_composite"）
         │     ├─ 违规=2 → Medium
         │     └─ 违规≤1 → Low
         └─ Unknown：4 项 feature 全部 latest=False → level=unknown（不强行判定）
```

阈值与等级映射硬编码于 `supplier_risk_service.RISK_RULES`（与 `DEFAULT_SUPPLIER_FEATURES` 同模式）。阈值不变更 → 不引入 DB 配置复杂度；Phase 6 Agent 平台可演进为 DB 配置。

### LLM 生成 risk_points（异常隔离）

```
_generateRiskPoints(profile, contributions, level, *, llm_factory)
   ├─ llm_factory 为 None（生产未注入）→ fallback_template（按违规 feature 拼装）
   ├─ 调 llm_factory(None).complete([system_msg, user_msg])
   ├─ 记录 prompt_tokens + completion_tokens → tokens_used + cost + llm_model_name
   ├─ 任何异常（网络 / 超时 / 鉴权）→ log.warn + fallback_template（不阻断主响应）
   └─ 返回 (risk_points, points_source, tokens, cost, model_name)
```

- **system prompt**：固定中文模板（不允许外部内容注入）。
- **user content**：仅 `enterprise_code + 4 个 feature value + 阈值 + passed`（不拼接 user 原句，规避 prompt injection）。
- **降级模板**：`"该供应商存在以下风险点：{reasons}"`，LLM 不可用时仍可读。

### 等级 → 建议动作矩阵（静态文案，不调 LLM）

| 等级 | 触发场景（静态） | 建议动作（节选） |
|---|---|---|
| **High** | score<0.60 / 3 违规 | 立即冻结新增订单 / 启动 8D 报告 / 第三方审核 |
| **Medium** | 0.60-0.80 / 2 违规 | 限定额度 / 制定改进计划 / 月度回顾 |
| **Low** | ≥0.80 / ≤1 违规 | 维持合作 / 季度回顾 |
| **Unknown** | 4 项 feature 全部缺失 | 建议补齐数据 / 触发人工评估 |

（详细文案见 `messages_zh.py:MSG_RISK_ACTIONS_*`）

### Chat 拦截与路由

- intent_service 正则 `_SUPPLIER_RISK_RE`：`供应商 X 的风险 / 健康度 / 评分` + 5-9 位 enterprise_key（与 `_SUPPLIER_360_RE` 关键词不重叠，互不误吸）
- 优先级：`supplier_360` → `supplier_risk` → `DEFINE` / `MAP` / ... → `QUERY`
- chat_service `_handleSupplierRisk`：
  - supplierKey 缺失 → `ChatResponse(answer=引导文案, supplier_risk=None)`
  - `NotFoundError` → `ChatResponse(answer=通用 404 消息, supplier_risk=None)`（Phase 4.5 ACL 原则：不暴露「不存在 vs 无权限」侧信道）
  - 成功 → `ChatResponse(answer=等级 + 主要风险点 + 首要动作, supplier_risk=完整对象)`
- 前端 MessageItem 按 `message.supplierRisk` 存在性路由渲染 SupplierRiskCard

### 数据契约增量

```python
# backend/app/domain/enums.py
class RiskLevel(str, Enum):
    HIGH = "high"; MEDIUM = "medium"; LOW = "low"; UNKNOWN = "unknown"

class IntentType(str, Enum):
    SUPPLIER_RISK = "supplier_risk"  # 紧跟 SUPPLIER_360

# backend/app/domain/schemas.py
class SupplierRiskKpiContribution(CamelModel):
    feature_name / feature_alias / value / unit
    + threshold / passed / note  # 比 KPI 多的字段

class SupplierRiskRead(CamelModel):
    profile: Supplier360Profile       # 复用 §5.3 Profile
    level: RiskLevel
    level_source: str                 # risk_score / fallback_composite / unknown
    contributions: list[SupplierRiskKpiContribution]   # 4 项一一对应
    risk_points: str | None
    risk_points_source: str           # llm / fallback_template
    recommended_actions: list[str]
    tokens_used: int; cost: float
    llm_model_name: str | None
    fetched_at: datetime

# ChatResponse 新增
supplier_risk: SupplierRiskRead | None = Field(default=None)
```

### ACL 与安全

- 仅 `getCurrentUser` 鉴权（与 supplier_360 一致）；底层 `entity_mapping` ACL 隔离；无新增 ACL 注入。
- NotFound 通用消息（避免「不存在 vs 无权限」侧信道）。
- DTO 禁止 mass-assignment（沿用 CamelModel `from_attributes=True`，不暴露 Create / Update DTO）。
- LLM prompt 注入防护：固定 system prompt；user content 仅含结构化数据，不拼 user 原句。
- Token 计量：每次 LLM 调用记录 prompt_tokens + completion_tokens + cost，fallback 时一律 0。
- 异常隔离：LLM 调用异常 → `log.warn` + fallback；4 项 feature 缺失 → `Unknown` 而非 500。

### 入口

- 直接 API：`GET /api/v1/supplier-risk/{supplier_key}`（侧栏「供应商风险」入口）
- Chat：AIChatService 中问「供应商 X 的风险 / 健康度 / 评分」

详见 [[Harness/changes/feat-supplier-risk-agent-mini/summary.md]]。

---

## Agent Runtime（Phase 6.4）

Agent 运行时最小版（MVP）：复用 [[Harness/changes/feat-agent-registry/summary.md|Agent Registry]] 注册资产，完成「注册 → 调度 → 工具调用」最小闭环。轻量调度器，不替代 LangGraph 等框架。

### 工具绑定（AGENT_TOOLS）

| Agent | 工具 | 数据层（运行时逐层授权） | 说明 |
|---|---|---|---|
| `SUPPLIER_360_AGENT` | `supplier_360` | DIM + FEATURE | 单供应商 360° 视图（只读聚合，无 LLM） |
| `SUPPLIER_RISK_AGENT` | `supplier_risk` | DIM + FEATURE | 风险等级评估 + LLM 风险点 |
| `GRAPH_REASONING_AGENT` | `graph_traverse` | DIM + DWD | Neo4j 多跳供应链链路推理 |

已注册但未绑定工具的元数据 Agent（`PROCUREMENT_COPILOT` / `SUPPLIER_OTD_REPORT`）→ 409 不可运行，Phase 7+ 排期。

### 触发路径

1. **REST**：`POST /api/v1/agents/{agent_code}/run`（确定性测试入口，不注入真实 LLM factory）。
2. **Chat 指名**：用户问题显式指名 Agent（如「用 supplier_risk_agent 评估供应商 100001」）→ `IntentType.AGENT_RUN`（classifyResult 最优先）→ 复用真实 LLM factory + 模型路由 + 术语字典。
3. **前端**：AgentRuntimePage 手动触发 + AgentResponseCard 卡片（chat 与 REST 双路径共用）。

### 运行编排

```
run(session, agent_code, params, *, actor)
  → 查注册（404）→ 状态门禁（非 ACTIVE → 不可运行）
  → AGENT_TOOLS 绑定（无绑定 → 不可运行）
  → deny-by-default 策略拦截：工具声明 data_layers，每层需被授权
    （AgentAccessPolicy.data_layer 精确匹配或 None 通配；缺失层 → 403，不静默放行）
  → FORBIDDEN / FORBIDDEN_WRITE 为显式否决：对象匹配 + 层匹配（或 None 通配）即 403，
    优先于任何 READ 授予（防跨层通配 READ 覆盖层级 FORBIDDEN）
  → arg_extractor 参数提取 → 工具执行 → AgentRunRead
```

### 审计与计量

- `actor` 从 API 层 `getCurrentUser` 透传（`ChatRequest` 无 user 字段，身份仅存在于 API 层）。
- 每次 Agent 内部 LLM 调用经 `_recordDirectUsage` 写 `token_usage` 审计（purpose=agent_run / supplier_risk；`modelConfigId=None`）。
- 真实计量修复：`LlmResponse` 为 camelCase（promptTokens/completionTokens/modelName），`_generateRiskPoints` 兼容读取（此前读 snake_case 恒为 0）。

### 流式路由

默认 UI 全走 `/chat/stream`：`_streamInterceptCard` 统一拦截 4 类卡片意图（agent_run / supplier_360 / supplier_risk / graph_reasoning），产出 `meta → token → done`；`done` 事件携带 `agentRun/supplier360/supplierRisk/graphTraversal` 卡片对象（前端按字段存在性渲染）。

### 入口

- 直接 API：`POST /api/v1/agents/{agent_code}/run`（侧栏「Agent 运行时」页面）
- Chat：AIChatService 中问「用 xxx_agent …」（显式指名，最优先）
- 幂等 Seed：`scripts/seed_agents.py`（5 个 Agent，3 可运行）

详见 [[Harness/changes/feat-agent-runtime-mvp/summary.md]]。

---

## 四者协同：Agent Registry ↔ Agent Runtime ↔ Supplier 360 ↔ Supplier Risk

> 业务场景：采购员对 AIChat 提问「用 supplier_risk_agent 评估供应商 10105」——
> 本节梳理这 4 个功能如何与真实 THBI 数据协作，端到端走通「意图 → 调度 → 数据 → 答案」。

### 角色定位

| 功能 | 角色 | 在数据流中的位置 |
|---|---|---|
| **Agent Registry** | 元数据目录 | 定义「哪些 Agent 可调用、绑定哪些工具、读哪些数据层」 |
| **Agent Runtime** | 调度执行器 | 在请求时按 Registry 装载 Agent，做 ACL 拦截 + 工具调用编排 |
| **Supplier 360** | 只读数据服务 | 聚合「主数据 + 跨系统编码 + 4 项 KPI」，是 supplier_360 tool 的后端 |
| **Supplier Risk** | LLM 增强服务 | 在 360° 之上叠加风险等级判定 + LLM 自然语言风险点，是 supplier_risk tool 的后端 |

四者是「**注册 → 调度 → 服务 → 服务**」的层级关系：Registry 是配置面，Runtime 是执行面，360/Risk 是被调用的领域服务。

### 端到端数据流（真实业务链路）

```
采购员：「用 supplier_risk_agent 评估供应商 10105」
   │
   ▼
[前端 / API]  POST /api/v1/agents/SUPPLIER_RISK_AGENT/run
   │              body: { "input": "...10105..." }
   ▼
[Intent Service] classifyResult  →  IntentType.AGENT_RUN
   │   agent_code = "SUPPLIER_RISK_AGENT"（从问句抽取，最优先领域信号）
   │   触发条件：用户显式指名 Agent（regex 在 supplier_360/risk/graph 之前）
   ▼
[Agent Runtime] run(session, "SUPPLIER_RISK_AGENT", params, actor=user)
   │
   │ ① 加载 Agent（来自 Registry / DB）
   │     - status 非 ACTIVE → 409
   │     - 无工具绑定     → 409（元数据 Agent：PROCUREMENT_COPILOT 等）
   │
   │ ② 工具绑定 + 分层策略拦截（deny-by-default）
   │     - SUPPLIER_RISK_AGENT → ['supplier_risk']
   │     - supplier_risk 声明 data_layers = (DIM, FEATURE)
   │     - AgentAccessPolicy 必须对两层都授权；任一缺失 → 403
   │
   │ ③ arg_extractor 抽 key（= "10105"，THBI BPSNUM_0）
   │
   ▼
[Agent Tool: supplier_risk]  handler(session, {"key": "10105"}, ctx)
   │
   │   key 不做 int()（THBI '10105' int → 10105 ≠ hash → 404，正是历史坑）
   │
   ▼
[SupplierRiskService.assess(session, "10105")]
   │
   │ ├─→ Supplier360Service.get360(session, "10105")
   │ │     │
   │ │     │   _resolveSupplier("10105")
   │ │     │     ├─ Pass 1: WHERE enterprise_code = '10105' → 命中 THBI.DWD_SUPPLIER 同步行
   │ │     │     │          （entity_mapping 表 sync_entity_mapping_from_thbi.py 写入）
   │ │     │     │          返回 (enterprise_key=3823452429, enterprise_code='10105')
   │ │     │     └─ Pass 2（兜底）：WHERE enterprise_key = 10105 → 不命中
   │ │     │
   │ │     ├─→ profile        = EntityMapping(DIM)
   │ │     ├─→ entity_codes   = EntityMapping(DIM) × 全部 source_system
   │ │     └─→ kpis[4]        = FeatureValue(FEATURE) WHERE entity_key = enterprise_code
   │ │                          （⚠ entity_key 是 VARCHAR 业务码，绝非 BIGINT hash）
   │ │
   │ ├─→ contributions = 4 项 KPI → passed / threshold / note
   │ ├─→ _decideLevel
   │ │     ├─ 主路径：RISK_SCORE <0.60 / 0.60-0.80 / ≥0.80 → High/Medium/Low
   │ │     ├─ Fallback：OTD/DEFECT/PRICE 违规计数
   │ │     └─ Unknown：4 项 KPI 全 latest=False
   │ │
   │ └─→ _generateRiskPoints(level, contributions, llm_factory)
   │       ├─ LLM 可用 → 1-2 句中文风险点（prompt 注入防护：仅含结构化数据）
   │       └─ LLM 异常 → fallback_template；tokens_used/cost 落 token_usage 审计
   │
   ▼
[SupplierRiskRead]
   profile / level / contributions / risk_points / risk_points_source
   recommended_actions / tokens_used / cost / llm_model_name
   │
   ▼
[Agent Tool]  ToolResult(data=<SupplierRiskRead>, answer="供应商 10105（3823452429）风险等级：**High**…")
   │
   ▼
[Agent Runtime]  组装 AgentRunRead（tokens 聚合 + cost 聚合 + actor=user）
   │
   ▼
[API Response / Chat Stream]
   → 前端 AgentResponseCard 渲染；chat 流式路由经 /chat/stream 的 _streamInterceptCard
```

### 与真实数据的对接点

四者最终落地到 4 张表（PG）+ 1 个图库（Neo4j）+ 1 个 Oracle（THBI）：

| 资产 | 来源 | 谁写入 | 谁读取 |
|---|---|---|---|
| `entity_mapping`（PG） | THBI.DWD_SUPPLIER / .DWD_MATERIAL 同步 | `sync_entity_mapping_from_thbi.py`（SHA-256 8B hash） | Supplier 360 / Supplier Risk / Agent Runtime（ACL 主题） |
| `feature_definition`（PG） | seed + 后续 ONTOLOGY 派生 | `seed_features.py` | Supplier 360 / Supplier Risk |
| `feature_value`（PG） | feature pipeline 计算 | `feature_pipeline/*` | Supplier 360 / Supplier Risk（按 enterprise_code VARCHAR JOIN） |
| `agent` / `agent_access_policy`（PG） | 元数据 seed | `seed_agents.py` | Agent Runtime |
| Neo4j（Supplier-PO-GR-IQC 业务图） | 实体映射关系同步 | `seed_graph_relations.py` | Agent Tool `graph_traverse`（不在 4 功能主链） |
| THBI Oracle（192.168.205.70:1521/X3V71ORA） | 真实业务库 | — | 仅 `sync_entity_mapping_from_thbi.py` 直连（read-only） |

**重要语义**：`feature_value.entity_key` 存的是 `enterprise_code`（VARCHAR，如 `'10105'`），**不是** `enterprise_key`（BIGINT hash，如 3823452429）。Supplier360Service 内部同时持两者——profile 字段回填需要两个，feature_value JOIN 只能用 VARCHAR。这是 Phase 6.x 的双路解析（`str | int` 入参 + Pass-1 code / Pass-2 key 双查询）的根因。

### Registry 与 Runtime 的契约（deny-by-default）

AgentAccessPolicy 决定 Agent 能读哪些数据层：

```python
# seed_agents.py 默认策略（与 agent_tools.data_layers 一一对应，Phase 7 G6 防漂移）
{
  "agent_code": "SUPPLIER_RISK_AGENT",
  "policies": [
    { "data_object": "SUPPLIER", "data_layer": "DIM",     "permission": "READ" },
    { "data_object": "SUPPLIER", "data_layer": "FEATURE", "permission": "READ" },
  ]
}
```

工具侧声明必须与策略侧**精确对齐**：

```python
# agent_tools.py
AgentTool(
    name="supplier_risk",
    data_object="SUPPLIER",
    data_layers=("DIM", "FEATURE"),   # ← 与策略一一对应
    ...
)
```

`_enforcePolicies` 逐层检查：工具声明的每一层都必须在策略中找到精确匹配（或 `None` 通配），任一缺失 → 403，绝不静默放行。这是 P0 安全补强（避免「只授 FEATURE 实际读到 DIM」的攻击面）。

### 用户面对的「供应商编码」语义

| 视角 | 编码 | 例子 |
|---|---|---|
| **采购员**（业务） | supplier_code（VARCHAR） | `'10105'`（THBI BPSNUM_0） |
| **实体映射**（系统） | enterprise_code + enterprise_key | `('10105', 3823452429)` |
| **特征数据**（计算） | entity_key（VARCHAR 业务码） | `'10105'` |
| **Neo4j 节点 key** | source_code / enterprise_code | `'10105'` 或 ERP/SRM 源侧 |

`Supplier360Service.get360` 与 `SupplierRiskService.assess` 的入参同时接受 VARCHAR 与 BIGINT，是为了让**「业务码用户」与「系统内部 ID 调用」共用同一入口**——前者来自 AutoComplete / Chat，后者来自已有 API / 老 chat 路径。

### 入口汇总

| 入口 | 4 功能在该路径中的角色 |
|---|---|
| `GET /api/v1/supplier-360/{supplier_key}` | 直接调 Supplier 360，绕过 Agent Registry / Runtime |
| `GET /api/v1/supplier-risk/{supplier_key}` | 直接调 Supplier Risk，绕过 Agent Registry / Runtime |
| `POST /api/v1/agents/SUPPLIER_360_AGENT/run` | 经 Agent Runtime → supplier_360 tool → Supplier 360 Service |
| `POST /api/v1/agents/SUPPLIER_RISK_AGENT/run` | 经 Agent Runtime → supplier_risk tool → Supplier Risk Service（含 LLM） |
| Chat「供应商 X 的 360° / 全貌」 | Intent SUPPLIER_360 → chat_service → Supplier 360 Service（不走 Agent Runtime） |
| Chat「用 supplier_risk_agent 评估供应商 X」 | Intent AGENT_RUN → Agent Runtime → supplier_risk tool → Supplier Risk Service |

**双路径并存的设计意图**：chat「问 360°」是**意图驱动**（轻量，不调 Agent Runtime）；chat「指名 supplier_risk_agent」是**显式调度**（含 ACL + Token 计量 + 工具调用追踪）。前者面向终端用户的自然语言提问，后者面向开发者 / 高级用户的精确控制。

详见 [[Harness/changes/feat-agent-registry/summary.md|Agent Registry]]、
[[Harness/changes/feat-agent-runtime-mvp/summary.md|Agent Runtime]]、
[[Harness/changes/feat-supplier-360-ads/summary.md|Supplier 360]]、
[[Harness/changes/feat-supplier-risk-agent-mini/summary.md|Supplier Risk]]。
