-- prune_ontology_classes.sql
-- 目的：去重 27 个语义类（id 1-27）与 27 个 ODS 原始类（id 68-94），
--       把 27 行的中文 class_alias + description 合并到 ODS 原始类，
--       把所有 1462 条外键引用 1-27 → 68-94，删除语义类。
-- 范围：prod qa_metadata
-- 约束：单事务原子；执行前后均有 count 校验
-- 备份：pre-pruning 已用 CREATE TABLE ontology_class_pre_pruning_20260918 AS SELECT * 备份

BEGIN;

\set ON_ERROR_STOP on

-- 0. 守卫映射：semantic(1-27) → ODS raw(68-94) by source_table 唯一对应
--    必须 27 行（已用 EXPLAIN 预验证）
CREATE TEMP TABLE _sem_to_ods ON COMMIT DROP AS
SELECT oc1.id AS sem_id, oc2.id AS ods_id, oc1.source_table
FROM ontology_class oc1
JOIN ontology_class oc2 ON oc1.source_table = oc2.source_table
WHERE oc1.id BETWEEN 1 AND 27
  AND oc2.id BETWEEN 68 AND 94;

-- 守 1：映射必须恰好 27 行（无重复 source_table）
DO $$
DECLARE
  map_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO map_count FROM _sem_to_ods;
  IF map_count != 27 THEN
    RAISE EXCEPTION '映射行数异常：期望 27 实际 %，中止', map_count;
  END IF;
END $$;

-- 1. ontology_join：source/target 双向迁移（106 + 106 = 212 行）
UPDATE ontology_join oj
SET source_class_id = m.ods_id
FROM _sem_to_ods m
WHERE oj.source_class_id = m.sem_id;

UPDATE ontology_join oj
SET target_class_id = m.ods_id
FROM _sem_to_ods m
WHERE oj.target_class_id = m.sem_id;

-- 2. ontology_property：class_id（883 行）+ ref_class_id（61 行）
UPDATE ontology_property op
SET class_id = m.ods_id
FROM _sem_to_ods m
WHERE op.class_id = m.sem_id;

UPDATE ontology_property op
SET ref_class_id = m.ods_id
FROM _sem_to_ods m
WHERE op.ref_class_id = m.sem_id;

-- 3. ontology_metric：target_class_id（9 行）
UPDATE ontology_metric om
SET target_class_id = m.ods_id
FROM _sem_to_ods m
WHERE om.target_class_id = m.sem_id;

-- 4. ontology_relation：source/target（24 + 24 = 48 行）
UPDATE ontology_relation orel
SET source_class_id = m.ods_id
FROM _sem_to_ods m
WHERE orel.source_class_id = m.sem_id;

UPDATE ontology_relation orel
SET target_class_id = m.ods_id
FROM _sem_to_ods m
WHERE orel.target_class_id = m.sem_id;

-- 5. ontology_class：self-FK parent_class_id（1 行）
UPDATE ontology_class oc
SET parent_class_id = m.ods_id
FROM _sem_to_ods m
WHERE oc.parent_class_id = m.sem_id;

-- 6. business_object：header_class_id（4 行，ON DELETE RESTRICT）
UPDATE business_object bo
SET header_class_id = m.ods_id
FROM _sem_to_ods m
WHERE bo.header_class_id = m.sem_id;

-- 7. coverage_cell：216 行 100% 与 ODS 原始类的 (dimension, ontology_class_id, domain)
--    唯一约束 uq_coverage_cell 冲突。语义类与 ODS 原始类有完全相同的 coverage
--    元组（dimension, domain, status），迁过去会全部撞键。安全做法：删除语义侧的
--    冗余行（与 ON DELETE CASCADE 行为一致），ODS 原始侧的 216 行已含相同信息。
DELETE FROM coverage_cell cc
USING _sem_to_ods m
WHERE cc.ontology_class_id = m.sem_id;

-- 8. data_quality_rule：source_class_id（28 行，无 FK 但要保数据完整性）
UPDATE data_quality_rule dq
SET source_class_id = m.ods_id
FROM _sem_to_ods m
WHERE dq.source_class_id = m.sem_id;

