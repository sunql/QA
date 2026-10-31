import { describe, expect, it } from "vitest";

import { asAgentTrace, formatDuration, groupStepsByIteration } from "./agentTrace";

const VALID_STEP = {
  index: 0,
  iteration: 0,
  tool: "list_tables",
  status: "ok",
  elapsedMs: 12,
  args: { keyword: "supply" },
  resultSummary: { tables: 3 },
};

function makeTrace(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    kind: "l4_agent_trace",
    status: "answered",
    iterations: 2,
    totalMs: 1234,
    stepCount: 1,
    steps: [VALID_STEP],
    ...overrides,
  };
}

describe("asAgentTrace", () => {
  it("returns a normalized trace for a well-formed snapshot", () => {
    const trace = asAgentTrace(makeTrace());

    expect(trace).not.toBeNull();
    expect(trace?.kind).toBe("l4_agent_trace");
    expect(trace?.status).toBe("answered");
    expect(trace?.iterations).toBe(2);
    expect(trace?.steps).toHaveLength(1);
    expect(trace?.steps[0]).toEqual(VALID_STEP);
  });

  it("returns null for non-objects and foreign kinds", () => {
    expect(asAgentTrace(null)).toBeNull();
    expect(asAgentTrace("l4_agent_trace")).toBeNull();
    expect(asAgentTrace([])).toBeNull();
    expect(asAgentTrace(makeTrace({ kind: "other" }))).toBeNull();
  });

  it("rejects terminal states that never produce a card", () => {
    expect(asAgentTrace(makeTrace({ status: "error" }))).toBeNull();
    expect(asAgentTrace(makeTrace({ status: "fallback" }))).toBeNull();
  });

  it("returns null when steps is empty or not an array", () => {
    expect(asAgentTrace(makeTrace({ steps: [] }))).toBeNull();
    expect(asAgentTrace(makeTrace({ steps: "nope" }))).toBeNull();
  });

  it("drops malformed steps and recomputes stepCount", () => {
    const trace = asAgentTrace(
      makeTrace({
        stepCount: 3,
        steps: [
          VALID_STEP,
          { ...VALID_STEP, index: 1, tool: 42 },
          { ...VALID_STEP, index: 2, status: "weird" },
        ],
      }),
    );

    expect(trace?.steps).toHaveLength(1);
    expect(trace?.stepCount).toBe(1);
  });

  it("caps steps at 50 and marks the snapshot truncated", () => {
    const steps = Array.from({ length: 60 }, (_, i) => ({ ...VALID_STEP, index: i }));

    const trace = asAgentTrace(makeTrace({ steps }));

    expect(trace?.steps).toHaveLength(50);
    expect(trace?.stepCount).toBe(50);
    expect(trace?.truncated).toBe(true);
  });

  it("keeps unknown tool names but normalizes non-object args to empty records", () => {
    const trace = asAgentTrace(
      makeTrace({
        steps: [{ ...VALID_STEP, tool: "brand_new_tool", args: null, resultSummary: "x" }],
      }),
    );

    expect(trace?.steps[0].tool).toBe("brand_new_tool");
    expect(trace?.steps[0].args).toEqual({});
    expect(trace?.steps[0].resultSummary).toEqual({});
  });

  it("keeps the error text only on failed steps", () => {
    const trace = asAgentTrace(
      makeTrace({
        steps: [
          { ...VALID_STEP, status: "error", error: "db_connectivity: timeout" },
          { ...VALID_STEP, index: 1, error: "stray" },
        ],
      }),
    );

    expect(trace?.steps[0].error).toBe("db_connectivity: timeout");
    expect(trace?.steps[1].error).toBeUndefined();
  });
});

describe("formatDuration", () => {
  it("renders sub-second durations in milliseconds", () => {
    expect(formatDuration(0)).toBe("0ms");
    expect(formatDuration(880.4)).toBe("880ms");
  });

  it("renders longer durations in seconds with one decimal", () => {
    expect(formatDuration(1234)).toBe("1.2s");
    expect(formatDuration(15000)).toBe("15.0s");
  });

  it("clamps invalid input to 0ms", () => {
    expect(formatDuration(Number.NaN)).toBe("0ms");
    expect(formatDuration(-5)).toBe("0ms");
  });
});

describe("groupStepsByIteration", () => {
  it("groups consecutive steps by iteration in ascending order", () => {
    const trace = asAgentTrace(
      makeTrace({
        steps: [
          { ...VALID_STEP, index: 0, iteration: 0 },
          { ...VALID_STEP, index: 1, iteration: 0 },
          { ...VALID_STEP, index: 2, iteration: 1 },
        ],
      }),
    );

    const groups = groupStepsByIteration(trace!.steps);

    expect(groups.map((g) => g.iteration)).toEqual([0, 1]);
    expect(groups[0].steps.map((s) => s.index)).toEqual([0, 1]);
    expect(groups[1].steps.map((s) => s.index)).toEqual([2]);
  });
});
