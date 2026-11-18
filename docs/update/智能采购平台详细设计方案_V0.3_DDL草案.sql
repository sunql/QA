-- 智能采购平台详细设计 V0.3：新增表 DDL 草案
-- 注意：本文件为设计参考，不可直接在生产执行。
-- Alembic revision/down_revision 必须从目标分支真实 head 生成。
-- 先在隔离测试库验证约束、索引、权限和回滚策略。

CREATE TABLE procurement_ontology_release (
  id BIGSERIAL PRIMARY KEY,
  release_code VARCHAR(40) NOT NULL UNIQUE,
  status VARCHAR(20) NOT NULL CHECK
    (status IN ('DRAFT','VALIDATING','READY','ACTIVE','RETIRED','FAILED')),
  manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  manifest_hash VARCHAR(64),
  created_by VARCHAR(64) NOT NULL,
  activated_by VARCHAR(64),
  activated_at TIMESTAMPTZ,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_procurement_release_one_active
  ON procurement_ontology_release ((status)) WHERE status='ACTIVE';

CREATE TABLE procurement_ontology_release_check (
  id BIGSERIAL PRIMARY KEY,
  release_id BIGINT NOT NULL REFERENCES procurement_ontology_release(id) ON DELETE CASCADE,
  check_code VARCHAR(64) NOT NULL,
  severity VARCHAR(12) NOT NULL CHECK (severity IN ('INFO','WARNING','ERROR')),
  result VARCHAR(12) NOT NULL CHECK (result IN ('PASS','FAIL','SKIP')),
  detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(release_id, check_code)
);

CREATE TABLE procurement_agent_run_context (
  run_id VARCHAR(36) PRIMARY KEY REFERENCES agent_orchestration_run(run_id) ON DELETE CASCADE,
  ontology_release_id BIGINT REFERENCES procurement_ontology_release(id) ON DELETE RESTRICT,
  rule_release_id BIGINT,
  scenario_code VARCHAR(64) NOT NULL,
  correlation_id VARCHAR(64) NOT NULL,
  context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE procurement_workflow_definition (
  id BIGSERIAL PRIMARY KEY,
  workflow_code VARCHAR(64) NOT NULL,
  version INTEGER NOT NULL,
  name VARCHAR(128) NOT NULL,
  status VARCHAR(16) NOT NULL CHECK (status IN ('DRAFT','PUBLISHED','RETIRED')),
  definition_json JSONB NOT NULL,
  created_by VARCHAR(64) NOT NULL,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workflow_code, version)
);

CREATE TABLE procurement_workflow_instance (
  id BIGSERIAL PRIMARY KEY,
  instance_id UUID NOT NULL UNIQUE,
  workflow_code VARCHAR(64) NOT NULL,
  workflow_version INTEGER NOT NULL,
  business_key VARCHAR(128) NOT NULL,
  status VARCHAR(24) NOT NULL,
  current_node VARCHAR(64),
  context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by VARCHAR(64) NOT NULL,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workflow_code, business_key, workflow_version)
);

CREATE TABLE procurement_workflow_task (
  id BIGSERIAL PRIMARY KEY,
  task_id UUID NOT NULL UNIQUE,
  instance_id UUID NOT NULL REFERENCES procurement_workflow_instance(instance_id) ON DELETE CASCADE,
  node_code VARCHAR(64) NOT NULL,
  task_type VARCHAR(24) NOT NULL,
  assignee VARCHAR(64),
  candidate_role VARCHAR(64),
  status VARCHAR(20) NOT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  due_at TIMESTAMPTZ,
  completed_by VARCHAR(64),
  completed_at TIMESTAMPTZ,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE procurement_business_command (
  id BIGSERIAL PRIMARY KEY,
  command_id UUID NOT NULL UNIQUE,
  command_type VARCHAR(64) NOT NULL,
  business_object VARCHAR(32) NOT NULL,
  business_key VARCHAR(128),
  payload_json JSONB NOT NULL,
  payload_hash VARCHAR(64) NOT NULL,
  status VARCHAR(24) NOT NULL,
  idempotency_key VARCHAR(128) NOT NULL UNIQUE,
  requested_by VARCHAR(64) NOT NULL,
  approved_by VARCHAR(64),
  external_system VARCHAR(32),
  external_ref VARCHAR(128),
  result_json JSONB,
  error_code VARCHAR(64),
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_time TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE procurement_approval_record (
  id BIGSERIAL PRIMARY KEY,
  command_id UUID NOT NULL REFERENCES procurement_business_command(command_id) ON DELETE RESTRICT,
  approval_step INTEGER NOT NULL,
  approver VARCHAR(64) NOT NULL,
  decision VARCHAR(16) NOT NULL CHECK (decision IN ('APPROVE','REJECT','RETURN')),
  comment TEXT,
  decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(command_id, approval_step, approver)
);

CREATE TABLE procurement_outbox_event (
  id BIGSERIAL PRIMARY KEY,
  event_id UUID NOT NULL UNIQUE,
  aggregate_type VARCHAR(64) NOT NULL,
  aggregate_id VARCHAR(128) NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  payload_json JSONB NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at TIMESTAMPTZ,
  published_at TIMESTAMPTZ,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_procurement_outbox_pending
  ON procurement_outbox_event(status, next_retry_at, id);

-- 建议后续补充：
-- 1) procurement_source_binding / procurement_property_binding / procurement_key_binding
-- 2) procurement_rule_definition / procurement_rule_version
-- 3) 所有状态字段 CHECK / FK / tenant-scope 索引（以最终权限模型为准）
-- 4) workflow instance/task 的乐观锁 revision 与租约字段


