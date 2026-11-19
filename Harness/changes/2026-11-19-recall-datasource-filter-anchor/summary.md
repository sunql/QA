# 召回数据源过滤 + 关键词锚定 —— 实施结果汇总

- **日期**：2026-11-19
- **批次**：`2026-11-19-recall-datasource-filter-anchor`（8 任务 TDD 分步）
- **分支**：`fix/recall-datasource-anchor`（基线 `origin/main` = `c959b77f`）
- **设计**：[design.md](./design.md)（同目录）
- **状态**：**代码已实施并回归通过（含 2026-11-19 终审修复 Task 9：4 Important + 2 必须合并前修）；生产生效仍需一次后端镜像发版**（本分支未部署）

## 一、问题与解法（回顾）

2026-11-19 生产（main `c959b77f` 部署后）暴露两个缺陷：

| 缺陷 | 现象 | 根因 | 本批解法 |
|---|---|---|---|
| ②跨源幽灵表 | 「公司目前有多少供应商」→ planner 被引到 `main_data_supplier`（id 270，物理表在另一数据源）→ `ORA-00942`，值域采样同表空转 6 次 | `ontology_class` 无「类→数据源」绑定列，召回链全局取类、SQL 却在会话选定源执行 | **第 1 级·数据源过滤**：`schema_cache` 运行时校验 + 三态 fail-open |
| ①问法敏感漏选 | 墓碑 id 270 后同一问句变「礼貌拒绝」：planner 选对了 DIM_SUPPLIER（id 12），validator 报「不在本体 schema 中」 | 向量召回对措辞敏感，改一个词就把正确类挤出 topK | **第 2 级·关键词锚定**：2–6 字 n-gram 包含匹配，锚定类强制纳入且排在向量命中之前 |

**执行顺序（不可颠倒）**：数据源过滤（淘汰跨源）→ 锚定（在幸存类里取锚）→ 向量命中交集 ⇒ **锚定永不捞回幽灵类**。

## 二、提交清单（8 任务）

| 任务 | 提交 | 内容 |
|---|---|---|
| Task 1 | `d4c91a99` | `ClassRecallInfo` 增加数据源过滤/锚定披露字段（默认值 ⇒ 向后兼容） |
| Task 2 | `dd14fc28` | 召回候选集数据源过滤模块 `recall_datasource_filter.py`（`schema_cache` 运行时校验，三态 fail-open） |
| Task 3 | `b7ec5871` | 问句关键词/别名锚定模块 `recall_keyword_anchor.py`（n-gram 包含匹配，纯增量兜向量漏选） |
| Task 4 | `19975ae2` | `classifyAndRecall` 接入跨源候选过滤并合并三态披露（第一调用点） |
| Task 5 | `75be16e7` | 单步 QUERY 链候选集接入跨源过滤（第二调用点） |
| Task 6 | `d2117a9f` | 类召回接入关键词锚定（锚定类前置保截断，`anchoredCount` 披露）；**补第三调用点** `chat_multistep._recallForStep` 跨源过滤 |
| Task 7 | `914b67be` | 前端类召回披露（TS 类型 + info 级 Alert + i18n zh/en + 渲染测试） |
| Task 8 | `a46b5c17` | 定向回归 + 文档回填（wiki 类召回窗口节 + 本 summary + design.md 落本分支） |
| Task 9 | `28eb1657` … `5878315f` | **终审修复**（4 Important + 2「必须合并前修」一次修完，见下节） |

代码改动规模：`16 files changed, 994 insertions(+), 6 deletions(-)`（后端 6 源文件 + 5 测试文件，前端 5 文件）。

## 三、测试证据（Task 8 定向回归）

### 后端（真实 PostgreSQL 测试库 5434 / `qa_metadata_test` + neo4j 7688；**禁裸 pytest**）

