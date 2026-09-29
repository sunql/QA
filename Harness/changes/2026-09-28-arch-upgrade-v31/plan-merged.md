# 合并实施计划（甲+乙 v3.1 架构升级）

> 负责：甲（数据/基建）+ 乙（服务/链路）合并执行
> 周期：W1-W12，关键路径 W1-W8
> 蓝本：summary.md + plan-person-a.md + plan-person-b.md

---

## ⚠️ 前置合并行动（立即执行）

**B1 合入 epic/v31-upgrade**（否则 A1 无法继续）：

```
feat/evidence-extension-v31（9 commit，0096）
         ↓ PR → epic/v31-upgrade
feat/m0-unified-id（无新 commit，等 epic 更新）
         ↓ rebase epic
开始 A1
```

**原因**：`feat/m0-unified-id` 当前分叉状态与 epic/v31-upgrade 完全一致（0 新 commit），说明 A1 的上游依赖是等 B1 先合入。**必须先合 B1，再 rebase m0-unified-id，然后才能做 A1**。

---

## 阶段一 · MB1 语义基础（W1-W5）

### W1 · B1 合入 + A1 启动

| 任务 | 负责人 | 步骤 |
|---|---|---|
| B1 合入 epic | 乙 | PR feat/evidence-extension-v31 → epic/v31-upgrade；解决冲突；合入后确认 0096 已占 |
| A1 rebase | 甲 | `git rebase epic/v31-upgrade`（feat/m0-unified-id）；确认无冲突 |
| A1 启动 | 甲 | 见 A1 详细任务块 |

### W2 · A1 完成 + A2 试点

| 任务 | 负责人 | 步骤 |
|---|---|---|
| A1 完成 | 甲 | id_mapping 表（0095）+ 回填脚本 + API + integration 测试 |
| A2 试点 | 甲 | supplier 试点批（先数据量小的类）；neo4j 迁移脚本 + 对账 |

### W3 · A2 全量 + A3 + B2 启动

| 任务 | 负责人 | 步骤 |
|---|---|---|
| A2 全量 | 甲 | 27 类 Neo4j 节点 ID 改造（分批，每批 count(*) 对账） |
| A3 启动 | 甲 | Milvus 三 collection 加 external_id（新建 collection + 双写 + release/load） |
| B2 启动 | 乙 | `business_db_pool.py` execute_read_only 加 Evidence 记录钩子（outbox 异步） |

### W4 · A3 完成 + A4 + A5 启动 + B2 完成

| 任务 | 负责人 | 步骤 |
|---|---|---|
| A3 完成 | 甲 | Milvus external_id 三库对账一致 |
| A4 启动 | 甲 | 写路径改造（IdMappingService.register → 三库写 unified_id） |
| A5 启动 | 甲+乙 | KnowledgeCompilerService 门面骨架（甲主导 Graph+SQL Metadata，乙负责 Vector） |
| B2 完成 | 乙 | SQL 自动 Evidence 落库；四链路集成测试 |

### W5 · A4 + A5 完成 + MB1 验收

| 任务 | 负责人 | 步骤 |
|---|---|---|
| A4 完成 | 甲 | 三库 unified_id 一致；对账脚本 diff 为空 |
| A5 完成 | 甲+乙 | Compiler 门面三编译器可调；每日对账任务上线（agent_scheduler_service 挂载） |
| MB1 验收 | 甲 | 三库统一 ID 对账一致；Evidence 3 类型齐（Document/SQL_QUERY/METRIC_RESULT）；Compiler 门面上线 |

---

## 阶段二 · MB2 Agent 收敛（W5-W8）

### W5（延续）· A6 完成 + B4 启动 + 接口对齐

| 任务 | 负责人 | 步骤 |
|---|---|---|
| A6 完成 | 甲 | Planner ≤5 步硬限（已完成，合入 epic/v31-upgrade） |
| B4 启动 | 乙 | confidence_service + 派生列（0098） |
| 接口对齐 | 甲+乙 | classify 返回结构 + semanticState 字段（乙 B5 依赖）；evidence payload 结构（甲 A8 依赖） |

