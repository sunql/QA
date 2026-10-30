# F2 KPI Datasource Write-Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** F2 kpi_catalog.datasource_id 列已 merged (migration 0125) 但全表 NULL；本 plan 让写入流必填 + 删 DS 拒引用 + FK 改 RESTRICT，让 chat 路径 L1 mismatch 警告实际可触发。

**Architecture:** 4 块改动（DTO 必填 / service 校验 / FK RESTRICT migration / DS 删除引用计数）+ 4 个新错误码 + 8 TDD 用例（U1-U3 unit + I1-I5 integration 真 PG）。逐 task TDD 推，每个 task 独立 reviewable。

**Tech Stack:** FastAPI / SQLAlchemy 2 async / Alembic / Pydantic v2 / pytest + 真实 PostgreSQL (per `qa-system-integration-env-contract` unit 也要 TEST_DATABASE_URL)。

## Global Constraints

(from spec + harness rules)

- **真 PG 测试强制**：unit 也要 `TEST_DATABASE_URL`；integration 5 个需真 PG + 完整 API 链路。per `qa-system-integration-env-contract`。
- **Test DB 端口 5434 may not have listener**：per `qa-system-test-db-solo-isolation`，用 5433 + `qa_metadata_test_solo`。
- **小文件 800 行硬限**：DTO 改动小，service 校验内联（per spec §4.2 不抽 helper，YAGNI）。
- **不可变数据**：per CLAUDE.md 顶层规则，所有 DTO 字段 readonly model 不允许 in-place 修改。
- **SQL 安全**：业务查询仅 SELECT，DELETE 仅 datasource_service.delete（受限路径）。
- **Type hints 字节级保留**：不引入新 type alias。
- **Conventional commits**：feat / fix / refactor / docs / test / chore。
- **Spec 偏差 2 项（plan 显式承认）**：
  1. **Migration 编号**：spec §4.5 写 `0126_kpi_catalog_datasource_fk_restrict.py`，但 0126 已被 `0126_session_token_usage_cost_source.py` 占。**新编号 0132**（`down_revision = "0131"`，main head），per `qa-system-alembic-merge-number-collision`。
  2. **Fixture 影响面**：spec §8.3 估 ≥ 8 个测试文件补 `datasource_id=<DS>`，实测 `grep -rn "KpiCatalog(" backend/app/tests/` = **31 处**，涉及 10+ 文件。**Task 6 显式列出每个文件**。
- **branch + worktree**：本 plan 在 `/private/tmp/qa-f2-kpi-datasource` worktree + `feat/f2-kpi-datasource-write-flow` 分支上。base = `aef0124e` (origin/main) + spec cherry-pick `7ec77da2`。
- **SSOT 入口**：`docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md` (310 lines) + `Harness/changes/2026-10-18-kpi-datasource-write-flow-spec/summary.md` (88 lines)。本 plan 实施后更新 summary.md 状态为 ✅。
- **TDD 严格**：每 task 先 RED (写失败 test) → GREEN (最小实现) → IMPROVE (重构)；不跳步。
- **拒绝 pre-existing 红伪装成新红**：每 task 跑相关 8 个用例，对比基线 fingerprint，**只允许 baseline 15 个 pre-existing 失败**，不允许新红。

## File Structure

| File | 责任 | 操作 |
|---|---|---|
| `backend/app/domain/schemas.py:680,713` | `KpiCatalogCreate` / `KpiCatalogUpdate` DTO | Modify（+2 字段） |
| `backend/app/services/kpi_catalog_service.py:80-109` | `createKpi` / `updateKpi` 校验 | Modify（+13 行） |
| `backend/app/services/datasource_service.py:185-211` | `delete` 引用计数 | Modify（+8 行） |
| `backend/app/services/messages_zh.py:503-505` 后追加 | 4 个新错误码 | Modify（+4 行） |
| `backend/alembic/versions/0132_kpi_catalog_datasource_fk_restrict.py` | FK 改 RESTRICT | Create |
| `backend/app/tests/unit/test_kpi_catalog_dto.py` | U1-U3 Pydantic 校验 | Create |
| `backend/app/tests/integration/test_kpi_catalog_datasource_validation.py` | I1-I3 service 校验 | Create |
| `backend/app/tests/integration/test_datasource_delete_kpi_guard.py` | I4-I5 DS 删除校验 | Create |
| `backend/app/tests/integration/conftest.py` | `seed_data_source` fixture | Modify |
| 10+ 既有 KpiCatalog 测试文件 | 补 `datasource_id=<seed_data_source.id>` | Modify |
| `Harness/changes/2026-10-18-kpi-datasource-write-flow-spec/summary.md` | 状态变更 + cross-ref | Modify |
| `docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md` | spec §4.5 改 0126 → 0132 + §8.3 改 8 → 31 | Modify（spec 同步偏差） |

---

## Task 1: DTO 改造 + U1-U3 (Pydantic 内存)

