# chat 输出中文化（表头/图例中文名 + 单位 + 治理提示）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 chat 输出的表头与图表图例使用字段的中文含义与单位；缺失时回落原字段名，并在回答末尾提示用户去本体属性补充别名/单位。

**Architecture:** 新增一个纯查询解析层 `field_display_resolver`（本体属性 → KPI/特征单位，批量查询、绝不抛错），把解析结果注入既有 `buildSpec`（`aliasOverrides` + 新增 `units`），由 `ChartSpec.columnLabels` 一路带到 `chart_option` / `table_option`；另新增 `field_hint_text` 纯函数模块，把 `fieldHints` 渲染成回答末尾的治理提示，四条回答装配路径与研究报告统一调用。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2 async / Alembic / pytest（真实 PostgreSQL 5434）/ React 18 + TypeScript + vitest。

## Global Constraints

以下约束对**每个** Task 都成立，正文不再重复：

- 所有命令在 worktree `/private/tmp/qa-chat-output-zh-labels` 内执行；**不要 `cd` 回主仓**。
- 解释器：`/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python`；跑本 worktree 代码时 `PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend`。
- 每条 pytest 命令都要同时 export：
  - `TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test_zhlabels'`
  - `TEST_NEO4J_URI='bolt://127.0.0.1:7688'`
  - `TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'`
- DSN 必须 `postgresql+asyncpg://`；停 pytest 用 SIGINT。
- 测试库结构由 Alembic `upgrade head` 建立（`_pg_support`），**禁止** `Base.metadata.create_all`。
- TDD 强制：先写失败的测试并亲眼看到它失败（RED），再写最小实现（GREEN），最后重构。
- 不可变：所有产出都是新 dict / 新 dataclass；不原地改 `option`、`tableOption`、`rows`、`plan`。
- 函数 < 50 行、文件 ≤ 800 行、嵌套 ≤ 4 层；错误显式处理，不静默吞（解析层**唯一的**降级点是 `logger.warning` + 返回空 map，这是设计决策而非意外）。
- 每个 Task 结束跑一次该 Task 的定向测试再 commit；commit message 用 `<type>: <description>`，**不加** attribution。
- 前端命令在 `frontend/` 下执行；单测 `npm test -- --run <path>`，类型检查 `npx tsc --noEmit`。
- 不硬编码 secret；生产写入不在此计划范围内。

---

## File Structure

**新建**

| 文件 | 职责 |
|---|---|
| `backend/alembic/versions/0139_ontology_property_unit.py` | 给 `ontology_property` 加 `unit` 列 |
| `backend/app/services/field_display_resolver.py` | 列名 → 中文名/单位/是否数值（批量查询、不抛错） |
| `backend/app/services/field_hint_text.py` | `fieldHints` 合并 + 提示文案拼装（纯函数） |
| `backend/app/tests/unit/test_field_display_resolver.py` | 解析层单测（真实 PG） |
| `backend/app/tests/unit/test_field_hint_text.py` | 提示文案单测（纯函数） |
| `backend/app/tests/unit/test_chart_output_zh_labels.py` | spec/renderer/tableOption 的 columnLabels + units 单测 |
| `backend/app/tests/integration/test_chat_zh_labels.py` | 完整 API 链路：表头中文、fieldHints、回答末尾提示 |

**修改**

| 文件 | 改动 |
|---|---|
| `backend/app/domain/models.py` | `OntologyProperty.unit` |
| `backend/app/domain/schemas.py` | Create/Update/Read 加 `unit` + 空串归一 |
| `backend/app/services/ontology_service.py` | `createProperty` 透传 `unit`（update 走 `model_dump` 自动带上） |
| `backend/app/services/messages_zh.py` | 4 条提示文案常量 |
| `backend/app/domain/chart_spec.py` | `ChartSpec.columnLabels`；`tableSpec(columnLabels)`；`coerceSpec` 保留 labels |
| `backend/app/services/chart_spec_builder.py` | `aliasByField` 公开；`buildSpec(units=...)`；产出 `columnLabels` |
| `backend/app/services/chart_renderer.py` | `_buildTable` 输出 `columnLabels` |
| `backend/app/services/visual_payload.py` | `assembleTableOption(columnLabels=...)` |
| `backend/app/services/chart_service.py` | 解析 → 注入 spec → `option` 挂 `fieldHints` |
| `backend/app/services/chart_integration.py` | `sourceTable` 参数 + 解析 + 返回 labels/hints |
| `backend/app/services/report_planner.py` | `_chartContent` 传 `sourceTable` |
| `backend/app/services/chat_service.py` | 单步非流式：回答末尾追加提示 |
| `backend/app/services/chat_stream_query.py` | 单步流式：`answerPieces` 追加 + 发 token 帧 |
| `backend/app/services/chat_multistep.py` / `chat_stream_multistep.py` | 多步：汇总各步 hints/labels |
| `backend/app/services/report_renderer.py` | MD 表头用 labels；末尾追加提示 |
| `backend/app/services/pdf_export_service.py` | PDF 表头用 labels |
| `frontend/src/types/chat.ts` | `TablePayload.columnLabels?` |
| `frontend/src/utils/chartContract.ts` | `asTablePayload` 解析 labels；`asColumnLabels` |
| `frontend/src/components/chat/ChartRenderer.tsx` | `DataTable` 用中文表头 |
| `frontend/src/components/research/ReportRenderer.tsx` | 表格块用中文表头 |
| `frontend/src/types/ontology.ts` | `unit` 字段 |
| `frontend/src/components/ontology/PropertyTab.tsx` | 单位表单项 + 列 |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | 「单位」/「Unit」 |

**已确认的偏差（写进变更记录，不在本批实现）**

- `EvidencePanel` 的 `TopNTable` 表头保持英文：证据摘要 `trySummarizeEvidence` 是纯函数、没有 DB 会话，本批无法接入解析。后续可让 `report_planner` 在 evidence payload 里带 `columnLabels`。
- `plan.aggregations[].alias` 在本仓是**中文业务别名**（不是 SQL 结果列别名），因此「聚合列反查基础属性」只能按列名与属性名**大小写不敏感精确相等**来匹配；不做模糊匹配（避免猜错列）。

---

### Task 0: 测试环境准备

**Files:** 无代码改动。

**Interfaces:**
- Produces: 数据库 `qa_metadata_test_zhlabels`（后续所有 Task 使用）与已迁移到 head 的表结构。

- [ ] **Step 1: 创建独立测试库（已存在则跳过）**

```bash
PGPASSWORD=qa_pg_dev_2026 psql -h 127.0.0.1 -p 5434 -U qa_user -d postgres \
  -c 'CREATE DATABASE qa_metadata_test_zhlabels' || echo "数据库已存在，继续"
```

- [ ] **Step 2: 确认库结构与当前 head 一致**

```bash
cd /private/tmp/qa-chat-output-zh-labels/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test_zhlabels'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/integration/test_upload_size_limit.py -q -p no:randomly
```

Expected: 全部通过。该用例走 `client` fixture ⇒ 集成 conftest 的 `_pg_support` 会对测试库执行 `alembic upgrade head`（幂等），同时验证测试库可用。

- [ ] **Step 3: 记录基线（本分支改动前的失败集合）**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_chart_spec_builder.py app/tests/unit/test_visual_payload.py \
  app/tests/unit/test_pdf_export_charts.py app/tests/unit/test_display_name_resolver.py \
  -q -p no:randomly
```

Expected: 全绿。若已有红，记录失败集合到 `Harness/changes/2026-11-01-chat-output-zh-labels/evidence/baseline.txt`，后续用「净新增失败 0」判定。

---

### Task 1: `ontology_property.unit` 端到端

**Files:**
- Create: `backend/alembic/versions/0139_ontology_property_unit.py`
- Modify: `backend/app/domain/models.py`（`OntologyProperty`，`source_column` 之后）
- Modify: `backend/app/domain/schemas.py`（`OntologyPropertyCreate` / `Update` / `Read`）
- Modify: `backend/app/services/ontology_service.py:686-697`（`createProperty` 显式字段）
- Modify: `frontend/src/types/ontology.ts`
- Modify: `frontend/src/components/ontology/PropertyTab.tsx`
- Modify: `frontend/src/i18n/zh-CN.ts` / `en-US.ts`
- Test: `backend/app/tests/unit/test_ontology_property.py`（追加）
- Test: `backend/app/tests/integration/test_ontology_property_unit.py`（新建）

**Interfaces:**
- Produces: `ontology_property.unit VARCHAR(50) NULL`；`OntologyPropertyCreate/Update/Read.unit: str | None`；空串/空白在 schema 层归一为 `None`。Task 2 的解析层读取该列。

- [ ] **Step 1: 写失败测试（schema 层）**

在 `backend/app/tests/unit/test_ontology_property.py` 末尾追加：

```python
def test_property_create_accepts_unit():
    dto = OntologyPropertyCreate(
        class_id=1, property_name="RCV_QTY_PUU", data_type="DECIMAL", unit="件"
    )
    assert dto.unit == "件"


def test_property_create_blank_unit_normalizes_to_none():
    dto = OntologyPropertyCreate(
        class_id=1, property_name="RCV_QTY_PUU", data_type="DECIMAL", unit="   "
    )
    assert dto.unit is None


def test_property_update_blank_unit_normalizes_to_none():
    dto = OntologyPropertyUpdate(unit="")
    assert dto.unit is None
