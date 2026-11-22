"""发布校验项（`semantic_release_check`）的纯评估逻辑（M1-1.2）。

计划 §1.2：「发布校验落表：`semantic_release_check`（校验项、severity、result、detail）」；
准则 H + D1 裁决：`procurement_ontology_release_check` **建**，改名为 `semantic_release_check`
（不新建 `procurement/*` 语义重复表）。

本模块**不查库、不写库**：把「一份 manifest 有哪些校验项、各自结论如何」变成
可单测的纯计算。落表与查询在 `semantic_release_check_service.py`。

## 两个字段的分工（刻意正交）

- `severity`：**该校验项失败时的严重级别**。
  `ERROR` = 阻断发布；`WARNING` = 治理债（显式暴露，但不拦）。
- `result`：**该校验项本次的结论**。`PASS` / `FAIL`。

⇒ `PASS` 行仍带 `severity`：它回答「若这条失败会有多严重」，
于是「全 PASS 的报告」也能直接告诉读者每项的风险面。

## 为什么要逐项（而不是只记失败项）

M1-a DoD：「每条 check 落表可查」。只落失败项 ⇒ 评审看到的是空表 ——
「通过」与「没跑」不可区分。⇒ `PASS` 行同样落表。

## 闸门口径零漂移

`checksToGateResult(evaluatePublishChecks(m))` 与既有 `evaluatePublishGate(m)` **完全相等**
（单测逐条比对）。逐项化是「落表」改造，不是闸门口径改造。

## 校验项目录（新增校验项只加常量，不改表结构）

| code | 目标类型 | severity | 判据 |
|---|---|---|---|
| `SNAPSHOT_INTEGRITY` | RELEASE | ERROR | 快照规范化 / 体量 / 哈希一致 |
| `MANIFEST_SCHEMA` | RELEASE | ERROR | Manifest 满足其 `schemaVersion` 对应的契约（v1 / v2） |
| `METRIC_SOURCE_PRESENT` | METRIC | WARNING | 指标有源类引用 |
| `METRIC_NUMERATOR_LANDING` | METRIC | WARNING/ERROR | 指标分子度量有落点类 |
| `METRIC_DENOMINATOR_LANDING` | METRIC | WARNING/ERROR | 指标分母度量有落点类 |
| `CHECKS_TRUNCATED` | RELEASE | ERROR | 结论条数超上限（报告不完整 ⇒ 阻断） |
| `DOMAIN_SLICE_EMPTY` | RELEASE | ERROR | 域切片为空（不能发布空采购快照） |
| `DOMAIN_SLICE_CROSS_DOMAIN` | RELEASE | WARNING | 为满足引用契约补齐了跨域依赖类 |
| `SOURCE_BINDING_CANDIDATE` | RELEASE | WARNING | 仍有未确认（候选）绑定（D-3 治理债） |
| `SOURCE_BINDING_JOIN_MISSING` | BINDING | **ERROR** | 绑定所属类在整个本体里零 JOIN 边（孤岛） |
| `SOURCE_BINDING_PROPERTY_UNBOUND` | BINDING | WARNING | 类已绑源表，但有属性没绑 `sourceColumn` |
| `CLS-001` | CLASS | ERROR | 类名唯一、objectType 合法、父子继承无环 |
| `CLS-002` | BUSINESS_OBJECT | ERROR | 头类存在、主域存在且已启用 |
| `REL-001` | RELEASE | ERROR | JOIN 两端类存在 + 活库语义关系类型 / 端点合法 |
| `AGT-001` | RELEASE | WARNING | Agent 域/层在启用词表、绑定工具已注册且启用 |
| `GRF-001` | RELEASE | WARNING | 图 label 在 Neo4j 白名单、头类有主键属性 |
| `MET-002` | KPI | WARNING | 已发布 KPI 绑定有效数据源、关联指标已发布 |

（`CLS-001`/`CLS-002`/`REL-001`/`AGT-001`/`GRF-001` 是设计稿 §5.5 的校验码，
实现在 `semantic_release_design_checks`（G2）；活库事实由
`semantic_release_context_facts` 装载。）

（M1-1.3 批次 B 把采购域校验项接进同一张表 —— 机制先立，判据后加。）

## 模块分层（批次 B 拆分）

| 模块 | 职责 |
|---|---|
| `semantic_release_check_types` | 类型与常量（叶子模块，零业务依赖） |
| `semantic_release_domain_checks` | 域级 + 绑定级校验项（切片 / 候选 / 孤岛 / 未绑列） |
| `semantic_release_checks`（本模块） | 契约 + 指标级校验项 + 闸门聚合 + 上限封顶 |

类型住在叶子模块的原因：域级模块要 import `PublishCheck`，本模块又要 import
域级模块 ⇒ 类型若只在本模块，两边只能靠参数注入绕环或直接成环。

⚠️ 域级校验项的**编码常量**（`CHECK_DOMAIN_SLICE_*` / `CHECK_SOURCE_BINDING_*`
及其 `ITEM_*` 名称）**只在域级模块里定义**，本模块**不再转出**：转出即产生
「两处都能 import ⇒ 迟早有一处改了另一处没改」的假象（代码审查 LOW-2）。
需要这些常量的消费方（测试、报告渲染）直接 import 域级模块。
"""

