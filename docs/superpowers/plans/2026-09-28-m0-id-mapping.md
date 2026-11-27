# M0-P0.1 统一 ID 映射中枢（id_mapping）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立三库统一 ID 的映射中枢 `id_mapping` 表：alembic 0095 建表 + 领域格式 SSOT + `IdMappingService` + 4 个 REST 端点 + 从 `ontology_class` 回填类级映射，让 Neo4j 节点 / Milvus 向量后续能挂到同一个 `unified_id`。

**Architecture:** 新表 `id_mapping`（`unified_id` 字符串主键 + `(business_object, external_id)` 唯一约束）承载**双层**映射：**类级**（回填自 `ontology_class`，`business_object = class_name`、`external_id = str(class_id)`，例 `obj:Supplier:4`）为 A2/A4 的 Neo4j/本体类改造提供统一 ID；**实例级**（`business_object = business_object.code`、`external_id = enterprise_code`，例 `obj:SUPPLIER:SUP000001`）由 A4 写路径运行时注册。统一 ID 的构造/反解是 `app/domain/id_mapping.py` 中的纯函数 SSOT，客户端只传 `businessObject` + `externalId`，`unifiedId` 一律服务端派生（防止客户端自造 ID 破坏三库对齐）。A1 只碰 PG，不碰 Neo4j / Milvus。

**Tech Stack:** Python 3.14、FastAPI、SQLAlchemy 2.0 async（`Mapped[]` 风格）、PostgreSQL 16（宿主端口 **5433**）、Alembic、pytest（`asyncio_mode = "auto"`）、真实 PG 集成测试（`qa_metadata_test`）。

## Global Constraints

- 本计划只交付 `plan-person-a.md` 的 **A1（M0-P0.1）**。A2/A3/A4/A5/A6/A7/A8 **不在本次范围**。
- 分支：从当前 HEAD 切 `epic/v31-upgrade`，再切 `feat/m0-unified-id`（main 已与 HEAD 分叉：main 领先 5 提交、HEAD 领先 110 提交，取 HEAD 保住存量能力）。
- 后端 Python：函数/变量 `camelCase`；类型/组件 `PascalCase`；常量 `UPPER_SNAKE_CASE`；**ORM / Pydantic 字段 `snake_case`**（与 DB 列及 JSON 契约一致，刻意偏离 PEP 8）。
- 文件 ≤ 800 行；函数 ≤ 50 行；嵌套 ≤ 4 层；不可变（构造新对象，禁止原地改）；显式错误处理。
- 提交格式 `<type>: <description>`，**无** `Co-Authored-By:` trailer。类型限 feat / fix / refactor / docs / test / chore / perf / ci。
- 测试库 URL：`postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test`（**5433，不是 5432**）。
- 后端测试必须真实 PG + 完整 API 链路（`_pg_support.pgApiClient`），**禁止 sqlite 内存库 / 直接调 service 当集成测试**。
- `alembic/env.py` **只读 `DATABASE_URL`**（`TEST_DATABASE_URL` 被静默忽略）⇒ 任何 shell 出去跑 alembic 的地方必须先过库名闸（`_assertTestDb`）。
- 覆盖门禁：`uv run pytest app/tests/ --cov=app --cov-fail-under=80`。
- alembic 链：`0094_wiki_page_category_id`（当前 head，已核 prod/测试库一致）→ **`0095_id_mapping`**。
- 本计划**已确认的偏离**（执行前需用户知悉，见 §偏离记录）：唯一约束冲突返回 **409**（`ConflictError`）而非计划书写的 422；回填默认**只回填存活类**（`valid_to IS NULL`）而非全部 52 行。

---

## 偏离记录（源自 plan-person-a.md 与代码现实的差异）

| # | 计划书原文 | 实际实现 | 依据 |
|---|---|---|---|
| D1 | 「冲突唯一约束 **422**」 | **409**（`ConflictError`） | `app/domain/exceptions.py::statusForError` 把 `ConflictError` → 409、`ValidationError` → 422；`exceptions.py` 明确写 `ConflictError` 就是「资源冲突（唯一约束 / 业务规则不允许重复等）」。全库所有唯一性守卫（`business_object_service.createObject`、`feature_rule` 等）一律 409，无一例外。写 422 需新造一个语义错位的异常类型。 |
| D2 | 「回填后 `count(*)` 与 `ontology_class` **数对平**」 | 默认对平 **存活类**：`count(*) == (SELECT count(*) FROM ontology_class WHERE valid_to IS NULL)` = **32**（当前 prod）。另外提供 `--include-deleted` 覆盖全部 52 行。 | 52 行含 20 行软删墓碑（`deleteClass` 把 `valid_to` 设为 now()，见 `OntologyClass` docstring）。给墓碑发统一 ID 会让 A2「27 类全部有 unified_id」验收口径含糊。 |
| D3 | 「alembic 0097 `knowledge_claim.source_version`」编号 | 本计划不涉及（A5 的活），0096 留给乙。A1 只占 **0095** | `README.md` §3 代码边界 |

---

## File Structure

### 新增文件

| 文件 | 责任 |
|---|---|
| `backend/app/domain/id_mapping.py` | 统一 ID 领域 SSOT：`buildUnifiedId` / `parseUnifiedId` 纯函数 + `IdMapping` ORM 模型 |
| `backend/alembic/versions/0095_id_mapping.py` | 建 `id_mapping` 表（唯一约束 + 2 索引），纯新增不触存量 |
| `backend/app/services/id_mapping_service.py` | `register` / `resolve` / `resolveByUnifiedId` / `listMappings` |
| `backend/app/api/v1/id_mapping.py` | 4 个 REST 端点（`POST` + `GET` 列表 + `GET /resolve` + `GET /{unifiedId}`） |
| `backend/scripts/backfill_id_mapping.py` | 从 `ontology_class` 回填类级映射（幂等 + 可 import 供测试直调） |
| `backend/app/tests/unit/test_id_mapping_format.py` | 统一 ID 构造/反解单测（含非法输入） |
| `backend/app/tests/integration/test_alembic_0095.py` | 迁移落库 + downgrade 双向对称（复用 0088/0091 测试的写库闸） |
| `backend/app/tests/integration/test_id_mapping_api.py` | 4 端点全链路 + 冲突 + 404 + 参数校验，含**路由顺序守卫** |
| `backend/app/tests/integration/test_backfill_id_mapping.py` | 回填对平 + 幂等 + 墓碑排除 |

### 修改文件

| 文件 | 改动 |
|---|---|
| `backend/app/domain/models.py:2013-2030` | 底部追加 `from app.domain.id_mapping import IdMapping  # noqa: E402,F401`（注册进 metadata，与 `wiki_models` 同模式） |
| `backend/app/domain/schemas.py` | 末尾新增 `IdMappingCreate` / `IdMappingRead` 两 DTO |
| `backend/app/services/messages_zh.py` | 末尾新增 3 个 `MSG_ID_MAPPING_*` 常量 |
| `backend/app/main.py:~395-413` | 局部 import 块加 `id_mapping`；health 之前加 `app.include_router(...)` |
| `backend/app/tests/_testapp.py` | 同步挂载同一路由（守卫见 `test_app_wiring.py`：`_testapp` 与 `main.py` 是平行手工维护的两套 wiring，漏一边则测试全绿而线上 404） |

---

## Task 0: 分支准备

**Files:** 无（仅 git）

**Interfaces:**
- Consumes: 无
- Produces: 分支 `feat/m0-unified-id`，后续所有任务的提交落在此

- [ ] **Step 1: 确认工作区状态，避免把无关改动带进新分支**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git status --short
```

预期：`M Harness/agents/owner.md` 与若干 `??` 未跟踪项。**这些都不属于 A1**，不要提交它们。

- [ ] **Step 2: 从当前 HEAD 切 epic 分支，再切任务分支**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git checkout -b epic/v31-upgrade HEAD
git checkout -b feat/m0-unified-id epic/v31-upgrade
git branch --show-current
```

预期输出：`feat/m0-unified-id`

- [ ] **Step 3: 记录基线，供后续 diff 自检**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git rev-parse HEAD > /tmp/a1-baseline-sha.txt
cat /tmp/a1-baseline-sha.txt
```

---

## Task 1: 统一 ID 领域模块（格式 SSOT + ORM 模型）

**Files:**
- Create: `backend/app/domain/id_mapping.py`
- Modify: `backend/app/domain/models.py:2030`（文件末尾，紧跟 `MenuConfig` 导入之后）
- Test: `backend/app/tests/unit/test_id_mapping_format.py`

**Interfaces:**
- Consumes: `app.domain.models.Base`、`app.domain.models.TimestampMixin`、`app.domain.exceptions.ValidationError`
- Produces:
  - `UNIFIED_ID_PREFIX: str = "obj"`
  - `UNIFIED_ID_SEPARATOR: str = ":"`
  - `UNIFIED_ID_MAX_LENGTH: int = 512`
  - `buildUnifiedId(businessObject: str, externalId: str) -> str`
  - `parseUnifiedId(unifiedId: str) -> tuple[str, str]`
  - `class IdMapping(Base, TimestampMixin)`，字段 `unified_id`（主键）/ `business_object` / `external_id` / `pg_table` / `pg_id` / `neo4j_node_id` / `milvus_collection` / `milvus_id` / `created_time` / `updated_time`

- [ ] **Step 1: 写失败的测试**

创建 `backend/app/tests/unit/test_id_mapping_format.py`：

```python
"""统一 ID 格式 SSOT 单测（M0-P0.1）。

纯函数，无需 DB：校验构造 / 反解 / 分隔符保护 / 长度上限 / 往返一致。
"""
from __future__ import annotations

