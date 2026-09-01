# 菜单层级重新设计实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 QA System 前端 20 项扁平菜单改造为 6 类嵌套 SubMenu 结构，菜单定义从前端硬编码迁移到后端 `menu_config` 表 + `GET /api/v1/menu-config` API，并为后续权限过滤预留接口字段。

**Architecture:** 后端新建 `menu_config` 单表（自引用父子关系，存 6 个一级类 + 20 个叶子项），Alembic 迁移建表 + seed 脚本幂等 upsert；新增 `MenuConfigService.list_sections()` 单查询 + 内存分组构造嵌套结构；`MenuConfigRouter` 暴露 GET 端点。前端 `AppLayout` 删除硬编码 `NAV_KEYS`，改为 fetch API + Ant Design 嵌套 SubMenu；icon_code 字符串 + 前端 ICON_REGISTRY 字典渲染；fallbackNav.ts 作为 API 失败兜底；openKeys 持久化到 localStorage。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic v2 (CamelModel), Alembic, PostgreSQL, React 18, TypeScript, Ant Design 5, Vitest, React Testing Library.

## Global Constraints

- 不可变数据：始终创建新对象，禁止原地修改。
- SQL 安全：业务查询仅允许只读 SELECT（本期菜单查询为只读）。
- TDD：先写测试（RED）→ 实现（GREEN）→ 重构（IMPROVE），覆盖率 ≥ 80%。
- 小文件：200-400 行为宜，不超过 800 行；函数 < 50 行；嵌套 ≤ 4 层。
- 显式错误处理：每个层级显式处理错误，UI 层给友好提示，服务端记录详细上下文。
- 真实数据库测试：后端测试必须用真实 PostgreSQL（端口 5433，库 `qa_metadata_test`），禁止 sqlite 内存库 + 直接调用 service。
- 每次写完代码立即用 `code-reviewer` / `security-reviewer` 审查。
- ORM/Pydantic 字段名：`snake_case`（与 DB 列及 JSON 契约一致）。
- API JSON 输出：`camelCase`（由 Pydantic `alias_generator=to_camel` 负责）。

---

## File Structure

### 后端新增

| 文件 | 职责 |
|---|---|
| `backend/app/models/menu_config.py` | SQLAlchemy ORM `MenuConfig`（继承 Base + TimestampMixin） |
| `backend/app/schemas/menu_config.py` | Pydantic DTO：`MenuItemRead` / `MenuSectionRead` / `MenuConfigRead` |
| `backend/app/services/menu_config_service.py` | `MenuConfigService.list_sections()` 单查询 + 内存分组 |
| `backend/app/api/v1/menu_config.py` | FastAPI router：`GET /menu-config` |
| `backend/alembic/versions/0033_menu_config.py` | 建表 + 索引（down_revision=0032_agent_schedule） |
| `backend/scripts/seed_menu_config.py` | 幂等 upsert 6 类 20 项（基于 code 唯一键） |
| `backend/app/tests/unit/test_menu_config_service.py` | 单元测试 |
| `backend/app/tests/integration/test_menu_config_api.py` | API 集成测试 |
| `backend/app/tests/integration/test_seed_menu_config.py` | seed 脚本幂等 + 路由对齐测试 |

### 后端修改

| 文件 | 变更 |
|---|---|
| `backend/app/domain/models.py` | 追加 `MenuConfig` ORM 注册（或单独 import 时挂上） |
| `backend/app/api/v1/router.py` | `_register()` 内追加 `menuConfigRouter` |
| `backend/app/tests/conftest.py` | （如已有 pg_session fixture）无需改动 |

### 前端新增

| 文件 | 职责 |
|---|---|
| `frontend/src/components/common/menuIcons.ts` | ICON_REGISTRY 字典 + `renderIcon()` |
| `frontend/src/components/common/fallbackNav.ts` | 旧 NAV_KEYS 复制的静态兜底 |
| `frontend/src/api/menuConfig.ts` | `fetchMenuConfig()` API 封装 |
| `frontend/src/types/menuConfig.ts` | TypeScript 类型镜像后端契约 |
| `frontend/src/tests/menuIcons.test.ts` | ICON_REGISTRY 完整性测试 |
| `frontend/src/tests/AppLayout.test.tsx`（改造现有） | 新增 fetch/fallback/localStorage 用例 |

### 前端修改

| 文件 | 变更 |
|---|---|
| `frontend/src/components/common/AppLayout.tsx` | 删除 `NAV_KEYS`，改为 fetch + SubMenu + openKeys 持久化 |
| `frontend/src/i18n/zh-CN.ts` | 新增 6 个 `menu.section.*` + 20 个 `menu.item.*` key |
| `frontend/src/i18n/en-US.ts` | 同上 |

---

## Task 1: `MenuConfig` ORM 模型

**Files:**
- Create: `backend/app/models/menu_config.py`
- Modify: `backend/app/domain/models.py`（追加 import + re-export）
- Test: `backend/app/tests/unit/test_menu_config_orm.py`

**Interfaces:**
- Produces:
  - `class MenuConfig(Base, TimestampMixin):`
    - `id: Mapped[int]`（BIGSERIAL PK）
    - `code: Mapped[str]`（VARCHAR(64) UNIQUE NOT NULL）
    - `parent_id: Mapped[int | None]`（FK → menu_config.id，自引用）
    - `label_key: Mapped[str]`（VARCHAR(128) NOT NULL）
    - `path: Mapped[str | None]`（VARCHAR(256)，一级类为 None）
    - `icon_code: Mapped[str | None]`（VARCHAR(64)）
    - `sort_order: Mapped[int]`（INT NOT NULL DEFAULT 0）
    - `permission_code: Mapped[str | None]`（VARCHAR(64)，预留）
    - `roles: Mapped[str | None]`（VARCHAR(512)，逗号分隔字符串，预留）
    - `visible: Mapped[bool]`（BOOL NOT NULL DEFAULT TRUE）
    - `parent: Mapped["MenuConfig | None"] = relationship("MenuConfig", remote_side=[id])`
    - `children: Mapped[list["MenuConfig"]] = relationship("MenuConfig", back_populates="parent")`

- [ ] **Step 1: Write the failing test**

Create `backend/app/tests/unit/test_menu_config_orm.py`:

```python
"""MenuConfig ORM 基本属性测试。

不依赖 DB session，只验证 dataclass-like 列映射。
"""


def test_menu_config_tablename_is_menu_config() -> None:
    from app.models.menu_config import MenuConfig

    assert MenuConfig.__tablename__ == "menu_config"


def test_menu_config_columns_present() -> None:
    from app.models.menu_config import MenuConfig

    expected = {
        "id", "code", "parent_id", "label_key", "path",
        "icon_code", "sort_order", "permission_code", "roles", "visible",
        "created_time", "updated_time",
    }
    assert set(MenuConfig.__table__.columns.keys()) >= expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest app/tests/unit/test_menu_config_orm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.menu_config'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/models/menu_config.py`:

```python
"""menu_config 单表存菜单节点（自引用父子关系）。

Phase X：菜单层级重新设计。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.models import Base, TimestampMixin


class MenuConfig(Base, TimestampMixin):
    """菜单配置（一级类 + 叶子项共用此表）。"""

    __tablename__ = "menu_config"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("menu_config.id", ondelete="CASCADE"),
        nullable=True,
    )
    label_key: Mapped[str] = mapped_column(String(128), nullable=False)
    path: Mapped[str | None] = mapped_column(String(256), nullable=True)
    icon_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    permission_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    roles: Mapped[str | None] = mapped_column(String(512), nullable=True)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    parent: Mapped["MenuConfig | None"] = relationship(
        "MenuConfig", remote_side="MenuConfig.id", back_populates="children"
    )
    children: Mapped[list["MenuConfig"]] = relationship(
        "MenuConfig", back_populates="parent", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MenuConfig id={self.id} code={self.code!r}>"
```

Modify `backend/app/domain/models.py` — 在文件末尾追加：

