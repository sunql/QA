"""类召回 / 过滤 / 排序（ChatService 的 RecallMixin）。

从本体全量类中筛出与当前问题相关的子集：向量检索 topK → ODS 过滤 → ADS 加权 →
层优先排序 → JOIN 扩边 → 上限截断；检索不可用时走 `_fallbackRecall` 降级。
只读 `self._ontology` / `self._embedding` 两个注入依赖，其余均为本模块纯函数。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas import ChatRequest, ClassRecallInfo
from app.services.chat_helpers import _clipText

logger = logging.getLogger(__name__)

_FEW_SHOT_TOP_K = 3  # 1-2：历史相似 SQL few-shot 的检索条数（注入即 token 成本，取小值）
_FEW_SHOT_SIMILARITY_MIN = 0.6  # 1-2：相似度低于该值的命中视为噪音，不注入
_FEW_SHOT_EXAMPLE_LIMIT = 400  # 1-2：单条示例的 question/sql 字符上限（few-shot 每阶段重复注入）
_CLASS_FILTER_TOP_K_DEFAULT = 15  # 1-1：类裁剪的向量检索 topK；运行期从 system_config.CLASS_FILTER_TOP_K 读，缺席用此值
_CLASS_FILTER_HIT_MATCH_MIN_DEFAULT = 0.5  # 1-1：命中中可解析为真实类的比例低于该值时告警；运行期从 system_config.CLASS_FILTER_HIT_MATCH_MIN 读
_CLASS_FILTER_MAX_CLASSES_DEFAULT = 30  # 召回扩边后的 schema 类总量上限；运行期从 system_config.CLASS_FILTER_MAX_CLASSES 读，缺席用此值
_ADS_RECALL_WEIGHT_DEFAULT = 1.5  # feat-ontology-recall-pruning step D：ADS 层类 score 加权系数
# 运行期从 system_config.ADS_RECALL_WEIGHT 读；缺席/格式错返此值。提高此值让
# ADS 黄金路径在向量召回 top15 中更靠前；降低/设为 1.0 关闭加权。


def _isOdsBusinessTable(cls: Any) -> bool:
    """是否 ODS 层业务表（feat-ontology-recall-pruning step C 用的层判定）。

    命名约定 100% 一致：ODS_* 前缀的表都是贴源原始表。DIM_* / DWD_* /
    DWS_* / ADS_* 都保留。维度表即使以 ODS_DIM 开头也保留（历史命名
    兜底）。ERPin/mdmtoerp/ETL_WATERMARK 等系统表以 isOds 返 False。

    返回 True 表示「应跳过扩边邻居」，False 表示「保留」。
    """
    src = getattr(cls, "source_table", None) or ""
    if not src.startswith("ODS_"):
        return False
    # 兜底：历史命名 ODS_DIM_* 也保留（视为维度表，非业务表）
    if src.startswith("ODS_DIM"):
        return False
    return True


# feat-layer-priority: 按 source_table 前缀识别层 + 显式 ODS 请求判定 + 维度词触发。
# 层排序规则：ADS > DWS > DWD > DIM > ODS_DICT > ODS_BUSINESS > UNKNOWN。
_LAYER_RANK = {
    "ADS": 0,
    "DWS": 1,
    "DWD": 2,
    "DIM": 3,
    "ODS_DICT": 4,
    "ODS_BUSINESS": 5,
    "UNKNOWN": 6,
}


def _getClassLayer(cls: Any) -> str:
    """按 source_table 前缀识别层。

    返回值（按优先级升序）：
    - ADS: 应用视图层
    - DWS: 数据汇总层
    - DWD: 明细层
    - DIM: 维度层
    - ODS_DICT: ODS 字典表（如 ODS_DIM_*）
    - ODS_BUSINESS: ODS 业务原始表
    - UNKNOWN: 未识别
    """
    src = (getattr(cls, "source_table", "") or "").upper()
    if src.startswith("ADS_"):
        return "ADS"
    if src.startswith("DWS_"):
        return "DWS"
    if src.startswith("DWD_"):
        return "DWD"
    if src.startswith("DIM_"):
        return "DIM"
    if src.startswith("ODS_DIM_"):
        return "ODS_DICT"
    if src.startswith("ODS_"):
        return "ODS_BUSINESS"
    return "UNKNOWN"


_ODS_TABLE_PATTERN = re.compile(r"\bODS_[A-Z][A-Z0-9_]*\b")


def _isExplicitOdsRequest(question: str) -> bool:
    """显式 ODS 请求判定：问题文本含 ODS_<UPPER_NAME> 表名。

    适用于「ODS_BPARTNER 里有什么」类直接指定原始表名的问法。
    """
    return bool(_ODS_TABLE_PATTERN.search((question or "").upper()))


_DIMENSION_HINTS = (
    "维度",
    "属性",
    "分类",
    "描述",
    "名称",
    "编号",
    "供应商编号",
    "物料描述",
    "物料编码",
    "供应商名称",
    "物料名称",
    "物料分类",
    "供应商分类",
)


def _isDimensionHint(question: str) -> bool:
    """维度词触发：问题含维度/属性/分类/编号/描述等关键词。

    触发后 DIM 类全部纳入候选，即使 score 低。
    """
    return any(kw in (question or "") for kw in _DIMENSION_HINTS)


class RecallMixin:
    """类召回 / 过滤 / 排序（由 ChatService 组合）。"""

    async def _getClassFilterMaxClasses(self, session: AsyncSession) -> int:
        """读 system_config.CLASS_FILTER_MAX_CLASSES；缺席/格式错/非正返 _DEFAULT。

        与 ``_isL4AgentLoopEnabled`` 同口径：读失败不阻断主链路，返硬编码默认。
        admin 改值后立即对新问句生效（每次扩边都现读，无缓存）。

        非正值同样按非法处理：0/负数不是「截得更狠」而是闸门静默失效
        （``ranked[:0]`` 返空、``ranked[:-5]`` 返「除末位以外全部」，truncated
        判定同时失真）。上限是正常裁剪与 H5 降级路径共用的唯一闸门。
        """
        try:
            row = await session.execute(
                text("SELECT value FROM system_config WHERE key = 'CLASS_FILTER_MAX_CLASSES'")
            )
            raw = row.scalar_one_or_none()
            if raw is None or raw == "":
                return _CLASS_FILTER_MAX_CLASSES_DEFAULT
            value = int(raw)
            if value <= 0:
                logger.warning(
                    "CLASS_FILTER_MAX_CLASSES 非正值 %r，返默认值 %d",
                    raw, _CLASS_FILTER_MAX_CLASSES_DEFAULT,
                )
                return _CLASS_FILTER_MAX_CLASSES_DEFAULT
            return value
        except (TypeError, ValueError):
            logger.warning(
                "CLASS_FILTER_MAX_CLASSES 值非法 %r，返默认值 %d",
                raw, _CLASS_FILTER_MAX_CLASSES_DEFAULT,
            )
            return _CLASS_FILTER_MAX_CLASSES_DEFAULT
        except Exception:
            logger.warning(
                "读取 CLASS_FILTER_MAX_CLASSES 失败，返默认值 %d",
                _CLASS_FILTER_MAX_CLASSES_DEFAULT, exc_info=True,
            )
            return _CLASS_FILTER_MAX_CLASSES_DEFAULT

    async def _getAdsRecallWeight(self, session: AsyncSession) -> float:
        """读 system_config.ADS_RECALL_WEIGHT；缺席/格式错返 _DEFAULT（feat-D）。

        与 ``_getClassFilterMaxClasses`` 同口径：读失败不阻断主链路，返硬编码默认。
        admin 改值后立即对新问句生效（每次召回都现读，无缓存）。
        """
        try:
            row = await session.execute(
                text("SELECT value FROM system_config WHERE key = 'ADS_RECALL_WEIGHT'")
            )
            raw = row.scalar_one_or_none()
            if raw is None or raw == "":
                return _ADS_RECALL_WEIGHT_DEFAULT
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "ADS_RECALL_WEIGHT 值非法 %r，返默认值 %.2f",
                raw, _ADS_RECALL_WEIGHT_DEFAULT,
            )
            return _ADS_RECALL_WEIGHT_DEFAULT
        except Exception:
            logger.warning(
                "读取 ADS_RECALL_WEIGHT 失败，返默认值 %.2f",
                _ADS_RECALL_WEIGHT_DEFAULT, exc_info=True,
            )
            return _ADS_RECALL_WEIGHT_DEFAULT

    async def _getClassFilterTopK(self, session: AsyncSession) -> int:
        """读 system_config.CLASS_FILTER_TOP_K；缺席/格式错/非正返 _DEFAULT。

        与 ``_getClassFilterMaxClasses`` 同口径：读失败不阻断主链路。非正值视同
        非法——topK=0 会让检索恒空 → 永远走全量回退（裁剪静默失效）。
        """
        try:
            row = await session.execute(
                text("SELECT value FROM system_config WHERE key = 'CLASS_FILTER_TOP_K'")
            )
            raw = row.scalar_one_or_none()
            if raw is None or raw == "":
                return _CLASS_FILTER_TOP_K_DEFAULT
            value = int(raw)
            if value <= 0:
                logger.warning(
                    "CLASS_FILTER_TOP_K 非正值 %r，返默认值 %d",
                    raw, _CLASS_FILTER_TOP_K_DEFAULT,
                )
                return _CLASS_FILTER_TOP_K_DEFAULT
            return value
        except (TypeError, ValueError):
            logger.warning(
                "CLASS_FILTER_TOP_K 值非法 %r，返默认值 %d",
                raw, _CLASS_FILTER_TOP_K_DEFAULT,
            )
            return _CLASS_FILTER_TOP_K_DEFAULT
        except Exception:
            logger.warning(
                "读取 CLASS_FILTER_TOP_K 失败，返默认值 %d",
                _CLASS_FILTER_TOP_K_DEFAULT, exc_info=True,
            )
            return _CLASS_FILTER_TOP_K_DEFAULT

    async def _getClassFilterHitMatchMin(self, session: AsyncSession) -> float:
        """读 system_config.CLASS_FILTER_HIT_MATCH_MIN；缺席/格式错返 _DEFAULT。

        与 ``_getAdsRecallWeight`` 同口径（float）：0.0 是合法配置（恒不告警），
        故不做非正拒绝。
        """
        try:
            row = await session.execute(
                text("SELECT value FROM system_config WHERE key = 'CLASS_FILTER_HIT_MATCH_MIN'")
            )
            raw = row.scalar_one_or_none()
            if raw is None or raw == "":
                return _CLASS_FILTER_HIT_MATCH_MIN_DEFAULT
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "CLASS_FILTER_HIT_MATCH_MIN 值非法 %r，返默认值 %.2f",
                raw, _CLASS_FILTER_HIT_MATCH_MIN_DEFAULT,
            )
            return _CLASS_FILTER_HIT_MATCH_MIN_DEFAULT
        except Exception:
            logger.warning(
                "读取 CLASS_FILTER_HIT_MATCH_MIN 失败，返默认值 %.2f",
                _CLASS_FILTER_HIT_MATCH_MIN_DEFAULT, exc_info=True,
            )
            return _CLASS_FILTER_HIT_MATCH_MIN_DEFAULT

    async def _rankByLayer(
        self,
        classes: list,
        *,
        dimension_hint: bool,
        session: AsyncSession,
    ) -> list:
        """按层优先排序：DIM 拉满（DIM 层按 dimension_hint 决定是否全拉）。

        Returns: 排序后的新 list（不修改输入）。
        """
        merged = list(classes)
        if dimension_hint:
            dim_classes = await self._fetchAllDimClasses(session)
            # 去重（按 id）
            seen = {c.id for c in merged if c.id is not None}
            for c in dim_classes:
                if c.id not in seen:
                    merged.append(c)
                    seen.add(c.id)

        def _key(cls):
            return _LAYER_RANK.get(_getClassLayer(cls), _LAYER_RANK["UNKNOWN"])

        return sorted(merged, key=_key)

    async def _fetchAllDimClasses(self, session: AsyncSession) -> list:
        """拉全量 DIM_<NAME> 类（DIM 维度词触发时使用）。"""
        from sqlalchemy import select

        from app.domain.models import OntologyClass

        rows = (
            await session.execute(
                select(OntologyClass).where(
                    OntologyClass.source_table.like("DIM_%"),
                    OntologyClass.valid_to.is_(None),
                )
            )
        ).scalars().all()
        return list(rows)

    async def _selectRelevantClasses(
        self, session: AsyncSession, question: str, allClasses: list[Any]
    ) -> list[Any]:
        """从全部本体类中筛出与当前问题相关的子集（1-1，根因修复）。

        诊断发现旧实现把全量 schema 一次性喂给 LLM（检索基础设施未接入），
        表越多越容易选错表/列。此处用向量检索（searchByKeyword）召回 topK 相关类，
        仅把相关子集送入 plan/SQL 阶段。

        检索是增强而非硬依赖：Milvus/embedding 未就绪、或测试桩未实现
        searchByKeyword 时，回退到 ``_fallbackRecall``（同样过滤 ODS + 截断上限，
        见 H5）——降级保的是「不报错」，不是「放弃裁剪」。
        返回新列表，不改动入参 allClasses。

        可观测性（1-1）：每次回退都记 warning，reason= 区分场景（search_error /
        no_hits / no_match / no_match_ods_filtered / ods_only_hits），供回退率聚合；
        降级实际做的过滤/截断由 ``_fallbackRecall`` 另记一行；命中中可解析为真实类的
        比例低于阈值时同样告警，避免检索漂移让裁剪在生产上悄悄失效。

        返回 (类列表, ClassRecallInfo 诊断)：诊断随 ChatResponse.classRecall 透出，
        前端在 truncated/fallback 时向用户提示（避免"看起来正常但 schema 缺表"）。
        """
        total = len(allClasses)
        if total == 0:
            return list(allClasses), ClassRecallInfo(
                mode="recall", hitCount=0, classCount=0,
            )
        topK = await self._getClassFilterTopK(session)
        try:
            hits = await self._ontology.searchByKeyword(
                question, topK=topK, typeFilter="class"
            )
        except Exception:
            # 只留堆栈：reason=/计数由 _fallbackRecall 单点透出，避免回退率翻倍
            logger.warning("本体类裁剪检索失败，进入降级路径", exc_info=True)
            return await self._fallbackRecall(
                session, question, allClasses, reason="search_error"
            )
        if not hits:
            return await self._fallbackRecall(
                session, question, allClasses, reason="no_hits"
            )
        hitIds = {hit.id for hit in hits}
        # feat-ontology-recall-pruning step D：按 ADS 层加权召回。
        # 构建 classById 用于 source_table 前缀判定；ADS_* 类 score × weight 后
        # 重排，让 ADS 黄金路径挤进 top。weight=1.0 时加权为空操作（保持原序）。
        adsWeight = await self._getAdsRecallWeight(session)
        classByIdForWeight = {cls.id: cls for cls in allClasses if cls.id is not None}
        weightedHits: list[tuple[Any, float]] = []
        for hit in hits:
            baseScore = float(getattr(hit, "score", 0.0) or 0.0)
            cls = classByIdForWeight.get(hit.id)
            if cls and (cls.source_table or "").startswith("ADS_"):
                baseScore *= adsWeight
            weightedHits.append((hit, baseScore))
        if adsWeight != 1.0:
            # weight != 1 时按加权 score 降序排，让 ADS 挤前
            weightedHits.sort(key=lambda x: x[1], reverse=True)
        # 2026-09-19 ODS_BPARTNER 事故：召回入口 hits 同样过滤 ODS 业务表。
        # 此前扩边阶段已用 _isOdsBusinessTable 跳过 ODS 邻居（TestClassFilterExpansionSkipOds），
        # 但召回入口直接命中的 ODS_BPARTNER 没被过滤，LLM 在没有 SUPPLIER_CODE
        # 等业务列的备份表上幻觉属性名。ODS_DIM_* 字典表保留（_isOdsBusinessTable 兜底）。
        #
        # 三态区分（不能合并）：
        #   (1) hits 解析到 classById 但全是 ODS 业务表 → ods_only_hits 回退
        #   (2) hits 解析不到任何 classById → 原 no_match 全量回退（保留旧测试）
        #   (3) 部分命中部分 ODS 过滤 → 走正常裁剪，ODS 类的部分丢弃
        classByIdResolved = [
            (h, classByIdForWeight.get(h.id))
            for h in hits
        ]
        validHitsCount = sum(
            1 for _, cls in classByIdResolved if cls is not None
        )
        allResolvedOdsOnly = (
            validHitsCount > 0
            and all(
                _isOdsBusinessTable(cls)
                for _, cls in classByIdResolved if cls is not None
            )
        )
        explicit_ods_recall = _isExplicitOdsRequest(question)
        weightedHits = [
            (h, s) for h, s in weightedHits
            if (cls := classByIdForWeight.get(h.id)) is not None
            and (not _isOdsBusinessTable(cls) or explicit_ods_recall)
        ]
        relevant = [
            classByIdForWeight[h.id] for h, _ in weightedHits
            if h.id in classByIdForWeight
        ]
        # 全解析为 ODS 业务表的场景：退而使用「非 ODS 业务表的全量类」，避免
        # ODS_BPARTNER 等备份表再次通过全量回退进 LLM schema。
        if not relevant and allResolvedOdsOnly:
            odsFiltered = [
                cls for cls in allClasses
                if cls.id is not None and not _isOdsBusinessTable(cls)
            ]
            if not odsFiltered:
                return await self._fallbackRecall(
                    session, question, allClasses, reason="no_match_ods_filtered"
                )
            return await self._fallbackRecall(
                session, question, allClasses, reason="ods_only_hits"
            )
        if not relevant:
            return await self._fallbackRecall(
                session, question, allClasses, reason="no_match", hitCount=len(hits)
            )
        matchedRatio = len(relevant) / len(hits)
        hitMatchMin = await self._getClassFilterHitMatchMin(session)
        if matchedRatio < hitMatchMin:
            logger.warning(
                "本体类裁剪命中率过低 hits=%d matched=%d ratio=%.2f",
                len(hits), len(relevant), matchedRatio,
            )
        else:
            logger.info(
                "本体类裁剪完成 pruned=%d total=%d hits=%d",
                len(relevant), total, len(hits),
            )
        # 层优先排序（feat-layer-priority）
        explicit_ods = _isExplicitOdsRequest(question)
        dimension_hint = _isDimensionHint(question)

        # 按层排序
        relevant = await self._rankByLayer(
            relevant, dimension_hint=dimension_hint, session=session
        )

        # 截取 top K
        max_classes = await self._getClassFilterMaxClasses(session)
        selected = relevant[:max_classes]
        pre_expand_truncated = len(relevant) > max_classes
        expanded, truncated_from_expand = await self._expandByJoinNeighbors(
            session, selected, allClasses
        )
        truncated = pre_expand_truncated or truncated_from_expand
        recall = ClassRecallInfo(
            mode="expanded" if len(expanded) > len(relevant) else "recall",
            hitCount=len(relevant),
            classCount=len(expanded),
            truncated=truncated,
        )
        return expanded, recall

    async def _fallbackRecall(
        self,
        session: AsyncSession,
        question: str,
        allClasses: list[Any],
        *,
        reason: str,
        hitCount: int | None = None,
    ) -> tuple[list[Any], ClassRecallInfo]:
        """召回不可用时的统一降级：ODS 过滤 + 层优先排序 + max 截断（H5）。

        降级不是免检：此前各回退分支直接 ``return list(allClasses)``，等于在
        Milvus/embedding 挂掉时把 2026-09-19 ODS_BPARTNER 事故（LLM 在贴源备份表上
        幻觉属性名）连同「表越多越选错」原样放回来。此处与正常裁剪同口径：
        ODS 过滤（显式点名 ODS 表的问题除外）→ 层优先排序 → 截到
        ``system_config.CLASS_FILTER_MAX_CLASSES``。排序不可省：入参是库表顺序，
        直接截前缀等于随机丢表。

        退化场景（全库只有 ODS 业务表）保留原样而非返回空列表：空 schema 会让所有
        问题都变成「无法回答」，而 ODS 表至少还能试 —— 该分支单独打 warning。

        日志是**唯一**的 reason= 出口（回退率按行聚合，多打一行就翻倍）：调用方不得
        再另打 reason= 行；本函数内退化分支那行也用 scene= 而非 reason=，同一事件
        只可能有一行带 reason=。hitCount 只在「取到 hits 但解析不出类」的场景有值。
        返回新列表，不改动入参。
        """
        total = len(allClasses)
        explicit_ods = _isExplicitOdsRequest(question)
        candidates = [
            cls for cls in allClasses
            if cls.id is not None
            and (not _isOdsBusinessTable(cls) or explicit_ods)
        ]
        odsFiltered = total - len(candidates)
        if not candidates:
            # 本行刻意用 scene= 而非 reason=：回退率按「含 reason= 的行」聚合，
            # 同一事件打两行就翻倍；场景名由紧随其后的主行给出（同 total 可对齐）。
            logger.warning(
                "本体类回退降级无可用非 ODS 类，保留原列表 scene=%s total=%d",
                reason, total,
            )
            candidates = list(allClasses)
            odsFiltered = 0
        # dimension_hint=False：入参本就是全量类，DIM 已在其中（_rankByLayer 的 DIM
        # 补拉是给召回子集用的），降级路径也不再引入一次 DB 读取。
        ranked = await self._rankByLayer(candidates, dimension_hint=False, session=session)
        max_classes = await self._getClassFilterMaxClasses(session)
        selected = ranked[:max_classes]
        truncated = len(ranked) > max_classes
        # hits= 只在本场景有值（NO_MATCH 才看得到命中数）；其余场景省略该段，
        # 以保持「reason=<场景> total=<全量>」这段 token 相邻（既有聚合/断言口径）。
        hitDetail = "" if hitCount is None else f" hits={hitCount}"
        logger.warning(
            "本体类回退降级 reason=%s%s total=%d kept=%d odsFiltered=%d truncated=%s",
            reason, hitDetail, total, len(selected), odsFiltered, truncated,
        )
        return selected, ClassRecallInfo(
            mode="fallback",
            hitCount=0,
            classCount=len(selected),
            truncated=truncated,
        )

    async def _expandByJoinNeighbors(
        self, session: AsyncSession, relevant: list[Any], allClasses: list[Any]
    ) -> tuple[list[Any], bool]:
        """召回结果沿本体 JOIN 目录 1-hop 扩边，返回 (新列表, 是否截断)（不改动入参）。

        背景：向量召回会把「成对使用」的类拆散——「供货量」问题命中 Receipt（收货单）
        但明细表 ReceiptDetail 落榜，schema 里没有明细类时 LLM 会编造类名/属性名，
        计划校验必拒（且数量/物料等列恰恰都在明细表）。头表↔明细表↔名称主表经
        JOIN 目录相连，命中的类自动带上 1-hop 邻居即可成对进 schema。

        顺序：命中类在前（保持召回相关性排序），邻居按命中顺序追加；总量超
        ``system_config.CLASS_FILTER_MAX_CLASSES`` 截断（截断标志随诊断透出）。
        上限 admin 可调，运行期每次扩边现读——改值后立即对新问句生效。JOIN 目录
        加载失败时退化为纯召回结果（扩边是增强而非硬依赖，与检索降级同口径）。
        """
        # admin 可调上限：缺席/格式错走默认（与 _isL4AgentLoopEnabled 同口径）
        maxClasses = await self._getClassFilterMaxClasses(session)
        try:
            joins = await self._ontology.listJoins(session)
        except Exception:
            logger.warning("JOIN 目录加载失败，跳过类召回扩边", exc_info=True)
            return relevant, False
        neighbors: dict[int, set[int]] = {}
        for join in joins:
            src, tgt = join.source_class_id, join.target_class_id
            if src is None or tgt is None:
                continue
            neighbors.setdefault(src, set()).add(tgt)
            neighbors.setdefault(tgt, set()).add(src)
        classById = {cls.id: cls for cls in allClasses if cls.id is not None}
        expandedIds: list[int] = [cls.id for cls in relevant if cls.id is not None]
        seen = set(expandedIds)
        truncated = False
        # feat-ontology-recall-pruning step C：按 source_table 前缀过滤
        # ODS 业务表邻居。命名约定 100% 一致（ODS_/DWD_/DWS_/DIM_/ADS_），
        # ODS_* 是贴源原始表，与 DWD/ADS 同主题但 schema 重复，扩边带上
        # 会挤占 30 上限且让 LLM 选错 JOIN 路径。维度表 / DWS / ADS / DWD
        # / ETL 系统表 / 接口表 都保留。
        for cid in expandedIds:
            for nb in sorted(neighbors.get(cid, ())):
                if nb in seen or nb not in classById:
                    continue
                nb_cls = classById[nb]
                if _isOdsBusinessTable(nb_cls):
                    continue
                seen.add(nb)
                expandedIds.append(nb)
                if len(expandedIds) >= maxClasses:
                    truncated = True
                    logger.info(
                        "类召回扩边截断 total=%d cap=%d",
                        len(expandedIds), maxClasses,
                    )
                    break
            if len(expandedIds) >= maxClasses:
                break
        if len(expandedIds) == len(relevant):
            return relevant, False
        expanded = [classById[i] for i in expandedIds]
        logger.info(
            "类召回扩边 hits=%d expanded=%d total=%d",
            len(relevant), len(expanded) - len(relevant), len(expanded),
        )
        return expanded, truncated

    async def _buildFewShot(self, dto: ChatRequest) -> str | None:
        """检索语义相似的历史成功查询，构造 few-shot 示例注入 NL2SQL prompt（1-2）。

        复用 embedding_service.searchSimilarQueries（原仅 /suggest 联想端点使用），
        让新问题直接参考相似问题的正确表/列/聚合写法，复用成功经验。

        检索是增强而非硬依赖：embedding/Milvus 未就绪、无 SQL 或无足够相似命中时
        返回 None（不注入），保证降级可用。sql 为空或相似度低于阈值的命中视为噪音
        丢弃；单条示例文本设长度上限，避免历史长 SQL 无界放大每次 NL2SQL 调用的
        输入 token（few-shot 会在计划/SQL/重试各阶段重复注入）。返回新字符串，
        不改动入参。返回内容仅为示例拼接，"数据而非指令"的框定由渲染方承担。
        """
        try:
            hits = await self._embedding.searchSimilarQueries(
                dto.question, topK=_FEW_SHOT_TOP_K, datasourceId=dto.datasourceId,
            )
        except Exception:
            logger.warning("历史相似查询检索不可用，跳过 few-shot", exc_info=True)
            return None
        examples: list[str] = []
        for hit in hits:
            if not hit.sql or hit.similarity < _FEW_SHOT_SIMILARITY_MIN:
                continue
            question = _clipText(hit.question, _FEW_SHOT_EXAMPLE_LIMIT)
            sql = _clipText(hit.sql, _FEW_SHOT_EXAMPLE_LIMIT)
            examples.append(f"示例 {len(examples) + 1}：\n问题：{question}\nSQL：\n{sql}")
        if not examples:
            return None
        return "\n\n".join(examples)