import pytest

from app.domain.exceptions import ValidationError
from app.domain.id_mapping import (
    UNIFIED_ID_MAX_LENGTH,
    buildUnifiedId,
    parseUnifiedId,
)


class TestBuildUnifiedId:
    def test_builds_class_level_id(self) -> None:
        """类级（回填自 ontology_class）：class_name + str(class_id)。"""
        assert buildUnifiedId("Supplier", "4") == "obj:Supplier:4"

    def test_builds_instance_level_id(self) -> None:
        """实例级（A4 运行时注册）：business_object.code + enterprise_code。"""
        assert buildUnifiedId("SUPPLIER", "SUP000001") == "obj:SUPPLIER:SUP000001"

    def test_strips_surrounding_whitespace(self) -> None:
        assert buildUnifiedId("  Supplier ", " 4 ") == "obj:Supplier:4"

    def test_rejects_empty_business_object(self) -> None:
        with pytest.raises(ValidationError):
            buildUnifiedId("", "4")

    def test_rejects_empty_external_id(self) -> None:
        with pytest.raises(ValidationError):
            buildUnifiedId("Supplier", "")

    def test_rejects_whitespace_only_segments(self) -> None:
        with pytest.raises(ValidationError):
            buildUnifiedId("   ", "4")

    def test_rejects_separator_in_business_object(self) -> None:
        """分隔符是格式的一部分：段内再出现 ':' 会让反解二义。"""
        with pytest.raises(ValidationError):
            buildUnifiedId("Sup:plier", "4")

    def test_rejects_separator_in_external_id(self) -> None:
        with pytest.raises(ValidationError):
            buildUnifiedId("Supplier", "C:10001")

    def test_rejects_overlong_result(self) -> None:
        with pytest.raises(ValidationError):
            buildUnifiedId("S" * 100, "E" * 500)


class TestParseUnifiedId:
    def test_round_trip_class_level(self) -> None:
        assert parseUnifiedId("obj:Supplier:4") == ("Supplier", "4")

    def test_round_trip_instance_level(self) -> None:
        assert parseUnifiedId("obj:SUPPLIER:SUP000001") == ("SUPPLIER", "SUP000001")

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "Supplier:4",
            "obj:Supplier",
            "obj::4",
            "obj:Supplier:",
            "cls:Supplier:4",
            "obj:Sup:plier:4",
        ],
    )
    def test_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            parseUnifiedId(bad)


class TestMaxLength:
    def test_max_length_matches_column_budget(self) -> None:
        """512 = 'obj:'(4) + business_object(100) + ':'(1) + external_id(200) 之上留余量。"""
        assert UNIFIED_ID_MAX_LENGTH == 512
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
uv run pytest app/tests/unit/test_id_mapping_format.py -v
```

预期：`ModuleNotFoundError: No module named 'app.domain.id_mapping'`

- [ ] **Step 3: 写最小实现**

创建 `backend/app/domain/id_mapping.py`：

```python
"""统一 ID（M0 / P0.1）领域定义 + `id_mapping` ORM 映射表。

统一 ID 格式（v3.1 蓝图 §17.1）：

    unified_id = "obj:{business_object}:{external_id}"

本表承载**两层**映射，用 ``business_object`` 作命名空间区分：

- **类级**（A1 回填自 ``ontology_class``）：``business_object = class_name``、
  ``external_id = str(ontology_class.id)``，例 ``obj:Supplier:4``。
  用途：A2 给 Neo4j ``Class`` / ``Property`` 节点挂统一 ID、A4 让
  ``ontology_service`` 三库同步写同一个 ID。
- **实例级**（A4 运行时 ``register``）：``business_object = business_object.code``、
  ``external_id = enterprise_code``，例 ``obj:SUPPLIER:SUP000001``。
  用途：业务实例（供应商 / 物料 / 采购订单）跨三库可寻址。

``':'`` 是格式的一部分，故 ``business_object`` / ``external_id`` 自身不得含 ``':'``
——否则 ``parseUnifiedId`` 二义，统一 ID 就不再是稳定主键。

**unifiedId 一律服务端派生**：客户端只传 ``businessObject`` + ``externalId``
（见 ``IdMappingCreate``），不允许客户端自带 ``unifiedId`` —— 否则三库对齐
的前提（同一个 ID 生成规则）就交给了调用方。
"""

from __future__ import annotations

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.exceptions import ValidationError
from app.domain.models import Base, TimestampMixin

UNIFIED_ID_PREFIX = "obj"
UNIFIED_ID_SEPARATOR = ":"

# 512 = len("obj:") + 100(business_object) + len(":") + 200(external_id) = 305，留余量。
# 同时是 unified_id 主键列宽；PG btree 索引上限 ~2704 字节，512 字符远在界内。
UNIFIED_ID_MAX_LENGTH = 512

_BUSINESS_OBJECT_MAX_LENGTH = 100
_EXTERNAL_ID_MAX_LENGTH = 200

MSG_UNIFIED_ID_FORMAT_INVALID = (
    "统一 ID 格式非法：应为 'obj:{业务对象}:{外部ID}'，且两段均非空"
)
MSG_UNIFIED_ID_SEPARATOR_FORBIDDEN = (
    "统一 ID 的业务对象 / 外部 ID 不允许包含 ':'（该字符是格式分隔符）"
)


def buildUnifiedId(businessObject: str, externalId: str) -> str:
    """构造统一 ID。

    抛 ``ValidationError``（→ 422）当且仅当：任一段去空白后为空、任一段含
    ``':'``、或结果超过 ``UNIFIED_ID_MAX_LENGTH``。
    """
    normalizedObject = (businessObject or "").strip()
    normalizedExternal = (externalId or "").strip()
    if not normalizedObject or not normalizedExternal:
        raise ValidationError(MSG_UNIFIED_ID_FORMAT_INVALID)
    if len(normalizedObject) > _BUSINESS_OBJECT_MAX_LENGTH or len(
        normalizedExternal
    ) > _EXTERNAL_ID_MAX_LENGTH:
        raise ValidationError(MSG_UNIFIED_ID_FORMAT_INVALID)
    if (
        UNIFIED_ID_SEPARATOR in normalizedObject
        or UNIFIED_ID_SEPARATOR in normalizedExternal
    ):
        raise ValidationError(MSG_UNIFIED_ID_SEPARATOR_FORBIDDEN)
    unifiedId = (
        f"{UNIFIED_ID_PREFIX}{UNIFIED_ID_SEPARATOR}"
        f"{normalizedObject}{UNIFIED_ID_SEPARATOR}{normalizedExternal}"
    )
    if len(unifiedId) > UNIFIED_ID_MAX_LENGTH:
        raise ValidationError(MSG_UNIFIED_ID_FORMAT_INVALID)
    return unifiedId


def parseUnifiedId(unifiedId: str) -> tuple[str, str]:
    """反解统一 ID → ``(businessObject, externalId)``；格式非法抛 ``ValidationError``。"""
    parts = (unifiedId or "").split(UNIFIED_ID_SEPARATOR)
    if (
        len(parts) != 3
        or parts[0] != UNIFIED_ID_PREFIX
        or not parts[1]
        or not parts[2]
    ):
        raise ValidationError(MSG_UNIFIED_ID_FORMAT_INVALID)
    return parts[1], parts[2]


class IdMapping(Base, TimestampMixin):
    """统一 ID 映射中枢（M0 / P0.1）。

    一行 = 一个统一 ID 及其在三库中的落点。``pg_table`` / ``pg_id`` 由 A1 回填，
    ``neo4j_node_id`` / ``milvus_collection`` / ``milvus_id`` 由 A2 / A3 回填
    （A1 故意留 NULL，不假装知道图与向量的落点）。

    ``(business_object, external_id)`` 唯一：同一业务键只允许一条映射，这是
    「统一 ID 稳定」的 DB 侧兜底；service 层先查后插，DB 约束负责并发兜底。
    """

    __tablename__ = "id_mapping"

    unified_id: Mapped[str] = mapped_column(
        String(UNIFIED_ID_MAX_LENGTH), primary_key=True
    )
    business_object: Mapped[str] = mapped_column(String(100), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    # PG 侧落点（A1 回填：ontology_class）
    pg_table: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pg_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 图侧落点（A2 回填）
    neo4j_node_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 向量侧落点（A3 回填）
    milvus_collection: Mapped[str | None] = mapped_column(String(100), nullable=True)
    milvus_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "business_object", "external_id", name="uq_id_mapping_bo_ext"
        ),
        Index("ix_id_mapping_business_object", "business_object"),
        Index("ix_id_mapping_pg", "pg_table", "pg_id"),
    )

    def __repr__(self) -> str:
        return f"<IdMapping {self.unified_id} pg={self.pg_table}/{self.pg_id}>"
```

- [ ] **Step 4: 在 models.py 末尾注册模型**

修改 `backend/app/domain/models.py`，找到文件最后一段（当前是 `MenuConfig` 的 re-export，约 2028-2030 行）：

```python
# Re-export MenuConfig so Alembic autogenerate picks it up.
from app.models.menu_config import MenuConfig  # noqa: E402,F401
```

**在其后追加**：

```python

