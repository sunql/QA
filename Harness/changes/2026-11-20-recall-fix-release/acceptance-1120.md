# 1120 召回根治 —— 生产上线验收记录

- **日期**：2026-11-20
- **批次**：`2026-11-20-recall-fix-release`
- **Runbook**：[deploy-prod.md](./deploy-prod.md)（部署 SSOT）· **交付汇总**：[summary.md](./summary.md)
- **验收人**：Claude（只读复核）+ 用户（生产命令执行）
- **结论**：**✅ 已上线，根治在运行时实证生效；不需要回滚**。剩余遗留只有 **1 条未根治路径（ds2 fail-open）**；
> 另有一处**已知属正确行为**的现象（3 个结构性占位类被过滤剔除，§三 —— ★ 理由链已订正：
> **不是「0 行不被 introspect 收录」**，而是「表不在被 introspect 的 owner 命名空间内 / 表不存在」，
> 详见 §3.2 与 §5.5）与一处**语义衔接待办**（采购域 M1），
> 均**不影响回滚决策**。

> 本文件是**事后验收**记录。runbook §2 的前置探针（`A1`/`A2`/`B1`/`C1`）**在上线后**才执行
> （用户按 runbook 执行了部署动作但未先跑 §2）⇒ 结论中单独标注了这一偏差，见 §五。

---

## 一、生产现状快照（1120 发版后）

| 项 | 实测值 | 判读 |
|---|---|---|
| 后端镜像 | `qa-backend:offline` ← `qa-system-backend:amd64-1120-recall` | ✔ |
| 前端镜像 | `qa-system_frontend` ← `qa-system-frontend:amd64-1120-recall` | ✔ |
| 前端 bundle | `index-t1K9T8s7.js` | ✔ **镜像内 = 运行态容器**（同一份产物，无漂移） |
| 前端文案 | `已按当前数据源过滤`=1 / `已按关键词锚定`=1 / `泰鸿智脑`=1 | ✔ 披露面与改名同在 |
| `alembic_version` | `0152` | ✔ 本批零迁移，与 main head 一致（无 0153+、无采购 0154–0158） |
| 回滚锚点 | `qa-backend:pre-1120-recall` / `qa-system_frontend:pre-1120-recall`（镜像 ID `e1c3ba24f9e7` / `f88e4a0846a1`） | ✔ load 前已建 |
| 数据备份 | `pg_dump` 106M（非空），未建 `_bak` 影子表 | ✔ |

**与 runbook §3「#157 单批均未上线过」的基线假设不符**：现场部署前的前端 bundle 是 `index-DizP7VQ_.js`
（= #157 单批那版）⇒ **至少有一个「只含部分修复」的中间态曾上过线**，风险窗口比 runbook 假设长一个批次。
本次未造成可见故障，但**下次发版前必须重新取基线 bundle**（不能凭 runbook 里的字面值判断）。

### 部署动作逐条核对（11 项，全部正确）

| # | 动作 | 判读 |
|---|---|---|
| 1 | compose 入口可解析（`docker-compose.centos7.yml` 单文件，v1 无 `!override`） | ✔ |
| 2 | `alembic_version` 前后均 `0152` | ✔ 本批零迁移 |
| 3 | 回滚锚点 tag 在 load **之前**建好 | ✔ |
| 4 | `pg_dump` 106M 非空、未建 `_bak` 影子表 | ✔ |
| 5 | 两个 tar load + retag 成功 | ✔ |
| 6 | **后端先于前端 recreate** | ✔（nginx 会缓存 upstream IP，顺序反了会连旧 IP） |
| 7 | bundle `index-DizP7VQ_.js` → `index-t1K9T8s7.js` | ✔ 1120 正确 bundle |
| 8 | 三个健康检查 200（backend / frontend / api-via-frontend） | ✔ |
| 9 | 两个新模块在镜像内 | ✔ |
| 10 | `applyDatasourceRecallFilter` 计数 = chat_recall 2 / chat_service 2 / chat_multistep 2 / research_agent_execution 3 | ✔ 与 runbook 期望逐字相符 |
| 11 | `classOntologyIds`=1、`class_scope_bypass`=1 | ✔ Gap A 接线在位 |

---

## 二、★ 运行时实证：过滤**双向**生效

这是本次验收的核心证据。**不是「代码在镜像里」（源码级 grep），而是运行时日志实测**。

### 2.1 两次会话的过滤结果（同一批类，两个方向）

| 会话选中 | `total`（活类数） | `dropped` | 保留 | 被剔的是谁 |
|---|---|---|---|---|
| **ds1**（THBI Oracle，默认） | 304 | **210** | 94 | ds3（SRM-TH/MySQL）的类 |
| **ds3**（SRM-TH MySQL） | 304 | **97** | 207 | ds1（THBI/Oracle）的类 |

**数字完全闭合**：`94 + 207 + 3 = 304` ⇒ 三个孤儿在**两侧都被剔除**（见 §三）。

ds3 会话原文（`datasourceId=3`）：

```
召回候选跨源过滤 dropped=97 total=304 droppedTables=['ADS_COST_REDUCE_MONTHLY',
  'ADS_PO_COMPLETE_RATE_PROD', 'DIM_FACILITY', 'DIM_IMATERIAL', 'DIM_SUPPLIER',
  'DWD_AP_INVOICE_DTL', 'DWD_ARRIVAL_NOTICE', 'DWD_ARRIVAL_NOTICE_LINE',
  'DWD_ARRIVAL_ORDER_DTL', 'DWD_BOM'] datasourceId=3
```

ds1 会话原文（`datasourceId=1`）：

```
召回候选跨源过滤 dropped=210 total=304 droppedTables=['DWD_INCOMING_INSPECTION',
  'DWD_MATERIAL', 'DWD_SUPPLIER', 'abarbeitung_order', 'ai_chat_message',
  'ai_chat_session', 'assess_deduct_list', 'assess_factors',
  'assess_factors_indicators', 'assess_indicators'] datasourceId=1
```

ds1 被剔的前 10 个正是 ds3 的表（`abarbeitung_order` / `ai_chat_*` / `assess_*` …）⇒ **方向正确，无误剔**。

### 2.2 口径交叉验证（探针 ↔ 运行时互证）

- 运行时 `total=304`（**类数**）vs 只读 SQL 探针 `distinct source_table = 302`
- 差值 **2** ⇒ 有 2 张表挂了多个类（与已知事实「`DWD_SUPPLIER_PRICE_LIST` 一表挂两类（id 55/29）」吻合）
- ⇒ 两套口径**互相印证，无口径漂移**；`schema_cache` 的 `table_name`/`owner` 键名解析正确
  （匹配代码 `_tableKeys`：裸名 ∪ owner 限定名并集、`.upper()`、owner 空则不生成限定名）

### 2.3 向量链路正常，**零降级**

