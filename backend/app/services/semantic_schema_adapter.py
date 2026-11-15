"""P2：已发布语义快照 → schema 文本渲染视图的复原层（D12）。

为什么必须有这一层
------------------
`nl2sql_schema.buildSchemaText` 是**全线按 id 组织**的：类列表被做成
`classesById = {cls.id: cls}`，父类 / 外键 / JOIN 两端全部按 id 回查。

而语义发布快照（`semantic_release.manifest_json`）是**按类名组织且不带 id** 的：
类之间的引用一律是 `parentClassName` / `refClassName` / `sourceClassName` /
`targetClassName`。快照不带 id 不是缺陷而是裁决（P0 裁决一 + 字节确定性）：
自增 id 在不同环境不同库里不一样，带进快照就没法保证「同一份 manifest 字节相同」。

所以本模块做的是**形状转换**：把「按名引用」翻译成「按 id 引用」。
转换手段是**合成 id**（plan §3.3 方案 A）：按类名排序、在**整份 manifest** 上
分配 1..N，保证同一份 manifest 每次得到同样的 id。

三条必须成立的等价性
--------------------
1. **只复原召回子集**。`buildSchemaText` 的入参是召回子集
   （`chat_service._selectRelevantClasses`），SQL 阶段还会再剪一次
   （`nl2sql_schema.pruneClassesForSqlStage`）。若把整份 manifest 灌进去，
   schema 文本会变长、JOIN 图会变宽 ⇒ 行为漂移，且 D13 的 diff 永远归不了零。
   ⇒ 按调用方给的 `class_name` 列表过滤 manifest。
2. **子集外的引用 = 查不到 = None**。ORM 路径里 `classesById` 只含子集，
   子集外的父类 / 外键目标就是查不到 ⇒ `parent_class_id` / `ref_class_id`
   解析成 `None`，渲染时 FK 只出列名不出目标表。本模块保持同口径。
   墓碑类同理：编译器把墓碑引用编译成 `None`，ORM 路径里墓碑 id 也不在
   `classesById` 里 —— 两者天然一致，无需额外处理。
3. **渲染器能原样消费**。`nl2sql_schema` 探 `cls.__dict__["parent"]` 与
   `prop.__dict__["ref_class"]`（SQLAlchemy 惰性关系探测）。
   ⇒ 复原对象必须是**带实例 `__dict__` 的普通类**：不能用 `slots=True`
   的 dataclass、不用 `NamedTuple`、不设 `__slots__`。

作用域边界
----------
本模块只服务 **schema 文本渲染路径**。`pc.classes` 在代码库里有 11 个消费方
（plan §3.2），其中 `_sampleValueDomains` / `_buildDriftWarning` /
`_classRecallScores` 依赖 ORM 实体的**惰性关系**与 `_recall_score` 属性，
换成复原视图会静默劣化（不是报错）。因此接线在 D15，且只接渲染那一段。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.exceptions import DomainError
from app.domain.semantic_manifest_schema import SCHEMA_VERSION
from app.services.semantic_registry_service import (
    SemanticReleaseIntegrityError,
    semanticRegistry,
)

logger = logging.getLogger(__name__)

# 合成 id 的起始值。刻意用小整数（1..N）而不是模仿 DB 自增 id 的大整数：
# 渲染器只要求 id 非 None 且子集内唯一，数值大小不参与任何语义。
_SYNTHETIC_ID_START = 1

# 快照缺字段时的兜底值。与 ORM 列的默认值一致（`OntologyJoin.join_type`
# 默认 INNER、`relation_type` 默认 business）。
_DEFAULT_JOIN_TYPE = "INNER"
_DEFAULT_RELATION_TYPE = "business"


class SemanticSnapshotStaleError(DomainError):
    """快照里没有调用方要的类 —— 快照比召回结果旧（或召回结果来自别的域）。

    「快照陈旧」是**可预期的**状态（发布之后又新增了类），不是数据损坏，
    所以它是领域异常而不是 500。调用方（`loadClassesForSchema`）据此降级回
    直读治理表，而不是让整个问答失败。
    """

    def __init__(self, message: str, *, missing: Sequence[str] = ()) -> None:
        super().__init__(message, details={"missing": list(missing)})


class SemanticSnapshotMalformedError(DomainError):
    """快照**内容**不合法（类型不对 / 同名类重复）⇒ 不能当作事实用。

    与 `SemanticSnapshotStaleError` 分开：陈旧是「快照比召回结果旧」（发布节奏
    的常态），畸形是「快照内容本身坏了」（绕过发布路径写库 / 数据被改坏）。
    两者都**降级回直读治理表**，但要分开记录 —— 畸形是数据事故信号。
    """

    def __init__(self, message: str, *, where: str | None = None) -> None:
        super().__init__(message, details={"where": where} if where else {})


@dataclass(frozen=True)
class SnapshotProperty:
    """快照属性 → 渲染视图。字段名与 ORM `OntologyProperty` 逐字对齐（snake_case）。

    刻意**不**用 `slots=True`：`nl2sql_schema._resolveRefTable` 探
    `prop.__dict__["ref_class"]`，`__slots__` 的实例没有 `__dict__` ⇒
    `AttributeError: __dict__`。

    `frozen=True`（不可变）却**不影响** `__dict__` 的存在（frozen dataclass
    实例照样有实例字典）⇒ 既满足项目「不可变数据」硬约束，又不破渲染器的探针。
    """

    id: int
    class_id: int
    property_name: str
    property_alias: str | None
    business_aliases: tuple[str, ...]
    description: str | None
    data_type: str
    is_primary_key: bool
    is_foreign_key: bool
    ref_class_id: int | None
    source_column: str | None
    unit: str | None
    allowed_values: tuple[str, ...]
    is_not_null: bool | None
    min_value: str | None
    max_value: str | None
    regex_pattern: str | None


@dataclass(frozen=True)
class SnapshotClass:
    """快照类 → 渲染视图。字段名与 ORM `OntologyClass` 对齐。

    刻意**不**声明 `parent` / `children` / `metrics`：`nl2sql_schema` 用
    `"parent" in cls.__dict__` 判定「relationship 是否已加载」，复原视图里
    永远不该有这个键，于是解析统一走 `parent_class_id` 分支 —— 与 ORM 路径
    在「父类不在召回子集内」时的结果一致。

    同样**不**用 `slots=True`：`__dict__` 是渲染器的探针入口。
    """

    id: int
    class_name: str
    class_alias: str | None
    description: str | None
    source_table: str | None
    object_type: str | None
    object_owner: str | None
    parent_class_id: int | None
    properties: tuple[SnapshotProperty, ...]


@dataclass(frozen=True)
class SnapshotJoin:
    """快照 join → 渲染视图。字段名与 ORM `OntologyJoin` 对齐。

    `source_class_id` / `target_class_id` 为 `None` 表示该端**不在召回子集内**
    —— 渲染器 `tableByClassId.get(...)` 查不到就把这条边跳过，与 ORM 路径
    （id 真实存在但不在子集里、同样查不到）行为一致。
    """

    id: int
    source_class_id: int | None
    source_columns: tuple[str, ...]
    target_class_id: int | None
    target_columns: tuple[str, ...]
    join_type: str
    relation_type: str
    description: str | None
    join_key: str


def restoreSnapshotSchema(
    manifest: dict[str, Any],
    classNames: Sequence[str],
) -> tuple[list[SnapshotClass], list[SnapshotJoin]]:
    """把一份发布快照复原成渲染器能吃的视图（只取召回子集）。

    - `manifest`：发布快照正文（`semantic_release.manifest_json`）。
    - `classNames`：**召回子集**的类名清单（调用方给的 `class_name` 列表）。
      清单里的类若不在快照中 ⇒ 抛 `SemanticSnapshotStaleError`。

    返回 `(classes, joins)`：`classes` 保持 `classNames` 的顺序；`joins` 是
    快照的**全量** join（两端按子集解析，子集外为 `None`），与 ORM 路径
    「传全量 join、渲染器自己过滤」同口径。
    """
    manifestClasses = _indexManifestClasses(manifest)

    # 「陈旧」先于「畸形」判定：陈旧是发布节奏的常态（发布之后又加了类），
    # 畸形是数据事故。若顺序反过来，一个既陈旧又畸形的快照会被报成
    # 「内容畸形」⇒ 排查方向被带偏（实测：id 合成阶段先跑 ⇒ 先炸的是畸形）。
    requested = _dedupe(classNames)
    missing = [name for name in requested if name not in manifestClasses]
    if missing:
        raise SemanticSnapshotStaleError(
            f"语义发布快照缺少 {len(missing)} 个被召回的类：{'、'.join(missing)}",
            missing=missing,
        )

    classIds, propertyIds = _synthesizeIds(manifestClasses)

    # 子集内的类名 → 合成 id。父类 / 外键 / join 两端都只在这个映射里查 ——
    # 查不到就是「子集外」，与 ORM 路径的 classesById 口径一致。
    subsetIds = {name: classIds[name] for name in requested}

    classes = [
        _restoreClass(
            manifestClasses[name],
            classId=classIds[name],
            propertyIds=propertyIds,
            subsetIds=subsetIds,
        )
        for name in requested
    ]
    joins = _restoreJoins(manifest, subsetIds)
    return classes, joins


async def loadClassesForSchema(
    classes: list[Any],
    joins: list[Any] | None,
    *,
    session: Any,
    enabled: bool,
    domainCode: str | None = None,
    registry: Any = None,
) -> tuple[list[Any], list[Any] | None]:
    """schema 文本渲染路径的取类闸门：**开关关 / 无发布 ⇒ 原样返回**。

    - 开关关 ⇒ 直接返回传入的治理表对象，与今天的输出**逐字节一致**（连库都不碰）。
    - 无 ACTIVE 发布 ⇒ 同上（未发布是正常状态，不是错误）。
    - 快照陈旧（缺召回子集里的类）⇒ **降级回直读治理表**并告警。
      快照落后于治理表是发布节奏的常态，不该让问答整体失败。

    `registry` 供测试注入替身；生产走模块级单例 `semanticRegistry`。
    """
    if not enabled:
        return classes, joins

    activeRegistry = registry if registry is not None else semanticRegistry
    snapshot = await _activeSnapshot(activeRegistry, session, domainCode)
    if snapshot is None:
        # 「没有 ACTIVE 快照」= 影子**没生效**（还没发布 / 域作用域没查到）。
        # 这件事必须**可观测**：静默直读 ⇒ 现场只会看到「开关开了但口径没变」，
        # 无从判断是配置没生效、还是发布没做、还是发布在别的域。
        logger.info(
            "当前没有 ACTIVE 语义发布快照（domain=%s），本次直读治理表", domainCode
        )
        return classes, joins

    mismatch = schemaVersionMismatch(snapshot)
    if mismatch:
        logger.warning(
            "语义发布快照的 schema 版本与当前代码不一致，本次降级为直读治理表："
            "version=%s mismatch=%s code=%s",
            snapshot.version,
            mismatch,
            SCHEMA_VERSION,
        )
        return classes, joins

    restored = _restoreForSchema(snapshot, classes)
    if restored is None:
        return classes, joins
    restoredClasses, restoredJoins = restored

    # 调用方传 `joins=None` = 不要 JOIN 段 ⇒ 快照侧也不塞一份进去，
    # 否则「关掉开关」与「打开开关」的 schema 文本会多出一整段 JOIN。
    if joins is None:
        restoredJoins = None

    logger.info(
        "语义发布快照已复原为 schema 渲染视图：version=%s classes=%s",
        snapshot.version,
        len(restoredClasses),
    )
    return restoredClasses, restoredJoins


async def _activeSnapshot(
    activeRegistry: Any,
    session: Any,
    domainCode: str | None,
) -> Any | None:
    """取 ACTIVE 快照；取不到 / 完整性校验不过 ⇒ `None`（降级直读）。

    「完整性校验不过」= 库里那行 ACTIVE 快照**被绕过发布路径改过**
    （哈希对不上 / 体量超限）⇒ 与「内容畸形」同级的数据事故信号。
    这一层存在的全部意义就是「坏快照不许炸问答」⇒ 降级直读 + 记 error，
    绝不让异常穿透成 500。
    """
    try:
        return await activeRegistry.getActive(session, domainCode=domainCode)
    except SemanticReleaseIntegrityError as exc:
        logger.error(
            "语义发布快照完整性校验失败，本次降级为直读治理表：%s", exc
        )
        return None


def _restoreForSchema(snapshot: Any, classes: list[Any]) -> tuple[list[Any], list[Any]] | None:
    """快照 → 渲染视图；陈旧 / 畸形 ⇒ `None`（降级直读治理表）。

    - **陈旧**（快照缺召回子集里的类）：发布之后又加了类的常态，不该让
      问答整体失败 ⇒ 告警后降级。
    - **畸形**（内容本身坏了）：数据事故信号 ⇒ 降级并记 error。
      宁可慢一点，也不能把错的事实喂给模型。
    """
    try:
        return restoreSnapshotSchema(
            snapshot.manifest, [cls.class_name for cls in classes]
        )
    except SemanticSnapshotStaleError as exc:
        logger.warning(
            "语义发布快照落后于召回子集，本次降级为直读治理表：version=%s missing=%s",
            snapshot.version,
            exc.details.get("missing"),
        )
        return None
    except SemanticSnapshotMalformedError as exc:
        logger.error(
            "语义发布快照内容畸形，本次降级为直读治理表：version=%s reason=%s",
            snapshot.version,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# 内部：索引 / id 合成 / 复原
# ---------------------------------------------------------------------------


def schemaVersionMismatch(snapshot: Any) -> list[str]:
    """列出与当前代码 `SCHEMA_VERSION` 不一致的快照版本号（空 = 一致）。

    为什么要拦：演化规则（`app/domain/semantic_manifest_schema.py:15`）是
    「改字段名 / 改类型 ⇒ `SCHEMA_VERSION` +1」。库里若还挂着一份旧版本的
    ACTIVE 快照而代码已经升级，复原层会把新字段读成空（继承没了、FK 目标没了、
    描述没了）—— **每一项事实都静默消失，且不报错不告警**。
    ⇒ 版本对不上就降级直读治理表（宁可慢一点，也不能少给事实）。

    比对两处：manifest 正文的 `schemaVersion` 与行上的 `schema_version`
    （后者由发布时写入，两者不一致说明有人绕过发布路径改过库）。

    **缺版本号也算不一致**：manifest 的 `schemaVersion` 是 JSON Schema 的
    **必填项**（`MANIFEST_SCHEMA` 的 `required` 覆盖全部字段）⇒ 缺它只可能是
    「绕过发布路径写进来的坏数据」。旧实现把「缺失」与「一致」混为一谈 ⇒
    坏快照照样被当好快照复原。

    本函数是**公开**的：影子比对脚本（D13）必须走**同一个**版本闸门，
    否则它会把「版本漂移」误判成「快照丢了事实」。
    """
    manifest = getattr(snapshot, "manifest", None)
    candidates: list[tuple[str, Any]] = []
    if isinstance(manifest, dict):
        candidates.append(("manifest.schemaVersion", manifest.get("schemaVersion")))
    candidates.append(("row.schema_version", getattr(snapshot, "schema_version", None)))
    mismatched: list[str] = []
    for label, value in candidates:
        if not isinstance(value, str) or not value:
            mismatched.append(f"{label}=缺失")
            continue
        if value == SCHEMA_VERSION:
            continue
        if value not in mismatched:
            mismatched.append(value)
    return mismatched


def _indexManifestClasses(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """快照类列表 → `{className: 类原文}`。

    **同名类重复 ⇒ 判为畸形**（不静默取第一个）：编译器在发布时就拒绝「同名多
    版本活跃类」，JSON Schema 与 `manifest_hash` 两道闸也各自保证类名唯一 ⇒
    快照里出现同名只可能是绕过发布路径写进来的坏数据。静默取第一个 = 静默
    丢掉一半事实（还可能丢掉的那一半正好是召回要用的）。

    **元素不是对象 / 缺 `className` ⇒ 同样判畸形**（不静默跳过）：跳过去 =
    静默丢掉一整个类 —— 丢掉的恰好是召回要用的那个时，schema 文本少一整段，
    模型照着残缺的元数据写 SQL，**全程无报错**。
    """
    indexed: dict[str, dict[str, Any]] = {}
    rawClasses = manifest.get("classes") if isinstance(manifest, dict) else None
    if not isinstance(rawClasses, list):
        return indexed
    for raw in rawClasses:
        if not isinstance(raw, dict):
            raise SemanticSnapshotMalformedError(
                f"快照 classes 里存在非对象元素：{_brief(raw)}", where="classes"
            )
        name = raw.get("className")
        if not isinstance(name, str) or not name:
            raise SemanticSnapshotMalformedError(
                f"快照里有类缺 className（或不是非空字符串）：{_brief(raw)}",
                where="classes",
            )
        if name in indexed:
            raise SemanticSnapshotMalformedError(
                f"快照里类名 {name!r} 出现多次（内容畸形）", where="classes"
            )
        indexed[name] = raw
    return indexed


def _brief(value: Any, limit: int = 200) -> str:
    """错误信息里的值摘要（截断）—— 快照类可能很大，整段塞进报错不合适。"""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _synthesizeIds(
    manifestClasses: dict[str, dict[str, Any]],
) -> tuple[dict[str, int], dict[tuple[str, int], int]]:
    """在**整份 manifest** 上分配稳定 id（类名排序 ⇒ 1..N）。

    为什么在整份 manifest 而不是只在子集上分配：子集每次召回都不同，
    若按子集分配，子集里同一个类的 id 会随同伴变化 ⇒ 快照不可复现、
    diff 不可比。按整份分配则「类名 → id」是这份快照的**常量映射**。

    属性 id 同理，但**按属性名排序后分配**（不是按清单下标）：属性 id 必须是
    manifest **内容**的常量映射 —— 若按下标分配，同一个属性名在「清单顺序变了」
    的两份 manifest 里会拿到不同 id，diff 就不可比了（编译器本来就按属性名排序，
    这里只是把「排序」这件事从编译器搬到复原层，不依赖调用方给的顺序）。
    """
    classIds: dict[str, int] = {}
    propertyIds: dict[tuple[str, int], int] = {}
    nextClassId = _SYNTHETIC_ID_START
    nextPropertyId = _SYNTHETIC_ID_START
    for name in sorted(manifestClasses):
        classIds[name] = nextClassId
        nextClassId += 1
        rawProperties = _strictObjectList(
            manifestClasses[name], "properties", where=f"class {name!r}"
        )
        # 同名属性（数据被写坏）用清单下标兜底排序，保证排序是全序、结果稳定。
        order = sorted(
            range(len(rawProperties)),
            key=lambda index: (
                str(rawProperties[index].get("propertyName") or ""),
                index,
            ),
        )
        for index in order:
            propertyIds[(name, index)] = nextPropertyId
            nextPropertyId += 1
    return classIds, propertyIds


def _dedupe(classNames: Sequence[str]) -> list[str]:
    """按首次出现去重（保序）。

    快照按类名判重，重复的类名没有第二份语义；渲染器会把每个类都渲染一遍，
    重复渲染与 ORM 路径（`listClasses` 每行一个类）不等价。
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for name in classNames:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _propertyIdAt(
    propertyIds: dict[tuple[str, int], int],
    className: str,
    index: int,
) -> int:
    """取 (类名, 属性下标) 的合成属性 id。

    刻意**不兜底**：合成 id 由 `_synthesizeIds` 在**整份 manifest** 上按属性名
    排序分配，取不到只可能是「取 id 的类不在合成名单里」或「属性清单在
    两次读取之间变了」—— 两种都是**数据不自洽**。此时旧的兜底
    （`index + 1`）会静默给出一个与其它快照**不一致**的 id ⇒ diff 比不出真实
    差异（假绿）。宁可抛异常让调用方降级，也不产出错误的 id。
    """
    try:
        return propertyIds[(className, index)]
    except KeyError as exc:
        raise SemanticSnapshotMalformedError(
            f"属性 id 合成表里没有 ({className!r}, {index})"
            " ⇒ 快照的类清单与合成 id 的名单不自洽",
            where=f"class {className!r}",
        ) from exc