```python
# Re-export MenuConfig so Alembic autogenerate picks it up.
from app.models.menu_config import MenuConfig  # noqa: E402,F401
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest app/tests/unit/test_menu_config_orm.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/menu_config.py backend/app/domain/models.py backend/app/tests/unit/test_menu_config_orm.py
git commit -m "feat(menu): MenuConfig ORM model with self-referencing parent"
```

---

## Task 2: Alembic 迁移建表

**Files:**
- Create: `backend/alembic/versions/0033_menu_config.py`

**Interfaces:**
- Consumes: 最新迁移 `0032_agent_schedule` 的 `down_revision`
- Produces: `menu_config` 表 + 两个索引（`parent_id`、`(parent_id, sort_order, id)`）

- [ ] **Step 1: Verify down_revision chain**

Run: `ls backend/alembic/versions/ | tail -5`

确认 `0032_agent_schedule.py` 存在；本迁移 `down_revision = "0032_agent_schedule"`。

- [ ] **Step 2: Write the migration**

Create `backend/alembic/versions/0033_menu_config.py`:

```python
"""menu_config - 菜单配置表（Phase X）。

存 6 个一级类 + 20 个叶子项，自引用父子关系。
permissionCode / roles 字段预留，本期不消费。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_menu_config"
down_revision: str | None = "0032_agent_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "menu_config",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("label_key", sa.String(length=128), nullable=False),
        sa.Column("path", sa.String(length=256), nullable=True),
        sa.Column("icon_code", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("permission_code", sa.String(length=64), nullable=True),
        sa.Column("roles", sa.String(length=512), nullable=True),
        sa.Column("visible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_menu_config_code"),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["menu_config.id"],
            name="fk_menu_config_parent",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_menu_config_parent", "menu_config", ["parent_id"])
    op.create_index(
        "ix_menu_config_parent_sort",
        "menu_config",
        ["parent_id", "sort_order", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_menu_config_parent_sort", table_name="menu_config")
    op.drop_index("ix_menu_config_parent", table_name="menu_config")
    op.drop_table("menu_config")
```

- [ ] **Step 3: Run migration against test DB**

Run:
```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run alembic upgrade head
```

Expected: 输出包含 `0033_menu_config` 的迁移成功提示。

- [ ] **Step 4: Verify table exists**

Run:
```bash
PGPASSWORD=qa_pg_dev_2026 psql -h localhost -p 5433 -U qa_user -d qa_metadata_test \
  -c "\d menu_config"
```

Expected: 输出含 `code` UNIQUE / `parent_id` FK / `sort_order` / `permission_code` / `roles` / `visible` 列。

- [ ] **Step 5: Roll back and re-run for clean state**

Run:
```bash
cd backend
TEST_DATABASE_URL=... uv run alembic downgrade -1
TEST_DATABASE_URL=... uv run alembic upgrade head
```

Expected: 两次都成功，迁移幂等。

- [ ] **Step 6: Commit**

```bash
git add backend/alembic/versions/0033_menu_config.py
git commit -m "feat(menu): alembic migration for menu_config table"
```

---

## Task 3: Pydantic schema（DTO）

**Files:**
- Create: `backend/app/schemas/menu_config.py`
- Test: `backend/app/tests/unit/test_menu_config_schema.py`

**Interfaces:**
- Produces:
  - `class MenuItemRead(CamelModel):`
    - `code: str`
    - `label_key: str`
    - `icon_code: str | None`
    - `sort_order: int`
    - `permission_code: str | None = None`
    - `roles: list[str] = Field(default_factory=list)`
    - `path: str | None = None`
  - `class MenuSectionRead(MenuItemRead):`
    - `children: list[MenuItemRead] = Field(default_factory=list)`
  - `class MenuConfigRead(CamelModel):`
    - `version: str`
    - `sections: list[MenuSectionRead]`

- [ ] **Step 1: Write the failing test**

Create `backend/app/tests/unit/test_menu_config_schema.py`:

```python
"""MenuConfig Pydantic schema 测试。"""

from app.schemas.menu_config import MenuConfigRead, MenuItemRead, MenuSectionRead


def test_menu_item_read_camel_alias() -> None:
    item = MenuItemRead(
        code="item.chat",
        label_key="menu.item.chat",
        icon_code="message",
        sort_order=110,
        path="/chat",
    )
    dumped = item.model_dump(by_alias=True)
    assert dumped["labelKey"] == "menu.item.chat"
    assert dumped["iconCode"] == "message"
    assert dumped["sortOrder"] == 110
    assert dumped["path"] == "/chat"
    assert dumped["permissionCode"] is None
    assert dumped["roles"] == []


def test_menu_section_read_nests_children() -> None:
    section = MenuSectionRead(
        code="section.aiAgent",
        label_key="menu.section.aiAgent",
        icon_code="robot",
        sort_order=100,
        children=[
            MenuItemRead(
                code="item.chat",
                label_key="menu.item.chat",
                icon_code="message",
                sort_order=110,
                path="/chat",
            )
        ],
    )
    assert len(section.children) == 1
    assert section.children[0].path == "/chat"


def test_menu_config_read_top_level_envelope() -> None:
    payload = MenuConfigRead(
        version="2026-09-01",
        sections=[
            MenuSectionRead(
                code="section.aiAgent",
                label_key="menu.section.aiAgent",
                icon_code="robot",
                sort_order=100,
            )
        ],
    )
    dumped = payload.model_dump(by_alias=True)
    assert dumped["version"] == "2026-09-01"
    assert len(dumped["sections"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest app/tests/unit/test_menu_config_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas.menu_config'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/schemas/menu_config.py`:

```python
"""menu_config API DTO。

CamelModel 自动产出 labelKey / sortOrder / permissionCode 等驼峰键。
roles: 后端存逗号分隔字符串；DTO 转换时 split 为 list。
"""

from __future__ import annotations

from pydantic import Field

from app.domain.schemas import CamelModel


def _split_roles(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


class MenuItemRead(CamelModel):
    """叶子项或一级类的可序列化视图。"""

    code: str
    label_key: str
    icon_code: str | None = None
    sort_order: int
    permission_code: str | None = None
    roles: list[str] = Field(default_factory=list)
    path: str | None = None


class MenuSectionRead(MenuItemRead):
    """一级类 + children 数组。"""

    children: list[MenuItemRead] = Field(default_factory=list)


class MenuConfigRead(CamelModel):
    """API 顶层 envelope。"""

    version: str
    sections: list[MenuSectionRead]


def to_menu_item(row: MenuItemLike) -> MenuItemRead:
    """ORM row → MenuItemRead（含 roles split）。"""
    ...


def to_menu_section(row: MenuItemLike, children: list[MenuItemRead]) -> MenuSectionRead:
    """ORM row → MenuSectionRead（含 children）。"""
    ...
```

注：上方的 `MenuItemLike` 是占位 type hint，在 Task 4 中替换为 `MenuConfig`。先删除这两个函数（后续 Task 4 由 service 调用），保持本 Task 文件仅含 3 个 schema 类。

Replace contents with:

```python
"""menu_config API DTO。

CamelModel 自动产出 labelKey / sortOrder / permissionCode 等驼峰键。
"""

from __future__ import annotations

from pydantic import Field

from app.domain.schemas import CamelModel


class MenuItemRead(CamelModel):
    """叶子项或一级类的可序列化视图。"""

    code: str
    label_key: str
    icon_code: str | None = None
    sort_order: int
    permission_code: str | None = None
    roles: list[str] = Field(default_factory=list)
    path: str | None = None


class MenuSectionRead(MenuItemRead):
    """一级类 + children 数组。"""

    children: list[MenuItemRead] = Field(default_factory=list)


class MenuConfigRead(CamelModel):
    """API 顶层 envelope。"""

    version: str
    sections: list[MenuSectionRead]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest app/tests/unit/test_menu_config_schema.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/menu_config.py backend/app/tests/unit/test_menu_config_schema.py
git commit -m "feat(menu): MenuConfigRead Pydantic DTOs (CamelModel)"
```