```
本体类裁剪完成 pruned=18 total=94 hits=20      ← 第 1 轮
类召回扩边   hits=18 expanded=19 total=37
本体类裁剪完成 pruned=16 total=94 hits=20      ← 第 2 轮
类召回扩边   hits=16 expanded=21 total=37
```

| 观测点 | 实测 | 判读 |
|---|---|---|
| `total=94` | 与「304 − 210 = 94」一致 | ✔ 过滤后的候选集正确喂给了向量层 |
| `hits=20` / `pruned=18`（16） | 命中率 90% / 80% | ✔ 向量命中能解析回本地类 |
| `reason=` 直方图 | **空** | ✔✔ 从未降级（`本体类回退降级 reason=…` 是 **warning 级** ⇒ 生产必可见 ⇒ 空 = 事件从未发生） |
| `scene=class_scope_bypass` | 0 | ✔ 作用域窗口非空，**未发生有意的作用域绕过** |
| `scene=class_scope_too_large` | 0 | ✔ 作用域 id 未超 2000（实测 37） |
| `scene=pinned_scope_skipped` | 0 | ✔ 近 30 分钟无会话钉住语义发布（预期行为，非故障） |
| `ORA-00942` | **0** | ✔ **硬红线未触发** |
| `schema_cache 无可用表清单` | **0** | ✔ 近 30 分钟无人选 ds2（fail-open 未触发） |
| `main_data_supplier` | **0** | ✔ 墓碑 270 仍生效，未被召回 |

⇒ **Gap A（#159）的作用域收窄生效，且没有把召回打死**（"本来能答、现在答不出"这个方向的回归未发生）。

### 2.4 端到端

用户在浏览器实测「问话结果没有问题」⇒ §6.3 步骤 1（**必须出数**）✔。

⚠️ **未取到原文的一项**：前端披露提示的**确切文案与 N 值**未收集到（用户反馈"更换数据源时出现提示"，但未复制原文）。
按 §2.1 推断 ds1 应为「已按当前数据源过滤 210 张跨源数据表。」、ds3 应为「…97 张…」。
⇒ **待补**：切到 ds1 / ds3 各问一次，把两条提示原文记入本文件。

---

## 三、★★ 三个「所有源下都召不回」的类 —— 复核后定性：**结构性占位类，被剔除是正确行为**

> ⚠️ **本节结论在 2026-11-20 复核过程中被修正过两次**：
> ① 最初按 runbook §2 的 A2 判据判为「死类 / 疑似漏 introspect」，**拿到这三条类的本体行后判定推翻**
>    （见 §3.2）；
> ② 再后来**订正了理由链**：「0 行 ⇒ 不被 introspect 收录」是错的（introspect 按**列**枚举，不看行数），
>    真实原因只能是「表不在被 introspect 的 owner 命名空间内 / 表不存在 / introspection 没刷到」
>    ⇒ **定性结论不变（剔除正确）**，但待 §7.2 第 2 项的目标库存在性探针定案。

### 3.1 实测：三个类的本体行

| id | class_name | source_table | object_type | class_alias | valid_to | description |
|---|---|---|---|---|---|---|
| 1 | `IncomingInspection` | `DWD_INCOMING_INSPECTION` | Transaction | **null** | null | 「**来料检验（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）**」 |
| 2 | `Supplier` | `DWD_SUPPLIER` | Master | **null** | null | 「**供应商主数据（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）**」 |
| 3 | `ItemMaster` | `DWD_MATERIAL` | Master | **null** | null | 「**物料主数据（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）**」 |

三者 `created_by=seed`、`created_time=2026-10-23T06:35:44.891589+00:00`（同一次种子导入）、
`version=1`、`valid_to IS NULL`（活类）、`parent_class_id IS NULL`、`object_owner IS NULL`。

### 3.2 定性：**不是缺陷，是正确行为**（★ 2026-11-20 订正理由链，结论不变）

`description` 明写「**结构性建模，0 行**」⇒ 这三个类是**采购域的语义占位**（表达"供应商主数据 / 物料主数据 /
来料检验"这三个业务对象的结构），其指向的物理表 **DWD_SUPPLIER / DWD_MATERIAL / DWD_INCOMING_INSPECTION
不在任何数据源的 `schema_cache` 覆盖范围内**。

> **⚠️ 订正（本日复核）**：本文初版把原因写成「**无数据的表不会被 introspect 收录**」——
> **这是错的**。introspect 是按**列**枚举的（Oracle 走 `ALL_TAB_COLUMNS WHERE owner = '{owner}'`，
> `schema_introspection_service.py:570-578`），**不看行数** ⇒ 一张**存在但 0 行**的表**照样会被写进 `schema_cache`**。
> ⇒ 它们之所以收录不到，只能是「**表在该 owner 下根本不存在**」或「**不在被 introspect 的 owner 命名空间内**」
> 或「**introspection 没刷到**」三者之一。
> ⇒ **定性结论不变（剔除是正确行为），但理由链要按 §5.5 的判别链走**，
> **生产上尚未跑目标库存在性确认**（§7.2 第 2 项）⇒ **严格说当前定性依据是 `description` 自陈 + 上述推断**。

⇒ **被硬过滤剔除 = 正确**：
- 它们指向的表在目标库里**不存在** ⇒ 本来就答不出任何东西；
- 若留在候选集里，LLM 反而可能选中 `DWD_SUPPLIER` 去查 ⇒ **ORA-00942**（1119 事故形态）；
- runbook §2 A2 判读表说「这些类会被**静默剔除** ⇒ 该数据源不得上线」—— **该判据对「有数据的类被误剔」成立，
  对「表不存在的占位类被剔除」不成立** ⇒ **A2 探针需要按「该表在目标库里是否存在」二次分层**，否则会持续误报。
  （分层判据 = §5.5 的判别链。）

### 3.3 运行时直接实证

ds1 会话的 `droppedTables` **前 3 位就是它们**（§2.1 原文）⇒ 剔除行为在生产确实发生，行为符合预期。

### 3.4 唯一需要挂账的衔接点（属采购域 M1，不属本批）

这 3 个类是**采购域 V0.3 的语义锚点**（指向 `docs/data-knowledge/采购域.md`）。
⇒ 若 M1 采购域语义发布/召回需要它们出现在候选集里 ⇒ 需要它们在某个源有缓存，
或引入「类→源绑定」机制（本体层至今**无 `datasource_id` 列**）。
⇒ **登记到采购域 M1 的待办，不在本批处置范围。**

### 3.5 ⚠️ 顺带发现的语义后果（影响 ds3 会话的问法）

`class_alias` **三条全为 null**、`class_name` 是英文（`Supplier` / `ItemMaster` / `IncomingInspection`）
⇒ **关键词锚定（2–6 字 n-gram ⊆ `class_alias` / `class_name`）对它们一律不生效**
（中文「供应商」字面不匹配英文 `Supplier`）。

