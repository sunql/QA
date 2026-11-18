"""A5 阳性对照自检：每条退出码语义都必须被一个「期望值先写死」的用例打中。

⚠️ 这不是「跑一遍看看」—— 它是评估工具唯一的可 CI 化证据。任何一条比较器
退化（别名坍缩、丢 FROM、解括号、忽略列名、L2 只比第 1 遍……）都必须让这里变红。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scripts.answer_parity_compare import (
    EXIT_ANSWER_MISMATCH,
    EXIT_INCONCLUSIVE,
    EXIT_OK,
    EXIT_PRECONDITION,
    evaluateQuestion,
)

# ---------------------------------------------------------------------------
# 夹具：D17 实测漂移原形（收货明细 × 供应商）
# ---------------------------------------------------------------------------

_OFF_SQL = """
SELECT s.BPSNUM_0 AS SUPPLIER_CODE, COUNT(d.RCV_LINE_NO) AS CNT
  FROM DWD_GOODS_RECEIPT_DTL d
  JOIN DIM_SUPPLIER s ON d.SUPPLIER_CODE = s.BPSNUM_0
 WHERE d.INTER_COM_CODE = '1' AND s.ENAFLG_0 = 2 AND s.BPSNUM_0 NOT IN ('T01', 'D01')
 GROUP BY s.BPSNUM_0
"""

_ON_SQL_DROP_PREDICATE = _OFF_SQL.replace(
    " AND s.ENAFLG_0 = 2", ""
)

_ON_SQL_DROP_JOIN = """
SELECT d.SUPPLIER_CODE AS SUPPLIER_CODE, COUNT(d.RCV_LINE_NO) AS CNT
  FROM DWD_GOODS_RECEIPT_DTL d
 WHERE d.INTER_COM_CODE = '1'
 GROUP BY d.SUPPLIER_CODE
"""

_ON_SQL_PROJECTION_ONLY = """
SELECT COUNT(d.RCV_LINE_NO) AS RECEIPT_COUNT, s.BPSNUM_0 AS SUPPLIER_CODE
  FROM DWD_GOODS_RECEIPT_DTL d
  JOIN DIM_SUPPLIER s ON d.SUPPLIER_CODE = s.BPSNUM_0
 WHERE d.INTER_COM_CODE = '1' AND s.ENAFLG_0 = 2 AND s.BPSNUM_0 NOT IN ('T01', 'D01')
 GROUP BY s.BPSNUM_0
"""

_ON_SQL_RENAMED_ALIASES = """
SELECT f.BPSNUM_0 AS SUPPLIER_CODE, COUNT(r.RCV_LINE_NO) AS CNT
  FROM DWD_GOODS_RECEIPT_DTL r
  JOIN DIM_SUPPLIER f ON r.SUPPLIER_CODE = f.BPSNUM_0
 WHERE r.INTER_COM_CODE = '1' AND f.ENAFLG_0 = 2 AND f.BPSNUM_0 NOT IN ('T01', 'D01')
 GROUP BY f.BPSNUM_0
"""

_ON_SQL_LEFT_JOIN = _OFF_SQL.replace("JOIN DIM_SUPPLIER", "LEFT JOIN DIM_SUPPLIER")
# 只有这一对是同一种口径（OUTER 是冗余词）；和 INNER 比是**真差异**，别混用。
_ON_SQL_LEFT_OUTER_JOIN = _ON_SQL_LEFT_JOIN.replace("LEFT JOIN", "LEFT OUTER JOIN")
_ON_SQL_GROUP_BY_CHANGED = _OFF_SQL.replace(
    "GROUP BY s.BPSNUM_0", "GROUP BY d.SUPPLIER_CODE"
)
_JITTER_SQL = _OFF_SQL.replace("COUNT(d.RCV_LINE_NO)", "COUNT(*)")

# 换基表 / 换度量：集合差异必须被看见（否则「空集 ⇒ 通过」）
_ON_SQL_OTHER_TABLE = _OFF_SQL.replace(
    "FROM DWD_GOODS_RECEIPT_DTL d", "FROM DWD_ARRIVAL_ORDER_DTL d"
)
_ON_SQL_OTHER_MEASURE = _OFF_SQL.replace(
    "COUNT(d.RCV_LINE_NO)", "SUM(d.RCV_QTY_PUU)"
)

# 外连接里 ON ↔ WHERE 搬家：结果集不同，绝不能判等价
_LEFT_ON_SQL = """
SELECT a.ID, b.V
  FROM A a LEFT JOIN B b ON a.ID = b.ID
 WHERE b.X = 1
