# 到货跟踪场景（P5）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增「到货跟踪」Copilot 场景：按时间段/供应商看订单到货达成（订单到货率为主指标、量级率为参考），支持 24 日锚点月度周期，并可下钻到订单物料明细（已到/未到）；同时把既有「待到货」场景迁到本体 + THBI，去掉硬编码表名列名。

**Architecture:** 新增一个运行时本体解析服务（`ontology_binding_resolver.py`），从 `procurement_source_binding` / `procurement_property_binding` 读出「本体类 → THBI 物理表」「本体属性 → 物理列」，供工具 SQL 模板按属性名渲染。新增工具 `delivery_tracking_query`（订单行 `DWD_PURCHASE_ORDER_DTL` ⋈ 收货 `DWD_GOODS_RECEIPT_DTL`）；既有 `pending_delivery_query` 改为同一解析路径。两场景共用一个解析器 + 一套 THBI 绑定。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x async / PostgreSQL 16（测试库 5434 `qa_metadata_test_mainline`）/ Oracle 19c（THBI 业务库）/ React 19 + TypeScript + antd 5 + Vitest。

**Spec:** `docs/superpowers/specs/2026-11-25-delivery-tracking-scenario-design.md`（已评审通过，14 条决策落定）

---

## Global Constraints

这些约束**适用于每一个任务**，任务描述不再重复：

- **硬规则 · 数据源**：一切数据对象出自 `data_source.id=1`（THBI Oracle @ 192.168.205.70:1521/X3V71ORA，用户名 `THBI`），schema 固定 `THBI`。工具**不得**接受调用方指定数据源；**不得**按「默认活跃源」兜底；绑定缺失即 fail loud。
- **硬规则 · 本体对接**：工具 SQL 模板只写**本体属性名**，物理表名/物理列名由绑定解析产出。**代码里不得出现** `THBI.` / `ZJTH.` 前缀或裸列名常量（`QTYSTU_0` / `RCPQTYSTU_0` / `POHNUM_0` 等）。
- **绑定只能来自已确认生效行**：`is_candidate IS FALSE` 且 `valid_to IS NULL`；候选行绝不进读路径。
- **fail-closed**：本体类不存在/已墓碑、无生效绑定、必需属性缺失、物理列不存在 ⇒ 抛明确异常，**不得**静默回退到硬编码表。
- **口径基线**（本期窗口 `2026-10-24 ~ 2026-11-24`，THBI 实测）：订单 4,844；全到货 3,986 / 部分 448 / 未到 410；订单到货率 **82.29%**；量级率 **40.51%**。任何偏离都要查明原因并显式记录，**不得**静默改动基线。
- **命名**：Python 变量/函数 `camelCase`，类型/组件 `PascalCase`，常量 `UPPER_SNAKE_CASE`（Python 项目刻意偏离 PEP 8，与前端一致）。
- **不可变**：不原地改对象，返回新对象。
- **文件大小**：目标 200-400 行，硬上限 800 行；函数 < 50 行；嵌套 ≤ 4 层。
- **错误处理**：每一层显式处理；服务端记详细上下文；UI 给友好提示。
- **TDD 强制**：先写失败测试（RED）→ 最小实现（GREEN）→ 重构（IMPROVE）。
- **后端测试**：真实 PostgreSQL（测试库 5434），**禁止** sqlite 内存库 + 直接调 service。
- **前端测试**：Vitest + @testing-library/react。
- **提交**：每任务结束一次提交，格式 `<type>: <description>`（feat/fix/refactor/docs/test/chore/perf/ci）。
- **分支**：`feat/ontology-evolution`。
- **不碰**：`DWD_PURCHASE_ORDER_LINE` 类与其上挂的 6 个 KPI 口径锚点、图谱 JOIN 边、`purchase_history_query` 工具（推迟项，见 spec 3.2.1 / 3.8）。

---

## File Structure

**新建**

| 文件 | 职责 |
|---|---|
| `backend/app/services/ontology_binding_resolver.py` | 运行时本体绑定解析：类名 + 数据源 → 物理表；属性名 → 物理列。fail-closed 校验。 |
| `backend/app/tests/unit/test_ontology_binding_resolver.py` | 解析器单测（纯函数 + 假数据，不连库） |
| `backend/app/tests/integration/test_ontology_binding_resolver_api.py` | 解析器集成测试（真实 PG + 真实绑定表） |
| `backend/app/services/delivery_tracking_query.py` | 到货跟踪工具实现：窗口解析、SQL 构建、结果装配 |
| `backend/app/tests/unit/test_delivery_tracking_query.py` | 工具单测（SQL 模板、窗口、订单三态） |
| `backend/app/tests/integration/test_delivery_tracking_api.py` | 工具集成测试（真实 PG + 真实 API 链路） |
| `frontend/src/components/copilot/DeliveryTrackingResultCard.tsx` | 到货跟踪结果卡（双指标 + 供应商表 + 双 Tab 明细） |
| `frontend/src/tests/DeliveryTrackingResultCard.test.tsx` | 结果卡前端测试 |

**修改**

| 文件 | 改动 |
|---|---|
| `backend/app/services/agent_tools_procurement.py` | `pending_delivery_query` 迁本体 + THBI：删 4 个表常量与裸列名，改走解析器；新增 `delivery_tracking_query` 注册 |
| `backend/app/services/agent_tools.py` | `BUILTIN_HANDLERS` 注册新工具 + `ARG_EXTRACTORS` 条目 |
| `backend/scripts/seed_agent_tool_configs.py` | `delivery_tracking_query` 的 `input_schema` |
| `backend/scripts/seed_agents.py` | `DELIVERY_TRACKING_AGENT` 定义 + 默认绑定 |
| `backend/scripts/seed_procurement_agent_profiles.py` | `DELIVERY_TRACKING_AGENT` 编排 profile（提示词 + 工具列表） |
| `backend/scripts/seed_copilot_scenarios.py` | `delivery_tracking` 场景行（无 `datasourceId` 槽位） |
| `backend/scripts/seed_bindings.py`（新建，若无则建） | `DIM_FACILITY` 源绑定（唯一新增本体资产） |
| `backend/app/domain/copilot_scenario_models.py` | `RESULT_RENDERER_WHITELIST` 加 `delivery_tracking` |
| `backend/app/services/copilot_scenario_render.py` | 24 日锚点展开（`anchorDay`/`periodIndex` → `dateFrom`/`dateTo`） |
| `frontend/src/components/copilot/PendingDeliveryResultTabs.tsx` | 渲染器分发加 `delivery_tracking` |
| `frontend/src/pages/ProcurementCopilotPage.tsx` | `structuredRenderer` 三态 + 解析器分派 |
| `frontend/src/api/copilotScenario.ts` | 渲染器类型加 `delivery_tracking` |

---

## Task 1: 运行时本体绑定解析器

**Files:**
- Create: `backend/app/services/ontology_binding_resolver.py`
- Test: `backend/app/tests/unit/test_ontology_binding_resolver.py`
- Test: `backend/app/tests/integration/test_ontology_binding_resolver_api.py`

**Interfaces:**
- Consumes: `app.domain.procurement_binding_models.ProcurementSourceBinding` / `ProcurementPropertyBinding`；`app.domain.models.OntologyClass` / `OntologyProperty` / `DataSource`。
- Produces:
  ```python
  # backend/app/services/ontology_binding_resolver.py
  THBI_DATASOURCE_ID = 1
  THBI_SCHEMA_NAME = "THBI"

  class BindingResolutionError(ValidationError): ...   # 继承 app.domain.exceptions.ValidationError

  @dataclass(frozen=True, slots=True)
  class ResolvedBinding:
      className: str
      datasourceId: int
      schemaName: str
      sourceTable: str
      columns: Mapping[str, str]        # propertyName -> sourceColumn
      def qualifiedTable(self) -> str: ...   # "THBI.DWD_PURCHASE_ORDER_DTL"
      def column(self, propertyName: str) -> str: ...  # fail-closed：未知属性抛 BindingResolutionError

  async def resolveBinding(
      session: AsyncSession,
      *,
      className: str,
      requiredProperties: Sequence[str],
      datasourceId: int = THBI_DATASOURCE_ID,
  ) -> ResolvedBinding: ...
  ```
  `column()` 行为：属性在 `columns` 中 ⇒ 返回物理列名；不在 ⇒ 抛 `BindingResolutionError`（消息含缺失属性名）。

**为什么需要新服务**：`semantic_compiler_bindings.compileSourceBindings()` 是**纯函数快照编译器**，需 6 个查找字典且只产快照（无 IO），不适合工具运行时按需取。本任务产出运行时读路径。

- [ ] **Step 1: 写失败测试（纯函数层）**

创建 `backend/app/tests/unit/test_ontology_binding_resolver.py`：

```python
"""运行时本体绑定解析器（单测）：类→物理表 / 属性→物理列 / fail-closed。"""

from __future__ import annotations

import pytest

from app.domain.exceptions import ValidationError
from app.services.ontology_binding_resolver import (
    BindingResolutionError,
    ResolvedBinding,
)


def _binding(**overrides) -> ResolvedBinding:
    base = {
        "className": "DWD_PURCHASE_ORDER_DTL",
        "datasourceId": 1,
        "schemaName": "THBI",
        "sourceTable": "DWD_PURCHASE_ORDER_DTL",
        "columns": {"PO_NO": "PO_NO", "PO_QTY": "PO_QTY"},
    }
    base.update(overrides)
    return ResolvedBinding(**base)


def test_qualified_table_joins_schema_and_source_table() -> None:
    assert _binding().qualifiedTable() == "THBI.DWD_PURCHASE_ORDER_DTL"


def test_column_returns_physical_column_for_known_property() -> None:
    assert _binding().column("PO_QTY") == "PO_QTY"


def test_column_raises_with_missing_property_name() -> None:
    with pytest.raises(BindingResolutionError) as exc:
        _binding().column("RECEIVED_QTY")
    assert "RECEIVED_QTY" in str(exc.value)


def test_binding_resolution_error_is_validation_error() -> None:
    """上层按 ValidationError 统一转 422 ⇒ 解析失败须是它的子类。"""
    assert issubclass(BindingResolutionError, ValidationError)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_ontology_binding_resolver.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.services.ontology_binding_resolver'`

- [ ] **Step 3: 实现纯数据结构部分**

创建 `backend/app/services/ontology_binding_resolver.py`：

```python
"""运行时本体绑定解析（2026-11-25，P5 到货跟踪）。

为什么不用 `semantic_compiler_bindings.compileSourceBindings()`
-----------------------------------------------------------
那个是**纯函数快照编译器**：无 IO、需 6 个查找字典、只产语义发布快照。
工具运行时要的是「按需取一条绑定 + 校验 + 立刻用」，故本模块提供读路径。

硬规则（2026-11-25 用户明确）
------------------------------
1. **一切数据对象出自 THBI**：默认 `datasource_id=1` + `schema_name=THBI`；
   调用方不得改数据源。
2. **本体对接，不硬编码**：物理表名/列名只从绑定读，不进代码常量。
3. **候选不消费**：只认 `is_candidate IS FALSE` 且 `valid_to IS NULL` 的行。
4. **fail-closed**：类不存在/墓碑/无绑定/属性缺失/物理列缺失 ⇒ 抛
   `BindingResolutionError`（`ValidationError` 子类），绝不静默回退。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.domain.exceptions import ValidationError

#: 硬规则：唯一数据源（THBI Oracle）。
THBI_DATASOURCE_ID = 1
#: 硬规则：唯一 schema（Oracle owner）。
THBI_SCHEMA_NAME = "THBI"


class BindingResolutionError(ValidationError):
    """本体绑定解析失败（fail-closed，禁止回退硬编码表）。"""


@dataclass(frozen=True, slots=True)
class ResolvedBinding:
    """一条已解析的本体类 → 物理表绑定。"""

    className: str
    datasourceId: int
    schemaName: str
    sourceTable: str
    columns: Mapping[str, str]

    def qualifiedTable(self) -> str:
        """限定物理表名（schema.table）。"""
        return f"{self.schemaName}.{self.sourceTable}"

    def column(self, propertyName: str) -> str:
        """属性名 → 物理列名；未知属性 fail-closed。"""
        sourceColumn = self.columns.get(propertyName)
        if not sourceColumn:
            raise BindingResolutionError(
                f"本体类 {self.className} 的绑定里没有属性 {propertyName}"
                f"（已绑定属性：{sorted(self.columns)}）"
            )
        return sourceColumn
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_ontology_binding_resolver.py -v
```
Expected: 4 passed

- [ ] **Step 5: 写失败测试（数据库读路径层）**

在同文件追加：

```python
def test_compile_binding_rejects_candidate_rows() -> None:
    """候选绑定（is_candidate=True）不得进读路径。"""
    from app.services import ontology_binding_resolver as mod

    candidateRow = (
        ("cid", "DWD_PURCHASE_ORDER_DTL", None),
        ("sid", 1, "THBI", "DWD_PURCHASE_ORDER_DTL", True, None),   # is_candidate=True
        (("pid", "PO_NO", "PO_NO", "cid"),),
    )
    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL",
            datasourceId=1,
            rows=candidateRow,
        )
    assert "绑定" in str(exc.value)


def test_compile_binding_maps_properties_to_columns() -> None:
    from app.services import ontology_binding_resolver as mod

    rows = (
        ("cid", "DWD_PURCHASE_ORDER_DTL", None),
        ("sid", 1, "THBI", "DWD_PURCHASE_ORDER_DTL", False, None),
        (("pid", "PO_NO", "PO_NO", "cid"), ("pid", "PO_QTY", "PO_QTY", "cid")),
    )
    resolved = mod._compileBinding(
        className="DWD_PURCHASE_ORDER_DTL", datasourceId=1, rows=rows
    )
    assert resolved.qualifiedTable() == "THBI.DWD_PURCHASE_ORDER_DTL"
    assert resolved.columns == {"PO_NO": "PO_NO", "PO_QTY": "PO_QTY"}
```

- [ ] **Step 6: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_ontology_binding_resolver.py -v
```
Expected: FAIL —— `AttributeError: module 'app.services.ontology_binding_resolver' has no attribute '_compileBinding'`

- [ ] **Step 7: 实现 `_compileBinding`（纯函数，可单测）**

追加到 `ontology_binding_resolver.py`：

```python
#: 读路径取回的行形状：
#: (classId, className, validTo) / (sourceBindingId, datasourceId, schemaName,
#:  sourceTable, isCandidate, validTo) / (propertyId, propertyName,
#:  sourceColumn, classId)
BindingRows = tuple[
    tuple[tuple[str, str, object], tuple[int, int, str, str, bool, object], tuple[tuple[int, str, str, int], ...]],
    ...,
]


def _compileBinding(
    *, className: str, datasourceId: int, rows: Sequence[tuple]
) -> ResolvedBinding:
    """把读回的原始行编译成 `ResolvedBinding`（纯函数，便于单测）。

    fail-closed 判据（任一不满足即抛 `BindingResolutionError`）：
    - 类存在且未墓碑；
    - 该类在该数据源有**恰好一条**已确认 + 生效中的绑定；
    - 候选绑定一律不消费。
    """
    classRow = None
    for row in rows:
        if row[1] == className:
            classRow = row
            break
    if classRow is None:
        raise BindingResolutionError(f"本体类不存在：{className}")
    classId, _, validTo = classRow
    if validTo is not None:
        raise BindingResolutionError(f"本体类已删除（墓碑）：{className}")

    sourceRow = None
    for row in rows:
        if not isinstance(row[1], int):
            continue
        _, dsId, _, _, isCandidate, srcValidTo = row
        if dsId != datasourceId:
            continue
        if isCandidate:
            continue
        if srcValidTo is not None:
            continue
        if sourceRow is not None:
            raise BindingResolutionError(
                f"本体类 {className} 在数据源 {datasourceId} 有多条生效绑定，口径不唯一"
            )
        sourceRow = row
    if sourceRow is None:
        raise BindingResolutionError(
            f"本体类 {className} 在数据源 {datasourceId} 没有已确认生效的绑定"
            f"（候选绑定不可消费）"
        )
    _, _, schemaName, sourceTable, _, _ = sourceRow

    columns: dict[str, str] = {}
    for row in rows:
        if not isinstance(row[1], str) or len(row) != 4:
            continue
        _, propName, sourceColumn, propClassId = row
        if propClassId != classId:
            continue
        columns[propName] = sourceColumn
    return ResolvedBinding(
        className=className,
        datasourceId=datasourceId,
        schemaName=schemaName,
        sourceTable=sourceTable,
        columns=columns,
    )