而 ds3 会话下 `DIM_SUPPLIER`（ds1 的 Oracle 表）**被硬过滤剔除** ⇒

> **ds3 会话里「供应商」的语义锚定没有腿**：竞争集内不含任何带中文「供应商」字样的类
> （`id=2` 是英文 `Supplier` 且无别名，不参与竞争）。
> ⇒ ds3 下问「供应商」**只能靠向量召回命中 `MAIN_DATA_SUPPLIER`**，锚定救援不接管。
> ⇒ 这与 §2.3 实测「ds3 会话零降级」**不矛盾**（没降级说明向量路径直接命中了），但意味着
> **ds3 会话对「供应商」类问句的鲁棒性弱于 ds1**（ds1 下 `DIM_SUPPLIER` 保留 ⇒ 锚定有腿）。

**待验证**（只读）：全库哪些活类带中文「供应商」字样，以及它们在 ds1 / ds3 下分别是否被保留。

```sql
SELECT id, class_name, class_alias, source_table
FROM ontology_class
WHERE valid_to IS NULL
  AND (class_name ILIKE '%供应商%' OR coalesce(class_alias, '') ILIKE '%供应商%')
ORDER BY id;
```

### 3.6 引用面（列名已确认，探针待跑）

`ontology_metric.target_class_id` / `ontology_metric_source.class_id` / `kpi_catalog.metric_id`
⇒ 精确引用查询：

```sql
SELECT id, metric_name, target_class_id, execution_source, status
FROM ontology_metric WHERE target_class_id IN (1, 2, 3);

SELECT ms.metric_id, ms.class_id, ms.role, m.metric_name
FROM ontology_metric_source ms JOIN ontology_metric m ON m.id = ms.metric_id
WHERE ms.class_id IN (1, 2, 3);

SELECT id, kpi_code, kpi_name, metric_id, datasource_id, status
FROM kpi_catalog
WHERE metric_id IN (SELECT id FROM ontology_metric WHERE target_class_id IN (1, 2, 3));
```

**参考事实**：M1-1.4 采购本体对象核对记录「**7 个墓碑类仍被 8 条 KPI 引用**」⇒ 采购域存在
「类被墓碑 ⇒ 指标仍引用它」的历史模式 ⇒ 这三个 0 行占位类**若也被引用**，
引用它们的 KPI 在召回时拿不到类 ⇒ 该形态属 M1 采购域的既有挂账，不是 1120 引入的。

---

## 四、遗留缺口：ds2（`SEED_DS_KPI_CATALOG`）路径**未根治**

`schema_cache` 里 **ds2 一行都没有** ⇒ 选中 ds2 时 `loadDatasourceTableSet` 返回 `None`
⇒ **fail-open ⇒ 完全不过滤** ⇒ 跨源幽灵表在 ds2 会话**原样回来**（1119 的原始故障路径没关掉）。

- runbook §2 A1 判读表原文：**「A1 空 ⇒ 该数据源不得上线 ⇒ 先补 introspect」**。
- **但需区分两种可能**：ds2 是 KPI 目录种子源（`postgresql` 类型），可能**本来就没有可 introspect 的业务表**
  ⇒ 它的 fail-open 是设计上不可避免（要么它不用于跑 SQL，要么它该 fail-closed）。
- 近 30 分钟 `schema_cache 无可用表清单` = 0 ⇒ **暂未有人用它**，无即时影响。
- **这不是本次发版引入的回归**（filter 上线前也无过滤），但"根治"声明对 ds2 不成立 ⇒ **策略已收敛为选项 B（`is_active=false`）**，见 §4.2。

### 4.1 三个数据源的缓存覆盖度（实测）

```sql
SELECT id, name, type, is_active, is_default,
       (SELECT count(*) FROM schema_cache sc WHERE sc.datasource_id = ds.id) AS cache_rows,
       (SELECT coalesce(sum(jsonb_array_length(sc.schema_data)),0)
          FROM schema_cache sc WHERE sc.datasource_id = ds.id) AS cached_tables
FROM data_source ds ORDER BY id;
```

| id | 源 | 类型 | 默认 | cache_rows（owner 数） | cached_tables | 类归属 | 运行时保留 | 过滤态 |
|---|---|---|---|---|---|---|---|---|
| 1 | THBI Oracle | oracle | ✔ | 2 | **1817** | ~99 张表 | 94 / 304 | **硬过滤生效** |
| 2 | SEED_DS_KPI_CATALOG | postgresql | — | **0** | **0** | — | — | ⚠️ **fail-open ⇒ 完全不过滤** |
| 3 | SRM-TH | mysql | — | 1 | **220** | ~200 张表 | 207 / 304 | **硬过滤生效** |

**判读**：
- **ds1**：缓存 1817 张物理表 ⇒ 是超集（类只挑了一小部分登记）⇒ 正常。类保留率 31%（94/304），
  剔掉的 210 个是 ds3 的表 ⇒ **方向正确**。
- **ds3**：缓存 220 张、类占 ~200 张 ⇒ **91% 覆盖**，很紧。类保留率 68%（207/304），
  剔掉的 97 个是 ds1 的 Oracle 表（**含 `DIM_SUPPLIER` / `DIM_FACILITY` / `DIM_IMATERIAL`**）
  ⇒ **维度表全在 ds1、业务表全在 ds3** ⇒ ds3 下问「按等级/分类统计供应商」这类需维度表的问句**答不出**
  （数据隔离的设计代价，非 bug，但要有业务预期）。
- **ds2**：缓存 0 行 ⇒ 选中即 fail-open ⇒ 候选集 = 全部 302 个类，而 SQL 打在 ds2 的 PostgreSQL 上
  ⇒ `DWD_*` / `MAIN_DATA_*` 在 PG 库里不存在 ⇒ **1119 的故障（PG 侧等价物
  `relation "main_data_supplier" does not exist`）在 ds2 会话里原样复现**。

### 4.2 ds2 的三个处置选项（**已用探针收敛为 B；本批未做任何改动**）

⚠️ **ds2 不能简单「补 introspect」** —— 那只会换个方式坏：若给 ds2 补上 introspection（只含 KPI 目录相关 PG 表），
它会变成硬过滤 ⇒ 那 302 个业务类**全部被剔**（都不在 ds2 的表清单里）⇒ ds2 彻底答不了任何业务问句。

| 选项 | 效果 | 代价 | 性质 |
|---|---|---|---|
| **A. 补 introspect** | ds2 只剩 KPI 目录类可用 | 业务问句在 ds2 全废（本来也不该在 ds2 问业务） | 生产数据写 |
| **B. ds2 `is_active=false`** | 下拉里不出现 ds2 ⇒ 等价于移除 | 可能打断别的引用 ds2 的功能（KPI 种子、feature 绑定…） | 生产数据写 |
| **C. 该源 fail-closed**（代码改） | API 直接传 `datasourceId=2` 也拦得住 | 改代码 | **代码** |