# Re-export 统一 ID 映射表（0095 migration，M0-P0.1）。独立成文件的原因与
# wiki_models 相同：models.py 已 2100+ 行，且 id_mapping 自带格式纯函数，
# 放在一起才是一份完整的「统一 ID 是什么」的 SSOT。
from app.domain.id_mapping import IdMapping  # noqa: E402,F401
```

- [ ] **Step 5: 跑测试确认通过 + 确认模型已注册进 metadata**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
uv run pytest app/tests/unit/test_id_mapping_format.py -v
uv run python -c "
import app.domain.models as m
assert 'id_mapping' in m.Base.metadata.tables, '模型未注册进 metadata'
print('registered:', sorted(m.Base.metadata.tables['id_mapping'].columns.keys()))
print('unique:', [c.name for c in m.Base.metadata.tables['id_mapping'].constraints])
"
```

预期：全部 PASS；第二条命令打印 `registered: [...]` 含 8 个业务列 + 2 个时间列，`unique` 含 `uq_id_mapping_bo_ext`。

- [ ] **Step 6: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/domain/id_mapping.py backend/app/domain/models.py backend/app/tests/unit/test_id_mapping_format.py
git commit -m "feat(id-mapping): 统一 ID 领域 SSOT + id_mapping ORM 模型（M0-P0.1）"
```

---

## Task 2: Alembic 0095 迁移

**Files:**
- Create: `backend/alembic/versions/0095_id_mapping.py`
- Test: `backend/app/tests/integration/test_alembic_0095.py`

**Interfaces:**
- Consumes: Task 1 的 `IdMapping`（用于 ORM↔DDL 一致性断言）
- Produces: 实测库表 `id_mapping`，`alembic_version = 0095_id_mapping`

- [ ] **Step 1: 写失败的测试**

创建 `backend/app/tests/integration/test_alembic_0095.py`（**写库闸**照抄 0091 测试：`alembic/env.py` 只读 `DATABASE_URL`，传错就打 prod）：

```python
"""验证 Alembic 0095：`id_mapping` 表落库（M0-P0.1）。

契约：
- upgrade head 后真实 PG 上存在 `id_mapping`，且列集合 / 唯一约束与 ORM 一致。
- downgrade 0094 后表被撤，再 upgrade head 恢复 —— 双向对称。

为什么不抄 DDL 重放：抄写本身就可能抄错，「跑完库里剩什么」只有真跑 alembic 才知道。
代价是会短暂改动测试库 `alembic_version`，故降级-升级循环由 try/finally 兜底复原。

安全闸：`alembic/env.py` 只读 `DATABASE_URL`（`TEST_DATABASE_URL` 被静默忽略），
传错就打到 **prod**。故 shell 出去之前强制校验库名，不匹配直接失败、不执行任何 DDL。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests._pg_support import resolveTestDatabaseUrl

_BACKEND_ROOT = Path(__file__).resolve().parents[3]  # backend/
_REQUIRED_DB = "qa_metadata_test"

_REVISION = "0095_id_mapping"
_PREVIOUS = "0094_wiki_page_category_id"
_TABLE = "id_mapping"

_ALEMBIC_TIMEOUT_SEC = 120

_EXPECTED_COLUMNS = {
    "unified_id",
    "business_object",
    "external_id",
    "pg_table",
    "pg_id",
    "neo4j_node_id",
    "milvus_collection",
    "milvus_id",
    "created_time",
    "updated_time",
}


def _assertTestDb(url: str) -> None:
    """写库闸：只认测试库，否则 raise（不执行任何 DDL）。"""
    match = re.search(r"/([^/?]+)(?:\?|$)", url)
    dbName = match.group(1) if match else ""
    if dbName != _REQUIRED_DB:
        raise RuntimeError(
            f"拒绝对非测试库执行迁移：解析到库名 {dbName!r}，要求 {_REQUIRED_DB!r}。"
            "alembic/env.py 只读 DATABASE_URL（TEST_DATABASE_URL 被静默忽略），"
            "传错就会打到 prod。"
        )


def _alembic(url: str, *args: str) -> str:
    """在测试库上跑一条 alembic 子命令；先过写库闸。"""
    _assertTestDb(url)
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_ALEMBIC_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} 失败（rc={result.returncode}）：{result.stderr[-2000:]}"
    )
    return result.stdout


async def _tableNames(dbSession: AsyncSession) -> set[str]:
    """读真实 PG：当前 schema 的所有表名（alembic 跨连接 DDL 必须 rollback 刷快照）。"""
    await dbSession.rollback()

    def _sync_inspect(sync_session) -> set[str]:
        return set(inspect(sync_session.bind).get_table_names())

    return await dbSession.run_sync(_sync_inspect)


async def _idMappingColumns(dbSession: AsyncSession) -> set[str]:
    """读真实 PG：id_mapping 的实际列集合（同样要先 rollback）。"""
    await dbSession.rollback()

    def _sync_inspect(sync_session) -> set[str]:
        columns = inspect(sync_session.bind).get_columns(_TABLE)
        return {c["name"] for c in columns}

    return await dbSession.run_sync(_sync_inspect)


@pytest.mark.asyncio
async def test_0095_creates_id_mapping_table_with_expected_columns(
    dbSession: AsyncSession,
) -> None:
    """upgrade head 后 `id_mapping` 实际落库，且列集合与 ORM 声明一致。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    # Arrange：升级到 head（含本迁移的最终状态）
    _alembic(url, "upgrade", "head")

    # Act
    tables = await _tableNames(dbSession)

    # Assert：先断表存在，再断列集合 —— 顺序反了会先抛 NoSuchTableError，
    # 失败信息从「迁移未生效」退化成一句 inspect 内部异常
    assert _TABLE in tables, f"upgrade head 后 {_TABLE!r} 表不存在 —— 0095 迁移未生效"
    columns = await _idMappingColumns(dbSession)
    assert columns == _EXPECTED_COLUMNS, (
        f"{_TABLE} 列集合与 ORM 声明不一致：多 {columns - _EXPECTED_COLUMNS}，"
        f"缺 {_EXPECTED_COLUMNS - columns}"
    )


@pytest.mark.asyncio
async def test_0095_unique_constraint_rejects_duplicate_business_key(
    dbSession: AsyncSession,
) -> None:
    """唯一约束 `(business_object, external_id)` 由 DB 真实兜底（不靠 service 自觉）。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)
    _alembic(url, "upgrade", "head")

    insert = text(
        "INSERT INTO id_mapping "
        "(unified_id, business_object, external_id, created_time, updated_time) "
        "VALUES (:uid, :bo, :ext, now(), now())"
    )
    await dbSession.execute(
        insert, {"uid": "obj:Supplier:4", "bo": "Supplier", "ext": "4"}
    )
    await dbSession.commit()

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        # 同 (business_object, external_id)、不同 unified_id ⇒ 必撞唯一约束
        await dbSession.execute(
            insert, {"uid": "obj:Supplier:4-dup", "bo": "Supplier", "ext": "4"}
        )
        await dbSession.commit()
    await dbSession.rollback()

    # cleanup：本用例直接写了表，避免污染同进程后续用例
    await dbSession.execute(text("DELETE FROM id_mapping WHERE business_object = 'Supplier'"))
    await dbSession.commit()


@pytest.mark.asyncio
async def test_0095_downgrade_removes_table_and_upgrade_restores(
    dbSession: AsyncSession,
) -> None:
    """双向对称：downgrade 0094 后表被撤，再 upgrade head 复原（finally）。"""
    url = resolveTestDatabaseUrl()
    _assertTestDb(url)

    _alembic(url, "upgrade", "head")
    before = await _tableNames(dbSession)
    assert _TABLE in before, "起点不在 head：id_mapping 不在测试库"

    try:
        # Act：降级到 0094
        _alembic(url, "downgrade", _PREVIOUS)
        after = await _tableNames(dbSession)
        version = (
            await dbSession.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()

        # Assert
        assert version == _PREVIOUS, (
            f"downgrade 后 alembic_version = {version!r}，期望 {_PREVIOUS!r}"
        )
        assert _TABLE not in after, (
            f"downgrade 0095 之后 {_TABLE!r} 仍存在 —— 降级没有对称反转 upgrade"
        )
    finally:
        # 无论如何恢复测试库到 head，避免污染后续测试
        _alembic(url, "upgrade", "head")

    restored = await _tableNames(dbSession)
    assert _TABLE in restored, "升级回来后 id_mapping 没恢复 —— 本测试把测试库留在了坏状态"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  uv run pytest app/tests/integration/test_alembic_0095.py -v
```

预期：`test_0095_creates_id_mapping_table_with_expected_columns` FAIL，`assert 'id_mapping' in tables` 不成立（`inspect.get_columns('id_mapping')` 会抛 `NoSuchTableError`，同样算失败）。

- [ ] **Step 3: 写迁移**

创建 `backend/alembic/versions/0095_id_mapping.py`：

```python
"""id_mapping 统一 ID 映射中枢（M0 / P0.1）。

新建 id_mapping 表：unified_id 字符串主键 + (business_object, external_id) 唯一约束
+ 2 个查询索引（按业务对象列；按 PG 落点反查）。纯新增表，不动任何存量数据，
故无需 <表>_<YYYYMMDD> 备份（两库使用策略里那条只针对 ALTER 型迁移）。

A2/A3 会往 neo4j_node_id / milvus_* 三列回填，本迁移先把它们建出来（可空）。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0095_id_mapping"
down_revision = "0094_wiki_page_category_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "id_mapping",
        sa.Column("unified_id", sa.String(length=512), primary_key=True),
        sa.Column("business_object", sa.String(length=100), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("pg_table", sa.String(length=100), nullable=True),
        sa.Column("pg_id", sa.String(length=100), nullable=True),
        sa.Column("neo4j_node_id", sa.String(length=200), nullable=True),
        sa.Column("milvus_collection", sa.String(length=100), nullable=True),
        sa.Column("milvus_id", sa.String(length=200), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "business_object", "external_id", name="uq_id_mapping_bo_ext"
        ),
    )
    op.create_index(
        "ix_id_mapping_business_object", "id_mapping", ["business_object"]
    )
    op.create_index("ix_id_mapping_pg", "id_mapping", ["pg_table", "pg_id"])


