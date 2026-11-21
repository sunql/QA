# 变更：采购平台 V0.3 · M0 底座（A1 本地栈同构核对与补齐）

- **日期**：2026-11-18
- **作者**：Claude
- **Phase**：M0 底座 / W0 前置（A 线：本地栈同构）
- **状态**：done（**已合入 main**，分批合入 1/4；**生产未部署**）
- **关联变更**：[m0-id-mapping-a1](../m0-id-mapping-a1/summary.md)（同族的 A1 前置线）、[fix-schema-ssot-alignment](../fix-schema-ssot-alignment/summary.md)（schema SSOT 对齐，判据 1 的前置）
- **迁移版本**：`0153_reconcile_procurement_class_source_tables`（**纯数据迁移，无 DDL**）
- **MEMORY**：[../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-procurement-v03-landing-program.md](../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-procurement-v03-landing-program.md)
- **验收包**：[docs/update/1118/M0/A1-本地栈同构核对表.md](../../../docs/update/1118/M0/A1-本地栈同构核对表.md)

> 用户已拍板的三条前提（2026-11-18）：**生产暂不部署**；**功能需要全部完成**；
> **本地全绿后统一部署**。本变更只做**本地**侧，不碰生产。

---

## 1. 需求

**用户视角**：智能采购平台 V0.3 的落地计划要在**问卷回收期间**并行开工（计划见
`docs/update/1118/M0/业务问卷/`），但开工第一件事不是写业务代码，而是让
**「本地全绿」这个判据真的成立**——否则后面每一次「本地跑过了」都可能是假的。

**验收标准（可测量）**：

1. **本地测试栈与 CI 同构**：三个测试依赖（PG / Neo4j / Milvus）在本地与 CI 指向**同一套隔离栈**，
   且**只有一个入口**能启动本地测试（不接受「手拼环境变量」）。
2. **判据 1（迁移）可判定**：在**干净 probe 库**上 `alembic upgrade head` → `alembic check` →
   `downgrade -1` → `upgrade head` → `check_schema_drift.py` **全绿**（实测输出贴进验收包）。
3. **采购域本体类的物理表绑定只有一个词表**：迁移链与 `seed_ontology` 在同一 `class_name` 上
   写**同一个** `source_table`；干净库上 `seed_ontology.seed()` **不崩**。
4. **测试不得连错库**：源码级守卫钉死「自带硬编码 DSN / 读开发库 URL / 旧端口+测试库组合」三类形态。
5. **本轮触及的用例全绿**，且不引入新的失败（失败集合对差，净新增 = 0）。

---

## 2. 设计评审

### 2.1 候选方案对比（本体类物理表绑定）

| 方案 | 做法 | 代价 | 结论 |
|---|---|---|---|
| **A. 改 `seed_ontology.CLASSES`，迁就迁移的 DWD_* 词表** | 把本体 SSOT 改成 `DWD_SUPPLIER` 等 | ❌ **方向反了**：那些表在系统的查询目标里**不存在**（本地栈只有 `BPSUPPLIER` / `PORDER` / `PORDERQ` 三张真实 ERP 表）⇒ NL2SQL 会生成指向不存在表的 SQL；且 DWD 是**数仓分层词表**，本系统的查询目标不落 DWD 层 | 否决 |
| **B. 数据迁移把绑定校准到 ERP 词表（本批采用）** | 新增 `0153`：**纯数据迁移**（无 DDL）把 5 个采购域类的 `source_table` 从 DWD_* 改成 ERP 词表（`IncomingInspection` 置 NULL） | 迁移链与 `seed_ontology` 的历史分叉被**一次性收敛**；`alembic check`/drift **不受影响**（无 DDL）；`downgrade` 可还原 | **采用** |
| **C. 只改迁移链、不动 seed_ontology 的查重键** | 把 `_seedClasses` 的查重键从 `source_table` 改成 `class_name` | ⚠️ 能解「seed 崩」，但**解不了「NL2SQL 指向不存在的表」**；且 `source_table` 仍分叉 ⇒ 两个种子继续分叉 | 否决（作为 B 的补充不采用） |