| 测试集 | HEAD | origin/main 基线 | 失败集合对差 |
|---|---|---|---|
| `integration/test_classify_and_recall.py` + `test_chat_pipeline_layer.py` + `test_multistep_global_filter.py` | **4 failed / 18 passed** | **4 failed / 12 passed** | 失败集合**逐条一致** ⇒ 净新增 **0**（+6 passed = 本批新增用例） |
| `unit/test_recall_datasource_filter.py` + `test_recall_keyword_anchor.py` + `test_class_recall_info_contract.py` | **10 passed** | 文件在 origin/main **不存在**（本批新增） | 净新增 0 失败，全绿 |
| `unit/test_chat_service.py`（连带红基线取证） | **5 failed / 166 passed** | **5 failed / 156 passed** | 失败集合**逐条一致** ⇒ 净新增 **0**（+10 passed = `test_recall_anchor_wiring.py` 新增用例） |

**基线取证方法（可复现）**：不用 `git stash`（共享 stash 栈，其他会话并发）。改为在 `/tmp/recall-baseline`
建 **origin/main 的 detached 临时 worktree**，把本 worktree 的 `backend/.venv` 软链过去（复用同一虚拟环境，
不重装依赖），用 `PYTHONPATH=/tmp/recall-baseline/backend` 保证 `import app` 解析到基线代码
（site-packages 里的 editable `.pth` 指向当前 worktree，路径优先级已实测校正），
再以**同一组环境变量**（`TEST_DATABASE_URL`/`TEST_NEO4J_URI`/`MILVUS_TEST_DB_NAME` 等）跑同一命令，逐条比对失败集合。
临时 worktree 用完已清理。

**已知红基线（origin/main 既有，非本批引入，4+5 条一致）**：

- `integration/test_classify_and_recall.py::test_reclassify_with_prior_state`（main「匿名写收紧」导致）
- `integration/test_multistep_global_filter.py`：`test_stream_follow_up_multistep_no_type_error`、
  `test_c_fallback_multistep_inherits_global_constraints`、
  `test_stream_c_fallback_multistep_inherits_global_constraints`
- `unit/test_chat_service.py::test_class_filter_logs_successful_pruning`（logger 名不匹配类）
  及 `TestBuildAnswerPrompt` 的 4 条（`test_empty_data_injects_hint`、`test_summary_includes_total_and_columns`、
  `test_summary_includes_numeric_stats`、`test_summary_27_rows_not_truncated_v2`）

**结论：失败集合对 origin/main 净新增 0。**

### 前端

`cd frontend && npx vitest run src/tests/MessageItem.test.tsx` ⇒ **58 passed**（与 Task 7 GREEN 一致）。
附带（Task 7 记录）：`npx vitest run src/i18n/i18n.test.ts` = 25 passed；`npx tsc --noEmit` 无输出。
⚠️ worktree 内 `frontend/node_modules` 是指向主检出的**软链**（Task 7 建，`.gitignore` 覆盖未入库），
**收尾需删**；本 worktree 内**未执行** `npm install` / `npm ci`（避免穿透写主检出）。

## 三之二、终审修复（Task 9，2026-11-19）

全分支终审（8 commits，opus）判 **Approve-with-conditions**，查出 4 个 Important + 2 项
「必须合并前修」。Task 9 一次修完（每个问题先写测试跑 RED → 实现跑 GREEN）。

