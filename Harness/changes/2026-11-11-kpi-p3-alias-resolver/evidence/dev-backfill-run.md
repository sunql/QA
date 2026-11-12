# D5 dev 写库执行证据（2026-11-12，用户已批准「两项都执行」）

## 操作 ① alembic upgrade head（5433 qa_metadata）
- `Running upgrade 0142 -> 0143, KPI 时间粒度：kpi_catalog.time_grain + ontology_metric.time_grain / time_column。`
- EXIT=0；`alembic_version` 复核 = **0143**

## 操作 ② 回填 `--apply --allow-prod-write`
- 结果：**summary: changes=11 noop=1 total=12**（与 dry-run 预期完全一致）
- 11 条 KPI：`time_grain=month` 写入 kpi_catalog + ontology_metric 两侧
- metric=5（KPI_INVOICE_MATCH_RATE）：额外写入 `time_column=INVOICE_DATE`（grain_keys 唯一日期 token）
- metric=2/3/7/11：time_column ambiguous（PROMISED_RECEIPT_DATE vs RECEIPT_DATE）⇒ 不写，只报告（R3 裁决）
- 其余：no-candidates ⇒ 不写
- metric=10（KPI_SUPPLIER_MAINTENANCE_RATIO）：grain「供应商（快照）」无时间词 ⇒ 全部 noop
- 守卫四道闸全程生效（--allow-prod-write 显式放行 + 只填 null + source_column 白名单 + knownColumns 门）

## 幂等验证
- apply 后复跑（report-only 模式）：**summary: changes=0 noop=12 total=12** ✅

## 佐证文件
- `dev-backfill-before.txt`：写前快照（12 kpi_catalog.grain + 12 ontology_metric）
- `dev-backfill-after.txt`：写后快照（time_grain / time_column 实际落库值）
- `backfill-report.txt`：写前 dry-run 报告（评审证据，与本次 apply 计划一致）