-- C. Procurement source bindings and rule versioning (proposed)

CREATE TABLE procurement_source_binding (
  id BIGSERIAL PRIMARY KEY,
  class_id BIGINT NOT NULL REFERENCES ontology_class(id) ON DELETE RESTRICT,
  datasource_id BIGINT NOT NULL REFERENCES data_source(id) ON DELETE RESTRICT,
  source_system VARCHAR(20) NOT NULL,
  schema_name VARCHAR(128),
  table_name VARCHAR(128) NOT NULL,
  table_alias VARCHAR(32),
  binding_role VARCHAR(16) NOT NULL DEFAULT 'PRIMARY'
    CHECK (binding_role IN ('PRIMARY','DETAIL','LOOKUP','AGGREGATE')),
  is_primary BOOLEAN NOT NULL DEFAULT false,
  filter_spec JSONB NOT NULL DEFAULT '{}'::jsonb,
  mapping_version INTEGER NOT NULL DEFAULT 1 CHECK (mapping_version > 0),
  status VARCHAR(16) NOT NULL DEFAULT 'DRAFT'
    CHECK (status IN ('DRAFT','ACTIVE','DISABLED')),
  created_by VARCHAR(64) NOT NULL,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_proc_source_binding_class
  ON procurement_source_binding(class_id, status);
CREATE INDEX ix_proc_source_binding_datasource
  ON procurement_source_binding(datasource_id, source_system, table_name);

CREATE TABLE procurement_property_binding (
  id BIGSERIAL PRIMARY KEY,
  property_id BIGINT NOT NULL REFERENCES ontology_property(id) ON DELETE RESTRICT,
  source_binding_id BIGINT NOT NULL REFERENCES procurement_source_binding(id) ON DELETE CASCADE,
  column_name VARCHAR(128),
  expression_spec JSONB,
  data_type VARCHAR(32) NOT NULL,
  unit VARCHAR(50),
  transform_code VARCHAR(64),
  nullable BOOLEAN,
  is_key_component BOOLEAN NOT NULL DEFAULT false,
  status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('ACTIVE','DISABLED')),
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (column_name IS NOT NULL OR expression_spec IS NOT NULL)
);
CREATE UNIQUE INDEX uq_proc_property_binding_column
  ON procurement_property_binding(property_id, source_binding_id, column_name)
  WHERE column_name IS NOT NULL;
CREATE INDEX ix_proc_property_binding_source
  ON procurement_property_binding(source_binding_id);

CREATE TABLE procurement_key_binding (
  id BIGSERIAL PRIMARY KEY,
  class_id BIGINT NOT NULL REFERENCES ontology_class(id) ON DELETE RESTRICT,
  source_system VARCHAR(20) NOT NULL,
  key_columns JSONB NOT NULL,
  enterprise_key_strategy VARCHAR(32) NOT NULL
    CHECK (enterprise_key_strategy IN ('ENTITY_MAPPING','STABLE_HASH','NATIVE_KEY')),
  match_rule VARCHAR(20) NOT NULL,
  effective_from DATE,
  effective_to DATE,
  status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('DRAFT','ACTIVE','RETIRED')),
  created_by VARCHAR(64) NOT NULL,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from)
);
CREATE INDEX ix_proc_key_binding_lookup
  ON procurement_key_binding(class_id, source_system, status);

-- D. Procurement rule definition/version tables (proposed)

CREATE TABLE procurement_rule_definition (
  id BIGSERIAL PRIMARY KEY,
  rule_code VARCHAR(64) NOT NULL UNIQUE,
  rule_name VARCHAR(128) NOT NULL,
  rule_domain VARCHAR(32) NOT NULL,
  description TEXT,
  owner VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'DRAFT'
    CHECK (status IN ('DRAFT','ACTIVE','RETIRED')),
  current_version INTEGER,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_time TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE procurement_rule_version (
  id BIGSERIAL PRIMARY KEY,
  rule_id BIGINT NOT NULL REFERENCES procurement_rule_definition(id) ON DELETE RESTRICT,
  version INTEGER NOT NULL CHECK (version > 0),
  effective_from TIMESTAMPTZ,
  effective_to TIMESTAMPTZ,
  rule_type VARCHAR(24) NOT NULL
    CHECK (rule_type IN ('ELIGIBILITY','APPROVAL','QUOTATION','SCORING','COMPLIANCE')),
  input_schema JSONB NOT NULL,
  rule_spec JSONB NOT NULL,
  output_schema JSONB NOT NULL,
  status VARCHAR(16) NOT NULL
    CHECK (status IN ('DRAFT','PUBLISHED','RETIRED')),
  published_by VARCHAR(64),
  published_at TIMESTAMPTZ,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(rule_id, version),
  CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from)
);
CREATE INDEX ix_proc_rule_version_effective
  ON procurement_rule_version(rule_id, status, effective_from, effective_to);
