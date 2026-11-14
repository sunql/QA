# 变更：prod-ontology-kpi-restore

- **日期**：2026-11-14
- **作者**：Claude（phase3-reconciler）
- **Phase**：Phase 3.x 运维工具 / 数据迁移
- **状态**：done（脚本 + 手册已就绪并本地演练通过；**生产导出待人工执行**）
- **关联变更**：[2026-11-14-prod-release-1114](../2026-11-14-prod-release-1114/summary.md)（生产 1114 发布，提供生产 PG 环境事实）
- **迁移版本**：无（`--data-only` 导出不带 DDL，本地 alembic 保持 `0143` 不变）
- **MEMORY**：[qa-system-prod-ontology-kpi-restore.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-prod-ontology-kpi-restore.md)

---

## 1. 需求

把生产 PostgreSQL（`192.168.205.82`）中「**本体管理的指标**」与「**KPI 目录**」相关表的数据备份出来，
恢复到**本地测试环境的 Docker PG**（容器 `qa-postgres` / 库 `qa_metadata`），并提供一份可照做的操作手册。

验收标准：

1. 生产侧有**只读**的子集导出脚本，产物含行数快照与 sha256；
2. 本地侧有可重复执行的恢复脚本，具备**目标库白名单守卫**、**回滚锚点**、**单事务原子性**；
3. 恢复后能自动证明"数据一致、无孤儿、序列不落后"；
4. 出错可一键回滚到恢复前状态；
5. 手册覆盖：网络约束、生产侧命令、传输校验、恢复、验证、回滚、坑位表。

## 2. 设计评审

### 候选方案

| 维度 | 方案 A：整库 `pg_dump` 恢复 | 方案 B：表级子集 `pg_dump --data-only -t`（**选中**） | 方案 C：逐表 `COPY` 手工搬运 |
|---|---|---|---|
| 范围 | 全库（329 MB，本地）/ 生产更大 | 仅 13 张目标表（本地 152K） | 仅目标表 |
| 本地数据影响 | **全部本地数据被覆盖**（用户、会话、审计、35 万行 entity_mapping 全没） | 只替换本体/指标/KPI 子集，其余本地数据不动 | 只替换子集 |
| 生产写入风险 | 无（只读） | 无（只读） | 无 |
| 可重复执行 | 差（每次都重置全库） | 好（幂等：整表替换） | 差（手工、易漏表） |
| 回滚 | 靠另一份整库 dump | 靠脚本自动打的子集锚点（160K） | 靠自己备份 |
| 端到端耗时 | 分钟级 | 秒级 | 十几分钟手工 |
| 两端表清单漂移风险 | 无 | 有 → 用 SSOT 文件 `scripts/pg_subset_tables.txt` 两端共用消除 | 高 |
| DDL 影响 | 会带入生产 schema | 无（data-only，本地 alembic 不动） | 无 |

### 取舍与最终决定

选 **方案 B**。理由：用户诉求明确是"指标 + KPI 目录"，方案 A 会**摧毁本地其余业务数据**
（35 万行 `entity_mapping`、用户与会话、审计流水），风险与收益完全不成比例；
方案 C 的手工漂移风险在"生产数据要长期当本地种子"这个用途下不可接受。

方案 B 的三个固有风险，用三个机制消解：

1. **FK 阻断**：`entity_mapping → business_object(code) ON DELETE RESTRICT`，35 万行子行挡住删除
   → 恢复窗口内 `SET session_replication_role = replica` 关触发器，恢复后**强制跑孤儿审计**；
2. **跨边界 id 依赖**：`kpi_catalog.datasource_id → data_source(id)` 是**按 id** 引用，
   而 `data_source` 是本地环境资产不恢复 → 手册把"生产/本地 `data_source(id,name)` 逐条比对"列为**前置必检**，
   并给出 id 重映射预案（§5.3）；
3. **两端清单漂移** → 表清单收敛到单一 SSOT 文件，导出脚本与恢复脚本读同一份。

### 已否决的替代

- **把 `data_source` / `data_domain` 一起恢复**：会打断本地业务库连接（本地 5 条数据源是环境资产），否决；
  `data_domain` 因按 code 引用，只需校验 code 存在性。
- **用 `--disable-triggers` 代替 `session_replication_role`**：pg_restore 的该选项只作用于它自己的连接，
  无法覆盖"先 DELETE 再 COPY"的组合事务，否决。

## 3. 数据模型变更

**无 DDL、无 alembic 迁移。**

- 导出用 `pg_dump --data-only --no-owner --no-privileges -t public.<table>`：只出 COPY 语句与 owned 序列的 `setval`；
- 本地 schema 不变，`alembic_version` 保持 `0143`（生产亦 `0143`，两端一致）。

恢复涉及的 13 张表（清单 SSOT：`scripts/pg_subset_tables.txt`）：

