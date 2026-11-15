"""镜像对账命令行（P3b D24）的单测。

脚本 `scripts/verify_graph_mirror.py` 是**只读**对账：计划（manifest + 当前解析）
vs 图上读回的镜像，逐条求差。本文件只测脚本自身的**决策与退出码契约**（端口用
替身，不连数据库）。

退出码契约：
    0 = 对账一致（且无未解析）
    1 = 镜像与计划不一致（缺/多节点、字段不符、缺/多边——明细在报告里）
    2 = 前置阻断（发布不存在 / 参数）
    3 = 图与计划一致，但计划本身有未解析 ⇒ 镜像相对 manifest 不完整
"""

from __future__ import annotations

import asyncio

from app.services.semantic_graph_mirror import EntityResolution
from scripts.verify_graph_mirror import (
    EXIT_BLOCKED,
    EXIT_INCOMPLETE,
    EXIT_INCONSISTENT,
    EXIT_OK,
    ScriptFailure,
    buildParser,
    runVerify,
)


def _manifest() -> dict:
    return {
        "classes": [
            {
                "className": "采购订单",
                "classAlias": "PO",
                "description": "采购订单",
                "sourceTable": "po",
                "parentClassName": None,
                "properties": [],
            }
        ],
        "joins": [],
        "metrics": [],
        "kpis": [],
    }


class _FakePorts:
    """只读端口替身：可注入解析结果与图上实然（节点/边）。"""

    def __init__(
        self,
        *,
        unresolved: bool = False,
        loadError: Exception | None = None,
        actualNodes: dict[str, list[dict]] | None = None,
        actualEdges: list[tuple[str, str, str]] | None = None,
    ) -> None:
        self.unresolved = unresolved
        self.loadError = loadError
        self.actualNodes = actualNodes or {}
        self.actualEdges = actualEdges or []

    async def loadRelease(self, *, releaseId, version):
        if self.loadError is not None:
            raise self.loadError
        from app.services.semantic_graph_mirror_job import ReleaseSnapshot

        return ReleaseSnapshot(
            releaseId=7,
            version="2026.11.09-1",
            status="ACTIVE",
            manifest=_manifest(),
            manifestHash="a" * 64,
        )

    async def resolveEntities(self, manifest):
        classIds = {} if self.unresolved else {"采购订单": "obj:CLASS:1"}
        return EntityResolution(
            classIds=classIds, propertyIds={}, metricIds={}, kpiIds={}
        )

    def readNodes(self, entityType, releaseId):
        return self.actualNodes.get(entityType, [])

    def readEdges(self, releaseId):
        return self.actualEdges


def _classRow(uid: str, name: str, alias: str) -> dict:
    """图上读回的镜像类节点（**全字段** ⇒ 与计划侧可比）。"""
    return {
        "unified_id": uid,
        "name": name,
        "alias": alias,
        "description": "采购订单",
        "sourceTable": "po",
        "release_id": 7,
        "release_version": "2026.11.09-1",
        "mirror_of": uid,
    }


def _run(ports: _FakePorts, **kwargs):
    return asyncio.run(runVerify(ports, releaseId=7, **kwargs))


def test_对账一致且无未解析时退出码_0() -> None:
    ports = _FakePorts(actualNodes={"class": [_classRow("obj:CLASS:1", "采购订单", "PO")]})
    report, exitCode = _run(ports)
    assert exitCode == EXIT_OK
    assert report["status"] == "consistent"
    assert report["diffCount"] == 0
    assert report["unresolvedTotal"] == 0


def test_镜像与计划不一致时退出码_1_且报告带明细() -> None:
    ports = _FakePorts(actualNodes={"class": []})  # 图上没有计划里的类
    report, exitCode = _run(ports)
    assert exitCode == EXIT_INCONSISTENT
    assert report["status"] == "inconsistent"
    assert report["missingNodes"] == [{"entityType": "class", "unifiedId": "obj:CLASS:1"}]
    assert report["diffCount"] >= 1


def test_字段不符也记进报告明细() -> None:
    ports = _FakePorts(actualNodes={"class": [_classRow("obj:CLASS:1", "被改过的名字", "PO")]})
    report, exitCode = _run(ports)
    assert exitCode == EXIT_INCONSISTENT
    assert any(
        m["field"] == "name" and m["actual"] == "被改过的名字"
        for m in report["fieldMismatches"]
    )


def test_发布不存在时退出码_2() -> None:
    ports = _FakePorts(loadError=ScriptFailure("发布不存在：version=x"))
    report, exitCode = _run(ports)
    assert exitCode == EXIT_BLOCKED
    assert "发布不存在" in report["error"]


def test_draft_发布对账被拦_退出码_2() -> None:
    """DRAFT 从未被物化 ⇒ 对它对账只会得到「全缺」的噪音 ⇒ 直接拦下。"""
    ports = _FakePorts()

    async def _loadDraft(*, releaseId, version):
        return _draftSnapshot()

    ports.loadRelease = _loadDraft
    report, exitCode = _run(ports)
    assert exitCode == EXIT_BLOCKED
    assert "DRAFT" in report["error"]


def _draftSnapshot():
    from app.services.semantic_graph_mirror_job import ReleaseSnapshot

    return ReleaseSnapshot(
        releaseId=7,
        version="2026.11.09-1",
        status="DRAFT",
        manifest=_manifest(),
        manifestHash="a" * 64,
    )


def test_图与计划一致但有未解析时退出码_3() -> None:
    """镜像忠实于计划，但计划有洞 ⇒ 镜像相对 manifest 不完整，不能报干净成功。"""
    ports = _FakePorts(unresolved=True, actualNodes={"class": []})
    report, exitCode = _run(ports)
    assert exitCode == EXIT_INCOMPLETE
    assert report["status"] == "incomplete"
    assert report["unresolvedTotal"] >= 1
    assert report["unresolved"]


def test_不一致优先于不完整_退出码_1() -> None:
    """既不一致又有未解析 ⇒ 报不一致（1）；未解析明细仍在报告里。"""
    ports = _FakePorts(
        unresolved=True,
        actualNodes={"class": [_classRow("obj:CLASS:1", "采购订单", "PO")]},
    )
    report, exitCode = _run(ports)
    assert exitCode == EXIT_INCONSISTENT
    assert report["unresolvedTotal"] >= 1


def test_退出码取值互不相同() -> None:
    codes = [EXIT_OK, EXIT_INCONSISTENT, EXIT_BLOCKED, EXIT_INCOMPLETE]
    assert len(set(codes)) == 4
    assert set(codes) == {0, 1, 2, 3}


def test_命令行契约() -> None:
    args = buildParser().parse_args(["--release-version", "2026.11.09-1"])
    assert args.release_id is None
    assert args.release_version == "2026.11.09-1"
    assert args.report_json is None
    args2 = buildParser().parse_args(
        ["--release-id", "7", "--report-json", "/tmp/x.json"]
    )
    assert args2.release_id == 7
    assert args2.report_json == "/tmp/x.json"


def test_报告含计划规模便于人工核对() -> None:
    ports = _FakePorts(actualNodes={"class": [_classRow("obj:CLASS:1", "采购订单", "PO")]})
    report, _ = _run(ports)
    assert report["planNodes"] == 1
    assert report["planEdges"] == 0
    assert report["releaseVersion"] == "2026.11.09-1"
