"""Wiki Category 分类树形层级服务（feat-wiki-category）。

职责边界：
- 自引用树 CRUD（create/update/delete + cycle 检测）
- ``listCategoriesTree`` 一次取全量行，内存 ``children_by_parent`` 建树，
  避免 N+1（与 menu_config_service._buildSections 同模式）。

不负责：
- WikiPage 业务字段（仍由 WikiPageService 维护）
- 权限校验（router 层统一处理）
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.wiki_models import WikiCategory


class CategoryNotFoundError(NotFoundError):
    pass


class CycleError(ValidationError):
    """把分类的 parent 设成自己的后代会形成环——拒绝并 422。"""


@dataclass(frozen=True)
class WikiCategoryNode:
    """树形 UI 直接消费的节点结构（与 ORM 解耦）。

    ORM WikiCategory 自带 children 关系，但那是 SA 懒加载的副作用；
    这里由 listCategoriesTree 一次性显式建树，避免触发额外 round-trip。
    """

    id: int
    parent_id: int | None
    sort_order: int
    name: str
    description: str | None
    page_id: str | None
    children: list["WikiCategoryNode"]


_SENTINEL_UNSET = object()


class WikiCategoryService:
    """分类树 CRUD + 树形查询。"""

    async def listCategoriesTree(self, session: AsyncSession) -> list[WikiCategoryNode]:
        """一次拉全量行，按 (parent_id, sort_order) 内存建树。

        行数预期 < 几百（admin 维护的目录），全量加载安全。
        """
        rows = (await session.execute(
            select(WikiCategory).order_by(
                WikiCategory.parent_id.is_(None).desc(),  # 根（parent_id IS NULL）排前
                WikiCategory.parent_id.asc(),
                WikiCategory.sort_order.asc(),
                WikiCategory.id.asc(),
            )
        )).scalars().all()

        return _buildTree(rows)

    async def createCategory(
        self,
        session: AsyncSession,
        *,
        name: str,
        parent_id: int | None,
        sort_order: int,
        description: str | None = None,
        page_id: str | None = None,
    ) -> WikiCategory:
        if parent_id is not None:
            await self._ensureCategoryExists(session, parent_id)
        row = WikiCategory(
            name=name, parent_id=parent_id,
            sort_order=sort_order, description=description, page_id=page_id,
        )
        session.add(row)
        await session.flush()
        return row

    async def updateCategory(
        self,
        session: AsyncSession,
        *,
        category_id: int,
        name=None,
        parent_id=_SENTINEL_UNSET,
        sort_order=_SENTINEL_UNSET,
        description=_SENTINEL_UNSET,
        page_id=_SENTINEL_UNSET,
    ) -> WikiCategory:
        row = await self._loadOrRaise(session, category_id)
        if name is not None:
            row.name = name
        if parent_id is not _SENTINEL_UNSET:
            if parent_id == category_id:
                raise CycleError(f"category {category_id} cannot be its own parent")
            if parent_id is not None:
                await self._ensureCategoryExists(session, parent_id)
                # 检查环：parent 不能是 category_id 的后代（即不能挂到自己下面）
                if await self._isDescendant(session, ancestor_id=category_id, candidate_id=parent_id):
                    raise CycleError(
                        f"setting parent={parent_id} on {category_id} would form a cycle"
                    )
            row.parent_id = parent_id
        if sort_order is not _SENTINEL_UNSET:
            row.sort_order = sort_order
        if description is not _SENTINEL_UNSET:
            row.description = description
        if page_id is not _SENTINEL_UNSET:
            row.page_id = page_id
        await session.flush()
        return row

    async def deleteCategory(self, session: AsyncSession, *, category_id: int) -> None:
        row = await self._loadOrRaise(session, category_id)
        await session.delete(row)
        await session.flush()
        # ON DELETE SET NULL 让子分类 parent_id 自动升级为根

    async def getCategory(self, session: AsyncSession, *, category_id: int) -> WikiCategoryNode:
        """单节点读（不带子树），用于详情页 / 关联校验。"""
        row = await self._loadOrRaise(session, category_id)
        return WikiCategoryNode(
            id=row.id, parent_id=row.parent_id, sort_order=row.sort_order,
            name=row.name, description=row.description, page_id=row.page_id,
            children=[],
        )

    # ---- private helpers ----

    async def _loadOrRaise(self, session: AsyncSession, category_id: int) -> WikiCategory:
        row = (await session.execute(
            select(WikiCategory).where(WikiCategory.id == category_id)
        )).scalar_one_or_none()
        if row is None:
            raise CategoryNotFoundError(f"wiki_category {category_id} not found")
        return row

    async def _ensureCategoryExists(self, session: AsyncSession, category_id: int) -> None:
        exists = (await session.execute(
            select(WikiCategory.id).where(WikiCategory.id == category_id)
        )).scalar_one_or_none()
        if exists is None:
            raise CategoryNotFoundError(f"wiki_category {category_id} not found")

    async def _isDescendant(
        self, session: AsyncSession, *, ancestor_id: int, candidate_id: int,
    ) -> bool:
        """candidate_id 是否是 ancestor_id 的后代（用于循环检测）。

        递归 CTE 防深链 O(n)——分类树深度预期 ≤ 5 层，朴素的 BFS 也可，
        但 CTE 与 org tree 现成模式一致（见 backend/app/services/org_service.py）。
        """
        from sqlalchemy import text
        sql = text("""
            WITH RECURSIVE descendants AS (
                SELECT id, parent_id FROM wiki_category WHERE id = :ancestor_id
                UNION ALL
                SELECT wc.id, wc.parent_id
                FROM wiki_category wc
                JOIN descendants d ON wc.parent_id = d.id
            )
            SELECT 1 FROM descendants WHERE id = :candidate_id LIMIT 1
        """)
        return (await session.execute(sql, {"ancestor_id": ancestor_id, "candidate_id": candidate_id})).first() is not None


def _buildTree(rows: list[WikiCategory]) -> list[WikiCategoryNode]:
    """children_by_parent 字典分组建树（与 menu_config_service 同模式）。"""
    children_by_parent: dict[int | None, list[WikiCategory]] = {}
    for row in rows:
        children_by_parent.setdefault(row.parent_id, []).append(row)

    def _to_node(row: WikiCategory) -> WikiCategoryNode:
        kids = children_by_parent.get(row.id, [])
        return WikiCategoryNode(
            id=row.id, parent_id=row.parent_id, sort_order=row.sort_order,
            name=row.name, description=row.description, page_id=row.page_id,
            children=[_to_node(k) for k in kids],
        )

    roots = children_by_parent.get(None, [])
    return [_to_node(r) for r in roots]