def _restoreClass(
    raw: dict[str, Any],
    *,
    classId: int,
    propertyIds: dict[tuple[str, int], int],
    subsetIds: dict[str, int],
) -> SnapshotClass:
    className = _requiredStr(raw, "className", where="class")
    parentName = raw.get("parentClassName")
    parentId = subsetIds.get(parentName) if isinstance(parentName, str) else None
    rawProperties = _strictObjectList(raw, "properties", where=f"class {className!r}")
    properties: list[SnapshotProperty] = []
    for index, rawProp in enumerate(rawProperties):
        properties.append(
            _restoreProperty(
                rawProp,
                classId=classId,
                propertyId=_propertyIdAt(propertyIds, className, index),
                subsetIds=subsetIds,
            )
        )
    where = f"class {className!r}"
    return SnapshotClass(
        id=classId,
        class_name=className,
        class_alias=_optionalStr(raw, "classAlias", where=where),
        description=_optionalStr(raw, "description", where=where),
        source_table=_optionalStr(raw, "sourceTable", where=where),
        object_type=_optionalStr(raw, "objectType", where=where),
        object_owner=_optionalStr(raw, "objectOwner", where=where),
        parent_class_id=parentId,
        properties=tuple(properties),
    )


def _restoreProperty(
    raw: dict[str, Any],
    *,
    classId: int,
    propertyId: int,
    subsetIds: dict[str, int],
) -> SnapshotProperty:
    refName = raw.get("refClassName")
    refId = subsetIds.get(refName) if isinstance(refName, str) else None
    where = f"property {raw.get('propertyName')!r}"
    return SnapshotProperty(
        id=propertyId,
        class_id=classId,
        property_name=_requiredStr(raw, "propertyName", where=where),
        property_alias=_optionalStr(raw, "propertyAlias", where=where),
        business_aliases=_strictStringList(raw, "businessAliases", where=where),
        description=_optionalStr(raw, "description", where=where),
        data_type=_requiredStr(raw, "dataType", where=where),
        is_primary_key=_strictBool(raw, "isPrimaryKey", where=where, default=False),
        is_foreign_key=_strictBool(raw, "isForeignKey", where=where, default=False),
        ref_class_id=refId,
        source_column=_optionalStr(raw, "sourceColumn", where=where),
        unit=_optionalStr(raw, "unit", where=where),
        allowed_values=_strictStringList(raw, "allowedValues", where=where),
        is_not_null=_strictBool(raw, "isNotNull", where=where, default=None),
        min_value=_optionalStr(raw, "minValue", where=where),
        max_value=_optionalStr(raw, "maxValue", where=where),
        regex_pattern=_optionalStr(raw, "regexPattern", where=where),
    )


