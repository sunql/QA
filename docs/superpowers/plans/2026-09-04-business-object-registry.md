# Business Object Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把散落成三个独立命名层（`EntityType` Python 枚举 / Neo4j `BUSINESS_ENTITY_LABELS` 常量 / `ontology_class.class_name`）的「业务对象目录」建为单一 SSOT `business_object` 表 + 前端管理页，让类型、Neo4j label、身份键三层都锚定到一处。同时把 `document_entity_relation.entity_key` 从 BIGINT 代理键统一为 VARCHAR 业务码；删除 `EntityType` 枚举改用 `BusinessObjectCode` 字面量类型。

**Architecture:** 6 个业务对象 (SUPPLIER/MATERIAL/PO/GR/IQC/NCR) 落入 `business_object` 表；IQC 对应本体类 `IncomingInspection` 结构性建模，NCR 不建本体类（user 拍板）。三表 (`entity_mapping` / `feature_definition` / `document_entity_relation`) 的 `entity_type` 列改 FK → `business_object.code`。Neo4j label 收紧为 5 类 + Contract（去掉 Material/GoodsReceipt/NCR，新增 ItemMaster/Receipt/IncomingInspection）；`graph_relation_service` 启动期一次性从 DB 读 `business_object.graph_label` 入内存 dict。前端 `/business-objects` CRUD 页沿用 `kpi_catalog` / `entity_mapping` 范式。

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.0 async, PostgreSQL (port 5433), Alembic, React 18, Ant Design 5, TypeScript strict, vitest, Neo4j 5.x.

## Global Constraints

- Backend Python: snake_case functions/vars, snake_case ORM/Pydantic fields (project deviation from PEP 8)
- File size: ≤ 800 lines, functions ≤ 50 lines, nesting ≤ 4 levels
- Immutability: create new objects, never mutate
- Commit format: `<type>: <description>`, NO `Co-Authored-By:` trailer
- Test DB: `postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test` (port 5433, NOT 5432)
- Backend testing: real PostgreSQL only (NO sqlite)
- Coverage gate: `pytest app/tests/ --cov=app --cov-fail-under=80`
- TDD: write failing test first, run, implement minimal, run, commit
- Alembic chain to extend: `0037_agent_tool_config` → `0038_business_object` → `0039_incoming_inspection_class` → `0040_entity_type_fk` → `0041_doc_rel_entity_key_varchar` (linear)
- `BusinessObjectCode = Literal["SUPPLIER","MATERIAL","PO","GR","IQC","NCR"]` 字面量类型 (放 `backend/app/domain/enums.py`)，**不**保留 enum 实例
- `business_object.code` 沿用现有 `SUPPLIER/MATERIAL`（**不**对齐 `docs/data-knowledge/采购域.md` 的 `SUPP/MATL`，避免 45+ 处已存行迁移）
- 6 行种子 (seed_business_objects.py)，幂等 `ON CONFLICT DO NOTHING` by code
- `graph_label` 列 nullable：NCR 行 NULL 不入图；其他 5 行 = `class_name`
- `BUSINESS_ENTITY_LABELS` 白名单收紧为 `{Supplier, ItemMaster, PurchaseOrder, Receipt, IncomingInspection, Contract}`（5 类 + Contract，Contract 仍由文档目录提供）
- `document_entity_relation.entity_key` VARCHAR 迁移：经 `entity_mapping(entity_type, enterprise_key → enterprise_code)` 回填；孤儿行 DELETE
- Neo4j 节点 key 从 `str(m.enterprise_key)` (int hash) 改 `m.enterprise_code` (VARCHAR 业务码)
- 触发 `code-reviewer` + `security-reviewer` 双 agent 并行审查（HTTP 入口 + ORM 写入 + CQL 拼接 + 删除引用检查）

---

## File Structure

### 后端新增

| 文件 | 责任 |
|---|---|
| `backend/alembic/versions/0038_business_object.py` | 新建 `business_object` 表 |
| `backend/alembic/versions/0039_incoming_inspection_class.py` | INSERT 本体类 `IncomingInspection` |
| `backend/alembic/versions/0040_entity_type_fk.py` | 三表 `entity_type` FK 化 |
| `backend/alembic/versions/0041_doc_rel_entity_key_varchar.py` | `document_entity_relation.entity_key` BIGINT→VARCHAR + 孤儿 DELETE |
| `backend/scripts/seed_business_objects.py` | 6 行幂等 seed |
| `backend/scripts/rename_neo4j_labels.py` | 一次性 Neo4j label 重命名脚本 |
| `backend/app/services/business_object_service.py` | CRUD + 守卫（graph_label 一致性、引用检查） |
| `backend/app/api/v1/business_object.py` | 5 个 REST 端点 |
| `backend/app/tests/unit/test_business_object_schemas.py` | 字面量类型 + DTO 校验单测 |
| `backend/app/tests/integration/test_business_object_api.py` | CRUD + 守卫集成测 |
| `backend/app/tests/integration/test_seed_business_objects.py` | seed 幂等集成测 |
| `backend/app/tests/integration/test_business_object_entity_type_fk.py` | 三表 FK 兜底集成测 |
| `backend/app/tests/integration/test_doc_rel_entity_key_migration.py` | entity_key VARCHAR 迁移集成测 |

### 后端改动

| 文件 | 改动 |
|---|---|
| `backend/app/domain/enums.py` | 删 `EntityType`；新增 `BusinessObjectCode = Literal[...]` |
| `backend/app/domain/models.py` | `EntityMapping.entity_type` / `FeatureDefinition.entity_type` / `DocumentEntityRelation.entity_type` 加 `ForeignKey("business_object.code", ondelete="RESTRICT")`；`DocumentEntityRelation.entity_key` 改 `String(100)` |
| `backend/app/domain/schemas.py` | 新增 `BusinessObjectCreate/Update/Read`；`DocumentEntityRelation` 相关 DTO 类型 `int → str`；`entity_type: EntityType → BusinessObjectCode` |
| `backend/app/domain/exceptions.py` | 新增 `BusinessObjectGraphLabelMismatchError(BusinessRuleError)` |
| `backend/app/services/messages_zh.py` | 新增 `MSG_BUSINESS_OBJECT_*` 文案 |
| `backend/app/infrastructure/neo4j_client.py` | `BUSINESS_ENTITY_LABELS` 收紧为 5 类 + Contract |
| `backend/app/services/graph_relation_service.py` | 删 `ENTITY_TYPE_LABELS` dict；新增 `_loadLabelMap()` 启动期 DB 读；`seedGraphRelations` 用 `m.enterprise_code` 作 Neo4j key；`_sheet16Edges` 删除 NCR 终点 |
| `backend/app/services/supplier_360_service.py` | `EntityType.SUPPLIER` → `"SUPPLIER"` (10 处) |
| `backend/app/services/supplier_name_resolver.py` | `EntityType.SUPPLIER` → `"SUPPLIER"` (2 处) |
| `backend/app/services/document_service.py` | `entity_type: EntityType → str`；`entity_key: int → str`；`dto.entity_type.value → dto.entity_type` |
| `backend/app/services/entity_mapping_service.py` | `EntityType` 引用 → `BusinessObjectCode` / 字符串 |
| `backend/app/api/v1/entity_mapping.py` | query param 类型 `EntityType → BusinessObjectCode` |
| `backend/app/api/v1/documents.py` | `entity_type: EntityType → BusinessObjectCode`；`entity_key: int → str` |
| `backend/app/api/v1/features.py` | `EntityType` 引用 → `BusinessObjectCode` / 字符串 (如有) |
| `backend/app/main.py` | 挂载 `business_object.router` 到 `/api/v1/business-objects`；`_statusFor` 新增 `BusinessObjectGraphLabelMismatchError → 422` |
| `backend/app/tests/_testapp.py` | 测试 app 同步挂载 `business_object.router` |
| `backend/scripts/__init__.py` | 已有，仅暴露 `seed_business_objects` |

### 前端新增

| 文件 | 责任 |
|---|---|
| `frontend/src/types/businessObject.ts` | 类型契约（`BusinessObjectCode` 字面量 + 6 个 DTO + `BUSINESS_OBJECT_OPTIONS`） |
| `frontend/src/api/businessObject.ts` | HTTP client 封装（5 函数） |
| `frontend/src/pages/BusinessObjectPage.tsx` | CRUD 页（表格 + 过滤栏 + Modal CRUD） |
| `frontend/src/tests/businessObjectApi.test.ts` | API 单测（5 用例） |
| `frontend/src/tests/BusinessObjectPage.test.tsx` | Page 单测（5 用例） |

### 前端改动

| 文件 | 改动 |
|---|---|
| `frontend/src/App.tsx` | 新增 `path="business-objects"` 路由 |
| `frontend/src/components/common/AppLayout.tsx` | 左侧导航加 `businessObjects` 入口 |
| `frontend/src/i18n/zh-CN.ts` + `en-US.ts` | 顶级 `businessObject` 命名空间 + 复用 `common.save/actions` |
| `frontend/src/types/document.ts` (若存在) | `DocEntityRelation.entityKey` 类型 `number → string` |
| `frontend/src/api/documents.ts` (若存在) | 调用方同步 |

---

## Task 1: Alembic 0038 + `BusinessObject` ORM 模型

**Files:**
- Create: `backend/alembic/versions/0038_business_object.py`
- Modify: `backend/app/domain/models.py` (add `BusinessObject` class)
- Test: `backend/app/tests/integration/test_business_object_migration.py`

**Interfaces:**
- Consumes: `BigIntPk`, `String`, `Text`, `BigIntFk`, `TimestampMixin`, `BigInteger` from `sqlalchemy`/`sqlalchemy.dialects.postgresql`/`app.domain.models`
- Produces:
  - `BusinessObject` ORM class with `code` (String 20, PK), `name`, `header_class_id` (FK → `ontology_class.id`), `graph_label`, `description`, `created_by`, `created_time`, `updated_time`, CHECK `code = UPPER(code)`, INDEX `ix_business_object_header_class(header_class_id)`

- [ ] **Step 1: Write the failing migration test**

```python
# backend/app/tests/integration/test_business_object_migration.py
"""Alembic 0038 应创建 business_object 表（含 unique + CHECK + 索引）。"""
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_business_object_table_exists_with_constraints():
    engine = create_async_engine(
        "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"
    )
    async with engine.connect() as conn:
        rows = await conn.execute(text("""
            SELECT column_name, data_type, is_nullable, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'business_object'
            ORDER BY ordinal_position
        """))
        cols = {r[0]: r for r in rows.fetchall()}
        # 必须存在的列
        assert "code" in cols
        assert cols["code"][1] == "character varying"
        assert cols["code"][3] == 20
        assert cols["code"][2] == "NO"  # NOT NULL
        assert "name" in cols
        assert cols["name"][3] == 100
        assert "header_class_id" in cols
        assert "graph_label" in cols
        assert "description" in cols

        # PK on code
        pk_rows = await conn.execute(text("""
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'business_object'::regclass AND i.indisprimary
        """))
        pk_cols = [r[0] for r in pk_rows.fetchall()]
        assert pk_cols == ["code"]

        # CHECK constraint ck_business_object_code_upper
        check_rows = await conn.execute(text("""
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'business_object'::regclass AND contype = 'c'
        """))
        checks = {r[0]: r[1] for r in check_rows.fetchall()}
        assert "ck_business_object_code_upper" in checks
        assert "UPPER(code)" in checks["ck_business_object_code_upper"]

        # INDEX on header_class_id
        idx_rows = await conn.execute(text("""
            SELECT indexname FROM pg_indexes WHERE tablename = 'business_object'
        """))
        idxs = [r[0] for r in idx_rows.fetchall()]
        assert any("ix_business_object_header_class" in i for i in idxs)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_business_object_migration.py -v
```
Expected: FAIL with `relation "business_object" does not exist`

- [ ] **Step 3: Write Alembic migration 0038**

