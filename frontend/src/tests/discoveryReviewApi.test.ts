import { describe, expect, it, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  decideCandidate,
  decideFinding,
  listCandidates,
  listDiscoveryRuns,
  listFindings,
} from "../api/discoveryReview";

describe("api/discoveryReview", () => {
  beforeEach(() => vi.clearAllMocks());

  it("运行列表带过滤与分页", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });

    await listDiscoveryRuns({ sourceId: 1, status: "SUCCEEDED" });

    expect(httpMock.get).toHaveBeenCalledWith("/ontology-discovery/runs", {
      params: { sourceId: 1, status: "SUCCEEDED", limit: 20, offset: 0 },
    });
  });

  it("候选/问题按 runId 查询", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });

    await listCandidates({ runId: 7 });
    await listFindings({ runId: 7 });

    expect(httpMock.get).toHaveBeenNthCalledWith(1, "/ontology-discovery/candidates", {
      params: { runId: 7, status: undefined, limit: 200, offset: 0 },
    });
    expect(httpMock.get).toHaveBeenNthCalledWith(2, "/ontology-discovery/findings", {
      params: { runId: 7, status: undefined, limit: 200, offset: 0 },
    });
  });

  it("判定走 PATCH（候选/问题各自路径）", async () => {
    httpMock.patch.mockResolvedValue({ data: {} });

    await decideCandidate(11, { status: "ACCEPTED", note: "ok" });
    await decideFinding(3, { status: "RESOLVED" });

    expect(httpMock.patch).toHaveBeenNthCalledWith(
      1,
      "/ontology-discovery/candidates/11/decision",
      { status: "ACCEPTED", note: "ok" },
    );
    expect(httpMock.patch).toHaveBeenNthCalledWith(
      2,
      "/ontology-discovery/findings/3/status",
      { status: "RESOLVED" },
    );
  });
});