| 编号 | 问题 | 修法 | 提交 |
|---|---|---|---|
| **I1** | **维度词触发的 DIM 全量拉取绕过过滤**（事故同路径）：`_DIMENSION_HINTS` 含「名称」等常用词 ⇒ `dimension_hint=True` ⇒ `_fetchAllDimClasses` 直查 `source_table LIKE 'DIM_%'`（不过数据源、不受已过滤 `allClasses` 约束）⇒ 跨源 DIM 类进 schema ⇒ ORA-00942 同形 | `chat_recall._rankByLayer` 新增 `allowedClasses` 入参（= 已过滤 `allClasses`），`dim_classes` 与其取 **id 交集**后再并入候选（保留「DIM 全拉」语义，只限定在本源）；快照分支天然同口径 | `28eb1657` |
| **I2** | **研究入口未纳入**（终审判定属本批必须闭环）：`research_agent_execution.generateStepSql` 全局 `listClasses` 未过滤；BO 臂 Milvus 全局检索无 valid_to 过滤、PG 回填缺 `valid_to IS NULL`（墓碑挡不住）；兜底 `matched or list(classes)` 全量类进 prompt | (a) 执行面复用 `applyDatasourceRecallFilter`（同三态 fail-open，**不另写口径**），过滤点在类候选集源头 ⇒ 兜底也只吐已过滤的类；数据源 id 在**相位边界**固化（`ExecutionDeps.datasourceId`，避免循环内 MissingGreenlet）；(b) BO 臂 PG 回填补 `valid_to IS NULL` ⇒ **只捞活类**（墓碑行与悬空向量一律不进 BO 臂） | `d1a4762b` |
| **I3** | **no_cache 服务端零日志**：空/缺分支静默返 None ⇒ 披露了 no_cache 但过滤 fail-open 空转**无声** | `loadDatasourceTableSet` 空/缺分支补 `logger.warning`（带 datasourceId，**不带**口令/DSN）；正常路径不打日志 | `1232841f` |
| **M4** | design.md「同分按向量 score」与实现不符（实现是 **id 升序**兜底）；「调用点（两处共用）」实为三处；研究入口未记 | **只改文档不改代码**：订正排序表述、补第三调用点（`chat_multistep._recallForStep`）、补研究入口与 DIM 交集两节 | `ab8acb1f` |
| **M5** | 多 owner「不误杀」的核心口径零覆盖（生产是 Oracle 多 owner） | 补 1 条单测（`_tableKeys` 裸名 ∪ owner 限定名）+ 1 条集成用例（同 datasource 两条 schema_cache 行、不同 owner ⇒ 两个 owner 的类都不被误杀） | `5878315f` |

**明确不在本次范围（终审已判定可挂账，未动）**：`chat_domain.py:698`（MAP 治理写入，不构造 SQL）；
行数拆分（chat_recall 901 / chat_multistep 1057 / chat_service 2445）；锚定类 `_recall_score=0.0`；
mode 判定基准；锚定同分 id 兜底（代码侧）；前端三态拆 2 个 i18n 键；步骤级披露丢弃；
`datasourceFiltered` 计数与前端「张表」文案的轻微口径差。

## 四、生产部署与裁决项

### 4.1 生产仍需后端镜像发版

本批**仅代码 + 文档**，未部署。生效路径：

1. PR 合 `main`（生产缺陷，走 main 而非采购线；采购分支阶段发版时需 `git merge main` 带回）；
2. 一次**后端镜像**构建与部署（runbook 另出，与既有发版批次同流程）；
3. **无新迁移**（`schema_cache` 现成可用，未改表结构）⇒ 无 alembic 升级/回滚负担。

### 4.2 上线前置条件（I4：owner 级部分缓存会误杀本源类 —— **代码不可解，须探针**）

**问题**：`schema_cache` 按 `(datasource_id, schema_name)` 唯一，Oracle 一源多 owner ⇒ 每个 owner
需**逐 owner** introspect；而体检制本体导入**不校验** `schema_cache` ⇒「类已登记、owner 未 introspect」
可达 ⇒ 该 owner 的**全部**类被硬过滤**静默剔除**（不是报错，是"看不见"）。
代码侧无法判定「某表属于本源但未被 introspect」，故只能**上线前探针核实**。

**前置条件 A —— 生产 `schema_cache` 逐 owner 覆盖度探针**（对每个**将被使用**的数据源各跑一次）：

