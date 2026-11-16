"""P4 四路编排的**统一输入**（编排层 → 路由）。

为什么要有这个对象
------------------
四路必须共用**同一个**权限口径（`KnowledgeScope`）与**同一版**发布面
（`ResolvedRelease`）⇒ 编排层把它们装进**一个不可变对象**，一次构造、四路传下去。

纪律
----
1. **只有编排层构造它**（`facade.py`）；路由只读，不许改。
2. **scope 必填**：即便某一路今天用不上权限口径，也必须**收下**它 ——
   「四路同一口径」靠的是「四路拿到的是同一个对象」，不是靠约定。
3. **session 可空**：纯函数路由（图谱 / 指标 / SQL）不查库；需要查库的路由自己判空。
4. **请求侧收窄只有一个入口**：`entity_filter` 由编排层算好（请求要的 ∩ 可见的）
   ⇒ 路由不许自己再收窄一遍（否则四路口径会漂）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.knowledge_schemas import DEFAULT_TOP_K
from app.services.knowledge.permission import KnowledgeScope
from app.services.knowledge.release_context import ResolvedRelease


@dataclass(frozen=True, slots=True)
class RouteInput:
    """一路召回的输入（四路同形）。

    Attributes:
        question: 用户问题（原文，不做改写）。
        domain_code: 请求的域（**已通过可见性校验** ⇒ 路由层不必再判权限）。
        release: 本轮钉住的发布面（四路同一版 ⇒ 引用回溯到的版本一致）。
        scope: 本轮权威权限口径（四路同一口径 ⇒ 收窄只按它做）。
        top_k: 本路证据条数上限。
        session: 数据库会话（需要查库的路由用；纯函数路由忽略）。
        entity_filter: **请求侧**对业务对象的收窄（`None` = 没要收窄；
            `()` = 收窄到空 ⇒ 各路切片为空）。已与可见集合求过交集
            （编排层算好）⇒ 路由只管照着切。
    """

    question: str
    domain_code: str
    release: ResolvedRelease
    scope: KnowledgeScope
    top_k: int = DEFAULT_TOP_K
    session: Any = None
    entity_filter: tuple[str, ...] | None = None


__all__ = ["RouteInput"]
