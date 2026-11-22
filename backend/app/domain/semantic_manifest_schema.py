"""语义发布 Manifest 的 JSON Schema（v1 / v2）+ 校验器 + 规范化哈希（P1a / D7；v2 = M1-1.3）。

三道闸，按顺序：

1. **结构闸**（`MANIFEST_SCHEMAS`，JSON Schema draft 2020-12）：分区齐全、字段齐全、
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

**v2（M1-1.3 / 决策 D-9）**：新增 `sourceBindings` 分区（采购源绑定）。
- **按版本分发**：`validateManifest` 先看 `manifest.schemaVersion`，
  命中 `MANIFEST_SCHEMAS` 的哪一版就用哪一版的 Schema + 引用闸；
  **未知版本直接拒绝**（不放行、不猜）。
- v1 快照**继续按 v1 校验**（历史快照不许被新契约判死）。
- 绑定分区**只收已确认且当前生效**的绑定（D-3：候选不是结论）；
  且**不含时间戳与自增 id** ⇒ 两次编译字节一致。
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

# 版本字面量住在**无依赖叶子模块**里（`app/domain/semantic_manifest_version.py`）：
# ORM 侧（`semantic_release_models`）也要用它，而 ORM 侧不能 import 本模块
# （`app.domain.schemas` → services → `app.domain.models` 会形成 import 环）。
# 这里 re-export，历史调用方（`from app.domain.semantic_manifest_schema import
# SCHEMA_VERSION`）不受影响。
from app.domain.semantic_manifest_version import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_V1,
    SCHEMA_VERSION_V2,
)

# 契约允许的**指标条数上限**（`metrics` 数组的 maxItems）。
# ⚠️ 公开常量：发布校验项的条数上限要**从它推导**（见
# `app/services/semantic_release_checks.py` 的 `MAX_CHECKS_PER_RELEASE`）——
# 写死一个上限就会出现「合法 manifest 也会被截断」⇒ 静默丢掉 ERROR 结论行。
MAX_MANIFEST_METRICS = 2000

# v2 契约允许的**源绑定条数上限**与**单绑定属性数上限**。
# ⚠️ 同上：校验项条数上限要从它们推导（每绑定最多 2 条绑定级校验项）。
MAX_MANIFEST_SOURCE_BINDINGS = 2000
MAX_MANIFEST_BINDING_PROPERTIES = 500

# 契约允许的 **KPI 条数上限**。`MET-002` 逐 KPI 出一条校验项
# ⇒ 发布校验项上限必须把它算进去（同样不许写死）。
MAX_MANIFEST_KPIS = 2000

_MAX_REPORTED_ERRORS = 10
_MAX_NAME_LENGTH = 200
_MAX_TEXT_LENGTH = 4000
#: 单条错误消息的上限（jsonschema 会把被拒的值原样嵌进消息 ⇒ 必须截断）。
_MAX_ERROR_MESSAGE_LENGTH = 500


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

# ---- v2 新增：采购源绑定分区 ----
#
# 为什么单独一个分区而不是折进 `classes[].sourceTable` / `properties[].sourceColumn`：
# 绑定是「类 →（数据源, schema, 表）」的**一对多**映射，还带过滤表达式、变换表达式、
# 键序号与逐属性映射 —— 折进 v1 字段会把这些真信息丢掉（快照失真），
# 而失真的快照比没有快照更危险。
_SOURCE_BINDING_PROPERTY_SCHEMA = _object(
    {
        "propertyName": _NAME,
        "sourceColumn": _NAME,
        # 复合键的序号（1..n）；非键属性为 null。
        "keyOrdinal": {"type": ["integer", "null"], "minimum": 1},
        "transformExpression": _NULLABLE_STRING,
    }
)

_SOURCE_BINDING_SCHEMA = _object(
    {
        "className": _NAME,
        # 按**名字**记数据源/schema，不记 id：id 是自增主键，进快照会让同一治理内容
        # 在不同环境编译出不同字节（`measure.class_id` / `kpis.datasourceId` 已各犯过一次）。
        "datasourceName": _NULLABLE_NAME,
        "schemaName": _NULLABLE_NAME,
        "sourceTable": _NAME,
        "bindingRole": _enum("BASE", "NUMERATOR", "DENOMINATOR", "FILTER"),
        "filterExpression": _NULLABLE_STRING,
        "filterAllowlist": _STRING_LIST,
        "properties": _array(
            _SOURCE_BINDING_PROPERTY_SCHEMA, maxItems=MAX_MANIFEST_BINDING_PROPERTIES
        ),
    }
)

MANIFEST_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://qa-system.local/schemas/semantic-manifest/v1.json",
    "title": "Semantic Manifest v1",
    **_object(
        {
            "schemaVersion": {"const": SCHEMA_VERSION_V1},
            "domains": _array(_DOMAIN_SCHEMA, maxItems=1000),
            "classes": _array(_CLASS_SCHEMA, maxItems=5000),
            "joins": _array(_JOIN_SCHEMA, maxItems=20000),
            "metrics": _array(_METRIC_SCHEMA, maxItems=MAX_MANIFEST_METRICS),
            "kpis": _array(_KPI_SCHEMA, maxItems=MAX_MANIFEST_KPIS),
            "businessObjects": _array(_BUSINESS_OBJECT_SCHEMA, maxItems=1000),
        }
    ),
}


def _deriveV2(v1: dict[str, Any]) -> dict[str, Any]:
    """v1 + `sourceBindings` 分区（**从 v1 派生**，不复制粘贴一份大 literal）。

    复制粘贴的后果：v1 改一个字段，v2 悄悄留在旧版 ⇒ 两版契约漂移，
    而漂移只有等某天有人拿 v2 快照去校验才发现。⇒ 结构上派生。

    ⚠️ `schemaVersion` 的 const 用 **v2 字面量**（不是「当前版本」）：
    契约升到 v3 时，v2 的契约必须**原样冻结**，否则已发布的 v2 快照会被
    拿 v3 的契约判成「版本不符」（code-review HIGH-1）。
    """
    properties = dict(v1["properties"])
    properties["schemaVersion"] = {"const": SCHEMA_VERSION_V2}
    properties["sourceBindings"] = _array(
        _SOURCE_BINDING_SCHEMA, maxItems=MAX_MANIFEST_SOURCE_BINDINGS
    )
    return {
        "$schema": v1["$schema"],
        "$id": "https://qa-system.local/schemas/semantic-manifest/v2.json",
        "title": "Semantic Manifest v2",
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


MANIFEST_SCHEMA_V2: dict[str, Any] = _deriveV2(MANIFEST_SCHEMA_V1)

# 版本注册表：校验时按 `manifest.schemaVersion` 取对应 Schema。
# ⚠️ **未知版本必须拒绝**（不猜、不放宽）—— 否则一个 v99 快照会被拿 v2 的契约
# 判成合法，或反之让历史快照莫名其妙失效。
#
# ⚠️ **键必须是版本字面量，不能是「当前版本」**（code-review HIGH-1）：
# 键写 `SCHEMA_VERSION` ⇒ 下一次升版（`SCHEMA_VERSION = "v3"`）会把 v2 从
# 注册表里抹掉 ⇒ 所有已发布的 v2 快照当场变成「未知版本」⇒ 无法校验 / 激活。
MANIFEST_SCHEMAS: dict[str, dict[str, Any]] = {
    SCHEMA_VERSION_V1: MANIFEST_SCHEMA_V1,
    SCHEMA_VERSION_V2: MANIFEST_SCHEMA_V2,
}

#: 哪些契约版本**含** `sourceBindings` 分区（引用闸据此决定要不要查这一段）。
#: ⚠️ 这是**版本配置**，不是版本字符串比较 —— 未来的 v3 若不带该分区，
#: 用「版本 ≠ v1」判断会让引用闸去读一个不存在的键 ⇒ KeyError ⇒ 500
#: （而不是一条可读的契约错误）。
MANIFEST_SCHEMA_SOURCE_BINDING_VERSIONS: frozenset[str] = frozenset(
    {SCHEMA_VERSION_V2}
)

def _assertCurrentVersionRegistered() -> None:
    """升版漏登记 ⇒ **import 期**就炸（而不是等发版时才发现快照全是未知版本）。

    两件事同时成立才算登记完整：① 当前版本在注册表里；② 当前版本的 Schema
    自述的 `schemaVersion` 就是当前版本。
    """
    if SCHEMA_VERSION not in MANIFEST_SCHEMAS:
        raise RuntimeError(
            f"当前 Schema 版本 {SCHEMA_VERSION!r} 未登记进 MANIFEST_SCHEMAS"
            f"（已登记：{sorted(MANIFEST_SCHEMAS)}）"
        )
    const = MANIFEST_SCHEMAS[SCHEMA_VERSION]["properties"]["schemaVersion"].get("const")
    if const != SCHEMA_VERSION:
        raise RuntimeError(
            f"当前版本的 Schema 自述版本为 {const!r}，与 SCHEMA_VERSION "
            f"{SCHEMA_VERSION!r} 不一致 ⇒ 契约与版本号漂移"
        )


_assertCurrentVersionRegistered()

# 兼容别名：当前版本的 Schema（历史代码按「一个 Schema」引用它）。
# 取自注册表而不是写死 V2 ⇒ 升版时自动跟随「当前版本」。
MANIFEST_SCHEMA: dict[str, Any] = MANIFEST_SCHEMAS[SCHEMA_VERSION]


class ManifestSchemaError(ValueError):
    """Manifest 不满足其 `schemaVersion` 对应的契约（结构 / 引用 / 哈希前校验）。"""


def validateManifest(manifest: Any) -> None:
    """校验并返回 None；不合法则抛 `ManifestSchemaError`（一次报多处）。

    **按 `manifest.schemaVersion` 分发**（v1 / v2 各有各的 Schema 与引用闸）：
    - 未知版本 ⇒ 直接拒绝（不放行、不猜）；
    - v1 快照继续按 v1 校验（历史快照不许被新契约判死）。
    """
    if not isinstance(manifest, dict):
        raise ManifestSchemaError("Manifest 必须是 JSON 对象")
    version = manifest.get("schemaVersion")
    schema = MANIFEST_SCHEMAS.get(version) if isinstance(version, str) else None
    if schema is None:
        raise ManifestSchemaError(
            f"Manifest 的 schemaVersion 未知：{version!r}"
            f"（已知版本：{sorted(MANIFEST_SCHEMAS)}）"
        )
    errors = _schemaErrors(manifest, schema) or _referenceErrors(manifest, version)
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


def _schemaErrors(manifest: Any, schema: dict[str, Any]) -> list[str]:
    """取前 N 条结构错误。

    用 `islice` 而不是「先物化全部再截断」：10 万条非法元素时，后者会先生成
    10 万个错误对象/字符串（内存随请求体线性增长），只为打印 10 行。

    ⚠️ **单条消息也要截断**（security F6）：jsonschema 的 `err.message` 会把
    **被拒的值**原样嵌进去（`'…4000 字符…' is too long`）⇒ 单条消息可达几十 KB，
    再落进 `semantic_release_check.detail` 就是一份可观的垃圾（还会把值里的
    敏感内容抄进校验结论）。
    """
    validator = Draft202012Validator(schema)
    found = islice(validator.iter_errors(manifest), _MAX_REPORTED_ERRORS)
    return [
        f"{_formatPath(err.absolute_path)}：{_truncateMessage(err.message)}"
        for err in found
    ]


def _referenceErrors(manifest: Any, version: str) -> list[str]:
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
    if version in MANIFEST_SCHEMA_SOURCE_BINDING_VERSIONS:
        _sourceBindingSectionErrors(
            manifest.get("sourceBindings") or [], manifest["classes"], classNames, errors
        )
    return errors


def _sourceBindingSectionErrors(
    bindings: list[dict[str, Any]],
    classes: list[dict[str, Any]],
    classNames: set[str],
    errors: list[str],
) -> None:
    """v2 源绑定分区的引用闸（两组独立判据，各自成函数）。

    身份判据（这条绑定指哪张表）：类必须存在、绑定主键不得重复；
    属性判据（这条绑定绑了哪些列）：属性必须属于该类、键序号不得重复、
    同一绑定内属性名不得重复。
    """
    propertyNamesByClass = _propertyNamesByClass(classes)
    seenBindings: set[tuple[str, str | None, str | None, str, str]] = set()
    for index, binding in enumerate(bindings):
        where = f"sourceBindings[{index}]"
        _bindingIdentityErrors(binding, where, classNames, seenBindings, errors)
        _bindingPropertyErrors(binding, where, propertyNamesByClass, errors)


def _propertyNamesByClass(classes: list[dict[str, Any]]) -> dict[str, set[str]]:
    """类名 → 该类的属性名集合（**空集合也是有效值**：类存在但没有属性）。"""
    propertyNamesByClass: dict[str, set[str]] = {}
    for cls in classes:
        name = cls.get("className")
        if not isinstance(name, str):
            continue
        names: set[str] = set()
        for prop in cls.get("properties", []) or []:
            if isinstance(prop, dict) and isinstance(prop.get("propertyName"), str):
                names.add(prop["propertyName"])
        propertyNamesByClass[name] = names
    return propertyNamesByClass


def _bindingIdentityErrors(
    binding: dict[str, Any],
    where: str,
    classNames: set[str],
    seenBindings: set[tuple[str, str | None, str | None, str, str]],
    errors: list[str],
) -> None:
    """绑定身份判据：类存在 + 绑定主键唯一。

    绑定主键 = `(className, datasourceName, schemaName, sourceTable, bindingRole)`；
    重复 ⇒ 「这条绑定到底指哪张表」变成多解。
    """
    className = binding.get("className")
    if not (isinstance(className, str) and className in classNames):
        _append(errors, f"{where}.className：{className!r} 不存在")
    datasourceName = binding.get("datasourceName")
    schemaName = binding.get("schemaName")
    key = (
        className if isinstance(className, str) else "",
        datasourceName if isinstance(datasourceName, str) else None,
        schemaName if isinstance(schemaName, str) else None,
        str(binding.get("sourceTable")),
        str(binding.get("bindingRole")),
    )
    if key in seenBindings:
        _append(errors, f"{where} 重复：{key!r}")
    seenBindings.add(key)


def _bindingPropertyErrors(
    binding: dict[str, Any],
    where: str,
    propertyNamesByClass: dict[str, set[str]],
    errors: list[str],
) -> None:
    """绑定属性判据：属性归属 + 键序号唯一 + 绑定内属性名唯一。

    - 属性必须属于**该类**（不能张冠李戴到别的类）；
    - 复合键的 `keyOrdinal` 不得重复（重复 ⇒ 键的定义有歧义）；
    - 同一绑定内 `propertyName` 不得重复（重复 ⇒ 「这条绑定把哪个属性映到哪列」
      变成多解；库里属性名无唯一约束，所以这里必须自己挡）。
    """
    className = binding.get("className")
    classExists = isinstance(className, str) and className in propertyNamesByClass
    # 类不存在时不再逐条报属性（那只是同一条错误的回声）；
    # 类存在但**没有属性**时，允许集合为空集 ⇒ 任何属性名都算张冠李戴。
    allowed = propertyNamesByClass.get(className) if classExists else None
    ordinals: set[int] = set()
    propertyNames: set[str] = set()
    for propIndex, prop in enumerate(binding.get("properties", []) or []):
        if not isinstance(prop, dict):
            continue
        propWhere = f"{where}.properties[{propIndex}]"
        propName = prop.get("propertyName")
        if allowed is not None and isinstance(propName, str):
            if propName not in allowed:
                _append(
                    errors,
                    f"{propWhere}.propertyName：{propName!r} 不属于类 {className!r}",
                )
            if propName in propertyNames:
                _append(errors, f"{propWhere}.propertyName 重复：{propName!r}")
            propertyNames.add(propName)
        ordinal = prop.get("keyOrdinal")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            if ordinal in ordinals:
                _append(errors, f"{propWhere}.keyOrdinal 重复：{ordinal}")
            ordinals.add(ordinal)


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
    # ⚠️ 不写死「v1」：契约按版本分发，报错文字里出现具体版本会误导读者
    # （读者会以为「过了 v1 契约」，而快照其实是 v2）。
    return "Manifest 不满足契约：\n" + "\n".join(errors)


def _truncateMessage(message: str) -> str:
    """截断单条错误消息（被拒的值可能长达数千字符）。"""
    if len(message) <= _MAX_ERROR_MESSAGE_LENGTH:
        return message
    return (
        f"{message[:_MAX_ERROR_MESSAGE_LENGTH]}…"
        f"（已截断，原文 {len(message)} 字符）"
    )