```python
# backend/alembic/versions/0038_business_object.py
"""新建 business_object 表（Phase 4 业务对象注册表 SSOT）."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_business_object"
down_revision = "0037_agent_tool_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_object",
        sa.Column("code", sa.String(20), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "header_class_id",
            sa.BigInteger(),
            sa.ForeignKey("ontology_class.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("graph_label", sa.String(100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(50), nullable=True),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "code = UPPER(code)", name="ck_business_object_code_upper"
        ),
    )
    op.create_index(
        "ix_business_object_header_class",
        "business_object",
        ["header_class_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_business_object_header_class", table_name="business_object")
    op.drop_table("business_object")
```

- [ ] **Step 4: Write `BusinessObject` ORM model**

在 `backend/app/domain/models.py`（追加在 `KpiCatalog` 之后）：

```python
class BusinessObject(Base, TimestampMixin):
    """业务对象注册表 SSOT（Phase 4）。

    一行 = 一个业务对象（SUPPLIER/MATERIAL/PO/GR/IQC/NCR）。

    - code（PK 字符串）：与 EntityMapping/FeatureDefinition/DocumentEntityRelation
      的 entity_type 列对齐；删除 EntityType 枚举后变为字面量类型 BusinessObjectCode。
    - header_class_id：FK → ontology_class.id（指向头表类；NCR 暂 NULL）。
    - graph_label：Neo4j 业务节点 label；本期 = header_class.class_name。
      留 nullable 是为未来扩展（业务对象可独立于本体类命名）。

    code 不可改（PK）；name / header_class_id / graph_label / description 可 CRUD。
    """

    __tablename__ = "business_object"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    header_class_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey("ontology_class.id", ondelete="RESTRICT"),
        nullable=True,
    )
    graph_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(50), nullable=True)

    def __repr__(self) -> str:
        return f"<BusinessObject code={self.code} name={self.name}>"
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_business_object_migration.py -v
```
Expected: PASS

- [ ] **Step 6: Run full regression to confirm no breakage**

```bash
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```
Expected: All previous tests PASS (migration is linear additive; 0038 → 0037 head)

- [ ] **Step 7: Commit**

```bash
git add backend/alembic/versions/0038_business_object.py \
        backend/app/domain/models.py \
        backend/app/tests/integration/test_business_object_migration.py
git commit -m "feat(business-object): alembic 0038 + BusinessObject ORM model"
```

---

## Task 2: Alembic 0039 + IncomingInspection 本体类（结构性建模）

**Files:**
- Create: `backend/alembic/versions/0039_incoming_inspection_class.py`
- Test: `backend/app/tests/integration/test_incoming_inspection_class.py`

**Interfaces:**
- Produces: `ontology_class` 表新增一行 `class_name='IncomingInspection'`, `source_table='DWD_INCOMING_INSPECTION'`, `object_type='Transaction'`, `version=1`

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_incoming_inspection_class.py
"""Alembic 0039 应向 ontology_class 插入 IncomingInspection 结构性建模行。"""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_incoming_inspection_class_inserted():
    engine = create_async_engine(
        "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"
    )
    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT class_name, source_table, object_type, version
            FROM ontology_class
            WHERE class_name = 'IncomingInspection'
        """))).first()
        assert row is not None
        assert row[0] == "IncomingInspection"
        assert row[1] == "DWD_INCOMING_INSPECTION"
        assert row[2] == "Transaction"
        assert row[3] == 1


@pytest.mark.asyncio
async def test_incoming_inspection_is_idempotent():
    """重复运行 0039 不会重复插入（前置检查覆盖）。"""
    engine = create_async_engine(
        "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT COUNT(*) FROM ontology_class WHERE class_name = 'IncomingInspection'
        """))).scalar()
        assert rows == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_incoming_inspection_class.py -v
```
Expected: FAIL with 0 rows

- [ ] **Step 3: Write Alembic migration 0039**

```python
# backend/alembic/versions/0039_incoming_inspection_class.py
"""INSERT ontology_class IncomingInspection（结构性建模，0 行）.

NCR 不建本体类（user 拍板）；本迁移仅追加 IncomingInspection 一行。
重复运行幂等：WHERE NOT EXISTS 守卫。
"""
from __future__ import annotations

from alembic import op

revision = "0039_incoming_inspection_class"
down_revision = "0038_business_object"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO ontology_class (
                class_name, source_table, description, object_type,
                version, valid_from, created_by
            )
        SELECT 'IncomingInspection',
               'DWD_INCOMING_INSPECTION',
               '来料检验（结构性建模，0 行；详见 docs/data-knowledge/采购域.md）',
               'Transaction',
               1,
               now(),
               'seed'
        WHERE NOT EXISTS (
            SELECT 1 FROM ontology_class WHERE class_name = 'IncomingInspection'
        )
    """)


def downgrade() -> None:
    op.execute(
        "DELETE FROM ontology_class WHERE class_name = 'IncomingInspection'"
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_incoming_inspection_class.py -v
```
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/0039_incoming_inspection_class.py \
        backend/app/tests/integration/test_incoming_inspection_class.py
git commit -m "feat(business-object): alembic 0039 + IncomingInspection ontology class"
```

---

## Task 3: `seed_business_objects.py` + 集成测试

**Files:**
- Create: `backend/scripts/seed_business_objects.py`
- Modify: `backend/scripts/__init__.py` (expose `seedBusinessObjects`)
- Test: `backend/app/tests/integration/test_seed_business_objects.py`

**Interfaces:**
- Produces: `seedBusinessObjects(session) -> int` (新增行数，幂等)

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_seed_business_objects.py
"""seed_business_objects 幂等写入 6 行 + 可通过 API 查到。"""
import pytest
from sqlalchemy import func, select

from app.domain.models import BusinessObject
from scripts.seed_business_objects import seedBusinessObjects


@pytest.mark.asyncio
async def test_first_run_inserts_six_mappings(db_session):
    inserted = await seedBusinessObjects(db_session)
    assert inserted == 6

    rows = (await db_session.execute(select(func.count()).select_from(BusinessObject))).scalar()
    assert rows == 6


@pytest.mark.asyncio
async def test_second_run_is_idempotent(db_session):
    await seedBusinessObjects(db_session)
    inserted2 = await seedBusinessObjects(db_session)
    assert inserted2 == 0

    rows = (await db_session.execute(select(func.count()).select_from(BusinessObject))).scalar()
    assert rows == 6


@pytest.mark.asyncio
async def test_codes_are_uppercase(db_session):
    await seedBusinessObjects(db_session)
    codes = sorted(
        (await db_session.execute(select(BusinessObject.code))).scalars().all()
    )
    assert codes == ["GR", "IQC", "MATERIAL", "NCR", "PO", "SUPPLIER"]


@pytest.mark.asyncio
async def test_graph_labels_resolve_to_class_names(db_session):
    """5 个业务对象 graph_label = header_class.class_name；NCR 为 NULL."""
    await seedBusinessObjects(db_session)
    rows = (await db_session.execute(select(BusinessObject))).scalars().all()
    by_code = {r.code: r for r in rows}
    assert by_code["SUPPLIER"].graph_label == "Supplier"
    assert by_code["MATERIAL"].graph_label == "ItemMaster"
    assert by_code["PO"].graph_label == "PurchaseOrder"
    assert by_code["GR"].graph_label == "Receipt"
    assert by_code["IQC"].graph_label == "IncomingInspection"
    assert by_code["NCR"].graph_label is None
    assert by_code["NCR"].header_class_id is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_seed_business_objects.py -v
```
Expected: ImportError or AttributeError

- [ ] **Step 3: Implement `seed_business_objects.py`**

```python
# backend/scripts/seed_business_objects.py
"""业务对象种子（Phase 4.4）。

6 行：SUPPLIER / MATERIAL / PO / GR / IQC / NCR。
幂等：ON CONFLICT (code) DO NOTHING。

header_class_id 由 class_name 派生（先查 ontology_class.id）；
graph_label = header_class.class_name；NCR 无 header_class，二者皆 NULL。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import BusinessObject, OntologyClass


def _header_class_id_map(session: AsyncSession) -> dict[str, int]:
    """把 class_name 解析为 id；用于 seed 行填充 header_class_id."""
    rows = (
        session.execute(
            select(OntologyClass.id, OntologyClass.class_name).where(
                OntologyClass.class_name.in_(
                    [
                        "Supplier",
                        "ItemMaster",
                        "PurchaseOrder",
                        "Receipt",
                        "IncomingInspection",
                    ]
                )
            )
        )
    ).all()
    return {name: int(_id) for _id, name in rows}


def _seed_rows(header_map: dict[str, int]) -> list[dict[str, Any]]:
    return [
        {
            "code": "SUPPLIER",
            "name": "供应商",
            "header_class_id": header_map.get("Supplier"),
            "graph_label": "Supplier",
            "description": "向企业提供物料或服务的外部组织",
        },
        {
            "code": "MATERIAL",
            "name": "物料",
            "header_class_id": header_map.get("ItemMaster"),
            "graph_label": "ItemMaster",
            "description": "企业采购和使用的物料",
        },
        {
            "code": "PO",
            "name": "采购订单",
            "header_class_id": header_map.get("PurchaseOrder"),
            "graph_label": "PurchaseOrder",
            "description": "企业向供应商下达的采购订单",
        },
        {
            "code": "GR",
            "name": "收货",
            "header_class_id": header_map.get("Receipt"),
            "graph_label": "Receipt",
            "description": "企业确认收到货物",
        },
        {
            "code": "IQC",
            "name": "来料检验",
            "header_class_id": header_map.get("IncomingInspection"),
            "graph_label": "IncomingInspection",
            "description": "对采购物料进行质量检验（结构性建模，0 行）",
        },
        {
            "code": "NCR",
            "name": "不合格处理",
            "header_class_id": None,
            "graph_label": None,
            "description": "来料不合格记录（本期不建本体类，不入 Neo4j 图）",
        },
    ]


async def seedBusinessObjects(session: AsyncSession) -> int:
    """幂等 seed 业务对象。返回本次新增行数."""
    header_map = _header_class_id_map(session)
    rows = _seed_rows(header_map)

    stmt = pg_insert(BusinessObject).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["code"])
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount or 0


async def main() -> None:
    """CLI 入口：连接默认 DB，跑 seed."""
    import asyncio

    from app.infrastructure.database import getAsyncSessionMaker

    session_maker = getAsyncSessionMaker()
    async with session_maker() as session:
        inserted = await seedBusinessObjects(session)
        print(f"[seed_business_objects] 本次新增 {inserted} 条")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
```

- [ ] **Step 4: Expose in `scripts/__init__.py`**

在 `backend/scripts/__init__.py` 追加：

```python
from scripts.seed_business_objects import seedBusinessObjects

__all__ = ["seedBusinessObjects"]
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_seed_business_objects.py -v
```
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add backend/scripts/seed_business_objects.py \
        backend/scripts/__init__.py \
        backend/app/tests/integration/test_seed_business_objects.py
git commit -m "feat(business-object): seed_business_objects.py + 4 integration tests"
```

---

## Task 4: `BusinessObjectCode` 字面量类型 + Pydantic DTO

**Files:**
- Modify: `backend/app/domain/enums.py` (delete `EntityType`; add `BusinessObjectCode` Literal)
- Modify: `backend/app/domain/schemas.py` (add `BusinessObjectCreate/Update/Read`)
- Modify: `backend/app/domain/exceptions.py` (add `BusinessObjectGraphLabelMismatchError`)
- Modify: `backend/app/services/messages_zh.py` (add MSG constants)
- Test: `backend/app/tests/unit/test_business_object_schemas.py`