```

（文件顶部 import 改为 `from app.domain.schemas import OntologyPropertyCreate, OntologyPropertyUpdate`。）

- [ ] **Step 2: 运行确认失败**

```bash
cd /private/tmp/qa-chat-output-zh-labels/backend
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_ontology_property.py -q -p no:randomly
```

Expected: FAIL — `OntologyPropertyCreate` 不接受 `unit`（`ValidationError: Extra inputs are not permitted` 或 `unit` 属性不存在）。

- [ ] **Step 3: 迁移文件**

```python
"""ontology_property：unit 列（字段单位治理）。

2026-11-01，feat/chat-output-zh-labels。chat 输出的数值字段需要显示单位，
而白名单列里没有任何单位字段；KPI/特征表的 unit 覆盖面只到已登记指标。
给本体属性加可空 unit 作为**字段级**单位来源（解析优先级最高）。

可空：存量 3717 行与未登记单位的字段行为不变。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None

_TABLE = "ontology_property"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("unit", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "unit")
```

- [ ] **Step 4: ORM + schema**

`backend/app/domain/models.py`，在 `source_column` 下一行加：

```python
    # 字段单位（2026-11-01）：chat 输出把单位并入表头/图例（「收货数量（件）」）。
    # 可空 = 未定义；解析层据此产出「未定义单位」治理提示。
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
```

`backend/app/domain/schemas.py`，在 `_validateBusinessAliases` 之后加模块级函数：

```python
def _normalizeUnit(value: str | None) -> str | None:
    """空串/纯空白视作未定义（前端清空输入框会提交 ""）。"""
    if value is None:
        return None
    return value.strip() or None
```

`OntologyPropertyCreate` 在 `source_column` 之后加：

```python
    unit: str | None = Field(default=None, max_length=50)

    _check_unit = field_validator("unit")(_normalizeUnit)
```

`OntologyPropertyUpdate` 在 `source_column` 之后加：

```python
    unit: str | None = Field(default=None, max_length=50)

    _check_unit = field_validator("unit")(_normalizeUnit)
```

`OntologyPropertyRead` 在 `source_column` 之后加：

```python
    unit: str | None = None
```

- [ ] **Step 5: service 透传**

`backend/app/services/ontology_service.py` 的 `createProperty` 里，`source_column=dto.source_column,` 之后加：

```python
            unit=dto.unit,
```

`updateProperty` 不用改：`dto.model_dump(exclude_unset=True)` 已包含 `unit`，`setattr` 自动生效；`_entityToDict` 按 `__table__.columns.keys()` 自动带出新列。

- [ ] **Step 6: 运行 schema 单测**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_ontology_property.py -q -p no:randomly
```

Expected: PASS。

- [ ] **Step 7: 写集成测试（真实 PG + API）**

新建 `backend/app/tests/integration/test_ontology_property_unit.py`：

```python
"""ontology_property.unit 落库与清空（真实 PG + 完整 API 链路）。"""

from __future__ import annotations

from httpx import AsyncClient

_ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


async def _createClass(client: AsyncClient, className: str) -> int:
    """经 API 建类（与 test_ontology_api 同一入口），返回 classId。"""
    response = await client.post(
        "/api/v1/ontology/classes",
        json={"className": className, "sourceTable": f"zjth.{className.lower()}"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def test_create_and_update_unit_persists(client: AsyncClient) -> None:
    classId = await _createClass(client, "UnitReceipt")
    created = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": classId,
            "propertyName": "RCV_QTY_PUU",
            "dataType": "DECIMAL",
            "unit": "件",
        },
    )
    assert created.status_code == 201, created.text
    prop = created.json()
    assert prop["unit"] == "件"

    # 更新走 PUT（见 app/api/v1/ontology.py:269；本仓没有 PATCH 路由）
    updated = await client.put(
        f"/api/v1/ontology/properties/{prop['id']}",
        json={"unit": "吨"},
        headers=_ADMIN_HEADERS,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["unit"] == "吨"


async def test_blank_unit_clears_value(client: AsyncClient) -> None:
    classId = await _createClass(client, "UnitBlank")
    created = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": classId,
            "propertyName": "RCV_QTY_PUU",
            "dataType": "DECIMAL",
            "unit": "件",
        },
    )
    assert created.status_code == 201, created.text
    propId = created.json()["id"]

    cleared = await client.put(
        f"/api/v1/ontology/properties/{propId}",
        json={"unit": ""},
        headers=_ADMIN_HEADERS,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["unit"] is None
```

- [ ] **Step 8: 运行集成测试**

```bash
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test_zhlabels'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/integration/test_ontology_property_unit.py -q -p no:randomly
```

Expected: PASS（测试库自动迁移到 `0139`）。

- [ ] **Step 9: 前端类型 + 表单**

`frontend/src/types/ontology.ts`：

- `OntologyProperty` 在 `sourceColumn: string | null;` 后加 `unit?: string | null;`（可选：mock 与旧 client 不传不报错）。
- `OntologyPropertyCreate` 加 `unit?: string;`
- `OntologyPropertyUpdate` 加 `unit?: string | null;`（`null`/`""` = 清空，后端归一为 `None`）。

`frontend/src/components/ontology/PropertyTab.tsx`：

1. `PropertyFormValues` 加 `unit: string;`，`EMPTY_PROPERTY_FORM` 加 `unit: ""`。
2. `openEdit` 的 `setFieldsValue` 加 `unit: record.unit ?? ""`。
3. `handleSubmit` 的 `payload` 加 `unit: values.unit || undefined`；`updatePayload` 加 `unit: values.unit || null`（显式 null，与 `refClassId` 同理）。
4. 表单里在 `sourceColumn` 同一行的右侧 `Col` 换成两行：`dataType` / `sourceColumn` 保持，新增一行：

```tsx
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="unit"
                label={t("forms.ontology.propertyLabels.unit")}
                tooltip={t("forms.ontology.propertyLabels.unitHint")}
              >
                <Input placeholder={t("forms.ontology.propertyPlaceholders.unit")} />
              </Form.Item>
            </Col>
          </Row>
```

5. `columns` 在 `sourceColumn` 列后加：

```tsx
    { title: t("forms.ontology.propertyColumns.unit"), dataIndex: "unit", width: 90 },
```

6. `propertyFilterFields` 在 `sourceColumn` 之后加：

```tsx
    { key: "unit", label: t("forms.ontology.propertyLabels.unit") },
```

`frontend/src/i18n/zh-CN.ts` 的 `forms.ontology.propertyLabels` 加：

```ts
        unit: "单位",
        unitHint: "数值字段的计量单位（如 件 / 吨 / 元）。留空表示未定义，聊天输出会提示补充。",
```

`propertyPlaceholders` 加：

```ts
        unit: "如 件 / 吨 / 元",
```

`forms.ontology.propertyColumns` 加：

```ts
      unit: "单位",
```

`frontend/src/i18n/en-US.ts` 对应加：

```ts
        unit: "Unit",
        unitHint: "Unit of measure for numeric fields (e.g. pcs / ton / CNY). Leave blank if undefined; chat output will prompt for it.",
```

```ts
        unit: "e.g. pcs / ton / CNY",
```

```ts
      unit: "Unit",
```

- [ ] **Step 10: 前端类型检查 + 相关测试**

```bash
cd /private/tmp/qa-chat-output-zh-labels/frontend
npx tsc --noEmit
npm test -- --run src/components/ontology
```

Expected: tsc 0 错误；ontology 相关 vitest 全绿（若 `PropertyTab` 现有测试快照/表单字段断言需要补 `unit`，在本步一并修正为**断言新行为**而不是删断言）。

- [ ] **Step 11: Commit**

```bash
cd /private/tmp/qa-chat-output-zh-labels
git add backend/alembic/versions/0139_ontology_property_unit.py \
        backend/app/domain/models.py backend/app/domain/schemas.py \
        backend/app/services/ontology_service.py \
        backend/app/tests/unit/test_ontology_property.py \
        backend/app/tests/integration/test_ontology_property_unit.py \
        frontend/src/types/ontology.ts frontend/src/components/ontology/PropertyTab.tsx \
        frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts
git commit -m "feat: 本体属性增加 unit 字段（表头/图例单位来源）"
```

---

### Task 2: 字段显示解析层 `field_display_resolver`

**Files:**
- Create: `backend/app/services/field_display_resolver.py`
- Create: `backend/app/tests/unit/test_field_display_resolver.py`
- Modify: `backend/app/services/chart_spec_builder.py`（`_aliasByField` → 公开 `aliasByField`）

**Interfaces:**
- Consumes: `ontology_property.unit`（Task 1）；`OntologyClass` / `OntologyProperty` / `KpiCatalog` / `FeatureDefinition`；`DISPLAY_COLUMN_DICT`。
- Produces:
  - `FieldDisplay(label: str | None, unit: str | None, isNumeric: bool, source: str)`
  - `async resolveFieldDisplays(session, *, columns, rows, plan=None, sourceTable=None) -> dict[str, FieldDisplay]`
  - `missingFieldHints(columnLabels, displays) -> {"missingLabels": [...], "missingUnits": [...]}`
  - `chart_spec_builder.aliasByField(plan, columns, overrides=None) -> dict[str, str]`（原 `_aliasByField`，签名不变）

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/unit/test_field_display_resolver.py`：

```python
"""字段显示名/单位解析（真实 PG）。"""

from __future__ import annotations

from decimal import Decimal

from app.domain.models import (
    DataSource,
    FeatureDefinition,
    KpiCatalog,
    OntologyClass,
    OntologyProperty,
)
from app.domain.query_plan import Aggregation, QueryPlan
from app.services.field_display_resolver import (
    FieldDisplay,
    missingFieldHints,
    resolveFieldDisplays,
)


async def _seedDataSource(session, *, dsId: int = 9001) -> None:
    """feature_definition.datasource_id 是非空 FK，先补一行最小数据源。"""
    session.add(
        DataSource(
            id=dsId,
            name="ds-zh-labels-test",
            type="postgresql",
            host="localhost",
            port=5432,
            database_name="x",
            username="u",
            password_encrypted="x",
        )
    )
    await session.commit()


async def _seedClass(session, *, className: str = "PRECEIPT") -> OntologyClass:
    cls = OntologyClass(class_name=className, source_table="ZJTH.PRECEIPT")
    cls.properties = [
        OntologyProperty(
            property_name="SUPPLIER_CODE",
            property_alias="供应商编码",
            data_type="STRING",
            source_column="SUPPLIER_CODE",
        ),
        OntologyProperty(
            property_name="SUPPLIER_NAME",
            business_aliases=["供应商名称", "供货方"],
            data_type="STRING",
            source_column="SUPPLIER_NAME",
        ),
        OntologyProperty(
            property_name="RCV_QTY_PUU",
            property_alias="收货数量",
            unit="件",
            data_type="DECIMAL",
            source_column="RCV_QTY_PUU",
        ),
        OntologyProperty(
            property_name="PLAN_QTY",
            data_type="DECIMAL",
            source_column="PLAN_QTY",
        ),
    ]
    session.add(cls)
    await session.commit()
    await session.refresh(cls)
    return cls


def _plan(*properties: str) -> QueryPlan:
    """最小 plan：不写聚合别名（别名相关断言各自构造 plan）。"""
    return QueryPlan(
        target="测试",
        selectedClasses=["PRECEIPT"],
        selectedProperties=list(properties),
        conditions=[],
        aggregations=[],
        groupBy=[],
        joins=[],
        sortBy=[],
        rowLimit=None,
    )


class TestResolveFieldDisplays:
    async def test_ontology_alias_and_unit(self, dbSession) -> None:
        await _seedClass(dbSession)
        rows = [{"supplier_code": "B130", "rcv_qty_puu": Decimal("12")}]
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["supplier_code", "rcv_qty_puu"],
            rows=rows,
            plan=_plan("SUPPLIER_CODE", "RCV_QTY_PUU"),
        )
        assert displays["supplier_code"].label == "供应商编码"
        assert displays["supplier_code"].source == "ontology_alias"
        assert displays["supplier_code"].isNumeric is False
        assert displays["rcv_qty_puu"].label == "收货数量"
        assert displays["rcv_qty_puu"].unit == "件"
        assert displays["rcv_qty_puu"].isNumeric is True

    async def test_business_alias_fallback(self, dbSession) -> None:
        await _seedClass(dbSession)
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["supplier_name"],
            rows=[{"supplier_name": "甲"}],
            plan=_plan("SUPPLIER_NAME"),
        )
        assert displays["supplier_name"].label == "供应商名称"
        assert displays["supplier_name"].source == "business_alias"

    async def test_ontology_alias_wins_over_plan_alias(self, dbSession) -> None:
        await _seedClass(dbSession)
        plan = QueryPlan(
            target="测试",
            selectedClasses=["PRECEIPT"],
            selectedProperties=["RCV_QTY_PUU"],
            conditions=[],
            aggregations=[
                Aggregation(function="SUM", property="RCV_QTY_PUU", alias="占比")
            ],
            groupBy=[],
            joins=[],
            sortBy=[],
            rowLimit=None,
        )
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["rcv_qty_puu"],
            rows=[{"rcv_qty_puu": Decimal("1")}],
            plan=plan,
        )
        assert displays["rcv_qty_puu"].label == "收货数量"
        assert displays["rcv_qty_puu"].source == "ontology_alias"

    async def test_plan_alias_used_when_ontology_missing(self, dbSession) -> None:
        await _seedClass(dbSession)
        plan = QueryPlan(
            target="测试",
            selectedClasses=["PRECEIPT"],
            selectedProperties=["PLAN_QTY"],
            conditions=[],
            aggregations=[
                Aggregation(function="SUM", property="PLAN_QTY", alias="计划数量")
            ],
            groupBy=[],
            joins=[],
            sortBy=[],
            rowLimit=None,
        )
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["plan_qty"],
            rows=[{"plan_qty": Decimal("1")}],
            plan=plan,
        )
        assert displays["plan_qty"].label == "计划数量"
        assert displays["plan_qty"].source == "plan_alias"

    async def test_unresolved_column_is_none(self, dbSession) -> None:
        await _seedClass(dbSession)
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["total_rcv_qty"],
            rows=[{"total_rcv_qty": Decimal("5")}],
            plan=_plan("RCV_QTY_PUU"),
        )
        # 聚合结果列名与属性名不相等 ⇒ 不猜，落空并等待提示
        assert displays["total_rcv_qty"].label is None
        assert displays["total_rcv_qty"].source == "none"

    async def test_numeric_ignores_bool(self, dbSession) -> None:
        await _seedClass(dbSession)
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["is_ok"],
            rows=[{"is_ok": True}],
            plan=_plan("RCV_QTY_PUU"),
        )
        assert displays["is_ok"].isNumeric is False

    async def test_no_context_returns_empty(self, dbSession) -> None:
        assert (
            await resolveFieldDisplays(
                dbSession, columns=["x"], rows=[{"x": 1}], plan=None, sourceTable=None
            )
            == {}
        )

    async def test_ambiguous_property_label_is_none(self, dbSession) -> None:
        await _seedClass(dbSession)
        other = OntologyClass(class_name="PRECEIPT_2", source_table="ZJTH.PRECEIPT_2")
        other.properties = [
            OntologyProperty(
                property_name="SUPPLIER_CODE",
                property_alias="供货商编号",
                data_type="STRING",
                source_column="SUPPLIER_CODE",
            )
        ]
        dbSession.add(other)
        await dbSession.commit()
        plan = QueryPlan(
            target="测试",
            selectedClasses=["PRECEIPT", "PRECEIPT_2"],
            selectedProperties=["SUPPLIER_CODE"],
            conditions=[],
            aggregations=[],
            groupBy=[],
            joins=[],
            sortBy=[],
            rowLimit=None,
        )
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["supplier_code"],
            rows=[{"supplier_code": "B130"}],
            plan=plan,
        )
        assert displays["supplier_code"].label is None
        assert displays["supplier_code"].source == "none"

    async def test_kpi_unit_by_exact_code(self, dbSession) -> None:
        await _seedClass(dbSession)
        # PLAN_QTY 在本体里没有 unit ⇒ 单位只能来自 KPI 目录
        dbSession.add(
            KpiCatalog(
                kpi_code="PLAN_QTY",
                kpi_name="计划数量",
                unit="吨",
                owner="test",
            )
        )
        await dbSession.commit()
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["plan_qty"],
            rows=[{"plan_qty": Decimal("1")}],
            plan=_plan("PLAN_QTY"),
        )
        assert displays["plan_qty"].unit == "吨"

    async def test_feature_unit_by_exact_name(self, dbSession) -> None:
        await _seedClass(dbSession)
        await _seedDataSource(dbSession)
        # 该列在本体里既无属性也无单位 ⇒ 单位只能来自特征定义
        dbSession.add(
            FeatureDefinition(
                feature_name="rcv_qty_feature",
                feature_alias="收货数量特征",
                entity_type="SUPPLIER",
                calculation_logic="sum(rcv_qty)",
                unit="件",
                datasource_id=1,
            )
        )
        await dbSession.commit()
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["rcv_qty_feature"],
            rows=[{"rcv_qty_feature": Decimal("1")}],
            plan=None,
            sourceTable="ZJTH.PRECEIPT",
        )
        assert displays["rcv_qty_feature"].unit == "件"
        assert displays["rcv_qty_feature"].label is None

    async def test_ambiguous_unit_is_dropped(self, dbSession) -> None:
        await _seedClass(dbSession)
        dbSession.add_all(
            [
                KpiCatalog(kpi_code="PLAN_QTY", kpi_name="计划数量", unit="吨", owner="a"),
                KpiCatalog(kpi_code="PLAN_QTY_2", kpi_name="PLAN_QTY", unit="箱", owner="b"),
            ]
        )
        await dbSession.commit()
        displays = await resolveFieldDisplays(
            dbSession,
            columns=["plan_qty"],
            rows=[{"plan_qty": Decimal("1")}],
            plan=_plan("PLAN_QTY"),
        )
        # 同一名字命中两个不同单位 ⇒ 不猜
        assert displays["plan_qty"].unit is None


class TestMissingFieldHints:
    def test_lists_only_unresolved_and_unitless_numeric(self) -> None:
        displays = {
            "a": FieldDisplay(label="甲", unit=None, isNumeric=True, source="ontology_alias"),
            "b": FieldDisplay(label=None, unit=None, isNumeric=False, source="none"),
            "c": FieldDisplay(label=None, unit=None, isNumeric=True, source="none"),
        }
        hints = missingFieldHints({"a": "甲"}, displays)
        assert hints["missingLabels"] == ["b", "c"]
        assert hints["missingUnits"] == ["a", "c"]
```

- [ ] **Step 2: 运行确认失败**

```bash
cd /private/tmp/qa-chat-output-zh-labels/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test_zhlabels'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_field_display_resolver.py -q -p no:randomly
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.field_display_resolver'`。

- [ ] **Step 3: 公开 `aliasByField`**

`backend/app/services/chart_spec_builder.py`：把 `def _aliasByField(` 改为 `def aliasByField(`，第 258 行的调用改为 `aliasByField(...)`。函数体与文档字符串一字不改。模块内不再保留私有别名（仓内无其它调用点）。

- [ ] **Step 4: 实现解析层**

新建 `backend/app/services/field_display_resolver.py`：

```python
"""字段显示名 / 单位解析（本体 + KPI/特征 + 代码字典）。

