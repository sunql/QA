# A1 统一 ID 映射：回填脚本 + 真实数据门禁（M0-P0.2 · ③）

日期：2026-10-31　分支：`feat/id-mapping-backfill`　基线：`origin/main @ e23655e9`
关联：#86（写入口 admin，`41e77b42`）、#87（创建契约收紧，`e23655e9`）

## 背景

#86 / #87 让 `id_mapping` 只能由服务端派生 ID、指针列只能由服务端写入，但这张表
对存量本体类仍是空的 —— A2（Neo4j 统一 ID）与 A4（三库同步）没有锚点可用。
本批补齐「把存量 `ontology_class` 落到 `id_mapping`」的可重跑路径，以及上线前
在真实数据上验证语义的门禁。

设计来源：备份线 `backup/evidence-sql-metric-docs` 的
`e8fc1d42`（回填）、`31c3d9e7`（动手前体检）、`ea5b627c`（真实数据门禁）。
脚本按 main 的约定改写，未直接照搬：

| 差异 | 备份线 | main（本批） |
|---|---|---|
| `business_object` | 类名（`obj:Supplier:101`） | `CLASS`（`obj:CLASS:101`），与 ontology 既有约定一致 |
| ORM 位置 | `app/domain/id_mapping.py` | `app/domain/models.py` |
| 宽度 / 字符合法性 | 无 | 复用 #87 的 `buildUnifiedId`（NFC、类别拒绝、列宽同源） |
| CLI 安全 | 直接写库 | 默认预演；`--apply` 才写；白名单 + 生产需 `--allow-prod` |
| 门禁口径 | 行数对平 | 集合关系（覆盖 + 无悬空），见下 |
| 门禁写库 | 哨兵先 commit 再还原 | 哨兵与重跑同一未提交事务，结束 rollback |

## 改动

| 文件 | 动作 |
|---|---|
| `backend/scripts/backfill_id_mapping.py`（新增） | 回填 `obj:CLASS:{class_id}`；`ON CONFLICT (unified_id) DO UPDATE` 只更新 `_OWNED_COLUMNS`（`pg_table`/`pg_id`）；**单事务**；动手前体检坏行 + 大小写漂移；`previewBackfill` 只读；写库守卫复用 `kpi_metric_db_guard` 白名单 + `APP_ENV` |
| `backend/scripts/id_mapping_realdata.py`（新增） | 只读门禁：派生一致 + 大小写漂移 + 覆盖（存活类 ⊆ CLASS 映射）+ 无悬空；`--full` 追加「重跑存活」「幂等」，**两者都在未提交事务内，结束 rollback** |
| `backend/app/tests/integration/test_backfill_id_mapping.py`（新增） | 17 条：对账、幂等、墓碑、不越权写跨库列、预演不写库、坏行体检（点名 / 总数 / 不误报 / 非法分段）、大小写漂移、单事务无半状态、`commit=False`、数据库异常落 FAIL |
| `backend/app/tests/integration/test_id_mapping_realdata_gate.py`（新增） | 15 条：PROPERTY/METRIC/墓碑混装表通过、漏类、换集、悬空、派生不一致、非法分段、大小写漂移、哨兵还原、放宽 `set_` 必被抓、空表、`--full` 不落盘、探针失败不遮蔽、回滚失败短路、数据库异常落 FAIL |
| `backend/app/tests/unit/test_id_mapping_cli_guard.py`（新增） | 10 条：白名单（deny-by-default）、`APP_ENV` 两路、非法 DSN、开关名文案 |
| `backend/app/tests/unit/test_pg_support_guard.py`（新增） | 7 条：测试库名 fail-fast（`qa_metadata_test` 完整名或 `_` 续名） |
| `backend/app/tests/_pg_support.py` + `unit/conftest.py` + `services/conftest.py` | `assertTestDatabase` 库名白名单 fail-fast：非 `qa_metadata_test*` 直接拒绝（三处夹具都会 `TRUNCATE ... CASCADE`） |
| `Harness/wiki/data-model.md`、`api-reference.md` | `id_mapping` 表与 `/api/v1/id-mappings` 契约 |

