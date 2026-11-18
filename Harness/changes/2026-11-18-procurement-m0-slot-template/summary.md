# 变更：采购平台 V0.3 · M0.5 后半 —— KPI 口径落库槽位模板（A7）

- **日期**：2026-11-18
- **作者**：Claude
- **Phase**：M0 底座 / W2 轨 A（A7）
- **状态**：in-review（分支 `feat/procurement-m0`，**未合并、未部署**）
- **关联变更**：[2026-11-18-procurement-m0-kpi-attribution](../2026-11-18-procurement-m0-kpi-attribution/summary.md)（A6，本批的输入）
- **迁移版本**：**无 DDL / 无数据迁移 / 无代码改动**（head 仍 `0153`，`0154` 留给 A4-0）
- **验收包**：[docs/update/1118/M0/M0.5-KPI口径槽位模板.md](../../../docs/update/1118/M0/M0.5-KPI口径槽位模板.md)
- **证据**：[evidence/kpi-slot-snapshot.txt](../../../docs/update/1118/M0/evidence/kpi-slot-snapshot.txt)、[evidence/kpi-time-column-candidates.txt](../../../docs/update/1118/M0/evidence/kpi-time-column-candidates.txt)
- **计划**：[并行轨与汇合点 §3 轨 A · A7](../../../docs/update/1118/智能采购平台_V0.3落地实施计划_并行轨与汇合点_1118.md)

> 用户已拍板：**生产暂不部署**；**功能需要全部完成**；**本地全绿后统一部署**。
> 决策 **D-3**：未确认的候选**不得被生产读路径消费**。

---

## 1. 需求

计划 A7 行的验收是「**回收后只需填值，不需再想『落到哪个字段』**」。这要求两样东西：

1. `ontology_metric` 的**槽位表**（12 条 KPI × 字段矩阵，空槽必须看得见）；
2. **问卷题号 ↔ 字段的映射**（一个答案进哪一列、一列收哪些题）。

隐含第三条（本批新增）：**映射不全必须暴露**。如果某些答案无处可落而文档不说，
「填值」阶段就会变成「临时拍脑袋建字段」，这比不填更糟。

---

## 2. 设计评审

### 2.1 13 个字段是"构成一个可发布口径"的全部列

从 `ontology_metric` 的 ORM + `kpi_metric_publish_gate` 的闸门项反推：
`metric_type` / `numerator_measure` / `denominator_measure` / `agg_function` /
`aggregation_order` / `grain_keys` / `filter_expression` / `time_grain` / `time_column` /
`execution_source` / `formula` / `status` / `metric_alias`。

排除项各有理由：`metric_name` 是标识、`id` / `created_by` / `target_class_id` /
`dimension_defaults` 不构成口径、**源类绑定在 `ontology_metric_source` 属 A8**。

每个字段都标注了「被哪道闸门要求」——闸门项来自
`buildKpiGateItems` 的 5 项（`metric_linked` / `metric_published` / `execution_source` /
`reconcile` / `datasource`）与 `_baseMetricItems` 的 4 项（`metric_type` / `numerator` /
`execution_source` / `grain`）+ 比率型的 `denominator` / `aggregation_order`。

### 2.2 空槽用五种状态，不用「有/无」

DB 里 `status` 全是 `DRAFT`、`metric_alias` 全等于 `kpi_code`、4 条 `NONE` 的 `formula`
有 95–131 个字符——**列非空 ≠ 口径已定**。所以状态分 `● ◐ ○ ⛔ ⊘`：
占位值标 `◐`，中文说明标 `◐`，被阻塞标 `⛔`，`MEASURE` 的分母标 `⊘`。

### 2.3 `time_column` 用脚本候选，不靠人猜

`scripts/kpi_backfill_time_grain.py` 的派生规则就是「多候选/零候选不猜」（R3）。
本批直接复用它的**候选清单**作为槽位提示：11 条里 8 条是 ambiguous、3 条 no-candidates，
只有 `KPI_INVOICE_MATCH_RATE` 唯一（`INVOICE_DATE`，且已在 `grain_keys` 里）。

### 2.4 两个"同名不同物"必须写进模板

- `ontology_metric.status`（技术形态 DRAFT/PUBLISHED）≠ `kpi_catalog.status`（治理状态）；
  「NCR 摘除首期」改的是后者，前者保持 `DRAFT`。
- `measure.filter`（度量级，如"只算逾期行"）≠ `filter_expression`（整条指标的 `WHERE`）；
  判错会一边算多一边算少。

---

## 3. 数据模型变更

