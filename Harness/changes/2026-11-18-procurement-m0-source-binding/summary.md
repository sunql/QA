# 变更：采购平台 V0.3 · M0.5 收尾 —— 源绑定候选与两张表结构设计（A8）

- **日期**：2026-11-18
- **作者**：Claude
- **Phase**：M0 底座 / W2 轨 A（A8，轨 A 末项）
- **状态**：done（**已合入 main**，分批合入 1/4；**生产未部署**）
- **关联变更**：[2026-11-18-procurement-m0-slot-template](../2026-11-18-procurement-m0-slot-template/summary.md)（A7）、[2026-11-18-procurement-m0-kpi-attribution](../2026-11-18-procurement-m0-kpi-attribution/summary.md)（A6）
- **迁移版本**：A8 本批**无 DDL**（head 当时 `0153`）；后续 **M1-1.1 已按本批 §4 建表** =
  **`0154_procurement_bindings`**（两张表同一迁移；A4-0 顺延到下一个空号）
- **验收包**：[docs/update/1118/M0/A8-采购源绑定候选与表结构设计.md](../../../docs/update/1118/M0/A8-采购源绑定候选与表结构设计.md)
- **候选数据**：[docs/update/1118/M0/procurement-source-binding-candidates.json](../../../docs/update/1118/M0/procurement-source-binding-candidates.json)
- **证据**：[evidence/procurement-binding-resolve.txt](../../../docs/update/1118/M0/evidence/procurement-binding-resolve.txt)
- **计划**：[并行轨与汇合点 §3 轨 A · A8](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_并行轨与汇合点_1118.md)

> 用户已拍板：**生产暂不部署**；**功能需要全部完成**；**本地全绿后统一部署**。
> 决策 **D-3**：未确认的候选**不得被生产读路径消费**。
> 决策 **D2**：源绑定用**两张表**，复合键 / 有效期 / 过滤白名单走**独立列**，不进 JSON。

---

## 1. 需求

计划 A8 行：产出「34 个采购类的**候选**绑定（带「待确认」标记）+ `procurement_source_binding` 表结构设计」，
验收「候选可被 C 快速核对；**未确认的不落生产**」。

隐含的三个前提必须在动工前先证伪或证实（本批全部机械核过）：

1. **「34 个采购类」这个数字能不能复现**（否则整个清单没根）；
2. **解析要不要连外部库**（THBI / SRM 在开发机上均不可达）；
3. **候选与生产读路径如何物理隔离**（不能只靠文档约定）。

---

## 2. 设计评审

### 2.1 「34」是可复现的，但**边界**需要确认

```text
有效类 59 = DWD_* 14 + ODS_* 20 (= 34，与计划吻合) + DIM_* 3 + bill_* 16 + 其它 6
```

数字对上了，但 `DIM_SUPPLIER` / `DIM_FACILITY` / `DIM_IMATERIAL` 这 3 个**最核心的主数据**
不在 34 里，而 4 条 KPI 的粒度键恰好用它们的编码。⇒ 登记为 **Q1**（39 = 34 + DIM 3 + 其它），
不擅自扩范围。

### 2.2 不连外部库也能解析 —— 用 `schema_cache`

`schema_cache` 缓存了 datasource 1 / 2 / 10 的完整表结构（1805 / 52 / 217 表），
`ontology_class.source_table` 与它做表名匹配即可。代价是缓存可能滞后 ⇒ 结论一律 `candidate`。

结果：**34 条全部命中（not-found = 0）**，28 unique + 6 schema-ambiguous。
6 条歧义全部是 `THBI` / `ZJTH` 同名 ODS 表；实测 **THBI ⊇ ZJTH**（ZJTH 独有列恒为 0，
5 张差 1 列 `ETL_LOAD_TS`、`ODS_BPARTNER` 差 2 列）⇒ 候选取 THBI，权威层由 C 确认（**Q2**）。

### 2.3 「待确认」必须是库级状态，不是文档措辞

设计里 `is_candidate BOOLEAN NOT NULL DEFAULT TRUE` + `CHECK (NOT is_candidate OR confirmed_by IS NULL)`：
未确认的绑定在**数据层**就是候选，manifest 构建侧只读 `is_candidate = FALSE` 的行。
文档里的「候选」措辞只是对它的描述，不是隔离机制本身（D-3 落地）。

### 2.4 三个施工前必须知道的结构事实

| 事实 | 后果 |
|---|---|
| **7 个墓碑类仍被 8 条 KPI 引用**（15 个「类×指标」对，2026-09-19 软删） | 绑定前**必须先复活**；否则 M1-b「≥8 个采购类」连不上 |
| `DWD_PURCHASE_ORDER_LINE`(29 列) vs `_DTL`(35 列) **只共享 5 列** | 「墓碑=被 `_DTL` 取代」不成立，不能改绑 |
| `DWD_SUPPLIER_PRICE_LIST` 一表挂两类（id 55 / 29） | 唯一键**必须含 `class_id`**，不能按表名定位类 |
| 复合主键是常态（4 列 / 3 列），`DIM_SUPPLIER` / `ODS_PORDER` **无主键** | 复合键位必须真列（D2）；ODS 去重只能靠业务键 |

