"""RAG 文档密级过滤集成测试（Task 4.3：audit-fx-4.3）。

测试 buildClearanceFilter 与 searchDocuments 的集成：
- L3 文档对无权限用户不可见
- L3 文档对 l3_acl 白名单用户可见
- L2 文档对 restricted_access 用户可见

真实 PG（用户/角色/l3_acl）+ 真实 Milvus（security_level 字段）+ mock embedding。
mockEmbeddingService 对相同文本生成相同向量，所以搜索时要用与入库时完全一致的查询文本。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text


class TestRagClearanceFilter:
    """RAG 检索密级过滤（Phase 4.3）。"""

    @pytest.mark.asyncio
    async def test_l3_document_excluded_for_non_authorized_user(
        self, client, dbSession, fakeMinio, mockEmbeddingService
    ) -> None:
        """无 L3 ACL 的普通用户不应看到 L3 文档。"""
        # Arrange：建一个 L3 文档（内容与查询文本一致，方便 mock embedding 命中）
        doc_content = "L3 confidential content about supplier audit report."
        files = {"file": ("l3.txt", doc_content.encode("utf-8"), "text/plain")}

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            upload_resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L3"},
                files=files,
            )
        assert upload_resp.status_code == 201, upload_resp.text
        l3_doc_id = upload_resp.json()["document_id"]

        # 建普通用户（无 restricted_access，无特殊角色，无 l3_acl）
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('regular_test_user_4_3', 'Regular Test', 'regular43@test', true, false)
                ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
            """)
        )
        regular_user_id = result.scalar()

        # 确保用户没有特殊角色
        await dbSession.execute(
            text("DELETE FROM user_roles WHERE user_id = :uid"),
            {"uid": regular_user_id}
        )
        await dbSession.commit()

        # Act：用该用户身份搜索（查询文本与文档内容一致，mock embedding 保证向量相同）
        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            search_resp = await client.post(
                "/api/v1/documents/search",
                # 用与文档内容完全相同的查询文本（mock embedding 保证同文本→同向量）
                params={"q": doc_content, "topK": 50},
                headers={"X-User-Id": "regular_test_user_4_3"},
            )

        assert search_resp.status_code == 200
        hits = search_resp.json()
        hit_doc_ids = {h["document_id"] for h in hits}
        assert l3_doc_id not in hit_doc_ids, (
            f"L3 doc {l3_doc_id} should not appear for regular user, got: {hit_doc_ids}"
        )

    @pytest.mark.asyncio
    async def test_l3_document_returned_for_l3_acl_holder(
        self, client, dbSession, fakeMinio, mockEmbeddingService
    ) -> None:
        """l3_acl 白名单用户应看到 L3 文档。"""
        # Arrange：建一个 L3 文档
        doc_content = "L3 authorized viewer can see confidential audit data."
        files = {"file": ("l3_auth.txt", doc_content.encode("utf-8"), "text/plain")}

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            upload_resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L3"},
                files=files,
            )
        assert upload_resp.status_code == 201, upload_resp.text
        l3_doc_id = upload_resp.json()["document_id"]

        # 建用户并写入 l3_acl 白名单
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('l3_auth_user_4_3', 'L3 Auth Test', 'l3auth43@test', true, false)
                ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
            """)
        )
        l3_user_id = result.scalar()

        # 写入 l3_acl（任意 resource_pattern）
        await dbSession.execute(
            text("""
                INSERT INTO l3_acl (user_id, resource_pattern, granted_by)
                VALUES (:uid, 'dataset:*', :granted_by)
                ON CONFLICT DO NOTHING
            """),
            {"uid": l3_user_id, "granted_by": l3_user_id}
        )
        await dbSession.commit()

        # Act（X-User-Id 是 username，不是 DB ID）
        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            search_resp = await client.post(
                "/api/v1/documents/search",
                # 查询文本与文档内容完全一致（mock embedding 保证同文本→同向量）
                params={"q": doc_content, "topK": 50},
                headers={"X-User-Id": "l3_auth_user_4_3"},
            )

        assert search_resp.status_code == 200
        hits = search_resp.json()
        hit_doc_ids = {h["document_id"] for h in hits}
        assert l3_doc_id in hit_doc_ids, (
            f"L3 doc {l3_doc_id} should appear for l3_acl user, got: {hit_doc_ids}"
        )

    @pytest.mark.asyncio
    async def test_l2_document_returned_for_restricted_user(
        self, client, dbSession, fakeMinio, mockEmbeddingService
    ) -> None:
        """restricted_access=True 的用户应看到 L2 文档（但不见 L3）。"""
        # Arrange：建 L2 和 L3 文档（内容与查询文本一致）
        l2_content = "L2 supplier contract terms and conditions apply."
        l3_content = "L3 audit findings are highly confidential report."

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            l2_resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L2"},
                files={"file": ("l2.txt", l2_content.encode("utf-8"), "text/plain")},
            )
            l3_resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L3"},
                files={"file": ("l3.txt", l3_content.encode("utf-8"), "text/plain")},
            )
        assert l2_resp.status_code == 201, l2_resp.text
        assert l3_resp.status_code == 201, l3_resp.text
        l2_doc_id = l2_resp.json()["document_id"]
        l3_doc_id = l3_resp.json()["document_id"]

        # 建 restricted_access=True 的用户
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('restricted_user_4_3', 'Restricted Test', 'restr43@test', true, true)
                ON CONFLICT (username) DO UPDATE SET restricted_access = EXCLUDED.restricted_access
                RETURNING id
            """)
        )
        restricted_user_id = result.scalar()
        await dbSession.commit()

        # Act（X-User-Id 是 username，不是 DB ID）
        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            search_resp = await client.post(
                "/api/v1/documents/search",
                # 查询同时匹配 L2 和 L3 文档内容（mock embedding 保证向量匹配）
                params={"q": f"{l2_content} {l3_content}", "topK": 50},
                headers={"X-User-Id": "restricted_user_4_3"},
            )

        assert search_resp.status_code == 200
        hits = search_resp.json()
        hit_doc_ids = {h["document_id"] for h in hits}

        assert l2_doc_id in hit_doc_ids, (
            f"L2 doc {l2_doc_id} should appear for restricted user, got: {hit_doc_ids}"
        )
        assert l3_doc_id not in hit_doc_ids, (
            f"L3 doc {l3_doc_id} should NOT appear for restricted user, got: {hit_doc_ids}"
        )
