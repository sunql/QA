"""G5 试点签收物料的**纯函数**（候选/问题 → CSV / 摘要 Markdown）。

业务签收需要给评审人一张能直接勾选 / 批注的表：候选、证据、置信度、判定列。
本模块只做「行 → 文本」，不连库、不写盘（写盘在 `scripts/discovery_pilot_*.py`）。

## 数据边界

导出的 CSV 含**真实业务值**（证据 summary 里有 min/max、供应商名等）
⇒ 默认写到仓库外的临时目录（脚本默认 `/tmp/...`），**不要把试点导出物提交进仓库**。
仓库里只保留**模板**（`docs/update/1118/M7/pilot/`）。

## CSV 口径

- `utf-8-sig`（Excel 直接双击不乱码）；
- 一行一候选 / 一行一问题；判定列留空（`businessVerdict` / `businessNote`）由业务填；
- 证据摘要截断到 400 字符（够定位，不把整行事实抄进去）。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from typing import Any

CANDIDATE_FIELDS = (
    "candidateId",
    "candidateType",
    "table",
    "column",
    "targetCode",
    "confidence",
    "evidenceCount",
    "evidenceSummary",
    "status",
    "businessVerdict",
    "businessNote",
)
FINDING_FIELDS = (
    "findingId",
    "findingType",
    "table",
    "column",
    "severity",
    "status",
    "detail",
    "evidenceCount",
    "businessVerdict",
    "businessNote",
)

_MAX_EVIDENCE_CHARS = 400


def candidateRows(candidates: Sequence[Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for candidate in candidates:
        rows.append(
            {
                "candidateId": str(candidate.id),
                "candidateType": str(candidate.candidate_type),
                "table": str(candidate.table_name or ""),
                "column": str(candidate.column_name or ""),
                "targetCode": str(candidate.target_code or ""),
                "confidence": (
                    "" if candidate.confidence is None else str(candidate.confidence)
                ),
                "evidenceCount": str(len(candidate.evidence_refs or [])),
                "evidenceSummary": _evidenceSummary(candidate.evidence_refs),
                "status": str(candidate.status),
                "businessVerdict": "",
                "businessNote": "",
            }
        )
    return rows


def findingRows(findings: Sequence[Any]) -> list[dict[str, str]]:
    return [
        {
            "findingId": str(finding.id),
            "findingType": str(finding.finding_type),
            "table": str(finding.table_name or ""),
            "column": str(finding.column_name or ""),
            "severity": str(finding.severity),
            "status": str(finding.status),
            "detail": str(finding.detail or ""),
            "evidenceCount": str(len(finding.evidence_refs or [])),
            "businessVerdict": "",
            "businessNote": "",
        }
        for finding in findings
    ]


def toCsv(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> str:
    """行 → CSV 文本（调用方用 `utf-8-sig` 写盘）。"""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fields), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(row))
    return buffer.getvalue()


def buildSummaryMarkdown(
    *,
    datasetVersion: str,
    runId: int,
    sourceId: int,
    snapshotId: int | None,
    metrics: Mapping[str, Any],
    verdict: str,
    generatedAt: str,
    tableNames: Sequence[str],
) -> str:
    """给业务签收的一页摘要（不含真实值，只含计数与结论）。"""
    byType = metrics.get("byType") or {}
    typeLines = "\n".join(
        f"| `{name}` | {count} |" for name, count in sorted(byType.items())
    ) or "| （无） | 0 |"
    tables = "、".join(tableNames)
    return f"""# 采购域试点签收摘要（G5）

- 生成时间：{generatedAt}
- 数据集版本：`{datasetVersion}`
- 发现源：{sourceId}｜快照：{snapshotId}｜运行：{runId}
- 覆盖表：{tables}

## 评测结论

- **verdict**：`{verdict}`
- 候选数：{metrics.get("candidateCount", 0)}
- 问题数：{metrics.get("findingCount", 0)}
- 证据完整率：{metrics.get("evidenceCompleteRate", 0)}

| 候选类型 | 条数 |
|---|---|
{typeLines}

## 签收步骤（业务）

1. 打开同目录的 `candidates.csv`：逐条填 `businessVerdict`（采纳 / 驳回 / 待证）与 `businessNote`；
2. 打开 `findings.csv`：同样逐条判定；
3. **误合并复核**：本流程**不做自动合并**，候选只有被人工采纳并进入提案后才可能生效
   ⇒ 误合并率结构性为 0；如发现「候选把两个不同实体当成一个」，请在 `businessNote` 注明；
4. 统计候选**准确率 / 召回**：需要标注集（本 CSV 的 `businessVerdict` 即标注），
   签收后由管理员用 `recordEvaluation` 上报：
   `{{"candidateAccuracy": …, "candidateRecall": …, "manualMinutes": …}}`。

> ⚠️ CSV 含真实业务值，默认生成在仓库外（`/tmp/...`）；**不要提交进仓库**。
"""


def _evidenceSummary(evidence: Any) -> str:
    if not isinstance(evidence, list):
        return ""
    parts = []
    for ref in evidence:
        if not isinstance(ref, Mapping):
            continue
        refName = str(ref.get("ref") or "")
        summary = str(ref.get("summary") or "")
        parts.append(f"{refName}:{summary}" if summary else refName)
    text = " | ".join(parts)
    return text[:_MAX_EVIDENCE_CHARS]
