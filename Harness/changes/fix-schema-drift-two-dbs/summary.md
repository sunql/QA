# 变更：收敛两库结构漂移（0060）+ 订正卡死的导入任务 #8

- **日期**：2026-09-12
- **作者**：AI 助手
- **状态**：done
- **触发**：用户要求核查「把 `qa_metadata_test` 有、`qa_metadata` 没有的表和数据补到 prod」

## 0. 先纠正一条错误结论（重要）

上一轮我报告过「对照 `qa_metadata_test` 反而有 28 MB」，并据此推断 test 库有数据。
**该结论是错的**：28 MB 是 `TRUNCATE` 留下的死元组膨胀，不随 `TRUNCATE` 回收；
test 库精确 `count(*)` 只有 **7 行**（`business_object` 6 + `alembic_version` 1），
prod 是 **2949 行**。两库**表数相同（各 56 张）**，不存在「test 有而 prod 没有的表」。

→ 用户请求（把 test 的表/数据补到 prod）**无对象可执行**，prod 未做任何数据写入。

**教训**：判据永远用精确 `count(*)`，不用体积、也不用 `pg_stat_user_tables.n_live_tup`
（后者 TRUNCATE 后不刷新，把空表报成 42 行）。

## 1. 真实问题：双向 schema 漂移

核查过程中发现真差异 —— 两库 `alembic_version` **都是 `0059_wiki_coverage_tables`**，
但结构并不相同：

| 项 | test | prod |
|---|---|---|
| `users` 列 | 7 | 11 |
| `ix_menu_config_visible_sort` | 有 | **无** |
| `ix_users_must_change_password` | 无 | 有 |

**根因**：Alembic 只记录「迁移脚本执行到哪一版」，**不校验实际 DDL 是否等于该版
应有的样子**。prod 是 dump 恢复出来的，`alembic_version` 一进去就是高位，
`upgrade head` 看到「已在 head」直接跳过 → 早年迁移（0033）建的对象永远不会补上；
反向地，手工 DDL 也不会被任何迁移接管。

- prod 缺的索引 `ix_menu_config_visible_sort`：由 `0033_menu_config.py:67` 创建，
  `app/models/menu_config.py:62` 也一直声明着，唯独 prod 没有
- prod 多的 4 列 + 1 部分索引：来自 `docs/superpowers/plans/2026-09-08-user-auth.md`
  —— 该计划的密码登录**从未落地**（`app/models/rbac.py` 的 `User` 至今无密码字段），
  是手工 DDL 残留，全库 grep 只在计划文档里出现过

**既有防线为何没拦住**：`scripts/check_schema_drift.py` 的比对粒度是**表**，
不查列与索引（`feat-schema-drift-detector` 的「遗留」章节已预告过这个盲区）。

## 2. 落地：Alembic 0060

`backend/alembic/versions/0060_reconcile_menu_index_and_users_legacy_cols.py`

- `ALTER TABLE users ADD COLUMN IF NOT EXISTS` × 4（`password_hash VARCHAR(255)`、
  `must_change_password BOOLEAN NOT NULL DEFAULT false`、`last_login_at TIMESTAMPTZ`、
  `last_login_ip VARCHAR(45)`）—— 与 prod 实际形状**逐字对齐**（类型/可空/默认值），
  否则新建库与 prod 只是列名相同、行为不同
- `CREATE INDEX IF NOT EXISTS ix_users_must_change_password ... WHERE must_change_password = true`
- `CREATE INDEX IF NOT EXISTS ix_menu_config_visible_sort ON menu_config (visible, sort_order)`

**决策：不删那 4 列。** prod 上唯一 admin 的 `password_hash` **非空（60 字符 bcrypt）**、
`must_change_password = true` —— 是真实凭据材料，不是空列。
**决策：downgrade 只删索引、不删列**（刻意不对称）：这些列早于本迁移存在且带数据，
`alembic downgrade` 不该成为隐形的数据销毁路径。

## 3. 订正：卡死的导入任务 #8

`wiki_import_task` #8 停在 `RUNNING` / success 0 / page_ids 空 / finished_time 空，
创建于 01:02:33 UTC，其下有 62 页真实落库（01:02:35–01:03:58）。
（注：这 62 页**不是孤儿**——`imported_via_task_id = 8` 一直挂着；缺的是 task 侧的 `page_ids`。）

