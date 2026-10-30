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
    getGraphRelevance,
    listGraphCommunities,
    recomputeGraphCommunities,
    getGraphView,
    getGraphInsights,
    rescanGraphInsights,
    previewCommunityTopic,
    updateCommunityTopic,
} from "../api/wikiGraph";

describe("api/wikiGraph", () => {
    beforeEach(() => { vi.clearAllMocks(); });

    it("getGraphRelevance GET /wiki/graph/relevance（pageA/pageB 入 params）", async () => {
        httpMock.get.mockResolvedValue({ data: { pageA: "p1", pageB: "p2" } });
        const result = await getGraphRelevance("p1", "p2");
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/graph/relevance", {
            params: { pageA: "p1", pageB: "p2" },
        });
        expect(result.pageB).toBe("p2");
    });

    it("listGraphCommunities GET /wiki/graph/communities", async () => {
        httpMock.get.mockResolvedValue({ data: [] });
        await listGraphCommunities();
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/graph/communities");
    });

    it("recomputeGraphCommunities POST /wiki/graph/communities/recompute", async () => {
        httpMock.post.mockResolvedValue({
            data: { communityCount: 3, memberPageCount: 30 },
        });
        const result = await recomputeGraphCommunities();
        expect(httpMock.post).toHaveBeenCalledWith(
            "/wiki/graph/communities/recompute",
        );
        expect(result.communityCount).toBe(3);
    });

    it("getGraphView GET /wiki/graph/view（默认 includeIsolated=false）", async () => {
        httpMock.get.mockResolvedValue({ data: { nodes: [], edges: [] } });
        await getGraphView();
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/graph/view", {
            params: { includeIsolated: false },
        });
    });

    it("getGraphView GET /wiki/graph/view（includeIsolated=true）", async () => {
        httpMock.get.mockResolvedValue({ data: { nodes: [], edges: [] } });
        await getGraphView(true);
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/graph/view", {
            params: { includeIsolated: true },
        });
    });

    it("getGraphInsights GET /wiki/graph/insights", async () => {
        httpMock.get.mockResolvedValue({
            data: {
                surprisingConnections: [],
                knowledgeGaps: [],
                bridgeNodes: [],
                scannedAt: "2026-01-01T00:00:00Z",
            },
        });
        const result = await getGraphInsights();
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/graph/insights");
        expect(result.knowledgeGaps).toEqual([]);
    });

    it("rescanGraphInsights POST /wiki/graph/insights/rescan", async () => {
        httpMock.post.mockResolvedValue({
            data: {
                surprisingCount: 1,
                gapCount: 2,
                bridgeCount: 3,
                explanationAttempts: 4,
                explanationFailures: 0,
            },
        });
        const result = await rescanGraphInsights();
        expect(httpMock.post).toHaveBeenCalledWith("/wiki/graph/insights/rescan");
        expect(result.surprisingCount).toBe(1);
    });

    it("previewCommunityTopic POST /wiki/graph/communities/:key/topic-suggest", async () => {
        httpMock.post.mockResolvedValue({
            data: { topic: "供应链", pageCount: 5, pageTitles: [] },
        });
        const result = await previewCommunityTopic("c1");
        expect(httpMock.post).toHaveBeenCalledWith(
            "/wiki/graph/communities/c1/topic-suggest",
        );
        expect(result.topic).toBe("供应链");
    });

    it("previewCommunityTopic 对 communityKey 做 URL 编码", async () => {
        httpMock.post.mockResolvedValue({ data: { topic: null, pageCount: 0, pageTitles: [] } });
        await previewCommunityTopic("c 1/2");
        expect(httpMock.post).toHaveBeenCalledWith(
            "/wiki/graph/communities/c%201%2F2/topic-suggest",
        );
    });

    it("updateCommunityTopic PATCH /wiki/graph/communities/:key（下发 topic 字段）", async () => {
        httpMock.patch.mockResolvedValue({
            data: { community: { id: 5, topic: "供应链" } },
        });
        const result = await updateCommunityTopic("c1", "供应链");
        expect(httpMock.patch).toHaveBeenCalledWith("/wiki/graph/communities/c1", {
            topic: "供应链",
        });
        expect(result.topic).toBe("供应链");
    });

    it("updateCommunityTopic topic=null 显式清空", async () => {
        httpMock.patch.mockResolvedValue({
            data: { community: { id: 5, topic: null } },
        });
        await updateCommunityTopic("c1", null);
        expect(httpMock.patch).toHaveBeenCalledWith("/wiki/graph/communities/c1", {
            topic: null,
        });
    });

    it("updateCommunityTopic onlyIfSet=true 时不下发 topic 字段（no-op）", async () => {
        httpMock.patch.mockResolvedValue({
            data: { community: { id: 5, topic: "旧主题" } },
        });
        await updateCommunityTopic("c1", "新主题", { onlyIfSet: true });
        expect(httpMock.patch).toHaveBeenCalledWith("/wiki/graph/communities/c1", {});
        const body = httpMock.patch.mock.calls[0][1];
        expect(Object.keys(body)).toEqual([]);
        expect(body).not.toHaveProperty("topic");
    });

    it("updateCommunityTopic 对 communityKey 做 URL 编码", async () => {
        httpMock.patch.mockResolvedValue({
            data: { community: { id: 5, topic: null } },
        });
        await updateCommunityTopic("c 1/2", null);
        expect(httpMock.patch).toHaveBeenCalledWith("/wiki/graph/communities/c%201%2F2", {
            topic: null,
        });
    });
});
