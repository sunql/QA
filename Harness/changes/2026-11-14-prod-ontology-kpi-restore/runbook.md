# 操作手册：生产 PG「本体 + 指标 + KPI 目录」子集备份 → 本地测试环境恢复

- **日期**：2026-11-14
- **目标**：把生产库 `qa_metadata` 中「本体管理的指标」与「KPI 目录」相关表的数据，
  备份出来并恢复到**本地测试环境的 Docker PG**（容器 `qa-postgres` / 库 `qa_metadata`）。
- **执行人**：生产侧命令由你在生产宿主机 `192.168.205.82` 上执行；恢复侧命令在开发机执行。
- **脚本**：
  - 生产侧导出：`scripts/prod_pg_dump_subset.sh`（**全程只读**）
  - 本地恢复：`scripts/restore_pg_subset_local.sh`
  - 表清单 SSOT：`scripts/pg_subset_tables.txt`

---

## 0. 先读这一段（网络与风险约束）

| 约束 | 说明 |
|---|---|
| **生产 PG 从开发机不可达** | `192.168.205.82:5432/5433` 从开发机 `nc` 均为 CLOSED。**导出必须在生产宿主机上做**，再用 scp / U 盘把产物搬过来。 |
| **生产无 SSH 免密通道** | 生产侧每条命令都由你在宿主机上手动执行；本文档给出可直接粘贴的命令块。 |
| **生产侧零写入** | 导出脚本只跑 `pg_dump` / `SELECT count(*)`，不写任何库、不改任何数据。 |
| **本地侧是"替换"不是"追加"** | 恢复会**先删掉本地这 13 张表的全部行**，再灌入生产行。本地已有的本体/指标/KPI 数据（含你本地调试造的数据）会被覆盖。 |
| **本地侧有回滚锚点** | 脚本在动手前自动 `pg_dump -Fc` 一份本地子集到 `backups/pg/local_subset_pre_restore_<时间戳>.dump`，回滚用它（见 §6）。 |
| **本地 schema 不变** | 导出用 `--data-only`，**不带 DDL**；本地 alembic 版本不受影响（本地 `0143`，生产 `0143`）。 |

---

## 1. 数据范围（恢复哪些 / 不恢复哪些）

清单见 `scripts/pg_subset_tables.txt`（13 张表，导出与恢复共用同一份，避免两端漂移）：

| 分类 | 表 |
|---|---|
| 本体图谱 | `ontology_class`、`ontology_property`、`ontology_relation`、`ontology_join` |
| 本体指标 | `ontology_metric`、`ontology_metric_source` |
| 域映射 | `class_domain_mapping` |
| 业务对象 | `business_object`、`business_object_domain` |
| **KPI 目录** | `kpi_catalog`、`kpi_catalog_history` |
| 配套 | `coverage_cell`、`id_mapping` |

**故意不恢复的表（重要）：**

| 表 | 为什么不恢复 | 恢复前必须核对什么 |
|---|---|---|
| `data_source` | 本地 5 条数据源是**本地环境资产**（THBI Oracle / localmysql / smoke / recall-probe / phase13_realdata_pg），恢复会打断本地业务库连接 | `kpi_catalog.datasource_id → data_source(id)` 是**按 id 引用**。必须比对生产的 `data_source(id,name)` 与本地是否**语义对齐**（见 §2 步骤 2）。不对齐则按 §5.3 做 id 重映射。 |
| `data_domain` | 本地 4 条域（LOGISTICS / PROCUREMENT / PRODUCT / QUALITY）是本地环境资产 | `business_object.primary_domain_id` 与 `business_object_domain.domain_id` 是**按 code 引用**，只要生产的 code 在本地存在即可。 |
| `entity_mapping`（本地 35 万行）、`feature_definition`、`document_entity_relation` | 本地业务数据，不属于"指标 / KPI 目录"范围 | 这三张表 `→ business_object(code) ON DELETE RESTRICT`。**恢复期间必须关闭 FK 触发器**（脚本已做），恢复后跑孤儿审计（脚本已做）。 |

---

## 2. 阶段 A：生产侧导出（在 `192.168.205.82` 上执行）