```sql
-- A1：本源有哪些 owner 进了 schema_cache、每个 owner 缓存了多少张表
SELECT sc.schema_name AS owner,
       count(*)       AS cache_rows,
       jsonb_array_length(sc.schema_data) AS table_cnt
FROM schema_cache sc
WHERE sc.datasource_id = :datasource_id
GROUP BY sc.schema_name
ORDER BY sc.schema_name;

-- A2：本源登记的活类里，有多少在本源缓存清单中**找不到**（这些会被静默剔除）
SELECT upper(oc.source_table) AS source_table,
       count(*)               AS class_cnt
FROM ontology_class oc
WHERE oc.valid_to IS NULL
  AND coalesce(oc.source_table, '') <> ''
  AND upper(oc.source_table) NOT IN (
      SELECT upper(t ->> 'table_name')
      FROM schema_cache sc,
           jsonb_array_elements(sc.schema_data) AS t
      WHERE sc.datasource_id = :datasource_id
  )
  AND upper(oc.source_table) NOT IN (
      SELECT upper(t ->> 'table_name' || '.' || coalesce(t ->> 'owner', ''))
      FROM schema_cache sc,
           jsonb_array_elements(sc.schema_data) AS t
      WHERE sc.datasource_id = :datasource_id
  )
GROUP BY upper(oc.source_table)
ORDER BY class_cnt DESC;
```

- A1 为空 ⇒ 该数据源**一行缓存都没有** ⇒ 过滤整体 fail-open（披露 `no_cache`，服务端有 warning），
  跨源幽灵表仍会进 ⇒ **该数据源不得上线**，须先补 introspect。
- A2 非空 ⇒ 这些表名是「本体登记了但本源没 introspect」⇒ 会被**硬过滤静默剔除** ⇒
  须先补该 owner 的 introspect（或人工确认这些类确实不该出现），再上线。
- A2 结果里**不含**跨源表名 ⇒ 覆盖度正常。

**前置条件 B —— 跨源 `DIM_*` 存在性探针**（验证 I1 修法在生产的实际覆盖面）：

```sql
-- B1：除本源外，其它数据源的 DIM_* 类有多少（维度词触发时会走 DIM 全量拉取路径）
SELECT count(*) AS cross_source_dim_classes
FROM ontology_class oc
WHERE oc.valid_to IS NULL
  AND upper(coalesce(oc.source_table, '')) LIKE 'DIM\_%'
  AND upper(coalesce(oc.source_table, '')) NOT IN (
      SELECT upper(t ->> 'table_name')
      FROM schema_cache sc,
           jsonb_array_elements(sc.schema_data) AS t
      WHERE sc.datasource_id = :datasource_id
  );
```

- B1 > 0 ⇒ 维度词（「名称」「属性」「分类」…）在本源会话曾能把这些**跨源 DIM 类**拉进 schema
  （I1 修复前的事故形态）⇒ 上线后**必须**用含维度词的问句回归一次，确认跨源 DIM 类不再进候选集。
- 探针只读、不改数据；结论写入发版记录后再执行 §4.3 的墓碑复活。

### 4.3 墓碑 id 270 复活裁决项（**待上线验证后执行**）

- **现状**：id 270（`main_data_supplier`）已由生产执行 `valid_to` 墓碑止血（2026-11-19，已生效——
  校验器相似类名列表已不含它）。
- **裁决**：**数据源过滤上线并验证后**，可**原地** `update ontology_class set valid_to = null where id = 270`，
  恢复该表在**正确数据源**会话下的可查性。
- ⚠️ **复活必须原地**：换行（新建一行）= 换 `class_id` = **Milvus 向量悬空**（旧向量指向已不存在的 id），
  等于白做。原地复活只改 `valid_to`，`id`/`class_id` 不变，向量不悬空。
- ⚠️ **前置条件**：过滤上线后 `main_data_supplier` 在 **SRM 数据源会话**下能被正常召回且不引发 `ORA-00942`；
  未验证前保持墓碑（宁可漏召，不可幽灵表）。

### 4.4 id 271 待探针核实（**未决，禁止臆断**）

同批疑似同类幽灵类 **id 271** 的 `source_table` / 所属数据源 / `valid_to` 现状**尚未探针核实**
（本地测试库 `qa_metadata_test` 无该 id 的种子数据，无法据本地推断）。**处置：先探针核实再决策**——
核实其 `source_table` 是否落在当前会话数据源的 `schema_cache` 清单内：

