"""P2 D15：schema 文本渲染路径的**接线闸门**（开关 + 完整性守卫 + 降级）。

为什么需要这一层
----------------
D12 的适配器（`semantic_schema_adapter`）只做「manifest → 渲染视图」的**形状转换**，
**不决定**这一轮问答到底用治理表还是已发布快照。那是**运行期策略**，由本模块承担。

本模块是 **schema 文本的唯一出口**（三个调用点都走这里）：
`nl2sql_plan.generateQueryPlan`（计划阶段）/ `nl2sql_service.generateSql`（SQL 阶段）/
`chat_domain._handleClarify`（概念解释）。

四段职责
--------
1. **开关**：`ENABLE_SEMANTIC_REGISTRY_NL2SQL`（默认关）。
   关 ⇒ 直接直读渲染，与今天**逐字节一致**，连库都不碰。
2. **取视图**：开关开 ⇒ 经适配器取 ACTIVE 快照的复原视图；
   没有发布 / 版本对不上 / 内容畸形 / 快照陈旧 ⇒ 适配器已经降级，本层原样透传治理表对象。
   **会话缺席**（`session is None`）⇒ 确定地直读（不查快照）——
   否则只能靠进程内缓存命中与否 ⇒ 同一轮的两次渲染可能一次快照一次直读
   ⇒ prompt 口径不可复现。
3. **完整性守卫**（本层的核心）：快照视图与治理表视图渲染出的 schema 文本
   必须**逐块行多重集相等**（= D14 的「内容等价 = 0」判据）。
   不相等 ⇒ 快照与当前本体**事实不一致**（发布之后本体又改了：加了列 / 改了类型 /
   加了 JOIN 边）⇒ **不许静默降级**：记 error 级日志并降级回直读，
   且降级后的输出与「开关关」**逐字节一致**。
4. **不许炸问答**：任何异常都降级，不穿透成 500（P0 裁决一）。

日志级别纪律
------------
- **error 级 = 事实缺口**（守卫判定快照与当前本体不一致）——那是**要人看**的信号；
- **warning 级 = 快照不可用**（表还没建 / 取快照抛异常）——不是事实缺口，
  不许占用 error 通道把真正的事实缺口淹没；
- 详细堆栈一律降到 **debug**（生产未部署 0142 时每次渲染都会走降级，
  error 级 + 堆栈会把日志刷爆）。


降级而不抛异常的理由
--------------------
P0 裁决一：「坏快照不许炸问答」。快照陈旧是**发布节奏的常态**
（本体改了还没重新发布），不是数据事故 ⇒ 让整轮问答 500 是错的。
但「不许静默」也是硬要求 ⇒ 降级必须**响**（error 级日志），
不许看起来跟「快照生效」一样。

守卫为什么按「渲染后的文本」比，而不是按「字段清单」比
------------------------------------------------------
守卫要回答的问题是「**模型看到的 prompt 会不会少了事实**」。
按字段清单比只能覆盖「少列 / 少边」，覆盖不了「列还在但**类型或描述变了**」
——而那恰恰是更容易把模型带偏的一类漂移。
⇒ 直接比两侧**渲染结果**的行多重集：任何事实差异都会落到文本上，
一处不漏；块内列序 / JOIN 条目序的差异不影响行多重集（D14 的已知差异，不算差异）。
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.config import getSettings
from app.services.nl2sql_schema import buildSchemaText
from app.services.semantic_schema_adapter import (
    resolveSchemaRelease,
    restoreSchemaFromSnapshot,
)

logger = logging.getLogger(__name__)

# 一次守卫最多记多少条差异（日志防刷屏；差异条数本身不是判据，判据是「有无差异」）。
_MAX_REPORTED_GAPS = 5
# 差异样本最多记多少字符（渲染出的列行带 description，可能是很长的业务文本）。
_MAX_SAMPLE_CHARS = 80


@dataclass(frozen=True)
class SchemaRenderResult:
    """一次 schema 文本渲染的结果 + **本轮真正用到的发布 id** + **钉住失效信号**。

    `semanticReleaseId` 的语义是「这一轮的 schema 文本是用哪一版发布渲染出来的」。
    它为 `None` 表示**这一轮没走快照**（开关关 / 会话缺席 / 降级直读）——
    此时**不许记账**：记账等于声称「本轮用的是那一版」，而事实并非如此。

    `pinInvalidated` 的语义是「**这一轮尝试过钉住某一版，但没用上**」
    （那一版已被删除 / 完整性校验失败 / id 非法 / 版本不符 / 快照陈旧）。
    运行记录据此**清掉**那个钉住：钉住还在、那一版却已经用不了 ⇒ 下一轮继续钉它
    ⇒ 每轮都降级直读、每轮都记一条 warning ⇒ **死钉**，会话再也起不来。
    没有钉住可失效时（本轮压根没钉）它为 `False` —— 否则会误清既有钉住。
    """

    text: str
    semanticReleaseId: int | None = None
    pinInvalidated: bool = False


def _flagEnabled() -> bool:
    """读开关。默认关 ⇒ 与今天的行为逐字节一致。"""
    return bool(getSettings().enableSemanticRegistryNl2sql)


async def renderSchemaText(
    classes: list[Any],
    joins: list[Any] | None = None,
    *,
    session: Any = None,
    pinnedReleaseId: int | None = None,
    directOnly: bool = False,
    **renderParams: Any,
) -> str:
    """渲染 schema 文本（**唯一的 schema 文本出口**）。

    `renderParams` 原样透传给 `nl2sql_schema.buildSchemaText`
    （schemaPrefix / valueSamples / driftWarning / digest* 等）。

    `pinnedReleaseId` 给了 ⇒ 钉住那一版发布（不看当前 ACTIVE），
    用于「同一请求重放 ⇒ 同一发布 ⇒ 同一口径」。

    `directOnly=True` ⇒ 确定地直读治理表（不查发布、不碰 registry）。
    """
    result = await renderSchemaWithRelease(
        classes,
        joins=joins,
        session=session,
        pinnedReleaseId=pinnedReleaseId,
        directOnly=directOnly,
        **renderParams,
    )
    return result.text


async def renderSchemaWithRelease(
    classes: list[Any],
    joins: list[Any] | None = None,
    *,
    session: Any = None,
    pinnedReleaseId: int | None = None,
    directOnly: bool = False,
    **renderParams: Any,
) -> SchemaRenderResult:
    """渲染 schema 文本，并回报**本轮真正用到的发布 id** + **钉住失效信号**。

    与 `renderSchemaText` 的区别只在于多回报一个 id 与一个失效信号；
    渲染口径**完全同一条**。

    守卫分两档（D16 偏差 ⑭）
    ----------------------
    - **未钉住**（取当前 ACTIVE）：快照与**当前本体**事实不一致 ⇒ 拒绝用快照、
      降级直读、记 **error**（事实缺口，D15 原语义不变）。
    - **已钉住**（按主键取那一版）：**不与当前本体比**。钉住的意义就是
      「这一轮读的还是上一轮那一版」；它与当前本体不同是**发布之后的常态**，
      不是数据事故 ⇒ 照旧降级的话，钉住永远拿不到那一版，「重放读同一发布」落空。
      钉住路径只做**自完整性**校验（manifest_hash / 体量 / schema 版本，
      由 Registry 的 `getById` 保证），差异**记 warning 但不降级**（不许静默）。
    """
    if directOnly:
        # 编排层显式要求直读（上游那一阶段没钉住发布 ⇒ 两阶段不许一半快照一半直读）。
        # ⇒ 不查 registry、不回报 id（id 为 None 即「本轮没走快照」）。
        return SchemaRenderResult(buildSchemaText(classes, joins=joins, **renderParams))

    if not _flagEnabled():
        return SchemaRenderResult(buildSchemaText(classes, joins=joins, **renderParams))

    if session is None:
        # 会话缺席 ⇒ 取快照只能靠 Registry 的进程内缓存：
        # 缓存冷 ⇒ 抛异常；缓存热 ⇒ 命中 ⇒ **同一轮的两次渲染可能一次快照一次直读**
        # ⇒ prompt 的 schema 口径不可复现（连类内列序都不同）⇒ 问题难定位。
        # ⇒ 这里确定地直读：不查快照、不试一把。
        logger.debug("语义快照闸门：会话缺席，确定地直读治理表")
        return SchemaRenderResult(buildSchemaText(classes, joins=joins, **renderParams))

    # 本轮是否**尝试过**钉住某一版（用于回报「钉住失效」，也用于分档守卫）。
    pinAttempted = pinnedReleaseId is not None

    def _direct(pinInvalidated: bool = False) -> SchemaRenderResult:
        return SchemaRenderResult(
            buildSchemaText(classes, joins=joins, **renderParams),
            None,
            pinInvalidated,
        )

    try:
        snapshot, releaseId = await resolveSchemaRelease(
            session, releaseId=pinnedReleaseId
        )
    except Exception as exc:  # noqa: BLE001 - 取快照失败不许炸问答（P0 裁决一）
        # 例：`semantic_release` 表还没建（生产未部署 0142）⇒ 查询报错。
        # 快照不可用 = 直读今天的路径，**不是事实缺口** ⇒ 不许占用 error 级通道
        # （error 级在本模块的语义是「快照与当前本体事实不一致」，那才是要人看的信号）。
        logger.warning("语义发布快照不可用，本次降级为直读治理表: %s", exc)
        logger.debug("语义发布快照不可用的详细堆栈", exc_info=True)
        return _direct(pinInvalidated=pinAttempted)

    if snapshot is None:
        # 没有发布 / 钉住的那一版已被删除 ⇒ 适配器已降级并留痕 ⇒ 直读渲染。
        return _direct(pinInvalidated=pinAttempted)

    try:
        restored = restoreSchemaFromSnapshot(snapshot, classes, joins)
    except Exception as exc:  # noqa: BLE001 - 复原失败不许炸问答（P0 裁决一）
        logger.warning("语义快照守卫渲染失败，本次降级为直读治理表: %s", exc)
        logger.debug("语义快照守卫渲染失败的详细堆栈", exc_info=True)
        return _direct(pinInvalidated=pinAttempted)

    if restored is None:
        # 版本不符 / 陈旧 / 畸形 ⇒ 适配器已降级并留痕 ⇒ 直读渲染。
        # 「陈旧」（快照缺召回子集里的类）意味着**这一版已经覆盖不了本轮的问题**
        # ⇒ 钉住它没有意义 ⇒ 钉住失效（下一轮回到当前 ACTIVE，自愈）。
        return _direct(pinInvalidated=pinAttempted)

    restoredClasses, restoredJoins = restored

    try:
        directText = buildSchemaText(classes, joins=joins, **renderParams)
        snapshotText = buildSchemaText(restoredClasses, joins=restoredJoins, **renderParams)
    except Exception as exc:  # noqa: BLE001 - 守卫渲染失败不许炸问答（P0 裁决一）
        logger.warning("语义快照守卫渲染失败，本次降级为直读治理表: %s", exc)
        logger.debug("语义快照守卫渲染失败的详细堆栈", exc_info=True)
        return _direct(pinInvalidated=pinAttempted)

    gaps = _factDiff(directText, snapshotText)
    if gaps:
        if pinAttempted:
            # 钉住路径：差异是**发布之后的常态**，不是事实缺口 ⇒ 不降级、不换版
            # （换了就不是「钉住」了）；但必须留痕（不许静默）。
            logger.warning(
                "钉住的语义发布与当前本体已不一致（releaseId=%s），按钉住语义仍用这一版："
                "差异块数=%d 示例=%s",
                releaseId,
                len(gaps),
                gaps[:_MAX_REPORTED_GAPS],
            )
            return SchemaRenderResult(snapshotText, releaseId)
        logger.error(
            "语义发布快照与当前本体事实不一致（缺/多/改），本次降级为直读治理表："
            "差异块数=%d 示例=%s",
            len(gaps),
            gaps[:_MAX_REPORTED_GAPS],
        )
        return SchemaRenderResult(directText)
    return SchemaRenderResult(snapshotText, releaseId)


def _sample(line: str) -> str:
    """差异样本的**定长**表示（渲染出的列行带 description，可能是很长的业务文本）。

    不截断 ⇒ 一条差异日志可能带上几百上千字的业务描述 ⇒ 日志被业务文本淹没，
    也把不该进日志的内容带了出去。
    """
    text = repr(line)
    if len(text) <= _MAX_SAMPLE_CHARS:
        return text
    return text[: _MAX_SAMPLE_CHARS - 1] + "…"


def _factDiff(directText: str, snapshotText: str) -> list[str]:
    """两侧渲染文本的**事实差异**（逐块比行多重集，忽略块内顺序）。

    与 D14 的「内容等价 = 0」判据同口径：

    - 块内列序 / JOIN 条目序不同 ⇒ **不算**差异（已知差异，见 `daily/D14.md`）；
    - 行在块之间搬家 ⇒ **算**差异（事实归属变了，整段文本的多重集仍会相等）。

    返回差异摘要（每条一个块），空 = 无差异。样本行**定长**（见 `_sample`）。
    """
    directBlocks = _splitBlocks(directText)
    snapshotBlocks = _splitBlocks(snapshotText)
    gaps: list[str] = []
    for key in sorted(set(directBlocks) | set(snapshotBlocks)):
        left = Counter(directBlocks.get(key, []))
        right = Counter(snapshotBlocks.get(key, []))
        if left == right:
            continue
        missing = sorted((left - right).elements())
        extra = sorted((right - left).elements())
        detail = []
        if missing:
            detail.append(f"直读侧多 {len(missing)} 行（示例 {_sample(missing[0])}）")
        if extra:
            detail.append(f"快照侧多 {len(extra)} 行（示例 {_sample(extra[0])}）")
        gaps.append(f"块 {key or '（前言）'}：" + "；".join(detail))
    return gaps


def _splitBlocks(text: str) -> dict[str, list[str]]:
    """按 `### ` 标题把渲染文本切成块；块内行保留（顺序不参与比较）。

    第一个 `### ` 之前的行归到「（前言）」块（当前渲染器没有前言，
    但保留这条 ⇒ 渲染器将来加了前言也不会被守卫漏掉）。
    """
    blocks: dict[str, list[str]] = {"": []}
    key = ""
    for line in text.splitlines():
        if line.startswith("### "):
            key = line
            blocks.setdefault(key, [])
            continue
        blocks[key].append(line)
    return blocks