### 2.1 前置检查（3 条）

```bash
# A1. 确认 PG 容器在跑、库名/用户从容器 env 取（不硬编码口令）
docker ps --format '{{.Names}}' | grep -qx qa-postgres && echo "PG 容器 OK"
docker exec qa-postgres printenv POSTGRES_DB POSTGRES_USER

# A2. 确认生产 alembic 版本（应为 0143）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# A3. 记录生产的数据源 id→名称（与本地比对用，见 §1 的 data_source 行）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT id, name, type FROM data_source ORDER BY id;"'

# A4. 记录生产的业务域 code（本地必须存在这些 code）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT code FROM data_domain ORDER BY code;"'
```

> **A3 若与本地 `data_source(id,name)` 不一致**（同样的数据源在不同环境拿到不同 id），
> **先别导**，走 §5.3 的 id 重映射预案。

### 2.2 执行导出

把两个文件拷到生产机（`scripts/pg_subset_tables.txt` + `scripts/prod_pg_dump_subset.sh`），然后：

```bash
# 生产宿主机上
cd /root/qa-scripts     # 放脚本的目录
chmod +x prod_pg_dump_subset.sh
./prod_pg_dump_subset.sh --out-dir /root/backup --label prod_20261114
```

**若生产机没法放脚本**，用等价的原地命令（表清单与 SSOT 一致）：

```bash
mkdir -p /root/backup
TS=$(date +%Y%m%d_%H%M)
OUT=/root/backup/prod_subset_${TS}
TABLES="ontology_metric_source kpi_catalog_history ontology_join ontology_relation
        ontology_property class_domain_mapping coverage_cell business_object_domain
        kpi_catalog ontology_metric business_object ontology_class id_mapping"

# 行数快照（恢复后对账）
: > ${OUT}.manifest.txt
for t in $TABLES; do
  c=$(docker exec qa-postgres sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc 'SELECT count(*) FROM $t;'" | tr -d '[:space:]')
  printf '%s\t%s\n' "$t" "$c" >> ${OUT}.manifest.txt
done

# 导出（文本格式，跨版本可直接 psql 灌入）
ARGS=""
for t in $TABLES; do ARGS="$ARGS -t public.$t"; done
docker exec qa-postgres sh -c "pg_dump -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" --data-only --no-owner --no-privileges $ARGS" > ${OUT}.sql
gzip -9 -f ${OUT}.sql
sha256sum ${OUT}.sql.gz | awk '{print "sha256: "$1}' >> ${OUT}.manifest.txt
ls -la ${OUT}.sql.gz ${OUT}.manifest.txt
```

### 2.3 预期输出

- `<OUT>.sql.gz`：子集数据（本地 13 张表约 150K；生产预计相近量级）
- `<OUT>.manifest.txt`：每张表的行数 + `sha256:` + `size:` + `source_db:` 等元信息
- `pg_dump` 会对 `ontology_class` 报一条 **circular foreign-key warning**（自引用父类）
  —— **这是预期的，不是错误**。恢复端用 `session_replication_role = replica` 关掉 FK 触发器，
  自引用不影响还原。

---

## 3. 阶段 B：传输与完整性校验

把两个产物搬到开发机（scp 或 U 盘），**先比 sha256 再恢复**：

```bash
# 生产侧
sha256sum /root/backup/prod_subset_*.sql.gz
# 开发机侧（macOS）
shasum -a 256 /path/to/prod_subset_*.sql.gz
```

两边 sha256 **必须完全一致**，否则重新拷贝。sha256 一致才继续 §4。

---

## 4. 阶段 C：本地恢复（在开发机执行）

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system

# 4.1 先干跑，确认守卫与目标库
bash scripts/restore_pg_subset_local.sh \
  --dump backups/pg/prod_subset_20261114_1015.sql.gz \
  --target-db qa_metadata --dry-run

# 4.2 真恢复（脚本会：打回滚锚点 → 停 qa-backend → 单事务灌入 → 校验 → 拉起 qa-backend）
bash scripts/restore_pg_subset_local.sh \
  --dump backups/pg/prod_subset_20261114_1015.sql.gz \
  --target-db qa_metadata --apply