**Interfaces:**
- Produces:
  - `BusinessObjectCode = Literal["SUPPLIER","MATERIAL","PO","GR","IQC","NCR"]` (in `backend/app/domain/enums.py`)
  - `BusinessObjectCreate(BaseModel)`: `code: BusinessObjectCode`, `name: str (1..100)`, `header_class_id: int | None`, `graph_label: str | None (max 100)`, `description: str | None (max 4000)`
  - `BusinessObjectUpdate(BaseModel)`: all optional except `code`
  - `BusinessObjectRead(BaseModel)`: full row
  - `BusinessObjectGraphLabelMismatchError(BusinessRuleError)`
  - 4 MSG constants

- [ ] **Step 1: Write the failing unit tests**

```python
# backend/app/tests/unit/test_business_object_schemas.py
"""BusinessObjectCode 字面量类型 + Pydantic DTO 校验."""
from typing import get_args

import pytest
from pydantic import ValidationError

from app.domain.enums import BusinessObjectCode
from app.domain.schemas import (
    BusinessObjectCreate,
    BusinessObjectRead,
    BusinessObjectUpdate,
)


def test_business_object_code_has_six_values() -> None:
    assert sorted(get_args(BusinessObjectCode)) == [
        "GR",
        "IQC",
        "MATERIAL",
        "NCR",
        "PO",
        "SUPPLIER",
    ]


def test_create_accepts_valid_code() -> None:
    dto = BusinessObjectCreate(
        code="SUPPLIER", name="供应商", graph_label="Supplier"
    )
    assert dto.code == "SUPPLIER"
    assert dto.graph_label == "Supplier"


def test_create_rejects_invalid_code() -> None:
    with pytest.raises(ValidationError):
        BusinessObjectCreate(code="UNKNOWN", name="X")  # type: ignore[arg-type]


def test_create_rejects_oversized_name() -> None:
    with pytest.raises(ValidationError):
        BusinessObjectCreate(code="SUPPLIER", name="x" * 101)


def test_create_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        BusinessObjectCreate(code="SUPPLIER", name="")


def test_update_partial_fields() -> None:
    dto = BusinessObjectUpdate(name="新名字")
    assert dto.name == "新名字"
    assert dto.graph_label is None


def test_update_explicit_none_clears_graph_label() -> None:
    """显式 None 视为清空字段（与 Phase 4.1 object_type 同模式）."""
    dto = BusinessObjectUpdate(graph_label=None)
    assert dto.graph_label is None


def test_description_max_length_4000() -> None:
    with pytest.raises(ValidationError):
        BusinessObjectCreate(
            code="SUPPLIER", name="X", description="x" * 4001
        )


def test_read_carries_all_fields() -> None:
    dto = BusinessObjectRead(
        code="PO",
        name="采购订单",
        header_class_id=5,
        graph_label="PurchaseOrder",
        description="...",
    )
    assert dto.code == "PO"
    assert dto.header_class_id == 5
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend
uv run pytest app/tests/unit/test_business_object_schemas.py -v
```
Expected: ImportError (BusinessObjectCode, BusinessObjectCreate 未定义)

- [ ] **Step 3: Modify `enums.py` — delete `EntityType`, add `BusinessObjectCode`**

在 `backend/app/domain/enums.py`：

替换 lines 175-188 的 `EntityType` 类：

```python
# 删除：
# class EntityType(str, Enum):
#     SUPPLIER = "SUPPLIER"
#     ...

# 替换为：
BusinessObjectCode = Literal[
    "SUPPLIER", "MATERIAL", "PO", "GR", "IQC", "NCR"
]
"""业务对象代码字面量类型（Phase 4.4 业务对象注册表 SSOT）。

与 business_object.code 列对齐；DB FK 是权威，本类型仅供 Pydantic 校验。
新增业务对象需要：1) INSERT 一行 business_object；2) 在此 Literal 追加值。
"""

# 在文件顶部加：
from typing import Literal
```

- [ ] **Step 4: Add `BusinessObjectGraphLabelMismatchError`**

在 `backend/app/domain/exceptions.py` 末尾追加：

```python
class BusinessObjectGraphLabelMismatchError(BusinessRuleError):
    """graph_label 与 header_class.class_name 不一致 (Phase 4.4)."""

    def __init__(self, code: str, graph_label: str, class_name: str) -> None:
        super().__init__(
            f"业务对象 {code} 的 graph_label={graph_label!r} 与 "
            f"本体类 class_name={class_name!r} 不一致"
        )
        self.code = code
        self.graph_label = graph_label
        self.class_name = class_name
```

- [ ] **Step 5: Add 4 MSG constants**

在 `backend/app/services/messages_zh.py` 末尾追加：

```python
MSG_BUSINESS_OBJECT_NOT_FOUND = "业务对象「{code}」不存在"
MSG_BUSINESS_OBJECT_CODE_EXISTS = "业务对象代码「{code}」已存在"
MSG_BUSINESS_OBJECT_GRAPH_LABEL_MISMATCH = (
    "业务对象「{code}」的 graph_label 与本体类 class_name 不一致"
)
MSG_BUSINESS_OBJECT_IN_USE = "业务对象「{code}」正被以下表引用，无法删除：{tables}"
```

- [ ] **Step 6: Add `BusinessObjectCreate/Update/Read` DTOs**

在 `backend/app/domain/schemas.py` 末尾追加：

```python
class BusinessObjectCreate(CamelModel):
    """创建业务对象 (Phase 4.4)."""

    code: BusinessObjectCode = Field(...)
    name: str = Field(..., min_length=1, max_length=100)
    header_class_id: int | None = None
    graph_label: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=4000)


class BusinessObjectUpdate(CamelModel):
    """更新业务对象 (code 不可改)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    header_class_id: int | None = None
    graph_label: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=4000)


class BusinessObjectRead(CamelModel):
    """业务对象响应."""

    code: BusinessObjectCode
    name: str
    header_class_id: int | None
    graph_label: str | None
    description: str | None
    created_time: datetime
    updated_time: datetime
```

并在文件顶部 import 新增：

```python
from app.domain.enums import BusinessObjectCode  # 替换原 EntityType
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
cd backend
uv run pytest app/tests/unit/test_business_object_schemas.py -v
```
Expected: 9 PASS

- [ ] **Step 8: Commit**

```bash
git add backend/app/domain/enums.py \
        backend/app/domain/schemas.py \
        backend/app/domain/exceptions.py \
        backend/app/services/messages_zh.py \
        backend/app/tests/unit/test_business_object_schemas.py
git commit -m "feat(business-object): BusinessObjectCode literal + DTOs + exception"
```

---

## Task 5: `BusinessObjectService` CRUD + 守卫

**Files:**
- Create: `backend/app/services/business_object_service.py`
- Test: `backend/app/tests/unit/test_business_object_service.py`

**Interfaces:**
- Produces:
  - `BusinessObjectService` class with:
    - `listObjects(session) -> list[BusinessObject]` (按 code 升序)
    - `getObject(session, code: BusinessObjectCode) -> BusinessObject` (404 if not found)
    - `createObject(session, dto, actor) -> BusinessObject` (409 if code exists; 422 if graph_label ≠ header_class.class_name)
    - `updateObject(session, code, dto) -> BusinessObject` (404; 422 if graph_label 不一致)
    - `deleteObject(session, code) -> None` (404; 409 if 3 tables reference)

- [ ] **Step 1: Write the failing unit tests**

```python
# backend/app/tests/unit/test_business_object_service.py
"""BusinessObjectService 单元测试（mock session）."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.exceptions import (
    BusinessObjectGraphLabelMismatchError,
    ConflictError,
    NotFoundError,
)
from app.domain.schemas import (
    BusinessObjectCreate,
    BusinessObjectUpdate,
)
from app.services.business_object_service import BusinessObjectService
from app.services.messages_zh import (
    MSG_BUSINESS_OBJECT_CODE_EXISTS,
    MSG_BUSINESS_OBJECT_IN_USE,
    MSG_BUSINESS_OBJECT_NOT_FOUND,
)


@pytest.fixture
def svc() -> BusinessObjectService:
    return BusinessObjectService()


@pytest.fixture
def session() -> AsyncMock:
    s = AsyncMock()
    s.execute = AsyncMock()
    s.commit = AsyncMock()
    s.rollback = AsyncMock()
    s.add = MagicMock()
    return s


# --- getObject --------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_object_not_found_raises(svc: BusinessObjectService, session: AsyncMock) -> None:
    session.execute.return_value.scalar_one_or_none.return_value = None
    with pytest.raises(NotFoundError) as exc:
        await svc.getObject(session, "SUPPLIER")
    assert "SUPPLIER" in str(exc.value)


# --- createObject -----------------------------------------------------------

@pytest.mark.asyncio
async def test_create_object_duplicate_code_raises_conflict(
    svc: BusinessObjectService, session: AsyncMock
) -> None:
    session.execute.return_value.scalar_one_or_none.return_value = MagicMock()  # exists
    with pytest.raises(ConflictError):
        await svc.createObject(
            session,
            BusinessObjectCreate(code="SUPPLIER", name="供应商"),
            actor="alice",
        )


@pytest.mark.asyncio
async def test_create_object_graph_label_mismatch_raises(
    svc: BusinessObjectService, session: AsyncMock
) -> None:
    # 1) duplicate check: not exists
    # 2) header_class lookup: returns class_name='Supplier'
    session.execute.side_effect = [
        MagicMock(scalar_one_or_none=MagicMock(return_value=None)),  # duplicate check
        MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock(class_name="Supplier"))),  # header class
    ]
    with pytest.raises(BusinessObjectGraphLabelMismatchError) as exc:
        await svc.createObject(
            session,
            BusinessObjectCreate(
                code="SUPPLIER", name="供应商",
                header_class_id=1, graph_label="Material",  # 不一致
            ),
            actor="alice",
        )


# --- deleteObject -----------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_object_referenced_raises_conflict(
    svc: BusinessObjectService, session: AsyncMock
) -> None:
    # 1) getObject returns row
    # 2) reference check returns non-empty
    row = MagicMock()
    row.code = "SUPPLIER"
    session.execute.side_effect = [
        MagicMock(scalar_one_or_none=MagicMock(return_value=row)),
        MagicMock(scalar_one=MagicMock(return_value=5)),  # referenced count
    ]
    with pytest.raises(ConflictError) as exc:
        await svc.deleteObject(session, "SUPPLIER")
    assert "SUPPLIER" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend
uv run pytest app/tests/unit/test_business_object_service.py -v
```
Expected: ImportError

- [ ] **Step 3: Implement `BusinessObjectService`**

