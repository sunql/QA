"""RBAC 身份/授权 REST API 集成测试（feat-rbac-identity, 真实 PG + 完整 API 链路）。

覆盖需求 #1-#4：
- 用户 / 角色 / 组织 CRUD（含内置 admin 防护、last-admin 防护、403 非 admin）
- 三维度授权 set-replace（USER/ROLE/ORGANIZATION）
- 有效权限查询（用户视图含来源拆分；角色/组织视图含直接授权码）
- 合集语义：direct ∪ role ∪ org，重合去重
- admin（超管）旁路：menuCodes = 全部可见叶子菜单
- 菜单管理 CRUD（动态新增/编辑/删除；section 删除前必须无子项）
- mass-assignment 防护：POST /users 夹带 roleIds 不产生授权

每测试 TRUNCATE 清库；autouse fixture 幂等 seed 基线（admin 用户/角色）+ 菜单行。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    Organization,
    PermissionGrant,
    Role,
    User,
    UserOrganization,
    UserRole,
)
from app.models.menu_config import MenuConfig
from app.models.rbac import ADMIN_ROLE_CODE

AUTH_ADMIN = {
    "X-User-Id": "admin",
    "X-User-Roles": ADMIN_ROLE_CODE,
    "X-User-Departments": "IT",
}

MENU_A, MENU_B, MENU_C, MENU_D = "menu_a", "menu_b", "menu_c", "menu_d"
LEAF_MENUS = (MENU_A, MENU_B, MENU_C, MENU_D)


def _auth_as(user_id: str, roles: str = "analyst") -> dict[str, str]:
    return {
        "X-User-Id": user_id,
        "X-User-Roles": roles,
        "X-User-Departments": "procurement",
    }


async def _seedMenus(dbSession: AsyncSession) -> None:
    """建一个 section + 4 个叶子菜单（leaf code 作为可授权目标）。"""
    section = MenuConfig(code="adminsys", label_key="admin_sys", sort_order=90)
    section.children = [
        MenuConfig(code=code, label_key=code, path=f"/admin/{code}", sort_order=i)
        for i, code in enumerate(LEAF_MENUS)
    ]
    dbSession.add(section)
    await dbSession.commit()


async def _mkUser(client: AsyncClient, username: str) -> dict:
    r = await client.post(
        "/api/v1/users", headers=AUTH_ADMIN,
        json={"username": username, "display_name": username, "email": None},
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture(autouse=True)
async def _rbacSeeds(client: AsyncClient, dbSession: AsyncSession) -> None:
    """幂等 seed RBAC 基线（admin 用户/角色）+ 菜单行，保证 admin 身份可用。"""
    from scripts.seed_rbac import seedRbacBaseline

    await seedRbacBaseline(dbSession)
    await _seedMenus(dbSession)
    assert client is not None  # 确保 client 已构造（TRUNCATE + app 就绪）


# --------------------------------------------------------------------------- users


class TestUsersCrud:
    async def test_create_and_list(self, client: AsyncClient) -> None:
        body = await _mkUser(client, "alice")
        assert body["username"] == "alice"
        assert body["enabled"] is True
        assert body["roleIds"] == [] and body["roleCodes"] == []

        r = await client.get("/api/v1/users", headers=AUTH_ADMIN)
        assert r.status_code == 200, r.text
        names = {u["username"]: u for u in r.json()}
        assert "admin" in names and "alice" in names
        # admin 基线用户应已绑定 admin 角色
        assert "admin" in names["admin"]["roleCodes"]

    async def test_create_409_duplicate_username(self, client: AsyncClient) -> None:
        await _mkUser(client, "alice")
        r = await client.post(
            "/api/v1/users", headers=AUTH_ADMIN,
            json={"username": "alice", "display_name": "dup"},
        )
        assert r.status_code == 409, r.text

    async def test_get_update_delete(self, client: AsyncClient) -> None:
        user = await _mkUser(client, "alice")

        g = await client.get(f"/api/v1/users/{user['id']}", headers=AUTH_ADMIN)
        assert g.status_code == 200 and g.json()["username"] == "alice"

        u = await client.put(
            f"/api/v1/users/{user['id']}", headers=AUTH_ADMIN,
            json={"display_name": "Alicia", "email": "a@x.com"},
        )
        assert u.status_code == 200, u.text
        assert u.json()["displayName"] == "Alicia"
        assert u.json()["email"] == "a@x.com"

        # email 空串清空
        u2 = await client.put(
            f"/api/v1/users/{user['id']}", headers=AUTH_ADMIN,
            json={"email": ""},
        )
        assert u2.status_code == 200 and u2.json()["email"] is None

        d = await client.delete(f"/api/v1/users/{user['id']}", headers=AUTH_ADMIN)
        assert d.status_code == 204
        g2 = await client.get(f"/api/v1/users/{user['id']}", headers=AUTH_ADMIN)
        assert g2.status_code == 404

    async def test_404_unknown(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/users/999999", headers=AUTH_ADMIN)
        assert r.status_code == 404

    async def test_write_403_for_non_admin(self, client: AsyncClient) -> None:
        await _mkUser(client, "alice")  # 保证 alice 在 DB 中存在（Phase D 接真实用户后仍非 admin）
        r = await client.post(
            "/api/v1/users", headers=_auth_as("alice"),
            json={"username": "bob", "display_name": "bob"},
        )
        assert r.status_code == 403, r.text

    async def test_cannot_delete_last_admin(self, client: AsyncClient) -> None:
        # 基线仅 admin 一个超管；删 admin → 409
        admin_row = next(
            u
            for u in (await client.get("/api/v1/users", headers=AUTH_ADMIN)).json()
            if u["username"] == "admin"
        )
        r = await client.delete(
            f"/api/v1/users/{admin_row['id']}", headers=AUTH_ADMIN
        )
        assert r.status_code == 409, r.text
        # 查询确认 admin 用户仍在
        g = await client.get("/api/v1/users", headers=AUTH_ADMIN)
        assert "admin" in {u["username"] for u in g.json()}

    async def test_cannot_disable_last_admin(self, client: AsyncClient) -> None:
        # 基线仅 admin 一个超管；停用 admin → 409（update_user 与 delete 同红线）
        admin_row = next(
            u
            for u in (await client.get("/api/v1/users", headers=AUTH_ADMIN)).json()
            if u["username"] == "admin"
        )
        r = await client.put(
            f"/api/v1/users/{admin_row['id']}", headers=AUTH_ADMIN,
            json={"enabled": False},
        )
        assert r.status_code == 409, r.text
        # 停用未生效：admin 仍 enabled
        g = await client.get(f"/api/v1/users/{admin_row['id']}", headers=AUTH_ADMIN)
        assert g.json()["enabled"] is True

    async def test_cannot_disable_last_admin_when_two_admins(self, client: AsyncClient) -> None:
        # 再造第二个 admin 用户后，停用其一允许（保留超管入口）
        admin_role_id = next(
            r["id"]
            for r in (await client.get("/api/v1/roles", headers=AUTH_ADMIN)).json()
            if r["code"] == ADMIN_ROLE_CODE
        )
        alice = await _mkUser(client, "alice")
        await _setUserRoles(client, alice["id"], [admin_role_id])
        r = await client.put(
            f"/api/v1/users/{alice['id']}", headers=AUTH_ADMIN,
            json={"enabled": False},
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False

    async def test_mass_assignment_roleIds_ignored(self, client: AsyncClient) -> None:
        # 建立 analyst 角色，取 admin 角色 id；POST /users 夹带 roleIds 不应产生授权
        rrole = await client.post(
            "/api/v1/roles", headers=AUTH_ADMIN,
            json={"code": "analyst", "name": "分析师"},
        )
        assert rrole.status_code == 201, rrole.text
        role = rrole.json()
        roles = await client.get("/api/v1/roles", headers=AUTH_ADMIN)
        admin_role_id = next(
            r["id"] for r in roles.json() if r["code"] == ADMIN_ROLE_CODE
        )

        r = await client.post(
            "/api/v1/users", headers=AUTH_ADMIN,
            json={
                "username": "eve",
                "display_name": "eve",
                "role_ids": [admin_role_id, role["id"]],  # 试图越权授予
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["roleIds"] == [] and body["roleCodes"] == []
        g = await client.get(f"/api/v1/users/{body['id']}", headers=AUTH_ADMIN)
        assert g.json()["roleIds"] == []


# --------------------------------------------------------------------------- roles


class TestRolesCrud:
    async def test_create_list_get(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/v1/roles", headers=AUTH_ADMIN,
            json={"code": "analyst", "name": "分析师", "description": "只读分析"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["code"] == "analyst"
        assert body["isBuiltin"] is False

        lst = await client.get("/api/v1/roles", headers=AUTH_ADMIN)
        codes = {x["code"]: x for x in lst.json()}
        assert codes[ADMIN_ROLE_CODE]["isBuiltin"] is True
        assert "analyst" in codes

        g = await client.get(f"/api/v1/roles/{body['id']}", headers=AUTH_ADMIN)
        assert g.status_code == 200 and g.json()["name"] == "分析师"

    async def test_create_admin_code_422(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/v1/roles", headers=AUTH_ADMIN,
            json={"code": ADMIN_ROLE_CODE, "name": "偷建超管"},
        )
        assert r.status_code == 422, r.text

    async def test_create_code_pattern_namespace(self, client: AsyncClient) -> None:
        """_CODE_PATTERN：首段小写+数字/下划线，后续段允许 camelCase（与 menu seed 同款）。"""
        # 接受：点分 + camelCase + 下划线
        for code in ["analyst", "view_audit", "data.manager", "section.owner"]:
            r = await client.post(
                "/api/v1/roles", headers=AUTH_ADMIN,
                json={"code": code, "name": code},
            )
            assert r.status_code == 201, f"{code}: {r.text}"
        # 拒绝：大写开头 / 数字开头 / 下划线开头 / 空段 / 连续点
        for code in ["Admin", "1abc", "_private", "data..mgr", ".admin", "admin."]:
            r = await client.post(
                "/api/v1/roles", headers=AUTH_ADMIN,
                json={"code": code, "name": code},
            )
            assert r.status_code == 422, f"{code}: {r.text}"

    async def test_create_409_duplicate(self, client: AsyncClient) -> None:
        payload = {"code": "analyst", "name": "分析师"}
        r1 = await client.post("/api/v1/roles", headers=AUTH_ADMIN, json=payload)
        assert r1.status_code == 201
        r2 = await client.post("/api/v1/roles", headers=AUTH_ADMIN, json=payload)
        assert r2.status_code == 409, r2.text

    async def test_update_delete(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/v1/roles", headers=AUTH_ADMIN,
            json={"code": "analyst", "name": "分析师"},
        )
        rid = r.json()["id"]
        u = await client.put(
            f"/api/v1/roles/{rid}", headers=AUTH_ADMIN,
            json={"name": "高级分析师"},
        )
        assert u.status_code == 200 and u.json()["name"] == "高级分析师"
        d = await client.delete(f"/api/v1/roles/{rid}", headers=AUTH_ADMIN)
        assert d.status_code == 204
        g = await client.get(f"/api/v1/roles/{rid}", headers=AUTH_ADMIN)
        assert g.status_code == 404

    async def test_cannot_delete_builtin_admin(self, client: AsyncClient) -> None:
        roles = await client.get("/api/v1/roles", headers=AUTH_ADMIN)
        admin_role_id = next(
            r["id"] for r in roles.json() if r["code"] == ADMIN_ROLE_CODE
        )
        d = await client.delete(f"/api/v1/roles/{admin_role_id}", headers=AUTH_ADMIN)
        assert d.status_code == 409, d.text

    async def test_403_non_admin(self, client: AsyncClient) -> None:
        await _mkUser(client, "alice")
        r = await client.post(
            "/api/v1/roles", headers=_auth_as("alice"),
            json={"code": "x", "name": "x"},
        )
        assert r.status_code == 403, r.text


# ----------------------------------------------------------------- organizations


class TestOrganizationsCrud:
    async def test_create_list_get_update_delete(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "procurement", "name": "采购部"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["code"] == "procurement" and body["parentId"] is None

        lst = await client.get("/api/v1/organizations", headers=AUTH_ADMIN)
        assert "procurement" in {o["code"] for o in lst.json()}

        g = await client.get(f"/api/v1/organizations/{body['id']}", headers=AUTH_ADMIN)
        assert g.status_code == 200

        u = await client.put(
            f"/api/v1/organizations/{body['id']}", headers=AUTH_ADMIN,
            json={"name": "全球采购中心"},
        )
        assert u.status_code == 200 and u.json()["name"] == "全球采购中心"

        d = await client.delete(f"/api/v1/organizations/{body['id']}", headers=AUTH_ADMIN)
        assert d.status_code == 204
        g2 = await client.get(f"/api/v1/organizations/{body['id']}", headers=AUTH_ADMIN)
        assert g2.status_code == 404

    async def test_delete_org_with_child_409(self, client: AsyncClient) -> None:
        parent = await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "group", "name": "集团"},
        )
        child = await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "sub", "name": "子公司", "parent_id": parent.json()["id"]},
        )
        assert child.status_code == 201, child.text
        d = await client.delete(
            f"/api/v1/organizations/{parent.json()['id']}", headers=AUTH_ADMIN
        )
        assert d.status_code == 409, d.text

    async def test_update_self_parent_422(self, client: AsyncClient) -> None:
        org = await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "self_parent", "name": "自引用"},
        )
        assert org.status_code == 201, org.text
        r = await client.put(
            f"/api/v1/organizations/{org.json()['id']}", headers=AUTH_ADMIN,
            json={"parent_id": org.json()["id"]},
        )
        assert r.status_code == 422, r.text
        g = await client.get(f"/api/v1/organizations/{org.json()['id']}", headers=AUTH_ADMIN)
        assert g.json()["parentId"] is None  # 未被自引用污染

    async def test_update_cycle_422(self, client: AsyncClient) -> None:
        """循环依赖检测：A.parent=B → B.parent=A 应被拒（A 是 B 的后代）。"""
        a = (await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "cycle_a", "name": "A"},
        )).json()
        b = (await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "cycle_b", "name": "B", "parent_id": a["id"]},
        )).json()
        # 把 A.parent 改成 B（A 已是 B 的后代 → 会成环）
        r = await client.put(
            f"/api/v1/organizations/{a['id']}", headers=AUTH_ADMIN,
            json={"parent_id": b["id"]},
        )
        assert r.status_code == 422, r.text
        # 验证 A.parent 未变（仍是 None）
        g = await client.get(f"/api/v1/organizations/{a['id']}", headers=AUTH_ADMIN)
        assert g.json()["parentId"] is None

    async def test_update_deep_cycle_422(self, client: AsyncClient) -> None:
        """深层循环：A→B→C，把 A.parent 改成 C（A 是 C 的后代）。"""
        a = (await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "deep_a", "name": "A"},
        )).json()
        b = (await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "deep_b", "name": "B", "parent_id": a["id"]},
        )).json()
        c = (await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "deep_c", "name": "C", "parent_id": b["id"]},
        )).json()
        # A→B→C：A.parent=C 会成环（A 是 C 的祖先走 C→B→A）
        r = await client.put(
            f"/api/v1/organizations/{a['id']}", headers=AUTH_ADMIN,
            json={"parent_id": c["id"]},
        )
        assert r.status_code == 422, r.text

    async def test_tree_api(self, client: AsyncClient) -> None:
        """GET /organizations/tree 返回嵌套树，多根按 sort_order 排序。"""
        a = (await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "tree_a", "name": "集团A", "sort_order": 20},
        )).json()
        # B 是更早的根（sort_order=10）→ 应排在 A 前
        b = (await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "tree_b", "name": "集团B", "sort_order": 10},
        )).json()
        # A 的两个子节点
        a1 = (await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "tree_a1", "name": "A-采购", "parent_id": a["id"], "sort_order": 1},
        )).json()
        a2 = (await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "tree_a2", "name": "A-研发", "parent_id": a["id"], "sort_order": 2},
        )).json()
        r = await client.get("/api/v1/organizations/tree", headers=AUTH_ADMIN)
        assert r.status_code == 200, r.text
        roots = r.json()
        assert len(roots) == 2
        # 根按 sort_order：B(10) 在 A(20) 前
        assert roots[0]["code"] == "tree_b" and roots[1]["code"] == "tree_a"
        a_node = roots[1]
        assert len(a_node["children"]) == 2
        assert a_node["children"][0]["code"] == "tree_a1"
        assert a_node["children"][1]["code"] == "tree_a2"
        # 字段齐全
        for n in [a_node] + a_node["children"]:
            assert {"id", "code", "name", "sortOrder", "description", "children"} <= set(n.keys())
        # 删除带子节点的 A → 409
        d = await client.delete(f"/api/v1/organizations/{a['id']}", headers=AUTH_ADMIN)
        assert d.status_code == 409, d.text

    async def test_sort_order_in_payload(self, client: AsyncClient) -> None:
        """create 返回 sortOrder 字段；update 可独立修改。"""
        r1 = await client.post(
            "/api/v1/organizations", headers=AUTH_ADMIN,
            json={"code": "sort_org", "name": "排序测试", "sort_order": 42},
        )
        assert r1.status_code == 201, r1.text
        assert r1.json()["sortOrder"] == 42
        oid = r1.json()["id"]
        r2 = await client.put(
            f"/api/v1/organizations/{oid}", headers=AUTH_ADMIN,
            json={"sort_order": 100},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["sortOrder"] == 100


# ------------------------------------------------------------------ 授权 + 合集


async def _setUserRoles(client: AsyncClient, user_id: int, role_ids: list[int]) -> None:
    r = await client.put(
        f"/api/v1/users/{user_id}/roles", headers=AUTH_ADMIN,
        json={"role_ids": role_ids},
    )
    assert r.status_code == 204, r.text


async def _setUserOrgs(client: AsyncClient, user_id: int, org_ids: list[int]) -> None:
    r = await client.put(
        f"/api/v1/users/{user_id}/organizations", headers=AUTH_ADMIN,
        json={"organization_ids": org_ids},
    )
    assert r.status_code == 204, r.text


async def _grant(client: AsyncClient, path: str, menu_codes: list[str]) -> None:
    r = await client.put(
        path, headers=AUTH_ADMIN, json={"menu_codes": menu_codes}
    )
    assert r.status_code == 204, r.text


class TestEffectivePermissions:
    """需求 #3 合集语义 + #4 用户有效权限视图（来源拆分）。"""

    async def test_union_of_three_dimensions(self, client: AsyncClient) -> None:
        # role R + org O + 用户 alice
        role = (
            await client.post(
                "/api/v1/roles", headers=AUTH_ADMIN,
                json={"code": "analyst", "name": "分析师"},
            )
        ).json()
        org = (
            await client.post(
                "/api/v1/organizations", headers=AUTH_ADMIN,
                json={"code": "procurement", "name": "采购部"},
            )
        ).json()
        alice = await _mkUser(client, "alice")
        await _setUserRoles(client, alice["id"], [role["id"]])
        await _setUserOrgs(client, alice["id"], [org["id"]])

        # role → {A,B}；org → {B,C}；direct → {C,D}；有效合集 = {A,B,C,D}
        await _grant(client, f"/api/v1/roles/{role['id']}/permissions", [MENU_A, MENU_B])
        await _grant(
            client, f"/api/v1/organizations/{org['id']}/permissions", [MENU_B, MENU_C]
        )
        await _grant(client, f"/api/v1/users/{alice['id']}/permissions", [MENU_C, MENU_D])

        r = await client.get(
            f"/api/v1/users/{alice['id']}/permissions", headers=AUTH_ADMIN
        )
        assert r.status_code == 200, r.text
        eff = r.json()
        assert eff["isSuperuser"] is False
        assert eff["menuCodes"] == sorted(LEAF_MENUS)
        assert eff["directGrants"] == [MENU_C, MENU_D]
        # role/org 来源拆分
        role_grants = {g["code"]: g["menuCodes"] for g in eff["roleGrants"]}
        org_grants = {g["code"]: g["menuCodes"] for g in eff["organizationGrants"]}
        assert role_grants["analyst"] == [MENU_A, MENU_B]
        assert org_grants["procurement"] == [MENU_B, MENU_C]

    async def test_set_replace_revokes(self, client: AsyncClient) -> None:
        role = (
            await client.post(
                "/api/v1/roles", headers=AUTH_ADMIN,
                json={"code": "analyst", "name": "分析师"},
            )
        ).json()
        alice = await _mkUser(client, "alice")
        await _setUserRoles(client, alice["id"], [role["id"]])
        await _grant(client, f"/api/v1/roles/{role['id']}/permissions", [MENU_A, MENU_B])
        # 覆盖为空 → 收回
        await _grant(client, f"/api/v1/roles/{role['id']}/permissions", [])
        r = await client.get(
            f"/api/v1/users/{alice['id']}/permissions", headers=AUTH_ADMIN
        )
        eff = r.json()
        assert eff["menuCodes"] == []
        assert eff["roleGrants"][0]["menuCodes"] == []

    async def test_grant_unknown_menu_code_422(self, client: AsyncClient) -> None:
        role = (
            await client.post(
                "/api/v1/roles", headers=AUTH_ADMIN,
                json={"code": "analyst", "name": "分析师"},
            )
        ).json()
        r = await client.put(
            f"/api/v1/roles/{role['id']}/permissions", headers=AUTH_ADMIN,
            json={"menu_codes": [MENU_A, "ghost_menu"]},
        )
        assert r.status_code == 422, r.text

    async def test_superuser_bypass_admin(self, client: AsyncClient) -> None:
        # admin 用户持 admin 角色 → 无任何授权也拥有全部叶子菜单
        admin = (
            await client.get("/api/v1/users", headers=AUTH_ADMIN)
        ).json()
        admin_user = next(u for u in admin if u["username"] == "admin")
        r = await client.get(
            f"/api/v1/users/{admin_user['id']}/permissions", headers=AUTH_ADMIN
        )
        eff = r.json()
        assert eff["isSuperuser"] is True
        assert eff["menuCodes"] == sorted(LEAF_MENUS)

    async def test_cannot_remove_admin_from_last_admin(self, client: AsyncClient) -> None:
        admin_row = (
            next(
                u
                for u in (
                    await client.get("/api/v1/users", headers=AUTH_ADMIN)
                ).json()
                if u["username"] == "admin"
            )
        )
        analyst = (
            await client.post(
                "/api/v1/roles", headers=AUTH_ADMIN,
                json={"code": "analyst", "name": "分析师"},
            )
        ).json()
        # 唯一超管尝试移除 admin 角色 → 409（防锁死）
        r = await client.put(
            f"/api/v1/users/{admin_row['id']}/roles", headers=AUTH_ADMIN,
            json={"role_ids": [analyst["id"]]},
        )
        assert r.status_code == 409, r.text


