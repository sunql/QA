"""语义发布 Manifest 的 JSON Schema v1 + 校验器 + 规范化哈希（P1a / D7）。

三道闸，按顺序：

1. **结构闸**（`MANIFEST_SCHEMA`，JSON Schema draft 2020-12）：分区齐全、字段齐全、
   类型正确、枚举合法、**不允许未知字段**（`additionalProperties: false` —— 拼错的
   键会被静默忽略，那是发布评审最不该有的失败模式）。
2. **引用闸**（`_validateReferences`）：Schema 表达不了的跨分区约束 —— 类名 / 指标名 /
   域名必须存在、同名不得重复、`metrics[].kpiCodes` 与 `kpis[].metricName` 必须互相对上。
   编译器在**输入侧**已经校验过一次；这里在**产物侧**再验一次，因为回放历史快照时
   不会再经过编译器。
3. **哈希闸**（`manifestHash`）：规范化 JSON（键序稳定、无空白、`allow_nan=False`）
   的 SHA-256。同一个治理内容必须得到同样的十六进制串，跨环境、跨语言可复算。

Schema 演进规则：新增字段 / 新增枚举值 / 改类型 ⇒ `SCHEMA_VERSION` 必须 +1，
旧快照按自身 `schemaVersion` 选对应 Schema 校验，绝不偷偷放宽 v1。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_VERSION = "v1"
"""Manifest Schema 版本。编译器从这里取，避免两处各写各的。"""

_MAX_REPORTED_ERRORS = 10

_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}
_STRING_LIST: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
_NULLABLE_OBJECT: dict[str, Any] = {"type": ["object", "null"]}


def _enum(*values: str, nullable: bool = False) -> dict[str, Any]:
    allowed: list[Any] = list(values)
    if nullable:
        allowed.append(None)
    return {"enum": allowed}


def _object(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required if required is not None else list(properties),
        "properties": properties,
    }


_PROPERTY_SCHEMA = _object(
    {
        "propertyName": {"type": "string", "minLength": 1},
        "propertyAlias": _NULLABLE_STRING,
        "businessAliases": _STRING_LIST,
        "description": _NULLABLE_STRING,
        "dataType": {"type": "string", "minLength": 1},
        "isPrimaryKey": {"type": "boolean"},
        "isForeignKey": {"type": "boolean"},
        "refClassName": _NULLABLE_STRING,
        "sourceColumn": _NULLABLE_STRING,
        "unit": _NULLABLE_STRING,
        "allowedValues": _STRING_LIST,
        # 三态：null = 未约束，false = 明确允许为空。
        "isNotNull": {"type": ["boolean", "null"]},
        "minValue": _NULLABLE_STRING,
        "maxValue": _NULLABLE_STRING,
        "regexPattern": _NULLABLE_STRING,
    }
)

_CLASS_SCHEMA = _object(
    {
        "className": {"type": "string", "minLength": 1},
        "classAlias": _NULLABLE_STRING,
        "description": _NULLABLE_STRING,
        "sourceTable": _NULLABLE_STRING,
        # 取值来自 `app.domain.enums.ObjectType`（Master/Transaction/Reference/Event）。
        "objectType": _enum("Master", "Transaction", "Reference", "Event", nullable=True),
        "objectOwner": _NULLABLE_STRING,
        "parentClassName": _NULLABLE_STRING,
        "properties": {"type": "array", "items": _PROPERTY_SCHEMA},
    }
)

_JOIN_SCHEMA = _object(
    {
        "joinKey": {"type": "string", "minLength": 1},
        "sourceClassName": {"type": "string", "minLength": 1},
        "sourceColumns": _STRING_LIST,
        "targetClassName": {"type": "string", "minLength": 1},
        "targetColumns": _STRING_LIST,
        # join_type / relation_type 在库侧是自由字符串（实测 INNER / foreign_key），
        # 没有 Python 枚举作 SSOT ⇒ 这里不做枚举约束，否则会凭空冻结未定义的口径。
        "joinType": {"type": "string"},
        "relationType": {"type": "string"},
        "description": _NULLABLE_STRING,
    }
)

_MEASURE_SCHEMA = _object(
    {
        # 取值来自 `schemas._MEASURE_AGG_ALLOWED`（SUM/COUNT/AVG/COUNT_DISTINCT）。
        "agg": {"type": "string", "minLength": 1},
        "expr": _NULLABLE_STRING,
        "filter": _NULLABLE_STRING,
        # 编译器把内嵌 class_id 归一成 className（墓碑类 ⇒ null）。
        "className": _NULLABLE_STRING,
        "classAlias": _NULLABLE_STRING,
    },
    required=["agg", "className"],
)

_METRIC_SOURCE_SCHEMA = _object(
    {
        "className": {"type": "string", "minLength": 1},
        "role": _enum("NUMERATOR", "DENOMINATOR", "FILTER", "BASE"),
    }
)

_METRIC_SCHEMA = _object(
    {
        "metricName": {"type": "string", "minLength": 1},
        "metricAlias": _NULLABLE_STRING,
        "formula": {"type": "string", "minLength": 1},
        "aggFunction": {"type": "string", "minLength": 1},
        "metricType": _enum("MEASURE", "RATIO", "DIFFERENCE", "DERIVED"),
        "targetClassName": _NULLABLE_STRING,
        "dimensionDefaults": _NULLABLE_OBJECT,
        "grainKeys": _STRING_LIST,
        "aggregationOrder": _enum(
            "WEIGHTED_RATIO", "AVG_OF_RATIOS", "NONE", nullable=True
        ),
        "numeratorMeasure": {"anyOf": [_MEASURE_SCHEMA, {"type": "null"}]},
        "denominatorMeasure": {"anyOf": [_MEASURE_SCHEMA, {"type": "null"}]},
        "filterExpression": _NULLABLE_STRING,
        "status": _enum("DRAFT", "PUBLISHED"),
        "owner": _NULLABLE_STRING,
        "executionSource": _enum("COMPILED", "ESCAPE_HATCH", "NONE", nullable=True),
        "sources": {"type": "array", "items": _METRIC_SOURCE_SCHEMA},
        "kpiCodes": _STRING_LIST,
    }
)

_KPI_SCHEMA = _object(
    {
        "kpiCode": {"type": "string", "minLength": 1},
        "kpiName": {"type": "string", "minLength": 1},
        "businessDefinition": _NULLABLE_STRING,
        "formula": _NULLABLE_STRING,
        "numerator": _NULLABLE_STRING,
        "denominator": _NULLABLE_STRING,
        "grain": _NULLABLE_STRING,
        "unit": _NULLABLE_STRING,
        "grainKeys": _STRING_LIST,
        "numeratorColumns": _STRING_LIST,
        "denominatorColumns": _STRING_LIST,
        "owner": _NULLABLE_STRING,
        "version": {"type": "string"},
        "status": _enum("DRAFT", "PUBLISHED", "DEPRECATED"),
        "semanticKeywords": _STRING_LIST,
        "metricName": _NULLABLE_STRING,
        # 弱引用：KPI 可先于技术实现存在。id 是实例相关的，仅供追溯，
        # 运行期解析一律用 datasourceName。
        "datasourceId": {"type": ["integer", "null"]},
        "datasourceName": _NULLABLE_STRING,
    }
)

_BUSINESS_OBJECT_SCHEMA = _object(
    {
        "code": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "headerClassName": {"type": "string", "minLength": 1},
        "primaryDomainCode": {"type": "string", "minLength": 1},
        "attachedDomainCodes": _STRING_LIST,
        "graphLabel": _NULLABLE_STRING,
        "description": _NULLABLE_STRING,
    }
)

_DOMAIN_SCHEMA = _object(
    {
        "code": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "description": _NULLABLE_STRING,
        "enabled": {"type": "boolean"},
    }
)

MANIFEST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://qa-system.local/schemas/semantic-manifest/v1.json",
    "title": "Semantic Manifest v1",
    **_object(
        {
            "schemaVersion": {"const": SCHEMA_VERSION},
            "domains": {"type": "array", "items": _DOMAIN_SCHEMA},
            "classes": {"type": "array", "items": _CLASS_SCHEMA},
            "joins": {"type": "array", "items": _JOIN_SCHEMA},
            "metrics": {"type": "array", "items": _METRIC_SCHEMA},
            "kpis": {"type": "array", "items": _KPI_SCHEMA},
            "businessObjects": {"type": "array", "items": _BUSINESS_OBJECT_SCHEMA},
        }
    ),
}


class ManifestSchemaError(ValueError):
    """Manifest 不满足 v1 契约（结构 / 引用 / 哈希前校验）。"""


def validateManifest(manifest: Any) -> None:
    """校验并返回 None；不合法则抛 `ManifestSchemaError`（一次报多处）。"""
    errors = _schemaErrors(manifest)
    if not errors:
        errors = _referenceErrors(manifest)
    if errors:
        raise ManifestSchemaError(_formatErrors(errors))


def canonicalManifestJson(manifest: dict[str, Any]) -> str:
    """规范化 JSON：键序稳定、无空白、保留中文、拒绝 NaN/Infinity。

    `allow_nan=False` 很重要：NaN 不是合法 JSON，放过去会让哈希在其他语言里算不出来。
    """
    return json.dumps(
        manifest,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def manifestHash(manifest: dict[str, Any]) -> str:
    """`semantic_release.manifest_hash` 的唯一算法：规范化 JSON 的 SHA-256。"""
    return hashlib.sha256(canonicalManifestJson(manifest).encode("utf-8")).hexdigest()


def _schemaErrors(manifest: Any) -> list[str]:
    validator = Draft202012Validator(MANIFEST_SCHEMA)
    found = sorted(validator.iter_errors(manifest), key=lambda err: list(err.absolute_path))
    return [f"{_formatPath(err.absolute_path)}：{err.message}" for err in found]


def _referenceErrors(manifest: Any) -> list[str]:
    """跨分区引用校验（Schema 表达不了的部分）。"""
    errors: list[str] = []
    classNames = _collectUnique(manifest["classes"], "className", "classes", errors)
    metricNames = _collectUnique(manifest["metrics"], "metricName", "metrics", errors)
    kpiCodes = _collectUnique(manifest["kpis"], "kpiCode", "kpis", errors)
    domainCodes = _collectUnique(manifest["domains"], "code", "domains", errors)

    for cls in manifest["classes"]:
        where = f"classes[{cls['className']}]"
        _checkRef(cls["parentClassName"], classNames, f"{where}.parentClassName", errors)
        _collectUnique(cls["properties"], "propertyName", where, errors)
        for prop in cls["properties"]:
            _checkRef(
                prop["refClassName"],
                classNames,
                f"{where}.properties[{prop['propertyName']}].refClassName",
                errors,
            )

    for join in manifest["joins"]:
        where = f"joins[{join['joinKey']}]"
        _checkRef(join["sourceClassName"], classNames, f"{where}.sourceClassName", errors)
        _checkRef(join["targetClassName"], classNames, f"{where}.targetClassName", errors)

    for metric in manifest["metrics"]:
        where = f"metrics[{metric['metricName']}]"
        _checkRef(metric["targetClassName"], classNames, f"{where}.targetClassName", errors)
        for field in ("numeratorMeasure", "denominatorMeasure"):
            measure = metric[field]
            if measure is not None:
                _checkRef(measure["className"], classNames, f"{where}.{field}.className", errors)
        for source in metric["sources"]:
            _checkRef(source["className"], classNames, f"{where}.sources", errors)
        for code in metric["kpiCodes"]:
            if code not in kpiCodes:
                errors.append(f"{where}.kpiCodes：KPI {code!r} 不存在于 kpis")

    for kpi in manifest["kpis"]:
        metricName = kpi["metricName"]
        if metricName is not None and metricName not in metricNames:
            errors.append(
                f"kpis[{kpi['kpiCode']}].metricName：指标 {metricName!r} 不存在于 metrics"
            )

    for businessObject in manifest["businessObjects"]:
        where = f"businessObjects[{businessObject['code']}]"
        _checkRef(
            businessObject["headerClassName"],
            classNames,
            f"{where}.headerClassName",
            errors,
        )
        _checkRef(
            businessObject["primaryDomainCode"],
            domainCodes,
            f"{where}.primaryDomainCode",
            errors,
        )
        for code in businessObject["attachedDomainCodes"]:
            _checkRef(code, domainCodes, f"{where}.attachedDomainCodes", errors)

    return errors


def _collectUnique(
    rows: list[dict[str, Any]],
    key: str,
    where: str,
    errors: list[str],
) -> set[str]:
    seen: set[str] = set()
    for row in rows:
        name = row[key]
        if name in seen:
            errors.append(f"{where}.{key} 重复：{name!r}")
        seen.add(name)
    return seen


def _checkRef(value: Any, allowed: set[str], where: str, errors: list[str]) -> None:
    if value is not None and value not in allowed:
        errors.append(f"{where}：{value!r} 不存在")


def _formatPath(path: Any) -> str:
    text = ""
    for part in path:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else str(part)
    return text or "<root>"


def _formatErrors(errors: list[str]) -> str:
    shown = errors[:_MAX_REPORTED_ERRORS]
    suffix = (
        f"\n…共 {len(errors)} 处，只显示前 {_MAX_REPORTED_ERRORS} 处"
        if len(errors) > _MAX_REPORTED_ERRORS
        else ""
    )
    return "Manifest 不满足 v1 契约：\n" + "\n".join(shown) + suffix
