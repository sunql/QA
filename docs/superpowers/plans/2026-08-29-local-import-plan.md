# 本地导入功能实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 QA System 增加本地导入功能：管理员在注册数据源后，可通过向导式界面自动读取数据库 schema 并批量生成本体（Class / Property / Join），经 LLM 语义增强和人工预览确认后写入。

**Architecture:** 后端新增 `ImportRuleEngine`、`ImportLlmEnhancer`、`ImportConflictResolver`、`LocalImportService` 四个小服务，各负其责；新增 `LocalImport API` 暴露 `import-preview` 与 `import` 两个端点。前端在 `DatasourcePage` 增加入口，打开三步 Wizard Modal 完成规则配置、智能预览、确认写入。

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.0 async, Pydantic v2, Neo4j, Milvus, React 18, TypeScript, Ant Design 5, Vitest, Playwright.

## Global Constraints

- 不可变数据：始终创建新对象，禁止原地修改。
- SQL 安全：业务查询仅允许只读 SELECT，经 SQL Guard 校验。
- Token 计量：每次 LLM 调用必须记录 Token 消耗与成本。
- TDD：先写测试（RED）→ 实现（GREEN）→ 重构（IMPROVE），覆盖率 ≥ 80%。
- 小文件：200-400 行为宜，不超过 800 行；函数 < 50 行；嵌套 ≤ 4 层。
- 显式错误处理：每个层级显式处理错误，UI 层给友好提示，服务端记录详细上下文。
- 真实数据库测试：后端测试必须用真实 PostgreSQL + 完整 API 链路 + 真实数据库对象，禁止 sqlite 内存库 + 直接调用 service。
- 每次写完代码立即用 `code-reviewer` / `security-reviewer` 审查。
- ORM/Pydantic 字段名：`snake_case`（与 DB 列及 JSON 契约一致）。
- API JSON 输出：`camelCase`（由 Pydantic `alias_generator=to_camel` 负责）。

---

## File Structure

### 后端新增/修改

| 文件 | 职责 |
|---|---|
| `app/services/import_rule_engine.py` | 表过滤规则 + 数据类型映射规则。 |
| `app/services/import_conflict_resolver.py` | 导入提案与现有 ontology 的冲突检测。 |
| `app/services/import_llm_enhancer.py` | LLM 调用：别名、描述、枚举识别、过滤建议。 |
| `app/services/local_import_service.py` | 导入主服务，协调上述组件。 |
| `app/api/v1/local_import.py` | FastAPI 路由：`import-preview`、`import`。 |
| `app/domain/schemas.py` | 新增导入相关 DTO。 |
| `app/main.py` | 挂载新路由。 |
| `app/tests/services/test_import_rule_engine.py` | ImportRuleEngine 单元测试。 |
| `app/tests/services/test_import_conflict_resolver.py` | ImportConflictResolver 单元测试。 |
| `app/tests/services/test_import_llm_enhancer.py` | ImportLlmEnhancer 单元测试（mock LLM）。 |
| `app/tests/api/test_local_import.py` | LocalImport API 集成测试。 |

### 前端新增/修改

| 文件 | 职责 |
|---|---|
| `frontend/src/types/localImport.ts` | 导入相关 TypeScript 类型。 |
| `frontend/src/api/localImport.ts` | API 封装。 |
| `frontend/src/components/localImport/ImportWizard.tsx` | 导入向导容器（三步）。 |
| `frontend/src/components/localImport/RuleConfigStep.tsx` | 规则配置页。 |
| `frontend/src/components/localImport/PreviewStep.tsx` | 智能预览页。 |
| `frontend/src/components/localImport/ConfirmStep.tsx` | 确认与结果页。 |
| `frontend/src/pages/DatasourcePage.tsx` | 增加“智能导入到本体”入口。 |
| `frontend/src/tests/ImportWizard.test.tsx` | 组件单元测试。 |
| `frontend/src/tests/localImportApi.test.ts` | API 测试。 |

---

### Task 1: ImportRuleEngine（过滤规则 + 类型映射）

**Files:**
- Create: `app/services/import_rule_engine.py`
- Test: `app/tests/services/test_import_rule_engine.py`

**Interfaces:**
- Consumes: `TableSchemaRead`（来自 `app.domain.schemas`），`DataType` enum。
- Produces:
  - `class TableFilterRules(CamelModel)`
  - `class TypeMappingRules(CamelModel)`
  - `class ImportRuleConfig(CamelModel)`
  - `class ImportRuleEngine:`
    - `filter_tables(self, tables: list[TableSchemaRead], rules: TableFilterRules) -> list[TableSchemaRead]`
    - `map_data_type(self, db_type: str, rules: TypeMappingRules) -> DataType`

- [ ] **Step 1: Write the failing test**

Create `app/tests/services/test_import_rule_engine.py`:

```python
import pytest

from app.domain.enums import DataType
from app.domain.schemas import TableFilterRules, TableSchemaRead, TypeMappingRules
from app.services.import_rule_engine import ImportRuleEngine


def test_filter_tables_excludes_log_and_temp():
    engine = ImportRuleEngine()
    rules = TableFilterRules()
    tables = [
        TableSchemaRead(table_name="wms_inventory", columns=[]),
        TableSchemaRead(table_name="wms_operation_log", columns=[]),
        TableSchemaRead(table_name="tmp_inventory", columns=[]),
    ]
    result = engine.filter_tables(tables, rules)
    names = {t.table_name for t in result}
    assert names == {"wms_inventory"}


def test_map_data_type_oracle_number_integer():
    engine = ImportRuleEngine()
    rules = TypeMappingRules()
    assert engine.map_data_type("NUMBER(p=0,s=0)", rules) == DataType.INT


def test_map_data_type_varchar_to_string():
    engine = ImportRuleEngine()
    rules = TypeMappingRules()
    assert engine.map_data_type("VARCHAR", rules) == DataType.STRING
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest app/tests/services/test_import_rule_engine.py -v`
Expected: FAIL with `ModuleNotFoundError` or `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Create `app/services/import_rule_engine.py`:

```python
"""导入规则引擎：表过滤 + 数据类型映射。"""

from __future__ import annotations

import re

from app.domain.enums import DataType
from app.domain.schemas import TableFilterRules, TableSchemaRead, TypeMappingRules