| 分类 | 表 |
|---|---|
| 本体图谱 | `ontology_class`（自引用 `parent_class_id`）、`ontology_property`、`ontology_relation`、`ontology_join` |
| 本体指标 | `ontology_metric`、`ontology_metric_source` |
| 域映射 | `class_domain_mapping` |
| 业务对象 | `business_object`、`business_object_domain` |
| KPI 目录 | `kpi_catalog`、`kpi_catalog_history` |
| 配套 | `coverage_cell`、`id_mapping` |

**明确不恢复**（理由见 §2 与手册 §1）：`data_source`、`data_domain`、`entity_mapping`、
`feature_definition`、`document_entity_relation`。

## 4. 接口契约变更

**无 API 变更。** 本变更只动数据，不改任何接口、DTO、状态机。

恢复后需人工验收的既有接口（本地 5173 前端 / 8000 后端）：

- `GET /api/v1/kpi-catalog` —— KPI 目录列表；
- `GET /api/v1/admin/kpi/governance-summary` —— KPI 治理看板；
- `GET /api/v1/ontology/classes` —— 本体类列表。

## 5. 实现要点

| 文件 | 职责 |
|---|---|
| `scripts/pg_subset_tables.txt` | **SSOT**：13 张表清单 + 不恢复表的理由。导出与恢复两端共用 |
| `scripts/prod_pg_dump_subset.sh` | 生产侧**只读**导出：行数 manifest → `pg_dump --data-only` → gzip -9 → sha256 |
| `scripts/restore_pg_subset_local.sh` | 本地恢复 / 回滚：守卫 → 回滚锚点 → 单事务灌入 → 三项校验 |
| `Harness/changes/2026-11-14-prod-ontology-kpi-restore/runbook.md` | **操作手册**（用户交付物） |

### 关键实现决策

1. **守卫是结构性的，不只是参数校验**：恢复脚本只经 `docker exec` 操作**本地容器**，
   且目标库名必须匹配 `^qa_metadata(_test.*)?$` ⇒ 脚本在结构上无法写生产库
   （生产在 `192.168.205.82`，开发机 `nc` 不通）。
2. **单事务原子性**：`BEGIN; SET session_replication_role = replica;` → 按子→父 `DELETE` 13 张表 →
   灌入 COPY → `COMMIT;`，整体喂给 `psql -v ON_ERROR_STOP=1`。任一条 COPY 失败 ⇒ 整体回滚，本地数据原样不动。
3. **序列随 dump 走**：`--data-only -t` 的产物自带 `setval('public.kpi_catalog_id_seq', 19, true)`，
   不需要手工补序列（已实测）。
4. **回滚锚点自动打**：每次动手前 `pg_dump -Fc --data-only` 一份本地子集到 `backups/pg/`（已 gitignore），
   `--rollback-from` 走同一套事务机制（`pg_restore --data-only -f -` 转成 SQL 文本再入事务）。
5. **校验自动化**：逐表行数与生产 manifest 做 `diff`、9 项孤儿审计、序列 vs `max(id)`，
   三项全绿才算成功；manifest 用 `chr(9)` 分隔（`format('%s\t%s')` 在 PG 里输出的是字面 `\t`，会让 diff 全红）。
6. **可移植性**：bash 3.2（macOS）会把 `$VAR（` 里的全角括号并进变量名导致 `unbound variable`，
   全部改用 `${VAR}`；sha256 工具 `sha256sum`（CentOS）优先、`shasum -a 256`（macOS）兜底；
   表清单读取用 `while IFS= read -r` 而非 `mapfile`（bash 3.2 无 `mapfile`）。

## 6. 测试

无新增 pytest 用例（纯运维脚本，无应用代码路径）；改为**影子库端到端演练**，逐项留证：

