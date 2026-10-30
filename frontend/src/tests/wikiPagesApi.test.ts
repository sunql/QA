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
    listWikiPages,
    searchWikiPages,
    semanticSearchWikiPages,
    createWikiPage,
    getWikiPage,
    updateWikiPage,
    deleteWikiPage,
    batchDeleteWikiPages,
    reclassifyWikiPage,
    listWikiRelations,
    discoverWikiRelations,
    confirmWikiRelation,
    rejectWikiRelation,
    listWikiClaims,
    extractWikiClaims,
    previewWikiPageClassify,
    suggestWikiRelations,
    listWikiCategoryTree,
    createWikiCategory,
    updateWikiCategory,
    deleteWikiCategory,
} from "../api/wikiPages";

describe("api/wikiPages", () => {
    beforeEach(() => { vi.clearAllMocks(); });

    it("listWikiPages GET /wiki/pages（默认 limit=20 offset=0）", async () => {
        httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
        const result = await listWikiPages();
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/pages", {
            params: { dimension: undefined, status: undefined, limit: 20, offset: 0 },
        });
        expect(result.total).toBe(0);
    });

    it("listWikiPages 携带 dimension/status 与自定义分页", async () => {
        httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
        await listWikiPages({
            dimension: "METRIC",
            status: "EFFECTIVE",
            limit: 50,
            offset: 100,
        });
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/pages", {
            params: { dimension: "METRIC", status: "EFFECTIVE", limit: 50, offset: 100 },
        });
    });

    it("searchWikiPages GET /wiki/pages/search（默认 limit=20 offset=0）", async () => {
        httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
        await searchWikiPages({ query: "库存" });
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/pages/search", {
            params: { query: "库存", dimension: undefined, limit: 20, offset: 0 },
        });
    });

    it("searchWikiPages 携带 dimension 与自定义分页", async () => {
        httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
        await searchWikiPages({
            query: "库存",
            dimension: "OBJECT",
            limit: 5,
            offset: 15,
        });
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/pages/search", {
            params: { query: "库存", dimension: "OBJECT", limit: 5, offset: 15 },
        });
    });

    it("semanticSearchWikiPages GET /wiki/pages/semantic-search（默认 topK=10）", async () => {
        httpMock.get.mockResolvedValue({ data: [] });
        await semanticSearchWikiPages({ query: "库存周转" });
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/pages/semantic-search", {
            params: { query: "库存周转", dimension: undefined, topK: 10 },
        });
    });

    it("semanticSearchWikiPages 携带 dimension 与 topK", async () => {
        httpMock.get.mockResolvedValue({ data: [] });
        await semanticSearchWikiPages({
            query: "库存周转",
            dimension: "PROCESS",
            topK: 3,
        });
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/pages/semantic-search", {
            params: { query: "库存周转", dimension: "PROCESS", topK: 3 },
        });
    });

    it("createWikiPage POST /wiki/pages", async () => {
        httpMock.post.mockResolvedValue({ data: { id: "p1" } });
        const payload = { title: "标题", content: "正文" };
        await createWikiPage(payload);
        expect(httpMock.post).toHaveBeenCalledWith("/wiki/pages", payload);
    });

    it("getWikiPage GET /wiki/pages/:pageId", async () => {
        httpMock.get.mockResolvedValue({ data: { id: "p1" } });
        await getWikiPage("p1");
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/pages/p1");
    });

    it("updateWikiPage PATCH /wiki/pages/:pageId", async () => {
        httpMock.patch.mockResolvedValue({ data: { id: "p1" } });
        await updateWikiPage("p1", { title: "新标题" });
        expect(httpMock.patch).toHaveBeenCalledWith("/wiki/pages/p1", {
            title: "新标题",
        });
    });

    it("deleteWikiPage DELETE /wiki/pages/:pageId", async () => {
        httpMock.delete.mockResolvedValue({});
        await deleteWikiPage("p1");
        expect(httpMock.delete).toHaveBeenCalledWith("/wiki/pages/p1");
    });

    it("batchDeleteWikiPages POST /wiki/pages/batch-delete（带 pageIds）", async () => {
        httpMock.post.mockResolvedValue({
            data: {
                requested: 2,
                deletedPageIds: ["p1", "p2"],
                notFound: [],
                cascade: { claims: 0, relations: 0, suggestions: 0, rules: 0, workflows: 0 },
            },
        });
        await batchDeleteWikiPages(["p1", "p2"]);
        expect(httpMock.post).toHaveBeenCalledWith("/wiki/pages/batch-delete", {
            pageIds: ["p1", "p2"],
        });
    });

    it("reclassifyWikiPage POST /wiki/pages/:pageId/reclassify（显式传 dimension）", async () => {
        httpMock.post.mockResolvedValue({ data: { pageId: "p1" } });
        await reclassifyWikiPage("p1", "RULE");
        expect(httpMock.post).toHaveBeenCalledWith("/wiki/pages/p1/reclassify", {
            dimension: "RULE",
        });
    });

    it("reclassifyWikiPage dimension=null 时仍下发 dimension 键（打回分类）", async () => {
        httpMock.post.mockResolvedValue({ data: { pageId: "p1" } });
        await reclassifyWikiPage("p1", null);
        expect(httpMock.post).toHaveBeenCalledWith("/wiki/pages/p1/reclassify", {
            dimension: null,
        });
        const body = httpMock.post.mock.calls[0][1];
        expect(Object.keys(body)).toEqual(["dimension"]);
    });

    it("listWikiRelations GET /wiki/pages/:pageId/relations（默认 confirmedOnly=false）", async () => {
        httpMock.get.mockResolvedValue({ data: [] });
        await listWikiRelations("p1");
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/pages/p1/relations", {
            params: { confirmedOnly: false },
        });
    });

    it("listWikiRelations GET /wiki/pages/:pageId/relations?confirmedOnly=true", async () => {
        httpMock.get.mockResolvedValue({ data: [] });
        await listWikiRelations("p1", true);
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/pages/p1/relations", {
            params: { confirmedOnly: true },
        });
    });

    it("discoverWikiRelations POST /wiki/pages/:pageId/relations/discover（无 modelId 传 null）", async () => {
        httpMock.post.mockResolvedValue({ data: { created: 0 } });
        await discoverWikiRelations("p1");
        expect(httpMock.post).toHaveBeenCalledWith(
            "/wiki/pages/p1/relations/discover",
            { modelId: null },
        );
    });

    it("discoverWikiRelations POST /wiki/pages/:pageId/relations/discover（带 modelId）", async () => {
        httpMock.post.mockResolvedValue({ data: { created: 1 } });
        await discoverWikiRelations("p1", 7);
        expect(httpMock.post).toHaveBeenCalledWith(
            "/wiki/pages/p1/relations/discover",
            { modelId: 7 },
        );
    });

    it("confirmWikiRelation POST /wiki/relations/:relationId/confirm", async () => {
        httpMock.post.mockResolvedValue({ data: { id: 21, confirmed: true } });
        const result = await confirmWikiRelation(21);
        expect(httpMock.post).toHaveBeenCalledWith("/wiki/relations/21/confirm");
        expect(result.confirmed).toBe(true);
    });

    it("rejectWikiRelation POST /wiki/relations/:relationId/reject", async () => {
        httpMock.post.mockResolvedValue({ data: { id: 21, confirmed: false } });
        const result = await rejectWikiRelation(21);
        expect(httpMock.post).toHaveBeenCalledWith("/wiki/relations/21/reject");
        expect(result.confirmed).toBe(false);
    });

    it("listWikiClaims GET /wiki/pages/:pageId/claims", async () => {
        httpMock.get.mockResolvedValue({ data: [] });
        await listWikiClaims("p1");
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/pages/p1/claims");
    });

    it("extractWikiClaims POST /wiki/pages/:pageId/claims/extract（默认 modelId=null force=false）", async () => {
        httpMock.post.mockResolvedValue({ data: { status: "ALREADY_DONE", claimCount: 3 } });
        const result = await extractWikiClaims("p1");
        expect(httpMock.post).toHaveBeenCalledWith(
            "/wiki/pages/p1/claims/extract",
            { modelId: null, force: false },
        );
        expect(result.claimCount).toBe(3);
    });

    it("extractWikiClaims POST /wiki/pages/:pageId/claims/extract（带 modelId/force）", async () => {
        httpMock.post.mockResolvedValue({ data: { status: "DONE", claimCount: 5 } });
        await extractWikiClaims("p1", 7, true);
        expect(httpMock.post).toHaveBeenCalledWith(
            "/wiki/pages/p1/claims/extract",
            { modelId: 7, force: true },
        );
    });

    it("previewWikiPageClassify POST /wiki/pages/:pageId/classify/preview", async () => {
        httpMock.post.mockResolvedValue({ data: { primary: "RULE", confidence: 0.9 } });
        const result = await previewWikiPageClassify("p1");
        expect(httpMock.post).toHaveBeenCalledWith(
            "/wiki/pages/p1/classify/preview",
        );
        expect(result.primary).toBe("RULE");
    });

    it("suggestWikiRelations POST /wiki/pages/:pageId/relations/suggest（modelId 恒为 null）", async () => {
        httpMock.post.mockResolvedValue({ data: { candidates: [], total: 0 } });
        await suggestWikiRelations("p1");
        expect(httpMock.post).toHaveBeenCalledWith(
            "/wiki/pages/p1/relations/suggest",
            { modelId: null },
        );
    });

    it("listWikiCategoryTree GET /wiki/categories/tree", async () => {
        httpMock.get.mockResolvedValue({ data: [] });
        await listWikiCategoryTree();
        expect(httpMock.get).toHaveBeenCalledWith("/wiki/categories/tree");
    });

    it("createWikiCategory POST /wiki/categories", async () => {
        httpMock.post.mockResolvedValue({ data: { id: 3, name: "流程" } });
        const payload = { name: "流程", parentId: null, sortOrder: 1 };
        await createWikiCategory(payload);
        expect(httpMock.post).toHaveBeenCalledWith("/wiki/categories", payload);
    });

    it("updateWikiCategory PATCH /wiki/categories/:categoryId", async () => {
        httpMock.patch.mockResolvedValue({ data: { id: 3, name: "新流程" } });
        await updateWikiCategory(3, { name: "新流程" });
        expect(httpMock.patch).toHaveBeenCalledWith("/wiki/categories/3", {
            name: "新流程",
        });
    });

    it("deleteWikiCategory DELETE /wiki/categories/:categoryId", async () => {
        httpMock.delete.mockResolvedValue({});
        await deleteWikiCategory(3);
        expect(httpMock.delete).toHaveBeenCalledWith("/wiki/categories/3");
    });
});
