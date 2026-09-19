"""通用「批量关系引擎」集成测试（真实 PostgreSQL + 完整 API 链路）。

覆盖 HTTP 契约（camelCase JSON）：
- POST /ontology/batch           执行：syncGraph / inferJoins / applyManifest
- POST /ontology/batch/preview   只读预览（推断候选 + 冲突预判计数，不落库）

场景：
- 校验：三动作全 false → 422；applyManifest 无 manifest → 422；非法 onConflict → 422
- inferJoins：X3 命名（BPRNUM_0→BPARTNER）+ 共享列一方 PK（IDCODE_0）推断 + 落库 + 审计
- applyManifest + onConflict=skip：同 join_key / 三元组 → skipped、不翻倍
- applyManifest + onConflict=overwrite：字段差异 → UPDATE 审计 + 覆盖
- 类不存在 → 行级 error、其余行继续
- syncGraph 计数（Neo4j 不可达则跳过该用例）
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AuditLog, OntologyJoin, OntologyRelation

pytestmark = pytest.mark.asyncio

_neo4jAvailable = False
try:  # 探测 Neo4j（同步探测在 async 环境外先跑；与图集成测试同口径）
    from app.infrastructure import neo4j_client as _neo4j

    _neo4jAvailable = _neo4j.isNeo4jAvailable()
except Exception:  # noqa: BLE001
    _neo4jAvailable = False


async def _createClass(client: AsyncClient, name: str, table: str) -> int:
    resp = await client.post(
        "/api/v1/ontology/classes",
        json={"className": name, "sourceTable": table},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _createProperty(
    client: AsyncClient,
    classId: int,
    name: str,
    *,
    sourceColumn: str,
    isPrimaryKey: bool = False,
) -> int:
    resp = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": classId,
            "propertyName": name,
            "dataType": "STRING",
            "sourceColumn": sourceColumn,
            "isPrimaryKey": isPrimaryKey,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestBatchValidation:
    async def test_empty_payload_rejected(self, client: AsyncClient) -> None:
        """三动作全 false（默认）→ 无操作可执行，422。"""
        resp = await client.post("/api/v1/ontology/batch", json={})
        assert resp.status_code == 422

    async def test_apply_manifest_without_manifest_rejected(
        self, client: AsyncClient
    ) -> None:
        resp = await client.post(
            "/api/v1/ontology/batch",
            json={"applyManifest": True, "onConflict": "skip"},
        )
        assert resp.status_code == 422

    async def test_invalid_on_conflict_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/ontology/batch",
            json={
                "syncGraph": False,
                "inferJoins": False,
                "applyManifest": True,
                "onConflict": "upsert",
                "manifest": {"joins": [], "relations": []},
            },
        )
        assert resp.status_code == 422


class TestBatchInferJoins:
    async def test_infer_x3_and_shared_column_writes_joins(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """X3 命名外键 + 共享列一方 PK 推断为 join 候选并落库 + 审计。"""
        # Pass A：PORDER.BPRNUM_0（未标 FK，纯列名约定）→ X3 map → BPARTNER
        srcId = await _createClass(client, "PORDER", "PORDER")
        await _createProperty(client, srcId, "BPRNUM_0", sourceColumn="BPRNUM_0")
        await _createClass(client, "BPARTNER", "BPARTNER")
        # Pass B：CONTRACT.IDCODE_0（非 PK）+ CUSTOMER.IDCODE_0（PK）→ FK→PK
        tgtId = await _createClass(client, "CUSTOMER", "CUSTOMER")
        await _createProperty(
            client, tgtId, "IDCODE_0", sourceColumn="IDCODE_0", isPrimaryKey=True
        )
        fkId = await _createClass(client, "CONTRACT", "CONTRACT")
        await _createProperty(client, fkId, "IDCODE_0", sourceColumn="IDCODE_0")

        resp = await client.post(
            "/api/v1/ontology/batch",
            json={"inferJoins": True, "onConflict": "skip"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 两条推断候选：X3 命名 + 共享列
        inferred = body["inferredJoins"]
        assert len(inferred) == 2
        bySource = {i["sourceClassId"]: i for i in inferred}
        assert bySource[srcId]["targetClassId"]  # BPARTNER 命中
        assert bySource[srcId]["inferredBy"] == "name_convention"
        assert bySource[fkId]["targetClassId"] == tgtId
        assert bySource[fkId]["inferredBy"] == "shared_column"
        # 全部新建落库
        assert body["joins"]["created"] == 2
        assert body["joins"]["skipped"] == 0
        assert body["joins"]["overwritten"] == 0
        assert body["relations"]["created"] == 0

        await dbSession.commit()
        rows = (
            await dbSession.execute(select(OntologyJoin))
        ).scalars().all()
        assert len(rows) == 2
        joinKeys = {r.join_key for r in rows}
        # X3 候选：PORDER.BPRNUM_0 → BPARTNER.BPRNUM_0
        assert any("BPRNUM_0" in k for k in joinKeys)
        # 审计归属：每建一条 join 落 ONTOLOGY_JOIN CREATE
        auditRows = (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "ONTOLOGY_JOIN",
                    AuditLog.action == "CREATE",
                )
            )
        ).scalars().all()
        assert len(auditRows) == 2
        assert all(a.actor for a in auditRows)


class TestBatchApplyManifest:
    def _joinManifest(
        self,
        srcId: int,
        tgtId: int,
        *,
        relationType: str = "foreign_key",
        joinType: str = "INNER",
    ) -> dict:
        return {
            "sourceClassId": srcId,
            "sourceColumns": ["BPRNUM_0"],
            "targetClassId": tgtId,
            "targetColumns": ["BPRNUM_0"],
            "joinType": joinType,
            "relationType": relationType,
        }

    async def test_apply_manifest_skip_on_existing(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """同 join_key 已存在 + skip → skipped、总数不翻倍。"""
        srcId = await _createClass(client, "PORDER", "PORDER")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")
        # 预建同 join_key
        pre = await client.post(
            "/api/v1/ontology/joins",
            json={
                "sourceClassId": srcId,
                "sourceColumns": ["BPRNUM_0"],
                "targetClassId": tgtId,
                "targetColumns": ["BPRNUM_0"],
            },
        )
        assert pre.status_code == 201

        resp = await client.post(
            "/api/v1/ontology/batch",
            json={
                "applyManifest": True,
                "onConflict": "skip",
                "manifest": {
                    "joins": [self._joinManifest(srcId, tgtId)],
                    "relations": [
                        {
                            "sourceClassId": srcId,
                            "targetClassId": tgtId,
                            "relationType": "SUPPLIES",
                        }
                    ],
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["joins"]["skipped"] == 1
        assert body["joins"]["created"] == 0
        # 语义关系不存在 → 新建
        assert body["relations"]["created"] == 1
        assert body["relations"]["skipped"] == 0

        await dbSession.commit()
        joins = (await dbSession.execute(select(OntologyJoin))).scalars().all()
        assert len(joins) == 1  # 不翻倍

    async def test_apply_manifest_skip_on_existing_relation(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        srcId = await _createClass(client, "PORDER", "PORDER")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")
        pre = await client.post(
            "/api/v1/ontology/relations",
            json={
                "sourceClassId": srcId,
                "targetClassId": tgtId,
                "relationType": "SUPPLIES",
            },
        )
        assert pre.status_code == 201

        resp = await client.post(
            "/api/v1/ontology/batch",
            json={
                "applyManifest": True,
                "onConflict": "skip",
                "manifest": {
                    "joins": [],
                    "relations": [
                        {
                            "sourceClassId": srcId,
                            "targetClassId": tgtId,
                            "relationType": "SUPPLIES",
                            "description": "覆盖不应发生",
                        }
                    ],
                },
            },
        )
        body = resp.json()
        assert body["relations"]["skipped"] == 1
        assert body["relations"]["created"] == 0

        await dbSession.commit()
        rows = (
            await dbSession.execute(select(OntologyRelation))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].description is None  # 未被覆盖

    async def test_apply_manifest_overwrite_updates_and_audits(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """同 join_key + overwrite：改 relation_type/description → UPDATE 审计 before/after。"""
        srcId = await _createClass(client, "PORDER", "PORDER")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")
        pre = await client.post(
            "/api/v1/ontology/joins",
            json={
                "sourceClassId": srcId,
                "sourceColumns": ["BPRNUM_0"],
                "targetClassId": tgtId,
                "targetColumns": ["BPRNUM_0"],
            },
        )
        joinId = pre.json()["id"]

        resp = await client.post(
            "/api/v1/ontology/batch",
            json={
                "applyManifest": True,
                "onConflict": "overwrite",
                "manifest": {
                    "joins": [
                        self._joinManifest(
                            srcId, tgtId, relationType="business", joinType="LEFT"
                        )
                    ],
                    "relations": [],
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["joins"]["overwritten"] == 1
        assert body["joins"]["created"] == 0

        await dbSession.commit()
        row = await dbSession.get(OntologyJoin, joinId)
        assert row is not None
        assert row.join_type == "LEFT"
        assert row.relation_type == "business"

        auditRows = (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "ONTOLOGY_JOIN",
                    AuditLog.action == "UPDATE",
                    AuditLog.entity_id == joinId,
                )
            )
        ).scalars().all()
        assert len(auditRows) == 1
        before = auditRows[0].before_json or {}
        after = auditRows[0].after_json or {}
        assert before["join_type"] == "INNER"
        assert after["join_type"] == "LEFT"
        assert auditRows[0].actor

    async def test_missing_class_records_row_error_and_continues(
        self, client: AsyncClient
    ) -> None:
        """清单中含不存在类 → 该行 error、其余行正常落库。"""
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")
        # 好行：source=PORDER target=BPARTNER
        srcId = await _createClass(client, "PORDER", "PORDER")
        resp = await client.post(
            "/api/v1/ontology/batch",
            json={
                "applyManifest": True,
                "onConflict": "skip",
                "manifest": {
                    "joins": [
                        {
                            "sourceClassId": 999999,
                            "sourceColumns": ["BPRNUM_0"],
                            "targetClassId": tgtId,
                            "targetColumns": ["BPRNUM_0"],
                        },
                        self._joinManifest(srcId, tgtId),
                    ],
                    "relations": [],
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["joins"]["errors"]) == 1
        assert body["joins"]["errors"][0]["index"] == 0
        assert body["joins"]["created"] == 1


class TestBatchSyncGraph:
    @pytest.mark.skipif(
        not _neo4jAvailable, reason="Neo4j 不可达，跳过 syncGraph 计数用例"
    )
    async def test_sync_graph_counts_nodes(self, client: AsyncClient) -> None:
        """syncGraph：把 live 类/属性 upsert 入图并返回计数。"""
        await _createClass(client, "PORDER", "PORDER")
        await _createClass(client, "BPARTNER", "BPARTNER")

        resp = await client.post(
            "/api/v1/ontology/batch",
            json={"syncGraph": True},
        )
        assert resp.status_code == 200, resp.text
        graph = resp.json()["syncGraph"]
        assert graph is not None
        assert graph["classes"] >= 2
        assert graph["properties"] >= 0
        assert graph["hasPropertyEdges"] >= 0
        assert graph["referenceEdges"] >= 0


class TestBatchPreview:
    async def test_preview_infers_without_writing(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """preview 只读：返回推断候选与冲突预判，不落库。"""
        srcId = await _createClass(client, "PORDER", "PORDER")
        await _createProperty(client, srcId, "BPRNUM_0", sourceColumn="BPRNUM_0")
        await _createClass(client, "BPARTNER", "BPARTNER")

        resp = await client.post(
            "/api/v1/ontology/batch/preview",
            json={"inferJoins": True, "onConflict": "skip"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["inferredJoins"]) == 1
        assert body["inferredJoins"][0]["sourceClassId"] == srcId
        # preview 不落库
        await dbSession.commit()
        rows = (await dbSession.execute(select(OntologyJoin))).scalars().all()
        assert rows == []

    async def test_preview_duplicate_manifest_rows_count_aligned(
        self, client: AsyncClient
    ) -> None:
        """批内重复的新 join：preview 计 created=1/skipped=1（与执行口径一致，不虚增 created）。"""
        srcId = await _createClass(client, "PORDER", "PORDER")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")
        join = {
            "sourceClassId": srcId,
            "sourceColumns": ["BPRNUM_0"],
            "targetClassId": tgtId,
            "targetColumns": ["BPRNUM_0"],
        }
        resp = await client.post(
            "/api/v1/ontology/batch/preview",
            json={
                "applyManifest": True,
                "onConflict": "skip",
                "manifest": {"joins": [dict(join), dict(join)], "relations": []},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["joins"]["created"] == 1
        assert body["joins"]["skipped"] == 1
        assert body["joins"]["errors"] == []

    async def test_execute_duplicate_manifest_rows_writes_once(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """批内重复的新 join：执行只落库一条，计数 created=1/skipped=1。"""
        srcId = await _createClass(client, "PORDER", "PORDER")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")
        join = {
            "sourceClassId": srcId,
            "sourceColumns": ["BPRNUM_0"],
            "targetClassId": tgtId,
            "targetColumns": ["BPRNUM_0"],
        }
        resp = await client.post(
            "/api/v1/ontology/batch",
            json={
                "applyManifest": True,
                "onConflict": "skip",
                "manifest": {"joins": [dict(join), dict(join)], "relations": []},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["joins"]["created"] == 1
        assert body["joins"]["skipped"] == 1
        await dbSession.commit()
        rows = (await dbSession.execute(select(OntologyJoin))).scalars().all()
        assert len(rows) == 1


class TestBatchCsv:
    async def test_template_download_has_header(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/ontology/batch/template", params={"kind": "relations"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        # UTF-8 BOM（Excel 兼容）+ 表头
        assert resp.text.lstrip("﻿").startswith(
            "sourceClassName,targetClassName,relationType,description"
        )

    async def test_parse_relations_csv_resolves_class_ids(
        self, client: AsyncClient
    ) -> None:
        """CSV 清单 → 按类名反解 id，返回可执行 manifest。"""
        srcId = await _createClass(client, "PORDER", "PORDER")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")
        csvContent = (
            "sourceClassName,targetClassName,relationType,description\n"
            "PORDER,BPARTNER,SUPPLIES,PORDER 由 BPARTNER 供应\n"
        )
        resp = await client.post(
            "/api/v1/ontology/batch/parse-csv",
            data={"kind": "relations"},
            files={"file": ("relations.csv", csvContent, "text/csv")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["errors"] == []
        relations = body["manifest"]["relations"]
        assert len(relations) == 1
        assert relations[0]["sourceClassId"] == srcId
        assert relations[0]["targetClassId"] == tgtId
        assert relations[0]["relationType"] == "SUPPLIES"

    async def test_parse_csv_unknown_class_reports_row_error(
        self, client: AsyncClient
    ) -> None:
        csvContent = (
            "sourceClassName,targetClassName,relationType,description\n"
            "PORDER,NOT_A_CLASS,SUPPLIES,x\n"
        )
        resp = await client.post(
            "/api/v1/ontology/batch/parse-csv",
            data={"kind": "relations"},
            files={"file": ("relations.csv", csvContent, "text/csv")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["errors"]) == 1
        assert body["manifest"]["relations"] == []

    async def test_parse_csv_over_size_limit_rejected(
        self, client: AsyncClient
    ) -> None:
        """清单超过 2MB 大小上限 → 拒绝（防超大文件拖垮解析）。"""
        # 一行约 60 字节，3.5 万行 ≈ 2.1MB > 2MB 上限
        oversized = (
            "sourceClassName,targetClassName,relationType,description\n"
            + "PORDER,BPARTNER,SUPPLIES," + "x" * 40 + "\n"
        ) * 35000
        resp = await client.post(
            "/api/v1/ontology/batch/parse-csv",
            data={"kind": "relations"},
            files={"file": ("huge.csv", oversized, "text/csv")},
        )
        assert resp.status_code == 422
        assert "大小限制" in resp.text

    async def test_parse_csv_overlength_fields_become_row_errors(
        self, client: AsyncClient
    ) -> None:
        """超长 relationType/joinType → 该行记错而不是整个解析 500，其余合法行照常。"""
        await _createClass(client, "PORDER", "PORDER")
        await _createClass(client, "BPARTNER", "BPARTNER")
        relCsv = (
            "sourceClassName,targetClassName,relationType,description\n"
            "PORDER,BPARTNER,SUPPLIES,ok\n"
            + f"PORDER,BPARTNER,{'R' * 40},too-long-type\n"
        )
        resp = await client.post(
            "/api/v1/ontology/batch/parse-csv",
            data={"kind": "relations"},
            files={"file": ("rel.csv", relCsv, "text/csv")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["errors"]) == 1
        assert len(body["manifest"]["relations"]) == 1  # 合法行照常解析

        joinCsv = (
            "sourceClassName,sourceColumns,targetClassName,targetColumns,"
            "joinType,relationType,description\n"
            "PORDER,BPRNUM_0,BPARTNER,BPRNUM_0,INNER,foreign_key,ok\n"
            + f"PORDER,BPRNUM_0,BPARTNER,BPRNUM_0,{'J' * 40},foreign_key,x\n"
        )
        resp = await client.post(
            "/api/v1/ontology/batch/parse-csv",
            data={"kind": "joins"},
            files={"file": ("join.csv", joinCsv, "text/csv")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["errors"]) == 1
        assert len(body["manifest"]["joins"]) == 1


class TestBatchJoinGapWarnings:
    """导入闸门（2026-09-18 DIM_SUPPLIER 孤岛事故产物）：batch 后置检查。

    执行/预览后扫描「有 FK 语义列（isForeignKey 或 *_CODE）但零 JOIN 边」的类，
    以 warnings 返回（不阻断）；已被本次建边连通的类不误报。
    """

    async def _seedGateClasses(self, client: AsyncClient) -> tuple[int, int, int, int]:
        # dimG：带 PK，会被建边连通；factG：带 *_CODE 列且保持孤岛（应告警）
        # bareG：孤岛但无 FK 语义列（不应告警）；linkS：join 的 source 侧
        dimG = await _createClass(client, "GATE_DIM", "GATE_DIM")
        await _createProperty(
            client, dimG, "GATE_ID", sourceColumn="GATE_ID", isPrimaryKey=True
        )
        factG = await _createClass(client, "GATE_FACT", "GATE_FACT")
        await _createProperty(client, factG, "GATE_CODE", sourceColumn="GATE_CODE")
        bareG = await _createClass(client, "GATE_BARE", "GATE_BARE")
        await _createProperty(client, bareG, "NOTE", sourceColumn="NOTE")
        linkS = await _createClass(client, "GATE_LINK_SRC", "GATE_LINK_SRC")
        await _createProperty(client, linkS, "L_CODE", sourceColumn="L_CODE")
        return dimG, factG, bareG, linkS

    async def test_apply_manifest_warns_for_fk_like_islands(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        dimG, factG, bareG, linkS = await self._seedGateClasses(client)

        resp = await client.post(
            "/api/v1/ontology/batch",
            json={
                "applyManifest": True,
                "onConflict": "skip",
                "manifest": {
                    "joins": [
                        {
                            "sourceClassId": linkS,
                            "sourceColumns": ["L_CODE"],
                            "targetClassId": dimG,
                            "targetColumns": ["GATE_ID"],
                        }
                    ],
                    "relations": [],
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["joins"]["created"] == 1
        # GATE_FACT 入告警；GATE_LINK_SRC 已被本次建边连通不误报；GATE_BARE 无 FK 列不告警
        assert any("GATE_FACT" in w for w in body["warnings"]), body["warnings"]
        assert not any("GATE_LINK_SRC" in w for w in body["warnings"])
        assert not any("GATE_BARE" in w for w in body["warnings"])
        await dbSession.commit()

    async def test_preview_also_reports_warnings(self, client: AsyncClient) -> None:
        _, factG, _, _ = await self._seedGateClasses(client)

        resp = await client.post(
            "/api/v1/ontology/batch/preview",
            json={"inferJoins": True, "onConflict": "skip"},
        )
        assert resp.status_code == 200, resp.text
        assert any("GATE_FACT" in w for w in resp.json()["warnings"])

    async def test_no_warnings_when_all_fk_like_classes_connected(
        self, client: AsyncClient
    ) -> None:
        dimG, _, _, linkS = await self._seedGateClasses(client)

        resp = await client.post(
            "/api/v1/ontology/batch",
            json={
                "applyManifest": True,
                "onConflict": "skip",
                "manifest": {
                    "joins": [
                        {
                            "sourceClassId": linkS,
                            "sourceColumns": ["L_CODE"],
                            "targetClassId": dimG,
                            "targetColumns": ["GATE_ID"],
                        },
                        {
                            "sourceClassId": linkS,
                            "sourceColumns": ["L_CODE"],
                            "targetClassId": dimG,
                            "targetColumns": ["GATE_ID"],
                        },
                    ],
                    "relations": [],
                },
            },
        )
        assert resp.status_code == 200
        warnings = resp.json()["warnings"]
        # linkS 与 dimG 已连通；仅 GATE_FACT 剩余告警与 FK 列相关
        assert not any("GATE_DIM" in w for w in warnings)
        assert not any("GATE_LINK_SRC" in w for w in warnings)

    async def test_warnings_capped_at_20(self, client: AsyncClient) -> None:
        for i in range(25):
            clsId = await _createClass(client, f"GATE_CAP_{i}", f"GATE_CAP_T_{i}")
            await _createProperty(client, clsId, "C_CODE", sourceColumn="C_CODE")

        resp = await client.post(
            "/api/v1/ontology/batch/preview",
            json={"inferJoins": True, "onConflict": "skip"},
        )
        assert resp.status_code == 200
        warnings = resp.json()["warnings"]
        gateWarnings = [w for w in warnings if "GATE_CAP" in w or "另有" in w]
        # 20 条告警上限 + 1 条「另有 N 个」汇总
        assert len(gateWarnings) <= 21
        assert any("另有" in w for w in gateWarnings)
