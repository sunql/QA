# ChainedStep data model and render_prior_cte utility for L3 multi-Plan CTE chaining.
#
# Design (plan summary.md §3 Phase 3):
#   LLM generates multiple Plans; each Plan's formula may depend on a prior
#   Plan's CTE output.  ChainedStep models one step in that chain:
#   - step_id       : unique identifier for this step (used in depends_on)
#   - step_index    : 0-based execution order
#   - description   : human-readable intent (used in LLM prompts / logs)
#   - formula       : SQL SELECT expression for this step's CTE body
#   - depends_on    : tuple of step_ids this step references
#   - output_alias  : CTE name used in subsequent steps (e.g. "ratio_cte")
#
# render_prior_cte() takes a tuple of ChainedSteps and a current_index, then
# emits a "WITH cte1 AS (...), cte2 AS (...)" string containing all steps whose
# step_index < current_index.  This string is injected into Nl2SqlService as
# prior_cte so that the current step's formula can reference prior CTEs.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChainedStep:
    """Immutable description of one step in a multi-Plan CTE chain."""

    step_id: str
    step_index: int
    description: str
    formula: str
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    output_alias: str = ""

    def __post_init__(self) -> None:
        if self.step_index < 0:
            raise ValueError(f"step_index must be >= 0, got {self.step_index}")
        if not self.step_id:
            raise ValueError("step_id must be non-empty")


def render_prior_cte(
    steps: tuple[ChainedStep, ...],
    current_index: int,
) -> str:
    """
    Render all ChainedSteps whose step_index < current_index as a
    ``WITH cte1 AS (formula1), cte2 AS (formula2), ...`` SQL clause.

    Returns an empty string when current_index <= 0 (nothing to chain).

    Parameters
    ----------
    steps
        All steps in the chain (ordered by step_index).
    current_index
        Index of the step being rendered; prior steps are those with
        step_index < current_index.

    Returns
    -------
    str
        ``WITH alias1 AS (...), alias2 AS (...)`` or ``""`` when there are
        no prior steps.
    """
    if current_index <= 0:
        return ""

    parts: list[str] = []
    for step in steps:
        if step.step_index >= current_index:
            break
        parts.append(f"{step.output_alias} AS ({step.formula})")

    if not parts:
        return ""

    return "WITH " + ", ".join(parts)