### 2.2 候选方案对比（本地栈同构）

| 方案 | 做法 | 代价 | 结论 |
|---|---|---|---|
| **A. 继续用 5433/qa_metadata_test** | 沿用旧端口 | ❌ 该库停在 0139（schema 缺列）⇒ `_ensureSchema()` 的 `alembic upgrade head` 抛 `DuplicateColumnError` ⇒ **整套测试跑不起来** | 否决 |
| **B. 换到 5434/qa_metadata_test（CI 同款）+ 唯一入口脚本** | 新增 `scripts/run_local_tests.sh` 一次性接线三依赖；测试代码统一走 `_pg_support._newEngine()` | 需要改若干测试文件的取库方式 | **采用** |
| **C. 把 CI 改成用 5433** | 让 CI 迁就本地 | ❌ 与 CI 同款的其他环境（本机另一套栈）不一致；且「同款」的价值就是 CI 验证的就是要部署的那套 | 否决 |

### 2.3 最终决定

- **绑定**：方案 B（`0153` 数据迁移）+ `seed_ontology._seedClasses` 复用路径补 `class_alias`/`description` 回写
  （与 `_seedProperties` 对别名/描述的口径一致——类级此前漏了这两项，导致 NL2SQL 渲染的 schema 文本缺中文别名、
  描述还写着「结构性建模，0 行」）。
- **同构**：方案 B；`scripts/run_local_tests.sh` 成为本地跑后端测试的**唯一入口**。
- **防回退**：新增 AST 守卫（`test_test_db_url_guard.py`），**不等它跑出假绿再查**。

---

## 3. 数据模型变更

**无 DDL** —— 本批唯一的迁移是**纯数据迁移**，因此 `alembic check` 与 `check_schema_drift.py`
的结论**不受影响**（这正是选它的原因之一）。

### 3.1 `0153_reconcile_procurement_class_source_tables`（数据迁移）

| 类 | 改前 `source_table`（`0039`/`0040` 写的） | 改后（= `seed_ontology.CLASSES` 词表） |
|---|---|---|
| `Supplier` | `DWD_SUPPLIER` | **`BPSUPPLIER`** |
| `ItemMaster` | `DWD_MATERIAL` | **`ITMMASTER`** |
| `PurchaseOrder` | `DWD_PURCHASE_ORDER` | **`PORDER`** |
| `Receipt` | `DWD_GOODS_RECEIPT` | **`PRECEIPT`** |
| `IncomingInspection` | `DWD_INCOMING_INSPECTION` | **`NULL`**（来料检验无物理表） |

- 实现：`UPDATE ontology_class SET source_table = :canonical, updated_time = now() WHERE class_name = :class_name AND source_table = :legacy`
  （`IncomingInspection` 用 `IS NULL` 守卫）——**全部走 bind 参数**，无字符串拼接 SQL。
- 幂等：WHERE 带旧值条件 ⇒ 重复执行影响 0 行。
- `downgrade()` 逆向还原为 DWD_* 词表 ⇒ **升降级可往返**（实测 `0153 → 0152 → 0153`）。
- **内容（描述 / 别名）不由迁移负责**——那是 `seed_ontology` 的 SSOT 职责。

### 3.2 `ontology_class` 表结构

**零变化**（无新表 / 新列 / 新索引 / 新 CHECK 约束）。

---

## 4. 接口契约变更

**无**。本批不新增 / 修改 / 删除任何 API、DTO 字段或状态机。

- 唯一「对外可见」的变化是**数据语义**：NL2SQL 渲染 schema 时，采购域 5 个类现在指向**真实存在**的物理表
  （改前指向不存在的 DWD_* 表）。

---

## 5. 实现要点

