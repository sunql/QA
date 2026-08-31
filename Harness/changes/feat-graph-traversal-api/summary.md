# 变更：feat-graph-traversal-api

- **日期**：2026-08-31
- **作者**：Claude (Phase 6.3)
- **Phase**：6.3（知识图谱多跳推理 API）
- **状态**：done

## 1. 需求

在 6.2 业务关系图（31 节点 / 47 边）之上暴露多跳推理能力：REST API + Chat 自然语言问法路由，让用户问「供应商 100001 涉及哪些物料」即可拿到供应链链路上的可达实体。

验收标准：
- `GET /api/v1/graph/traverse?startType=Supplier&startKey=100001&maxHops=3` 返回逐跳可达链（depth 1..3）。
- Chat 集成：「供应商 X 涉及/关联哪些 物料/订单/问题」类问法 → `intent=graph_reasoning` + `ChatResponse.graph_traversal` 卡片。
- 意图隔离：`supplier_360` / `supplier_risk` 问法优先级在前，不被图推理吸走；普通查询（「订单数」）不被吸走。
- 起点不存在 → 404（通用消息，防侧信道区分 label 是否存在）；孤立节点 → 200 + 空 hops。
- Neo4j 不可达 → Chat 降级为「知识图谱服务暂时不可用」提示（不阻断主链路）；REST → 503。
- CQL label / 关系类型不可参数化 → 白名单防注入（复用 6.2 双白名单）。
- 覆盖率 ≥80%；真实 PG + 真实 Neo4j 集成测试。

## 2. 设计评审

**关键设计决策**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 遍历 CQL 形态 | variable-length path `-[:*1..maxHops]->` + `size(rels) AS depth`，每行按**末边**展开 | 一条 CQL 拿全链路；每行 from→rel→to 是**真实边**（from = 末边前驱 `nodes(path)[-2]`，depth=1 时即起点，depth≥2 时为中间节点），前端可直接按行渲染「Hop N」 |
| 起点存在性校验 | 先 `getBusinessNode`（MATCH key 节点），不存在 → `NotFoundError` | 防「空 hops 当未知实体」的语义混淆：未知实体 404、已知孤立实体 200 |
| maxHops 约束 | 服务层 `ValueError`（1..5）+ router Query `ge/le` 兜底 | 防超大路径拖垮 Neo4j；`_MAX_TRAVERSAL_HOPS = 5` 单点常量 |
| 防侧信道 | 起点不存在一律 404 + 通用消息（不区分 label 是否合法） | 不泄露「该 label 白名单是否包含某类型」 |
| 可达类型摘要 | 按 toType 去重集合（`reachableTypes`） | 前端徽标快速摘要；hops 保留完整逐跳明细 |
| Chat answer 生成 | 确定性模板（按类型分组 + 去重编码计数 + 长结果截断提示），**不调 LLM** | 图推理是纯数据检索，无需生成；避免 prompt 注入面 + Token 成本 |
| Chat 意图正则 | `_SUPPLIER_GRAPH_RE` 三分支（含关键词优先 + supplier key 提取）；`supplier_360`/`supplier_risk` 在**其后** dispatch | 与 supplier_risk 同优先级模式（360 在前 → risk → graph），重叠问法不被图推理吸走 |
| 图遍历默认参数（Chat） | `traverseForChat`：Supplier 起点 + 2 跳 | Chat 链路短（供应商→物料→订单），2 跳覆盖主链路；REST 默认 3 跳可调 |
| DTO 契约 | `GraphTraversalRead`（startKey/startType/maxHops/hops/reachableTypes/fetchedAt）camelCase JSON alias | 与现有 CamelModel 全项目契约一致 |

**开发中修复的 bug**（测试驱动暴露）：

