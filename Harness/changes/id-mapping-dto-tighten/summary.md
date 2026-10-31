# id_mapping 创建契约收紧（A1 验收 #4）

日期：2026-10-31　分支：`feat/id-mapping-dto-tighten`　基线：`origin/main @ 927e4024`
关联：#86（① 写入口 admin 管控，同批不同文件）

## 问题

`IdMappingCreate` 允许客户端自带跨库落点（`pgTable` / `pgId`）。这三库列是三库
对齐的**结论**，不是调用方可声明的输入：

- 回填按「`milvus_id IS NULL` = 尚未同步」筛选，被乐观写死的行会被跳过；
- 节点/向量从未创建，行却声称已同步 ⇒ 静默的三库漂移，且无任何报错。

同时 `unified_id` 主键列是 `String(128)`，而 DTO 允许两段最长 32 + 128（拼出
165 字符），服务端派生时没有总长校验 —— 撞列宽会以 DB 错的形式炸在最深处。

## 改动

| 文件 | 动作 |
|---|---|
| `app/domain/id_mapping.py`（新增） | 统一 ID 格式 SSOT：`buildUnifiedId` / `parseUnifiedId` + 与列宽对齐的常量（32 / 128 / 128）；分段去空白 + NFC 归一，拒绝控制字符与零宽字符 |
| `app/domain/schemas.py` | `IdMappingCreate` 删 `pg_table`/`pg_id`，加 `extra="forbid"`（传指针 ⇒ 422 显式报错，不静默丢弃）；`IdMappingUpdate` 刻意保留（运维修数入口，① 已限 admin） |
| `app/services/id_mapping_service.py` | `register(..., *, pg_table=None, pg_id=None)`；经 `buildUnifiedId` 派生并归一（去空白），落库值取反解结果，保证 `unified_id` 与两列永远自洽 |
| `app/services/ontology_service.py` ×6、`knowledge_compiler_service.py` ×1 | DTO 只传两段，PG 落点改关键字参数（服务端派生值） |
| `scripts/rebind_ontology_thbi.py` ×2 | 同上（安全评审发现：`extra="forbid"` 落地后，仍按旧 DTO 传指针会让待执行的 THBI rebind 在中途抛错） |

## 测试

- RED：`app.domain.id_mapping` 不存在（11 条单测 collection error）；客户端带指针
  POST 返回 **201 且写库**。加固阶段另跑出 9 条 RED（控制字符 / 零宽字符 / NFC 未归一）。
- GREEN：格式单测 + `test_id_mapping_api.py` = **54 passed**；`test_knowledge_compiler_service.py` = 22 passed。
- 回归：`test_ontology_unified_id` / `test_ontology_api` /
  `test_ontology_property_service` / `test_knowledge_compiler_reconcile` =
  **8 failed / 57 passed**；同一批 8 条在 pristine `origin/main @ 927e4024`
  worktree 上逐条同名复现、条数一致 ⇒ **净新增失败 0**。
- `test_ontology_unified_id`（建类后 `pg_table=ontology_class`）与
  `test_knowledge_compiler_reconcile` 作为反向守卫：内部调用点漏改会直接红。
- 指纹类断言直接查库列（`pg_table/pg_id/neo4j_node_id/milvus_collection/milvus_id`
  五列全 NULL），不依赖 `to_camel` 产出的怪键名 `neo4JNodeId`。

## 行为变化与影响面

- 客户端 POST 带 `pgTable`/`pgId`/`neo4jNodeId`/`milvusCollection`/`milvusId`
  → **422**（改前 201 且静默写库）。前端无 `/id-mappings` 调用；影响面=第三方集成。
- 输入两段自动去空白：`' supplier '` 与 `'supplier'` 落到同一行，不再绕过
  `(business_object, external_id)` 唯一约束生成两行。
- 含 `:` 的分段从「能写入但反解二义」变为 **422**。
- 按 Unicode 通用类别拒绝（不枚举码点）：`Cc` 控制字符（NUL 炸 asyncpg、换行污染
  日志）、`Cf` 格式字符（零宽空格/软连字符/双向覆盖 ⇒ 肉眼同形 ID）、`Cs` 代理项
  （无法 UTF-8 编码 ⇒ flush 时 500）、`Zl`/`Zp` 行分隔符（日志换行注入）→ **422**。
  首尾空白（含换行、行分隔符）仍按「去空白」归一，不算非法。
- `parseUnifiedId` 与派生同宽：反解时同样校验两段长度（32 / 128）与总长（128），
  但不去空白、不做 NFC —— 历史行按原值匹配。
- 分段按 Unicode NFC 归一：`é`（U+00E9）与 `e`+组合重音落到同一行。
- 分段落库值改为归一后的值；历史未归一的行不受影响（查询按原值匹配）。

## 评审记录

两轮并行评审（code-reviewer / security-reviewer），0 CRITICAL / 0 HIGH：

- 已修 MEDIUM：零宽字符原为 5 码点枚举，软连字符 `U+00AD`、双向覆盖 `U+202E`、
  孤立代理项 `U+D800` 仍可入库（同形 ID / flush 500）⇒ 改为按类别拒绝。
- 已修 LOW：`test_knowledge_compiler_service` 之外没人用的宽度常量改由
  `schemas.py` 从本模块导入；`parseUnifiedId` 补宽度校验；补 API 级负例
  （五列指针逐一 422、非法 externalId 422 且不落库、空白归一后 409）。

## 不在本 PR

- ③ 回填脚本 + 真实数据门禁 + A1 变更记录（下一步，依赖本 PR 的 `buildUnifiedId`）。
- ontology/wiki 服务层直接写 id_mapping 的路径：本 PR 后只能传服务端派生值，
  客户端已无法注入；其路由自身的权限归属 ontology 域，不在此处收紧。
- `PUT /id-mappings/{id}` 仍接受指针字段（运维改数入口）：**验收 #4 要等 #86 合并
  后才完整成立**（#86 把写入口限为 admin）。合并顺序：#86 → 本 PR。
- 路径参数 `unified_id` 直接进 `NotFoundError` 文案、由全局 handler 原样记日志，
  可用 `%0A` 伪造日志行 —— **先于本 PR 存在**，修它要动 `api/v1/id_mapping.py`
  （与 #86 同文件），留给 #86 或单独立项。
- `business_object` 未做命名空间白名单（`CLASS`/`PROPERTY`/`METRIC` 可被预占）：
  #86 限 admin 后风险降为内部误用，后续再议。
- `knowledge_compiler_service.parseUnifiedId` 与本模块同名但语义不同
  （类型 + `int` id），本 PR 未合并两者。