| 文件 | 作用 | 关键点 |
|---|---|---|
| `backend/alembic/versions/0153_reconcile_procurement_class_source_tables.py` | 绑定校准（数据迁移） | 纯数据、幂等、可往返；bind 参数 |
| `backend/seed_ontology.py` | `_seedClasses` 复用路径补 `class_alias`/`description` 回写 | 与 `_seedProperties` 口径一致；只增不删（seed 未提供则保留 DB 既有值） |
| `backend/scripts/procurement_class_source_table_realdata.py` | **真实数据门禁**（SSOT 要求 5） | 自建一次性干净库 → 真跑 `alembic upgrade head` → 断言 5 个绑定 + `object_type`/`version` + **反向断言无 `DWD\_%` 残留** → 跑 `_seedClasses`（**只跑类种子 ⇒ 不碰 Neo4j**）断言复用不崩 → 删库。退出码 0/1/2；**库名白名单自校验**（脚本会 DROP 它）+ `URL.set()` 派生 DSN（口令特殊字符由 SQLAlchemy 转义）+ 建/删库前 `pg_terminate_backend` + **清理失败非零退出** |
| `scripts/run_local_tests.sh` | **本地跑后端测试的唯一入口** | 一次性接线 PG 5434 / Neo4j 7688 / Milvus `qa_test`；口令**只从 `.env` 读**（不写死）；端口可达性前置检查；Neo4j 必须指 7688（7687 是开发实例） |
| `backend/app/tests/conftest.py` | 显式给了 `TEST_DATABASE_URL` ⇒ 让 `DATABASE_URL` 跟它对齐 | 让「万一有人读了 `settings.databaseUrl`」时两边仍一致（**根源**由守卫 R3 堵死） |
| `backend/app/tests/unit/test_test_db_url_guard.py` | 源码级 AST 守卫 R1–R4 | 全部 AST 判定（不用正则：正则会被自己写的注释/docstring 命中 ⇒ 假红）；**每条规则都有正向样本**（空集假绿防护）+ 反向样本（不误伤样本 DSN 字符串） |
| `backend/app/tests/integration/test_l2_cte_avg_of_ratios.py` | teardown 删掉 sandbox 表 | 共享测试库是 `alembic check` 的判定目标 ⇒ 测试期建的 `po_lines_sandbox` 会让判据 1 假红。⚠️ 必须**先 `dbSession.rollback()`**（未提交事务持 ACCESS SHARE ⇒ DROP 无限等锁，实测挂死 10 分钟）+ `SET lock_timeout='15s'` 兜底 |

**依赖注入点**：本批**无** FastAPI `Depends` 变更；测试侧引擎一律由
`app.tests._pg_support._newEngine()` 提供（`resolveTestDatabaseUrl()` + `assertTestDatabase()` 白名单 + `_ensureSchema()`）。

---

## 6. 测试

### 6.1 新增 / 重写用例

