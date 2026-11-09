"""语义发布 Manifest 的 JSON Schema v1 + 校验器 + 规范化哈希（P1a / D7）。

三道闸，按顺序：

1. **结构闸**（`MANIFEST_SCHEMA`，JSON Schema draft 2020-12）：分区齐全、字段齐全、
   类型正确、枚举合法、**不允许未知字段**（`additionalProperties: false` —— 拼错的
   键会被静默忽略，那是发布评审最不该有的失败模式）。
2. **引用闸**（`_referenceErrors`）：Schema 表达不了的跨分区约束 —— 类名 / 指标名 /
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
from itertools import islice
from typing import Any

from jsonschema import Draft202012Validator

# 度量聚合白名单的单一事实源在写入侧（Pydantic 的 `MeasureSpecSchema`）。
# 校验器是回放历史/人工编辑快照的最后一道闸，必须用同一份白名单。
from app.domain.schemas import _MEASURE_AGG_ALLOWED

SCHEMA_VERSION = "v1"
"""Manifest Schema 版本。编译器从这里取，避免两处各写各的。"""

_MAX_REPORTED_ERRORS = 10
_MAX_NAME_LENGTH = 200
_MAX_TEXT_LENGTH = 4000


_NULLABLE_STRING: dict[str, Any] = {
    "type": ["string", "null"],
    "maxLength": _MAX_TEXT_LENGTH,
}
_NAME: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": _MAX_NAME_LENGTH}
# 公式/表达式可比名字长得多（真实库最长 formula 上千字符）。
_TEXT: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": _MAX_TEXT_LENGTH,
}
_NULLABLE_NAME: dict[str, Any] = {
    "type": ["string", "null"],
    "minLength": 1,
    "maxLength": _MAX_NAME_LENGTH,
}
# 体量上限是纵深防御：D9 的 API 层还必须有字节级上限，两层缺一不可。
_STRING_LIST: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "maxLength": _MAX_NAME_LENGTH},
    "maxItems": 1000,
}
# 唯一允许的自由对象：扁平字符串映射（否则是任意深/大的体量杠杆）。
_NULLABLE_OBJECT: dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": {"type": "string", "maxLength": _MAX_NAME_LENGTH},
    "maxProperties": 50,
}


def _array(items: dict[str, Any], *, maxItems: int) -> dict[str, Any]:
    return {"type": "array", "items": items, "maxItems": maxItems}


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
        "propertyName": _NAME,
        "propertyAlias": _NULLABLE_STRING,
        "businessAliases": _STRING_LIST,
        "description": _NULLABLE_STRING,
        "dataType": _NAME,
        "isPrimaryKey": {"type": "boolean"},
        "isForeignKey": {"type": "boolean"},
        "refClassName": _NULLABLE_NAME,
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
        "className": _NAME,
        "classAlias": _NULLABLE_STRING,
        "description": _NULLABLE_STRING,
        "sourceTable": _NULLABLE_STRING,
        # 取值来自 `app.domain.enums.ObjectType`（Master/Transaction/Reference/Event）。
        "objectType": _enum("Master", "Transaction", "Reference", "Event", nullable=True),
        "objectOwner": _NULLABLE_STRING,
        "parentClassName": _NULLABLE_NAME,
        "properties": _array(_PROPERTY_SCHEMA, maxItems=500),
    }
)

_JOIN_SCHEMA = _object(
    {
        "joinKey": _NAME,
        "sourceClassName": _NAME,
        "sourceColumns": _STRING_LIST,
        "targetClassName": _NAME,
        "targetColumns": _STRING_LIST,
        # join_type / relation_type 在库侧是自由字符串（实测 INNER / foreign_key），
        # 没有 Python 枚举作 SSOT ⇒ 这里不做枚举约束，否则会凭空冻结未定义的口径。
        "joinType": {"type": "string", "maxLength": _MAX_NAME_LENGTH},
        "relationType": {"type": "string", "maxLength": _MAX_NAME_LENGTH},
        "description": _NULLABLE_STRING,
    }
)

_MEASURE_SCHEMA = _object(
    {
        # 与写入侧 `MeasureSpecSchema` 共用白名单（SUM/COUNT/AVG/COUNT_DISTINCT）。
        "agg": _enum(*sorted(_MEASURE_AGG_ALLOWED)),
        "expr": _NULLABLE_STRING,
        "filter": _NULLABLE_STRING,
        # 编译器把内嵌 class_id 归一成 className（墓碑类 ⇒ null）。
        "className": _NULLABLE_NAME,
        "classAlias": _NULLABLE_STRING,
    },
    required=["agg", "className"],
)

_METRIC_SOURCE_SCHEMA = _object(
    {
        "className": _NAME,
        "role": _enum("NUMERATOR", "DENOMINATOR", "FILTER", "BASE"),
    }
)

_METRIC_SCHEMA = _object(
    {
        "metricName": _NAME,
        "metricAlias": _NULLABLE_STRING,
        "formula": _TEXT,
        "aggFunction": _NAME,
        "metricType": _enum("MEASURE", "RATIO", "DIFFERENCE", "DERIVED"),
        "targetClassName": _NULLABLE_NAME,
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
        "sources": _array(_METRIC_SOURCE_SCHEMA, maxItems=50),
        "kpiCodes": _array(_NAME, maxItems=100),
    }
)

_KPI_SCHEMA = _object(
    {
        "kpiCode": _NAME,
        "kpiName": _NAME,
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
        "metricName": _NULLABLE_NAME,
        # 弱引用：KPI 可先于技术实现存在。数据源只记名字 —— id 是自增主键，
        # 入档会让同一治理内容在不同环境编译出不同字节（与 measure.class_id 同类）。
        "datasourceName": _NULLABLE_STRING,
    }
)

_BUSINESS_OBJECT_SCHEMA = _object(
    {
        "code": _NAME,
        "name": _NAME,
        "headerClassName": _NAME,
        "primaryDomainCode": _NAME,
        "attachedDomainCodes": _array(_NAME, maxItems=200),
        "graphLabel": _NULLABLE_STRING,
        "description": _NULLABLE_STRING,
    }
)

_DOMAIN_SCHEMA = _object(
    {
        "code": _NAME,
        "name": _NAME,
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
            "domains": _array(_DOMAIN_SCHEMA, maxItems=1000),
            "classes": _array(_CLASS_SCHEMA, maxItems=5000),
            "joins": _array(_JOIN_SCHEMA, maxItems=20000),
            "metrics": _array(_METRIC_SCHEMA, maxItems=2000),
            "kpis": _array(_KPI_SCHEMA, maxItems=2000),
            "businessObjects": _array(_BUSINESS_OBJECT_SCHEMA, maxItems=1000),
        }
    ),
}


class ManifestSchemaError(ValueError):
    """Manifest 不满足 v1 契约（结构 / 引用 / 哈希前校验）。"""


def validateManifest(manifest: Any) -> None:
    """校验并返回 None；不合法则抛 `ManifestSchemaError`（一次报多处）。"""
    errors = _schemaErrors(manifest) or _referenceErrors(manifest)
    if errors:
        raise ManifestSchemaError(_formatErrors(errors))
    _ensureCanonicalizable(manifest)


def _ensureCanonicalizable(manifest: Any) -> None:
    """第三道闸的前置：能被规范化序列化，才谈得上哈希。

    孤立代理项（`"\\ud800"`）能通过 `json.loads` 与 Schema 的类型检查，却会在
    UTF-8 编码时抛 `UnicodeEncodeError`。不在这里转成契约错误，调用方按
    `ManifestSchemaError` 捕获就会漏掉，变成 500。
    """
    try:
        # 走完整哈希路径：UTF-8 编码失败（孤立代理项）只在 `.encode()` 处暴露，
        # 单看 `canonicalManifestJson` 不会报错。
        manifestHash(manifest)
    except (ValueError, TypeError) as exc:
        raise ManifestSchemaError(f"Manifest 无法规范化序列化：{exc}") from exc


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
    """取前 N 条结构错误。

    用 `islice` 而不是「先物化全部再截断」：10 万条非法元素时，后者会先生成
    10 万个错误对象/字符串（内存随请求体线性增长），只为打印 10 行。
    """
    validator = Draft202012Validator(MANIFEST_SCHEMA)
    found = islice(validator.iter_errors(manifest), _MAX_REPORTED_ERRORS)
    return [f"{_formatPath(err.absolute_path)}：{err.message}" for err in found]


def _referenceErrors(manifest: Any) -> list[str]:
    """跨分区引用校验（Schema 表达不了的部分）。"""
    errors: list[str] = []
    classNames = _collectUnique(manifest["classes"], "className", "classes", errors)
    metricNames = _collectUnique(manifest["metrics"], "metricName", "metrics", errors)
    kpiCodes = _collectUnique(manifest["kpis"], "kpiCode", "kpis", errors)
    domainCodes = _collectUnique(manifest["domains"], "code", "domains", errors)
    _collectUnique(manifest["joins"], "joinKey", "joins", errors)
    _collectUnique(manifest["businessObjects"], "code", "businessObjects", errors)

    _classSectionErrors(manifest["classes"], classNames, errors)
    _joinSectionErrors(manifest["joins"], classNames, errors)
    _metricSectionErrors(manifest["metrics"], classNames, kpiCodes, errors)
    _kpiSectionErrors(manifest["kpis"], metricNames, errors)
    _businessObjectSectionErrors(
        manifest["businessObjects"], classNames, domainCodes, errors
    )
    _reverseIndexErrors(manifest["metrics"], manifest["kpis"], errors)
    return errors


def _classSectionErrors(
    classes: list[dict[str, Any]], classNames: set[str], errors: list[str]
) -> None:
    for cls in classes:
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


def _joinSectionErrors(
    joins: list[dict[str, Any]], classNames: set[str], errors: list[str]
) -> None:
    for join in joins:
        where = f"joins[{join['joinKey']}]"
        _checkRef(join["sourceClassName"], classNames, f"{where}.sourceClassName", errors)
        _checkRef(join["targetClassName"], classNames, f"{where}.targetClassName", errors)


def _metricSectionErrors(
    metrics: list[dict[str, Any]],
    classNames: set[str],
    kpiCodes: set[str],
    errors: list[str],
) -> None:
    for metric in metrics:
        where = f"metrics[{metric['metricName']}]"
        _checkRef(metric["targetClassName"], classNames, f"{where}.targetClassName", errors)
        for field in ("numeratorMeasure", "denominatorMeasure"):
            measure = metric[field]
            if measure is not None:
                _checkRef(
                    measure["className"], classNames, f"{where}.{field}.className", errors
                )
        for source in metric["sources"]:
            _checkRef(source["className"], classNames, f"{where}.sources", errors)
        _checkUniqueList(metric["kpiCodes"], f"{where}.kpiCodes", errors)
        for code in metric["kpiCodes"]:
            if code not in kpiCodes:
                _append(errors, f"{where}.kpiCodes：KPI {code!r} 不存在于 kpis")


def _kpiSectionErrors(
    kpis: list[dict[str, Any]], metricNames: set[str], errors: list[str]
) -> None:
    for kpi in kpis:
        metricName = kpi["metricName"]
        if metricName is not None and metricName not in metricNames:
            _append(
                errors,
                f"kpis[{kpi['kpiCode']}].metricName：指标 {metricName!r} 不存在于 metrics",
            )


def _businessObjectSectionErrors(
    businessObjects: list[dict[str, Any]],
    classNames: set[str],
    domainCodes: set[str],
    errors: list[str],
) -> None:
    for businessObject in businessObjects:
        where = f"businessObjects[{businessObject['code']}]"
        _checkRef(
            businessObject["headerClassName"], classNames, f"{where}.headerClassName", errors
        )
        _checkRef(
            businessObject["primaryDomainCode"],
            domainCodes,
            f"{where}.primaryDomainCode",
            errors,
        )
        _checkUniqueList(
            businessObject["attachedDomainCodes"], f"{where}.attachedDomainCodes", errors
        )
        for code in businessObject["attachedDomainCodes"]:
            _checkRef(code, domainCodes, f"{where}.attachedDomainCodes", errors)


def _reverseIndexErrors(
    metrics: list[dict[str, Any]], kpis: list[dict[str, Any]], errors: list[str]
) -> None:
    """`metrics[].kpiCodes` 与 `kpis[].metricName` 必须**双向闭合**。

    只校验「名字存在」会放行错配：两侧都指向真实存在的对象，但指的是不同的指标。
    """
    metricByName = {metric["metricName"]: metric for metric in metrics}
    kpiByCode = {kpi["kpiCode"]: kpi for kpi in kpis}
    for metric in metrics:
        for code in metric["kpiCodes"]:
            kpi = kpiByCode.get(code)
            if kpi is not None and kpi["metricName"] != metric["metricName"]:
                _append(
                    errors,
                    f"metrics[{metric['metricName']}].kpiCodes：KPI {code!r} 指向指标 "
                    f"{kpi['metricName']!r}，不一致",
                )
    for kpi in kpis:
        metricName = kpi["metricName"]
        if metricName is None:
            continue
        metric = metricByName.get(metricName)
        if metric is not None and kpi["kpiCode"] not in metric["kpiCodes"]:
            _append(
                errors,
                f"kpis[{kpi['kpiCode']}].metricName：指标 {metricName!r} 的 kpiCodes "
                f"未包含 {kpi['kpiCode']!r}，不一致",
            )


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
            _append(errors, f"{where}.{key} 重复：{name!r}")
        seen.add(name)
    return seen


def _checkUniqueList(values: list[str], where: str, errors: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            _append(errors, f"{where} 重复：{value!r}")
        seen.add(value)


def _checkRef(value: Any, allowed: set[str], where: str, errors: list[str]) -> None:
    if value is not None and value not in allowed:
        _append(errors, f"{where}：{value!r} 不存在")


def _append(errors: list[str], message: str) -> None:
    """错误收集上限：与展示上限一致，避免为打印 10 行而缓存整份输入的错误。"""
    if len(errors) < _MAX_REPORTED_ERRORS:
        errors.append(message)


def _formatPath(path: Any) -> str:
    text = ""
    for part in path:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else str(part)
    return text or "<root>"


def _formatErrors(errors: list[str]) -> str:
    return "Manifest 不满足 v1 契约：\n" + "\n".join(errors)