**它是什么**：把结果集列名翻译成「中文名 + 单位 + 是否数值」，供表头、图例与
治理提示共用。一次调用最多 4 条批量查询（类 / 属性 / KPI / 特征），不做 N+1。

**优先级**（已确认的设计）：
- 中文名：`property_alias` → `business_aliases[0]` → plan 聚合中文别名 →
  `DISPLAY_COLUMN_DICT` → None；
- 单位：`ontology_property.unit` → `kpi_catalog.unit` / `feature_definition.unit`
  （名字归一后**精确匹配**，不做模糊匹配）→ None。

**歧义不猜**：同名属性命中多个类且取值不一致 ⇒ 视为「无中文含义」。
**无上下文不提示**：既无 plan 也无 sourceTable ⇒ 返回空 map（调用方据此不发提示）。

**绝不抛错**：查询失败记 warning 并返回空 map —— 出图/出表不该被治理数据拖垮。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    FeatureDefinition,
    KpiCatalog,
    OntologyClass,
    OntologyProperty,
)
from app.domain.query_plan import QueryPlan
from app.services.chart_spec_builder import aliasByField
from app.services.display_name_resolver import DISPLAY_COLUMN_DICT

logger = logging.getLogger(__name__)

SOURCE_ONTOLOGY_ALIAS = "ontology_alias"
SOURCE_BUSINESS_ALIAS = "business_alias"
SOURCE_PLAN_ALIAS = "plan_alias"
SOURCE_DICT = "dict"
SOURCE_NONE = "none"

__all__ = [
    "FieldDisplay",
    "SOURCE_BUSINESS_ALIAS",
    "SOURCE_DICT",
    "SOURCE_NONE",
    "SOURCE_ONTOLOGY_ALIAS",
    "SOURCE_PLAN_ALIAS",
    "missingFieldHints",
    "resolveFieldDisplays",
]


@dataclass(frozen=True)
class FieldDisplay:
    """单列的显示信息。`label` 不含单位；None = 无中文含义。"""

    label: str | None
    unit: str | None
    isNumeric: bool
    source: str


async def resolveFieldDisplays(
    session: AsyncSession,
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    plan: QueryPlan | None = None,
    sourceTable: str | None = None,
) -> dict[str, FieldDisplay]:
    """解析每一列的显示信息；无解析上下文时返回 `{}`。"""
    if not columns:
        return {}
    class_names = _classNamesOf(plan)
    if not class_names and not sourceTable:
        return {}
    try:
        return await _resolve(
            session,
            columns=list(columns),
            rows=rows,
            class_names=class_names,
            source_table=sourceTable,
            plan=plan,
        )
    except Exception:  # noqa: BLE001 — 治理数据缺失不应打断出图
        logger.warning("字段显示解析失败，全部按未解析处理", exc_info=True)
        return {}


def _classNamesOf(plan: QueryPlan | None) -> list[str]:
    if plan is None:
        return []
    return [name.strip().upper() for name in plan.selectedClasses if name and name.strip()]


```

```python
def _planAliasByColumn(
    plan: QueryPlan | None, columns: list[str]
) -> dict[str, str]:
    """plan 聚合别名（仅指标列）；复用 `aliasByField` 的同一口径。"""
    return aliasByField(plan, tuple(columns))


async def _loadClasses(
    session: AsyncSession, class_names: list[str], source_table: str | None
) -> list[OntologyClass]:
    statement = select(OntologyClass).where(OntologyClass.valid_to.is_(None))
    if class_names:
        statement = statement.where(func.upper(OntologyClass.class_name).in_(class_names))
    else:
        statement = statement.where(
            func.upper(OntologyClass.source_table) == (source_table or "").strip().upper()
        )
    result = await session.execute(statement)
    return list(result.scalars().all())


async def _loadProperties(
    session: AsyncSession, class_ids: list[int]
) -> list[OntologyProperty]:
    if not class_ids:
        return []
    result = await session.execute(
        select(OntologyProperty).where(OntologyProperty.class_id.in_(class_ids))
    )
    return list(result.scalars().all())


def _pick(values: Iterable[str | None]) -> str | None:
    """去空去重后恰好一个值才返回；0 个或 ≥2 个（歧义）一律 None。"""
    cleaned = {value.strip() for value in values if value and value.strip()}
    if len(cleaned) != 1:
        return None
    return next(iter(cleaned))


def _propertyLabels(properties: list[OntologyProperty]) -> dict[str, str | None]:
    """属性名（大写）→ 中文名（多行歧义 ⇒ None）。"""
    byName: dict[str, list[str | None]] = {}
    for prop in properties:
        alias = prop.property_alias
        if not alias and prop.business_aliases:
            alias = prop.business_aliases[0]
        byName.setdefault(prop.property_name.strip().upper(), []).append(alias)
    return {name: _pick(values) for name, values in byName.items()}


