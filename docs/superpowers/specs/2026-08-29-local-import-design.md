# 本地导入功能设计文档

## 概述

为 QA System 增加“本地导入”能力：管理员在系统中注册新的业务数据源后，可以通过一个向导式界面，自动读取该数据库的 schema 元数据，并批量生成本体（Ontology）中的 Class、Property 与 Join。导入过程由 LLM 做语义增强（中文别名、描述、枚举识别、过滤建议），最终结果需经用户在预览界面确认后才真正写入。

## 目标

1. 降低新数据库接入成本：从手工逐个创建 Class/Property/Join 变为自动化导入。
2. 保证导入质量：预览确认 + 冲突处理 + 规则过滤，避免日志表、系统表污染本体。
3. 利用 LLM 提升本体可读性：自动生成中文别名、描述、枚举识别结果。
4. 保持系统安全：所有对业务库的查询仍为只读，经 SQL Guard 校验；LLM 只接触元数据，不接触真实数据行。

## 非目标

1. 不自动生成 Metric：指标含义无法从 schema 元数据可靠推导，留空由用户后续手动补充。
2. 不做全自动无确认导入：必须经用户在预览界面确认后才写入。
3. 不处理跨数据源同名表冲突：当前 ontology 为全局共享（R4 已知限制），导入时按 `source_table` 去重；如需多库隔离，依赖后续版本管理或数据源级命名空间增强。
4. 第一期不持久化导入规则配置：规则通过请求 DTO 传入，前端预填默认值；后续可升级为 `import_profile` 表。

## 架构

```
用户/前端 ── HTTP ──> LocalImport API
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
    SchemaIntrospection  LLM Enhancer    OntologyService
    (读 schema_cache)    (别名/描述/枚举)  (PG + Neo4j 写入)
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
    ImportRuleEngine   ImportConflictResolver  EmbeddingService
    (过滤/类型映射)     (冲突检测)            (Milvus 同步)
```

## 用户流程

1. 管理员在 `DatasourcePage` 注册并测试数据源。
2. 点击数据源行的“智能导入到本体”。
3. 打开 Wizard Modal：
   - **Step 1 规则配置**：调整表过滤规则、数据类型映射规则；可一键采纳 LLM 建议。
   - **Step 2 智能预览**：查看新增 Class/Property/Join（带 LLM 生成的别名/描述/枚举），处理冲突清单（跳过/覆盖/重命名）。
   - **Step 3 确认写入**：展示汇总，确认后批量写入 ontology，显示结果。

## 后端组件

| 文件 | 职责 |
|---|---|
| `app/services/local_import_service.py` | 导入主服务：协调 schema 读取、LLM 增强、规则应用、冲突检测、批量写入。 |
| `app/services/import_rule_engine.py` | 过滤规则 + 数据类型映射规则的应用与默认配置。 |
| `app/services/import_llm_enhancer.py` | 调用 LLM 生成别名、描述、枚举识别、过滤建议。 |
| `app/services/import_conflict_resolver.py` | 比对提案与现有 ontology，生成冲突清单。 |
| `app/api/v1/local_import.py` | 两个接口：`import-preview`、`import`。 |
| `app/domain/schemas.py` | 新增导入相关 DTO。 |

### LocalImportService

```python
class LocalImportService:
    async def build_preview(
        self,
        session: AsyncSession,
        datasource_id: int,
        rules: ImportRuleConfig,
    ) -> ImportPreview

    async def execute_import(
        self,
        session: AsyncSession,
        datasource_id: int,
        request: ImportExecuteRequest,
        created_by: str | None,
    ) -> ImportResult
```

### ImportRuleEngine

```python
class ImportRuleEngine:
    DEFAULT_TABLE_RULES: TableFilterRules
    DEFAULT_TYPE_MAPPINGS: TypeMappingRules

    def filter_tables(self, tables: list[TableSchemaRead], rules: TableFilterRules) -> list[TableSchemaRead]
    def map_data_type(self, db_type: str, rules: TypeMappingRules) -> DataType
```

### ImportLlmEnhancer

```python
class ImportLlmEnhancer:
    async def enhance_schema(
        self,
        tables: list[TableSchemaRead],
        *,
        generate_aliases: bool = True,
        generate_descriptions: bool = True,
        detect_enums: bool = True,
        suggest_filters: bool = True,
    ) -> EnhancedSchemaResult
```

### ImportConflictResolver

```python
class ImportConflictResolver:
    def detect_conflicts(
        self,
        proposed_classes: list[ProposedClass],
        proposed_properties: list[ProposedProperty],
        existing_classes: list[OntologyClass],
        existing_properties: list[OntologyProperty],
    ) -> list[ImportConflict]
```

## 数据流

