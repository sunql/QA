import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { getAgentOptions } from "../api/agentOptions";

describe("api/agentOptions", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("getAgentOptions GET /agents/options", async () => {
    httpMock.get.mockResolvedValue({
      data: { domains: ["PROCUREMENT"], layers: ["DIM", "DWD"] },
    });
    const result = await getAgentOptions();
    expect(httpMock.get).toHaveBeenCalledWith("/agents/options");
    expect(result.domains).toContain("PROCUREMENT");
    expect(result.layers).toContain("DWD");
  });
});