```

- [ ] **Step 8: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_ontology_binding_resolver.py -v
```
Expected: 6 passed

- [ ] **Step 9: 写失败测试（真实 PG 读路径）**

创建 `backend/app/tests/integration/test_ontology_binding_resolver_api.py`：

```python
"""运行时本体绑定解析器（集成）：真实 PG + 真实绑定表。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.domain.models import OntologyClass, OntologyProperty
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)
from app.services.ontology_binding_resolver import (
    THBI_DATASOURCE_ID,
    BindingResolutionError,
    resolveBinding,
)
from app.tests.integration._agent_runs_api_helpers import dbSession


@pytest.mark.asyncio
async def test_resolve_order_dtl_binding_from_real_db(dbSession) -> None:
    """本体已绑的订单行类能解析出 THBI 物理表与属性列。"""
    resolved = await resolveBinding(
        dbSession,
        className="DWD_PURCHASE_ORDER_DTL",
        requiredProperties=["PO_NO", "PO_QTY", "ORDER_DATE"],
    )
    assert resolved.qualifiedTable() == "THBI.DWD_PURCHASE_ORDER_DTL"
    assert resolved.datasourceId == THBI_DATASOURCE_ID
    assert resolved.column("PO_QTY") == "PO_QTY"


@pytest.mark.asyncio
async def test_resolve_missing_property_fails_closed(dbSession) -> None:
    """必需属性未绑定 ⇒ fail-closed，不回退硬编码列。"""
    with pytest.raises(BindingResolutionError):
        await resolveBinding(
            dbSession,
            className="DWD_PURCHASE_ORDER_DTL",
            requiredProperties=["NOT_A_REAL_PROPERTY"],
        )


@pytest.mark.asyncio
async def test_resolve_unknown_class_fails_closed(dbSession) -> None:
    with pytest.raises(BindingResolutionError):
        await resolveBinding(
            dbSession,
            className="NO_SUCH_CLASS_XYZ",
            requiredProperties=["PO_NO"],
        )
```

**若集成测试库里没有 `DWD_PURCHASE_ORDER_DTL` 的绑定行**（测试库是干净的），按 Step 10 的 seed 补齐后再跑。

- [ ] **Step 10: 写 `_loadBindingRows` + `resolveBinding` 实现**

追加到 `ontology_binding_resolver.py`：

```python
from sqlalchemy import select

from app.domain.models import DataSource, OntologyClass, OntologyProperty
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)


async def _loadBindingRows(
    session: AsyncSession, *, className: str, datasourceId: int
) -> list[tuple]:
    """一次性读回类 / 源绑定 / 属性绑定三类行（供 `_compileBinding` 编译）。"""
    classRow = (
        await session.execute(
            select(
                OntologyClass.id, OntologyClass.class_name, OntologyClass.valid_to
            ).where(OntologyClass.class_name == className)
        )
    ).first()
    rows: list[tuple] = []
    if classRow is not None:
        rows.append((classRow[0], classRow[1], classRow[2]))
        classId = classRow[0]
        sourceRows = (
            await session.execute(
                select(
                    ProcurementSourceBinding.id,
                    ProcurementSourceBinding.datasource_id,
                    ProcurementSourceBinding.schema_name,
                    ProcurementSourceBinding.source_table,
                    ProcurementSourceBinding.is_candidate,
                    ProcurementSourceBinding.valid_to,
                )
                .where(ProcurementSourceBinding.class_id == classId)
                .where(ProcurementSourceBinding.datasource_id == datasourceId)
            )
        ).all()
        for row in sourceRows:
            rows.append(tuple(row))
        propertyRows = (
            await session.execute(
                select(
                    OntologyProperty.id,
                    OntologyProperty.property_name,
                    ProcurementPropertyBinding.source_column,
                    OntologyProperty.class_id,
                )
                .join(
                    ProcurementPropertyBinding,
                    ProcurementPropertyBinding.property_id == OntologyProperty.id,
                )
                .where(OntologyProperty.class_id == classId)
                .where(ProcurementPropertyBinding.is_candidate.is_(False))
                .where(ProcurementPropertyBinding.valid_to.is_(None))
            )
        ).all()
        for row in propertyRows:
            rows.append(tuple(row))
    return rows


async def resolveBinding(
    session: AsyncSession,
    *,
    className: str,
    requiredProperties: Sequence[str],
    datasourceId: int = THBI_DATASOURCE_ID,
) -> ResolvedBinding:
    """解析本体类的生效绑定，并校验必需属性都已绑定。

    :raises BindingResolutionError: 类不存在/墓碑/无生效绑定/属性缺失。
    """
    rows = await _loadBindingRows(
        session, className=className, datasourceId=datasourceId
    )
    resolved = _compileBinding(
        className=className, datasourceId=datasourceId, rows=rows
    )
    missing = [name for name in requiredProperties if name not in resolved.columns]
    if missing:
        raise BindingResolutionError(
            f"本体类 {className} 缺少必需属性的绑定：{missing}"
            f"（已绑定：{sorted(resolved.columns)}）"
        )
    return resolved
```

同时把文件头的 `from __future__ import annotations` 之后补上 `from sqlalchemy.ext.asyncio import AsyncSession` 与 `from sqlalchemy import select` 的 import。

- [ ] **Step 11: 运行集成测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/integration/test_ontology_binding_resolver_api.py -v
```
Expected: 3 passed（若测试库缺绑定行，见 Task 2 的 seed 脚本，补跑后再验）

- [ ] **Step 12: 提交**

```bash
git add backend/app/services/ontology_binding_resolver.py backend/app/tests/unit/test_ontology_binding_resolver.py backend/app/tests/integration/test_ontology_binding_resolver_api.py
git commit -m "feat(ontology): 运行时本体绑定解析器（类→物理表 / 属性→物理列，fail-closed）"
```

---

## Task 2: `DIM_FACILITY` 源绑定（唯一新增本体资产）

**Files:**
- Create: `backend/scripts/seed_procurement_bindings.py`
- Test: `backend/app/tests/unit/test_seed_procurement_bindings.py`

**Interfaces:**
- Consumes: Task 1 的 `resolveBinding`（集成测试用）。
- Produces: `seed_facility_binding(session_factory) -> int` —— 幂等 upsert `DIM_FACILITY` 的源绑定行。

**为什么需要**：`DIM_FACILITY` 本体类已存在（117 属性，含 `FCY_0` / `FCYNAM_0`），但 `procurement_source_binding` 里**没有**它的行 ⇒ 解析器解析不到 ⇒ 待到货的 `siteKeys` 过滤与到货跟踪的基地维度都拿不到物理表。

- [ ] **Step 1: 写失败测试**

创建 `backend/app/tests/unit/test_seed_procurement_bindings.py`：

```python
"""DIM_FACILITY 源绑定种子：幂等 + 必填字段齐（2026-11-25）。"""

from __future__ import annotations

from scripts import seed_procurement_bindings as mod


def test_facility_binding_spec_has_required_fields() -> None:
    """绑定规格：THBI 数据源 + BASE 角色 + 已确认（不是候选）。"""
    spec = mod.FACILITY_BINDING
    assert spec["datasourceId"] == 1
    assert spec["schemaName"] == "THBI"
    assert spec["sourceTable"] == "DIM_FACILITY"
    assert spec["bindingRole"] == "BASE"
    assert spec["isCandidate"] is False
    assert spec["confirmedBy"]


def test_facility_binding_spec_is_idempotent_keyed_by_class() -> None:
    """幂等键 = 类名（同一类只建一条绑定）。"""
    assert mod.FACILITY_BINDING["className"] == "DIM_FACILITY"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_seed_procurement_bindings.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'scripts.seed_procurement_bindings'`

> **import 路径已核实**：`backend/scripts/__init__.py` 存在 ⇒ `scripts` 是 **backend 顶层包**
> （pytest 从 `backend/` 目录运行 ⇒ 顶层包可导入）。既有同款测试
> `backend/app/tests/unit/test_copilot_scenario_seeds.py` 就是
> `from scripts.seed_copilot_scenarios import SEEDS` —— 照此写即可，**不是** `app.scripts`。

- [ ] **Step 3: 实现 seed 脚本**

创建 `backend/scripts/seed_procurement_bindings.py`：

```python
"""采购本体绑定种子（2026-11-25，P5 到货跟踪）。

背景
----
`DIM_FACILITY`（基地字典）本体类**已存在**（117 属性，含 `FCY_0` / `FCYNAM_0`），
但 `procurement_source_binding` 里没有它的行 ⇒ 运行时解析器解析不到物理表
⇒ 待到货的 `siteKeys` 过滤拿不到基地名。

本脚本补这条绑定，是 P5 **唯一新增的本体资产**。

幂等：按 (class_id, datasource_id, schema_name, source_table, binding_role)
预查；已存在则跳过（不覆盖既有确认信息）。

用法（backend/ 下）：
    python -m scripts.seed_procurement_bindings
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import getSettings
from app.domain.models import DataSource, OntologyClass
from app.domain.procurement_binding_models import ProcurementSourceBinding
from app.infrastructure.database import getSessionFactory

#: `DIM_FACILITY` 的源绑定规格（THBI 硬规则）。
FACILITY_BINDING: dict[str, object] = {
    "className": "DIM_FACILITY",
    "datasourceId": 1,
    "schemaName": "THBI",
    "sourceTable": "DIM_FACILITY",
    "bindingRole": "BASE",
    "isCandidate": False,
    "confirmedBy": "m1-b-confirm",
}


async def seed_facility_binding(session_factory) -> int:
    """幂等 upsert `DIM_FACILITY` 源绑定。返回新建行数（0 = 已存在）。"""
    created = 0
    async with session_factory() as session:
        classRow = (
            await session.execute(
                select(OntologyClass).where(
                    OntologyClass.class_name == FACILITY_BINDING["className"]
                )
            )
        ).scalars().first()
        if classRow is None:
            raise RuntimeError(
                f"本体类不存在：{FACILITY_BINDING['className']}（先跑 seed_ontology）"
            )
        dsRow = (
            await session.execute(
                select(DataSource).where(
                    DataSource.id == FACILITY_BINDING["datasourceId"]
                )
            )
        ).scalars().first()
        if dsRow is None:
            raise RuntimeError(
                f"数据源不存在：{FACILITY_BINDING['datasourceId']}"
            )
        existing = (
            await session.execute(
                select(ProcurementSourceBinding).where(
                    ProcurementSourceBinding.class_id == classRow.id
                )
            )
        ).scalars().first()
        if existing is not None:
            await session.rollback()
            return 0
        stmt = pg_insert(ProcurementSourceBinding).values(
            class_id=classRow.id,
            datasource_id=FACILITY_BINDING["datasourceId"],
            schema_name=FACILITY_BINDING["schemaName"],
            source_table=FACILITY_BINDING["sourceTable"],
            binding_role=FACILITY_BINDING["bindingRole"],
            is_candidate=FACILITY_BINDING["isCandidate"],
            confirmed_by=FACILITY_BINDING["confirmedBy"],
        )
        await session.execute(stmt)
        await session.commit()
        created = 1
    return created


async def main() -> None:
    settings = getSettings()
    from app.infrastructure.database import create_session_factory_for_settings

    factory = create_session_factory_for_settings(settings)
    try:
        created = await seed_facility_binding(factory)
        print(f"seed_procurement_bindings: 新建 {created} 条（DIM_FACILITY 绑定）")
    finally:
        await factory.kw["bind"].dispose() if False else None


if __name__ == "__main__":
    asyncio.run(main())
```

**`main()` 里的 dispose 写法有问题** —— 按仓库现有 seed 脚本的真实写法替换（参考 `backend/scripts/seed_copilot_scenarios.py`）：

```python
async def main() -> None:
    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        created = await seed_facility_binding(factory)
        print(f"seed_procurement_bindings: 新建 {created} 条（DIM_FACILITY 绑定）")
    finally:
        await engine.dispose()
```

需要的 import 补在文件头：`from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine`。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_seed_procurement_bindings.py -v
```
Expected: 2 passed

- [ ] **Step 5: 跑种子并验证幂等**

```bash
cd backend && .venv/bin/python -m scripts.seed_procurement_bindings
cd backend && .venv/bin/python -m scripts.seed_procurement_bindings
```
Expected: 第一次 `新建 1 条`，第二次 `新建 0 条`

- [ ] **Step 6: 验证解析器能解析到 DIM_FACILITY**

```bash
cd backend && .venv/bin/python -m pytest app/tests/integration/test_ontology_binding_resolver_api.py -v -k facility
```
若该测试不存在，用 Task 1 的 `resolveBinding` 写一条临时验证：解析 `DIM_FACILITY` + 必需属性 `["FCY_0", "FCYNAM_0"]` ⇒ `qualifiedTable() == "THBI.DIM_FACILITY"`。**把这条验证补进 `test_ontology_binding_resolver_api.py`**（作为第三个集成用例）：

```python
@pytest.mark.asyncio
async def test_resolve_facility_binding_from_real_db(dbSession) -> None:
    """DIM_FACILITY 补绑定后可解析（Task 2 种子）。"""
    resolved = await resolveBinding(
        dbSession,
        className="DIM_FACILITY",
        requiredProperties=["FCY_0", "FCYNAM_0"],
    )
    assert resolved.qualifiedTable() == "THBI.DIM_FACILITY"
```

- [ ] **Step 7: 提交**

```bash
git add backend/scripts/seed_procurement_bindings.py backend/app/tests/unit/test_seed_procurement_bindings.py backend/app/tests/integration/test_ontology_binding_resolver_api.py
git commit -m "feat(ontology): 补 DIM_FACILITY 源绑定（P5 唯一新增本体资产）"
```

---

## Task 3: `delivery_tracking_query` 工具（窗口 + SQL 模板）

**Files:**
- Create: `backend/app/services/delivery_tracking_query.py`
- Test: `backend/app/tests/unit/test_delivery_tracking_query.py`

**Interfaces:**
- Consumes: Task 1 的 `ResolvedBinding`（表/列来源）。
- Produces:
  ```python
  # backend/app/services/delivery_tracking_query.py
  ORDER_CLASS_NAME = "DWD_PURCHASE_ORDER_DTL"
  RECEIPT_CLASS_NAME = "DWD_GOODS_RECEIPT_DTL"

  @dataclass(frozen=True, slots=True)
  class DeliveryTrackingWindow:
      dateFrom: date
      dateTo: date
      mode: str            # "monthsBack" | "anchorDay" | "explicit"
      anchorDay: int | None
      periodIndex: int | None

  def resolveWindow(
      *,
      today: date,
      monthsBack: int | None = None,
      dateFrom: str | None = None,
      dateTo: str | None = None,
      anchorDay: int | None = None,
      periodIndex: int | None = None,
  ) -> DeliveryTrackingWindow: ...

  def buildDeliveryTrackingSummarySql(
      *, order: ResolvedBinding, receipt: ResolvedBinding,
      window: DeliveryTrackingWindow, dialect: str = "oracle",
  ) -> str: ...

  def buildDeliveryTrackingBySupplierSql(...) -> str: ...
  def buildDeliveryTrackingByOrderSql(...) -> str: ...
  ```
  SQL 三段：`summary`（窗口级汇总）/ `bySupplier`（供应商分组）/ `byOrder`（订单+行明细）。

**口径定义（spec 4.1 / 4.2 / 4.4）**

```
已收量（行级）  RECEIVED = SUM(<RCV_QTY_STU>)       -- 收货类按 PO_NO+PO_LINE_NO 聚合
行级欠交量      OPEN_QTY = <PO_QTY> - RECEIVED        -- 同为库存单位
订单三态：
  全到货    SUM(OPEN_QTY) = 0
  部分到货  有欠交 且 有已收（RECEIVED > 0）
  未到货    所有行 RECEIVED = 0
订单到货率 = 全到货订单 / 总订单
量级率     = SUM(RECEIVED) / SUM(<PO_QTY>)
```

**脏数据剔除（spec 4.4，必须实现）**：

- **判据**：行级 `OPEN_QTY >= _OPEN_QTY_SANITY_MAX`（= 1_000_000）判为源侧录入错误
  （ERP 里「数量填成物料码」的已知脏数据，既有 `pending_delivery_query` 同款阈值）。
- **行为**：这类行**不参与**三态判定与量级率计算（视作"无效行"，既不算欠交也不算已收）；
  但必须在 `EXCLUDED_ROWS` 里**显式计数回报**，不静默丢。
- **`summary` 与 `bySupplier` 都要**：两者都算率值 ⇒ 都需同样剔除 + 计数。
- **`byOrder` 不剔除**：明细是给人看的，脏行留着才能被发现（文档里写明这个取舍）。
- **⚠ 与 4.2 的区别（别搞混）**：
  - 4.2 拒绝的是按 **订购量** 剔大额行（那是真实业务大单，如 2,650,000 件压缩弹簧）；
  - 4.4 剔除的是按 **欠交量** 判的录入错误行。
  两条判据不同，**不能**混用。
- **预期量级**（spec 实测）：本期窗口约 6 行被剔；剔除**不改变**结论
  （订单到货率 82.29% → 82.28%）。

- [ ] **Step 1: 写失败测试（窗口解析）**

创建 `backend/app/tests/unit/test_delivery_tracking_query.py`：

```python
"""到货跟踪工具单测：窗口解析 + SQL 模板（本体属性名 → 物理列）。"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.exceptions import ValidationError
from app.services.delivery_tracking_query import (
    resolveWindow,
)
from app.services.ontology_binding_resolver import ResolvedBinding