**Files:**
- Modify: `backend/app/domain/schemas.py:680` (KpiCatalogCreate 加 `datasource_id`)
- Modify: `backend/app/domain/schemas.py:713` (KpiCatalogUpdate 加 `datasource_id`)
- Create: `backend/app/tests/unit/test_kpi_catalog_dto.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `KpiCatalogCreate.datasource_id: int = Field(..., ge=1)` + `KpiCatalogUpdate.datasource_id: int | None = Field(default=None, ge=1)`

- [ ] **Step 1: 写 U1 失败测试**

在 `backend/app/tests/unit/test_kpi_catalog_dto.py` 写：

```python
"""Pydantic 内存校验 — U1-U3（per F2 spec §5.1）。"""
from __future__ import annotations
import pytest
from pydantic import ValidationError
from app.domain.schemas import KpiCatalogCreate, KpiCatalogUpdate


def test_kpi_create_with_valid_datasource_id_passes() -> None:
    """U1: datasource_id=5 合法。"""
    dto = KpiCatalogCreate(
        kpi_code="K001", kpi_name="test", business_definition="d",
        formula="SUM(x)", numerator="x", denominator=None, grain="g",
        unit="u", data_source="legacy_text", owner="o", version="1",
        metric_id=None, created_by="u",
        semantic_keywords=[], match_threshold=0.5,
        datasource_id=5,  # 关键
    )
    assert dto.datasource_id == 5


def test_kpi_create_with_zero_datasource_id_rejected() -> None:
    """U2: datasource_id=0 拒绝（ge=1）。"""
    with pytest.raises(ValidationError) as exc_info:
        KpiCatalogCreate(
            kpi_code="K001", kpi_name="test", business_definition="d",
            formula="SUM(x)", numerator="x", denominator=None, grain="g",
            unit="u", data_source="legacy_text", owner="o", version="1",
            metric_id=None, created_by="u",
            semantic_keywords=[], match_threshold=0.5,
            datasource_id=0,
        )
    assert "datasource_id" in str(exc_info.value)


def test_kpi_create_with_negative_datasource_id_rejected() -> None:
    """U3: datasource_id=-1 拒绝（ge=1）。"""
    with pytest.raises(ValidationError) as exc_info:
        KpiCatalogCreate(
            kpi_code="K001", kpi_name="test", business_definition="d",
            formula="SUM(x)", numerator="x", denominator=None, grain="g",
            unit="u", data_source="legacy_text", owner="o", version="1",
            metric_id=None, created_by="u",
            semantic_keywords=[], match_threshold=0.5,
            datasource_id=-1,
        )
    assert "datasource_id" in str(exc_info.value)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && uv run pytest app/tests/unit/test_kpi_catalog_dto.py -v
```

Expected: 3 FAIL with `AttributeError: 'KpiCatalogCreate' has no attribute 'datasource_id'` (or `TypeError: missing required field`).

- [ ] **Step 3: DTO 加字段（最小实现）**

在 `backend/app/domain/schemas.py:680`（`class KpiCatalogCreate(CamelModel):` 之后，**`metric_id` 字段之前**）加：

```python
datasource_id: int = Field(..., ge=1, description="绑定的业务数据源 ID（FK → data_source.id）")
```

在 `backend/app/domain/schemas.py:713`（`class KpiCatalogUpdate(CamelModel):` 之后，**`metric_id` 字段之前**）加：

```python
datasource_id: int | None = Field(default=None, ge=1, description="重绑数据源；显式传 null 解绑")
```

位置与 spec §4.1 一致：放在 `metric_id` 之前（与 DB 列顺序对齐）。

- [ ] **Step 4: 跑测试确认 PASS**

```bash
cd backend && uv run pytest app/tests/unit/test_kpi_catalog_dto.py -v
```

Expected: 3 PASS。

- [ ] **Step 5: 跑既有 KpiCatalogCreate/Update 用例确认 0 回归**

```bash
cd backend && uv run pytest app/tests/integration/test_kpi_catalog.py app/tests/integration/test_seed_kpi_catalog.py -v
```

Expected: 既有失败名字不变（如果有），**新失败 = 0**。如果新失败是因为 DTO 缺少 `datasource_id` 必填，那是 expected（Task 6 修）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/domain/schemas.py backend/app/tests/unit/test_kpi_catalog_dto.py
git commit -m "feat(schemas): KpiCatalogCreate/Update add datasource_id (U1-U3 Pydantic)"
```

---

## Task 2: messages_zh 加 4 个错误码

**Files:**
- Modify: `backend/app/services/messages_zh.py:505` 后追加 4 行

**Interfaces:**
- Consumes: nothing
- Produces: 4 个常量（`MSG_KPI_DATASOURCE_REQUIRED` / `MSG_KPI_DATASOURCE_NOT_FOUND` / `MSG_KPI_DATASOURCE_INACTIVE` / `MSG_DATASOURCE_HAS_KPI_BINDINGS`）

- [ ] **Step 1: 追加 4 个错误码**

在 `backend/app/services/messages_zh.py:505`（`MSG_KPI_CATALOG_STATUS_NULL` 后）追加：

```python
# KPI Catalog datasource 绑定（F2 写入流收尾 — 2026-10-18 spec §4.4）
MSG_KPI_DATASOURCE_REQUIRED = "KPI 必须绑定一个数据源（datasource_id 必填）"
MSG_KPI_DATASOURCE_NOT_FOUND = "KPI 绑定的数据源 id={id} 不存在"
MSG_KPI_DATASOURCE_INACTIVE = "KPI 绑定的数据源 id={id}（{name}）已停用"
MSG_DATASOURCE_HAS_KPI_BINDINGS = "数据源 id={id} 被 {count} 个 KPI 引用，无法删除"
```

