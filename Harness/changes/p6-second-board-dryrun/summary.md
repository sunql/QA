# 变更：p6-second-board-dryrun（P6 第二板块 dry-run，D58–D62）

- **日期**：2026-11-16
- **作者**：Claude
- **Phase**：P6 第二板块 dry-run（G7）
- **状态**：**完成，G7 通过**（零代码接入成功 + Playbook 可照做）
- **关联变更**：[feat-semantic-nl2sql-call-sites](../feat-semantic-nl2sql-call-sites/summary.md)（P3d）·
  [feat-semantic-agent-snapshot](../feat-semantic-agent-snapshot/summary.md)（P3c）·
  [feat-semantic-release-graph-mirror](../feat-semantic-release-graph-mirror/summary.md)（P3b）·
  [feat-semantic-release](../feat-semantic-release-a/summary.md)（P1a/P1b）
- **迁移版本**：无（**零代码/零 DDL**——本批连代码都没有，全部是 API 操作 + 数据准备 + 文档）
- **MEMORY**：[qa-system-p6-second-board-dryrun](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-p6-second-board-dryrun.md)

---

## 1. 需求

D58–D62：用一个**假想的第二业务板块**（ASSET 资产域）走通「域 → 本体 → BO →
发布 → 镜像 → 问答」全链路，验收**零代码接入**（G7：Playbook 可被第三方照做）。

## 2. 设计评审

演练域的选择：复用已 seed 的 QUALITY/LOGISTICS vs **新建 ASSET**（采纳）——
「新增域行 via API」本身就是 D58 要验收的动作；ASSET 域与既有四域无数据纠葛，
排查干净。

## 3. 数据模型变更

**PG 侧：无 DDL**。新增的是**数据行**（经 API）：data_domain 1 行（ASSET）、
ontology_class 2 行（id=56/57）、ontology_property 5 行、ontology_join 1 行（id=89）、
business_object 1 行（ASSET_MGMT）、semantic_release 1 行（id=2，ACTIVE）、
data_source 1 行（id=9）。演示 MySQL：asset_demo 库 2 表 4 行样例。

## 4. 接口契约变更

**无代码变更**。演练覆盖的现有 API 面：data-domains / ontology(classes,properties,joins) /
business-objects / datasources / auth；服务层等效调用（semantic releases，
本地栈开关未开时）；CLI（semantic_graph_sync / verify_graph_mirror）。

## 5. 实现要点

无代码实现。**产出物**：
- Playbook：[`docs/update/1109/p6/新板块接入Playbook.md`](../../../docs/update/1109/p6/新板块接入Playbook.md)
  （8 步 + checklist + 4 个实测易错点：join targetColumns 填被引用列、BO graphLabel=类名、
  join 列不可改只能删重建、灌数要 utf8mb4）
- 演练记录：[`docs/update/1109/p6/演练记录_D58-D62.md`](../../../docs/update/1109/p6/演练记录_D58-D62.md)

## 6. 测试

演练本身就是验证（每步实测 + 返回样例留档）。端到端验收 = chat `[L4:answered]`
（SQL 正确 JOIN、中文数据正确）。镜像对账 exit 0；双发布共存（37/39 类）。

## 7. 安全审查

本批零代码。安全观察：数据源密码经 API 写入（加密存储，响应不回显明文）✓；
发布走治理闸门 ✓；镜像写目标闸门只准测试实例 ✓。

## 8. 部署验证

无部署。环境操作全部在**本地测试栈**（dev 5433 / 测试图 7688 / 演示 MySQL），
未触碰生产。

## 9. 关联

- **Playbook**：[`docs/update/1109/p6/新板块接入Playbook.md`](../../../docs/update/1109/p6/新板块接入Playbook.md)
- **演练记录**：[`docs/update/1109/p6/演练记录_D58-D62.md`](../../../docs/update/1109/p6/演练记录_D58-D62.md)
- **计划**：[`docs/update/1109/智能采购平台升级计划_日级排程_1109.md`](../../../docs/update/1109/智能采购平台升级计划_日级排程_1109.md) §12（D58–D62、G7）
- **上游**：P1a/P1b（发布 API）· P2（钉住）· P3b（镜像/对账 CLI）· P3c/P3d（读侧钉住链）
- **Memory**：`qa-system-p6-second-board-dryrun`

---

## SSOT 校验清单

- [x] frontmatter 齐全；9 段非空
- [x] 第 2 段候选对比（域选择）
- [x] 零 DDL / 零代码（本批特殊性：连代码都没有）
- [x] 安全观察（数据源密码/治理闸门/写目标闸门）
- [x] 端到端证据（chat answered + 镜像对账）
- [x] 跨文件链接 ≥ 3
- [x] MEMORY 已添加