**成因**：`execute` 先 commit 一条 `RUNNING` task 再逐条导入；进程被外力杀死
（容器重建 / OOM / kill -9）时，兜底的 `except` 分支根本不会执行 ——
`_markFailedBestEffort` 只在「异常抛到 Python 层」时生效，覆盖不了「进程直接没了」。

**修复口径 = 复刻 `_markFailedBestEffort` 语义**（不自创）：
`status = FAILED`、`page_ids`/`success_pages` ← 从 `wiki_page.imported_via_task_id` 反查
真实 62 条、`total_cost_usd` ← `wiki_token_usage` SUM（**0.063437**）、
`finished_time` ← 最晚一条页时间、`failed_pages` 保持 0（无条目「冲突/校验失败」，
未处理的 13 条体现为 `total_pages - success_pages`）。

工具：`backend/scripts/repair_stuck_import_tasks.py`（幂等：只处理 `status='RUNNING'`；
默认预览，`--apply` 落库；用 Core UPDATE + status 守卫，避开 identity map 缓存歧义）。

## 4. 测试

- `backend/app/tests/integration/test_schema_reconcile_migration.py`（新增 4 用例）：
  4 个遗留列的类型/可空/默认值 + 两个索引的 `indexdef`（含部分索引谓词）+
  迁移 DDL 在「对象已存在」库上重放为 no-op。
  断言直查 `information_schema` / `pg_indexes` 真实 PG，**不读 ORM 元数据**
  —— 本迁移的存在意义正是「ORM 元数据与实际结构不一致」，用 ORM 自证是同义反复。
  **RED→GREEN 已实证**：迁移前 2 failed（`users.password_hash 缺失`、
  `缺少 ix_users_must_change_password`），迁移后 4 passed。
- 回归（0060 触及的两张表）：`test_rbac_api` / `test_menu_config_api` /
  `test_menu_config_service` / `test_menu_tree_api` / `test_seed_menu_config`
  + 新增用例 → **66 passed**。

## 5. 部署与验证（prod）

```bash
# 1) 备份（《数据库环境使用规范》§2）
docker exec qa-postgres pg_dump -U qa_user -d qa_metadata -t users        > /tmp/users_20260912.sql
docker exec qa-postgres pg_dump -U qa_user -d qa_metadata -t menu_config  > /tmp/menu_config_20260912.sql
docker exec qa-postgres pg_dump -U qa_user -d qa_metadata -t wiki_import_task > /tmp/wiki_import_task_20260912.sql

# 2) 先重建镜像（baked alembic 含 0060），再 up —— 避免「DB 已 0060 / 容器仍 0059」
#    的窗口期触发 drift 阻断（容器 CMD 会跑 alembic upgrade head 自愈）
docker compose build backend && docker compose up -d backend
```

验证结果：

- 容器日志：`Running upgrade 0059_wiki_coverage_tables -> 0060_schema_reconcile`，
  `Application startup complete.`，`RestartCount = 0`
- **两库列/索引 diff 现为 IDENTICAL**（`information_schema.columns` + `pg_indexes` 全量比对）
- 行数**逐项不变**：`users=1 menu_config=44 wiki_page=587 wiki_import_task=8`
- admin 凭据完好：`has_hash=t hash_len=60 must_change_password=t`
- API 冒烟：`GET /wiki/import/tasks` 返回 #8 = `FAILED` 且 `pageIds` 62 条；
  `GET /menu-config` → 200
- 修复脚本重跑 → 「没有处于 RUNNING 的导入任务，无需修复。」（幂等成立）

## 6. ORM 正式建模（`users` 4 列 + 部分索引）

用户拍板「要正式建模进 `User`，不过修改表或处理数据之前先备份」。
**本次是纯代码变更，不动表、不动数据** —— 列与索引已由 0060 落库，这一步只是让
ORM 认识它们。

**先备份**：`./scripts/backup_pg.sh` → `backups/pg/qa_metadata_2026-09-12_0921.dump`
（430454 B，脚本自带 14 天保留策略）。备份在改动之前完成，符合用户指令。

`backend/app/models/rbac.py` 的 `User`（+35 −1）：