- [ ] **Step 2: 跑 lint 确认无格式问题**

```bash
cd backend && uv run ruff check app/services/messages_zh.py
```

Expected: 0 errors.

- [ ] **Step 3: 提交**

```bash
git add backend/app/services/messages_zh.py
git commit -m "feat(messages): add 4 KPI datasource binding error codes (spec §4.4)"
```

---

## Task 3: kpi_catalog_service 校验 + I1-I3 (真 PG 5433)

**Files:**
- Modify: `backend/app/services/kpi_catalog_service.py:80-109` (`createKpi` 末尾加校验)
- Modify: `backend/app/services/kpi_catalog_service.py:updateKpi` (同款校验)
- Modify: `backend/app/services/kpi_catalog_service.py` 顶部 docstring（per spec §4.6）
- Create: `backend/app/tests/integration/test_kpi_catalog_datasource_validation.py`

**Interfaces:**
- Consumes: `MSG_KPI_DATASOURCE_NOT_FOUND` / `MSG_KPI_DATASOURCE_INACTIVE` (from Task 2)
- Consumes: `DataSource` model (from `app.domain.models`)
- Produces: 422 ValidationError on datasource_id 不存在或 is_active=false

- [ ] **Step 1: 写 I1-I3 失败测试（先确认 RED）**

在 `backend/app/tests/integration/test_kpi_catalog_datasource_validation.py` 写：

```python
"""I1-I3 KPI Catalog datasource 校验（per F2 spec §5.2）。

注意：KpiCatalogService.createKpi / updateKpi 签名是
  (session, dto: KpiCatalogCreate|KpiCatalogUpdate, actor: CurrentUser)
不是 dict。构造 DTO 实例后传，actor 用默认 CurrentUser()（含 admin role → ACL 通过）。
"""
from __future__ import annotations
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import CurrentUser
from app.domain.exceptions import ValidationError as DomainValidationError
from app.domain.models import DataSource, KpiCatalog
from app.domain.schemas import KpiCatalogCreate, KpiCatalogUpdate
from app.services.kpi_catalog_service import KpiCatalogService
from app.services.messages_zh import (
    MSG_KPI_DATASOURCE_NOT_FOUND,
    MSG_KPI_DATASOURCE_INACTIVE,
)


@pytest.mark.asyncio
async def test_createKpi_with_nonexistent_datasource_id_returns_422(
    dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    """I1: datasource_id=不存在 → 422 + MSG_KPI_DATASOURCE_NOT_FOUND。"""
    svc = KpiCatalogService()
    current_user = CurrentUser()  # default roles 含 admin → ACL 通过
    dto = KpiCatalogCreate(
        kpi_code="TEST_I1", kpi_name="test",
        business_definition="d", formula="SUM(x)",
        numerator="x", denominator=None, grain="g",
        unit="u", data_source="legacy_text", owner="o",
        version="1",  # 注意：DTO 期望 str，不是 int
        metric_id=None,
        created_by="u", semantic_keywords=[], match_threshold=0.5,
        datasource_id=99999,  # 不存在
    )
    with pytest.raises(DomainValidationError) as exc_info:
        await svc.createKpi(dbSession, dto, current_user)
    assert MSG_KPI_DATASOURCE_NOT_FOUND.format(id=99999) in str(exc_info.value)


@pytest.mark.asyncio
async def test_createKpi_with_inactive_datasource_id_returns_422(
    dbSession: AsyncSession, seed_inactive_data_source: DataSource,
) -> None:
    """I2: datasource_id=is_active=false → 422 + MSG_KPI_DATASOURCE_INACTIVE。"""
    svc = KpiCatalogService()
    current_user = CurrentUser()
    ds_id = seed_inactive_data_source.id
    dto = KpiCatalogCreate(
        kpi_code="TEST_I2", kpi_name="test",
        business_definition="d", formula="SUM(x)",
        numerator="x", denominator=None, grain="g",
        unit="u", data_source="legacy_text", owner="o",
        version="1",  # str
        metric_id=None,
        created_by="u", semantic_keywords=[], match_threshold=0.5,
        datasource_id=ds_id,
    )
    with pytest.raises(DomainValidationError) as exc_info:
        await svc.createKpi(dbSession, dto, current_user)
    assert MSG_KPI_DATASOURCE_INACTIVE.format(id=ds_id, name="INACTIVE_TEST_DS") in str(exc_info.value)


@pytest.mark.asyncio
async def test_updateKpi_with_none_datasource_id_skips_validation(
    dbSession: AsyncSession, seed_data_source: DataSource, sample_kpi: KpiCatalog,
) -> None:
    """I3: updateKpi datasource_id=None 跳过校验，列保持原值。"""
    svc = KpiCatalogService()
    current_user = CurrentUser()
    kpi = sample_kpi
    original_ds_id = kpi.datasource_id
    # 显式传 None → 跳过校验 → 原 datasource_id 不变
    update = KpiCatalogUpdate(datasource_id=None, kpi_name="updated_name")
    result = await svc.updateKpi(dbSession, kpi.id, update, current_user)
    assert result.datasource_id == original_ds_id
    assert result.kpi_name == "updated_name"
```

