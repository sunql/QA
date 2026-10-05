-- 回滚脚本：撤销 2026-10-03 对 DIM_FACILITY 关联/外键元数据的修改
--
-- 撤销内容：
--   A. 删除扇出边 83、84（8.7~8.9× 行数放大）
--   B. 撤销属性 702（DWD_GOODS_RECEIPT_DTL.COM_CODE）的 FK 标记
--
-- 执行前提：**先读这份文件再执行**，确认当前状态与下面「撤销前」一致。
-- 若期间有人改过这两条边/这个属性，直接跑本脚本会覆盖掉别人的改动。
--
-- 用法（prod 库，手工执行；本脚本不会自动跑）：
--   psql -U qa_user -d qa_metadata -f rollback-metadata.sql
--
-- ⚠️ 直接写 PG **不会**同步 Neo4j / Milvus。若曾通过 API 建过边，
--    回滚应优先走 API（DELETE /ontology/joins/{id}），以便审计 + 图同步。
--    本脚本仅供「API 不可用 / 需要精确还原字段」时兜底。

\set ON_ERROR_STOP on

BEGIN;

-- ── 撤销前状态（2026-10-03 核对）────────────────────────────────────────
--   ontology_join 全库 85 条，其中触及 class_id=1 的 4 条：
--     82  DIM_FACILITY.FCY_0    → 19 DWD_PURCHASE_ORDER_DTL.PUR_SITE_CODE   1.00×  保留
--     83  DIM_FACILITY.LEGCPY_0 → 19 DWD_PURCHASE_ORDER_DTL.COM_CODE      8.72×  删除
--     84  DIM_FACILITY.LEGCPY_0 → 13 DWD_GOODS_RECEIPT_DTL.COM_CODE       8.87×  删除
--     85  DIM_FACILITY.FCY_0    → 13 DWD_GOODS_RECEIPT_DTL.RCV_SITE_CODE   1.00×  保留
--
--   ontology_property 702 = DWD_GOODS_RECEIPT_DTL.COM_CODE
--     撤销前： is_foreign_key = true, ref_class_id = 1
--     撤销后： is_foreign_key = false, ref_class_id = NULL

-- ── A. 还原两条扇出边 ──────────────────────────────────────────────────
-- 字段值取自删除前的完整行（含 join_key 与时间戳），保证能精确还原。
-- 注意 id 显式指定：若该序号已被别的行占用，本 INSERT 会因主键冲突报错
-- （这是刻意的 —— 宁可失败也不要静默插到别的位置）。

INSERT INTO ontology_join (
    id, source_class_id, source_columns, target_class_id, target_columns,
    join_type, relation_type, description, join_key, created_by,
    created_time, updated_time
) VALUES (
    83, 1, '["LEGCPY_0"]'::jsonb, 19, '["COM_CODE"]'::jsonb,
    'INNER', 'business',
    '采购订单的公司编码和公司及工厂信息的公司代码是关联关系',
    '1|LEGCPY_0->19|COM_CODE', NULL,
    '2026-10-03 08:59:50.496571+00', '2026-10-03 08:59:50.496573+00'
);

INSERT INTO ontology_join (
    id, source_class_id, source_columns, target_class_id, target_columns,
    join_type, relation_type, description, join_key, created_by,
    created_time, updated_time
) VALUES (
    84, 1, '["LEGCPY_0"]'::jsonb, 13, '["COM_CODE"]'::jsonb,
    'INNER', 'business',
    '公司及工厂信息的公司编码和公司及工厂的信息的主数据的公司代码是业务关联关系',
    '1|LEGCPY_0->13|COM_CODE', NULL,
    '2026-10-03 09:00:56.762739+00', '2026-10-03 09:00:56.762742+00'
);

-- ── B. 还原属性 702 的 FK 标记 ──────────────────────────────────────────
-- 只动这两个字段，其余列不动（等价于界面上的「勾外键 + 选引用类=工厂」）。
UPDATE ontology_property
SET is_foreign_key = true,
    ref_class_id   = 1,
    updated_time   = now()
WHERE id = 702
  AND property_name = 'COM_CODE';

-- ── 核对 ───────────────────────────────────────────────────────────────
-- 期望：0 行
SELECT id, source_class_id, target_class_id
FROM ontology_join
WHERE id IN (83, 84);

-- 期望：1 行，且 is_foreign_key = true、ref_class_id = 1
SELECT id, property_name, is_foreign_key, ref_class_id
FROM ontology_property
WHERE id = 702;

COMMIT;