| Bug | 根因 | 修复 |
|---|---|---|
| CQL `Variable 'start' not defined` | `WITH end, rels, size(rels) AS depth` 遮蔽了 start 变量 | 改为 `WITH start, end, rels, size(rels) AS depth` |
| `_MockRecord` KeyError 0 | `dict(record)` 迭代裸 record | 增加 `keys()` + `__iter__` |
| `_SUPPLIER_GRAPH_RE` 贪婪 | 第 4 分支裸捕获 `供应商 <key>`，吸走「订单数」类普通查询 | 移除裸 key 分支；保留含推理关键词的 3 分支 |
| 未知实体 503 | router `except Exception` 吞掉 DomainError | 在 `except ValueError` 与 `except Exception` 之间加 `except DomainError: raise`，交全局 handler → 404 |
| Chat 测试 422 datasourceId=null | ChatRequest.datasourceId 必填 int | 测试 seed DataSource（id=9402）并传有效 id（对齐 supplier_risk 9401 模式） |
| **HIGH：`相关` 关键词误吸聚合查询**（code-reviewer） | `_SUPPLIER_GRAPH_RE` 含「相关」，「供应商 100001 相关的采购订单总金额是多少」被吸为 GRAPH_REASONING | 从关键词集移除「相关/相连/有关/有关系」+ 新增 `_SUPPLIER_AGGREGATION_RE` 聚合量词兜底（金额/数量/合计/成本/占比…）；3 个负向测试锁定 |
| **MEDIUM：多跳行折叠路径**（code-reviewer） | 深度 ≥2 行 `from*`=遍历起点、rel=末边，`from→rel→to` 非真实边，前端渲染误导 | CQL 改 `nodes(path)[-2] AS from*`（末边真实前驱）；每行是真实边；docstring + 前端多跳测试更新 |

## 3. 数据模型变更

**无新表、无 Alembic 迁移**。新增 2 个 DTO（`app/domain/schemas.py`）：

```python
class GraphTraversalHop(CamelModel):      # 一跳记录
    depth, from_key, from_code, from_name, from_type,
    rel_type, to_key, to_code, to_name, to_type

class GraphTraversalRead(CamelModel):     # 遍历结果
    start_key, start_type, max_hops,
    hops: list[GraphTraversalHop],
    reachable_types: list[str],
    fetched_at: datetime

# ChatResponse 增量字段
graph_traversal: GraphTraversalRead | None = Field(default=None)
```

> `GraphTraversalRequest`（请求 DTO）经 code-reviewer 标记为死代码（GET 端点用 Query 参数，
> 服务层用方法签名），已移除。

**IntentType 新增**：`GRAPH_REASONING = "graph_reasoning"`。

## 4. 关键代码

**新增**：
- `backend/app/services/graph_traversal_service.py`（~150 行）：
  - `traverse(start_type, start_key, max_hops)`：白名单校验 → maxHops 校验 → `getBusinessNode`（NotFoundError）→ `traverseBusinessGraph` → `GraphTraversalRead`；
  - `traverseForChat(key)`：Supplier 起点 + 2 跳；
  - `buildChatAnswer(result)`：按类型分组去重编码计数 + 「仅列前 8 个编码」截断提示，纯确定性无 LLM。
- `backend/app/api/v1/graph_traversal.py`：`GET /graph/traverse`（挂 `/api/v1/graph`）。`VALID_START_TYPES` 供前端 Select / OpenAPI 复用。

**修改**：
- `backend/app/infrastructure/neo4j_client.py`：新增 `_MAX_TRAVERSAL_HOPS`、`BUSINESS_ENTITY_LABELS` / `BUSINESS_RELATION_TYPES` 白名单（与 6.2 合并）、`getBusinessNode`、`traverseBusinessGraph`（variable-length path CQL + 白名单断言 + `$param` 绑定）。
- `backend/app/services/intent_service.py`：`_SUPPLIER_GRAPH_RE`（3 分支）+ `_extractSupplierGraphKey`；`supplier_360`/`supplier_risk` 之后 dispatch `GRAPH_REASONING`。
- `backend/app/services/chat_service.py`：构造器注入 `graphTraversalService`；`_handleGraphReasoning`（NotFoundError → 通用消息；异常 → MSG_GRAPH_TRAVERSAL_UNAVAILABLE）；dispatch。
- `backend/app/domain/enums.py` / `schemas.py` / `error_messages.py` / `services/messages_zh.py`：枚举 + DTO + 校验/降级文案常量。

## 5. 测试

**执行结果**（真实环境，2026-08-31）：

- 单元：`test_graph_traversal_service.py` **21 passed**（含 code-reviewer HIGH 修复后新增 3 个负向测试）+ `test_graph_relation_service.py` 18 passed（0.06s）
- 集成：`test_graph_traversal_api.py` 10 passed（真实 PG + 真实 Neo4j）：REST 6（成功/非法 label 422/未知 404/maxHops 越界 422/3 跳链路验收/孤立节点空 hops）+ Chat 4（图推理命中/未知供应商降级/360 不被吸/risk 不被吸）
- 回归：chat/intent 单元 188 passed；supplier_360 + supplier_risk chat 集成 9 passed
- 全量后端：**1805 passed, 1 skipped**，覆盖率 **93.17%**（`--cov-fail-under=80` 通过；新增审查回归测试后从 92.69% 上升）
- 前端：`tsc --noEmit` 0 errors；`npm run build` 成功；vitest **338 passed**（新增 GraphTraversalCard 4 + GraphTraversalPanel 3，含 MEDIUM 修复后多跳行渲染测试）