### W6 · B4 完成 + A7 启动 + B5 启动

| 任务 | 负责人 | 步骤 |
|---|---|---|
| B4 完成 | 乙 | Confidence 4 级 API + 前端徽标 + REFUSE 原因 |
| A7 启动 | 甲 | Intent/Semantic 合并（单一 classifyAndRecall 入口）；冻结 IntentType 路由枚举 |
| B5 启动 | 乙 | Memory Phase A：字段继承规则 + `_resolveInheritedState()` 收敛为单一入口 |

### W7 · A7 + B5 完成 + B3 收尾

| 任务 | 负责人 | 步骤 |
|---|---|---|
| A7 完成 | 甲 | Intent 合并对拍测试；单步 ≤1 次 LLM 集成验证 |
| B5 完成 | 乙 | 三轮追问继承集成测试；state 快照入库可查 |
| B3 收尾 | 乙 | Chat 证据展示（SQL + row_count + result_hash 前 8 位）；多步每步独立 evidence |

### W8 · MB2 联合验收 + bugfix

| 任务 | 负责人 | 步骤 |
|---|---|---|
| MB2 验收 | 甲+乙 | 单步 ≤1 次 LLM；多步 ≤3 次；Confidence 4 级全链路可见；三轮追问字段继承正确 |
| bugfix | 甲+乙 | 根据验收发现修复 |

---

## 阶段三 · MB3 报告 MVP（W9-W12）

### W9-W10 · A8 前半 + B6 启动

| 任务 | 负责人 | 步骤 |
|---|---|---|
| A8 前半 | 甲 | supplier-360-v1 模板：声明式 JSON 模板 + report_template_service + ECharts spec |
| B6 启动 | 乙 | Hypothesis Hook：driver 发现 + ≤3 假设 + 验证 SQL（经 SQL Guard） |

### W11-W12 · A8 后半 + B6 完成 + MB3 验收

| 任务 | 负责人 | 步骤 |
|---|---|---|
| A8 后半 | 甲 | monthly-ops-v1 模板；前端 ReportsPage + seed_menu_config.py + i18n |
| B6 完成 | 乙 | Hypothesis Hook 集成；假设可展开看验证 SQL；INFERENCE claim 写入 |
| MB3 验收 | 甲+乙 | supplier-360 + monthly-ops 模板导出 PDF ≤30s；假设段无非因果断言；Evidence 引用可点击 |

---

## Alembic 版本号规划

| 版本 | 内容 | 负责人 | 状态 |
|---|---|---|---|
| 0095 | id_mapping 表 | 甲 | 待执行 |
| 0096 | evidence payload JSONB + session_id | 乙 | **已完成**（feat/evidence-extension-v31） |
| 0097 | claim source_version | 甲 | 待执行 |
| 0098 | claim confidence_level 派生列 | 乙 | 待执行 |

**冲突规则**：先合并者优先占用；后合并者负责 renumber。0096 已由乙完成占用。

---

## 立即行动清单（合并后第一步）

- [ ] **甲**：PR feat/planner-step-limit → epic/v31-upgrade（A6 已完成，5 commit），合入
- [ ] **乙**：PR feat/evidence-extension-v31 → epic/v31-upgrade，合入
- [ ] **甲**：等 B1 合入后，`git rebase epic/v31-upgrade` feat/m0-unified-id，开始 A1

---

## 剩余关键风险

| 风险 | 概率 | 缓解 |
|---|---|---|
| M0 全量改造引发三库漂移 | 高 | 分批迁移 + 每批 count(*) 对账 + 双写窗口 |
| Intent/Semantic 合并误伤业务路由 | 中 | 路由枚举冻结；只合并 LLM 调用层；50 条历史对拍 |
| 两人同改 chat_service.py 冲突 | 中 | 分日提交；周rebase 对齐 |
| 0095/0097 与 0096/0098 合并撞车 | 低 | 先合优先占号 |