def _propertyUnits(properties: list[OntologyProperty]) -> dict[str, str | None]:
    byName: dict[str, list[str | None]] = {}
    for prop in properties:
        byName.setdefault(prop.property_name.strip().upper(), []).append(prop.unit)
    return {name: _pick(values) for name, values in byName.items()}


def _labelSource(properties: list[OntologyProperty], name: str, label: str) -> str:
    for prop in properties:
        if prop.property_name.strip().upper() != name:
            continue
        if prop.property_alias == label:
            return SOURCE_ONTOLOGY_ALIAS
    return SOURCE_BUSINESS_ALIAS


def _numericColumns(
    columns: list[str], rows: list[dict[str, Any]]
) -> set[str]:
    numeric: set[str] = set()
    for column in columns:
        for row in rows:
            value = row.get(column)
            if value is None:
                continue
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                numeric.add(column)
            break
    return numeric


def _normalizedNames(values: Iterable[str]) -> set[str]:
    return {value.strip().upper() for value in values if value and value.strip()}


def _addUnits(
    buckets: dict[str, list[str]], keys: tuple[str | None, ...], unit: str
) -> None:
    for key in keys:
        if key and key.strip():
            buckets.setdefault(key.strip().upper(), []).append(unit)


def _pickUnits(buckets: dict[str, list[str]]) -> dict[str, str]:
    """多行歧义（同名不同单位）⇒ 该名字不提供单位。"""
    picked: dict[str, str] = {}
    for name, values in buckets.items():
        unit = _pick(values)
        if unit:
            picked[name] = unit
    return picked


async def _loadUnitCatalog(
    session: AsyncSession, names: set[str]
) -> dict[str, str]:
    """KPI/特征单位：按归一后的名字精确匹配（不做模糊匹配）。"""
    if not names:
        return {}
    buckets: dict[str, list[str]] = {}
    kpi_rows = await session.execute(
        select(KpiCatalog.kpi_code, KpiCatalog.kpi_name, KpiCatalog.unit).where(
            KpiCatalog.unit.is_not(None),
            or_(
                func.upper(KpiCatalog.kpi_code).in_(names),
                func.upper(KpiCatalog.kpi_name).in_(names),
            ),
        )
    )
    for code, name, unit in kpi_rows.all():
        _addUnits(buckets, (code, name), unit)
    feature_rows = await session.execute(
        select(
            FeatureDefinition.feature_name,
            FeatureDefinition.feature_alias,
            FeatureDefinition.unit,
        ).where(
            FeatureDefinition.unit.is_not(None),
            or_(
                func.upper(FeatureDefinition.feature_name).in_(names),
                func.upper(FeatureDefinition.feature_alias).in_(names),
            ),
        )
    )
    for name, alias, unit in feature_rows.all():
        _addUnits(buckets, (name, alias), unit)
    return _pickUnits(buckets)


def _dictLabel(source_tables: set[str], column: str) -> str | None:
    if len(source_tables) != 1:
        return None
    table = next(iter(source_tables)).strip().upper()
    return DISPLAY_COLUMN_DICT.get((table, column.strip().upper()))
```

`_resolve`（同文件，排在上面这些辅助函数之后）：

```python
async def _resolve(
    session: AsyncSession,
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    class_names: list[str],
    source_table: str | None,
    plan: QueryPlan | None,
) -> dict[str, FieldDisplay]:
    classes = await _loadClasses(session, class_names, source_table)
    if not classes:
        return {}
    properties = await _loadProperties(session, [cls.id for cls in classes])
    labels = _propertyLabels(properties)
    units = _propertyUnits(properties)
    plan_aliases = _planAliasByColumn(plan, columns)
    source_tables = {
        cls.source_table.strip().upper()
        for cls in classes
        if cls.source_table and cls.source_table.strip()
    }
    numeric = _numericColumns(columns, rows)
    candidate_names = _normalizedNames(
        list(columns) + list(plan_aliases.values())
    )
    catalog_units = await _loadUnitCatalog(session, candidate_names)

    displays: dict[str, FieldDisplay] = {}
    for column in columns:
        name = column.strip().upper()
        ontology_label = labels.get(name)
        plan_label = plan_aliases.get(column)
        if ontology_label:
            label, source = ontology_label, _labelSource(properties, name, ontology_label)
        elif plan_label:
            label, source = plan_label, SOURCE_PLAN_ALIAS
        else:
            dict_label = _dictLabel(source_tables, column)
            label, source = dict_label, (SOURCE_DICT if dict_label else SOURCE_NONE)

        unit = units.get(name)
        if not unit:
            unit = catalog_units.get(name) or catalog_units.get(
                (plan_label or "").strip().upper()
            )
        displays[column] = FieldDisplay(
            label=label,
            unit=unit or None,
            isNumeric=column in numeric,
            source=source,
        )
    return displays
```

`missingFieldHints`：

```python
def missingFieldHints(
    columnLabels: Mapping[str, str], displays: Mapping[str, FieldDisplay]
) -> dict[str, list[str]]:
    """未解析出中文名的列 + 无单位的数值列（按输入顺序，便于稳定断言）。"""
    missing_labels = [
        column
        for column, display in displays.items()
        if display.label is None and column not in columnLabels
    ]
    missing_units = [
        column
        for column, display in displays.items()
        if display.isNumeric and display.unit is None
    ]
    return {"missingLabels": missing_labels, "missingUnits": missing_units}
```

> 实现自检：`_pick` 的集合迭代在 `len == 1` 时是确定的；不要改成 `list(set)[0]` 后再对多值取第一个（那是猜）。

- [ ] **Step 5: 运行解析层测试**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_field_display_resolver.py -q -p no:randomly
```

Expected: PASS。

- [ ] **Step 6: 回归 `chart_spec_builder`（改名影响面）**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_chart_spec_builder.py -q -p no:randomly
```

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
cd /private/tmp/qa-chat-output-zh-labels
git add backend/app/services/field_display_resolver.py \
        backend/app/services/chart_spec_builder.py \
        backend/app/tests/unit/test_field_display_resolver.py
git commit -m "feat: 新增字段中文名/单位解析层"
```

---

### Task 3: 提示文案模块 `field_hint_text`

**Files:**
- Create: `backend/app/services/field_hint_text.py`
- Modify: `backend/app/services/messages_zh.py`（末尾追加常量）
- Create: `backend/app/tests/unit/test_field_hint_text.py`

**Interfaces:**
- Consumes: 无（纯函数）。
- Produces:
  - `mergeFieldHints(*hints) -> {"missingLabels": [...], "missingUnits": [...]}`
  - `composeFieldHintText(hints, *, columnLabels=None) -> str`（无内容时返回 `""`；有内容时**以 `"\n\n"` 开头**）
  - `appendFieldHints(answer, hints, *, columnLabels=None) -> str`
  - `fieldHintsOf(payload) -> dict | None`
  - `columnLabelsOf(*payloads) -> dict[str, str]`
  - `hintsFromSteps(steps) -> tuple[dict, dict]`

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/unit/test_field_hint_text.py`：

```python
"""治理提示文案拼装（纯函数）。"""

from __future__ import annotations

from app.services import messages_zh
from app.services.field_hint_text import (
    appendFieldHints,
    columnLabelsOf,
    composeFieldHintText,
    fieldHintsOf,
    hintsFromSteps,
    mergeFieldHints,
)


def test_empty_hints_produce_no_text():
    assert composeFieldHintText({}) == ""
    assert composeFieldHintText({"missingLabels": [], "missingUnits": []}) == ""


def test_both_kinds():
    text = composeFieldHintText(
        {"missingLabels": ["a"], "missingUnits": ["b"]},
        columnLabels={"b": "收货数量"},
    )
    assert text.startswith("\n\n")
    assert messages_zh.MSG_FIELD_HINT_TITLE in text
    assert "a" in text
    assert "收货数量" in text


def test_truncates_after_eight():
    fields = [f"f{i}" for i in range(10)]
    text = composeFieldHintText({"missingLabels": fields, "missingUnits": []})
    assert "f7" in text
    assert "f8" not in text
    assert "等 10 个" in text


def test_merge_deduplicates_preserving_order():
    merged = mergeFieldHints(
        {"missingLabels": ["a", "b"], "missingUnits": ["x"]},
        {"missingLabels": ["b", "c"], "missingUnits": ["x", "y"]},
    )
    assert merged == {"missingLabels": ["a", "b", "c"], "missingUnits": ["x", "y"]}


def test_append_keeps_answer_when_no_hints():
    assert appendFieldHints("正文", {}) == "正文"


def test_field_hints_of_ignores_malformed():
    assert fieldHintsOf({"fieldHints": {"missingLabels": ["a"]}}) == {
        "missingLabels": ["a"],
        "missingUnits": [],
    }
    assert fieldHintsOf({"fieldHints": "nope"}) is None
    assert fieldHintsOf(None) is None


def test_column_labels_merges_table_and_chart():
    labels = columnLabelsOf(
        {"columnLabels": {"a": "甲"}},
        {"columnLabels": {"b": "乙", "a": "甲2"}},
    )
    assert labels == {"a": "甲2", "b": "乙"}


def test_hints_from_steps_reads_chart_and_table():
    class Step:
        def __init__(self, chart_option, table_option):
            self.chart_option = chart_option
            self.table_option = table_option

    hints, labels = hintsFromSteps(
        [
            Step(
                {"fieldHints": {"missingLabels": ["a"], "missingUnits": []},
                 "columnLabels": {"b": "乙"}},
                {"columnLabels": {"c": "丙"}},
            ),
            Step(
                {"fieldHints": {"missingLabels": [], "missingUnits": ["d"]},
                 "columnLabels": {}},
                None,
            ),
        ]
    )
    assert hints == {"missingLabels": ["a"], "missingUnits": ["d"]}
    assert labels == {"b": "乙", "c": "丙"}
```

- [ ] **Step 2: 运行确认失败**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_field_hint_text.py -q -p no:randomly
```

Expected: FAIL — `ModuleNotFoundError`。

- [ ] **Step 3: 文案常量**

`backend/app/services/messages_zh.py` 末尾追加：

```python
# --- 字段治理提示（2026-11-01，chat 输出中文化） -------------------------------
MSG_FIELD_HINT_TITLE = "字段治理提示"
MSG_FIELD_HINT_MISSING_LABEL = "以下字段缺少中文含义，请在「本体 → 属性管理」补充别名：{fields}"
MSG_FIELD_HINT_MISSING_UNIT = "以下数值字段未定义单位，请在本体属性中补充「单位」：{fields}"
MSG_FIELD_HINT_TRUNCATED = " 等 {count} 个"
```

- [ ] **Step 4: 实现**

新建 `backend/app/services/field_hint_text.py`：