**覆盖率明细**（Phase 6.3 相关模块）：

| 模块 | 覆盖率 |
|---|---|
| `graph_traversal_service.py` | 100%（39/39） |
| `graph_relation_service.py`（6.2） | 95% |
| `api/v1/graph_traversal.py` | 85%（4 未覆盖 = 503 兜底/ValueError 分支） |

**手动验证**（真实库）：

- 遍历冒烟：`Supplier 100001` 3 跳 → 34 hops，可达类型 `[Contract, GoodsReceipt, Material, PurchaseOrder, Supplier]`，answer 正确按类型分组。
- 集成测试清空真实 Neo4j 业务图后重跑 seed 脚本恢复：31 节点 / 47 边幂等不变。

## 6. 安全审查

- **CQL 注入**：label / relType 不可参数化 → `BUSINESS_ENTITY_LABELS` / `BUSINESS_RELATION_TYPES` 双白名单 + 拼接前 `_assertBusiness*` 断言；`startKey` 等值全部 `$param` 绑定。注入尝试（非法 label 内嵌 CQL）单测 → ValueError。
- **防侧信道**：起点不存在统一 404 通用消息（`MSG_GRAPH_TRAVERSAL_NOT_FOUND` 不含 label 合法性信息）；`traverse_invalid_label_422` 与 `traverse_unknown_entity_404` 语义分离但消息不泄漏白名单内容。
- **maxHops 边界**：服务层 + Query 双重校验（1..5），防超大路径 DoS。
- **只读端点**：图遍历仅 `GET`，`getCurrentUser` 鉴权（与 supplier_360 同 ACL 策略）；业务图无行级敏感数据，敏感过滤仍在 supplier_360/risk 路径。
- **无 LLM 调用**：`buildChatAnswer` 纯确定性模板，图推理问法不触发 LLM → 无 prompt 注入面 + 无额外 Token 成本。
- **降级不泄漏**：Neo4j 异常 → 503 通用文案 / Chat 降级文案，不暴露 driver 堆栈。
- 无凭据 / 无用户输入直接进 CQL 值绑定之外的位置；无新增密钥。

## 7. 部署与迁移

```bash
cd backend

# 无 Alembic 迁移（无新表）；仅需 Neo4j 运行（docker compose qa-neo4j，bolt 7687）
# 跑过集成测试后必须重跑 seed 恢复业务图：
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/python scripts/seed_graph_relations.py
# 期望：节点 31，边 47

# 测试
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/pytest app/tests/unit/test_graph_traversal_service.py \
    app/tests/integration/test_graph_traversal_api.py -v
# 期望：28 passed（Neo4j 可达时）；Neo4j 不可达时集成 10 用例 skip

# REST 冒烟
curl -X GET 'http://localhost:8000/api/v1/graph/traverse?startType=Supplier&startKey=100001&maxHops=3' \
  -H 'X-User-Id: smoke' -H 'X-User-Tenant: default' | jq '.reachableTypes'

# 前端
cd frontend && npx tsc --noEmit && npm run build
```

无新 Python 包依赖、无环境变量新增、无 schema 迁移。

## 8. 关键文件清单

**后端新增**：
- `backend/app/services/graph_traversal_service.py`
- `backend/app/api/v1/graph_traversal.py`
- `backend/app/tests/unit/test_graph_traversal_service.py`（18 用例）
- `backend/app/tests/integration/test_graph_traversal_api.py`（10 用例）

**后端修改**：
- `backend/app/infrastructure/neo4j_client.py`（遍历 CQL + `getBusinessNode` + 白名单合并，~+60 行）
- `backend/app/services/intent_service.py`（`_SUPPLIER_GRAPH_RE` + dispatch）
- `backend/app/services/chat_service.py`（`_handleGraphReasoning` + 注入）
- `backend/app/domain/enums.py` / `schemas.py` / `error_messages.py`
- `backend/app/services/messages_zh.py`
- `backend/app/main.py` + `backend/app/tests/_testapp.py`（router include）