from __future__ import annotations

from typing import Any

from app.domain.semantic_manifest_schema import (
    MAX_MANIFEST_KPIS,
    MAX_MANIFEST_METRICS,
    MAX_MANIFEST_SOURCE_BINDINGS,
    ManifestSchemaError,
    validateManifest,
)
from app.services.messages_zh import (
    MSG_SEMANTIC_RELEASE_CHECKS_TRUNCATED,
    MSG_SEMANTIC_RELEASE_GATE_NO_LANDING,
    MSG_SEMANTIC_RELEASE_GATE_NO_SOURCE,
)
from app.services.semantic_release_check_types import (
    RESULT_FAIL,
    RESULT_PASS,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    TARGET_METRIC,
    TARGET_RELEASE,
    PublishCheck,
    PublishGateResult,
)
from app.services.semantic_release_design_checks import evaluateDesignChecks
from app.services.semantic_release_domain_checks import (
    ReleaseCheckContext,
    evaluateDomainChecks,
)

# ---- 校验项编码（机器可读）----
CHECK_SNAPSHOT_INTEGRITY = "SNAPSHOT_INTEGRITY"
CHECK_MANIFEST_SCHEMA = "MANIFEST_SCHEMA"
CHECK_METRIC_SOURCE = "METRIC_SOURCE_PRESENT"
CHECK_METRIC_NUMERATOR_LANDING = "METRIC_NUMERATOR_LANDING"
CHECK_METRIC_DENOMINATOR_LANDING = "METRIC_DENOMINATOR_LANDING"
CHECK_CHECKS_TRUNCATED = "CHECKS_TRUNCATED"

# ---- 校验项名称（中文，展示用）----
ITEM_SNAPSHOT_INTEGRITY = "快照完整性"
ITEM_MANIFEST_SCHEMA = "Manifest 契约"
ITEM_METRIC_SOURCE = "指标源类引用"
ITEM_METRIC_NUMERATOR_LANDING = "指标分子落点类"
ITEM_METRIC_DENOMINATOR_LANDING = "指标分母落点类"
ITEM_CHECKS_TRUNCATED = "校验结论被截断"

# 闸门报告封顶：体量失控的数据不该把响应撑爆（与既有闸门口径同值）。
MAX_GATE_MESSAGES = 20