- 4 列显式声明：`password_hash VARCHAR(255) NULL`、
  `must_change_password BOOLEAN NOT NULL DEFAULT false`（`server_default=text("false")`，
  与 DB 默认值一致才不会被 autogenerate 反复判为差异）、
  `last_login_at TIMESTAMPTZ NULL`、`last_login_ip VARCHAR(45) NULL`
- `Index("ix_users_must_change_password", "must_change_password", postgresql_where=text("must_change_password = true"))`
  —— 谓词必须一起声明；丢了 `postgresql_where` 就退化成全表索引，是另一个东西
- 类 docstring 改写：说明这 4 列的来源计划、为何现在才建档、以及**声明它们不是为了
  启用密码登录**，而是让 `alembic autogenerate` 不再提议 DROP

**验证**（这是本次的核心交付，不是「测试绿了」）：

- `alembic check` 对着 prod 与 test 两个库跑，输出里**零次**出现这 4 个列名与
  这个索引名 → DROP 提议消失，建模范式的目的达成
- 直接对 prod 比对：`ORM 列 == DB 列 ? True`（两侧差集均为空）；prod 的 4 个 `users`
  索引全部有 ORM 对应物（`users_pkey` ← PK、`uq_users_username` ← UniqueConstraint、
  `ix_users_enabled` / `ix_users_must_change_password` ← Index）
- **残留噪音（预存，非本次引入）**：autogenerate 仍报 76 个 `remove_index` +
  38 个 `remove_constraint`，散在 12 张表（如 `ck_agent_permission`、
  `ix_agent_definition_owner`、表达式索引 `suggested_at DESC` 被读成 `suggested_at`）。
  这批与本特性无关，已在控制变量 A/B 中确认改动前后完全一致。

**泄漏面核查**（新进的 `password_hash` 不能顺着 DTO 漏出去）：

- `UserRead` / `UserCreate` / `UserUpdate` 均为显式字段声明（无 `from_attributes`
  全字段透出），`update_user` 逐字段赋值，审计 `_user_payload` 走白名单 → 三处均不含
  `password_hash`
- 该不变量由 `test_password_hash_never_exposed_in_read_dto` 钉住

**测试**：`backend/app/tests/integration/test_user_orm_matches_schema.py`（新增 4 用例，
RED 3 failed → GREEN 4 passed）。第一条用例断言的是 **ORM 列集合与 DB 双向相等** ——
「ORM 少列」（autogenerate 真 DROP 掉数据）和「ORM 多列」（迁移漏了、新建库缺列）
方向相反但由同一条断言拦下。

**回归**：`test_rbac_api` / `test_menu_config_api` / `test_menu_config_service` /
`test_menu_tree_api` / `test_seed_menu_config` + 本次两个新增测试文件 → **70 passed**。

## 7. 决策与遗留

**决策**：
1. 漂移走**迁移**收敛（而非手改 DDL），让新建库与 prod 结构可复现
2. 遗留列「写进迁移建档」而非删除 —— prod 上有真实 bcrypt 凭据
3. 卡死任务记 `FAILED`（代码对「异常中止」用的就是这个值，不是 `PARTIAL`）
4. 遗留列**正式建于 ORM**（§6）：迁移负责「新建库也有这些列」，ORM 负责
   「autogenerate 不会把它们 DROP 掉」，两侧合起来才算建档完成

**遗留（未做，待用户决定）**：
- **`check_schema_drift.py` 仍只比表级**：本次漂移正是列/索引级，
  建议扩到列 + 索引粒度（`feat-schema-drift-detector` 遗留项）。
- **`wiki_import_task.page_ids` 只写不读**：模型 docstring 说「失败重跑时用来跳过
  已成功项」，但全后端/前端 grep 无任何读取点。后果是重跑同批次会产出**又一份完整
  副本**（`page_id` 每次重新生成）—— 库里 587 条 `wiki_page` ≈ 同一份 75 条文档的
  7.8 份副本就是反复试导入留下的。#8 的 13 条未处理项若要补齐，目前只能整批重跑。
- **`entity_mapping` 35 万行不在任何现存备份里**：09-09 那份 dump 本身已是丢数据
  之后的状态，本地备份链无处可寻，要恢复需外部来源。
