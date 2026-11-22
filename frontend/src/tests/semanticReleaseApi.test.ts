import { describe, expect, it, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  activateSemanticRelease,
  archiveSemanticRelease,
  createSemanticReleaseDraft,
  getSemanticRelease,
  listSemanticReleaseChecks,
  listSemanticReleases,
  rollbackSemanticRelease,
  validateSemanticRelease,
} from "../api/semanticRelease";

describe("api/semanticRelease", () => {
  beforeEach(() => vi.clearAllMocks());

  it("listSemanticReleases 带过滤与分页", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });

    await listSemanticReleases({ status: "DRAFT", domainCode: "PROCUREMENT" });

    expect(httpMock.get).toHaveBeenCalledWith("/semantic/releases", {
      params: {
        status: "DRAFT",
        domainCode: "PROCUREMENT",
        globalOnly: undefined,
        limit: 50,
        offset: 0,
      },
    });
  });

  it("详情与校验结论各自走 GET", async () => {
    httpMock.get.mockResolvedValue({ data: {} as never });

    await getSemanticRelease(3);
    await listSemanticReleaseChecks(3);

    expect(httpMock.get).toHaveBeenNthCalledWith(1, "/semantic/releases/3");
    expect(httpMock.get).toHaveBeenNthCalledWith(2, "/semantic/releases/3/checks", {
      params: { limit: 200, offset: 0 },
    });
  });

  it("写操作走 POST，路径与后端一致", async () => {
    httpMock.post.mockResolvedValue({ data: { release: {}, blockers: [], warnings: [] } });

    await createSemanticReleaseDraft({ version: "v1", domainCode: "PROCUREMENT" });
    await validateSemanticRelease(7);
    await activateSemanticRelease(7);
    await archiveSemanticRelease(7);
    await rollbackSemanticRelease(7, { version: "v1-rb", note: "回滚" });

    const calls = httpMock.post.mock.calls.map(([url]) => url);
    expect(calls).toEqual([
      "/semantic/releases",
      "/semantic/releases/7/validate",
      "/semantic/releases/7/activate",
      "/semantic/releases/7/archive",
      "/semantic/releases/7/rollback",
    ]);
    expect(httpMock.post.mock.calls[4][1]).toEqual({ version: "v1-rb", note: "回滚" });
  });
});
