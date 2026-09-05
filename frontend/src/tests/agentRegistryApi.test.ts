import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listAgents,
  getAgent,
  createAgent,
  updateAgent,
  deprecateAgent,
  listAgentPolicies,
  addAgentPolicy,
  updateAgentPolicy,
  deleteAgentPolicy,
  runAgent,
} from "../api/agentRegistry";

// =============================================================================
// AgentDefinition
// =============================================================================

describe("api/agentRegistry — AgentDefinition", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listAgents GET /agents（无 filter）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listAgents();
    expect(httpMock.get).toHaveBeenCalledWith("/agents", {
      params: {},
      paramsSerializer: expect.any(Function),
    });
  });

  it("listAgents 序列化 status / dataDomain filter", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listAgents({ status: "active", dataDomain: ["PROCUREMENT", "QUALITY"] });
    const call = httpMock.get.mock.calls[0];
    expect(call[0]).toBe("/agents");
    expect(call[1].params).toEqual({ status: "active", dataDomain: ["PROCUREMENT", "QUALITY"] });
    // paramsSerializer 把数组序列化为 repeat 格式
    const serialized = call[1].paramsSerializer(call[1].params);
    expect(serialized).toContain("dataDomain=PROCUREMENT");
    expect(serialized).toContain("dataDomain=QUALITY");
  });

  it("getAgent GET /agents/:code", async () => {
    httpMock.get.mockResolvedValue({ data: { agentCode: "supplier_risk" } });
    const result = await getAgent("supplier_risk");
    expect(httpMock.get).toHaveBeenCalledWith("/agents/supplier_risk");
    expect(result.agentCode).toBe("supplier_risk");
  });

  it("createAgent POST /agents", async () => {
    const payload = {
      agentCode: "x",
      agentName: "X",
      agentType: "GRAPH_REASONING",
      description: "x",
      dataDomain: "PROCUREMENT" as const,
      dataLayer: "DWD" as const,
      requiredTools: [],
      enabled: true,
    };
    httpMock.post.mockResolvedValue({ data: { agentCode: "x", id: 1 } });
    await createAgent(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/agents", payload);
  });

  it("updateAgent PUT /agents/:code", async () => {
    httpMock.put.mockResolvedValue({ data: { agentCode: "x", status: "deprecated" } });
    await updateAgent("x", { status: "deprecated" });
    expect(httpMock.put).toHaveBeenCalledWith("/agents/x", { status: "deprecated" });
  });

  it("deprecateAgent DELETE /agents/:code 并返回 data", async () => {
    httpMock.delete.mockResolvedValue({ data: { agentCode: "x", status: "DEPRECATED" } });
    const result = await deprecateAgent("x");
    expect(httpMock.delete).toHaveBeenCalledWith("/agents/x");
    expect(result.status).toBe("DEPRECATED");
  });

  it("runAgent POST /agents/:code/run", async () => {
    httpMock.post.mockResolvedValue({ data: { runId: "r-1", status: "RUNNING" } });
    await runAgent("supplier_360", "输入");
    expect(httpMock.post).toHaveBeenCalledWith("/agents/supplier_360/run", {
      input: "输入",
    });
  });
});

// =============================================================================
// AgentAccessPolicy
// =============================================================================

describe("api/agentRegistry — AgentAccessPolicy", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listAgentPolicies GET /agents/:code/policies", async () => {
    httpMock.get.mockResolvedValue({ data: [{ id: 1, agentCode: "x" }] });
    await listAgentPolicies("x");
    expect(httpMock.get).toHaveBeenCalledWith("/agents/x/policies");
  });

  it("addAgentPolicy POST /agents/:code/policies", async () => {
    const payload = {
      dataObject: "core.customer",
      permission: "read" as const,
    };
    httpMock.post.mockResolvedValue({ data: { id: 9, ...payload } });
    await addAgentPolicy("x", payload);
    expect(httpMock.post).toHaveBeenCalledWith("/agents/x/policies", payload);
  });

  it("updateAgentPolicy PUT /agents/:code/policies/:policyId", async () => {
    httpMock.put.mockResolvedValue({ data: { id: 9, permission: "masked_read" } });
    await updateAgentPolicy("x", 9, { permission: "masked_read" });
    expect(httpMock.put).toHaveBeenCalledWith("/agents/x/policies/9", { permission: "masked_read" });
  });

  it("deleteAgentPolicy DELETE /agents/:code/policies/:policyId", async () => {
    httpMock.delete.mockResolvedValue({ data: undefined });
    await deleteAgentPolicy("x", 9);
    expect(httpMock.delete).toHaveBeenCalledWith("/agents/x/policies/9");
  });
});