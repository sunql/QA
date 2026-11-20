-- =============================================================================
-- 召回「跨源过滤」判定探针 v2（SSOT，勿各写一套）
--
-- 口径完全对齐运行时代码：
--   backend/app/services/recall_datasource_filter.py
--     _tableKeys()          —— 裸名 ∪ owner 限定名，并集，.upper()
--     loadDatasourceTableSet() —— 无缓存行 / 清单为空 / 读异常 ⇒ None ⇒ fail-open
--     filterClassesByDatasource() —— source_table 为空 ⇒ 保留（不误杀）
--
-- 用法（生产）：
--   docker exec -i <pg-container> sh -c 'psql -U qa_user -d qa_metadata' <<'SQL'
--   ...粘贴本文件...
--   SQL
-- ⚠️ 勿用 docker exec X sh -c 'psql -c "…SQL…"' —— 外层单引号会吃掉 SQL 里所有单引号。
--
-- ★ 三个必记的坑（实测踩过）：
--   ① jsonb_array_length() 不是聚合函数 ⇒ 必须 sum() 包裹，否则报
--      "must appear in the GROUP BY clause"；
--   ② t->>'a' || '.' || t->>'b' 有类型歧义 ⇒ 用
--      upper(concat_ws('.', t->>'table_name', nullif(t->>'owner','')))；
--   ③ 裸名匹配**不区分 owner**（多个 owner 的行会汇成同一个裸名键）⇒
--      跨 owner 同名表会互相放行（口径固有的松，见文末「口径注记」）。
-- =============================================================================


-- =============================================================================
-- P1｜每源「运行时语义 + 类保留/剔除」总览（**主口径**）
--
-- 输出每数据源：缓存行数 / 缓存表数 / 运行时语义 / 活类总数 / 保留 / 剔除 / fail-open 保留
-- 判读：
--   runtime_mode = 'fail_open_no_cache' ⇒ 该源 schema_cache 无可用清单 ⇒ **完全不过滤**
--                                   ⇒ 跨源幽灵表原样进来（1119 故障路径），
--                                   这是 **A1 探针**要抓的东西；
--   runtime_mode = 'hard_filter'     ⇒ 硬过滤生效 ⇒ kept + dropped = 活类总数（可闭合校验）。
-- =============================================================================
WITH cache_rows AS (
  SELECT datasource_id,
         count(*)                                AS cache_rows,
         coalesce(sum(jsonb_array_length(schema_data)), 0) AS cached_tables
  FROM schema_cache
  GROUP BY datasource_id
),
cache_keys AS (
  SELECT sc.datasource_id,
         array_agg(DISTINCT upper(t ->> 'table_name')) AS bare_keys,
         array_agg(DISTINCT upper(concat_ws('.', t ->> 'table_name', nullif(t ->> 'owner', '')))) AS qual_keys
  FROM schema_cache sc, LATERAL jsonb_array_elements(sc.schema_data) t
  GROUP BY sc.datasource_id
),
cls AS (
  SELECT id, class_name, source_table, description
  FROM ontology_class
  WHERE valid_to IS NULL
),
per_class AS (
  SELECT d.id                AS ds_id,
         d.name              AS ds_name,
         c.id                AS class_id,
         c.class_name,
         c.source_table,
         c.description,
         coalesce(cr.cached_tables, 0) AS cached_tables,
         CASE WHEN coalesce(cr.cached_tables, 0) = 0
              THEN 'fail_open_no_cache' ELSE 'hard_filter' END AS runtime_mode,
         CASE
           WHEN coalesce(cr.cached_tables, 0) = 0
             THEN NULL                                   -- fail-open：不过滤 ⇒ 全部保留
           WHEN coalesce(btrim(c.source_table), '') = ''
             THEN TRUE                                   -- 无 source_table ⇒ 保留（不误杀）
           WHEN upper(c.source_table) = ANY(ck.bare_keys)
             OR upper(c.source_table) = ANY(ck.qual_keys)
             THEN TRUE
           ELSE FALSE
         END AS kept
  FROM data_source d
  CROSS JOIN cls c
  LEFT JOIN cache_rows cr ON cr.datasource_id = d.id
  LEFT JOIN cache_keys ck ON ck.datasource_id = d.id
)
SELECT ds_id,
       ds_name,
       runtime_mode,
       cached_tables,
       count(*)                                     AS total_active_classes,
       count(*) FILTER (WHERE kept IS TRUE)         AS kept,
       count(*) FILTER (WHERE kept IS FALSE)        AS dropped,
       count(*) FILTER (WHERE kept IS NULL)         AS kept_fail_open
