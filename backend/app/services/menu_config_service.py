"""菜单配置服务。

list_sections() 单查询 + 内存分组构造嵌套结构；MenuAdminService 提供管理 CRUD。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.menu_config import MenuConfig
from app.schemas.menu_config import (
    MenuConfigCreate,
    MenuConfigRead,
    MenuConfigTreeNode,
    MenuConfigUpdate,
    MenuItemRead,
    MenuRowRead,
    MenuSectionRead,
)
from app.services.outbox_service import OutboxService


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

    async def list_sections(
        self,
        *,
        version: str = DEFAULT_VERSION,
        allowed_codes: frozenset[str] | None = None,
    ) -> MenuConfigRead:
        """返回菜单树。allowed_codes 非空时：叶子项只保留 code ∈ allowed_codes，
        且该集合为空 → 不显示任何菜单；None → 返回全部可见项（admin / 无过滤）。
        """
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
            if allowed_codes is not None:
                kids = [c for c in kids if c.code in allowed_codes]
                if not kids:
                    continue  # 无可见叶子项的一级类不展示
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

    async def list_admin_rows(self) -> list[MenuRowRead]:
        """管理页全量扁平列表（含不可见项），供菜单管理 / 授权勾选树使用。"""
        rows = (
            await self._session.execute(
                select(MenuConfig).order_by(MenuConfig.sort_order, MenuConfig.id)
            )
        ).scalars().all()
        child_counts: dict[int, int] = {}
        code_by_id: dict[int, str] = {}
        for row in rows:
            code_by_id[row.id] = row.code
            if row.parent_id is not None:
                child_counts[row.parent_id] = child_counts.get(row.parent_id, 0) + 1
        return [
            MenuRowRead(
                id=row.id,
                code=row.code,
                parent_id=row.parent_id,
                parent_code=code_by_id.get(row.parent_id) if row.parent_id else None,
                label_key=row.label_key,
                icon_code=row.icon_code,
                sort_order=row.sort_order,
                path=row.path,
                permission_code=row.permission_code,
                visible=row.visible,
                has_children=child_counts.get(row.id, 0) > 0,
            )
            for row in rows
        ]

    async def leaf_codes(self, *, only_visible: bool = True) -> frozenset[str]:
        """所有叶子项 code（path 非空）。only_visible=True 时过滤可见项。"""
        stmt = select(MenuConfig.code).where(MenuConfig.path.is_not(None))
        if only_visible:
            stmt = stmt.where(MenuConfig.visible.is_(True))
        codes = (await self._session.execute(stmt)).scalars().all()
        return frozenset(codes)

    async def list_menu_tree(self) -> list[MenuConfigTreeNode]:
        """返回菜单嵌套树（多根：所有 section；feat-menu-tree）。

        含不可见项 + 含 section 自身（与 /menu-config 的「侧边栏可见」语义
        独立）。一次性按 (sort_order, id) 排序后 Python 侧 O(n) 构建
        （id→节点 + 两趟：建节点、挂 children）。n≤几百行足够快。

    注意：seed 永远只造 2 层（section→item），但本方法对任意层 section 嵌
        套都安全——orphan 检测（parent_id 不在 nodes 里）把脏数据视为根。
        """
        rows = (
            await self._session.execute(
                select(MenuConfig).order_by(MenuConfig.sort_order, MenuConfig.id)
            )
        ).scalars().all()
        nodes: dict[int, MenuConfigTreeNode] = {}
        id_to_code: dict[int, str] = {}
        for r in rows:
            id_to_code[r.id] = r.code
            nodes[r.id] = MenuConfigTreeNode(
                code=r.code,
                label_key=r.label_key,
                icon_code=r.icon_code,
                sort_order=r.sort_order,
                path=r.path,
                permission_code=r.permission_code,
                visible=r.visible,
                parent_code=None,  # 第二趟 fill
                children=[],
            )
        roots: list[MenuConfigTreeNode] = []
        for r in rows:
            node = nodes[r.id]
            if r.parent_id is None or r.parent_id not in nodes:
                roots.append(node)
            else:
                parent = nodes[r.parent_id]
                parent.children.append(node)
                node.parent_code = parent.code
        return roots


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


def _row_payload(row: MenuConfig) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "parent_id": row.parent_id,
        "label_key": row.label_key,
        "icon_code": row.icon_code,
        "sort_order": row.sort_order,
        "path": row.path,
        "permission_code": row.permission_code,
        "visible": row.visible,
    }


class MenuAdminService:
    """菜单管理 CRUD（动态增删改菜单，admin only）。code 创建后不可变（权限授予以 code 为键）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._outbox = OutboxService()

    async def _get_by_code(self, code: str) -> MenuConfig:
        row = (
            await self._session.execute(
                select(MenuConfig).where(MenuConfig.code == code)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"菜单不存在: {code}")
        return row

    async def _resolve_parent(self, parent_code: str | None) -> int | None:
        if parent_code is None:
            return None
        parent = await self._get_by_code(parent_code)
        if parent.path is not None:
            raise ValidationError(f"父级必须是 section（一级类）：{parent_code!r}")
        return parent.id

    async def _would_create_menu_cycle(
        self, code: str, new_parent_code: str | None
    ) -> bool:
        """若 new_parent_code 是 code 的后代（含自身），把 code.parent 改成它会成环。

        仅在 new_parent_code 非 None 时调用；new_parent_code 解析后必须是
        section（_resolve_parent 校验）。section→section 的移动当前被
        MenuAdminService.update 的「section 不能挂 parent」规则拒绝，所以
        实际上该检查只在叶子项移动时触发，section 移动走不到这里。

        SQL 递归 CTE：从 new_parent_id 向上追溯到根，看是否能遇到 code.id。
        O(深度) 一步查询，n≤几百行足够快。
        """
        if new_parent_code is None:
            return False
        new_parent = await self._get_by_code(new_parent_code)
        if new_parent.path is not None:
            # 双层防御：_resolve_parent 已校验，再查一遍保护直接调用者
            raise ValidationError(f"父级必须是 section（一级类）：{new_parent_code!r}")
        if new_parent.code == code:
            return True
        target = await self._get_by_code(code)
        cte = text(
            """
            WITH RECURSIVE ancestors AS (
                SELECT id, parent_id
                FROM menu_config
                WHERE id = :new_parent_id
                UNION ALL
                SELECT m.id, m.parent_id
                FROM menu_config m
                JOIN ancestors a ON m.id = a.parent_id
            )
            SELECT EXISTS (
                SELECT 1 FROM ancestors WHERE id = :code_id
            )
            """
        )
        result = await self._session.execute(
            cte, {"code_id": target.id, "new_parent_id": new_parent.id}
        )
        return bool(result.scalar_one())

    async def create(self, dto: MenuConfigCreate, actor: CurrentUser) -> MenuConfig:
        """新增菜单节点。path 为空 → section；否则为叶子项。"""
        is_section = not dto.path
        if is_section and dto.parent_code is not None:
            raise ValidationError("section 不能挂 parent（仅一层分组）")
        if dto.parent_code is None and not is_section:
            raise ValidationError("叶子项必须指定 parent_code")
        parent_id = await self._resolve_parent(dto.parent_code)

        row = MenuConfig(
            code=dto.code,
            parent_id=parent_id,
            label_key=dto.label_key,
            icon_code=dto.icon_code,
            sort_order=dto.sort_order,
            path=dto.path,
            permission_code=dto.permission_code,
            visible=dto.visible,
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            await self._session.rollback()
            if "menu_config" in str(e.orig) and "unique" in str(e.orig).lower():
                raise ConflictError(f"菜单 code 已存在: {dto.code}")
            raise
        await self._outbox.enqueue(
            self._session,
            event_type="menu_created",
            entity_type="menu",
            entity_id=row.id,
            actor=actor.userId,
            payload={"before": None, "after": _row_payload(row)},
        )
        return row

    async def update(self, code: str, dto: MenuConfigUpdate, actor: CurrentUser) -> MenuConfig:
        """更新菜单节点；可迁移父级 / 改 visible / 调整字段。"""
        row = await self._get_by_code(code)
        before = _row_payload(row)
        provided = dto.model_fields_set
        if "parent_code" in provided:
            if row.path is None and dto.parent_code is not None:
                raise ValidationError("section 不能移动到其它 section 下")
            if row.path is not None and dto.parent_code is None:
                raise ValidationError("叶子项必须挂在 section 下，不能置顶")
            new_parent = await self._resolve_parent(dto.parent_code)
            if new_parent == row.id:
                raise ValidationError("parent 不能是自己")
            if await self._would_create_menu_cycle(code, dto.parent_code):
                raise ValidationError("不能将节点移动到自身下级（会导致循环引用）")
            row.parent_id = new_parent
        if "label_key" in provided and dto.label_key is not None:
            row.label_key = dto.label_key
        if "icon_code" in provided:
            row.icon_code = dto.icon_code
        if "sort_order" in provided and dto.sort_order is not None:
            row.sort_order = dto.sort_order
        if "path" in provided:
            row.path = dto.path
        if "permission_code" in provided:
            row.permission_code = dto.permission_code
        if "visible" in provided and dto.visible is not None:
            row.visible = dto.visible
        row.updated_time = datetime.now(timezone.utc)
        await self._session.flush()
        await self._outbox.enqueue(
            self._session,
            event_type="menu_updated",
            entity_type="menu",
            entity_id=row.id,
            actor=actor.userId,
            payload={"before": before, "after": _row_payload(row)},
        )
        return row

    async def delete(self, code: str, actor: CurrentUser) -> None:
        """删除菜单节点。section 含子项 → 409（需先删子项）。
        叶子项删除时 FK 级联清掉 permission_grant。"""
        row = await self._get_by_code(code)
        has_children = (
            await self._session.execute(
                select(MenuConfig.id).where(MenuConfig.parent_id == row.id).limit(1)
            )
        ).scalars().first()
        if has_children is not None:
            raise ConflictError(f"菜单含子项，请先删除子项: {code}")
        before = _row_payload(row)
        await self._session.delete(row)
        await self._session.flush()
        await self._outbox.enqueue(
            self._session,
            event_type="menu_deleted",
            entity_type="menu",
            entity_id=row.id,
            actor=actor.userId,
            payload={"before": before, "after": None},
        )