```
POST /datasources/{id}/import-preview
    │
    ├── 1. 确保 schema_cache 存在（不存在则触发 introspect_and_cache）
    │
    ├── 2. 读取 schema_cache + 现有 ontology
    │
    ├── 3. 调用 LLM 做 schema 语义增强
    │      ├─ 表/列中文别名
    │      ├─ 一句话描述
    │      ├─ 枚举/状态字段识别
    │      └─ 过滤规则建议
    │
    ├── 4. 应用用户确认后的过滤规则 + 类型映射
    │
    ├── 5. 生成本体导入提案
    │      ├─ proposedClasses（嵌套 properties）
    │      ├─ proposedJoins
    │      └─ conflicts（已存在项清单）
    │
    ▼
返回 ImportPreviewResponse
    │
    ▼
用户确认后
    │
    ▼
POST /datasources/{id}/import
    │
    ├── 6. 按用户确认项批量写入 ontology（PG + Neo4j）
    │
    └── 7. 可选：同步生成 embedding 到 Milvus
```

## API 设计

### 1. 获取智能导入预览

```http
POST /api/v1/datasources/{datasource_id}/import-preview
```

**请求体** `ImportPreviewRequest`：

```json
{
  "rules": {
    "tableFilter": {
      "includeViews": false,
      "includeSystemTables": false,
      "includeTempTables": false,
      "nameBlacklistPatterns": ["^temp_", "_log$", "_backup$"],
      "ownerWhitelist": null
    },
    "typeMappings": {
      "VARCHAR": "STRING",
      "TEXT": "STRING",
      "INT": "INT",
      "BIGINT": "INT",
      "DECIMAL": "DECIMAL",
      "NUMBER(p=0,s=0)": "INT",
      "NUMBER": "DECIMAL",
      "DATETIME": "DATETIME",
      "BOOLEAN": "BOOLEAN"
    },
    "llmEnhanceOptions": {
      "generateAliases": true,
      "generateDescriptions": true,
      "detectEnums": true,
      "suggestFilters": true
    }
  }
}
```

**响应** `ImportPreviewResponse`：

```json
{
  "datasourceId": 3,
  "proposedClasses": [
    {
      "sourceTable": "wms_inventory",
      "className": "wms_inventory",
      "classAlias": "库存",
      "description": "仓库物料库存主数据",
      "properties": [
        {
          "sourceColumn": "quantity",
          "propertyName": "quantity",
          "propertyAlias": "数量",
          "description": "当前库存数量",
          "dataType": "INT",
          "isPrimaryKey": false,
          "isForeignKey": false,
          "enumValues": null
        }
      ],
      "isSelected": true
    }
  ],
  "proposedJoins": [
    {
      "sourceTable": "wms_inventory",
      "sourceColumns": ["material_id"],
      "targetTable": "wms_material",
      "targetColumns": ["id"],
      "joinType": "INNER",
      "relationType": "foreign_key",
      "isSelected": true
    }
  ],
  "conflicts": [
    {
      "type": "class",
      "sourceTable": "wms_inventory",
      "existingId": 12,
      "existingName": "库存",
      "proposedName": "wms_inventory",
      "action": "skip"
    }
  ],
  "filterSuggestions": {
    "recommendedBlacklistPatterns": ["_temp$", "^sys_"],
    "excludedTables": ["wms_operation_log", "tmp_inventory"]
  },
  "llmUsage": {
    "modelName": "gpt-4o-mini",
    "promptTokens": 1200,
    "completionTokens": 800
  }
}
```

### 2. 执行导入

```http
POST /api/v1/datasources/{datasource_id}/import
```

**请求体** `ImportExecuteRequest`：

```json
{
  "confirmedClasses": [
    {
      "sourceTable": "wms_inventory",
      "className": "wms_inventory",
      "classAlias": "库存",
      "description": "仓库物料库存主数据",
      "properties": [
        {
          "sourceColumn": "quantity",
          "propertyName": "quantity",
          "propertyAlias": "数量",
          "description": "当前库存数量",
          "dataType": "INT",
          "isPrimaryKey": false,
          "isForeignKey": false
        }
      ]
    }
  ],
  "confirmedJoins": [
    {
      "sourceTable": "wms_inventory",
      "sourceColumns": ["material_id"],
      "targetTable": "wms_material",
      "targetColumns": ["id"],
      "joinType": "INNER",
      "relationType": "foreign_key"
    }
  ],
  "conflictResolutions": [
    {
      "type": "class",
      "existingId": 12,
      "action": "skip"
    }
  ],
  "syncEmbeddings": true
}
```

**响应** `ImportExecuteResponse`：

```json
{
  "success": true,
  "createdClasses": 5,
  "createdProperties": 42,
  "createdJoins": 8,
  "skippedConflicts": 2,
  "overwrittenConflicts": 0,
  "errors": []
}
```

## 前端设计

### 入口

在 `DatasourcePage` 的数据源表格增加操作列：“智能导入到本体”。

### Wizard 三步

#### Step 1：规则配置