def downgrade() -> None:
    op.drop_index("ix_id_mapping_pg", table_name="id_mapping")
    op.drop_index("ix_id_mapping_business_object", table_name="id_mapping")
    op.drop_table("id_mapping")
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  uv run pytest app/tests/integration/test_alembic_0095.py -v
```

预期：3 个用例全 PASS。

- [ ] **Step 5: 确认迁移链无分支（多 head 会让 alembic 直接报错）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  uv run alembic heads
```

预期：`0095_id_mapping (head)` —— 恰好一行。

- [ ] **Step 6: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/alembic/versions/0095_id_mapping.py backend/app/tests/integration/test_alembic_0095.py
git commit -m "feat(id-mapping): alembic 0095 建 id_mapping 表 + 迁移双向对称测试"
```

---

## Task 3: DTO + IdMappingService

**Files:**
- Modify: `backend/app/domain/schemas.py`（文件末尾新增一节）
- Modify: `backend/app/services/messages_zh.py`（文件末尾追加）
- Create: `backend/app/services/id_mapping_service.py`
- Test: `backend/app/tests/integration/test_id_mapping_api.py`（本任务只写 service 层用例；Task 4 在同一文件补端点用例）

**Interfaces:**
- Consumes: Task 1 的 `buildUnifiedId` / `IdMapping`；Task 2 的 `id_mapping` 表
- Produces:
  - `class IdMappingCreate(CamelModel)`：`business_object: str`、`external_id: str`、`pg_table/pg_id/neo4j_node_id/milvus_collection/milvus_id: str | None`
  - `class IdMappingRead(CamelModel)`：上述字段 + `unified_id: str` + `created_time/updated_time: datetime | None`
  - `MSG_ID_MAPPING_NOT_FOUND`、`MSG_ID_MAPPING_NOT_REGISTERED`、`MSG_ID_MAPPING_EXISTS`
  - `class IdMappingService`：
    - `async register(session: AsyncSession, dto: IdMappingCreate) -> IdMapping`
    - `async resolve(session: AsyncSession, businessObject: str, externalId: str) -> IdMapping`
    - `async resolveByUnifiedId(session: AsyncSession, unifiedId: str) -> IdMapping`
    - `async listMappings(session: AsyncSession, *, businessObject: str | None = None, limit: int = 100, offset: int = 0) -> list[IdMapping]`

- [ ] **Step 1: 写失败的测试**

创建 `backend/app/tests/integration/test_id_mapping_api.py`：

```python
"""id_mapping 集成测试：service 层（本任务）+ REST 端点（Task 4）。

全部走真实 PG + 完整 API 链路（Harness/rules/测试规范.md）。
断言用 raw SQL 读权威值，不用 ORM identity map（seed/commit 后 ORM 可能返回旧值）。
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.schemas import IdMappingCreate
from app.services.id_mapping_service import IdMappingService

# 类内 async 用例显式打 asyncio 标记（asyncio_mode=auto 已覆盖，但同仓库
# test_business_object_api.py 的既有写法是显式标注 —— 保持一致，且类方法
# 在 auto 模式下的覆盖是 pytest-asyncio 版本相关的，显式标注无成本）
pytestmark = pytest.mark.asyncio


class TestRegister:
    async def test_register_persists_row_with_server_derived_unified_id(
        self, dbSession: AsyncSession
    ) -> None:
        # Arrange
        service = IdMappingService()
        dto = IdMappingCreate(
            business_object="Supplier",
            external_id="4",
            pg_table="ontology_class",
            pg_id="4",
        )

        # Act
        row = await service.register(dbSession, dto)

        # Assert：unified_id 由服务端派生，客户端没传也不可能传
        assert row.unified_id == "obj:Supplier:4"
        raw = (
            await dbSession.execute(
                text(
                    "SELECT unified_id, business_object, external_id, pg_table, pg_id "
                    "FROM id_mapping WHERE unified_id = :uid"
                ),
                {"uid": "obj:Supplier:4"},
            )
        ).one()
        assert tuple(raw) == ("obj:Supplier:4", "Supplier", "4", "ontology_class", "4")

    async def test_register_leaves_cross_store_columns_null(
        self, dbSession: AsyncSession
    ) -> None:
        """A1 不碰 Neo4j/Milvus：三列必须是 NULL，不假装知道落点。"""
        service = IdMappingService()
        await service.register(
            dbSession, IdMappingCreate(business_object="Supplier", external_id="4")
        )
        raw = (
            await dbSession.execute(
                text(
                    "SELECT neo4j_node_id, milvus_collection, milvus_id "
                    "FROM id_mapping WHERE unified_id = 'obj:Supplier:4'"
                )
            )
        ).one()
        assert tuple(raw) == (None, None, None)

    async def test_register_duplicate_business_key_raises_conflict(
        self, dbSession: AsyncSession
    ) -> None:
        # Arrange
        service = IdMappingService()
        dto = IdMappingCreate(business_object="Supplier", external_id="4")
        await service.register(dbSession, dto)

        # Act + Assert：同一 (business_object, external_id) 第二次必拒
        with pytest.raises(ConflictError):
            await service.register(dbSession, dto)

    async def test_register_rejects_separator_in_external_id(
        self, dbSession: AsyncSession
    ) -> None:
        service = IdMappingService()
        with pytest.raises(ValidationError):
            await service.register(
                dbSession,
                IdMappingCreate(business_object="Supplier", external_id="C:10001"),
            )

    async def test_register_strips_whitespace_before_persisting(
        self, dbSession: AsyncSession
    ) -> None:
        """去空白必须发生在写库**之前**，否则 (bo, ext) 唯一的语义会被空白绕过。"""
        service = IdMappingService()
        await service.register(
            dbSession, IdMappingCreate(business_object=" Supplier ", external_id=" 4 ")
        )
        count = (
            await dbSession.execute(
                text("SELECT count(*) FROM id_mapping WHERE business_object = 'Supplier'")
            )
        ).scalar_one()
        assert count == 1


class TestResolve:
    async def test_resolve_by_business_key(self, dbSession: AsyncSession) -> None:
        service = IdMappingService()
        await service.register(
            dbSession, IdMappingCreate(business_object="SUPPLIER", external_id="SUP000001")
        )
        row = await service.resolve(dbSession, "SUPPLIER", "SUP000001")
        assert row.unified_id == "obj:SUPPLIER:SUP000001"

    async def test_resolve_missing_raises_not_found(self, dbSession: AsyncSession) -> None:
        service = IdMappingService()
        with pytest.raises(NotFoundError):
            await service.resolve(dbSession, "SUPPLIER", "NOPE")

    async def test_resolve_by_unified_id(self, dbSession: AsyncSession) -> None:
        service = IdMappingService()
        await service.register(
            dbSession, IdMappingCreate(business_object="Supplier", external_id="4")
        )
        row = await service.resolveByUnifiedId(dbSession, "obj:Supplier:4")
        assert row.business_object == "Supplier"

    async def test_resolve_by_malformed_unified_id_raises_validation(
        self, dbSession: AsyncSession
    ) -> None:
        """非法格式是 422（输入问题），不是 404（资源不存在）—— 两者语义不同。"""
        service = IdMappingService()
        with pytest.raises(ValidationError):
            await service.resolveByUnifiedId(dbSession, "Supplier:4")


class TestList:
    async def test_list_filters_by_business_object(self, dbSession: AsyncSession) -> None:
        service = IdMappingService()
        await service.register(
            dbSession, IdMappingCreate(business_object="Supplier", external_id="4")
        )
        await service.register(
            dbSession, IdMappingCreate(business_object="Customer", external_id="2")
        )
        rows = await service.listMappings(dbSession, businessObject="Supplier")
        assert [r.unified_id for r in rows] == ["obj:Supplier:4"]

    async def test_list_ordered_by_unified_id_and_paginated(
        self, dbSession: AsyncSession
    ) -> None:
        service = IdMappingService()
        for ext in ("4", "2", "9"):
            await service.register(
                dbSession, IdMappingCreate(business_object="Supplier", external_id=ext)
            )
        pageOne = await service.listMappings(dbSession, limit=2, offset=0)
        pageTwo = await service.listMappings(dbSession, limit=2, offset=2)
        assert [r.unified_id for r in pageOne] == ["obj:Supplier:2", "obj:Supplier:4"]
        assert [r.unified_id for r in pageTwo] == ["obj:Supplier:9"]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  uv run pytest app/tests/integration/test_id_mapping_api.py -v
```

预期：收集期报错 —— `ImportError: cannot import name 'IdMappingCreate' from 'app.domain.schemas'`（Task 3 的 DTO 与 service 都还没写；先撞哪个 import 取决于文件内顺序，报错即算 RED）。

- [ ] **Step 3: 在 schemas.py 末尾追加 DTO**

修改 `backend/app/domain/schemas.py`，**在文件最后追加**：

```python


# ---------------------------------------------------------------------------
# M0-P0.1 统一 ID 映射（id_mapping）
# ---------------------------------------------------------------------------


class IdMappingCreate(CamelModel):
    """注册一条统一 ID 映射。

    **刻意不接受 `unifiedId`**：统一 ID 由服务端 `buildUnifiedId` 从
    `businessObject` + `externalId` 派生。让客户端自带 ID 等于把「三库对齐」
    的前提交给调用方，A2/A3 就再也无法假设 ID 规则唯一。
    """

    business_object: str = Field(..., min_length=1, max_length=100)
    external_id: str = Field(..., min_length=1, max_length=200)
    pg_table: str | None = Field(default=None, max_length=100)
    pg_id: str | None = Field(default=None, max_length=100)
    neo4j_node_id: str | None = Field(default=None, max_length=200)
    milvus_collection: str | None = Field(default=None, max_length=100)
    milvus_id: str | None = Field(default=None, max_length=200)


class IdMappingRead(CamelModel):
    """统一 ID 映射响应。

    `created_time` / `updated_time` 允许缺省（与 `BusinessObjectRead` 同模式）：
    DTO 也用于未落库的构造场景。
    """

    unified_id: str
    business_object: str
    external_id: str
    pg_table: str | None = None
    pg_id: str | None = None
    neo4j_node_id: str | None = None
    milvus_collection: str | None = None
    milvus_id: str | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None
```

- [ ] **Step 4: 在 messages_zh.py 末尾追加文案**

修改 `backend/app/services/messages_zh.py`，**在文件最后追加**：

```python


# =============================================================================
# M0-P0.1 统一 ID 映射（id_mapping）
# =============================================================================

MSG_ID_MAPPING_NOT_FOUND = "统一 ID「{unifiedId}」未注册"
MSG_ID_MAPPING_NOT_REGISTERED = (
    "业务对象「{businessObject}」的外部 ID「{externalId}」未注册"
)
MSG_ID_MAPPING_EXISTS = (
    "映射已存在：业务对象「{businessObject}」的外部 ID「{externalId}」"
    "已注册为「{unifiedId}」"
)
```

- [ ] **Step 5: 写 IdMappingService**

创建 `backend/app/services/id_mapping_service.py`：

```python
"""统一 ID 映射 service（M0-P0.1）。