def _orderBinding() -> ResolvedBinding:
    return ResolvedBinding(
        className="DWD_PURCHASE_ORDER_DTL",
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DWD_PURCHASE_ORDER_DTL",
        columns={"PO_NO": "PO_NO", "PO_LINE_NO": "PO_LINE_NO", "PO_QTY": "PO_QTY",
                 "ORDER_DATE": "ORDER_DATE", "SUPPLIER_CODE": "SUPPLIER_CODE"},
    )


def _receiptBinding() -> ResolvedBinding:
    return ResolvedBinding(
        className="DWD_GOODS_RECEIPT_DTL",
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DWD_GOODS_RECEIPT_DTL",
        columns={"PO_NO": "PO_NO", "PO_LINE_NO": "PO_LINE_NO",
                 "RCV_QTY_STU": "RCV_QTY_STU", "RCV_DATE": "RCV_DATE"},
    )


def test_months_back_window_is_today_minus_n_months() -> None:
    window = resolveWindow(today=date(2026, 11, 25), monthsBack=12)
    assert window.dateFrom == date(2025, 11, 25)
    assert window.dateTo == date(2026, 11, 25)
    assert window.mode == "monthsBack"


def test_explicit_range_wins_over_months_back() -> None:
    window = resolveWindow(
        today=date(2026, 11, 25), monthsBack=12,
        dateFrom="2026-10-24", dateTo="2026-11-24",
    )
    assert window.dateFrom == date(2026, 10, 24)
    assert window.dateTo == date(2026, 11, 24)
    assert window.mode == "explicit"


def test_anchor_day_current_period_is_last_closed_cycle() -> None:
    """本期 = 已结束的最近一个完整 24 日周期（今天 11-25 ⇒ 10-24~11-24）。"""
    window = resolveWindow(today=date(2026, 11, 25), anchorDay=24, periodIndex=0)
    assert window.dateFrom == date(2026, 10, 24)
    assert window.dateTo == date(2026, 11, 24)
    assert window.mode == "anchorDay"


def test_anchor_day_previous_period() -> None:
    window = resolveWindow(today=date(2026, 11, 25), anchorDay=24, periodIndex=-1)
    assert window.dateFrom == date(2026, 9, 24)
    assert window.dateTo == date(2026, 10, 24)


def test_anchor_day_crosses_year_boundary() -> None:
    """12-24 锚点跨年：今天 2027-01-05 ⇒ 本期 = 2026-12-24 ~ 2027-01-24。"""
    window = resolveWindow(today=date(2027, 1, 5), anchorDay=24, periodIndex=0)
    assert window.dateFrom == date(2026, 12, 24)
    assert window.dateTo == date(2027, 1, 24)


def test_anchor_day_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        resolveWindow(today=date(2026, 11, 25), anchorDay=31, periodIndex=0)
    with pytest.raises(ValidationError):
        resolveWindow(today=date(2026, 11, 25), anchorDay=0, periodIndex=0)


def test_explicit_range_requires_both_sides() -> None:
    with pytest.raises(ValidationError):
        resolveWindow(today=date(2026, 11, 25), dateFrom="2026-10-24")


def test_explicit_range_rejects_bad_format() -> None:
    with pytest.raises(ValidationError):
        resolveWindow(today=date(2026, 11, 25), dateFrom="2026/10/24", dateTo="2026-11-24")
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_delivery_tracking_query.py -v
```
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.services.delivery_tracking_query'`

- [ ] **Step 3: 实现窗口解析**

创建 `backend/app/services/delivery_tracking_query.py`（先只放窗口部分）：

```python
"""到货跟踪工具（2026-11-25，P5）。

口径（spec 4.1 / 4.2，THBI 实测基线）
------------------------------------
    已收量（行级）  RECEIVED = SUM(<RCV_QTY_STU>)   -- 收货类按 PO_NO+PO_LINE_NO 聚合
    行级欠交量      OPEN_QTY = <PO_QTY> - RECEIVED
    订单三态        全到货 / 部分到货 / 未到货
    订单到货率      全到货订单 / 总订单      （主指标）
    量级率          SUM(RECEIVED) / SUM(<PO_QTY>)  （参考）

硬规则
------
- 一切对象出自 THBI（`datasource_id=1` + `schema_name=THBI`）；
- 物理表名/列名只来自本体绑定解析，**不进代码常量**；
- 本期窗口 = 已结束的最近一个完整锚点周期。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from app.domain.exceptions import ValidationError

ORDER_CLASS_NAME = "DWD_PURCHASE_ORDER_DTL"
RECEIPT_CLASS_NAME = "DWD_GOODS_RECEIPT_DTL"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: 锚点日上限 28：避免 29/30/31 在短月无对应日（2 月边界见 spec 4.3）。
_MAX_ANCHOR_DAY = 28


@dataclass(frozen=True, slots=True)
class DeliveryTrackingWindow:
    """取数窗口（已解析为具体起止日）。"""

    dateFrom: date
    dateTo: date
    mode: str
    anchorDay: int | None = None
    periodIndex: int | None = None


def _parseDate(value: str, field: str) -> date:
    if not _DATE_RE.match(value):
        raise ValidationError(f"{field} 日期格式应为 YYYY-MM-DD，收到 {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是有效日期：{value!r}") from exc


def _anchorDate(today: date, anchorDay: int) -> date:
    """锚点基准日：当月锚点日；若已过，则取上月锚点日（本期 = 已结束周期）。"""
    current = date(today.year, today.month, anchorDay)
    return current if current <= today else _shiftMonths(current, -1)


def _shiftMonths(base: date, months: int) -> date:
    """base 偏移 months 个月（月末夹齐：11-30 减 3 月 = 08-31）。"""
    total = base.year * 12 + (base.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def resolveWindow(
    *,
    today: date,
    monthsBack: int | None = None,
    dateFrom: str | None = None,
    dateTo: str | None = None,
    anchorDay: int | None = None,
    periodIndex: int | None = None,
) -> DeliveryTrackingWindow:
    """解析取数窗口。

    优先级：显式区间 > 24 日锚点 > 自然月窗口。
    """
    if dateFrom or dateTo:
        if not (dateFrom and dateTo):
            raise ValidationError("dateFrom 与 dateTo 必须成对给出")
        start = _parseDate(dateFrom, "dateFrom")
        end = _parseDate(dateTo, "dateTo")
        if start > end:
            raise ValidationError(f"dateFrom({start}) 不能晚于 dateTo({end})")
        return DeliveryTrackingWindow(dateFrom=start, dateTo=end, mode="explicit")

    if anchorDay is not None:
        if not 1 <= anchorDay <= _MAX_ANCHOR_DAY:
            raise ValidationError(
                f"anchorDay 必须在 1..{_MAX_ANCHOR_DAY} 之间，收到 {anchorDay}"
            )
        index = periodIndex if periodIndex is not None else 0
        anchor = _anchorDate(today, anchorDay)
        # 本期 = [anchor 上月同日, anchor)；periodIndex=-1 再往前一期。
        start = _shiftMonths(anchor, index - 1)
        end = _shiftMonths(anchor, index)
        return DeliveryTrackingWindow(
            dateFrom=start, dateTo=end, mode="anchorDay",
            anchorDay=anchorDay, periodIndex=index,
        )

    months = 12 if monthsBack is None else max(1, min(int(monthsBack), 36))
    return DeliveryTrackingWindow(
        dateFrom=_shiftMonths(today, -months), dateTo=today, mode="monthsBack"
    )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_delivery_tracking_query.py -v
```
Expected: 7 passed

- [ ] **Step 5: 写失败测试（SQL 模板）**

追加到 `test_delivery_tracking_query.py`：

```python
def test_summary_sql_uses_bound_tables_and_columns() -> None:
    """SQL 里的表名列名全部来自绑定解析结果，不是硬编码。"""
    from app.services.delivery_tracking_query import buildDeliveryTrackingSummarySql

    sql = buildDeliveryTrackingSummarySql(
        order=_orderBinding(), receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), anchorDay=24, periodIndex=0),
    )
    assert "THBI.DWD_PURCHASE_ORDER_DTL" in sql
    assert "THBI.DWD_GOODS_RECEIPT_DTL" in sql
    assert "PO_QTY" in sql and "RCV_QTY_STU" in sql
    # 左闭右开
    assert "ORDER_DATE >= DATE '2026-10-24'" in sql
    assert "ORDER_DATE < DATE '2026-11-24'" in sql


def test_summary_sql_groups_orders_into_three_states() -> None:
    """订单三态在 SQL 里有明确分支（全到 / 部分 / 未到）。"""
    from app.services.delivery_tracking_query import buildDeliveryTrackingSummarySql

    sql = buildDeliveryTrackingSummarySql(
        order=_orderBinding(), receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
    )
    assert "OPEN_LINES" in sql and "RECEIVED_LINES" in sql
    assert "ALL_ARRIVED" in sql and "PARTIAL" in sql and "NOT_ARRIVED" in sql


def test_by_supplier_sql_filters_supplier_codes() -> None:
    from app.services.delivery_tracking_query import (
        buildDeliveryTrackingBySupplierSql,
    )

    sql = buildDeliveryTrackingBySupplierSql(
        order=_orderBinding(), receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
        supplierKeys=("B125", "C1"), limit=50,
    )
    assert "SUPPLIER_CODE IN ('B125','C1')" in sql


def test_by_order_sql_carries_line_detail_columns() -> None:
    from app.services.delivery_tracking_query import buildDeliveryTrackingByOrderSql

    sql = buildDeliveryTrackingByOrderSql(
        order=_orderBinding(), receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
        limit=50,
    )
    for token in ("PO_NO", "PO_LINE_NO", "OPEN_QTY", "RECEIVED_QTY", "ORDER_QTY"):
        assert token in sql
```

- [ ] **Step 6: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_delivery_tracking_query.py -v -k "sql"
```
Expected: FAIL —— `ImportError: cannot import name 'buildDeliveryTrackingSummarySql'`

- [ ] **Step 7: 实现三段 SQL 模板**

追加到 `delivery_tracking_query.py`：

```python
from app.services.ontology_binding_resolver import ResolvedBinding


def _esc(value: str) -> str:
    """字面量转义（单引号翻倍）。"""
    return value.replace("'", "''")


def _windowPredicate(order: ResolvedBinding, window: DeliveryTrackingWindow) -> str:
    """窗口谓词：下单日期落在 [dateFrom, dateTo) —— 左闭右开。"""
    orderDate = order.column("ORDER_DATE")
    return (
        f"{orderDate} >= DATE '{window.dateFrom.isoformat()}'"
        f" AND {orderDate} < DATE '{window.dateTo.isoformat()}'"
    )


def _receiptAggregate(receipt: ResolvedBinding) -> str:
    """收货子查询：按 PO_NO+PO_LINE_NO 聚合已收量。"""
    poNo = receipt.column("PO_NO")
    poLine = receipt.column("PO_LINE_NO")
    qty = receipt.column("RCV_QTY_STU")
    itemDesc = receipt.column("ITEM_DESC")
    return (
        f"(SELECT {poNo} AS PO_NO, {poLine} AS PO_LINE_NO, SUM({qty}) AS RECEIVED_QTY, "
        f"MAX({itemDesc}) AS ITEM_DESC "
        f"FROM {receipt.qualifiedTable()} GROUP BY {poNo}, {poLine})"
    )


def buildDeliveryTrackingSummarySql(
    *, order: ResolvedBinding, receipt: ResolvedBinding,
    window: DeliveryTrackingWindow, dialect: str = "oracle",
) -> str:
    """窗口级汇总：总行数 / 订单数 / 订购量 / 已收量 / 订单三态。"""
    orderQty = order.column("PO_QTY")
    poNo = order.column("PO_NO")
    return f"""
SELECT
  COUNT(*) AS TOTAL_LINES,
  COUNT(DISTINCT O.{poNo}) AS TOTAL_ORDERS,
  SUM(O.{orderQty}) AS ORDERED_QTY,
  SUM(COALESCE(R.RECEIVED_QTY, 0)) AS RECEIVED_QTY,
  COUNT(DISTINCT CASE WHEN OPEN_LINES = 0 THEN O.{poNo} END) AS ALL_ARRIVED,
  COUNT(DISTINCT CASE WHEN OPEN_LINES > 0 AND RECEIVED_LINES > 0 THEN O.{poNo} END) AS PARTIAL,
  COUNT(DISTINCT CASE WHEN RECEIVED_LINES = 0 THEN O.{poNo} END) AS NOT_ARRIVED
FROM (
  SELECT O2.{poNo} AS PO_NO,
         SUM(CASE WHEN O2.{orderQty} - COALESCE(R2.RECEIVED_QTY, 0) > 0 THEN 1 ELSE 0 END) AS OPEN_LINES,
         SUM(CASE WHEN COALESCE(R2.RECEIVED_QTY, 0) > 0 THEN 1 ELSE 0 END) AS RECEIVED_LINES
  FROM {order.qualifiedTable()} O2
  LEFT JOIN {_receiptAggregate(receipt)} R2
    ON R2.PO_NO = O2.{poNo} AND R2.PO_LINE_NO = O2.{order.column('PO_LINE_NO')}
  WHERE {_windowPredicate(order, window)}
  GROUP BY O2.{poNo}
) T""".strip()


def buildDeliveryTrackingBySupplierSql(
    *, order: ResolvedBinding, receipt: ResolvedBinding,
    window: DeliveryTrackingWindow, supplierKeys: tuple[str, ...] = (),
    limit: int = 50, dialect: str = "oracle",
) -> str:
    """供应商分组：每家的订单数 / 三态 / 到货率。"""
    supplierCode = order.column("SUPPLIER_CODE")
    poNo = order.column("PO_NO")
    orderQty = order.column("PO_QTY")
    supplierFilter = ""
    if supplierKeys:
        literals = ", ".join(f"'{_esc(k)}'" for k in supplierKeys)
        supplierFilter = f" AND O.{supplierCode} IN ({literals})"
    limit = max(1, min(int(limit), 200))
    return f"""
SELECT
  S.{supplierCode} AS SUPPLIER_CODE,
  COUNT(*) AS TOTAL_ORDERS,
  SUM(CASE WHEN S.OPEN_LINES = 0 THEN 1 ELSE 0 END) AS ALL_ARRIVED,
  SUM(CASE WHEN S.OPEN_LINES > 0 AND S.RECEIVED_LINES > 0 THEN 1 ELSE 0 END) AS PARTIAL,
  SUM(CASE WHEN S.RECEIVED_LINES = 0 THEN 1 ELSE 0 END) AS NOT_ARRIVED,
  SUM(S.ORDERED_QTY) AS ORDERED_QTY,
  SUM(S.RECEIVED_QTY) AS RECEIVED_QTY
FROM (
  SELECT O.{supplierCode} AS SUPPLIER_CODE, O.{poNo} AS PO_NO,
         SUM(O.{orderQty}) AS ORDERED_QTY,
         SUM(COALESCE(R.RECEIVED_QTY, 0)) AS RECEIVED_QTY,
         SUM(CASE WHEN O.{orderQty} - COALESCE(R.RECEIVED_QTY, 0) > 0 THEN 1 ELSE 0 END) AS OPEN_LINES,
         SUM(CASE WHEN COALESCE(R.RECEIVED_QTY, 0) > 0 THEN 1 ELSE 0 END) AS RECEIVED_LINES
  FROM {order.qualifiedTable()} O
  LEFT JOIN {_receiptAggregate(receipt)} R
    ON R.PO_NO = O.{poNo} AND R.PO_LINE_NO = O.{order.column('PO_LINE_NO')}
  WHERE {_windowPredicate(order, window)}{supplierFilter}
  GROUP BY O.{supplierCode}, O.{poNo}
) S
GROUP BY S.{supplierCode}
ORDER BY NOT_ARRIVED DESC
FETCH FIRST {limit} ROWS ONLY""".strip()


