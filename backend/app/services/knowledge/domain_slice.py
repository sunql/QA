"""P4 域切片（**知识侧入口**）：切片实现已迁到 `app/services/semantic_domain_slice.py`。

为什么搬家（M1-1.3 批次 B）
--------------------------
切片口径原本是知识侧（P4 召回）独有的；M1-1.3 要用它做**发布域切片**
（`createDraft(domainCode=...)` 只发该域的那一份快照）。⇒ 实现下沉到
服务层公共位置 `app/services/semantic_domain_slice.py`（纯函数，不吃 DB），
知识侧与发布侧**共用同一份口径** ⇒ 两边不会各切各的、悄悄漂移。

本模块保留为**转发层**：既有调用方（`knowledge/metric_route.py` 与单测）
的 import 路径不变。

⚠️ 切片口径本身没变：业务对象挂域 → 类的父类闭包 → JOIN 两端都在类集合里
→ 指标按类命中 → KPI 按指标命中；归不了属的条目一律排除（fail-closed）。
发布切片在此之上多做「引用闭合补齐」，见新模块的文档串。
"""

from __future__ import annotations

from app.services.semantic_domain_slice import (
    DomainSlice,
    sliceManifestByDomain,
    sliceManifestForRelease,
    sliceToManifest,
)

__all__ = [
    "DomainSlice",
    "sliceManifestByDomain",
    "sliceManifestForRelease",
    "sliceToManifest",
]
