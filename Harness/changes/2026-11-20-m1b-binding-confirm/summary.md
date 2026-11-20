# 变更：M1-b 绑定确认批（采购域 9 头类绑定 候选→确认 + 域首发布 + 回滚演练）

- 日期：2026-11-20　分支：`feat/m1b-binding-confirm`（off `feat/procurement-m0` @ `762116c4`）
- 载体：`backend/scripts/m1b_confirm_procurement_bindings.py`（service 层直连 dev 5433/qa_metadata，
  幂等 + `--dry-run` + `--drill` + 库名护栏）

## 解决什么
M1-b 确认闸第 8 条「每条绑定已确认（非候选）」：dev 库绑定表原为 0 行 ⇒ 采购域发布是
"名词卡片"（D-3：候选不得被读路径消费 ⇒ NL2SQL 无权威概念→表/列映射）。

## 前置探针（同日，dev 库只读）
9 个采购头类 `source_table` 在 **ds1（THBI Oracle）9/9 全保留**（283 万键 cache）；
ds10 SRM-TH-01 全剔=正确行为；ds9 无 cache fail-open（低风险）。⇒ 1119 后
「采购类可能看不见」的挂账**解除**，确认对象真实可达。

## 执行记录（全部 service 层，审计留痕，actor=m1b-confirm）
1. **JOIN 补边 ×4**（幂等）：QUOT header→报价行（PRICE_LIST_CODE）、采购发票/付款→
   DIM_SUPPLIER（SUPPLIER_CODE→BPSNUM_0）、请购行→DIM_IMATERIAL（MATERIAL_CODE→ITMREF_0）。
   ⚠️ 根因：`_compileJoins` **丢弃任一端已墓碑的边** ⇒ 这 4 个类的既有边全连死类
   （DWD_ITEM_FACILITY/DWD_QUOTATION*）⇒ `SOURCE_BINDING_JOIN_MISSING`（ERROR）阻断发布
   ——闸门 fail-closed 按设计工作。
2. **9 源绑定 + 属性绑定全确认**（确认=版本冻结点，序：属性先、源后）：id 1–9；
   属性绑定 11–248 个/类（`ontology_property.source_column` 非空全绑）；键序号取
   `is_primary_key`（权威来源，非猜测）；bindingRole=**BASE**（实仓枚举
   BASE/DENOMINATOR/FILTER/NUMERATOR，设计稿 C.1 的 PRIMARY 已被实现取舍替代）。
3. **域首发布** `v-m1b-confirm-1-2` ACTIVE：manifest **classes=17 / metrics=9 /
   sourceBindings=9**；ERROR 级校验全 PASS（JOIN_MISSING ×9 PASS / MANIFEST_SCHEMA /
   SNAPSHOT_INTEGRITY）；SOURCE_BINDING_CANDIDATE PASS（候选清零）；WARNING 治理债：
   METRIC_*_LANDING FAIL ×7 + DOMAIN_SLICE_CROSS_DOMAIN ×1（不阻断，登记）。
   ⚠️ v-m1b-confirm-1 被首次失败的陈旧 DRAFT 占用（id 16，保留为预览）。
4. **回滚演练 ✅**（M1-a 结构闸第 4 条补账）：副本式回滚（D-11，副本出生即 ACTIVE）
   → manifest_hash 一致 `5f2c5e275bd33e2d…` 记录在案。

## 幂等复跑
全 skip（join ×4 / 绑定 ×9 / release 跳过），零写入。

## 已知限制（按 M1-b 第 8 条登记）
- `filter_expression` 未设（软删除/有效期口径待业务答案 ⇒ C 问卷证据在 G1 摄入包）
- METRIC_*_LANDING WARNING 治理债 ×7（分母落地检查，不阻断）
- 陈旧 DRAFT id16 保留（发布预览按设计留痕）
