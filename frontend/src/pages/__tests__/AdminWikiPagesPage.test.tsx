/**
 * AdminWikiPagesPage 测试（feat-wiki-knowledge M7）。
 *
 * 重点不在「表格渲染了」，而在**机制 1 的人机分工**：机器给建议，人确认/改判/
 * 打回。所以断言集中在三处 —— 建议可见、没改动时不能提交、打回发的是 null
 * 而不是「不传字段」。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../../i18n";
import AdminWikiPagesPage from "../AdminWikiPagesPage";
import type { WikiPage } from "../../types/wikiPages";

const api = vi.hoisted(() => ({
    listWikiPages: vi.fn(),
    createWikiPage: vi.fn(),
    getWikiPage: vi.fn(),
    updateWikiPage: vi.fn(),
    deleteWikiPage: vi.fn(),
    reclassifyWikiPage: vi.fn(),
}));

vi.mock("../../api/wikiPages", () => api);

function makePage(overrides: Partial<WikiPage> = {}): WikiPage {
    return {
        id: 1,
        pageId: "wiki-1",
        title: "供应商准入要求",
        content: "注册资本 >= 1000 万",
        dimension: "RULE",
        structureStage: "MARKDOWN",
        autoClassification: { primary: "RULE", confidence: 0.92 },
        status: "DRAFT",
        authorityLevel: null,
        version: "v1.0",
        createdByUserId: null,
        validFrom: null,
        validTo: null,
        createdTime: null,
        updatedTime: null,
        ...overrides,
    };
}

function renderPage() {
    return render(
        <I18nextProvider i18n={i18n}>
            <ConfigProvider locale={zhCN}>
                <AdminWikiPagesPage />
            </ConfigProvider>
        </I18nextProvider>,
    );
}

/**
 * 选中一个 antd Select 的选项。
 *
 * antd 的 Select 由 mousedown 打开（click 不会），且选项、选中项各渲染一份
 * 同名文案 —— 用 role 查询会撞上重复元素，所以在这里一次性封装掉这两个坑。
 */
async function selectOption(
    select: HTMLElement,
    optionText: string,
): Promise<void> {
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

describe("AdminWikiPagesPage", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        api.listWikiPages.mockResolvedValue({ rows: [makePage()], total: 1 });
        api.getWikiPage.mockResolvedValue(makePage());
    });

    it("renders page title and rows from the API", async () => {
        renderPage();

        expect(await screen.findByText("知识条目")).toBeInTheDocument();
        expect(await screen.findByText("供应商准入要求")).toBeInTheDocument();
        // 维度列的 Tag 文案来自 i18n，不是原始枚举值
        expect(await screen.findByText("业务规则")).toBeInTheDocument();
        expect(await screen.findByText("草稿")).toBeInTheDocument();
    });

    it("pages with no dimension render the undetermined placeholder, not a blank cell", async () => {
        api.listWikiPages.mockResolvedValue({
            rows: [makePage({ pageId: "wiki-2", dimension: null })],
            total: 1,
        });

        renderPage();

        expect(await screen.findByText("未判定")).toBeInTheDocument();
    });

    it("opens the detail drawer and shows the model's classification suggestion", async () => {
        renderPage();
        await userEvent.click(await screen.findByText("供应商准入要求"));

        await waitFor(() => {
            expect(api.getWikiPage).toHaveBeenCalledWith("wiki-1");
        });
        expect(
            await screen.findByText(/机器建议：RULE（置信度 0.92）/),
        ).toBeInTheDocument();
    });

    it("keeps 确认分类 disabled until the expert actually changes the value", async () => {
        renderPage();
        await userEvent.click(await screen.findByText("供应商准入要求"));

        const confirmBtn = await screen.findByRole("button", { name: /确认分类/ });
        // 原值未改动 = 没有新信息可反馈，提交它只会往学习反馈里灌噪声
        expect(confirmBtn).toBeDisabled();

        // 改成另一个维度后按钮才可用。按名字取抽屉里那个下拉 —— 顶部还有两个
        // 筛选下拉，靠下标取会点到筛选器上。
        const dimensionSelect = screen.getByRole("combobox", { name: "知识维度" });
        await selectOption(dimensionSelect, "业务流程");

        await waitFor(() => {
            expect(screen.getByRole("button", { name: /确认分类/ })).toBeEnabled();
        });
    });

    it("打回分类 sends an explicit null dimension", async () => {
        api.reclassifyWikiPage.mockResolvedValue({
            page: makePage({ dimension: null }),
            action: "REJECT",
        });

        renderPage();
        await userEvent.click(await screen.findByText("供应商准入要求"));
        await userEvent.click(await screen.findByRole("button", { name: /打回分类/ }));

        await waitFor(() => {
            expect(api.reclassifyWikiPage).toHaveBeenCalledWith("wiki-1", null);
        });
    });

    it("surfaces a page-level alert when classification cannot be recorded", async () => {
        api.reclassifyWikiPage.mockRejectedValue(new Error("boom"));

        renderPage();
        await userEvent.click(await screen.findByText("供应商准入要求"));
        await userEvent.click(await screen.findByRole("button", { name: /打回分类/ }));

        expect(
            await screen.findByText("分类处置失败，请重试"),
        ).toBeInTheDocument();
    });

    it("falls back to an explanatory line when the page was never auto-classified", async () => {
        api.listWikiPages.mockResolvedValue({
            rows: [makePage({ autoClassification: null })],
            total: 1,
        });
        api.getWikiPage.mockResolvedValue(makePage({ autoClassification: null }));

        renderPage();
        await userEvent.click(await screen.findByText("供应商准入要求"));

        expect(
            await screen.findByText(/该条目没有自动分类结论/),
        ).toBeInTheDocument();
    });

    it("keeps the previously loaded rows when a refetch fails", async () => {
        renderPage();
        expect(await screen.findByText("供应商准入要求")).toBeInTheDocument();

        api.listWikiPages.mockRejectedValue(new Error("network"));
        // antd 会在两个汉字之间插一个空格，「刷新」的可访问名其实是「刷 新」
        await userEvent.click(screen.getByRole("button", { name: /刷\s*新/ }));

        // 清空表格会被读成「知识都没了」，比看到过期数据更吓人
        await waitFor(() => {
            expect(api.listWikiPages).toHaveBeenCalledTimes(2);
        });
        expect(screen.getByText("供应商准入要求")).toBeInTheDocument();
    });
});
