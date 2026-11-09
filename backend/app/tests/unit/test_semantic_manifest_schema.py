"""Manifest JSON Schema v1（P1a / D7）：合法通过、非法全拒、哈希稳定。

D7 完成定义（排程原文）：「合法 Manifest 通过、5 类非法输入全部拒绝；哈希稳定」。

五类非法输入各有一条用例，且都从**真实 golden** 变异而来（不是手搓空壳）：
缺字段 / 类型错 / 未知字段 / 枚举值非法 / 引用悬空。

Schema 只是第一道闸；引用完整性（类名、指标名、域名）是 Schema 表达不了的部分，
由 `validateManifest` 的第二遍检查兜底 —— 回放历史快照时同样要过这一关。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.domain.semantic_manifest_schema import (
    SCHEMA_VERSION,
    ManifestSchemaError,
    canonicalManifestJson,
    manifestHash,
    validateManifest,
)
from app.services.semantic_compiler import SCHEMA_VERSION as COMPILER_SCHEMA_VERSION
from app.services.semantic_compiler import compileFromRows
from app.tests.unit._semantic_rows import canonicalJson
from app.tests.unit.test_semantic_manifest_golden import compileGolden

_GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "semantic_manifest_golden.json"


def golden() -> dict[str, Any]:
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. 合法输入
# ---------------------------------------------------------------------------


def test_schema_version_matches_compiler() -> None:
    """Schema 版本是单一事实源：编译器与校验器不得各写各的。"""
    assert SCHEMA_VERSION == COMPILER_SCHEMA_VERSION


def test_release_orm_default_schema_version_matches() -> None:
    """`semantic_release.schema_version` 的库侧默认值不得与 Schema 版本漂移。

    迁移 0142 写死了 `server_default='v1'`。SCHEMA_VERSION 一旦 +1，这里会先红，
    逼着做一次明确决定（补迁移改默认值，还是由落库路径显式赋值）。
    """
    from app.domain.semantic_release_models import SemanticRelease

    default = SemanticRelease.__table__.c.schema_version.server_default.arg

    assert default == SCHEMA_VERSION


def test_golden_manifest_passes_validation() -> None:
    validateManifest(golden())


def test_compiler_output_passes_validation() -> None:
    """编译器产出必须天然合法 —— 否则发布流程会在自己的产物上卡住。"""
    validateManifest(compileGolden())


def test_manifest_with_every_section_empty_passes() -> None:
    """空库也能发布（首个快照）：分区可以空，但键必须齐。"""
    manifest = compileFromRows(classes=[], properties=[], joins=[])

    validateManifest(manifest)
    assert all(manifest[section] == [] for section in manifest if section != "schemaVersion")


# ---------------------------------------------------------------------------
# 2. 五类非法输入
# ---------------------------------------------------------------------------


def test_rejects_missing_section() -> None:
    """① 缺字段：少一个顶层分区。"""
    broken = golden()
    del broken["metrics"]

    with pytest.raises(ManifestSchemaError, match="metrics"):
        validateManifest(broken)


def test_rejects_missing_element_field() -> None:
    """① 缺字段：元素级（类缺少 className）。"""
    broken = golden()
    del broken["classes"][0]["className"]

    with pytest.raises(ManifestSchemaError, match="className"):
        validateManifest(broken)


def test_rejects_wrong_type() -> None:
    """② 类型错：joins 给成字符串、布尔字段给成字符串。"""
    broken = golden()
    broken["joins"] = "PURCHASE_ORDER_SUPPLIER"

    with pytest.raises(ManifestSchemaError, match="joins"):
        validateManifest(broken)

    broken = golden()
    purchaseOrder = _classByName(broken, "PurchaseOrder")
    purchaseOrder["properties"][0]["isPrimaryKey"] = "yes"

    with pytest.raises(ManifestSchemaError, match="isPrimaryKey"):
        validateManifest(broken)


def test_rejects_unknown_field() -> None:
    """③ 未知字段：顶层与元素级都必须拒绝（拼错键不能被静默忽略）。"""
    broken = golden()
    broken["extraSection"] = []

    with pytest.raises(ManifestSchemaError, match="extraSection"):
        validateManifest(broken)

    broken = golden()
    broken["metrics"][0]["metricTypo"] = "RATIO"

    with pytest.raises(ManifestSchemaError, match="metricTypo"):
        validateManifest(broken)


def test_rejects_invalid_enum_and_schema_version() -> None:
    """④ 枚举值非法 / schemaVersion 非法。"""
    broken = golden()
    broken["metrics"][0]["metricType"] = "ratio"

    with pytest.raises(ManifestSchemaError, match="metricType"):
        validateManifest(broken)

    broken = golden()
    broken["schemaVersion"] = "v2"

    with pytest.raises(ManifestSchemaError, match="schemaVersion"):
        validateManifest(broken)


def test_rejects_dangling_references() -> None:
    """⑤ 引用悬空：类名 / 指标名 / 域名 / 度量内嵌类名。"""
    broken = golden()
    broken["joins"][0]["targetClassName"] = "NoSuchClass"

    with pytest.raises(ManifestSchemaError, match="NoSuchClass"):
        validateManifest(broken)

    broken = golden()
    _metricByName(broken, "supplierOnTimeRate")["numeratorMeasure"]["className"] = "NoSuchClass"

    with pytest.raises(ManifestSchemaError, match="NoSuchClass"):
        validateManifest(broken)

    broken = golden()
    broken["kpis"][0]["metricName"] = "noSuchMetric"

    with pytest.raises(ManifestSchemaError, match="noSuchMetric"):
        validateManifest(broken)

    broken = golden()
    broken["businessObjects"][0]["primaryDomainCode"] = "NO_SUCH_DOMAIN"

    with pytest.raises(ManifestSchemaError, match="NO_SUCH_DOMAIN"):
        validateManifest(broken)


def test_rejects_duplicate_names() -> None:
    """同名多行是治理数据损坏：快照里出现两份会让运行期任选其一。"""
    broken = golden()
    duplicate = copy.deepcopy(broken["classes"][0])
    broken["classes"].append(duplicate)

    with pytest.raises(ManifestSchemaError, match="重复"):
        validateManifest(broken)


def test_rejects_reverse_index_disagreeing_with_kpi_link() -> None:
    """`metrics[].kpiCodes` 与 `kpis[].metricName` 必须互相对得上。"""
    broken = golden()
    _metricByName(broken, "supplierOnTimeRate")["kpiCodes"].append("KPI-999")

    with pytest.raises(ManifestSchemaError, match="KPI-999"):
        validateManifest(broken)


# ---------------------------------------------------------------------------
# 3. 规范化序列化与哈希
# ---------------------------------------------------------------------------


def test_canonical_json_is_key_order_independent() -> None:
    manifest = golden()
    shuffled = _reverseKeyOrder(manifest)

    canonical = canonicalManifestJson(shuffled)

    assert canonical == canonicalManifestJson(manifest)
    # 分隔符不带空白：键值用 `":"`、元素用 `","`（内容里自带的空格不受影响）。
    assert '": "' not in canonical
    assert '", "' not in canonical


def test_hash_is_stable_and_content_sensitive() -> None:
    manifest = golden()

    assert manifestHash(manifest) == manifestHash(_reverseKeyOrder(manifest))
    assert len(manifestHash(manifest)) == 64

    changed = golden()
    changed["domains"][0]["name"] = "采购域（改名）"
    assert manifestHash(changed) != manifestHash(manifest)


def test_golden_hash_is_pinned() -> None:
    """钉死规范化形式与哈希算法的具体取值：换序列化参数会立刻变红。

    期望值由 `canonicalManifestJson`（sort_keys + 无空白 + 保留中文）算出；
    与落库列 `semantic_release.manifest_hash` 的口径必须完全一致。
    """
    assert manifestHash(golden()) == _GOLDEN_HASH


def test_hash_matches_test_helper_canonical_json() -> None:
    """测试里的 `canonicalJson` 与生产 `canonicalManifestJson` 不得漂移。"""
    manifest = golden()

    assert canonicalJson(manifest) == canonicalManifestJson(manifest)


# ---------------------------------------------------------------------------
# 4. 评审补强（D7b）：评审实证的放行缺口
# ---------------------------------------------------------------------------


def test_rejects_duplicate_join_and_business_object_keys() -> None:
    """joinKey / BO code 也必须唯一：回放时按它解析，重复会任选其一或重复扇出。"""
    broken = golden()
    broken["joins"].append(copy.deepcopy(broken["joins"][0]))

    with pytest.raises(ManifestSchemaError, match="joinKey"):
        validateManifest(broken)

    broken = golden()
    broken["businessObjects"].append(copy.deepcopy(broken["businessObjects"][0]))

    with pytest.raises(ManifestSchemaError, match="businessObjects"):
        validateManifest(broken)


def test_rejects_duplicate_entries_inside_list_fields() -> None:
    """`kpiCodes` / `attachedDomainCodes` 内部重复同样是治理数据损坏。"""
    broken = golden()
    _metricByName(broken, "supplierOnTimeRate")["kpiCodes"].append("KPI-001")

    with pytest.raises(ManifestSchemaError, match="kpiCodes"):
        validateManifest(broken)

    broken = golden()
    _businessObjectByCode(broken, "SUPPLIER")["attachedDomainCodes"].append("QUALITY")

    with pytest.raises(ManifestSchemaError, match="attachedDomainCodes"):
        validateManifest(broken)


def test_rejects_kpi_metric_link_mismatch() -> None:
    """反向索引必须双向闭合，不只是「名字存在」。

    KPI-001 原本挂 supplierOnTimeRate；改成另一个**真实存在**的指标后，
    单向存在性检查放行，但两个指标各自的 `kpiCodes` 与之对不上。
    """
    broken = golden()
    _kpiByCode(broken, "KPI-001")["metricName"] = "purchaseAmount"

    with pytest.raises(ManifestSchemaError, match="KPI-001"):
        validateManifest(broken)

    # 反方向：指标的 kpiCodes 里点了名，但 KPI 指向别的指标。
    broken = golden()
    _metricByName(broken, "supplierOnTimeRate")["kpiCodes"].append("KPI-002")

    with pytest.raises(ManifestSchemaError, match="KPI-002"):
        validateManifest(broken)


def test_rejects_measure_agg_outside_whitelist() -> None:
    """`agg` 必须取自 `MeasureSpecSchema._MEASURE_AGG_ALLOWED`。

    写入侧由 Pydantic 挡住，但校验器是回放历史/人工编辑快照的最后一道闸。
    """
    broken = golden()
    _metricByName(broken, "supplierOnTimeRate")["numeratorMeasure"]["agg"] = "BOGUS"

    with pytest.raises(ManifestSchemaError, match="agg"):
        validateManifest(broken)


def test_rejects_non_flat_dimension_defaults() -> None:
    """`dimensionDefaults` 只能是扁平字符串映射（唯一不设键集的自由格，必须收窄）。"""
    broken = golden()
    _metricByName(broken, "supplierOnTimeRate")["dimensionDefaults"] = {
        "time": {"nested": 1}
    }

    with pytest.raises(ManifestSchemaError, match="dimensionDefaults"):
        validateManifest(broken)


def test_rejects_payload_that_cannot_be_canonicalized() -> None:
    """孤立代理项能通过 `json.loads` 与 Schema，但 UTF-8 编码失败 ⇒ 必须转成契约错误。

    否则 D9 按 `ManifestSchemaError` 捕获会漏掉，变成 500。
    """
    broken = golden()
    broken["domains"][0]["name"] = "\ud800"

    with pytest.raises(ManifestSchemaError, match="规范化"):
        validateManifest(broken)


def test_manifest_carries_no_instance_ids() -> None:
    """反向守卫：快照里不得出现自增 id 型键（跨环境字节一致性的硬约束）。"""
    _assertNoInstanceIds(golden())


def _assertNoInstanceIds(value: Any, path: str = "") -> None:
    forbidden = {"class_id", "classId", "datasourceId", "targetClassId"}
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in forbidden, f"{path}.{key} 是实例相关 id，不得进入 Manifest"
            _assertNoInstanceIds(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assertNoInstanceIds(child, f"{path}[{index}]")


def _classByName(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in manifest["classes"] if c["className"] == name)


def _metricByName(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    return next(m for m in manifest["metrics"] if m["metricName"] == name)


def _kpiByCode(manifest: dict[str, Any], code: str) -> dict[str, Any]:
    return next(k for k in manifest["kpis"] if k["kpiCode"] == code)


def _businessObjectByCode(manifest: dict[str, Any], code: str) -> dict[str, Any]:
    return next(b for b in manifest["businessObjects"] if b["code"] == code)


def _reverseKeyOrder(value: Any) -> Any:
    """递归把字典构造顺序倒过来（不改内容，只改插入顺序）。"""
    if isinstance(value, dict):
        return {key: _reverseKeyOrder(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reverseKeyOrder(item) for item in value]
    return value


_GOLDEN_HASH = "29ecf144f910f4e7d9b3c2a204a6bb8c48d1e0666a22b4d318d8dd3368cfe867"