| # | 场景 | 方法 | 结果 |
|---|---|---|---|
| T1 | 生产侧导出 | 对本地 `qa-postgres` 跑 `prod_pg_dump_subset.sh` | ✅ 产出 152K `.sql.gz` + manifest（13 表行数 + sha256） |
| T2 | 影子库构造 | 建 `qa_metadata_test_restore`，灌 schema + 复制 `entity_mapping`(354,422) / `feature_definition`(6) / `data_source`(5) 作为 FK 阻断物 | ✅ 复现 `entity_mapping → business_object(code)` RESTRICT 阻断 |
| T3 | 正向恢复 | `restore_pg_subset_local.sh --target-db qa_metadata_test_restore --apply` | ✅ 逐表行数对账 `全部一致`；9 项孤儿审计全 `0`；10 个序列全 `✅` |
| T4 | 守卫负例 1 | `--target-db postgres` | ✅ 拒绝，exit 1 |
| T5 | 守卫负例 2 | `--target-db qa_metadata_prod`（伪装生产名） | ✅ 拒绝，exit 1 |
| T6 | 守卫负例 3 | 目标库不存在 | ✅ 拒绝，exit 1 |
| T7 | 原子性（坏 dump） | 破坏 dump 尾部 COPY 结束标记后灌入 | ✅ 在 `COPY ontology_relation` 处报错，整体回滚，三表行数与灌入前**完全一致** |
| T8 | 回滚路径 | 删 100 条 `ontology_property` + 2 条 `kpi_catalog` 后 `--rollback-from` 良好锚点 | ✅ 精确恢复为 3717 / 12 |
| T9 | 真实目标库 | 对本地 `qa_metadata` 执行（含停/启 `qa-backend`） | ✅ 灌入成功 + 三项校验全绿 + 后端重启后 `/openapi.json` HTTP 200 |
| T10 | ORM 层可见性 | 后端容器内用应用自己的 session factory 只读查询 | ✅ `KpiCatalog=12 OntologyMetric=12 OntologyClass=54 OntologyProperty=3717`；KPI→指标 已绑定 12 / 断链 0 |

覆盖率说明：本次无应用代码变更，不适用 80% 代码覆盖率门槛；验证强度以 T1–T10 的端到端行为断言替代。

## 7. 安全审查

**已触发 security-reviewer 视角自查**（涉及：文件操作、数据库写入、外部系统边界、口令处理）。

| 级别 | 项 | 结论 |
|---|---|---|
| CRITICAL | 生产库被写入 | **无**。生产侧脚本只跑 `pg_dump` 与 `SELECT count(*)`；恢复脚本只经 `docker exec` 操作本地容器 |
| CRITICAL | 口令泄漏 | **无**。DB 用户从容器 `printenv POSTGRES_USER` 取，口令不进命令行、不进日志、不进 manifest；产物只含业务数据，不含 `users` / 会话 / token 表 |
| HIGH | 守卫可被绕过 | **已缓解**：库名正则白名单 + 只操作本地容器 + 只经 `docker exec`，三重限制；T4–T6 负例实测拒绝 |
| HIGH | 误删本地数据 | **已缓解**：恢复前自动打回滚锚点（`backups/pg/`，gitignore），全流程单事务，失败自动回滚；T7/T8 实测 |
| MEDIUM | 跨边界 id 错指（`datasource_id`） | **已缓解**：手册 §2.1 前置必检 + §5.3 重映射预案 + 恢复后 `orphan_kpi_datasource` 审计 |
| MEDIUM | 恢复期间 FK 触发器全关 | **已知取舍**：`session_replication_role = replica` 会同时关掉该会话内其它用户触发器；
  缓解：恢复窗口内不写业务、恢复后必跑 9 项孤儿审计 + 序列校验 + T10 ORM 层验证 |
| LOW | 产物进入 git | **已缓解**：`backups/` 已在 `.gitignore:65`；`/tmp/qa_subset_rehearsal` 演练产物已清理 |

## 8. 部署验证

### 8.1 容器状态

```bash
$ docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' | grep -E 'qa-(backend|postgres|frontend)'
qa-backend	qa-system-backend	0.0.0.0:8000->8000/tcp
qa-frontend	qa-system-frontend	0.0.0.0:5173->80/tcp
qa-postgres	postgres:16-alpine	0.0.0.0:5433->5432/tcp
```

### 8.2 端点冒烟（恢复后）

```bash
$ curl -s -o /dev/null -w "openapi HTTP %{http_code}\n" http://localhost:8000/openapi.json
openapi HTTP 200

$ curl -s http://localhost:8000/openapi.json | jq -r '.paths | keys[]' | grep -iE 'kpi|ontology'
/api/v1/admin/kpi/diagnose
/api/v1/admin/kpi/governance-summary
/api/v1/kpi-catalog
/api/v1/kpi-catalog/{id}
/api/v1/kpi-catalog/{id}/history
/api/v1/kpi-catalog/{id}/datasource-suggestions
/api/v1/kpi-catalog/search
/api/v1/ontology/classes
```

> 带鉴权的接口需真实登录态：本地 admin 账号口令已被改过（`must_change_password=False`），
> 种子默认口令（`backend/scripts/seed_rbac.py` 的 `DEFAULT_ADMIN_PASSWORD`）**不适用**，
> 本次**未改动**该账号口令。接口层验证改用 T10 的 ORM 只读断言兜底。

### 8.3 真实数据验证输出（恢复脚本自带校验，对本地 `qa_metadata` 实跑）