def _strictObjectList(
    raw: dict[str, Any],
    key: str,
    *,
    where: str,
) -> list[dict[str, Any]]:
    """严格对象列表：缺省 `[]`；不是列表、或元素不是对象 ⇒ 畸形。

    为什么不能「跳过非法元素」：跳过去 = **静默丢掉一条属性**。被丢掉的
    恰好是召回要用的那条时，schema 文本少一列，模型照着残缺的元数据写 SQL，
    **全程无报错**。宁可整份快照判畸形、降级直读，也不要残缺的事实。

    为什么不能「让 Python 自己报错」：`list("ABC")` / `raw[0].get(...)` 会抛
    `AttributeError` / `KeyError` —— 那类异常不在复原层的异常契约里
    ⇒ 直接穿透到问答路径 = 500（这一层存在的意义就是拦住它们）。
    """
    if key not in raw or raw[key] is None:
        return []
    value = raw[key]
    if not isinstance(value, list):
        raise SemanticSnapshotMalformedError(
            f"{where} 的 {key} 不是列表：{value!r}", where=where
        )
    for item in value:
        if not isinstance(item, dict):
            raise SemanticSnapshotMalformedError(
                f"{where} 的 {key} 里存在非对象元素：{item!r}", where=where
            )
    return value


def _requiredStr(
    raw: dict[str, Any],
    key: str,
    *,
    where: str,
) -> str:
    """必填字符串：缺省 / 非字符串 / 空串 ⇒ 畸形。

    manifest 的 JSON Schema 把这些字段列为 **required**（`_object` 默认
    ``required = list(properties)``）⇒ 缺它只可能是绕过发布路径写进来的坏数据。

    为什么不强转：`str(123)` = "123" 看着无害，但 `str(None)` = "" 会让
    「属性名 / 数据类型」静默变成空串 ⇒ schema 文本少一列 ⇒ 模型照着残缺的
    元数据写 SQL，**全程无报错、无告警**。宁可整份快照判畸形、降级直读。
    """
    if key not in raw or raw[key] is None:
        raise SemanticSnapshotMalformedError(
            f"{where} 缺必填字段 {key}", where=where
        )
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise SemanticSnapshotMalformedError(
            f"{where} 的 {key} 不是非空字符串：{value!r}", where=where
        )
    return value


