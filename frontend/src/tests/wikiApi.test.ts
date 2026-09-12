/**
 * wiki 四个 API 客户端的契约测试（feat-wiki-knowledge M7）。
 *
 * 这些断言盯的是**请求形状**（路径、query 参数名、body 字段），而不是返回值
 * 搬运 —— 后端路由用了别名与 query 参数，客户端把 `ontologyClassId` 拼成
 * `classId`、或把 DELETE 的参数塞进 body，类型系统一律看不出来，线上表现是
 * 404 / 422 或者**删错行**。所以这里逐条钉住。
 */

import { describe, expect, it, vi, beforeEach } from "vitest";

const httpClient = vi.hoisted(() => ({
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
}));

vi.mock("../api/client", () => ({ httpClient }));

import {
    confirmWikiRelation,
    createWikiPage,
    deleteWikiPage,
    discoverWikiRelations,
    getWikiPage,
    listWikiPages,
    listWikiRelations,
    reclassifyWikiPage,
    rejectWikiRelation,
    updateWikiPage,
} from "../api/wikiPages";
import {
    detectWikiConflicts,
    listWikiConflicts,
    resolveWikiConflict,
} from "../api/wikiConflicts";
import {
    acceptWikiSuggestion,
    generateWikiSuggestions,
    listAllWikiSuggestions,
    listPageSuggestions,
    rejectWikiSuggestion,
} from "../api/wikiSuggestions";
import {
    createDomainMapping,
    deleteDomainMapping,
    getCoverageOverview,
    listCoverageCells,
    listCoverageDomains,
    listDomainMappings,
    refreshCoverage,
} from "../api/wikiCoverage";

/** 让桩返回 `data`，模拟拦截器已解包信封后的结果 */
function resolvesWith(data: unknown) {
    return { data };
}

describe("wikiPages API", () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it("listWikiPages sends dimension/status filters with paging defaults", async () => {
        httpClient.get.mockResolvedValueOnce(resolvesWith({ rows: [], total: 0 }));

        await listWikiPages({ dimension: "RULE", status: "DRAFT" });

        expect(httpClient.get).toHaveBeenCalledWith("/wiki/pages", {
            params: { dimension: "RULE", status: "DRAFT", limit: 20, offset: 0 },
        });
    });

    it("listWikiPages leaves filters undefined so the backend does not filter", async () => {
        httpClient.get.mockResolvedValueOnce(resolvesWith({ rows: [], total: 0 }));

        await listWikiPages({ limit: 50, offset: 100 });

        expect(httpClient.get).toHaveBeenCalledWith("/wiki/pages", {
            params: {
                dimension: undefined,
                status: undefined,
                limit: 50,
                offset: 100,
            },
        });
    });

    it("createWikiPage posts title and content", async () => {
        httpClient.post.mockResolvedValueOnce(resolvesWith({ pageId: "p1" }));

        await createWikiPage({ title: "T", content: "C" });

        expect(httpClient.post).toHaveBeenCalledWith("/wiki/pages", {
            title: "T",
            content: "C",
        });
    });

    it("getWikiPage reads by pageId", async () => {
        httpClient.get.mockResolvedValueOnce(resolvesWith({ pageId: "p1" }));

        await getWikiPage("p1");

        expect(httpClient.get).toHaveBeenCalledWith("/wiki/pages/p1");
    });

    it("updateWikiPage patches by pageId", async () => {
        httpClient.patch.mockResolvedValueOnce(resolvesWith({ pageId: "p1" }));

        await updateWikiPage("p1", { status: "APPROVED" });

        expect(httpClient.patch).toHaveBeenCalledWith("/wiki/pages/p1", {
            status: "APPROVED",
        });
    });

    it("deleteWikiPage deletes by pageId", async () => {
        httpClient.delete.mockResolvedValueOnce(resolvesWith(undefined));

        await deleteWikiPage("p1");

        expect(httpClient.delete).toHaveBeenCalledWith("/wiki/pages/p1");
    });

    it("reclassifyWikiPage always sends the dimension key, even when null", async () => {
        // 「打回分类」就是显式传 null —— 若被优化成省略字段，后端会读成
        // 「调用方写错了」，语义正好相反。
        httpClient.post.mockResolvedValueOnce(resolvesWith({ page: {}, action: null }));

        await reclassifyWikiPage("p1", null);

        expect(httpClient.post).toHaveBeenCalledWith("/wiki/pages/p1/reclassify", {
            dimension: null,
        });
    });

    it("listWikiRelations defaults confirmedOnly to false", async () => {
        httpClient.get.mockResolvedValueOnce(resolvesWith([]));

        await listWikiRelations("p1");

        expect(httpClient.get).toHaveBeenCalledWith("/wiki/pages/p1/relations", {
            params: { confirmedOnly: false },
        });
    });

    it("discoverWikiRelations posts modelId (null when omitted)", async () => {
        httpClient.post.mockResolvedValueOnce(
            resolvesWith({ candidates: [], total: 0, classExtractionStatus: "SKIPPED" }),
        );

        await discoverWikiRelations("p1");

        expect(httpClient.post).toHaveBeenCalledWith(
            "/wiki/pages/p1/relations/discover",
            { modelId: null },
        );
    });

    it("confirm/reject relation hit the relation-scoped routes", async () => {
        httpClient.post.mockResolvedValue(resolvesWith({ id: 7 }));

        await confirmWikiRelation(7);
        await rejectWikiRelation(7);

        expect(httpClient.post).toHaveBeenNthCalledWith(1, "/wiki/relations/7/confirm");
        expect(httpClient.post).toHaveBeenNthCalledWith(2, "/wiki/relations/7/reject");
    });
});

