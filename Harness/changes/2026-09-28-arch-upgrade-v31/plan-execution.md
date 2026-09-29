# v3.1 架构升级 · 执行计划

> 基于 2026-09-29 实勘状态；**同日二次更新：M0 全链（A1-A4）+ A6 + B1 已合 main，MB1 完成 2/3**
> 目标：MB1(W5) → MB2(W8) → MB3(W12)

---

## 当前状态（2026-09-29 二次更新）

| 任务 | 分支 | 状态 |
|---|---|---|
| A1-A4（M0 统一 ID 全链：id_mapping / Neo4j unified_id / Milvus 3-collection + external_id / 写路径） | feat/m0-unified-id | ✅ 已合 main（经 epic，76ef6ea；后续 R3 修复 55149ee、dim 修复 7d16a08） |
| A6（Planner ≤5 步硬限） | feat/planner-step-limit | ✅ 已合 main（经 epic） |
| B1（Evidence payload JSONB + session_id，0096） | feat/evidence-extension-v31 | ✅ 已合 epic（d547355）→ main |
| A5（Compiler 门面）/ A7（Intent 合并）/ A8（报告模板） | — | 🔴 未开始（服务文件实勘缺失） |
| B2（SQL 自动 Evidence 钩子）/ B3（Chat 证据展示）/ B4（Confidence 4 级）/ B5（Memory Phase A）/ B6（Hypothesis Hook） | — | 🔴 未开始 |

**alembic 实况**：main head = `0097_id_mapping`（A1 让号后落地）+ `0096_evidence_payload`（B1）。
后续编号顺延：A5 claim source_version → **0098**；B4 confidence_level → **0099**。原计划的 0097/0098 指派作废，**勿硬编码**。

**分支策略（2026-09-29 拍板，方案 A）**：epic/v31-upgrade 快进对齐 main；后续 feat 分支照旧从 epic 切，保留每周对齐点。

---

## W1 · 2026-09-29 ~ 2026-10-03

### 立即行动（今天）— ✅ 已全部完成（见文末清单）

- [ ] **甲**：PR `feat/planner-step-limit` → `epic/v31-upgrade`（A6，5 commit）
- [ ] **甲**：`git rebase epic/v31-upgrade feat/m0-unified-id`，然后开始 A1

### A1 · M0-P0.1 id_mapping 表

**目标**：建 id_mapping 表 + 回填 + API + integration 测试

- alembic 0095：`id_mapping` 表（unified_id PK，`business_object+external_id` 唯一索引，三库 ID 列 nullable）
- `app/domain/id_mapping.py`：IdMapping ORM
- `app/services/id_mapping_service.py`：`register()` / `resolve()` / `reconcile()`
- 回填脚本 `scripts/backfill_id_mapping.py`：从 ontology_class / entity_mapping 只读生成初始映射
- REST 端点：POST/GET/DELETE/PUT `/id-mappings`
- TDD：integration 测试（建映射 → 冲突唯一约束 422）
- 验收：`count(*)` 与 ontology_class 数对平

### A2 启动 · Neo4j 试点（本周后半）

- 备份：`neo4j-admin dump` + `pg_dump id_mapping`
- supplier 试点批：脚本 `MATCH (n) WHERE n.id = $old SET n.unified_id = $new`
- 校验：固定业务 id 重插 + 删对查不可见 + count 对平

---

## W2 · 2026-10-06 ~ 2026-10-10

### A1 收尾

- integration 测试全绿
- API 端点文档确认

### A2 全量（27 类 Neo4j 节点 ID）

- **必须分批**：先小类（supplier 已试点），大批量类（实体多的）每批 ≤5 类
- 每批：`MATCH SET n.unified_id` → 校验 count → 图遍历回归测试
- 全部完成后：下一版本再移除旧 ID 属性（双字段保留一个版本周期）

### B2 启动 · SQL 自动 Evidence 记录

- `business_db_pool.execute_read_only` 加 Evidence 记录钩子
- `result_hash = sha256(json.dumps(rows, sort_keys=True, default=str))`
- outbox 异步写（不阻塞查询主链路）
- 失败 best-effort（warning，不让查询失败）

---

## W3 · 2026-10-13 ~ 2026-10-17

### A2 收尾 + A3 启动

- 27 类 Neo4j unified_id 全覆盖
- 对账脚本 `reconcile_id_mapping.py` 输出 diff 为空

### A3 · M0-P0.3 Milvus external_id

- schema / kpi / wiki 三 collection 加 external_id
- **Milvus 2.4.6 不支持加列**：新建 collection + 双写迁移 + release/load
- 迁移分批 flush（8-25s 坑），批间 sleep
- 验收：`query(expr="external_id != ''")` 与 PG 对平

### B2 收尾

- L1/L2/多步/L4 四链路集成测试
- evidence 表 SQL_QUERY 记录可查

---

## W4 · 2026-10-20 ~ 2026-10-24

### A3 收尾 + A4 启动

- 三 collection external_id 全量对账
- 写路径改造：IdMappingService.register() → 三库写 unified_id

### A5 启动 · KnowledgeCompilerService 门面