**前端新增**：
- `frontend/src/types/graphTraversal.ts`
- `frontend/src/api/graphTraversal.ts`
- `frontend/src/components/chat/GraphTraversalCard.tsx`
- `frontend/src/components/graph/GraphTraversalPanel.tsx`
- `frontend/src/tests/GraphTraversalCard.test.tsx` / `GraphTraversalPanel.test.tsx`

**前端修改**：
- `frontend/src/types/chat.ts`（`graph_reasoning` intent + `graphTraversal` 字段）
- `frontend/src/components/chat/MessageItem.tsx`（渲染 GraphTraversalCard）
- `frontend/src/pages/Neo4jGraphPage.tsx`（新增「业务图」Segmented Tab）
- `frontend/src/i18n/zh-CN.ts` / `en-US.ts`（`graphTraversal` + `graphTraversalPage` 命名空间；补 `appLayout.menu.graph/vectors` en 缺失 key）

## 9. 验证

Phase 6.3 验收标准逐项核对：

- ✅ `GET /api/v1/graph/traverse` 返回逐跳可达链（深度 1..3；3 跳链路验收 PO→GR→IQC→Supplier）
- ✅ Chat 集成：图推理问法 → `intent=graph_reasoning` + `graphTraversal` 卡片（前端 GraphTraversalCard 渲染）
- ✅ 意图隔离：360 / risk / 普通查询均不被图推理吸走（单测 + 集成双覆盖）
- ✅ 未知实体 404 通用消息；孤立节点 200 + 空 hops
- ✅ Neo4j 不可达 → Chat 降级文案 + REST 503（异常捕获兜底，不暴露堆栈）
- ✅ CQL 防注入（双白名单 + 注入尝试单测）
- ✅ 后端全量覆盖率 93.17% ≥ 80%；前端 338 tests 通过
- ✅ 真实 Neo4j 业务图恢复（31/47 幂等）
- ✅ **双 agent 审查通过**：code-reviewer 的 HIGH（`相关` 关键词误吸聚合查询）与 MEDIUM（多跳行 `from*` 折叠）已修复 + 测试锁定；security-reviewer 0 CRITICAL/HIGH/MEDIUM（3 LOW 为接受性权衡 / 加固建议：async 内 sync driver 与既有 graph.py 一致、遍历结果无 LIMIT、无行级 ACL）

**前端覆盖率门禁（已知遗留，非本期回归）**：`--coverage` lines 79.42% / funcs 70.56% 低于 80% 门槛。根因是**既有** feature 文件的 0% funcs（EntityMapping/Lineage/KpiCatalog 等页面组件无前端测试），memory `qa-system-frontend-coverage-gate` 已记录为前置失败；本期新增组件 GraphTraversalCard / GraphTraversalPanel 均带测试。全量覆盖率模式下另有 2 例 antd Modal 表单交互超时 flaky（EntityMappingPage/FeatureCatalogPage 提交），单独运行通过，非本期改动引入。

## 10. 已知缺口 / 后续 Phase

- **遍历可视化**：当前「业务图」Tab 是逐跳 Table（结构化），未做 ECharts 图渲染；如需要「供应商→物料→订单」力导向图，可复用 Phase 2 lineage 的 ECharts graph 封装。
- **类级推理**：`ontology_join` 的 56 条类级 JOIN 未入图；「类 A 与类 B 如何关联」类问法仍走 NL2SQL 而非图推理。
- **循环路径去重**：`reachableTypes` 已按 toType 去重，但 `hops` 保留重复可达路径（同一实体多路径可达计多次）；前端按编码去重显示，明细保留完整路径是刻意选择。
- **多起点 / 多类型**：`startType` 单值；「供应商 X 的所有关联合同和物料」类混合问法需多起点（Phase 6.4 Agent Runtime 的 tool 组合层）。
- **LOW（接受性权衡）**：遍历结果集无 LIMIT（白名单 label + 深度 1..5 + 单起点，现实中结果集有限；如需防超集可在 CQL 加 `LIMIT`）；async 端点内同步 Neo4j driver 与既有 `graph.py` 一致（热点可 `asyncio.to_thread`）；业务图无行级 ACL（与 supplier_360/risk 的 ACL 策略一致，图数据非敏感明细）。
- **Phase 6.4 `feat-agent-runtime-mvp`**：Agent 注册 + 调度 + 工具调用，可把图遍历注册为 Tool，供 Agent 组合使用。