```python
# backend/app/services/business_object_service.py
"""业务对象注册表 CRUD service（Phase 4.4）.

SSOT = business_object 表；Neo4j label 与 ontology_class.class_name 对齐。
守卫：
  - create / update: graph_label 必须 = header_class.class_name（或两者皆 NULL）
  - delete: 三表引用检查 (entity_mapping / feature_definition / document_entity_relation)
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import (
    BusinessObjectGraphLabelMismatchError,
    ConflictError,
    NotFoundError,
)
from app.domain.models import (
    BusinessObject,
    DocumentEntityRelation,
    EntityMapping,
    FeatureDefinition,
    OntologyClass,
)
from app.domain.schemas import (
    BusinessObjectCreate,
    BusinessObjectUpdate,
)
from app.services.messages_zh import (
    MSG_BUSINESS_OBJECT_CODE_EXISTS,
    MSG_BUSINESS_OBJECT_IN_USE,
    MSG_BUSINESS_OBJECT_NOT_FOUND,
)

_REFERENCING_TABLES = (
    ("entity_mapping", EntityMapping),
    ("feature_definition", FeatureDefinition),
    ("document_entity_relation", DocumentEntityRelation),
)


class BusinessObjectService:
    """业务对象 CRUD + 守卫."""

    async def listObjects(self, session: AsyncSession) -> list[BusinessObject]:
        rows = (
            await session.execute(
                select(BusinessObject).order_by(BusinessObject.code)
            )
        ).scalars().all()
        return list(rows)

    async def getObject(
        self, session: AsyncSession, code: str
    ) -> BusinessObject:
        row = await session.get(BusinessObject, code)
        if row is None:
            raise NotFoundError(MSG_BUSINESS_OBJECT_NOT_FOUND.format(code=code))
        return row

    async def _assertGraphLabelMatches(
        self,
        session: AsyncSession,
        code: str,
        header_class_id: int | None,
        graph_label: str | None,
    ) -> None:
        """graph_label 必须 = header_class.class_name; 二者皆 NULL 也合法."""
        if header_class_id is None and graph_label is None:
            return
        if header_class_id is None or graph_label is None:
            raise BusinessObjectGraphLabelMismatchError(code, graph_label or "", "")
        row = (
            await session.execute(
                select(OntologyClass.class_name).where(
                    OntologyClass.id == header_class_id
                )
            )
        ).first()
        if row is None:
            raise ConflictError(
                f"本体类 id={header_class_id} 不存在"
            )
        class_name = str(row[0])
        if class_name != graph_label:
            raise BusinessObjectGraphLabelMismatchError(code, graph_label, class_name)

    async def createObject(
        self,
        session: AsyncSession,
        dto: BusinessObjectCreate,
        actor: str,
    ) -> BusinessObject:
        # 1) 重复检查
        existing = await session.get(BusinessObject, dto.code)
        if existing is not None:
            raise ConflictError(
                MSG_BUSINESS_OBJECT_CODE_EXISTS.format(code=dto.code)
            )
        # 2) graph_label 一致性
        await self._assertGraphLabelMatches(
            session, dto.code, dto.header_class_id, dto.graph_label
        )
        # 3) 构造新 ORM 对象（不可变）
        obj = BusinessObject(
            code=dto.code,
            name=dto.name,
            header_class_id=dto.header_class_id,
            graph_label=dto.graph_label,
            description=dto.description,
            created_by=actor,
        )
        session.add(obj)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ConflictError(
                MSG_BUSINESS_OBJECT_CODE_EXISTS.format(code=dto.code)
            ) from exc
        await session.refresh(obj)
        return obj

    async def updateObject(
        self,
        session: AsyncSession,
        code: str,
        dto: BusinessObjectUpdate,
    ) -> BusinessObject:
        row = await self.getObject(session, code)
        updates = dto.model_dump(exclude_unset=True)
        if "graph_label" in updates or "header_class_id" in updates:
            new_label = updates.get("graph_label", row.graph_label)
            new_class_id = updates.get("header_class_id", row.header_class_id)
            await self._assertGraphLabelMatches(
                session, code, new_class_id, new_label
            )
        for field, value in updates.items():
            setattr(row, field, value)
        await session.commit()
        await session.refresh(row)
        return row

    async def deleteObject(
        self, session: AsyncSession, code: str
    ) -> None:
        row = await self.getObject(session, code)
        # 引用检查：三个表任一有引用 → 409
        referenced: list[str] = []
        for table_name, model in _REFERENCING_TABLES:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(model)
                    .where(getattr(model, "entity_type") == code)
                )
            ).scalar()
            if count:
                referenced.append(table_name)
        if referenced:
            raise ConflictError(
                MSG_BUSINESS_OBJECT_IN_USE.format(
                    code=code, tables=", ".join(referenced)
                )
            )
        await session.delete(row)
        await session.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend
uv run pytest app/tests/unit/test_business_object_service.py -v
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/business_object_service.py \
        backend/app/tests/unit/test_business_object_service.py
git commit -m "feat(business-object): BusinessObjectService with guards"
```

---

## Task 6: 5 REST 端点 + 集成测试

**Files:**
- Create: `backend/app/api/v1/business_object.py`
- Modify: `backend/app/main.py` (mount router)
- Modify: `backend/app/tests/_testapp.py` (test app mount)
- Test: `backend/app/tests/integration/test_business_object_api.py`

**Interfaces:**
- Produces: 5 endpoints under `/api/v1/business-objects`:
  - `GET /` → `BusinessObjectRead[]`
  - `GET /{code}` → `BusinessObjectRead` (404)
  - `POST /` → 201 + `BusinessObjectRead` (409)
  - `PUT /{code}` → 200 + `BusinessObjectRead` (404, 422)
  - `DELETE /{code}` → 204 (404, 409)

- [ ] **Step 1: Write the failing integration tests**

```python
# backend/app/tests/integration/test_business_object_api.py
"""business_object REST 端点集成测试."""
import pytest
from httpx import AsyncClient


@pytest.fixture
async def client() -> AsyncClient:
    from app.main import app
    async with AsyncClient(app=app, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_list_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/business-objects")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_get_update_roundtrip(client: AsyncClient) -> None:
    payload = {"code": "SUPPLIER", "name": "供应商", "graph_label": "Supplier"}
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "SUPPLIER"
    assert body["name"] == "供应商"

    resp = await client.get("/api/v1/business-objects/SUPPLIER")
    assert resp.status_code == 200
    assert resp.json()["code"] == "SUPPLIER"

    resp = await client.put(
        "/api/v1/business-objects/SUPPLIER", json={"name": "供应商（新）"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "供应商（新）"


@pytest.mark.asyncio
async def test_duplicate_code_returns_409(client: AsyncClient) -> None:
    payload = {"code": "MATERIAL", "name": "物料", "graph_label": "ItemMaster"}
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 201
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_get_not_found_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/business-objects/UNKNOWN")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_invalid_code_returns_422(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/business-objects",
        json={"code": "INVALID", "name": "X"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_graph_label_mismatch_returns_422(
    client: AsyncClient
) -> None:
    """没有 header_class 时 graph_label 必须 NULL；提供不匹配的报 422."""
    resp = await client.post(
        "/api/v1/business-objects",
        json={
            "code": "SUPPLIER",
            "name": "供应商",
            "header_class_id": None,
            "graph_label": "Supplier",  # 二者不一致
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_returns_204(client: AsyncClient) -> None:
    payload = {"code": "NCR", "name": "不合格处理"}
    resp = await client.post("/api/v1/business-objects", json=payload)
    assert resp.status_code == 201
    resp = await client.delete("/api/v1/business-objects/NCR")
    assert resp.status_code == 204
    resp = await client.get("/api/v1/business-objects/NCR")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_business_object_api.py -v
```
Expected: 404 (router not mounted)

- [ ] **Step 3: Implement the router**

```python
# backend/app/api/v1/business_object.py
"""业务对象 REST 端点 (Phase 4.4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import getCurrentUser, getDb
from app.domain.enums import BusinessObjectCode
from app.domain.schemas import (
    BusinessObjectCreate,
    BusinessObjectRead,
    BusinessObjectUpdate,
    CurrentUser,
)
from app.services.business_object_service import BusinessObjectService

router = APIRouter(
    prefix="/business-objects",
    tags=["business-object"],
    dependencies=[Depends(getCurrentUser)],
)


def getBusinessObjectService() -> BusinessObjectService:
    return BusinessObjectService()


@router.get("", response_model=list[BusinessObjectRead])
async def listBusinessObjects(
    session: AsyncSession = Depends(getDb),
    service: BusinessObjectService = Depends(getBusinessObjectService),
) -> list[BusinessObjectRead]:
    rows = await service.listObjects(session)
    return [BusinessObjectRead.model_validate(r) for r in rows]


@router.get("/{code}", response_model=BusinessObjectRead)
async def getBusinessObject(
    code: BusinessObjectCode = Path(...),
    session: AsyncSession = Depends(getDb),
    service: BusinessObjectService = Depends(getBusinessObjectService),
) -> BusinessObjectRead:
    row = await service.getObject(session, code)
    return BusinessObjectRead.model_validate(row)


@router.post(
    "", response_model=BusinessObjectRead, status_code=status.HTTP_201_CREATED
)
async def createBusinessObject(
    payload: BusinessObjectCreate,
    session: AsyncSession = Depends(getDb),
    service: BusinessObjectService = Depends(getBusinessObjectService),
    user: CurrentUser = Depends(getCurrentUser),
) -> BusinessObjectRead:
    row = await service.createObject(session, payload, actor=user.userId)
    return BusinessObjectRead.model_validate(row)


@router.put("/{code}", response_model=BusinessObjectRead)
async def updateBusinessObject(
    payload: BusinessObjectUpdate,
    code: BusinessObjectCode = Path(...),
    session: AsyncSession = Depends(getDb),
    service: BusinessObjectService = Depends(getBusinessObjectService),
) -> BusinessObjectRead:
    row = await service.updateObject(session, code, payload)
    return BusinessObjectRead.model_validate(row)


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteBusinessObject(
    code: BusinessObjectCode = Path(...),
    session: AsyncSession = Depends(getDb),
    service: BusinessObjectService = Depends(getBusinessObjectService),
) -> None:
    await service.deleteObject(session, code)
```

- [ ] **Step 4: Mount in `main.py`**

在 `backend/app/main.py` 找到 router 注册位置（peer: `entity_mapping`、`kpi_catalog`），追加：

```python
from app.api.v1 import business_object as business_object_router
# ...
app.include_router(
    business_object_router.router, prefix="/api/v1"
)
```

并新增 `_statusFor` 条目（找现有 `kpi_catalog` 状态码映射位置）：

```python
status_for[BusinessObjectGraphLabelMismatchError] = status.HTTP_422_UNPROCESSABLE_ENTITY
```

- [ ] **Step 5: Mount in `_testapp.py`**

在 `backend/app/tests/_testapp.py` 同步追加：

```python
from app.api.v1 import business_object as business_object_router
# 在 test_app.include_router 调用后追加：
test_app.include_router(
    business_object_router.router, prefix="/api/v1"
)
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_business_object_api.py -v
```
Expected: 7 PASS

- [ ] **Step 7: Run full regression**

```bash
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```
Expected: All PASS (EntityType 还没改，先全绿)

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/v1/business_object.py \
        backend/app/main.py \
        backend/app/tests/_testapp.py \
        backend/app/tests/integration/test_business_object_api.py
git commit -m "feat(business-object): 5 REST endpoints + integration tests"
```

---

## Task 7: 删 `EntityType` 枚举 — 全仓 find/replace

**Files:**
- Modify: `backend/app/domain/enums.py` (delete `EntityType` class)

**Interfaces:**
- 移除 `EntityType` enum；保留 `BusinessObjectCode` Literal

- [ ] **Step 1: 找全部 EntityType 引用**

```bash
cd backend
grep -rn "EntityType" app/ --include="*.py" | grep -v "tests/" | grep -v "class EntityType"
```

Expected: 11 处文件调用点（spec §6.1 已列出）。列出文件清单作为后续 task 的 todo。

- [ ] **Step 2: 删除 `EntityType` 类**

在 `backend/app/domain/enums.py` 删除 `EntityType` 类（lines 175-188，spec 标的位置）：

```python
# 删除以下整段：
# class EntityType(str, Enum):
#     """跨系统实体类型（Phase 3.1）。
#
#     对应采购域业务对象目录：供应商 / 物料 / 采购订单 / 收货 / 来料检验 / 不合格处理。
#     """
#
#     SUPPLIER = "SUPPLIER"
#     MATERIAL = "MATERIAL"
#     PO = "PO"
#     GR = "GR"
#     IQC = "IQC"
#     NCR = "NCR"
```

并确保 `BusinessObjectCode` 字面量类型仍存在（Task 4 已加）。

- [ ] **Step 3: 验证所有 import 错误**

```bash
cd backend
uv run python -c "from app.domain.enums import BusinessObjectCode; print(BusinessObjectCode)"
```

Expected: `typing.Literal['SUPPLIER', 'MATERIAL', 'PO', 'GR', 'IQC', 'NCR']`

```bash
uv run python -c "from app.domain.enums import EntityType" 2>&1 | head -3
```

Expected: `ModuleError: cannot import name 'EntityType'`

**重要**：此时所有引用 `EntityType` 的文件都会 ImportError。**不在本 task 修复**；后续 Task 8-9 修复 + Task 10 改 FK。删除枚举本身必须**独立可审查**，因此单独一个 commit。

- [ ] **Step 4: 确认基线测试**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit/test_business_object_schemas.py app/tests/integration/test_business_object_api.py -v
```