| 文件 | 用例 | 断言要点 |
|---|---|---|
| `app/tests/unit/test_test_db_url_guard.py` | `test_noHardcodedTestDbDsnInTestSources` + R1–R4 各一条正向样本 + 2 条反向样本 + `test_eachRuleHasPositiveControl` | 全量扫 `app/tests/**`；**空集假绿防护**（每条规则都有正向样本） |
| `app/tests/integration/test_supplier_itemmaster_purchaseorder_receipt_class.py` | `test_four_classes_bound_to_business_tables`、`test_canonical_seed_replay_is_idempotent` | 4 个类的 `source_table` = ERP 词表 + `version==1`；重放幂等 |
| `app/tests/integration/test_incoming_inspection_class.py` | `test_incoming_inspection_has_no_business_table_binding`、`test_canonical_seed_replay_is_idempotent` | `source_table IS NULL` + `version==1`；重放幂等 |
| `app/tests/integration/test_seed_business_objects.py` | `test_first_run_inserts_five_mappings`、`test_second_run_is_idempotent`、`test_missing_header_class_skips_row_and_creates_it_when_class_arrives`、`test_codes_are_uppercase`、`test_graph_labels_resolve_to_class_names` | 5 行 + **NCR 不存在**；`header_class_id` 非 NULL 且指向正确类 id；类缺失 ⇒ **0 行**、类到达 ⇒ 建行且绑定正确、再 seed 幂等 |
| `app/tests/integration/test_ontology_governance_integration.py` | `test_seed_backfills_governance_fields`（改断言） | CLASSES 域内**恰好各一行且无重复**；额外行**只允许** `IncomingInspection`；逐类 `object_type`/`object_owner` 契约不变 |
| `app/tests/integration/test_admin_user_password.py` | `_rbacSeeds`（改前置） | 清 `admin.must_change_password`（守卫 403 前置）+ 用真实种子 `seed_menu_config` 补菜单（`permission_grant` 外键前置）；4 个用例断言不变 |
| `app/tests/integration/test_wiki_batch_delete_api.py` | `test_batch_delete_requires_authentication`（改前置） | `monkeypatch.setenv` 后 **`getSettings.cache_clear()`**（`@lru_cache` 否则仍走 stub=true ⇒ 实测 200）；`finally` 恢复 env 再清缓存；断言不变 |

### 6.2 实测结果（2026-11-18）

| 批次 | 结果 |
|---|---|
| A1 触及的 6 个文件（`test_test_db_url_guard` + 5 个集成文件） | **31 passed**（此前 6 red） |
| 第二批过期用例（`test_admin_user_password` 4 条 + `test_wiki_batch_delete_api` 1 条） | **8 passed**（改前置后；此前 5 red，且这两个文件自 2026-09-24 未改动 ⇒ **长期红，非本批回归**） |
| 判据 1（干净 probe 库） | `upgrade head`→`0153 (head)`；`alembic check`→`No new upgrade operations detected.`（exit 0）；`downgrade -1`→`0152`→`upgrade head`→`0153`；`check_schema_drift.py`→`Schema drift 检查通过`（exit 0） |
| 真实数据门禁 `scripts/procurement_class_source_table_realdata.py` | **exit 0**（日志：`class exists: BPSUPPLIER (id=2, name=Supplier)` ⇒ 复用迁移行，不崩）；**评审修复后（URL 派生 / 清理非零退出 / 建库前掐连接）复跑仍 exit 0** |
| 变异反证（把库 downgrade 回 0152 再跑门禁） | 门禁**变红** ⇒ 门禁不是空集假绿 |
| **评审修复后的全量复核**（`./scripts/run_local_tests.sh`，19 个文件 = 本分支触及的全部测试 + 守卫 + `test_user_clearance.py`） | **122 passed / 0 failed**（14:24，exit 0） |

### 6.3 覆盖率

本批改的是**迁移 / 种子 / 测试基座**，新增可执行逻辑集中在
`procurement_class_source_table_realdata.py` 与 AST 守卫 —— 两者均被**端到端实跑**
（前者 exit 0，后者全量扫 `app/tests/**` 并对每条规则做正向/反向样本验证）。
仓库级覆盖率门禁（`--cov-fail-under=80`）在**全量绿跑**时产出，本批未跑全量
（后端全量 unit 单文件可能 27 分钟且含既有红基线，见 [判据书 §1.3](../../../docs/update/1118/M0/本地全绿判据书.md)）。

---

## 7. 安全审查

- **触发 `security-reviewer`**（改动涉及：DDL/DML 迁移、数据库连接与建库/删库、用户输入边界、环境变量读取）。
- 结论：见 §7.1。

### 7.1 安全审查结论

由 `security-reviewer` 审阅本分支 diff 得出（同批 `code-reviewer` 的质量项一并并入）。
**0 CRITICAL / 2 HIGH（全修）/ 4 MEDIUM（全修）/ 7 LOW（6 修、1 记档）**。

