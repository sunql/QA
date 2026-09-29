"""M4 Report 模板 MVP（A8）：声明式报告模板包。

裁决（brief §实勘 1）：模板是版本化产物，存代码不存库——每模板一个模块
导出 ``TEMPLATE: dict``，注册表 ``REPORT_TEMPLATES`` 汇总；不做模板
CRUD / DB 表 / 管理 UI（YAGNI，蓝图 §21 3b/3c 再议）。

模板结构契约（每节）::

    {"sectionId", "title", "kind": "table"|"kpi_cards"|"text",
     "source": {"type": "kpi", "kpiCodes": [...]}
              | {"type": "supplier360", "supplierKey": "{{params.xxx}}", "part": ...}}

占位符只允许 ``{{params.xxx}}`` 整串形式，值经模板 ``paramsModel``
（Pydantic）校验后注入——参数永不拼接 SQL（数据绑定全部走既有服务）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.services.report_templates.monthly_ops_v1 import TEMPLATE as MONTHLY_OPS_V1
from app.services.report_templates.supplier_360_v1 import TEMPLATE as SUPPLIER_360_V1

REPORT_TEMPLATES: dict[str, dict[str, Any]] = {
    MONTHLY_OPS_V1["code"]: MONTHLY_OPS_V1,
    SUPPLIER_360_V1["code"]: SUPPLIER_360_V1,
}

# 前端模板选择器需要的元数据字段
_TEMPLATE_META_KEYS = ("code", "title", "description")


def paramsSchemaOf(model: type[BaseModel]) -> list[dict[str, Any]]:
    """把模板 paramsModel 转成前端可渲染的参数 schema（只读派生）。"""
    fields: list[dict[str, Any]] = []
    for name, field in model.model_fields.items():
        pattern = next(
            (m.pattern for m in field.metadata if hasattr(m, "pattern")), None
        )
        fields.append(
            {
                "name": name,
                "type": getattr(field.annotation, "__name__", "string"),
                "required": field.is_required(),
                "pattern": pattern,
            }
        )
    return fields


def listTemplateMetas() -> list[dict[str, Any]]:
    """模板元数据列表（供 GET /reports/templates），注册表顺序稳定。"""
    metas: list[dict[str, Any]] = []
    for template in REPORT_TEMPLATES.values():
        meta = {key: template[key] for key in _TEMPLATE_META_KEYS}
        meta["paramsSchema"] = paramsSchemaOf(template["paramsModel"])
        metas.append(meta)
    return metas


__all__ = [
    "REPORT_TEMPLATES",
    "listTemplateMetas",
    "paramsSchemaOf",
]