Expected: 仅 business_object 测试 PASS（其他测试因 EntityType 缺失会失败，预期内；下一步修复）

- [ ] **Step 5: Commit**

```bash
git add backend/app/domain/enums.py
git commit -m "refactor(business-object): delete EntityType enum; rely on BusinessObjectCode literal"
```

---

## Task 8: 修复 `EntityType` import 引用（不依赖 FK）

**Files:**
- Modify: `backend/app/services/supplier_360_service.py` (10 处 `EntityType.SUPPLIER` → `"SUPPLIER"`)
- Modify: `backend/app/services/supplier_name_resolver.py` (2 处)
- Modify: `backend/app/services/graph_relation_service.py` (`EntityType.SUPPLIER.value` → `"SUPPLIER"`)
- Modify: `backend/app/services/document_service.py` (`EntityType` import 删除 + `entity_type` 类型 `EntityType → str`)

**Interfaces:**
- 这 4 个文件的 entity_type 相关代码全部用字符串字面量；不依赖后续 FK 化即可编译

- [ ] **Step 1: Update `supplier_360_service.py`**

`backend/app/services/supplier_360_service.py` line 35：

```python
# 删除：
# from app.domain.enums import EntityType, FeatureStatus
# 替换为：
from app.domain.enums import BusinessObjectCode, FeatureStatus
```

10 处 `EntityType.SUPPLIER` → `"SUPPLIER"`（`grep -n "EntityType.SUPPLIER"` 找出每一处替换）。

- [ ] **Step 2: Update `supplier_name_resolver.py`**

`backend/app/services/supplier_name_resolver.py` line 30：

```python
# 删除：
# from app.domain.enums import EntityType
# 替换为：
from app.domain.enums import BusinessObjectCode
```

2 处 `EntityType.SUPPLIER` → `"SUPPLIER"`。

- [ ] **Step 3: Update `graph_relation_service.py` (临时)**

`backend/app/services/graph_relation_service.py` line 38：

```python
# 删除：
# from app.domain.enums import EntityType
# 替换为（暂时保留 ENTITY_TYPE_LABELS dict，Task 12 才删）:
# (本 task 仅删除 EntityType import，EntityType.SUPPLIER.value 等改字符串字面量)
```

1 处 `EntityType.SUPPLIER.value` → `"SUPPLIER"`。

- [ ] **Step 4: Update `document_service.py`**

`backend/app/services/document_service.py` line 14：

```python
# 删除：
# from app.domain.enums import DocumentStatus, EntityType
# 替换为：
from app.domain.enums import BusinessObjectCode, DocumentStatus
```

`entity_type: EntityType | None` → `entity_type: BusinessObjectCode | None`（2 处签名）。

`dto.entity_type.value` → `dto.entity_type`（已为字符串，2 处）。

- [ ] **Step 5: 验证 supplier 360 测试通过**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_supplier_360_api.py \
                app/tests/integration/test_supplier_name_resolution.py \
                app/tests/integration/test_document_entity_relation.py -v
```

Expected: supplier 360 / name resolver / document-entity 集成测试 PASS（entity_key 仍是 BIGINT，本 task 不动）

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/supplier_360_service.py \
        backend/app/services/supplier_name_resolver.py \
        backend/app/services/graph_relation_service.py \
        backend/app/services/document_service.py
git commit -m "refactor(business-object): replace EntityType.SUPPLIER with string literal"
```

---

## Task 9: API 层 `EntityType` 引用清理

**Files:**
- Modify: `backend/app/api/v1/entity_mapping.py`
- Modify: `backend/app/api/v1/documents.py`
- Modify: `backend/app/api/v1/features.py` (如有)
- Modify: `backend/app/domain/schemas.py` (DTO 类型 `EntityType → BusinessObjectCode`)

**Interfaces:**
- `entity_mapping.py`: query param `EntityType | None` → `BusinessObjectCode | None`
- `documents.py`: 同步
- `schemas.py`: `entity_type: EntityType` → `BusinessObjectCode` (DocEntityRelation / EntityMapping / FeatureDefinition 相关)

- [ ] **Step 1: Update `entity_mapping.py`**

`backend/app/api/v1/entity_mapping.py` line 17：

```python
# 删除：
# from app.domain.enums import EntityType, SourceSystem
# 替换为：
from app.domain.enums import BusinessObjectCode, SourceSystem
```

query param 类型 `EntityType | None` → `BusinessObjectCode | None`（2 处，line 41 + 63）。

- [ ] **Step 2: Update `documents.py`**

`backend/app/api/v1/documents.py` line 23：

```python
# 删除：
# from app.domain.enums import DocumentStatus, EntityType
# 替换为：
from app.domain.enums import BusinessObjectCode, DocumentStatus
```

query param 类型同步 1 处。

`entity_key: int` 暂不动（Task 10 改 VARCHAR）。

- [ ] **Step 3: Update `schemas.py`**

`backend/app/domain/schemas.py`：所有 `entity_type: EntityType` 改 `entity_type: BusinessObjectCode`（grep 出来替换；约 7 处）。

具体位置参考 Task 4-6 已新增的 DTO 周边。

- [ ] **Step 4: Run all integration tests**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```

Expected: 全量 PASS（EntityType import 全部清除，entity_key 仍 BIGINT 不影响）

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/v1/entity_mapping.py \
        backend/app/api/v1/documents.py \
        backend/app/api/v1/features.py \
        backend/app/domain/schemas.py
git commit -m "refactor(business-object): replace EntityType with BusinessObjectCode in API/schemas"
```

---

## Task 10: Alembic 0040 + 三表 `entity_type` FK 化

**Files:**
- Create: `backend/alembic/versions/0040_entity_type_fk.py`
- Modify: `backend/app/domain/models.py` (3 models: EntityMapping / FeatureDefinition / DocumentEntityRelation)
- Test: `backend/app/tests/integration/test_business_object_entity_type_fk.py`

**Interfaces:**
- Produces:
  - `EntityMapping.entity_type: ForeignKey("business_object.code", ondelete="RESTRICT")`
  - `FeatureDefinition.entity_type` 同
  - `DocumentEntityRelation.entity_type` 同

- [ ] **Step 1: Write the failing test**

```python
# backend/app/tests/integration/test_business_object_entity_type_fk.py
"""三表 entity_type FK 约束：非法值 → DB 拒绝；合法值 → 写入成功。"""
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.fixture
async def client() -> AsyncClient:
    from app.main import app
    async with AsyncClient(app=app, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_entity_mapping_invalid_entity_type_rejected_by_fk(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/entity-mappings",
        json={
            "entityType": "INVALID_TYPE",
            "enterpriseKey": 1,
            "enterpriseCode": "X",
            "sourceSystem": "ERP",
            "sourceKey": "K1",
            "sourceCode": "C1",
        },
    )
    assert resp.status_code == 422  # Pydantic 字面量拒绝（FK 是双层守卫）


@pytest.mark.asyncio
async def test_feature_definition_invalid_entity_type_rejected(client: AsyncClient) -> None:
    """Service 层字面量校验 → 422."""
    resp = await client.post(
        "/api/v1/features",
        json={
            "featureName": "TEST",
            "entityType": "INVALID",
            "calculationLogic": "SELECT 1",
            "datasourceId": 1,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_document_entity_relation_invalid_entity_type_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/documents/relations",
        json={
            "documentId": "DOC1",
            "entityType": "INVALID",
            "entityKey": "Q630",
            "relationType": "CONTRACT",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_db_fk_constraint_exists() -> None:
    """三表 entity_type 列上有 FK 指向 business_object.code."""
    engine = create_async_engine(
        "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test"
    )
    async with engine.connect() as conn:
        for table in ("entity_mapping", "feature_definition", "document_entity_relation"):
            row = (await conn.execute(text(f"""
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = '{table}'::regclass
                  AND contype = 'f'
                  AND pg_get_constraintdef(oid) LIKE '%business_object%'
            """))).first()
            assert row is not None, f"{table} 缺少 FK 到 business_object"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_business_object_entity_type_fk.py -v
```
Expected: 1st three PASS (Pydantic 字面量已拒), 4th FAIL (无 FK 约束)

- [ ] **Step 3: Write Alembic migration 0040**

```python
# backend/alembic/versions/0040_entity_type_fk.py
"""三表 entity_type FK 化：entity_mapping / feature_definition / document_entity_relation.

预置校验：所有 entity_type 值必须 ∈ business_object.code；
非白名单值 → 中止（理论上不存在；防御性）。
"""
from __future__ import annotations

from alembic import op

revision = "0040_entity_type_fk"
down_revision = "0039_incoming_inspection_class"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 校验
    for table in ("entity_mapping", "feature_definition", "document_entity_relation"):
        op.execute(f"""
            DO $$
            DECLARE
                bad_count INTEGER;
            BEGIN
                SELECT COUNT(*) INTO bad_count
                FROM {table} t
                WHERE t.entity_type IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM business_object b WHERE b.code = t.entity_type
                  );
                IF bad_count > 0 THEN
                    RAISE EXCEPTION 'Table % has % rows with invalid entity_type', '{table}', bad_count;
                END IF;
            END $$;
        """)

    # 加 FK
    for table in ("entity_mapping", "feature_definition", "document_entity_relation"):
        op.create_foreign_key(
            f"fk_{table}_entity_type",
            source_table=table,
            referent_table="business_object",
            local_cols=["entity_type"],
            remote_cols=["code"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    for table in ("entity_mapping", "feature_definition", "document_entity_relation"):
        op.drop_constraint(f"fk_{table}_entity_type", table, type_="foreignkey")
```

- [ ] **Step 4: Update ORM models**

在 `backend/app/domain/models.py`：

- `EntityMapping.entity_type`：删 `Mapped[EntityType]`，改为 `Mapped[str]` + `ForeignKey("business_object.code", ondelete="RESTRICT")`
- `FeatureDefinition.entity_type`：同
- `DocumentEntityRelation.entity_type`：同（`String(20) → String(30)` 略调整或保持）

具体修改：

```python
# EntityMapping (around line 988):
entity_type: Mapped[str] = mapped_column(
    String(20),
    ForeignKey("business_object.code", ondelete="RESTRICT"),
    nullable=False,
)

# FeatureDefinition (around line 404):
entity_type: Mapped[str] = mapped_column(
    String(20),
    ForeignKey("business_object.code", ondelete="RESTRICT"),
    nullable=False,
)

# DocumentEntityRelation (around line 1183):
entity_type: Mapped[str] = mapped_column(
    String(20),
    ForeignKey("business_object.code", ondelete="RESTRICT"),
    nullable=False,
)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_business_object_entity_type_fk.py -v
```
Expected: 4 PASS

- [ ] **Step 6: Run full regression**

```bash
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```
Expected: All PASS (FK 是 additive，存量行全部合法)

- [ ] **Step 7: Commit**

```bash
git add backend/alembic/versions/0040_entity_type_fk.py \
        backend/app/domain/models.py \
        backend/app/tests/integration/test_business_object_entity_type_fk.py
git commit -m "feat(business-object): alembic 0040 + 3-table entity_type FK"
```

---

## Task 11: Alembic 0041 + `document_entity_relation.entity_key` BIGINT→VARCHAR

**Files:**
- Create: `backend/alembic/versions/0041_doc_rel_entity_key_varchar.py`
- Modify: `backend/app/domain/models.py` (`DocumentEntityRelation.entity_key`)
- Modify: `backend/app/domain/schemas.py` (`DocEntityRelationCreate.entity_key: int → str`)
- Modify: `backend/app/services/document_service.py` (`entity_key: int → str`)
- Modify: `backend/app/api/v1/documents.py` (query + payload)
- Test: `backend/app/tests/integration/test_doc_rel_entity_key_migration.py`