-- 9. evaluation_report：class_ids jsonb（1 行 [1]）
--    jsonb 数组元素是 number 形式的 class_id，需要逐元素转换
UPDATE evaluation_report er
SET class_ids = (
  SELECT jsonb_agg(
    CASE
      WHEN jsonb_typeof(v) = 'number' AND (v::int) BETWEEN 1 AND 27
      THEN to_jsonb((SELECT ods_id FROM _sem_to_ods WHERE sem_id = v::int))
      ELSE v
    END
  )
  FROM jsonb_array_elements(er.class_ids) v
)
WHERE EXISTS (
  SELECT 1 FROM jsonb_array_elements(er.class_ids) v
  WHERE jsonb_typeof(v) = 'number' AND (v::int) BETWEEN 1 AND 27
);

-- 10. 把语义类的 class_alias + description 合并到 ODS 原始类
--     class_name 不动（ODS raw 用 ODS_BOM 等表名，避免与历史 class_name 冲突）
UPDATE ontology_class ods
SET class_alias = sem.class_alias,
    description = sem.description
FROM ontology_class sem
WHERE sem.id BETWEEN 1 AND 27
  AND ods.id BETWEEN 68 AND 94
  AND ods.source_table = sem.source_table;

-- 守 2：迁移后引用 1-27 的行数为 0
DO $$
DECLARE
  bad_count INTEGER;
BEGIN
  SELECT (
    (SELECT COUNT(*) FROM ontology_join WHERE source_class_id BETWEEN 1 AND 27 OR target_class_id BETWEEN 1 AND 27) +
    (SELECT COUNT(*) FROM ontology_property WHERE class_id BETWEEN 1 AND 27 OR ref_class_id BETWEEN 1 AND 27) +
    (SELECT COUNT(*) FROM ontology_metric WHERE target_class_id BETWEEN 1 AND 27) +
    (SELECT COUNT(*) FROM ontology_relation WHERE source_class_id BETWEEN 1 AND 27 OR target_class_id BETWEEN 1 AND 27) +
    (SELECT COUNT(*) FROM ontology_class WHERE parent_class_id BETWEEN 1 AND 27) +
    (SELECT COUNT(*) FROM business_object WHERE header_class_id BETWEEN 1 AND 27) +
    (SELECT COUNT(*) FROM coverage_cell WHERE ontology_class_id BETWEEN 1 AND 27) +
    (SELECT COUNT(*) FROM data_quality_rule WHERE source_class_id BETWEEN 1 AND 27)
  ) INTO bad_count;
  IF bad_count > 0 THEN
    RAISE EXCEPTION '迁移后仍有 % 条残留 1-27 引用，回滚', bad_count;
  END IF;
END $$;

-- 11. 守 3：合并后 ODS 原始类必须有 Chinese alias（不能空）
DO $$
DECLARE
  empty_alias_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO empty_alias_count
  FROM ontology_class
  WHERE id BETWEEN 68 AND 94
    AND (class_alias IS NULL OR class_alias = '');
  IF empty_alias_count > 0 THEN
    RAISE EXCEPTION 'ODS 原始类仍有 % 行 class_alias 为空，回滚', empty_alias_count;
  END IF;
END $$;

-- 12. 删除 27 行语义类
DELETE FROM ontology_class WHERE id BETWEEN 1 AND 27;

-- 守 4：最终行数 = 69（96 - 27）
DO $$
DECLARE
  final_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO final_count FROM ontology_class;
  IF final_count != 69 THEN
    RAISE EXCEPTION '最终 ontology_class 行数 = %，期望 69，回滚', final_count;
  END IF;
END $$;

COMMIT;

-- 事后报告
SELECT 'ontology_class_total' AS metric, COUNT(*) AS value FROM ontology_class
UNION ALL
SELECT 'ontology_join_total', COUNT(*) FROM ontology_join
UNION ALL
SELECT 'ontology_property_total', COUNT(*) FROM ontology_property
UNION ALL
SELECT 'ontology_relation_total', COUNT(*) FROM ontology_relation
UNION ALL
SELECT 'ontology_metric_total', COUNT(*) FROM ontology_metric
UNION ALL
SELECT 'business_object_with_ontology', COUNT(*) FROM business_object WHERE header_class_id IS NOT NULL
UNION ALL
SELECT 'coverage_cell_total', COUNT(*) FROM coverage_cell;
