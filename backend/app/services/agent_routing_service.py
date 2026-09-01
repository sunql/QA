"""未指名 Agent 语义路由（Phase 7 G4）。

在既有 360/risk/graph/METRIC 等确定性意图拦截均未命中的前提下，对供应商查询做
纯关键词评分，把「高置信 → 自动调度 Agent、中置信 → 建议卡片」的语义接到
Chat 意图管线：

- ``route()`` 只做关键词评分，返回 ``AgentSuggestion`` 或 ``None``（置信 < 0.4）；
- 实体门禁（无供应商编码不路由）与聚合量词兜底由调用方 intent_service 施加
  （``_routeSupplierAgent``），避免本模块 import intent_service 造成循环依赖；
- 置信度 = ``min(1.0, 命中关键词数 * 0.4)``：单关键词命中即达 0.4 中置信阈值，
  命中 ≥2 个（如「评估…风险」）达 0.8 高置信，自动调度 Agent。

增量语义（用户确认的 G4 解释）：路由只在既有意图未命中时生效，绝不覆盖
SUPPLIER_360 / SUPPLIER_RISK / GRAPH_REASONING 等确定性拦截。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.schemas import AgentSuggestion

# 路由置信阈值（intent_service 共享决策语义：≥ HIGH 自动调度，≥ MEDIUM 建议卡片）
HIGH_CONFIDENCE = 0.7
MEDIUM_CONFIDENCE = 0.4
_SCORE_PER_KEYWORD = 0.4

# Agent 编码 → 触发关键词（业务领域语言，按 Phase 5.3/5.4/6.3 既有领域词汇扩展；
# 与各确定性拦截正则去重，避免已被吸走的问法再被路由二次触发）。
_AGENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "SUPPLIER_360_AGENT": (
        "360",
        "全貌",
        "全景",
        "概况",
        "总览",
        "画像",
        "表现",
        "视图",
        "整体",
        "全维度",
    ),
    "SUPPLIER_RISK_AGENT": (
        "风险",
        "健康度",
        "评估",
        "靠谱",
        "可靠",
        "合规",
        "信誉",
        "资质",
        "审核",
        "稳定",
    ),
    "GRAPH_REASONING_AGENT": (
        "关联",
        "链路",
        "上下游",
        "涉及",
        "关系",
        "网络",
        "依赖",
        "路径",
        "物料",
        "下一环",
    ),
}

# Agent 编码 → 推荐理由模板（贴合领域语义，展示给前端建议卡片）
_AGENT_REASONS: dict[str, str] = {
    "SUPPLIER_360_AGENT": "检测到供应商全景/表现类诉求，推荐 360° 视图",
    "SUPPLIER_RISK_AGENT": "检测到风险评估类诉求，推荐风险健康度评估",
    "GRAPH_REASONING_AGENT": "检测到关联/链路类诉求，推荐知识图谱推理",
}


@dataclass(frozen=True)
class _TopAgent:
    """关键词评分结果：命中关键词最多的 Agent 及其命中数。"""

    agent_code: str
    hits: int


class AgentRoutingService:
    """纯关键词评分的未指名 Agent 语义路由。"""

    def route(self, message: str) -> AgentSuggestion | None:
        """对消息做关键词评分，返回建议卡片或 None（置信 < 中置信阈值）。"""
        top = self._bestAgent(message)
        if top is None:
            return None
        confidence = min(1.0, top.hits * _SCORE_PER_KEYWORD)
        if confidence < MEDIUM_CONFIDENCE:
            return None
        return AgentSuggestion(
            recommended_agent_code=top.agent_code,
            confidence=round(confidence, 2),
            reason=_AGENT_REASONS[top.agent_code],
        )

    @staticmethod
    def _bestAgent(message: str) -> _TopAgent | None:
        """返回命中关键词最多的 Agent；无任何命中返回 None。

        并列时取先注册的 Agent（字典插入序，360 → risk → graph），评分稳定。
        """
        best: _TopAgent | None = None
        for code, keywords in _AGENT_KEYWORDS.items():
            hits = sum(1 for kw in keywords if kw in message)
            if best is None or hits > best.hits:
                best = _TopAgent(agent_code=code, hits=hits)
        if best is None or best.hits == 0:
            return None
        return best