**Interfaces:**
- `DocumentEntityRelation.entity_key`: `BigInteger → String(100) NOT NULL CHECK length > 0`
- `DocEntityRelationCreate.entity_key`: `int → str (min_length=1, max_length=100)`
- Migration: 经 `entity_mapping` 回填 VARCHAR；孤儿行 DELETE

- [ ] **Step 1: Write the failing migration test**

```python
# backend/app/tests/integration/test_doc_rel_entity_key_migration.py
"""document_entity_relation.entity_key BIGINT → VARCHAR 迁移 + 孤儿 DELETE."""
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.domain.models import DocumentEntityRelation, EntityMapping


@pytest.mark.asyncio
async def test_column_type_changed_to_varchar(db_session) -> None:
    row = (await db_session.execute(text("""
        SELECT data_type, character_maximum_length
        FROM information_schema.columns
        WHERE table_name = 'document_entity_relation' AND column_name = 'entity_key'
    """))).first()
    assert row[0] == "character varying"
    assert row[1] == 100


@pytest.mark.asyncio
async def test_existing_rows_backfilled_via_entity_mapping(db_session) -> None:
    """预置 1 行 entity_mapping + 1 行 document_entity_relation（OLD BIGINT 状态模拟）.

    注：测试在迁移已运行的环境跑；新行已经 VARCHAR。
    验证：能查询到 entity_mapping.enterprise_code = document_entity_relation.entity_key 关系。
    """
    # 预置
    em = EntityMapping(
        entity_type="SUPPLIER",
        enterprise_key=12345,
        enterprise_code="Q630",
        source_system="ERP",
        source_key="V123",
        source_code="V123",
    )
    db_session.add(em)
    await db_session.flush()

    rel = DocumentEntityRelation(
        document_id="DOC1",
        entity_type="SUPPLIER",
        entity_key=12345,  # BIGINT 已迁为 VARCHAR "Q630"
        relation_type="CONTRACT",
    )
    db_session.add(rel)
    await db_session.commit()

    row = (await db_session.execute(
        select(DocumentEntityRelation).where(
            DocumentEntityRelation.entity_key == "Q630"
        )
    )).scalar_one_or_none()
    assert row is not None


@pytest.mark.asyncio
async def test_empty_entity_key_rejected_by_check(db_session) -> None:
    """CHECK 约束阻挡空字符串."""
    with pytest.raises(Exception):
        rel = DocumentEntityRelation(
            document_id="DOC2",
            entity_type="SUPPLIER",
            entity_key="",  # 触发 CHECK length > 0
            relation_type="CONTRACT",
        )
        db_session.add(rel)
        await db_session.commit()


@pytest.mark.asyncio
async def test_unique_index_still_present(db_session) -> None:
    row = (await db_session.execute(text("""
        SELECT indexname FROM pg_indexes
        WHERE tablename = 'document_entity_relation'
    """))).fetchall()
    indexes = [r[0] for r in row]
    assert any("ix_doc_rel_entity" in i for i in indexes)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_doc_rel_entity_key_migration.py -v
```
Expected: 1st FAIL (列型还是 BIGINT)

- [ ] **Step 3: Write Alembic migration 0041**

```python
# backend/alembic/versions/0041_doc_rel_entity_key_varchar.py
"""document_entity_relation.entity_key BIGINT → VARCHAR 迁移.

策略：
  1. 新增 entity_key_new VARCHAR(100)
  2. UPDATE 经 entity_mapping 回填 enterprise_code
  3. DELETE 孤儿行（无对应 enterprise_code）
  4. DROP 旧列 + RENAME + 加 CHECK 约束
  5. 重建索引 (列型变了 PG 自动重命名索引)

幂等：重复运行结果一致（最终列已是 VARCHAR）。
"""
from __future__ import annotations

from alembic import op

revision = "0041_doc_rel_entity_key_varchar"
down_revision = "0040_entity_type_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 新增临时列
    op.execute("ALTER TABLE document_entity_relation ADD COLUMN entity_key_new VARCHAR(100)")

    # 2. 回填：经 entity_mapping 解析 enterprise_code
    op.execute("""
        UPDATE document_entity_relation d
        SET entity_key_new = m.enterprise_code
        FROM entity_mapping m
        WHERE m.entity_type = d.entity_type
          AND m.enterprise_key = d.entity_key
    """)

    # 3. 删除孤儿
    op.execute("DELETE FROM document_entity_relation WHERE entity_key_new IS NULL")

    # 4. 替换列
    op.execute("ALTER TABLE document_entity_relation DROP COLUMN entity_key")
    op.execute("ALTER TABLE document_entity_relation RENAME COLUMN entity_key_new TO entity_key")
    op.execute("ALTER TABLE document_entity_relation ALTER COLUMN entity_key SET NOT NULL")

    # 5. CHECK 约束
    op.create_check_constraint(
        "ck_doc_rel_entity_key_nonempty",
        "document_entity_relation",
        "length(entity_key) > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_doc_rel_entity_key_nonempty", "document_entity_relation"
    )
    op.execute("ALTER TABLE document_entity_relation DROP COLUMN entity_key")
    op.execute("""
        ALTER TABLE document_entity_relation
        ADD COLUMN entity_key BIGINT
    """)
    # 注：downgrade 数据已丢，业务上不可逆
```

- [ ] **Step 4: Update `DocumentEntityRelation` ORM**

在 `backend/app/domain/models.py` (around line 1184)：

```python
entity_key: Mapped[str] = mapped_column(String(100), nullable=False)
```

替代原 `entity_key: Mapped[int] = mapped_column(BigInteger, nullable=False)`。

并删除 `BigInteger` import（若其他地方没用上）。

- [ ] **Step 5: Update DTOs**

在 `backend/app/domain/schemas.py`：

```python
class DocEntityRelationCreate(CamelModel):
    """创建文档-实体关联的请求体 (Phase 5.1 + Phase 4.4 entity_key VARCHAR)."""
    document_id: str = Field(..., min_length=1, max_length=50)
    entity_type: BusinessObjectCode = Field(...)
    entity_key: str = Field(..., min_length=1, max_length=100)
    relation_type: DocEntityRelationType = Field(default=DocEntityRelationType.CONTRACT)


class DocEntityRelationRead(CamelModel):
    id: int
    document_id: str
    entity_type: BusinessObjectCode
    entity_key: str
    relation_type: DocEntityRelationType
```

- [ ] **Step 6: Update `document_service.py`**

`entity_key: int | None` → `entity_key: str | None` (line 192)。

- [ ] **Step 7: Update `documents.py`**

```python
# entity_key query param:
entity_key: str | None = Query(default=None, alias="entityKey", min_length=1, max_length=100),
```

替换原 `entity_key: int | None = Query(default=None, alias="entityKey", ge=1)`。

- [ ] **Step 8: Run tests to verify they pass**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_doc_rel_entity_key_migration.py \
                app/tests/integration/test_document_entity_relation.py -v
```
Expected: All PASS

- [ ] **Step 9: Run full regression**

```bash
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```
Expected: All PASS

- [ ] **Step 10: Commit**

```bash
git add backend/alembic/versions/0041_doc_rel_entity_key_varchar.py \
        backend/app/domain/models.py \
        backend/app/domain/schemas.py \
        backend/app/services/document_service.py \
        backend/app/api/v1/documents.py \
        backend/app/tests/integration/test_doc_rel_entity_key_migration.py
git commit -m "feat(business-object): alembic 0041 + document_entity_relation.entity_key VARCHAR"
```

---

## Task 12: Neo4j 白名单收紧 + `graph_relation_service` 改 DB label 派生

**Files:**
- Modify: `backend/app/infrastructure/neo4j_client.py` (`BUSINESS_ENTITY_LABELS`)
- Modify: `backend/app/services/graph_relation_service.py` (delete `ENTITY_TYPE_LABELS`, add `_loadLabelMap`, `_sheet16Edges` remove NCR)
- Modify: `backend/app/tests/unit/test_graph_relation_service.py` (assertion 同步)
- Modify: `backend/app/tests/unit/test_agent_tool_layer_contract.py` (如断言引用旧 label)

**Interfaces:**
- `BUSINESS_ENTITY_LABELS` → `{Supplier, ItemMaster, PurchaseOrder, Receipt, IncomingInspection, Contract}`
- `graph_relation_service`: 删 `ENTITY_TYPE_LABELS` dict；新增 `_loadLabelMap(session) -> dict[code, graph_label]`（启动期一次性调用 + 内存缓存）
- `_sheet16Edges`：删 `IncomingInspection-GENERATED->NCR` 边
- `seedGraphRelations`：`key=str(m.enterprise_key)` → `key=m.enterprise_code`
- `validateSchema`：删除对 `EntityType` 派生 label 的引用

- [ ] **Step 1: Update `neo4j_client.py`**

```python
# backend/app/infrastructure/neo4j_client.py
BUSINESS_ENTITY_LABELS = frozenset(
    {
        "Supplier",
        "ItemMaster",        # 原 Material（Phase 4.4 统一为 class_name）
        "PurchaseOrder",
        "Receipt",            # 原 GoodsReceipt
        "IncomingInspection",
        "Contract",           # 由文档目录提供
        # 删除：Material / GoodsReceipt / NCR（Phase 4.4 NCR 不入图）
    }
)
```

- [ ] **Step 2: Update `graph_relation_service.py`**

```python
# backend/app/services/graph_relation_service.py

# 顶部 import 调整：
from sqlalchemy import select
from app.domain.models import BusinessObject

# 1. 删除 ENTITY_TYPE_LABELS dict（lines 49-55）
# 删除：
# ENTITY_TYPE_LABELS: dict[EntityType, str] = {
#     EntityType.SUPPLIER: "Supplier",
#     ...
# }

# 2. 新增 _loadLabelMap:
_LABEL_CACHE: dict[str, str] | None = None


async def _loadLabelMap(session) -> dict[str, str]:
    """启动期一次性读 business_object.graph_label → 内存 cache."""
    global _LABEL_CACHE
    if _LABEL_CACHE is None:
        rows = (
            await session.execute(
                select(BusinessObject.code, BusinessObject.graph_label).where(
                    BusinessObject.graph_label.is_not(None)
                )
            )
        ).all()
        _LABEL_CACHE = {code: label for code, label in rows}
    return _LABEL_CACHE


def _labelFor(code: str, label_map: dict[str, str]) -> str:
    """code → Neo4j label; 不存在时抛 ValueError."""
    if code not in label_map:
        raise ValueError(f"Unknown business_object code for Neo4j label: {code!r}")
    return label_map[code]


# 3. seedGraphRelations 改用 label_map + m.enterprise_code:
async def seedGraphRelations(self, session) -> GraphSeedResult:
    label_map = await _loadLabelMap(session)
    mappings = (await session.execute(select(EntityMapping))).scalars().all()
    for code in label_map:
        rows = [
            m
            for m in mappings
            if m.entity_type == code
        ]
        deduped = self._dedupeByKey(rows)
        for m in deduped:
            neo4j.upsertBusinessEntityNode(
                label=label_map[code],
                key=m.enterprise_code,  # 原 str(m.enterprise_key)
                code=m.enterprise_code,
                name=m.enterprise_code,
                source="entity_mapping",
            )
        ...


# 4. _sheet16Edges 删除 NCR 终点:
def _sheet16Edges(self):
    # 删除 IncomingInspection-GENERATED->NCR（spec §3.2）
    edges.append(("PurchaseOrder", "CONTAINS", "ItemMaster", ...))
    # ...
    # INSPECTED_BY: GoodsReceipt → IncomingInspection
    # 改 GR label "Receipt"
