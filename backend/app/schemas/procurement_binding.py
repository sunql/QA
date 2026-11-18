"""采购源绑定的 API DTO（M1-1.1）。

对应两张表（迁移 `0154`）：

- `procurement_source_binding`：类 → 数据源 / schema / 物理表；
- `procurement_property_binding`：属性 → 物理列 / 复合键序号 / 列级变换。

**写入一律先落候选**（`is_candidate = TRUE`）：确认动作必须由人显式触发
（决策 D-3：未经确认的候选不得被任何读路径消费）。
确认人取当前登录用户，**不接受客户端传入**（防伪造审计）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field, StringConstraints

from app.domain.schemas import CamelModel
from app.services.procurement_binding_validation import MAX_ALLOWLIST_ITEM_LENGTH

#: schema / 表名 / 列名的合法形态（与 `procurement_binding_validation` 的标识符规则一致，
#: 但 DTO 层要更早失败 ⇒ 先在 Pydantic 挡一道）。
_IDENTIFIER = r"^[A-Za-z_][A-Za-z0-9_]*$"

#: 白名单条目 = 一个列标识符。
#: ⚠️ 长度必须与库列宽一致（`ARRAY(String(100))`）—— PostgreSQL 对**数组元素超长
#: 是静默截断**（不是报错）⇒ 写时校验的白名单与落库的白名单会不是同一个对象。
#: 长度上限用**共享常量**（校验层也卡 ⇒ 不经 HTTP 的写入方同样被拒）。
_AllowlistItem = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_ALLOWLIST_ITEM_LENGTH, pattern=_IDENTIFIER),
]

#: 键序号的库列类型是 SMALLINT ⇒ DTO 必须与列宽对齐，否则越界值会在**查询绑定参数**
#: 时抛 DataError（500），而不是在边界上被拒（422）。
_MAX_KEY_ORDINAL = 32767

#: 白名单条目数上限（避免把一张表的所有列都放行成常态）。
_MAX_ALLOWLIST_ITEMS = 100


class SourceBindingCreate(CamelModel):
    """新增一条**候选**源绑定（类 → 源表）。"""

    class_id: int = Field(gt=0)
    datasource_id: int = Field(gt=0)
    schema_name: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    source_table: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    binding_role: str = Field(min_length=1, max_length=20)
    filter_expression: str | None = Field(default=None, max_length=2000)
    filter_allowlist: list[_AllowlistItem] = Field(
        default_factory=list, max_length=_MAX_ALLOWLIST_ITEMS
    )


class PropertyBindingCreate(CamelModel):
    """在源绑定下新增一条**候选**属性绑定（属性 → 列）。"""

    property_id: int = Field(gt=0)
    source_column: str = Field(min_length=1, max_length=100, pattern=_IDENTIFIER)
    key_ordinal: int | None = Field(default=None, ge=1, le=_MAX_KEY_ORDINAL)
    transform_expression: str | None = Field(default=None, max_length=2000)


class PropertyBindingRead(CamelModel):
    """属性绑定视图。"""

    id: int
    source_binding_id: int
    property_id: int
    property_name: str | None = None
    source_column: str
    key_ordinal: int | None = None
    transform_expression: str | None = None
    is_candidate: bool
    confirmed_by: str | None = None
    confirmed_time: datetime | None = None
    valid_from: datetime
    valid_to: datetime | None = None


class SourceBindingRead(CamelModel):
    """源绑定视图（含类名，便于前端一屏可读）。"""

    id: int
    class_id: int
    class_name: str | None = None
    datasource_id: int
    schema_name: str
    source_table: str
    binding_role: str
    filter_expression: str | None = None
    filter_allowlist: list[str] = Field(default_factory=list)
    is_candidate: bool
    confirmed_by: str | None = None
    confirmed_time: datetime | None = None
    valid_from: datetime
    valid_to: datetime | None = None
    created_by: str | None = None
    property_count: int = 0


class SourceBindingDetail(CamelModel):
    """源绑定详情 = 绑定本体 + 其属性绑定列表。"""

    binding: SourceBindingRead
    properties: list[PropertyBindingRead] = Field(default_factory=list)