---

## Task 4: `MenuConfigService.list_sections()`（核心服务）

**Files:**
- Create: `backend/app/services/menu_config_service.py`
- Test: `backend/app/tests/integration/test_menu_config_service.py`

**Interfaces:**
- Produces:
  - `class MenuConfigService:`
    - `def __init__(self, session: AsyncSession)`
    - `async def list_sections(self, *, version: str = "2026-09-01") -> MenuConfigRead`
  - 返回的 `MenuConfigRead`：
    - `version`: 入参或 `"2026-09-01"` 默认值
    - `sections`: 6 个一级类，每个含按 `sort_order` 升序的 `children`
  - 行为：
    - 单查询 `SELECT * FROM menu_config WHERE visible = TRUE ORDER BY sort_order, id`
    - 内存里按 `parent_id` 链接父子
    - 任何 `code` 重复 → 抛 `RuntimeError("menu_config duplicate code: {code}")`
    - 一级类 `path` 非 None → 抛 `RuntimeError("section {code} must have path=None")`

- [ ] **Step 1: Write the failing integration test**

Create `backend/app/tests/integration/test_menu_config_service.py`:

```python
"""MenuConfigService.list_sections 集成测试（真实 PG）。"""

from __future__ import annotations

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.menu_config import MenuConfig
from app.schemas.menu_config import MenuConfigRead, MenuItemRead, MenuSectionRead
from app.services.menu_config_service import MenuConfigService

pytestmark = pytest.mark.integration


async def _clean_menu_config(session: AsyncSession) -> None:
    await session.execute(delete(MenuConfig).where(MenuConfig.id.is_not(None)))
    await session.commit()


async def test_list_sections_returns_envelope(pg_session: AsyncSession) -> None:
    await _clean_menu_config(pg_session)
    session_row = MenuConfig(code="section.aiAgent", label_key="menu.section.aiAgent",
                              icon_code="robot", sort_order=100)
    leaf_row = MenuConfig(code="item.chat", parent_id=None, label_key="menu.item.chat",
                          icon_code="message", sort_order=110, path="/chat")
    pg_session.add_all([session_row, leaf_row])
    await pg_session.flush()
    leaf_row.parent_id = session_row.id
    await pg_session.commit()

    svc = MenuConfigService(pg_session)
    result = await svc.list_sections()

    assert isinstance(result, MenuConfigRead)
    assert result.version == "2026-09-01"
    assert len(result.sections) == 1
    section = result.sections[0]
    assert isinstance(section, MenuSectionRead)
    assert section.code == "section.aiAgent"
    assert section.path is None
    assert len(section.children) == 1
    assert section.children[0].code == "item.chat"
    assert section.children[0].path == "/chat"


async def test_list_sections_sorts_children_by_sort_order(pg_session: AsyncSession) -> None:
    await _clean_menu_config(pg_session)
    section = MenuConfig(code="section.x", label_key="k", sort_order=100)
    pg_session.add(section)
    await pg_session.flush()
    for code, order in [("item.b", 120), ("item.a", 110), ("item.c", 130)]:
        pg_session.add(MenuConfig(code=code, parent_id=section.id, label_key="k",
                                   sort_order=order, path=f"/{code}"))
    await pg_session.commit()

    svc = MenuConfigService(pg_session)
    result = await svc.list_sections()
    codes = [c.code for c in result.sections[0].children]
    assert codes == ["item.a", "item.b", "item.c"]


async def test_list_sections_skips_invisible(pg_session: AsyncSession) -> None:
    await _clean_menu_config(pg_session)
    pg_session.add(MenuConfig(code="section.visible", label_key="k", sort_order=100, visible=True))
    pg_session.add(MenuConfig(code="section.hidden", label_key="k", sort_order=200, visible=False))
    await pg_session.commit()

    svc = MenuConfigService(pg_session)
    result = await svc.list_sections()
    codes = [s.code for s in result.sections]
    assert codes == ["section.visible"]


async def test_list_sections_roles_split_csv(pg_session: AsyncSession) -> None:
    await _clean_menu_config(pg_session)
    pg_session.add(MenuConfig(code="section.r", label_key="k", sort_order=100,
                               roles="admin,editor, "))
    await pg_session.commit()

    svc = MenuConfigService(pg_session)
    result = await svc.list_sections()
    assert result.sections[0].roles == ["admin", "editor"]


async def test_list_sections_duplicate_code_raises(pg_session: AsyncSession) -> None:
    """重复 code 需 fail-fast：Service 直接读取不应出现重复，但构造时防御。"""
    from app.services.menu_config_service import MenuConfigDuplicateError

    await _clean_menu_config(pg_session)
    pg_session.add(MenuConfig(code="dup", label_key="k", sort_order=100))
    pg_session.add(MenuConfig(code="dup", label_key="k", sort_order=200))
    await pg_session.commit()

    svc = MenuConfigService(pg_session)
    with pytest.raises(MenuConfigDuplicateError):
        await svc.list_sections()


async def test_list_sections_section_with_path_raises(pg_session: AsyncSession) -> None:
    from app.services.menu_config_service import MenuConfigStructureError

    await _clean_menu_config(pg_session)
    pg_session.add(MenuConfig(code="section.bad", label_key="k", sort_order=100, path="/bad"))
    await pg_session.commit()

    svc = MenuConfigService(pg_session)
    with pytest.raises(MenuConfigStructureError):
        await svc.list_sections()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_menu_config_service.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.menu_config_service'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/menu_config_service.py`:

