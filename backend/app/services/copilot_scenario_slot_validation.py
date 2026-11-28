"""场景槽位取值校验（**发起编排前**）——把非法槽位挡在 LLM 之前。

生产缺陷（2026-11-28，用户实测本机复现）：采购 Copilot → 到货跟踪场景，
高级参数里「周期期序」(periodIndex) 填 **1** ⇒ 查询了**所有**供应商。

根因链（已定性）：

1. ``periodIndex`` 语义是 **0=本期、-1=上期，正数非法**（工具契约
   ``delivery_tracking_query.resolveWindow``）；
2. 前端 number 槽位控件无范围校验 ⇒ 用户能填正数；
3. 工具报错后编排 runner 记工具步 FAILED 但**不终止 run**，回主循环让
   **LLM 重新规划**；
4. 场景槽位只拼进**问题文本**，没有「槽位→工具参数」的强制注入 ⇒ LLM
   重规划时丢了 ``supplierKeys`` ⇒ 查了所有供应商。

防线 A（消除触发条件）在场景配置面：``slot_schema`` 声明
``minimum`` / ``maximum``，前端 InputNumber 按此限制输入。**但配置面是
软约束**——已部署库的存量场景行拿不到新字段（seed 只在「行不存在」时
INSERT），配置一过期防线就跟着失效。

防线 B（消除放大器）在本模块：**发起编排前**校验槽位值 ⇒ 非法直接
422，不进 LLM、不产生「查所有供应商」的错结果。B 内部又分两层：

- **配置层**（通用规则）：按 ``slot_schema`` 里每个槽位声明的
  ``minimum`` / ``maximum`` 校验对应槽位值；不针对某个场景写特例。
- **契约层**（硬约束）：范围规则的**真值写在代码里**（工具契约），
  不依赖任何配置 ⇒ 生产库即便还没打上 seed 补丁，防线也立即生效。

契约层的两条（均来自 ``delivery_tracking_query`` 工具契约）：

- ``periodIndex ≤ 0``：正数 = 未结束的周期，不可查询；
- ``anchorDay ∈ 1..28``：29/30/31 在短月无对应日。

另有一条**成对校验**：``periodIndex`` 必须与 ``anchorDay`` 一起使用
（工具侧同款规则）。成对校验只在场景**声明了这两个槽位**时生效 ——
否则会对「本就无此槽位」的场景误报。
"""

from __future__ import annotations

from typing import Any

from app.domain.exceptions import ValidationError

#: 只有 number 槽位参与范围校验（其它类型的槽位不声明 min/max）。
_NUMERIC_SLOT_TYPE = "number"

#: 绑定了 delivery_tracking_query 的 Agent 码。
#: 契约层硬约束 + 成对校验只对这类场景生效（工具契约同款规则）。
DELIVERY_TRACKING_AGENT_CODE = "DELIVERY_TRACKING_AGENT"

#: 到货跟踪场景里成对出现的槽位键。
_PERIOD_INDEX_KEY = "periodIndex"
_ANCHOR_DAY_KEY = "anchorDay"

#: 契约层：周期期序上限（0=本期、-1=上期；正数非法）。
#: 同 ``delivery_tracking_query._resolveAnchor`` 的「periodIndex 不能为正数」。
_CONTRACT_PERIOD_INDEX_MAX = 0

#: 契约层：锚点日范围（同 ``delivery_tracking_query._MAX_ANCHOR_DAY``）。
_CONTRACT_ANCHOR_DAY_MIN = 1
_CONTRACT_ANCHOR_DAY_MAX = 28


def _slotLabel(item: dict[str, Any]) -> str:
    """槽位展示名（label 优先，回退 key）。"""
    return str(item.get("label") or item.get("key") or "").strip()


