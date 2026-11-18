"""A6：KPI 归因探针的**纯函数**单测 —— 引用列抽取 / 本体覆盖判定。

为什么钉死
----------
归因表要回答「技术层声明的列在本体里有没有」。若抽取器静默返回空集，
「覆盖检查」就变成空集假绿（本仓反复踩过的坑）⇒ 这里必须证明：

1. 抽取器认得别名限定（``R.RECEIPT_DATE`` → ``RECEIPT_DATE``）、函数（``TRUNC`` 不是列）、
   字符串字面量（``'FAIL'`` 不是列）；
2. **多处来源都参与**：measure.expr / measure.filter / formula / filter_expression /
   grain_keys / time_column —— 少一处就会漏报「本体缺列」；
3. SQL 不可解析 ⇒ 显式 ``None``（不可判），不能当成「没有引用列」；
4. 覆盖判定大小写不敏感，且**反向守卫**：真缺列时必须报出来。
"""

from __future__ import annotations

import pytest

from scripts import kpi_metric_attribution_probe as probe


def _metric(**overrides):
    base = {
        "numerator_measure": None,
        "denominator_measure": None,
        "formula": None,
        "filter_expression": None,
        "grain_keys": None,
        "time_column": None,
    }
    base.update(overrides)
    return probe.MetricRefs(**base)


class TestReferencedTokens:
    def test_别名限定被剥离且函数名不算列(self):
        metric = _metric(
            numerator_measure={"agg": "AVG", "expr": "TRUNC(R.RECEIPT_DATE) - TRUNC(O.PROMISED_RECEIPT_DATE)"},
        )
        assert probe.referenceTokens(metric) == {"RECEIPT_DATE", "PROMISED_RECEIPT_DATE"}

    def test_字符串字面量不算列(self):
        # 注意：``I.RESULT`` 是真列（谓词里的标识符），``'FAIL'`` 才是字面量 ——
        # 只剔字面量、不剔谓词列，否则「本体缺 RESULT 列」会被漏报。
        metric = _metric(numerator_measure={"agg": "SUM", "expr": "CASE WHEN I.RESULT = 'FAIL' THEN I.QTY END"})
        tokens = probe.referenceTokens(metric)
        assert tokens == {"RESULT", "QTY"}
        assert "FAIL" not in tokens

    def test_measure_filter_formula_grain_time_全部参与(self):
        metric = _metric(
            numerator_measure={"agg": "COUNT", "expr": "VALUE_DATE", "filter": "VALUE_DATE <= DUE_DATE"},
            formula="SELECT AMOUNT_EXCL_TAX FROM T WHERE PAYMENT_TYPE = 1",
            filter_expression="SUPPLIER_CODE IS NOT NULL",
            grain_keys=["SUPPLIER_CODE", "FACILITY_CODE"],
            time_column="INVOICE_DATE",
        )
        assert probe.referenceTokens(metric) == {
            "VALUE_DATE",
            "DUE_DATE",
            "AMOUNT_EXCL_TAX",
            "PAYMENT_TYPE",
            "SUPPLIER_CODE",
            "FACILITY_CODE",
            "INVOICE_DATE",
        }

    def test_什么都没有时返回空集(self):
        assert probe.referenceTokens(_metric()) == set()

    def test_SQL不可解析时返回None而不是空集(self):
        metric = _metric(formula="SELECT FROM WHERE (((")
        assert probe.referenceTokens(metric) is None

    def test_中文口径说明不是SQL_返回None(self):
        """NONE 类指标的 formula 是中文说明，sqlglot 会把它解析成 ``Div`` 表达式 ⇒ 必须判不可判。"""
        metric = _metric(formula="口径声明：不合格数量 / 检验总数量（IQC）")
        assert probe.referenceTokens(metric) is None

    def test_measure为JSON_null时不崩(self):
        metric = _metric(numerator_measure=None, denominator_measure={"agg": "COUNT"})
        assert probe.referenceTokens(metric) == set()


class TestTableNames:
    """「没有表」和「判断不了」必须分开，否则覆盖检查会空集假绿。"""

    def test_可解析SQL返回表名(self):
        metric = _metric(formula="SELECT A.X FROM DWD_A A JOIN DWD_B B ON A.K = B.K")
        assert probe._tableNames(metric) == {"DWD_A", "DWD_B"}

    def test_中文说明文字不可解析时返回None(self):
        metric = _metric(formula="口径声明：不合格数量 / 检验总数量（IQC）")
        assert probe._tableNames(metric) is None

    def test_无SQL时返回None(self):
        assert probe._tableNames(_metric()) is None


class TestMissingOntologyColumns:
    def test_大小写不敏感(self):
        assert probe.missingOntologyColumns({"receipt_date"}, {"RECEIPT_DATE"}) == ()

    def test_真缺列必须报出来_反向守卫(self):
        missing = probe.missingOntologyColumns(
            {"RECEIPT_DATE", "VALUE_DATE"}, {"RECEIPT_DATE"}
        )
        assert missing == ("VALUE_DATE",)

    def test_不可判时不给空集结论(self):
        assert probe.missingOntologyColumns(None, {"RECEIPT_DATE"}) is None


class TestClassifyCategory:
    """四分类是 A6 的验收口径（治理层需改 / 技术层需改 / 仅列名不同 / 真口径分歧）。"""

    def test_四类都是合法值(self):
        assert probe.CATEGORIES == (
            "治理层需改",
            "技术层需改",
            "仅列名不同",
            "真口径分歧",
        )

    def test_非四类的分类被拒绝(self):
        with pytest.raises(ValueError):
            probe.assertCategory("随便写")

    def test_四类均被接受(self):
        for category in probe.CATEGORIES:
            assert probe.assertCategory(category) == category
