"""WikiCategoryService 单测（feat-wiki-category，real PG per Harness 测试规范）。

覆盖：
  1. 建树：rows → nested tree，sort_order 升序
  2. CRUD：create / update / delete
  3. 循环检测：把 parent 设成自己的后代 → ValidationError
  4. 详情：getCategory
  5. 删除带子分类 → 子分类 parent_id 自动 SET NULL
"""
from __future__ import annotations

from app.services.wiki_category_service import (
    CategoryNotFoundError, CycleError, WikiCategoryService,
)

_svc = WikiCategoryService()


async def _make(dbSession, *, name: str, parent_id: int | None = None,
                sort_order: int = 0, description: str | None = None) -> int:
    row = await _svc.createCategory(
        dbSession, name=name, parent_id=parent_id,
        sort_order=sort_order, description=description,
    )
    await dbSession.commit()
    return row.id


async def test_list_tree_builds_nested_structure(dbSession):
    a = await _make(dbSession, name="采购管理", sort_order=0)
    b = await _make(dbSession, name="质量管理", sort_order=1)
    c = await _make(dbSession, name="供应商准入", parent_id=a, sort_order=0)
    d = await _make(dbSession, name="IQC 来料", parent_id=b, sort_order=0)

    tree = await _svc.listCategoriesTree(dbSession)
    by_name = {node.name: node for node in tree}
    assert set(by_name) == {"采购管理", "质量管理"}
    assert [c.name for c in by_name["采购管理"].children] == ["供应商准入"]
    assert [c.name for c in by_name["质量管理"].children] == ["IQC 来料"]


async def test_list_tree_orders_by_sort_order(dbSession):
    a = await _make(dbSession, name="根", sort_order=0)
    await _make(dbSession, name="子C", parent_id=a, sort_order=2)
    await _make(dbSession, name="子A", parent_id=a, sort_order=0)
    await _make(dbSession, name="子B", parent_id=a, sort_order=1)

    tree = await _svc.listCategoriesTree(dbSession)
    [root] = tree
    assert [c.name for c in root.children] == ["子A", "子B", "子C"]


async def test_create_category_returns_persisted_row(dbSession):
    row = await _svc.createCategory(
        dbSession, name="采购管理", parent_id=None,
        sort_order=0, description="顶级业务域",
    )
    await dbSession.commit()
    assert row.id > 0
    assert row.name == "采购管理"
    assert row.description == "顶级业务域"


async def test_update_category_changes_fields(dbSession):
    cid = await _make(dbSession, name="原名", sort_order=0)
    updated = await _svc.updateCategory(
        dbSession, category_id=cid, name="新名", sort_order=5,
    )
    await dbSession.commit()
    assert updated.name == "新名"
    assert updated.sort_order == 5


async def test_cycle_detection_rejects_self_as_descendant(dbSession):
    a = await _make(dbSession, name="A", sort_order=0)
    b = await _make(dbSession, name="B", parent_id=a, sort_order=0)
    c = await _make(dbSession, name="C", parent_id=b, sort_order=0)

    # 试图把 A 设为 C 的 parent → 形成 A→B→C→A 的环
    import pytest
    with pytest.raises(CycleError):
        await _svc.updateCategory(dbSession, category_id=a, parent_id=c)


async def test_get_category_raises_404(dbSession):
    import pytest
    with pytest.raises(CategoryNotFoundError):
        await _svc.getCategory(dbSession, category_id=999999)


async def test_delete_category_cascades_children_to_null(dbSession):
    a = await _make(dbSession, name="A", sort_order=0)
    b = await _make(dbSession, name="B", parent_id=a, sort_order=0)

    await _svc.deleteCategory(dbSession, category_id=a)
    await dbSession.commit()

    # A 已被删
    import pytest
    with pytest.raises(CategoryNotFoundError):
        await _svc.getCategory(dbSession, category_id=a)
    # B 仍然在，但 parent_id 变 None（DB ON DELETE SET NULL）
    orphan = await _svc.getCategory(dbSession, category_id=b)
    assert orphan.parent_id is None


async def test_wiki_page_can_link_category(dbSession):
    """feat-wiki-category 闭环：page 挂到 category，category 删除后 page 自动脱钩。"""
    from app.domain.models import WikiPage
    from datetime import datetime, timezone
    from app.domain.wiki_schemas import WikiPageUpdate
    from app.services.wiki_page_service import WikiPageService

    cat_id = await _make(dbSession, name="采购管理", sort_order=0)

    page = WikiPage(
        page_id="PAGE-TEST-001", title="测试页", content="c",
        status="EFFECTIVE", created_time=datetime.now(timezone.utc),
        updated_time=datetime.now(timezone.utc),
    )
    dbSession.add(page)
    await dbSession.flush()

    # PATCH 挂分类
    updated = await WikiPageService().updatePage(
        dbSession, pageId="PAGE-TEST-001",
        dto=WikiPageUpdate(category_id=cat_id),
    )
    await dbSession.commit()
    assert updated.category_id == cat_id

    # 删分类 → page.category_id 自动 SET NULL（DB ON DELETE SET NULL）
    await _svc.deleteCategory(dbSession, category_id=cat_id)
    await dbSession.commit()

    dbSession.expire_all()
    reloaded = await WikiPageService().getPage(dbSession, "PAGE-TEST-001")
    assert reloaded.category_id is None


async def test_wiki_page_update_rejects_unknown_category(dbSession):
    from app.domain.models import WikiPage
    from datetime import datetime, timezone
    from app.domain.wiki_schemas import WikiPageUpdate
    from app.services.wiki_page_service import WikiPageService
    from app.domain.exceptions import NotFoundError

    page = WikiPage(
        page_id="PAGE-TEST-002", title="测试页", content="c",
        status="EFFECTIVE", created_time=datetime.now(timezone.utc),
        updated_time=datetime.now(timezone.utc),
    )
    dbSession.add(page)
    await dbSession.flush()

    import pytest
    with pytest.raises(NotFoundError):
        await WikiPageService().updatePage(
            dbSession, pageId="PAGE-TEST-002",
            dto=WikiPageUpdate(category_id=999999),
        )