"""设计稿 §5.5 的 6 个校验码（G2）：类 / 业务对象 / 关系 / Agent / 图 / KPI。

对照《采购本体模型与语义编译器详细设计 V0.1》§5.5 的校验规则目录，
此前只覆盖约一半（MET-001 三码、JOIN 孤岛、属性未绑列）。本模块补齐：

| code | 校验对象 | 判据 | severity |
|---|---|---|---|
| `CLS-001` | Class | 类名唯一、objectType 合法、父子继承无环 | ERROR |
| `CLS-002` | BusinessObject | 头类存在、主域存在且**已启用** | ERROR |
| `REL-001` | 关系 | JOIN 两端类存在 + 活库语义关系类型在白名单 / 端点未失效 | ERROR |
| `AGT-001` | Agent | 数据域/数据层在启用词表、绑定工具已注册且启用 | WARNING |
| `GRF-001` | Graph | 图 label 在 Neo4j 白名单、头类有主键属性可稳定定位 | WARNING |
| `MET-002` | KPI | 已发布 KPI 绑定有效数据源、关联指标已发布 | WARNING |

## 为什么 CLS/REL 的判据与 `MANIFEST_SCHEMA` 部分重叠

`MANIFEST_SCHEMA`（契约闸）已经在**产物侧**校验了「类名唯一 / 父类存在 / JOIN 端点存在」，
再出 CLS-001 / REL-001 不是重复劳动：

- 契约闸只给**一句**「Manifest 不满足契约」；校验码要回答「**哪一条**规则、涉及**哪些对象**、
  有多严重」——评审读的是校验码目录，不是契约错误串；
- **继承成环**契约闸根本不查（JSON Schema 与引用闸都表达不了），这是 CLS-001 的真新增；
- 关系类型白名单、端点墓碑只有**活库**知道（`ontology_relation`），不能从快照推。

⇒ 契约闸失败时两条结论会同时出现（契约行 + 具体码行），这是刻意的**证据分层**。

## 为什么 AGT-001 / 部分 REL-001 / MET-002 的「不存在」判据需要 context

- `AGT-001`：Agent 定义**不进 manifest**（manifest 无 agents 分区）⇒ 只能读活库；
  「没查过」时**不出行**（`None` 语义，见 `ReleaseCheckContext`）。
- `REL-001`：语义关系表 `ontology_relation` 不在 manifest 里 ⇒ 只能读活库。
- `MET-002`：快照只记数据源**名字**（id 是自增主键，入档会破坏跨环境字节一致）⇒
  「这个名字还有没有、是否停用」只能实时查。

## 为什么 CLS/REL/GRF 出**一条汇总行**，MET-002 出**逐 KPI 行**

- 汇总行的条数上界是**常数**（每码 1 条）⇒ 发布校验项上限可以精确地从契约推导；
- CLS 逐类 / REL 逐 JOIN 会让上界暴涨到数万（契约允许 5000 类 / 20000 JOIN），
  一次 validate 就要往 `semantic_release_check` 写几万行 —— 写放大而且没人读；
- KPI 上限 2000 且「哪个 KPI 没绑源」必须逐条可操作 ⇒ 逐 KPI 落表（与指标级校验同型）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.enums import ObjectType
from app.domain.graph_allowlist import BUSINESS_ENTITY_LABELS
from app.services.semantic_release_check_types import (
    RESULT_FAIL,
    RESULT_PASS,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    TARGET_BUSINESS_OBJECT,
    TARGET_CLASS,
    TARGET_KPI,
    TARGET_RELEASE,
    PublishCheck,
)
from app.services.semantic_release_domain_checks import ReleaseCheckContext

# ---- 校验项编码（机器可读，与设计稿 §5.5 对齐）----
CHECK_CLASS_INTEGRITY = "CLS-001"
CHECK_BUSINESS_OBJECT_INTEGRITY = "CLS-002"
CHECK_RELATION_INTEGRITY = "REL-001"
CHECK_AGENT_INTEGRITY = "AGT-001"
CHECK_GRAPH_ALLOWLIST = "GRF-001"
CHECK_KPI_DATASOURCE = "MET-002"

# ---- 校验项名称（中文，展示用）----
ITEM_CLASS_INTEGRITY = "类定义完整性"
ITEM_BUSINESS_OBJECT_INTEGRITY = "业务对象完整性"
ITEM_RELATION_INTEGRITY = "关系完整性"
ITEM_AGENT_INTEGRITY = "Agent 配置一致性"
ITEM_GRAPH_ALLOWLIST = "图谱白名单"
ITEM_KPI_DATASOURCE = "KPI 数据源绑定"

#: `ontology_class.object_type` 的合法值（None = 未分类，允许）。
_OBJECT_TYPES = frozenset(item.value for item in ObjectType)

#: 一条结论里最多列几条问题（超长 detail 会让评审读不下去）。
_MAX_DETAIL_ISSUES = 10


def evaluateDesignChecks(
    manifest: Mapping[str, Any], *, context: ReleaseCheckContext
) -> tuple[PublishCheck, ...]:
    """6 个设计码（纯函数；活库事实由调用方放进 context）。"""
    checks: list[PublishCheck] = [
        _classCheck(manifest),
        _businessObjectCheck(manifest),
        _relationCheck(manifest, context),
        _graphCheck(manifest),
        *_kpiChecks(manifest, context),
    ]
    agentCheck = _agentCheck(context)
    if agentCheck is not None:
        checks.append(agentCheck)
    return tuple(checks)


# ---------------------------------------------------------------------------
# CLS-001 类定义
# ---------------------------------------------------------------------------


def _classCheck(manifest: Mapping[str, Any]) -> PublishCheck:
    """类名唯一 + objectType 合法 + 父子继承无环。"""
    classes = _partition(manifest, "classes")
    names = [
        name
        for name in (cls.get("className") for cls in classes)
        if isinstance(name, str) and name
    ]
    nameSet = set(names)
    issues: list[str] = _duplicateIssues(names)
    parentByName: dict[str, str | None] = {}
    for cls in classes:
        name = cls.get("className")
        if not isinstance(name, str) or not name:
            continue
        objectType = cls.get("objectType")
        if objectType is not None and objectType not in _OBJECT_TYPES:
            issues.append(f"类 {name} 的 objectType={objectType!r} 非法")
        parent = cls.get("parentClassName")
        parentByName[name] = parent if isinstance(parent, str) else None
        if isinstance(parent, str) and parent not in nameSet:
            issues.append(f"类 {name} 的父类 {parent} 不存在")
    cycle = _findCycle(parentByName)
    if cycle:
        issues.append(f"继承环：{' → '.join(cycle)}")
    return _summaryRow(
        code=CHECK_CLASS_INTEGRITY,
        item=ITEM_CLASS_INTEGRITY,
        severity=SEVERITY_ERROR,
        targetType=TARGET_CLASS,
        issues=issues,
        passDetail=(
            f"{len(names)} 个类：类名唯一、objectType 合法、继承无环"
        ),
    )


def _duplicateIssues(names: list[str]) -> list[str]:
    """重复类名（同名多行 ⇒ 「这个类指哪一行」变成多解）。"""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    return [f"类名重复：{name}" for name in sorted(duplicates)]


def _findCycle(parentByName: Mapping[str, str | None]) -> list[str] | None:
    """返回值形如 `["A", "B", "A"]` 的环（无环 ⇒ None）。

    迭代式着色（0 未访问 / 1 在栈上 / 2 已完成），O(n)：
    递归实现在 5000 类的深链上会撞 Python 递归上限。
    """
    state: dict[str, int] = {}
    for start in parentByName:
        if state.get(start):
            continue
        path: list[str] = []
        node: str | None = start
        while node is not None and state.get(node, 0) == 0:
            state[node] = 1
            path.append(node)
            node = parentByName.get(node)
        if node is not None and state.get(node) == 1:
            return [*path[path.index(node) :], node]
        for visited in path:
            state[visited] = 2
    return None


# ---------------------------------------------------------------------------
# CLS-002 业务对象
# ---------------------------------------------------------------------------


def _businessObjectCheck(manifest: Mapping[str, Any]) -> PublishCheck:
    """头类存在 + 主域存在且已启用。"""
    classNames = {
        name
        for name in (cls.get("className") for cls in _partition(manifest, "classes"))
        if isinstance(name, str)
    }
    domains = {
        domain.get("code"): domain.get("enabled")
        for domain in _partition(manifest, "domains")
        if isinstance(domain.get("code"), str)
    }
    businessObjects = _partition(manifest, "businessObjects")
    issues: list[str] = []
    for businessObject in businessObjects:
        code = businessObject.get("code")
        header = businessObject.get("headerClassName")
        if header not in classNames:
            issues.append(f"业务对象 {code} 的头类 {header!r} 不存在")
        domainCode = businessObject.get("primaryDomainCode")
        if domainCode not in domains:
            issues.append(f"业务对象 {code} 的主域 {domainCode!r} 不存在")
        elif domains.get(domainCode) is False:
            issues.append(f"业务对象 {code} 的主域 {domainCode} 已停用")
    return _summaryRow(
        code=CHECK_BUSINESS_OBJECT_INTEGRITY,
        item=ITEM_BUSINESS_OBJECT_INTEGRITY,
        severity=SEVERITY_ERROR,
        targetType=TARGET_BUSINESS_OBJECT,
        issues=issues,
        passDetail=f"{len(businessObjects)} 个业务对象：头类存在、主域存在且已启用",
    )


# ---------------------------------------------------------------------------
# REL-001 关系
# ---------------------------------------------------------------------------


def _relationCheck(
    manifest: Mapping[str, Any], context: ReleaseCheckContext
) -> PublishCheck:
    """JOIN 两端类存在（快照） + 活库语义关系类型白名单 / 端点墓碑。"""
    classNames = {
        name
        for name in (cls.get("className") for cls in _partition(manifest, "classes"))
        if isinstance(name, str)
    }
    joins = _partition(manifest, "joins")
    issues: list[str] = []
    for join in joins:
        joinKey = join.get("joinKey")
        for field in ("sourceClassName", "targetClassName"):
            value = join.get(field)
            if value not in classNames:
                issues.append(f"JOIN {joinKey} 的 {field}={value!r} 不存在")
    live = context.relationViolations
    if live is not None:
        issues.extend(live)
    passDetail = f"{len(joins)} 条 JOIN 两端类存在"
    if live is None:
        passDetail += "；活库语义关系未查（本次不判）"
    else:
        passDetail += "；活库语义关系无非法类型 / 失效端点"
    return _summaryRow(
        code=CHECK_RELATION_INTEGRITY,
        item=ITEM_RELATION_INTEGRITY,
        severity=SEVERITY_ERROR,
        targetType=TARGET_RELEASE,
        issues=issues,
        passDetail=passDetail,
    )


# ---------------------------------------------------------------------------
# AGT-001 Agent
# ---------------------------------------------------------------------------


def _agentCheck(context: ReleaseCheckContext) -> PublishCheck | None:
    """Agent 域/层/工具一致性（**没查过 ⇒ 不出行**）。

    Agent 定义不进 manifest ⇒ 这是纯活库事实；`None` 表示调用方没查，
    写一句「Agent 全部正常」是没依据的结论（比缺一行更坏）。
    """
    violations = context.agentViolations
    if violations is None:
        return None
    return _summaryRow(
        code=CHECK_AGENT_INTEGRITY,
        item=ITEM_AGENT_INTEGRITY,
        severity=SEVERITY_WARNING,
        targetType=TARGET_RELEASE,
        issues=list(violations),
        passDetail="启用的 Agent：数据域/数据层均在词表内，绑定工具已注册且启用",
    )


# ---------------------------------------------------------------------------
# GRF-001 图谱白名单
# ---------------------------------------------------------------------------


def _graphCheck(manifest: Mapping[str, Any]) -> PublishCheck:
    """图 label 在 Neo4j 白名单 + 入图对象有主键属性（可稳定定位）。

    只判 `graphLabel` 非空的业务对象：不打算入图的对象不该被图规则约束。
    severity=WARNING：label 不在白名单**很多是为了不入图**（如 DWD 中间层对象），
    阻断它等于要求每个治理对象都必须进 Neo4j 业务图 —— 设计稿没这个意思。
    """
    classByName = {
        cls.get("className"): cls
        for cls in _partition(manifest, "classes")
        if isinstance(cls.get("className"), str)
    }
    issues: list[str] = []
    graphed = 0
    for businessObject in _partition(manifest, "businessObjects"):
        label = businessObject.get("graphLabel")
        if not label:
            continue
        graphed += 1
        code = businessObject.get("code")
        if label not in BUSINESS_ENTITY_LABELS:
            allowed = "、".join(sorted(BUSINESS_ENTITY_LABELS))
            issues.append(
                f"业务对象 {code} 的图 label {label} 不在 Neo4j 业务实体白名单"
                f"（{allowed}）"
            )
        header = classByName.get(businessObject.get("headerClassName"))
        if header is not None and _declaresProperties(header) and not _hasPrimaryKey(header):
            issues.append(f"业务对象 {code} 的头类无主键属性 ⇒ 图节点无法稳定定位")
    return _summaryRow(
        code=CHECK_GRAPH_ALLOWLIST,
        item=ITEM_GRAPH_ALLOWLIST,
        severity=SEVERITY_WARNING,
        targetType=TARGET_RELEASE,
        issues=issues,
        passDetail=f"{graphed} 个入图业务对象：label 在白名单内、头类有主键属性",
    )


def _declaresProperties(cls: Mapping[str, Any]) -> bool:
    """类**声明了属性**才谈得上「哪个是主键」。

    ⚠️ 空属性列表 ⇒ **不判主键**（不是 FAIL）：一个类压根没声明属性，
    是属性治理（PRP）的问题，不是图定位的问题；拿它报 GRF-001 会让
    每条「只有类、还没补属性」的发布都背一条无行动价值的告警。
    """
    return bool(cls.get("properties"))


def _hasPrimaryKey(cls: Mapping[str, Any]) -> bool:
    return any(
        isinstance(prop, Mapping) and prop.get("isPrimaryKey") is True
        for prop in cls.get("properties") or []
    )


# ---------------------------------------------------------------------------
# MET-002 KPI 数据源
# ---------------------------------------------------------------------------


def _kpiChecks(
    manifest: Mapping[str, Any], context: ReleaseCheckContext
) -> list[PublishCheck]:
    """已发布 KPI：绑定有效数据源 + 关联指标已发布（逐 KPI 落表）。

    只判 `status=PUBLISHED` 的 KPI：草稿 KPI 允许先于技术实现存在（设计 §5.6
    的弱关联），对草稿报「没绑数据源」会让报告被噪音淹没。
    """
    metricsByName = {
        metric.get("metricName"): metric
        for metric in _partition(manifest, "metrics")
        if isinstance(metric.get("metricName"), str)
    }
    checks: list[PublishCheck] = []
    for kpi in _partition(manifest, "kpis"):
        if kpi.get("status") != "PUBLISHED":
            continue
        kpiCode = str(kpi.get("kpiCode"))
        checks.append(_kpiCheck(kpi, kpiCode, metricsByName, context))
    if checks:
        return checks
    return [
        PublishCheck(
            code=CHECK_KPI_DATASOURCE,
            item=ITEM_KPI_DATASOURCE,
            severity=SEVERITY_WARNING,
            result=RESULT_PASS,
            targetType=TARGET_RELEASE,
            detail="没有已发布（status=PUBLISHED）的 KPI，本项不适用",
        )
    ]


def _kpiCheck(
    kpi: Mapping[str, Any],
    kpiCode: str,
    metricsByName: Mapping[str, Mapping[str, Any]],
    context: ReleaseCheckContext,
) -> PublishCheck:
    issues = _kpiIssues(kpi, metricsByName, context.activeDatasourceNames)
    return _summaryRow(
        code=CHECK_KPI_DATASOURCE,
        item=ITEM_KPI_DATASOURCE,
        severity=SEVERITY_WARNING,
        targetType=TARGET_KPI,
        targetKey=kpiCode,
        issues=issues,
        passDetail=f"KPI {kpiCode} 已绑定启用中的数据源，关联指标已发布",
    )


def _kpiIssues(
    kpi: Mapping[str, Any],
    metricsByName: Mapping[str, Mapping[str, Any]],
    activeDatasourceNames: frozenset[str] | None,
) -> list[str]:
    issues: list[str] = []
    datasourceName = kpi.get("datasourceName")
    if not datasourceName:
        issues.append("未绑定数据源")
    elif activeDatasourceNames is not None and datasourceName not in activeDatasourceNames:
        issues.append(f"数据源 {datasourceName!r} 不存在或已停用")
    metricName = kpi.get("metricName")
    metric = metricsByName.get(metricName) if isinstance(metricName, str) else None
    if metric is not None and metric.get("status") != "PUBLISHED":
        issues.append(f"关联指标 {metricName} 仍为 {metric.get('status')}")
    return issues


# ---------------------------------------------------------------------------
# 内部：通用
# ---------------------------------------------------------------------------


def _summaryRow(
    *,
    code: str,
    item: str,
    severity: str,
    targetType: str,
    issues: list[str],
    passDetail: str,
    targetKey: str | None = None,
) -> PublishCheck:
    """有 issue ⇒ FAIL（列出前 N 条 + 总数）；无 issue ⇒ PASS（说清查了什么）。"""
    if not issues:
        return PublishCheck(
            code=code,
            item=item,
            severity=severity,
            result=RESULT_PASS,
            targetType=targetType,
            targetKey=targetKey,
            detail=passDetail,
        )
    listed = "；".join(issues[:_MAX_DETAIL_ISSUES])
    if len(issues) > _MAX_DETAIL_ISSUES:
        listed += f"；等共 {len(issues)} 项"
    return PublishCheck(
        code=code,
        item=item,
        severity=severity,
        result=RESULT_FAIL,
        targetType=targetType,
        targetKey=targetKey,
        detail=listed,
    )


def _partition(manifest: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """取一个分区（缺分区 / 非列表 ⇒ 空列表）。"""
    value = manifest.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