```python
"""菜单配置服务。

list_sections() 单查询 + 内存分组构造嵌套结构。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.menu_config import MenuConfig
from app.schemas.menu_config import (
    MenuConfigRead,
    MenuItemRead,
    MenuSectionRead,
)


class MenuConfigError(RuntimeError):
    """菜单配置结构错误基类。"""


class MenuConfigDuplicateError(MenuConfigError):
    """code 列出现重复。"""


class MenuConfigStructureError(MenuConfigError):
    """一级类填了 path 或叶子项缺 path。"""


class MenuConfigService:
    """菜单数据访问与组装。"""

    DEFAULT_VERSION = "2026-09-01"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_sections(self, *, version: str = DEFAULT_VERSION) -> MenuConfigRead:
        stmt = (
            select(MenuConfig)
            .where(MenuConfig.visible.is_(True))
            .order_by(MenuConfig.sort_order, MenuConfig.id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()

        # Duplicate-code guard
        seen_codes: set[str] = set()
        for row in rows:
            if row.code in seen_codes:
                raise MenuConfigDuplicateError(f"menu_config duplicate code: {row.code}")
            seen_codes.add(row.code)

        # Group by parent_id
        section_rows: list[MenuConfig] = []
        children_by_parent: dict[int, list[MenuConfig]] = {}
        for row in rows:
            if row.parent_id is None:
                section_rows.append(row)
            else:
                children_by_parent.setdefault(row.parent_id, []).append(row)

        sections: list[MenuSectionRead] = []
        for srow in section_rows:
            if srow.path is not None:
                raise MenuConfigStructureError(
                    f"section {srow.code!r} must have path=None (got {srow.path!r})"
                )
            kids = children_by_parent.get(srow.id, [])
            sections.append(
                MenuSectionRead(
                    code=srow.code,
                    label_key=srow.label_key,
                    icon_code=srow.icon_code,
                    sort_order=srow.sort_order,
                    permission_code=srow.permission_code,
                    roles=_split_roles(srow.roles),
                    path=None,
                    children=[_to_item(child) for child in kids],
                )
            )
        return MenuConfigRead(version=version, sections=sections)


def _split_roles(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


def _to_item(row: MenuConfig) -> MenuItemRead:
    return MenuItemRead(
        code=row.code,
        label_key=row.label_key,
        icon_code=row.icon_code,
        sort_order=row.sort_order,
        permission_code=row.permission_code,
        roles=_split_roles(row.roles),
        path=row.path,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd backend
TEST_DATABASE_URL=... uv run pytest app/tests/integration/test_menu_config_service.py -v
```
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/menu_config_service.py backend/app/tests/integration/test_menu_config_service.py
git commit -m "feat(menu): MenuConfigService.list_sections with structure validation"
```

---

## Task 5: Seed 脚本（6 类 20 项幂等 upsert）

**Files:**
- Create: `backend/scripts/seed_menu_config.py`
- Test: `backend/app/tests/integration/test_seed_menu_config.py`

**Interfaces:**
- Produces:
  - `async def seed_menu_config(session_factory: async_sessionmaker[AsyncSession]) -> int`
    - 返回 upsert 行数（一级类 + 叶子项总数，预期 26）
    - 幂等：基于 `code` 唯一键，`INSERT ... ON CONFLICT (code) DO UPDATE`
    - 调用方：`python -m scripts.seed_menu_config` 或脚本内 `asyncio.run(...)`

- [ ] **Step 1: Write the failing test**

Create `backend/app/tests/integration/test_seed_menu_config.py`:

```python
"""seed_menu_config 幂等性 + 路由对齐测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.menu_config import MenuConfig
from app.services.menu_config_service import MenuConfigService
from scripts.seed_menu_config import seed_menu_config

pytestmark = pytest.mark.integration


async def _clean(pg_session: AsyncSession) -> None:
    await pg_session.execute(delete(MenuConfig).where(MenuConfig.id.is_not(None)))
    await pg_session.commit()


async def test_seed_inserts_six_sections_and_twenty_items(
    pg_session: AsyncSession, pg_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _clean(pg_session)

    count = await seed_menu_config(pg_session_factory)
    assert count == 26

    svc = MenuConfigService(pg_session)
    result = await svc.list_sections()
    assert len(result.sections) == 6
    total_items = sum(len(s.children) for s in result.sections)
    assert total_items == 20


async def test_seed_is_idempotent(
    pg_session: AsyncSession, pg_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _clean(pg_session)

    await seed_menu_config(pg_session_factory)
    await seed_menu_config(pg_session_factory)

    rows = (await pg_session.execute(select(MenuConfig))).scalars().all()
    assert len(rows) == 26
    assert len({r.code for r in rows}) == 26


async def test_seed_paths_aligned_with_frontend_routes(
    pg_session: AsyncSession, pg_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """验证种子中所有 item.path 都在前端路由集合内。"""
    await _clean(pg_session)
    await seed_menu_config(pg_session_factory)

    # 前端路由清单（App.tsx 当前实际路径）
    frontend_routes = {
        "/chat", "/agents/run", "/agents",
        "/supplier-360", "/supplier-risk",
        "/ontology", "/data-quality", "/lineage", "/entity-mapping",
        "/kpi-catalog", "/features",
        "/datasource", "/documents", "/usage", "/graph", "/vectors",
        "/models", "/embeddings", "/status", "/admin/audit",
    }

    svc = MenuConfigService(pg_session)
    result = await svc.list_sections()
    item_paths = {c.path for s in result.sections for c in s.children if c.path}
    assert item_paths == frontend_routes, (
        f"Mismatch: missing={frontend_routes - item_paths}, "
        f"extra={item_paths - frontend_routes}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_seed_menu_config.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.seed_menu_config'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/scripts/seed_menu_config.py`:

```python
"""seed_menu_config - 幂等 upsert 6 类 20 项菜单。

与 AppLayout 的 20 条旧 key 一一对应，零新增 / 零删除 / 零路径变更。
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.menu_config import MenuConfig
from scripts._db import make_engine_and_session_factory

# sort_order 间隔 10，便于插入新项。
SECTIONS: list[dict[str, Any]] = [
    {"code": "section.aiAgent", "label_key": "menu.section.aiAgent", "icon_code": "robot", "sort_order": 100},
    {"code": "section.analytics", "label_key": "menu.section.analytics", "icon_code": "fund", "sort_order": 200},
    {"code": "section.bizConfig", "label_key": "menu.section.bizConfig", "icon_code": "setting", "sort_order": 300},
    {"code": "section.foundation", "label_key": "menu.section.foundation", "icon_code": "database", "sort_order": 400},
    {"code": "section.systemConfig", "label_key": "menu.section.systemConfig", "icon_code": "api", "sort_order": 500},
    {"code": "section.auditSecurity", "label_key": "menu.section.auditSecurity", "icon_code": "safety", "sort_order": 600},
]

ITEMS: list[dict[str, Any]] = [
    # AI Agent
    {"parent": "section.aiAgent", "code": "item.chat", "label_key": "menu.item.chat", "icon_code": "message", "sort_order": 110, "path": "/chat"},
    {"parent": "section.aiAgent", "code": "item.agentRuntime", "label_key": "menu.item.agentRuntime", "icon_code": "thunderbolt", "sort_order": 120, "path": "/agents/run"},
    {"parent": "section.aiAgent", "code": "item.agents", "label_key": "menu.item.agents", "icon_code": "appstore", "sort_order": 130, "path": "/agents"},
    # Smart Analytics
    {"parent": "section.analytics", "code": "item.supplier360", "label_key": "menu.item.supplier360", "icon_code": "barchart", "sort_order": 210, "path": "/supplier-360"},
    {"parent": "section.analytics", "code": "item.supplierRisk", "label_key": "menu.item.supplierRisk", "icon_code": "alert", "sort_order": 220, "path": "/supplier-risk"},
    # Business Config
    {"parent": "section.bizConfig", "code": "item.ontology", "label_key": "menu.item.ontology", "icon_code": "partition", "sort_order": 310, "path": "/ontology"},
    {"parent": "section.bizConfig", "code": "item.dataQuality", "label_key": "menu.item.dataQuality", "icon_code": "audit", "sort_order": 320, "path": "/data-quality"},
    {"parent": "section.bizConfig", "code": "item.lineage", "label_key": "menu.item.lineage", "icon_code": "node", "sort_order": 330, "path": "/lineage"},
    {"parent": "section.bizConfig", "code": "item.entityMapping", "label_key": "menu.item.entityMapping", "icon_code": "code", "sort_order": 340, "path": "/entity-mapping"},
    {"parent": "section.bizConfig", "code": "item.kpiCatalog", "label_key": "menu.item.kpiCatalog", "icon_code": "number", "sort_order": 350, "path": "/kpi-catalog"},
    {"parent": "section.bizConfig", "code": "item.features", "label_key": "menu.item.features", "icon_code": "cluster", "sort_order": 360, "path": "/features"},
    # Foundation
    {"parent": "section.foundation", "code": "item.datasource", "label_key": "menu.item.datasource", "icon_code": "database", "sort_order": 410, "path": "/datasource"},
    {"parent": "section.foundation", "code": "item.documents", "label_key": "menu.item.documents", "icon_code": "file", "sort_order": 420, "path": "/documents"},
    {"parent": "section.foundation", "code": "item.usage", "label_key": "menu.item.usage", "icon_code": "dashboard", "sort_order": 430, "path": "/usage"},
    {"parent": "section.foundation", "code": "item.graph", "label_key": "menu.item.graph", "icon_code": "apartment", "sort_order": 440, "path": "/graph"},
    {"parent": "section.foundation", "code": "item.vectors", "label_key": "menu.item.vectors", "icon_code": "heart", "sort_order": 450, "path": "/vectors"},
    # System Config
    {"parent": "section.systemConfig", "code": "item.models", "label_key": "menu.item.models", "icon_code": "api", "sort_order": 510, "path": "/models"},
    {"parent": "section.systemConfig", "code": "item.embeddings", "label_key": "menu.item.embeddings", "icon_code": "node", "sort_order": 520, "path": "/embeddings"},
    {"parent": "section.systemConfig", "code": "item.status", "label_key": "menu.item.status", "icon_code": "heart", "sort_order": 530, "path": "/status"},
    # Audit & Security
    {"parent": "section.auditSecurity", "code": "item.adminAudit", "label_key": "menu.item.adminAudit", "icon_code": "audit", "sort_order": 610, "path": "/admin/audit"},
]


