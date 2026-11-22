"""G5 采购域试点：供应商 + 物料 —— 剖析 → 候选 → 评测 → 生成签收物料。

一次跑完，把业务签收要的三样东西写到 `--out-dir`（默认 `/tmp/discovery-pilot-<时间戳>`）：

- `summary.md`：一页摘要（候选数/问题数/证据完整率/verdict），**不含真实值**；
- `candidates.csv`：逐条候选 + 证据摘要 + 空白的业务判定列（utf-8-sig，Excel 可开）；
- `findings.csv`：逐条主数据问题 + 空白判定列。

⚠️ CSV 含**真实业务值**（证据 summary 里有 min/max、供应商名等）⇒ 默认写仓库外；
**不要提交进仓库**。仓库只保留签收模板。

用法::

    python -m scripts.discovery_pilot_supplier_material --datasource 1 \\
        --tables DIM_SUPPLIER,DIM_IMATERIAL --dataset-version pilot-2026-11

退出码：0 = 剖析+候选+评测完成；2 = 无数据源 / 无快照 / 无模型配置 / LLM 失败。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.domain.discovery_models import DiscoverySource  # noqa: E402
from app.domain.discovery_pipeline_models import (  # noqa: E402
    DiscoveryCandidate,
    DiscoveryFinding,
)
from app.domain.models import DataSource  # noqa: E402
from app.infrastructure.database import ownSession  # noqa: E402
from app.services.discovery_candidate_service import (  # noqa: E402
    DiscoveryCandidateService,
)
from app.services.discovery_evaluation_service import (  # noqa: E402
    DiscoveryEvaluationService,
)
from app.services.discovery_profiler import DiscoveryProfiler  # noqa: E402
from app.services.discovery_signoff import (  # noqa: E402
    CANDIDATE_FIELDS,
    FINDING_FIELDS,
    buildSummaryMarkdown,
    candidateRows,
    findingRows,
    toCsv,
)
from app.services.discovery_snapshot_service import (  # noqa: E402
    DiscoverySnapshotService,
)

DEFAULT_TABLES = "DIM_SUPPLIER,DIM_IMATERIAL"
DEFAULT_DATASET_VERSION = "pilot-2026-11"


def _parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasource", default="1", help="业务数据源 id 或名称（默认 1）")
    parser.add_argument("--tables", default=DEFAULT_TABLES, help="逗号分隔的表名")
    parser.add_argument(
        "--dataset-version", default=DEFAULT_DATASET_VERSION, help="评测数据集版本"
    )
    parser.add_argument(
        "--out-dir", default=None, help="签收物料输出目录（默认 /tmp/discovery-pilot-<时间戳>）"
    )
    return parser.parse_args()


async def _resolveDatasource(session, spec: str) -> DataSource | None:
    statement = select(DataSource).where(DataSource.is_active.is_(True))
    if spec.isdigit():
        statement = statement.where(DataSource.id == int(spec))
    else:
        statement = statement.where(DataSource.name == spec)
    return (await session.execute(statement.limit(1))).scalars().first()


async def _resolveSource(session, datasource: DataSource) -> DiscoverySource:
    source = (
        await session.execute(
            select(DiscoverySource).where(DiscoverySource.datasource_id == datasource.id)
        )
    ).scalars().first()
    if source is not None:
        return source
    return await DiscoverySnapshotService().registerSource(
        session,
        name=f"pilot:{datasource.name}"[:100],
        datasourceId=datasource.id,
        owner="pilot",
        note="discovery_pilot_supplier_material.py 自动登记（G5 试点）",
        createdBy="pilot-script",
    )


async def _latestSnapshotId(
    session, snapshotService: DiscoverySnapshotService, source: DiscoverySource
) -> int:
    rows = await snapshotService.listSnapshots(session, sourceId=source.id, limit=1)
    if rows:
        return rows[0].id
    captured = await snapshotService.captureSnapshot(
        session, sourceId=source.id, createdBy="pilot-script"
    )
    return captured.snapshotId


async def _run(args: argparse.Namespace) -> int:
    tables = [name.strip() for name in args.tables.split(",") if name.strip()]
    outDir = Path(
        args.out_dir
        or f"/tmp/discovery-pilot-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    )
    outDir.mkdir(parents=True, exist_ok=True)

    async with ownSession() as session:
        datasource = await _resolveDatasource(session, args.datasource)
        if datasource is None:
            print("[pilot] 没有可用的活跃业务数据源", file=sys.stderr)
            return 2
        source = await _resolveSource(session, datasource)
        snapshotId = await _latestSnapshotId(
            session, DiscoverySnapshotService(), source
        )
        print(f"[pilot] 数据源={datasource.name} 发现源={source.id} 快照={snapshotId}")

        try:
            profile = await DiscoveryProfiler().profileTables(
                session,
                sourceId=source.id,
                snapshotId=snapshotId,
                tables=tables,
                createdBy="pilot-script",
            )
            print(
                f"[pilot] 剖析完成 run={profile.runId} tables={profile.tableCount} "
                f"rows={profile.profileRowCount}"
            )
            candidates = await DiscoveryCandidateService().generateCandidates(
                session, runId=profile.runId
            )
            print(
                f"[pilot] 候选完成 candidates={candidates.candidateCount} "
                f"findings={candidates.findingCount} tokens={candidates.promptTokens}"
                f"/{candidates.completionTokens} cost={candidates.cost}"
            )
        except Exception as exc:  # noqa: BLE001 - 试点脚本：打印原因并退出码 2
            print(f"[pilot] 试点失败：{exc}", file=sys.stderr)
            return 2

        evaluation = await DiscoveryEvaluationService().evaluateRun(
            session,
            runId=profile.runId,
            datasetVersion=args.dataset_version,
            createdBy="pilot-script",
        )
        rows = list(
            (
                await session.execute(
                    select(DiscoveryCandidate)
                    .where(DiscoveryCandidate.run_id == profile.runId)
                    .order_by(DiscoveryCandidate.id)
                )
            )
            .scalars()
            .all()
        )
        findings = list(
            (
                await session.execute(
                    select(DiscoveryFinding)
                    .where(DiscoveryFinding.run_id == profile.runId)
                    .order_by(DiscoveryFinding.id)
                )
            )
            .scalars()
            .all()
        )

    (outDir / "candidates.csv").write_text(
        toCsv(candidateRows(rows), CANDIDATE_FIELDS), encoding="utf-8-sig"
    )
    (outDir / "findings.csv").write_text(
        toCsv(findingRows(findings), FINDING_FIELDS), encoding="utf-8-sig"
    )
    (outDir / "summary.md").write_text(
        buildSummaryMarkdown(
            datasetVersion=args.dataset_version,
            runId=profile.runId,
            sourceId=source.id,
            snapshotId=snapshotId,
            metrics=evaluation.metrics_json,
            verdict=evaluation.verdict,
            generatedAt=datetime.now(UTC).isoformat(),
            tableNames=tables,
        ),
        encoding="utf-8",
    )
    print(
        f"[pilot] 评测 verdict={evaluation.verdict} "
        f"evidenceRate={evaluation.metrics_json.get('evidenceCompleteRate')}"
    )
    print(f"[pilot] 签收物料已写入 {outDir}（含真实值，勿提交仓库）")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run(_parseArgs())))


if __name__ == "__main__":
    main()