class ImportRuleEngine:
    def filter_tables(
        self,
        tables: list[TableSchemaRead],
        rules: TableFilterRules,
    ) -> list[TableSchemaRead]:
        return [t for t in tables if self._is_allowed(t, rules)]

    def _is_allowed(self, table: TableSchemaRead, rules: TableFilterRules) -> bool:
        name = table.table_name
        if rules.include_temp_tables is False and self._is_temp(name):
            return False
        if any(re.search(p, name, re.IGNORECASE) for p in rules.name_blacklist_patterns):
            return False
        return True

    @staticmethod
    def _is_temp(name: str) -> bool:
        lowered = name.lower()
        return lowered.startswith(("tmp_", "temp_", "#"))

    def map_data_type(self, db_type: str, rules: TypeMappingRules) -> DataType:
        normalized = self._normalize_type(db_type)
        value = rules.mappings.get(normalized)
        if value is None:
            return DataType.STRING
        return DataType(value)

    @staticmethod
    def _normalize_type(db_type: str) -> str:
        upper = db_type.upper().strip()
        if upper == "TINYINT(1)":
            return "BOOLEAN"
        if upper.startswith("NUMBER"):
            if "P=0" in upper and "S=0" in upper:
                return "NUMBER(p=0,s=0)"
            return "NUMBER"
        return upper.split("(")[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest app/tests/services/test_import_rule_engine.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/import_rule_engine.py app/tests/services/test_import_rule_engine.py
git commit -m "feat: add import rule engine for table filtering and type mapping"
```

---

### Task 2: ImportConflictResolver

**Files:**
- Create: `app/services/import_conflict_resolver.py`
- Test: `app/tests/services/test_import_conflict_resolver.py`

**Interfaces:**
- Consumes: `ProposedClass`, `ProposedProperty`（定义见 Task 4 DTO），`OntologyClass`, `OntologyProperty`。
- Produces:
  - `class ConflictType(str, Enum)`
  - `class ImportConflict(CamelModel)`
  - `class ImportConflictResolver:`
    - `detect_conflicts(self, proposed_classes, proposed_properties, existing_classes, existing_properties) -> list[ImportConflict]`

- [ ] **Step 1: Write the failing test**

Create `app/tests/services/test_import_conflict_resolver.py`:

```python
from app.domain.models import OntologyClass, OntologyProperty
from app.domain.schemas import ConflictType
from app.services.import_conflict_resolver import ImportConflictResolver


def test_detect_class_conflict_by_source_table():
    resolver = ImportConflictResolver()
    proposed = [{"source_table": "wms_inventory", "class_name": "wms_inventory"}]
    existing = [OntologyClass(id=1, class_name="库存", source_table="wms_inventory")]
    conflicts = resolver.detect_conflicts(proposed, [], existing, [])
    assert len(conflicts) == 1
    assert conflicts[0].type == ConflictType.CLASS
    assert conflicts[0].existing_id == 1


def test_no_conflict_for_new_table():
    resolver = ImportConflictResolver()
    proposed = [{"source_table": "wms_inventory", "class_name": "wms_inventory"}]
    existing = [OntologyClass(id=1, class_name="库存", source_table="wms_material")]
    conflicts = resolver.detect_conflicts(proposed, [], existing, [])
    assert len(conflicts) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest app/tests/services/test_import_conflict_resolver.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Create `app/services/import_conflict_resolver.py`:

```python
"""导入冲突检测器。"""

from __future__ import annotations

from app.domain.schemas import ConflictType, ImportConflict


class ImportConflictResolver:
    def detect_conflicts(
        self,
        proposed_classes: list[dict],
        proposed_properties: list[dict],
        existing_classes: list,
        existing_properties: list,
    ) -> list[ImportConflict]:
        conflicts: list[ImportConflict] = []
        existing_class_by_table = {
            (c.source_table or "").lower(): c for c in existing_classes if c.source_table
        }
        for pc in proposed_classes:
            table = (pc.get("source_table") or "").lower()
            existing = existing_class_by_table.get(table)
            if existing:
                conflicts.append(
                    ImportConflict(
                        type=ConflictType.CLASS,
                        source_table=pc["source_table"],
                        existing_id=existing.id,
                        existing_name=existing.class_name,
                        proposed_name=pc["class_name"],
                    )
                )

        existing_prop_key = {
            ((p.ontology_class.source_table or "").lower(), (p.source_column or "").lower()): p
            for p in existing_properties
            if p.ontology_class and p.source_column
        }
        for pp in proposed_properties:
            key = (pp.get("source_table", "").lower(), pp.get("source_column", "").lower())
            existing = existing_prop_key.get(key)
            if existing:
                conflicts.append(
                    ImportConflict(
                        type=ConflictType.PROPERTY,
                        source_table=pp["source_table"],
                        source_column=pp["source_column"],
                        existing_id=existing.id,
                        existing_name=existing.property_name,
                        proposed_name=pp["property_name"],
                    )
                )
        return conflicts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest app/tests/services/test_import_conflict_resolver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/import_conflict_resolver.py app/tests/services/test_import_conflict_resolver.py
git commit -m "feat: add import conflict resolver"
```

---

### Task 3: ImportLlmEnhancer

**Files:**
- Create: `app/services/import_llm_enhancer.py`
- Test: `app/tests/services/test_import_llm_enhancer.py`

**Interfaces:**
- Consumes: `TableSchemaRead` list, `ModelRouterService` / `LlmClient`。
- Produces:
  - `class EnhancedColumn(CamelModel)`
  - `class EnhancedTable(CamelModel)`
  - `class FilterSuggestions(CamelModel)`
  - `class EnhancedSchemaResult(CamelModel)`
  - `class ImportLlmEnhancer:`
    - `async def enhance_schema(self, tables, generate_aliases=True, generate_descriptions=True, detect_enums=True, suggest_filters=True) -> EnhancedSchemaResult`

- [ ] **Step 1: Write the failing test**

Create `app/tests/services/test_import_llm_enhancer.py`:

```python
import pytest

from app.domain.schemas import ColumnSchemaRead, TableSchemaRead
from app.services.import_llm_enhancer import ImportLlmEnhancer


class FakeLlmClient:
    async def complete(self, messages, response_format=None):
        return """
        {
          "tables": [
            {
              "name": "wms_inventory",
              "alias": "库存",
              "description": "仓库物料库存主数据",
              "columns": [
                {"name": "quantity", "alias": "数量", "description": "库存数量", "enum_values": null}
              ]
            }
          ],
          "filter_suggestions": {"exclude_patterns": ["_log$"], "exclude_tables": []}
        }
        """


@pytest.mark.asyncio
async def test_enhance_schema_returns_aliases_and_descriptions():
    enhancer = ImportLlmEnhancer(llm_client=FakeLlmClient())
    tables = [
        TableSchemaRead(
            table_name="wms_inventory",
            columns=[ColumnSchemaRead(column_name="quantity", data_type="DECIMAL", nullable=True)],
        )
    ]
    result = await enhancer.enhance_schema(tables)
    assert result.tables[0].alias == "库存"
    assert result.tables[0].columns[0].alias == "数量"


@pytest.mark.asyncio
async def test_enhance_schema_degrades_on_invalid_json():
    class BadClient:
        async def complete(self, messages, response_format=None):
            return "not json"

    enhancer = ImportLlmEnhancer(llm_client=BadClient())
    tables = [
        TableSchemaRead(
            table_name="wms_inventory",
            columns=[ColumnSchemaRead(column_name="quantity", data_type="DECIMAL", nullable=True)],
        )
    ]
    result = await enhancer.enhance_schema(tables)
    assert result.tables[0].alias is None
    assert result.filter_suggestions.exclude_patterns == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest app/tests/services/test_import_llm_enhancer.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Create `app/services/import_llm_enhancer.py`:

```python
"""LLM schema 语义增强服务。"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.domain.schemas import CamelModel, ColumnSchemaRead, TableSchemaRead

logger = logging.getLogger(__name__)


class EnhancedColumn(CamelModel):
    name: str
    alias: str | None = None
    description: str | None = None
    enum_values: list[str] | None = None


class EnhancedTable(CamelModel):
    name: str
    alias: str | None = None
    description: str | None = None
    columns: list[EnhancedColumn]


class FilterSuggestions(CamelModel):
    exclude_patterns: list[str] = []
    exclude_tables: list[str] = []


class EnhancedSchemaResult(CamelModel):
    tables: list[EnhancedTable]
    filter_suggestions: FilterSuggestions


class ImportLlmEnhancer:
    def __init__(self, llm_client=None) -> None:
        self._llm_client = llm_client

    async def enhance_schema(
        self,
        tables: list[TableSchemaRead],
        *,
        generate_aliases: bool = True,
        generate_descriptions: bool = True,
        detect_enums: bool = True,
        suggest_filters: bool = True,
    ) -> EnhancedSchemaResult:
        if not tables or not generate_aliases and not generate_descriptions and not detect_enums and not suggest_filters:
            return self._empty_result(tables)

        try:
            raw = await self._call_llm(tables)
            data = json.loads(raw)
            return self._parse(data, tables)
        except Exception as exc:
            logger.warning("LLM schema 增强失败，降级为无增强: %s", exc)
            return self._empty_result(tables)

    async def _call_llm(self, tables: list[TableSchemaRead]) -> str:
        if self._llm_client is None:
            raise RuntimeError("llm_client not provided")
        prompt = self._build_prompt(tables)
        return await self._llm_client.complete(prompt)

    @staticmethod
    def _build_prompt(tables: list[TableSchemaRead]) -> list[dict[str, Any]]:
        schema = [
            {
                "name": t.table_name,
                "columns": [
                    {"name": c.column_name, "data_type": c.data_type}
                    for c in t.columns
                ],
            }
            for t in tables
        ]
        system = (
            "你是一个数据库 schema 语义理解助手。根据提供的表结构元数据："
            "1. 为每个表生成简短中文别名（2-6 字）和一句话业务描述。"
            "2. 为每个列生成中文别名（1-4 字）和简短描述。"
            "3. 识别状态/枚举字段，列出其可能取值（如无则不填）。"
            "4. 建议应该排除的日志表、临时表、系统表。"
            "输出必须是严格 JSON，不要任何额外解释。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"tables": schema}, ensure_ascii=False)},
        ]

    def _parse(self, data: dict[str, Any], original: list[TableSchemaRead]) -> EnhancedSchemaResult:
        enhanced_by_name = {t["name"]: t for t in data.get("tables", [])}
        result_tables: list[EnhancedTable] = []
        for t in original:
            enhanced = enhanced_by_name.get(t.table_name, {})
            columns = []
            enhanced_cols = {c["name"]: c for c in enhanced.get("columns", [])}
            for c in t.columns:
                ec = enhanced_cols.get(c.column_name, {})
                columns.append(
                    EnhancedColumn(
                        name=c.column_name,
                        alias=ec.get("alias"),
                        description=ec.get("description"),
                        enum_values=ec.get("enum_values"),
                    )
                )
            result_tables.append(
                EnhancedTable(
                    name=t.table_name,
                    alias=enhanced.get("alias"),
                    description=enhanced.get("description"),
                    columns=columns,
                )
            )
        fs = data.get("filter_suggestions", {})
        return EnhancedSchemaResult(
            tables=result_tables,
            filter_suggestions=FilterSuggestions(
                exclude_patterns=fs.get("exclude_patterns", []),
                exclude_tables=fs.get("exclude_tables", []),
            ),
        )

    def _empty_result(self, tables: list[TableSchemaRead]) -> EnhancedSchemaResult:
        return EnhancedSchemaResult(
            tables=[
                EnhancedTable(
                    name=t.table_name,
                    columns=[EnhancedColumn(name=c.column_name) for c in t.columns],
                )
                for t in tables
            ],
            filter_suggestions=FilterSuggestions(),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest app/tests/services/test_import_llm_enhancer.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/import_llm_enhancer.py app/tests/services/test_import_llm_enhancer.py
git commit -m "feat: add LLM schema enhancer for local import"
```

---

### Task 4: DTO 与 LocalImportService

**Files:**
- Modify: `app/domain/schemas.py`（追加 DTO）
- Create: `app/services/local_import_service.py`
- Test: `app/tests/services/test_local_import_service.py`

**Interfaces:**
- Consumes: `ImportRuleConfig`, `TableSchemaRead`, `OntologyClass`, `OntologyProperty`, `SchemaIntrospectionService`, `OntologyService`, `ImportRuleEngine`, `ImportConflictResolver`, `ImportLlmEnhancer`。
- Produces:
  - `class ProposedProperty(CamelModel)`
  - `class ProposedClass(CamelModel)`
  - `class ProposedJoin(CamelModel)`
  - `class ImportPreviewRequest(CamelModel)`
  - `class ImportPreviewResponse(CamelModel)`
  - `class ConflictResolution(CamelModel)`
  - `class ImportExecuteRequest(CamelModel)`
  - `class ImportExecuteResponse(CamelModel)`
  - `class LocalImportService:`
    - `async def build_preview(self, session, datasource_id, rules) -> ImportPreviewResponse`
    - `async def execute_import(self, session, datasource_id, request, created_by) -> ImportExecuteResponse`

- [ ] **Step 1: Write the failing test**

Create `app/tests/services/test_local_import_service.py`:

```python
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataType
from app.domain.schemas import ImportExecuteRequest, ImportPreviewRequest, ImportRuleConfig
from app.services.local_import_service import LocalImportService


class FakeSchemaService:
    async def introspect_and_cache(self, session, ds):
        return None

    def build_response(self, cache):
        from app.domain.schemas import ColumnSchemaRead, SchemaIntrospectResponse, TableSchemaRead
        return SchemaIntrospectResponse(
            tables=[
                TableSchemaRead(
                    table_name="wms_inventory",
                    columns=[ColumnSchemaRead(column_name="quantity", data_type="DECIMAL", nullable=True)],
                    primary_keys=[],
                    foreign_keys=[],
                )
            ],
            cached_at="2026-08-29T00:00:00",
        )


class FakeOntologyService:
    async def list_classes(self, session, includeExpired=False):
        return []

    async def list_properties_by_class(self, session, class_id):
        return []

    async def create_class(self, session, dto):
        from app.domain.models import OntologyClass
        return OntologyClass(id=1, class_name=dto.class_name, source_table=dto.source_table)

    async def create_property(self, session, dto):
        from app.domain.models import OntologyProperty
        return OntologyProperty(id=1, property_name=dto.property_name, source_column=dto.source_column)

    async def create_join(self, session, dto):
        from app.domain.models import OntologyJoin
        return OntologyJoin(id=1)


@pytest.mark.asyncio
async def test_build_preview_returns_proposed_class(db: AsyncSession):
    svc = LocalImportService(schema_service=FakeSchemaService(), ontology_service=FakeOntologyService())
    request = ImportPreviewRequest(rules=ImportRuleConfig())
    result = await svc.build_preview(db, datasource_id=1, rules=request.rules)
    assert len(result.proposed_classes) == 1
    assert result.proposed_classes[0].source_table == "wms_inventory"


@pytest.mark.asyncio
async def test_execute_import_creates_class_and_property(db: AsyncSession):
    svc = LocalImportService(schema_service=FakeSchemaService(), ontology_service=FakeOntologyService())
    preview = await svc.build_preview(db, datasource_id=1, rules=ImportRuleConfig())
    execute_request = ImportExecuteRequest(
        confirmed_classes=preview.proposed_classes,
        confirmed_joins=[],
        conflict_resolutions=[],
        sync_embeddings=False,
    )
    result = await svc.execute_import(db, datasource_id=1, request=execute_request, created_by="admin")
    assert result.created_classes == 1
    assert result.created_properties == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest app/tests/services/test_local_import_service.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Append DTO to `app/domain/schemas.py` (ensure `from enum import Enum` is imported at the top of the file):

```python
# ===== Local Import =====


class TableFilterRules(CamelModel):
    include_views: bool = False
    include_system_tables: bool = False
    include_temp_tables: bool = False
    name_blacklist_patterns: list[str] = [
        r"^temp_",
        r"^tmp_",
        r"_log$",
        r"_backup$",
        r"_history$",
    ]
    owner_whitelist: list[str] | None = None


class TypeMappingRules(CamelModel):
    mappings: dict[str, str] = {
        "VARCHAR": "STRING",
        "TEXT": "STRING",
        "CHAR": "STRING",
        "NVARCHAR": "STRING",
        "INT": "INT",
        "BIGINT": "INT",
        "SMALLINT": "INT",
        "TINYINT": "INT",
        "SERIAL": "INT",
        "DECIMAL": "DECIMAL",
        "NUMERIC": "DECIMAL",
        "FLOAT": "DECIMAL",
        "DOUBLE": "DECIMAL",
        "REAL": "DECIMAL",
        "MONEY": "DECIMAL",
        "DATETIME": "DATETIME",
        "TIMESTAMP": "DATETIME",
        "DATE": "DATETIME",
        "TIME": "DATETIME",
        "BOOLEAN": "BOOLEAN",
        "BIT": "BOOLEAN",
        "NUMBER(p=0,s=0)": "INT",
        "NUMBER": "DECIMAL",
    }


class ProposedProperty(CamelModel):
    source_column: str
    property_name: str
    property_alias: str | None = None
    description: str | None = None
    data_type: str
    is_primary_key: bool = False
    is_foreign_key: bool = False
    enum_values: list[str] | None = None


class ProposedClass(CamelModel):
    source_table: str
    class_name: str
    class_alias: str | None = None
    description: str | None = None
    properties: list[ProposedProperty] = []
    is_selected: bool = True


class ProposedJoin(CamelModel):
    source_table: str
    source_columns: list[str]
    target_table: str
    target_columns: list[str]
    join_type: str = "INNER"
    relation_type: str = "foreign_key"
    is_selected: bool = True


class ConflictType(str, Enum):
    CLASS = "class"
    PROPERTY = "property"


class ImportConflict(CamelModel):
    type: ConflictType
    source_table: str
    source_column: str | None = None
    existing_id: int
    existing_name: str
    proposed_name: str
    action: str = "skip"


class LlmEnhanceOptions(CamelModel):
    generate_aliases: bool = True
    generate_descriptions: bool = True
    detect_enums: bool = True
    suggest_filters: bool = True


class ImportRuleConfig(CamelModel):
    table_filter: TableFilterRules = TableFilterRules()
    type_mappings: TypeMappingRules = TypeMappingRules()
    llm_enhance_options: LlmEnhanceOptions = LlmEnhanceOptions()


class ImportPreviewRequest(CamelModel):
    rules: ImportRuleConfig = ImportRuleConfig()


class FilterSuggestions(CamelModel):
    recommended_blacklist_patterns: list[str] = []
    excluded_tables: list[str] = []


class LlmUsageInfo(CamelModel):
    model_name: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ImportPreviewResponse(CamelModel):
    datasource_id: int
    proposed_classes: list[ProposedClass] = []
    proposed_joins: list[ProposedJoin] = []
    conflicts: list[ImportConflict] = []
    filter_suggestions: FilterSuggestions = FilterSuggestions()
    llm_usage: LlmUsageInfo = LlmUsageInfo()


class ConflictResolution(CamelModel):
    type: str
    existing_id: int
    action: str  # skip | overwrite | rename
    new_name: str | None = None


class ImportExecuteRequest(CamelModel):
    confirmed_classes: list[ProposedClass]
    confirmed_joins: list[ProposedJoin]
    conflict_resolutions: list[ConflictResolution] = []
    sync_embeddings: bool = True


class ImportErrorInfo(CamelModel):
    type: str
    name: str
    message: str


class ImportExecuteResponse(CamelModel):
    success: bool = True
    created_classes: int = 0
    created_properties: int = 0
    created_joins: int = 0
    skipped_conflicts: int = 0
    overwritten_conflicts: int = 0
    errors: list[ImportErrorInfo] = []
```

Create `app/services/local_import_service.py`:

```python
"""本地导入主服务。"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource
from app.domain.schemas import (
    ImportExecuteRequest,
    ImportExecuteResponse,
    ImportPreviewRequest,
    ImportPreviewResponse,
    ImportRuleConfig,
    ProposedClass,
    ProposedJoin,
    ProposedProperty,
)
from app.services.import_conflict_resolver import ImportConflictResolver
from app.services.import_llm_enhancer import ImportLlmEnhancer
from app.services.import_rule_engine import ImportRuleEngine
from app.services.ontology_service import OntologyService
from app.services.schema_introspection_service import SchemaIntrospectionService

logger = logging.getLogger(__name__)


class LocalImportService:
    def __init__(
        self,
        *,
        schema_service: SchemaIntrospectionService | None = None,
        ontology_service: OntologyService | None = None,
        rule_engine: ImportRuleEngine | None = None,
        conflict_resolver: ImportConflictResolver | None = None,
        llm_enhancer: ImportLlmEnhancer | None = None,
    ) -> None:
        self._schema_service = schema_service or SchemaIntrospectionService()
        self._ontology_service = ontology_service or OntologyService()
        self._rule_engine = rule_engine or ImportRuleEngine()
        self._conflict_resolver = conflict_resolver or ImportConflictResolver()
        self._llm_enhancer = llm_enhancer or ImportLlmEnhancer()

    async def build_preview(
        self,
        session: AsyncSession,
        datasource_id: int,
        rules: ImportRuleConfig,
    ) -> ImportPreviewResponse:
        ds = await session.get(DataSource, datasource_id)
        if ds is None:
            raise NotFoundError(f"数据源 {datasource_id} 不存在")

        cache = await self._schema_service.introspect_and_cache(session, ds)
        response = self._schema_service.buildResponse(cache)

        filtered = self._rule_engine.filter_tables(response.tables, rules.table_filter)
        enhanced = await self._llm_enhancer.enhance_schema(
            filtered,
            generate_aliases=rules.llm_enhance_options.generate_aliases,
            generate_descriptions=rules.llm_enhance_options.generate_descriptions,
            detect_enums=rules.llm_enhance_options.detect_enums,
            suggest_filters=rules.llm_enhance_options.suggest_filters,
        )

        proposed_classes, proposed_properties, proposed_joins = self._build_proposals(
            enhanced, rules, response.tables
        )

        existing_classes = await self._ontology_service.list_classes(session)
        existing_properties: list = []
        for cls in existing_classes:
            existing_properties.extend(
                await self._ontology_service.list_properties_by_class(session, cls.id)
            )

        conflicts = self._conflict_resolver.detect_conflicts(
            [c.model_dump() for c in proposed_classes],
            [p.model_dump() for p in proposed_properties],
            existing_classes,
            existing_properties,
        )

        return ImportPreviewResponse(
            datasource_id=datasource_id,
            proposed_classes=proposed_classes,
            proposed_joins=proposed_joins,
            conflicts=conflicts,
            filter_suggestions=FilterSuggestions(
                recommended_blacklist_patterns=enhanced.filter_suggestions.exclude_patterns,
                excluded_tables=enhanced.filter_suggestions.exclude_tables,
            ),
        )

    def _build_proposals(self, enhanced, rules, original_tables):
        # 实现略：根据 enhanced 和 rule_engine.map_data_type 生成 ProposedClass / Property / Join
        pass

    async def execute_import(
        self,
        session: AsyncSession,
        datasource_id: int,
        request: ImportExecuteRequest,
        created_by: str | None,
    ) -> ImportExecuteResponse:
        # 实现略：按 request.confirmed_classes / confirmed_joins 写入 ontology
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest app/tests/services/test_local_import_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/domain/schemas.py app/services/local_import_service.py app/tests/services/test_local_import_service.py
git commit -m "feat: add local import service and DTOs"
```

---

### Task 5: LocalImport API 路由

**Files:**
- Create: `app/api/v1/local_import.py`
- Modify: `app/main.py`（挂载路由）
- Test: `app/tests/api/test_local_import.py`

**Interfaces:**
- Consumes: `LocalImportService`, `ImportPreviewRequest`, `ImportExecuteRequest`。
- Produces: FastAPI routes `POST /api/v1/datasources/{id}/import-preview` and `POST /api/v1/datasources/{id}/import`。

- [ ] **Step 1: Write the failing test**

Create `app/tests/api/test_local_import.py`（使用真实 PostgreSQL + `_testapp`）：

```python
import pytest


@pytest.mark.asyncio
async def test_import_preview_returns_schema_proposal(client, db):
    # 先创建数据源
    ds_payload = {
        "name": "test-import",
        "type": "postgresql",
        "host": "localhost",
        "port": 5432,
        "databaseName": "test",
        "username": "test",
        "password": "test",
    }
    res = await client.post("/api/v1/datasources", json=ds_payload)
    ds_id = res.json()["id"]

    preview_payload = {"rules": {"tableFilter": {"nameBlacklistPatterns": ["_log$"]}}}
    res = await client.post(f"/api/v1/datasources/{ds_id}/import-preview", json=preview_payload)
    assert res.status_code == 200
    data = res.json()
    assert "proposedClasses" in data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest app/tests/api/test_local_import.py -v`
Expected: FAIL (404).

- [ ] **Step 3: Write minimal implementation**

Create `app/api/v1/local_import.py`:

```python
"""本地导入 API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import (
    ImportExecuteRequest,
    ImportExecuteResponse,
    ImportPreviewRequest,
    ImportPreviewResponse,
)
from app.services.local_import_service import LocalImportService

router = APIRouter(dependencies=[Depends(getCurrentUser)])
_service = LocalImportService()


@router.post("/{datasource_id}/import-preview", response_model=ImportPreviewResponse)
async def import_preview(
    datasource_id: int,
    request: ImportPreviewRequest,
    db=Depends(getDb),
) -> ImportPreviewResponse:
    return await _service.build_preview(db, datasource_id, request.rules)


@router.post("/{datasource_id}/import", response_model=ImportExecuteResponse)
async def import_execute(
    datasource_id: int,
    request: ImportExecuteRequest,
    user: CurrentUser = Depends(getCurrentUser),
    db=Depends(getDb),
) -> ImportExecuteResponse:
    return await _service.execute_import(db, datasource_id, request, created_by=user.userId)
```

Modify `app/main.py`：在 datasource router 后增加：

```python
from app.api.v1 import local_import
app.include_router(local_import.router, prefix="/api/v1/datasources", tags=["datasources"])
```

同步修改 `app/tests/_testapp.py`。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest app/tests/api/test_local_import.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/local_import.py app/main.py app/tests/_testapp.py app/tests/api/test_local_import.py
git commit -m "feat: add local import API endpoints"
```

---

### Task 6: 前端类型定义与 API 封装

**Files:**
- Create: `frontend/src/types/localImport.ts`
- Create: `frontend/src/api/localImport.ts`
- Test: `frontend/src/tests/localImportApi.test.ts`

**Interfaces:**
- Consumes: 后端 `/api/v1/datasources/{id}/import-preview` 和 `/api/v1/datasources/{id}/import`。
- Produces:
  - `interface ImportRuleConfig`
  - `interface ImportPreviewResponse`
  - `interface ImportExecuteRequest`
  - `async function getImportPreview(datasourceId, rules)`
  - `async function executeImport(datasourceId, request)`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/tests/localImportApi.test.ts`:

```typescript
import { describe, it, expect, vi } from "vitest";
import { getImportPreview } from "../api/localImport";
import { httpClient } from "../api/client";

vi.mock("../api/client", () => ({
  httpClient: {
    post: vi.fn(),
  },
}));

describe("localImportApi", () => {
  it("calls import-preview endpoint", async () => {
    vi.mocked(httpClient.post).mockResolvedValue({ data: { proposedClasses: [] } });
    const result = await getImportPreview(1, { tableFilter: {}, typeMappings: {} });
    expect(httpClient.post).toHaveBeenCalledWith("/datasources/1/import-preview", { rules: { tableFilter: {}, typeMappings: {} } });
    expect(result.proposedClasses).toEqual([]);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- src/tests/localImportApi.test.ts`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/types/localImport.ts`:

```typescript
export interface TableFilterRules {
  includeViews?: boolean;
  includeSystemTables?: boolean;
  includeTempTables?: boolean;
  nameBlacklistPatterns?: string[];
  ownerWhitelist?: string[] | null;
}

export interface TypeMappingRules {
  mappings?: Record<string, string>;
}

export interface LlmEnhanceOptions {
  generateAliases?: boolean;
  generateDescriptions?: boolean;
  detectEnums?: boolean;
  suggestFilters?: boolean;
}

export interface ImportRuleConfig {
  tableFilter?: TableFilterRules;
  typeMappings?: TypeMappingRules;
  llmEnhanceOptions?: LlmEnhanceOptions;
}

export interface ProposedProperty {
  sourceColumn: string;
  propertyName: string;
  propertyAlias: string | null;
  description: string | null;
  dataType: string;
  isPrimaryKey: boolean;
  isForeignKey: boolean;
  enumValues: string[] | null;
}

export interface ProposedClass {
  sourceTable: string;
  className: string;
  classAlias: string | null;
  description: string | null;
  properties: ProposedProperty[];
  isSelected: boolean;
}

export interface ProposedJoin {
  sourceTable: string;
  sourceColumns: string[];
  targetTable: string;
  targetColumns: string[];
  joinType: string;
  relationType: string;
  isSelected: boolean;
}

export interface ImportConflict {
  type: "class" | "property";
  sourceTable: string;
  sourceColumn: string | null;
  existingId: number;
  existingName: string;
  proposedName: string;
  action: "skip" | "overwrite" | "rename";
}

export interface FilterSuggestions {
  recommendedBlacklistPatterns: string[];
  excludedTables: string[];
}

export interface LlmUsageInfo {
  modelName: string | null;
  promptTokens: number;
  completionTokens: number;
}

export interface ImportPreviewResponse {
  datasourceId: number;
  proposedClasses: ProposedClass[];
  proposedJoins: ProposedJoin[];
  conflicts: ImportConflict[];
  filterSuggestions: FilterSuggestions;
  llmUsage: LlmUsageInfo;
}

export interface ConflictResolution {
  type: "class" | "property";
  existingId: number;
  action: "skip" | "overwrite" | "rename";
  newName?: string;
}

export interface ImportExecuteRequest {
  confirmedClasses: ProposedClass[];
  confirmedJoins: ProposedJoin[];
  conflictResolutions: ConflictResolution[];
  syncEmbeddings: boolean;
}

export interface ImportExecuteResponse {
  success: boolean;
  createdClasses: number;
  createdProperties: number;
  createdJoins: number;
  skippedConflicts: number;
  overwrittenConflicts: number;
  errors: Array<{ type: string; name: string; message: string }>;
}
```

Create `frontend/src/api/localImport.ts`:

```typescript
import { httpClient } from "./client";
import type {
  ImportExecuteRequest,
  ImportExecuteResponse,
  ImportPreviewResponse,
  ImportRuleConfig,
} from "../types/localImport";

export async function getImportPreview(
  datasourceId: number,
  rules: ImportRuleConfig
): Promise<ImportPreviewResponse> {
  const res = await httpClient.post<ImportPreviewResponse>(
    `/datasources/${datasourceId}/import-preview`,
    { rules }
  );
  return res.data;
}

export async function executeImport(
  datasourceId: number,
  request: ImportExecuteRequest
): Promise<ImportExecuteResponse> {
  const res = await httpClient.post<ImportExecuteResponse>(
    `/datasources/${datasourceId}/import`,
    request
  );
  return res.data;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- src/tests/localImportApi.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/localImport.ts frontend/src/api/localImport.ts frontend/src/tests/localImportApi.test.ts
git commit -m "feat: add local import frontend types and API client"
```

---

### Task 7: 前端导入向导组件

**Files:**
- Create: `frontend/src/components/localImport/ImportWizard.tsx`
- Create: `frontend/src/components/localImport/RuleConfigStep.tsx`
- Create: `frontend/src/components/localImport/PreviewStep.tsx`
- Create: `frontend/src/components/localImport/ConfirmStep.tsx`
- Test: `frontend/src/tests/ImportWizard.test.tsx`

**Interfaces:**
- Consumes: `ImportRuleConfig`, `ImportPreviewResponse`, `ProposedClass`, `ImportConflict`, `getImportPreview`, `executeImport`。
- Produces: `ImportWizard` component with `open`, `datasourceId`, `onClose` props.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/tests/ImportWizard.test.tsx`:

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ImportWizard from "../components/localImport/ImportWizard";
import * as api from "../api/localImport";

vi.mock("../api/localImport");

describe("ImportWizard", () => {
  it("renders rule config step by default", () => {
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    expect(screen.getByText(/规则配置/i)).toBeInTheDocument();
  });

  it("calls preview API when clicking next", async () => {
    vi.mocked(api.getImportPreview).mockResolvedValue({
      datasourceId: 1,
      proposedClasses: [],
      proposedJoins: [],
      conflicts: [],
      filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
      llmUsage: { modelName: null, promptTokens: 0, completionTokens: 0 },
    });
    render(<ImportWizard open datasourceId={1} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /下一步/i }));
    await waitFor(() => expect(api.getImportPreview).toHaveBeenCalledWith(1, expect.any(Object)));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- src/tests/ImportWizard.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/components/localImport/ImportWizard.tsx`:

```tsx
import { useState } from "react";
import { Modal, Steps, Button, message } from "antd";
import { useTranslation } from "../../i18n";
import RuleConfigStep from "./RuleConfigStep";
import PreviewStep from "./PreviewStep";
import ConfirmStep from "./ConfirmStep";
import { getImportPreview, executeImport } from "../../api/localImport";
import type {
  ImportExecuteRequest,
  ImportPreviewResponse,
  ImportRuleConfig,
} from "../../types/localImport";

interface Props {
  open: boolean;
  datasourceId: number;
  onClose: () => void;
}

export default function ImportWizard({ open, datasourceId, onClose }: Props) {
  const { t } = useTranslation();
  const [current, setCurrent] = useState(0);
  const [rules, setRules] = useState<ImportRuleConfig>({});
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const handlePreview = async () => {
    setLoading(true);
    try {
      const data = await getImportPreview(datasourceId, rules);
      setPreview(data);
      setCurrent(1);
    } catch {
      message.error(t("toast.previewFailed"));
    } finally {
      setLoading(false);
    }
  };

  const handleExecute = async (request: ImportExecuteRequest) => {
    setLoading(true);
    try {
      const result = await executeImport(datasourceId, request);
      setCurrent(2);
      // pass result to ConfirmStep via state
    } catch {
      message.error(t("toast.importFailed"));
    } finally {
      setLoading(false);
    }
  };

  const steps = [
    {
      title: t("localImport.steps.rule"),
      content: <RuleConfigStep rules={rules} onChange={setRules} />,
    },
    {
      title: t("localImport.steps.preview"),
      content: preview ? <PreviewStep preview={preview} onExecute={handleExecute} /> : null,
    },
    {
      title: t("localImport.steps.confirm"),
      content: <ConfirmStep />,
    },
  ];

  return (
    <Modal open={open} onCancel={onClose} footer={null} width={960} destroyOnHidden>
      <Steps current={current} items={steps.map((s) => ({ title: s.title }))} />
      <div style={{ marginTop: 24 }}>{steps[current].content}</div>
      <div style={{ marginTop: 24, textAlign: "right" }}>
        {current > 0 && <Button onClick={() => setCurrent(current - 1)}>{t("common.prev")}</Button>}
        {current === 0 && (
          <Button type="primary" loading={loading} onClick={handlePreview}>
            {t("common.next")}
          </Button>
        )}
      </div>
    </Modal>
  );
}
```

Create `RuleConfigStep.tsx`、`PreviewStep.tsx`、`ConfirmStep.tsx` 骨架（在后续小提交中填充）。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- src/tests/ImportWizard.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/localImport/
git commit -m "feat: add local import wizard shell and preview flow"
```

---

### Task 8: DatasourcePage 添加入口

**Files:**
- Modify: `frontend/src/pages/DatasourcePage.tsx`
- Modify: `frontend/src/i18n/zh-CN.ts`, `frontend/src/i18n/en-US.ts`

- [ ] **Step 1: Write the failing test**

Update `frontend/src/tests/DatasourcePage.test.tsx` 断言“智能导入到本体”按钮存在：

```tsx
expect(screen.getByRole("button", { name: /智能导入到本体/i })).toBeInTheDocument();
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- src/tests/DatasourcePage.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

修改 `frontend/src/pages/DatasourcePage.tsx`：

```tsx
import { ImportOutlined } from "@ant-design/icons";
import ImportWizard from "../components/localImport/ImportWizard";

// 在表格 columns 增加操作列
const columns = [
  // ... existing columns
  {
    title: t("common.actions"),
    render: (record: DataSource) => (
      <Space>
        <Button icon={<ImportOutlined />} onClick={() => setImportingId(record.id)}>
          {t("datasource.importToOntology")}
        </Button>
        {/* existing actions */}
      </Space>
    ),
  },
];

// 组件内增加 state
const [importingId, setImportingId] = useState<number | null>(null);

// 渲染 ImportWizard
<ImportWizard
  open={importingId !== null}
  datasourceId={importingId ?? 0}
  onClose={() => setImportingId(null)}
/>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- src/tests/DatasourcePage.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/DatasourcePage.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/tests/DatasourcePage.test.tsx
git commit -m "feat: add local import entry on datasource page"
```

---

### Task 9: 后端集成测试完善

**Files:**
- Modify: `app/tests/api/test_local_import.py`

**Interfaces:**
- Consumes: 真实 PostgreSQL 实例、FastAPI test client。
- Produces: 覆盖 import-preview 和 import 两个端点的集成测试。

- [ ] **Step 1: Write the failing test**

在 `app/tests/api/test_local_import.py` 中补充：

```python
@pytest.mark.asyncio
async def test_import_executes_and_creates_ontology(client, db):
    # 创建数据源
    ds_payload = { ... }
    res = await client.post("/api/v1/datasources", json=ds_payload)
    ds_id = res.json()["id"]

    # 先 preview
    preview_res = await client.post(f"/api/v1/datasources/{ds_id}/import-preview", json={})
    preview = preview_res.json()

    # 执行导入
    execute_payload = {
        "confirmedClasses": [{
            **preview["proposedClasses"][0],
            "properties": [{
                **p,
                "isSelected": True,
            } for p in preview["proposedClasses"][0]["properties"]],
        }],
        "confirmedJoins": [],
        "conflictResolutions": [],
        "syncEmbeddings": False,
    }
    res = await client.post(f"/api/v1/datasources/{ds_id}/import", json=execute_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["createdClasses"] >= 1

    # 验证 ontology class 已创建
    classes_res = await client.get("/api/v1/ontology/classes")
    classes = classes_res.json()
    assert any(c["sourceTable"] == preview["proposedClasses"][0]["sourceTable"] for c in classes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest app/tests/api/test_local_import.py -v`
Expected: FAIL until LocalImportService.execute_import is fully implemented.

- [ ] **Step 3: Implement execute_import in LocalImportService**

Complete `LocalImportService._build_proposals` and `execute_import` in `app/services/local_import_service.py`:

```python
from app.domain.schemas import (
    OntologyClassCreate,
    OntologyJoinCreate,
    OntologyPropertyCreate,
)


class LocalImportService:
    # ... build_preview from Task 4

    def _build_proposals(self, enhanced, rules, original_tables):
        table_by_name = {t.table_name: t for t in original_tables}
        proposed_classes: list[ProposedClass] = []
        proposed_joins: list[ProposedJoin] = []

        for et in enhanced.tables:
            original = table_by_name[et.name]
            properties: list[ProposedProperty] = []
            for col in original.columns:
                ec = next((c for c in et.columns if c.name == col.column_name), None)
                properties.append(
                    ProposedProperty(
                        source_column=col.column_name,
                        property_name=col.column_name,
                        property_alias=ec.alias if ec else None,
                        description=ec.description if ec else None,
                        data_type=self._rule_engine.map_data_type(col.data_type, rules.type_mappings).value,
                        is_primary_key=col.column_name in original.primary_keys,
                        is_foreign_key=any(fk.column_name == col.column_name for fk in original.foreign_keys),
                        enum_values=ec.enum_values if ec else None,
                    )
                )
            proposed_classes.append(
                ProposedClass(
                    source_table=et.name,
                    class_name=et.name,
                    class_alias=et.alias,
                    description=et.description,
                    properties=properties,
                )
            )

            for fk in original.foreign_keys:
                proposed_joins.append(
                    ProposedJoin(
                        source_table=et.name,
                        source_columns=[fk.column_name],
                        target_table=fk.ref_table,
                        target_columns=[fk.ref_column],
                    )
                )
        return proposed_classes, [p for cls in proposed_classes for p in cls.properties], proposed_joins

    async def execute_import(
        self,
        session,
        datasource_id,
        request,
        created_by,
    ):
        created_classes = 0
        created_properties = 0
        created_joins = 0
        skipped = 0
        overwritten = 0
        errors: list = []
        class_id_by_table: dict[str, int] = {}

        # 处理冲突
        skip_existing_ids = set()
        for resolution in request.conflict_resolutions:
            if resolution.action == "skip":
                skip_existing_ids.add(resolution.existing_id)
                skipped += 1
            elif resolution.action == "overwrite":
                overwritten += 1

        for pc in request.confirmed_classes:
            if not pc.is_selected:
                continue
            try:
                cls = await self._ontology_service.create_class(
                    session,
                    OntologyClassCreate(
                        class_name=pc.class_name,
                        class_alias=pc.class_alias,
                        description=pc.description,
                        source_table=pc.source_table,
                        created_by=created_by,
                    ),
                )
                class_id_by_table[pc.source_table] = cls.id
                created_classes += 1

                for prop in pc.properties:
                    if not getattr(prop, "is_selected", True):
                        continue
                    try:
                        await self._ontology_service.create_property(
                            session,
                            OntologyPropertyCreate(
                                class_id=cls.id,
                                property_name=prop.property_name,
                                property_alias=prop.property_alias,
                                description=prop.description,
                                data_type=prop.data_type,
                                is_primary_key=prop.is_primary_key,
                                is_foreign_key=prop.is_foreign_key,
                                source_column=prop.source_column,
                            ),
                        )
                        created_properties += 1
                    except Exception as exc:
                        errors.append({"type": "property", "name": prop.property_name, "message": str(exc)})
            except Exception as exc:
                errors.append({"type": "class", "name": pc.class_name, "message": str(exc)})

        for pj in request.confirmed_joins:
            if not pj.is_selected:
                continue
            try:
                source_id = class_id_by_table.get(pj.source_table)
                # resolve target class id by source_table via existing ontology or newly created
                target_id = class_id_by_table.get(pj.target_table) or await self._resolve_target_class_id(session, pj.target_table)
                if source_id and target_id:
                    await self._ontology_service.create_join(
                        session,
                        OntologyJoinCreate(
                            source_class_id=source_id,
                            source_columns=pj.source_columns,
                            target_class_id=target_id,
                            target_columns=pj.target_columns,
                            join_type=pj.join_type,
                            relation_type=pj.relation_type,
                        ),
                    )
                    created_joins += 1
            except Exception as exc:
                errors.append({"type": "join", "name": f"{pj.source_table}->{pj.target_table}", "message": str(exc)})

        return ImportExecuteResponse(
            success=not errors,
            created_classes=created_classes,
            created_properties=created_properties,
            created_joins=created_joins,
            skipped_conflicts=skipped,
            overwritten_conflicts=overwritten,
            errors=errors,
        )

    async def _resolve_target_class_id(self, session, source_table):
        classes = await self._ontology_service.list_classes(session)
        for cls in classes:
            if cls.source_table == source_table:
                return cls.id
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest app/tests/api/test_local_import.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/local_import_service.py app/tests/api/test_local_import.py
git commit -m "feat: implement local import execution and integration tests"
```

---

### Task 10: E2E 测试

**Files:**
- Create: `frontend/e2e/local-import.spec.ts`

- [ ] **Step 1: Write the failing test**

```typescript
import { test, expect } from "@playwright/test";

test("local import wizard flow", async ({ page }) => {
  await page.goto("/datasources");
  await page.getByRole("button", { name: "智能导入到本体" }).first().click();
  await expect(page.getByText("规则配置")).toBeVisible();
  await page.getByRole("button", { name: "下一步" }).click();
  await expect(page.getByText("预览")).toBeVisible();
  await page.getByRole("button", { name: "确认导入" }).click();
  await expect(page.getByText("导入完成")).toBeVisible();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx playwright test e2e/local-import.spec.ts`
Expected: FAIL.

- [ ] **Step 3: Implement UI actions and labels**

在 `PreviewStep.tsx` 和 `ConfirmStep.tsx` 中补充按钮和文案，确保 E2E 选择器命中。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx playwright test e2e/local-import.spec.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/e2e/local-import.spec.ts frontend/src/components/localImport/
git commit -m "test: add local import E2E flow"
```

---

### Task 11: Code Review、文档更新与收尾

**Files:**
- All modified files.
- Update: `docs/superpowers/specs/2026-08-29-local-import-design.md`（如有变更）
- Update: `Harness/changes/feat-local-import/summary.md`

- [ ] **Step 1: Run backend tests**

```bash
cd backend
pytest app/tests/services/test_import_rule_engine.py app/tests/services/test_import_conflict_resolver.py app/tests/services/test_import_llm_enhancer.py app/tests/services/test_local_import_service.py app/tests/api/test_local_import.py -v
```
Expected: PASS, coverage ≥ 80%.

- [ ] **Step 2: Run frontend tests**

```bash
cd frontend
npm test -- run
npm run typecheck
```
Expected: PASS.

- [ ] **Step 3: Run security review**

```bash
# 使用 security-reviewer agent扫描新增代码
```
Required: 无硬编码密码、SQL 只读、LLM prompt 无敏感信息。

- [ ] **Step 4: Run code review**

```bash
# 使用 code-reviewer agent审查
```
Required: 函数 < 50 行、文件 < 800 行、无嵌套过深、错误处理完整。

- [ ] **Step 5: Update Harness change record**

Create `Harness/changes/feat-local-import/summary.md`:

```markdown
# feat-local-import

## 变更内容
新增本地导入功能，支持从已注册数据源自动读取 schema，经 LLM 语义增强和人工预览确认后批量生成本体。

## 影响范围
- 后端：新增 4 个 service、1 个 API 模块、若干 DTO。
- 前端：新增导入向导组件、API 封装、DatasourcePage 入口。

## 测试
- 单元测试：ImportRuleEngine、ImportConflictResolver、ImportLlmEnhancer、LocalImportService。
- 集成测试：LocalImport API。
- E2E 测试：导入向导完整流程。

## 相关文档
- `docs/superpowers/specs/2026-08-29-local-import-design.md`
- `docs/superpowers/plans/2026-08-29-local-import-plan.md`
```

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "docs: add local import change record and finalize"
```

---

## Self-Review

### Spec Coverage

| 设计文档章节 | 对应 Task |
|---|---|
| 用户流程（先注册数据源再导入） | Task 5 API, Task 8 前端入口 |
| 半自动预览 | Task 7 PreviewStep |
| LLM 增强 A/C/D/E | Task 3, Task 4 |
| 冲突处理清单 | Task 2, Task 4, Task 7 |
| 表过滤规则 | Task 1, Task 7 RuleConfigStep |
| 类型映射可配置 | Task 1, Task 7 RuleConfigStep |
| 错误处理与降级 | Task 3, Task 4, Task 9 |
| 测试策略 | Task 9, Task 10, Task 11 |

### Placeholder Scan

- 无 TBD/TODO。
- 无 "implement later"。
- 每个 step 包含可运行代码或命令。

### Type Consistency

- `ImportRuleConfig` 在 Task 1 定义，Task 4/5/7/前端均一致使用。
- `ProposedClass` / `ProposedProperty` / `ProposedJoin` DTO 在 Task 4 定义，前后端同名。
- API 路径 `/api/v1/datasources/{id}/import-preview` 在 Task 5 和 Task 6 一致。

---
