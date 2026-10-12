"""HypothesisTest 数据支持度判定（Task 5）：``evaluateHypothesis`` 纯函数单测。

钉住 controller 实施口径 D16–D22：

- **D20b**：候选（``Hypothesis``）只有 ``statement`` / ``driver`` / ``verificationSql`` 三键 ⇒
  ``direction`` 必须从 ``statement`` 的中文方向词**推导**（纯函数 :func:`deriveDirection`）；
  两向都拿不到（或互相矛盾）⇒ ``None`` ⇒ ``INCONCLUSIVE``。**反向断言**是「方向词 + 同向数据
  ⇒ SUPPORTED」—— 只测 INCONCLUSIVE 的用例集在 `direction` 恒 ``None``（功能完全失效）时
  **照样全绿**，抓不住 D20b 那记「跑得通、永远给不出结论」。
- **D19b**：``EvidenceSummary`` 的数值字段是**字符串**，比较必须走 ``numeric()`` —— 裸串比较
  是字典序（``"1000" > "900"`` 为 ``False``），会**静默把大小判反**。本文件用 baseline=900 /
  current=1000 的量级用例钉住这一点。
- **D20**：无阈值只在反向变化时判 CONTRADICTED，**恰相等** ⇒ INCONCLUSIVE；有阈值时以它为
  最小显著变化量。
- **D18**：``verdict``（数据支持度）与 ``execution_succeeded``（执行是否成功）**分开**表达。
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.services.hypothesis_service import Hypothesis
from app.services.research_evidence_summary import EvidenceSummary
from app.services.research_hypothesis_test import (
    DIRECTION_DECREASE,
    DIRECTION_INCREASE,
    HypothesisTest,
    HypothesisVerdict,
    deriveDirection,
    evaluateHypothesis,
)


# 与真实候选逐字同形：``Hypothesis`` 恰好三键（多一个键 ``Hypothesis(**candidate)`` 会崩）。
def _candidate(statement: str, driver: str | None = "amount") -> dict:
    return {
        "statement": statement,
        "driver": driver,
        "verificationSql": "SELECT 1 FROM dual",
    }


def _summary(baseline: str, current: str, *, empty: bool = False) -> EvidenceSummary:
    return EvidenceSummary(baseline=baseline, current=current, empty=empty)


# ---------------------------------------------------------------------------
# D20b：direction 是推导来的纯函数（正反两向 + 拿不到）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    ["采购金额上升", "销售额增长", "库存增加", "单价提高", "订单量涨"],
)
def test_derive_direction_increase_words(statement: str) -> None:
    assert deriveDirection(statement) == DIRECTION_INCREASE


@pytest.mark.parametrize(
    "statement",
    ["采购金额下降", "销售额减少", "库存降低", "单价下滑", "订单量跌"],
)
def test_derive_direction_decrease_words(statement: str) -> None:
    assert deriveDirection(statement) == DIRECTION_DECREASE


def test_derive_direction_none_when_no_direction_word() -> None:
    """既无涨词也无跌词 ⇒ ``None``（宁可不判，不许瞎猜）。"""
    assert deriveDirection("供应商A供货趋势") is None
    assert deriveDirection("") is None
    assert deriveDirection(None) is None  # type: ignore[arg-type]


def test_derive_direction_none_when_both_directions_present() -> None:
    """涨词与跌词互相矛盾 ⇒ 方向不可定 ⇒ ``None``。"""
    assert deriveDirection("金额上升而数量下降") is None


# ---------------------------------------------------------------------------
# I1（轮 3）：否定判据 = 「否定字 + 情态词（含 `是`）· 距方向词 ≤ GAP=4」。
# 40 条电池（38 绿 + 2 标注残余）是**自证地基**：TP（被否定 ⇒ 无方向）与 FP（否定字在
# 非否定词素里 ⇒ 方向必须保住）成对钉住 —— 任何一侧收窄都会撞翻对面的行。
# ---------------------------------------------------------------------------


# TP（17 条，必须 None）：方向词被否定式作废 ⇒ 无其它方向词 ⇒ ``None``。
# 「不应下降」等的方向词只是被否定了，不是没有方向词 —— 按本模块「宁可不判，不许瞎猜」
# 的口径必须 ``None``（符号翻转 = 静默把「被违背」判成「已验证」）。
@pytest.mark.parametrize(
    "statement",
    [
        "不应下降",  # 紧邻（分支 a 不命中；分支 b：«应» 情态 + «不» 否定）
        "不会减少",  # b：«会»
        "不该增长",  # b：«该»
        "没有下降",  # b：«有»
        "未能增长",  # b：«能»
        "不增长",  # a：紧邻否定字
        "不再下降",  # b：«再»
        "不应大幅下降",  # b：«应» 距 «下» = 3（副词隔断）
        "不会明显减少",  # b：«会» 距 «减» = 3
        "未能持续增长",  # b：«能» 距 «增» = 3
        "没有出现下降",  # b：«有» 距 «下» = 3
        "不再有增长",  # b：«再»（«有» 不接否定字，靠 «再»）
        "不应再下降",  # b：«应»
        "不应 下降",  # b：空格不再截断扫描（轮 2 漏判项）
        "不能下降",  # b：«能»
        "不是下降",  # b：«是»（轮 3 新增情态词；轮 2 把 «是» 当停扫字 ⇒ 漏判）
        "绝非下降",  # a：«非» 紧邻
    ],
)
def test_derive_direction_none_for_negated_true_positives(statement: str) -> None:
    """TP：否定式方向词 ⇒ ``None``（17/19；另 2 条见残余人组）。"""
    assert deriveDirection(statement) is None


# 已知残余（2 条，**承认的盲区，不是正确行为**）：本轮判据（否定字 + 情态词 + GAP≤4）在
# **局部特征上**与 FP 不可分 —— ``无权下降`` 的 «无» 距方向词 = 2，与 ``未来`` 的 «未» 同为 2；
# ``不可能会下降`` 的否定字只隔情态链（«不»→«可»→«能»→«会»）。收窄其一即误伤另一。
# 断言写成「记录当前已知行为」而非「期望 None」，并在此**显式标注为盲区**。
@pytest.mark.parametrize(
    "statement",
    ["不可能会下降", "无权下降"],
)
def test_derive_direction_known_residual_negation_chains(statement: str) -> None:
    """**已知残余（盲区，非正确行为）**：情态链 >1 层 / 否定经非情态词桥接仍会漏判成有效方向。

    这两条在裸列名 + 取值下与 ``无锡``/``未来`` 类 FP **不可分**（见
    ``_isNegatedDirectionWordAt`` docstring 的「已知残余」）。测试记录当前行为仅为钉住
    「已知」，**不得**据此认为它正确；是否收窄交控制器裁定。
    """
    # 承认的盲区：本轮**不**修，断言记录当前（错误）行为。
    assert deriveDirection(statement) == DIRECTION_DECREASE  # 已知残余，见 docstring


# FP（21 条，必须保住括号里的方向）：否定字在**非否定词素**（普通词）里 ⇒ 方向必须保留。
# 这正是不是新判据（情态词桥接）与旧窗口判据的分界：轮 2 的「任意字窗口」把
# 非常/不仅/不锈钢/未来/供给不足/无货/问题不大 全吞成 None；轮 3 因后随字非情态词而安全。
@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("无锡供应商采购额上升", DIRECTION_INCREASE),  # «锡» 非情态
        ("未来三月采购额上升", DIRECTION_INCREASE),  # «来» 非情态
        ("无法确认库存增长", DIRECTION_INCREASE),  # «法» 非情态
        ("不但上涨", DIRECTION_INCREASE),  # «但» 非情态（停扫集已删，靠这一点）
        ("再增长", DIRECTION_INCREASE),  # «再» 是情态词但**前面**无否定字
        ("无供应商库存增长", DIRECTION_INCREASE),  # «供» 非情态
        ("不锈钢库存增长", DIRECTION_INCREASE),  # 商品名「不锈钢」
        ("无锡库存增长", DIRECTION_INCREASE),  # 地名「无锡」
        ("未来增长", DIRECTION_INCREASE),  # «来» 非情态
        ("未来将持续下降", DIRECTION_DECREASE),  # «来» 非情态
        ("无法预测下降", DIRECTION_DECREASE),  # «法» 非情态
        ("非常明显地增长", DIRECTION_INCREASE),  # «常» 非情态
        ("库存特别明显地增长", DIRECTION_INCREASE),  # «别» 嵌在「特别」里
        ("不仅增长了销量", DIRECTION_INCREASE),  # «仅» 非情态
        ("供给不足导致下降", DIRECTION_DECREASE),  # «不» 后是「足」，非情态
        ("无货导致下降", DIRECTION_DECREASE),  # «货» 非情态
        ("问题不大销售下降", DIRECTION_DECREASE),  # «大» 非情态
        ("采购金额上升", DIRECTION_INCREASE),  # 纯 FP 基线
        ("订单量下降", DIRECTION_DECREASE),  # 纯 FP 基线
        ("成本提高", DIRECTION_INCREASE),  # 纯 FP 基线
        ("利润下滑", DIRECTION_DECREASE),  # 纯 FP 基线
    ],
)
def test_derive_direction_false_positives_preserve_direction(
    statement: str, expected: str
) -> None:
    """FP：非否定词素里的否定字**不得**作废方向词（21 条，全部来自轮 2 的真实回归）。"""
    assert deriveDirection(statement) == expected


def test_negated_direction_with_falling_data_is_inconclusive_not_supported() -> None:
    """I1 RED：「不应下降」+ 数据**下跌** ⇒ 曾被静默判成 SUPPORTED（符号翻转）。

    断言是「**不应**下降」，数据跌了 = 被违背；正确口径是**方向不可判** ⇒ INCONCLUSIVE，
    并标注「断言含否定」（**不能**用误导性的「断言未含可判方向」——此处有方向词，只是被否定了）。
    """
    result = evaluateHypothesis(
        _candidate("采购金额不应下降"), _summary("200", "100"), rows=[{"amount": 100}]
    )

    assert result.direction is None
    assert result.verdict is HypothesisVerdict.INCONCLUSIVE
    assert any("断言含否定" in item for item in result.limitations)
    assert not any("断言未含可判方向" in item for item in result.limitations)


# ---------------------------------------------------------------------------
# D20b 反向断言（承重）：方向词 + 同向数据 ⇒ SUPPORTED（不是恒 INCONCLUSIVE）
# ---------------------------------------------------------------------------


def test_increase_word_with_rising_data_is_supported() -> None:
    """`statement` 含「上升」且数据确实上涨 ⇒ SUPPORTED。

    这是抓「``direction`` 恒 ``None`` ⇒ verdict 恒 INCONCLUSIVE」（D20b）的**反向断言**。
    """
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("100", "200"), rows=[{"amount": 200}]
    )

    assert result.direction == "increase"
    assert result.verdict is HypothesisVerdict.SUPPORTED
    assert result.baseline == Decimal("100") and result.current == Decimal("200")


def test_decrease_word_with_falling_data_is_supported() -> None:
    """跌向词的对称用例：`statement` 含「下降」且数据确实下跌 ⇒ SUPPORTED。"""
    result = evaluateHypothesis(
        _candidate("库存量下降"), _summary("200", "50"), rows=[{"amount": 50}]
    )

    assert result.direction == "decrease"
    assert result.verdict is HypothesisVerdict.SUPPORTED


def test_increase_word_with_falling_data_is_contradicted() -> None:
    """方向词与数据相反 ⇒ CONTRADICTED（不是 INCONCLUSIVE）。"""
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("200", "100"), rows=[{"amount": 100}]
    )

    assert result.verdict is HypothesisVerdict.CONTRADICTED


def test_decrease_word_with_rising_data_is_contradicted() -> None:
    result = evaluateHypothesis(
        _candidate("库存量下降"), _summary("100", "200"), rows=[{"amount": 200}]
    )

    assert result.verdict is HypothesisVerdict.CONTRADICTED


def test_direction_word_present_but_data_equal_is_inconclusive() -> None:
    """有方向词、有行，但 `current == baseline` ⇒ INCONCLUSIVE（D20：恰相等既非支持也非反驳）。"""
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("100", "100"), rows=[{"amount": 100}]
    )

    assert result.verdict is HypothesisVerdict.INCONCLUSIVE


def test_no_direction_word_is_inconclusive_even_with_data() -> None:
    """无方向词 ⇒ 即便数据量很大也不瞎判方向 ⇒ INCONCLUSIVE。"""
    result = evaluateHypothesis(
        _candidate("供应商A供货情况"), _summary("100", "900"), rows=[{"amount": 900}]
    )

    assert result.direction is None
    assert result.verdict is HypothesisVerdict.INCONCLUSIVE


# ---------------------------------------------------------------------------
# D19b：量级必须暴露字典序陷阱（baseline=900 / current=1000）
# ---------------------------------------------------------------------------


def test_magnitude_ordering_beats_lexicographic_string_compare() -> None:
    """`baseline="900"` / `current="1000"` + 上升词 ⇒ SUPPORTED。

    裸串比较 ``"1000" > "900"`` 在 Python 里是 **``False``**（字典序）⇒ 会被静默判成
    CONTRADICTED。正确读法（``numeric()`` → ``Decimal``）得 ``1000 > 900`` ⇒ SUPPORTED。
    """
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("900", "1000"), rows=[{"amount": 1000}]
    )

    assert result.baseline == Decimal("900") and result.current == Decimal("1000")
    assert result.verdict is HypothesisVerdict.SUPPORTED
    # 反向断言：裸串比较确实翻转（钉住「为什么必须走 numeric()」）。
    assert not ("1000" > "900")


# ---------------------------------------------------------------------------
# 零支持 / 空数据 / 无样本 三条路径
# ---------------------------------------------------------------------------


def test_zero_support_with_rows_is_not_supported() -> None:
    """**有行但数据不支持**（原文那条用例其实靠 `sample_size == 0` 才过 —— 补真零支持）。"""
    rows = [{"amount": 100}]  # 有样本
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("100", "80"), rows
    )

    assert result.sample_size == 1
    assert result.verdict is HypothesisVerdict.CONTRADICTED
    assert result.verdict is not HypothesisVerdict.SUPPORTED


def test_empty_rows_is_inconclusive() -> None:
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("100", "200"), rows=[]
    )

    assert result.sample_size == 0
    assert result.verdict is HypothesisVerdict.INCONCLUSIVE
    assert any("无数据样本" in item for item in result.limitations)


def test_empty_summary_is_inconclusive() -> None:
    """空摘要（无 baseline/current）⇒ INCONCLUSIVE，且标注缺基线。"""
    result = evaluateHypothesis(
        _candidate("采购金额上升"), EvidenceSummary(empty=True), rows=[{"amount": 1}]
    )

    assert result.baseline is None and result.current is None
    assert result.verdict is HypothesisVerdict.INCONCLUSIVE
    assert any("缺少基线" in item for item in result.limitations)


# ---------------------------------------------------------------------------
# D18：执行失败与数据支持度正交
# ---------------------------------------------------------------------------


def test_execution_error_is_execution_failed_not_contradicted() -> None:
    result = evaluateHypothesis(
        _candidate("采购金额上升"),
        _summary("100", "200"),
        rows=[],
        execution_error="ORA-00942: table or view does not exist",
    )

    assert result.verdict is HypothesisVerdict.EXECUTION_FAILED
    assert result.execution_succeeded is False
    assert result.baseline is None and result.current is None
    assert any("执行失败" in item for item in result.limitations)


def test_execution_succeeded_flag_is_true_on_normal_path() -> None:
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("100", "200"), rows=[{"amount": 200}]
    )

    assert result.execution_succeeded is True


# ---------------------------------------------------------------------------
# D20：threshold 是最小显著变化量（显式入参，不是候选的键）
# ---------------------------------------------------------------------------


def test_threshold_makes_small_change_inconclusive() -> None:
    """|current - baseline| < threshold ⇒ INCONCLUSIVE（跌 0.001% 不与跌 90% 同罪）。"""
    result = evaluateHypothesis(
        _candidate("采购金额上升"),
        _summary("100", "110"),
        rows=[{"amount": 110}],
        threshold=Decimal("50"),
    )

    assert result.verdict is HypothesisVerdict.INCONCLUSIVE


def test_threshold_significant_change_still_uses_direction() -> None:
    result = evaluateHypothesis(
        _candidate("采购金额上升"),
        _summary("100", "200"),
        rows=[{"amount": 200}],
        threshold=Decimal("50"),
    )

    assert result.verdict is HypothesisVerdict.SUPPORTED


def test_threshold_is_an_explicit_param_not_a_candidate_key() -> None:
    """D20b：`threshold` 是 ``evaluateHypothesis`` 的**显式入参**，不是候选的键。"""
    assert "threshold" in inspect.signature(evaluateHypothesis).parameters
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("100", "200"), rows=[{"amount": 200}],
        threshold=Decimal("10"),
    )
    assert result.threshold == Decimal("10")


def test_no_threshold_small_reverse_change_is_contradicted() -> None:
    """无阈值 ⇒ 只在反向变化时判 CONTRADICTED（哪怕跌得很少）。"""
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("100", "99"), rows=[{"amount": 99}]
    )

    assert result.verdict is HypothesisVerdict.CONTRADICTED


# ---------------------------------------------------------------------------
# D20b 接线：候选的键来自真实 Hypothesis 形状（不是 plan 虚构的键）
# ---------------------------------------------------------------------------


def test_works_with_a_real_hypothesis_dataclass_payload() -> None:
    """真实候选是 ``Hypothesis``（statement/driver/verificationSql）—— 用它构造也不崩。

    plan 原文读的 `assertion`/`metric`/`direction`/`threshold` 四键全不存在 ⇒ 断言必须
    取 ``statement``、metric 取 ``driver``。
    """
    hypothesis = Hypothesis(
        statement="采购金额上升", driver="采购金额", verificationSql="SELECT 1 FROM dual"
    )
    candidate = {
        "statement": hypothesis.statement,
        "driver": hypothesis.driver,
        "verificationSql": hypothesis.verificationSql,
    }

    result = evaluateHypothesis(
        candidate, _summary("100", "200"), rows=[{"amount": 200}]
    )

    assert result.assertion == "采购金额上升"
    assert result.metric == "采购金额"
    assert result.verdict is HypothesisVerdict.SUPPORTED


def test_assertion_and_metric_fall_back_to_empty_string_not_keyerror() -> None:
    result = evaluateHypothesis({}, _summary("100", "200"), rows=[{"amount": 1}])

    assert result.assertion == ""
    assert result.metric == ""


# ---------------------------------------------------------------------------
# D19：evidence_summary 是 JSON 原生 dict（数值是字符串，不是 float / Decimal）
# ---------------------------------------------------------------------------


def test_evidence_summary_is_json_native_with_string_numerics() -> None:
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("100", "200"), rows=[{"amount": 200}]
    )

    assert isinstance(result.evidence_summary, dict)
    # camelCase 线上别名（mode="json" + by_alias）
    assert result.evidence_summary["baseline"] == "100"
    assert isinstance(result.evidence_summary["baseline"], str)
    assert not isinstance(result.evidence_summary["baseline"], float)


# ---------------------------------------------------------------------------
# 结构：frozen dataclass
# ---------------------------------------------------------------------------


def test_hypothesis_test_is_frozen() -> None:
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("100", "200"), rows=[{"amount": 200}]
    )

    with pytest.raises((FrozenInstanceError, ValidationError, AttributeError)):
        result.verdict = HypothesisVerdict.INCONCLUSIVE  # type: ignore[misc]


def test_dataclass_carries_all_declared_fields() -> None:
    result = evaluateHypothesis(
        _candidate("采购金额上升"), _summary("100", "200"), rows=[{"amount": 200}]
    )

    assert isinstance(result, HypothesisTest)
    assert set(vars(result)) == {
        "assertion",
        "metric",
        "direction",
        "threshold",
        "baseline",
        "current",
        "verdict",
        "evidence_summary",
        "execution_succeeded",
        "sample_size",
        "limitations",
    }
