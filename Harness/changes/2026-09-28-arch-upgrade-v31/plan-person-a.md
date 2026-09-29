# 甲 · 实施计划（数据/基建线）

> 负责：M0 统一 ID（P0.1-P0.4）→ M8 Knowledge Compiler 收口 → M2 Planner 步限 + Intent 合并 → M4 报告模板
> 周期：W1-W12，关键路径 W1-W8
> 分支：`feat/m0-unified-id` → `feat/knowledge-compiler` → `feat/planner-step-limit` → `feat/intent-semantic-merge` → `feat/report-templates`

---

## 任务 A1 · M0-P0.1 id_mapping 表（W1，3 天）

**目标**：建立统一 ID 的映射中枢。

- 新建 `app/domain/id_mapping.py`（或并入 models.py，遵循现有组织方式）：
  ```python
  class IdMapping(Base):
      __tablename__ = "id_mapping"
      unified_id: str        # "obj:customer:C10001" 主键
      business_object: str   # customer / supplier / ...
      external_id: str       # C10001
      pg_table: str | None
      pg_id: str | None
      neo4j_node_id: str | None
      milvus_collection: str | None
      milvus_id: str | None
      created_time / updated_time
  ```
- alembic 0095 + 唯一索引 `(business_object, external_id)`
- 回填脚本 `scripts/backfill_id_mapping.py`：从 ontology_class / entity_mapping 现有数据生成初始映射（只读源表，纯新增）

**TDD**：integration 测试 `test_id_mapping_api.py`——建映射 → 三端点查询 → 冲突唯一约束 422。
**验收**：表建成 + 回填后 `count(*)` 与 ontology_class 数对平。

## 任务 A2 · M0-P0.2 Neo4j 节点 ID 改造（W2-W3，高危，分批）

**目标**：图节点主标识改为 `obj:{bo}:{ext_id}` 格式。

**风控（必须按序执行，不可跳步）**：
1. 执行前：`neo4j-admin dump` 到 `backups/<日期>_m0/neo4j/` + `pg_dump id_mapping` 前置表
2. 按 business_object **逐类迁移**（先 supplier 试点——数据量小、Supplier360 链路可回归），每批：
   - 脚本读 IdMapping → Neo4j `MATCH (n) WHERE n.id = $old SET n.unified_id = $new`（不动旧属性，加新属性）
   - 校验：固定业务 id 重插 + 删对查不可见 + 该类 count 对平
   - 图遍历 API（graph_traversal_service）回归测试全绿
3. 全部类迁移完成后，下一版本再移除旧 ID 属性（保留双字段一个版本周期）

**TDD**：integration 测试用真实 Neo4j（docker 环境），单类迁移的 round-trip 测试（复用 Milvus round-trip 随机 id 模式）。
**验收**：27 类全部有 unified_id；`graph_traversal` 全链路测试绿；无孤儿节点。

## 任务 A3 · M0-P0.3 Milvus external_id（W3，与 A2 后半并行）

**目标**：schema / kpi / wiki 三个 collection 增加 `external_id` 字段（varchar），写入路径带统一 ID。

- 注意 Milvus 2.4.6 不支持加列——需**新建 collection + 双写迁移 + 切读**，参考 wiki_page_embeddings 建 collection 的既有模式
- flush 单批 8-25s 的坑：迁移脚本分批 flush，批间 sleep
- Strong 一致性 per-request 实测不生效：迁移后必须 release + load

**验收**：三 collection `query(expr="external_id != ''")` 计数与 PG 对平。

## 任务 A4 · M0-P0.4 写路径改造（W3-W4）

**目标**：ontology_service 三处同步点（PG → Neo4j + Milvus）改产统一 ID。

- 新增 `IdMappingService`：`register()` / `resolve()` / `reconcile()`（对账用）
- ontology_service 创建/更新类时：先注册 IdMapping（拿 unified_id）→ 三库写 unified_id
- 读路径兼容：一个版本内旧 ID 读仍可用（双读归一）

**验收**：新建业务对象后三库 unified_id 一致；`scripts/` 下对账脚本 `reconcile_id_mapping.py` 输出三库 diff 为空。

## 任务 A5 · M8 Knowledge Compiler 收口（W4-W5，与乙共建，甲主导）

**目标**：不新建编译器，收编现有四处编译逻辑为统一门面 + 补对账/回滚约束。

- 新建 `app/services/knowledge_compiler_service.py`：
  ```python
  class KnowledgeCompilerService:
      async def compileObject(unifiedId)   # 门面：分发到 graph/vector/sql_metadata 三个编译器
      async def reconcile()                # 三库对账巡检（每日任务）
  ```
- 收编点（只包门面，不改内部逻辑）：
  | 现有逻辑 | 门面中的角色 |
  |---|---|
  | ontology_service 三库同步 | Semantic Graph 编译器 |
  | embedding_service / wiki_compile_service | Vector Index 编译器 |
  | metric_promotion_service / kpi_catalog | SQL Metadata 编译器 |
