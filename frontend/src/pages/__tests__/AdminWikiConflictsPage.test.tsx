/**
 * AdminWikiConflictsPage 测试（feat-wiki-knowledge M7、机制 3）。
 *
 * 这一页的风险不在渲染，而在**处置动作的语义**：``IGNORED`` 是「系统误报」，
 * 它会进机制 3 的准确率统计。所以断言重点有三：默认只看待处理、不选动作不能
 * 提交、提交的是用户选的那个动作而不是某个默认值。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../../i18n";
import AdminWikiConflictsPage from "../AdminWikiConflictsPage";
import type { KnowledgeConflict } from "../../types/wikiConflicts";

const api = vi.hoisted(() => ({
    listWikiConflicts: vi.fn(),
    resolveWikiConflict: vi.fn(),
}));

vi.mock("../../api/wikiConflicts", () => api);

function makeConflict(overrides: Partial<KnowledgeConflict> = {}): KnowledgeConflict {
    return {
        id: 11,
        conflictType: "CONTRADICTION",
        pageIds: ["wiki-1", "wiki-2"],
        description: "两条知识对注册资本下限的表述不一致",
        severity: "HIGH",
        detectedBy: "LLM",
        autoDetectedAt: "2026-09-01T00:00:00Z",
        resolvedAt: null,
        resolutionAction: null,
        resolvedByUserId: null,
        ...overrides,
    };
}

function renderPage() {
    return render(
        <I18nextProvider i18n={i18n}>
            <ConfigProvider locale={zhCN}>
                <AdminWikiConflictsPage />
            </ConfigProvider>
        </I18nextProvider>,
    );
}

/** antd Select 由 mousedown 打开；选项与选中项同名，只能按 class 取下拉里的那个 */
async function selectOption(select: HTMLElement, optionText: string): Promise<void> {
    fireEvent.mouseDown(select);
    const option = await waitFor(() => {
        const found = Array.from(
            document.querySelectorAll(".ant-select-item-option-content"),
        ).find((el) => el.textContent === optionText);
        if (!found) {
            throw new Error(`option not found: ${optionText}`);
        }
        return found;
    });
    await userEvent.click(option);
}

describe("AdminWikiConflictsPage", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        api.listWikiConflicts.mockResolvedValue({
            rows: [makeConflict()],
            total: 1,
        });
    });

    it("defaults to the OPEN filter — this is a worklist, not an audit log", async () => {
        renderPage();

        await waitFor(() => {
            expect(api.listWikiConflicts).toHaveBeenCalledWith(
                expect.objectContaining({ status: "OPEN" }),
            );
        });
    });

    it("renders i18n labels for type and severity instead of raw enums", async () => {
        renderPage();

        expect(await screen.findByText("相互矛盾")).toBeInTheDocument();
        expect(await screen.findByText("高")).toBeInTheDocument();
        expect(await screen.findByText("模型")).toBeInTheDocument();
        expect(
            await screen.findByText("两条知识对注册资本下限的表述不一致"),
        ).toBeInTheDocument();
    });

    it("does not preselect an outcome — 确定 stays disabled until one is chosen", async () => {
        renderPage();
        await userEvent.click(
            await screen.findByRole("button", { name: /处\s*置/ }),
        );

        const okBtn = await screen.findByRole("button", { name: /确\s*定/ });
        expect(okBtn).toBeDisabled();

        await selectOption(
            screen.getByRole("combobox", { name: "处置" }),
            "已合并",
        );

        await waitFor(() => {
            expect(screen.getByRole("button", { name: /确\s*定/ })).toBeEnabled();
        });
    });

    it("submits exactly the outcome the expert picked", async () => {
        api.resolveWikiConflict.mockResolvedValue(undefined);

        renderPage();
        await userEvent.click(
            await screen.findByRole("button", { name: /处\s*置/ }),
        );
        await selectOption(
            screen.getByRole("combobox", { name: "处置" }),
            "误报",
        );
        await userEvent.click(screen.getByRole("button", { name: /确\s*定/ }));

        await waitFor(() => {
            expect(api.resolveWikiConflict).toHaveBeenCalledWith(11, "IGNORED");
        });
    });

    it("treats a 409 as a predictable outcome, not a crash", async () => {
        const already = new Error("conflict resolved") as Error & { status: number };
        already.status = 409;
        api.resolveWikiConflict.mockRejectedValue(already);

        renderPage();
        await userEvent.click(
            await screen.findByRole("button", { name: /处\s*置/ }),
        );
        await selectOption(
            screen.getByRole("combobox", { name: "处置" }),
            "已修正",
        );
        await userEvent.click(screen.getByRole("button", { name: /确\s*定/ }));

        expect(
            await screen.findByText(/这条冲突已被处置（终态不可逆）/),
        ).toBeInTheDocument();
    });

    it("shows the recorded outcome instead of a resolve button on handled rows", async () => {
        api.listWikiConflicts.mockResolvedValue({
            rows: [
                makeConflict({
                    resolvedAt: "2026-09-02T00:00:00Z",
                    resolutionAction: "IGNORED",
                }),
            ],
            total: 1,
        });

        renderPage();

        expect(await screen.findByText("误报")).toBeInTheDocument();
        expect(
            screen.queryByRole("button", { name: /处\s*置/ }),
        ).not.toBeInTheDocument();
    });

    it("keeps the previously loaded rows when a refetch fails", async () => {
        renderPage();
        expect(await screen.findByText("相互矛盾")).toBeInTheDocument();

        api.listWikiConflicts.mockRejectedValue(new Error("network"));
        await userEvent.click(screen.getByRole("button", { name: /刷\s*新/ }));

        await waitFor(() => {
            expect(api.listWikiConflicts).toHaveBeenCalledTimes(2);
        });
        expect(screen.getByText("相互矛盾")).toBeInTheDocument();
    });
});