| 级别 | 发现 | 处置 |
|---|---|---|
| CRITICAL | 无 | — |
| **HIGH** | **H1 测试库闸只校验库名、不校验主机**：夹具每用例 `TRUNCATE ... CASCADE`、`_ensureSchema()` 还要跑 alembic DDL；远端/共享 PG 上若恰好有 `qa_metadata_test*` 同名库 ⇒ 只凭库名放行 | **已修**：`assertTestDatabase()` 增**回环主机**白名单（`""`/`localhost`/`127.0.0.1`/`::1`），非回环须显式 `QA_TEST_DB_ALLOW_REMOTE=1`；`resolveTestDatabaseUrl()` 的「只改写库名」分支**改写后再过一次**双白名单（否则「生产主机 + 测试库名」仍会放行） |
| **HIGH** | **H2 唯一测试入口脚本写死开发口令 + 明文打印完整 DSN**：口令进终端回滚缓冲与 CI 日志 | **已修**：兜底口令删除 ⇒ 找不到即 fail-fast（exit 2，提示去 `backend/.env`/`docker/.env` 设或显式传 `QA_TEST_PG_PASSWORD`/`QA_TEST_NEO4J_PASSWORD`）；日志改 `user:***@` 打码 |
| MEDIUM | **M1 进程级环境改写无校验**：`conftest.py` 把 `TEST_DATABASE_URL` 原样拷进 `DATABASE_URL`，所有非 `_newEngine()` 路径（`settings.databaseUrl`、后台 `getEngine()`）跟着走 | **已修**：**拷贝前**先过 `assertTestDatabase()` |
| MEDIUM | **M2 失败消息回显 DSN 字面量**（守卫 R4 的违规值前 80 字符通常正含口令） | **已修**：守卫加 `_redactDsn()`（`//***@` + 截断 80 字符） |
| MEDIUM | **M3 门禁脚本 f-string 手拼 DSN**：口令含 `@` / `/` / `:` 时会拼出错误主机（连错库）；`pg_terminate_backend` 的 `datname` 走 f-string 拼接 | **已修**：改用 `URL.set()` + `render_as_string(hide_password=False)`（SQLAlchemy 负责转义）；`datname = :name` 改 bind 参数 |
| MEDIUM | **M4 门禁脚本清理失败只 WARN 且仍 exit 0**（残留 scratch 库被一行「[OK]」掩盖）；建库前不掐残留连接 | **已修**：清理失败 ⇒ 非零退出（成功用例由 0 改 2）；建/删库前都先 `pg_terminate_backend` |
| LOW | L1 守卫 R1 只认**位置**实参 ⇒ `create_async_engine(url="...")` 漏网；R2 只看模块/类作用域 ⇒ 函数内常量漏网 | **已修**：R1 补 `url=` 关键字实参；R2 改 `ast.walk` 覆盖**任意作用域**；新增 2 条正向样本（共 13 passed） |
| LOW | L2 守卫扫描**空集假绿**：目录被挪/路径拼错时「0 违规」恒通过 | **已修**：`_testSources()` 钉死「≥100 个 .py」+ conftest 锚点必须在扫描集内 |
| LOW | L3 迁移 `0153` 的 `WHERE` 未锚 `version = 1`（`uq_ontology_class_name_version` 允许多版本共存 ⇒ 可能误改后续版本的行） | **已修**：upgrade / downgrade / `IS NULL` 分支三处都补 `AND version = 1` |
| LOW | L4 `SCRATCH_DB_NAME` 未做白名单自校验（脚本会 `DROP DATABASE` 这个名字） | **已修**：`_scratchDbNameError()` 在**任何 DB 操作之前** fail-fast |
| LOW | L5 两个集成用例 `_newEngine()` 无 `try/finally: dispose()`（断言失败时连接池泄漏，掩盖真实错误） | **已修**：`test_business_object_migration.py`、`test_business_object_entity_type_fk.py` |
| LOW | L6 `wiki_dedup_realdata.py` 的 docstring / 写库闸消息写死**旧端口 5433 + 明文开发口令** | **已修**：改 5434 + `<user>:<pwd>` 占位 |
| LOW | L7 `backend/app/config.py` 的开发默认口令（`DATABASE_URL` / `neo4jPassword` 默认值）与仓内多处「样本 DSN 含开发口令」 | **不改**（既有基线、非本批引入；生产由 env 覆盖）⇒ 记入 **M7 卫生项**（仓库级口令清理要单独立项，避免本批 diff 扩大到全仓） |