FROM per_class
GROUP BY ds_id, ds_name, runtime_mode, cached_tables
ORDER BY ds_id;


-- =============================================================================
-- P2｜逐类明细（**判读用**；把 P1 的 per_class 展开，每类一行 + 判定位）
--
-- 用法：把下面 WHERE 换成你要看的源，例如 ds_id = 1
-- 判定位（verdict）语义：
--   '保留'                       —— 该类在本源缓存命中，运行时保留
--   'fail-open 保留'             —— 该源无缓存清单 ⇒ 不过滤 ⇒ 保留（**且不过滤是风险**）
--   '保留（无 source_table）'    —— 无表名，无法判定 ⇒ 代码不误杀
--   '剔除（该表不在本源清单）'    —— 运行时会被剔；**须走 P3 判别判据**定性质
-- 附加列 `placeholder_hint` 只是**快速分流启发式**（非判据）：
--   命中「0 行 / 结构性」字样 ⇒ 疑似结构性占位类 ⇒ 大概率剔除正确
--   未命中 ⇒ 必须走 P3 到目标库确认
-- =============================================================================
WITH cache_rows AS (
  SELECT datasource_id,
         count(*)                                AS cache_rows,
         coalesce(sum(jsonb_array_length(schema_data)), 0) AS cached_tables
  FROM schema_cache GROUP BY datasource_id
),
cache_keys AS (
  SELECT sc.datasource_id,
         array_agg(DISTINCT upper(t ->> 'table_name')) AS bare_keys,
         array_agg(DISTINCT upper(concat_ws('.', t ->> 'table_name', nullif(t ->> 'owner', '')))) AS qual_keys
  FROM schema_cache sc, LATERAL jsonb_array_elements(sc.schema_data) t
  GROUP BY sc.datasource_id
),
cls AS (
  SELECT id, class_name, source_table, description FROM ontology_class WHERE valid_to IS NULL
),
per_class AS (
  SELECT d.id AS ds_id, d.name AS ds_name,
         c.id AS class_id, c.class_name, c.source_table, c.description,
         CASE WHEN coalesce(cr.cached_tables, 0) = 0
              THEN 'fail_open_no_cache' ELSE 'hard_filter' END AS runtime_mode,
         CASE
           WHEN coalesce(cr.cached_tables, 0) = 0 THEN NULL
           WHEN coalesce(btrim(c.source_table), '') = '' THEN TRUE
           WHEN upper(c.source_table) = ANY(ck.bare_keys)
             OR upper(c.source_table) = ANY(ck.qual_keys) THEN TRUE
           ELSE FALSE
         END AS kept
  FROM data_source d
  CROSS JOIN cls c
  LEFT JOIN cache_rows cr ON cr.datasource_id = d.id
  LEFT JOIN cache_keys ck ON ck.datasource_id = d.id
)
SELECT ds_id,
       ds_name,
       class_id,
       class_name,
       source_table,
       runtime_mode,
       kept,
       CASE
         WHEN kept IS TRUE  THEN '保留'
         WHEN kept IS NULL  THEN 'fail-open 保留（该源无缓存清单 ⇒ 完全不过滤）'
         WHEN coalesce(btrim(source_table), '') = '' THEN '保留（无 source_table，无法判定）'
         ELSE '剔除（该表不在本源 schema_cache 清单）'
       END AS verdict,
       CASE
         WHEN kept IS NOT FALSE THEN '—（未被剔除，无需判定）'
         WHEN description ILIKE '%0 行%' OR description ILIKE '%0行%'
           OR description ILIKE '%结构性%'
           THEN '疑似结构性占位类（**启发式，须 P3 确认**）'
         ELSE '须 P3 到目标库确认'
       END AS placeholder_hint
FROM per_class
-- WHERE ds_id = 1 AND kept IS FALSE      -- 只看被剔的类
ORDER BY ds_id, kept NULLS LAST, class_id;