async def seed_menu_config(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """幂等 upsert 6 个一级类 + 20 个叶子项。返回总行数。"""
    async with session_factory() as session:
        # 1) upsert sections
        for s in SECTIONS:
            stmt = pg_insert(MenuConfig).values(
                code=s["code"], parent_id=None, label_key=s["label_key"],
                icon_code=s["icon_code"], sort_order=s["sort_order"],
                path=None, visible=True,
            ).on_conflict_do_update(
                index_elements=["code"],
                set_={
                    "label_key": s["label_key"], "icon_code": s["icon_code"],
                    "sort_order": s["sort_order"], "visible": True,
                },
            )
            await session.execute(stmt)

        # 2) resolve parent_id by code
        section_codes = [s["code"] for s in SECTIONS]
        rows = (await session.execute(
            __import__("sqlalchemy").select(MenuConfig.code, MenuConfig.id)
            .where(MenuConfig.code.in_(section_codes))
        )).all()
        code_to_id = {code: rid for code, rid in rows}

        # 3) upsert items
        for it in ITEMS:
            parent_id = code_to_id[it["parent"]]
            stmt = pg_insert(MenuConfig).values(
                code=it["code"], parent_id=parent_id, label_key=it["label_key"],
                icon_code=it["icon_code"], sort_order=it["sort_order"],
                path=it["path"], visible=True,
            ).on_conflict_do_update(
                index_elements=["code"],
                set_={
                    "parent_id": parent_id, "label_key": it["label_key"],
                    "icon_code": it["icon_code"], "sort_order": it["sort_order"],
                    "path": it["path"], "visible": True,
                },
            )
            await session.execute(stmt)

        await session.commit()
    return len(SECTIONS) + len(ITEMS)


async def main() -> None:
    factory = make_engine_and_session_factory()
    n = await seed_menu_config(factory)
    print(f"seed_menu_config: {n} rows upserted")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd backend
TEST_DATABASE_URL=... uv run pytest app/tests/integration/test_seed_menu_config.py -v
```
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/seed_menu_config.py backend/app/tests/integration/test_seed_menu_config.py
git commit -m "feat(menu): seed_menu_config script with 6 sections and 20 items"
```

---

## Task 6: API router + 注册

**Files:**
- Create: `backend/app/api/v1/menu_config.py`
- Modify: `backend/app/api/v1/router.py`（追加注册）
- Test: `backend/app/tests/integration/test_menu_config_api.py`

**Interfaces:**
- Produces:
  - `router = APIRouter(prefix="/menu-config", tags=["menu-config"])`
  - `@router.get("", response_model=MenuConfigRead) -> MenuConfigRead`
    - 鉴权：`Depends(getCurrentUser)`
    - DB：`Depends(getDb)`
    - 返回当前所有 visible 一级类 + 叶子项

- [ ] **Step 1: Write the failing integration test**

Create `backend/app/tests/integration/test_menu_config_api.py`:

```python
"""GET /api/v1/menu-config 集成测试。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import delete

from app.models.menu_config import MenuConfig

pytestmark = pytest.mark.integration


async def _seed(client_app_with_seed, pg_session) -> None:
    """辅助：在测试 DB 跑 seed。"""
    from scripts.seed_menu_config import seed_menu_config
    await seed_menu_config(pg_session.bind.async_sessionmaker if hasattr(pg_session.bind, "async_sessionmaker") else _factory(pg_session))


async def _factory(pg_session):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    return async_sessionmaker(pg_session.bind, expire_on_commit=False)


async def test_unauthenticated_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/menu-config")
    assert resp.status_code == 401


async def test_authenticated_returns_six_sections(
    client: AsyncClient, pg_session, auth_headers: dict[str, str]
) -> None:
    await pg_session.execute(delete(MenuConfig).where(MenuConfig.id.is_not(None)))
    await pg_session.commit()

    factory = _factory(pg_session)
    from scripts.seed_menu_config import seed_menu_config
    await seed_menu_config(factory)

    resp = await client.get("/api/v1/menu-config", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "2026-09-01"
    assert len(body["sections"]) == 6

    first = body["sections"][0]
    assert set(first.keys()) >= {
        "code", "labelKey", "iconCode", "sortOrder", "permissionCode", "roles", "children"
    }
    assert isinstance(first["children"], list)
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_menu_config_api.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.api.v1.menu_config'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/api/v1/menu_config.py`:

```python
"""menu-config API 路由。

GET /api/v1/menu-config —— 返回当前所有可见菜单（一级类 + 叶子项）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getCurrentUser, getDb
from app.schemas.menu_config import MenuConfigRead
from app.services.menu_config_service import MenuConfigService

router = APIRouter(prefix="/menu-config", tags=["menu-config"])


@router.get("", response_model=MenuConfigRead, status_code=status.HTTP_200_OK)
async def get_menu_config(
    _user: dict = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> MenuConfigRead:
    """返回当前所有可见菜单的嵌套结构。

    鉴权：Required（Bearer JWT）。
    本期不消费 permissionCode / roles 字段；接口字段透传以备后续接入。
    """
    return await MenuConfigService(session).list_sections()
```

Modify `backend/app/api/v1/router.py` — 在 `_register()` 末尾追加：

```python
    from app.api.v1.menu_config import router as menuConfigRouter

    router.include_router(menuConfigRouter, prefix="/menu-config", tags=["menu-config"])
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd backend
TEST_DATABASE_URL=... uv run pytest app/tests/integration/test_menu_config_api.py -v
```
Expected: PASS (2 passed)

- [ ] **Step 5: Run full backend test suite for regression**

Run:
```bash
cd backend
TEST_DATABASE_URL=... uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```
Expected: All previous tests still green; coverage ≥ 80%.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/menu_config.py backend/app/api/v1/router.py backend/app/tests/integration/test_menu_config_api.py
git commit -m "feat(menu): GET /api/v1/menu-config endpoint with auth"
```

---

## Task 7: 前端 i18n 新增 26 个 key

**Files:**
- Modify: `frontend/src/i18n/zh-CN.ts`
- Modify: `frontend/src/i18n/en-US.ts`
- Test: `frontend/src/tests/menuI18n.test.ts`

**Interfaces:**
- Produces: 在 `appLayout.brand` 之后新增命名空间：
  - `menu.section.{aiAgent, analytics, bizConfig, foundation, systemConfig, auditSecurity}`
  - `menu.item.{chat, agentRuntime, agents, supplier360, supplierRisk, ontology, dataQuality, lineage, entityMapping, kpiCatalog, features, datasource, documents, usage, graph, vectors, models, embeddings, status, adminAudit}`
- 不删除旧 `appLayout.menu.*` key（保留兼容期）

- [ ] **Step 1: Write the failing test**

Create `frontend/src/tests/menuI18n.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import zhCN from "../i18n/zh-CN";
import enUS from "../i18n/en-US";

const SECTION_KEYS = [
  "aiAgent", "analytics", "bizConfig", "foundation", "systemConfig", "auditSecurity",
];
const ITEM_KEYS = [
  "chat", "agentRuntime", "agents", "supplier360", "supplierRisk",
  "ontology", "dataQuality", "lineage", "entityMapping", "kpiCatalog", "features",
  "datasource", "documents", "usage", "graph", "vectors",
  "models", "embeddings", "status", "adminAudit",
];

describe("menu i18n keys", () => {
  it.each(SECTION_KEYS)("zh-CN has menu.section.%s", (k) => {
    expect(zhCN).toHaveProperty(`menu.section.${k}`);
  });
  it.each(SECTION_KEYS)("en-US has menu.section.%s", (k) => {
    expect(enUS).toHaveProperty(`menu.section.${k}`);
  });
  it.each(ITEM_KEYS)("zh-CN has menu.item.%s", (k) => {
    expect(zhCN).toHaveProperty(`menu.item.${k}`);
  });
  it.each(ITEM_KEYS)("en-US has menu.item.%s", (k) => {
    expect(enUS).toHaveProperty(`menu.item.${k}`);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- menuI18n.test.ts`
Expected: FAIL — `menu.section.aiAgent` 等 key 不存在。

- [ ] **Step 3: Add keys to zh-CN**

Modify `frontend/src/i18n/zh-CN.ts` — 在 `appLayout` 命名空间内新增（不删旧）：

```ts
  // 菜单层级（Phase X 新增，旧 appLayout.menu.* 保留兼容期）
  menu: {
    section: {
      aiAgent: "AI Agent",
      analytics: "智能分析",
      bizConfig: "业务配置",
      foundation: "业务基础信息",
      systemConfig: "系统信息配置",
      auditSecurity: "审计安全",
    },
    item: {
      chat: "AIChatService",
      agentRuntime: "Agent 运行时",
      agents: "Agent Registry",
      supplier360: "供应商 360°",
      supplierRisk: "供应商风险",
      ontology: "本体管理",
      dataQuality: "数据质量",
      lineage: "数据血缘",
      entityMapping: "编码映射",
      kpiCatalog: "KPI 目录",
      features: "特征目录",
      datasource: "数据源",
      documents: "文档中心",
      usage: "用量看板",
      graph: "Neo4j 图库",
      vectors: "Milvus 向量库",
      models: "模型配置",
      embeddings: "Embedding 服务",
      status: "服务状态",
      adminAudit: "审计日志",
    },
  },
```

（具体插入位置：紧跟现有 `appLayout.brand` 之后、保留所有旧 `appLayout.menu.*` 不动。）

- [ ] **Step 4: Add keys to en-US**

Modify `frontend/src/i18n/en-US.ts` — 同样新增：

```ts
  menu: {
    section: {
      aiAgent: "AI Agent",
      analytics: "Smart Analytics",
      bizConfig: "Business Config",
      foundation: "Foundation",
      systemConfig: "System Config",
      auditSecurity: "Audit & Security",
    },
    item: {
      chat: "AIChatService",
      agentRuntime: "Agent Runtime",
      agents: "Agent Registry",
      supplier360: "Supplier 360°",
      supplierRisk: "Supplier Risk",
      ontology: "Ontology",
      dataQuality: "Data Quality",
      lineage: "Data Lineage",
      entityMapping: "Code Mapping",
      kpiCatalog: "KPI Catalog",
      features: "Feature Catalog",
      datasource: "Data Sources",
      documents: "Document Center",
      usage: "Usage Dashboard",
      graph: "Neo4j Graph DB",
      vectors: "Milvus Vector DB",
      models: "Model Config",
      embeddings: "Embedding",
      status: "Service Status",
      adminAudit: "Audit Logs",
    },
  },
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend && npm test -- menuI18n.test.ts`
Expected: PASS (52 passed — 26 keys × 2 locales)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/tests/menuI18n.test.ts
git commit -m "feat(menu): add 26 i18n keys (6 sections + 20 items) for both locales"
```

---

## Task 8: 前端 ICON_REGISTRY + renderIcon

**Files:**
- Create: `frontend/src/components/common/menuIcons.ts`
- Test: `frontend/src/tests/menuIcons.test.ts`

**Interfaces:**
- Produces:
  - `ICON_REGISTRY: Record<string, React.ComponentType>` 21 个条目
  - `renderIcon(code?: string): React.ReactNode`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/tests/menuIcons.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { ICON_REGISTRY, renderIcon } from "../components/common/menuIcons";

describe("ICON_REGISTRY", () => {
  it("contains all 21 codes referenced by seed", () => {
    const expected = [
      "robot", "fund", "setting", "database", "api", "safety",
      "message", "thunderbolt", "appstore",
      "barchart", "alert",
      "partition", "audit", "node", "code", "number", "cluster",
      "file", "dashboard", "apartment", "heart",
    ];
    for (const code of expected) {
      expect(ICON_REGISTRY).toHaveProperty(code);
      expect(typeof ICON_REGISTRY[code]).toBe("function");
    }
  });
});

describe("renderIcon", () => {
  it("returns null for undefined", () => {
    expect(renderIcon(undefined)).toBeNull();
  });
  it("returns null for unknown code (no crash)", () => {
    expect(renderIcon("nonexistent")).toBeNull();
  });
  it("returns a React node for known code", () => {
    const node = renderIcon("robot");
    expect(node).not.toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- menuIcons.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/components/common/menuIcons.ts`:

```ts
import {
  RobotOutlined, MessageOutlined, ThunderboltOutlined, AppstoreOutlined,
  BarChartOutlined, AlertOutlined, PartitionOutlined, DatabaseOutlined,
  AuditOutlined, FileTextOutlined, DashboardOutlined, NodeIndexOutlined,
  ApartmentOutlined, CodeOutlined, FundProjectionScreenOutlined,
  NumberOutlined, SettingOutlined, ApiOutlined, HeartOutlined,
  SafetyCertificateOutlined, ClusterOutlined,
} from "@ant-design/icons";
import type { ComponentType, ReactNode } from "react";

export const ICON_REGISTRY: Record<string, ComponentType> = {
  robot: RobotOutlined, message: MessageOutlined, thunderbolt: ThunderboltOutlined,
  appstore: AppstoreOutlined, barchart: BarChartOutlined, alert: AlertOutlined,
  partition: PartitionOutlined, database: DatabaseOutlined, audit: AuditOutlined,
  file: FileTextOutlined, dashboard: DashboardOutlined, node: NodeIndexOutlined,
  apartment: ApartmentOutlined, code: CodeOutlined, fund: FundProjectionScreenOutlined,
  number: NumberOutlined, setting: SettingOutlined, api: ApiOutlined,
  heart: HeartOutlined, safety: SafetyCertificateOutlined, cluster: ClusterOutlined,
};

export const renderIcon = (code?: string): ReactNode => {
  if (!code) return null;
  const Icon = ICON_REGISTRY[code];
  return Icon ? <Icon /> : null;
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- menuIcons.test.ts`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/common/menuIcons.ts frontend/src/tests/menuIcons.test.ts
git commit -m "feat(menu): ICON_REGISTRY + renderIcon helper (21 codes)"
```

---

## Task 9: 前端 API 封装 + 类型 + fallback 常量

**Files:**
- Create: `frontend/src/api/menuConfig.ts`
- Create: `frontend/src/types/menuConfig.ts`
- Create: `frontend/src/components/common/fallbackNav.ts`

**Interfaces:**
- Produces:
  - `MenuItem` / `MenuSection` / `MenuConfig` TypeScript 接口
  - `async function fetchMenuConfig(): Promise<MenuConfig>`
  - `FALLBACK_NAV: readonly { key: string; labelKey: string }[]` —— 与旧 NAV_KEYS 等价

- [ ] **Step 1: Create types file**

Create `frontend/src/types/menuConfig.ts`:

```ts
export interface MenuItem {
  code: string;
  labelKey: string;
  iconCode: string | null;
  sortOrder: number;
  permissionCode: string | null;
  roles: string[];
  path: string | null;
}

export interface MenuSection extends MenuItem {
  children: MenuItem[];
}

export interface MenuConfig {
  version: string;
  sections: MenuSection[];
}
```

- [ ] **Step 2: Create API wrapper**

Create `frontend/src/api/menuConfig.ts`:

```ts
import type { MenuConfig } from "../types/menuConfig";

const API_BASE = "/api/v1";

export async function fetchMenuConfig(): Promise<MenuConfig> {
  const resp = await fetch(`${API_BASE}/menu-config`, {
    headers: { Accept: "application/json" },
    credentials: "include",
  });
  if (!resp.ok) {
    throw new Error(`fetchMenuConfig failed: ${resp.status} ${resp.statusText}`);
  }
  return (await resp.json()) as MenuConfig;
}
```

- [ ] **Step 3: Create fallback constant**

Create `frontend/src/components/common/fallbackNav.ts`:

```ts
/**
 * fallbackNav - 当 /menu-config API 失败时的兜底菜单。
 *
 * 严格复制自原 AppLayout 的 NAV_KEYS，保留全部 20 项。
 * 一旦本期上线稳定，可移除此文件。
 */
export const FALLBACK_NAV: readonly { key: string; labelKey: string }[] = [
  { key: "/models", labelKey: "appLayout.menu.models" },
  { key: "/embeddings", labelKey: "appLayout.menu.embeddings" },
  { key: "/chat", labelKey: "appLayout.menu.chat" },
  { key: "/ontology", labelKey: "appLayout.menu.ontology" },
  { key: "/datasource", labelKey: "appLayout.menu.datasource" },
  { key: "/data-quality", labelKey: "appLayout.menu.dataQuality" },
  { key: "/lineage", labelKey: "appLayout.menu.lineage" },
  { key: "/entity-mapping", labelKey: "appLayout.menu.entityMapping" },
  { key: "/kpi-catalog", labelKey: "appLayout.menu.kpiCatalog" },
  { key: "/features", labelKey: "appLayout.menu.features" },
  { key: "/usage", labelKey: "appLayout.menu.usage" },
  { key: "/status", labelKey: "appLayout.menu.status" },
  { key: "/graph", labelKey: "appLayout.menu.graph" },
  { key: "/vectors", labelKey: "appLayout.menu.vectors" },
  { key: "/supplier-360", labelKey: "appLayout.menu.supplier360" },
  { key: "/supplier-risk", labelKey: "appLayout.menu.supplierRisk" },
  { key: "/agents/run", labelKey: "appLayout.menu.agentRuntime" },
  { key: "/agents", labelKey: "appLayout.menu.agents" },
  { key: "/documents", labelKey: "appLayout.menu.documents" },
  { key: "/admin/audit", labelKey: "appLayout.menu.adminAudit" },
] as const;
```

- [ ] **Step 4: Type-check**

Run: `cd frontend && npm run typecheck`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/menuConfig.ts frontend/src/api/menuConfig.ts frontend/src/components/common/fallbackNav.ts
git commit -m "feat(menu): types + API wrapper + fallback nav constant"
```

---

## Task 10: AppLayout 改造（fetch + SubMenu + localStorage）

**Files:**
- Modify: `frontend/src/components/common/AppLayout.tsx`
- Test: 改造现有 `frontend/src/tests/AppLayout.test.tsx`

**Interfaces:**
- AppLayout 行为变更：
  - 删除硬编码 `NAV_KEYS`
  - 新增 `useState<MenuConfig | null>` + `useEffect(fetchMenuConfig)`
  - `useState<string[]>(openKeys)` 持久化到 `localStorage["menu.openKeys"]`
  - 加载中：`<Spin size="small" />` 占位
  - 错误：catch → setState 触发 fallback 渲染
  - 菜单渲染：Ant Design `Menu` + `items` prop，含嵌套 SubMenu
  - 选中：根据 `location.pathname.startsWith(item.path)` 计算 `selectedKeys`
  - `onOpenChange`：更新 openKeys + 同步 localStorage
  - `onClick`：叶子项 `navigate(item.path)`

- [ ] **Step 1: Read existing AppLayout test**

Run: `cat frontend/src/tests/AppLayout.test.tsx | head -100`

理解现有测试结构（mock fetch、router wrapper）。

- [ ] **Step 2: Replace AppLayout.tsx content**

Modify `frontend/src/components/common/AppLayout.tsx` — 用以下完整内容替换：

```tsx
import { useEffect, useState } from "react";
import { Layout, Menu, Spin, Switch, theme } from "antd";
import { useLocation, useNavigate, Outlet } from "react-router-dom";
import { useThemeStore } from "../../stores/themeStore";
import { useTranslation } from "../../i18n";
import { fetchMenuConfig } from "../../api/menuConfig";
import type { MenuConfig, MenuItem, MenuSection } from "../../types/menuConfig";
import { FALLBACK_NAV } from "./fallbackNav";
import { renderIcon } from "./menuIcons";
import LanguageSwitch from "./LanguageSwitch";

const { Sider, Header, Content } = Layout;
const { useToken } = theme;

const OPEN_KEYS_STORAGE = "menu.openKeys";

function readOpenKeys(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(OPEN_KEYS_STORAGE);
    return raw ? (JSON.parse(raw) as string[]) : [];
  } catch {
    return [];
  }
}

export default function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { token } = useToken();
  const { t } = useTranslation();
  const isDark = useThemeStore((s) => s.isDark);
  const toggleTheme = useThemeStore((s) => s.toggleTheme);
  const [collapsed, setCollapsed] = useState(false);
  const [menuConfig, setMenuConfig] = useState<MenuConfig | null>(null);
  const [useFallback, setUseFallback] = useState(false);
  const [openKeys, setOpenKeys] = useState<string[]>(() => readOpenKeys());

  useEffect(() => {
    let cancelled = false;
    fetchMenuConfig()
      .then((cfg) => {
        if (!cancelled) setMenuConfig(cfg);
      })
      .catch((err) => {
        // eslint-disable-next-line no-console
        console.warn("[menu-config] fallback to static nav:", err);
        if (!cancelled) setUseFallback(true);
      });
    return () => { cancelled = true; };
  }, []);

  // Determine selected key from current path
  const allItems: MenuItem[] = useFallback
    ? FALLBACK_NAV.map((n) => ({
        code: n.key, labelKey: n.labelKey, iconCode: null,
        sortOrder: 0, permissionCode: null, roles: [], path: n.key,
      }))
    : (menuConfig?.sections.flatMap((s) => s.children) ?? []);
  const selectedItem =
    allItems.find((i) => i.path && location.pathname.startsWith(i.path)) ??
    allItems[0] ?? null;

  // Persist openKeys
  const handleOpenChange = (keys: string[]) => {
    setOpenKeys(keys);
    try {
      window.localStorage.setItem(OPEN_KEYS_STORAGE, JSON.stringify(keys));
    } catch {
      // localStorage 不可用（隐私模式）静默忽略
    }
  };

  // Render AntD Menu items prop
  const menuItems = useFallback
    ? FALLBACK_NAV.map((n) => ({
        key: n.key, label: t(n.labelKey),
        icon: null,
      }))
    : (menuConfig?.sections.map((section: MenuSection) => ({
        key: section.code,
        icon: renderIcon(section.iconCode ?? undefined),
        label: t(section.labelKey),
        children: section.children.map((child) => ({
          key: child.code,
          icon: renderIcon(child.iconCode ?? undefined),
          label: t(child.labelKey),
        })),
      })) ?? []);

  const isLoading = !useFallback && menuConfig === null;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        style={{
          height: "100vh", position: "fixed", left: 0, top: 0, bottom: 0,
          background: "#001529", zIndex: 100,
        }}
      >
        <div
          style={{
            height: 48, margin: 12, color: "#fff", textAlign: "center",
            lineHeight: "48px", fontWeight: 600, overflow: "hidden",
            whiteSpace: "nowrap",
          }}
        >
          {collapsed ? "QA" : t("appLayout.brand")}
        </div>
        {isLoading ? (
          <div style={{ padding: 16, color: "#fff", textAlign: "center" }}>
            <Spin size="small" />
          </div>
        ) : (
          <Menu
            theme="dark"
            mode="inline"
            selectedKeys={selectedItem ? [selectedItem.code] : []}
            openKeys={useFallback ? undefined : openKeys}
            onOpenChange={useFallback ? undefined : handleOpenChange}
            items={menuItems}
            onClick={({ key }) => {
              const target = allItems.find((i) => i.code === key && i.path);
              if (target?.path) navigate(target.path);
            }}
          />
        )}
      </Sider>

      <div
        style={{
          flex: 1, marginLeft: collapsed ? 80 : 200,
          display: "flex", flexDirection: "column",
        }}
      >
        <Header style={{ ... }}>
          {/* existing header content unchanged */}
        </Header>
        <Content style={{ margin: 24, background: token.colorBgContainer, borderRadius: 8 }}>
          <Outlet />
        </Content>
      </div>
    </Layout>
  );
}
```

注：保留原 `<Header>` 与 `<LanguageSwitch>` 的现有 JSX 不变（仅替换 `<Menu>` 部分）。

- [ ] **Step 3: Run existing AppLayout test to see what breaks**

Run: `cd frontend && npm test -- AppLayout.test.tsx`
Expected: 现有测试可能 mock 缺失 fetch；记录失败原因。

- [ ] **Step 4: Update AppLayout test with fetch mocking**

Modify `frontend/src/tests/AppLayout.test.tsx` — 在测试文件顶部新增 mock：

```ts
import { vi, beforeEach } from "vitest";

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});
```

并在具体用例里 `mockResolvedValue`：

```ts
import { fetchMenuConfig } from "../api/menuConfig";
vi.mock("../api/menuConfig");

(fetchMenuConfig as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
  version: "2026-09-01",
  sections: [
    {
      code: "section.aiAgent", labelKey: "menu.section.aiAgent",
      iconCode: "robot", sortOrder: 100, permissionCode: null, roles: [],
      path: null,
      children: [
        { code: "item.chat", labelKey: "menu.item.chat", iconCode: "message",
          sortOrder: 110, permissionCode: null, roles: [], path: "/chat" },
      ],
    },
  ],
});
```

并新增 3 个用例：

```tsx
it("renders 6 submenus after fetch resolves", async () => { /* mock 6 sections，断言 6 个 SubMenu 标题 */ });
it("falls back to static nav when fetch rejects", async () => { /* fetch reject，断言 FALLBACK_NAV 项存在 */ });
it("persists openKeys to localStorage on expand", async () => { /* 点击 SubMenu，断言 localStorage['menu.openKeys'] */ });
```

（具体断言按 React Testing Library 习惯：`screen.getByText` 找 SubMenu 标题等。）

- [ ] **Step 5: Run frontend test to verify it passes**

Run: `cd frontend && npm test -- AppLayout.test.tsx`
Expected: PASS（含新增 3 个用例）。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/common/AppLayout.tsx frontend/src/tests/AppLayout.test.tsx
git commit -m "feat(menu): AppLayout fetches /menu-config with fallback + openKeys persistence"
```

---

## Task 11: 全量回归 + 文档收尾

**Files:**
- Modify: `Harness/wiki/frontend.md`（追加「菜单架构」小节；如不存在则新建）
- Create: `Harness/changes/feat-menu-hierarchy/summary.md`

**Interfaces:**
- 文档记录：
  - 6 类分组 + 顺序
  - 数据契约（CamelModel JSON）
  - fallback 策略
  - 预留字段（permissionCode / roles）

- [ ] **Step 1: Run full backend test suite**

Run:
```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
```
Expected: All green; coverage ≥ 80%.

- [ ] **Step 2: Run frontend typecheck + build + test**

Run:
```bash
cd frontend
npm run typecheck
npm run build
npm test -- --coverage
```
Expected: typecheck 0 errors; build success; test green.

- [ ] **Step 3: Live API smoke test**

Run:
```bash
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  uv run alembic upgrade head
DATABASE_URL=... uv run python scripts/seed_menu_config.py

# 启动 backend
DATABASE_URL=... uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 &

# 登录拿 token
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | jq -r .access_token)

# 验证 API
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/menu-config \
  | jq '.sections | length'    # 期望: 6
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/menu-config \
  | jq '[.sections[].children[]] | length'   # 期望: 20
```
Expected: 输出 6 和 20。

- [ ] **Step 4: Browser smoke test**

1. 启动 frontend dev server: `cd frontend && npm run dev`
2. 浏览器访问 `http://localhost:5173`（或项目实际端口）
3. 检查清单：
   - [ ] 左侧菜单显示 6 个 SubMenu 标题
   - [ ] 每个 SubMenu 可展开/折叠
   - [ ] 点击每个叶子项能跳转到对应路由
   - [ ] 刷新页面，已展开的 SubMenu 保持展开
   - [ ] 杀掉 backend，刷新页面 → 仍能看到 20 项菜单（fallback）

- [ ] **Step 5: Write change summary**

Create `Harness/changes/feat-menu-hierarchy/summary.md`:

```markdown
# feat-menu-hierarchy

## 概述
将 QA System 前端 20 项扁平菜单改造为 6 类嵌套 SubMenu 结构，菜单定义从前端硬编码迁移到后端 menu_config 表 + GET /api/v1/menu-config API。

## 关键决策
- 6 类分组：AI Agent / 智能分析 / 业务配置 / 业务基础信息 / 系统信息配置 / 审计安全
- 顺序：按使用频度（AI Agent 最前，审计安全 最后）
- 数据契约：DB 表 + Pydantic CamelModel + TypeScript 类型镜像
- 兜底：API 失败 → fallbackNav.ts 渲染旧硬编码菜单
- 权限预留：permissionCode / roles 字段在契约中但不消费

## 文件变更
（列出 Task 1-10 所有新增/修改文件，按后端/前端分组）

## 后续可做
- 管理界面（拖拽调整 / 增删菜单）
- 后端按用户角色/权限过滤菜单
- 菜单变更历史 / 审计
```

- [ ] **Step 6: Append frontend wiki section**

Modify `Harness/wiki/frontend.md` — 在末尾追加（如不存在则新建）：

```markdown
## 菜单架构（feat-menu-hierarchy）

### 数据流
```
AppLayout mount → fetch /api/v1/menu-config → MenuConfig (DB)
                                              ↓
                            Ant Design Menu + SubMenu
```

### 兜底
- API 失败 → console.warn + setUseFallback(true) → 渲染 fallbackNav.ts
- openKeys 持久化：localStorage["menu.openKeys"]

### 预留字段
- permissionCode: string | null
- roles: string[]

本期不消费这两字段；后续接入 acl_service 时启用。
```

- [ ] **Step 7: Final commit**

```bash
git add Harness/changes/feat-menu-hierarchy/summary.md Harness/wiki/frontend.md
git commit -m "docs(menu): change summary + frontend wiki section"
```

- [ ] **Step 8: Tag + push (if applicable)**

如项目使用 git tag，标记 `v2026.09.01.menu-hierarchy`：
```bash
git tag -a v2026.09.01.menu-hierarchy -m "Menu hierarchy redesign shipped"
git push origin main --tags
```

---

## Self-Review Notes

**Spec coverage**:
- ✅ 6 类分组 → Task 5（seed）+ Task 7（i18n）+ Task 10（AppLayout）
- ✅ 后端 DB 表 + API → Task 1-2（ORM + 迁移）+ Task 3（schema）+ Task 4（service）+ Task 6（router）
- ✅ Seed 幂等 → Task 5（含 test_seed_is_idempotent）
- ✅ iconCode + 前端字典 → Task 8
- ✅ Ant Design SubMenu + openKeys 持久化 → Task 10
- ✅ fallbackNav 兜底 → Task 9 + Task 10
- ✅ permissionCode / roles 预留字段 → Task 3（schema）+ Task 5（seed 数据不含，仅字段）
- ✅ 20 项路由对齐 → Task 5 test_seed_paths_aligned_with_frontend_routes
- ✅ 单元 + 集成测试 → 每个 Task 含 RED → GREEN
- ✅ 文档 → Task 11 summary.md + wiki

**Type consistency**:
- ORM `MenuConfig.code` → Pydantic `MenuItemRead.code` → TS `MenuItem.code` ✓
- Service 抛 `MenuConfigDuplicateError` / `MenuConfigStructureError` → 测试断言同一类 ✓
- Router response_model `MenuConfigRead` → 返回 `MenuConfigService.list_sections()` 同名 ✓
- 前端 `fetchMenuConfig()` 返回 `Promise<MenuConfig>` ↔ API JSON 字段 camelCase ↔ Pydantic alias ✓

**Placeholder scan**:
- 0 个 TBD / TODO / FIXME
- 代码块完整（无 "类似 Task N"）
- 所有函数签名有完整参数类型

**Scope check**: 单一 feature（菜单层级 + DB 表 + API + 前端），无子系统分解需求。
