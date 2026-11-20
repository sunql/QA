# 变更：2026-11-20-a4-0-approval-subject-key（A4-0 · 放开 P5 审批面）

- **日期**：2026-11-20
- **作者**：Claude / phase3-reconciler
- **Phase**：WP-ONT-AI **A4-0**（A4-3「四眼审核复用 P5 审批面」的**前置**）
- **状态**：done（本批代码与测试完成；**生产未部署**）
- **关联变更**：[2026-11-16-ontology-discovery-ai](../2026-11-16-ontology-discovery-ai/summary.md)（WP-ONT-AI 计划；本批补上 A4 任务卡的第一条：**A4-0**）
- **迁移版本**：`0159_approval_task_subject.py`（29 字符 ≤ 32；`down_revision = "0158"` ⇒ 迁移号**串行、不留空号**）

---

## 1. 需求

**背景**：WP-ONT-AI A4 要做「本体变更提案 · 四眼审核」，计划**复用 P5 审批面**
（`approval_task`）—— A4-3 写得很死：「**不自建第二套审批栈**」。

**但按现状代码走不通**（两条都是**结构性**的，不是缺功能）：

1. `approval_task.instance_id` 是 **NOT NULL + FK** → `workflow_instance`
   ⇒ 一条审批任务**必须**挂在某个工作流实例上；而「本体变更提案」
   **没有工作流实例可挂**；
2. `WorkflowApprovalService._createRoundTasks` 把 `subjectId` **写死**成
   `str(instance.id)` ⇒ 被审对象恒等于工作流实例。

**目标**：把 P5 审批面**开一条缝**给「无实例的被审对象」，且**不破坏**现有工作流路径的
任何语义（幂等 / 四眼 / 超时 / 越权守卫逐字不变）。

**验收标准**

1. 一条审批任务可以**不挂**工作流实例（`instance_id IS NULL`）；
2. 该路径下**幂等依然成立**（同一被审对象同一矩阵条目只落一行）——
   ⚠️ 这是**最难的一条**：见 §2.2；
3. 工作流路径的幂等键语义**逐字不变**（不得因换键而改变既有行为）；
4. 只改**一张表**、只**放宽**不加不减；降级可逆。

---

## 2. 设计评审

### 2.1 方案对比（为什么选 A）

| 方案 | 做法 | 判定 |
|---|---|---|
| **A（采用）** | `instance_id` 放开为可空；工作流路径**两列都填**，提案路径**只填 subject 三元组** | ✅ 复用同一张表、同一套幂等/超时/审计；改动面 = 1 张表 + 1 个约束 |
| B | 给每个提案造一个「包装实例」 | ❌ **语义污染**：提案不是一次工作流执行；实例状态机（RUNNING / WAITING_APPROVAL / COMPLETED）对提案无意义 ⇒ 状态与真实业务对不上 |
| C | 另建 `proposal_approval` 表 | ❌ 等于把四眼 / 超时 / 幂等 / 审计**重写一遍**（违反 DRY），且与 A4-3「不自建第二套审批栈」的硬约束直接冲突 |

### 2.2 **幂等键必须换**（本批最关键的一处）

旧键 = `UNIQUE(instance_id, matrix_code)`。**PG 里 NULL 互不相等** ⇒
`instance_id IS NULL` 时，**唯一约束根本不会命中** ⇒ 同一个提案的同一矩阵条目
**能落两行** ⇒ 重复发起 = 两份待审 ⇒ **四眼失效**（而且是静默的）。

⇒ 幂等键**不能含可空列** ⇒ 换成 `UNIQUE(subject_type, subject_id, matrix_code)`
（三列**全部 NOT NULL** ⇒ 两条路径上幂等都成立）。

**工作流路径的语义逐字不变**：该路径恒有 `subject_id = str(instance.id)` ⇒
`(subject_type, subject_id, matrix_code)` 与旧的 `(instance_id, matrix_code)`
**一一对应**（同一实例同一矩阵条目仍只落一条）。

⚠️ 这一条**不能只靠「改个约束名」**：必须同时改**写入口的冲突目标**
（`ON CONFLICT DO NOTHING (instance_id, matrix_code)` → subject 三元组），
否则**写入口与约束不一致** ⇒ 冲突目标落在已不存在的索引上 ⇒ 直接报错。

### 2.3 为什么**不动**列注释

`approval_task.instance_id` 的注释是 0151 写死的，仓内有单测
`test_迁移列注释与orm双向逐字一致` 做**双向字典比对**；改注释会让 0151 与 ORM 不一致
⇒ 该测试红；且 0151 永不重跑 ⇒ 改注释还会引入 alembic-check 的注释漂移。
⇒ **可空性用 `nullable=True` 表达，注释一字不动**。

---

## 3. 变更内容