```python
"""字段治理提示：`fieldHints` → 回答末尾文案（纯函数，零 IO）。

**为什么放正文而不是结构化提示条**（已确认的决策）：追加进最终回答并随消息落库，
PDF 导出 / 历史回放 / 研究报告天然继承，不需要每个消费端各写一套 UI。

**两类提示**：缺中文名、数值列缺单位。字段最多列 8 个，其余显示「等 N 个」。
**无内容时返回空串**（调用方直接拼接即可），保证存量回答逐字不变。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.services.messages_zh import (
    MSG_FIELD_HINT_MISSING_LABEL,
    MSG_FIELD_HINT_MISSING_UNIT,
    MSG_FIELD_HINT_TITLE,
    MSG_FIELD_HINT_TRUNCATED,
)

MAX_HINT_FIELDS = 8
_HINT_KEYS = ("missingLabels", "missingUnits")

__all__ = [
    "MAX_HINT_FIELDS",
    "appendFieldHints",
    "columnLabelsOf",
    "composeFieldHintText",
    "fieldHintsOf",
    "hintsFromSteps",
    "mergeFieldHints",
]


def _fieldsOf(hints: Any, key: str) -> list[str]:
    if not isinstance(hints, dict):
        return []
    values = hints.get(key)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value]


def mergeFieldHints(*hints: Any) -> dict[str, list[str]]:
    """合并多份 hints，去重且保持首次出现顺序。"""
    merged: dict[str, list[str]] = {key: [] for key in _HINT_KEYS}
    for hint in hints:
        for key in _HINT_KEYS:
            for field in _fieldsOf(hint, key):
                if field not in merged[key]:
                    merged[key].append(field)
    return merged


def _joinNames(fields: list[str], columnLabels: Mapping[str, str] | None) -> str:
    labels = columnLabels or {}
    shown = [labels.get(field, field) for field in fields[:MAX_HINT_FIELDS]]
    text = "、".join(shown)
    if len(fields) > MAX_HINT_FIELDS:
        return text + MSG_FIELD_HINT_TRUNCATED.format(count=len(fields))
    return text


def composeFieldHintText(
    hints: Any, *, columnLabels: Mapping[str, str] | None = None
) -> str:
    """hints → 以 `\\n\\n` 开头的提示段；无内容返回空串。"""
    labels = _fieldsOf(hints, "missingLabels")
    units = _fieldsOf(hints, "missingUnits")
    if not labels and not units:
        return ""
    lines = [MSG_FIELD_HINT_TITLE]
    if labels:
        lines.append(
            MSG_FIELD_HINT_MISSING_LABEL.format(
                fields=_joinNames(labels, columnLabels)
            )
        )
    if units:
        lines.append(
            MSG_FIELD_HINT_MISSING_UNIT.format(
                fields=_joinNames(units, columnLabels)
            )
        )
    return "\n\n" + "\n".join(lines)


def appendFieldHints(
    answer: str, hints: Any, *, columnLabels: Mapping[str, str] | None = None
) -> str:
    """在回答末尾追加提示；无内容时原样返回。"""
    text = composeFieldHintText(hints, columnLabels=columnLabels)
    return f"{answer}{text}" if text else answer


def fieldHintsOf(payload: Any) -> dict[str, Any] | None:
    """从 chart_option 取 fieldHints；缺键/形状不对 → None（旧消息不提示）。"""
    if not isinstance(payload, dict):
        return None
    hints = payload.get("fieldHints")
    if not isinstance(hints, dict):
        return None
    return {
        "missingLabels": _fieldsOf(hints, "missingLabels"),
        "missingUnits": _fieldsOf(hints, "missingUnits"),
    }


def columnLabelsOf(*payloads: Any) -> dict[str, str]:
    """合并多份负载里的 columnLabels（后写覆盖，与渲染器的取值口径一致）。"""
    labels: dict[str, str] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        raw = payload.get("columnLabels")
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str) and value:
                labels[key] = value
    return labels


def hintsFromSteps(steps: Iterable[Any]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """多步：汇总每步 chart_option/table_option 的 hints 与 labels。"""
    hints: list[Any] = []
    labels: dict[str, str] = {}
    for step in steps:
        chart_option = getattr(step, "chart_option", None)
        table_option = getattr(step, "table_option", None)
        hints.append(fieldHintsOf(chart_option))
        labels.update(columnLabelsOf(chart_option, table_option))
    return mergeFieldHints(*hints), labels
```

- [ ] **Step 5: 运行**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_field_hint_text.py -q -p no:randomly
```

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
cd /private/tmp/qa-chat-output-zh-labels
git add backend/app/services/field_hint_text.py backend/app/services/messages_zh.py \
        backend/app/tests/unit/test_field_hint_text.py
git commit -m "feat: 字段治理提示文案模块"
```

---

### Task 4: `columnLabels` / `units` 进入 spec 渲染链

**Files:**
- Modify: `backend/app/domain/chart_spec.py:105-131`（`ChartSpec`、`tableSpec`）、`coerceSpec`
- Modify: `backend/app/services/chart_spec_builder.py`（`buildSpec`、`_buildMeasures`）
- Modify: `backend/app/services/chart_renderer.py`（`_buildTable`）
- Modify: `backend/app/services/visual_payload.py`（`assembleTableOption`）
- Test: `backend/app/tests/unit/test_chart_output_zh_labels.py`（新建）

**Interfaces:**
- Consumes: `aliasByField`（Task 2 公开）、`units` 映射（`{列名: 单位}`）。
- Produces:
  - `ChartSpec.columnLabels: Mapping[str, str]`（原始列 → `中文名（单位）`）
  - `tableSpec(columns, *, columnLabels=None)`
  - `buildSpec(..., units: Mapping[str, str] | None = None)`
  - `assembleTableOption(..., columnLabels: Mapping[str, str] | None = None)`
  - TABLE 负载与明细表负载里出现可选键 `columnLabels`

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/unit/test_chart_output_zh_labels.py`：

```python
"""columnLabels / units 在 spec→renderer→tableOption 链路上的透传。"""

from __future__ import annotations

from app.domain.chart_spec import ChartSpec, coerceSpec, tableSpec
from app.domain.enums import ChartType
from app.services.chart_decision import ChartDecision, buildChartSignals
from app.services.chart_renderer import renderChartOption
from app.services.chart_spec_builder import buildSpec
from app.services.visual_payload import assembleTableOption

_ROWS = [
    {"supplier_code": "B1", "rcv_qty": 3},
    {"supplier_code": "B2", "rcv_qty": 5},
    {"supplier_code": "B3", "rcv_qty": 7},
]


def _signals(columns: list[str], rows: list[dict]):
    """复用生产同一派生入口，避免手搓 ChartSignals（字段多、易漂移）。"""
    return buildChartSignals(None, columns, rows, "")


def _decision(kind: ChartType) -> ChartDecision:
    return ChartDecision(kind=kind, ruleId="R_TEST", candidates=(), ambiguous=False)


def test_build_spec_appends_unit_to_measure_and_series():
    spec = buildSpec(
        _decision(ChartType.BAR),
        _signals(["supplier_code", "rcv_qty"], _ROWS),
        _ROWS,
        aliasOverrides={"supplier_code": "供应商编码", "rcv_qty": "收货数量"},
        units={"rcv_qty": "件"},
    )
    assert spec.measures[0].label == "收货数量（件）"
    assert spec.series[0].name == "收货数量（件）"
    assert spec.dimensions[0].label == "供应商编码"
    assert spec.columnLabels == {
        "supplier_code": "供应商编码",
        "rcv_qty": "收货数量（件）",
    }


def test_build_spec_omits_labels_when_unresolved():
    rows = [{"supplier_name": "甲", "total_qty": 3}]
    spec = buildSpec(
        _decision(ChartType.BAR),
        _signals(["supplier_name", "total_qty"], rows),
        rows,
    )
    assert spec.columnLabels == {}
    assert spec.measures[0].label == "total qty"


def test_table_spec_carries_labels():
    spec = tableSpec(["a", "b"], columnLabels={"a": "甲"})
    assert spec.columnLabels == {"a": "甲"}


def test_coerce_degrade_preserves_labels():
    spec = ChartSpec(
        kind=ChartType.BAR,
        columns=("a",),
        columnLabels={"a": "甲"},
        measures=(),
    )
    coerced, reason = coerceSpec(spec, ["a"])
    assert reason is not None
    assert coerced.kind is ChartType.TABLE
    assert coerced.columnLabels == {"a": "甲"}


def test_render_table_includes_column_labels():
    spec = tableSpec(["a"], columnLabels={"a": "甲（件）"})
    option = renderChartOption(spec, [{"a": 1}])
    assert option["columnLabels"] == {"a": "甲（件）"}


def test_render_table_omits_empty_labels():
    option = renderChartOption(tableSpec(["a"]), [{"a": 1}])
    assert "columnLabels" not in option


def test_assemble_table_option_passes_labels():
    payload = assembleTableOption(
        specKind=ChartType.BAR,
        columns=["a"],
        data=[{"a": 1}],
        fullDataThreshold=100,
        columnLabels={"a": "甲"},
    )
    assert payload is not None
    assert payload["columnLabels"] == {"a": "甲"}
```

**如果 `ChartSignals` 的构造参数与上面不一致**（以 `backend/app/services/chart_decision.py` 的实际定义为准），按实际字段名调整 `_signals`，但断言保持不变。

- [ ] **Step 2: 运行确认失败**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_chart_output_zh_labels.py -q -p no:randomly
```

Expected: FAIL — `ChartSpec` 不接受 `columnLabels` / `buildSpec` 不接受 `units`。

- [ ] **Step 3: `chart_spec.py`**

`ChartSpec` 在 `axisLabels` 前加：

```python
    # 原始列名 → 表头/图例显示名（可含单位）。缺键 = 回落原始列名。
    # 降级成 TABLE 时必须保留：降级是「画不出来」，不是「丢掉中文名」。
    columnLabels: Mapping[str, str] = field(default_factory=dict)
```

`tableSpec` 改为：

```python
def tableSpec(
    columns: list[str] | tuple[str, ...],
    *,
    columnLabels: Mapping[str, str] | None = None,
) -> ChartSpec:
    """构造 TABLE spec —— 也是所有降级路径的终点。"""
    return ChartSpec(
        kind=ChartType.TABLE,
        columns=tuple(columns),
        columnLabels=dict(columnLabels or {}),
    )
```

`coerceSpec` 的降级行改为：

```python
    return tableSpec(columns, columnLabels=spec.columnLabels), reason
```

- [ ] **Step 4: `chart_spec_builder.py`**

`_buildMeasures` 改为接受 units：

```python
def _withUnit(label: str, unit: str | None) -> str:
    return f"{label}（{unit}）" if unit else label


def _buildMeasures(
    fields: tuple[str, ...],
    aliases: dict[str, str],
    units: Mapping[str, str],
) -> tuple[SpecMeasure, ...]:
    return tuple(
        SpecMeasure(field=f, label=_withUnit(aliases.get(f) or _prettify(f), units.get(f)))
        for f in fields
    )


def _buildColumnLabels(
    columns: tuple[str, ...],
    aliases: dict[str, str],
    units: Mapping[str, str],
) -> dict[str, str]:
    """只收录「有中文名或有单位」的列；都没有的列缺键 ⇒ 前端回落原始列名。"""
    labels: dict[str, str] = {}
    for column in columns:
        unit = units.get(column)
        alias = aliases.get(column)
        if not alias and not unit:
            continue
        labels[column] = _withUnit(alias or _prettify(column), unit)
    return labels
```