需要的 fixtures（`seed_data_source` / `seed_inactive_data_source` / `sample_kpi`）在 Task 6 一起加。先在本文件写 import 注释 + `# noqa: F811`，让 Task 6 一次补齐。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run pytest app/tests/integration/test_kpi_catalog_datasource_validation.py -v
```

Expected: 3 FAIL with `AttributeError: 'KpiCatalogService' object has no attribute 'createKpi'` 或 `fixture seed_data_source not found` (后者是 Task 6 的事，**这步接受 fixture 缺失也算 RED 通过**)。

- [ ] **Step 3: kpi_catalog_service 顶部 docstring 加历史 NULL 说明（spec §4.6）**

在 `backend/app/services/kpi_catalog_service.py` 顶部 docstring 替换为：

```python
"""KPI Catalog service.

Legacy rows: kpi_catalog.datasource_id IS NULL（migration 0125 之前的历史行 +
未填的运营行）— chat 路径 L1 mismatch 检查会跳过（per chat_stream_output.py:197
"NULL ⇒ 不校验"分支），不警告。要让警告生效，必须先 PUT 一次以填补 datasource_id。
"""
```

- [ ] **Step 4: createKpi 末尾加校验（最小实现）**

在 `backend/app/services/kpi_catalog_service.py` 的 `createKpi` 方法末尾（commit/flush 之前）加：

```python
# F2 §4.2: 校验 datasource 存在 + is_active
ds = await session.get(DataSource, dto.datasource_id)
if ds is None:
    raise ValidationError(MSG_KPI_DATASOURCE_NOT_FOUND.format(id=dto.datasource_id))
if not ds.is_active:
    raise ValidationError(MSG_KPI_DATASOURCE_INACTIVE.format(id=ds.id, name=ds.name))
```

加 imports（如未加）：
```python
from app.domain.models import DataSource
from app.services.messages_zh import (
    MSG_KPI_DATASOURCE_INACTIVE,
    MSG_KPI_DATASOURCE_NOT_FOUND,
)
```

- [ ] **Step 5: updateKpi 加同款校验（仅非 None 时）**

在 `updateKpi` 末尾加（与 createKpi 不同：`datasource_id is None` 时跳过）：

```python
# F2 §4.2: updateKpi 仅在 datasource_id 非 None 时校验
if dto.datasource_id is not None:
    ds = await session.get(DataSource, dto.datasource_id)
    if ds is None:
        raise ValidationError(MSG_KPI_DATASOURCE_NOT_FOUND.format(id=dto.datasource_id))
    if not ds.is_active:
        raise ValidationError(MSG_KPI_DATASOURCE_INACTIVE.format(id=ds.id, name=ds.name))
```

- [ ] **Step 6: 跑测试确认 PASS**

```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run pytest app/tests/integration/test_kpi_catalog_datasource_validation.py -v
```

Expected: 3 PASS（fixture 已由 Task 6 准备好；如 Task 6 未做完，至少 I1/I2 跑 fixture 缺失的错，OK）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/kpi_catalog_service.py backend/app/tests/integration/test_kpi_catalog_datasource_validation.py
git commit -m "feat(svc): KpiCatalogService.createKpi/updateKpi validate datasource (I1-I3)"
```

---

## Task 4: datasource_service.delete 引用计数 + I4-I5

**Files:**
- Modify: `backend/app/services/datasource_service.py:185-211` (`delete` 方法加引用计数)
- Create: `backend/app/tests/integration/test_datasource_delete_kpi_guard.py`

**Interfaces:**
- Consumes: `KpiCatalog` model, `MSG_DATASOURCE_HAS_KPI_BINDINGS` (from Task 2)
- Produces: 409 ConflictError on delete if KPI refs > 0

- [ ] **Step 1: 写 I4-I5 失败测试**

在 `backend/app/tests/integration/test_datasource_delete_kpi_guard.py` 写：

```python
"""I4-I5 datasource_service.delete 引用计数（per F2 spec §5.3）。"""
from __future__ import annotations
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.exceptions import ConflictError
from app.domain.models import DataSource, KpiCatalog
from app.services.datasource_service import DataSourceService
from app.services.messages_zh import MSG_DATASOURCE_HAS_KPI_BINDINGS


@pytest.mark.asyncio
async def test_deleteDatasource_with_no_kpi_refs_succeeds(
    dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    """I4: 无 KPI 引用 → 204 + DS 行消失。"""
    svc = DataSourceService()
    ds_id = seed_data_source.id
    await svc.delete(dbSession, ds_id)
    # Verify DS 行消失
    ds_after = await dbSession.get(DataSource, ds_id)
    assert ds_after is None


@pytest.mark.asyncio
async def test_deleteDatasource_with_kpi_ref_returns_409(
    dbSession: AsyncSession, seed_data_source: DataSource, sample_kpi: KpiCatalog,
) -> None:
    """I5: 有 1 个 KPI 引用 → 409 + MSG_DATASOURCE_HAS_KPI_BINDINGS + DS 行保留。"""
    svc = DataSourceService()
    ds_id = seed_data_source.id
    with pytest.raises(ConflictError) as exc_info:
        await svc.delete(dbSession, ds_id)
    assert MSG_DATASOURCE_HAS_KPI_BINDINGS.format(id=ds_id, count=1) in str(exc_info.value)
    # DS 行保留
    ds_after = await dbSession.get(DataSource, ds_id)
    assert ds_after is not None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run pytest app/tests/integration/test_datasource_delete_kpi_guard.py -v
```

