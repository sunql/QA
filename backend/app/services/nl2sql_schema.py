"""schema 文本渲染 + JOIN 图（从 nl2sql_service 拆出）。

把本体类列表渲染成 LLM 可读的 schema 文本（含外键 JOIN 关系、继承层级、关键口径
摘要、值域采样），并维护运行时 JOIN 的唯一真源（join 目录 → 邻接表 → BFS 路径）。
同时收纳 schema 字段净化原语（_sanitizeContext / _sanitizeSchemaField / _safeSchemaPrefix），
供 prompts 与 plan 复用。

未来扩展：schema 摘要策略 / join 策略演进只改本模块。
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from app.domain.models import OntologyClass, OntologyJoin
from app.domain.query_plan import JoinSpec, QueryPlan

logger = logging.getLogger(__name__)


def _sanitizeContext(context: str) -> str:
    """转义用户历史中的尖括号，使其无法构造任何标签逃逸出包装。

    相比删除标签更彻底：分片拼接（`</conversation_his<conversation_history>tory>`）、
    大小写变体（`</CONVERSATION_HISTORY>`）或自闭合/带属性变体都无法重组出
    闭合标签——因为所有 `<`/`>` 已被替换为 HTML 实体，LLM 只将其视为字面文本。
    """
    return context.replace("<", "&lt;").replace(">", "&gt;")


def _sanitizeSchemaField(value: str) -> str:
    """本体配置字段（别名/描述）渲染前的净化：转义尖括号 + 折叠换行。

    换行不折叠会向 schema 文本注入裸行，形成"忽略规则"式指令行；本体配置虽是
    管理员写入，但属外部输入，按同一防御标准处理。
    """
    return _sanitizeContext(value).replace("\r", " ").replace("\n", " ")


# schema 前缀（数据源用户名）仅允许合法标识符，防止提示注入
_SCHEMA_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safeSchemaPrefix(schemaPrefix: str | None) -> str | None:
    """仅放行合法标识符的 schema 前缀（来自数据源用户名），防止提示注入。

    非法值返回 None 并告警（不记录原始值，避免配置数据落日志），而非原样注入 prompt。
    """
    if schemaPrefix is None:
        return None
    if _SCHEMA_PREFIX_RE.match(schemaPrefix):
        return schemaPrefix
    logger.warning("schema 前缀非法，已忽略（仅允许字母/数字/下划线）")
    return None


def _bakeTableName(name: str, safePrefix: str | None) -> str:
    """把 schema 前缀烘焙进表名（0-4）：让 schema 文本里的表名已是完整限定形式，
    LLM 更可能照搬而非自行拼凑。已带该前缀的表名原样返回，避免双重前缀。
    """
    if safePrefix and not name.startswith(f"{safePrefix}."):
        return f"{safePrefix}.{name}"
    return name


def _renderJoinColumns(bakedTable: str, columns: list[str]) -> str:
    """渲染 join 单侧列：单列 `表.列`，多列 `表.列1 + 表.列2`（全部完整限定，避免歧义）。"""
    return " + ".join(f"{bakedTable}.{c}" for c in columns)


# =========================================================================
# JOIN 图（Feature B）：外键邻接表 + BFS 路径发现
# =========================================================================

# JOIN 图类型：table → [(refTable, viaColumn), ...]
JoinGraph = dict[str, list[tuple[str, str]]]


def _buildJoinGraph(
    classes: list[OntologyClass], joins: list[OntologyJoin] | None
) -> JoinGraph:
    """从 join 目录构建关联邻接表（运行时 JOIN 的唯一真源）。

    只保留两端都能在相关类子集中解析到 source_table 的边（跳过软删除/不在子集的类）。
    viaColumn 取第一个源列作可达性提示，不参与 SQL 生成（planToText 不渲染 joins）。
    """
    graph: JoinGraph = {}
    if not joins:
        return graph
    tableByClassId = {
        cls.id: cls.source_table
        for cls in classes
        if cls.id is not None and cls.source_table
    }
    for join in joins:
        srcTable = tableByClassId.get(join.source_class_id)
        tgtTable = tableByClassId.get(join.target_class_id)
        if not srcTable or not tgtTable:
            continue
        via = join.source_columns[0] if join.source_columns else ""
        graph.setdefault(srcTable, []).append((tgtTable, via))
        # 双向记录（join 边单向定义，但 JOIN 遍历需要双向）
        graph.setdefault(tgtTable, []).append((srcTable, via))
    return graph


def _findJoinPath(
    source: str, target: str, graph: JoinGraph
) -> list[str] | None:
    """BFS 找两张表之间的最短 JOIN 路径，返回中间表列表（不含 source/target）。

    若 source==target 返回空列表；无路径返回 None。
    """
    if source == target:
        return []
    visited: set[str] = {source}
    queue: list[tuple[str, list[str]]] = [(source, [])]

    while queue:
        node, path = queue.pop(0)
        for neighbor, _ in graph.get(node, []):
            if neighbor == target:
                return path  # 找到终点，中间表已在 path 中
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))
    return None


def _resolveRefTable(
    prop: Any, classesById: dict[int, OntologyClass]
) -> str | None:
    """解析外键引用目标表：优先已加载的 relationship，其次按 ref_class_id 查列表。

    仅在 relationship 已加载时访问，避免对 detached 实例触发懒加载。
    """
    if "ref_class" in prop.__dict__:
        ref = prop.__dict__["ref_class"]
        if ref is not None:
            return ref.source_table or None
    target = classesById.get(prop.ref_class_id) if prop.ref_class_id else None
    return target.source_table if target else None


# schema 文本中未映射到真实库列的属性标记（2-3）：source_column 为空时不再回退
# property_name，避免 LLM 拿业务名当列名产出真实库不存在的列
_UNMAPPED_COLUMN_MARKER = "未映射"

# 关键过滤口径摘要硬上限（feat-schema-digest, 2026-09-21）。
# 防恶意管理员堆 description 把 schema prompt 撑爆：单次调用渲染的摘要条数与
# 单条描述长度都做硬封顶（security-reviewer MEDIUM）。
# 实测生产 PG 有 1731 个属性，仅 10 条带 description，50/200 远超现状。
# 运行期从 system_config 现读（魔数治理 Phase 2 hard tier），缺席/格式错返 _DEFAULT。
_CRITICAL_DIGEST_MAX_ITEMS_DEFAULT = 50
_CRITICAL_DIGEST_MAX_DESC_CHARS_DEFAULT = 200

# 值域采样（2-1）：单值在 schema 文本中的字符上限，超长截断防止 prompt 膨胀。
# 运行期从 system_config 现读（魔数治理 Phase 2 hard tier）。
_VALUE_SAMPLE_VALUE_MAX_DEFAULT = 30


def _sampleValuesFor(
    prop: Any, table: str, valueSamples: dict[tuple[str, str], list[str]] | None
) -> list[str] | None:
    """按 (表, 真实列) 取值域采样；未映射列或未提供采样时返回 None。"""
    if not valueSamples or not prop.source_column:
        return None
    return valueSamples.get((table, prop.source_column))


def _formatSampleValue(value: Any, valueMax: int = _VALUE_SAMPLE_VALUE_MAX_DEFAULT) -> str:
    """格式化单个采样值为 SQL 字面量提示：截断 + 转义引号/尖括号（防误导与数据注入）。

    内嵌单引号按 SQL 标准加倍（O'Brien → O''Brien），否则 LLM 照抄会写出断裂的字面量。
    尖括号经 _sanitizeContext 转义，DB 数据无法构造标签逃逸出包装。
    valueMax 运行期从 system_config.VALUE_SAMPLE_VALUE_MAX 现读（魔数治理 Phase 2）。
    """
    text = str(value).strip()
    if len(text) > valueMax:
        text = text[:valueMax] + "…"
    text = text.replace("'", "''")
    return f"'{_sanitizeContext(text)}'"


def _pruneClassesForSql(
    plan: QueryPlan | None,
    classes: list[OntologyClass],
) -> list[OntologyClass]:
    """SQL 阶段 schema 裁剪：只保留 plan 实际引用的类（feat-token-prune，2026-09-28）。

    只裁 SQL 阶段的 schema 文本——`generateSql` 里 `classes` 仅用于
    `buildSchemaText`（plan 校验 / SQL Guard / planToText 都不碰它），故本函数
    是纯收益、不动语义。

    口径 = `plan.selectedClasses` ∪ `plan.joins[].sourceClass/targetClass`。
    joins 那一半不可省：`supplementJoinPath` 补的中间 hop 类**只写进 plan.joins、
    不进 selectedClasses**，而 SQL 阶段获取 JOIN 知识的唯一来源是 schema 文本的
    「### JOIN 关系」段（planToText 不渲染 joins），漏掉 hop 类就会漏 JOIN 行。
    引用名一律是 class_name（与 validatePlan 的 `{cls.class_name: cls}` 同口径）。

    fail-closed：plan 为 None、无任何引用、或**有引用名反查不到类**时原样返回
    全量 classes。因为 `buildSchemaText` 对缺类只做**静默降级**（JOIN 行跳过、
    继承标注丢失、FK 退化裸 FK），缺表既不报错也不告警，只会生成错 SQL——
    宁可多投 token，不裁出一个缺表的 schema。
    """
    if plan is None or not classes:
        return classes
    needed: set[str] = set(plan.selectedClasses or ())
    for join in plan.joins or ():
        needed.add(join.sourceClass)
        needed.add(join.targetClass)
    if not needed:
        return classes
    pruned = [cls for cls in classes if cls.class_name in needed]
    if len(pruned) < len(needed):
        return classes
    return pruned


def buildSchemaText(
    classes: list[OntologyClass],
    *,
    schemaPrefix: str | None = None,
    valueSamples: dict[tuple[str, str], list[str]] | None = None,
    driftWarning: str | None = None,
    joins: list[OntologyJoin] | None = None,
    digestMaxItems: int = _CRITICAL_DIGEST_MAX_ITEMS_DEFAULT,
    digestMaxDescChars: int = _CRITICAL_DIGEST_MAX_DESC_CHARS_DEFAULT,
    valueSampleValueMax: int = _VALUE_SAMPLE_VALUE_MAX_DEFAULT,
) -> str:
    """将本体类列表渲染为 LLM 可读的 schema 文本（含外键 JOIN 关系与继承层级）。

    父类先于子类输出（拓扑排序），子类标题标注 ``(继承 父类名)``，
    让 LLM 理解子类共享父类的语义与列结构。

    schemaPrefix（0-4）：经 _safeSchemaPrefix 放行后，把前缀烘焙进表名，
    使 schema 文本里的 table= 头与 JOIN 行都已是完整限定形式（如 APP.PRECEIPT），
    降低 LLM 漏写前缀的概率。非前缀方言（MySQL/PG）传 None 即不烘焙。
    """
    safePrefix = _safeSchemaPrefix(schemaPrefix)
    classesById = {cls.id: cls for cls in classes if cls.id is not None}
    ordered = _topoSortByInheritance(classes)
    blocks: list[str] = []

    # 0：关键过滤口径摘要（feat-schema-digest，2026-09-21）。
    # 长 schema（28K+ 字符 / 10+ 类）下，非 PK 列的说明文字易被 LLM 注意力
    # 漏读（真实事故：TCLCOD_0 的「生产型物料」描述全文入 prompt 但 LLM
    # 仍把外协 C079 算入供货量 Top3）。把带 description 的属性集中前置，
    # 让 LLM 第一眼看见口径约束。原文仍保留在下方类块中，不影响 SQL 渲染端。
    digest = _buildCriticalColumnsDigest(
        ordered,
        maxItems=digestMaxItems,
        maxDescChars=digestMaxDescChars,
    )
    if digest:
        blocks.extend(digest)

    for cls in ordered:
        if not cls.source_table:
            continue
        bakedTable = _bakeTableName(cls.source_table, safePrefix)
        header = f"### {cls.class_name}"
        if cls.class_alias:
            header += f" ({cls.class_alias})"
        parent = _resolveParent(cls, classesById)
        if parent is not None:
            header += f" (继承 {parent.class_name})"
        header += f": table={bakedTable}"
        blocks.append(header)
        blocks.append("  Columns:")
        for prop in cls.properties:
            column = prop.source_column or _UNMAPPED_COLUMN_MARKER
            markers: list[str] = []
            if prop.is_primary_key:
                markers.append("PK")
            refTable = _resolveRefTable(prop, classesById)
            if prop.is_foreign_key:
                markers.append(f"FK → {refTable}" if refTable else "FK")
            markerText = "".join(f" [{m}]" for m in markers)
            # 2-2：别名/业务别名/描述渲染进列行，缩写列名（AMT_0）与业务词（营业额）对得上。
            # 三者均来自本体配置（外部输入），经 _sanitizeSchemaField 转义尖括号并折叠换行，
            # 防止标签逃逸或注入"忽略规则"式指令行。
            # 单个别名放名称位（与类头 `### NAME (alias)` 同构），多别名/描述放行尾。
            nameToken = prop.property_name
            if prop.property_alias:
                nameToken += f" ({_sanitizeSchemaField(prop.property_alias)})"
            line = f"    {nameToken}: {prop.data_type} (column={column}){markerText}"
            if prop.business_aliases:
                line += (
                    " 业务别名: ["
                    + ", ".join(_sanitizeSchemaField(a) for a in prop.business_aliases)
                    + "]"
                )
            if prop.description:
                line += f" 说明: {_sanitizeSchemaField(prop.description)}"
            # 2-1：关键列注入值域采样（WHERE 值不再写错）；值经去重/截断/转义
            samples = _sampleValuesFor(prop, cls.source_table, valueSamples)
            if prop.source_column and samples:
                line += f" 值域示例: [{', '.join(_formatSampleValue(v, valueSampleValueMax) for v in samples)}]"
            blocks.append(line)

    if joins:
        joinLines = _renderJoinLines(classes, joins, safePrefix)
        if joinLines:
            blocks.append("### JOIN 关系")
            blocks.extend(joinLines)
    hints = _buildIndirectJoinHints(classes, joins)
    if hints:
        blocks.append(hints)
    # 2-4：漂移告警追加在末尾（来自 buildDriftWarning，非空时注入）。
    # schema 文本仍渲染本体全部类（含已漂移对象），告警明确告知模型不得引用，
    # 避免生成引用数据库中已不存在表/列的 SQL（ORA-00942）。
    if driftWarning:
        blocks.append(driftWarning)
    return "\n".join(blocks)


def _renderJoinLines(
    classes: list[OntologyClass],
    joins: list[OntologyJoin],
    safePrefix: str | None,
) -> list[str]:
    """渲染 JOIN 行（join 目录唯一真源）：只保留两端都能解析到相关类 source_table 的边。"""
    tableByClassId = {
        cls.id: cls.source_table
        for cls in classes
        if cls.id is not None and cls.source_table
    }
    lines: list[str] = []
    for join in joins:
        srcTable = tableByClassId.get(join.source_class_id)
        tgtTable = tableByClassId.get(join.target_class_id)
        if not srcTable or not tgtTable:
            continue
        src = _renderJoinColumns(
            _bakeTableName(srcTable, safePrefix), join.source_columns
        )
        tgt = _renderJoinColumns(
            _bakeTableName(tgtTable, safePrefix), join.target_columns
        )
        line = f"  {src} → {tgt}"
        # description 是管理员写入的外部输入，同样需转义防标签逃逸/指令注入（与属性行同标准）。
        if join.description:
            line += f"  # {_sanitizeSchemaField(join.description)}"
        lines.append(line)
    return lines


def _buildIndirectJoinHints(
    classes: list[OntologyClass], joins: list[OntologyJoin] | None
) -> str:
    """生成 2 跳间接 JOIN 路径提示文本，供 schema prompt 注入。

    找出 A->B->C 路径（A、C 无直接关联），用类名表示。去重并限制数量。
    无间接路径返回空串。
    """
    graph = _buildJoinGraph(classes, joins)
    if not graph:
        return ""
    tableToClass = {
        cls.source_table: cls.class_name
        for cls in classes
        if cls.source_table
    }
    directEdges: set[tuple[str, str]] = set()
    for node, neighbors in graph.items():
        for ref, _ in neighbors:
            directEdges.add(tuple(sorted([node, ref])))
    pathSet: set[tuple[str, str, str]] = set()
    for a, neighbors in graph.items():
        for b, _ in neighbors:
            for c, _ in graph.get(b, []):
                if c == a:
                    continue
                if tuple(sorted([a, c])) in directEdges:
                    continue
                lo, hi = sorted([a, c])
                pathSet.add((lo, b, hi))
    if not pathSet:
        return ""
    lines = sorted(
        f"  {tableToClass.get(lo, lo)} -> {tableToClass.get(b, b)} -> {tableToClass.get(hi, hi)}"
        for lo, b, hi in pathSet
    )
    if len(lines) > 20:
        lines = lines[:20]
    return "### 间接 JOIN 路径参考（无直接外键时可经中间表中转）\n" + "\n".join(lines)


def _resolveParent(
    cls: OntologyClass, classesById: dict[int, OntologyClass]
) -> OntologyClass | None:
    """解析父类：仅在父类存在于当前批次时返回，避免引用列表外的类。"""
    # 优先用已加载的 parent relationship（避免对 detached 实例触发懒加载）
    if "parent" in cls.__dict__:
        parent = cls.__dict__["parent"]
        if parent is not None and parent.id is not None:
            return classesById.get(parent.id)
    if cls.parent_class_id is not None:
        return classesById.get(cls.parent_class_id)
    return None


def _buildCriticalColumnsDigest(
    classes: list[OntologyClass],
    *,
    maxItems: int = _CRITICAL_DIGEST_MAX_ITEMS_DEFAULT,
    maxDescChars: int = _CRITICAL_DIGEST_MAX_DESC_CHARS_DEFAULT,
) -> list[str]:
    """汇总关键过滤口径摘要：收集带非平凡 description 的属性集中前置。

    长 schema 文本（28K+ 字符 / 10+ 类）下，非 PK 列的说明文字易被 LLM
    注意力漏读——真实事故 2026-09-21：用户已在 DIM_IMATERIAL.TCLCOD_0 写入
    「生产型物料」过滤口径描述，PG / Milvus / schema 文本全链路都到位，
    但 LLM 仍把外协供应商 C079 算入供货量 Top3。本方法在 buildSchemaText
    顶部插入一节，把这些关键口径集中前置，让 LLM 第一眼看见。

    仅收集 ``description 长度 ≥ 20`` 的属性：避免主键说明（"主键"等）
    等低信息量字面进入噪声节。原文仍保留在下方类块里，本节不重复渲染到
    SQL 输出端（仅影响 LLM prompt 渲染）。

    返回值是预格式化好的 block 列表（空表示无摘要，跳过此节）。
    description 经 ``_sanitizeSchemaField`` 转义防标签逃逸。
    """
    # 关键业务列关键词（property_name / business_aliases 命中则纳入）。
    # 中文常见业务维度：物料/类别/类型/供应商/工厂/部门/数量/金额/日期/
    # 编码/代码/状态；英文常见：TYP/COD/STATUS/QTY/AMT/NUM/DAT/IDX。
    keywords = (
        "物料", "类别", "类型", "供应商", "工厂", "部门", "数量", "金额",
        "日期", "编码", "代码", "状态", "税率", "价格",
        "TYP", "COD", "STATUS", "QTY", "AMT", "NUM", "DAT", "IDX",
        "TCLCOD",
    )
    items: list[tuple[str, str]] = []
    for cls in classes:
        if not cls.class_name:
            continue
        for prop in cls.properties:
            # property_name 必须非空（与 cls.class_name 一致的 falsy 守卫，
            # 否则 qualified name 会渲染成 "ClassName." 或 "ClassName.None"）
            if not prop.property_name:
                continue
            if not prop.description or len(prop.description) < 20:
                continue
            haystack = " ".join(
                filter(
                    None,
                    [
                        prop.property_name,
                        prop.property_alias or "",
                        *(prop.business_aliases or []),
                    ],
                )
            )
            if not any(kw in haystack for kw in keywords):
                continue
            items.append(
                (
                    f"{cls.class_name}.{prop.property_name}",
                    _sanitizeSchemaField(prop.description),
                )
            )
    if not items:
        return []
    # 按 (class_name, property_name) 排序：保证同类输入下 digest 渲染顺序
    # 完全确定（class 列表输入顺序变了也不影响 LLM 看到的口径摘要位置）。
    items.sort(key=lambda pair: pair[0])
    # 安全：单次 schema 调用渲染的摘要项数与单条描述长度都做硬上限。
    # 防止恶意管理员写海量长 description 把 prompt 撑爆（实测 schema 已 28K+ 字，
    # 再叠 100 条 500 字 ≈ 65K token，会让 plan 阶段输入直接超限）。
    # 真实生产 1731 个属性里只有 10 条带 description，50/200 远高于现状。
    truncated_items = items[:maxItems]
    if len(items) > maxItems:
        logger.warning(
            "_buildCriticalColumnsDigest 截断: 共 %d 条, 仅保留前 %d 条",
            len(items),
            maxItems,
        )
    lines = ["### 关键过滤口径摘要（管理员维护的业务口径，生成查询时必须遵循）"]
    for qualified, desc in truncated_items:
        # 单条描述截断（防单条 500 字被全文灌入摘要）
        if len(desc) > maxDescChars:
            desc = desc[:maxDescChars] + "…"
        lines.append(f"- {qualified}: {desc}")
    return lines


def _topoSortByInheritance(classes: list[OntologyClass]) -> list[OntologyClass]:
    """按继承层级拓扑排序：父类在子类之前，避免子类先于父类出现。

    存在数据环时（不应发生，detectInheritanceCycle 会拦截）仍能终止：
    visited 集合保证每个类只入队一次。id 为 None 的类（如测试构造的 detached
    实体）按对象身份去重，仍正常入队。
    """
    classesById = {cls.id: cls for cls in classes if cls.id is not None}
    visited: set[int] = set()

    def _key(cls: OntologyClass) -> int:
        # id 为 None 时退化为对象身份，保证同实例不被重复入队
        return cls.id if cls.id is not None else id(cls)

    def visit(cls: OntologyClass) -> None:
        key = _key(cls)
        if key in visited:
            return
        visited.add(key)
        parent: OntologyClass | None = None
        if "parent" in cls.__dict__ and cls.__dict__["parent"] is not None:
            parent = cls.__dict__["parent"]
        elif cls.parent_class_id is not None:
            parent = classesById.get(cls.parent_class_id)
        if parent is not None and _key(parent) not in visited:
            visit(parent)
        ordered.append(cls)

    ordered: list[OntologyClass] = []
    # 按类名稳定排序后遍历，保证输出顺序确定
    for cls in sorted(classes, key=lambda c: (c.class_name, c.id or 0)):
        visit(cls)
    return ordered


def supplementJoinPath(
    plan: QueryPlan, classes: list[OntologyClass], joins: list[OntologyJoin] | None
) -> QueryPlan:
    """补充中间表 JOIN：无直接关联的 JOIN 用 BFS 中间路径替换。

    对每条原 JOIN：
    - 有直接关联（join 目录中两端相邻）：保留原样。
    - 无直接关联但有中间路径：用 hop 序列替换原 JOIN（不保留无效原 JOIN）。
    - 无路径：保留原样（交由 validateConnectivity 报错）。
    返回新的 QueryPlan（不可变）；无任何替换时返回原 plan。
    """
    if not plan.joins:
        return plan
    graph = _buildJoinGraph(classes, joins)
    if not graph:
        return plan

    tableToClass: dict[str, str] = {}
    classToTable: dict[str, str] = {}
    for cls in classes:
        if cls.source_table:
            tableToClass[cls.source_table] = cls.class_name
            classToTable[cls.class_name] = cls.source_table

    def _colsBetween(fromTable: str, toTable: str) -> tuple[str, ...]:
        col = next(
            (c for ref, c in graph.get(fromTable, []) if ref == toTable),
            "",
        )
        return (col,) if col else ()

    extra: list[JoinSpec] = []
    changed = False
    for join in plan.joins:
        sTable = classToTable.get(join.sourceClass)
        tTable = classToTable.get(join.targetClass)
        if not sTable or not tTable:
            extra.append(join)
            continue
        hasDirect = any(ref == tTable for ref, _ in graph.get(sTable, []))
        if hasDirect:
            extra.append(join)
            continue
        path = _findJoinPath(sTable, tTable, graph)
        if path is None:
            extra.append(join)
            continue
        hops = [sTable, *path, tTable]
        for i in range(len(hops) - 1):
            a, b = hops[i], hops[i + 1]
            extra.append(JoinSpec(
                sourceClass=tableToClass.get(a, a),
                targetClass=tableToClass.get(b, b),
                columns=_colsBetween(a, b),
            ))
        changed = True

    if not changed:
        return plan
    # P2 bug 修复（2026-08-17）：手工重建 QueryPlan 时漏了 interpretation 字段，
    # 凡是补充过 JOIN 的计划都会丢失"理解"字段（前端计划卡片 + 下游 prompt 都受影响）。
    # 改用 dataclasses.replace 保持不可变且不漏字段。
    return replace(plan, joins=tuple(extra))
