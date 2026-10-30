import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  listAgentTools,
  getAgentTool,
  createAgentTool,
  updateAgentTool,
  deleteAgentTool,
  toggleAgentTool,
} from "../api/agentTools";

describe("api/agentTools", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listAgentTools GET /agent-tools（默认 params 为空对象）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listAgentTools();
    expect(httpMock.get).toHaveBeenCalledWith("/agent-tools", { params: {} });
  });

  it("listAgentTools enabledOnly=true → query 值是字符串 \"true\"", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listAgentTools({ enabledOnly: true });
    expect(httpMock.get).toHaveBeenCalledWith("/agent-tools", {
      params: { enabledOnly: "true" },
    });
  });

  it("listAgentTools enabledOnly=false → falsy 不进 query", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listAgentTools({ enabledOnly: false });
    expect(httpMock.get).toHaveBeenCalledWith("/agent-tools", { params: {} });
  });

  it("getAgentTool GET /agent-tools/:name", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 1, name: "supplier_360" } });
    const result = await getAgentTool("supplier_360");
    expect(httpMock.get).toHaveBeenCalledWith("/agent-tools/supplier_360");
    expect(result.name).toBe("supplier_360");
  });

  it("createAgentTool POST /agent-tools", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 2, name: "new_tool" } });
    const payload = {
      name: "new_tool",
      dataObject: "SUPPLIER",
      dataLayers: ["DIM"],
      inputSchema: {},
      handlerKind: "BUILTIN" as const,
      handlerRef: "supplier_360",
      argExtractorKind: "supplier_key",
    };
    const result = await createAgentTool(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/agent-tools", payload);
    expect(result.id).toBe(2);
  });

  it("updateAgentTool PUT /agent-tools/:name", async () => {
    httpMock.put.mockResolvedValue({ data: { id: 1, name: "supplier_360", version: 2 } });
    await updateAgentTool("supplier_360", { version: 1, enabled: false });
    expect(httpMock.put).toHaveBeenCalledWith("/agent-tools/supplier_360", {
      version: 1,
      enabled: false,
    });
  });

  it("deleteAgentTool DELETE /agent-tools/:name", async () => {
    httpMock.delete.mockResolvedValue({});
    await deleteAgentTool("supplier_360");
    expect(httpMock.delete).toHaveBeenCalledWith("/agent-tools/supplier_360");
  });

  it("toggleAgentTool POST /agent-tools/:name/toggle（body 只带 enabled）", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 1, enabled: false } });
    const result = await toggleAgentTool("supplier_360", false);
    expect(httpMock.post).toHaveBeenCalledWith("/agent-tools/supplier_360/toggle", {
      enabled: false,
    });
    expect(result.enabled).toBe(false);
  });
});
