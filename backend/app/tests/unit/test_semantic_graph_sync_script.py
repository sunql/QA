"""图谱镜像物化 Job 命令行（P3b D23）的单测。

脚本是**装配点**：把 `MirrorJobPorts` 接到真实 PG / Neo4j。本文件只测脚本自身的
**决策与退出码契约**（端口用替身，不连数据库）——运维靠退出码判断成败，所以
「什么情况对应哪个退出码」是这里最要紧的契约。

退出码契约：
    0 = 成功（物化完成 / 预览完成）
    1 = 运行失败（写图失败、收尾核对不一致）
    2 = 前置失败（参数 / 发布不存在 / DRAFT / 已物化拒写 / 未解析全 /
        裁剪数量对不上 / 生产目标被闸门拒）
    3 = 物化完成但仍有未解析实体（镜像不完整）
"""

from __future__ import annotations

import asyncio

from app.services.semantic_graph_mirror import EntityResolution
from scripts.semantic_graph_sync import (
    EXIT_BLOCKED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_PARTIAL,
    JobOptions,
    ScriptFailure,
    buildParser,
    runJob,
)

# =============================================================================
# 替身端口
# =============================================================================


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


def _snapshot(status: str = "ACTIVE"):
    from app.services.semantic_graph_mirror_job import ReleaseSnapshot

    return ReleaseSnapshot(
        releaseId=7,
        version="2026.11.09-1",
        status=status,
        manifest=_manifest(),
        manifestHash="a" * 64,
    )


class _FakePorts:
    """最小端口替身：可指定发布状态、解析结果与失败点。"""

    def __init__(self, *, status: str = "ACTIVE", failOn: str = "", staleCount: int = 0):
        self.status = status
        self.failOn = failOn
        self.staleCount = staleCount
        self.nodeWrites: list[str] = []
        self.nodeRows: dict[str, int] = {}
        self.edgeWrites: list[int] = []
        self.prunes: list[set[str]] = []
        self.states: list[object] = []
        self.batchWrites: list[tuple[str, str]] = []
        self.loadError: Exception | None = None

    async def loadRelease(self, *, releaseId, version):
        if self.loadError is not None:
            raise self.loadError
        return _snapshot(self.status)

    async def resolveEntities(self, manifest):
        return EntityResolution(
            classIds={"采购订单": "obj:CLASS:1"},
            propertyIds={},
            metricIds={},
            kpiIds={},
        )

    def writeNodes(self, entityType, releaseId, releaseVersion, rows):
        if self.failOn == f"nodes:{entityType}":
            raise RuntimeError(f"写入 {entityType} 失败")
        self.nodeWrites.append(entityType)
        self.nodeRows[entityType] = self.nodeRows.get(entityType, 0) + len(rows)
        return len(rows)

    def writeEdges(self, releaseId, edges):
        if self.failOn == "edges":
            raise RuntimeError("写入边失败")
        self.edgeWrites.append(len(edges))
        return len(edges)

    def prune(self, releaseId, keepUnifiedIds, *, confirm):
        self.prunes.append(set(keepUnifiedIds))
        return 0

    def countStaleNodes(self, releaseId, keepUnifiedIds):
        return self.staleCount

    def countNodes(self, releaseId):
        # 按**行数**统计（不是调用次数）⇒ 与「图上节点数」同口径
        return {
            "class": self.nodeRows.get("class", 0),
            "property": self.nodeRows.get("property", 0),
            "metric": self.nodeRows.get("metric", 0),
            "kpi": self.nodeRows.get("kpi", 0),
        }

    def countEdges(self, releaseId):
        return {}

    def readState(self, releaseId):
        return self.states[-1] if self.states else None

    def writeState(self, state):
        self.states.append(state)

    def readBatches(self, releaseId):
        return []

    def writeBatch(self, batch):
        self.batchWrites.append((batch.batchNo, batch.status))


def _run(ports: _FakePorts, **kwargs):
    options = JobOptions(releaseId=7, **kwargs)
    return asyncio.run(runJob(ports, options))


# =============================================================================
# 退出码契约
# =============================================================================


def test_默认不_apply_时只预览不写图() -> None:
    ports = _FakePorts()
    report, exitCode = _run(ports)
    assert exitCode == EXIT_OK
    assert report["status"] == "dry-run"
    assert report["dryRun"] is True
    assert ports.nodeWrites == []
    assert ports.states == []


