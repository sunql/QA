"""发布校验项（M1-1.2）的纯逻辑用例。

钉四件事：

1. **逐项而非只记失败**：`PASS` 行也要产出 —— 「通过」与「没跑」不可区分，
   M1-a DoD 的「每条 check 落表可查」依赖它。
2. **severity 与 result 正交**：severity 回答「失败会有多严重」，result 回答「这次结论」。
   硬拒绝仍只针对 `PUBLISHED` 的 RATIO/DIFFERENCE（D07），不因逐项化而变口径。
3. **闸门语义零漂移**：`checksToGateResult(evaluatePublishChecks(m))` 必须与既有
   `evaluatePublishGate(m)` **完全相等** —— 逐项化是「落表」改造，不是闸门口径改造。
4. **确定性**：同一 manifest 两次评估的校验项序列完全一致（落库顺序可预期）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.semantic_manifest_schema import SCHEMA_VERSION
from app.services import semantic_release_checks as checksModule
from app.services.semantic_release_checks import (
    CHECK_MANIFEST_SCHEMA,
    CHECK_METRIC_DENOMINATOR_LANDING,
    CHECK_METRIC_NUMERATOR_LANDING,
    CHECK_METRIC_SOURCE,
    CHECK_SNAPSHOT_INTEGRITY,
    MAX_CHECKS_PER_RELEASE,
    MAX_GATE_MESSAGES,
    RESULT_FAIL,
    RESULT_PASS,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    TARGET_METRIC,
    TARGET_RELEASE,
    PublishCheck,
    capChecks,
    checksToGateResult,
    evaluateManifestChecks,
    evaluatePublishChecks,
)
from app.services.semantic_release_service import evaluatePublishGate
from app.tests._knowledge_manifest import sampleManifest


def _measure(className: str | None = "DWD_CLASS") -> dict[str, Any]:
    return {
        "agg": "SUM",
        "expr": None,
        "filter": None,
        "className": className,
        "classAlias": None,
    }


def _metric(
    *,
    name: str = "M_RATIO",
    metricType: str = "RATIO",
    status: str = "DRAFT",
    sources: list[dict[str, Any]] | None = None,
    numerator: dict[str, Any] | None | str = "default",
    denominator: dict[str, Any] | None | str = "default",
) -> dict[str, Any]:
    return {
        "metricName": name,
        "metricType": metricType,
        "status": status,
        "sources": [] if sources is None else sources,
        "numeratorMeasure": _measure() if numerator == "default" else numerator,
        "denominatorMeasure": _measure() if denominator == "default" else denominator,
    }


def _manifest(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """残缺 manifest（只有 metrics 段）—— 版本取当前契约。

    刻意不给全部分区：契约闸的结论由它单独产出（见 `evaluateManifestChecks`），
    这里只测**指标层**校验项。
    """
    return {"schemaVersion": SCHEMA_VERSION, "metrics": metrics}


def _byCode(checks: tuple[PublishCheck, ...], code: str) -> list[PublishCheck]:
    return [check for check in checks if check.code == code]


# ---------------------------------------------------------------------------
# 逐项产出：PASS 行也必须落表
# ---------------------------------------------------------------------------


def test_healthy_metric_produces_pass_rows_for_every_check_item() -> None:
    """「每条 check 落表可查」——健康指标也要留下 PASS 痕迹。"""
    checks = evaluatePublishChecks(
        _manifest(
            [
                _metric(
                    status="PUBLISHED",
                    sources=[{"className": "DWD_CLASS", "role": "BASE"}],
                )
            ]
        )
    )

    assert {check.code for check in checks} == {
        CHECK_METRIC_SOURCE,
        CHECK_METRIC_NUMERATOR_LANDING,
        CHECK_METRIC_DENOMINATOR_LANDING,
    }
    assert all(check.result == RESULT_PASS for check in checks)
    assert all(check.severity == SEVERITY_WARNING for check in checks)
    assert all(check.targetType == TARGET_METRIC for check in checks)
    assert all(check.targetKey == "M_RATIO" for check in checks)
    assert all(check.detail for check in checks), "PASS 行也要说清「查了什么」"


def test_manifest_level_checks_are_release_scoped() -> None:
    """快照完整性 / Manifest 契约是发布级校验项，目标类型是 RELEASE。"""
    checks = evaluateManifestChecks(sampleManifest())

    schemaChecks = _byCode(checks, CHECK_MANIFEST_SCHEMA)
    assert len(schemaChecks) == 1
    assert schemaChecks[0].targetType == TARGET_RELEASE
    assert schemaChecks[0].result == RESULT_PASS
    assert schemaChecks[0].severity == SEVERITY_ERROR


def test_broken_manifest_yields_error_fail_schema_row() -> None:
    """契约不满足 ⇒ severity=ERROR 的 FAIL 行（发布级校验项也逐项落表）。"""
    broken = sampleManifest()
    broken["joins"].append(dict(broken["joins"][0]))

    checks = evaluateManifestChecks(broken)

    schemaChecks = _byCode(checks, CHECK_MANIFEST_SCHEMA)
    assert len(schemaChecks) == 1
    assert schemaChecks[0].result == RESULT_FAIL
    assert schemaChecks[0].severity == SEVERITY_ERROR
    assert schemaChecks[0].detail


def test_snapshot_integrity_check_is_produced_by_the_release_evaluator() -> None:
    """完整性结论由「行」决定（快照哈希），不属于 manifest 自身。"""
    checks = evaluateManifestChecks(_manifest([_metric()]))

    assert not _byCode(checks, CHECK_SNAPSHOT_INTEGRITY)


# ---------------------------------------------------------------------------
# severity 与 result 正交
# ---------------------------------------------------------------------------


def test_metric_without_sources_is_warning_fail() -> None:
    """无源指标是治理债：显式暴露但不阻断（D07 第 1 条）。"""
    checks = evaluatePublishChecks(_manifest([_metric(status="DRAFT")]))

    sourceChecks = _byCode(checks, CHECK_METRIC_SOURCE)
    assert len(sourceChecks) == 1
    assert sourceChecks[0].result == RESULT_FAIL
    assert sourceChecks[0].severity == SEVERITY_WARNING
    assert "M_RATIO" in sourceChecks[0].detail


def test_published_ratio_without_landing_is_error_fail() -> None:
    """已发布的比率缺落点 ⇒ 不可执行，severity=ERROR（D07 第 2 条）。"""
    checks = evaluatePublishChecks(
        _manifest(
            [
                _metric(
                    status="PUBLISHED",
                    numerator=_measure(className=None),
                    denominator=None,
                )
            ]
        )
    )

    landingChecks = [
        check
        for check in checks
        if check.code in (CHECK_METRIC_NUMERATOR_LANDING, CHECK_METRIC_DENOMINATOR_LANDING)
    ]
    assert len(landingChecks) == 2
    assert all(check.result == RESULT_FAIL for check in landingChecks)
    assert all(check.severity == SEVERITY_ERROR for check in landingChecks)


def test_draft_ratio_without_landing_is_warning_fail() -> None:
    """治理库 12 条指标全是 DRAFT —— 首个快照不能被闸门卡住。"""
    checks = evaluatePublishChecks(
        _manifest(
            [
                _metric(
                    status="DRAFT",
                    sources=[{"className": "DWD_CLASS", "role": "BASE"}],
                    numerator=_measure(className=None),
                    denominator=None,
                )
            ]
        )
    )

    landingChecks = [
        check
        for check in checks
        if check.code in (CHECK_METRIC_NUMERATOR_LANDING, CHECK_METRIC_DENOMINATOR_LANDING)
    ]
    assert len(landingChecks) == 2
    assert all(check.severity == SEVERITY_WARNING for check in landingChecks)


def test_measure_metric_has_no_denominator_check() -> None:
    """MEASURE 指标不存在「分母缺落点」；给它加一条就是噪音。"""
    checks = evaluatePublishChecks(
        _manifest([_metric(metricType="MEASURE", numerator=_measure(className=None))])
    )

    assert not _byCode(checks, CHECK_METRIC_DENOMINATOR_LANDING)
    assert len(_byCode(checks, CHECK_METRIC_NUMERATOR_LANDING)) == 1


# ---------------------------------------------------------------------------
# 闸门语义零漂移
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "metrics",
    [
        [],
        [_metric(status="DRAFT")],
        [_metric(status="DRAFT", numerator=_measure(className=None), denominator=None)],
        [
            _metric(
                status="PUBLISHED",
                numerator=_measure(className=None),
                denominator=None,
            )
        ],
        [_metric(metricType="MEASURE", status="PUBLISHED", numerator=_measure(className=None))],
        [
            _metric(name="M_A", status="PUBLISHED"),
            _metric(name="M_B", status="DRAFT", numerator=_measure(className=None)),
        ],
    ],
)
def test_checks_to_gate_result_equals_legacy_gate(metrics: list[dict[str, Any]]) -> None:
    """逐项化是落表改造，闸门口径必须一字不变。"""
    manifest = _manifest(metrics)

    assert checksToGateResult(evaluatePublishChecks(manifest)) == evaluatePublishGate(manifest)


def test_gate_cap_applies_to_blockers_and_warnings_independently() -> None:
    """报告体量封顶：两条切片各自生效（既有单测对闸门侧的断言在此复钉一次）。"""
    metrics = [
        _metric(
            name=f"M{index}",
            status="PUBLISHED",
            numerator=_measure(className=None),
            denominator=None,
        )
        for index in range(50)
    ]

    gate = checksToGateResult(evaluatePublishChecks(_manifest(metrics)))

    assert len(gate.blockers) == MAX_GATE_MESSAGES
    assert len(gate.warnings) == MAX_GATE_MESSAGES


# ---------------------------------------------------------------------------
# 确定性 / 防御
# ---------------------------------------------------------------------------


def test_evaluation_is_deterministic() -> None:
    """同一 manifest 两次评估的校验项序列必须完全一致（落库顺序可预期）。"""
    manifest = _manifest([_metric(name="M_A"), _metric(name="M_B")])

    assert evaluatePublishChecks(manifest) == evaluatePublishChecks(manifest)


def test_duplicate_metric_names_are_deduplicated() -> None:
    """Manifest 契约保证 metricName 唯一；去重只是防御（否则唯一索引会炸）。"""
    checks = evaluatePublishChecks(_manifest([_metric(name="M_DUP"), _metric(name="M_DUP")]))

    keys = [(check.code, check.targetKey) for check in checks]
    assert len(keys) == len(set(keys))


def test_manifest_without_metrics_yields_no_checks() -> None:
    """残缺 manifest（无 metrics 段）不得抛异常 —— 交给契约闸去报错。"""
    assert evaluatePublishChecks({"schemaVersion": SCHEMA_VERSION}) == ()


def test_non_dict_manifest_is_tolerated() -> None:
    """外部数据一律不可信：非字典输入返回空校验集，不炸。"""
    assert evaluatePublishChecks({"metrics": "not-a-list"}) == ()
    assert evaluatePublishChecks({"metrics": ["not-a-dict"]}) == ()


def test_malformed_sources_are_tolerated_as_missing_source() -> None:
    """契约外的 manifest：``sources`` 不是列表 / 元素不是对象 / 类名为空
    ⇒ 一律按「没有源类引用」处理（WARNING），不得抛异常把整次评估炸掉。"""
    notAList = {**_metric(name="M_SOURCES_DICT"), "sources": {"className": "DWD_CLASS"}}
    notDicts = {
        **_metric(name="M_SOURCES_JUNK"),
        "sources": ["not-a-dict", {"className": ""}, {"className": 42}],
    }

    for metric in (notAList, notDicts):
        checks = evaluatePublishChecks(_manifest([metric]))
        source = next(check for check in checks if check.code == CHECK_METRIC_SOURCE)
        assert source.result == RESULT_FAIL
        assert source.severity == SEVERITY_WARNING


# ---------------------------------------------------------------------------
# capChecks：上限必须从契约推导，截断必须显式标记（fail-closed）
# ---------------------------------------------------------------------------


def test_cap_is_derived_from_the_manifest_contract() -> None:
    """上限 ≥ 契约允许的最大校验项数。

    契约：1 条发布级（Schema）+ 每条指标最多 3 条（源类 / 分子 / 分母）
    + 1 条发布级（快照完整性）= 2 + 3 × MAX_MANIFEST_METRICS。

    ⚠️ 写死一个「看起来更宽」的数会出事：契约允许的**合法** manifest 也会被截断，
    而截断丢的是**尾部**校验项 —— 尾部往往正是最后一条指标的 ERROR 结论行。
    """
    from app.domain.semantic_manifest_schema import MAX_MANIFEST_METRICS

    assert MAX_CHECKS_PER_RELEASE == 2 + 3 * MAX_MANIFEST_METRICS
    assert MAX_CHECKS_PER_RELEASE >= MAX_MANIFEST_METRICS * 3


def test_cap_checks_does_not_truncate_within_the_cap() -> None:
    """上限内 ⇒ 原样返回（同一个对象，不复制、不改内容）。"""
    checks = tuple(
        PublishCheck(
            code=f"CODE_{index}",
            item=f"校验项 {index}",
            severity=SEVERITY_ERROR,
            result=RESULT_PASS,
            targetType="METRIC",
            targetKey=f"M_{index}",
        )
        for index in range(5)
    )

    assert capChecks(checks) is checks


def test_cap_checks_truncation_emits_an_error_fail_synthetic_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超限 ⇒ 保留上限-1 条 + 1 条 `CHECKS_TRUNCATED`（ERROR/FAIL）合成行。

    静默截断 = 假绿：读者看到「有结论」却不知道结论不完整 ⇒ 必须显式标记，
    且它必须**阻断**闸门（报告不完整 ⇒ 不得据此放行）。
    """
    monkeypatch.setattr(checksModule, "MAX_CHECKS_PER_RELEASE", 3)

    checks = tuple(
        PublishCheck(
            code=f"CODE_{index}",
            item=f"校验项 {index}",
            severity=SEVERITY_ERROR,
            result=RESULT_PASS,
            targetType="METRIC",
            targetKey=f"M_{index}",
            detail=f"第 {index} 项",
        )
        for index in range(5)
    )

    capped = capChecks(checks)

    assert len(capped) == 3
    assert [check.code for check in capped] == ["CODE_0", "CODE_1", "CHECKS_TRUNCATED"]
    synthetic = capped[-1]
    assert synthetic.severity == SEVERITY_ERROR
    assert synthetic.result == RESULT_FAIL
    assert synthetic.targetType == TARGET_RELEASE
    assert "5" in synthetic.detail and "3" in synthetic.detail

    # 截断行必须阻断闸门。
    gate = checksToGateResult(capped)
    assert gate.canPublish is False
    assert len(gate.blockers) == 1


def test_cap_checks_and_gate_use_the_same_capped_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """闸门与落表必须来自**同一个**集合。

    否则会出现「闸门按 6002 条判、表里只有 6000 条」⇒ 被截断掉的尾部
    （往往正是 ERROR 行）既不阻断、也不可查。
    """
    monkeypatch.setattr(checksModule, "MAX_CHECKS_PER_RELEASE", 3)

    checks = tuple(
        PublishCheck(
            code=f"CODE_{index}",
            item=f"校验项 {index}",
            severity=SEVERITY_ERROR,
            result=RESULT_FAIL,
            targetType="METRIC",
            targetKey=f"M_{index}",
            detail=f"第 {index} 项失败",
        )
        for index in range(5)
    )

    capped = capChecks(checks)
    gate = checksToGateResult(capped)

    # 表里落的就是被闸门判过的那一批。
    assert len(capped) == 3
    assert all(check.detail in gate.blockers for check in capped[:2])
    assert capped[-1].detail in gate.blockers