Expected: 2 FAIL — I4 可能 PASS（因旧实现就直接 delete 没校验），I5 FAIL（应该被 409 拒但实际能删）。

- [ ] **Step 3: datasource_service.delete 加引用计数（最小实现）**

在 `backend/app/services/datasource_service.py:185-211` 的 `delete` 方法最开头加：

```python
# F2 §4.3: 删前查 KPI 引用计数，>0 抛 409
# 注意：delete 方法的参数名是 datasourceId（不是 id，会 shadow builtin）
from sqlalchemy import func, select
from app.domain.exceptions import ConflictError
from app.domain.models import KpiCatalog
from app.services.messages_zh import MSG_DATASOURCE_HAS_KPI_BINDINGS
ref_count_scalar = (
    await session.execute(
        select(func.count()).select_from(KpiCatalog).where(
            KpiCatalog.datasource_id == datasourceId
        )
    )
).scalar_one()
if ref_count_scalar > 0:
    raise ConflictError(
        MSG_DATASOURCE_HAS_KPI_BINDINGS.format(id=datasourceId, count=ref_count_scalar)
    )
```

> 把 imports 提到文件顶部（与已有 import 区合并），别内联在方法内。scalar_one() 调用 1 次存到变量。

- [ ] **Step 4: 跑测试确认 PASS**

```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run pytest app/tests/integration/test_datasource_delete_kpi_guard.py -v
```

Expected: 2 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/datasource_service.py backend/app/tests/integration/test_datasource_delete_kpi_guard.py
git commit -m "feat(svc): datasource_service.delete guards KPI refs (I4-I5, 409 on ref>0)"
```

---

## Task 5: FK 改 RESTRICT migration 0132

**Files:**
- Create: `backend/alembic/versions/0132_kpi_catalog_datasource_fk_restrict.py`

**Interfaces:**
- Consumes: `down_revision = "0131"` (current main head)
- Produces: `kpi_catalog.datasource_id` FK ondelete: SET NULL → RESTRICT

- [ ] **Step 1: 创建 migration 文件**

在 `backend/alembic/versions/0132_kpi_catalog_datasource_fk_restrict.py` 写：

```python
"""kpi_catalog.datasource_id FK 改 RESTRICT（F2 写入流收尾 — 2026-10-18 spec §4.5）。

原 0125 加 FK 时 ondelete=SET NULL（兼容历史 NULL 行）。
本 migration 改 RESTRICT：删 data_source 时若有 KPI 引用，DB 抛 FK violation。
应用层 datasource_service.delete 已先抛 409（per spec §4.3 + Task 4），RESTRICT 是兜底。

部署前自检（per spec §4.5）：
  SELECT count(*) FROM kpi_catalog WHERE datasource_id IS NOT NULL;
  -- 必须返回 0；否则本 migration 部署会因数据约束阻塞，需先 backfill
"""
from __future__ import annotations
from alembic import op


revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "kpi_catalog_datasource_id_fkey", "kpi_catalog", type_="foreignkey"
    )
    op.create_foreign_key(
        "kpi_catalog_datasource_id_fkey",
        "kpi_catalog", "data_source",
        ["datasource_id"], ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "kpi_catalog_datasource_id_fkey", "kpi_catalog", type_="foreignkey"
    )
    op.create_foreign_key(
        "kpi_catalog_datasource_id_fkey",
        "kpi_catalog", "data_source",
        ["datasource_id"], ["id"],
        ondelete="SET NULL",
    )