- 新建 `app/services/knowledge_compiler_service.py`
- 门面分发：Graph（ontology_service）/ Vector（embedding_service）/ SQL Metadata（metric_promotion_service）
- **共建**：甲主导 Graph+SQL Metadata，乙负责 Vector（wiki_compile 侧）
- 每日对账任务挂 `agent_scheduler_service`
- alembic 0097：knowledge_claim 加 source_version 字段

### B2 合入 epic/v31-upgrade

---

## W5 · 2026-10-27 ~ 2026-10-31

### A4 + A5 收尾 + MB1 验收

**MB1 验收标准**：
- ✅ 三库统一 ID 对账一致
- ✅ Evidence 3 类型齐（Document/SQL_QUERY/METRIC_RESULT）
- ✅ Compiler 门面上线 + 每日对账任务运行

### A7 启动 + B4 启动 + 接口对齐

- A7：Intent/Semantic 合并（单一 classifyAndRecall 入口）
- B4：confidence_service + alembic 0098（confidence_level 派生列）
- 接口对齐：classify 返回 + semanticState 字段；evidence payload 结构

---

## W6 · 2026-11-03 ~ 2026-11-07

### A7 + B4 收尾

- A7：Intent 合并对拍测试（50 条历史问题路由 diff 为空）；单步 ≤1 次 LLM
- B4：Confidence 4 级 API + 前端徽标 + REFUSE 原因

### B5 启动 · Memory Phase A

- `chat_context.py` JSONB 快照扩展
- 字段继承规则：`metric / time / dimension / filter` 省略式追问继承
- **收敛**：4 处追问入口 → 单一 `_resolveInheritedState()`

---

## W7 · 2026-11-10 ~ 2026-11-14

### B5 + B3 收尾

- B5：三轮追问集成测试；state 快照入库
- B3：Chat 证据展示（SQL + row_count + result_hash 前 8 位）；多步每步独立 evidence

### B4 合入 epic/v31-upgrade

---

## W8 · 2026-11-17 ~ 2026-11-21

### MB2 联合验收

**MB2 验收标准**：
- ✅ 单步 ≤1 次 LLM；多步 ≤3 次
- ✅ Confidence 4 级全链路可见
- ✅ 三轮追问字段继承正确

### bugfix + 合入 epic

- A7 合入 epic/v31-upgrade
- B5 合入 epic/v31-upgrade

---

## W9 · 2026-11-24 ~ 2026-11-28

### A8 前半 · supplier-360-v1 模板

- 声明式 JSON 模板：`{blocks: [{type: kpi|chart|table|text, binding, chart_spec}]}`
- `app/services/report_template_service.py` + `app/services/report_renderer.py`
- ECharts spec 复用 chart_service formatter 规范（`{c}` 非 `{d}`）
- LLM 仅在「总结」段调用 1 次（≤200 字）

### B6 启动 · Hypothesis Hook

- driver 强制从 schema digest / 已执行步骤聚合维度发现
- ≤3 假设（硬约束 + 解析截断）
- 每条附验证 SQL（经 SQL Guard，不自动执行）

---

## W10 · 2026-12-01 ~ 2026-12-05

### A8 前半收尾 + B6 推进

- supplier-360-v1 模板集成测试
- B6：假设输出 + INFERENCE claim 写入

---

## W11 · 2026-12-08 ~ 2026-12-12

### A8 后半 · monthly-ops-v1 模板

- `frontend/src/pages/ReportsPage.tsx`（新页面）
- `seed_menu_config.py` + i18n `menu.item`

### B6 收尾

- 假设可展开看验证 SQL
- INFERENCE claim 链路全通

---

## W12 · 2026-12-15 ~ 2026-12-19

### MB3 验收

**MB3 验收标准**：
- ✅ supplier-360 + monthly-ops 模板导出 PDF ≤30s
- ✅ 假设段无非因果断言
- ✅ Evidence 引用可点击展开

### 合入 epic/v31-upgrade + 主线合并

- 所有分支 PR 合 epic
- epic 合 main

---

## Alembic 版本号

| 版本 | 内容 | 状态 |
|---|---|---|
| 0096 | evidence payload JSONB + session_id | ✅ 已合（B1，d547355） |
| 0097 | id_mapping 表（原计划 0095，让号后落地） | ✅ 已合 main（A1） |
| 0098 | claim source_version | 待执行（A5；原计划编号 0097 作废） |
| 0099 | claim confidence_level 派生列 | 待执行（B4；原计划编号 0098 作废） |

---

## 立即可执行任务（更新于 2026-09-29 下午）

- [x] A1-A4 M0 全链合 main（76ef6ea）
- [x] A6 合 epic → main
- [x] B1 合 epic → main（0096）
- [x] epic/v31-upgrade 快进对齐 main（方案 A）
- [ ] A5：KnowledgeCompilerService 门面 + 0098 claim source_version
- [ ] B2：`execute_read_only` SQL 自动 Evidence 钩子（outbox 异步 + best-effort）
- [ ] 1-2 天 runbook 验证（M0 部署稳定性观察，per qa-system-stale-container-deploy）
