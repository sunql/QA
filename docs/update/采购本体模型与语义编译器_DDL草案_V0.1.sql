
CREATE TABLE procurement_ontology_release (
  id BIGSERIAL PRIMARY KEY,
  release_code VARCHAR(40) NOT NULL UNIQUE,
  status VARCHAR(20) NOT NULL CHECK
    (status IN ('DRAFT','VALIDATING','READY','ACTIVE','RETIRED','FAILED')),
  manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  manifest_hash VARCHAR(64),
  compiler_version VARCHAR(32) NOT NULL,
  source_revision VARCHAR(64),
  created_by VARCHAR(64) NOT NULL,
  activated_by VARCHAR(64),
  activated_at TIMESTAMPTZ,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_procurement_ontology_release_active
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

CREATE TABLE procurement_source_binding (
  id BIGSERIAL PRIMARY KEY,
  class_id BIGINT NOT NULL REFERENCES ontology_class(id) ON DELETE RESTRICT,
  datasource_id BIGINT NOT NULL REFERENCES data_source(id) ON DELETE RESTRICT,
  source_system VARCHAR(20) NOT NULL,
  schema_name VARCHAR(128),
  table_name VARCHAR(128) NOT NULL,
  table_alias VARCHAR(32),
  binding_role VARCHAR(16) NOT NULL
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
CREATE INDEX ix_proc_source_binding_class ON procurement_source_binding(class_id,status);
CREATE INDEX ix_proc_source_binding_source
  ON procurement_source_binding(datasource_id,source_system,table_name);

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
CREATE INDEX ix_proc_property_binding_source ON procurement_property_binding(source_binding_id);
CREATE INDEX ix_proc_property_binding_property ON procurement_property_binding(property_id);

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
CREATE INDEX ix_proc_key_binding_lookup ON procurement_key_binding(class_id,source_system,status);

CREATE TABLE procurement_agent_run_context (
  run_id VARCHAR(36) PRIMARY KEY REFERENCES agent_orchestration_run(run_id) ON DELETE CASCADE,
  ontology_release_id BIGINT REFERENCES procurement_ontology_release(id) ON DELETE RESTRICT,
  rule_release_id BIGINT,
  scenario_code VARCHAR(64) NOT NULL,
  correlation_id VARCHAR(64) NOT NULL,
  context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now()
);
