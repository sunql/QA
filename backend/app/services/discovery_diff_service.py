"""本体发现：基线发布 vs 当前治理表的差异编排（WP-ONT-AI A1-4 的 diff 端点）。

**纯函数与 IO 的分界**（D-7）：

- `semantic_diff_service.diffManifests` 是**纯函数**（manifest in / diff out，
  零 IO、零 DB、不改入参）——它不知道「基线」是谁、也不关心数据从哪来；
- 本模块只做**取数与编排**：基线 = 已发布 ACTIVE 的 `manifest_json`，
  候选 = 当前治理表编译结果（`compileManifest`）。

比较基线的选择理由见 `plan.md` D-4：基线必须**固定**（已发布的那一版），
才能回答「发布之后本体被改了什么」；拿「当前治理表」当基线会自我抵消。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.semantic_compiler import compileManifest
from app.services.semantic_diff_service import SemanticDiff, diffManifests
from app.services.semantic_registry_service import semanticRegistry


@dataclass(frozen=True)
class OntologyDiffResult:
    """一次差异分析的结果（不可变）。"""

    baselineReleaseId: int | None
    baselineVersion: str | None
    baselineHash: str | None
    baselineDomainCode: str | None
    scope: str
    diff: SemanticDiff


async def computeOntologyDiff(
    session: AsyncSession, *, domainCode: str | None = None
) -> OntologyDiffResult:
    """基线（ACTIVE 发布）vs 候选（当前治理表编译结果）的差异。

    没有 ACTIVE 发布时基线取空 ⇒ 当前全部类/属性表现为 `ADDITIVE`
    （「还没发布过」是正常状态，不是错误）。

    ⚠️ `domainCode` **只用于定位基线**（取该域的 ACTIVE 发布），**不裁剪候选侧**
    ——候选是全域编译结果（`compileManifest` 无域参数）⇒ 比较范围恒为全域。
    该事实由 `scope="GLOBAL"` 如实披露（代码审查 M-2），避免调用方把
    `domainCode=SALES` 误读成「只看 SALES 域的差异」。
    """

    baseline = await semanticRegistry.getActive(session, domainCode=domainCode)
    candidate = await compileManifest(session)
    baseManifest: dict[str, Any] = baseline.manifest if baseline is not None else {}
    diff = diffManifests(baseManifest, candidate)
    return OntologyDiffResult(
        baselineReleaseId=baseline.id if baseline is not None else None,
        baselineVersion=baseline.version if baseline is not None else None,
        baselineHash=baseline.manifest_hash if baseline is not None else None,
        baselineDomainCode=baseline.domain_code if baseline is not None else None,
        scope="GLOBAL",
        diff=diff,
    )
