import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  createOrchestration,
  listOrchestrations,
  listOrchestrationToolOptions,
  getOrchestration,
  updateOrchestration,
  toggleOrchestration,
  createAgentRun,
  listAgentRuns,
  getAgentRun,
  getAgentRunSteps,
  resumeAgentRun,
  cancelAgentRun,
} from "../api/agentOrchestrationApi";

// =============================================================================
// Agent 编排配置（/agent-orchestrations）
// =============================================================================

describe("api/agentOrchestrationApi — 配置", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("createOrchestration POST /agent-orchestrations", async () => {
    const payload = {
      agentCode: "supplier_360",
      instructions: "按 360° 视角分析供应商",
      toolNames: ["supplier_profile", "supplier_orders"],
      defaultMaxIterations: 20,
      defaultMaxToolCalls: 30,
      defaultMaxCostUsd: 5,
      defaultTimeoutSeconds: 900,
      maxObservationChars: 50000,
    };
    httpMock.post.mockResolvedValue({ data: { ...payload, enabled: false, revision: 1, bindings: [] } });
    const result = await createOrchestration(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/agent-orchestrations", payload);
    expect(result.agentCode).toBe("supplier_360");
  });

  it("listOrchestrations GET /agent-orchestrations（无 query）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listOrchestrations();
    expect(httpMock.get).toHaveBeenCalledWith("/agent-orchestrations");
  });

  it("listOrchestrationToolOptions GET /agent-orchestrations/tool-options", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listOrchestrationToolOptions();
    expect(httpMock.get).toHaveBeenCalledWith("/agent-orchestrations/tool-options");
  });

  it("getOrchestration GET /agent-orchestrations/:agentCode", async () => {
    httpMock.get.mockResolvedValue({ data: { agentCode: "supplier_360" } });
    const result = await getOrchestration("supplier_360");
    expect(httpMock.get).toHaveBeenCalledWith("/agent-orchestrations/supplier_360");
    expect(result.agentCode).toBe("supplier_360");
  });

  it("getOrchestration 对 agentCode 做 URI 编码", async () => {
    httpMock.get.mockResolvedValue({ data: { agentCode: "a/b" } });
    await getOrchestration("a/b");
    expect(httpMock.get).toHaveBeenCalledWith("/agent-orchestrations/a%2Fb");
  });

  it("updateOrchestration PUT /agent-orchestrations/:agentCode", async () => {
    const payload = {
      instructions: "新的指令",
      toolNames: ["supplier_profile"],
      defaultMaxIterations: 10,
      defaultMaxToolCalls: 12,
      defaultMaxCostUsd: 1.5,
      defaultTimeoutSeconds: 300,
      maxObservationChars: 20000,
      revision: 3,
    };
    httpMock.put.mockResolvedValue({ data: { agentCode: "supplier_360", revision: 4 } });
    const result = await updateOrchestration("supplier_360", payload);
    expect(httpMock.put).toHaveBeenCalledWith("/agent-orchestrations/supplier_360", payload);
    expect(result.revision).toBe(4);
  });

  it("toggleOrchestration POST /agent-orchestrations/:agentCode/toggle", async () => {
    const payload = { enabled: true, revision: 4 };
    httpMock.post.mockResolvedValue({ data: { agentCode: "supplier_360", enabled: true, revision: 5 } });
    const result = await toggleOrchestration("supplier_360", payload);
    expect(httpMock.post).toHaveBeenCalledWith("/agent-orchestrations/supplier_360/toggle", payload);
    expect(result.enabled).toBe(true);
  });
});

// =============================================================================
// Agent 编排运行（/agent-runs）
// =============================================================================

describe("api/agentOrchestrationApi — 运行", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("createAgentRun POST /agent-runs", async () => {
    const payload = {
      agentCode: "supplier_360",
      input: "帮我看看这家供应商",
      modelConfigId: 7,
      limits: { maxIterations: 5, maxToolCalls: 8, maxCostUsd: 1, timeoutSeconds: 120 },
    };
    httpMock.post.mockResolvedValue({ data: { runId: "run-1", status: "QUEUED" } });
    const result = await createAgentRun(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/agent-runs", payload);
    expect(result.runId).toBe("run-1");
  });

  it("listAgentRuns GET /agent-runs（省略 undefined filter）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listAgentRuns({ agentCode: "supplier_360", status: "RUNNING", limit: 10 });
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/agent-runs");
    expect(call[1].params).toEqual({ agentCode: "supplier_360", status: "RUNNING", limit: 10 });
    expect(Object.prototype.hasOwnProperty.call(call[1].params, "offset")).toBe(false);
  });

  it("listAgentRuns 无 filter 时不发送任何 query 值", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listAgentRuns();
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/agent-runs");
    expect(call[1].params).toEqual({});
  });

  it("getAgentRun GET /agent-runs/:runId", async () => {
    httpMock.get.mockResolvedValue({ data: { runId: "run-1", status: "RUNNING" } });
    const result = await getAgentRun("run-1");
    expect(httpMock.get).toHaveBeenCalledWith("/agent-runs/run-1");
    expect(result.status).toBe("RUNNING");
  });

  it("getAgentRunSteps GET /agent-runs/:runId/steps 带 afterStepNo/limit", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await getAgentRunSteps("id", { afterStepNo: 40, limit: 100 });
    expect(httpMock.get).toHaveBeenCalledWith("/agent-runs/id/steps", {
      params: { afterStepNo: 40, limit: 100 },
    });
  });

  it("getAgentRunSteps 省略未指定的 query 值", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await getAgentRunSteps("run-1");
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/agent-runs/run-1/steps");
    expect(call[1].params).toEqual({});
  });

  it("resumeAgentRun POST /agent-runs/:runId/resume", async () => {
    const payload = { input: "补充说明", additionalIterations: 3 };
    httpMock.post.mockResolvedValue({ data: { runId: "run-1", status: "QUEUED" } });
    const result = await resumeAgentRun("run-1", payload);
    expect(httpMock.post).toHaveBeenCalledWith("/agent-runs/run-1/resume", payload);
    expect(result.status).toBe("QUEUED");
  });

  it("cancelAgentRun POST /agent-runs/:runId/cancel（无 body）", async () => {
    httpMock.post.mockResolvedValue({ data: { runId: "run-1", status: "CANCELLED" } });
    const result = await cancelAgentRun("run-1");
    expect(httpMock.post).toHaveBeenCalledWith("/agent-runs/run-1/cancel");
    expect(result.status).toBe("CANCELLED");
  });

  it("cancelAgentRun 对 runId 做 URI 编码", async () => {
    httpMock.post.mockResolvedValue({ data: { runId: "a b" } });
    await cancelAgentRun("a b");
    expect(httpMock.post).toHaveBeenCalledWith("/agent-runs/a%20b/cancel");
  });
});
