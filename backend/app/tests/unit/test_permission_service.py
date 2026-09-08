"""PermissionService._composeEffective 纯逻辑单测（feat-rbac-identity）。

合集语义：
- admin 角色 → 旁路，menu_codes = all_menu_codes
- 非 admin → 直接授权 ∪ 角色授权 ∪ 组织授权（只含用户持有主体）
- 输出按码排序；role_grants/organization_grants 仅含持有主体

纯函数无 DB / 无 IO，不依赖 fixtures。
"""

from __future__ import annotations

from app.services.permission_service import (
    SubjectGrantView,
    _composeEffective,
)

ALL_MENUS = frozenset({"a", "b", "c", "d", "e", "f"})


def _compose(**kw):
    defaults = dict(
        user_id=1,
        role_meta={},
        organization_meta={},
        role_grants={},
        organization_grants={},
        direct_codes=frozenset(),
        all_menu_codes=ALL_MENUS,
    )
    defaults.update(kw)
    return _composeEffective(defaults.pop("user_id"), **defaults)


class TestSuperuserBypass:
    def test_admin_role_gets_everything_even_with_no_grants(self) -> None:
        """admin 角色无任何授权时合集仍是全量 grantable 菜单。"""
        eff = _compose(
            role_meta={10: ("admin", "系统管理员")},
        )
        assert eff.is_superuser is True
        assert eff.menu_codes == ("a", "b", "c", "d", "e", "f")
        assert eff.direct_menu_codes == ()
        assert eff.role_codes == ("admin",)

    def test_admin_besides_normal_role_still_bypasses(self) -> None:
        """用户同时持有普通角色与 admin → 仍旁路（超管优先级最高）。"""
        eff = _compose(
            role_meta={1: ("analyst", "分析师"), 9: ("admin", "系统管理员")},
            direct_codes=frozenset({"a"}),
        )
        assert eff.is_superuser is True
        assert eff.menu_codes == ("a", "b", "c", "d", "e", "f")


class TestUnionSemantics:
    def test_union_of_direct_role_org_is_sorted_deduped(self) -> None:
        """三维来源合集 + 去重 + 排序输出。"""
        eff = _compose(
            role_meta={1: ("analyst", "分析师")},
            organization_meta={7: ("procurement", "采购部")},
            direct_codes=frozenset({"a", "c"}),
            role_grants={1: frozenset({"b", "c"})},
            organization_grants={7: frozenset({"d", "a"})},
        )
        assert eff.menu_codes == ("a", "b", "c", "d")
        assert eff.direct_menu_codes == ("a", "c")

    def test_union_ignores_grants_of_non_held_subjects(self) -> None:
        """grants 里出现用户未持有的角色/组织 → 不得并入合集与拆分。"""
        eff = _compose(
            role_meta={1: ("analyst", "分析师")},
            organization_meta={7: ("procurement", "采购部")},
            direct_codes=frozenset({"a"}),
            role_grants={1: frozenset({"b"}), 999: frozenset({"e"})},
            organization_grants={7: frozenset({"c"}), 888: frozenset({"f"})},
        )
        assert eff.menu_codes == ("a", "b", "c")
        assert eff.role_grants == (
            SubjectGrantView(subject_id=1, code="analyst", name="分析师", menu_codes=("b",)),
        )
        assert len(eff.organization_grants) == 1

    def test_empty_subjects_and_grants_yields_empty_union(self) -> None:
        """无角色/组织/直接授权 → 空合集，非超管。"""
        eff = _compose(direct_codes=frozenset())
        assert eff.menu_codes == ()
        assert eff.is_superuser is False
        assert eff.role_codes == ()
        assert eff.organization_codes == ()

    def test_multiple_roles_organizations_breakdown(self) -> None:
        """多角色/多组织拆分各自成组，不交叉。"""
        eff = _compose(
            role_meta={1: ("analyst", "分析师"), 2: ("viewer", "只读")},
            organization_meta={7: ("procurement", "采购部"), 8: ("quality", "质量部")},
            role_grants={1: frozenset({"a"}), 2: frozenset({"b"})},
            organization_grants={7: frozenset({"c"}), 8: frozenset({"d"})},
        )
        assert eff.menu_codes == ("a", "b", "c", "d")
        assert [g.code for g in eff.role_grants] == ["analyst", "viewer"]
        assert [g.menu_codes for g in eff.role_grants] == [("a",), ("b",)]
        assert [g.code for g in eff.organization_grants] == ["procurement", "quality"]

    def test_superuser_breakdown_still_lists_its_role_grants(self) -> None:
        """超管：合集为全量，但来源拆分仍展示其真实授权（供审计 UI）。"""
        eff = _compose(
            role_meta={9: ("admin", "系统管理员"), 1: ("analyst", "分析师")},
            role_grants={1: frozenset({"a"})},
        )
        assert eff.menu_codes == ("a", "b", "c", "d", "e", "f")
        # role_grants 按 subject_id 排序：analyst(1) 在前，admin(9) 在后
        assert [g.code for g in eff.role_grants] == ["analyst", "admin"]
        assert [g.menu_codes for g in eff.role_grants] == [("a",), ()]
