"""文档列表/详情密级过滤集成测试（Task 4.4：audit-fx-4.4）。

测试 document_service.listDocuments / getDocument 密级过滤：
- L3 文档对无权限普通用户不可见（列表排除 + 详情 403）
- L3 文档对 l3_acl 白名单用户可见
- L2 文档对 restricted_access 用户可见（但不见 L3）

真实 PG（用户/角色/l3_acl/document_catalog）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import pytest
from httpx import AsyncClient


class TestDocumentsClearanceFilter:
    """文档目录密级过滤（Phase 4.4）。"""

    @pytest.fixture
    async def regularUserToken(self, dbSession: AsyncSession) -> str:
        """无 restricted_access、无特殊角色、无 l3_acl 的普通用户 token。"""
        from app.tests.integration.test_model_config_rbac import _createUserWithRole

        user_id = await _createUserWithRole(
            dbSession, "test_regular_clearance", role_code="user"
        )
        return await _mintTokenFromId(dbSession, user_id)

    @pytest.fixture
    async def l3Doc(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        fakeMinio,
        mockEmbeddingService,
    ) -> dict:
        """创建一个 L3 文档（通过 upload 接口，securityLevel=L3）。"""
        from unittest.mock import patch

        # 用 admin token 建 L3 文档
        from app.tests.integration.test_model_config_rbac import (
            _createUserWithRole,
        )

        admin_user = await _createUserWithRole(
            dbSession, "test_admin_clearance", role_code="admin"
        )
        admin_token = await _mintTokenFromId(dbSession, admin_user.id)

        files = {"file": ("l3_test.txt", b"L3 confidential audit content.", "text/plain")}
        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ), patch("app.services.rag_service.insertDocumentChunks"):
            resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L3"},
                files=files,
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        assert resp.status_code == 201, resp.text
        doc_data = resp.json()
        # 查询 document_catalog 得到数据库 PK id
        result = await dbSession.execute(
            text("SELECT id FROM document_catalog WHERE document_id = :did"),
            {"did": doc_data["document_id"]},
        )
        row = result.scalar_one()
        return {"id": row, **doc_data}

    @pytest.fixture
    async def l2Doc(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        fakeMinio,
        mockEmbeddingService,
    ) -> dict:
        """创建一个 L2 文档。"""
        from unittest.mock import patch

        from app.tests.integration.test_model_config_rbac import (
            _createUserWithRole,
        )

        admin_user = await _createUserWithRole(
            dbSession, "test_admin_clearance2", role_code="admin"
        )
        admin_token = await _mintTokenFromId(dbSession, admin_user.id)

        files = {"file": ("l2_test.txt", b"L2 internal contract content.", "text/plain")}
        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ), patch("app.services.rag_service.insertDocumentChunks"):
            resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L2"},
                files=files,
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        assert resp.status_code == 201, resp.text
        doc_data = resp.json()
        # 查询 document_catalog 得到数据库 PK id
        result = await dbSession.execute(
            text("SELECT id FROM document_catalog WHERE document_id = :did"),
            {"did": doc_data["document_id"]},
        )
        row = result.scalar_one()
        return {"id": row, **doc_data}

    @pytest.mark.asyncio
    async def test_documents_list_excludes_l3_for_unauthorized(
        self,
        client: AsyncClient,
        regularUserToken: str,
        l3Doc: dict,
        l2Doc: dict,
    ) -> None:
        """无 L3 权限的普通用户：列表不应返回 L3 文档。"""
        headers = {"Authorization": f"Bearer {regularUserToken}"}
        resp = await client.get("/api/v1/documents", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()
        assert not isinstance(items, dict), "list endpoint should return list, not paginated dict"
        doc_ids = [d["id"] for d in items]
        assert l3Doc["id"] not in doc_ids, (
            f"L3 doc {l3Doc['id']} should NOT appear in list for regular user, got: {doc_ids}"
        )

    @pytest.mark.asyncio
    async def test_documents_detail_403_for_unauthorized(
        self,
        client: AsyncClient,
        regularUserToken: str,
        l3Doc: dict,
    ) -> None:
        """无 L3 权限的普通用户访问 L3 文档详情 → 403。"""
        headers = {"Authorization": f"Bearer {regularUserToken}"}
        resp = await client.get(f"/api/v1/documents/{l3Doc['id']}", headers=headers)
        assert resp.status_code == 403, (
            f"Expected 403 for L3 doc access by unauthorized user, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_documents_list_includes_l2_for_restricted_user(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        l2Doc: dict,
        l3Doc: dict,
    ) -> None:
        """restricted_access=True 的用户应看到 L2（但不见 L3）。"""
        # 建 restricted_access=True 的用户
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('restricted_clearance_user', 'Restricted Clearance', 'restclear@test', true, true)
                ON CONFLICT (username) DO UPDATE SET restricted_access = EXCLUDED.restricted_access
                RETURNING id
            """)
        )
        user_id = result.scalar()

        token = await _mintTokenFromId(dbSession, user_id)

        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get("/api/v1/documents", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()
        doc_ids = [d["id"] for d in items]
        # L2 should be visible
        assert l2Doc["id"] in doc_ids, (
            f"L2 doc {l2Doc['id']} should appear for restricted user, got: {doc_ids}"
        )
        # L3 should NOT be visible
        assert l3Doc["id"] not in doc_ids, (
            f"L3 doc {l3Doc['id']} should NOT appear for restricted user, got: {doc_ids}"
        )

    @pytest.mark.asyncio
    async def test_documents_list_includes_l3_for_l3_acl_user(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        l3Doc: dict,
    ) -> None:
        """l3_acl 白名单用户应看到 L3 文档。"""
        # 建用户并写入 l3_acl
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('l3_acl_clearance_user', 'L3 ACL Clearance', 'l3aclclear@test', true, false)
                ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
            """)
        )
        user_id = result.scalar()

        await dbSession.execute(
            text("""
                INSERT INTO l3_acl (user_id, resource_pattern, granted_by)
                VALUES (:uid, 'dataset:*', :granted_by)
                ON CONFLICT DO NOTHING
            """),
            {"uid": user_id, "granted_by": user_id},
        )
        await dbSession.commit()

        token = await _mintTokenFromId(dbSession, user_id)

        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get("/api/v1/documents", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()
        doc_ids = [d["id"] for d in items]
        assert l3Doc["id"] in doc_ids, (
            f"L3 doc {l3Doc['id']} should appear for l3_acl user, got: {doc_ids}"
        )


# ---------------------------------------------------------------------------
# Task 4.5: exhaustive negative test suite
# 4 scenarios × 2 endpoints (list + detail) = 8 cases per file
# ---------------------------------------------------------------------------


class TestDocumentsClearanceExhaustive:
    """密级双向反例测试（Task 4.5：audit-fx-4.5）。

    覆盖 4 个用户类别 × 3 个密级（L1/L2/L3）的完整矩阵。
    每个场景验证 list 过滤 + detail 403/200 是否符合 policy。
    """

    @pytest.fixture
    async def allLevelDocs(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        fakeMinio,
        mockEmbeddingService,
    ) -> dict[str, dict]:
        """创建 3 个文档：L1 / L2 / L3。"""
        from unittest.mock import patch

        # Create admin user inline to avoid asyncpg parameter-type ambiguity
        # that affects _createUserWithRole with long usernames in text() SQL
        await dbSession.execute(
            text("""
                INSERT INTO roles (code, name, description)
                VALUES ('admin', 'admin', 'admin role')
                ON CONFLICT (code) DO NOTHING
            """)
        )
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('tstadmall45', 'tstadmall45', 'tstadmall45@test', true, false)
                ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
            """)
        )
        admin_user = result.scalar()

        await dbSession.execute(
            text("""
                INSERT INTO user_roles (user_id, role_id)
                SELECT :uid, id FROM roles WHERE code = 'admin'
                ON CONFLICT DO NOTHING
            """),
            {"uid": admin_user}
        )
        await dbSession.commit()

        admin_token = await _mintTokenFromId(dbSession, admin_user)

        docs = {}
        for level in ["L1", "L2", "L3"]:
            files = {
                "file": (f"{level.lower()}_test.txt",
                         f"{level} public content for exhaustive test.".encode(),
                         "text/plain")
            }
            with patch(
                "app.services.rag_service._getEmbeddingService",
                return_value=mockEmbeddingService,
            ), patch("app.services.rag_service.insertDocumentChunks"):
                resp = await client.post(
                    "/api/v1/documents/upload",
                    params={"documentType": "CONTRACT", "securityLevel": level},
                    files=files,
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
            assert resp.status_code == 201, f"{level}: {resp.text}"
            doc_data = resp.json()
            result = await dbSession.execute(
                text("SELECT id FROM document_catalog WHERE document_id = :did"),
                {"did": doc_data["document_id"]},
            )
            row = result.scalar_one()
            docs[level] = {"id": row, **doc_data}
        return docs

    # ------------------------------------------------------------------
    # Scenario 1: Regular user → L1 OK, L2/L3 denied
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_regular_user_list_sees_only_l1(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        allLevelDocs: dict[str, dict],
    ) -> None:
        """普通用户：list 只返回 L1，L2/L3 排除。"""
        user_id = await _createUserWithRole(
            dbSession, "test_regular_4_5", role_code="user"
        )
        token = await _mintTokenFromId(dbSession, user_id)

        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get("/api/v1/documents", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()
        doc_ids = [d["id"] for d in items]

        # L1 must be visible
        assert allLevelDocs["L1"]["id"] in doc_ids, (
            f"L1 doc should be visible to regular user, got: {doc_ids}"
        )
        # L2/L3 must be filtered out
        assert allLevelDocs["L2"]["id"] not in doc_ids, (
            f"L2 doc should NOT appear for regular user, got: {doc_ids}"
        )
        assert allLevelDocs["L3"]["id"] not in doc_ids, (
            f"L3 doc should NOT appear for regular user, got: {doc_ids}"
        )

    @pytest.mark.asyncio
    async def test_regular_user_detail_403_for_l2_and_l3(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        allLevelDocs: dict[str, dict],
    ) -> None:
        """普通用户访问 L2/L3 文档详情 → 403。"""
        user_id = await _createUserWithRole(
            dbSession, "test_regular_4_5_detail", role_code="user"
        )
        token = await _mintTokenFromId(dbSession, user_id)

        headers = {"Authorization": f"Bearer {token}"}

        for level in ["L2", "L3"]:
            resp = await client.get(
                f"/api/v1/documents/{allLevelDocs[level]['id']}",
                headers=headers,
            )
            assert resp.status_code == 403, (
                f"Regular user accessing {level} doc detail should get 403, "
                f"got {resp.status_code}: {resp.text}"
            )

    # ------------------------------------------------------------------
    # Scenario 2: L3 ACL user → all 3 levels visible
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_l3_acl_user_list_sees_all_levels(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        allLevelDocs: dict[str, dict],
    ) -> None:
        """l3_acl 白名单用户：list 返回全部 L1/L2/L3。"""
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('l3_acl_exhaustive_4_5', 'L3 ACL Exhaustive', 'l3ex5@test', true, false)
                ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
            """)
        )
        user_id = result.scalar()

        await dbSession.execute(
            text("""
                INSERT INTO l3_acl (user_id, resource_pattern, granted_by)
                VALUES (:uid, 'dataset:*', :granted_by)
                ON CONFLICT DO NOTHING
            """),
            {"uid": user_id, "granted_by": user_id},
        )
        await dbSession.commit()

        token = await _mintTokenFromId(dbSession, user_id)

        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.get("/api/v1/documents", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()
        doc_ids = [d["id"] for d in items]

        for level in ["L1", "L2", "L3"]:
            assert allLevelDocs[level]["id"] in doc_ids, (
                f"L3 ACL user should see {level} doc, got: {doc_ids}"
            )

    @pytest.mark.asyncio
    async def test_l3_acl_user_detail_200_for_all_levels(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        allLevelDocs: dict[str, dict],
    ) -> None:
        """l3_acl 白名单用户访问任意文档详情 → 200。"""
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('l3_acl_detail_4_5', 'L3 ACL Detail', 'l3dtl5@test', true, false)
                ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
            """)
        )
        user_id = result.scalar()

        await dbSession.execute(
            text("""
                INSERT INTO l3_acl (user_id, resource_pattern, granted_by)
                VALUES (:uid, 'dataset:*', :granted_by)
                ON CONFLICT DO NOTHING
            """),
            {"uid": user_id, "granted_by": user_id},
        )
        await dbSession.commit()

        token = await _mintTokenFromId(dbSession, user_id)
        headers = {"Authorization": f"Bearer {token}"}

        for level in ["L1", "L2", "L3"]:
            resp = await client.get(
                f"/api/v1/documents/{allLevelDocs[level]['id']}",
                headers=headers,
            )
            assert resp.status_code == 200, (
                f"L3 ACL user accessing {level} doc detail should get 200, "
                f"got {resp.status_code}: {resp.text}"
            )

    # ------------------------------------------------------------------
    # Scenario 3: restricted_access=True → L1/L2 OK, L3 denied
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_restricted_user_list_sees_l1_l2_not_l3(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        allLevelDocs: dict[str, dict],
    ) -> None:
        """restricted_access=True 用户：list 返回 L1/L2，排除 L3。"""
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('restricted_exhaustive_4_5', 'Restricted Exhaustive',
                        'restex5@test', true, true)
                ON CONFLICT (username) DO UPDATE SET restricted_access = EXCLUDED.restricted_access
                RETURNING id
            """)
        )
        user_id = result.scalar()
        await dbSession.commit()

        token = await _mintTokenFromId(dbSession, user_id)
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.get("/api/v1/documents", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()
        doc_ids = [d["id"] for d in items]

        for level in ["L1", "L2"]:
            assert allLevelDocs[level]["id"] in doc_ids, (
                f"Restricted user should see {level} doc, got: {doc_ids}"
            )
        assert allLevelDocs["L3"]["id"] not in doc_ids, (
            f"Restricted user should NOT see L3 doc, got: {doc_ids}"
        )

    @pytest.mark.asyncio
    async def test_restricted_user_detail_403_for_l3(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        allLevelDocs: dict[str, dict],
    ) -> None:
        """restricted_access=True 用户访问 L3 文档详情 → 403。"""
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('restricted_detail_4_5', 'Restricted Detail',
                        'restdtl5@test', true, true)
                ON CONFLICT (username) DO UPDATE SET restricted_access = EXCLUDED.restricted_access
                RETURNING id
            """)
        )
        user_id = result.scalar()
        await dbSession.commit()

        token = await _mintTokenFromId(dbSession, user_id)
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.get(
            f"/api/v1/documents/{allLevelDocs['L3']['id']}",
            headers=headers,
        )
        assert resp.status_code == 403, (
            f"Restricted user accessing L3 doc detail should get 403, "
            f"got {resp.status_code}: {resp.text}"
        )

    # ------------------------------------------------------------------
    # Scenario 4: compliance_officer role → all 3 levels visible
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_compliance_officer_list_sees_all_levels(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        allLevelDocs: dict[str, dict],
    ) -> None:
        """compliance_officer 角色用户：list 返回全部 L1/L2/L3。"""
        # Insert compliance_officer role (not seeded) + user + membership
        await dbSession.execute(
            text("""
                INSERT INTO roles (code, name, description)
                VALUES ('compliance_officer', 'Compliance Officer',
                        'Full document clearance for audit')
                ON CONFLICT (code) DO NOTHING
            """)
        )
        await dbSession.commit()

        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('compliance_officer_4_5', 'Compliance Officer Test',
                        'comploff5@test', true, false)
                ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
            """)
        )
        user_id = result.scalar()

        await dbSession.execute(
            text("""
                INSERT INTO user_roles (user_id, role_id)
                SELECT :uid, id FROM roles WHERE code = 'compliance_officer'
                ON CONFLICT DO NOTHING
            """),
            {"uid": user_id},
        )
        await dbSession.commit()

        token = await _mintTokenFromId(dbSession, user_id)
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.get("/api/v1/documents", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()
        doc_ids = [d["id"] for d in items]

        for level in ["L1", "L2", "L3"]:
            assert allLevelDocs[level]["id"] in doc_ids, (
                f"Compliance officer should see {level} doc, got: {doc_ids}"
            )

    @pytest.mark.asyncio
    async def test_compliance_officer_detail_200_for_all_levels(
        self,
        client: AsyncClient,
        dbSession: AsyncSession,
        allLevelDocs: dict[str, dict],
    ) -> None:
        """compliance_officer 角色用户访问任意文档详情 → 200。"""
        # Insert compliance_officer role + user + membership
        await dbSession.execute(
            text("""
                INSERT INTO roles (code, name, description)
                VALUES ('compliance_officer', 'Compliance Officer',
                        'Full document clearance for audit')
                ON CONFLICT (code) DO NOTHING
            """)
        )
        await dbSession.commit()

        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('compliance_officer_detail_4_5', 'Compliance Officer Detail',
                        'compldtl5@test', true, false)
                ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
            """)
        )
        user_id = result.scalar()

        await dbSession.execute(
            text("""
                INSERT INTO user_roles (user_id, role_id)
                SELECT :uid, id FROM roles WHERE code = 'compliance_officer'
                ON CONFLICT DO NOTHING
            """),
            {"uid": user_id},
        )
        await dbSession.commit()

        token = await _mintTokenFromId(dbSession, user_id)
        headers = {"Authorization": f"Bearer {token}"}

        for level in ["L1", "L2", "L3"]:
            resp = await client.get(
                f"/api/v1/documents/{allLevelDocs[level]['id']}",
                headers=headers,
            )
            assert resp.status_code == 200, (
                f"Compliance officer accessing {level} doc detail should get 200, "
                f"got {resp.status_code}: {resp.text}"
            )


