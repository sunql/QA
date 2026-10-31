# id_mapping 写入口 admin 管控（A1 验收 #5）

日期：2026-10-31　分支：`fix/id-mapping-admin-acl`　基线：`origin/main @ 927e4024`

## 背景

M0-A1 的 id_mapping 是 PG / Neo4j / Milvus 三库地址中枢。原验收清单第 5 条要求
**写入口为运维/后台专用**，但 main 上 `app/api/v1/id_mapping.py` 的五个端点全部只用
`Depends(getCurrentUser)`——任何普通登录用户都能改三库指针、删除映射、整表列举。

`git log -S 'requireIdMappingAdmin' origin/main` 为空，确认该管控从未落到 main
（此前在 `backup/evidence-sql-metric-docs` 分支里只有未合并的草稿）。

## 改动

| 端点 | 改前 | 改后 |
|---|---|---|
| `GET /api/v1/id-mappings`（整表列举/按业务对象） | 登录即可 | **admin** |
| `POST /api/v1/id-mappings` | 登录即可 | **admin** |
| `PUT /api/v1/id-mappings/{unified_id}` | 登录即可 | **admin** |
| `DELETE /api/v1/id-mappings/{unified_id}` | 登录即可 | **admin** |
| `GET /api/v1/id-mappings/{unified_id}`（单点读） | 登录即可 | 不变（刻意保留） |

复用既有 `app.dependencies.requireAdmin`（`dependencies.py:366`，非 admin 抛
`PermissionDenied(403, "需要 admin 角色")`），没有新增守卫。

单点读保持开放：NL2SQL / 图谱前端需要按 unified_id 反查业务对象，关闭会误伤只读链路；
单点读只暴露一个已知 ID 的三库指针，与整表列举、写入口的风险量级不同。

**内部调用不受影响**：`IdMappingService.register/update/delete` 被
`ontology_service` 等 6+ 处服务层直接调用，不经 HTTP，本次不改变其签名与权限模型。

## 测试（TDD）

新增 `backend/app/tests/integration/test_id_mapping_admin_acl.py`，真实 PostgreSQL +
完整 API 链路 + Bearer JWT（`sign_jwt` + `user_sessions` 落库），不以 stub 头代替认证：

- RED（改动前）：4 个期望 403 的用例分别得到 201 / 200 / 204 / 200，2 个对照用例通过。
- GREEN（改动后）：本文件 + `test_id_mapping_api.py` + `test_model_config_rbac.py`
  共 21 passed。
- 回归：id_mapping 相关 9 个套件 → 9 failed / 91 passed；同一批 9 个失败在
  pristine `origin/main` worktree 上**逐条复现**（`test_ontology_reconcile_sync.py`
  8 条 + `test_ontology_unified_id.py::testCreateClass_neo4j_receives_unified_id`），
  属既有基线红，**净新增失败 0**。

用例覆盖：普通用户 POST/PUT/DELETE/整表列举 → 403 且库内 `pg_id`/行/总行数未被改动；
普通用户单点读 → 200；admin Bearer 创建 → 201、列举 → 200、删除 → 204（守卫不误伤管理员）。
403 断言除状态码外还校验 `detail.code == "PERMISSION_DENIED"`，防止其它来源的 403 冒充守卫生效。

## 不在本 PR 范围（待讨论）

- **② DTO 收紧**：`IdMappingCreate` 仍接受 `pgTable/pgId` 等跨库指针，需先改
  `ontology_service` 等 6+ 内部调用点，再禁止客户端声明。
- **③ 回填脚本与变更记录归档**：`backfill_id_mapping.py`、`id_mapping_realdata.py`
  等仍在 `backup/evidence-sql-metric-docs` 分支。

## 已知遗留（不在本 PR 修）

1. **服务层旁路**：`POST /api/v1/ontology/classes`、`/wiki/compile` 等接口（仅
   `getCurrentUser`）在服务层直接调 `IdMappingService.register/update`，普通用户仍能
   间接写入 id_mapping 行。写入的指针全部由服务端硬编码派生（如 `ontology_class` +
   `entity.id`），客户端无法注入跨库指针，故不构成越权，但"写入口 = admin"在 HTTP 面
   并未 100% 成立；是否把 ontology 写路径也收 admin 需随 ② 一并讨论。
2. **stub auth 默认带 admin**：`AUTH_MODE=stub`（dev/test 默认）下
   `DEFAULT_STUB_ROLES` 含 admin，`requireAdmin` 形同虚设；生产由
   `validateProduction` 强制 `AUTH_STUB_ENABLED=0` 兜底，非本次引入。
3. 本文件既有的未使用 import（`IntegrityError`）与未使用变量（list 分支的 `total`），
   main 上即存在，未在本次顺手清理。
