# 变更：M2-2b 受控邻接 `GET /procurement/knowledge/graph/neighbors`

- 日期：2026-11-20　分支：`feat/knowledge-evidence-contract`（与 M2-1/M2-2a 同支累积，off main）

## 内容（设计稿 §8.3 第二端点）
- **新路由** `app/api/v1/procurement_knowledge_graph.py`：
  `entityType / entityKey / relationTypes[] / maxHops(1..5)` ⇒
  `nodes[] + edges[] + truncated`；
- **守卫全复用，零新增图查询**：label 白名单 / 起点存在性 404 / 深度 [1,5] /
  图侧关系白名单 Cypher 全部来自既有 `GraphTraversalService.traverse`；
- **视图变换纯函数** `services/knowledge/graph_neighbors.buildNeighborsView`：
  - **关系白名单交集**：请求 relationTypes ∩ `BUSINESS_RELATION_TYPES`
    （neo4j_client 单点事实，7 种业务关系）；白名单外类型**不可遍历**，
    丢弃经 `effectiveRelationTypes` 自证（调用方不会误以为跑了一条被丢弃的路）；
  - **边数上限** `MAX_NEIGHBOR_EDGES=200` 截断 + `truncated` 置位
    （真实规模 92 万 CONTAINS 行，深跳子图不能原样进响应）；
  - 节点按 key 去重、(entity_type, key) 排序（确定性）。
- **双接线**（main.py + _testapp.py 同 `enableKnowledgeFacade` 块）；
  接线守卫扩展到两个新模块。

## 测试（7 用例，新 unit 文件）
白名单交集过滤 ×1 / 全白名单缺省 ×1 / 节点去重 ×1 / 截断 ×2 / 空输入身份回显 ×1 /
双接线守卫 ×1。ruff：B008 ×4 为仓内 FastAPI 路由惯例（与 procurement_knowledge.py
同款基线）；其余零新增。