```

**脚本内置的四道守卫**（已本地实测，见 §7）：

1. 目标库名必须匹配 `^qa_metadata(_test.*)?$` —— 传 `postgres` / `qa_metadata_prod` 会被直接拒绝；
2. 只经 `docker exec` 操作**本地容器**，结构上无法写生产库；
3. 目标库必须存在且 13 张表齐全（schema 版本不对会拒跑）；
4. 灌入全程单事务 + `ON_ERROR_STOP=1`，任何一条 COPY 失败 ⇒ 整体回滚，**本地数据保持原样**。

**想先在隔离库里验证**（不碰本地主库）：

```bash
# 建一个影子库并灌 schema（不改主库）
docker exec qa-postgres psql -U qa_user -d postgres -c "CREATE DATABASE qa_metadata_test_restore;"
docker exec qa-postgres pg_dump -U qa_user -d qa_metadata --schema-only --no-owner --no-privileges \
  | docker exec -i qa-postgres psql -v ON_ERROR_STOP=1 -q -U qa_user -d qa_metadata_test_restore
bash scripts/restore_pg_subset_local.sh \
  --dump backups/pg/prod_subset_20261114_1015.sql.gz \
  --target-db qa_metadata_test_restore --keep-backend --apply
```

---

## 5. 阶段 D：验证

### 5.1 脚本自动做的三项校验（`--apply` 输出里）

| 校验 | 期望 | 不符时怎么办 |
|---|---|---|
| **逐表行数对账**（本地 vs 生产 manifest） | `✅ 全部一致` | 有 diff ⇒ 生产导出与本地灌入不完整，重新走 §4 |
| **孤儿审计**（9 项） | 全部 `0` | 非 0 ⇒ 子集内有断链，见 §5.2 |
| **序列 vs max(id)** | 全部 `✅`（序列 ≥ max(id)） | `❌ 序列落后` ⇒ 下次插入会撞主键，执行 §5.4 |

### 5.2 孤儿审计非 0 时

| 审计项 | 含义 | 处置 |
|---|---|---|
| `orphan_kpi_metric` | KPI 指向的指标不在子集内 | 生产数据本身断链，或漏导了表 —— 回到 §1 核对清单 |
| `orphan_kpi_datasource` | **KPI 指向的 `data_source` id 本地不存在** | 最常见的一种，见 §5.3 |
| `orphan_entity_mapping_bo` | 本地 `entity_mapping` 指向的 `business_object.code` 在恢复后不存在 | 本地 `entity_mapping` 是本地业务数据，生产 BO 覆盖不全导致 —— 不影响指标/KPI 功能，可暂不处理；确需对齐则把生产 `business_object` 一并纳入子集 |
| 其余 6 项 | 子集内部断链 | 清单漏表，回 §1 |

### 5.3 `datasource_id` 不对齐时（预案）

若 §2.1 步骤 A3 发现生产的 `data_source` id 与本地不同语义，恢复后需要重映射：

```bash
# 恢复后执行（把生产的旧 id 映射到本地实际 id，按实际差异改）
docker exec qa-postgres psql -U qa_user -d qa_metadata -c \
  "UPDATE kpi_catalog SET datasource_id = <本地id> WHERE datasource_id = <生产id>;"
```

映射关系请以 §2.1 的 A3 输出为准，**逐条核对 name 一一对应后再改**。

### 5.4 序列落后时

```bash
# 例：ontology_metric 序列落后
docker exec qa-postgres psql -U qa_user -d qa_metadata -c \
  "SELECT setval('ontology_metric_id_seq', (SELECT max(id) FROM ontology_metric));"
```

### 5.5 后端与 UI 冒烟

```bash
docker ps --format '{{.Names}}' | grep -E 'qa-backend|qa-frontend'
docker logs --tail 50 qa-backend
curl -s http://localhost:8000/openapi.json | jq '.paths | keys | length'
```

UI 验收点（本地前端 5173）：

- 「业务配置」→「KPI 治理」：KPI 列表条数与生产一致，指标绑定（`metric_id`）能正常展开；
- 「本体管理」：类 / 属性 / 指标树可见，指标的技术形态、聚合表达式按生产数据显示；
- 新建一条 KPI 并保存 → 验证 id 序列正常（不撞主键）。

---

## 6. 回滚

恢复脚本在动手前自动生成的锚点：

```
backups/pg/local_subset_pre_restore_<YYYYMMDD_HHMMSS>.dump
```

```bash
# 用锚点回滚到恢复前的本地状态
bash scripts/restore_pg_subset_local.sh \
  --rollback-from backups/pg/local_subset_pre_restore_20261114_101500.dump \
  --target-db qa_metadata --apply