`buildSpec` 签名加 `units: Mapping[str, str] | None = None`，函数体：

```python
    unitsMap = dict(units or {})
    ...
    measures = _buildMeasures(_measureFields(signals, kind), aliases, unitsMap)
    ...
        columnLabels=_buildColumnLabels(signals.columns, aliases, unitsMap),
```

`ChartSpec(...)` 构造参数里加 `columnLabels=...`。

- [ ] **Step 5: `chart_renderer.py` `_buildTable`**

```python
def _buildTable(spec: ChartSpec, data: list[dict]) -> dict:
    payload: dict = {"columns": list(spec.columns), "rows": list(data)}
    if spec.columnLabels:
        payload["columnLabels"] = dict(spec.columnLabels)
    return payload
```

（以文件中实际的 `_buildTable` 为准，只加 `columnLabels` 分支；不要改 rows 截断行为。）

- [ ] **Step 6: `visual_payload.py`**

`assembleTableOption` 签名加 `columnLabels: Mapping[str, str] | None = None`，返回：

```python
    payload = {
        "columns": list(columns),
        "rows": rows,
        "truncated": len(data) > fullDataThreshold,
    }
    if columnLabels:
        payload["columnLabels"] = dict(columnLabels)
    return payload
```

顶部 `from collections.abc import Mapping`。

- [ ] **Step 7: 运行**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_chart_output_zh_labels.py app/tests/unit/test_chart_spec_builder.py \
  app/tests/unit/test_visual_payload.py app/tests/unit/test_chart_renderer.py \
  -q -p no:randomly
```

Expected: PASS（含既有用例）。

- [ ] **Step 8: Commit**

```bash
cd /private/tmp/qa-chat-output-zh-labels
git add backend/app/domain/chart_spec.py backend/app/services/chart_spec_builder.py \
        backend/app/services/chart_renderer.py backend/app/services/visual_payload.py \
        backend/app/tests/unit/test_chart_output_zh_labels.py
git commit -m "feat: ChartSpec 增加 columnLabels 与单位并入标签"
```

---

### Task 5: 接入 chat 与研究两条构建链

**Files:**
- Modify: `backend/app/services/chart_service.py:80-165`
- Modify: `backend/app/services/chart_integration.py`
- Modify: `backend/app/services/report_planner.py:420-426`
- Test: `backend/app/tests/integration/test_chat_zh_labels.py`（新建）

**Interfaces:**
- Consumes: `resolveFieldDisplays` / `missingFieldHints`（Task 2）、`columnLabels` 链路（Task 4）。
- Produces:
  - chat 的 `table_option` 带 `columnLabels`；`chart_option` 带 `fieldHints`（TABLE 图的 `chart_option` 另有 `columnLabels`，其余图型没有——列头断言一律读 `table_option`）。
  - `buildReportChart(..., sourceTable=None)` 的返回 dict 带 `columnLabels` + `fieldHints`（有值时）。

- [ ] **Step 1: 写失败集成测试**

新建 `backend/app/tests/integration/test_chat_zh_labels.py`：

```python
"""chat 输出中文化端到端（真实 PG + 完整 API 链路）。"""

from __future__ import annotations

from sqlalchemy import update

from app.domain.models import OntologyProperty
from app.tests.integration.test_chat_api import (
    _chat_payload,
    _installFakes,
    _seed,
)


async def _governQty(session, *, alias: str | None, unit: str | None) -> None:
    await session.execute(
        update(OntologyProperty)
        .where(OntologyProperty.property_name == "QTY")
        .values(property_alias=alias, unit=unit)
    )
    await session.commit()