## 关键语义

1. **覆盖是集合关系，不是计数**。`id_mapping` 不是 CLASS 专用表：`PROPERTY` /
   `METRIC` 行由 ontology API 写入，软删类也留着映射行 —— 「总行数 == 存活类数」
   在真实库上恒假。反过来计数相等也抓不到「漏了 102、多了 999」这种换集。
   所以门禁断言的是：存活类 ⊆ `CLASS` 映射 ⊆ 现有类 id。
2. **单事务 + 幂等且不越权**：所有批次一次提交，中途失败不留半状态；
   `DO UPDATE` 的 `set_` 只含 `pg_table`/`pg_id`，重跑不会抹掉 A2/A3 的
   `neo4j_*` / `milvus_*`。
3. **哨兵不落盘**：`--full` 的探针 UPDATE 与重跑回填在同一个未提交事务里，
   断言完 `rollback`。进程被杀 / 容器被 stop 都不会留下假坐标；并发的对账脚本
   读不到哨兵；探针持行锁，不会在还原时覆盖别人的并发写入。
4. **探针必须命中回填真的会写的行**：取 id 最小的**存活**类。取到墓碑或非 CLASS
   行时回填不碰它，`set_` 被放宽也全绿（测试用 `obj:AGENT:0` 排在前面的场景钉住）。
5. **体检前置**：坏行与大小写漂移都在**任何写入之前**报错。大小写漂移已知来源是
   `scripts/reconcile_milvus_id_mapping.py` 用 Milvus 的 `type`（小写 `class`）
   写占位行；放任会让同一类拥有两个统一 ID。
6. **默认只读 + deny-by-default 写库守卫**：回填默认预演；门禁默认只读。
   `--apply` / `--full` 的库必须命中 `qa_metadata` / `qa_metadata_test*`，
   且 `APP_ENV` 是生产或库名为 `qa_metadata` 时还需 `--allow-prod`。
   只读门禁**刻意不套白名单**（它不写库，要能对任何库做体检）。

## 评审与修复

首轮两个评审（安全 + 代码）共 1 HIGH、4 MEDIUM、5 LOW，全部处置：

| 编号 | 问题 | 处置 |
|---|---|---|
| 安全 H1 | 生产判定 allow-by-default、只看库名 | 复用 `kpi_metric_db_guard.assertSafeTarget`（deny-by-default 白名单）+ `APP_ENV` 第二路；开关名替换为本脚本的 `--allow-prod` |
| 安全 H2/H3/H4 + M3 | 哨兵先 commit：进程死留假坐标、还原覆盖并发写、失败时 restore 抛 `PendingRollbackError`、窗口内对读者可见 | 改为**同一未提交事务**：`backfillIdMappings(commit=False)`，`finally` 直接 rollback |
| 安全 M1 / 代码 M2 | 探针可能取到回填不碰的行（空转） | 探针钉到 id 最小的存活类；`_OWNED_COLUMNS` 提为模块常量，测试放宽它来验证门禁能抓回归 |
| 安全 M2 | 分批 commit 非原子、体检 TOCTOU | 单事务；体检与写在同一次调用内 |
| 安全 M4 | 破坏性 PG 夹具无测试库 fail-fast | `_pg_support.resolveTestDatabaseUrl` 增加 `qa_metadata_test*` 白名单 |
| 安全 M5 | `class` vs `CLASS` 大小写漂移 | `CaseVariantMappingError` 前置体检 + 修复 SQL（见上线步骤附录） |
| 代码 HIGH | 门禁计数口径在真实库上恒假且抓不到换集 | 改为集合覆盖 + 无悬空断言 |
| 代码 M3 | `_describeCorrupt` / `_checkDerivedIds` 可能自己抛 `ValidationError` | 报错器降级为「无法派生」原文；派生检查转 `VerifyFailure` |
| 安全 L1 | 门禁未处理 `ValidationError` | 在源头转换（`_checkDerivedIds`），`main` 无需额外分支 |
| 安全 L2 | 守卫在 try 外、坏 DSN 未捕获 | `assertSafeWriteTarget` 只在 `--apply`/`--full` 前调用并捕获 `SystemExit`；坏 DSN 解析成空库名 ⇒ 落白名单拒绝 |
| 安全 L3 | `--allow-prod` 是静态开关、无备份前置 | 接受：备份写进上线步骤第 1 步，且生产写库前必须把命令贴给用户过目（本仓既有纪律） |
| 代码 LOW | `updated_time` 在 DO UPDATE 路径不 bump | 接受（informational）：本脚本不承诺「最后回填时间」语义 |
| 代码 LOW | 存活类口径两处写法 | `ALIVE_CLASS_PREDICATE` + `countAliveClasses` 由门禁直接 import；回填内再加一条 SQL 计数 vs ORM 扫描的一致性断言 |
| 代码 LOW | 变更记录测试条数漂移 | 本文件已更正 |

