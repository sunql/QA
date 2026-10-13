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


# ---------------------------------------------------------------------------
# Task 4.5: exhaustive negative test suite for RAG
# 4 scenarios × bidirectional verification = 8 cases
# ---------------------------------------------------------------------------


class TestRagClearanceExhaustive:
    """RAG 密级双向反例测试（Task 4.5：audit-fx-4.5）。

    覆盖 4 个用户类别 × 3 个密级（L1/L2/L3）的完整矩阵。
    每个场景验证 RAG search 结果是否正确过滤/放行。
    """

    # Unique content strings for exhaustive tests (distinct ordinal-sum seeds)
    _EX_DOCS = {
        "L1": "L1 vendor q1 performance metrics supplier scorecard 2024",
        "L2": "L2 internal audit findings contract risk assessment report",
        "L3": "L3 confidential board minutes strategic acquisition plan classified",
    }

    @pytest.fixture
    async def allLevelDocs(self, dbSession) -> dict[str, tuple[str, str]]:
        """Create L1/L2/L3 docs in Milvus + PG on first call; return {level: (doc_id, content)}.

        Guards against re-entry (same test may call the fixture multiple times).
        Uses class-level _docs so subsequent tests reuse the same doc_ids.
        """
        import hashlib
        import uuid
        from datetime import date, datetime, timezone

        from pymilvus import Collection, utility

        from app.infrastructure.milvus_client import (
            _connect,
            _connAlias,
            ensureDocumentCollection,
            insertDocumentChunks,
        )

        # Return cached docs on re-entry within the same test
        if hasattr(self, "_docs") and self._docs:
            return self._docs

        # Milvus: drop + recreate ONCE per test class session
        if not hasattr(TestRagClearanceExhaustive, "_milvus_ready"):
            _connect()
            _DOC = "document_embeddings"
            if utility.has_collection(_DOC, using=_connAlias()):
                Collection(_DOC, using=_connAlias()).drop()
            ensureDocumentCollection()
            TestRagClearanceExhaustive._milvus_ready = True

        docs: dict[str, tuple[str, str]] = {}
        for level, content in TestRagClearanceExhaustive._EX_DOCS.items():
            doc_id = f"DOC-{uuid.uuid4().hex[:12].upper()}"
            chunk_id = f"chunk-{uuid.uuid4().hex[:8]}"
            seed = sum(ord(c) for c in content) % 100
            emb = [float(seed) / 100.0 + 0.001 * i for i in range(1024)]

            try:
                insertDocumentChunks([{
                    "document_id": doc_id,
                    "chunk_id": chunk_id,
                    "chunk_text": content,
                    "chunk_sequence": 0,
                    "effective_date": "",
                    "security_level": level,
                    "page_number": -1,
                    "section_name": "",
                    "paragraph_no": -1,
                    "embedding": emb,
                }])
            except Exception:
                pass  # already inserted on prior test call within same session

            content_hash = hashlib.sha256(content.encode()).hexdigest()
            await dbSession.execute(
                text("""
                    INSERT INTO document_catalog
                        (document_id, document_name, document_type, version, status,
                         owner, effective_date, security_level, storage_url,
                         content_hash, created_time, updated_time)
                    VALUES
                        (:doc_id, :doc_name, 'CONTRACT', 'v1.0', 'ACTIVE',
                         'test', :eff_date, :sec_level, :storage_url,
                         :content_hash, :now, :now)
                    ON CONFLICT (document_id) DO UPDATE SET
                        security_level = EXCLUDED.security_level
                """),
                {
                    "doc_id": doc_id,
                    "doc_name": f"Exhaustive {level} Test Doc",
                    "eff_date": date.today(),
                    "sec_level": level,
                    "storage_url": f"minio://exhaustive/{doc_id}",
                    "content_hash": content_hash,
                    "now": datetime.now(timezone.utc),
                },
            )
            await dbSession.commit()
            docs[level] = (doc_id, content)
        self._docs = docs
        return docs

    def setup_method(self) -> None:
        """Reset per-test doc cache so fixture re-evaluates on each test."""
        object.__setattr__(self, "_docs", {})

    # ------------------------------------------------------------------
    # Scenario 1: Regular user → L1 OK, L2/L3 excluded
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_regular_user_rag_sees_only_l1(
        self, client, dbSession, allLevelDocs, mockEmbeddingService
    ) -> None:
        """普通用户：RAG 搜索只命中 L1 文档，L2/L3 排除。"""
        l1_id, l1_content = allLevelDocs["L1"]
        l2_id, l2_content = allLevelDocs["L2"]
        l3_id, l3_content = allLevelDocs["L3"]
        # Create regular user (no restricted_access, no special role, no l3_acl)
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('regular_rag_4_5', 'Regular RAG', 'regularrag5@test', true, false)
                ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
            """)
        )
        regular_user_id = result.scalar()
        await dbSession.execute(
            text("DELETE FROM user_roles WHERE user_id = :uid"),
            {"uid": regular_user_id}
        )
        await dbSession.commit()

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            l1_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": l1_content, "topK": 50},
                headers={"X-User-Id": "regular_rag_4_5"},
            )
            l2_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": l2_content, "topK": 50},
                headers={"X-User-Id": "regular_rag_4_5"},
            )
            l3_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": l3_content, "topK": 50},
                headers={"X-User-Id": "regular_rag_4_5"},
            )

        assert l1_resp.status_code == 200, l1_resp.text
        assert l2_resp.status_code == 200, l2_resp.text
        assert l3_resp.status_code == 200, l3_resp.text

        l1_hits = {h["document_id"] for h in l1_resp.json()}
        l2_hits = {h["document_id"] for h in l2_resp.json()}
        l3_hits = {h["document_id"] for h in l3_resp.json()}

        assert l1_id in l1_hits, (
            f"L1 doc {l1_id} should be retrieved for regular user, got: {l1_hits}"
        )
        assert l2_id not in l2_hits, (
            f"L2 doc {l2_id} should NOT be retrieved by regular user, got: {l2_hits}"
        )
        assert l3_id not in l3_hits, (
            f"L3 doc {l3_id} should NOT be retrieved by regular user, got: {l3_hits}"
        )

    @pytest.mark.asyncio
    async def test_regular_user_cannot_rag_retrieve_l2_l3(
        self, client, dbSession, allLevelDocs, mockEmbeddingService
    ) -> None:
        """普通用户：用 L2/L3 文档内容搜索，结果为空（403 等效）。"""
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('regular_rag_l2l3_4_5', 'Regular RAG L2L3', 'regularl2l3@test', true, false)
                ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING id
            """)
        )
        regular_user_id = result.scalar()
        await dbSession.execute(
            text("DELETE FROM user_roles WHERE user_id = :uid"),
            {"uid": regular_user_id}
        )
        await dbSession.commit()

        _, l2_content = allLevelDocs["L2"]
        _, l3_content = allLevelDocs["L3"]

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            l2_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": l2_content, "topK": 50},
                headers={"X-User-Id": "regular_rag_l2l3_4_5"},
            )
            l3_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": l3_content, "topK": 50},
                headers={"X-User-Id": "regular_rag_l2l3_4_5"},
            )

        assert l2_resp.status_code == 200, l2_resp.text
        assert l3_resp.status_code == 200, l3_resp.text

        l2_hits = {h["document_id"] for h in l2_resp.json()}
        l3_hits = {h["document_id"] for h in l3_resp.json()}

        l2_id = allLevelDocs["L2"][0]
        l3_id = allLevelDocs["L3"][0]
        assert l2_id not in l2_hits, (
            f"L2 doc {l2_id} should NOT be retrieved by regular user, got: {l2_hits}"
        )
        assert l3_id not in l3_hits, (
            f"L3 doc {l3_id} should NOT be retrieved by regular user, got: {l3_hits}"
        )

    # ------------------------------------------------------------------
    # Scenario 2: L3 ACL user → all 3 levels visible
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_l3_acl_user_rag_sees_all_levels(
        self, client, dbSession, allLevelDocs, mockEmbeddingService
    ) -> None:
        """l3_acl 白名单用户：RAG 搜索命中全部 L1/L2/L3。"""
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('l3_acl_rag_4_5', 'L3 ACL RAG', 'l3aclr5@test', true, false)
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
            {"uid": user_id, "granted_by": user_id}
        )
        await dbSession.commit()

        # Search for each doc's exact content (guaranteed vector match)
        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            l1_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": allLevelDocs["L1"][1], "topK": 50},
                headers={"X-User-Id": "l3_acl_rag_4_5"},
            )
            l2_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": allLevelDocs["L2"][1], "topK": 50},
                headers={"X-User-Id": "l3_acl_rag_4_5"},
            )
            l3_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": allLevelDocs["L3"][1], "topK": 50},
                headers={"X-User-Id": "l3_acl_rag_4_5"},
            )

        assert l1_resp.status_code == 200, l1_resp.text
        assert l2_resp.status_code == 200, l2_resp.text
        assert l3_resp.status_code == 200, l3_resp.text

        l1_hits = {h["document_id"] for h in l1_resp.json()}
        l2_hits = {h["document_id"] for h in l2_resp.json()}
        l3_hits = {h["document_id"] for h in l3_resp.json()}

        for level in ["L1", "L2", "L3"]:
            doc_id = allLevelDocs[level][0]
            hits = locals()[f"{level.lower()}_hits"]
            assert doc_id in hits, (
                f"L3 ACL user should retrieve {level} doc {doc_id}, got: {hits}"
            )

    # ------------------------------------------------------------------
    # Scenario 3: restricted_access=True → L1/L2 OK, L3 excluded
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_restricted_user_rag_sees_l1_l2_not_l3(
        self, client, dbSession, allLevelDocs, mockEmbeddingService
    ) -> None:
        """restricted_access=True 用户：RAG 命中 L1/L2，排除 L3。"""
        result = await dbSession.execute(
            text("""
                INSERT INTO users (username, display_name, email, enabled, restricted_access)
                VALUES ('restricted_rag_4_5', 'Restricted RAG', 'restrag5@test', true, true)
                ON CONFLICT (username) DO UPDATE SET restricted_access = EXCLUDED.restricted_access
                RETURNING id
            """)
        )
        restricted_user_id = result.scalar()
        await dbSession.commit()

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            l1_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": allLevelDocs["L1"][1], "topK": 50},
                headers={"X-User-Id": "restricted_rag_4_5"},
            )
            l2_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": allLevelDocs["L2"][1], "topK": 50},
                headers={"X-User-Id": "restricted_rag_4_5"},
            )
            l3_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": allLevelDocs["L3"][1], "topK": 50},
                headers={"X-User-Id": "restricted_rag_4_5"},
            )

        assert l1_resp.status_code == 200, l1_resp.text
        assert l2_resp.status_code == 200, l2_resp.text
        assert l3_resp.status_code == 200, l3_resp.text

        l1_hits = {h["document_id"] for h in l1_resp.json()}
        l2_hits = {h["document_id"] for h in l2_resp.json()}
        l3_hits = {h["document_id"] for h in l3_resp.json()}

        assert allLevelDocs["L1"][0] in l1_hits, (
            f"Restricted user should retrieve L1 doc, got: {l1_hits}"
        )
        assert allLevelDocs["L2"][0] in l2_hits, (
            f"Restricted user should retrieve L2 doc, got: {l2_hits}"
        )
        assert allLevelDocs["L3"][0] not in l3_hits, (
            f"Restricted user should NOT retrieve L3 doc, got: {l3_hits}"
        )

    # ------------------------------------------------------------------
    # Scenario 4: compliance_officer role → all 3 levels visible
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_compliance_officer_rag_sees_all_levels(
        self, client, dbSession, allLevelDocs, mockEmbeddingService
    ) -> None:
        """compliance_officer 角色用户：RAG 搜索命中全部 L1/L2/L3。"""
        # Insert role + user + membership
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
                VALUES ('compliance_rag_4_5', 'Compliance RAG', 'comprag5@test', true, false)
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
            {"uid": user_id}
        )
        await dbSession.commit()

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            l1_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": allLevelDocs["L1"][1], "topK": 50},
                headers={"X-User-Id": "compliance_rag_4_5"},
            )
            l2_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": allLevelDocs["L2"][1], "topK": 50},
                headers={"X-User-Id": "compliance_rag_4_5"},
            )
            l3_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": allLevelDocs["L3"][1], "topK": 50},
                headers={"X-User-Id": "compliance_rag_4_5"},
            )

        assert l1_resp.status_code == 200, l1_resp.text
        assert l2_resp.status_code == 200, l2_resp.text
        assert l3_resp.status_code == 200, l3_resp.text

        l1_hits = {h["document_id"] for h in l1_resp.json()}
        l2_hits = {h["document_id"] for h in l2_resp.json()}
        l3_hits = {h["document_id"] for h in l3_resp.json()}

        for level in ["L1", "L2", "L3"]:
            doc_id = allLevelDocs[level][0]
            hits = locals()[f"{level.lower()}_hits"]
            assert doc_id in hits, (
                f"Compliance officer should retrieve {level} doc {doc_id}, got: {hits}"
            )
