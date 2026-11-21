"""I2-B 证据冲突检测（设计稿 §4.3）。

> 证据冲突时输出**冲突提示与来源时间**，**不让模型自行选一个看起来合理的值**。

判定口径（确定性、无 LLM）：
1. **同一键** = `(kind, ref)`（同一个类/属性/JOIN/指标/文档）；
2. **来自 ≥2 个不同通道**；
3. 且各通道给出的 `snippet`（归一后）**不完全一致** ⇒ 判为冲突。

为什么要求「跨通道」：同一通道内部的多条渲染（如同一类的多个属性）不是冲突；
只有**不同通道对同一对象的描述不一致**才是需要人工裁决的口径分歧。

为什么输出全部来源而不是挑一个：§4.3 明确禁止替模型选值。冲突条目带
`channel / releaseVersion / observedAt / snippet` ⇒ 人工可裁决、可追责。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from app.domain.knowledge_schemas import (
    KnowledgeConflictEntryRead,
    KnowledgeConflictRead,
    KnowledgeEvidenceRead,
)


def _normSnippet(value: str | None) -> str:
    """归一（去空白、小写）——仅比较「是否同一内容」，不改写原文。"""
    return "".join((value or "").split()).lower()


def detectConflicts(
    evidence: Iterable[KnowledgeEvidenceRead],
) -> tuple[KnowledgeConflictRead, ...]:
    """按 `(kind, ref)` 分组检测跨通道冲突（输出按 key 升序，确定可复现）。"""
    grouped: dict[str, list[KnowledgeEvidenceRead]] = defaultdict(list)
    for item in evidence:
        grouped[f"{item.kind}:{item.ref}"].append(item)

    conflicts: list[KnowledgeConflictRead] = []
    for key in sorted(grouped):
        items = grouped[key]
        channels = tuple(sorted({item.channel for item in items}))
        if len(channels) < 2:
            continue  # 单通道内部不构成冲突
        if len({_normSnippet(item.snippet) for item in items}) < 2:
            continue  # 各通道内容一致 ⇒ 不是冲突
        conflicts.append(
            KnowledgeConflictRead(
                key=key,
                reason=(
                    f"同一对象（{key}）在多个通道给出不同内容："
                    f"{'、'.join(channels)}。请人工核对来源与口径，系统不代选。"
                ),
                channels=channels,
                entries=tuple(
                    KnowledgeConflictEntryRead(
                        channel=item.channel,
                        snippet=item.snippet,
                        release_version=item.release_version,
                        observed_at=item.observed_at,
                    )
                    for item in sorted(items, key=lambda it: (it.channel, it.ref))
                ),
            )
        )
    return tuple(conflicts)