**无。** 本批只产出文档与两份机器生成的证据快照，不改任何表、不写任何行。

---

## 4. 接口契约变更

| 契约 | 变更 | 处置 |
|---|---|---|
| `ontology_metric` 13 字段 | **不改**，本批首次把它们作为「口径槽位」系统化定义（值域 + 必填条件 + 闸门依赖） | 文档化 |
| `evidence/kpi-slot-snapshot.txt` | 新增（12 条 × 13 字段实查快照，SQL 内嵌在文件头） | 只读生成 |
| `evidence/kpi-time-column-candidates.txt` | 新增（现有脚本输出，`ambiguous` / `no-candidates` 结论） | 只读生成 |
| 代码 / 迁移 / API | **无变更** | — |

---

## 5. 实现要点

| 文件 | 职责 |
|---|---|
| `docs/update/1118/M0/M0.5-KPI口径槽位模板.md` | 13 槽位定义（值域 / 必填 / 谁写 / 闸门） + 12×13 空槽矩阵 + 逐条待填清单（含答案候选值）+ 题号↔字段映射 + 5 类能力边界 + 填槽顺序 |
| `docs/update/1118/M0/evidence/kpi-slot-snapshot.txt` | `ontology_metric` × 12 条实查（`sort` 后稳定） |
| `docs/update/1118/M0/evidence/kpi-time-column-candidates.txt` | `kpi_backfill_time_grain.py` dry-run 全量输出 |
| `docs/update/1118/M0/README.md` | 0.5 行状态与目录树更新 |
| `Harness/changes/2026-11-18-procurement-m0-slot-template/` | 本记录 |

关键判断：

- **「答案无落点」被当作一等产出**：§5.2 列出 13 项装不进槽位的答案（时间窗边界、
  指标间关系、加权权重、中位数、展示规则、制度规则、术语、试点范围），
  再归并为 §6 的 5 类能力边界并指明归属（M2′ / M4 / M6 / 知识层）。
- **能力边界里只有 2 项真的要改模型**（`COMPILED` 轨道的加权权重与 `MEDIAN`），
  而当前 12 条**全是 `ESCAPE_HATCH`**，没有一条走 `COMPILED` ⇒ 不阻塞 A9。
- **「列非空」不当作已填**：`status` / `metric_alias` 全库都是占位，矩阵里标 `◐`。
- **回问项集中登记**：A4.1↔A4.2（提前到货是否计入）、A4.3（未到货算不算）、
  A8.4（付款粒度）、A10.2（分子批/分母行单位不一致）。

---

## 6. 测试

本批无代码改动，无新增单测。**可复现性由证据文件承担**：

```text
槽位快照     12 条 × 13 字段实查（文件头含完整 SQL，可直接复跑）
time_column  dry-run 12/12：1 条 basis=grain_keys、8 条 ambiguous、3 条 no-candidates
A6 探针      pass 8 / unknown 4 / mismatch 0（本批未改变任何库内事实）
```

> 本批不运行 `./scripts/run_local_tests.sh`：零代码改动，定向测试无新增覆盖对象。
> 若评审要求，A6 的 38 条（unit 35 + integration 3）仍可复跑证明库面未漂移。

---

## 7. 安全审查

- **纯只读**：两条 SQL 均为 `SELECT`，无写入；`psql` 走本地容器，不经网络。
- **无口令泄漏**：证据文件不含 DSN（`kpi_backfill_time_grain.py` 自带 `maskDsn`，输出为 `qa_user:***@localhost:5433`）。
- **无外部输入面**：文档与证据都是本地生成。
- **D-3 合规**：本文件里所有「候选值」都带 `待确认` 语义，未被任何读路径消费。

---

## 8. 部署验证

**未部署**（按用户决策）。无 DDL，本地无需重建库。

真实数据验证：12 条来自本地开发库 `kpi_catalog ⨝ ontology_metric` 实查；
`time_column` 候选来自 `sourcePropertiesForMetric`（与 chat 运行时同源）。

---

## 9. 关联

- 槽位模板：[M0.5-KPI口径槽位模板.md](../../../docs/update/1118/M0/M0.5-KPI口径槽位模板.md)
- 上游：A6 归因清单（[M0.5-KPI归因清单.md](../../../docs/update/1118/M0/M0.5-KPI归因清单.md)）
- 下一页：
  - **A8** 源绑定候选（质量域建类；合同价/价目表价改「历史均价」；`DIM_SUPPLIER` 停用列指认）；
  - **A9** 采购域首次发布（alias `--apply` 与重发布同批）；
  - **G1 汇合**：按本文件 §7 的顺序填槽。
