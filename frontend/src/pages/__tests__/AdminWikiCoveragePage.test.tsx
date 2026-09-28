/**
 * AdminWikiCoveragePage 测试（feat-wiki-knowledge M7、机制 6）。
 *
 * 这一页的关键区别是**快照语义**：矩阵读的是上一次刷新的结果，打开看板不会
 * 重算。所以「刷新」是显式动作，并且要把刷新结果说清楚（尤其 removedCount ——
 * 那是自愈信号：类被软删、域标注被摘掉后旧格消失）。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../../i18n";
import AdminWikiCoveragePage from "../AdminWikiCoveragePage";
import type { CoverageOverview } from "../../types/wikiCoverage";

const api = vi.hoisted(() => ({
    getCoverageOverview: vi.fn(),
    listCoverageCells: vi.fn(),
    listCoverageDomains: vi.fn(),
    listDomainMappings: vi.fn(),
    createDomainMapping: vi.fn(),
    deleteDomainMapping: vi.fn(),
    refreshCoverage: vi.fn(),
}));
const ontologyApi = vi.hoisted(() => ({
    listClasses: vi.fn(),
}));

vi.mock("../../api/wikiCoverage", () => api);
vi.mock("../../api/ontology", () => ontologyApi);

function makeOverview(overrides: Partial<CoverageOverview> = {}): CoverageOverview {
    return {
        summary: {
            totalCells: 12,
            byStatus: { COMPLETE: 3, PARTIAL: 2, MISSING: 7 },
            unassignedCells: 4,
        },
        gaps: [
            {
                dimension: "RULE",
                ontologyClassId: 7,
                className: "Supplier",
                domain: "PROCUREMENT",
                status: "MISSING",
                pageCount: 0,
                approvedCount: 0,
            },
            {
                dimension: "PROCESS",
                ontologyClassId: 9,
                className: "PurchaseOrder",
                domain: "UNASSIGNED",
                status: "PARTIAL",
                pageCount: 2,
                approvedCount: 1,
            },
        ],
        unlinked: {
            gapType: "UNLINKED",
            pageCount: 5,
            dimensions: { OBJECT: 3, RULE: 2 },
        },
        ...overrides,
    };
}

function renderPage() {
    return render(
        <I18nextProvider i18n={i18n}>
            <ConfigProvider locale={zhCN}>
                <AdminWikiCoveragePage />
            </ConfigProvider>
        </I18nextProvider>,
    );
}

describe("AdminWikiCoveragePage", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        api.getCoverageOverview.mockResolvedValue(makeOverview());
        api.listCoverageDomains.mockResolvedValue(["PROCUREMENT", "QUALITY"]);
        api.listDomainMappings.mockResolvedValue([
            {
                ontologyClassId: 7,
                className: "Supplier",
                domain: "PROCUREMENT",
                createdByUserId: 1,
                createdTime: "2026-09-01T00:00:00Z",
            },
        ]);
    });

    it("renders the summary counters from the overview response", async () => {
        renderPage();

        expect(await screen.findByText("矩阵格数")).toBeInTheDocument();
        expect(await screen.findByText("12")).toBeInTheDocument();
        expect(await screen.findByText("完全缺失")).toBeInTheDocument();
        expect(await screen.findByText("7")).toBeInTheDocument();
        expect(await screen.findByText("未标域格数")).toBeInTheDocument();
    });

    it("renders gaps with i18n labels and flags untagged domains", async () => {
        renderPage();

        // Supplier 同时出现在缺口表与域标注表（同一类两处视角），两处都要有
        expect(await screen.findAllByText("Supplier")).toHaveLength(2);
        expect(await screen.findByText("0 条 / 已审 0 条")).toBeInTheDocument();
        expect(await screen.findByText("2 条 / 已审 1 条")).toBeInTheDocument();
        // UNASSIGNED 是哨兵值，直接显示它用户会以为是个业务域
        expect(await screen.findByText("未标业务域")).toBeInTheDocument();
        expect(await screen.findByText("部分覆盖")).toBeInTheDocument();
    });

    it("surfaces unlinked knowledge — the part the matrix cannot show", async () => {
        renderPage();

        expect(
            await screen.findByText(/有 5 条知识没有挂到任何已确认的业务对象/),
        ).toBeInTheDocument();
    });

    it("hides the unlinked banner when nothing is orphaned", async () => {
        api.getCoverageOverview.mockResolvedValue(
            makeOverview({
                unlinked: { gapType: "UNLINKED", pageCount: 0, dimensions: {} },
            }),
        );

        renderPage();

        await waitFor(() => {
            expect(screen.queryByText(/没有挂到任何已确认的业务对象/)).toBeNull();
        });
    });

    it("refresh is an explicit action and reports what it cleaned up", async () => {
        api.refreshCoverage.mockResolvedValue({
            cellCount: 12,
            classCount: 30,
            unmappedClassCount: 5,
            removedCount: 2,
            refreshedAt: "2026-09-11T00:00:00Z",
        });

        renderPage();
        await userEvent.click(
            await screen.findByRole("button", { name: /刷新覆盖度/ }),
        );

        await waitFor(() => {
            expect(api.refreshCoverage).toHaveBeenCalledTimes(1);
        });
        expect(
            await screen.findByText(/已刷新：12 个格子，覆盖 25\/30 个已标域的本体类，清理 2 个失效格。/),
        ).toBeInTheDocument();
    });

    it("deletes a domain tag using both class and domain as the key", async () => {
        api.deleteDomainMapping.mockResolvedValue(undefined);

        renderPage();
        await userEvent.click(await screen.findByRole("button", { name: /删\s*除/ }));
        await userEvent.click(await screen.findByRole("button", { name: /确\s*定/ }));

        await waitFor(() => {
            expect(api.deleteDomainMapping).toHaveBeenCalledWith(7, "PROCUREMENT");
        });
    });

    it("explains a 404 as an already-removed tag rather than a failure", async () => {
        const gone = new Error("not found") as Error & { status: number };
        gone.status = 404;
        api.deleteDomainMapping.mockRejectedValue(gone);

        renderPage();
        await userEvent.click(await screen.findByRole("button", { name: /删\s*除/ }));
        await userEvent.click(await screen.findByRole("button", { name: /确\s*定/ }));

        expect(
            await screen.findByText(/这条标注已经不在了/),
        ).toBeInTheDocument();
    });
});