-- =============================================================================
-- P3｜★ 判别判据：剔除是「正确行为」还是「漏召回」——**必须到目标库确认**
--
-- 判别链（三个类，务必按序走）：
--
--  [1] PG 侧（P2 已给）：类被剔除 ⇔ upper(source_table) 不在该源 schema_cache 的表名集合。
--      —— 这一步只说明「缓存里没有这张表」，**不能直接定性**。
--
--  [2] 目标库侧（**决定性**）：查该表在目标库里到底存不存在。
--      Oracle 模板（在**目标 Oracle 库**上跑，owner 取 data_source.username 的大写）：
--
--        SELECT owner, table_name, num_rows FROM all_tables
--        WHERE upper(table_name) IN ('DWD_SUPPLIER','DWD_MATERIAL','DWD_INCOMING_INSPECTION');
--
--      ★ 为什么「存不存在」就是决定性的：introspect 是按**列**枚举的
--        （all_tab_columns / information_schema.columns），**不看行数**
--        ⇒ 一张**存在但 0 行**的表**照样会被写进 schema_cache**。
--        ⇒ 「表存在却不在 schema_cache」= **introspection 过期/漏刷** ⇒ 真缺陷。
--
--  [3] 定性表：
--
--      目标库里【查无此表】
--        ⇒ 剔除是**正确行为**。类指向一张不存在的物理表；留着它 LLM 会选中它
--          ⇒ ORA-00942（1119 事故形态）。**无需任何处置**，只登记「类→源绑定」语义缺口。
--
--      目标库里【有此表】但不在 schema_cache
--        ⇒ **真缺陷（漏召回：类有数据却看不见）**。根因是 introspection 过期，
--          **不是**过滤逻辑 ⇒ 处置 = 重跑 introspect，**不要改过滤**。
--          （此路还需排除「owner 口径不匹配」：见文末口径注记 ③）
--
--      目标库里【有此表】且在 schema_cache 里
--        ⇒ 该类不该被剔；若 P2 显示它被剔，说明**匹配口径有问题**（owner/大小写/裸名）
--          ⇒ 属代码缺陷，单独提 issue。
--
--  [4]（可选，仅当 [2] 得到「有此表」）确认真有数据：
--        SELECT count(*) FROM <OWNER>.<TABLE>;
--        = 0   ⇒ 表存在但空 ⇒ 严格说「不是漏召回」（本来也答不出数），
--                但仍需修 introspection（否则表一旦有数据就静默漏召回）；
--        > 0   ⇒ 确证漏召回。
-- =============================================================================


-- =============================================================================
-- P4｜全源孤儿（所有源都找不到的类）——与 acceptance-1120.md §5.2 同口径
--      （区别：P4 看「所有源」，A2 判据只看「本源」⇒ A2 查不出这一类）
-- =============================================================================
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
SELECT c.t AS orphan_table
FROM cls c
WHERE NOT EXISTS (SELECT 1 FROM cache cc
                  WHERE cc.bare = c.t OR cc.qualified = c.t)
ORDER BY 1;


-- =============================================================================
-- 口径注记（写代码/写判据前必读）
--
-- ① 三态 fail-open（recall_datasource_filter.applyDatasourceRecallFilter）：
--      datasourceId is None      ⇒ 不过滤，披露 no_datasource，**服务端零日志**（观测盲区）
--      schema_cache 无可用清单    ⇒ 不过滤，披露 no_cache，**服务端 warning**
--      有清单                    ⇒ 硬过滤
--    ★ 现实里能触发的只有 no_cache；no_datasource 分支在 chat/多步/研究执行面
--      **不可达**（三处 datasourceId 来源都强制非空）。
--
-- ② 裸名匹配**不区分 owner**：_tableKeys 把每个 owner 行的 table_name 都加进裸名键，
--    于是 A_OWNER.T 与 B_OWNER.T 的裸名键相同 ⇒ 跨 owner 同名表互相放行。
--    判读 P2/P3 结果时若表名跨 owner 重名，先排除这一条。
--
-- ③ Oracle introspection 按 **owner** 命名空间分片（owner = UPPER(data_source.username)，
--    可显式覆盖）；一个源可有多行 schema_cache（多个 owner）⇒
--    「某表不在本源清单」也可能只是**owner 选错**，不是表不存在。
--    P3 查 all_tables 时请带 owner 一起比对。
--
-- ④ 无 source_table 的类**永远保留**（代码不误杀）⇒ 判读时这类计在 kept，
--    不算「漏过滤」，也不算「漏召回」。
-- =============================================================================