# ---------------------------------------------------------------------------
# Helper imports needed by fixtures (copied from test_model_config_rbac.py)
# ---------------------------------------------------------------------------


async def _createUserWithRole(
    session: AsyncSession, username: str, role_code: str = "user"
) -> int:
    """创建一个启用用户并赋予指定角色，返回用户 ID。

    使用独立参数避免 asyncpg 对同一符号多上下文复用时的类型推断歧义。
    """
    from app.models.rbac import User, UserRole
    from sqlalchemy import select

    email = f"{username}@test"
    # 唯一约束冲突时忽略
    result = await session.execute(
        text("""
            INSERT INTO users (username, display_name, email, enabled, restricted_access)
            VALUES (:username, :display_name, :email, true, false)
            ON CONFLICT (username) DO NOTHING
            RETURNING id
        """),
        {"username": username, "display_name": username, "email": email},
    )
    row = result.fetchone()
    if row is None:
        result = await session.execute(
            text("SELECT id FROM users WHERE username = :u"),
            {"u": username},
        )
        user_id = result.scalar_one()
    else:
        user_id = row.id

    # 赋予角色
    role_result = await session.execute(
        text("SELECT id FROM roles WHERE code = :code"),
        {"code": role_code},
    )
    role_row = role_result.fetchone()
    if role_row is None:
        # upsert role (roles.name is NOT name_cn)
        await session.execute(
            text("""
                INSERT INTO roles (code, name, description)
                VALUES (:code, :name, :desc)
                ON CONFLICT (code) DO NOTHING
            """),
            {"code": role_code, "name": role_code, "desc": role_code},
        )
        role_result = await session.execute(
            text("SELECT id FROM roles WHERE code = :code"),
            {"code": role_code},
        )
        role_row = role_result.fetchone()

    await session.execute(
        text("""
            INSERT INTO user_roles (user_id, role_id)
            VALUES (:uid, :rid)
            ON CONFLICT DO NOTHING
        """),
        {"uid": user_id, "rid": role_row.id},
    )
    await session.commit()
    return user_id


async def _mintTokenFromId(session: AsyncSession, userId: int) -> str:
    """为指定用户 mint JWT token（接受 user ID 而非 ORM 对象）。"""
    from datetime import datetime, timezone
    from app.models.rbac import UserSession
    from app.services.jwt_codec import sign_jwt

    # 支持传入 int ID 或 User ORM 对象
    if hasattr(userId, "id"):
        uid = userId.id
        username = userId.username
    else:
        uid = userId
        result = await session.execute(
            text("SELECT username FROM users WHERE id = :id"),
            {"id": userId},
        )
        username = result.scalar_one()

    token, jti, exp = sign_jwt(user_id=uid, username=username)
    session.add(
        UserSession(
            jti=jti,
            user_id=uid,
            issued_at=datetime.now(timezone.utc),
            expires_at=exp,
        )
    )
    await session.commit()
    return token
