"""业务图 label / 关系类型白名单（代码级唯一事实源）。

为什么单独一个模块
------------------
这组白名单有**两个消费方**，而且分别住在不同层：

- `app.infrastructure.neo4j_client`：CQL 拼接前置防御（label / 关系类型不可参数化）；
- `app.services.semantic_release_design_checks`：发布校验码 `GRF-001`
  （「图 label / 关系类型必须在代码白名单内」）。

若白名单留在 infrastructure，纯校验模块就得 import 基础设施层（domain → infra 的反向依赖）。
⇒ 白名单下沉到 domain 叶子模块（零依赖），两侧都从这里取，**不可能漂移**。

⚠️ 新增业务实体类型时**三处同改**：本白名单 + `BUSINESS_RELATION_TYPES`（若带来新关系）
+ 前端图查询的 `startType` 可选项（后端 `VALID_START_TYPES` 已从白名单派生）。
"""

from __future__ import annotations

# 业务实体节点 label：节点统一携带 BusinessEntity 主 label + 具体类型副 label，
# 与本体图（Class/Property/Metric）物理隔离 —— 本体同步/删除路径永不触碰业务节点。
# CQL label 不可参数化，必须白名单防注入。
BUSINESS_ENTITY_LABELS = frozenset(
    {
        "Supplier",
        "ItemMaster",  # 原 Material（Phase 4.4 统一为 class_name）
        "PurchaseOrder",
        "Receipt",  # 原 GoodsReceipt
        "IncomingInspection",
        "Contract",  # 由文档目录提供
        # 删除：Material / GoodsReceipt / NCR（Phase 4.4 NCR 不入图）
    }
)

# 业务关系类型（Sheet 16 采购业务流转语义）：
# Supplier-SUPPLIES->Material / PurchaseOrder-CONTAINS->Material /
# PurchaseOrder-GENERATES->GoodsReceipt / GoodsReceipt-INSPECTED_BY->IncomingInspection /
# IncomingInspection-GENERATED->NCR / Supplier-SIGNED->Contract
# PLACES（Supplier->PurchaseOrder）为 THBI 采购订单真实数据回填新增
# （feat-business-graph-backfill）：demo 种子里没有「供应商下单」语义边。
# CQL 关系类型同样不可参数化，白名单防注入。
BUSINESS_RELATION_TYPES = frozenset(
    {
        "SUPPLIES",
        "CONTAINS",
        "GENERATES",
        "INSPECTED_BY",
        "SIGNED",
        "PLACES",
    }
)