class TestChatFieldLabels:
    async def test_answer_hints_and_column_labels(
        self, client, dbSession, monkeypatch
    ) -> None:
        config, ds = await _seed(dbSession)
        await _governQty(dbSession, alias="收货数量", unit="件")
        _installFakes(monkeypatch, config)

        resp = await client.post(
            "/api/v1/chat", json=_chat_payload("各供应商的收货数量汇总", ds.id)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # 图例：series 名带中文 + 单位
        assert body["chartOption"]["series"][0]["name"] == "收货数量（件）"
        # 表头映射：只收录治理过的列，未治理列缺键 ⇒ 前端回落原始列名。
        # 本用例是 bar 图：`columnLabels` 挂在明细表负载 `tableOption` 上，
        # 柱状图的 `chartOption` 只有 series/坐标轴，没有 `columnLabels`。
        labels = body["tableOption"]["columnLabels"]
        assert labels["QTY"] == "收货数量（件）"
        assert "NAME" not in labels
        # 治理提示：NAME 缺中文含义；QTY 有单位 ⇒ 不缺单位
        hints = body["chartOption"]["fieldHints"]
        assert hints["missingLabels"] == ["NAME"]
        assert hints["missingUnits"] == []
        assert "字段治理提示" in body["answer"]
        assert "NAME" in body["answer"]

    async def test_no_hint_when_every_field_governed(
        self, client, dbSession, monkeypatch
    ) -> None:
        config, ds = await _seed(dbSession)
        await dbSession.execute(
            update(OntologyProperty).values(property_alias="已治理")
        )
        await _governQty(dbSession, alias="已治理", unit="件")
        _installFakes(monkeypatch, config)

        resp = await client.post(
            "/api/v1/chat", json=_chat_payload("各供应商的收货数量汇总", ds.id)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "字段治理提示" not in body["answer"]
        assert body["chartOption"]["fieldHints"] == {
            "missingLabels": [],
            "missingUnits": [],
        }
```


- [ ] **Step 2: 运行确认失败**

```bash
cd /private/tmp/qa-chat-output-zh-labels/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test_zhlabels'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/integration/test_chat_zh_labels.py -q -p no:randomly
```

Expected: FAIL — `KeyError: 'columnLabels'` / `字段治理提示` 不在回答里。

- [ ] **Step 3: `chart_service.buildChart`**

在 `spec = buildSpec(...)` 之前插入解析：

```python
        displays = await resolveFieldDisplays(
            session, columns=columns, rows=data, plan=plan,
        )
        aliasOverrides = {
            column: display.label
            for column, display in displays.items()
            if display.label
        }
        units = {
            column: display.unit
            for column, display in displays.items()
            if display.unit
        }
```

`buildSpec` 调用改为：

```python
        spec = buildSpec(
            decision, signals, data, plan,
            aliasOverrides=aliasOverrides or None,
            units=units or None,
        )
```

`assembleTableOption(...)` 调用加：

```python
            columnLabels=spec.columnLabels,
```

`option` 之后（`rationale = ...` 之前）加：

```python
        if displays:
            option = {
                **option,
                "fieldHints": missingFieldHints(spec.columnLabels, displays),
            }
```

顶部 import：

```python
from app.services.field_display_resolver import (
    missingFieldHints,
    resolveFieldDisplays,
)
```

**行为校验**：`aliasOverrides` 现在会包含本体中文名，因此**维度**标签也可能变中文（这是本特性的目标）。兼容性不变量是「**既无单位、也无可解析中文名** ⇒ `aliasOverrides` 为空 ⇒ 输出与改动前逐字一致」：LLM 聚合别名（`plan_alias`）与 `DISPLAY_COLUMN_DICT` 在改动前就参与 `_prettify`/标签，它们不引入新差异；只有新增的本体单位与本体中文名才会改变输出。

- [ ] **Step 4: `chart_integration.buildReportChart`**

签名加 `sourceTable: str | None = None`。函数体：`signals` 之后、`buildSpec` 之前：

```python
        displays = await resolveFieldDisplays(
            session, columns=list(columns), rows=rows, plan=plan,
            sourceTable=sourceTable,
        )
        units = {
            column: display.unit
            for column, display in displays.items()
            if display.unit
        }
        mergedOverrides = {
            **{k: v for k, v in (aliasOverrides or {}).items() if v},
            **{
                column: display.label
                for column, display in displays.items()
                if display.label
            },
        }
```

`buildSpec` 调用改为：

```python
        spec: ChartSpec = buildSpec(
            decision, signals, rows, plan,
            aliasOverrides=mergedOverrides or None,
            axisLabels=mergedOverrides or None,
            units=units or None,
        )
```

返回 dict 加：

```python
            "columnLabels": dict(spec.columnLabels),
            "fieldHints": (
                missingFieldHints(spec.columnLabels, displays)
                if displays
                else None
            ),
```

（字段值为 None 时前端按可选处理；`columnLabels` 为空 dict 也无害。）

- [ ] **Step 5: `report_planner._chartContent`**

`buildReportChart(...)` 调用加：

```python
        sourceTable=view.sourceTable,
```

- [ ] **Step 6: 运行集成测试**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/integration/test_chat_zh_labels.py -q -p no:randomly
```

Expected: PASS。

- [ ] **Step 7: 回归既有 chat 集成套件**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/integration/test_chat_api.py app/tests/integration/test_chat_multi_step.py \
  app/tests/unit/test_chart_service.py app/tests/unit/test_report_planner.py \
  -q -p no:randomly
```

Expected: PASS（净新增失败 0）。

- [ ] **Step 8: Commit**

```bash
cd /private/tmp/qa-chat-output-zh-labels
git add backend/app/services/chart_service.py backend/app/services/chart_integration.py \
        backend/app/services/report_planner.py \
        backend/app/tests/integration/test_chat_zh_labels.py
git commit -m "feat: chat/研究构建链注入字段中文名与单位"
```

---

### Task 6: 四条回答装配路径追加提示

**Files:**
- Modify: `backend/app/services/chat_service.py:700-706`
- Modify: `backend/app/services/chat_stream_query.py:455-464`
- Modify: `backend/app/services/chat_multistep.py:698-712`
- Modify: `backend/app/services/chat_stream_multistep.py:238-243`
- Test: `backend/app/tests/unit/test_field_hint_wiring.py`（新建，纯函数级断言 + 源码锚点守卫）

**Interfaces:**
- Consumes: `appendFieldHints` / `composeFieldHintText` / `fieldHintsOf` / `columnLabelsOf` / `hintsFromSteps`（Task 3）。
- Produces: 最终回答正文末尾带提示；流式路径多发一个 `token` 帧，落库文本与前端所见一致。

- [ ] **Step 1: 写失败测试**

新建 `backend/app/tests/unit/test_field_hint_wiring.py`：

```python
"""回答装配点的接入守卫（源码级锚点）。

这些路径各自需要 LLM / DB / 流式管线才能跑通，模块级单测成本远高于收益；
真正的内容断言由 Task 5 的集成用例覆盖。这里用源码锚点锁住「四个装配点都调了
同一个入口」，防止后续重构时漏接一路。
"""

from __future__ import annotations

from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[2] / "services"


def _source(name: str) -> str:
    return (_SERVICE_DIR / name).read_text(encoding="utf-8")


def test_single_step_non_stream_appends_hints():
    source = _source("chat_service.py")
    assert "appendFieldHints(" in source
    assert "fieldHintsOf(option)" in source


def test_single_step_stream_emits_hint_token():
    source = _source("chat_stream_query.py")
    assert "composeFieldHintText(" in source
    assert 'EVENT_TOKEN, {"content": hintText}' in source
    # 提示必须在 join 之前进入 answerPieces，否则落库文本与前端所见不一致
    assert source.index("answerPieces.append(hintText)") < source.index('answer = "".join(answerPieces)')


def test_multistep_non_stream_merges_step_hints():
    source = _source("chat_multistep.py")
    assert "hintsFromSteps(completed)" in source


def test_multistep_stream_merges_step_hints():
    source = _source("chat_stream_multistep.py")
    assert "hintsFromSteps(completed)" in source
```

- [ ] **Step 2: 运行确认失败**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_field_hint_wiring.py -q -p no:randomly
```

Expected: FAIL（四个断言全失败）。

- [ ] **Step 3: 单步非流式**

`chat_service.py`，在 `if denominatorWarning:` 之后加：

```python
        answerText = appendFieldHints(
            answerText,
            fieldHintsOf(option),
            columnLabels=columnLabelsOf(option, tableOption),
        )
```

import：`from app.services.field_hint_text import appendFieldHints, columnLabelsOf, fieldHintsOf`。

- [ ] **Step 4: 单步流式**

`chat_stream_query.py`，在 `thinkFilter.flush()` 块之后、`answer = "".join(answerPieces)` 之前加：

```python
        hintText = composeFieldHintText(
            fieldHintsOf(option),
            columnLabels=columnLabelsOf(option, tableOption),
        )
        if hintText:
            answerPieces.append(hintText)
            yield StreamEvent(EVENT_TOKEN, {"content": hintText})
```

import：`from app.services.field_hint_text import columnLabelsOf, composeFieldHintText, fieldHintsOf`。

- [ ] **Step 5: 多步非流式**

`chat_multistep.py`，在 `agg_content = await applyThinkPolicy(...)` 之后加：

```python
                stepHints, stepLabels = hintsFromSteps(completed)
                agg_content = appendFieldHints(
                    agg_content, stepHints, columnLabels=stepLabels,
                )
```

import 同 Task 3。

- [ ] **Step 6: 多步流式**

`chat_stream_multistep.py`，同一位置加：

```python
                stepHints, stepLabels = hintsFromSteps(completed)
                agg_content = appendFieldHints(
                    agg_content, stepHints, columnLabels=stepLabels,
                )
```

因为该路径随后先 `_storeSessionMessages(... agg_content ...)` 再 `yield EVENT_TOKEN {"content": agg_content}`，落库与前端天然一致，无需额外发帧。

- [ ] **Step 7: 运行 + 回归**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_field_hint_wiring.py app/tests/unit/test_field_hint_text.py \
  app/tests/integration/test_chat_zh_labels.py app/tests/integration/test_chat_multi_step.py \
  app/tests/integration/test_chat_stream.py \
  -q -p no:randomly
```

Expected: PASS。若流式既有断言依赖回答结尾精确匹配，改为断言正文前缀 + 允许提示后缀（**不要**删掉提示）。

- [ ] **Step 8: Commit**

```bash
cd /private/tmp/qa-chat-output-zh-labels
git add backend/app/services/chat_service.py backend/app/services/chat_stream_query.py \
        backend/app/services/chat_multistep.py backend/app/services/chat_stream_multistep.py \
        backend/app/tests/unit/test_field_hint_wiring.py
git commit -m "feat: 四条回答装配路径追加字段治理提示"
```

---

### Task 7: 研究报告 MD 表头与末尾提示

**Files:**
- Modify: `backend/app/services/report_renderer.py:126-175`
- Test: `backend/app/tests/unit/test_report_renderer.py`（追加）

**Interfaces:**
- Consumes: `mdTable(..., columnLabels=...)`；`mergeFieldHints` / `composeFieldHintText`。
- Produces: MD 表头显示中文名；报告末尾追加与 chat 相同的提示段。

- [ ] **Step 1: 写失败测试**

在 `backend/app/tests/unit/test_report_renderer.py` 追加：

```python
def test_render_markdown_uses_column_labels_in_table_header():
    from app.services.report_renderer import renderMarkdown

    payload = {
        "title": "t",
        "mode": "research",
        "question": "q",
        "sections": [
            {
                "title": "数据",
                "blocks": [
                    {
                        "type": "table",
                        "content": {
                            "columns": ["rcv_qty"],
                            "rows": [{"rcv_qty": 1}],
                            "columnLabels": {"rcv_qty": "收货数量（件）"},
                        },
                    }
                ],
            }
        ],
    }
    markdown = renderMarkdown(payload)
    assert "| 收货数量（件） |" in markdown


def test_render_markdown_appends_field_hints():
    from app.services.report_renderer import renderMarkdown

    payload = {
        "title": "t",
        "mode": "research",
        "question": "q",
        "sections": [
            {
                "title": "数据",
                "blocks": [
                    {
                        "type": "chart",
                        "content": {
                            "columns": ["a"],
                            "data": [{"a": 1}],
                            "chartType": "bar",
                            "fieldHints": {
                                "missingLabels": ["a"],
                                "missingUnits": [],
                            },
                        },
                    }
                ],
            }
        ],
    }
    markdown = renderMarkdown(payload)
    assert "字段治理提示" in markdown
    # 提示必须收尾（不是夹在正文中间）
    lastLine = [line for line in markdown.splitlines() if line.strip()][-1]
    assert lastLine.startswith("以下字段缺少中文含义")
```

- [ ] **Step 2: 运行确认失败**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_report_renderer.py -q -p no:randomly
```

Expected: FAIL（表头是 `rcv_qty`，且无提示）。

- [ ] **Step 3: 实现**

`mdTable` 改为：

```python
def mdTable(
    columns: list[str],
    rows: list[dict[str, Any]],
    columnLabels: dict[str, str] | None = None,
) -> list[str]:
    labels = columnLabels or {}
    lines = [
        "| " + " | ".join(escapeCell(labels.get(column, column)) for column in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines += [
        "| " + " | ".join(escapeCell(row.get(column)) for column in columns) + " |" for row in rows
    ]
    return lines
```

`renderBlock` 的两处 `mdTable(...)` 调用传 `content.get("columnLabels")`：

```python
        return mdTable(content["columns"], content["rows"], content.get("columnLabels")) + [""]
```

```python
        table = mdTable(columns, data, content.get("columnLabels"))
```

`renderMarkdown` 末尾改为：

```python
    hints, labels = _hintsAndLabels(payload.get("sections") or [])
    hintText = composeFieldHintText(hints, columnLabels=labels)
    if hintText:
        lines += ["", hintText.strip()]
    return "\n".join(lines).strip() + "\n"
```

新增：

```python
def _hintsAndLabels(
    sections: list[dict[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    hints: list[Any] = []
    labels: dict[str, str] = {}
    for section in sections:
        for block in section.get("blocks") or []:
            if block.get("type") != BLOCK_CHART:
                continue
            content = block.get("content") or {}
            hints.append(content.get("fieldHints"))
            for key, value in (content.get("columnLabels") or {}).items():
                if isinstance(key, str) and isinstance(value, str):
                    labels[key] = value
    return mergeFieldHints(*hints), labels
```

import：`from app.services.field_hint_text import composeFieldHintText, mergeFieldHints`。

- [ ] **Step 4: 运行**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_report_renderer.py -q -p no:randomly
```

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd /private/tmp/qa-chat-output-zh-labels
git add backend/app/services/report_renderer.py backend/app/tests/unit/test_report_renderer.py
git commit -m "feat: 研究报告表头中文名与治理提示"
```

---

### Task 8: PDF 导出表头

**Files:**
- Modify: `backend/app/services/pdf_export_service.py:611-637`
- Test: `backend/app/tests/unit/test_pdf_export_charts.py`（追加）

**Interfaces:**
- Consumes: `chartOption["columnLabels"]`。
- Produces: PDF 原生表格表头显示中文名（无 labels 时逐字不变）。

- [ ] **Step 1: 写失败测试**

在 `backend/app/tests/unit/test_pdf_export_charts.py` 的 `TestChartFlowables` 类内追加。与同类既有用例一致：用模块级辅助 `_turn(chart_type=..., chart_option=...)` 构造轮次，经 `ChatExportPdfBuilder()._chart_flowables(turn)` 走完整分派（builder 无构造参数，现有用例都是 `ChatExportPdfBuilder()` 直接构造），再从 `flowables[0]._cellvalues` 取 `cell.text` 断言：

```python
    def test_table_header_uses_column_labels(self) -> None:
        """有 columnLabels 时表头显示中文名（含单位），数据仍按原始列键取值。"""
        turn = _turn(
            chart_type="table",
            chart_option={
                "columns": ["rcv_qty"],
                "rows": [{"rcv_qty": 1}],
                "columnLabels": {"rcv_qty": "收货数量（件）"},
            },
        )

        flowables = ChatExportPdfBuilder()._chart_flowables(turn)

        assert isinstance(flowables[0], Table)
        body = [[cell.text for cell in row] for row in flowables[0]._cellvalues]
        assert body == [["收货数量（件）"], ["1"]]

    def test_table_header_falls_back_to_column_key(self) -> None:
        """存量消息没有 columnLabels：表头必须逐字不变（回归守卫）。"""
        turn = _turn(
            chart_type="table",
            chart_option={"columns": ["rcv_qty"], "rows": [{"rcv_qty": 1}]},
        )

        flowables = ChatExportPdfBuilder()._chart_flowables(turn)

        body = [[cell.text for cell in row] for row in flowables[0]._cellvalues]
        assert body == [["rcv_qty"], ["1"]]
```

- [ ] **Step 2: 运行确认失败**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_pdf_export_charts.py -q -p no:randomly
```

Expected: FAIL（表头是 `rcv_qty`）。

- [ ] **Step 3: 实现**

`_table_from_chart_option` 里：

```python
        labels = chartOption.get("columnLabels")
        labelMap = labels if isinstance(labels, dict) else {}
        header = [labelMap.get(col, col) for col in columns]
        body = [[self._escape_cell(col) for col in header]]
```

其余不变。

- [ ] **Step 4: 运行**

```bash
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_pdf_export_charts.py -q -p no:randomly
```

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd /private/tmp/qa-chat-output-zh-labels
git add backend/app/services/pdf_export_service.py backend/app/tests/unit/test_pdf_export_charts.py
git commit -m "feat: PDF 导出表头使用字段中文名"
```

---

### Task 9: 前端表头渲染

**Files:**
- Modify: `frontend/src/types/chat.ts:41-46`
- Modify: `frontend/src/utils/chartContract.ts:56-70`
- Modify: `frontend/src/components/chat/ChartRenderer.tsx:54-77,200-244`
- Modify: `frontend/src/components/research/ReportRenderer.tsx:78-153,206-208`
- Test: `frontend/src/tests/ChartRenderer.test.tsx` / `frontend/src/tests/ResearchReportRenderer.test.tsx` / `frontend/src/tests/chartContract.test.ts`（追加）

**Interfaces:**
- Consumes: 后端 `columnLabels`（可选键）。
- Produces: `TablePayload.columnLabels?: Record<string, string>`；`asColumnLabels(value) -> Record<string, string>`；表头 `labels[key] ?? key`，`dataIndex` 仍用原始键。

- [ ] **Step 1: 写失败测试**

在 `frontend/src/tests/ChartRenderer.test.tsx`（已 import `render` / `screen` / `it`）追加：

```tsx
  it("TABLE 表头用 columnLabels 的中文名，缺键回落原始列名", () => {
    render(
      <ChartRenderer
        chartType="table"
        chartOption={{
          columns: ["rcv_qty", "supplier_code"],
          rows: [{ rcv_qty: 1, supplier_code: "B130" }],
          columnLabels: { rcv_qty: "收货数量（件）" },
        }}
      />
    );
    expect(screen.getByText("收货数量（件）")).toBeInTheDocument();
    expect(screen.getByText("supplier_code")).toBeInTheDocument();
    expect(screen.queryByText("rcv_qty")).not.toBeInTheDocument();
  });
```

在 `frontend/src/tests/ResearchReportRenderer.test.tsx` 的 `describe("ReportRenderer", …)` 内追加（用该文件既有的 `renderPayload` 辅助）：

```tsx
  it("table 块有 columnLabels 时表头用中文名", () => {
    renderPayload({
      sessionId: "s",
      turnId: "t",
      mode: "research",
      title: "报告",
      question: "q",
      sections: [
        {
          id: "1",
          kind: "data",
          title: "数据",
          blocks: [
            {
              type: "table",
              content: {
                columns: ["rcv_qty"],
                rows: [{ rcv_qty: 1 }],
                columnLabels: { rcv_qty: "收货数量（件）" },
              },
            },
          ],
        },
      ],
    });
    expect(screen.getByText("收货数量（件）")).toBeInTheDocument();
    expect(screen.queryByText("rcv_qty")).not.toBeInTheDocument();
  });
```

在 `frontend/src/tests/chartContract.test.ts` 追加（记得把 `asColumnLabels` 加进文件顶部从 `../utils/chartContract` 的 import）：

```ts
describe("asColumnLabels — 表头显示名收窄", () => {
  it("只收「string → 非空 string」的键值", () => {
    expect(asColumnLabels({ a: "甲", b: 2, c: "" })).toEqual({ a: "甲" });
  });

  it("非普通对象 → 空对象", () => {
    expect(asColumnLabels(null)).toEqual({});
    expect(asColumnLabels(["a"])).toEqual({});
  });
});
```

同文件 `describe("asTablePayload — 明细表负载收窄")` 内再加两条：

```ts
  it("columnLabels 收窄后透传；非法值被丢弃", () => {
    const payload = {
      columns: ["QTY"],
      rows: [{ QTY: 1 }],
      columnLabels: { QTY: "收货数量（件）", BAD: 1, EMPTY: "" },
    };
    expect(asTablePayload(payload)).toEqual({
      columns: ["QTY"],
      rows: [{ QTY: 1 }],
      columnLabels: { QTY: "收货数量（件）" },
    });
  });

  it("无 columnLabels 时结果不含该键（旧消息）", () => {
    expect(asTablePayload({ columns: ["QTY"], rows: [] })).not.toHaveProperty("columnLabels");
  });
```

- [ ] **Step 2: 运行确认失败**

```bash
cd /private/tmp/qa-chat-output-zh-labels/frontend && npx vitest run \
  src/tests/ChartRenderer.test.tsx \
  src/tests/ResearchReportRenderer.test.tsx \
  src/tests/chartContract.test.ts
```

Expected: FAIL（表头仍是 `rcv_qty`；`asColumnLabels` 尚未导出导致 import 解析失败也算 RED）。

- [ ] **Step 3: 类型与收窄**

`frontend/src/types/chat.ts` 的 `TablePayload`：

```ts
  // 原始列名 → 表头显示名（可含单位）。缺键 = 回落原始列名；旧消息无此键。
  columnLabels?: Record<string, string>;
```

`frontend/src/utils/chartContract.ts` 加：

```ts
/** 表头显示名收窄：只收「string → 非空 string」的键值；其余忽略。 */
export function asColumnLabels(value: unknown): Record<string, string> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return {};
  const labels: Record<string, string> = {};
  for (const [key, label] of Object.entries(value as Record<string, unknown>)) {
    if (typeof label === "string" && label.length > 0) labels[key] = label;
  }
  return labels;
}
```

`asTablePayload` 加：

```ts
  const columnLabels = asColumnLabels(record.columnLabels);
  if (Object.keys(columnLabels).length > 0) payload.columnLabels = columnLabels;
```

- [ ] **Step 4: ChartRenderer**

`DataTableProps` 加 `columnLabels?: Record<string, string>;`，`DataTable` 解构与列定义改为：

```tsx
  const labels = columnLabels ?? {};
  const columnDefs = (columns.length > 0 ? columns : rows.length > 0 ? Object.keys(rows[0]) : []).map(
    (key) => ({ title: labels[key] ?? key, dataIndex: key, key })
  );
```

TABLE 分支：

```tsx
        <DataTable
          columns={columnNames}
          rows={rows}
          truncated={isTruncatedTable}
          columnLabels={asColumnLabels(chartOption?.columnLabels)}
        />
```

明细表分支：

```tsx
                    <DataTable
                      columns={tableOption.columns}
                      rows={tableOption.rows}
                      truncated={tableOption.truncated === true}
                      columnLabels={tableOption.columnLabels}
                    />
```

import：`import { asColumnLabels } from "../../utils/chartContract";`。

- [ ] **Step 5: ReportRenderer**

`TableContent` 加 `columnLabels?: Record<string, string>;`；`asTableContent` / `asChartContent` 解析它：

```ts
  const columnLabels = asColumnLabels(value.columnLabels);
```

返回对象里带上（为空时给 `undefined` 或空对象都可，`DataTable` 用 `?? {}` 兜底）。

`DataTable`：

```tsx
function DataTable({ content }: { content: TableContent }) {
  const { columns, rows, columnLabels } = content;
  const labels = columnLabels ?? {};
  ...
          {columns.map((column) => (
            <th key={column}>{labels[column] ?? column}</th>
          ))}
```

- [ ] **Step 6: 运行前端测试 + tsc**

```bash
cd /private/tmp/qa-chat-output-zh-labels/frontend && npx vitest run \
  src/tests/ChartRenderer.test.tsx \
  src/tests/ResearchReportRenderer.test.tsx \
  src/tests/chartContract.test.ts
cd /private/tmp/qa-chat-output-zh-labels/frontend && npx tsc --noEmit
```

Expected: PASS；tsc 0 错误。

- [ ] **Step 7: Commit**

```bash
cd /private/tmp/qa-chat-output-zh-labels
git add frontend/src/types/chat.ts frontend/src/utils/chartContract.ts \
        frontend/src/components/chat/ChartRenderer.tsx \
        frontend/src/components/research/ReportRenderer.tsx \
        frontend/src/tests/ChartRenderer.test.tsx \
        frontend/src/tests/ResearchReportRenderer.test.tsx \
        frontend/src/tests/chartContract.test.ts
git commit -m "feat: 前端表头渲染中文字段名"
```

---

### Task 10: 变更记录 + 定向回归矩阵

**Files:**
- Modify: `Harness/changes/2026-11-01-chat-output-zh-labels/summary.md`（§8 交付记录）
- Create: `Harness/changes/2026-11-01-chat-output-zh-labels/evidence/regression.md`

- [ ] **Step 1: 后端定向回归**

```bash
cd /private/tmp/qa-chat-output-zh-labels/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test_zhlabels'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
PYTHONPATH=/private/tmp/qa-chat-output-zh-labels/backend \
  /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/.venv/bin/python -m pytest \
  app/tests/unit/test_field_display_resolver.py app/tests/unit/test_field_hint_text.py \
  app/tests/unit/test_field_hint_wiring.py app/tests/unit/test_chart_output_zh_labels.py \
  app/tests/unit/test_chart_spec_builder.py app/tests/unit/test_visual_payload.py \
  app/tests/unit/test_report_renderer.py app/tests/unit/test_pdf_export_charts.py \
  app/tests/unit/test_ontology_property.py \
  app/tests/integration/test_ontology_property_unit.py \
  app/tests/integration/test_chat_zh_labels.py app/tests/integration/test_chat_api.py \
  app/tests/integration/test_chat_multi_step.py \
  -q -p no:randomly
```

Expected: 全绿；若基线有红，记录「净新增失败 0」的失败集合对差。

- [ ] **Step 2: 前端全量**

```bash
cd /private/tmp/qa-chat-output-zh-labels/frontend
npx tsc --noEmit
npm test -- --run
```

Expected: tsc 0 错误；vitest 与基线对差后净新增失败 0。

- [ ] **Step 3: 写交付记录**

在 `summary.md` §8 填：分支名、每个 commit 的 SHA、Task 0 基线、最终回归结果、两条已确认偏差（EvidencePanel / 聚合别名反查）。`evidence/regression.md` 存命令与结果摘要。

- [ ] **Step 4: Commit**

```bash
cd /private/tmp/qa-chat-output-zh-labels
git add Harness/changes/2026-11-01-chat-output-zh-labels/summary.md \
        Harness/changes/2026-11-01-chat-output-zh-labels/evidence/regression.md
git commit -m "docs: chat 输出中文化交付记录"
```

---

## 自检（写完计划后已执行）

**1. 规格覆盖**

| summary.md 条目 | 覆盖任务 |
|---|---|
| 表头中文名（缺则回落字段名） | Task 2/4/5/8（PDF）/9（前端） |
| 图例中文名 | Task 4/5（measure.label → series.name） |
| 缺中文含义提示用户补充 | Task 3/6/7 |
| 数值字段显示单位 | Task 1/2/4 |
| 缺单位提示在本体属性定义 | Task 3/6/7 |
| 本体属性增加 unit | Task 1 |
| 复用 KPI/特征单位 | Task 2 |
| 提示随消息落库、PDF/回放/报告继承 | Task 6（落库文本）/Task 7（MD）/Task 8（PDF） |
| 单位并入表头/图例 | Task 4 |
| 不新增表题 | 无表题改动，符合 |
| 前端渲染 | Task 9 |
| 真实 PG 集成测试 | Task 1/2/5 |

**2. 占位符扫描**：无 TBD/TODO、「类似 Task N」或「按需补全」式说明；每个代码步骤都给出可直接落盘的完整片段。唯一允许执行者自行调整的是行号（随实现漂移），不得调整代码语义。

**3. 类型一致性**

- `resolveFieldDisplays(session, *, columns, rows, plan=None, sourceTable=None) -> dict[str, FieldDisplay]`：Task 2 定义，Task 5 两处调用一致。
- `missingFieldHints(columnLabels, displays)`：Task 2 定义，Task 5 两处调用一致。
- `composeFieldHintText(hints, *, columnLabels=None)` / `appendFieldHints(answer, hints, *, columnLabels=None)` / `fieldHintsOf(payload)` / `columnLabelsOf(*payloads)` / `hintsFromSteps(steps)`：Task 3 定义，Task 6/7 调用一致。
- `buildSpec(..., units: Mapping[str, str] | None = None)`：Task 4 定义，Task 5 两处调用一致。
- `assembleTableOption(..., columnLabels=...)`：Task 4 定义，Task 5 调用一致。
- 前端 `TablePayload.columnLabels?: Record<string, string>`：Task 9 内三处消费一致。
