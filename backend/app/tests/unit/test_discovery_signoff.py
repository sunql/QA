"""G5 签收物料导出（纯函数）。

1. 行映射：候选/问题 → 固定列，判定列留空（业务填）；
2. CSV：带逗号/引号/换行的值必须正确转义（否则 Excel 列错位）；
3. 摘要：只含计数与结论，**不回显真实业务值**。
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any

from app.services.discovery_signoff import (
    CANDIDATE_FIELDS,
    FINDING_FIELDS,
    buildSummaryMarkdown,
    candidateRows,
    findingRows,
    toCsv,
)


class _Candidate:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def _candidate(**overrides: Any) -> _Candidate:
    base = {
        "id": 7,
        "candidate_type": "OBJECT",
        "table_name": "DIM_SUPPLIER",
        "column_name": None,
        "target_code": "SUPPLIER",
        "confidence": Decimal("0.9"),
        "evidence_refs": [
            {
                "ref": "DIM_SUPPLIER.BPSNUM_0",
                "summary": "nullCount=0, distinctCount=3500",
            }
        ],
        "status": "CANDIDATE",
    }
    base.update(overrides)
    return _Candidate(**base)


def test_candidate_rows_fill_verdict_columns_blank() -> None:
    rows = candidateRows([_candidate()])

    assert rows[0]["candidateId"] == "7"
    assert rows[0]["evidenceCount"] == "1"
    assert "distinctCount=3500" in rows[0]["evidenceSummary"]
    assert rows[0]["businessVerdict"] == ""
    assert rows[0]["businessNote"] == ""


def test_csv_escapes_commas_quotes_and_newlines() -> None:
    rows = candidateRows([_candidate(target_code='A,"B"\nC')])

    text = toCsv(rows, CANDIDATE_FIELDS)
    parsed = list(csv.DictReader(io.StringIO(text)))

    assert parsed[0]["targetCode"] == 'A,"B"\nC'


def test_finding_rows_keep_detail_and_blank_verdict() -> None:
    finding = _Candidate(
        id=3,
        finding_type="MISSING",
        table_name="DIM_SUPPLIER",
        column_name="BPSNUM_0",
        severity="ERROR",
        status="OPEN",
        detail="剖析未声明主键",
        evidence_refs=[{"ref": "DIM_SUPPLIER.BPSNUM_0", "summary": "rowCount=3500"}],
    )

    rows = findingRows([finding])

    assert rows[0]["findingType"] == "MISSING"
    assert rows[0]["detail"] == "剖析未声明主键"
    assert rows[0]["businessVerdict"] == ""
    assert toCsv(rows, FINDING_FIELDS).splitlines()[0].startswith("findingId,")


def test_summary_reports_counts_without_real_values() -> None:
    markdown = buildSummaryMarkdown(
        datasetVersion="pilot-2026-11",
        runId=4,
        sourceId=1,
        snapshotId=1,
        metrics={
            "candidateCount": 8,
            "findingCount": 5,
            "evidenceCompleteRate": 1.0,
            "byType": {"OBJECT": 1, "PROPERTY": 5},
        },
        verdict="PASS",
        generatedAt="2026-11-22T10:00:00+00:00",
        tableNames=["DIM_SUPPLIER", "DIM_IMATERIAL"],
    )

    assert "PASS" in markdown
    assert "| `OBJECT` | 1 |" in markdown
    assert "DIM_SUPPLIER" in markdown
    assert "不要提交进仓库" in markdown
    # 摘要不抄真实值（值为空/计数，不含供应商名等）
    assert "山东" not in markdown