- **表过滤规则**：开关（视图/临时表/系统表）+ 表名黑名单正则 + LLM 建议采纳区。
- **类型映射规则**：数据库类型 → 本体类型表格，支持 `NUMBER` 按精度细分，提供“恢复默认”。

#### Step 2：智能预览

左右分栏：
- 左侧：新增列表（Class / Property / Join），支持搜索、全选、按 schema 分组、折叠/展开、虚拟滚动。
- 右侧：冲突清单，支持按类型筛选、批量处理（跳过/覆盖/重命名）。

#### Step 3：确认与结果

- 汇总卡片：待创建 Class 数 / Property 数 / Join 数 / 冲突处理数。
- 确认导入按钮。
- 导入完成后展示结果与失败详情。

## LLM Prompt 设计

采用单轮结构化 JSON Prompt，输出严格限定 JSON Schema。

### 输入

精简后的 schema 元数据（仅表名、列名、数据类型）。不传真实数据行、不传密码/连接信息。

### System Prompt 要点

```
你是一个数据库 schema 语义理解助手。请根据提供的表结构元数据：
1. 为每个表生成简短中文别名（2-6 字）和一句话业务描述。
2. 为每个列生成中文别名（1-4 字）和简短描述。
3. 识别状态/枚举字段，列出其可能的取值（如无则不填）。
4. 建议应该排除的日志表、临时表、系统表。

输出必须是严格 JSON，符合给定的 JSON Schema，不要任何额外解释。
```

### 输出

```json
{
  "tables": [
    {
      "name": "wms_inventory",
      "alias": "库存",
      "description": "仓库物料库存主数据",
      "columns": [
        {
          "name": "material_id",
          "alias": "物料编号",
          "description": "关联物料主档的外键",
          "enum_values": null
        },
        {
          "name": "status",
          "alias": "状态",
          "description": "库存记录状态",
          "enum_values": ["0", "1"]
        }
      ]
    }
  ],
  "filter_suggestions": {
    "exclude_patterns": ["_log$", "^tmp_", "^temp_"],
    "exclude_tables": ["wms_operation_log"]
  }
}
```

### 成本控制

- 默认走 `model_router_service` 的成本敏感路径，优先轻量模型。
- Token 消耗记录到 `session_token_usage`。
- 表数量 > 50 时按 50 张表一批并行调用。

## 错误处理

| 错误类型 | 处理方式 |
|---|---|
| 数据源连接失败 / schema 未缓存 | 预览接口返回 400/404，提示先测试连接或触发 introspect |
| LLM 输出 JSON 解析失败 / 超时 | 降级为无 LLM 增强的原始提案，记录错误日志 |
| 单条 Class/Property 写入失败 | 记录到 `ImportResult.errors`，继续处理其他项 |
| 冲突项 action 非法 | 执行阶段校验，非法则跳过 |

写入策略：每个 Class 及其 Property 作为独立子事务；Join 在所有 Class 写入完成后批量创建。

## 测试策略

### 单元测试

- `test_import_rule_engine.py`：过滤规则、类型映射（含 Oracle NUMBER 细分）。
- `test_import_conflict_resolver.py`：冲突检测。
- `test_import_llm_enhancer.py`：mock LLM 输出，测试解析、降级、枚举识别。

### 集成测试

- 真实 PostgreSQL + 完整 API 链路。
- 创建 DataSource → 调用 `import-preview` → 调用 `import` → 验证 ontology、Neo4j、Milvus 状态。
- 测试重复导入幂等性。

### E2E 测试

- Playwright：从数据源页点击“智能导入到本体”，完成三步 Wizard，验证 ontology 页面出现新 Class。

### 覆盖率目标

≥ 80%。

## 默认数据类型映射规则（附录）

| 数据库类型 | 本体类型 |
|---|---|
| `VARCHAR`, `TEXT`, `CHAR`, `NVARCHAR`, `JSON` | `STRING` |
| `INT`, `BIGINT`, `SMALLINT`, `TINYINT`, `SERIAL` | `INT` |
| `DECIMAL`, `NUMERIC`, `FLOAT`, `DOUBLE`, `REAL`, `MONEY` | `DECIMAL` |
| `DATETIME`, `TIMESTAMP`, `DATE`, `TIME` | `DATETIME` |
| `BOOLEAN`, `BIT`, `TINYINT(1)` | `BOOLEAN` |
| Oracle `NUMBER(p=0,s=0)` | `INT` |
| Oracle `NUMBER`（其他精度） | `DECIMAL` |

## 默认表过滤规则（附录）

默认排除：
- 视图（`information_schema` 中 `table_type = 'VIEW'`）
- 临时表（名含 `tmp_`, `temp_`, `#` 前缀）
- 系统表（MySQL: `information_schema`, `mysql`, `performance_schema`, `sys`；PG: `pg_catalog`, `information_schema`；Oracle: `SYS`, `SYSTEM` 等）
- 日志/备份表（名匹配 `_log$`, `_backup$`, `_history$`）