"""
_LEFT_WHERE_SQL = """
SELECT a.ID, b.V
  FROM A a LEFT JOIN B b ON a.ID = b.ID AND b.X = 1
"""

# 自连接：两个别名是两个角色，别名坍缩会漏判
_SELF_JOIN_1 = "SELECT s1.CODE FROM DIM_SUPPLIER s1 JOIN DIM_SUPPLIER s2 ON s1.PARENT_0 = s2.CODE WHERE s1.ENAFLG_0 = 2"
_SELF_JOIN_2 = "SELECT s1.CODE FROM DIM_SUPPLIER s1 JOIN DIM_SUPPLIER s2 ON s1.PARENT_0 = s2.CODE WHERE s2.ENAFLG_0 = 2"

# 括号改变优先级：`(A+B)*C` 与 `A+B*C` 不是同一个口径
_PAREN_SQL = "SELECT 1 FROM T WHERE (A + B) * C = 10"
_NO_PAREN_SQL = "SELECT 1 FROM T WHERE A + B * C = 10"

# 同名列、不同表：删掉表前缀会碰巧相等
_SAME_COLUMN_LEFT = "SELECT 1 FROM T1 a JOIN T2 b ON a.X = b.Y WHERE a.NAME = 'x'"
_SAME_COLUMN_RIGHT = "SELECT 1 FROM T1 a JOIN T2 b ON a.X = b.Y WHERE b.NAME = 'x'"


@dataclass(frozen=True)
class SelfTestCase:
    name: str
    expectedExit: int
    actualExit: int
    detail: str

    @property
    def passed(self) -> bool:
        return self.expectedExit == self.actualExit


def _selfCase(
    name: str,
    expectedExit: int,
    off1: str | None,
    off2: str | None,
    on1: str | None,
    on2: str | None,
    *,
    requiredLayers: tuple[str, ...] = ("L1", "L3"),
    rows: tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None] | None = None,
    rows2: tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None] | None = None,
    answers: tuple[str | None, str | None] | None = None,
    answers2: tuple[str | None, str | None] | None = None,
) -> SelfTestCase:
    evaluation = evaluateQuestion(
        off1,
        off2,
        on1,
        on2,
        requiredLayers=requiredLayers,
        offRows1=rows[0] if rows else None,
        onRows1=rows[1] if rows else None,
        offRows2=rows2[0] if rows2 else None,
        onRows2=rows2[1] if rows2 else None,
        offAnswer1=answers[0] if answers else None,
        onAnswer1=answers[1] if answers else None,
        offAnswer2=answers2[0] if answers2 else None,
        onAnswer2=answers2[1] if answers2 else None,
    )
    return SelfTestCase(
        name=name,
        expectedExit=expectedExit,
        actualExit=evaluation.exitCode,
        detail=evaluation.reason,
    )


def _same(sql: str) -> tuple[str, str, str, str]:
    return sql, sql, sql, sql


def _sqlNormalizationCases() -> tuple[SelfTestCase, ...]:
    """「写法不同但同一种口径」⇒ 0：投影列序 / 别名 / 单值 IN / OUTER 冗余词。"""
    return (
        _selfCase("逐字相同 ⇒ 0", EXIT_OK, *_same(_OFF_SQL)),
        _selfCase(
            "仅投影列序/别名不同 ⇒ 0（L1 仅定位）",
            EXIT_OK,
            _OFF_SQL,
            _OFF_SQL,
            _ON_SQL_PROJECTION_ONLY,
            _ON_SQL_PROJECTION_ONLY,
        ),
        _selfCase(
            "别名重命名 ⇒ 0（解析到表身份）",
            EXIT_OK,
            _OFF_SQL,
            _OFF_SQL,
            _ON_SQL_RENAMED_ALIASES,
            _ON_SQL_RENAMED_ALIASES,
        ),
        _selfCase("单值 NOT IN 与 <> ⇒ 0", EXIT_OK,
                  "SELECT 1 FROM T WHERE X NOT IN ('A')",
                  "SELECT 1 FROM T WHERE X NOT IN ('A')",
                  "SELECT 1 FROM T WHERE X <> 'A'",
                  "SELECT 1 FROM T WHERE X <> 'A'"),
        _selfCase("LEFT JOIN 与 LEFT OUTER JOIN ⇒ 0（OUTER 是冗余词）", EXIT_OK,
                  _ON_SQL_LEFT_JOIN, _ON_SQL_LEFT_JOIN,
                  _ON_SQL_LEFT_OUTER_JOIN, _ON_SQL_LEFT_OUTER_JOIN),
    )


def _coverageCases() -> tuple[SelfTestCase, ...]:
    """「查的东西不一样」⇒ 3：这是 L3 硬红线的全部原形。"""
    return (
        _selfCase(
            "丢 WHERE 谓词 ⇒ 3（L3 硬红线）",
            EXIT_ANSWER_MISMATCH,
            _OFF_SQL,
            _OFF_SQL,
            _ON_SQL_DROP_PREDICATE,
            _ON_SQL_DROP_PREDICATE,
        ),
        _selfCase("丢 JOIN ⇒ 3", EXIT_ANSWER_MISMATCH, _OFF_SQL, _OFF_SQL,
                  _ON_SQL_DROP_JOIN, _ON_SQL_DROP_JOIN),
        _selfCase("JOIN 类型 INNER→LEFT ⇒ 3", EXIT_ANSWER_MISMATCH, _OFF_SQL, _OFF_SQL,
                  _ON_SQL_LEFT_JOIN, _ON_SQL_LEFT_JOIN),
        _selfCase("GROUP BY 键变化 ⇒ 3", EXIT_ANSWER_MISMATCH, _OFF_SQL, _OFF_SQL,
                  _ON_SQL_GROUP_BY_CHANGED, _ON_SQL_GROUP_BY_CHANGED),
        _selfCase("换 FROM 基表 ⇒ 3（不许空集通过）", EXIT_ANSWER_MISMATCH, _OFF_SQL, _OFF_SQL,
                  _ON_SQL_OTHER_TABLE, _ON_SQL_OTHER_TABLE),
        _selfCase("换度量表达式 ⇒ 3", EXIT_ANSWER_MISMATCH, _OFF_SQL, _OFF_SQL,
                  _ON_SQL_OTHER_MEASURE, _ON_SQL_OTHER_MEASURE),
        _selfCase("外连接 ON→WHERE 搬家 ⇒ 3", EXIT_ANSWER_MISMATCH, _LEFT_ON_SQL, _LEFT_ON_SQL,
                  _LEFT_WHERE_SQL, _LEFT_WHERE_SQL),
        _selfCase("自连接换角色 ⇒ 3（别名不许坍缩）", EXIT_ANSWER_MISMATCH,
                  _SELF_JOIN_1, _SELF_JOIN_1, _SELF_JOIN_2, _SELF_JOIN_2),
        _selfCase("括号改变优先级 ⇒ 3", EXIT_ANSWER_MISMATCH, _PAREN_SQL, _PAREN_SQL,
                  _NO_PAREN_SQL, _NO_PAREN_SQL),
        _selfCase("同名列不同表 ⇒ 3（表前缀不能丢）", EXIT_ANSWER_MISMATCH,
                  _SAME_COLUMN_LEFT, _SAME_COLUMN_LEFT, _SAME_COLUMN_RIGHT, _SAME_COLUMN_RIGHT),
    )


def _inconclusiveCases() -> tuple[SelfTestCase, ...]:
    """样本层面的「不可判定」：抖动、不可解析、某态没有 SQL。"""
    return (
        _selfCase("同态抖动 J>0 ⇒ 6", EXIT_INCONCLUSIVE, _OFF_SQL, _JITTER_SQL, _OFF_SQL, _OFF_SQL),
        _selfCase("SQL 不可解析 ⇒ 6", EXIT_INCONCLUSIVE,
                  "not a sql at all (", "not a sql at all (", _OFF_SQL, _OFF_SQL),
        _selfCase("某态产不出 SQL ⇒ 6", EXIT_INCONCLUSIVE, _OFF_SQL, _OFF_SQL, None, None),
    )


def _pagingAndSchemaCases() -> tuple[SelfTestCase, ...]:
    """分页 / 排序 / schema 限定：曾因只收 `exp.Limit` 而漏判。"""
    return (
        _selfCase(
            "FETCH 行数不同 ⇒ 3",
            EXIT_ANSWER_MISMATCH,
            "SELECT X FROM T ORDER BY X FETCH FIRST 10 ROWS ONLY",
            "SELECT X FROM T ORDER BY X FETCH FIRST 10 ROWS ONLY",
            "SELECT X FROM T ORDER BY X FETCH FIRST 20 ROWS ONLY",
            "SELECT X FROM T ORDER BY X FETCH FIRST 20 ROWS ONLY",
        ),
        _selfCase(
            "OFFSET 变化 ⇒ 3",
            EXIT_ANSWER_MISMATCH,
            "SELECT X FROM T ORDER BY X OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY",
            "SELECT X FROM T ORDER BY X OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY",
            "SELECT X FROM T ORDER BY X OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY",
            "SELECT X FROM T ORDER BY X OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY",
        ),
        _selfCase(
            "分页时 ORDER BY 键变化 ⇒ 3（决定取到哪些行）",
            EXIT_ANSWER_MISMATCH,
            "SELECT X FROM T ORDER BY X FETCH FIRST 10 ROWS ONLY",
            "SELECT X FROM T ORDER BY X FETCH FIRST 10 ROWS ONLY",
            "SELECT X FROM T ORDER BY Y FETCH FIRST 10 ROWS ONLY",
            "SELECT X FROM T ORDER BY Y FETCH FIRST 10 ROWS ONLY",
        ),
        _selfCase(
            "无分页时 ORDER BY 键变化 ⇒ 0（行序不影响多重集）",
            EXIT_OK,
            "SELECT X FROM T ORDER BY X",
            "SELECT X FROM T ORDER BY X",
            "SELECT X FROM T ORDER BY Y",
            "SELECT X FROM T ORDER BY Y",
        ),
        _selfCase(
            "schema 限定不同的同名表 ⇒ 3（ODS.T ≠ DWD.T）",
            EXIT_ANSWER_MISMATCH,
            "SELECT X FROM ODS.T",
            "SELECT X FROM ODS.T",
            "SELECT X FROM DWD.T",
            "SELECT X FROM DWD.T",
        ),
    )


def _resultSetCases() -> tuple[SelfTestCase, ...]:
    """L2 结果集：多重集 / 列名参与 / 编码字符串不许被 float 抹平。"""
    return (
        _selfCase(
            "L2 结果集有差异 ⇒ 3",
            EXIT_ANSWER_MISMATCH,
            *_same(_OFF_SQL),
            requiredLayers=("L1", "L2", "L3"),
            rows=([{"SUPPLIER_CODE": "S1", "CNT": 10}], [{"SUPPLIER_CODE": "S1", "CNT": 11}]),
            rows2=([{"SUPPLIER_CODE": "S1", "CNT": 10}], [{"SUPPLIER_CODE": "S1", "CNT": 11}]),
        ),
        _selfCase(
            "L2 行序不同但多重集相同 ⇒ 0",
            EXIT_OK,
            *_same(_OFF_SQL),
            requiredLayers=("L1", "L2", "L3"),
            rows=(
                [{"SUPPLIER_CODE": "S1", "CNT": 10}, {"SUPPLIER_CODE": "S2", "CNT": 5}],
                [{"SUPPLIER_CODE": "S2", "CNT": 5}, {"SUPPLIER_CODE": "S1", "CNT": 10}],
            ),
            rows2=(
                [{"SUPPLIER_CODE": "S1", "CNT": 10}, {"SUPPLIER_CODE": "S2", "CNT": 5}],
                [{"SUPPLIER_CODE": "S2", "CNT": 5}, {"SUPPLIER_CODE": "S1", "CNT": 10}],
            ),
        ),
        _selfCase(
            "L2 列名不同但值相同 ⇒ 3（列名参与比较）",
            EXIT_ANSWER_MISMATCH,
            *_same(_OFF_SQL),
            requiredLayers=("L1", "L2", "L3"),
            rows=([{"A": 1, "B": 2}], [{"B": 1, "C": 2}]),
            rows2=([{"A": 1, "B": 2}], [{"B": 1, "C": 2}]),
        ),
        _selfCase(
            "L2 字符串编码前导零不同 ⇒ 3（不许 float 抹平）",
            EXIT_ANSWER_MISMATCH,
            *_same(_OFF_SQL),
            requiredLayers=("L1", "L2", "L3"),
            rows=([{"CODE": "0001"}], [{"CODE": "1"}]),
            rows2=([{"CODE": "0001"}], [{"CODE": "1"}]),
        ),
        _selfCase(
            "要求 L2 但没给结果集 ⇒ 6",
            EXIT_INCONCLUSIVE,
            *_same(_OFF_SQL),
            requiredLayers=("L1", "L2", "L3"),
        ),
    )


def _answerCases() -> tuple[SelfTestCase, ...]:
    """L4 数值 / L5 答案：容差、抖动、以及「两侧都没数字 ⇒ 判不了」。"""
    return (
        _selfCase(
            "L4 金额差 0.01 以内 ⇒ 0",
            EXIT_OK,
            *_same(_OFF_SQL),
            requiredLayers=("L1", "L3", "L4"),
            answers=("合计 1,234.56 元", "合计 1234.57 元"),
            answers2=("合计 1,234.56 元", "合计 1234.57 元"),
        ),
        _selfCase(
            "L4 同态答案抖动 ⇒ 6（不得归因给开关）",
            EXIT_INCONCLUSIVE,
            *_same(_OFF_SQL),
            requiredLayers=("L1", "L3", "L4"),
            answers=("合计 100 元", "合计 101 元"),
            answers2=("合计 100 元", "合计 100 元"),
        ),
        _selfCase(
            "L4 两侧都没有数值 ⇒ 6（空集不许通过）",
            EXIT_INCONCLUSIVE,
            *_same(_OFF_SQL),
            requiredLayers=("L1", "L3", "L4"),
            answers=("供应商A是唯一可供应方", "供应商B是唯一可供应方"),
            answers2=("供应商A是唯一可供应方", "供应商B是唯一可供应方"),
        ),
        _selfCase(
            "L5 一侧说未接入 ⇒ 3",
            EXIT_ANSWER_MISMATCH,
            *_same(_OFF_SQL),
            requiredLayers=("L1", "L3", "L5"),
            answers=("合计 1,234.56 元", "该数据域未接入"),
            answers2=("合计 1,234.56 元", "该数据域未接入"),
        ),
        _selfCase(
            "L5 两侧都没有数值 ⇒ 6（纯文本差异判不了）",
            EXIT_INCONCLUSIVE,
            *_same(_OFF_SQL),
            requiredLayers=("L1", "L3", "L5"),
            answers=("供应商A是唯一可供应方", "供应商B是唯一可供应方"),
            answers2=("供应商A是唯一可供应方", "供应商B是唯一可供应方"),
        ),
    )


def runSelfTest() -> tuple[SelfTestCase, ...]:
    return (
        *_sqlNormalizationCases(),
        *_coverageCases(),
        *_inconclusiveCases(),
        *_pagingAndSchemaCases(),
        *_resultSetCases(),
        *_answerCases(),
    )


def selfTestExitCode(results: tuple[SelfTestCase, ...] | None = None) -> int:
    """自检全过 ⇒ 0；有任一用例不符 ⇒ 4（脚本缺陷 = 前置条件不满足）。"""
    cases = results if results is not None else runSelfTest()
    return EXIT_OK if cases and all(case.passed for case in cases) else EXIT_PRECONDITION


def formatSelfTest(results: tuple[SelfTestCase, ...] | None = None) -> str:
    cases = results if results is not None else runSelfTest()
    lines = ["阳性对照自检（期望值先写死，比较器失效即红）："]
    for case in cases:
        mark = "✅" if case.passed else "❌"
        lines.append(
            f"{mark} {case.name}：期望 {case.expectedExit} / 实得 {case.actualExit}"
            + (f"（{case.detail}）" if not case.passed else "")
        )
    passed = sum(1 for case in cases if case.passed)
    lines.append(f"== {passed}/{len(cases)} 通过 ==")
    return "\n".join(lines)