def test_draft_发布被拦下时退出码是_2() -> None:
    ports = _FakePorts(status="DRAFT")
    report, exitCode = _run(ports, apply=True)
    assert exitCode == EXIT_BLOCKED
    assert report["status"] == "blocked"
    assert "DRAFT" in report["error"]


def test_写图失败时退出码是_1() -> None:
    ports = _FakePorts(failOn="nodes:class")
    report, exitCode = _run(ports, apply=True)
    assert exitCode == EXIT_FAILED
    assert report["status"] == "failed"
    assert report["error"]


def test_发布不存在时退出码是_2() -> None:
    ports = _FakePorts()
    ports.loadError = ScriptFailure("发布不存在：version=x")
    report, exitCode = _run(ports, apply=True)
    assert exitCode == EXIT_BLOCKED
    assert report["status"] == "blocked"
    assert "发布不存在" in report["error"]


def test_裁剪未给_expect_delete_时退出码是_2() -> None:
    """Neo4j 无备份 ⇒ 不知道会删多少就不许裁剪。"""
    ports = _FakePorts()
    report, exitCode = _run(ports, apply=True, prune=True)
    assert exitCode == EXIT_BLOCKED
    assert "expect-delete" in report["error"]
    assert ports.prunes == []


def test_裁剪数量对不上时退出码是_2() -> None:
    ports = _FakePorts(staleCount=5)
    report, exitCode = _run(ports, apply=True, prune=True, expectDelete=3)
    assert exitCode == EXIT_BLOCKED
    assert "不一致" in report["error"]
    assert ports.prunes == []


def test_物化成功时退出码是_0_且报告带计数() -> None:
    ports = _FakePorts()
    report, exitCode = _run(ports, apply=True)
    assert exitCode == EXIT_OK
    assert report["status"] == "materialized"
    assert report["nodes"] == 1
    assert report["releaseVersion"] == "2026.11.09-1"
    assert report["unresolvedTotal"] == 0


def test_报告含批次明细便于排障() -> None:
    ports = _FakePorts()
    report, _ = _run(ports, apply=True)
    kinds = [batch["kind"] for batch in report["batches"]]
    assert "nodes" in kinds
    for batch in report["batches"]:
        assert batch["status"] == "succeeded"
        assert batch["elapsedMs"] >= 0


# =============================================================================
# 命令行契约
# =============================================================================


def test_命令行默认不写图() -> None:
    args = buildParser().parse_args(["--release-id", "7"])
    assert args.apply is False
    assert args.force_rebuild is False
    assert args.allow_partial is False
    assert args.prune is False
    assert args.expect_delete is None
    assert args.allow_prod_write is False


def test_命令行能显式打开写与重建() -> None:
    args = buildParser().parse_args(
        [
            "--release-id",
            "7",
            "--apply",
            "--force-rebuild",
            "--allow-partial",
            "--prune",
            "--expect-delete",
            "3",
            "--allow-prod-write",
        ]
    )
    assert args.apply is True
    assert args.force_rebuild is True
    assert args.allow_partial is True
    assert args.prune is True
    assert args.expect_delete == 3
    assert args.allow_prod_write is True


def test_未解析全时默认拒物化_退出码_2() -> None:
    """半份镜像比没镜像更危险 ⇒ 默认 fail-closed。"""

    class _PartialPorts(_FakePorts):
        async def resolveEntities(self, manifest):
            return EntityResolution(classIds={}, propertyIds={}, metricIds={}, kpiIds={})

    ports = _PartialPorts()
    report, exitCode = _run(ports, apply=True)
    assert exitCode == EXIT_BLOCKED
    assert "未解析" in report["error"]


def test_未解析全时放行则退出码是_3() -> None:
    """放行也会留下不完整的镜像 ⇒ 不能报干净成功。"""

    class _PartialPorts(_FakePorts):
        async def resolveEntities(self, manifest):
            return EntityResolution(classIds={}, propertyIds={}, metricIds={}, kpiIds={})

    ports = _PartialPorts()
    report, exitCode = _run(ports, apply=True, allowPartial=True)
    assert exitCode == EXIT_PARTIAL
    assert report["unresolvedTotal"] >= 1
    assert report["unresolved"]


def test_退出码取值互不相同_避免运维误判() -> None:
    """0/1/2/3 各自含义不同 ⇒ 不能撞号（撞号 ⇒ 运维拿错判据）。"""
    codes = [EXIT_OK, EXIT_FAILED, EXIT_BLOCKED, EXIT_PARTIAL]
    assert len(set(codes)) == 4
    assert set(codes) == {0, 1, 2, 3}