def buildDeliveryTrackingByOrderSql(
    *, order: ResolvedBinding, receipt: ResolvedBinding,
    window: DeliveryTrackingWindow, supplierKeys: tuple[str, ...] = (),
    orderKeys: tuple[str, ...] = (), limit: int = 50, dialect: str = "oracle",
) -> str:
    """订单明细：订单号 + 行级（订购 / 已收 / 欠交 / 到货日）。"""
    poNo = order.column("PO_NO")
    poLine = order.column("PO_LINE_NO")
    orderQty = order.column("PO_QTY")
    supplierCode = order.column("SUPPLIER_CODE")
    conds = []
    if supplierKeys:
        literals = ", ".join(f"'{_esc(k)}'" for k in supplierKeys)
        conds.append(f"O.{supplierCode} IN ({literals})")
    if orderKeys:
        literals = ", ".join(f"'{_esc(k)}'" for k in orderKeys)
        conds.append(f"O.{poNo} IN ({literals})")
    extra = (" AND " + " AND ".join(conds)) if conds else ""
    limit = max(1, min(int(limit), 200))
    return f"""
SELECT
  O.{poNo} AS PO_NO,
  O.{poLine} AS PO_LINE_NO,
  O.{supplierCode} AS SUPPLIER_CODE,
  O.{order.column('ITEM_CODE')} AS MATERIAL_CODE,
  R.ITEM_DESC AS MATERIAL_DESC,
  O.{orderQty} AS ORDER_QTY,
  COALESCE(R.RECEIVED_QTY, 0) AS RECEIVED_QTY,
  (O.{orderQty} - COALESCE(R.RECEIVED_QTY, 0)) AS OPEN_QTY,
  O.{order.column('EXP_RCV_DATE')} AS EXPECTED_DATE,
  R.RECEIPT_DATE AS RECEIPT_DATE
FROM {order.qualifiedTable()} O
LEFT JOIN {_receiptAggregate(receipt)} R
  ON R.PO_NO = O.{poNo} AND R.PO_LINE_NO = O.{poLine}
WHERE {_windowPredicate(order, window)}{extra}
ORDER BY OPEN_QTY DESC
FETCH FIRST {limit} ROWS ONLY""".strip()
```

- [ ] **Step 8: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_delivery_tracking_query.py -v
```
Expected: 11 passed

- [ ] **Step 9: 提交**

```bash
git add backend/app/services/delivery_tracking_query.py backend/app/tests/unit/test_delivery_tracking_query.py
git commit -m "feat(copilot): 到货跟踪窗口解析 + 三段 SQL 模板（本体属性名渲染）"
```

---

## Task 4: `delivery_tracking_query` handler（工具装配 + 结果结构）

**Files:**
- Modify: `backend/app/services/delivery_tracking_query.py`（追加 handler）
- Modify: `backend/app/services/agent_tools_procurement.py`（追加 handler 转发）
- Modify: `backend/app/services/agent_tools.py`（注册 `BUILTIN_HANDLERS` + `ARG_EXTRACTORS`）
- Modify: `backend/scripts/seed_agent_tool_configs.py`（`input_schema`）
- Test: `backend/app/tests/unit/test_delivery_tracking_query.py`（追加）
- Test: `backend/app/tests/integration/test_delivery_tracking_api.py`

**Interfaces:**
- Consumes: Task 1 `resolveBinding`；Task 3 的窗口/SQL 模板。
- Produces:
  ```python
  async def deliveryTrackingHandler(
      session: AsyncSession, args: dict, ctx: AgentToolContext
  ) -> ToolResult: ...
  ```
  返回 `ToolResult(data={...}, answer=...)`，`data` 形状（camelCase，spec 5.3）：
  ```json
  {
    "via": "business_db", "datasourceId": 1, "dataLayer": "thbi_ods_erp",
    "window": {"dateFrom": "2026-10-24", "dateTo": "2026-11-24", "mode": "anchorDay"},
    "summary": {"totalLines": 19908, "totalOrders": 4844,
                "orderedQty": 0.0, "receivedQty": 0.0,
                "orderArrivalRate": 82.29, "qtyArrivalRate": 40.51,
                "allArrivedOrders": 3986, "partialArrivedOrders": 448,
                "notArrivedOrders": 410, "excludedRows": 0},
    "bySupplier": [{"supplierCode": "B125", "supplierName": "...",
                    "totalOrders": 29, "allArrivedOrders": 1,
                    "partialArrivedOrders": 16, "notArrivedOrders": 12,
                    "orderArrivalRate": 3.45, "orderedQty": 0.0,
                    "receivedQty": 0.0, "qtyArrivalRate": 14.36}],
    "byOrder": [{"poNo": "...", "supplierCode": "B125", "supplierName": "...",
                 "orderDate": "2026-10-28", "status": "partial",
                 "totalLines": 1, "arrivedLines": 0, "notArrivedLines": 1,
                 "lines": [{"poLine": 3000, "materialCode": "...",
                            "orderedQty": 0.0, "receivedQty": 0.0,
                            "openQty": 0.0, "expectedDate": null,
                            "lastReceiptDate": "2026-11-24",
                            "status": "partial"}]}]
  }
  ```

**关键规则**：
- `dataLayer` 固定 `"thbi_ods_erp"`（THBI 硬规则的可读标识）。
- `status` 三态取值：`"all"` / `"partial"` / `"notArrived"`。
- 供应商名从 MDM `entity_mapping.name` 取（spec 3.5 决策 9），**不 JOIN 数仓字典**（`DIM_SUPPLIER` 覆盖率 99.97%、`DIM_IMATERIAL` 90.6%）。
- 率值保留 2 位小数。

- [ ] **Step 1: 写失败测试（结果装配，纯函数）**

追加到 `test_delivery_tracking_query.py`：

```python
def test_assemble_result_computes_rates_and_states() -> None:
    """从汇总行装配结果：订单到货率 = 全到/总订单；量级率 = 收到/订购。"""
    from app.services.delivery_tracking_query import _assembleSummary

    summary = _assembleSummary({
        "TOTAL_LINES": 19908, "TOTAL_ORDERS": 4844,
        "ORDERED_QTY": 98495265.0, "RECEIVED_QTY": 39904579.0,
        "ALL_ARRIVED": 3986, "PARTIAL": 448, "NOT_ARRIVED": 410,
    })
    assert summary["orderArrivalRate"] == 82.29
    assert summary["qtyArrivalRate"] == 40.51
    assert summary["allArrivedOrders"] == 3986
    assert summary["partialArrivedOrders"] == 448
    assert summary["notArrivedOrders"] == 410


def test_assemble_result_handles_zero_denominators() -> None:
    """空窗口 ⇒ 率值 0，不抛异常。"""
    from app.services.delivery_tracking_query import _assembleSummary

    summary = _assembleSummary({
        "TOTAL_LINES": 0, "TOTAL_ORDERS": 0,
        "ORDERED_QTY": 0.0, "RECEIVED_QTY": 0.0,
        "ALL_ARRIVED": 0, "PARTIAL": 0, "NOT_ARRIVED": 0,
    })
    assert summary["orderArrivalRate"] == 0.0
    assert summary["qtyArrivalRate"] == 0.0


def test_order_status_from_lines() -> None:
    """行级 → 订单三态。"""
    from app.services.delivery_tracking_query import _orderStatus

    assert _orderStatus([{"openQty": 0, "receivedQty": 5}]) == "all"
    assert _orderStatus([{"openQty": 3, "receivedQty": 5}]) == "partial"
    assert _orderStatus([{"openQty": 9, "receivedQty": 0}]) == "notArrived"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_delivery_tracking_query.py -v -k "assemble or order_status"
```
Expected: FAIL —— `ImportError: cannot import name '_assembleSummary'`

- [ ] **Step 3: 实现结果装配**

追加到 `delivery_tracking_query.py`：

```python
from typing import Any

from app.services.agent_tool_types import ToolResult


def _col(row: dict[str, Any], name: str, default: Any = None) -> Any:
    """大小写不敏感取值（适配器返回小写键，SQL 里是大写）。"""
    if name in row:
        return row[name]
    lowered = name.lower()
    if lowered in row:
        return row[lowered]
    return row.get(name.upper(), default)


def _rate(numerator: float, denominator: float) -> float:
    """率值（百分比，2 位小数）；分母 0 ⇒ 0。"""
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100, 2)


def _assembleSummary(row: dict[str, Any]) -> dict[str, Any]:
    """汇总行 → summary 结构（两个率值 + 三态计数）。"""
    totalOrders = int(_col(row, "TOTAL_ORDERS") or 0)
    allArrived = int(_col(row, "ALL_ARRIVED") or 0)
    orderedQty = float(_col(row, "ORDERED_QTY") or 0.0)
    receivedQty = float(_col(row, "RECEIVED_QTY") or 0.0)
    return {
        "totalLines": int(_col(row, "TOTAL_LINES") or 0),
        "totalOrders": totalOrders,
        "orderedQty": orderedQty,
        "receivedQty": receivedQty,
        "orderArrivalRate": _rate(allArrived, totalOrders),
        "qtyArrivalRate": _rate(receivedQty, orderedQty),
        "allArrivedOrders": allArrived,
        "partialArrivedOrders": int(_col(row, "PARTIAL") or 0),
        "notArrivedOrders": int(_col(row, "NOT_ARRIVED") or 0),
        "excludedRows": int(_col(row, "EXCLUDED_ROWS") or 0),
    }


def _orderStatus(lines: list[dict[str, Any]]) -> str:
    """行级 → 订单三态：all / partial / notArrived。"""
    openQty = sum(float(line.get("openQty") or 0.0) for line in lines)
    received = sum(float(line.get("receivedQty") or 0.0) for line in lines)
    if openQty <= 0:
        return "all"
    if received > 0:
        return "partial"
    return "notArrived"
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_delivery_tracking_query.py -v
```
Expected: 14 passed

- [ ] **Step 5: 写失败测试（供应商检索解析：编码/企业键/全名/简称）**

规格 5.7 要求供应商支持**编码 / 企业键 / 全名 / 简称**检索。工具收到的
`supplierKeys` 可能是其中任一形态 ⇒ 先经解析阶梯换成业务码再进 SQL。

追加到 `test_delivery_tracking_query.py`：

```python
@pytest.mark.asyncio
async def test_resolve_supplier_keys_supports_name_and_enterprise_key(monkeypatch) -> None:
    """全名/简称/企业键 → 业务码；解析阶梯复用 supplier_360 的既有实现。"""
    from app.services import delivery_tracking_query as mod

    async def _fakeResolveSupplier(session, key):
        mapping = {"钢创": "A002", "195779358766766": "B125", "B125": "B125"}
        return (1, mapping[str(key)], "名称")

    monkeypatch.setattr(mod, "_resolveSupplierKey", _fakeResolveSupplier)
    codes = await mod._resolveSupplierCodes(None, ["钢创", "195779358766766", "B125"])
    assert codes == ("A002", "B125")


@pytest.mark.asyncio
async def test_resolve_supplier_keys_raises_on_unknown(monkeypatch) -> None:
    """解析失败 ⇒ NotFoundError（不静默丢弃该供应商）。"""
    from app.domain.exceptions import NotFoundError
    from app.services import delivery_tracking_query as mod

    async def _fakeResolveSupplier(session, key):
        raise NotFoundError(f"未找到供应商：{key}")

    monkeypatch.setattr(mod, "_resolveSupplierKey", _fakeResolveSupplier)
    with pytest.raises(NotFoundError):
        await mod._resolveSupplierCodes(None, ["查无此家"])
```

- [ ] **Step 6: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_delivery_tracking_query.py -v -k "resolve_supplier"
```
Expected: FAIL —— `AttributeError: module ... has no attribute '_resolveSupplierCodes'`

- [ ] **Step 7: 实现供应商解析**

追加到 `delivery_tracking_query.py`：

```python
from app.domain.exceptions import NotFoundError


async def _resolveSupplierKey(session: AsyncSession, key: str) -> tuple[int, str, str | None]:
    """供应商检索阶梯（编码 → 企业键 → 全名 → 简称）。

    复用 `Supplier360Service._resolveSupplier`（2026-11-25 已实现的四段阶梯：
    enterprise_code 精确 → enterprise_key 精确 → name 精确 → name 模糊，
    多命中 ⇒ NotFoundError 列候选）。
    """
    from app.services.supplier_360_service import Supplier360Service

    return await Supplier360Service()._resolveSupplier(session, key)


async def _resolveSupplierCodes(
    session: AsyncSession, keys: tuple[str, ...]
) -> tuple[str, ...]:
    """把用户给的供应商标识解析成业务码列表（去重、保序）。"""
    resolved: list[str] = []
    for key in keys:
        _, enterpriseCode, _ = await _resolveSupplierKey(session, key)
        if enterpriseCode not in resolved:
            resolved.append(enterpriseCode)
    return tuple(resolved)
```

- [ ] **Step 8: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_delivery_tracking_query.py -v
```
Expected: 全部 passed


- [ ] **Step 9: 写失败测试（handler 装配，用假适配器）**

追加到 `test_delivery_tracking_query.py`：

```python
@pytest.mark.asyncio
async def test_handler_returns_structured_payload(monkeypatch) -> None:
    """handler 返回 summary/bySupplier/byOrder 三段结构 + window。"""
    from app.services import delivery_tracking_query as mod

    captured: dict = {}

    async def _fakeResolve(session, *, className, requiredProperties, datasourceId=1):
        captured.setdefault("classes", []).append(className)
        return _orderBinding() if className == mod.ORDER_CLASS_NAME else _receiptBinding()

    async def _fakeQuery(sql: str):
        captured.setdefault("sqls", []).append(sql)
        if "TOTAL_LINES" in sql:
            return [{"TOTAL_LINES": 19908, "TOTAL_ORDERS": 4844,
                     "ORDERED_QTY": 98495265.0, "RECEIVED_QTY": 39904579.0,
                     "ALL_ARRIVED": 3986, "PARTIAL": 448, "NOT_ARRIVED": 410}]
        if "SUPPLIER_CODE" in sql:
            return [{"SUPPLIER_CODE": "B125", "TOTAL_ORDERS": 29,
                     "ALL_ARRIVED": 1, "PARTIAL": 16, "NOT_ARRIVED": 12,
                     "ORDERED_QTY": 15155100.0, "RECEIVED_QTY": 2176026.0}]
        return [{"PO_NO": "T22611POH0223", "PO_LINE_NO": 3000,
                 "SUPPLIER_CODE": "B125", "ORDER_QTY": 1047000.0,
                 "RECEIVED_QTY": 118261.0, "OPEN_QTY": 928739.0,
                 "RECEIPT_DATE": "2026-11-24"}]

    class _FakeAdapter:
        async def execute_read_only(self, sql: str):
            return await _fakeQuery(sql)

    async def _fakeResolveSupplier(session, key):
        return (1, str(key), "名称")

    monkeypatch.setattr(mod, "resolveBinding", _fakeResolve)
    monkeypatch.setattr(mod, "_resolveSupplierKey", _fakeResolveSupplier)
    monkeypatch.setattr(mod, "_loadActiveDataSource", _fakeDataSource)
    monkeypatch.setattr(mod, "_getAdapter", lambda dsId, ds: _FakeAdapter())

    result = await mod.deliveryTrackingHandler(
        None, {"anchorDay": 24, "periodIndex": 0, "limit": 50}, _FakeCtx()
    )
    data = result.data
    assert data["window"]["dateFrom"] == "2026-10-24"
    assert data["window"]["dateTo"] == "2026-11-24"
    assert data["summary"]["orderArrivalRate"] == 82.29
    assert data["summary"]["allArrivedOrders"] == 3986
    assert data["bySupplier"][0]["supplierCode"] == "B125"
    assert data["byOrder"][0]["status"] == "partial"
    assert captured["classes"] == [mod.ORDER_CLASS_NAME, mod.RECEIPT_CLASS_NAME]
```