#### 4.2.1 ds2 引用面探针（**已执行，全 0**）

```sql
DO $$
DECLARE r record; c bigint;
BEGIN
  FOR r IN SELECT table_name FROM information_schema.columns
           WHERE column_name = 'datasource_id' AND table_schema = 'public' ORDER BY table_name
  LOOP
    EXECUTE format('SELECT count(*) FROM %I WHERE datasource_id = 2', r.table_name) INTO c;
    RAISE NOTICE '% -> %', r.table_name, c;
  END LOOP;
END $$;
```

生产实测输出（10 张带 `datasource_id` 的表，**引用 ds2 的行数全为 0**）：

```
data_quality_rule -> 0          data_quality_violation_sample -> 0
discovery_source -> 0           feature_definition -> 0
kpi_catalog -> 0                multi_step_run -> 0
research_run_state -> 0         research_session -> 0
schema_cache -> 0               session_query_state -> 0
```

⇒ **ds2 是纯摆设：没有任何一张表、任何一行数据引用它。**

#### 4.2.2 代码侧只读核对（开发机 grep `origin/main`，无任何改动）

| 检查 | 结果 |
|---|---|
| 全仓硬编码 `SEED_DS_KPI_CATALOG` | **只有 1 处**：`backend/scripts/seed_kpi_catalog.py:36`（它就是创建 ds2 的脚本本身） |
| 全仓硬编码 `datasource_id = 2` | **0 处**（`backend/app` 内无） |
| ds2 的 `is_active` 初值 | **True**（`seed_kpi_catalog.py` 的 `_getOrCreateSeedDataSource` 显式 `is_active=True`）⇒ **它现在就在聊天下拉里**（`listDataSources(true)` 会返回它） |
| 聊天面板取源 | `frontend/src/components/chat/ChatPanel.tsx:68` → `listDataSources(true)` ⇒ **只列 active** ⇒ `is_active=false` 生效 |
| 研究入口取源 | `frontend/src/hooks/useDatasourceOptions.ts:21` → `listDataSources(true)` ⇒ 同上 |
| 管理页取源 | `frontend/src/pages/DatasourcePage.tsx:112` → `listDataSources(false)` ⇒ 管理视图**仍能看到** ds2（可随时改回 active）—— 符合预期 |
| 后端列表口径 | `GET /datasources` 默认 `activeOnly=false`；`activeOnly=true` 时才加 `is_active IS TRUE` |
| 后端按 id 取源 | `DataSourceService.get()` **不校验 `is_active`** ⇒ 显式传 `datasourceId=2` 的 API 请求**仍然走得通** |
| 聊天面板自动选源 | `datasourceId === null` 时自动选中 `is_default` 的源（**生产 = ds1**）⇒ 正常聊天路径**永远带具体 datasourceId**，不会落进 `no_datasource` 的 fail-open 分支 |

#### 4.2.3 结论：**走 B（`is_active=false`）**

- **零代码、零迁移、零误伤**：ds2 无任何数据引用（4.2.1）+ 无任何代码引用（4.2.2）⇒ 停用它不影响任何在用功能；
- **它现在确实在聊天下拉里**（`is_active=True` + `listDataSources(true)` 只挡 inactive，active 的照列）⇒
  这正是「用户误选 ds2 ⇒ 走 PG 侧不存在表 ⇒ 1119 故障复现」的入口 ⇒ **B 直接堵住这个入口**；
- **B 的边界**（必须记挂账）：B **只挡 UI 选择**，不挡显式 API 请求 ⇒ 要彻底关死才需要 **C（代码 fail-closed）**。
  C 是代码改动，**本轮不做**，单独评审。

#### 4.2.4 探针**已在生产执行**（2026-11-20，用户执行）⇒ 命中第三支 ⇒ **B 零影响**

原假设：「生产要么没跑过这个种子，要么跑的是绑定前的旧版本」。
**⚠️ 该假设已订正** —— 实测是**种子跑过（KPI 目录有 16 行），但绑定落在 ds1 上**，
既不是「没跑」也不是「旧版绑定逻辑」。

生产实测输出（只读）：

```sql
SELECT count(*) AS total, count(datasource_id) AS with_ds FROM kpi_catalog;
SELECT datasource_id, count(*) FROM kpi_catalog GROUP BY 1 ORDER BY 1;
SELECT id, name, type, is_active, is_default FROM data_source ORDER BY id;
```

```
 total | with_ds                          datasource_id | count         id |        name         |  type    | is_active | is_default
--------+---------                        ---------------+-------         ----+---------------------+----------+-----------+------------
   16  |      16                                     1 |    16            1 | THBI Oracle         | oracle   | t         | t
                                                                  2 | SEED_DS_KPI_CATALOG | postgresql | t      | f
                                                                  3 | SRM-TH              | mysql    | t         | f
```

**判读**：命中原三分支里的**第三支**（「有行的 `datasource_id` 指向别的源」）
⇒ **ds2 对 KPI 目录零引用**（16 行全部绑 ds1）⇒ **B 零影响**。

⇒ **B（`UPDATE data_source SET is_active=false WHERE id=2`）现在是一步零影响的操作**，
但它仍是**生产数据写** ⇒ 须**用户明示批准后由用户在生产执行**，本轮不代执行。

**★ `is_active=false` 对 ds2 的完整影响面（开发机核 `origin/main` 的全部 `is_active` 消费点）**：

| 消费点 | ds2 停用后的行为 | 影响 |
|---|---|---|
| `datasource_service.list(activeOnly=True)`（:105） | ds2 从 active 列表消失 | ✅ **B 的目的本身**（聊天下拉不再出现 ds2） |
| `datasource_service.list(activeOnly=False)` | ds2 **仍在**管理页可见 | ✅ **可逆**（随时改回 `is_active=true`） |
| `_promoteNextDefault`（:348） | 仅在**删除默认源**时提升下一个 active | 本次不动 ds1（仍是默认）⇒ **不触发** |
| `data_quality_service.py:123` | ds2 移出 DQ 规则的数据源候选集 | 无影响（ds2 引用面全 0） |
| `kpi_datasource_suggestion_service.py:275` | ds2 不再被推荐为 KPI 数据源 | 无影响（ds2 从未在用） |
| `ontology_join_health_service.py:166` | ds2 不参与 JOIN 健康度 | 无影响 |
| `discovery_snapshot_service.py:479` | ds2 不再做 discovery snapshot | 无影响（ds2 无 `schema_cache` ⇒ 本来也不做） |
| `kpi_catalog_service.py:228` | **ds2 被停用后不能再把 KPI 绑到 ds2** | 无影响（现有 16 行全绑 ds1）＋语义上正是期望约束 |
| `datasource_service.get()` | **不校验 `is_active`** ⇒ 显式 API 传 `datasourceId=2` 仍走得通 | ⚠️ **B 的已知边界**（要彻底关死需 **C 代码 fail-closed**，另批） |

