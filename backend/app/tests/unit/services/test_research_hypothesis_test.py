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
# I1：否定式方向词必须作废（位置性否定门）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    ["不应下降", "不会减少", "不该增长", "没有下降", "未能增长", "不增长", "不再下降"],
)
def test_derive_direction_none_when_direction_word_is_negated(statement: str) -> None:
    """TP 矩阵：方向词被**紧邻**否定式否定 ⇒ 作废 ⇒ 无其它方向词 ⇒ ``None``。

    「不应下降」等断言的方向词只是被否定了，不是没有方向词 —— 按本模块「宁可不判，不许瞎猜」
    的口径必须返回 ``None``（符号翻转 = 静默把「被违背」判成「已验证」）。
    """
    assert deriveDirection(statement) is None


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("无锡供应商采购额上升", DIRECTION_INCREASE),
        ("未来三月采购额上升", DIRECTION_INCREASE),
        ("无法确认库存增长", DIRECTION_INCREASE),
        ("不但上涨", DIRECTION_INCREASE),
        ("再增长", DIRECTION_INCREASE),
    ],
)
def test_derive_direction_unaffected_by_non_negating_particles(
    statement: str, expected: str
) -> None:
    """FP 反向矩阵（承重）：``无``/``未``/``不``/``再`` 出现在**窗口外或非否定位置**时**不得**误判为否定。

    这些词如果走全文扫描，会把「无锡」「未来」「无法」整段误伤 —— 故此实现只从方向词向后看
    **小句内 5 字窗口**（``NEGATION_SCAN_WINDOW``），且半路遇到转折/连接词即停扫。

    **注意本矩阵不证明该子类安全**：这里的 ``无``/``未`` 都**恰好**落在窗口外（``无锡供应商
    采购额上升`` 的 «无» 距 «上升» = 8、``未来三月采购额上升`` 的 «未» 距 «上升» = 7），是靠
    距离躲开，不是靠词边界。窗口内的同类词（``非常``/``特别``/``不仅``/``不锈钢``…）仍会被
    误判为否定 ⇒ 丢方向（详见 ``_isNegatedDirectionWordAt`` docstring 的「已知残余」第 2 条，
    属本轮**未收窄**的已知残余，交控制器裁定）。
    """
    assert deriveDirection(statement) == expected


@pytest.mark.parametrize(
    "statement",
    [
        "不应大幅下降",  # Hole A：副词隔断（«不» 距 «下降» = 4）
        "不会明显减少",  # Hole A
        "未能持续增长",  # Hole A
        "没有出现下降",  # Hole A
        "不再有增长",  # Hole B：助词链断（«不» 距 «增长» = 3）
        "不应再下降",  # Hole B
    ],
)
def test_derive_direction_none_when_negation_separated_by_fillers(statement: str) -> None:
    """I1 第二轮：否定门从「紧邻前缀」放宽为「小句内 5 字窗口」后，**被隔断的否定式**也必须作废。

    控制器在 ``0266d16`` 上原生复现过这 6 行符号翻转（副词/助词隔断 ⇒ 否定门看不见 ⇒
    被违背的断言被判成 SUPPORTED）。每行都钉住「否定字 → 填充字 → 方向词」这一同缺陷类。
    """
    assert deriveDirection(statement) is None


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        # «无» 距方向词**恰好 6 > W(=5)** ⇒ 落在窗口外，不判否定（与既有行「无法确认库存增长」
        # 同形，独立一条自证窗口上界；若把 W 改成 6 这条立即翻红）。
        ("无供应商库存增长", DIRECTION_INCREASE),
        # 承重回归行：既有 FP 里钉住窗口上界的那条（«无» 距 «增长» = 6）。
        ("无法确认库存增长", DIRECTION_INCREASE),
    ],
)
def test_scan_window_upper_bound_ignores_negation_beyond_five_chars(
    statement: str, expected: str
) -> None:
    """窗口上界 ``W=5``：超出 5 字的否定字**不**作废方向词（否则「无锡/未来/无法」全误伤）。"""
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
