"""语义编译器的共享原语（类引用解析 / 稳定排序 / 列表归一）。

编译器拆成三个文件的原因：本体核心（类/属性/JOIN）与指标/BO/域是两块独立关注点，
拆开后每个文件都能一屏读完。两侧都要用到的原语放这里，避免相互 import 成环。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.domain.models import OntologyClass


def sortBy(rows: Iterable[Any], *, key: Any) -> list[Any]:
    return sorted(rows, key=key)


def propertyNameSortKey(name: str | None) -> tuple[bool, str]:
    """属性排序键：`property_name` 升序（Unicode 码位）。

    **编译侧与渲染侧必须用同一个键**（A4）：编译侧按它产 manifest 顺序，
    渲染侧按它排直读侧的 ORM 物理行序 —— 两侧同键才能渲染出逐字节相同的
    schema 文本（否则开闸 = 静默换 prompt，见 M0.4 §3）。

    `None` 排末尾而不是抛错：DB 列 NOT NULL，出现 `None` 只可能是测试构造
    实体；渲染路径不能因为一个缺名属性整段挂掉（M0.4 §3.1 V4）。
    元组比较 `(False, "A") < (False, "B")` 与纯字符串比较同序 ⇒ 对真实数据
    行为与原来的 `key=name` **完全一致**。
    """
    return (name is None, name or "")


def joinKeySortKey(joinKey: str | None) -> tuple[bool, str]:
    """JOIN 排序键：`join_key` 升序（Unicode 码位）。

    与 `propertyNameSortKey` 同一理由：编译侧 `_compileJoins` 与渲染侧
    `_renderJoinLines` 必须同键。`join_key` 在 DB 里 NOT NULL + UNIQUE
    ⇒ 真实数据上无并列，`None` 分支只服务构造实体（排末尾，不抛错）。
    """
    return (joinKey is None, joinKey or "")


def asList(value: list[str] | None) -> list[str]:
    """None → []，其余复制一份（不把 DB 对象的列表引用直接暴露给调用方）。"""
    return list(value) if value else []


def requireClass(
    classId: int | None,
    allClassById: dict[int, OntologyClass],
    fieldName: str,
) -> OntologyClass:
    row = allClassById.get(classId) if classId is not None else None
    if row is None:
        raise ValueError(
            f"本体数据异常：{fieldName}={classId} 不存在于 ontology_class"
        )
    return row


def resolveActiveName(
    classId: int | None,
    allClassById: dict[int, OntologyClass],
    fieldName: str,
) -> str | None:
    """引用活跃类 → 类名；引用墓碑类 → None（目标已不在模型中）。"""
    if classId is None:
        return None
    row = allClassById.get(classId)
    if row is None:
        raise ValueError(f"本体数据异常：{fieldName}={classId} 不存在于 ontology_class")
    return None if row.valid_to is not None else row.class_name