**★ 顺带修正一处口径**：生产只有 **3** 个数据源（id 1/2/3），
本地探针出现的 ds4/ds5/ds9 **生产不存在** ⇒ 不影响任何判据（口径按 `data_source` 实际行取，不写死 id）。

**ds2 现状**：`is_active = t` ⇒ **仍挂在聊天下拉里** ⇒ **仍是「用户误选 ds2 ⇒ 1119 故障原样复现」的入口**
⇒ **B 依然是正确的堵法**，只是「是否现在执行」是用户决策。


---

## 五、★ 与 runbook 的偏差 + 口径矩阵（runbook 偏差 + 入口口径 + 判别判据 + 运维坑）

### 5.1 runbook §2 的三条 SQL **本身是坏的**（实测报错）

| 原写法 | 实测错误 | 根因 | 正确写法 |
|---|---|---|---|
| `jsonb_array_length(sc.schema_data)` 直接进 `GROUP BY` 查询 | `column "sc.schema_data" must appear in the GROUP BY clause` | `jsonb_array_length()` **不是聚合函数** | 必须 `sum(jsonb_array_length(...))` 包裹 |
| `upper(t ->> 'table_name' \|\| '.' \|\| t ->> 'owner')` | `operator does not exist: text ->> unknown` | `->>` 与 `\|\|` 优先级/类型歧义 | `upper(concat_ws('.', t ->> 'table_name', nullif(t ->> 'owner', '')))` |
| `LIKE 'DIM\_%'` | `invalid command \_%'` | psql 把反斜杠当元命令 | `starts_with(t, 'DIM_')` |

**额外坑（不在 runbook 里）**：`docker exec X sh -c 'psql -c "...SQL..."'` 的**外层单引号会吃掉 SQL 里所有单引号**
（不只 `''`）⇒ 必用 heredoc 走 stdin：`docker exec -i X sh -c 'psql …' <<'SQL' … SQL`（或 `psql -f`）。

### 5.2 runbook §2 的 A2 判据**只查单源**，且**不区分「0 行占位类」与「真被误剔的类」**

A2 的口径是「本源的活类在本源缓存里找不到」⇒ 只能发现「本源覆盖度不足」，
**发现不了「在所有源下都找不到」的类**（本次的 3 个就是被跨源口径的探针发现的）。

⚠️ **更重要的**：A2 的判读表把「A2 非空 ⇒ 该数据源不得上线」写成**无条件**规则，
但本次实测证明**存在合法反例** —— 3 个类是 **0 行结构性占位类**（§3.2）⇒ 被剔除是**正确行为**。
⇒ **建议 A2 判据加一层过滤**：只对「该 `source_table` 在目标库里**确实存在且有数据**」的类报警，
否则占位类会持续误报「该数据源不得上线」。
**★ 完整判别链（PG 侧 + 目标库侧 + 定性表）见 §5.5；可复用探针见 [`probe-recall-datasource.sql`](./probe-recall-datasource.sql)。**

**建议补一条全源孤儿探针**（本次实际使用的口径；= 探针文件 P4）：

```sql
WITH cls AS (
  SELECT DISTINCT upper(oc.source_table) AS t
  FROM ontology_class oc
  WHERE oc.valid_to IS NULL AND coalesce(oc.source_table, '') <> ''
),
cache AS (
  SELECT DISTINCT sc.datasource_id,
         upper(t ->> 'table_name') AS bare,
         upper(concat_ws('.', t ->> 'table_name', nullif(t ->> 'owner', ''))) AS qualified
  FROM schema_cache sc, jsonb_array_elements(sc.schema_data) AS t
)
SELECT c.t AS orphan_table FROM cls c
WHERE NOT EXISTS (SELECT 1 FROM cache cc
                  WHERE cc.bare = c.t OR cc.qualified = c.t)
ORDER BY 1;
```

### 5.3 四条入口的过滤口径矩阵（★ chat / 多步 / 研究 / 显式 API 是否同口径）

> 本节为**跨会话共用口径**（采购线 b4 验收直接复用，不要另写一套）。

**过滤调用点共 4 处，全部调用同一个 `applyDatasourceRecallFilter`**（`recall_datasource_filter.py:108`），
三态语义完全一致 ⇒ **过滤层口径统一**：

| # | 入口 | 调用点 | `datasourceId` 来源 | 可能为 `None`？ | 运行时语义 |
|---|---|---|---|---|---|
| 1 | chat 主链路（类召回） | `chat_recall.py:314` | `ChatRequest.datasourceId`（`domain/schemas.py:2079`，**必填 `int`**） | **否** | 有缓存 ⇒ 硬过滤；无缓存 ⇒ fail-open `no_cache` |
| 2 | chat NL2SQL（生成 SQL） | `chat_service.py:1013` | 同上 `dto.datasourceId` | 否 | 同上 |
| 3 | 多步 · 步骤级召回 | `chat_multistep.py:536` | `getattr(pc.ds, "id", None)`；`pc.ds` 由**同一个** `dto.datasourceId` 取来 | 否 | 同上 |
| 4 | 研究 · 执行面（步 SQL 生成） | `research_agent_execution.py:386` | `deps.datasourceId` ← `research_session.datasource_id`（空则 `RuntimeError`，**不静默回落**，`research_agent_execution.py:159-162`） | 否 | 同上 |
| — | **研究 · BO 臂（规划层）** | **无过滤**（`_searchBusinessObjects`，`api/v1/research.py:254`） | — | — | **全局检索，无数据源维度** ⇒ 见 §5.4 |

**三条结论：**

1. **口径一致**：4 个过滤调用点共用同一函数、同一三态语义 ⇒ chat / 多步 / 研究执行面**不存在**「各写一套过滤」的问题。
2. **`no_datasource` 分支在这 4 条入口上不可达**：三个 `datasourceId` 来源（必填 int / 同一 dto / 会话绑定非空）
   **都强制非空** ⇒ `datasourceId=None ⇒ 不过滤且服务端零日志` 这个**观测盲区**在这几条链路上**不会出现**。
   ⇒ 现实里能触发的 fail-open **只有 `no_cache`**（缓存缺失/空/读失败）⇒ 当前只有 ds2 命中。
3. **显式 API 传 `datasourceId=2`** ⇒ 走入口 1/2 ⇒ `loadDatasourceTableSet(2)` 返回 `None`
   ⇒ **fail-open ⇒ 完全不过滤** ⇒ 1119 故障原样复现。⇒ 这就是 §4.2.3 里 **B 方案的边界**
   （B 只挡 UI 选择，不挡显式 API），要彻底关死需 **C（代码 fail-closed）**。

