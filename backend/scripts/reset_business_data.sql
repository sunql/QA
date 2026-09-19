-- feat-business-data-reset — PG 业务表清理
-- 顺序：TRUNCATE ... RESTART IDENTITY CASCADE 单 SQL，依赖自动级联
-- 不删 ontology_*（本体走人工导入向导）
-- 不删 ontology_class_pre_pruning_20260918（过程快照）
-- 不删 users/roles/data_source/llm_config/menu_config/agent_* 等系统配置
-- 不删 audit_log/audit_outbox/session_* 等历史追溯

BEGIN;

TRUNCATE
  -- 评估 + 分享
  evaluation_report_share,
  evaluation_report_schedule,
  evaluation_report,
  -- 文档 + 证据
  document_entity_relation,
  document_catalog,
  evidence,
  -- 知识发现
  structure_suggestion,
  knowledge_conflict,
  knowledge_community_member,
  knowledge_community,
  knowledge_relation,
  knowledge_claim,
  -- wiki
  wiki_graph_insight,
  wiki_compile_item,
  wiki_compile_task,
  wiki_import_task,
  wiki_page,
  -- in-app
  in_app_message,
  -- KPI + 特征
  kpi_catalog_history,
  kpi_catalog,
  feature_definition_history,
  feature_value,
  feature_rule,
  feature_definition,
  -- DQ
  data_quality_violation_sample,
  data_quality_score,
  data_quality_rule,
  -- 血缘
  data_lineage,
  -- 实体映射
  entity_mapping
RESTART IDENTITY CASCADE;

COMMIT;

-- 本体 7 张单独清（不在上面批量，避免审计混淆）
BEGIN;

TRUNCATE
  ontology_metric,
  ontology_relation,
  class_domain_mapping,
  business_object,
  ontology_join,
  ontology_property,
  ontology_class
RESTART IDENTITY CASCADE;

COMMIT;