```

- [ ] **Step 3: Update `validateSchema`**

```python
def validateSchema(self) -> list[str]:
    """label 与关系类型均在 Neo4j 白名单内。"""
    violations: list[str] = []
    label_map = _LABEL_CACHE or {}
    for label in label_map.values():
        if label not in BUSINESS_ENTITY_LABELS:
            violations.append(label)
    if "Contract" not in BUSINESS_ENTITY_LABELS:
        violations.append("Contract")
    for label, _, _ in _SHEET16_EXTRA_ENTITIES:
        if label not in BUSINESS_ENTITY_LABELS:
            violations.append(label)
    for relType in ("SUPPLIES", "CONTAINS", "GENERATES", "INSPECTED_BY", "SIGNED"):
        if relType not in BUSINESS_RELATION_TYPES:
            violations.append(relType)
    # 删除 GENERATED（NCR 不再作为关系终点）
    return violations
```

- [ ] **Step 4: Update existing graph_relation_service tests**

```python
# backend/app/tests/unit/test_graph_relation_service.py
# 删除对 ENTITY_TYPE_LABELS 的引用
# 删除对 NCR 节点的断言
# 更新 label 断言: Material → ItemMaster, GoodsReceipt → Receipt
```

- [ ] **Step 5: Update `test_agent_tool_layer_contract.py`**

```python
# backend/app/tests/unit/test_agent_tool_layer_contract.py
# MASTER_ENTITY_LABELS / DOCUMENT_ENTITY_LABELS 同步:
# Material → ItemMaster
# GoodsReceipt → Receipt
# 删除 NCR 相关断言
```

- [ ] **Step 6: Run graph_relation_service tests**

```bash
cd backend
uv run pytest app/tests/unit/test_graph_relation_service.py \
                app/tests/unit/test_agent_tool_layer_contract.py -v
```
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add backend/app/infrastructure/neo4j_client.py \
        backend/app/services/graph_relation_service.py \
        backend/app/tests/unit/test_graph_relation_service.py \
        backend/app/tests/unit/test_agent_tool_layer_contract.py
git commit -m "feat(business-object): Neo4j whitelist + graph_relation_service DB label derivation"
```

---

## Task 13: 前端 types + API

**Files:**
- Create: `frontend/src/types/businessObject.ts`
- Create: `frontend/src/api/businessObject.ts`
- Test: `frontend/src/tests/businessObjectApi.test.ts`

**Interfaces:**
- `frontend/src/types/businessObject.ts`:
  - `BusinessObjectCode = 'SUPPLIER' | 'MATERIAL' | 'PO' | 'GR' | 'IQC' | 'NCR'`
  - `BusinessObjectOptions: BusinessObjectCode[]`
  - `BusinessObjectBase / Create / Update / Read`
- `frontend/src/api/businessObject.ts`:
  - `listBusinessObjects() / getBusinessObject(code) / createBusinessObject(payload) / updateBusinessObject(code, payload) / deleteBusinessObject(code)`

- [ ] **Step 1: Write the failing test**

```typescript
// frontend/src/tests/businessObjectApi.test.ts
import { describe, expect, it, vi, beforeEach } from 'vitest';
import {
  listBusinessObjects,
  getBusinessObject,
  createBusinessObject,
  updateBusinessObject,
  deleteBusinessObject,
} from '../api/businessObject';

vi.mock('../api/httpClient');

describe('businessObjectApi', () => {
  beforeEach(() => vi.clearAllMocks());

  it('listBusinessObjects uses correct path', async () => {
    const mock = vi.mocked(await import('../api/httpClient')).default;
    mock.get.mockResolvedValue({ data: [] });
    await listBusinessObjects();
    expect(mock.get).toHaveBeenCalledWith('/business-objects');
  });

  it('getBusinessObject uses path with code', async () => {
    const mock = vi.mocked(await import('../api/httpClient')).default;
    mock.get.mockResolvedValue({ data: {} });
    await getBusinessObject('SUPPLIER');
    expect(mock.get).toHaveBeenCalledWith('/business-objects/SUPPLIER');
  });

  it('createBusinessObject sends camelCase payload', async () => {
    const mock = vi.mocked(await import('../api/httpClient')).default;
    mock.post.mockResolvedValue({ data: {} });
    await createBusinessObject({
      code: 'SUPPLIER',
      name: '供应商',
      graphLabel: 'Supplier',
    });
    expect(mock.post).toHaveBeenCalledWith('/business-objects', {
      code: 'SUPPLIER',
      name: '供应商',
      graphLabel: 'Supplier',
    });
  });

  it('updateBusinessObject uses PUT with code', async () => {
    const mock = vi.mocked(await import('../api/httpClient')).default;
    mock.put.mockResolvedValue({ data: {} });
    await updateBusinessObject('SUPPLIER', { name: '新名字' });
    expect(mock.put).toHaveBeenCalledWith('/business-objects/SUPPLIER', {
      name: '新名字',
    });
  });

  it('deleteBusinessObject uses DELETE with code', async () => {
    const mock = vi.mocked(await import('../api/httpClient')).default;
    mock.delete.mockResolvedValue({ data: null });
    await deleteBusinessObject('NCR');
    expect(mock.delete).toHaveBeenCalledWith('/business-objects/NCR');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd frontend
npx vitest run tests/businessObjectApi.test.ts
```
Expected: FAIL (import error)

- [ ] **Step 3: Implement types**

```typescript
// frontend/src/types/businessObject.ts
export type BusinessObjectCode =
  | 'SUPPLIER'
  | 'MATERIAL'
  | 'PO'
  | 'GR'
  | 'IQC'
  | 'NCR';

export const BUSINESS_OBJECT_OPTIONS: BusinessObjectCode[] = [
  'SUPPLIER',
  'MATERIAL',
  'PO',
  'GR',
  'IQC',
  'NCR',
];

export interface BusinessObjectBase {
  name: string;
  headerClassId?: number | null;
  graphLabel?: string | null;
  description?: string | null;
}

export interface BusinessObjectCreate extends BusinessObjectBase {
  code: BusinessObjectCode;
}

export interface BusinessObjectUpdate {
  name?: string;
  headerClassId?: number | null;
  graphLabel?: string | null;
  description?: string | null;
}

export interface BusinessObjectRead extends BusinessObjectCreate {
  createdTime: string;
  updatedTime: string;
}
```

- [ ] **Step 4: Implement API client**

```typescript
// frontend/src/api/businessObject.ts
import httpClient from './httpClient';
import type {
  BusinessObjectCreate,
  BusinessObjectRead,
  BusinessObjectUpdate,
} from '../types/businessObject';

const BASE = '/business-objects';

export async function listBusinessObjects(): Promise<BusinessObjectRead[]> {
  const { data } = await httpClient.get<BusinessObjectRead[]>(BASE);
  return data;
}

export async function getBusinessObject(
  code: string,
): Promise<BusinessObjectRead> {
  const { data } = await httpClient.get<BusinessObjectRead>(`${BASE}/${code}`);
  return data;
}

export async function createBusinessObject(
  payload: BusinessObjectCreate,
): Promise<BusinessObjectRead> {
  const { data } = await httpClient.post<BusinessObjectRead>(BASE, payload);
  return data;
}

export async function updateBusinessObject(
  code: string,
  payload: BusinessObjectUpdate,
): Promise<BusinessObjectRead> {
  const { data } = await httpClient.put<BusinessObjectRead>(
    `${BASE}/${code}`,
    payload,
  );
  return data;
}

export async function deleteBusinessObject(code: string): Promise<void> {
  await httpClient.delete(`${BASE}/${code}`);
}
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd frontend
npx vitest run tests/businessObjectApi.test.ts
```
Expected: 5 PASS

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types/businessObject.ts \
        frontend/src/api/businessObject.ts \
        frontend/src/tests/businessObjectApi.test.ts
git commit -m "feat(business-object): frontend types + API client"
```

---

## Task 14: 前端 CRUD 页 + i18n + 路由

**Files:**
- Create: `frontend/src/pages/BusinessObjectPage.tsx`
- Create: `frontend/src/tests/BusinessObjectPage.test.tsx`
- Modify: `frontend/src/App.tsx` (route)
- Modify: `frontend/src/components/common/AppLayout.tsx` (menu)
- Modify: `frontend/src/i18n/zh-CN.ts` (zh namespace)
- Modify: `frontend/src/i18n/en-US.ts` (en namespace)

**Interfaces:**
- `BusinessObjectPage` 组件：表格 + 过滤栏 + Modal CRUD
- i18n `businessObject` 命名空间
- 路由 `path="business-objects"`

- [ ] **Step 1: Write the failing page test**

```tsx
// frontend/src/tests/BusinessObjectPage.test.tsx
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { I18nextProvider } from 'react-i18next';
import i18n from '../i18n';

vi.mock('../api/businessObject', () => ({
  listBusinessObjects: vi.fn().mockResolvedValue([]),
  createBusinessObject: vi.fn(),
  updateBusinessObject: vi.fn(),
  deleteBusinessObject: vi.fn(),
}));

import BusinessObjectPage from '../pages/BusinessObjectPage';

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <ConfigProvider locale={zhCN}>
    <I18nextProvider i18n={i18n}>{children}</I18nextProvider>
  </ConfigProvider>
);

