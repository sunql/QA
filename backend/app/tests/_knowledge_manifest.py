"""P4 单测共用的**发布快照样本**。

为什么单独放一个模块：域切片、图谱路、指标路三组用例要打的是**同一份**快照 ——
各写一份会出现「切片按 A 口径、召回按 B 口径」的漂移，而这种漂移在集成测试里
表现为「路由返回 0 条」，很难定位。

样本刻意覆盖这些形状：
- 两个域（`PROCUREMENT` / `QUALITY`）⇒ 域切片有东西可切；
- 父类关系（`PurchaseOrder.parentClassName = Supplier`）⇒ 父类闭包可验；
- 跨域 JOIN（`iqc_po`）⇒ 边界可验；
- 指标两种锚点（`targetClassName` / `numeratorMeasure.className`）；
- KPI ↔ 指标双向映射（`metrics[].kpiCodes` ↔ `kpis[].metricName`）；
- 属性的 `propertyAlias` / `businessAliases`（属性级召回的入口）。
"""

from __future__ import annotations

from typing import Any

from app.domain.semantic_manifest_schema import SCHEMA_VERSION


def _property(
    name: str,
    alias: str | None,
    businessAliases: list[str] | None = None,
    dataType: str = "varchar",
    description: str | None = None,
) -> dict[str, Any]:
    return {
        "propertyName": name,
        "propertyAlias": alias,
        "businessAliases": businessAliases or [],
        "description": description,
        "dataType": dataType,
        "isPrimaryKey": False,
        "isForeignKey": False,
        "refClassName": None,
        "sourceColumn": name.upper(),
        "unit": None,
        "allowedValues": [],
        "isNotNull": None,
        "minValue": None,
        "maxValue": None,
        "regexPattern": None,
    }


def _class(
    name: str,
    alias: str,
    *,
    parent: str | None = None,
    objectType: str = "Master",
    properties: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "className": name,
        "classAlias": alias,
        "description": f"{alias}主数据",
        "sourceTable": f"T_{name.upper()}",
        "objectType": objectType,
        "objectOwner": None,
        "parentClassName": parent,
        "properties": properties or [],
    }


def _businessObject(
    code: str,
    name: str,
    headerClassName: str,
    primaryDomainCode: str,
    attachedDomainCodes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "name": name,
        "headerClassName": headerClassName,
        "primaryDomainCode": primaryDomainCode,
        "attachedDomainCodes": attachedDomainCodes or [],
        "graphLabel": headerClassName,
        "description": None,
    }


def _metric(
    metricName: str,
    metricAlias: str,
    targetClassName: str | None,
    kpiCodes: list[str],
    *,
    metricType: str = "MEASURE",
    numeratorMeasure: dict[str, Any] | None = None,
    denominatorMeasure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "metricName": metricName,
        "metricAlias": metricAlias,
        "formula": f"{metricAlias} 的计算口径",
        "aggFunction": "AVG",
        "metricType": metricType,
        "targetClassName": targetClassName,
        "dimensionDefaults": None,
        "grainKeys": [],
        "aggregationOrder": "NONE",
        "numeratorMeasure": numeratorMeasure,
        "denominatorMeasure": denominatorMeasure,
        "filterExpression": None,
        "status": "PUBLISHED",
        "owner": "data-owner",
        "executionSource": "COMPILED",
        "sources": [],
        "kpiCodes": kpiCodes,
    }


def _kpi(
    kpiCode: str,
    kpiName: str,
    metricName: str | None,
    semanticKeywords: list[str],
) -> dict[str, Any]:
    return {
        "kpiCode": kpiCode,
        "kpiName": kpiName,
        "businessDefinition": None,
        "formula": None,
        "numerator": None,
        "denominator": None,
        "grain": None,
        "unit": "%",
        "grainKeys": [],
        "numeratorColumns": [],
        "denominatorColumns": [],
        "owner": "data-owner",
        "version": "1",
        "status": "PUBLISHED",
        "semanticKeywords": semanticKeywords,
        "metricName": metricName,
        "datasourceName": None,
    }


def sampleManifest() -> dict[str, Any]:
    """一份跨两个域的发布快照样本（每次调用返回**新字典**，测试互不污染）。

    版本取 `SCHEMA_VERSION`（当前契约），并带齐 v2 的 `sourceBindings` 段
    ⇒ 样本代表「当前契约的合法 manifest」，契约升版时只需改这一处。
    """
    return {
        "schemaVersion": SCHEMA_VERSION,
        "domains": [
            {
                "code": "PROCUREMENT",
                "name": "采购",
                "description": "采购域",
                "enabled": True,
            },
            {
                "code": "QUALITY",
                "name": "质量",
                "description": "质量域",
                "enabled": True,
            },
        ],
        "classes": [
            _class(
                "Supplier",
                "供应商",
                properties=[
                    _property("delivery_days", "交付天数", ["交期"]),
                    _property("supplier_name", "供应商名称"),
                ],
            ),
            _class(
                "PurchaseOrder",
                "采购订单",
                parent="Supplier",
                objectType="Transaction",
                properties=[
                    _property("po_no", "采购订单号"),
                    _property("order_date", "下单日期"),
                ],
            ),
            _class(
                "InspectionRecord",
                "检验记录",
                objectType="Transaction",
                properties=[_property("pass_qty", "合格数量", dataType="int")],
            ),
        ],
        "joins": [
            {
                "joinKey": "po_supplier",
                "sourceClassName": "PurchaseOrder",
                "sourceColumns": ["SUPPLIER_ID"],
                "targetClassName": "Supplier",
                "targetColumns": ["ID"],
                "joinType": "INNER",
                "relationType": "foreign_key",
                "description": "采购订单关联供应商",
            },
            {
                "joinKey": "iqc_po",
                "sourceClassName": "InspectionRecord",
                "sourceColumns": ["PO_ID"],
                "targetClassName": "PurchaseOrder",
                "targetColumns": ["ID"],
                "joinType": "INNER",
                "relationType": "foreign_key",
                "description": "检验记录关联采购订单",
            },
        ],
        "metrics": [
            _metric(
                "supplier_delivery_days",
                "供应商交付天数",
                "Supplier",
                ["KPI_DELIVERY"],
            ),
            _metric(
                "po_amount",
                "采购订单金额",
                None,
                ["KPI_PO_AMOUNT"],
                metricType="MEASURE",
                numeratorMeasure={
                    "agg": "SUM",
                    "expr": None,
                    "filter": None,
                    "className": "PurchaseOrder",
                    "classAlias": None,
                },
                denominatorMeasure=None,
            ),
            _metric(
                "iqc_pass_rate",
                "来料合格率",
                "InspectionRecord",
                ["KPI_IQC"],
                metricType="RATIO",
            ),
        ],
        "kpis": [
            _kpi(
                "KPI_DELIVERY",
                "供应商交付及时率",
                "supplier_delivery_days",
                ["交付", "及时率"],
            ),
            _kpi("KPI_PO_AMOUNT", "采购订单金额", "po_amount", ["订单金额"]),
            _kpi("KPI_IQC", "来料合格率", "iqc_pass_rate", ["合格率"]),
        ],
        "businessObjects": [
            _businessObject("SUPPLIER", "供应商", "Supplier", "PROCUREMENT"),
            _businessObject(
                "PURCHASE_ORDER", "采购订单", "PurchaseOrder", "PROCUREMENT"
            ),
            _businessObject(
                "INSPECTION_RECORD", "检验记录", "InspectionRecord", "QUALITY"
            ),
        ],
        "sourceBindings": [],
    }