---

## 3. 数据模型变更

**本批不建表。** 交付两张表的**列级设计**，供 M1-1.1 直接落地：

`procurement_source_binding`：
`class_id` / `datasource_id` / `schema_name` / `source_table` / `binding_role`（复用 `MetricSourceRole`）/
`filter_expression` / `filter_allowlist TEXT[]` / `is_candidate` / `confirmed_by` / `confirmed_time` /
`valid_from` / `valid_to`。
约束：`UNIQUE(class_id, datasource_id, schema_name, source_table, binding_role) WHERE valid_to IS NULL`；
`CHECK (NOT is_candidate OR confirmed_by IS NULL)`。

`procurement_property_binding`：
`source_binding_id` / `property_id` / `source_column` / `key_ordinal`（复合键序号）/
`transform_expression` / 候选与有效期同上层。
约束：同绑定内「属性唯一」「键序号唯一」各一条部分唯一索引。

> 新名字**未**与既有 `agent_orchestration_binding` / `class_domain_mapping` / `entity_mapping` / `id_mapping`
> 重复（已逐一核对语义）；D1「复用 `semantic_release` / `workflow_*`」针对的是**发布与审批面**，不是源绑定，
> 两者不冲突（源绑定是新域，无既有表可复用）。

---

## 4. 接口契约变更

| 契约 | 变更 | 处置 |
|---|---|---|
| `procurement-source-binding-candidates.json` | 新增（34 条，`status` 全为 `candidate`） | 机器可校验；`counts` 不符即视为过期 |
| `evidence/procurement-binding-resolve.txt` | 新增 A–F 六段（解析 / 列缺口 / 墓碑引用 / 同表双类 / 未建模表 / THBI-vs-ZJTH） | 只读生成 |
| 代码 / 迁移 / API | **无变更** | — |

---

## 5. 实现要点

| 文件 | 职责 |
|---|---|
| `A8-采购源绑定候选与表结构设计.md` | 34 的来源、解析方法、3 个结构问题、两张表 DDL 级设计、6 项待确认、45 张未建模表分层、复现命令 |
| `procurement-source-binding-candidates.json` | 34 条候选（含 3 条定制 notes：同表双类 / 列缺口 / `DWD_AP_INVOICE_DTL` 不可替代墓碑类） |
| `evidence/procurement-binding-resolve.txt` | A 34 行逐条 / B 6 个列缺口 / C 7 个墓碑类 / D 同表双类 / E 45 张未建模表 / F THBI vs ZJTH 列差 |
| `M0/README.md` | 0.5 行状态、目录树、产能对照修正 |
| `Harness/agents/owner.md` | 热点行 A1–A8 |

关键判断：

- **墓碑类复活是 A8 的一等产出**，不是"顺带发现"：它决定 M1-b 能不能开工，且复活是**治理写操作**，须与 A9 首次发布同批。
- **`procurement_property_binding` 必需**：`ontology_property.source_column` 是单源假设，表达不了「同属性多源 / 列级变换 / 复合键位」，四列主键就是反例。
- **产能对照如实修正**：A1–A7 估工 10.5–16.5 人日，加 A8（3–5）后上界 21.5 已溢出回收窗口 12–15 人日；
  A8 的建表部分本就在 M1-1.1（8–12 人日），不重复计，溢出由 W1–W3 的「M1 建表启动」吸收。

---

## 6. 测试

本批零代码、零 DDL、零迁移，无新增单测。**可复现性由证据文件承担**：

```text
candidates.json     bindings=34 / unique=28 / schemaAmbiguous=6 / notFound=0 / status 全 candidate
classId 唯一性      34 条 → 34 个不同 classId（无重复绑定）
证据 A–F           六段 SQL 结果原样落盘，文件头注明库与只读方式
```

> 不跑 `./scripts/run_local_tests.sh`：无代码改动；且按铁律**绝不裸跑 pytest**（会动线上 Milvus/Neo4j）。
> M1-1.1 建表时会按「真实 PG + 完整 API 链路」补齐单测与集成测试。

---

## 7. 安全审查

- **纯只读**：所有 SQL 均为 `SELECT`，走本地容器，不经网络，不写任何行。
- **无口令泄漏**：证据文件不含 DSN / 口令；未连外部库（THBI / SRM）。
- **D-3 合规**：34 条全部 `status: "candidate"`；表设计以 `is_candidate` 做**库级**隔离。
- **无 PII**：只涉及表名 / 列名 / 计数，无业务行数据。

---

## 8. 部署验证

**未部署**（按用户决策）。无 DDL，本地无需重建库；head 不变（`0153`）。

真实数据验证：34 条来自本地开发库 `qa_metadata`（5433）实查，`schema_cache.updated_time` 为 2026-11-14 / 11-16。

---

## 9. 关联

- A8 验收包：[A8-采购源绑定候选与表结构设计.md](../../../docs/update/1118/M0/A8-采购源绑定候选与表结构设计.md)
- 上游：A6 归因 / A7 槽位模板
- 下一页：**M1-1.1 建表**（迁移 `0154`，已落地）+ **A4-0**（下一个空号）+ **A9** 首次发布（复活 7 个墓碑类与 alias `--apply` 同批）