def _optionalStr(
    raw: dict[str, Any],
    key: str,
    *,
    where: str,
) -> str | None:
    """可空字符串：缺省 / `null` ⇒ `None`；是字符串 ⇒ 原样；其余 ⇒ 畸形。

    manifest 的字符串字段在 JSON Schema 里是 `string | null`（契约如此），
    复原层不发明第二套方言（与 `_strictBool` 同一原则）。
    """
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if isinstance(value, str):
        return value
    raise SemanticSnapshotMalformedError(
        f"{where} 的 {key} 不是字符串：{value!r}", where=where
    )


def _strictBool(
    raw: dict[str, Any],
    key: str,
    *,
    where: str,
    default: bool | None,
) -> bool | None:
    """严格布尔取值：**只接受真正的 `bool`**，其余一律判畸形。

    `bool("false")` 是 `True` —— 一个非主键会被当成主键渲染进 schema 文本，
    LLM 照着错的元数据写 SQL，而**全程无报错、无告警**。这比直接崩掉危险得多。

    为什么不接受 `"true"` / `"false"` 字符串（宽容解析）：快照的契约由 JSON
    Schema 定死（布尔字段就是布尔），复原层再发明一套「字符串也认」的方言 =
    第二套真相来源。契约外的东西一律走降级（直读治理表），不重新解释。
    """
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if isinstance(value, bool):
        return value
    raise SemanticSnapshotMalformedError(
        f"{where} 的 {key} 不是布尔值：{value!r}（拒绝按 Python 语义强转）",
        where=where,
    )