**未引入的新面**：无新依赖、无新外部网络调用、无新 API 端点、无新权限点；
本批对数据库的动作只有「数据迁移 UPDATE（bind 参数）+ 一次性 scratch 库 CREATE/DROP」。

---

## 8. 部署验证

**本批不部署**（用户 2026-11-18 决策：生产暂不部署，功能全完成后统一发版）。

### 8.1 本地冒烟

| 检查 | 结果 |
|---|---|
| 判据 1 全链（干净 probe 库） | ✅ 见 §6.2 |
| A1 触及用例（评审修复后复核） | ✅ **122 passed / 0 failed** |
| 真实数据门禁脚本 | ✅ exit 0 |
| Neo4j | ✅ 本批**未触碰**（门禁脚本只跑 `_seedClasses`，不跑全量 `seed()` ⇒ 不同步图谱） |
| Milvus | ✅ 本批**未触碰**（无向量/图谱写入） |

### 8.2 真实数据验证报告（`scripts/procurement_class_source_table_realdata.py`）

- **跑的是什么**：自建一次性干净库 `qa_metadata_test_purcclass` → 真跑 `alembic upgrade head`
  → 逐类核对 `source_table`/`object_type`/`version` + 反向断言无 `DWD_*` 残留
  → 跑 `seed_ontology._seedClasses`（类种子，**不碰 Neo4j**）→ 断言 4 个类**复用**迁移行（id 不变、行数不增）→ 删库。
- **发现的真实 bug**：迁移 `0039`/`0040` 与本体 SSOT 分叉，导致**干净库上 `seed_ontology.seed()` 直接崩**
  （`duplicate key value violates unique constraint "uq_ontology_class_name_version"`，Key `(class_name, version)=(Supplier, 1)`），
  且 NL2SQL 会指向**不存在的表**。→ 由 `0153` 修复。
- **绕过的理由**：无（全部真实链路，无 mock）。

---

## 9. 关联

- **设计稿 / 计划**：[docs/update/1118/M0/业务问卷/A-采购部-指标口径与制度规则.md](../../../docs/update/1118/M0/业务问卷/A-采购部-指标口径与制度规则.md)（问卷与回收登记）、[docs/update/1118/M0/本地全绿判据书.md](../../../docs/update/1118/M0/本地全绿判据书.md)（七条判据）
- **验收包**：[docs/update/1118/M0/A1-本地栈同构核对表.md](../../../docs/update/1118/M0/A1-本地栈同构核对表.md)
- **Wiki**：[Harness/wiki/operations-runbook.md](../../wiki/operations-runbook.md)（测试栈 5434 端口契约）、[Harness/wiki/data-model.md](../../wiki/data-model.md)（`ontology_class.source_table` 绑定词表）
- **Rules**：[Harness/rules/数据库环境使用规范.md](../../rules/数据库环境使用规范.md)（测试栈 5434）、[Harness/rules/开发流程规范.md](../../rules/开发流程规范.md)（端口契约）、[Harness/rules/测试规范.md](../../rules/测试规范.md)（示例 DSN）
- **Memory**：`~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-procurement-v03-landing-program.md`（主）、`.../qa-system-a1-local-stack-isomorphism.md`（本批）
- **关联变更（predecessor / successor）**：[m0-id-mapping-a1](../m0-id-mapping-a1/summary.md)；successor = A2 开关落地 / A4-0（**迁移号从 0153 顺延为 0154** —— 0153 已被本批占用，**不许跳号**）