# 单个发布的校验项条数上限 —— **从契约推导，不写死**：
#   6 条发布级（快照完整性 / Manifest 契约 / 结论截断 / 域切片为空 /
#   跨域依赖补齐 / 候选绑定治理债）
#   + 每条指标最多 3 条（源类引用 / 分子落点 / 分母落点）
#   + 每条绑定最多 2 条（无 JOIN 边 / 属性未绑列）
#   + 每条 KPI 最多 1 条（MET-002；无已发布 KPI 时出 1 条汇总行）
#   + 5 条设计码汇总行（CLS-001 / CLS-002 / REL-001 / AGT-001 / GRF-001 各 1 条）
#   = 6 + 3 × MAX_MANIFEST_METRICS + 2 × MAX_MANIFEST_SOURCE_BINDINGS
#     + MAX_MANIFEST_KPIS + 1 + 5。
# ⚠️ 写死一个「看起来更宽」的数会出事：契约允许的合法 manifest 也会被截断，
# 而截断丢的是**尾部**校验项 —— 尾部往往正是最后一条指标的 ERROR 结论行
# （安全评审 HIGH-1）。⇒ 上限必须 ≥ 契约上限，截断只应作用于**绕过契约**的 manifest。
# ⚠️ 绑定级校验项**不**按候选数计（候选不进快照 ⇒ 数量无契约上限）⇒
# 候选只出**一条发布级汇总行**（见域级模块的 `CHECK_SOURCE_BINDING_CANDIDATE`）。
# ⚠️ 设计码 CLS/REL/GRF/AGT 也是**每个码一条汇总行**（不逐类/逐 JOIN）：
# 逐类会有 5000 条、逐 JOIN 会有 20000 条，写放大且没人读 —— 汇总行条数是常数。
MAX_CHECKS_PER_RELEASE = (
    6
    + 3 * MAX_MANIFEST_METRICS
    + 2 * MAX_MANIFEST_SOURCE_BINDINGS
    + MAX_MANIFEST_KPIS
    + 1
    + 5
)

# 「没有落点的比率不可执行」——D07 发布闸门要求只对这两类指标硬拒绝。
_RATIO_METRIC_TYPES = frozenset({"RATIO", "DIFFERENCE"})


def evaluateManifestChecks(
    manifest: dict[str, Any], *, context: ReleaseCheckContext | None = None
) -> tuple[PublishCheck, ...]:
    """Manifest 契约 + 指标层 + 域级/绑定层校验项（纯函数）。

    快照完整性**不在这里**：它取决于库里那一行的 `manifest_hash`，
    属于「行」的属性而不是 manifest 自身的属性（见 `semantic_release_check_service`）。

    `context` 携带纯函数够不到的事实（域编码、活库里的候选绑定数）；
    不传 ⇒ 不做域切片校验（全局发布没有域），但绑定级校验照做
    （绑定分区在 manifest 里，自足）。
    """
    live = context or ReleaseCheckContext()
    checks: list[PublishCheck] = [_schemaCheck(manifest)]
    checks.extend(evaluatePublishChecks(manifest))
    checks.extend(evaluateDomainChecks(manifest, context=live))
    checks.extend(evaluateDesignChecks(manifest, context=live))
    return _dedupe(checks)


def evaluatePublishChecks(manifest: dict[str, Any]) -> tuple[PublishCheck, ...]:
    """指标层发布校验项（纯函数）：源类引用 / 分子落点 / 分母落点。

    分级按 D07：
    1. 指标 `sources` 为空 → **告警**（治理债要显式暴露，不静默放过）；
    2. `status=PUBLISHED` 的 RATIO/DIFFERENCE 指标，分子/分母度量没有落点类
       → **阻断**（已发布的比率不可执行）；`DRAFT` 指标同型问题仅告警。
    """
    checks: list[PublishCheck] = []
    for metric in _iterMetrics(manifest):
        checks.extend(_metricChecks(metric))
    return _dedupe(checks)


