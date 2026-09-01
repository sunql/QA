"""Phase 6.3 知识图谱多跳推理服务（feat-graph-traversal-api）。

职责：对 Neo4j 业务关系图（Phase 6.2 BusinessEntity 子图）做多跳遍历，
供 REST API（``GET /api/v1/graph/traverse``）与 Chat 推理问法两条路径消费。

推理语义（plan §6.3）::

    GET /graph/traverse?start_type=SUPPLIER&start_key=100001&max_hops=3
    -> 返回经过的节点与关系链（逐跳展开）

设计约束：
- 只读：纯图查询，无写入、无 LLM、无 SQL；
- 起点校验：白名单 label + 节点存在性（404 语义 NotFoundError）；
- 深度上限：``_MAX_TRAVERSAL_HOPS``（5），防无界遍历；
- 空结果：节点存在但无可达边 -> hops=[] + 空结果消息（200 语义，非 404）；
- Chat 集成：问句抽取 supplierKey -> 从 Supplier 起点 2 跳遍历 -> answer
  由模板合成（列出可达实体类型 + 数量，不调 LLM，保持响应稳定）。
- 异步化：Neo4j 官方驱动同步调用，用 ``asyncio.to_thread`` 包装，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import logging

from app.domain.error_messages import (
    MSG_GRAPH_TRAVERSAL_EMPTY,
    MSG_GRAPH_TRAVERSAL_NOT_FOUND,
)
from app.domain.exceptions import NotFoundError
from app.domain.schemas import GraphTraversalHop, GraphTraversalRead
from app.infrastructure import neo4j_client as neo4j
from app.infrastructure.neo4j_client import _MAX_TRAVERSAL_HOPS

logger = logging.getLogger(__name__)

# Chat 推理问法默认深度：2 跳覆盖「供应商 -> 物料 / 合同 / 订单」主语义。
_CHAT_DEFAULT_HOPS = 2


def resolveChatMaxHops(max_hops: int | None) -> int:
    """Chat 图推理跳数解析（Phase 7 G3）：None 默认 2，越界 clamp 到 [1, 5]。

    与 API 层 ``Query(ge=1, le=5)`` 直接拒绝不同，chat 里越界跳数是口语
    （「10 跳」纯属用户不了解上限），clamp 比 4xx/500 更友好。clamp 后
    传入 ``traverse`` 必然通过其 ``1 <= maxHops <= _MAX_TRAVERSAL_HOPS`` 校验。
    """
    requested = _CHAT_DEFAULT_HOPS if max_hops is None else max_hops
    return max(1, min(requested, _MAX_TRAVERSAL_HOPS))


class GraphTraversalService:
    """业务关系图多跳遍历器。"""

    async def traverse(
        self, startType: str, startKey: str, maxHops: int = 3
    ) -> GraphTraversalRead:
        """从起始实体做多跳遍历（方向不限）。

        - startType：业务实体类型（白名单校验，非法 -> ValueError）
        - startKey：实体键（enterprise_key 字符串化 / Contract 的 document_id）
        - maxHops：遍历深度上限（1..5，越界 -> ValueError）
        - 节点不存在 -> NotFoundError（通用消息，不区分类型是否存在，防侧信道）
        """
        if not 1 <= maxHops <= _MAX_TRAVERSAL_HOPS:
            raise ValueError(
                f"maxHops must be in [1, {_MAX_TRAVERSAL_HOPS}], got {maxHops}"
            )

        # 起点存在性（白名单校验在 getBusinessNode 内，非法 label -> ValueError）
        node = await asyncio.to_thread(neo4j.getBusinessNode, startType, startKey)
        if node is None:
            raise NotFoundError(
                MSG_GRAPH_TRAVERSAL_NOT_FOUND.format(label=startType, key=startKey)
            )

        rows = await asyncio.to_thread(
            neo4j.traverseBusinessGraph,
            startLabel=startType,
            startKey=startKey,
            maxHops=maxHops,
        )
        hops = [GraphTraversalHop(**row) for row in rows]
        reachableTypes = sorted({h.to_type for h in hops})

        return GraphTraversalRead(
            start_key=startKey,
            start_type=startType,
            max_hops=maxHops,
            hops=hops,
            reachable_types=reachableTypes,
        )

    async def traverseForChat(self, supplierKey: str) -> GraphTraversalRead:
        """Chat 推理路径：供应商起点、默认 2 跳。

        与 traverse 的差异：起点固定 Supplier（问句已抽取 key）、
        深度取 _CHAT_DEFAULT_HOPS（避免深跳导致 answer 冗长）。
        """
        return await self.traverse("Supplier", supplierKey, _CHAT_DEFAULT_HOPS)

    def buildChatAnswer(self, result: GraphTraversalRead) -> str:
        """把遍历结果合成自然语言 answer（模板，不调 LLM）。

        空 hops -> 空结果消息；否则按可达类型分组计数，
        末行提示可调用 traverse API 加深遍历。
        """
        if not result.hops:
            return MSG_GRAPH_TRAVERSAL_EMPTY.format(maxHops=result.max_hops)

        # 按类型分组（不可变：从 hops 派生新结构，不改原对象）
        byType: dict[str, list[GraphTraversalHop]] = {}
        for hop in result.hops:
            byType.setdefault(hop.to_type, []).append(hop)

        typeLabels = {
            "Material": "物料",
            "PurchaseOrder": "采购订单",
            "GoodsReceipt": "收货单",
            "IncomingInspection": "来料检验",
            "NCR": "不合格处理",
            "Contract": "合同",
            "Supplier": "供应商",
        }
        segments = []
        for entityType in sorted(byType):
            label = typeLabels.get(entityType, entityType)
            # 去重计数：同一实体可能经多条路径可达
            codes = sorted({h.to_code for h in byType[entityType]})
            segments.append(f"{len(codes)} 个{label}（{'、'.join(codes[:8])}）")

        code = result.start_key
        answer = f"供应商 {code} 在 {result.max_hops} 跳内关联：{'；'.join(segments)}。"
        if len(result.hops) >= 20:
            answer += "（结果较多，仅列前 8 个编码；可用 /graph/traverse 接口查看完整链路）"
        return answer
