"""发布校验项的**公共类型与常量**（M1-1.3 批次 B 的叶子模块）。

钉住**绑定身份键**这一个纯函数 —— 它是校验侧（`semantic_release_check.target_key`）
与差异侧（`SemanticChange.target`）**共用的唯一渲染器** ⇒ 一旦漂移，
「差异报告里的一个绑定」就**查不到** check 表里对应的那行。

三条判据：

1. **身份完整**：键必须带**数据源**与**角色**（少一个维度 ⇒ 两行撞唯一索引 ⇒ 500）。
2. **超宽不截断**：列宽 `String(200)` ⇒ 超宽时改用「类名摘要(64) + 完整身份 sha256[:16]」
   —— **截断**会让两个不同的绑定撞成同一个键（静默丢证据）。
3. **两侧同源**：差异侧与校验侧共用 ⇒ 同一个绑定在两个接口里是**同一个名字**（可对账）。
"""

from __future__ import annotations

from typing import Any

from app.services.semantic_release_check_types import (
    MAX_TARGET_KEY_LENGTH,
    PublishGateResult,
    bindingTargetKey,
)


def _binding(**overrides: Any) -> dict[str, Any]:
    binding = {
        "className": "Supplier",
        "datasourceName": "THBI",
        "schemaName": "ERP",
        "sourceTable": "T_SUPPLIER",
        "bindingRole": "BASE",
    }
    binding.update(overrides)
    return binding


def test_bindingTargetKey_carries_all_five_identity_fields() -> None:
    key = bindingTargetKey(_binding())

    assert key == "Supplier@THBI.ERP.T_SUPPLIER#BASE"


def test_bindingTargetKey_distinguishes_the_binding_role() -> None:
    """同一类同表两个角色 ⇒ 两个键（否则撞唯一索引 ⇒ 整次发布 500）。"""
    base = bindingTargetKey(_binding(bindingRole="BASE"))
    numerator = bindingTargetKey(_binding(bindingRole="NUMERATOR"))

    assert base != numerator


def test_bindingTargetKey_distinguishes_the_datasource() -> None:
    """同一类同表在两个数据源各绑一次 ⇒ 两个键。"""
    first = bindingTargetKey(_binding(datasourceName="THBI"))
    second = bindingTargetKey(_binding(datasourceName="ERP2"))

    assert first != second


def test_bindingTargetKey_keeps_a_normal_key_intact() -> None:
    """不超宽 ⇒ 原样返回（不做任何加工）。"""
    key = bindingTargetKey(_binding())

    assert len(key) <= MAX_TARGET_KEY_LENGTH
    assert "@" in key and "#" in key


def test_bindingTargetKey_does_not_truncate_an_oversized_key() -> None:
    """超宽 ⇒ 摘要 + **完整身份**哈希 ⇒ 两个不同的超宽绑定仍能区分。"""
    first = bindingTargetKey(_binding(className="C" * 300, sourceTable="T_A"))
    second = bindingTargetKey(_binding(className="C" * 300, sourceTable="T_B"))

    assert len(first) <= MAX_TARGET_KEY_LENGTH
    assert first != second
    # 摘要 + 哈希，不是「原样截断」⇒ 原键的区分信息（表名）还在哈希里
    assert "~" in first
    assert first == bindingTargetKey(_binding(className="C" * 300, sourceTable="T_A"))


def test_bindingTargetKey_is_stable_for_the_same_binding() -> None:
    """同一绑定 ⇒ 同一个键（两次渲染逐字相同 ⇒ 两侧可对账）。"""
    assert bindingTargetKey(_binding()) == bindingTargetKey(_binding())


# --- 闸门结论 ---------------------------------------------------------------


def test_gateResult_canPublish_is_false_when_blockers_exist() -> None:
    gate = PublishGateResult(blockers=("孤岛绑定",))

    assert gate.canPublish is False


def test_gateResult_canPublish_is_true_without_blockers() -> None:
    """只有 warning 不拦发布（warning = 治理债，不是阻断项）。"""
    gate = PublishGateResult(warnings=("跨域补齐 3 个类",))

    assert gate.canPublish is True