def _strictStringList(
    raw: dict[str, Any],
    key: str,
    *,
    where: str,
) -> tuple[str, ...]:
    """严格字符串列表：缺省 `[]`；不是列表、元素不是字符串 ⇒ 畸形。

    `list("a,b")` 会得到 `['a', ',', 'b']` —— 别名被逐字符拆开渲染进 schema
    文本，模型看到的是「供、应、，、商」这种垃圾。

    元素也必须**真的是字符串**：`str(item)` 会把 `123` 变成 "123"、把 `None`
    变成 "None" ⇒ 静默渲染出一列垃圾元数据（模型照着它写 SQL，无报错）。
    """
    if key not in raw or raw[key] is None:
        return ()
    value = raw[key]
    if not isinstance(value, list):
        raise SemanticSnapshotMalformedError(
            f"{where} 的 {key} 不是列表：{value!r}", where=where
        )
    for item in value:
        if not isinstance(item, str):
            raise SemanticSnapshotMalformedError(
                f"{where} 的 {key} 里存在非字符串元素：{item!r}", where=where
            )
    return tuple(value)


def _restoreJoins(manifest: dict[str, Any], subsetIds: dict[str, int]) -> list[SnapshotJoin]:
    """全量 join 的复原（两端按子集解析，子集外为 `None`）。

    与 ORM 路径同口径：那边传的是**全量** join（`listJoins`），由渲染器按
    `tableByClassId`（= 召回子集）过滤掉两端不在子集里的边。
    """
    rawJoins = manifest.get("joins") if isinstance(manifest, dict) else None
    if not isinstance(rawJoins, list):
        return []

    joins: list[SnapshotJoin] = []
    for index, raw in enumerate(rawJoins):
        if not isinstance(raw, dict):
            raise SemanticSnapshotMalformedError(
                f"joins 里存在非对象元素：{raw!r}", where="joins"
            )
        sourceName = raw.get("sourceClassName")
        targetName = raw.get("targetClassName")
        where = f"join {raw.get('joinKey')!r}"
        joins.append(
            SnapshotJoin(
                id=index + 1,
                source_class_id=subsetIds.get(sourceName) if isinstance(sourceName, str) else None,
                source_columns=_strictStringList(raw, "sourceColumns", where=where),
                target_class_id=subsetIds.get(targetName) if isinstance(targetName, str) else None,
                target_columns=_strictStringList(raw, "targetColumns", where=where),
                join_type=_optionalStr(raw, "joinType", where=where) or _DEFAULT_JOIN_TYPE,
                relation_type=(
                    _optionalStr(raw, "relationType", where=where) or _DEFAULT_RELATION_TYPE
                ),
                description=_optionalStr(raw, "description", where=where),
                join_key=(
                    _optionalStr(raw, "joinKey", where=where)
                    or f"{sourceName}->{targetName}"
                ),
            )
        )
    return joins