二轮复审（同一对评审对修复后的代码再查一遍）：

| 编号 | 问题 | 处置 |
|---|---|---|
| 安全 NEW-1 / 代码 N1 | `_checkIdempotent` 用了默认 `commit=True`：`--full` 实际会提交回填，与「不落盘」契约矛盾 | 改 `commit=False` + `finally` 回滚；测试用「陈旧 `pg_table` 经另一连接读取仍为陈旧值」钉住 |
| 代码 N2 | 探针的存活谓词没有测试能咬住（墓碑排在最前时删掉谓词仍全绿） | 放宽 `set_` 的用例里补一个 id 更小、**有**映射的墓碑 |
| 代码 N3 | 覆盖断言的墓碑排除没有被真正断言（墓碑自带映射，删掉 `valid_to IS NULL` 仍通过） | 混装表用例补第二个**无映射**的墓碑 |
| 代码 N4 | 探针失败路径（旧实现留下假坐标的那条）无测试 | 注入回填异常，断言原异常透传 + 另一连接读到原跨库落点 |
| 代码 N5 | 只读门禁放过大小写漂移（canonical 行满足覆盖，小写行自洽） | `verifyReadOnly` 增加 `assertNoCaseVariantMappings`；两脚本共用同一判定与文案 |
| 安全 NEW-2 | 测试库白名单前缀匹配过宽（`qa_metadata_testing` 也放过） | `_pg_support` 与 `kpi_metric_db_guard` 同步收紧为「完整名或 `_` 续名」 |
| 安全 NEW-4 | DB 异常冒 traceback | 两个 CLI 的 `main` 增捕 `SQLAlchemyError` → 可读 FAIL / exit 1 |
| 代码 LOW | `LIMIT` 用 f-string 拼常量 | 改绑定参数 `:limit` |
| 代码 LOW | 回滚失败后会话已坏，后续报无关错误 | `_discardProbe` 在无在途异常时转 `VerifyFailure` 短路；有在途异常只告警不遮蔽 |
| 安全 NEW-3 | 大小写检测只覆盖 CLASS（`property`/`metric` 小写行不可见） | 属 A3 写入方根因，随属性/指标级映射一并在「不在本批」记录 |
| 安全 NEW-5 | `--full` 期间持全程行锁 | 上线步骤补注：类数很大时选窗口执行 |

## 测试

```
pytest app/tests/integration/test_id_mapping_realdata_gate.py \
       app/tests/integration/test_backfill_id_mapping.py \
       app/tests/unit/test_pg_support_guard.py \
       app/tests/unit/test_id_mapping_cli_guard.py \
       app/tests/unit/test_kpi_metric_realdata_lib.py
# 78 passed

# 加上 #87 的 api / format 套件与相邻 services / KPI 计划套件
pytest app/tests/integration/test_id_mapping_api.py \
       app/tests/unit/test_id_mapping_format.py \
       app/tests/unit/test_kpi_metric_backfill_plan.py \
       app/tests/services/test_import_rule_engine.py
# 127 passed ⇒ 合计 205 passed
```