def _slotByKey(slotSchema: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    """按 key 取槽位定义；未声明 ⇒ None。"""
    for item in slotSchema:
        if str(item.get("key", "")) == key:
            return item
    return None


def _labelOf(slotSchema: list[dict[str, Any]], key: str) -> str:
    """按 key 取槽位 label；槽位不存在 ⇒ 回退 key 本身。"""
    item = _slotByKey(slotSchema, key)
    return _slotLabel(item) if item is not None else key


def _asNumber(value: Any) -> float | None:
    """任意值 → 数值（不可判定 ⇒ None）。

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
    """配置层通用规则：number 槽位按声明的 minimum / maximum 校验取值。

    min/max 一律过 ``_asNumber``：管理员写成字符串（``"0"``）也能正确比较；
    不可解析（非数值）⇒ 跳过**该侧**比较（不 500）。
    """
    for item in slotSchema:
        if str(item.get("type", "")) != _NUMERIC_SLOT_TYPE:
            continue
        minimum = _asNumber(item.get("minimum"))
        maximum = _asNumber(item.get("maximum"))
        if minimum is None and maximum is None:
            continue  # 未声明范围（或声明了但不可解析）⇒ 不拦
        key = str(item.get("key", ""))
        raw = slots.get(key)
        number = _asNumber(raw)
        if number is None:
            continue  # 非数值 ⇒ 不拦（交给既有逻辑）
        if minimum is not None and number < minimum:
            raise _rangeError(item=item, value=raw, minimum=minimum, maximum=maximum)
        if maximum is not None and number > maximum:
            raise _rangeError(item=item, value=raw, minimum=minimum, maximum=maximum)


def _validateContractRanges(*, slots: dict[str, Any], slotSchema: list[dict[str, Any]]) -> None:
    """契约层硬约束：范围真值写在代码里（配置过期/缺失也生效）。

    - ``periodIndex ≤ 0``：正数 = 未结束的周期，不可查询；
    - ``anchorDay ∈ 1..28``：29/30/31 在短月无对应日。

    只对**场景声明了**对应槽位时生效（未声明 = 该槽位与本场景无关）。
    """
    periodItem = _slotByKey(slotSchema, _PERIOD_INDEX_KEY)
    if periodItem is not None:
        raw = slots.get(_PERIOD_INDEX_KEY)
        number = _asNumber(raw) if _isGiven(raw) else None
        if number is not None and number > _CONTRACT_PERIOD_INDEX_MAX:
            raise ValidationError(
                f"{_slotLabel(periodItem)} 不能为正数"
                f"（未结束的周期不可查询），收到 {raw}"
            )

    anchorItem = _slotByKey(slotSchema, _ANCHOR_DAY_KEY)
    if anchorItem is not None:
        raw = slots.get(_ANCHOR_DAY_KEY)
        number = _asNumber(raw) if _isGiven(raw) else None
        if number is not None and not (
            _CONTRACT_ANCHOR_DAY_MIN <= number <= _CONTRACT_ANCHOR_DAY_MAX
        ):
            raise ValidationError(
                f"{_slotLabel(anchorItem)} 必须在 "
                f"{_CONTRACT_ANCHOR_DAY_MIN}..{_CONTRACT_ANCHOR_DAY_MAX} 之间，收到 {raw}"
            )


def _validatePeriodAnchorPair(*, slots: dict[str, Any], slotSchema: list[dict[str, Any]]) -> None:
    """periodIndex 必须与 anchorDay 成对（工具契约同款规则，提前到发起前）。

    不成对时工具会报错 ⇒ 编排 runner 记 FAILED 但不终止 run ⇒ LLM 重规划
    ⇒ 槽位丢失 ⇒ 错结果。提前拦 ⇒ 直接 422。

    仅当场景**声明了** periodIndex 与 anchorDay 两个槽位时才校验 ——
    否则会对「本就无此槽位」的场景误报。
    """
    if _slotByKey(slotSchema, _PERIOD_INDEX_KEY) is None:
        return
    if _slotByKey(slotSchema, _ANCHOR_DAY_KEY) is None:
        return
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

    - **配置层**：`type == "number"` 且声明了 ``minimum`` / ``maximum`` 的
      槽位，取值越界 ⇒ 报错（消息点名槽位 label 与允许范围）；
    - **契约层**：绑定了 ``delivery_tracking_query`` 的场景，
      ``periodIndex ≤ 0``、``anchorDay ∈ 1..28``（真值在代码里，不依赖配置）；
    - **成对校验**：同一场景的 ``periodIndex`` 与 ``anchorDay`` 必须成对
      （且两个槽位都声明了才校验）。

    :param slots: 请求里的槽位值（键 = ``slot_schema[].key``）。
    :param slotSchema: 场景配置的槽位定义（DB 里的 JSONB）。
    :param agentCode: 场景钉定的 Agent 码（决定是否走契约层规则）。
    :raises ValidationError: 槽位取值非法。
    """
    _validateNumericRanges(slots=slots, slotSchema=slotSchema)
    if agentCode == DELIVERY_TRACKING_AGENT_CODE:
        _validateContractRanges(slots=slots, slotSchema=slotSchema)
        _validatePeriodAnchorPair(slots=slots, slotSchema=slotSchema)