```text
[restore 20:29:07] 目标库=qa_metadata（白名单校验通过）容器=qa-postgres 用户=qa_user 表数=13
[restore 20:29:07] 回滚锚点：.../backups/pg/local_subset_pre_restore_20261114_202907.dump（160K）
[restore 20:29:11] 已停止 qa-backend
[restore 20:29:11] 开始灌入（单事务，失败自动回滚）…
[restore 20:29:11] 灌入成功
[restore 20:29:11] 行数对账：✅ 全部一致
[restore 20:29:11] === 校验 2：孤儿审计 ===
orphan_kpi_metric|0        orphan_kpi_datasource|0   orphan_metric_class|0
orphan_property_class|0    orphan_class_parent|0    orphan_coverage_cell|0
orphan_bo_header_class|0   orphan_entity_mapping_bo|0
orphan_feature_definition_bo|0
[restore 20:29:11] === 校验 3：序列 vs max(id) ===
class_domain_mapping  last_value=-      max_id=-      ✅
coverage_cell         last_value=1008   max_id=496    ✅
kpi_catalog           last_value=19     max_id=12     ✅
kpi_catalog_history   last_value=-      max_id=-      ✅
ontology_class        last_value=54     max_id=54     ✅
ontology_join         last_value=87     max_id=87     ✅
ontology_metric       last_value=13     max_id=13     ✅
ontology_metric_source last_value=18    max_id=18     ✅
ontology_property     last_value=3717   max_id=3717   ✅
ontology_relation     last_value=10     max_id=10     ✅
[restore 20:29:11] 已拉起 qa-backend
```

### 8.4 ORM 层真实数据验证（后端容器内，只读）

```text
ORM 读到：KpiCatalog=12 OntologyMetric=12 OntologyClass=54 OntologyProperty=3717
KPI→指标关系：已绑定 12 个，未绑定 0 个，断链 0 个
  - kpi_id=1 code=KPI_SUPPLIER_OTD        metric_id=2  metric_name=KPI_SUPPLIER_OTD 供应商准时交付率 datasource_id=1
  - kpi_id=2 code=KPI_SUPPLIER_OVERDUE_RATIO metric_id=3 metric_name=KPI_SUPPLIER_OVERDUE_RATIO 供应商逾期率 datasource_id=1
  - kpi_id=3 code=KPI_PURCHASE_CYCLE_TIME  metric_id=6  metric_name=KPI_PURCHASE_CYCLE_TIME 采购周期 datasource_id=1
```

### 8.5 遗留：生产侧导出（需人工执行）

生产 PG 从开发机不可达（`192.168.205.82:5432/5433` CLOSED），**导出必须在生产宿主机上由人工执行**，
命令与预期输出见手册 §2；执行后按 §3 比对 sha256，再按 §4 在开发机恢复。

## 9. 关联

- **手册**：`Harness/changes/2026-11-14-prod-ontology-kpi-restore/runbook.md`
- **脚本**：`scripts/pg_subset_tables.txt`、`scripts/prod_pg_dump_subset.sh`、`scripts/restore_pg_subset_local.sh`
- **关联变更（predecessor）**：`Harness/changes/2026-11-14-prod-release-1114/summary.md`（生产 1114 发布 → 提供生产 PG 环境事实与 alembic `0143` 基线）
- **关联变更（同批）**：`Harness/changes/2026-11-11-term-dictionary-add-entry/summary.md`（治理写入口收口，本次恢复的 KPI/指标属同类治理对象）
- **Rules**：`Harness/rules/开发流程规范.md`（真实数据验证门禁 —— §8.3/§8.4 即其证据形态）
- **Rules**：`Harness/rules/变更记录强制规范.md`（本文件自身的 9 段格式约束）
- **Wiki**：`Harness/wiki/`（数据模型 / NL2SQL 引擎 —— 本次恢复的是本体指标与 KPI 目录的种子数据）
- **MEMORY**：`~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-prod-ontology-kpi-restore.md`
- **既有本地备份工具（本次未复用）**：`scripts/backup_pg.sh` + `scripts/install_pg_backup_cron.sh`（`-Fc` 自定义格式、14 天保留、cron 定时；与本次子集导出刻意分离）

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A / B / C + 取舍表 + 最终决定 + 已否决替代）
- [x] 第 3 段迁移文件名 ≤ 32 字符 —— **本次无迁移**（data-only，不改 schema）
- [x] 第 7 段安全审查含 CRITICAL/HIGH/MEDIUM/LOW 分级结论
- [x] 第 8 段容器状态 + 端点冒烟命令与输出 + 真实数据验证输出均已贴出
- [x] 第 9 段 ≥ 3 个跨文件链接（关联变更 2 + Rules 2 + Wiki 1 + MEMORY 1 + 脚本 3）
- [x] 手册 `runbook.md` 同步产出
- [x] MEMORY 条目已建并入索引
- [x] 涉及真实 DB 操作 —— 用影子库 `qa_metadata_test_restore` 演练，**未触碰生产**；演练后影子库已 drop