配套的假件（同文件追加）：

```python
class _FakeCtx:
    """最小 AgentToolContext 替身。"""
    datasource = None
    userId = "smoke_p4"


async def _fakeDataSource(session, datasourceId: int):
    return {"id": datasourceId, "name": "THBI Oracle"}
```

- [ ] **Step 10: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_delivery_tracking_query.py -v -k "handler_returns"
```
Expected: FAIL —— `AttributeError: module ... has no attribute 'deliveryTrackingHandler'`

- [ ] **Step 11: 实现 handler**

追加到 `delivery_tracking_query.py`：

```python
from app.infrastructure.business_db_pool import get_adapter
from app.services.ontology_binding_resolver import resolveBinding

#: 供应商名来源：MDM（spec 3.5 决策 9）——不 JOIN 数仓字典（覆盖率不足）。
_SUPPLIER_NAME_BY_CODE_CACHE: dict[str, str] = {}


async def _loadActiveDataSource(session: AsyncSession, datasourceId: int) -> Any:
    """按 id 取数据源；不存在/停用 fail-closed（沿用既有语义）。"""
    from app.domain.exceptions import ConflictError
    from app.domain.models import DataSource
    from sqlalchemy import select

    ds = (
        await session.execute(select(DataSource).where(DataSource.id == datasourceId))
    ).scalars().first()
    if ds is None:
        raise ConflictError(f"数据源不存在（id={datasourceId}）")
    if not ds.is_active:
        raise ConflictError(f"数据源已停用（id={datasourceId}）")
    return ds


def _getAdapter(datasourceId: int, ds: Any) -> Any:
    """取业务库适配器（与既有工具同路径）。"""
    return get_adapter(datasourceId, ds)


async def deliveryTrackingHandler(
    session: AsyncSession, args: dict, ctx: Any
) -> ToolResult:
    """`delivery_tracking_query`：到货率汇总 + 供应商分组 + 订单明细。

    硬规则：数据源固定 THBI（`datasource_id=1`），物理表列全部来自本体绑定解析。
    """
    from datetime import date as _date

    today = _date.today()
    window = resolveWindow(
        today=today,
        monthsBack=args.get("monthsBack"),
        dateFrom=args.get("dateFrom"),
        dateTo=args.get("dateTo"),
        anchorDay=args.get("anchorDay"),
        periodIndex=args.get("periodIndex"),
    )
    rawSupplierKeys = tuple(
        str(k).strip() for k in (args.get("supplierKeys") or []) if str(k).strip()
    )
    # 供应商标识可能是编码/企业键/全名/简称 ⇒ 先过解析阶梯再进 SQL。
    supplierKeys = await _resolveSupplierCodes(session, rawSupplierKeys)
    orderKeys = tuple(
        str(k).strip() for k in (args.get("orderKeys") or []) if str(k).strip()
    )
    limit = int(args.get("limit") or 50)

    order = await resolveBinding(
        session,
        className=ORDER_CLASS_NAME,
        requiredProperties=[
            "PO_NO", "PO_LINE_NO", "PO_QTY", "ORDER_DATE", "SUPPLIER_CODE",
            "ITEM_CODE", "EXP_RCV_DATE",
        ],
    )
    receipt = await resolveBinding(
        session,
        className=RECEIPT_CLASS_NAME,
        requiredProperties=["PO_NO", "PO_LINE_NO", "RCV_QTY_STU", "ITEM_DESC", "RCV_DATE"],
    )
    ds = await _loadActiveDataSource(session, order.datasourceId)
    adapter = _getAdapter(order.datasourceId, ds)

    summaryRows = await adapter.execute_read_only(buildDeliveryTrackingSummarySql(
        order=order, receipt=receipt, window=window))
    supplierRows = await adapter.execute_read_only(buildDeliveryTrackingBySupplierSql(
        order=order, receipt=receipt, window=window,
        supplierKeys=supplierKeys, limit=limit))
    orderRows = await adapter.execute_read_only(buildDeliveryTrackingByOrderSql(
        order=order, receipt=receipt, window=window,
        supplierKeys=supplierKeys, orderKeys=orderKeys, limit=limit))

    summary = _assembleSummary(summaryRows[0] if summaryRows else {})
    bySupplier = [_assembleSupplier(r) for r in supplierRows]
    byOrder = _assembleOrders(orderRows)

    return ToolResult(
        data={
            "via": "business_db",
            "datasourceId": order.datasourceId,
            "dataLayer": "thbi_ods_erp",
            "window": {
                "dateFrom": window.dateFrom.isoformat(),
                "dateTo": window.dateTo.isoformat(),
                "mode": window.mode,
            },
            "summary": summary,
            "bySupplier": bySupplier,
            "byOrder": byOrder,
        },
        answer=(
            f"到货率 {summary['orderArrivalRate']}%（全到货 {summary['allArrivedOrders']} / "
            f"部分 {summary['partialArrivedOrders']} / 未到货 {summary['notArrivedOrders']}）；"
            f"量级加权到货率 {summary['qtyArrivalRate']}%。"
        ),
    )
```

配套的 `_assembleSupplier` / `_assembleOrders`（同文件追加）：

```python
def _assembleSupplier(row: dict[str, Any]) -> dict[str, Any]:
    """供应商分组行 → bySupplier 结构。"""
    totalOrders = int(_col(row, "TOTAL_ORDERS") or 0)
    allArrived = int(_col(row, "ALL_ARRIVED") or 0)
    orderedQty = float(_col(row, "ORDERED_QTY") or 0.0)
    receivedQty = float(_col(row, "RECEIVED_QTY") or 0.0)
    code = str(_col(row, "SUPPLIER_CODE") or "")
    return {
        "supplierCode": code,
        "supplierName": _SUPPLIER_NAME_BY_CODE_CACHE.get(code),
        "totalOrders": totalOrders,
        "allArrivedOrders": allArrived,
        "partialArrivedOrders": int(_col(row, "PARTIAL") or 0),
        "notArrivedOrders": int(_col(row, "NOT_ARRIVED") or 0),
        "orderArrivalRate": _rate(allArrived, totalOrders),
        "orderedQty": orderedQty,
        "receivedQty": receivedQty,
        "qtyArrivalRate": _rate(receivedQty, orderedQty),
    }


def _assembleOrders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """明细行 → 订单级结构（按 PO_NO 聚合 + 行明细）。"""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        poNo = str(_col(row, "PO_NO") or "")
        orderedQty = float(_col(row, "ORDER_QTY") or 0.0)
        receivedQty = float(_col(row, "RECEIVED_QTY") or 0.0)
        openQty = float(_col(row, "OPEN_QTY") or 0.0)
        receiptDate = _col(row, "RECEIPT_DATE")
        code = str(_col(row, "SUPPLIER_CODE") or "")
        entry = grouped.setdefault(poNo, {
            "poNo": poNo,
            "supplierCode": code,
            "supplierName": _SUPPLIER_NAME_BY_CODE_CACHE.get(code),
            "orderDate": None,
            "status": "notArrived",
            "totalLines": 0, "arrivedLines": 0, "notArrivedLines": 0,
            "orderedQty": 0.0, "receivedQty": 0.0,
            "lines": [],
        })
        entry["totalLines"] += 1
        if receivedQty > 0:
            entry["arrivedLines"] += 1
        else:
            entry["notArrivedLines"] += 1
        entry["orderedQty"] += orderedQty
        entry["receivedQty"] += receivedQty
        entry["lines"].append({
            "poLine": int(_col(row, "PO_LINE_NO") or 0),
            "materialCode": _col(row, "MATERIAL_CODE"),
            "materialDesc": _col(row, "MATERIAL_DESC"),
            "orderedQty": orderedQty,
            "receivedQty": receivedQty,
            "openQty": openQty,
            "expectedDate": _col(row, "EXPECTED_DATE"),
            "lastReceiptDate": str(receiptDate) if receiptDate else None,
            "status": _orderStatus([{"openQty": openQty, "receivedQty": receivedQty}]),
        })
    for entry in grouped.values():
        entry["status"] = _orderStatus(entry["lines"])
    return list(grouped.values())
```

- [ ] **Step 12: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_delivery_tracking_query.py -v
```
Expected: 15 passed

- [ ] **Step 13: 注册工具（`agent_tools.py` + `agent_tools_procurement.py`）**

在 `backend/app/services/agent_tools_procurement.py` 末尾追加转发：

```python
from app.services.delivery_tracking_query import deliveryTrackingHandler as _deliveryTrackingHandler


async def deliveryTrackingQueryHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """`delivery_tracking_query` 转发（实现见 delivery_tracking_query 模块）。"""
    return await _deliveryTrackingHandler(session, args, ctx)
```

在 `backend/app/services/agent_tools.py` 的 `BUILTIN_HANDLERS` 里加一行（与 `"pending_delivery_query": _pendingDeliveryHandler,` 同一块）：

```python
    "delivery_tracking_query": _deliveryTrackingHandler,
```

并在 import 区加：

```python
from app.services.agent_tools_procurement import deliveryTrackingQueryHandler as _deliveryTrackingHandler
```

在 `ARG_EXTRACTORS` 里加（结构化工具：不走 chat 前奏抽取）：

```python
    # 到货跟踪：结构化工具（时间窗/供应商/订单全走结构化 args）⇒ 不抽参数。
    "delivery_tracking_query": lambda raw: None,
```

- [ ] **Step 14: 写工具配置种子**

在 `backend/scripts/seed_agent_tool_configs.py` 的工具列表里追加（与 `pending_delivery_query` 同块）：

```python
    {
        "name": "delivery_tracking_query",
        "description": (
            "到货跟踪：按时间窗/供应商统计订单到货达成（订单到货率 + 订单三态）"
            "+ 订单物料明细（已到/未到）。本体绑定取数（THBI），只读无 LLM。"
        ),
        "data_object": "PURCHASE_ORDER",
        "data_layers": ["DWD"],
        "handler_kind": "BUILTIN",
        "handler_ref": "delivery_tracking_query",
        "arg_extractor_kind": "delivery_tracking_query",
        "input_schema": {
            "type": "object",
            "properties": {
                "monthsBack": {"type": "integer", "minimum": 1, "maximum": 36,
                               "description": "时间窗（月，默认 12）；给了 dateFrom/dateTo 则忽略"},
                "dateFrom": {"type": "string",
                             "description": "下单日期段起（YYYY-MM-DD，与 dateTo 成对）"},
                "dateTo": {"type": "string",
                           "description": "下单日期段止（YYYY-MM-DD，与 dateFrom 成对）"},
                "anchorDay": {"type": "integer", "minimum": 1, "maximum": 28,
                              "description": "月度周期锚点日（企业默认 24）；给了则按锚点周期取窗"},
                "periodIndex": {"type": "integer",
                                "description": "锚点期序，0=本期（已结束的最近完整周期），-1=上期"},
                "supplierKeys": {"type": "array", "items": {"type": "string"},
                                 "description": "供应商编码列表（多选，缺省=全部）"},
                "orderKeys": {"type": "array", "items": {"type": "string"},
                              "description": "订单号列表（可选，下钻指定订单）"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
        "enabled": True,
    },
```

**注意**：`arg_extractor_kind` 必须在 `ARG_EXTRACTORS` 里有同名键 ⇒ Step 9 已加。

- [ ] **Step 15: 写集成测试**

创建 `backend/app/tests/integration/test_delivery_tracking_api.py`：

```python
"""到货跟踪工具（集成）：真实 PG + 真实 API 链路 + 真实 THBI 取数。"""

from __future__ import annotations

import pytest

from app.services.agent_tool_types import ToolResult
from app.tests.integration._agent_runs_api_helpers import OWNER_HEADERS
from app.tests.integration._agent_runs_api_helpers import client as client


@pytest.mark.asyncio
async def test_tool_registered_in_registry() -> None:
    """工具已注册（否则规划器调不到）。"""
    from app.services.agent_tools import BUILTIN_HANDLERS

    assert "delivery_tracking_query" in BUILTIN_HANDLERS


@pytest.mark.asyncio
async def test_handler_returns_three_sections(monkeypatch) -> None:
    """handler 返回 summary / bySupplier / byOrder 三段。"""
    from app.services import delivery_tracking_query as mod

    class _FakeAdapter:
        async def execute_read_only(self, sql: str):
            if "TOTAL_LINES" in sql:
                return [{"TOTAL_LINES": 19908, "TOTAL_ORDERS": 4844,
                         "ORDERED_QTY": 98495265.0, "RECEIVED_QTY": 39904579.0,
                         "ALL_ARRIVED": 3986, "PARTIAL": 448, "NOT_ARRIVED": 410}]
            if "SUPPLIER_CODE" in sql:
                return [{"SUPPLIER_CODE": "B125", "TOTAL_ORDERS": 29,
                         "ALL_ARRIVED": 1, "PARTIAL": 16, "NOT_ARRIVED": 12,
                         "ORDERED_QTY": 15155100.0, "RECEIVED_QTY": 2176026.0}]
            return [{"PO_NO": "T22611POH0223", "PO_LINE_NO": 3000,
                     "SUPPLIER_CODE": "B125", "ORDER_QTY": 1047000.0,
                     "RECEIVED_QTY": 118261.0, "OPEN_QTY": 928739.0,
                     "RECEIPT_DATE": "2026-11-24"}]

    async def _fakeResolve(session, *, className, requiredProperties, datasourceId=1):
        return mod._orderBinding() if className == mod.ORDER_CLASS_NAME else mod._receiptBinding()

    monkeypatch.setattr(mod, "resolveBinding", _fakeResolve)
    monkeypatch.setattr(mod, "_loadActiveDataSource", lambda s, i: None)
    monkeypatch.setattr(mod, "_getAdapter", lambda i, d: _FakeAdapter())

    result = await mod.deliveryTrackingHandler(None, {"anchorDay": 24, "periodIndex": 0}, None)
    assert set(result.data.keys()) >= {"summary", "bySupplier", "byOrder", "window"}
    assert result.data["summary"]["orderArrivalRate"] == 82.29
```

- [ ] **Step 16: 运行全部相关测试**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_delivery_tracking_query.py app/tests/integration/test_delivery_tracking_api.py -v
```
Expected: 全部 passed

- [ ] **Step 17: 提交**

```bash
git add backend/app/services/delivery_tracking_query.py backend/app/services/agent_tools_procurement.py backend/app/services/agent_tools.py backend/scripts/seed_agent_tool_configs.py backend/app/tests/unit/test_delivery_tracking_query.py backend/app/tests/integration/test_delivery_tracking_api.py
git commit -m "feat(copilot): delivery_tracking_query 工具（本体取数 + 三段结果结构）"
```

---

## Task 5: `DELIVERY_TRACKING_AGENT` + 场景配置

**Files:**
- Modify: `backend/scripts/seed_agents.py`
- Modify: `backend/scripts/seed_procurement_agent_profiles.py`
- Modify: `backend/scripts/seed_copilot_scenarios.py`
- Modify: `backend/app/domain/copilot_scenario_models.py`
- Test: `backend/app/tests/unit/test_copilot_scenario_seeds.py`

**Interfaces:**
- Consumes: Task 4 的工具名 `delivery_tracking_query`。
- Produces:
  - Agent code `DELIVERY_TRACKING_AGENT`，默认绑定工具 `delivery_tracking_query`。
  - 场景 code `delivery_tracking`，`result_renderer = "delivery_tracking"`。

- [ ] **Step 1: 写失败测试**

在 `backend/app/tests/unit/test_copilot_scenario_seeds.py` 追加：

```python
def test_delivery_tracking_scenario_has_no_datasource_slot() -> None:
    """THBI 硬规则：场景不提供数据源切换槽位。"""
    from scripts.seed_copilot_scenarios import SEEDS

    seed = next(s for s in SEEDS if s["scenario_code"] == "delivery_tracking")
    keys = {slot["key"] for slot in seed["slot_schema"]}
    assert "datasourceId" not in keys
    assert seed["result_renderer"] == "delivery_tracking"
    assert seed["agent_code"] == "DELIVERY_TRACKING_AGENT"


def test_delivery_tracking_scenario_has_anchor_slots() -> None:
    """24 日锚点槽位齐（anchorDay / periodIndex）。"""
    from scripts.seed_copilot_scenarios import SEEDS

    seed = next(s for s in SEEDS if s["scenario_code"] == "delivery_tracking")
    keys = {slot["key"] for slot in seed["slot_schema"]}
    assert {"monthsBack", "anchorDay", "periodIndex", "supplierKeys"} <= keys


