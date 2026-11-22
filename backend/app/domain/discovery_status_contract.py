"""WP-ONT-AI ↔ V0.3 语义发布的**状态映射契约**（融合 F1，可执行 SSOT）。

为什么要有这个模块：两份设计各有自己的状态机——

- **发现域**（WP-ONT-AI §6）：`DISCOVERED → CANDIDATE → PROPOSED → IN_REVIEW
  → APPROVED → VALIDATED → PUBLISHED`（任何环节可 `REJECTED` / `NEEDS_EVIDENCE`）
- **语义发布**（V0.3 I1）：`DRAFT → ACTIVE → ARCHIVED`（**域内**单活）

两套并存本身没错（一个是"候选生命周期"，一个是"发布版本生命周期"），但**接缝必须
写死**，否则「谁算已发布」两边各说各话。本模块把接缝变成可 import、可测试的常量：

    APPROVED / VALIDATED  ──►  semantic_release(DRAFT)
    调 activate           ──►  semantic_release(ACTIVE)     ← 只有这一步是"发布"
    REJECTED / NEEDS_EVIDENCE ─►  不产生 release 行

红线：**发现域不得自行把状态置为 PUBLISHED**；`PUBLISHED` 只是"该提案已由发布链
激活"的**派生视图**，真相在 `semantic_release.status`。
"""

from __future__ import annotations

# --- 发现域状态（WP-ONT-AI §6）---
DISCOVERY_DISCOVERED = "DISCOVERED"
DISCOVERY_CANDIDATE = "CANDIDATE"
DISCOVERY_PROPOSED = "PROPOSED"
DISCOVERY_IN_REVIEW = "IN_REVIEW"
DISCOVERY_APPROVED = "APPROVED"
DISCOVERY_VALIDATED = "VALIDATED"
DISCOVERY_PUBLISHED = "PUBLISHED"
DISCOVERY_REJECTED = "REJECTED"
DISCOVERY_NEEDS_EVIDENCE = "NEEDS_EVIDENCE"

#: 发现域全部合法状态（顺序即流程顺序，供前端画流程条）。
DISCOVERY_STATES: tuple[str, ...] = (
    DISCOVERY_DISCOVERED,
    DISCOVERY_CANDIDATE,
    DISCOVERY_PROPOSED,
    DISCOVERY_IN_REVIEW,
    DISCOVERY_APPROVED,
    DISCOVERY_VALIDATED,
    DISCOVERY_PUBLISHED,
)

#: 终止/回退态（不属于主流程）。
DISCOVERY_OFF_TRACK_STATES: frozenset[str] = frozenset(
    {DISCOVERY_REJECTED, DISCOVERY_NEEDS_EVIDENCE}
)

# --- 语义发布状态（V0.3 I1）---
RELEASE_DRAFT = "DRAFT"
RELEASE_ACTIVE = "ACTIVE"
RELEASE_ARCHIVED = "ARCHIVED"

#: 发布链的唯一"变为已发布"动作（**发现域不得绕过**）。
PUBLISH_ACTION_PATH = "/api/v1/semantic/releases/{releaseId}/activate"

#: 发现状态 → 应创建的发布版本状态；None = 不创建发布版本。
_TO_RELEASE_STATUS: dict[str, str | None] = {
    DISCOVERY_DISCOVERED: None,
    DISCOVERY_CANDIDATE: None,
    DISCOVERY_PROPOSED: None,
    DISCOVERY_IN_REVIEW: None,
    # 审核通过后才允许落一个 DRAFT 发布版本（此时**仍未发布**）
    DISCOVERY_APPROVED: RELEASE_DRAFT,
    DISCOVERY_VALIDATED: RELEASE_DRAFT,
    # PUBLISHED 是派生视图：它对应的 release 行必须已经 ACTIVE
    DISCOVERY_PUBLISHED: RELEASE_ACTIVE,
    DISCOVERY_REJECTED: None,
    DISCOVERY_NEEDS_EVIDENCE: None,
}


def toReleaseStatus(discoveryStatus: str) -> str | None:
    """发现状态 → 发布版本状态；``None`` = 该状态下不产生/不要求发布版本。"""
    if discoveryStatus not in _TO_RELEASE_STATUS:
        raise ValueError(f"未知的发现状态：{discoveryStatus!r}")
    return _TO_RELEASE_STATUS[discoveryStatus]


def isPublished(discoveryStatus: str, releaseStatus: str | None) -> bool:
    """**唯一判定口径**：只有"提案 PUBLISHED 且对应 release 为 ACTIVE"才算真的发布了。

    只看发现域状态不算（发现域不得自行把状态置为 PUBLISHED 而无 release 行），
    只看 release 状态也不算（可能是别的来源发布的版本）。
    """
    return discoveryStatus == DISCOVERY_PUBLISHED and releaseStatus == RELEASE_ACTIVE
