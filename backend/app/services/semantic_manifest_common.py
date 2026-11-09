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