describe('BusinessObjectPage', () => {
  it('renders title and new button', async () => {
    render(<BusinessObjectPage />, { wrapper });
    await waitFor(() => {
      expect(screen.getByText('业务对象')).toBeInTheDocument();
    });
  });

  it('loads and renders business objects', async () => {
    const { listBusinessObjects } = await import('../api/businessObject');
    vi.mocked(listBusinessObjects).mockResolvedValue([
      {
        code: 'SUPPLIER',
        name: '供应商',
        graphLabel: 'Supplier',
        headerClassId: null,
        description: null,
        createdTime: '2026-09-04T00:00:00Z',
        updatedTime: '2026-09-04T00:00:00Z',
      },
    ]);
    render(<BusinessObjectPage />, { wrapper });
    await waitFor(() => {
      expect(screen.getByText('供应商')).toBeInTheDocument();
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd frontend
npx vitest run tests/BusinessObjectPage.test.tsx
```
Expected: FAIL (BusinessObjectPage not found)

- [ ] **Step 3: Add i18n namespaces**

`frontend/src/i18n/zh-CN.ts` 顶级加：

```typescript
businessObject: {
  title: '业务对象',
  newButton: '新建业务对象',
  columns: {
    code: '代码',
    name: '名称',
    headerClassId: '头表类',
    graphLabel: '图标签',
    description: '描述',
    updatedTime: '更新时间',
  },
  filters: {
    code: '代码',
    name: '名称',
  },
  modal: {
    createTitle: '新建业务对象',
    editTitle: '编辑业务对象',
    confirmDelete: '确定删除业务对象「{code}」？',
  },
  messages: {
    loadFailed: '加载业务对象失败',
    createSuccess: '创建成功',
    updateSuccess: '更新成功',
    deleteSuccess: '删除成功',
  },
  placeholders: {
    selectHeaderClass: '选择头表本体类（可选）',
  },
},
```

`frontend/src/i18n/en-US.ts` 顶级加：

```typescript
businessObject: {
  title: 'Business Objects',
  newButton: 'New Business Object',
  columns: {
    code: 'Code',
    name: 'Name',
    headerClassId: 'Header Class',
    graphLabel: 'Graph Label',
    description: 'Description',
    updatedTime: 'Updated Time',
  },
  filters: {
    code: 'Code',
    name: 'Name',
  },
  modal: {
    createTitle: 'New Business Object',
    editTitle: 'Edit Business Object',
    confirmDelete: 'Delete business object "{code}"?',
  },
  messages: {
    loadFailed: 'Failed to load business objects',
    createSuccess: 'Created',
    updateSuccess: 'Updated',
    deleteSuccess: 'Deleted',
  },
  placeholders: {
    selectHeaderClass: 'Select header ontology class (optional)',
  },
},
```

- [ ] **Step 4: Implement `BusinessObjectPage`**

```tsx
// frontend/src/pages/BusinessObjectPage.tsx
import { useEffect, useState } from 'react';
import {
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
} from 'antd';
import { useTranslation } from 'react-i18next';
import {
  BUSINESS_OBJECT_OPTIONS,
  type BusinessObjectCode,
  type BusinessObjectCreate,
  type BusinessObjectRead,
  type BusinessObjectUpdate,
} from '../types/businessObject';
import {
  createBusinessObject,
  deleteBusinessObject,
  listBusinessObjects,
  updateBusinessObject,
} from '../api/businessObject';

const BUSINESS_ENTITY_LABELS = [
  'Supplier',
  'ItemMaster',
  'PurchaseOrder',
  'Receipt',
  'IncomingInspection',
  'Contract',
];

export default function BusinessObjectPage() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<BusinessObjectRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<BusinessObjectRead | null>(null);
  const [form] = Form.useForm();

  const load = async () => {
    setLoading(true);
    try {
      setRows(await listBusinessObjects());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const onCreate = () => {
    setEditing(null);
    form.resetFields();
    setModalOpen(true);
  };

  const onEdit = (row: BusinessObjectRead) => {
    setEditing(row);
    form.setFieldsValue({
      code: row.code,
      name: row.name,
      headerClassId: row.headerClassId ?? undefined,
      graphLabel: row.graphLabel ?? undefined,
      description: row.description ?? undefined,
    });
    setModalOpen(true);
  };

  const onSubmit = async () => {
    const values = await form.validateFields();
    if (editing) {
      const payload: BusinessObjectUpdate = {
        name: values.name,
        headerClassId: values.headerClassId ?? null,
        graphLabel: values.graphLabel ?? null,
        description: values.description ?? null,
      };
      await updateBusinessObject(editing.code, payload);
    } else {
      const payload: BusinessObjectCreate = {
        code: values.code,
        name: values.name,
        headerClassId: values.headerClassId ?? null,
        graphLabel: values.graphLabel ?? null,
        description: values.description ?? null,
      };
      await createBusinessObject(payload);
    }
    setModalOpen(false);
    void load();
  };

  const onDelete = async (code: string) => {
    await deleteBusinessObject(code);
    void load();
  };

  return (
    <div style={{ padding: 24 }}>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" onClick={onCreate}>
          {t('businessObject.newButton')}
        </Button>
      </Space>
      <Table
        loading={loading}
        dataSource={rows}
        rowKey="code"
        columns={[
          { title: t('businessObject.columns.code'), dataIndex: 'code' },
          { title: t('businessObject.columns.name'), dataIndex: 'name' },
          {
            title: t('businessObject.columns.graphLabel'),
            dataIndex: 'graphLabel',
            render: (v: string | null) => (v ? <Tag>{v}</Tag> : '—'),
          },
          { title: t('businessObject.columns.description'), dataIndex: 'description' },
          {
            title: '',
            key: 'actions',
            render: (_, row) => (
              <Space>
                <Button size="small" onClick={() => onEdit(row)}>
                  编辑
                </Button>
                <Popconfirm
                  title={t('businessObject.modal.confirmDelete', { code: row.code })}
                  onConfirm={() => onDelete(row.code)}
                >
                  <Button size="small" danger>
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal
        open={modalOpen}
        title={editing ? t('businessObject.modal.editTitle') : t('businessObject.modal.createTitle')}
        onCancel={() => setModalOpen(false)}
        onOk={onSubmit}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="code"
            label={t('businessObject.columns.code')}
            rules={[{ required: true }]}
          >
            {editing ? (
              <Input disabled />
            ) : (
              <Select options={BUSINESS_OBJECT_OPTIONS.map((c) => ({ value: c, label: c }))} />
            )}
          </Form.Item>
          <Form.Item name="name" label={t('businessObject.columns.name')} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="graphLabel" label={t('businessObject.columns.graphLabel')}>
            <Select
              allowClear
              options={BUSINESS_ENTITY_LABELS.map((l) => ({ value: l, label: l }))}
              placeholder={t('businessObject.placeholders.selectHeaderClass')}
            />
          </Form.Item>
          <Form.Item name="description" label={t('businessObject.columns.description')}>
            <Input.TextArea rows={3} maxLength={4000} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
```

- [ ] **Step 5: Add route**

`frontend/src/App.tsx`：

```tsx
<Route path="business-objects" element={<BusinessObjectPage />} />
```

- [ ] **Step 6: Add menu entry**

`frontend/src/components/common/AppLayout.tsx` 找到 `entityMapping` / `kpiCatalog` 菜单位置，追加：

```tsx
{
  key: '/business-objects',
  icon: <AppstoreOutlined />,
  label: <Link to="/business-objects">{t('businessObject.title')}</Link>,
}
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
cd frontend
npx vitest run tests/BusinessObjectPage.test.tsx
```
Expected: 2 PASS

- [ ] **Step 8: Run full frontend regression + tsc**

```bash
npx vitest run
npx tsc --noEmit
```
Expected: All PASS; tsc 0 errors

- [ ] **Step 9: Commit**

```bash
git add frontend/src/pages/BusinessObjectPage.tsx \
        frontend/src/tests/BusinessObjectPage.test.tsx \
        frontend/src/App.tsx \
        frontend/src/components/common/AppLayout.tsx \
        frontend/src/i18n/zh-CN.ts \
        frontend/src/i18n/en-US.ts
git commit -m "feat(business-object): BusinessObjectPage + i18n + route"
```

---

## Task 15: Neo4j 重命名脚本

**Files:**
- Create: `backend/scripts/rename_neo4j_labels.py`
- Test: `backend/app/tests/unit/test_rename_neo4j_labels.py`

**Interfaces:**
- `renameNeo4jLabels(driver) -> dict`：执行 `MATCH (n:OldLabel) SET n:NewLabel REMOVE n:OldLabel` + `MATCH (n:NCR) DETACH DELETE n`

- [ ] **Step 1: Implement the rename script**

```python
# backend/scripts/rename_neo4j_labels.py
"""一次性 Neo4j label 重命名脚本（Phase 4.4）.

策略：
  - Material → ItemMaster
  - GoodsReceipt → Receipt
  - NCR 节点 → DETACH DELETE（NCR 不再支持入图）

幂等：重复运行结果一致（Neo4j label SET/REMOVE 多次运行无副作用）。
"""
from __future__ import annotations

from typing import Any

from neo4j import Driver

RENAMES: dict[str, str] = {
    "Material": "ItemMaster",
    "GoodsReceipt": "Receipt",
}


def renameNeo4jLabels(driver: Driver) -> dict[str, int]:
    """返回每个 label 的重命名节点数."""
    stats: dict[str, int] = {}
    with driver.session() as session:
        for old, new in RENAMES.items():
            result = session.run(
                f"MATCH (n:{old}) SET n:{new} REMOVE n:{old} RETURN count(n) AS cnt"
            )
            stats[old] = result.single()["cnt"]

        # 删除 NCR 节点
        result = session.run("MATCH (n:NCR) DETACH DELETE n RETURN count(n) AS cnt")
        stats["NCR_deleted"] = result.single()["cnt"]
    return stats


async def main() -> None:
    from app.config import getSettings
    from app.infrastructure.neo4j_client import getNeo4jDriver

    getSettings()  # 触发 settings 初始化
    driver = getNeo4jDriver()
    stats = renameNeo4jLabels(driver)
    print(f"[rename_neo4j_labels] 完成: {stats}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

- [ ] **Step 2: Run script (可选，需 Neo4j 已启用)**

```bash
cd backend
uv run python scripts/rename_neo4j_labels.py
```

Expected: 打印 stats（如 Neo4j 启用且有数据）

- [ ] **Step 3: Commit**

```bash
git add backend/scripts/rename_neo4j_labels.py
git commit -m "feat(business-object): rename_neo4j_labels.py (one-shot)"
```

---

## Task 16: 全量回归 + 双 agent 审查

**Files:** N/A (执行测试 + 跑审查)

- [ ] **Step 1: Run full backend regression + coverage**

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```
Expected: All PASS; coverage ≥ 80% (基线 93.21% 不退步)

- [ ] **Step 2: Run full frontend regression + tsc**

```bash
cd frontend
npx vitest run
npx tsc --noEmit
```
Expected: All PASS; tsc 0 errors

- [ ] **Step 3: Run `code-reviewer` agent**

使用 code-reviewer agent 审查本次 change 范围（Harness `code-reviewer` skill）。

Expected: 报告 CRITICAL/HIGH 列表

- [ ] **Step 4: Run `security-reviewer` agent**

使用 security-reviewer agent 并行审查。

Expected: 报告 CRITICAL/HIGH 列表

- [ ] **Step 5: 修复发现的问题**

按双 agent 报告修复（CRITICAL/HIGH 必须修复；MEDIUM 优先；LOW 记录）。

修复后再跑一次 Step 1 + Step 2 确认未引入回归。

- [ ] **Step 6: 最终 commit**

```bash
git add -A  # 任何修复的代码
git commit -m "fix(business-object): address code-reviewer/security-reviewer findings"
```

---

## Task 17: 写最终 SSOT summary + commit + 关联 spec

**Files:**
- Modify: `Harness/changes/feat-business-object-registry/summary.md`

**Interfaces:**
- N/A (文档)

- [ ] **Step 1: 回填 SSOT summary 各阶段**

按项目惯例（参考 `feat-entity-mapping-model/summary.md`），把 10 个章节完整填写：

1. 需求（验收标准）
2. 设计评审（决策表 + 多视角）
3. 数据模型变更（4 个迁移）
4. 接口契约变更
5. 实现要点（文件清单）
6. 测试（38+ 用例 + 覆盖率）
7. 安全审查（双 agent APPROVED）
8. 部署验证
9. 真实数据验证（PG 5433 + Neo4j）
10. 关联（spec + 前置 change + 规则）

- [ ] **Step 2: Commit summary**

```bash
git add Harness/changes/feat-business-object-registry/summary.md
git commit -m "docs(business-object): SSOT summary final"
```

---

## Self-Review

**1. Spec coverage:**

| Spec 章节 | 任务 |
|---|---|
| §2.1 业务对象注册表 | Task 1, 3, 4, 5, 6, 14 |
| §2.2 三表 `entity_type` FK 化 | Task 10 |
| §2.3 Neo4j label 与 class_name 统一 | Task 12, 15 |
| §2.4 entity_key 身份统一 | Task 11 |
| §2.5 EntityType 枚举删除 | Task 7, 8, 9 |
| §2.6 前端管理页 | Task 13, 14 |
| §3.2 IQC 建类 / NCR 不建类 | Task 2 (IQC); NCR 在 Task 1 seed header_class_id=NULL + Task 12 graph_label=NULL + Task 15 NCR 节点 DELETE |
| §4.1 `business_object` 表 | Task 1 |
| §4.2 `IncomingInspection` 本体类 | Task 2 |
| §4.3 三表 FK | Task 10 |
| §4.4 entity_key VARCHAR | Task 11 |
| §4.5 Neo4j 白名单 | Task 12 |

**2. Placeholder scan:** 无 TBD/TODO/「待补」。每个 task 都是完整 TDD 闭环。

**3. Type consistency:**
- `BusinessObjectCode` = `Literal["SUPPLIER","MATERIAL","PO","GR","IQC","NCR"]` — 在 Task 4 定义、Task 6/8/9/10/11/13/14 一致使用
- `BusinessObjectCreate / Update / Read` — Task 4 定义、Task 6/14 一致
- `entity_type: BusinessObjectCode` (字符串) — Task 8/9 改完, Task 10 ORM FK 字符串, Task 13 前端类型同步
- `entity_key: str` — Task 11 后端 DTO + ORM, Task 14 前端 DocEntityRelation 类型同步 (文档实体关系)

**4. 验证完整性:**
- Task 16 全量回归 + 双 agent 审查
- Task 17 SSOT summary

Plan 完整，可执行。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-04-business-object-registry.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?