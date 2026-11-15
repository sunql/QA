"""图谱镜像的**对账**命令行入口（P3b D24）。

把「镜像 vs 计划」逐条求差：计划 = manifest + **当前**解析（`buildMirrorPlan`），
实然 = 图上读回的镜像节点与边。**只读**（一个字节都不写，也不需要写目标闸门）。

口径（L-13）：计划用的是**当前** `id_mapping` ⇒ 物化之后映射变了会出「成对差异」
（计划有图上没有 + 图上有计划没有）——可能是解析漂移而非镜像缺陷；报告原样给出
明细，运维结合台账 `manifest_hash` 判断。见
`app/services/semantic_graph_mirror_verify.py` 模块头。

用法：
    cd backend
    uv run python -m scripts.verify_graph_mirror --release-version 2026.11.09-1
    uv run python -m scripts.verify_graph_mirror --release-id 7 --report-json /tmp/v.json

退出码（运维唯一的判据）：
    0 = 对账一致（且无未解析）
    1 = 镜像与计划不一致（缺/多节点、字段不符、缺/多边 —— 明细在报告里）
    2 = 前置阻断（发布不存在 / DRAFT / 参数）
    3 = 图与计划一致，但计划本身有未解析 ⇒ 镜像相对 manifest 不完整
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# 允许 `python scripts/verify_graph_mirror.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import getSettings
from app.domain.enums import SemanticReleaseStatus
from app.domain.semantic_graph_mirror_spec import RELEASE_MIRROR_ENTITY_TYPES
from app.services.semantic_graph_mirror import buildMirrorPlan
from app.services.semantic_graph_mirror_verify import (
    verifyMirrorAgainstPlan,
)
from scripts.semantic_graph_sync import (
    ScriptFailure,
    _Ports,
    _silenceSqlEcho,
    getSessionFactory,
)

# 退出码（与物化脚本同一个 4 槽契约：0 成功 / 1 有问题 / 2 前置 / 3 部分完成）
EXIT_OK = 0
EXIT_INCONSISTENT = 1
EXIT_BLOCKED = 2
EXIT_INCOMPLETE = 3


def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P3b 图谱镜像对账（只读）")
    parser.add_argument("--release-id", type=int, default=None, help="semantic_release.id")
    parser.add_argument(
        "--release-version", default=None, help="semantic_release.version"
    )
    parser.add_argument(
        "--report-json", default=None, help="把 JSON 报告另存到文件"
    )
    return parser


def _reportOf(
    snapshot, plan, verification, *, unresolved: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "status": verification.status,
        "releaseId": snapshot.releaseId,
        "releaseVersion": snapshot.version,
        "releaseStatus": snapshot.status.value
        if hasattr(snapshot.status, "value")
        else str(snapshot.status),
        "planNodes": len(plan.nodes),
        "planEdges": len(plan.edges),
        "unresolvedTotal": len(plan.unresolved),
        "diffCount": verification.diffCount,
        "missingNodes": [
            {"entityType": d.entityType, "unifiedId": d.unifiedId}
            for d in verification.missingNodes
        ],
        "extraNodes": [
            {"entityType": d.entityType, "unifiedId": d.unifiedId}
            for d in verification.extraNodes
        ],
        "fieldMismatches": [
            {
                "entityType": m.entityType,
                "unifiedId": m.unifiedId,
                "field": m.field,
                "expected": m.expected,
                "actual": m.actual,
            }
            for m in verification.fieldMismatches
        ],
        "missingEdges": [
            {"relType": e.relType, "source": e.sourceUid, "target": e.targetUid}
            for e in verification.missingEdges
        ],
        "extraEdges": [
            {"relType": e.relType, "source": e.sourceUid, "target": e.targetUid}
            for e in verification.extraEdges
        ],
        "unresolved": unresolved,
    }


async def runVerify(
    ports: Any, *, releaseId: int | None = None, version: str | None = None
) -> tuple[dict[str, Any], int]:
    """对账一次 ⇒ (报告, 退出码)。端口只需要 4 个读方法：
    `loadRelease` / `resolveEntities` / `readNodes` / `readEdges`。
    """
    try:
        snapshot = await ports.loadRelease(releaseId=releaseId, version=version)
        if snapshot.status == SemanticReleaseStatus.DRAFT:
            raise ScriptFailure(
                f"发布 {snapshot.version} 还是 DRAFT（从未物化）⇒ 没有可对账的镜像"
            )
        resolution = await ports.resolveEntities(snapshot.manifest)
    except ScriptFailure as exc:
        return {"status": "blocked", "error": str(exc)}, EXIT_BLOCKED
    plan = buildMirrorPlan(
        snapshot.manifest,
        resolution,
        releaseId=snapshot.releaseId,
        releaseVersion=snapshot.version,
    )
    actualNodes = {
        entityType: ports.readNodes(entityType, snapshot.releaseId)
        for entityType in RELEASE_MIRROR_ENTITY_TYPES
    }
    actualEdges = ports.readEdges(snapshot.releaseId)
    verification = verifyMirrorAgainstPlan(plan, actualNodes, actualEdges)
    unresolved = [
        {
            "kind": item.kind,
            "entityType": item.entityType,
            "relType": item.relType,
            "name": item.name,
            "reason": item.reason,
        }
        for item in plan.unresolved
    ]
    report = _reportOf(snapshot, plan, verification, unresolved=unresolved)
    if not verification.isConsistent:
        return report, EXIT_INCONSISTENT
    if plan.unresolved:
        # 图忠实于计划，但计划有洞 ⇒ 镜像相对 manifest 不完整（不要当干净成功）
        report["status"] = "incomplete"
        return report, EXIT_INCOMPLETE
    return report, EXIT_OK


def _printOutcome(report: dict[str, Any], exitCode: int) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exitCode == EXIT_OK:
        print(
            f"[verify_graph_mirror] PASS：一致（计划节点 {report['planNodes']}、"
            f"边 {report['planEdges']}，未解析 0）"
        )
    elif exitCode == EXIT_INCONSISTENT:
        print(
            f"[verify_graph_mirror] FAIL：{report['diffCount']} 条差异"
            f"（缺失/多余/不符明细见报告）",
            file=sys.stderr,
        )
    elif exitCode == EXIT_INCOMPLETE:
        print(
            f"[verify_graph_mirror] PARTIAL：图与计划一致，但有 "
            f"{report['unresolvedTotal']} 条未解析 ⇒ 镜像相对 manifest 不完整",
            file=sys.stderr,
        )
    else:
        print(f"[verify_graph_mirror] {report.get('error', '')}", file=sys.stderr)


async def main() -> int:
    args = buildParser().parse_args()
    settings = getSettings()
    from scripts.kpi_metric_db_guard import databaseNameOf

    print(
        f"[verify_graph_mirror] 数据库：{databaseNameOf(settings.databaseUrl)}"
        f"（只读，不打印凭据）"
    )
    sessionMaker = getSessionFactory()
    _silenceSqlEcho()
    try:
        async with sessionMaker() as session:
            ports = _Ports(session, allowProdWrite=False)
            report, exitCode = await runVerify(
                ports, releaseId=args.release_id, version=args.release_version
            )
    except Exception as exc:  # noqa: BLE001 - 驱动/网络异常也要脚本化退出
        print(
            f"[verify_graph_mirror] FAIL: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return EXIT_INCONSISTENT
    _printOutcome(report, exitCode)
    if args.report_json:
        Path(args.report_json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return exitCode


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