### 5.4 研究入口的口径缺口：**只存在于规划层，不在执行层**（收口方案要按这个分层）

peer 线挂的缺口「研究入口召回未收窄到数据源」**属实，但分层要分清**：

| 层 | 有无数据源维度 | 依据 |
|---|---|---|
| **规划层（BO 臂）** | ❌ **无** —— `_searchBusinessObjects` 是**全局**本体类检索 | `api/v1/research.py:254`；代码注释自陈：「本臂是**全局**检索（无数据源维度）；数据源维度的收窄在执行面」（`:266-268`） |
| **执行层（步 SQL 生成）** | ✔ **有** —— 类候选集先按数据源硬过滤 | `research_agent_execution.py:386` + `selectClassesForTables`（`research_agent_ports.py:643`） |

**为什么执行层是安全的**（关键在 `selectClassesForTables` 的取交集语义）：

- 传给 `selectClassesForTables` 的 `classes` = **已按数据源过滤后**的集合（不是全局集合）；
- 它取「过滤后集合 ∩ ESL 给出的表名」⇒ 别源的类**不在过滤后集合里** ⇒ 交集里没有它；
- 交集为空时**退回的是「过滤后的全量」**（`return matched or list(classes)`，`research_agent_ports.py:657`），
  **不是全局全量** ⇒ **别源的表不会被塞进 LLM 的 schema 上下文**。

⇒ **残余风险是「规划层选错 BO」**（步骤问句围绕别源的业务对象编排 ⇒ 执行时大概率查空 / `STEP_MISSING_SQL`），
**不是 ORA-00942**。⇒ 收口方案：BO 臂加数据源维度（需要 BO→源绑定，本体层至今无 `datasource_id` 列）
**与**过滤层加固是**两件事**，不要混做。

### 5.5 ★ 判定「剔除是正确行为（结构性 0 行）vs 漏召回（类有数据却看不见）」的**判别判据**

> 可复用探针全文见 [`probe-recall-datasource.sql`](./probe-recall-datasource.sql)（P1 总览 / P2 逐类明细 / P3 判别链 / P4 全源孤儿）。
> 四段均已在本地 `qa_metadata` 实跑通过（**别照抄 runbook 那三条坏 SQL**）。

**判别链（三步，务必按序走）：**

| 步 | 在哪查 | 查什么 | 结论 |
|---|---|---|---|
| **[1] PG 侧** | `qa_metadata` | 类被剔除 ⇔ `upper(source_table)` 不在该源 `schema_cache` 的表名集合（裸名 ∪ owner 限定名，`.upper()`） | 只说明「缓存里没这张表」，**不能定性** |
| **[2] 目标库侧（决定性）** | **目标 Oracle 库**：`SELECT owner, table_name FROM all_tables WHERE upper(table_name) = '<表名>'` | 该表**存不存在** | 见下表 |
| **[3] 定性** | — | 组合 [1]+[2] | 见下表 |

| [2] 的结果 | 定性 | 处置 |
|---|---|---|
| **查无此表** | ✅ **剔除是正确行为**（结构性占位类）—— 类指向一张**不存在的物理表**；留着它 LLM 会选中它 ⇒ ORA-00942（1119 事故形态） | **无需任何处置**；只登记「类→源绑定」语义缺口（挂采购域 M1） |
| **有此表**，但不在 `schema_cache` | ❌ **真缺陷（漏召回：类有数据却看不见）** —— 根因是 **introspection 过期/漏刷**，**不是**过滤逻辑 | **重跑 introspect**；**不要改过滤** |
| **有此表**且在 `schema_cache` | ⚠️ 匹配口径有 bug（owner / 大小写 / 裸名） | 代码缺陷，单独提 issue |

#### ★★ 5.5.1 Oracle 侧必须按 **(owner, table_name)** 精确比对（否则会把「跨 owner 同名」误判成漏召回）

> 这条是跨会话评审（采购线）提出的方法论订正，我复核了实现，**成立且必须写进判据**。

**实现事实（已核 `origin/main`）**：

- `schema_introspection_service.py:73-77` `_oracleOwner(username)` ⇒ **owner = `UPPER(ds.username)`**，
  可显式覆盖；**一个连接可以看到并缓存多个 owner 的表**
  （代码注释自陈：「ZJTH 连接可同时见 ZJTH 与 THBI」）⇒ **一个源可有多行 `schema_cache` = 多个 owner**；
- `recall_datasource_filter._tableKeys` ⇒ 键集合 = **裸名 ∪ `OWNER.TABLE` 限定名**（全大写），
  且**裸名不区分 owner** ⇒ 各 owner 的同名表在裸名维度上**互相放行**。

**⇒ 判据必须细化为三态**（判读时先取「该源的 owner 集合」，
即 `schema_cache.schema_data` 里出现过的 `owner` 去重，见探针 P3b）：

| 目标库 `all_tables` 的结果 | 定性 | 处置 |
|---|---|---|
| **任何 owner 下都查无此表** | ✅ **剔除正确**（类指向一张不存在的物理表） | 无需处置；登记「类→源绑定」缺口 |
| 此表存在于**该源的 owner 集合内**（本源的 schema），但不在该源的缓存表名清单里 | ❌ **真缺陷候选**（introspection 过期/漏刷）⇒ 漏召回 | **重跑 introspect**；不要改过滤 |
| 此表**只**存在于该源 owner 集合**之外**的 owner（别的 schema / 别的用户下的同名表） | ✅ **剔除正确**（那不是本源的表） | 无需处置 |
| 此表在缓存里却仍被判「剔除」 | ⚠️ 匹配口径 bug | 代码缺陷，单独提 issue |

**⚠️ 第 2 行只能判「真缺陷候选」，不能直接定案**：`ontology_class` **没有 `owner` / `datasource_id` 列**
⇒ 从本体侧**无法确定某个类指的是哪个 owner** ⇒ 「类语义上指的本源 owner 下的那张表存在，但缓存里没有」
与「类其实指的是别的 owner」在现有数据下**不可区分** ⇒ 定案还需要「类→源/owner」绑定信息
（这正是 §3.4 / 采购域 M1 挂的那个语义缺口）。

**★ 本地实测佐证（`qa_metadata`，探针 P3b 实跑）**：ds1 的 `schema_cache` 覆盖 **3 个 owner**
= `OUTLN`(3 表) / `THBI`(110 表) / `ZJTH`(1680 表) ⇒ **单源多 owner 是实证事实，不是理论**
⇒ 不带 owner 查 `all_tables` 必然有误判风险。

