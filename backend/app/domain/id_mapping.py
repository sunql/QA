"""统一 ID（M0-P0.1）格式的单一事实源。

    unified_id = "obj:{business_object}:{external_id}"

`business_object` / `external_id` 自身不得含 ``':'`` —— 否则反解二义，统一 ID
不再是稳定主键。ID 一律由服务端派生：客户端只传两个分段（见
``IdMappingCreate``），不允许自带 ``unifiedId`` 或跨库落点。

长度上限与 ``id_mapping`` 列宽一致（``models.py``）：business_object
``String(32)``、external_id ``String(128)``、unified_id 主键 ``String(128)``。
两段各自合法但拼起来超列宽时同样拒绝 —— 否则会撞主键列宽报 DB 错。
"""
from __future__ import annotations

import unicodedata

from app.domain.exceptions import ValidationError

UNIFIED_ID_PREFIX = "obj"
UNIFIED_ID_SEPARATOR = ":"

BUSINESS_OBJECT_MAX_LENGTH = 32
EXTERNAL_ID_MAX_LENGTH = 128
UNIFIED_ID_MAX_LENGTH = 128

MSG_UNIFIED_ID_FORMAT_INVALID = (
    "统一 ID 格式非法：应为 'obj:{业务对象}:{外部ID}'，两段均非空、"
    f"业务对象 ≤{BUSINESS_OBJECT_MAX_LENGTH}、外部 ID ≤{EXTERNAL_ID_MAX_LENGTH}、"
    f"总长 ≤{UNIFIED_ID_MAX_LENGTH}"
)
MSG_UNIFIED_ID_SEPARATOR_FORBIDDEN = (
    "统一 ID 的业务对象 / 外部 ID 不允许包含 ':'（该字符是格式分隔符）"
)
MSG_UNIFIED_ID_ILLEGAL_CHAR = (
    "统一 ID 的业务对象 / 外部 ID 不允许包含控制字符、不可见格式字符或行分隔符"
)

# 按 Unicode 通用类别拒绝，而不是枚举码点（枚举总会漏）：
#   Cc 控制字符 —— NUL 炸 asyncpg，换行/制表符可伪造日志
#   Cf 格式字符 —— 零宽空格、软连字符、双向覆盖（U+202E 等）造肉眼同形 ID
#   Cs 代理项   —— 无法 UTF-8 编码，flush 时抛 UnicodeEncodeError → 500
#   Zl/Zp       —— 行/段分隔符，日志换行注入
_REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _assertNoIllegalChars(segment: str) -> None:
    if any(unicodedata.category(ch) in _REJECTED_CATEGORIES for ch in segment):
        raise ValidationError(MSG_UNIFIED_ID_ILLEGAL_CHAR)


def _normalizeSegment(segment: str) -> str:
    """去空白 + NFC 归一：é（U+00E9）与 e+组合重音落到同一个 ID。"""
    normalized = unicodedata.normalize("NFC", (segment or "").strip())
    _assertNoIllegalChars(normalized)
    return normalized


def buildUnifiedId(businessObject: str, externalId: str) -> str:
    """构造统一 ID；非法输入抛 ``ValidationError``（HTTP 层 → 422）。

    两段先去空白再校验，故 ``' supplier '`` 与 ``'supplier'`` 落到同一行，
    不会因为空白绕过 ``(business_object, external_id)`` 唯一约束。
    """
    normalizedObject = _normalizeSegment(businessObject)
    normalizedExternal = _normalizeSegment(externalId)
    if not normalizedObject or not normalizedExternal:
        raise ValidationError(MSG_UNIFIED_ID_FORMAT_INVALID)
    if (
        len(normalizedObject) > BUSINESS_OBJECT_MAX_LENGTH
        or len(normalizedExternal) > EXTERNAL_ID_MAX_LENGTH
    ):
        raise ValidationError(MSG_UNIFIED_ID_FORMAT_INVALID)
    if (
        UNIFIED_ID_SEPARATOR in normalizedObject
        or UNIFIED_ID_SEPARATOR in normalizedExternal
    ):
        raise ValidationError(MSG_UNIFIED_ID_SEPARATOR_FORBIDDEN)
    unifiedId = (
        f"{UNIFIED_ID_PREFIX}{UNIFIED_ID_SEPARATOR}"
        f"{normalizedObject}{UNIFIED_ID_SEPARATOR}{normalizedExternal}"
    )
    if len(unifiedId) > UNIFIED_ID_MAX_LENGTH:
        raise ValidationError(MSG_UNIFIED_ID_FORMAT_INVALID)
    return unifiedId


def parseUnifiedId(unifiedId: str) -> tuple[str, str]:
    """反解统一 ID → ``(business_object, external_id)``；格式非法抛 ``ValidationError``。

    只做结构 / 字符 / 宽度校验，**不去空白、不做 NFC 归一**：调用方应传入
    ``buildUnifiedId`` 的产物；历史行按原值匹配，不能因为新规则被改写。
    """
    parts = (unifiedId or "").split(UNIFIED_ID_SEPARATOR)
    if (
        len(parts) != 3
        or parts[0] != UNIFIED_ID_PREFIX
        or not parts[1]
        or not parts[2]
    ):
        raise ValidationError(MSG_UNIFIED_ID_FORMAT_INVALID)
    _assertNoIllegalChars(parts[1])
    _assertNoIllegalChars(parts[2])
    if (
        len(parts[1]) > BUSINESS_OBJECT_MAX_LENGTH
        or len(parts[2]) > EXTERNAL_ID_MAX_LENGTH
        or len(unifiedId) > UNIFIED_ID_MAX_LENGTH
    ):
        raise ValidationError(MSG_UNIFIED_ID_FORMAT_INVALID)
    return parts[1], parts[2]