class TestRoleAndOrgPermissionView:
    """需求 #4：查看任意角色 / 组织的当前授权。"""

    async def test_role_permissions_view(self, client: AsyncClient) -> None:
        role = (
            await client.post(
                "/api/v1/roles", headers=AUTH_ADMIN,
                json={"code": "analyst", "name": "分析师"},
            )
        ).json()
        await _grant(client, f"/api/v1/roles/{role['id']}/permissions", [MENU_A, MENU_C])
        r = await client.get(
            f"/api/v1/roles/{role['id']}/permissions", headers=AUTH_ADMIN
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["subjectId"] == role["id"]
        assert body["menuCodes"] == [MENU_A, MENU_C]

    async def test_org_permissions_view(self, client: AsyncClient) -> None:
        org = (
            await client.post(
                "/api/v1/organizations", headers=AUTH_ADMIN,
                json={"code": "procurement", "name": "采购部"},
            )
        ).json()
        await _grant(
            client, f"/api/v1/organizations/{org['id']}/permissions", [MENU_B, MENU_D]
        )
        r = await client.get(
            f"/api/v1/organizations/{org['id']}/permissions", headers=AUTH_ADMIN
        )
        assert r.status_code == 200, r.text
        assert r.json()["menuCodes"] == [MENU_B, MENU_D]

    async def test_unknown_subject_404(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/roles/999999/permissions", headers=AUTH_ADMIN)
        assert r.status_code == 404


# ------------------------------------------------------------------ 菜单管理


class TestMenuAdmin:
    """需求 #3：动态增删改菜单（admin only）。"""

    async def test_create_section_and_leaf(self, client: AsyncClient) -> None:
        # 新增一级类
        s = await client.post(
            "/api/v1/menu-config", headers=AUTH_ADMIN,
            json={"code": "analytics", "label_key": "analytics", "sort_order": 95},
        )
        assert s.status_code == 201, s.text
        sec = s.json()
        assert sec["path"] is None and sec["hasChildren"] is False

        # 一级类下新增叶子
        lf = await client.post(
            "/api/v1/menu-config", headers=AUTH_ADMIN,
            json={
                "code": "analytics_new",
                "label_key": "analytics_new",
                "path": "/admin/analytics_new",
                "parent_code": "analytics",
                "sort_order": 1,
            },
        )
        assert lf.status_code == 201, lf.text
        leaf = lf.json()
        assert leaf["parentCode"] == "analytics"

        # admin 扁平行含新节点
        rows = await client.get("/api/v1/menu-config/admin", headers=AUTH_ADMIN)
        by_code = {r["code"]: r for r in rows.json()}
        assert by_code["analytics"]["hasChildren"] is True
        assert by_code["analytics_new"]["parentCode"] == "analytics"

    async def test_update_visible_and_move(self, client: AsyncClient) -> None:
        s = await client.post(
            "/api/v1/menu-config", headers=AUTH_ADMIN,
            json={"code": "old_section", "label_key": "old", "sort_order": 10},
        )
        sec_id = s.json()["id"]
        await client.post(
            "/api/v1/menu-config", headers=AUTH_ADMIN,
            json={
                "code": "leaf_x",
                "label_key": "leaf_x",
                "path": "/leaf_x",
                "parent_code": "old_section",
            },
        )
        # 编辑叶子：换 label + 隐藏
        u = await client.put(
            "/api/v1/menu-config/leaf_x", headers=AUTH_ADMIN,
            json={"label_key": "renamed", "visible": False},
        )
        assert u.status_code == 200, u.text
        assert u.json()["labelKey"] == "renamed" and u.json()["visible"] is False
        assert sec_id  # unused guard

    async def test_delete_leaf_then_section(self, client: AsyncClient) -> None:
        await client.post(
            "/api/v1/menu-config", headers=AUTH_ADMIN,
            json={"code": "tmp_sec", "label_key": "tmp", "sort_order": 1},
        )
        await client.post(
            "/api/v1/menu-config", headers=AUTH_ADMIN,
            json={
                "code": "tmp_leaf",
                "label_key": "tmp_leaf",
                "path": "/tmp_leaf",
                "parent_code": "tmp_sec",
            },
        )
        # section 含子项 → 409
        d1 = await client.delete("/api/v1/menu-config/tmp_sec", headers=AUTH_ADMIN)
        assert d1.status_code == 409, d1.text
        # 先删叶子
        d2 = await client.delete("/api/v1/menu-config/tmp_leaf", headers=AUTH_ADMIN)
        assert d2.status_code == 204
        # 再删 section
        d3 = await client.delete("/api/v1/menu-config/tmp_sec", headers=AUTH_ADMIN)
        assert d3.status_code == 204
        rows = await client.get("/api/v1/menu-config/admin", headers=AUTH_ADMIN)
        assert "tmp_leaf" not in {r["code"] for r in rows.json()}

    async def test_admin_crud_403_non_admin(self, client: AsyncClient) -> None:
        await _mkUser(client, "alice")
        cases: list[tuple[str, str, dict | None]] = [
            ("post", "/api/v1/menu-config", {"code": "x", "label_key": "x"}),
            ("put", "/api/v1/menu-config/menu_a", {"label_key": "hijack"}),
            ("delete", "/api/v1/menu-config/menu_a", None),
        ]
        for method, path, payload in cases:
            kw: dict = {"headers": _auth_as("alice")}
            if payload is not None:
                kw["json"] = payload
            r = await getattr(client, method)(path, **kw)
            assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


# ------------------------------------------------------- Phase D: 菜单按人过滤


async def _mkDbUser(
    dbSession: AsyncSession,
    username: str,
    *,
    role_code: str,
    role_menus: tuple[str, ...] = (),
    org_code: str,
    org_menus: tuple[str, ...] = (),
) -> int:
    """直接造 DB 用户 + 角色/组织 + 授权，验证真实身份→有效权限→菜单过滤链路。"""
    user = User(username=username, display_name=username, enabled=True)
    dbSession.add(user)
    await dbSession.flush()
    role = Role(code=role_code, name=role_code)
    dbSession.add(role)
    await dbSession.flush()
    org = Organization(code=org_code, name=org_code)
    dbSession.add(org)
    await dbSession.flush()
    dbSession.add(UserRole(user_id=user.id, role_id=role.id))
    dbSession.add(UserOrganization(user_id=user.id, organization_id=org.id))
    for code in role_menus:
        dbSession.add(
            PermissionGrant(subject_type="ROLE", subject_id=role.id, menu_code=code)
        )
    for code in org_menus:
        dbSession.add(
            PermissionGrant(
                subject_type="ORGANIZATION", subject_id=org.id, menu_code=code
            )
        )
    await dbSession.commit()
    return user.id


def _leaf_codes(payload: dict) -> set[str]:
    return {child["code"] for sec in payload["sections"] for child in sec["children"]}


class TestMenuConfigFiltering:
    """需求 #3 落点 + #5 前置：GET /menu-config 按当前用户有效菜单合集过滤。"""

    async def test_non_admin_sees_only_granted_union(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        # carol：role → {A,B}；org → {B,C}；有效合集 = {A,B,C}
        await _mkDbUser(
            dbSession,
            "carol",
            role_code="analyst",
            role_menus=(MENU_A, MENU_B),
            org_code="procurement",
            org_menus=(MENU_B, MENU_C),
        )
        r = await client.get("/api/v1/menu-config", headers=_auth_as("carol"))
        assert r.status_code == 200, r.text
        assert _leaf_codes(r.json()) == {MENU_A, MENU_B, MENU_C}
        assert MENU_D not in _leaf_codes(r.json())

    async def test_admin_sees_all_visible(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/menu-config", headers=AUTH_ADMIN)
        assert r.status_code == 200, r.text
        assert _leaf_codes(r.json()) == set(LEAF_MENUS)

    async def test_stub_unknown_user_fallback_all_visible(
        self, client: AsyncClient
    ) -> None:
        # 查无此人 → 桩回退 admin → 全部可见（Phase D 前行为不变）
        r = await client.get(
            "/api/v1/menu-config", headers=_auth_as("ghost", roles="analyst")
        )
        assert r.status_code == 200, r.text
        assert _leaf_codes(r.json()) == set(LEAF_MENUS)

    async def test_section_hidden_when_no_leaf_granted(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        # dave 角色授权为空 → 其下无可见叶子 → 一级类不展示
        await _mkDbUser(
            dbSession,
            "dave",
            role_code="viewer",
            role_menus=(),
            org_code="quality",
            org_menus=(),
        )
        r = await client.get("/api/v1/menu-config", headers=_auth_as("dave"))
        assert r.status_code == 200, r.text
        assert r.json()["sections"] == []