- 属跨源 ⇒ 与 id 270 同策（保持墓碑；过滤上线验证后再原地复活）；
- 属本源 ⇒ 直接原地复活（`valid_to = null`），并确认其别名/类名不会被锚定误捞。

**未核实前不动 271**（改墓碑状态属生产数据写操作，须有探针证据）。

## 五、遗留挂账（本批已立项，final review triage）

以下均为各任务评审产出，**行为无静默缺陷**（不阻断本批合入），但需后续批次处理：

| # | 来源 | 等级 | 内容 | 建议处置 |
|---|---|---|---|---|
| M1 | Task 6 | MEDIUM | 文件超行数红线：`chat_recall.py` **901 行**（改前 831，已超 800）、`chat_multistep.py` **1057 行**（改前 1046）、`chat_service.py` **2445 行** | 后续批次切出 `recall_anchor_wiring.py`（召回配置读取 + 锚定接线） |
| M2 | Task 6 | MEDIUM | 极端配置：`ANCHOR_MAX_CLASSES ≥ MAX_CLASSES` 时锚定可占满窗口、挤掉向量命中 | 补配置约束说明（`ANCHOR_MAX_CLASSES` 应 < `MAX_CLASSES`），或代码侧再夹取 |
| M3 | Task 6 | MEDIUM | 步骤级 `datasourceFiltered` 披露被丢弃：`_recallForStep` 把召回信息整体丢弃（既有模式，非本批引入） | 后续批次决定是否透传步骤级披露 |
| M4 | Task 6 | MEDIUM | 锚定同分兜底用 **id 升序**，与 design.md「同分按向量 score」不符（Task 3 已合实现差异，design.md 未同步） | ✅ **Task 9 已修**（design.md 订正为 id 升序，代码不动） |
| LOW-2 | Task 6 | LOW | 锚定类 `_recall_score = 0.0`（不在 `recallScoreIndex`）影响 wiki 注入打分 | 另批定权重（需 wiki 侧口径） |
| LOW-3 | Task 6 | LOW | `mode` 判定基准随 `relevant` 变大（`mode="expanded" if len(expanded) > len(relevant)`），极端可从 expanded 落回 recall | 另批裁决判定基准 |
| — | Task 7 | LOW | 三态 `skipped`（`no_datasource` / `no_cache`）在前端合并成一条文案，两态**运维含义不可区分** | 新增 2 个 i18n 键并按值分支 |
| — | Task 7 | LOW | worktree 内 `frontend/node_modules` 软链（指向主检出） | **收尾删除**（不影响入库，`.gitignore` 覆盖） |
| — | Task 4/5 | LOW | 多 owner 并集 / 空 `table_name` 两态**无集成夹具**（纯函数层已覆盖） | ✅ **Task 9 已修**（1 单测 `_tableKeys` + 1 集成用例「同源两 owner 不误杀」） |

## 六、偏离与已知边界（供接手人）

1. **简报的测试入口不存在**：简报用 `./scripts/run_local_tests.sh`，但该脚本只在采购分支
   （`feat/procurement-m0`）有、main 没有 ⇒ 本批改用等价 wrapper
   `.superpowers/sdd/2026-11-19-recall-datasource-filter-anchor/run-tests.sh`（接 5434 测试库 /
   7688 neo4j / `MILVUS_TEST_DB_NAME=qa_test`，pytest 在 worktree backend 内跑）。**禁裸 pytest**。
2. **design.md 原不在本分支**：它只存在于 `feat/procurement-m0` 的 commit `06d4fbcf` ⇒ 本批已拷入本分支
   （`git show 06d4fbcf:.../design.md`），使 PR 自洽（Task 1 的模块 docstring 引用它）。
3. **前端 i18n 占位符**：仓内 i18n 显式用单花括号 `{total}`（非 brief 的 `{{total}}`）⇒ 照仓内既有写法。
4. **`_fallbackRecall` 不接锚定**（降级是纯增量缺失场景，另批再议）。
5. **`ontology_class` 不加 `datasource_id` 列**（回填难题与运行时校验同构 ⇒ 本批用运行时校验绕开）。