```

- [ ] **Step 2: 验证 migration 链连贯**

```bash
cd backend && uv run alembic history 2>&1 | tail -10
```

Expected: 0131 → 0132 链连贯。

- [ ] **Step 3: 在 test DB 跑 upgrade + downgrade 演练**

```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run alembic upgrade head 2>&1 | tail -5
```

Expected: `running upgrade 0131 -> 0132, ...` + 无错。

```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run alembic downgrade -1 2>&1 | tail -5
```

Expected: `running downgrade 0132 -> 0131, ...` + 无错。

```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run alembic upgrade head 2>&1 | tail -5
```

Expected: 回到 0132。

- [ ] **Step 4: 提交**

```bash
git add backend/alembic/versions/0132_kpi_catalog_datasource_fk_restrict.py
git commit -m "feat(alembic): migration 0132 kpi_catalog.datasource_id FK ondelete SET NULL -> RESTRICT (spec §4.5)"
```

---

## Task 6: 3 个 fixture + 既有测试 fixture 兼容（31 处）

**Files:**
- Modify: `backend/app/tests/integration/conftest.py` (加 `seed_data_source` / `seed_inactive_data_source` / `sample_kpi` 3 个 fixture)
- Modify: 10+ 既有 KpiCatalog 测试文件（31 处 KpiCatalog(...) 构造补 `datasource_id`）

**Interfaces:**
- Consumes: `DataSource` / `KpiCatalog` models, `MSG_KPI_*` constants
- Produces: 3 个新 fixture；既有测试 fixture 兼容

- [ ] **Step 1: 加 3 个 fixture 到 integration conftest**

在 `backend/app/tests/integration/conftest.py` 末尾追加：

```python
@pytest_asyncio.fixture
async def seed_data_source(dbSession: AsyncSession) -> DataSource:
    """F2 §5.4: 默认 is_active=true 的 DataSource fixture。"""
    ds = DataSource(
        name="TEST_DS_DEFAULT",
        type="mysql",
        host="x", port=3306,
        database_name="x", username="x",
        password_encrypted="x",
        is_active=True,
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


@pytest_asyncio.fixture
async def seed_inactive_data_source(dbSession: AsyncSession) -> DataSource:
    """F2 I2: is_active=false 的 DataSource fixture。"""
    ds = DataSource(
        name="INACTIVE_TEST_DS",
        type="mysql",
        host="x", port=3306,
        database_name="x", username="x",
        password_encrypted="x",
        is_active=False,
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


@pytest_asyncio.fixture
async def sample_kpi(dbSession: AsyncSession, seed_data_source: DataSource) -> KpiCatalog:
    """F2 I3: 绑定了 seed_data_source 的 KpiCatalog。"""
    from decimal import Decimal
    kpi = KpiCatalog(
        kpi_code="SAMPLE_KPI_FIXTURE",
        kpi_name="sample",
        business_definition="d",
        formula="SUM(x)",
        numerator="x",
        denominator=None,
        grain="g",
        unit="u",
        data_source="legacy_text",
        owner="o",
        version="1",  # model: Mapped[str]，不是 int
        # status 默认 KpiStatus.DRAFT（server_default 也是 DRAFT.value），不传
        metric_id=None,
        created_by="u",
        semantic_keywords=[],
        match_threshold=Decimal("0.5"),  # Numeric(3,2) → 必须 Decimal
        datasource_id=seed_data_source.id,
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi
```

加 imports（如未加）：
```python
from decimal import Decimal
from app.domain.models import DataSource, KpiCatalog
```

- [ ] **Step 2: 跑 Task 3 + Task 4 集成测试确认 fixture 通**

```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run pytest app/tests/integration/test_kpi_catalog_datasource_validation.py app/tests/integration/test_datasource_delete_kpi_guard.py -v
```

Expected: 5 PASS（I1 + I2 + I3 + I4 + I5）。

- [ ] **Step 3: 找出所有需要改的 KpiCatalog 构造点**

```bash
cd backend && grep -rn "KpiCatalog(" app/tests/ | head -40
```

完整 31 处列表（实施时按文件改）：

| 文件 | KpiCatalog 出现位置 |
|---|---|
| `tests/unit/test_audit_worker.py` | ~2 处 |
| `tests/unit/test_knowledge_compiler_service.py` | ~1 处 |
| `tests/unit/test_kpi_match_cache.py` | ~1 处 |
| `tests/unit/test_history_service.py` | ~2 处 |
| `tests/integration/test_evidence_metric_result.py` | ~2 处 |
| `tests/integration/test_seed_kpi_semantic_index.py` | ~3 处 |
| `tests/integration/test_stream_meta_single_frame.py` | ~2 处 |
| `tests/integration/test_research_adapters.py` | ~1 处 |
| `tests/integration/test_chat_l1_routing.py` | ~3 处 |
| `tests/integration/test_seed_kpi_catalog.py` | ~5 处 |
| 其他（按需） | ~9 处 |

**改法**：每个 KpiCatalog 构造点加 `datasource_id=<some_ds_id>`。最简方案：每个测试文件加一个文件级 fixture 提供一个默认 DS，然后所有 KpiCatalog 构造补 `datasource_id=default_ds.id`。

- [ ] **Step 4: 改 test_kpi_match_cache.py（最小示例）**

在 `backend/app/tests/unit/test_kpi_match_cache.py` 找到 KpiCatalog 构造，添加 `datasource_id=1`（or fixture 化）。如该文件有 conftest，加 `seed_data_source` fixture 引用即可。

- [ ] **Step 5: 改其余 9+ 文件（同样 pattern）**

对每个含 KpiCatalog 构造的测试文件，按 Step 4 pattern 改。**逐文件跑测试验证 0 回归**：

```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run pytest <modified_test_file> -v
```

Expected: 该文件 0 新失败（pre-existing 失败不变）。

- [ ] **Step 6: 跑全量 baseline 指纹（15 红 IDENTITY）**

```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run pytest \
  app/tests/unit/test_chat_service.py \
  app/tests/unit/test_chat_service_stream.py \
  app/tests/unit/test_chat_stream_meta_constants.py \
  app/tests/unit/test_kpi_catalog_dto.py \
  app/tests/integration/test_chat_api.py \
  app/tests/integration/test_chat_stream_api.py \
  app/tests/integration/test_chat_stream_query_state_scope.py \
  app/tests/integration/test_chat_stream_unanswerable.py \
  app/tests/integration/test_stream_meta_single_frame.py \
  app/tests/integration/test_preamble_concurrent_isolation.py \
  app/tests/integration/test_multi_step_persist_wiring.py \
  app/tests/integration/test_chat_agent_run.py \
  app/tests/integration/test_chat_supplier_360.py \
  app/tests/integration/test_chat_supplier_risk.py \
  app/tests/integration/test_kpi_catalog_datasource_validation.py \
  app/tests/integration/test_datasource_delete_kpi_guard.py \
  --tb=line -q 2>&1 | tail -25
```

Expected: 15 failed (同基线 fingerprint), 新测试 (U1-U3 + I1-I5 = 8 个) 全 PASS。

- [ ] **Step 7: 提交**

```bash
git add backend/app/tests/integration/conftest.py backend/app/tests/ -A
git commit -m "test: seed_data_source fixture + 31 处 KpiCatalog 构造补 datasource_id (spec §5.4)"
```

---

## Task 7: 同步 spec 偏差 + SSOT 状态变更

**Files:**
- Modify: `docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md` (spec 偏差 2 处同步)
- Modify: `Harness/changes/2026-10-18-kpi-datasource-write-flow-spec/summary.md` (状态 ✅)

**Interfaces:**
- Consumes: 实施后实际状态
- Produces: spec 与现实对齐，SSOT 关闭

- [ ] **Step 1: spec §4.5 改 migration 编号 0126 → 0132**

在 `docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md` §4.5 改：

- "新 migration `0126_kpi_catalog_datasource_fk_restrict.py`" → "新 migration `0132_kpi_catalog_datasource_fk_restrict.py`（spec 写时 0126 未占，落地时 0126 已被 `0126_session_token_usage_cost_source.py` 占，按 `qa-system-alembic-merge-number-collision` 改号到 main head 之后）"
- §3.1 / §3.2 / §3.3 / §3.4 / §4.5 等任何引用 0126 的地方全部更新
- §9 链接加 `0132_kpi_catalog_datasource_fk_restrict.py` (新增)

- [ ] **Step 2: spec §8.3 改 8 → 31**

在 `docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md` §8.3 改：

- "per `grep -rn \"KpiCatalog(\" backend/app/tests/` 实测至少 8 个测试文件需补 `datasource_id=<some DS>`" → "实测 31 处 KpiCatalog 构造需补 `datasource_id=<some DS>`，涉及 10+ 文件"

- [ ] **Step 3: SSOT summary 状态 ✅ + cross-ref**

在 `Harness/changes/2026-10-18-kpi-datasource-write-flow-spec/summary.md` 顶部加：

```markdown
> **状态**：✅ 已实施（PR #N）— 2026-10-18 6-task plan 完成
> **PR**：(待 PR 编号)
> **实施计划**：[docs/superpowers/plans/2026-10-18-f2-kpi-datasource-write-flow.md](../../docs/superpowers/plans/2026-10-18-f2-kpi-datasource-write-flow.md)
> **实施触发条件**（per spec §6）：
> - 用户/产品要求"自动反查"——未发生
> - 用户报障 ≥ 3 次"datasource 删除失败"——未发生
> - 用户报障 ≥ 3 次"chat 警告实际不触发"——未发生
> **主动实施原因**：F2 收口义务 + 写 spec 时本就该 1 步到位（避免 spec 长期挂着）
```

- [ ] **Step 4: 提交**

```bash
git add docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md Harness/changes/2026-10-18-kpi-datasource-write-flow-spec/summary.md
git commit -m "docs(spec): sync 2 deviations (migration #0126→0132, fixture impact 8→31) + SSOT close (spec §6 主动实施)"
```

---

## Task 8: code-reviewer 终审 + push + 开 PR

**Files:** (无代码改动)
- Modify: (PR description 由我写)

- [ ] **Step 1: 跑全量最终验证**

```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run pytest \
  app/tests/unit/test_kpi_catalog_dto.py \
  app/tests/integration/test_kpi_catalog_datasource_validation.py \
  app/tests/integration/test_datasource_delete_kpi_guard.py \
  --tb=short -v 2>&1 | tail -20
```

Expected: 8 PASS（U1-U3 + I1-I5）。

- [ ] **Step 2: 派 code-reviewer subagent 审 7 commits**

使用 general-purpose subagent，传递：
- BASE = `7ec77da2` (cherry-picked spec)
- HEAD = current `feat/f2-kpi-datasource-write-flow` tip
- 全量 diff（7 commits）

Reviewer 关注：
- Migration 0132 链连贯（不破坏 0131 → b1ea6cb 双 head）
- DTO 字段顺序（metric_id 之前）
- service 校验放置点（commit/flush 之前）
- 删除引用计数 race condition（虽然 spec 不要求并发安全）
- fixture 影响是否漏改
- spec 偏差是否完整同步

- [ ] **Step 3: 修 critical/high findings（若有）**

按 reviewer 反馈修，重复 Step 1 验证。

- [ ] **Step 4: push + 开 PR**

```bash
cd /private/tmp/qa-f2-kpi-datasource
git push -u origin feat/f2-kpi-datasource-write-flow
gh pr create --base main --title "feat(kpi): F2 datasource_id 接入写入流 (DTO 必填 + DS 删除 409 + FK RESTRICT)" --body "$(cat <<'EOF'
## 背景

F2 (1.2a ba3303e + 1.2b 188cf84 + 1.2c 7b0544c) 已 merged PR #18，列 + FK + 后端比较 + 前端 i18n 查表 + 守卫查表 + 反向断言全部就绪。但**全表 datasource_id=NULL** ⇒ chat 路径 L1 datasource-mismatch 警告实际不触发。

本 PR 落定"接入写入流"的设计边界（per [spec §4](../../docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md)）：
- DTO 必填：`KpiCatalogCreate.datasource_id: int` 必填 + `KpiCatalogUpdate.datasource_id: int | None` 显式 unbind
- service 校验：datasource 存在 + is_active=true
- FK 改 RESTRICT：migration 0132（spec 写 0126 但被占，按 alembic-merge-number-collision 改号）
- DS 删除拒引用：datasource_service.delete 加 409 ConflictError
- 4 个新错误码（`MSG_KPI_DATASOURCE_*` / `MSG_DATASOURCE_HAS_KPI_BINDINGS`）

## 改动

7 commits on `feat/f2-kpi-datasource-write-flow`:
1. `feat(schemas):` KpiCatalogCreate/Update 加 datasource_id
2. `feat(messages):` 4 个错误码
3. `feat(svc):` KpiCatalogService 校验（I1-I3）
4. `feat(svc):` datasource_service.delete 引用计数（I4-I5）
5. `feat(alembic):` migration 0132 FK ondelete RESTRICT
6. `test:` 3 个 fixture + 31 处 KpiCatalog 构造补 datasource_id
7. `docs(spec):` 同步 2 偏差 + SSOT 关闭

## 验证

8 用例（U1-U3 + I1-I5）全 PASS on TEST_DATABASE_URL 5433/qa_metadata_test_solo：
```bash
cd backend && export TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test_solo" && uv run pytest \
  app/tests/unit/test_kpi_catalog_dto.py \
  app/tests/integration/test_kpi_catalog_datasource_validation.py \
  app/tests/integration/test_datasource_delete_kpi_guard.py \
  -v
# 8 passed
```

基线 IDENTITY：15 failed (pre-existing) + 247 + 8 new = 270 passed.

## 关联

- Spec: [docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md](../../docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md)
- SSOT 收口: [Harness/changes/2026-10-18-kpi-datasource-write-flow-spec/summary.md](../../Harness/changes/2026-10-18-kpi-datasource-write-flow-spec/summary.md)
- F2 phase01 SDD: [F2 AIChat 生产缺陷 phase01](../../.superpowers/sdd/2026-10-14-aichat-phase01-implementation/progress.md)
- 实施计划: [docs/superpowers/plans/2026-10-18-f2-kpi-datasource-write-flow.md](../../docs/superpowers/plans/2026-10-18-f2-kpi-datasource-write-flow.md)
- 部署前自检: `SELECT count(*) FROM kpi_catalog WHERE datasource_id IS NOT NULL;` 必须返回 0
EOF
)"
```

- [ ] **Step 5: 等用户审 PR**

不主动 merge。PR 等用户/peer review。

---

## Self-Review（plan 作者自检）

1. **Spec coverage**：
   - §4.1 DTO → Task 1 ✓
   - §4.2 service 校验 → Task 3 ✓
   - §4.3 DS 删除 → Task 4 ✓
   - §4.4 错误码 → Task 2 ✓
   - §4.5 FK RESTRICT → Task 5 ✓（含 spec 偏差：编号 0126→0132）
   - §4.6 历史 NULL docstring → Task 3 Step 3 ✓
   - §5.1 U1-U3 → Task 1 ✓
   - §5.2 I1-I3 → Task 3 ✓
   - §5.3 I4-I5 → Task 4 ✓
   - §5.4 fixture → Task 6 ✓
   - §6 SSOT 关闭 → Task 7 ✓
   - §7 实施计划 → 本 plan ✓

2. **Placeholder scan**：
   - "TBD" / "TODO" / "implement later" / "类似 to Task N" / "fill in details" — 0 命中 ✓
   - 完整代码块无省略 ✓
   - 每个 step 有具体命令 ✓

3. **Type consistency**：
   - `KpiCatalogCreate.datasource_id: int` (Task 1) ↔ service 校验 `dto.datasource_id` (Task 3) ✓
   - `KpiCatalogUpdate.datasource_id: int | None` (Task 1) ↔ `if dto.datasource_id is not None` (Task 3) ✓
   - `MSG_KPI_DATASOURCE_NOT_FOUND` (Task 2) ↔ service raise (Task 3) ↔ test 断言 (Task 3 test) ✓
   - `MSG_DATASOURCE_HAS_KPI_BINDINGS` (Task 2) ↔ DS delete 抛 (Task 4) ↔ test 断言 (Task 4 test) ✓
   - `seed_data_source` / `sample_kpi` fixture (Task 6) ↔ test 用法 (Task 3/4 test) ✓

4. **风险显式承认**：
   - Migration 编号 0126→0132（plan 全程 + spec §4.5 同步）
   - Fixture 影响 8→31（plan 全程 + spec §8.3 同步）
   - 2 个 alembic head (0131 + b1ea6cb32588) — 本 plan 选 0131 链，b1ea6cb 链不动。后续若有 rebase 需小心。

5. **Per harness TDD 严格**：每 task RED→GREEN→IMPROVE 流程显式 ✓

6. **拒绝 pre-existing 红伪装**：Task 6 Step 6 显式对比 15-failure baseline IDENTITY ✓