```

> 注意：锚点是**执行前**的快照，所以「回滚」= 回到恢复前的本地数据。
> 回滚本身也会再打一个新锚点，所以回滚也可再被回滚。

---

## 7. 已实测的坑位与结论（本地演练，非推测）

演练环境：本地 `qa-postgres`（PG 16.15，库 `qa_metadata`）+ 影子库 `qa_metadata_test_restore`。

| # | 坑 | 实测结论 |
|---|---|---|
| 1 | `entity_mapping` 35 万行 `→ business_object(code) ON DELETE RESTRICT` | **真会挡住 `DELETE FROM business_object`**。已在影子库复现（`ERROR: Key (entity_type)=(SUPPLIER) is not present in table "business_object"`）。必须靠 `SET session_replication_role = replica` 关掉 FK 触发器，恢复后再跑孤儿审计。 |
| 2 | `pg_dump --data-only -t` 会不会带序列值？ | **会**。dump 尾部自带 `setval('public.kpi_catalog_id_seq', 19, true)` 等 owned 序列的 setval，不需要手工补。 |
| 3 | `ontology_class` 自引用（`parent_class_id`） | `pg_dump` 报 circular FK warning，**预期现象**，恢复端靠关触发器解决。 |
| 4 | 损坏的 dump 会不会写坏本地库？ | **不会**。人为破坏 dump 后灌入，在 `COPY ontology_relation` 处报错，事务整体回滚，三张表行数与灌入前**完全一致**。 |
| 5 | 恢复后本地数据对不对？ | 影子库全流程：逐表行数对账 `✅ 全部一致`，9 项孤儿审计全 `0`，10 个序列全部 `✅`。 |
| 6 | 回滚路径能不能真的回滚？ | 已实测：人为删掉 100 条 `ontology_property` + 2 条 `kpi_catalog`，从良好锚点回滚后行数**精确恢复**为 3717 / 12。 |
| 7 | 守卫会不会被绕过？ | 三条负例均被拒：`--target-db postgres`、`--target-db qa_metadata_prod`（伪装生产名）、目标库不存在。 |
| 8 | bash 3.2（macOS）变量紧跟全角标点会被吞 | `$VAR（` 会被解析成变量名的一部分，报 `unbound variable`。脚本内所有此类位置已改用 `${VAR}`。 |
| 9 | CentOS 7 没有 `shasum` | 导出脚本已改为 `sha256sum` 优先、`shasum -a 256` 兜底。 |

---

## 8. 安全约束（贯穿全程）

- 本流程**不接触任何口令**：DB 用户从容器 env 取，`-U "$POSTGRES_USER"` 传入，脚本日志里不出现口令。
- 生产侧**全程只读**（`pg_dump` + `SELECT count(*)`），不改生产数据。
- 恢复侧只写**本地容器内的 `qa_metadata` / `qa_metadata_test*`**，库名白名单是硬约束。
- 导出的子集**不含业务数据**（业务数据在 Oracle，不在 PG），不含用户、账号、会话、token 用量等敏感表。
- 产物落在 `backups/pg/`（已在 `.gitignore`），**不要提交到 git**。

---

## 9. 相关文件

- 表清单 SSOT：`scripts/pg_subset_tables.txt`
- 生产侧导出：`scripts/prod_pg_dump_subset.sh`
- 本地恢复 / 回滚：`scripts/restore_pg_subset_local.sh`
- 变更记录：`Harness/changes/2026-11-14-prod-ontology-kpi-restore/summary.md`
- 生产发布记录（背景）：`Harness/changes/2026-11-14-prod-release-1114/deploy-prod-1114.md`