describe("wikiConflicts API", () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it("listWikiConflicts forwards status/severity/conflictType with paging", async () => {
        httpClient.get.mockResolvedValueOnce(resolvesWith({ rows: [], total: 0 }));

        await listWikiConflicts({
            status: "OPEN",
            severity: "HIGH",
            conflictType: "STALENESS",
        });

        expect(httpClient.get).toHaveBeenCalledWith("/wiki/conflicts", {
            params: {
                status: "OPEN",
                severity: "HIGH",
                conflictType: "STALENESS",
                limit: 20,
                offset: 0,
            },
        });
    });

    it("detectWikiConflicts posts to the page-scoped detect route", async () => {
        httpClient.post.mockResolvedValueOnce(
            resolvesWith({ conflicts: [], total: 0, llmStatus: "SKIPPED" }),
        );

        await detectWikiConflicts("p1", 3);

        expect(httpClient.post).toHaveBeenCalledWith(
            "/wiki/pages/p1/conflicts/detect",
            { modelId: 3 },
        );
    });

    it("resolveWikiConflict posts the chosen action", async () => {
        httpClient.post.mockResolvedValueOnce(resolvesWith({ id: 9 }));

        await resolveWikiConflict(9, "IGNORED");

        expect(httpClient.post).toHaveBeenCalledWith("/wiki/conflicts/9/resolve", {
            action: "IGNORED",
        });
    });
});