| 文件 | 改动 |
|---|---|
| `backend/alembic/versions/0159_approval_task_subject.py` | **新建**：放宽 `instance_id` 为可空；`uq_approval_task_matrix` → `uq_approval_task_subject(subject_type, subject_id, matrix_code)`；降级反向 |
| `backend/app/domain/workflow_models.py` | `instance_id: Mapped[int \| None]` + `nullable=True`；`UniqueConstraint` 换名换列 |
| `backend/app/services/workflow_approval_store.py` | `createApprovalTask` 的 `instanceId` 放宽为 `int \| None`；冲突目标换成 subject 三元组；读回改为 `_loadApprovalTaskBySubject`；新增 `listApprovalTasksBySubject`（被审对象口径） |
| `backend/app/services/workflow_approval_service.py` | **仅文档**：幂等键口径更新（工作流路径行为**零改动**） |
| `backend/app/tests/unit/test_workflow_models.py` | 迁移语料**纳入约束改写迁移**（0159 不建表 ⇒ 只扫 `create_table` 扫不到它的效果）；唯一键断言换名换列；新增 0159 的 DDL 形状 / 升降级对称 / 可空性测试 |
| `backend/app/tests/unit/test_approval_task_subject_key.py` | **新建**：A4-0 的**运行时行为** 6 条（真实 PG + 真实表约束） |
| `backend/app/tests/unit/test_procurement_binding_models.py` | 「不得 drop 既有索引」清单补上新约束名 |

---

## 4. 测试

### 4.1 定向运行（真实 PostgreSQL 5434 `qa_metadata_test`）

| 范围 | 结果 |
|---|---|
| `test_approval_task_subject_key.py` + `test_workflow_models.py` | **36 passed** |
| 全量 workflow 单测（`-k workflow`，含审批 / 决策 / 审计 / 锁序 / 端到端 / worker / 清扫） | **380 passed**（8401 deselected） |
| `alembic current` | `0159 (head)` |
| `alembic check` | ⚠️ **仅一条 drift**：`remove_table procurement_object_reconciliation_journal` —— 来自 **0158**（M1-1.4）的 journal 表**设计使然**（无 ORM 模型，降级即删），**与本批无关**；`approval_task` **零 drift**（可空性与唯一键均已对齐） |

### 4.2 新增行为用例（`test_approval_task_subject_key.py`）

| 用例 | 钉住什么 |
|---|---|
| `test_提案路径审批任务不挂工作流实例` | `instance_id IS NULL` 能落行 |
| `test_同一提案同一矩阵条目重复发起只落一条` | ⚠️ **旧键在这里会静默失效** ⇒ 这条是换键的**存在理由** |
| `test_不同提案同矩阵条目互不冲突` | 幂等键含 `subject_id` ⇒ 两个提案各留一行 |
| `test_工作流路径幂等语义逐字不变` | 新旧键一一对应 |
| `test_实例口径与被审对象口径互斥` | 两条查询口径不重叠 |
| `test_无实例任务的审批发起事件落outbox且实例为空` | 审计面**不因放开实例而丢** |

### 4.3 结构性用例（`test_workflow_models.py`）

- `test_审批任务在同一被审对象同一矩阵条目唯一`（换名换列 + 旧键必须已消失）
- `test_审批任务实例列可空与迁移一致`（ORM `nullable` 与迁移 `alter_column` 双向对齐）
- `test_约束改写迁移只动一张表的约束`（0159 只发 3 条 DDL，不建表 / 不删表 / 不执行裸 SQL）
- `test_约束改写迁移升级与降级对称`（键互换 + 列集互换 + 可空性互换）
- `test_约束改写迁移串在审批域之后`（`0159` ← `0158`，不留空号）

---

## 5. 风险与回滚

| 风险 | 处置 |
|---|---|
| 换幂等键 ⇒ 存量行与新键不匹配？ | **不会**：工作流路径写出的存量行恒有 `subject_id = str(instance_id)` ⇒ 新键与存量行**天然一致**；0159 只加约束不重写数据 |
| 换幂等键 ⇒ 同一实例同一矩阵条目能落两行？ | **不会**：见 §2.2 的一一对应 |
| 提案路径的两行并存（历史脏数据）？ | 本批**没有提案路径的数据** ⇒ 无存量；将来提案路径上线时幂等键已就位 |
| 回滚 | `alembic downgrade -1` ⇒ 删新键、恢复旧键、`instance_id` 收回 NOT NULL。⚠️ 库里若已有 `instance_id IS NULL` 的行 ⇒ **降级会失败**（预期行为：数据已超出旧口径 ⇒ 先导出） |

---

## 6. **本批刻意不做**（留给 A4-1 / A4-3）

1. **提案的审批矩阵来源**：没有实例 ⇒ `requestApproval` 取不到定义体 ⇒ 选不出矩阵条目
   ⇒ 需要「按 `subject_type` 取矩阵」的新查询面或一份提案专用定义体；
2. **无实例任务的超时清扫**：`listTasksWithContext` 是 `INNER JOIN workflow_instance`
   ⇒ 扫不到 `instance_id IS NULL` 的行 ⇒ 提案路径的超时策略要单设计；
3. **提案侧的 service / API / 提案表**：属 A4-1 / A4-3。

⇒ **A4-0 只是「存储契约」的放开，不是第二条审批栈。**

---

## 7. 关联

- 计划：[2026-11-16-ontology-discovery-ai/plan.md](../2026-11-16-ontology-discovery-ai/plan.md)
  （A4 任务卡，已补 **A4-0** 条目）
- P5 变更：[2026-11-16-p5-workflow-command](../2026-11-16-p5-workflow-command/summary.md)
  （`approval_task` 的原始定义，迁移 0149–0152）