**⚠️ 反向误判（这就是「不带 owner 查 `all_tables`」的坑）**：
若查 `all_tables` 时**不按 owner 过滤**，则「别的 owner 下的同名表」会让 [2] 返回「有此表」
⇒ 会把**剔除正确**误判成**漏召回（真缺陷）** ⇒ 可能触发一次**不必要的 introspect 重跑**，
甚至误改过滤逻辑。**这就是本条判据必须带 owner 的原因。**

**★ 为什么「存不存在」就是决定性的**：introspect 是按**列**枚举的
（Oracle 走 `ALL_TAB_COLUMNS WHERE owner = '{owner}'`，`schema_introspection_service.py:570-578`），
**不看行数** ⇒ 一张**存在但 0 行**的表**照样会被写进 `schema_cache`**
⇒ 「表存在却不在 `schema_cache`」= **introspection 没刷到**（真缺陷），
而不是「表是空的所以不算」。

**⚠️ 三条口径注记（写判据前必读）：**

1. **裸名匹配不区分 owner**：`_tableKeys` 把每个 owner 行的 `table_name` 都加进裸名键
   ⇒ 跨 owner 同名表会互相放行 ⇒ 判读结果时若表名跨 owner 重名，先排除这一条。
2. **Oracle introspection 按 owner 分片**（owner = `UPPER(data_source.username)`，可显式覆盖；
   一个源可有多行 `schema_cache` = 多个 owner）⇒ 「某表不在本源清单」也可能只是**owner 选错**，
   P3 查 `all_tables` 时**要带 owner 一起比对**。
   ★ **已细化为三态判据 + 精确比对口径，见 §5.5.1**（不按 owner 过滤 ⇒ 跨 owner 同名表
   会把「剔除正确」误判成「漏召回」⇒ 可能触发不必要的 introspect 重跑）。
3. **无 `source_table` 的类永远保留**（代码不误杀）⇒ 这类计在 kept，
   既不算「漏过滤」也不算「漏召回」。

**启发式只用于快速分流（不是判据）**：`description` 含「0 行 / 结构性」字样 ⇒ 大概率是占位类
⇒ 本次三个类即属此类（`acceptance-1120.md` §三）。

### 5.6 其他运维坑（本次踩到）


| 坑 | 现象 | 正确做法 |
|---|---|---|
| `docker images \| grep repo:tag` | **假阴性**（`docker images` 分列输出，字面量永不出现 ⇒ 误判"镜像没 load"） | `docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}'` |
| `grep -c "reason=no_match"` | **误命中** `reason=no_match_ods_filtered` | `grep -oE "reason=[a-z_]+" \| sort \| uniq -c` |
| `scene=anchor_rescue_*` 是 **info 级** | 生产 `LOG_LEVEL=WARNING` 时**永远看不见** ⇒ 观测盲区 | 本次不构成漏判（`reason=` 全空已证明压根没进锚定救援）；但以后靠这两个观测点判断锚定是否触发，**必须先确认生产 LOG_LEVEL** |

---

## 六、§7 墓碑 270：**未执行**（判定保留）

```sql
UPDATE ontology_class SET valid_to = NULL WHERE id = 270;   -- ★ 未执行
```

**当前判定：保留墓碑。** 理由：

1. runbook §7 前置写死「§6.2 / §6.3 已全绿」⇒ §6.3 步骤 2（披露提示 N 值）未取证；
2. 必须在 **SRM 数据源会话（ds3）** 下实测「正常召回 + 无 ORA-00942」，**不能用 ds1 会话的结果代替**；
3. 「换行（新建一行）= 换 `class_id` = **Milvus 向量悬空** = 白做」⇒ 宁可漏召，不可幽灵表。

**复活场景已就位**：`MAIN_DATA_SUPPLIER` 只在 ds3 有缓存（`sources=3`）
⇒ ds3 会话下它**会被保留**（不会被剔）⇒ 复活后在 ds3 会话能被正常召回，SQL 打在 MySQL 上 ⇒ 不会 ORA-00942。
**复活后必须在 ds3 会话重跑一遍验证，不能靠推断。**

**id 271 未探针核实前一律不动**（本地测试库无该 id 种子，不得据本地推断）。

---

## 七、最终判读与待办

### 7.1 判读

| 维度 | 结论 |
|---|---|
| 部署动作正确性 | ✔ 11 项逐条核过，全部正确 |
| 本批代码落地 | ✔ 镜像内 = 运行态 bundle；5 调用点 grep 与 runbook 期望逐字相符 |
| **过滤运行时生效** | ✔✔ **双向实证**（ds1 剔 210 / ds3 剔 97，数字闭合） |
| **Gap A 未打死召回** | ✔ 零降级 / 零 bypass / 零 too_large；命中解析率 80–90% |
| 硬红线 `ORA-00942` | ✔ 0 |
| 端到端出数 | ✔ 用户实测无问题 |
| **§2 前置门禁** | ⚠️ **上线后才执行**；A2 非空但**经复核属合法反例**（3 个 0 行占位类，§3.2）⇒ **不构成「该数据源不得上线」**；真正的门禁缺口是 **ds2 的 A1 为空**（§四） |
| **三个 0 行占位类** | ✔ **被剔除是正确行为**（§3.2）⇒ **无需任何处置**；⚠️ **理由链已订正**（不是「0 行不被 introspect 收录」，而是「表不在被 introspect 的 owner 命名空间内 / 表不存在」—— introspect 按**列**枚举，0 行的表也会被收录；**定性不变**，待 §7.2 第 2 项的目标库存在性探针定案）；遗留：采购域 M1 衔接（§3.4）+ ds3 下「供应商」锚定无腿（§3.5） |
| ds2 路径 | ✘ **fail-open 未根治**（§四）—— 1119 故障在 ds2 会话原样复现 ⇒ **已收敛为选项 B（`is_active=false`）**：引用面 10 张表全 0 + 代码零硬编码 + **§4.2.4 探针已在生产执行（命中第三支 ⇒ B 零影响）** ⇒ **只剩「执行」这一步，属生产数据写，待用户明示批准** |
| §7 墓碑 270 | ⛔ 未执行，判定保留（§六） |
| **是否需要回滚** | ❌ **不需要** —— 零 ORA-00942、零崩溃、零数据损坏；剩余缺口是 ds2 路径与采购域衔接，**都不需要动镜像** |

### 7.2 待办清单（按优先级）