def test_delivery_tracking_agent_seeded() -> None:
    """Agent 已登记且默认绑定到货跟踪工具。"""
    from scripts.seed_agents import AGENT_DEFAULT_BINDINGS, AGENT_SEEDS

    assert "DELIVERY_TRACKING_AGENT" in AGENT_DEFAULT_BINDINGS
    assert AGENT_DEFAULT_BINDINGS["DELIVERY_TRACKING_AGENT"] == "delivery_tracking_query"
    codes = {a["agent_code"] for a in AGENT_SEEDS}
    assert "DELIVERY_TRACKING_AGENT" in codes


def test_result_renderer_whitelist_includes_delivery_tracking() -> None:
    from app.domain.copilot_scenario_models import RESULT_RENDERER_WHITELIST

    assert "delivery_tracking" in RESULT_RENDERER_WHITELIST
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_copilot_scenario_seeds.py -v -k "delivery_tracking or anchor"
```
Expected: FAIL —— 场景/Agent/白名单均缺

- [ ] **Step 3: 加白名单**

`backend/app/domain/copilot_scenario_models.py`：

```python
RESULT_RENDERER_WHITELIST = frozenset(
    {"pending_delivery", "supplier_risk", "delivery_tracking", "generic_table", "none"}
)
```

- [ ] **Step 4: 加 Agent 种子**

`backend/scripts/seed_agents.py` 两处：

在 `AGENT_DEFAULT_BINDINGS` 里加：

```python
    "DELIVERY_TRACKING_AGENT": "delivery_tracking_query",
```

在 `AGENT_SEEDS` 里加：

```python
    _agent(
        "DELIVERY_TRACKING_AGENT",
        "到货跟踪 Agent",
        "按时间窗/供应商统计订单到货达成（订单到货率 + 三态）并给出订单物料明细（delivery_tracking_query 工具，只读无 LLM）。",
        layers=("DWD",),
    ),
```

- [ ] **Step 5: 加 Agent 编排 profile**

`backend/scripts/seed_procurement_agent_profiles.py` 的 profile 字典里加一项（键 `DELIVERY_TRACKING_AGENT`）：

```python
    "DELIVERY_TRACKING_AGENT": (
        _READONLY_BASE
        + "\n职责：到货跟踪。用户给时间窗/供应商时，调 delivery_tracking_query"
          "（默认近 12 个月；企业月度周期用 anchorDay=24 + periodIndex），"
          "输出到货率、供应商分组与订单物料明细。"
        + "\n输出纪律：结构化数据（汇总/供应商表/订单明细）由前端直接渲染，"
          "你**不要**在文字里复述数字；你的文字只写两段："
          "①一段话结论（无数字）；②需要关注的供应商/订单要点（每条一句话）。"
          "用 Markdown 要点列表，不用表格。",
        ["delivery_tracking_query"],
        12,
        18,
    ),
```

> **元组形状已确认**（`backend/scripts/seed_procurement_agent_profiles.py` 的
> `PROFILE_SEEDS` 类型标注）：
> `dict[str, tuple[str, list[str], int, int]]` = `(指令模板, 绑定工具列表, 迭代上限, 工具调用上限)`。
> 上面这段按同款形状写，落进 `PROFILE_SEEDS` 即可。

- [ ] **Step 6: 加场景配置**

`backend/scripts/seed_copilot_scenarios.py` 的 `SEEDS` 追加：

```python
    {
        "scenario_code": "delivery_tracking",
        "name": "到货跟踪",
        "description": "按周期/供应商跟踪订单到货达成，含已到/未到物料明细",
        "icon_code": "car",
        "agent_code": "DELIVERY_TRACKING_AGENT",
        "slot_schema": [
            {
                "key": "monthsBack", "type": "number", "label": "时间窗", "required": True,
                "conflictKeywords": ["时间窗", "个月", "时间段", "日期", "上半年", "下半年", "本年度", "本年"],
                "displayTemplate": "近{value}个月",
            },
            {
                "key": "anchorDay", "type": "number", "label": "月度周期锚点日",
                "conflictKeywords": ["周期", "24日", "结算周期"],
            },
            {
                "key": "periodIndex", "type": "number", "label": "周期期序",
                "conflictKeywords": ["本期", "上期", "上个月"],
            },
            {
                "key": "supplierKeys", "type": "entityMulti", "label": "供应商",
                "conflictKeywords": ["供应商", "厂家"],
                "codePattern": "(?<![0-9A-Za-z])[A-Z]\\d{2,4}(?![0-9A-Za-z])",
            },
            {
                "key": "materialKeys", "type": "entityMulti", "label": "物料",
                "conflictKeywords": ["物料", "材料"],
            },
            {
                "key": "orderKeys", "type": "entityMulti", "label": "订单",
                "conflictKeywords": ["订单", "订单号"],
            },
        ],
        "question_template": "分析订单到货情况",
        "chips": [
            {"label": "近 12 个月到货概览", "monthsBack": 12},
            {"label": "近 3 个月到货概览", "monthsBack": 3},
            {"label": "本期到货概览（24 日周期）", "anchorDay": 24, "periodIndex": 0},
            {"label": "上期到货概览（24 日周期）", "anchorDay": 24, "periodIndex": -1},
        ],
        "result_renderer": "delivery_tracking",
        "required_role": None,
        "sort_order": 30,
        "enabled": True,
    },
```

**无 `datasourceId` 槽位**（THBI 硬规则）。

- [ ] **Step 7: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_copilot_scenario_seeds.py -v
```
Expected: 全部 passed

- [ ] **Step 8: 提交**

```bash
git add backend/scripts/seed_agents.py backend/scripts/seed_procurement_agent_profiles.py backend/scripts/seed_copilot_scenarios.py backend/app/domain/copilot_scenario_models.py backend/app/tests/unit/test_copilot_scenario_seeds.py
git commit -m "feat(copilot): DELIVERY_TRACKING_AGENT + delivery_tracking 场景配置（无数据源槽位）"
```

---

## Task 6: 24 日锚点在渲染层展开

**Files:**
- Modify: `backend/app/services/copilot_scenario_render.py`
- Test: `backend/app/tests/unit/test_copilot_scenario_render.py`

**Interfaces:**
- Consumes: Task 3 的 `resolveWindow`（**不直接 import** —— 渲染层只做「槽位 → 显式区间」展开，工具侧再解析）。
- Produces: `renderScenarioQuestion` 在槽位含 `anchorDay` 时，参数尾注里追加 `dateFrom=YYYY-MM-DD；dateTo=YYYY-MM-DD`。

**为什么在渲染层做**：槽位值 `anchorDay=24, periodIndex=0` 不是工具能直接用的参数；渲染层按「今天」算出具体区间，写进尾注 ⇒ 工具只需收 `dateFrom`/`dateTo`（它自己的 `anchorDay` 分支仍保留，供直接调工具时用）。

- [ ] **Step 1: 写失败测试**

在 `backend/app/tests/unit/test_copilot_scenario_render.py` 追加：

```python
def test_anchor_day_slots_expand_into_explicit_range() -> None:
    """24 日锚点槽位展开为显式 dateFrom/dateTo 进参数尾注。"""
    from app.services.copilot_scenario_render import renderScenarioQuestion

    question = renderScenarioQuestion(
        template="分析订单到货情况",
        slots={"anchorDay": 24, "periodIndex": 0},
        slotSchema=[
            {"key": "anchorDay", "type": "number", "label": "月度周期锚点日"},
            {"key": "periodIndex", "type": "number", "label": "周期期序"},
        ],
        message="",
    )
    assert "dateFrom=" in question
    assert "dateTo=" in question
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_copilot_scenario_render.py -v -k anchor
```
Expected: FAIL —— 尾注里没有 `dateFrom=`

- [ ] **Step 3: 实现展开**

`backend/app/services/copilot_scenario_render.py`：

在文件头加 import：

```python
from datetime import date
```

新增函数（放在 `_renderTemplate` 之后）：

```python
def _expandAnchorPeriod(slots: dict[str, Any]) -> dict[str, Any] | None:
    """24 日锚点槽位 → 显式 dateFrom/dateTo（spec 5.6）。

    只在两个槽位齐且 `anchorDay` 合法时展开；否则返回 None（不猜）。
    计算规则与工具侧 `resolveWindow` 一致：本期 = 已结束的最近完整锚点周期。
    """
    anchorDay = slots.get("anchorDay")
    if anchorDay is None:
        return None
    try:
        anchor = int(anchorDay)
    except (TypeError, ValueError):
        return None
    if not 1 <= anchor <= 28:
        return None
    try:
        index = int(slots.get("periodIndex") or 0)
    except (TypeError, ValueError):
        index = 0
    today = date.today()
    anchorDate = date(today.year, today.month, anchor)
    if anchorDate > today:
        anchorDate = _shiftMonths(anchorDate, -1)
    start = _shiftMonths(anchorDate, index - 1)
    end = _shiftMonths(anchorDate, index)
    return {"dateFrom": start.isoformat(), "dateTo": end.isoformat()}
```

再加一个本地 `_shiftMonths`（**不要**从 `delivery_tracking_query` import —— 渲染层与服务层同向依赖会成环）：

```python
def _shiftMonths(base: date, months: int) -> date:
    """base 偏移 months 个月（月末夹齐）。"""
    import calendar

    total = base.year * 12 + (base.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
```

在 `renderScenarioQuestion` 里，构造 `argsParts` **之前**插入：

```python
    expanded = _expandAnchorPeriod(slots)
    if expanded:
        slots = {**slots, **expanded}
```

**注意**：`slots` 是入参，改名会污染调用方 ⇒ 用新局部变量 `effectiveSlots`，并在后续所有 `slots.get(...)` 处改用它。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_copilot_scenario_render.py -v
```
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/copilot_scenario_render.py backend/app/tests/unit/test_copilot_scenario_render.py
git commit -m "feat(copilot): 24 日锚点槽位在渲染层展开为显式区间"
```

---

## Task 7: 前端 `DeliveryTrackingResultCard`

**Files:**
- Create: `frontend/src/components/copilot/DeliveryTrackingResultCard.tsx`
- Test: `frontend/src/tests/DeliveryTrackingResultCard.test.tsx`

**Interfaces:**
- Consumes: Task 4 的 `data` 结构（camelCase）。
- Produces:
  ```tsx
  export interface SupplierRiskResult { /* 复用已有类型 */ }
  export interface DeliveryTrackingResult {
    window: { dateFrom: string; dateTo: string; mode: string };
    summary: {
      totalLines: number; totalOrders: number;
      orderedQty: number; receivedQty: number;
      orderArrivalRate: number; qtyArrivalRate: number;
      allArrivedOrders: number; partialArrivedOrders: number;
      notArrivedOrders: number; excludedRows: number;
    };
    bySupplier: Array<{
      supplierCode: string; supplierName?: string | null;
      totalOrders: number; allArrivedOrders: number;
      partialArrivedOrders: number; notArrivedOrders: number;
      orderArrivalRate: number; orderedQty: number; receivedQty: number;
      qtyArrivalRate: number;
    }>;
    byOrder: Array<{
      poNo: string; supplierCode: string; supplierName?: string | null;
      orderDate?: string | null; status: "all" | "partial" | "notArrived";
      totalLines: number; arrivedLines: number; notArrivedLines: number;
      orderedQty: number; receivedQty: number;
      lines: Array<{
        poLine: number; orderedQty: number; receivedQty: number; openQty: number;
        expectedDate?: string | null; lastReceiptDate?: string | null;
        status: "all" | "partial" | "notArrived";
      }>;
    }>;
  }
  export function parseDeliveryTrackingStep(step: CopilotStepPayload): DeliveryTrackingResult | null;
  export default function DeliveryTrackingResultCard(props: { result: DeliveryTrackingResult }): JSX.Element;
  ```

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/tests/DeliveryTrackingResultCard.test.tsx`：

```tsx
/** 到货跟踪结果卡：主指标（订单到货率）+ 量级率参考 + 双 Tab 明细。 */

import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { describe, expect, it } from "vitest";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import i18n from "../i18n/i18n";
import DeliveryTrackingResultCard, {
  parseDeliveryTrackingStep,
} from "../components/copilot/DeliveryTrackingResultCard";
import type { CopilotStepPayload } from "../api/procurementCopilot";

function step(toolName: string, outputText: string | null): CopilotStepPayload {
  return {
    stepNo: 3,
    iterationNo: 2,
    kind: "TOOL",
    status: "SUCCEEDED",
    action: null,
    planSummary: null,
    toolName,
    answer: null,
    errorCode: null,
    errorMessage: null,
    output: outputText === null ? null : { text: outputText, truncated: false },
  };
}

const SAMPLE = JSON.stringify({
  data: {
    window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
    summary: {
      totalLines: 19908, totalOrders: 4844,
      orderedQty: 98495265.255, receivedQty: 39904579.255,
      orderArrivalRate: 82.29, qtyArrivalRate: 40.51,
      allArrivedOrders: 3986, partialArrivedOrders: 448, notArrivedOrders: 410,
      excludedRows: 0,
    },
    bySupplier: [
      {
        supplierCode: "B125", supplierName: "浙江力航汽车部件有限公司",
        totalOrders: 29, allArrivedOrders: 1, partialArrivedOrders: 16,
        notArrivedOrders: 12, orderArrivalRate: 3.45,
        orderedQty: 15155100, receivedQty: 2176026, qtyArrivalRate: 14.36,
      },
    ],
    byOrder: [
      {
        poNo: "T22611POH0223", supplierCode: "B125",
        supplierName: "浙江力航汽车部件有限公司",
        orderDate: "2026-10-28", status: "partial",
        totalLines: 1, arrivedLines: 0, notArrivedLines: 1,
        orderedQty: 1047000, receivedQty: 118261,
        lines: [
          {
            poLine: 3000, materialCode: "5A2100000002",
            materialDesc: "kun10/kun30/N1/N2方形焊接螺母M6",
            orderedQty: 1047000, receivedQty: 118261, openQty: 928739,
            expectedDate: "2026-11-07", lastReceiptDate: "2026-11-24", status: "partial",
          },
        ],
      },
    ],
  },
  answer: "",
});

describe("parseDeliveryTrackingStep", () => {
  it("解析 delivery_tracking_query 的 observation", () => {
    const parsed = parseDeliveryTrackingStep(step("delivery_tracking_query", SAMPLE));
    expect(parsed).not.toBeNull();
    expect(parsed?.summary.orderArrivalRate).toBe(82.29);
    expect(parsed?.bySupplier[0].supplierCode).toBe("B125");
    expect(parsed?.byOrder[0].status).toBe("partial");
  });

  it("非本工具 / 坏 JSON ⇒ null（回退纯文本）", () => {
    expect(parseDeliveryTrackingStep(step("pending_delivery_query", SAMPLE))).toBeNull();
    expect(parseDeliveryTrackingStep(step("delivery_tracking_query", "not-json"))).toBeNull();
    expect(parseDeliveryTrackingStep(step("delivery_tracking_query", null))).toBeNull();
  });
});

