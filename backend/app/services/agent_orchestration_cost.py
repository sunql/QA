"""Cost labelling and usage normalisation for the orchestration subsystem.

Why this module exists
----------------------
The metered recorder (:mod:`app.services.agent_orchestration_metering`) owns
the *writing* of usage rows; the *meaning* of those rows — which cost source a
set of rows aggregates to, and whether a provider-reported usage block counts
as "unknown" — is a separate, purely functional responsibility. Keeping it
here gives both the recorder and any future consumer exactly one place to
look, and keeps the recorder module focused on persistence.

Cost labelling (the part that must not drift)
---------------------------------------------
- A cost computed locally from the model's configured unit prices is
  ``ESTIMATED`` — never ``MEASURED``. ``MEASURED`` is reserved for an explicit
  provider-reported cost, which this phase does not have.
- Unknown tokens ⇒ ``cost_source=UNKNOWN`` and the run's ``cost_complete``
  turns false. ``summarizeCostSources`` is the single place that decides the
  aggregate, so the run level and the usage level can never disagree.
- A provider that reports **no** usage (missing ``usage`` block, or a 0/0
  token count as a truncated ollama stream produces) is *unknown*, not
  *free*: such a call is recorded ``UNKNOWN``/``cost_complete=false`` and
  never as an ``ESTIMATED`` row with cost 0 — a "known total" of zero would
  be exactly the fabricated complete bill this labelling forbids.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.services.agent_orchestration_types import CostSource

MEASURED = CostSource.MEASURED.value
ESTIMATED = CostSource.ESTIMATED.value
UNKNOWN = CostSource.UNKNOWN.value
MIXED = CostSource.MIXED.value


def summarizeCostSources(sources: Iterable[str]) -> tuple[str, bool]:
    """Aggregate usage-row cost sources into ``(source, complete)``.

    Truth table (the run level is the aggregate of that run's usage rows):

    ==========================  ================  ==========
    usage row sources           run cost_source   complete
    ==========================  ================  ==========
    (none)                      UNKNOWN           False
    all MEASURED                MEASURED          True
    all ESTIMATED               ESTIMATED         True
    MEASURED + ESTIMATED        MIXED             True
    anything with UNKNOWN       UNKNOWN           False
    ==========================  ================  ==========

    An unrecognized source is treated like UNKNOWN: an unclassifiable cost
    figure must never be presented as measured or complete.
    """
    values = {str(source) for source in sources if source is not None}
    if not values:
        return (UNKNOWN, False)
    if UNKNOWN in values:
        return (UNKNOWN, False)
    known = values - {UNKNOWN}
    if known == {MEASURED}:
        return (MEASURED, True)
    if known == {ESTIMATED}:
        return (ESTIMATED, True)
    if known == {MEASURED, ESTIMATED} or MIXED in known:
        return (MIXED, True)
    return (UNKNOWN, False)


def isUnknownUsage(promptTokens: int, completionTokens: int) -> bool:
    """0/0 表示 provider 没有给出可用用量。

    与 `LlmResponse.isApproximateUsage` 同义（那里只有 LlmResponse 有这个
    property，流式终止块与 usage mapping 没有）：provider 省略 usage 字段、
    或 ollama 那样截断出一个 token 数为 0 的终止块，都属于「用量未知」，
    不是「这次调用花了 0 块」。
    """
    return promptTokens == 0 and completionTokens == 0


def usageFromMapping(usage: dict | None) -> tuple[int, int, int | None]:
    """Read ``(prompt, completion, cached)`` out of a provider usage mapping."""
    if not isinstance(usage, dict):
        return (0, 0, None)
    promptTokens = int(usage.get("prompt_tokens") or 0)
    completionTokens = int(usage.get("completion_tokens") or 0)
    cachedTokens = usage.get("cached_tokens")
    return (
        promptTokens,
        completionTokens,
        int(cachedTokens) if isinstance(cachedTokens, int) else None,
    )