回归取证：`app/tests/integration/test_evidence_metric_result.py` 有 3 红
（`header_class_id` NOT NULL，fixture 未种 header 类），在 pristine
`origin/main @ e23655e9` 上逐条同红 ⇒ 净新增失败 0。

CLI 冒烟（测试库 `qa_metadata_test`，`APP_ENV=development`）：

- 回填预演：`存活类 5，计划 upsert 5 行`；`--apply`：`已写入：存活类 5，扫描 5，受影响行 5`
- 只读门禁：`PASS（只读）: 存活类 5，CLASS 映射 5 行（覆盖 ✓ 无悬空 ✓），派生一致 5 行`
- 全量门禁：`PASS（全量（重跑存活 ✓，幂等 ✓））`；SQL 日志确认探针 UPDATE 与两次回填各自以
  `ROLLBACK` 结束，全程**没有 COMMIT**
- 拒绝路径（不建连接）：`qa_metadata` 无 `--allow-prod` ⇒ exit 2；`scratch` ⇒ exit 2

## 上线步骤（待用户批准）

1. **当日备份**（生产写库前置；本仓既有纪律，脚本不代做）。
2. 从 fresh worktree 在容器里跑**只读门禁**：`python -m scripts.id_mapping_realdata`。
   若报「大小写漂移」或「漏类 / 悬空」，先按附录处置，不要跳到第 3 步。
3. 跑**回填预演**：`python -m scripts.backfill_id_mapping`，把「将 upsert N 行」
   的结果贴给用户过目。
4. 显式批准后再 `--apply --allow-prod`；随后重跑只读门禁。
5. `--full` 会开未提交事务并回滚（全程无 COMMIT），但它持住被回填行的行锁直到结束；
   本体类数量很大时仍建议选窗口执行，避免短暂阻塞本体 API 的写入。

### 附录：大小写漂移的修复 SQL（人工确认后执行）

`reconcile_milvus_id_mapping.py` 写过的小写占位行会让回填拒绝执行。修复顺序：
先把跨库落点并到规范行（不存在就先建），再删小写行。**执行前先 `SELECT` 核对
行数与落点，并在测试库演练。**

```sql
-- 1) 规范行不存在时新建（保留 Milvus 落点）
INSERT INTO id_mapping
  (unified_id, business_object, external_id, pg_table, pg_id,
   milvus_collection, milvus_id, created_time, updated_time)
SELECT 'obj:CLASS:' || l.external_id, 'CLASS', l.external_id,
       'ontology_class', l.external_id, l.milvus_collection, l.milvus_id, now(), now()
FROM id_mapping l
WHERE l.business_object = 'class'
  AND NOT EXISTS (
    SELECT 1 FROM id_mapping t
    WHERE t.business_object = 'CLASS' AND t.external_id = l.external_id);

-- 2) 规范行已存在时把落点并过去
UPDATE id_mapping t
SET milvus_collection = l.milvus_collection,
    milvus_id = l.milvus_id
FROM id_mapping l
WHERE t.business_object = 'CLASS'
  AND l.business_object = 'class'
  AND l.external_id = t.external_id
  AND l.milvus_collection IS NOT NULL;

-- 3) 删除小写占位行
DELETE FROM id_mapping WHERE business_object = 'class';
```

## 不在本批

- A2（Neo4j 节点统一 ID）与 A3（Milvus 向量落点）回填：本批只写 PG 落点。
  上述大小写漂移的**根因**在 A3 的写入方，待 A2/A3 对齐命名空间时一并修。
  当前体检只覆盖 `CLASS`：`reconcile_milvus_id_mapping.py` 写的小写 `property` /
  `metric` 行既不会被回填拒绝，也不会被只读门禁点名。
- 属性 / 指标级映射（`PROPERTY` / `METRIC`）：先做类级，属性级随 A2 一起。
- 并发跑两个回填：单事务 + 行锁会串行化，且重跑幂等；未加 advisory lock。
- 12/12 指标技术形态 mismatch 的业务对齐（独立议题）。
