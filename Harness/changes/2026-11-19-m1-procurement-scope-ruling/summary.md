# M1-1.6 范围裁决落地（准则 H 可执行化）

## 1. 背景与目标

V0.3 实施计划 1.6：「按准则 H 落表决策；同步更新 `test_workflow_write_gate.py` 的守卫口径」。

准则 H = **范围裁决：不建重复语义的表**。V0.3 §7.3 草案列了 14 张 `procurement_*` 表，
逐条裁决后：9 张**不建**（复用既有表，双写 = 双口径）、3 张**建**（1 张改名）、
2 张**延后到 M6**（D3）。原文是计划文档里的一张表 —— 文档挡不住「下一个人照草案把表
建出来」，这正是裁决二要防的事（M1-a DoD 第 5 条要求**守卫用例证明**）。

⇒ 把裁决**数据化**为可执行 SSOT，两个守卫消费同一份事实。

## 2. 决策

- **裁决 SSOT**：`backend/app/domain/procurement_scope_ruling.py` —— frozen slotted
  `TableRuling(tableName, replacement, reason)` + 三个集合：
  - `FORBIDDEN_DUPLICATE_TABLES`（10 张）：复用 `semantic_release`（0142）/ `audit_log`
    / `agent_orchestration_run` 三列（0145）/ `workflow_definition`（`domain_code='procurement'`）
    / `workflow_instance` / `workflow_step` / `business_command`（0150）/ `approval_task`
    （0151）/ `workflow_outbox`（0151，消费者待补）/ `procurement_source_binding.key_columns`
    JSON + `entity_mapping`；
  - `APPROVED_TABLES`（3 张）：`semantic_release_check`（0155，改名自草案
    `procurement_ontology_release_check`）、`procurement_source_binding`（0154）、
    `procurement_property_binding`（0154）；
  - `DEFERRED_TO_M6_TABLES`（2 张）：`procurement_rule_definition` / `procurement_rule_version`
    （D3：先扩展 `feature_rule`，M6 前禁建；要建**先改目录再动表**）。
- **守卫四条红线**（`test_procurement_scope_ruling.py`，全部源码断言不碰 DB）：
  1. 禁建表任何 `__tablename__` / 迁移 `op.create_table` / 迁移裸 SQL `CREATE TABLE`
     出现 ⇒ 红；
  2. 批准表必须在对应迁移里**真实**建表（反向空集守卫：目录说建、迁移里没有 = 目录漂移；
     迁移号从 `replacement` 前缀解析）；
  3. 延后表 M6 前禁建；
  4. **闭环**：未入册的 `procurement_*` 新表定义 ⇒ 红（豁免迁移本地自删表
     `procurement_object_reconciliation_journal`）—— 只查已入册名字挡不住「另起名建
     语义重复的表」。
- **为什么用 AST 不用正则**：正则会扫到自己写的注释里的表名（0155 docstring 提到改名
  前名）⇒ 假红；字面量不出现又空集假绿（既有结论：源码级守卫用 AST）。
- **变异反证**：合成违规样本写入 `app/_synthetic_violation_tmp.py`（扫描根内、tests 外）
  必须被抓到并清理 —— 否则「守卫恒通过」只是「什么都没比」。
- **write-gate 同步**（`test_workflow_write_gate.py` 的 `test_P5表的ORM定义点唯一`，
  794→799 行）：追加 `FORBIDDEN_DUPLICATE_TABLE_NAMES` 检查，与新守卫消费同一份目录。

## 3. 实现中的坑（三个真缺陷 + 两个环境雷）

| # | 问题 | 修复 |
|---|---|---|
| 1 | **self-hit 假红**：扫描器扫到守卫自己测试文件里的反证 SQL 字符串（`procurement_approval_record` 被报在守卫测试文件里） | 扫描根排除 `app/tests/`；反证样本改写 `app/` 根下临时文件（仍在扫描内） |
| 2 | **批准表扫描空集假红**：0155 用 `_TABLE`、0154 用 `_TABLE_SOURCE` 模块级常量变量作 `op.create_table` 首参，AST 只认字面量 ⇒ 扫出 `[]` | `_createTableNames` 登记模块级字符串常量赋值（`col_offset == 0`），首参为 `Name` 时查表 |
| 3 | **环境雷①**：裸 `pytest` 连错库 —— `app/tests/conftest.py` 用 `backend/.env` 的 `DATABASE_URL`（开发库 5433）改写库名 ⇒ 连不存在的 `5433/qa_metadata_test` | 必须走 `scripts/run_local_tests.sh`（设 `TEST_DATABASE_URL`=5434）—— 既有规定，再次验证 |
| 4 | **环境雷②**：僵尸 pytest 并发占用共享测试库（一个在跑 milvus 系列）⇒ provisioning 撞 `InvalidCatalogNameError` | `ps` 抓僵尸 kill 后重跑（既有规则：先抓僵尸再信「回归」） |

## 4. 实证

- `test_procurement_scope_ruling.py` 36 用例 + `test_workflow_write_gate.py` 27 用例
  = **63 passed**（`run_local_tests.sh` 入口）。
- ruff check 全绿（新文件）；`test_workflow_write_gate.py` 存在**既有** format 漂移
  （整文件重排越界，不处理）。
- 文件行数：`test_workflow_write_gate.py` 799 ≤ 800（守卫自证）。

## 5. 评审发现与处置

code-reviewer 审查（含实跑 63 passed 复现 + 逐条对照计划文档准则 H 表与 D1–D3 + 抽查
0142/0145/0150/0151/0154/0155 迁移建表形态）：**0 CRITICAL / 0 HIGH / 0 MEDIUM / 3 LOW，
APPROVE**。三条 LOW 均已修复：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | LOW | 目录与测试 docstring 写「9 张不建」，实际 FORBIDDEN 有 10 条（计划文档 workflow 三行展开为 3 张） | 两处 docstring 改为 10 张并注明展开口径 |
| 2 | LOW | `GOVERNED_PROCUREMENT_TABLE_NAMES` 注释称「procurement_ 表名」，但含改名的 `semantic_release_check` | 注释补「含 1 张改名批准表，不带 procurement_ 前缀」 |
| 3 | LOW | 扫描器已知边界（首参不可解析跳过 / if 块内常量不入册 / sa.Table 与 *.sql 不在扫描面）未像 write-gate 那样显式声明 | 测试 docstring 补「守卫的已知边界」段（YAGNI：不加 fail-loud 计数器） |

## 6. 遗留

- 本批触碰 CI 面 ⇒ PR 时 merge-gate 可能 exit 6，需仲裁（既有通道）。
- M6 若裁决建 `procurement_rule_definition/version`：先改目录再动表。
- 下一步：Milvus ef/limit bug 修复（随本窗口发版）→ 本地发版测试。
