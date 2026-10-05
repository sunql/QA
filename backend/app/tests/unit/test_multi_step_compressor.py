import pytest

from app.services.multi_step_compressor import (
    COMPRESS_THRESHOLD,
    MAX_DISTINCT_VALUES,
    TOP_N_EXTREMES,
    classifyColumn,
    compressStepData,
    estimatePromptTokens,
    shouldCompress,
)


def testClassifyColumnBySuffixAndType():
    assert classifyColumn("stat_month", ["2025-01", "2025-02"]) == "time"
    assert classifyColumn("日期", ["2025-01-01"]) == "time"
    assert classifyColumn("amount", [1, 2, 3]) == "numeric"
    assert classifyColumn("qty", [1.5, None, 2.5]) == "numeric"
    assert classifyColumn("supplier_name", ["A", "B"]) == "category"
    # 数字看起来像字符串时按类别处理
    assert classifyColumn("code", ["001", "002"]) == "category"


def testCompressKeepsRowsUnderLimitAndAggregatesNumerics():
    rows = [{"stat_month": f"2025-{m:02d}", "amount": m * 10} for m in range(1, 13)]

    result = compressStepData(rows, maxRows=5)

    assert len(result["rows"]) == 5
    assert result["meta"]["original_rows"] == 12
    assert result["meta"]["compressed_rows"] == 5
    amountSummary = result["columns"]["amount"]
    assert amountSummary["max"] == 120
    assert amountSummary["min"] == 10
    assert amountSummary["sum"] == 780
    assert len(amountSummary["top"]) == TOP_N_EXTREMES
    assert amountSummary["top"][0]["amount"] == 120


def testCompressKeepsAllDistinctTimeValues():
    rows = [{"stat_month": f"2025-{m:02d}", "amount": m} for m in range(1, 13)]
    result = compressStepData(rows, maxRows=3)
    assert result["columns"]["stat_month"]["distinct"] == [f"2025-{m:02d}" for m in range(1, 13)]


def testCompressCapsCategoryDistinctValues():
    rows = [{"name": f"n{i}", "amount": i} for i in range(200)]
    result = compressStepData(rows, maxRows=1)
    assert len(result["columns"]["name"]["distinct"]) == MAX_DISTINCT_VALUES


def testCompressEmptyRowsIsSafe():
    result = compressStepData([], maxRows=10)
    assert result["rows"] == []
    assert result["meta"]["original_rows"] == 0
    assert result["meta"]["ratio"] == 1.0


def testCompressIgnoresBooleansAsNumeric():
    rows = [{"is_active": True, "amount": 1}, {"is_active": False, "amount": 2}]
    result = compressStepData(rows, maxRows=2)
    assert "max" not in result["columns"]["is_active"]


def testEstimatePromptTokensCountsCjkAndLatin():
    assert estimatePromptTokens("") == 0
    # 5 个汉字 ≈ 5 token；8 个 latin 字符 ≈ 2 token
    assert estimatePromptTokens("供应商名称") == 5
    assert estimatePromptTokens("abcdefgh") == 2


@pytest.mark.parametrize(
    "estimated, maxInput, expected",
    [
        (700, 1000, False),   # 恰好 70%，不压
        (701, 1000, True),
        (100, 1000, False),
        (950, 1000, True),
    ],
)
def testShouldCompress(estimated, maxInput, expected):
    assert shouldCompress(estimated, maxInput) is expected
    assert COMPRESS_THRESHOLD == 0.7
