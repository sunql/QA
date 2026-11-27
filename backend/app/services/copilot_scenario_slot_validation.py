"""场景槽位取值校验（**发起编排前**）——把非法槽位挡在 LLM 之前。

生产缺陷（2026-11-28，用户实测本机复现）：采购 Copilot → 到货跟踪场景，
高级参数里「周期期序」(periodIndex) 填 **1** ⇒ 查询了**所有**供应商。

根因链（已定性，此处只做防线 B）：

1. ``periodIndex`` 语义是 **0=本期、-1=上期，正数非法**（工具契约
   ``delivery_tracking_query.resolveWindow``）；
2. 前端 number 槽位控件无范围校验 ⇒ 用户能填正数；
3. 工具报错后编排 runner 记工具步 FAILED 但**不终止 run**，回主循环让
   **LLM 重新规划**；
4. 场景槽位只拼进**问题文本**，没有「槽位→工具参数」的强制注入 ⇒ LLM
   重规划时丢了 ``supplierKeys`` ⇒ 查了所有供应商。

防线 A（消除触发条件）在场景配置面：``slot_schema`` 声明
``minimum`` / ``maximum``，前端 InputNumber 按此限制输入。
防线 B（消除放大器）在本模块：**发起编排前**校验槽位值 ⇒ 非法直接
422，不进 LLM、不产生"查所有供应商"的错结果。

规则是**通用的**：按 ``slot_schema`` 里每个槽位声明的 ``minimum`` /
``maximum`` 校验对应槽位值；不针对某个场景写特例。唯一的例外是
``periodIndex`` / ``anchorDay`` 成对校验——它来自**工具契约**（工具侧同款
规则「periodIndex 必须与 anchorDay 一起使用」），因此只对绑定了
``delivery_tracking_query`` 的场景生效。
"""

from __future__ import annotations

from typing import Any

from app.domain.exceptions import ValidationError

#: 只有 number 槽位参与范围校验（其它类型的槽位不声明 min/max）。
_NUMERIC_SLOT_TYPE = "number"

#: 绑定了 delivery_tracking_query 的 Agent 码。
#: periodIndex / anchorDay 成对校验只对这类场景生效（工具契约同款规则）。
DELIVERY_TRACKING_AGENT_CODE = "DELIVERY_TRACKING_AGENT"

#: 到货跟踪场景里成对出现的槽位键。
_PERIOD_INDEX_KEY = "periodIndex"
_ANCHOR_DAY_KEY = "anchorDay"


def _slotLabel(item: dict[str, Any]) -> str:
    """槽位展示名（label 优先，回退 key）。"""
    return str(item.get("label") or item.get("key") or "").strip()


def _labelOf(slotSchema: list[dict[str, Any]], key: str) -> str:
    """按 key 取槽位 label；槽位不存在 ⇒ 回退 key 本身。"""
    for item in slotSchema:
        if str(item.get("key", "")) == key:
            return _slotLabel(item)
    return key


def _asNumber(value: Any) -> float | None:
    """槽位值 → 数值。

    不可判定（None / 空串 / 非数值文本 / 布尔 / 数组）⇒ ``None``
    ⇒ 不参与范围校验，交给既有逻辑（必填校验 / 工具侧校验）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _isGiven(value: Any) -> bool:
    """槽位是否「给了值」（None / 空串 / 空列表 = 没给）。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _formatRange(minimum: Any, maximum: Any) -> str:
    """允许范围的可读文本（如 ``-12..0``）。"""
    if minimum is not None and maximum is not None:
        return f"{minimum}..{maximum}"
    if minimum is not None:
        return f">= {minimum}"
    return f"<= {maximum}"


def _rangeError(*, item: dict[str, Any], value: Any, minimum: Any, maximum: Any) -> ValidationError:
    """构造越界错误：点名槽位 label、允许范围与收到的值。"""
    label = _slotLabel(item)
    hint = str(item.get("hint") or "").strip()
    rangeText = _formatRange(minimum, maximum)
    message = f"{label} 必须在 {rangeText} 之间，收到 {value}"
    if hint:
        message = f"{message}（{hint}）"
    return ValidationError(message)


def _validateNumericRanges(*, slots: dict[str, Any], slotSchema: list[dict[str, Any]]) -> None:
    """通用规则：number 槽位按声明的 minimum / maximum 校验取值。"""
    for item in slotSchema:
        if str(item.get("type", "")) != _NUMERIC_SLOT_TYPE:
            continue
        minimum = item.get("minimum")
        maximum = item.get("maximum")
        if minimum is None and maximum is None:
            continue  # 未声明范围 ⇒ 不拦
        key = str(item.get("key", ""))
        raw = slots.get(key)
        number = _asNumber(raw)
        if number is None:
            continue  # 非数值 ⇒ 不拦（交给既有逻辑）
        if minimum is not None and number < minimum:
            raise _rangeError(item=item, value=raw, minimum=minimum, maximum=maximum)
        if maximum is not None and number > maximum:
            raise _rangeError(item=item, value=raw, minimum=minimum, maximum=maximum)


def _validatePeriodAnchorPair(*, slots: dict[str, Any], slotSchema: list[dict[str, Any]]) -> None:
    """periodIndex 必须与 anchorDay 成对（工具契约同款规则，提前到发起前）。

    不成对时工具会报错 ⇒ 编排 runner 记 FAILED 但不终止 run ⇒ LLM 重规划
    ⇒ 槽位丢失 ⇒ 错结果。提前拦 ⇒ 直接 422。
    """
    if not _isGiven(slots.get(_PERIOD_INDEX_KEY)):
        return
    if _isGiven(slots.get(_ANCHOR_DAY_KEY)):
        return
    periodLabel = _labelOf(slotSchema, _PERIOD_INDEX_KEY)
    anchorLabel = _labelOf(slotSchema, _ANCHOR_DAY_KEY)
    raise ValidationError(f"{periodLabel} 必须与 {anchorLabel} 一起使用")


def validateScenarioSlotValues(
    *,
    slots: dict[str, Any],
    slotSchema: list[dict[str, Any]],
    agentCode: str,
) -> None:
    """按场景声明校验槽位取值；非法 ⇒ ``ValidationError``（⇒ 422）。

    - **通用规则**：`type == "number"` 且声明了 ``minimum`` / ``maximum`` 的
      槽位，取值越界 ⇒ 报错（消息点名槽位 label 与允许范围）；
    - **场景专属规则**：绑定了 ``delivery_tracking_query`` 的场景，
      ``periodIndex`` 与 ``anchorDay`` 必须成对。

    :param slots: 请求里的槽位值（键 = ``slot_schema[].key``）。
    :param slotSchema: 场景配置的槽位定义（DB 里的 JSONB）。
    :param agentCode: 场景钉定的 Agent 码（决定是否做成对校验）。
    :raises ValidationError: 槽位取值非法。
    """
    _validateNumericRanges(slots=slots, slotSchema=slotSchema)
    if agentCode == DELIVERY_TRACKING_AGENT_CODE:
        _validatePeriodAnchorPair(slots=slots, slotSchema=slotSchema)