def checksToGateResult(checks: tuple[PublishCheck, ...]) -> PublishGateResult:
    """校验项集合 ⇒ 闸门结论（blockers / warnings 各封顶）。

    只有 `FAIL` 行进入报告：`PASS` 行是「已检查且通过」的证据，不是治理债。
    """
    blockers = [
        check.detail
        for check in checks
        if check.result == RESULT_FAIL and check.severity == SEVERITY_ERROR
    ]
    warnings = [
        check.detail
        for check in checks
        if check.result == RESULT_FAIL and check.severity == SEVERITY_WARNING
    ]
    return PublishGateResult(
        blockers=tuple(blockers[:MAX_GATE_MESSAGES]),
        warnings=tuple(warnings[:MAX_GATE_MESSAGES]),
    )


def capChecks(checks: tuple[PublishCheck, ...]) -> tuple[PublishCheck, ...]:
    """超出上限 ⇒ 截断并**显式标记**（绝不静默丢证据）。

    截断时保留前 `MAX_CHECKS_PER_RELEASE - 1` 条，**最后一条换成**
    `CHECKS_TRUNCATED`（severity=ERROR / result=FAIL）的合成行 ⇒

    - 表里能查到「这份报告被截断」；
    - 闸门把它当**阻断项** ⇒ 报告不完整就不许放行（fail-closed）；
    - 闸门与落表来自**同一个集合** ⇒ 不会出现「闸门按 6002 判、表里只有 6000」。

    契约内的 manifest 永远走不到这里（上限就是从契约推导的）。
    """
    if len(checks) <= MAX_CHECKS_PER_RELEASE:
        return checks
    kept = list(checks[: MAX_CHECKS_PER_RELEASE - 1])
    kept.append(
        PublishCheck(
            code=CHECK_CHECKS_TRUNCATED,
            item=ITEM_CHECKS_TRUNCATED,
            severity=SEVERITY_ERROR,
            result=RESULT_FAIL,
            targetType=TARGET_RELEASE,
            detail=MSG_SEMANTIC_RELEASE_CHECKS_TRUNCATED.format(
                total=len(checks), max=MAX_CHECKS_PER_RELEASE
            ),
        )
    )
    return tuple(kept)


# ---------------------------------------------------------------------------
# 内部：单条校验项
# ---------------------------------------------------------------------------


def _schemaCheck(manifest: dict[str, Any]) -> PublishCheck:
    """Manifest 契约校验（结构 + 引用 + 可规范化）。

    契约按 `manifest.schemaVersion` 分发（v1 / v2）⇒ 结论文字必须写**实际版本**，
    否则读者会以为「过了 v1 契约」而快照其实是 v2。
    """
    try:
        validateManifest(manifest)
    except ManifestSchemaError as exc:
        return PublishCheck(
            code=CHECK_MANIFEST_SCHEMA,
            item=ITEM_MANIFEST_SCHEMA,
            severity=SEVERITY_ERROR,
            result=RESULT_FAIL,
            targetType=TARGET_RELEASE,
            detail=str(exc),
        )
    version = manifest.get("schemaVersion") if isinstance(manifest, dict) else None
    return PublishCheck(
        code=CHECK_MANIFEST_SCHEMA,
        item=ITEM_MANIFEST_SCHEMA,
        severity=SEVERITY_ERROR,
        result=RESULT_PASS,
        targetType=TARGET_RELEASE,
        detail=f"Manifest 满足 {version} 契约",
    )


def _metricChecks(metric: dict[str, Any]) -> list[PublishCheck]:
    name = _metricName(metric)
    return [_sourceCheck(metric, name), *_landingChecks(metric, name)]