describe("wikiSuggestions API", () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it("listAllWikiSuggestions hits the cross-page workbench route", async () => {
        httpClient.get.mockResolvedValueOnce(resolvesWith({ rows: [], total: 0 }));

        await listAllWikiSuggestions({ status: "PENDING" });

        expect(httpClient.get).toHaveBeenCalledWith("/wiki/suggestions", {
            params: { status: "PENDING", limit: 20, offset: 0 },
        });
    });

    it("listPageSuggestions hits the page-scoped route (different view)", async () => {
        httpClient.get.mockResolvedValueOnce(resolvesWith({ rows: [], total: 0 }));

        await listPageSuggestions("p1", "ACCEPTED");

        expect(httpClient.get).toHaveBeenCalledWith("/wiki/pages/p1/suggestions", {
            params: { status: "ACCEPTED" },
        });
    });

    it("generateWikiSuggestions posts modelId (null when omitted)", async () => {
        httpClient.post.mockResolvedValueOnce(resolvesWith({ suggestions: [], total: 0 }));

        await generateWikiSuggestions("p1");

        expect(httpClient.post).toHaveBeenCalledWith("/wiki/pages/p1/suggestions", {
            modelId: null,
        });
    });

    it("accept/reject suggestion hit their own routes", async () => {
        httpClient.post.mockResolvedValue(resolvesWith({ id: 4 }));

        await acceptWikiSuggestion(4);
        await rejectWikiSuggestion(4);

        expect(httpClient.post).toHaveBeenNthCalledWith(1, "/wiki/suggestions/4/accept");
        expect(httpClient.post).toHaveBeenNthCalledWith(2, "/wiki/suggestions/4/reject");
    });
});

describe("wikiCoverage API", () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it("listCoverageCells passes filters straight through", async () => {
        httpClient.get.mockResolvedValueOnce(resolvesWith([]));

        await listCoverageCells({ domain: "PROCUREMENT", dimension: "RULE" });

        expect(httpClient.get).toHaveBeenCalledWith("/wiki/coverage", {
            params: { domain: "PROCUREMENT", dimension: "RULE" },
        });
    });

    it("getCoverageOverview defaults includeUnassigned to false", async () => {
        httpClient.get.mockResolvedValueOnce(resolvesWith({}));

        await getCoverageOverview({ gapLimit: 30 });

        expect(httpClient.get).toHaveBeenCalledWith("/wiki/coverage/overview", {
            params: { domain: undefined, includeUnassigned: false, gapLimit: 30 },
        });
    });

    it("refreshCoverage posts with no body", async () => {
        httpClient.post.mockResolvedValueOnce(resolvesWith({ cellCount: 0 }));

        await refreshCoverage();

        expect(httpClient.post).toHaveBeenCalledWith("/wiki/coverage/refresh");
    });

    it("listCoverageDomains unwraps the domains envelope field", async () => {
        httpClient.get.mockResolvedValueOnce(
            resolvesWith({ domains: ["PROCUREMENT", "QUALITY"] }),
        );

        await expect(listCoverageDomains()).resolves.toEqual([
            "PROCUREMENT",
            "QUALITY",
        ]);
        expect(httpClient.get).toHaveBeenCalledWith("/wiki/coverage/domains");
    });

    it("listDomainMappings returns the bare array", async () => {
        httpClient.get.mockResolvedValueOnce(resolvesWith([{ domain: "QUALITY" }]));

        await expect(listDomainMappings()).resolves.toEqual([{ domain: "QUALITY" }]);
        expect(httpClient.get).toHaveBeenCalledWith("/wiki/coverage/mappings");
    });

    it("createDomainMapping posts the pair", async () => {
        httpClient.post.mockResolvedValueOnce(resolvesWith({}));

        await createDomainMapping(12, "logistics");

        expect(httpClient.post).toHaveBeenCalledWith("/wiki/coverage/mappings", {
            ontologyClassId: 12,
            domain: "logistics",
        });
    });

    it("deleteDomainMapping sends params as query, never as a body", async () => {
        // DELETE 带 body 在部分代理/客户端上会被丢掉，而这两个参数就是
        // 「删哪一行」的全部信息 —— 丢了就变成删错行或删不掉。
        httpClient.delete.mockResolvedValueOnce(resolvesWith(undefined));

        await deleteDomainMapping(12, "PROCUREMENT");

        expect(httpClient.delete).toHaveBeenCalledWith("/wiki/coverage/mappings", {
            params: { ontologyClassId: 12, domain: "PROCUREMENT" },
        });
    });
});
