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

from app.domain.exceptions import ValidationError
from app.services.semantic_compiler import compileManifest
from app.services.semantic_compiler_bindings import SourceBindingCompileError
from app.services.semantic_diff_service import SemanticDiff, diffManifests
from app.services.semantic_domain_slice import sliceManifestToDomain
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

    ⚠️ **两侧必须同口径**：按域发布后，域 ACTIVE 行的正文是**切片**
    （D-10），而候选是全域编译结果 ⇒ 不裁剪候选侧的话，
    「切片 vs 全量」会把**别的域**的成千上万个类/JOIN/绑定全报成 `ADDITIVE`
    （评审看到「新增 3000 个类」等于什么都没看到），而且**他域的破坏性删除
    在两边都不存在 ⇒ 彻底不可见**。

    ⇒ 候选侧按**同一个域**切一次（切片是幂等的 ⇒ 与基线同宇宙），
    并用 `scope` 如实披露比较范围（`DOMAIN` / `GLOBAL`）。
    """

    baseline = await semanticRegistry.getActive(session, domainCode=domainCode)
    try:
        candidate = await compileManifest(session)
    except SourceBindingCompileError as exc:
        # 源绑定数据不满足发布不变量（表达式不安全 / 归属错配）——
        # 这是**治理数据问题**，调用方能修 ⇒ 给一条说得清「哪条绑定」的 422，
        # 而不是让未处理异常变成 500（丢掉诊断信息）。
        raise ValidationError(f"源绑定数据不满足发布约束：{exc}") from exc
    scopeDomain = baseline.domain_code if baseline is not None else domainCode
    if scopeDomain is not None:
        candidate = sliceManifestToDomain(candidate, scopeDomain)
    baseManifest: dict[str, Any] = baseline.manifest if baseline is not None else {}
    diff = diffManifests(baseManifest, candidate)
    return OntologyDiffResult(
        baselineReleaseId=baseline.id if baseline is not None else None,
        baselineVersion=baseline.version if baseline is not None else None,
        baselineHash=baseline.manifest_hash if baseline is not None else None,
        baselineDomainCode=baseline.domain_code if baseline is not None else None,
        scope="DOMAIN" if scopeDomain is not None else "GLOBAL",
        diff=diff,
    )
