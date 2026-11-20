# 1120 召回根治 —— 生产上线验收记录

- **日期**：2026-11-20
- **批次**：`2026-11-20-recall-fix-release`
- **Runbook**：[deploy-prod.md](./deploy-prod.md)（部署 SSOT）· **交付汇总**：[summary.md](./summary.md)
- **验收人**：Claude（只读复核）+ 用户（生产命令执行）
- **结论**：**✅ 已上线，根治在运行时实证生效；不需要回滚**。遗留 1 个「死类」问题 + 1 条未根治路径（ds2），均**不影响回滚决策**，需单独处置。

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

## 三、★★ 新发现：三个「所有源下都召不回」的死类

```
DWD_SUPPLIER
DWD_MATERIAL
DWD_INCOMING_INSPECTION
```

**事实**：这三个是 `valid_to IS NULL` 的**活**类，其 `source_table` **不在任何数据源的 `schema_cache` 里**
⇒ 按 `filterClassesByDatasource`（`source_table` 非空且不在 `tableSet` ⇒ 无条件剔除）
⇒ **选中 ds1 被剔、选中 ds3 也被剔** ⇒ **在生产没有任何数据源能召回到它们**。

**直接实证**：ds1 会话的 `droppedTables` **前 3 位就是它们**（§2.1 原文）。

**危害**：不是报错，是**静默不可达**。用户无论切哪个源，问到这 3 张表的问句都答不出，且没有任何提示。
若这 3 张表**真实存在于 Oracle**（只是没 introspect），则 `DWD_SUPPLIER` 这条「供应商台账」路径在**所有源下都是断的**。

**根因判定**：runbook §2 已预警「类已登记、owner 未 introspect」这个**代码不可解**风险；
这 3 个名字的 `DWD_` 命名风格与 ds1 已 introspect 的同层表（`DWD_BOM` / `DWD_AP_INVOICE_DTL` /
`DWD_SUPPLIER_PAYMENT` / `DWD_BUSINESS_PARTNER` …）一致 ⇒ **极可能是 Oracle 侧真实存在、只是该 owner 没 introspect**。

**处置分支（只差一条 Oracle 侧事实；本次**未做任何改动**）**：

```sql
-- 在 Oracle 侧用只读账号执行（不涉及本系统）
SELECT owner, table_name FROM all_tables
WHERE table_name IN ('DWD_SUPPLIER','DWD_MATERIAL','DWD_INCOMING_INSPECTION');
```

| 分支 | 事实 | 处置 | 性质 |
|---|---|---|---|
| **A** | 查得到 | 补 ds1 对应 owner 的 introspect（把表写进 `schema_cache`） | **生产数据写操作 ⇒ 需单独评审 + 逐条 SQL 过目** |
| **B** | 查不到 | 这 3 个类打墓碑（`valid_to` 落时间），让"静默不可达"变成"明确作废" | 生产 DML ⇒ 需单独评审 |
| **C** | 表其实在 ds3、`source_table` 登记错 | 走 M1 绑定语义改绑 | **禁止裸改 `source_table`** |

**待补的影响面**：这三个类被哪些 `ontology_metric` / `kpi_catalog` 引用（若被引用，影响不止"答不出"，
还可能是"口径错乱"）。只读探针（未执行）：

```sql
SELECT jsonb_pretty(to_jsonb(oc)) AS row FROM ontology_class oc
WHERE upper(coalesce(oc.source_table, ''))
  IN ('DWD_SUPPLIER','DWD_MATERIAL','DWD_INCOMING_INSPECTION') ORDER BY oc.id;

SELECT table_name, string_agg(column_name, ', ' ORDER BY ordinal_position) AS cols
FROM information_schema.columns
WHERE table_name IN ('ontology_class','ontology_metric','ontology_metric_source','kpi_catalog')
GROUP BY table_name ORDER BY table_name;
```

---

## 四、遗留缺口：ds2（`SEED_DS_KPI_CATALOG`）路径**未根治**

`schema_cache` 里 **ds2 一行都没有** ⇒ 选中 ds2 时 `loadDatasourceTableSet` 返回 `None`
⇒ **fail-open ⇒ 完全不过滤** ⇒ 跨源幽灵表在 ds2 会话**原样回来**（1119 的原始故障路径没关掉）。

- runbook §2 A1 判读表原文：**「A1 空 ⇒ 该数据源不得上线 ⇒ 先补 introspect」**。
- **但需区分两种可能**：ds2 是 KPI 目录种子源（`postgresql` 类型），可能**本来就没有可 introspect 的业务表**
  ⇒ 它的 fail-open 是设计上不可避免（要么它不用于跑 SQL，要么它该 fail-closed）。