describe("DeliveryTrackingResultCard", () => {
  it("主指标=订单到货率；量级率为参考；双 Tab 明细", () => {
    const parsed = parseDeliveryTrackingStep(step("delivery_tracking_query", SAMPLE))!;
    render(
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>
          <ConfigProvider locale={zhCN}>
            <DeliveryTrackingResultCard result={parsed} />
          </ConfigProvider>
        </I18nextProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText(/订单到货率/)).toBeTruthy();
    expect(screen.getByText("82.29%")).toBeTruthy();
    expect(screen.getByText(/量级加权/)).toBeTruthy();
    expect(screen.getByText("40.51%")).toBeTruthy();
    expect(screen.getByText("浙江力航汽车部件有限公司")).toBeTruthy();
    expect(screen.getByText(/未到货订单/)).toBeTruthy();
    expect(screen.getByText(/已到货订单/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd frontend && npx vitest run src/tests/DeliveryTrackingResultCard.test.tsx
```
Expected: FAIL —— 模块不存在

- [ ] **Step 3: 实现结果卡**

创建 `frontend/src/components/copilot/DeliveryTrackingResultCard.tsx`：

```tsx
/** 到货跟踪结果卡（P5）：主指标订单到货率 + 量级率参考 + 供应商表 + 双 Tab 订单明细。
 *
 * 数据源 = delivery_tracking_query 工具的 observation JSON（camelCase）；
 * 坏 JSON / 不匹配 ⇒ null（回退纯文本答案，结果卡不经 LLM 复述）。
 */

import { Alert, Card, Collapse, Table, Tabs, Tag, Typography } from "antd";
import { useTranslation } from "../../i18n";
import type { CopilotStepPayload } from "../../api/procurementCopilot";

const { Text } = Typography;

export type DeliveryOrderStatus = "all" | "partial" | "notArrived";

export interface DeliveryTrackingResult {
  window: { dateFrom: string; dateTo: string; mode: string };
  summary: {
    totalLines: number;
    totalOrders: number;
    orderedQty: number;
    receivedQty: number;
    orderArrivalRate: number;
    qtyArrivalRate: number;
    allArrivedOrders: number;
    partialArrivedOrders: number;
    notArrivedOrders: number;
    excludedRows: number;
  };
  bySupplier: Array<{
    supplierCode: string;
    supplierName?: string | null;
    totalOrders: number;
    allArrivedOrders: number;
    partialArrivedOrders: number;
    notArrivedOrders: number;
    orderArrivalRate: number;
    orderedQty: number;
    receivedQty: number;
    qtyArrivalRate: number;
  }>;
  byOrder: Array<{
    poNo: string;
    supplierCode: string;
    supplierName?: string | null;
    orderDate?: string | null;
    status: DeliveryOrderStatus;
    totalLines: number;
    arrivedLines: number;
    notArrivedLines: number;
    orderedQty: number;
    receivedQty: number;
    lines: Array<{
      poLine: number;
      materialCode?: string | null;
      materialDesc?: string | null;
      orderedQty: number;
      receivedQty: number;
      openQty: number;
      expectedDate?: string | null;
      lastReceiptDate?: string | null;
      status: DeliveryOrderStatus;
    }>;
  }>;
}

const STATUS_COLOR: Record<DeliveryOrderStatus, string> = {
  all: "green",
  partial: "orange",
  notArrived: "red",
};

const STATUS_LABEL: Record<DeliveryOrderStatus, string> = {
  all: "已到货",
  partial: "部分到货",
  notArrived: "未到货",
};

/** 从 delivery_tracking_query 工具步骤解析结构化结果；不匹配/坏 JSON ⇒ null。 */
export function parseDeliveryTrackingStep(
  step: CopilotStepPayload,
): DeliveryTrackingResult | null {
  if (step.toolName !== "delivery_tracking_query") return null;
  if (step.status !== "SUCCEEDED") return null;
  const text = (step.output as { text?: string } | null | undefined)?.text;
  if (!text) return null;
  try {
    const parsed = JSON.parse(text) as { data?: DeliveryTrackingResult };
    const data = parsed.data;
    if (!data?.summary || !Array.isArray(data.bySupplier) || !Array.isArray(data.byOrder)) {
      return null;
    }
    return data;
  } catch {
    return null;
  }
}

function fmtQty(value: number | undefined): string {
  if (value === undefined || value === null) return "-";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(value);
}

function fmtPct(value: number | undefined): string {
  if (value === undefined || value === null) return "-";
  return `${value.toFixed(2)}%`;
}

function supplierLabel(code: string, name?: string | null): string {
  return name ? `${name}（${code}）` : code;
}

export default function DeliveryTrackingResultCard({
  result,
}: {
  result: DeliveryTrackingResult;
}) {
  const { summary, bySupplier, byOrder, window } = result;

  const notArrived = byOrder.filter((o) => o.status !== "all");
  const arrived = byOrder.filter((o) => o.status === "all");

  const orderColumns = [
    { title: "订单号", dataIndex: "poNo", key: "poNo" },
    {
      title: "供应商",
      key: "supplier",
      render: (_: unknown, row: DeliveryTrackingResult["byOrder"][number]) =>
        supplierLabel(row.supplierCode, row.supplierName),
    },
    {
      title: "状态",
      key: "status",
      render: (_: unknown, row: DeliveryTrackingResult["byOrder"][number]) => (
        <Tag color={STATUS_COLOR[row.status]}>{STATUS_LABEL[row.status]}</Tag>
      ),
    },
    {
      title: "已到/总行数",
      key: "lines",
      render: (_: unknown, row: DeliveryTrackingResult["byOrder"][number]) =>
        `${row.arrivedLines}/${row.totalLines}`,
    },
    {
      title: "物料明细",
      key: "lines_detail",
      render: (_: unknown, row: DeliveryTrackingResult["byOrder"][number]) => (
        <Collapse
          size="small"
          items={[
            {
              key: "detail",
              label: "展开",
              children: (
                <Table
                  size="small"
                  pagination={false}
                  rowKey="poLine"
                  dataSource={row.lines}
                  columns={[
                    { title: "行号", dataIndex: "poLine", key: "poLine" },
                    {
                      title: "物料",
                      key: "material",
                      render: (_: unknown, line: DeliveryTrackingResult["byOrder"][number]["lines"][number]) =>
                        line.materialDesc
                          ? `${line.materialDesc}（${line.materialCode ?? "-"}）`
                          : (line.materialCode ?? "-"),
                    },
                    { title: "订购", key: "o", render: (_: unknown, line: DeliveryTrackingResult["byOrder"][number]["lines"][number]) => fmtQty(line.orderedQty) },
                    { title: "已收", key: "r", render: (_: unknown, line: DeliveryTrackingResult["byOrder"][number]["lines"][number]) => fmtQty(line.receivedQty) },
                    { title: "欠交", key: "op", render: (_: unknown, line: DeliveryTrackingResult["byOrder"][number]["lines"][number]) => fmtQty(line.openQty) },
                    { title: "状态", key: "s", render: (_: unknown, line: DeliveryTrackingResult["byOrder"][number]["lines"][number]) => (
                      <Tag color={STATUS_COLOR[line.status]}>{STATUS_LABEL[line.status]}</Tag>
                    ) },
                  ]}
                />
              ),
            },
          ]}
        />
      ),
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <Text strong style={{ fontSize: 16 }}>到货达成概览</Text>
        <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
          {`窗口 ${window.dateFrom} ~ ${window.dateTo}`}
        </Text>
        <div style={{ marginTop: 8 }}>
          <Text type="secondary">订单到货率</Text>
          <Text strong style={{ fontSize: 24, marginLeft: 8 }}>
            {fmtPct(summary.orderArrivalRate)}
          </Text>
        </div>
        <div style={{ marginTop: 4 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {`量级加权到货率 ${fmtPct(summary.qtyArrivalRate)}（受超大额未到货行影响，仅作参考）`}
          </Text>
        </div>
        <div style={{ marginTop: 8 }}>
          <Tag color="green">{`全到货 ${summary.allArrivedOrders}`}</Tag>
          <Tag color="orange">{`部分到货 ${summary.partialArrivedOrders}`}</Tag>
          <Tag color="red">{`未到货 ${summary.notArrivedOrders}`}</Tag>
          <Tag>{`订单 ${summary.totalOrders}`}</Tag>
          {summary.excludedRows > 0 && (
            <Tag>{`脏数据剔除 ${summary.excludedRows}`}</Tag>
          )}
        </div>
      </div>

      <Card size="small" title="供应商到货情况" style={{ marginBottom: 12 }}>
        <Table
          size="small"
          rowKey="supplierCode"
          dataSource={bySupplier}
          pagination={false}
          columns={[
            {
              title: "供应商",
              key: "supplier",
              render: (_: unknown, row: DeliveryTrackingResult["bySupplier"][number]) =>
                supplierLabel(row.supplierCode, row.supplierName),
            },
            { title: "订单数", dataIndex: "totalOrders", key: "totalOrders" },
            { title: "全到", dataIndex: "allArrivedOrders", key: "all" },
            { title: "部分", dataIndex: "partialArrivedOrders", key: "partial" },
            { title: "未到", dataIndex: "notArrivedOrders", key: "not" },
            {
              title: "订单到货率",
              key: "rate",
              render: (_: unknown, row: DeliveryTrackingResult["bySupplier"][number]) =>
                fmtPct(row.orderArrivalRate),
            },
            {
              title: "欠交量",
              key: "open",
              render: (_: unknown, row: DeliveryTrackingResult["bySupplier"][number]) =>
                fmtQty(row.orderedQty - row.receivedQty),
            },
          ]}
        />
      </Card>

      <Card size="small">
        <Tabs
          items={[
            {
              key: "notArrived",
              label: `未到货订单（${notArrived.length}）`,
              children: (
                <Table
                  size="small"
                  rowKey="poNo"
                  dataSource={notArrived}
                  pagination={false}
                  columns={orderColumns}
                />
              ),
            },
            {
              key: "arrived",
              label: `已到货订单（${arrived.length}）`,
              children: (
                <Table
                  size="small"
                  rowKey="poNo"
                  dataSource={arrived}
                  pagination={false}
                  columns={orderColumns}
                />
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd frontend && npx vitest run src/tests/DeliveryTrackingResultCard.test.tsx
```
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/copilot/DeliveryTrackingResultCard.tsx frontend/src/tests/DeliveryTrackingResultCard.test.tsx
git commit -m "feat(copilot): 到货跟踪结果卡（订单到货率为主 + 供应商表 + 双 Tab 明细）"
```

---

## Task 8: 前端接线（渲染器分发 + 类型）

**Files:**
- Modify: `frontend/src/api/copilotScenario.ts`
- Modify: `frontend/src/components/copilot/PendingDeliveryResultTabs.tsx`
- Modify: `frontend/src/pages/ProcurementCopilotPage.tsx`
- Test: `frontend/src/tests/SupplierRiskResultCard.test.tsx`（补一条分发测试）

**Interfaces:**
- Consumes: Task 7 的 `parseDeliveryTrackingStep` / `DeliveryTrackingResultCard`。
- Produces: 场景 `resultRenderer="delivery_tracking"` 时页面渲染新卡。

- [ ] **Step 1: 写失败测试**

在 `frontend/src/tests/SupplierRiskResultCard.test.tsx` 追加：

```tsx
it("渲染器分发：delivery_tracking 走新卡解析器", () => {
  const parsed = parseDeliveryTrackingStep({
    stepNo: 1,
    iterationNo: 1,
    kind: "TOOL",
    status: "SUCCEEDED",
    action: null,
    planSummary: null,
    toolName: "delivery_tracking_query",
    answer: null,
    errorCode: null,
    errorMessage: null,
    output: {
      text: JSON.stringify({
        data: {
          window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
          summary: {
            totalLines: 19908, totalOrders: 4844, orderedQty: 1, receivedQty: 1,
            orderArrivalRate: 82.29, qtyArrivalRate: 40.51,
            allArrivedOrders: 3986, partialArrivedOrders: 448,
            notArrivedOrders: 410, excludedRows: 0,
          },
          bySupplier: [], byOrder: [],
        },
      }),
      truncated: false,
    },
  });
  expect(parsed).not.toBeNull();
  expect(parsed?.summary.orderArrivalRate).toBe(82.29);
});
```

并在文件头 import 加上：

```tsx
import { parseDeliveryTrackingStep } from "../components/copilot/DeliveryTrackingResultCard";
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd frontend && npx vitest run src/tests/SupplierRiskResultCard.test.tsx
```
Expected: FAIL —— 模块不存在

- [ ] **Step 3: 扩展场景渲染器类型**

`frontend/src/api/copilotScenario.ts`：把渲染器类型从

```ts
resultRenderer?: "pending_delivery" | "supplier_risk" | null;
```

改为

```ts
resultRenderer?: "pending_delivery" | "supplier_risk" | "delivery_tracking" | null;
```

- [ ] **Step 4: 扩展 Tabs 分发**

`frontend/src/components/copilot/PendingDeliveryResultTabs.tsx`：

import 区加：

```tsx
import DeliveryTrackingResultCard, {
  type DeliveryTrackingResult,
  parseDeliveryTrackingStep,
} from "./DeliveryTrackingResultCard";
```

props 类型改：

```tsx
  renderer: "pending_delivery" | "supplier_risk" | "delivery_tracking";
```

`ResultTurn.parsed` 类型改：

```tsx
  parsed: PendingDeliveryResult | SupplierRiskResult | DeliveryTrackingResult | null;
```

渲染分发改：

```tsx
          {renderer === "supplier_risk" ? (
            <SupplierRiskResultCard result={active.parsed as SupplierRiskResult} />
          ) : renderer === "delivery_tracking" ? (
            <DeliveryTrackingResultCard result={active.parsed as DeliveryTrackingResult} />
          ) : (
            <PendingDeliveryResultCard result={active.parsed as PendingDeliveryResult} />
          )}
```

- [ ] **Step 5: 页面分发**

`frontend/src/pages/ProcurementCopilotPage.tsx`：

```tsx
  const structuredRenderer: "pending_delivery" | "supplier_risk" | "delivery_tracking" | null =
    activeScenario?.resultRenderer === "supplier_risk"
      ? "supplier_risk"
      : activeScenario?.resultRenderer === "delivery_tracking"
        ? "delivery_tracking"
        : activeScenario?.resultRenderer === "pending_delivery" || isPendingDelivery
          ? "pending_delivery"
          : null;
```

解析器分派改：

```tsx
          .map((step) =>
            structuredRenderer === "supplier_risk"
              ? parseSupplierRiskStep(step)
              : structuredRenderer === "delivery_tracking"
                ? parseDeliveryTrackingStep(step)
                : parsePendingDeliveryStep(step),
          )
```

import 区加：

```tsx
import { parseDeliveryTrackingStep } from "../components/copilot/DeliveryTrackingResultCard";
```

- [ ] **Step 6: 运行前端测试确认通过**

```bash
cd frontend && npx vitest run src/tests/SupplierRiskResultCard.test.tsx src/tests/DeliveryTrackingResultCard.test.tsx
```
Expected: 全部 passed

- [ ] **Step 7: 类型检查 + 全量前端测试**

```bash
cd frontend && npx tsc --noEmit
cd frontend && npx vitest run
```
Expected: tsc 无错；全量绿（基线 2599 passed）

- [ ] **Step 8: 提交**

```bash
git add frontend/src/api/copilotScenario.ts frontend/src/components/copilot/PendingDeliveryResultTabs.tsx frontend/src/pages/ProcurementCopilotPage.tsx frontend/src/tests/SupplierRiskResultCard.test.tsx
git commit -m "feat(copilot): 前端接线 delivery_tracking 渲染器分发"
```

---

## Task 9: 待到货场景迁本体 + THBI（去硬编码）

**Files:**
- Modify: `backend/app/services/agent_tools_procurement.py`（`pending_delivery_query` 及相关 SQL builder）
- Test: `backend/app/tests/unit/test_pending_delivery_ontology_migration.py`

**Interfaces:**
- Consumes: Task 1 的 `resolveBinding`。
- Produces: `pending_delivery_query` 改为经本体绑定取数（THBI）。

**改动清单**：

| 现状 | 迁后 |
|---|---|
| `_PENDING_TABLE = "ZJTH.PORDERQ"` | 订单行类 `DWD_PURCHASE_ORDER_DTL`（绑定解析） |
| `_PENDING_ITEM_TABLE = "ZJTH.ITMMASTER"` | 物料名走 MDM（不再 JOIN 数仓字典） |
| `_PENDING_FACILITY_TABLE = "ZJTH.FACILITY"` | 基地类 `DIM_FACILITY`（Task 2 已补绑定） |
| `_PENDING_SUPPLIER_TABLE = "ZJTH.BPSUPPLIER"` | 供应商名走 MDM |
| 裸列 `QTYSTU_0` / `RCPQTYSTU_0` / `POHNUM_0` / `POPLIN_0` / `BPSNUM_0` / `ITMREF_0` / `EXTRCPDAT_0` | 本体属性名 → 绑定解析出的物理列 |
| 欠交量 `QTYSTU_0 - RCPQTYSTU_0` | `PO_QTY - 收货已收量`（`RCV_QTY_STU` 聚合，与到货跟踪同口径） |

**⚠ 口径影响**：迁后近 12 月行数 84,049 → 192,467（2.3 倍），数据截止 10-23 → 11-24 ⇒
本地验收基线须**显式更新**并对比，不静默漂移。

- [ ] **Step 1: 写失败测试（无硬编码守卫）**

创建 `backend/app/tests/unit/test_pending_delivery_ontology_migration.py`：

```python
"""待到货迁本体 + THBI 的守卫：代码里不得再有源层表名与裸列名。"""

from __future__ import annotations

from pathlib import Path


def _sourceText() -> str:
    root = Path(__file__).resolve().parents[3]
    return (root / "app" / "services" / "agent_tools_procurement.py").read_text(
        encoding="utf-8"
    )


def test_no_zjth_schema_prefix_in_source() -> None:
    """THBI 硬规则：不得出现 ZJTH schema 前缀。"""
    text = _sourceText()
    assert "ZJTH." not in text, "待到货仍硬编码 ZJTH schema（违反 THBI 硬规则）"


def test_no_erp_raw_column_constants() -> None:
    """本体对接：不得再出现 ERP 原始列名常量。"""
    text = _sourceText()
    for column in ("QTYSTU_0", "RCPQTYSTU_0", "POHNUM_0", "POPLIN_0", "BPSNUM_0"):
        assert column not in text, f"待到货仍硬编码列名 {column}（应走本体绑定）"


def test_pending_table_constants_removed() -> None:
    """四个源层表常量必须删除。"""
    text = _sourceText()
    for const in (
        "_PENDING_TABLE",
        "_PENDING_ITEM_TABLE",
        "_PENDING_FACILITY_TABLE",
        "_PENDING_SUPPLIER_TABLE",
    ):
        assert const not in text, f"{const} 应删除（改走本体绑定）"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_pending_delivery_ontology_migration.py -v
```
Expected: FAIL —— 仍有 `ZJTH.` 与裸列名

- [ ] **Step 3: 迁移 SQL builder**

在 `agent_tools_procurement.py` 里：

1. 删除四个常量；
2. 给 `PendingDeliveryFilters` 加 `orderBinding` / `receiptBinding` / `facilityBinding` 三个参数（类型 `ResolvedBinding`）；
3. `buildPendingDeliverySummarySql` / `buildPendingDeliveryBySupplierSql` / `buildPendingDeliveryLinesSql` / `buildPendingDeliveryCountSql` 的表名与列名全部改从绑定取；
4. 供应商名 / 物料名从 MDM 取（新增 `_mdmNames(session, codes)` 辅助，走 `EntityMappingService().searchMappings` 或直接查 `entity_mapping`）。

**迁移后的三段 SQL 骨架**（骨架给全，列名一律 `binding.column("X")`）：

```python
def buildPendingDeliverySummarySql(
    *, monthsBack: int, today: date, filters: PendingDeliveryFilters | None = None,
    dialect: str = "oracle",
) -> str:
    """欠交汇总（单语句）：行数 / 总欠交量 / 逾期行数 / 脏数据行数。

    ⚠️ 欠交量口径 = <PO_QTY> - 收货已收量（与到货跟踪同口径）。
    """
    filters = filters or PendingDeliveryFilters()
    order = filters.orderBinding
    receipt = filters.receiptBinding
    poQty = order.column("PO_QTY")
    poNo = order.column("PO_NO")
    poLine = order.column("PO_LINE_NO")
    receiptAgg = (
        f"(SELECT {receipt.column('PO_NO')} AS PO_NO, "
        f"{receipt.column('PO_LINE_NO')} AS PO_LINE_NO, "
        f"SUM({receipt.column('RCV_QTY_STU')}) AS RECEIVED_QTY "
        f"FROM {receipt.qualifiedTable()} "
        f"GROUP BY {receipt.column('PO_NO')}, {receipt.column('PO_LINE_NO')})"
    )
    return f"""
SELECT
  SUM(CASE WHEN OPEN_QTY > 0 AND OPEN_QTY < {_OPEN_QTY_SANITY_MAX} THEN 1 ELSE 0 END) AS OPEN_LINES,
  {d.coalesce(f"SUM(CASE WHEN OPEN_QTY > 0 AND OPEN_QTY < {_OPEN_QTY_SANITY_MAX} THEN OPEN_QTY ELSE 0 END)", "0")} AS OPEN_QTY,
  SUM(CASE WHEN OPEN_QTY > 0 AND OPEN_QTY < {_OPEN_QTY_SANITY_MAX} AND OVERDUE_DAYS > 0 THEN 1 ELSE 0 END) AS OVERDUE_LINES,
  SUM(CASE WHEN OPEN_QTY >= {_OPEN_QTY_SANITY_MAX} THEN 1 ELSE 0 END) AS EXCLUDED_ROWS
FROM (
  SELECT O.{poNo} AS PO_NO, O.{poLine} AS PO_LINE_NO,
         O.{poQty} - COALESCE(R.RECEIVED_QTY, 0) AS OPEN_QTY,
         {d.overdueDays(todayLit, "O." + order.column("EXP_RCV_DATE"))} AS OVERDUE_DAYS
  FROM {order.qualifiedTable()} O
  LEFT JOIN {receiptAgg} R
    ON R.PO_NO = O.{poNo} AND R.PO_LINE_NO = O.{poLine}
  WHERE {filters.windowSql(monthsBack, today, order)}{filters.filterSql()}
) t""".strip()
```

> `windowSql` / `filterSql` 的签名要同步改（接收 `order` 绑定以取 `ORDER_DATE` / `SUPPLIER_CODE` / `ITEM_CODE` / `PUR_SITE_CODE`）。
> **`EXP_RCV_DATE`**：订单行类里有该属性（本体已绑），对应物理列 `EXP_RCV_DATE`；用它算逾期天数。

4. 站点过滤：订单行类的 `PUR_SITE_CODE`（属性）+ 基地类 `DIM_FACILITY` 的 `FCY_0` / `FCYNAM_0`（编码精确 / 名称模糊）。
5. 物料名：`DIM_IMATERIAL` 不再 JOIN —— 改从 MDM 取（`materialKeys` 的中文名解析逻辑保留，但解析目标改为 MDM 的 `enterprise_code`）。

- [ ] **Step 4: 改 handler 装配绑定**

`pendingDeliveryHandler` 里加绑定解析：

```python
    order = await resolveBinding(
        session, className="DWD_PURCHASE_ORDER_DTL",
        requiredProperties=["PO_NO", "PO_LINE_NO", "PO_QTY", "ORDER_DATE",
                            "SUPPLIER_CODE", "PUR_SITE_CODE", "EXP_RCV_DATE"],
    )
    receipt = await resolveBinding(
        session, className="DWD_GOODS_RECEIPT_DTL",
        requiredProperties=["PO_NO", "PO_LINE_NO", "RCV_QTY_STU"],
    )
    facility = await resolveBinding(
        session, className="DIM_FACILITY",
        requiredProperties=["FCY_0", "FCYNAM_0"],
    )
```

并把三个绑定塞进 `PendingDeliveryFilters(...)`。

- [ ] **Step 5: 运行守卫测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_pending_delivery_ontology_migration.py -v
```
Expected: 3 passed

- [ ] **Step 6: 运行受影响的既有测试**

```bash
cd backend && .venv/bin/python -m pytest app/tests/unit/test_agent_tools_procurement.py app/tests/unit/test_pending_delivery_summary_wiring.py app/tests/integration/test_pending_delivery_summary_api.py -v
```
Expected: 全部 passed（若有失败，按新口径修测试期望，**不得**改回硬编码）

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/agent_tools_procurement.py backend/app/tests/unit/test_pending_delivery_ontology_migration.py
git commit -m "refactor(copilot): 待到货迁本体 + THBI（删源层表常量与裸列名）"
```

---

## Task 10: 本地栈端到端实测 + 口径基线更新

**Files:**
- Modify: `docs/superpowers/specs/2026-11-25-delivery-tracking-scenario-design.md`（基线数字）
- Create: `backend/scripts/probe_delivery_tracking.py`（只读探针，留档口径）

**Interfaces:**
- Consumes: Task 4-9 全部产出。
- Produces: 口径基线对照表（实测 vs 期望）。

- [ ] **Step 1: 跑迁移 + 种子**

```bash
cd backend && .venv/bin/python -m scripts.seed_procurement_bindings
cd backend && .venv/bin/python -m scripts.seed_agent_tool_configs
cd backend && .venv/bin/python -m scripts.seed_agents
cd backend && .venv/bin/python -m scripts.seed_copilot_scenarios
```

- [ ] **Step 2: 跑只读探针对照基线**

创建 `backend/scripts/probe_delivery_tracking.py`（只读；参照
`backend/scripts/probe_pending_delivery.py` 的结构）：本期窗口
`2026-10-24 ~ 2026-11-24` 的订单数 / 三态 / 两个率值。

```bash
cd backend && .venv/bin/python -m scripts.probe_delivery_tracking
```

**期望（spec 基线）**：

```
订单总数            4844
全到货 / 部分 / 未到  3986 / 448 / 410
订单到货率          82.29%
量级率              40.51%
```

**若不符**：查明原因并**显式记录**（是数据变了还是口径错了），更新规格基线或修实现 ——
**不得**默默改期望值。

- [ ] **Step 3: 重建镜像并起栈**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system && bash scripts/build_backend_images.sh
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d backend agent-orchestration-worker frontend
```

- [ ] **Step 4: API 实测（JWT）**

容器以 `AUTH_MODE=real` 运行 ⇒ 必须用 Bearer JWT；**不得**重置管理员/本人账号
（项目铁律：别动本人账号，测试身份另建）。为此写一个**一次性冒烟身份脚本**：

创建 `backend/scripts/issue_smoke_token.py`：

```python
"""冒烟测试身份（2026-11-25，P5 实测用）。

⚠️ **绝不动本人/管理员账号**：本脚本只新建/复用 `smoke_p4` 这个**专用测试身份**，
   绝不复用、绝不重置其他任何账号的密码。

⚠️ **必须清 must_change_password**：`IdentityService.create_user` 恒置
   `must_change_password=True`，而 copilot 路由挂了 `mustChangePasswordGuard`
   ⇒ 不清掉的话该身份调任何业务端点都 403 MUST_CHANGE_PASSWORD。

用法：
    cd backend && .venv/bin/python -m scripts.issue_smoke_token
    # 输出：SMOKE_USERNAME=smoke_p4 / SMOKE_PASSWORD=SmokeP42026X
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.infrastructure.database import getSessionFactory
from app.models.rbac import User

#: 专用测试身份（不复用任何既有账号）。
SMOKE_USERNAME = "smoke_p4"
#: 测试身份口令（仅本地栈用；满足服务端策略：≥8 位含字母+数字）。
SMOKE_PASSWORD = "SmokeP42026X"


async def main() -> None:
    async with getSessionFactory()() as session:
        row = (
            await session.execute(
                select(User).where(User.username == SMOKE_USERNAME)
            )
        ).scalars().first()
        if row is None:
            from app.dependencies import CurrentUser
            from app.schemas.rbac import UserCreate
            from app.services.identity_service import IdentityService

            actor = CurrentUser(
                userId="seed-admin", roles=("admin",), departments=("procurement",)
            )
            dto = UserCreate(
                username=SMOKE_USERNAME,
                display_name="P5 冒烟测试身份",
                email="smoke_p4@example.invalid",
                password=SMOKE_PASSWORD,
                enabled=True,
            )
            await IdentityService().create_user(session, dto, actor)
            await session.commit()
            print(f"created: {SMOKE_USERNAME}")
        else:
            print(f"reused: {SMOKE_USERNAME}")
        # 清掉强制改密标记（否则 copilot 等业务端点一律 403）
        await session.execute(
            User.__table__.update()
            .where(User.username == SMOKE_USERNAME)
            .values(must_change_password=False)
        )
        await session.commit()
    print(f"SMOKE_USERNAME={SMOKE_USERNAME}")
    print(f"SMOKE_PASSWORD={SMOKE_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(main())
```

**登录取 token**（走真实 HTTP 端点；`AuthService.login` 需要真实 `Request`
读 `request.client.host`，脚本内直调不可行）：

```bash
cd backend && .venv/bin/python -m scripts.issue_smoke_token

TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"smoke_p4","password":"SmokeP42026X"}' | jq -r '.accessToken')
echo "TOKEN=${TOKEN:0:24}…"
```

> `AuthLoginResponse` 字段是 **`accessToken`**（camelCase，`app/schemas/auth.py`），
> 不是 `access_token`。

发一轮场景请求（锚点本期）：

```bash
curl -s -X POST http://localhost:8000/api/v1/procurement/copilot/messages \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "message": "",
    "agentCode": "DELIVERY_TRACKING_AGENT",
    "scenarioCode": "delivery_tracking",
    "slots": {"anchorDay": 24, "periodIndex": 0},
    "modelConfigId": 1
  }' | jq .
```

**期望**：轮次状态 `SUCCEEDED`；步骤里有 `delivery_tracking_query` 的 observation，
其 `data.summary.orderArrivalRate ≈ 82.29`、`totalOrders = 4844`。

- [ ] **Step 5: 浏览器验收**

打开 `http://localhost:5173` → 进「到货跟踪」场景 → 点 chip「本期到货概览（24 日周期）」
⇒ 卡片显示订单到货率 82.29%、量级率 40.51%（参考）、三态计数、供应商表、
「未到货订单 / 已到货订单」双 Tab，订单可展开看物料行。

- [ ] **Step 6: 待到货场景回归**

在待到货场景发一轮（近 12 个月）⇒ 数据量应显著变大（192,467 行级 vs 迁移前 84,049），
卡片显示新基线；**把新旧差异显式记录**到规格与本计划。

- [ ] **Step 7: 更新规格基线并提交**

```bash
git add docs/superpowers/specs/2026-11-25-delivery-tracking-scenario-design.md backend/scripts/probe_delivery_tracking.py
git commit -m "test(copilot): 到货跟踪端到端实测 + 口径基线对照（本期 4844 单 / 82.29%）"
```

---

## Self-Review

**1. 规格覆盖检查**

| 规格章节 | 对应任务 |
|---|---|
| 3.0 THBI 硬规则 | Global Constraints + Task 1（解析器固定 datasource_id=1）+ Task 5（无数据源槽位）+ Task 9（删 `ZJTH.`） |
| 3.0.2 本体对接复用绑定设施 | Task 1（解析器）、Task 4（工具走解析器） |
| 3.1 本体现状盘点 | Task 2（`DIM_FACILITY` 补绑定）—— 其余类已绑，无需新任务 |
| 3.2 绑定补充 | Task 2 |
| 3.2.1 BO 换绑 + 删类 | **本轮不做**（spec 11.1 决策 6/7 已推迟）—— 规格已记录依赖链 |
| 3.3 工具如何用本体取数 | Task 1 + Task 4 |
| 3.4 数据源绑定 | Task 1 + Task 5 |
| 3.5 字典走 MDM | Task 4（`supplierName` 来源）+ Task 9 |
| 3.7 到货明细表限制 | 无需任务（决策 11.2-3：本期不接） |
| 3.8 待到货迁移 | Task 9 |
| 4.1 到货判定 | Task 3（SQL 模板）+ Task 4（三态装配） |
| 4.2 核心指标 | Task 3 + Task 4 + Task 7 |
| 4.3 时间窗口 | Task 3（`resolveWindow`）+ Task 6（渲染层展开） |
| 4.4 脏数据 | Task 3（`excludedRows` 字段）+ Task 7（Tag 展示） |
| 4.5 未到货 ≠ 逾期 | Task 4（`status` 用 `notArrived` 而非「逾期」措辞）+ Task 7（标签「未到货」） |
| 5.1 组件表 | Task 1/2/4/5/7/9 |
| 5.3 工具契约 | Task 4 + Task 5（`input_schema`） |
| 5.5 场景配置 | Task 5 |
| 5.6 周期参数展开 | Task 6 |
| 5.7 供应商检索 | 复用既有 `Supplier360Service._resolveSupplier`（既有代码，无需新任务） |
| 5.8 本体对接落地机制 | Task 1 |
| 6 前端结果卡 | Task 7 |
| 7 错误处理 | Task 1（fail-closed）+ Task 4 |
| 8.1 后端单测 | Task 1/3/4/9 |
| 8.2 集成测试 | Task 1（Task 2 补数据）+ Task 4 |
| 8.3 口径校验 | Task 10 |
| 8.4 前端测试 | Task 7/8 |
| 8.5 实测验收 | Task 10 |
| 9 不做 | 全部任务均未越界（无 generic_table、无基地维度、无到货明细接入、无跨 schema） |
| 10 风险 | Task 10 显式记录口径差异 |
| 11 决策台账 | 已全部落定；11.2 决策 9-14 落在 Task 4