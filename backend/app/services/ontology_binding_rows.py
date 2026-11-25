"""运行时本体绑定读路径的**行类型契约**（2026-11-25，P5 到货跟踪）。

为什么单独一个模块
------------------
读路径要把三类行从 SQL 层搬到纯函数层（`ontology_binding_resolver._compileBinding`）。
这层搬运的形状**必须**与编译层认定的形状一致 —— 之前用「一个扁平 list + 元组长度」
表达形状，列数一变就会静默改道（评审 Minor #4）。
⇒ 用 `NamedTuple` 显式声明三类行：编译期就能分派，长度变化不会改道。

行的字段与库列的对应
--------------------
- `ClassRow` ← `ontology_class`（id / class_name / valid_to）
- `SourceBindingRow` ← `procurement_source_binding`
  （id / datasource_id / schema_name / source_table / binding_role /
   is_candidate / valid_to / valid_from）
- `PropertyBindingRow` ← `ontology_property` × `procurement_property_binding`
  （property id / property_name / source_column / class_id /
   source_binding_id / valid_from）

「已生效」的三条判据（源绑定与属性绑定同口径，与
`semantic_compiler_bindings._isEffectiveBinding` 一致）：
`is_candidate IS FALSE` 且 `valid_to IS NULL` 且 `valid_from <= now()`。
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple


class ClassRow(NamedTuple):
    """本体类行。"""

    classId: int
    className: str
    validTo: datetime | None


class SourceBindingRow(NamedTuple):
    """源绑定行（本体类 → 数据源 → 物理表）。"""

    sourceBindingId: int
    datasourceId: int
    schemaName: str
    sourceTable: str
    bindingRole: str
    isCandidate: bool
    validTo: datetime | None
    validFrom: datetime | None


class PropertyBindingRow(NamedTuple):
    """属性绑定行（本体属性 → 物理列）。"""

    propertyId: int
    propertyName: str
    sourceColumn: str
    classId: int
    sourceBindingId: int
    validFrom: datetime | None


#: 读路径一次读回的三类行（`classRow` 为空 = 类不存在）。
BindingRows = tuple[
    ClassRow | None,
    tuple[SourceBindingRow, ...],
    tuple[PropertyBindingRow, ...],
]