| # | 事项 | 性质 | 阻塞 |
|---|---|---|---|
| 1 | 3 个 0 行占位类的**引用面**探针（`ontology_metric.target_class_id` / `ontology_metric_source.class_id` / `kpi_catalog.metric_id`） | 只读 | 影响面评估（确认无指标挂靠 ⇒ 剔除无业务后果） |
| 2 | **★ 决定性探针**：Oracle 侧 `all_tables` 查 `DWD_SUPPLIER` / `DWD_MATERIAL` / `DWD_INCOMING_INSPECTION` 是否存在 ⇒ ① 决定 §3.2 定性理由链（**表不存在 ⇒ 剔除正确**；**表存在却不在 schema_cache ⇒ introspection 过期 ⇒ 真缺陷，须重跑 introspect**，判据见 §5.5）；② 事实登记到**采购域 M1**（**不再定分支、不改本体**） | 只读 | §3.2 定性 / M1 衔接（§3.4） |
| 3 | **ds2 处置：执行 `UPDATE data_source SET is_active=false WHERE id=2`**（**生产数据写**）—— ✅ **前置只读探针已执行并闭环（§4.2.4，命中第三支 ⇒ B 零影响）** ⇒ **只剩执行本身**，须用户明示批准后由用户在生产执行；B 只挡 UI，显式 API 仍可打 ds2 ⇒ C（代码 fail-closed）另批 | 生产数据写 | ds2 路径根治 |
| 4 | 补 ds1 / ds3 各一次的**披露提示原文与 N 值** | 只读 | §6.3 闭环 |
| 5 | 研究入口回归（C1 ≈ 200+ > 0 ⇒ runbook 明写必做） | 只读 | §6.3 闭环 |
| 6 | 泛化问句回归（不含维度词，验证窗口未收得过窄） | 只读 | §6.3 闭环 |
| 7 | runbook §2 三条 SQL 修正 + A2 判据加「该表是否存在/有数据」二次分层 + 补全源孤儿探针（**探针文件已备好**：[`probe-recall-datasource.sql`](./probe-recall-datasource.sql) P1/P2/P4，已本地实跑通过） | 文档 | 下次发版 |
| 8 | `anchor_rescue_*` 的日志级别（info → warning） | **代码** | 观测盲区（本批不阻塞） |
| 9 | ds3 会话「供应商」锚定无腿（id=2 `class_alias=null` + `DIM_SUPPLIER` 被剔）⇒ 定语义补法 | 决策 | 问法敏感（§3.5） |
| 10 | §7 墓碑 270 原地复活 | **生产 DML** | 待 4/5/6 全绿 |
| 11 | **研究 BO 臂（规划层）加数据源维度**（`api/v1/research.py:254` `_searchBusinessObjects` 是全局检索）⇒ 需「BO→源绑定」（本体层无 `datasource_id` 列）；**执行层已过滤，不必动**（§5.4） | 决策 + **代码** | 研究入口问法准度（非本批） |

### 7.3 ★ 交接（2026-11-20，本只读复核会话收尾）

本会话（**只读复核**）到此结束。下列事项**移交采购线会话 `qa-system-9e`** 作为其待办：

| 类别 | 事项 | 备注 |
|---|---|---|
| **代码** | **C 方案：代码级 fail-closed** —— `DataSourceService.get()` **不校验 `is_active`** ⇒ 显式传 `datasourceId=2` 的 API 仍可打 ds2 ⇒ B 的已知边界 | 采购线已声明「单列一批，不与 B 混做」 |
| **代码** | **研究 BO 臂（规划层）加数据源维度** —— `api/v1/research.py:254` `_searchBusinessObjects` 是全局检索（§5.4） | 需「BO→源绑定」（本体层无 `datasource_id` 列） |
| **代码** | `anchor_rescue_*` 日志级别 **info → warning** —— 现为 info 级 ⇒ 生产 `LOG_LEVEL=WARNING` 时永远看不见（观测盲区） | §5.6 |
| **版本** | **PR #162 的 CI 结论判定**（doc-only ⇒ 按「净新增失败 0」判据，不按 run 结论） | PR OPEN / MERGEABLE / BLOCKED |
| **版本** | **PR #162 合并执行** | 用户可随时改判 |
| **版本** | **镜像/版本等价性复核** —— `git diff <构建基线> origin/main -- backend/ frontend/ docker/` ⇒ 文档提交不改镜像字节 ⇒ 无需重建镜像 | ⚠️ 只比这三个路径；`Harness/`、`docs/` 被 `.dockerignore` 排除 ⇒ 整树 diff 是假警 |

> **★ 交接澄清（peer 回执核对后追加）**：采购线 peer 在回执里把「仓内有过『关闭 logger.warning 降噪』的既有决策」列为第 3 项（`anchor_rescue_*` 日志级别）的前置检查。
> **该前提经复核不成立**：`Harness/wiki/chat-service-assessment.md:1226` 的 §15 残差**第 8 条**原文是
> 「**`logger.warning(…, exc)` 截断评估**（2026-09-28 评估，**关闭**）… 三类形态全部合法 … **截断隐患不存在**」
> ⇒ **关闭的是「消息截断」这个待评估项，不是「关掉 logger.warning」**；仓内**无**任何「warning 降级/降噪」决策，
> 相反口径占主流：**降级点必须 `logger.warning` 留痕，不静默吞** ⇒ 第 3 项**无冲突，可直接做**。
> ⚠️ 实现口径（供接手方，省一轮 recon）：两行在 `chat_recall.py` **:1025-1027** / **:1039-1041**（核对源 = 1120 构建基线 `d1c20de9`），均为 `logger.info`；
> **info 是刻意选的**（docstring `:1018-1020`：「scene= 不是 reason=，不污染回退率聚合」）⇒ 升 warning **不破坏**该聚合（聚合按 `reason=` 取键），
> 但须确认 `reason=` 那行**仍是 warning 级**、直方图口径不变；并补一条断言该两行级别的单测（否则「改了但没人看得见」）。
> ⚠️ **C 方案覆盖面提醒**：`ontology_class` 侧**无 `is_active`/`datasource_id` 列** ⇒ C 只覆盖**数据源入口**，
> 不覆盖「类→源」语义（后者是采购域 M1 的挂账，**不要在 C 里顺手扩**）。

**明确不在交接范围（仍在用户手上）**：

- **B 的生产写**：`UPDATE data_source SET is_active=false WHERE id=2` —— 探针已证零影响（§4.2.4），但**须用户明示批准后由用户在生产执行**，任何会话不代执行；
- **§7 墓碑 270** —— ⛔ **不得执行**（§六的三条前置未满足）；
- **PR #162 是否合并的最终决定权**在用户；
- 其余 **5 条只读探针**（§7.2 第 1、2、4、5、6 项）。

> ⚠️ **本次全程只读复核，未做任何代码改动、未做任何生产数据写入。**
> 第 8、11 项是代码改动，第 3 项与第 10 项是生产写 ⇒ 都需要单独评审；第 1、2、4、5、6、7、9 项均可只读完成。
> **原「Oracle 侧 `all_tables` 定死类处置分支 A/B/C」一项已作废** —— 三个类是 0 行结构性占位类，
> 被剔除是正确行为，不需要补 introspect、不需要打墓碑；Oracle 侧事实只用于**采购域 M1 的语义衔接**。
