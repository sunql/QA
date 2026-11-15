"""「按发布读图」的路由器（P3b D25）。

本体图谱读**有两个源**：

- **主图**（`:Class` 等，现状）：被写侧维护（`ontology_service` 图同步差分）与
  治理状态计数消费 —— 这两个**永远读主图**（同步写主图、状态报主图），
  **不经过本路由器**；
- **发布镜像**（`:ReleaseClass` 等，D22 起）：给「按发布读」的消费面
  （P4 知识服务 Facade 起接入）。

开关 = 配置 `SEMANTIC_GRAPH_RELEASE_ID`（发布 id）：

- **不设 / 0**（默认）⇒ 主图（与今天完全一致 ⇒ 不部署不改变任何行为）；
- **设了 id** ⇒ 那一版的镜像；**一键回退 = 拿掉环境变量**。
- 切换前置条件：`scripts/verify_graph_mirror` 退出码 0（对账一致才允许切）。

⚠️ 范围声明：镜像**不含** `ontology_relation` 驱动的语义关系边（manifest 无
relations 分区，L-2/R-2）⇒ 发布 reader 的 `relationTriples()` 只覆盖
`SUBCLASS_OF`（镜像里唯一的 class→class 非 JOIN 边型）。消费方要语义关系边
⇒ 回主图或等向量/关系镜像另立卡。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.config import getSettings
from app.infrastructure import neo4j_client as neo4j
from app.infrastructure import neo4j_mirror_client as mirror

_MODE_MAIN = "main"
_MODE_RELEASE = "release"


class _OntologyGraphSource(Protocol):
    """本体读的鸭子类型（主图与镜像两侧同形）。"""

    def classIds(self) -> set[str]: ...
    def propertyIds(self) -> set[str]: ...
    def metricIds(self) -> set[str]: ...
    def kpiCatalogIds(self) -> set[str]: ...
    def joinPairs(self) -> set[tuple[str, str]]: ...
    def relationTriples(self) -> set[tuple[str, str, str]]: ...


@dataclass(frozen=True)
class MainGraphReader:
    """主图读（现状行为；`neo4j_client` 的对账读函数）。"""

    mode: str = _MODE_MAIN
    releaseId: int | None = None

    def classIds(self) -> set[str]:
        return neo4j.getClassIds()

    def propertyIds(self) -> set[str]:
        return neo4j.getPropertyIds()

    def metricIds(self) -> set[str]:
        return neo4j.getMetricIds()

    def kpiCatalogIds(self) -> set[str]:
        return neo4j.getKpiCatalogIds()

    def joinPairs(self) -> set[tuple[str, str]]:
        return neo4j.getJoinPairs()

    def relationTriples(self) -> set[tuple[str, str, str]]:
        return neo4j.getRelationTriples()


@dataclass(frozen=True)
class ReleaseGraphReader:
    """发布镜像读（D25 起；全部按 `releaseId` 钉住）。"""

    releaseId: int
    mode: str = _MODE_RELEASE

    def classIds(self) -> set[str]:
        return mirror.getReleaseClassIds(self.releaseId)

    def propertyIds(self) -> set[str]:
        return mirror.getReleasePropertyIds(self.releaseId)

    def metricIds(self) -> set[str]:
        return mirror.getReleaseMetricIds(self.releaseId)

    def kpiCatalogIds(self) -> set[str]:
        return mirror.getReleaseKpiCatalogIds(self.releaseId)

    def joinPairs(self) -> set[tuple[str, str]]:
        return mirror.getReleaseJoinPairs(self.releaseId)

    def relationTriples(self) -> set[tuple[str, str, str]]:
        return mirror.getReleaseRelationTriples(self.releaseId)


def resolveOntologyGraphReader() -> _OntologyGraphSource:
    """按配置路由：`SEMANTIC_GRAPH_RELEASE_ID` 打开 ⇒ 镜像；否则主图。

    非数字的开关值**显式炸掉**（静默回主图 = 假切换，比炸更危险）；
    `0` 视同不钉住（主图）。
    """
    raw = getSettings().semanticGraphReleaseId
    if raw is None or raw == 0:
        return MainGraphReader()
    if not isinstance(raw, int) or raw < 0:
        raise ValueError(
            f"SEMANTIC_GRAPH_RELEASE_ID 必须是非负整数或不设，得到 {raw!r}"
        )
    return ReleaseGraphReader(releaseId=raw)