def _sourceCheck(metric: dict[str, Any], name: str) -> PublishCheck:
    sourceNames = _sourceClassNames(metric.get("sources"))
    if sourceNames:
        return PublishCheck(
            code=CHECK_METRIC_SOURCE,
            item=ITEM_METRIC_SOURCE,
            severity=SEVERITY_WARNING,
            result=RESULT_PASS,
            targetType=TARGET_METRIC,
            targetKey=name,
            detail=f"指标 {name} 的源类引用：{'、'.join(sourceNames)}",
        )
    return PublishCheck(
        code=CHECK_METRIC_SOURCE,
        item=ITEM_METRIC_SOURCE,
        severity=SEVERITY_WARNING,
        result=RESULT_FAIL,
        targetType=TARGET_METRIC,
        targetKey=name,
        detail=MSG_SEMANTIC_RELEASE_GATE_NO_SOURCE.format(metric=name),
    )


def _landingChecks(metric: dict[str, Any], name: str) -> list[PublishCheck]:
    """分子 / 分母落点校验。

    分母只对 RATIO/DIFFERENCE 有意义：MEASURE 指标不存在「分母缺落点」这种说法，
    对它报分母会让报告被噪音淹没（评审会开始忽略整份报告）。
    """
    fields = [("numeratorMeasure", CHECK_METRIC_NUMERATOR_LANDING, ITEM_METRIC_NUMERATOR_LANDING, "分子")]
    if metric.get("metricType") in _RATIO_METRIC_TYPES:
        fields.append(
            ("denominatorMeasure", CHECK_METRIC_DENOMINATOR_LANDING, ITEM_METRIC_DENOMINATOR_LANDING, "分母")
        )

    checks: list[PublishCheck] = []
    for field, code, item, part in fields:
        className = _measureClassName(metric.get(field))
        if className:
            checks.append(
                PublishCheck(
                    code=code,
                    item=item,
                    severity=SEVERITY_WARNING,
                    result=RESULT_PASS,
                    targetType=TARGET_METRIC,
                    targetKey=name,
                    detail=f"指标 {name} 的{part}度量落点类：{className}",
                )
            )
            continue
        checks.append(
            PublishCheck(
                code=code,
                item=item,
                severity=_landingSeverity(metric),
                result=RESULT_FAIL,
                targetType=TARGET_METRIC,
                targetKey=name,
                detail=MSG_SEMANTIC_RELEASE_GATE_NO_LANDING.format(metric=name, part=part),
            )
        )
    return checks


def _landingSeverity(metric: dict[str, Any]) -> str:
    """落点缺失的严重级别：只有「已发布的比率」是硬拒绝（D07）。"""
    if metric.get("status") == "PUBLISHED" and metric.get("metricType") in _RATIO_METRIC_TYPES:
        return SEVERITY_ERROR
    return SEVERITY_WARNING


# ---------------------------------------------------------------------------
# 内部：解析
# ---------------------------------------------------------------------------


def _iterMetrics(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = manifest.get("metrics") if isinstance(manifest, dict) else None
    if not isinstance(metrics, list):
        return []
    return [metric for metric in metrics if isinstance(metric, dict)]


def _metricName(metric: dict[str, Any]) -> str:
    name = metric.get("metricName")
    return name if isinstance(name, str) and name else "<未命名指标>"


def _measureClassName(measure: Any) -> str | None:
    className = measure.get("className") if isinstance(measure, dict) else None
    return className if isinstance(className, str) and className else None


def _sourceClassNames(sources: Any) -> list[str]:
    if not isinstance(sources, list):
        return []
    names: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        className = source.get("className")
        if isinstance(className, str) and className:
            names.append(className)
    return names


def _dedupe(checks: list[PublishCheck]) -> tuple[PublishCheck, ...]:
    """同一 (code, targetKey) 只留第一条。

    Manifest 契约保证 `metricName` 唯一 ⇒ 正常路径不会触发；
    这里只是防御「契约外的 manifest」把唯一索引撞炸（同 code 同 target 落两行）。
    """
    seen: set[tuple[str, str | None]] = set()
    unique: list[PublishCheck] = []
    for check in checks:
        key = (check.code, check.targetKey)
        if key in seen:
            continue
        seen.add(key)
        unique.append(check)
    return tuple(unique)
