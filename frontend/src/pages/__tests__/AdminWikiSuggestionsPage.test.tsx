/**
 * AdminWikiSuggestionsPage 测试（feat-wiki-knowledge M7、机制 4）。
 *
 * 这一页是两个「静默失败」的高发地，所以断言围着它们转：
 *  1. 列表只有 ``pageId`` 没有标题 —— 审核人无从判断该不该接受；
 *  2. 接受是不可逆终态，重复处置返回 409，界面必须把它当业务反馈说明白。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../../i18n";
import AdminWikiSuggestionsPage from "../AdminWikiSuggestionsPage";
import type { StructureSuggestion } from "../../types/wikiSuggestions";

const suggestionsApi = vi.hoisted(() => ({
    listAllWikiSuggestions: vi.fn(),
    listPageSuggestions: vi.fn(),
    generateWikiSuggestions: vi.fn(),
    acceptWikiSuggestion: vi.fn(),
    rejectWikiSuggestion: vi.fn(),
}));
const pagesApi = vi.hoisted(() => ({
    listWikiPages: vi.fn(),
    createWikiPage: vi.fn(),
    getWikiPage: vi.fn(),
    updateWikiPage: vi.fn(),
    deleteWikiPage: vi.fn(),
    reclassifyWikiPage: vi.fn(),
    listWikiRelations: vi.fn(),
    discoverWikiRelations: vi.fn(),
    confirmWikiRelation: vi.fn(),
    rejectWikiRelation: vi.fn(),
}));
const importApi = vi.hoisted(() => ({
    listImportModels: vi.fn(),
}));

vi.mock("../../api/wikiSuggestions", () => suggestionsApi);
vi.mock("../../api/wikiPages", () => pagesApi);
vi.mock("../../api/wikiImport", () => importApi);

function makeSuggestion(
    overrides: Partial<StructureSuggestion> = {},
): StructureSuggestion {
    return {
        id: 5,
        pageId: "wiki-1",
        pageTitle: "供应商准入要求",
        suggestedDimension: "RULE",
        extractedStructure: { conditions: [{ field: "注册资本", op: ">=", value: 1000 }] },
        confidence: 0.88,
        status: "PENDING",
        suggestedAt: "2026-09-01T00:00:00Z",
        resolvedAt: null,
        resolvedByUserId: null,
        ...overrides,
    };
}

function renderPage() {
    return render(
        <I18nextProvider i18n={i18n}>
            <ConfigProvider locale={zhCN}>
                <AdminWikiSuggestionsPage />
            </ConfigProvider>
        </I18nextProvider>,
    );
}

describe("AdminWikiSuggestionsPage", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        suggestionsApi.listAllWikiSuggestions.mockResolvedValue({
            rows: [makeSuggestion()],
            total: 1,
        });
    });

    it("shows the joined page title so a reviewer can actually judge the suggestion", async () => {
        renderPage();

        expect(await screen.findByText("供应商准入要求")).toBeInTheDocument();
        expect(await screen.findByText("业务规则")).toBeInTheDocument();
        expect(await screen.findByText("0.88")).toBeInTheDocument();
    });

    it("does not hide an orphan suggestion behind a missing title", async () => {
        suggestionsApi.listAllWikiSuggestions.mockResolvedValue({
            rows: [makeSuggestion({ pageTitle: null, pageId: "wiki-gone" })],
            total: 1,
        });

        renderPage();

        // 一条谁也不处理的待办，比一条标题为空的待办危险得多
        expect(
            await screen.findByText(/条目已不存在：wiki-gone/),
        ).toBeInTheDocument();
    });

    it("defaults to the PENDING filter", async () => {
        renderPage();

        await waitFor(() => {
            expect(suggestionsApi.listAllWikiSuggestions).toHaveBeenCalledWith(
                expect.objectContaining({ status: "PENDING" }),
            );
        });
    });

    it("accepts a suggestion only after an explicit confirmation", async () => {
        suggestionsApi.acceptWikiSuggestion.mockResolvedValue(makeSuggestion());

        renderPage();
        await userEvent.click(await screen.findByRole("button", { name: /接\s*受/ }));
        await userEvent.click(await screen.findByRole("button", { name: /确\s*定/ }));

        await waitFor(() => {
            expect(suggestionsApi.acceptWikiSuggestion).toHaveBeenCalledWith(5);
        });
    });

    it("rejects a suggestion only after an explicit confirmation", async () => {
        suggestionsApi.rejectWikiSuggestion.mockResolvedValue(makeSuggestion());

        renderPage();
        await userEvent.click(await screen.findByRole("button", { name: /拒\s*绝/ }));
        await userEvent.click(await screen.findByRole("button", { name: /确\s*定/ }));

        await waitFor(() => {
            expect(suggestionsApi.rejectWikiSuggestion).toHaveBeenCalledWith(5);
        });
    });

    it("explains a 409 as an irreversible terminal state, not a failure", async () => {
        const conflict = new Error("already resolved") as Error & { status: number };
        conflict.status = 409;
        suggestionsApi.acceptWikiSuggestion.mockRejectedValue(conflict);

        renderPage();
        await userEvent.click(await screen.findByRole("button", { name: /接\s*受/ }));
        await userEvent.click(await screen.findByRole("button", { name: /确\s*定/ }));

        expect(
            await screen.findByText(/这条建议已被处置（终态不可逆）/),
        ).toBeInTheDocument();
    });

    it("offers no accept/reject buttons on already-resolved suggestions", async () => {
        suggestionsApi.listAllWikiSuggestions.mockResolvedValue({
            rows: [makeSuggestion({ status: "ACCEPTED" })],
            total: 1,
        });

        renderPage();

        expect(await screen.findByText("已接受")).toBeInTheDocument();
        expect(
            screen.queryByRole("button", { name: /接\s*受/ }),
        ).not.toBeInTheDocument();
        expect(
            screen.queryByRole("button", { name: /拒\s*绝/ }),
        ).not.toBeInTheDocument();
    });
});