- 近 30 分钟 `schema_cache 无可用表清单` = 0 ⇒ **暂未有人用它**，无即时影响。
- **这不是本次发版引入的回归**（filter 上线前也无过滤），但"根治"声明对 ds2 不成立 ⇒ **需有人拍板策略**。

---

## 五、★ 与 runbook 的偏差（两处，都会误导下次执行）

### 5.1 runbook §2 的三条 SQL **本身是坏的**（实测报错）

| 原写法 | 实测错误 | 根因 | 正确写法 |
|---|---|---|---|
| `jsonb_array_length(sc.schema_data)` 直接进 `GROUP BY` 查询 | `column "sc.schema_data" must appear in the GROUP BY clause` | `jsonb_array_length()` **不是聚合函数** | 必须 `sum(jsonb_array_length(...))` 包裹 |
| `upper(t ->> 'table_name' \|\| '.' \|\| t ->> 'owner')` | `operator does not exist: text ->> unknown` | `->>` 与 `\|\|` 优先级/类型歧义 | `upper(concat_ws('.', t ->> 'table_name', nullif(t ->> 'owner', '')))` |
| `LIKE 'DIM\_%'` | `invalid command \_%'` | psql 把反斜杠当元命令 | `starts_with(t, 'DIM_')` |

**额外坑（不在 runbook 里）**：`docker exec X sh -c 'psql -c "...SQL..."'` 的**外层单引号会吃掉 SQL 里所有单引号**
（不只 `''`）⇒ 必用 heredoc 走 stdin：`docker exec -i X sh -c 'psql …' <<'SQL' … SQL`（或 `psql -f`）。

### 5.2 runbook §2 的 A2 判据**只查单源**，会漏掉「跨源死类」

A2 的口径是「本源的活类在本源缓存里找不到」⇒ 只能发现「本源覆盖度不足」，
**发现不了「在所有源下都找不到」的死类**（本次的 3 个就是被跨源口径的探针发现的）。
⇒ **建议补一条全源孤儿探针**（本次实际使用的口径）：

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

### 5.3 其他运维坑（本次踩到）

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
| **§2 前置门禁** | ✘ **上线后才执行，且 A2 非空** ⇒ runbook 口径「该数据源不得上线」**未通过**；实际后果是**漏召而非崩溃** |
| **三个死类** | ★ **新发现**（§三）—— 待 Oracle 侧事实定分支 |
| ds2 路径 | ✘ fail-open 未根治（§四）—— 待拍板策略 |
| §7 墓碑 270 | ⛔ 未执行，判定保留（§六） |
| **是否需要回滚** | ❌ **不需要** —— 零 ORA-00942、零崩溃、零数据损坏；缺口是"漏召"，用补 introspect / 打墓碑 / 改 ds2 策略补，**不需要动镜像** |

### 7.2 待办清单（按优先级）

| # | 事项 | 性质 | 阻塞 |
|---|---|---|---|
| 1 | Oracle 侧 `all_tables` 查 3 张表是否存在 → 定分支 A/B/C | 只读 | 死类处置 |
| 2 | 查 3 个类被哪些 `ontology_metric` / `kpi_catalog` 引用 | 只读 | 影响面评估 |
| 3 | ds2 策略拍板（补 introspect / 移出可选源 / fail-closed） | 决策 | ds2 路径根治 |
| 4 | 补 ds1 / ds3 各一次的**披露提示原文与 N 值** | 只读 | §6.3 闭环 |
| 5 | 研究入口回归（C1 ≈ 200+ > 0 ⇒ runbook 明写必做） | 只读 | §6.3 闭环 |
| 6 | 泛化问句回归（不含维度词，验证窗口未收得过窄） | 只读 | §6.3 闭环 |
| 7 | runbook §2 三条 SQL 修正 + 补全源孤儿探针 | 文档 | 下次发版 |
| 8 | `anchor_rescue_*` 的日志级别（info → warning） | **代码** | 观测盲区（本批不阻塞） |
| 9 | §7 墓碑 270 原地复活 | **生产 DML** | 待 1/4/5/6 全绿 |

> ⚠️ **本次全程只读复核，未做任何代码改动、未做任何生产数据写入。**
> 第 8 项是代码改动，第 9 项是生产 DML ⇒ 都需要单独评审；第 1–7 项除必要的 DML 外均可只读完成。
