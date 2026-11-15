"""发布镜像的图谱标签契约（P3b D22）。

**为什么单独一份契约文件**：镜像节点的 label 与主图不同，而 CQL 里 label /
关系类型都**不可参数化**（只能拼字符串）⇒ 必须有一份**白名单**兜住注入与误写。
这份契约被服务层（构造镜像计划）与基础设施层（生成 CQL）共同引用，避免两处
各写一份、各漂一份。

**为什么不复用主图 label**：主图消费方是 `MATCH (c:Class)` / `getClassIds()` 这类
不带发布维度的查询。镜像若复用主图 label，一次发布就会污染所有已有查询；
独立 label（`ReleaseClass` 等）让镜像对现有消费方**完全不可见** ⇒ 主图零影响。
"""

from __future__ import annotations

from app.domain.id_mapping import (
    BUSINESS_OBJECT_CLASS,
    BUSINESS_OBJECT_KPI,
    BUSINESS_OBJECT_METRIC,
    BUSINESS_OBJECT_PROPERTY,
)

# 实体类型 → 镜像 label
RELEASE_MIRROR_LABELS: dict[str, str] = {
    "class": "ReleaseClass",
    "property": "ReleaseProperty",
    "metric": "ReleaseMetric",
    "kpi": "ReleaseKpiCatalog",
}

# 实体类型 → id_mapping.business_object（解析侧与写图侧共用同一口径，
# 避免「解析用一套字面量、写入用另一套」⇒ 解析静默全落空）。
RELEASE_MIRROR_ENTITY_BUSINESS_OBJECTS: dict[str, str] = {
    "class": BUSINESS_OBJECT_CLASS,
    "property": BUSINESS_OBJECT_PROPERTY,
    "metric": BUSINESS_OBJECT_METRIC,
    "kpi": BUSINESS_OBJECT_KPI,
}

# 实体类型 → 镜像节点属性名。
# 刻意与主图同名字段保持一致（Class: name/alias/description/sourceTable …），
# 这样「镜像节点去掉发布标识字段」= 主图节点 ⇒ 两侧可直接对照。
RELEASE_MIRROR_NODE_FIELDS: dict[str, tuple[str, ...]] = {
    "class": ("name", "alias", "description", "sourceTable"),
    "property": (
        "name",
        "alias",
        "dataType",
        "sourceColumn",
        "isPrimaryKey",
        "isForeignKey",
    ),
    "metric": ("name", "alias", "formula", "aggFunction"),
    "kpi": ("code", "name", "status", "owner", "unit"),
}

# 每个镜像节点都带的发布标识字段（主图没有 ⇒ 镜像专属）
RELEASE_MIRROR_RELEASE_FIELDS: tuple[str, ...] = (
    "release_id",
    "release_version",
    "mirror_of",
)

# 镜像边的关系类型 → (源实体类型, 目标实体类型)。
#
# 语义关系边（ontology_relation 驱动）**不在内**：manifest 无 relations 分区 ⇒
# 该类边无法从快照自包含重建 ⇒ 收录它就不是「这一版发布的图」。边界声明见
# daily/D22.md 第四节。
#
# `REFERENCES`（属性 → 被引用类）在内：manifest 的 `properties[].refClassName`
# 自带它，且编译器会把悬空引用编译成 null ⇒ 镜像能从快照自包含重建。
# （早期版本漏了它 ⇒ 镜像 ≠ 主图本体子图，D25 切读侧会静默少一截引用路径。）
RELEASE_MIRROR_REL_ENDPOINT_TYPES: dict[str, tuple[str, str]] = {
    "HAS_PROPERTY": ("class", "property"),
    "SUBCLASS_OF": ("class", "class"),
    "JOIN": ("class", "class"),
    "DERIVED_FROM": ("metric", "class"),
    "MEASURED_BY": ("kpi", "metric"),
    "REFERENCES": ("property", "class"),
}

# 镜像边的关系类型白名单（由端点类型表派生 ⇒ 只有一份事实源）
RELEASE_MIRROR_REL_TYPES: frozenset[str] = frozenset(
    RELEASE_MIRROR_REL_ENDPOINT_TYPES
)

# 实体类型的稳定顺序（镜像计划排序、写入分批顺序都按它 ⇒ 结果可复现）
RELEASE_MIRROR_ENTITY_TYPES: tuple[str, ...] = ("class", "property", "metric", "kpi")