- 补运维约束（v3.1 §4.14）：
  1. **对账任务**：借 agent_scheduler_service 模式挂每日任务，结果落 audit_history，三库 count 不一致即 warning
  2. **编译失败回滚**：编译任务 try/except 全兜底（参考导入台账僵尸任务教训——兜底 except 必须执行），半成品不入库
- `knowledge_claim` 补 `source_version` 字段（alembic 0097，与乙的 0096 错开）

**验收**：门面可调通三类编译；人为制造 Milvus 断连 → 编译任务标记 failed 且 PG 无半成品。

## 任务 A6 · M2a Planner ≤5 步硬限（W5，1.5 天）

**目标**：`step_query_planner.py` 加硬上限。

- `MAX_PLAN_STEPS = 5`（constants，禁魔数）
- 解析 steps 后：`len(steps) > MAX_PLAN_STEPS` → 返回 PlanDrop 式拒绝（复用半空计划闸门模式），话术：「该问题需 N 步拆解，超出 5 步上限，请聚焦单一维度提问（如先分析客户层面原因）」
- 分级落地（§5.3）：单步跳过 planner（现状已如此）；2-3 步走模板校验；4-5 步 LLM + 校验
- ⚠️ 与乙协调：此任务动 planner 出口结构，乙的 evidence 接线在 executor 侧，无冲突

**TDD**：unit 测试 6 步计划被拒、5 步通过；AST 守卫可选。
**验收**：「完整分析华东销售下降所有原因」类问题返回拆解提示而非 12 步计划。

## 任务 A7 · M2b Intent/Semantic 合并（W6，2.5 天）

**目标**：合并 LLM 调用层，**冻结 IntentType 路由枚举**。

- 现状：`intent_service.py`（规则分类）+ `chat_recall.py`（语义召回）各调/各走一遍
- 改造：单一入口 `classifyAndRecall()` 一次输出 `{intent, semantic_objects, recall_hits}`；prompt 合并（意图 + 语义映射同一次 LLM 调用）
- **红线**：IntentType.SUPPLIER_360 / SUPPLIER_RISK / AGENT_RUN / CHITCHAT / CLARIFY 路由行为逐条冻结，合并前后行为对拍测试（同一批 50 条历史问题，路由结果 diff 为空）
- LLM 调用计数：合并后单步查询链路 ≤1 次（token_usage_service 断言）

**验收**：对拍测试绿；单步任务 LLM 调用从 2 次降为 1 次的集成测试。

## 任务 A8 · M4 报告模板（W9-W12）

**目标**：`supplier-360-v1`（W9-W10）+ `monthly-ops-v1`（W11-W12）。

- 模板 = 声明式 JSON：`{blocks: [{type: "kpi" | "chart" | "table" | "text", binding, chart_spec}]}`
- 新建 `app/services/report_template_service.py` + `app/services/report_renderer.py`（数据绑定，零 LLM）
- ECharts spec 复用 chart_service 的 formatter 规范（`{c}` 非 `{d}`，smart summary 教训）
- LLM 仅在「自然语言总结」段调用 1 次（≤200 字输出，走 token_usage 收口）
- 硬约束实现（§21 Phase 3a）：查询 ≤10 个、结论必须挂 Evidence ID（引用乙的 evidence 表）、报告 ≤2 页
- 前端：`frontend/src/pages/ReportsPage.tsx`（新页面，需同步 seed_menu_config.py + i18n menu.item——菜单 seed 强制规则）

**验收**：两个模板从选择到导出 PDF ≤30s（数据绑定路径）；总结段有 Evidence 引用可点击展开。

---

## 周计划汇总

| 周 | 任务 | 交付 |
|---|---|---|
| W1 | A1 | id_mapping 表 + 回填 |
| W2 | A2 前半（supplier 试点批） | 试点类迁移 + 对账 |
| W3 | A2 后半（其余 26 类）+ A3 | 全量迁移 + Milvus 改造 |
| W4 | A4 + A5 启动 | 写路径改造 + Compiler 门面骨架 |
| W5 | A5 完成 + A6 | 对账任务上线 + Planner 步限 |
| W6 | A7 | Intent/Semantic 合并 |
| W7 | 缓冲（M0 回归/bugfix） | — |
| W8 | MB2 联合验收 | 验收报告 |
| W9-W10 | A8 前半 | supplier-360-v1 |
| W11-W12 | A8 后半 | monthly-ops-v1 |

## 协调事项

- W4-W5 与乙共建 Compiler：乙负责 Vector 编译器收编（wiki_compile 侧），甲负责 Graph + SQL Metadata 侧
- W5 前必须完成与乙的 chat_service.py rebase 对齐（乙 W1-W2 改了 evidence 接线）
- 所有 LLM 新增调用 → token_usage_service；所有新表 → alembic 先行，**prod qa_metadata 直接打 migration**（两库使用策略），迁移前备份 `<表>_<YYYYMMDD>`
