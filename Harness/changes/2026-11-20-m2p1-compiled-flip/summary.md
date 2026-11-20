# 变更：M2′-1 KPI 指标 execution_source ESCAPE_HATCH→COMPILED（数据批）

- 日期：2026-11-20　分支：`feat/m2p1-compiled-flip`（off `feat/procurement-m0`）
- 载体：`backend/scripts/m2p1_flip_metric_execution_source.py`
  （显式 DSN + 库名白名单/回环 host 护栏 + 编译前置门 + 层 3 对账 + 幂等 + dry-run）

## 结果（dev 5433/qa_metadata 实跑，runtime 同路径验证）

**3 条翻转成功**（execution_source 现为 COMPILED，Tier A eligible 实证）：
- `KPI_SUPPLIER_OTD`（id 2）供应商准时交付率
- `KPI_SUPPLIER_OVERDUE_RATIO`（id 3）供应商逾期率
- `KPI_PAYMENT_ON_TIME`（id 4）付款及时率

**5 条保留 ESCAPE_HATCH = 编译器能力边界（非数据缺口）**：
| 指标 | fail-closed 码 | 性质 |
|---|---|---|
| COST_SAVING / DELAY_DAYS / CYCLE_TIME / PRICE_VARIANCE | `compiled_measure_class_unresolved` | 分子/分母表达式**跨表**（R./O./P. 前缀横 multi-class，BASE 角色 2–3 个类 ⇒ 解析歧义）；即使绑死类，跨表表达式也会撞 `COMPILED_EXPR_CROSS_TABLE_UNSUPPORTED` 预留码 ⇒ **编译器单类表达式模型不支持**，扩列另批 |
| INVOICE_MATCH_RATE | `compiled_filter_source_unsupported` | FILTER 角色源（三单匹配过滤）为编译器预留能力 |

**4 条 NONE**（DEFECT_RATE/FPY/MAINTENANCE_RATIO/NCR_RATE）= M2′-2，卡问卷
A9–A12 技术形态（问卷已回收，待 G1 摄入答案映射后补）——本批不动。

## 门禁与验证
- 层 3 对账（formula vs 结构化列）全部无 MISMATCH ⇒ 翻转前最后一次文本比对完成
  （翻成 COMPILED 后此检查不再适用——设计如此）；
- 翻转后 Tier A probe 复跑：3 条 `source=COMPILED tier=A eligible=True`
  （**运行时同路径** ⇒ chat 链路将走编译器产出的 SQL）；
- 域重发布 `v-m2p1-compiled-1` ACTIVE（id 19，manifest metrics=12）；
  幂等复跑：零翻转 ⇒ 跳过发布，no-op。

## 纪律说明
「8 条→COMPILED」是计划估算；实况以**前置门数据裁决** = 3 翻 + 5 登记能力边界
——不盲翻是本批的核心纪律（翻了 COMPILED 运行时即走编译器，编译不出来=线上降级）。
