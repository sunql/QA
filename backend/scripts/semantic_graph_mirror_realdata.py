"""P3b 发布镜像（Neo4j）的真实数据验证脚本。

在**真实数据**上验证「发布镜像」这件事，而不是在 fixture 上自说自话：

1. **计划可复算**：同一份 manifest + 同一份活数据 ⇒ 两次 `buildMirrorPlan`
   结果**逐字节相同**（幂等的前置条件）。
2. **解析结果可见**：manifest 用名字、图谱用 `unified_id` ⇒ 解析不到的实体
   **逐条**列出（类型 / 名字 / 原因），不静默跳过。
3. **写得出、读得回（`--write`）**：把镜像写进 **Neo4j 测试实例**，回读节点/边
   计数与计划**逐类相等**；再写一次，计数不变 ⇒ 幂等。
4. **主图零影响（`--write`）**：镜像写完后，主图各 label 的节点计数**不变**
   ⇒ 镜像 label 确实对现有消费方不可见。

**默认只读**（1、2），可在生产直接跑；`--write` 会写图 ⇒ 写目标闸门
**默认只准测试实例**（`app/infrastructure/neo4j_write_target_guard.py`），
其它目标需显式 `--allow-prod-write`。

用法：
    cd backend
    # 只读门禁（合成快照：从活治理表编译一份 manifest）
    uv run python -m scripts.semantic_graph_mirror_realdata

    # 只读门禁（指定已发布版本）
    uv run python -m scripts.semantic_graph_mirror_realdata --release-id 12

    # 含真实写 + 回读 + 幂等（默认写测试实例）
    NEO4J_URI=bolt://localhost:7688 uv run python -m scripts.semantic_graph_mirror_realdata --write

    # 清理本次写入的镜像节点（须先知道会删多少 ⇒ 用 --expect-delete 对账）
    NEO4J_URI=bolt://localhost:7688 uv run python -m scripts.semantic_graph_mirror_realdata \
        --write --cleanup --expect-delete 3328
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.semantic_release_models import SemanticRelease
from app.infrastructure.database import getSessionFactory
from app.infrastructure.neo4j_write_target_guard import (
    assertSafeNeo4jTarget,
    describeNeo4jTarget,
)
from app.services.semantic_compiler import compileManifest
from app.services.semantic_graph_mirror import (
    ManifestShapeError,
    MirrorPlan,
    buildMirrorPlan,
)
from app.services.semantic_graph_mirror_resolution import resolveManifestEntities

# 合成快照（非已发布版本）使用的 release_id / 版本号
_SYNTHETIC_RELEASE_ID = 0
_SYNTHETIC_RELEASE_VERSION = "synthetic-local"

# 主图 label（写镜像前后要逐个核对计数不变 ⇒ 证明镜像对主图零影响）
_MAIN_GRAPH_LABELS = ("Class", "Property", "Metric", "KpiCatalog")


class VerifyFailure(Exception):
    """一项断言不成立。"""


def _silenceSqlEcho() -> None:
    """关掉 SQL 回显（dev 下 engine `echo=True`），免得把 JSON 报告淹没。

    SQLAlchemy 的 echo 由**实例 logger** 决定：`InstanceLogger` 直接按 echo
    映射成 INFO 并调 `logger._log()`，**绕过** logger 层级 ⇒ 改 logger level
    不管用，只能摘掉它自动挂上的默认 handler（`sqlalchemy.log._add_default_handler`）。
    """
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    engineLogger = logging.getLogger("sqlalchemy.engine.Engine")
    for handler in list(engineLogger.handlers):
        engineLogger.removeHandler(handler)


def _assertSafeNeo4jTarget(uri: str, allowProdWrite: bool) -> None:
    """写图前的目标闸（脚本侧包装：失败 ⇒ 干净退出，不抛栈）。

    判定逻辑在 `app/infrastructure/neo4j_write_target_guard.py`（库层共用），
    这里只把 `ValueError` 转成脚本的退出语义。
    """
    try:
        assertSafeNeo4jTarget(uri, allowProdWrite=allowProdWrite)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


async def _loadManifest(
    session: AsyncSession, releaseId: int
) -> tuple[dict[str, Any], str]:
    """取待镜像的 manifest：指定发布 ⇒ 读快照；未指定 ⇒ 从活治理表编译一份。"""
    if releaseId > 0:
        release = await session.scalar(
            select(SemanticRelease).where(SemanticRelease.id == releaseId)
        )
        if release is None:
            raise VerifyFailure(f"发布 {releaseId} 不存在")
        return dict(release.manifest_json), release.version
    return await compileManifest(session), _SYNTHETIC_RELEASE_VERSION


def _planCounts(plan: MirrorPlan) -> dict[str, Any]:
    rowsByType = plan.nodeRowsByType()
    edgeCounts: dict[str, int] = {}
    for edge in plan.edges:
        edgeCounts[edge.relType] = edgeCounts.get(edge.relType, 0) + 1
    return {
        "nodes": {name: len(rows) for name, rows in rowsByType.items()},
        "nodeTotal": len(plan.nodes),
        "edges": dict(sorted(edgeCounts.items())),
        "edgeTotal": len(plan.edges),
        "unresolvedTotal": len(plan.unresolved),
    }


def _unresolvedItems(plan: MirrorPlan) -> list[dict[str, str]]:
    return [
        {
            "kind": item.kind,
            "entityType": item.entityType,
            "relType": item.relType,
            "name": item.name,
            "reason": item.reason,
        }
        for item in plan.unresolved
    ]


def _mainGraphCounts() -> dict[str, int]:
    """主图各 label 的节点数（写镜像前后对比 ⇒ 零影响证据）。"""
    from app.infrastructure import neo4j_client

    return {
        label: len(neo4j_client.listNodesByLabel(label))
        for label in _MAIN_GRAPH_LABELS
    }


async def _writeAndVerify(
    plan: MirrorPlan,
    neo4jUri: str,
    cleanup: bool,
    expectDelete: int | None,
    allowProdWrite: bool,
) -> dict[str, Any]:
    """真实写 + 回读 + 幂等 + 主图零影响。"""
    os.environ["NEO4J_URI"] = neo4jUri
    # 设置刚改过 ⇒ 清掉 settings 缓存，让 neo4j_client 用新 URI 重建 driver
    from app.config import getSettings as _getSettings
    from app.infrastructure import neo4j_client
    from app.infrastructure import neo4j_mirror_client as mirror

    _getSettings.cache_clear()
    neo4j_client.closeDriver()

    mainNodesBefore = _mainGraphCounts()
    rowsByType = plan.nodeRowsByType()
    written = 0
    for entityType, rows in rowsByType.items():
        if not rows:
            continue
        written += mirror.upsertReleaseMirrorNodes(
            entityType,
            plan.releaseId,
            plan.releaseVersion,
            rows,
            allowProdWrite=allowProdWrite,
        )
    edgeRows = plan.edgeRows()
    edgesWritten = mirror.upsertReleaseMirrorEdges(
        plan.releaseId, edgeRows, allowProdWrite=allowProdWrite
    )

    nodeCounts = mirror.countReleaseMirrorNodes(plan.releaseId)
    edgeCounts = mirror.countReleaseMirrorEdges(plan.releaseId)

    # 幂等：再写一次，节点与边的计数都必须不变
    for entityType, rows in rowsByType.items():
        if not rows:
            continue
        mirror.upsertReleaseMirrorNodes(
            entityType,
            plan.releaseId,
            plan.releaseVersion,
            rows,
            allowProdWrite=allowProdWrite,
        )
    if edgeRows:
        mirror.upsertReleaseMirrorEdges(
            plan.releaseId, edgeRows, allowProdWrite=allowProdWrite
        )
    nodeCountsAfter = mirror.countReleaseMirrorNodes(plan.releaseId)
    edgeCountsAfter = mirror.countReleaseMirrorEdges(plan.releaseId)

    mainNodesAfter = _mainGraphCounts()

    plannedNodes = {name: len(rows) for name, rows in rowsByType.items()}
    if nodeCounts != plannedNodes:
        raise VerifyFailure(
            f"回读节点数与计划不一致：回读 {nodeCounts} vs 计划 {plannedNodes}"
        )
    plannedEdges = _planCounts(plan)["edges"]
    for relType, expected in plannedEdges.items():
        if edgeCounts.get(relType, 0) != expected:
            raise VerifyFailure(
                f"回读 {relType} 边数与计划不一致：回读 {edgeCounts.get(relType, 0)} vs 计划 {expected}"
            )
    if nodeCountsAfter != nodeCounts:
        raise VerifyFailure(
            f"重复写入后节点计数变了（不幂等）：{nodeCounts} → {nodeCountsAfter}"
        )
    if edgeCountsAfter != edgeCounts:
        raise VerifyFailure(
            f"重复写入后边计数变了（不幂等）：{edgeCounts} → {edgeCountsAfter}"
        )
    if mainNodesAfter != mainNodesBefore:
        raise VerifyFailure(
            f"主图节点数变了（镜像污染了主图）：{mainNodesBefore} → {mainNodesAfter}"
        )

    deleted = 0
    if cleanup:
        # keep 集合置空 ⇒ 删掉该 release_id 下的**全部**镜像节点（含本次写入的）。
        # 「计划外节点」式的裁剪是 D23 物化 Job 的职责（幂等重物化时用），
        # 这里的 --cleanup 是收尾动作：把测试实例里这次写的东西擦干净。
        doomed = sum(mirror.countReleaseMirrorNodes(plan.releaseId).values())
        if expectDelete is None:
            raise VerifyFailure(
                f"--cleanup 会删除 {doomed} 个镜像节点，"
                "必须显式给 --expect-delete <数量> 对账后才执行"
            )
        if doomed != expectDelete:
            raise VerifyFailure(
                f"--cleanup 预期删 {expectDelete} 个，实际将删 {doomed} 个 ⇒ 拒绝执行"
                "（数量对不上说明目标选错了）"
            )
        deleted = mirror.pruneReleaseMirror(
            plan.releaseId,
            set(),
            confirm=True,
            allowProdWrite=allowProdWrite,
        )

    return {
        "writtenNodes": written,
        "writtenEdges": edgesWritten,
        "readBackNodes": nodeCounts,
        "readBackEdges": edgeCounts,
        "idempotent": True,
        "mainGraphUnchanged": True,
        "mainGraphNodes": mainNodesAfter,
        "cleanedUp": deleted if cleanup else None,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="P3b 发布镜像真实数据验证")
    parser.add_argument(
        "--release-id",
        type=int,
        default=0,
        help="已发布版本的 semantic_release.id；0 = 从活治理表编译一份合成快照",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="把镜像真实写进 Neo4j 并回读校验（默认只读）",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="写完清掉本次镜像节点（清该 release_id 下的全部镜像节点）",
    )
    parser.add_argument(
        "--expect-delete",
        type=int,
        default=None,
        help="配合 --cleanup：预期删除的镜像节点数（与实际对不上 ⇒ 拒绝执行）",
    )
    parser.add_argument(
        "--allow-prod-write",
        action="store_true",
        help="允许向生产 Neo4j 写镜像（默认拒绝）",
    )
    args = parser.parse_args()

    settings = getSettings()
    from scripts.kpi_metric_db_guard import databaseNameOf

    print(
        f"[semantic_graph_mirror_realdata] 数据库："
        f"{databaseNameOf(settings.databaseUrl)}（只读，不打印凭据）"
    )

    sessionMaker = getSessionFactory()
    _silenceSqlEcho()
    async with sessionMaker() as session:
        try:
            manifest, version = await _loadManifest(session, args.release_id)
            resolution = await resolveManifestEntities(session, manifest)
            plan = buildMirrorPlan(
                manifest,
                resolution,
                releaseId=args.release_id
                if args.release_id > 0
                else _SYNTHETIC_RELEASE_ID,
                releaseVersion=version,
            )
            # 复算一次 ⇒ 确定性（同输入同输出 ⇒ 幂等的前提）
            planAgain = buildMirrorPlan(
                manifest,
                resolution,
                releaseId=plan.releaseId,
                releaseVersion=plan.releaseVersion,
            )
            if plan != planAgain:
                raise VerifyFailure("同一输入两次 buildMirrorPlan 结果不同（不可复算）")
        except (VerifyFailure, ManifestShapeError, SQLAlchemyError) as exc:
            print(f"[semantic_graph_mirror_realdata] FAIL: {exc}", file=sys.stderr)
            return 1

        report: dict[str, Any] = {
            "releaseId": plan.releaseId,
            "releaseVersion": plan.releaseVersion,
            "manifest": {
                "classes": len(manifest.get("classes") or []),
                "joins": len(manifest.get("joins") or []),
                "metrics": len(manifest.get("metrics") or []),
                "kpis": len(manifest.get("kpis") or []),
            },
            "plan": _planCounts(plan),
            "deterministic": True,
            "unresolved": _unresolvedItems(plan),
        }

        if args.write:
            neo4jUri = os.environ.get("NEO4J_URI", settings.neo4jUri)
            try:
                _assertSafeNeo4jTarget(neo4jUri, args.allow_prod_write)
                report["neo4j"] = {
                    "target": describeNeo4jTarget(neo4jUri),
                    **await _writeAndVerify(
                        plan,
                        neo4jUri,
                        args.cleanup,
                        args.expect_delete,
                        args.allow_prod_write,
                    ),
                }
            except (VerifyFailure, ManifestShapeError) as exc:
                print(f"[semantic_graph_mirror_realdata] FAIL: {exc}", file=sys.stderr)
                return 1
            except Exception as exc:  # noqa: BLE001 - 驱动/网络异常也要脚本化退出
                print(
                    f"[semantic_graph_mirror_realdata] FAIL: 写图异常 "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    unresolvedTotal = report["plan"]["unresolvedTotal"]
    suffix = f"，未解析 {unresolvedTotal} 条（见 unresolved）" if unresolvedTotal else ""
    print(
        f"[semantic_graph_mirror_realdata] PASS：镜像节点 "
        f"{report['plan']['nodeTotal']}、边 {report['plan']['edgeTotal']}"
        f"{suffix}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