职责：id_mapping 表的注册 / 解析 / 列举。三库落点（Neo4j / Milvus）的**回填**
属于 A2 / A3，本 service 只写 PG。

不变式：
  - `unified_id` 一律经 `buildUnifiedId` 派生，调用方无从注入自造 ID。
  - `(business_object, external_id)` 唯一由 DB 约束兜底；service 先查后插，
    并发窗口下的 `IntegrityError` 统一翻译成 `ConflictError`（→ 409，
    与全库其余唯一性守卫同码）。
  - 读路径不做隐式创建（`resolve` 不 upsert）——「查不到」与「创建了」是两种
    语义，混在一起会让 A4 的写路径无法区分自己该不该注册。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.id_mapping import IdMapping, buildUnifiedId, parseUnifiedId
from app.domain.schemas import IdMappingCreate
from app.services.messages_zh import (
    MSG_ID_MAPPING_EXISTS,
    MSG_ID_MAPPING_NOT_FOUND,
    MSG_ID_MAPPING_NOT_REGISTERED,
)

_DEFAULT_LIST_LIMIT = 100


class IdMappingService:
    """id_mapping 注册 / 解析 / 列举。"""

    async def register(
        self, session: AsyncSession, dto: IdMappingCreate
    ) -> IdMapping:
        """注册一条映射；业务键已存在 → `ConflictError`（409）。

        `buildUnifiedId` 内部先去空白再校验，故 `' Supplier '` 与 `'Supplier'`
        落到同一行，不会因为空白绕过唯一约束。
        """
        unifiedId = buildUnifiedId(dto.business_object, dto.external_id)
        businessObject, externalId = parseUnifiedId(unifiedId)

        existing = await session.get(IdMapping, unifiedId)
        if existing is not None:
            raise ConflictError(
                MSG_ID_MAPPING_EXISTS.format(
                    businessObject=businessObject,
                    externalId=externalId,
                    unifiedId=unifiedId,
                )
            )

        row = IdMapping(
            unified_id=unifiedId,
            business_object=businessObject,
            external_id=externalId,
            pg_table=dto.pg_table,
            pg_id=dto.pg_id,
            neo4j_node_id=dto.neo4j_node_id,
            milvus_collection=dto.milvus_collection,
            milvus_id=dto.milvus_id,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            # 并发窗口：另一请求刚插了同一业务键
            await session.rollback()
            raise ConflictError(
                MSG_ID_MAPPING_EXISTS.format(
                    businessObject=businessObject,
                    externalId=externalId,
                    unifiedId=unifiedId,
                )
            ) from exc
        await session.refresh(row)
        return row

    async def resolve(
        self, session: AsyncSession, businessObject: str, externalId: str
    ) -> IdMapping:
        """按业务键解析；未注册 → `NotFoundError`（404）。"""
        unifiedId = buildUnifiedId(businessObject, externalId)
        row = await session.get(IdMapping, unifiedId)
        if row is None:
            raise NotFoundError(
                MSG_ID_MAPPING_NOT_REGISTERED.format(
                    businessObject=businessObject, externalId=externalId
                )
            )
        return row

    async def resolveByUnifiedId(
        self, session: AsyncSession, unifiedId: str
    ) -> IdMapping:
        """按统一 ID 解析。

        格式非法 → `ValidationError`（422，输入问题）；格式合法但未注册 →
        `NotFoundError`（404，资源不存在）。两者刻意区分。
        """
        parseUnifiedId(unifiedId)
        row = await session.get(IdMapping, unifiedId)
        if row is None:
            raise NotFoundError(MSG_ID_MAPPING_NOT_FOUND.format(unifiedId=unifiedId))
        return row

    async def listMappings(
        self,
        session: AsyncSession,
        *,
        businessObject: str | None = None,
        limit: int = _DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[IdMapping]:
        """按统一 ID 升序列举；`businessObject` 给出时按其过滤。"""
        stmt = select(IdMapping).order_by(IdMapping.unified_id)
        if businessObject is not None:
            stmt = stmt.where(IdMapping.business_object == businessObject)
        stmt = stmt.limit(limit).offset(offset)
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    async def countMappings(
        self, session: AsyncSession, *, businessObject: str | None = None
    ) -> int:
        """计数（供对账 / 运维核对用）。"""
        stmt = select(func.count()).select_from(IdMapping)
        if businessObject is not None:
            stmt = stmt.where(IdMapping.business_object == businessObject)
        return int((await session.execute(stmt)).scalar_one())
```

- [ ] **Step 6: 跑测试确认通过**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  uv run pytest app/tests/integration/test_id_mapping_api.py -v
```

预期：12 个用例全 PASS。

- [ ] **Step 7: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/domain/schemas.py backend/app/services/messages_zh.py \
        backend/app/services/id_mapping_service.py \
        backend/app/tests/integration/test_id_mapping_api.py
git commit -m "feat(id-mapping): IdMappingService + DTO + 中文文案"
```

---

## Task 4: REST 端点 + 双 app 装配

**Files:**
- Create: `backend/app/api/v1/id_mapping.py`
- Modify: `backend/app/main.py:~413`（局部 import 块）与 `:~519`（health 之前）
- Modify: `backend/app/tests/_testapp.py:~57`（import）与 `:~208`（include_router）
- Test: `backend/app/tests/integration/test_id_mapping_api.py`（追加端点用例；新类追加到文件末尾）

**Interfaces:**
- Consumes: Task 3 的 `IdMappingService` / `IdMappingCreate` / `IdMappingRead`
- Produces 4 条路由，全部要求认证（`Depends(getCurrentUser)`）：
  - `GET  /api/v1/id-mappings/resolve?businessObject=&externalId=` → 200 `IdMappingRead` / 404 / 422
  - `GET  /api/v1/id-mappings?businessObject=&limit=&offset=` → 200 `list[IdMappingRead]`
  - `GET  /api/v1/id-mappings/{unifiedId}` → 200 `IdMappingRead` / 404 / 422
  - `POST /api/v1/id-mappings` → 201 `IdMappingRead` / 409 / 422

- [ ] **Step 1: 写失败的端点测试**

在 `backend/app/tests/integration/test_id_mapping_api.py` **文件末尾追加**：

```python


class TestRoutes:
    """REST 端点全链路（经路由/中间件/service/真实 PG）。"""

    async def test_register_then_get_by_unified_id(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/id-mappings",
            json={
                "businessObject": "Supplier",
                "externalId": "4",
                "pgTable": "ontology_class",
                "pgId": "4",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["unifiedId"] == "obj:Supplier:4"
        assert body["businessObject"] == "Supplier"

        # unifiedId 含 ':' —— 验证 Starlette 单段路径参数确实收得下
        resp = await client.get("/api/v1/id-mappings/obj:Supplier:4")
        assert resp.status_code == 200
        assert resp.json()["externalId"] == "4"

    async def test_resolve_by_query_params(self, client: AsyncClient) -> None:
        await client.post(
            "/api/v1/id-mappings",
            json={"businessObject": "SUPPLIER", "externalId": "SUP000001"},
        )
        resp = await client.get(
            "/api/v1/id-mappings/resolve",
            params={"businessObject": "SUPPLIER", "externalId": "SUP000001"},
        )
        assert resp.status_code == 200
        assert resp.json()["unifiedId"] == "obj:SUPPLIER:SUP000001"

    async def test_resolve_route_not_shadowed_by_dynamic_route(
        self, client: AsyncClient
    ) -> None:
        """路由顺序守卫：`/resolve` 必须注册在 `/{unifiedId}` **之前**。

        若顺序反了，`/resolve` 会被 `{unifiedId}` 吞掉 → 报「统一 ID「resolve」
        格式非法」的 422 而不是走到 resolve 端点的 404/200。这是本仓库的复发
        类 bug（`/search` vs `/{id}`、`/next-code` vs `/{rule_id}`），故钉成
        独立用例：谁把顺序改回去，这里立刻红。
        """
        resp = await client.get(
            "/api/v1/id-mappings/resolve",
            params={"businessObject": "Supplier", "externalId": "MISSING"},
        )
        # 走到真 resolve 端点 → 404（未注册）；被吞 → 422（格式非法）
        assert resp.status_code == 404, (
            f"/resolve 被 /{{unifiedId}} 遮蔽了：拿到 {resp.status_code} "
            f"{resp.json()!r}，期望 404"
        )

    async def test_list_filters_and_returns_envelope_keys(
        self, client: AsyncClient
    ) -> None:
        for ext in ("4", "2"):
            await client.post(
                "/api/v1/id-mappings",
                json={"businessObject": "Supplier", "externalId": ext},
            )
        await client.post(
            "/api/v1/id-mappings",
            json={"businessObject": "Customer", "externalId": "2"},
        )

        resp = await client.get(
            "/api/v1/id-mappings", params={"businessObject": "Supplier"}
        )
        assert resp.status_code == 200
        assert [r["unifiedId"] for r in resp.json()] == [
            "obj:Supplier:2",
            "obj:Supplier:4",
        ]

    async def test_list_pagination(self, client: AsyncClient) -> None:
        for ext in ("1", "2", "3"):
            await client.post(
                "/api/v1/id-mappings",
                json={"businessObject": "Supplier", "externalId": ext},
            )
        resp = await client.get(
            "/api/v1/id-mappings", params={"limit": 2, "offset": 1}
        )
        assert resp.status_code == 200
        assert [r["unifiedId"] for r in resp.json()] == [
            "obj:Supplier:2",
            "obj:Supplier:3",
        ]

    async def test_duplicate_returns_409(self, client: AsyncClient) -> None:
        payload = {"businessObject": "Supplier", "externalId": "4"}
        assert (await client.post("/api/v1/id-mappings", json=payload)).status_code == 201
        resp = await client.post("/api/v1/id-mappings", json=payload)
        assert resp.status_code == 409

    async def test_missing_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/id-mappings/obj:Supplier:NOPE")
        assert resp.status_code == 404

    async def test_malformed_unified_id_returns_422(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/id-mappings/Supplier:4")
        assert resp.status_code == 422

    async def test_separator_in_external_id_returns_422(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/id-mappings",
            json={"businessObject": "Supplier", "externalId": "C:10001"},
        )
        assert resp.status_code == 422

    async def test_list_limit_upper_bound_is_enforced(self, client: AsyncClient) -> None:
        """无界查询是性能事故的常见入口：limit 上限必须由 DTO 层拒绝。"""
        resp = await client.get("/api/v1/id-mappings", params={"limit": 10000})
        assert resp.status_code == 422

    async def test_empty_business_object_returns_422(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/v1/id-mappings/resolve",
            params={"businessObject": "", "externalId": "4"},
        )
        assert resp.status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  uv run pytest app/tests/integration/test_id_mapping_api.py::TestRoutes -v
```

预期：全部 FAIL，`assert 404 == ...`，实际 404 但路径不存在（FastAPI 对未注册路径返回 404）—— 关键是 `test_register_then_get_by_unified_id` 断言 201 拿到 404。

- [ ] **Step 3: 写路由模块**

创建 `backend/app/api/v1/id_mapping.py`：

```python
"""统一 ID 映射 REST 端点（M0-P0.1）。

端点顺序即语义：`/resolve` 必须声明在 `/{unifiedId}` **之前**。Starlette 按
注册顺序匹配，`{unifiedId}` 是单段通配 —— 顺序反了 `/resolve` 会被它吞掉，
表现为「统一 ID「resolve」格式非法」的 422。本仓库已复发过同类 bug
（`/search` vs `/{id}`、`/next-code` vs `/{rule_id}`），守卫用例见
`tests/integration/test_id_mapping_api.py::TestRoutes::test_resolve_route_not_shadowed_by_dynamic_route`。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getCurrentUser, getDb
from app.domain.id_mapping import UNIFIED_ID_MAX_LENGTH
from app.domain.schemas import IdMappingCreate, IdMappingRead
from app.services.id_mapping_service import IdMappingService

router = APIRouter(
    prefix="/id-mappings",
    tags=["id-mapping"],
    dependencies=[Depends(getCurrentUser)],
)

_MAX_LIST_LIMIT = 500


def getIdMappingService() -> IdMappingService:
    return IdMappingService()


@router.get("/resolve", response_model=IdMappingRead)
async def resolveIdMapping(
    businessObject: str = Query(..., min_length=1, max_length=100),
    externalId: str = Query(..., min_length=1, max_length=200),
    session: AsyncSession = Depends(getDb),
    service: IdMappingService = Depends(getIdMappingService),
) -> IdMappingRead:
    """按业务键解析统一 ID（未注册 404，格式非法 422）。"""
    row = await service.resolve(session, businessObject, externalId)
    return IdMappingRead.model_validate(row)


@router.get("", response_model=list[IdMappingRead])
async def listIdMappings(
    businessObject: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=_MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(getDb),
    service: IdMappingService = Depends(getIdMappingService),
) -> list[IdMappingRead]:
    """列举映射（按 unifiedId 升序），可选按业务对象过滤。"""
    rows = await service.listMappings(
        session, businessObject=businessObject, limit=limit, offset=offset
    )
    return [IdMappingRead.model_validate(r) for r in rows]


@router.get("/{unifiedId}", response_model=IdMappingRead)
async def getIdMapping(
    unifiedId: str = Path(..., min_length=1, max_length=UNIFIED_ID_MAX_LENGTH),
    session: AsyncSession = Depends(getDb),
    service: IdMappingService = Depends(getIdMappingService),
) -> IdMappingRead:
    """按统一 ID 解析（未注册 404，格式非法 422）。"""
    row = await service.resolveByUnifiedId(session, unifiedId)
    return IdMappingRead.model_validate(row)


@router.post("", response_model=IdMappingRead, status_code=status.HTTP_201_CREATED)
async def registerIdMapping(
    payload: IdMappingCreate,
    session: AsyncSession = Depends(getDb),
    service: IdMappingService = Depends(getIdMappingService),
) -> IdMappingRead:
    """注册映射；业务键已存在 → 409。unifiedId 由服务端派生。"""
    row = await service.register(session, payload)
    return IdMappingRead.model_validate(row)
```

- [ ] **Step 4: 装配进真实 app（main.py）**

修改 `backend/app/main.py`。`createApp()` 内的局部 import 块（当前 379-415 行，`from app.api.v1 import (` 起）中，在 `graph_traversal,` 与 `kpi_catalog,` 之间插入一行 `id_mapping,`：

```python
        graph,
        graph_traversal,
        id_mapping,
        kpi_catalog,
```

然后在 `app.include_router(admin_wiki_links_router)` 之后、`# 健康检查。**必须注册在下方 MCP 挂载之前**` 注释**之前**追加：

```python
    app.include_router(
        id_mapping.router, prefix="/api/v1", tags=["id-mapping"]
    )
```

- [ ] **Step 5: 同步装配进测试 app（_testapp.py）**

修改 `backend/app/tests/_testapp.py`：

1. 在 `from app.api.v1 import (` 块（19-57 行）中，`graph_traversal,` 与 `kpi_catalog,` 之间插入 `id_mapping,`；
2. 在 `testApp.include_router(admin_wiki_links_router)`（209-210 行）之后追加：

```python
    testApp.include_router(
        id_mapping.router, prefix="/api/v1", tags=["id-mapping"]
    )
```

> 为什么必须两边都改：`_testapp.py` 是与 `main.py` **平行手工维护**的另一套 wiring。只改 `main.py` 则测试看不到新端点；只改 `_testapp.py` 则测试全绿而线上 404（`test_app_wiring.py` 的文件头注释记录了这个教训）。

- [ ] **Step 6: 跑测试确认通过**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  uv run pytest app/tests/integration/test_id_mapping_api.py -v
```

预期：22 个用例全 PASS（12 service + 10 端点）。

- [ ] **Step 7: 跑装配守卫，确认新路由没落在 catch-all Mount 之后**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  uv run pytest app/tests/integration/test_app_wiring.py -v
```

预期：全 PASS。

- [ ] **Step 8: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/app/api/v1/id_mapping.py backend/app/main.py backend/app/tests/_testapp.py \
        backend/app/tests/integration/test_id_mapping_api.py
git commit -m "feat(id-mapping): 4 个 REST 端点 + main/testapp 双装配 + 路由顺序守卫"
```

---

## Task 5: 回填脚本

**Files:**
- Create: `backend/scripts/backfill_id_mapping.py`
- Test: `backend/app/tests/integration/test_backfill_id_mapping.py`

**Interfaces:**
- Consumes: Task 1 的 `IdMapping` / `buildUnifiedId`；Task 3 的 `IdMappingService.countMappings`
- Produces:
  - `@dataclass(frozen=True) class BackfillSummary`：`aliveClasses: int`、`scannedClasses: int`、`affectedRows: int`
  - `async def backfillIdMappings(session: AsyncSession, *, includeDeleted: bool = False) -> BackfillSummary`
  - `async def main() -> None`（CLI）

- [ ] **Step 1: 写失败的测试**

创建 `backend/app/tests/integration/test_backfill_id_mapping.py`：

```python
"""id_mapping 回填脚本集成测试（M0-P0.1）。

契约：
- 存活本体类全部拿到统一 ID，count 与 `valid_to IS NULL` 的行数对平。
- 二次执行幂等（PG conflict 路径 DO UPDATE，不新增行、不报错）。
- 软删墓碑（valid_to 非空）默认跳过，`includeDeleted=True` 时才纳入。

为什么直接插 ontology_class 而不走 ontology API：本用例要钉的是「回填脚本对
给定行集做了什么」，class_id 必须由测试指定（否则断言不出 `obj:Supplier:<id>`
里的 id）。ontology_class.id 是 `nextval(...)` 默认值而非 GENERATED ALWAYS，
显式给 id 合法。

断言一律 raw SQL：ORM identity map 在跨 commit 后可能返回旧值（历史教训
见 seed_business_objects 的 header_class_id 回填事故）。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.backfill_id_mapping import backfillIdMappings

pytestmark = pytest.mark.asyncio

_INSERT_CLASS_SQL = text(
    "INSERT INTO ontology_class "
    "(id, class_name, version, valid_from, valid_to, created_time, updated_time) "
    "VALUES (:id, :name, 1, now(), :validTo, now(), now())"
)


async def _insertClass(
    session: AsyncSession, classId: int, className: str, *, deleted: bool = False
) -> None:
    """直接插 ontology_class：class_id 显式给，便于断言 unified_id = obj:<name>:<id>。"""
    await session.execute(
        _INSERT_CLASS_SQL,
        {
            "id": classId,
            "name": className,
            "validTo": datetime.now(UTC) if deleted else None,
        },
    )
    await session.commit()


async def _countMappings(session: AsyncSession) -> int:
    return int(
        (await session.execute(text("SELECT count(*) FROM id_mapping"))).scalar_one()
    )


class TestBackfill:
    async def test_registers_all_alive_classes(self, dbSession: AsyncSession) -> None:
        # Arrange：3 个存活类
        await _insertClass(dbSession, 101, "Supplier")
        await _insertClass(dbSession, 102, "Customer")
        await _insertClass(dbSession, 103, "ItemMaster")

        # Act
        summary = await backfillIdMappings(dbSession)

        # Assert
        assert summary.aliveClasses == 3
        assert summary.scannedClasses == 3
        assert await _countMappings(dbSession) == 3
        rows = (
            await dbSession.execute(
                text(
                    "SELECT unified_id, business_object, external_id, pg_table, pg_id "
                    "FROM id_mapping ORDER BY unified_id"
                )
            )
        ).all()
        assert [tuple(r) for r in rows] == [
            ("obj:Customer:102", "Customer", "102", "ontology_class", "102"),
            ("obj:ItemMaster:103", "ItemMaster", "103", "ontology_class", "103"),
            ("obj:Supplier:101", "Supplier", "101", "ontology_class", "101"),
        ]

    async def test_skips_soft_deleted_by_default(self, dbSession: AsyncSession) -> None:
        await _insertClass(dbSession, 201, "Supplier")
        await _insertClass(dbSession, 202, "SupplierOld", deleted=True)

        summary = await backfillIdMappings(dbSession)

        assert summary.aliveClasses == 1
        assert summary.scannedClasses == 1
        assert await _countMappings(dbSession) == 1

    async def test_include_deleted_registers_tombstones(
        self, dbSession: AsyncSession
    ) -> None:
        await _insertClass(dbSession, 301, "Supplier")
        await _insertClass(dbSession, 302, "SupplierOld", deleted=True)

        summary = await backfillIdMappings(dbSession, includeDeleted=True)

        assert summary.aliveClasses == 1
        assert summary.scannedClasses == 2
        assert await _countMappings(dbSession) == 2

    async def test_rerun_is_idempotent(self, dbSession: AsyncSession) -> None:
        """重跑不得新增行，也不得报错（PG conflict 路径走 DO UPDATE）。"""
        await _insertClass(dbSession, 401, "Supplier")
        await _insertClass(dbSession, 402, "Customer")

        first = await backfillIdMappings(dbSession)
        second = await backfillIdMappings(dbSession)

        assert first.aliveClasses == 2
        assert second.aliveClasses == 2
        assert await _countMappings(dbSession) == 2

    async def test_rerun_does_not_clobber_cross_store_columns(
        self, dbSession: AsyncSession
    ) -> None:
        """A2/A3 回填的 neo4j/milvus 列不能被重跑抹掉 —— 回填只写它自己拥有的列。"""
        await _insertClass(dbSession, 501, "Supplier")
        await backfillIdMappings(dbSession)
        await dbSession.execute(
            text(
                "UPDATE id_mapping SET neo4j_node_id = 'node-501', "
                "milvus_collection = 'ontology_embeddings', milvus_id = 'vec-501' "
                "WHERE unified_id = 'obj:Supplier:501'"
            )
        )
        await dbSession.commit()

        await backfillIdMappings(dbSession)

        raw = (
            await dbSession.execute(
                text(
                    "SELECT neo4j_node_id, milvus_collection, milvus_id "
                    "FROM id_mapping WHERE unified_id = 'obj:Supplier:501'"
                )
            )
        ).one()
        assert tuple(raw) == ("node-501", "ontology_embeddings", "vec-501")

    async def test_empty_ontology_registers_nothing(self, dbSession: AsyncSession) -> None:
        summary = await backfillIdMappings(dbSession)
        assert summary.aliveClasses == 0
        assert summary.scannedClasses == 0
        assert await _countMappings(dbSession) == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  uv run pytest app/tests/integration/test_backfill_id_mapping.py -v
```

预期：`ModuleNotFoundError: No module named 'scripts.backfill_id_mapping'`

- [ ] **Step 3: 写回填脚本**

创建 `backend/scripts/backfill_id_mapping.py`：

```python
"""从 ontology_class 回填 id_mapping 类级映射（M0-P0.1）。

为每个存活本体类生成 `obj:{class_name}:{class_id}` —— 这是 A2（Neo4j 节点
统一 ID）与 A4（ontology_service 三库同步）的输入。

**只读源表，纯新增**：不动 ontology_class 一行。

冲突路径用 `ON CONFLICT ... DO UPDATE`（不是 DO NOTHING）：DO NOTHING 会让
「先前插过但 PG 侧列不全」的行永远补不回来（seed_business_objects 的
header_class_id 事故就是这么来的）。但 `set_` **只更新本脚本拥有的两列**
（pg_table / pg_id），绝不触碰 A2/A3 回填的 neo4j_* / milvus_* —— 否则重跑
会把图与向量的落点抹掉。

幂等：重复执行行数不变。

用法：
    cd backend
    uv run python -m scripts.backfill_id_mapping              # 只回填存活类
    uv run python -m scripts.backfill_id_mapping --include-deleted
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.id_mapping import IdMapping, buildUnifiedId
from app.domain.models import OntologyClass

_ONTOLOGY_CLASS_TABLE = "ontology_class"
# 单批 upsert 行数：本体类是几十行量级，一批足够；分批只为将来类数上千时不爆参数
_BATCH_SIZE = 500


@dataclass(frozen=True)
class BackfillSummary:
    """回填结果。

    - `aliveClasses`：库中存活本体类总数（`valid_to IS NULL`）
    - `scannedClasses`：本次实际扫描的类数（`includeDeleted=True` 时含墓碑）
    - `affectedRows`：PG 报告的受影响行数（DO UPDATE 语义下含值未变的冲突行）
    """

    aliveClasses: int
    scannedClasses: int
    affectedRows: int


async def _countAliveClasses(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(OntologyClass)
                .where(OntologyClass.valid_to.is_(None))
            )
        ).scalar_one()
    )


async def _loadClassRows(
    session: AsyncSession, *, includeDeleted: bool
) -> list[tuple[int, str]]:
    """取 `(class_id, class_name)`，按 id 升序。"""
    stmt = select(OntologyClass.id, OntologyClass.class_name).order_by(
        OntologyClass.id
    )
    if not includeDeleted:
        stmt = stmt.where(OntologyClass.valid_to.is_(None))
    rows = (await session.execute(stmt)).all()
    return [(int(classId), str(className)) for classId, className in rows]


def _buildRows(classRows: list[tuple[int, str]]) -> list[dict[str, str]]:
    """构造 upsert 载荷。external_id 用 str(class_id) —— id 稳定且天然唯一。"""
    return [
        {
            "unified_id": buildUnifiedId(className, str(classId)),
            "business_object": className,
            "external_id": str(classId),
            "pg_table": _ONTOLOGY_CLASS_TABLE,
            "pg_id": str(classId),
        }
        for classId, className in classRows
    ]


async def _upsertBatch(session: AsyncSession, rows: list[dict[str, str]]) -> int:
    """一批 upsert；冲突时只更新本脚本拥有的两列。"""
    stmt = pg_insert(IdMapping).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["unified_id"],
        set_={"pg_table": stmt.excluded.pg_table, "pg_id": stmt.excluded.pg_id},
    )
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount or 0)


async def backfillIdMappings(
    session: AsyncSession, *, includeDeleted: bool = False
) -> BackfillSummary:
    """回填 id_mapping 类级映射；幂等，可安全重跑。

    返回 `BackfillSummary`。`includeDeleted=False`（默认）只回填存活类，
    与 `IdMappingService.countMappings()` 对账时口径为
    `count(*) == ontology_class WHERE valid_to IS NULL`。
    """
    aliveClasses = await _countAliveClasses(session)
    classRows = await _loadClassRows(session, includeDeleted=includeDeleted)
    rows = _buildRows(classRows)

    affected = 0
    for start in range(0, len(rows), _BATCH_SIZE):
        affected += await _upsertBatch(session, rows[start : start + _BATCH_SIZE])

    return BackfillSummary(
        aliveClasses=aliveClasses,
        scannedClasses=len(classRows),
        affectedRows=affected,
    )


async def main() -> None:
    """CLI 入口：连默认 DB（读 DATABASE_URL）跑回填。"""
    parser = argparse.ArgumentParser(description="回填 id_mapping 类级统一 ID")
    parser.add_argument(
        "--include-deleted",
        action="store_true",
        help="连同软删墓碑（valid_to IS NOT NULL）一起回填",
    )
    args = parser.parse_args()

    from app.infrastructure.database import getSessionFactory

    sessionMaker = getSessionFactory()
    async with sessionMaker() as session:
        summary = await backfillIdMappings(
            session, includeDeleted=args.include_deleted
        )
        print(
            f"[backfill_id_mapping] 存活类 {summary.aliveClasses}，"
            f"扫描 {summary.scannedClasses}，受影响行 {summary.affectedRows}"
        )


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  uv run pytest app/tests/integration/test_backfill_id_mapping.py -v
```

预期：6 个用例全 PASS。

- [ ] **Step 5: 提交**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add backend/scripts/backfill_id_mapping.py \
        backend/app/tests/integration/test_backfill_id_mapping.py
git commit -m "feat(id-mapping): ontology_class → id_mapping 回填脚本（幂等 + 不越权写跨库列）"
```

---

## Task 6: 覆盖率门禁 + prod 迁移 + 部署验证

**Files:** 无新增（仅执行与记录）

**Interfaces:**
- Consumes: Task 1-5 全部产物
- Produces: 迁移后在 prod 生效的 `id_mapping` + 回填数据；验收证据

- [ ] **Step 1: 覆盖率门禁（先修到绿再往下走）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80
```

预期：PASS，`Required test coverage of 80% reached`。若新文件有未覆盖行，补用例而不是降门禁。

> 注意：unit / services / integration **不要同进程混跑**（TRUNCATE 会抹掉 ontology_class，见历史教训）；上面的整目录跑法已按 `testpaths` 分目录执行，若出现莫名失败先拆开单目录复现。

- [ ] **Step 2: 备份 prod 迁移前状态**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
mkdir -p backups/2026-09-28_m0
export PGPASSWORD=qa_pg_dev_2026
pg_dump -h localhost -p 5433 -U qa_user -d qa_metadata --schema-only \
  > backups/2026-09-28_m0/qa_metadata_schema_before_0095.sql
psql -h localhost -p 5433 -U qa_user -d qa_metadata \
  -c "select version_num from alembic_version" \
  > backups/2026-09-28_m0/alembic_version_before_0095.txt
ls -la backups/2026-09-28_m0/
```

预期：两个文件非空；`version_num = 0094_wiki_page_category_id`。

> 0095 只 `CREATE TABLE`（纯新增、无 ALTER），不存在旧表数据被改写的风险，故无需 `<表>_<YYYYMMDD>` 数据备份；上面留 schema 快照只为「迁移前长什么样」可查。

- [ ] **Step 3: 对 prod 打迁移**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata' \
  uv run alembic upgrade head
DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata' \
  uv run alembic current
```

预期：`0095_id_mapping (head)`。

- [ ] **Step 4: 对 prod 跑回填并核对计数对平**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata' \
  uv run python -m scripts.backfill_id_mapping
export PGPASSWORD=qa_pg_dev_2026
psql -h localhost -p 5433 -U qa_user -d qa_metadata -c "
SELECT
  (SELECT count(*) FROM id_mapping) AS id_mapping_rows,
  (SELECT count(*) FROM ontology_class WHERE valid_to IS NULL) AS alive_classes,
  (SELECT count(*) FROM ontology_class) AS all_classes;"
```

预期：`id_mapping_rows = alive_classes`（当前 prod 为 **32**）；`all_classes = 52`。若不等，先查 NULL/重复 class_name 再决定是否 `--include-deleted`。

- [ ] **Step 5: 再跑一次回填确认幂等（prod 上）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata' \
  uv run python -m scripts.backfill_id_mapping
export PGPASSWORD=qa_pg_dev_2026
psql -h localhost -p 5433 -U qa_user -d qa_metadata \
  -c "SELECT count(*) FROM id_mapping;"
```

预期：行数不变（仍 32）—— 幂等成立。

- [ ] **Step 6: drift 校验（两库结构不得分叉）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
uv run python scripts/check_schema_drift.py --database-url \
  'postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata'
```

预期：无 blocking 差异（`id_mapping` 在两库都存在且列一致）。若报 drift，先跑测试库 `alembic upgrade head` 再复跑。

- [ ] **Step 7: 部署后端镜像（容器跑旧代码的教训）**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
./scripts/deploy_backend.sh
```

- [ ] **Step 8: 端到端探活（真实容器，经 nginx 之外的直连端口）**

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  'http://localhost:8000/api/v1/id-mappings?limit=5' \
  -H 'X-User-Id: system' -H 'X-User-Roles: admin'
curl -s 'http://localhost:8000/api/v1/id-mappings?limit=3' \
  -H 'X-User-Id: system' -H 'X-User-Roles: admin'
```

预期：第一条 `200`；第二条返回 `id_mapping` 中的真实行（`unifiedId` 形如 `obj:Supplier:4`）。

> 走 8000 直连而非 nginx：nginx 会缓存 upstream IP，后端重建后 `/api` 可能 502（历史事故）。若 8000 通而 5173 不通，重启 nginx 容器即可。

- [ ] **Step 9: 写变更记录（SSOT）+ 提交**

创建 `Harness/changes/2026-09-28-m0-id-mapping-a1/summary.md`，内容按 `Harness/changes/` 既有格式，至少包含：

```markdown
# 变更：m0-id-mapping-a1（M0-P0.1 统一 ID 映射中枢）

- **日期**：2026-09-28
- **分支**：feat/m0-unified-id（← epic/v31-upgrade ← 4ac87b5）
- **Phase**：v3.1 M0 P0.1
- **状态**：done
- **迁移版本**：0095_id_mapping（0094 → 0095）
- **关联变更**：../2026-09-28-arch-upgrade-v31/（母工程）

## 交付
- `id_mapping` 表（unified_id 主键 + (business_object, external_id) 唯一）
- 统一 ID SSOT：`app/domain/id_mapping.py::buildUnifiedId/parseUnifiedId`
- `IdMappingService`（register/resolve/resolveByUnifiedId/list/count）
- 4 端点：GET /id-mappings、GET /id-mappings/resolve、GET /id-mappings/{unifiedId}、POST /id-mappings
- 回填：`scripts/backfill_id_mapping.py`（类级，幂等）

## 验收证据
- 覆盖率：<贴 pytest 输出末行>
- prod 计数对平：id_mapping_rows = alive_classes = <N>
- drift：<贴结果>
- 探活：GET /api/v1/id-mappings → 200

## 偏离记录
- D1 冲突返回 409（非计划书的 422），依据 exceptions.py statusForError + 全库唯一性守卫惯例
- D2 回填默认只覆盖存活类（valid_to IS NULL），--include-deleted 覆盖墓碑
- D3 双层语义：类级（回填 ontology_class）+ 实例级（A4 运行时注册）
```

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git add Harness/changes/2026-09-28-m0-id-mapping-a1/summary.md
git commit -m "docs: M0-P0.1 统一 ID 映射中枢变更记录（0095 + 回填）"
```

- [ ] **Step 10: 与基线 diff 自检，确认没碰禁区**

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system
git diff --stat $(cat /tmp/a1-baseline-sha.txt)..HEAD
git diff --name-only $(cat /tmp/a1-baseline-sha.txt)..HEAD | grep -E 'ontology_service|graph_traversal|sql_guard|business_db_pool|raw/' \
  && echo '⚠️ 碰到了本计划禁改的文件，需复核' || echo '✅ 未触碰 M0 禁区（ontology_service / graph_traversal / SQL Guard / business_db_pool / raw/）'
```

预期：改动仅落在本计划 File Structure 列出的文件；末行打印 `✅`。

---

## 验收对照（plan-person-a.md A1）

| 计划书要求 | 本计划落点 | 验证 |
|---|---|---|
| 新建 `app/domain/id_mapping.py`，`IdMapping` 含 unified_id/bo/ext_id/三库列 | Task 1 | `TestBuildUnifiedId` + metadata 注册断言 |
| alembic 0095 + 唯一索引 `(business_object, external_id)` | Task 2 | `test_0095_unique_constraint_rejects_duplicate_business_key` |
| 回填脚本 `scripts/backfill_id_mapping.py`（只读源表，纯新增） | Task 5 | `TestBackfill` 6 用例（含「不越权写跨库列」） |
| integration 测试 `test_id_mapping_api.py`：建映射 → 三端点查询 → 冲突 | Task 3 + 4 | 22 用例 |
| 表建成 + 回填后 `count(*)` 与 ontology_class 数对平 | Task 6 Step 4 | prod 实测 `id_mapping_rows == alive_classes == 32` |

## 后续任务的前置（本计划**不**实现，供 A2/A3/A4 接手）

- A2：读 `id_mapping` 中 `pg_table='ontology_class'` 的行 → Neo4j `MATCH (n:Class {id: toInteger(pg_id)}) SET n.unified_id = ...`，回填 `neo4j_node_id`
- A3：Milvus 三 collection 重建加 `external_id`，回填 `milvus_collection` / `milvus_id`
- A4：`IdMappingService.ensureRegistered()`（get-or-create，用于 `ontology_service` 写路径）**尚未实现** —— A1 刻意不提前造它（YAGNI），A4 应带真实调用方的并发语义来加
- 写侧 ACL：A1 的 4 端点只要求认证（`getCurrentUser`），未加 admin 闸。是否需要写侧 admin 限制，留待 A4 接真实写路径时一并定
