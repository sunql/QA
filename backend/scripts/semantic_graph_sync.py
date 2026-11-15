"""图谱镜像的**物化 Job** 命令行入口（P3b D23）。

把「一次已编译的语义发布」物化成 Neo4j 图谱镜像：读发布快照 → 解析实体 →
建镜像计划 → 按批次幂等写图 → 记台账 → 收尾核对。编排逻辑在
`app/services/semantic_graph_mirror_job.py`，**本脚本是唯一的装配点**
（把端口接到真实 PG / Neo4j）。

**默认只读**（预览）：不加 `--apply` 就只出计划，一个字节都不写。
真写时**写目标闸门默认只准测试实例**（`app/infrastructure/neo4j_write_target_guard.py`），
生产目标要显式 `--allow-prod-write`。

用法：
    cd backend
    # 预览（默认）：只解析 + 只出计划，不写图
    uv run python -m scripts.semantic_graph_sync --release-version 2026.11.09-1

    # 真写（默认写测试实例）
    NEO4J_URI=bolt://localhost:7688 uv run python -m scripts.semantic_graph_sync \
        --release-version 2026.11.09-1 --apply

    # 重建已物化的发布（快照不可变 ⇒ 默认拒写，重建必须显式）
    ... --apply --force-rebuild

    # 裁剪「计划外」的残留镜像节点（必须给 --expect-delete 对账）
    ... --apply --prune --expect-delete 3

退出码（运维唯一的判据）：
    0 = 成功（物化完成 / 预览完成）
    1 = 运行失败（写图失败、收尾核对不一致）
    2 = 前置失败（参数 / 发布不存在 / DRAFT / 已物化拒写 / 实体未解析全 /
        裁剪数量对不上 / 生产目标被写目标闸门拒绝）
    3 = 物化完成但仍有未解析实体（镜像不完整；只有 `--allow-partial` 才可能出现）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 允许 `python scripts/semantic_graph_sync.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.semantic_release_models import SemanticRelease
from app.infrastructure.database import getSessionFactory
from app.services.semantic_graph_mirror import EntityResolution
from app.services.semantic_graph_mirror_job import (
    MirrorJobBlocked,
    MirrorJobError,
    MirrorJobResult,
    ReleaseSnapshot,
    runMirrorJob,
)
from app.services.semantic_graph_mirror_ledger import (
    batchFromRow,
    batchToRow,
    stateFromRow,
    stateToRow,
)
from app.services.semantic_graph_mirror_resolution import resolveManifestEntities

# 退出码
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 2
EXIT_PARTIAL = 3


class ScriptFailure(Exception):
    """脚本层的前置失败（参数 / 发布不存在等）⇒ 退出码 2。"""


@dataclass(frozen=True)
class JobOptions:
    """Job 的运行选项（与 argparse 解耦 ⇒ 便于单测直接构造）。"""

    releaseId: int | None = None
    version: str | None = None
    apply: bool = False
    forceRebuild: bool = False
    allowPartial: bool = False
    prune: bool = False
    expectDelete: int | None = None
    resume: bool = True


class _Ports:
    """把 Job 的端口接到真实 PG / Neo4j。"""

    def __init__(self, session: AsyncSession, *, allowProdWrite: bool) -> None:
        self._session = session
        self._allowProdWrite = allowProdWrite

    # --- 发布快照 / 解析 ---------------------------------------------------
    async def loadRelease(
        self, *, releaseId: int | None, version: str | None
    ) -> ReleaseSnapshot:
        row = await self._loadReleaseRow(releaseId, version)
        return ReleaseSnapshot(
            releaseId=row.id,
            version=row.version,
            status=row.status,
            manifest=dict(row.manifest_json),
            manifestHash=row.manifest_hash,
        )

    async def _loadReleaseRow(
        self, releaseId: int | None, version: str | None
    ) -> SemanticRelease:
        if releaseId is None and version is None:
            raise ScriptFailure("必须给 --release-id 或 --release-version")
        stmt = select(SemanticRelease)
        if releaseId is not None:
            stmt = stmt.where(SemanticRelease.id == releaseId)
        else:
            stmt = stmt.where(SemanticRelease.version == version)
        row = await self._session.scalar(stmt)
        if row is None:
            target = f"id={releaseId}" if releaseId is not None else f"version={version}"
            raise ScriptFailure(f"发布不存在：{target}")
        return row

    async def resolveEntities(self, manifest) -> EntityResolution:
        return await resolveManifestEntities(self._session, manifest)

    # --- 图写 -------------------------------------------------------------
    def writeNodes(
        self, entityType, releaseId, releaseVersion, rows
    ) -> int:
        from app.infrastructure import neo4j_mirror_client as mirror

        return mirror.upsertReleaseMirrorNodes(
            entityType,
            releaseId,
            releaseVersion,
            rows,
            allowProdWrite=self._allowProdWrite,
        )

    def writeEdges(self, releaseId, edges) -> int:
        from app.infrastructure import neo4j_mirror_client as mirror

        return mirror.upsertReleaseMirrorEdges(
            releaseId, edges, allowProdWrite=self._allowProdWrite
        )

    def prune(self, releaseId, keepUnifiedIds, *, confirm) -> int:
        from app.infrastructure import neo4j_mirror_client as mirror

        return mirror.pruneReleaseMirror(
            releaseId,
            keepUnifiedIds,
            confirm=confirm,
            allowProdWrite=self._allowProdWrite,
        )

    def countStaleNodes(self, releaseId, keepUnifiedIds) -> int:
        from app.infrastructure import neo4j_mirror_client as mirror

        return mirror.countStaleReleaseMirrorNodes(releaseId, keepUnifiedIds)

    def countNodes(self, releaseId) -> dict[str, int]:
        from app.infrastructure import neo4j_mirror_client as mirror

        return mirror.countReleaseMirrorNodes(releaseId)

    def countEdges(self, releaseId) -> dict[str, int]:
        from app.infrastructure import neo4j_mirror_client as mirror

        return mirror.countReleaseMirrorEdges(releaseId)

    # --- 台账 -------------------------------------------------------------
    def readState(self, releaseId):
        from app.infrastructure import neo4j_mirror_client as mirror

        row = mirror.readMirrorJobState(releaseId)
        return None if row is None else stateFromRow(row)

    def writeState(self, state) -> None:
        from app.infrastructure import neo4j_mirror_client as mirror

        mirror.writeMirrorJobState(
            stateToRow(state), allowProdWrite=self._allowProdWrite
        )

    def readBatches(self, releaseId):
        from app.infrastructure import neo4j_mirror_client as mirror

        return [
            batchFromRow(row) for row in mirror.readMirrorJobBatches(releaseId)
        ]

    def writeBatch(self, batch) -> None:
        from app.infrastructure import neo4j_mirror_client as mirror

        mirror.writeMirrorJobBatch(
            batchToRow(batch), allowProdWrite=self._allowProdWrite
        )


def _silenceSqlEcho() -> None:
    """关掉 SQL 回显（dev 下 engine `echo=True`），免得把 JSON 报告淹没。"""
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    engineLogger = logging.getLogger("sqlalchemy.engine.Engine")
    for handler in list(engineLogger.handlers):
        engineLogger.removeHandler(handler)


def _unresolvedItems(result: MirrorJobResult) -> list[dict[str, str]]:
    return [
        {
            "kind": item.kind,
            "entityType": item.entityType,
            "relType": item.relType,
            "name": item.name,
            "reason": item.reason,
        }
        for item in result.unresolved
    ]


def _batchItems(result: MirrorJobResult) -> list[dict[str, Any]]:
    return [
        {
            "batchNo": batch.batchNo,
            "kind": batch.kind,
            "status": batch.status,
            "count": batch.count,
            "elapsedMs": batch.elapsedMs,
            "error": batch.error,
        }
        for batch in result.batches
    ]


async def runJob(ports: Any, options: JobOptions) -> tuple[dict[str, Any], int]:
    """跑一次物化并给出 (报告, 退出码)。**不碰 IO**（IO 全在端口里）。"""
    try:
        result = await runMirrorJob(
            ports,
            releaseId=options.releaseId,
            version=options.version,
            dryRun=not options.apply,
            forceRebuild=options.forceRebuild,
            allowPartial=options.allowPartial,
            prune=options.prune,
            pruneConfirm=options.prune,
            expectDelete=options.expectDelete,
            resume=options.resume,
        )
    except ScriptFailure as exc:
        return {"status": "blocked", "error": str(exc)}, EXIT_BLOCKED
    except MirrorJobBlocked as exc:
        return {"status": "blocked", "error": str(exc)}, EXIT_BLOCKED
    except MirrorJobError as exc:
        return {"status": "failed", "error": str(exc)}, EXIT_FAILED
    report = {
        "status": result.status,
        "releaseId": result.releaseId,
        "releaseVersion": result.releaseVersion,
        "dryRun": result.dryRun,
        "nodes": result.nodeCount,
        "edges": result.edgeCount,
        "unresolvedTotal": result.unresolvedCount,
        "batches": _batchItems(result),
        "unresolved": _unresolvedItems(result),
    }
    # 物化完成但仍有未解析 ⇒ 镜像不完整 ⇒ 退出码 3（不要当成干净成功）
    exitCode = EXIT_PARTIAL if result.unresolvedCount else EXIT_OK
    return report, exitCode


def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P3b 图谱镜像物化 Job")
    parser.add_argument("--release-id", type=int, default=None, help="semantic_release.id")
    parser.add_argument(
        "--release-version", default=None, help="semantic_release.version"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真的写图（默认只预览：一个字节都不写）",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="允许重建已物化的发布（默认拒写：快照不可变）",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="实体没解析全也继续物化（默认拒绝：半份镜像更危险）",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="裁剪本次计划外的残留镜像节点与边（破坏性操作）",
    )
    parser.add_argument(
        "--expect-delete",
        type=int,
        default=None,
        help="配合 --prune：预期删除的镜像节点数（与实际对不上 ⇒ 拒绝执行）",
    )
    parser.add_argument(
        "--allow-prod-write",
        action="store_true",
        help="允许向生产 Neo4j 写（默认拒绝；写目标闸门在库层）",
    )
    parser.add_argument(
        "--report-json", default=None, help="把 JSON 报告另存到文件"
    )
    return parser


async def main() -> int:
    args = buildParser().parse_args()
    settings = getSettings()
    from scripts.kpi_metric_db_guard import databaseNameOf

    print(
        f"[semantic_graph_sync] 数据库：{databaseNameOf(settings.databaseUrl)}"
        f"（只读，不打印凭据）"
    )

    neo4jUri = _applyNeo4jOverride(settings)
    if args.apply:
        blocked = _assertWriteTarget(neo4jUri, allowProdWrite=args.allow_prod_write)
        if blocked:
            return EXIT_BLOCKED

    options = JobOptions(
        releaseId=args.release_id,
        version=args.release_version,
        apply=args.apply,
        forceRebuild=args.force_rebuild,
        allowPartial=args.allow_partial,
        prune=args.prune,
        expectDelete=args.expect_delete,
    )
    report, exitCode = await _runWithOptions(
        options, allowProdWrite=args.allow_prod_write
    )
    if report is not None:
        _printReport(args, report, exitCode)
    return exitCode


def _applyNeo4jOverride(settings) -> str:
    """NEO4J_URI 环境变量优先于配置；生效后要重载设置并重置 driver。"""
    neo4jUri = os.environ.get("NEO4J_URI", settings.neo4jUri)
    if neo4jUri != settings.neo4jUri:
        os.environ["NEO4J_URI"] = neo4jUri
        getSettings.cache_clear()
        from app.infrastructure import neo4j_client

        neo4j_client.closeDriver()
    return neo4jUri


def _assertWriteTarget(neo4jUri: str, *, allowProdWrite: bool) -> bool:
    """脚本侧提前拦一道（库层闸门仍然生效 ⇒ 这是双保险，不是替代）。

    返回 True 表示被拦下（调用方以退出码 2 结束）。
    """
    from app.infrastructure.neo4j_write_target_guard import (
        assertSafeNeo4jTarget,
        describeNeo4jTarget,
    )

    try:
        assertSafeNeo4jTarget(neo4jUri, allowProdWrite=allowProdWrite)
    except ValueError as exc:
        print(f"[semantic_graph_sync] BLOCKED: {exc}", file=sys.stderr)
        return True
    print(f"[semantic_graph_sync] 写目标：{describeNeo4jTarget(neo4jUri)}")
    return False


async def _runWithOptions(
    options: JobOptions, *, allowProdWrite: bool
) -> tuple[dict[str, Any] | None, int]:
    """装配真实端口并跑一次 Job ⇒ (报告, 退出码)；异常 ⇒ (None, 1)。

    异常时报告给不了就不给（错误已打到 stderr）。
    """
    sessionMaker = getSessionFactory()
    _silenceSqlEcho()
    try:
        async with sessionMaker() as session:
            ports = _Ports(session, allowProdWrite=allowProdWrite)
            return await runJob(ports, options)
    except SQLAlchemyError as exc:
        print(f"[semantic_graph_sync] FAIL: 数据库异常 {exc}", file=sys.stderr)
        return None, EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - 驱动/网络异常也要脚本化退出
        print(
            f"[semantic_graph_sync] FAIL: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None, EXIT_FAILED


def _printReport(args, report: dict[str, Any], exitCode: int) -> None:
    """报告打到 stdout / 落盘（`--report-json`）/ 给一行结论。"""
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report_json:
        Path(args.report_json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if exitCode == EXIT_OK:
        print(
            f"[semantic_graph_sync] PASS：{report['status']}，节点 {report['nodes']}、"
            f"边 {report['edges']}"
        )
    elif exitCode == EXIT_PARTIAL:
        print(
            f"[semantic_graph_sync] PARTIAL：已物化但有 {report['unresolvedTotal']} "
            f"条未解析（镜像不完整，见 unresolved）",
            file=sys.stderr,
        )
    else:
        print(
            f"[semantic_graph_sync] {report.get('status', 'FAIL')}："
            f"{report.get('error', '')}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
