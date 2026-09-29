/**
 * AdminWikiCategoriesPage 行为测试（feat-wiki-category）。
 *
 * 重点在「CRUD 三态」的可观测信号：
 *  - 树渲染 + 空态
 *  - 选中 → 右侧详情可见
 *  - 新建 Modal 提交 → 调 API + 自动 refresh
 *  - 编辑 Modal 预填当前值
 *  - 删除要 confirm → 才发请求
 *  - 失败 → Alert 可见（不会静默吞）
 *
 * 模式参考 WikiLinksPage.test.tsx：vi.hoisted mock + vi.mock +
 * I18nextProvider + ConfigProvider 包装。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../../i18n";
import AdminWikiCategoriesPage from "../AdminWikiCategoriesPage";
import type { WikiCategoryNode, WikiPage } from "../../types/wikiPages";

const api = vi.hoisted(() => ({
    listWikiCategoryTree: vi.fn(),
    listWikiPages: vi.fn(),
    createWikiCategory: vi.fn(),
    updateWikiCategory: vi.fn(),
    deleteWikiCategory: vi.fn(),
}));

vi.mock("../../api/wikiPages", () => api);

const SEED_TREE: WikiCategoryNode[] = [
    {
        id: 1, parentId: null, sortOrder: 0, name: "采购管理",
        description: "采购相关知识", pageId: null,
        children: [
            { id: 4, parentId: 1, sortOrder: 0, name: "供应商准入流程",
              description: null, pageId: null, children: [] },
            { id: 5, parentId: 1, sortOrder: 1, name: "采购订单执行",
              description: null, pageId: null, children: [] },
        ],
    },
    {
        id: 2, parentId: null, sortOrder: 1, name: "质量管理",
        description: null, pageId: null,
        children: [
            { id: 6, parentId: 2, sortOrder: 0, name: "IQC 来料检验",
              description: null, pageId: null, children: [] },
        ],
    },
];

const SEED_PAGES: WikiPage[] = [
    { id: 101, pageId: "page-overview-1", title: "采购管理概览",
      content: "", dimension: "PROCESS", structureStage: "MARKDOWN",
      autoClassification: null, status: "EFFECTIVE", authorityLevel: null,
      authorityDepartment: null, version: "v1.0", createdByUserId: null, validFrom: null, validTo: null,
      createdTime: null, updatedTime: null, categoryId: 1 },
];

function renderPage() {
    return render(
        <I18nextProvider i18n={i18n}>
            <ConfigProvider locale={zhCN}>
                <App>
                    <AdminWikiCategoriesPage />
                </App>
            </ConfigProvider>
        </I18nextProvider>,
    );
}

async function selectTreeNode(text: string): Promise<void> {
    await waitFor(() => {
        expect(api.listWikiCategoryTree).toHaveBeenCalled();
    });
    await waitFor(() => {
        const titles = Array.from(document.querySelectorAll(".ant-tree-title"))
            .map((el) => (el.textContent ?? "").trim());
        expect(titles).toContain(text);
    });
    const target = Array.from(document.querySelectorAll(".ant-tree-title"))
        .find((el) => (el.textContent ?? "").trim() === text);
    if (!target) throw new Error(`tree node not found: ${text}`);
    const wrapper = target.closest(".ant-tree-node-content-wrapper") as HTMLElement;
    await userEvent.click(wrapper);
    // 等选中状态落地：右侧详情面板 h3 的文案匹配
    await waitFor(() => {
        expect(
            screen.getByRole("heading", { level: 3, name: new RegExp(text) }),
        ).toBeInTheDocument();
    });
}

describe("AdminWikiCategoriesPage", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        api.listWikiCategoryTree.mockResolvedValue(SEED_TREE);
        api.listWikiPages.mockResolvedValue({ rows: SEED_PAGES, total: 1 });
        api.createWikiCategory.mockImplementation(async (payload) => ({
            id: 99, parentId: payload.parentId ?? null,
            sortOrder: payload.sortOrder ?? 0, name: payload.name,
            description: payload.description ?? null,
            pageId: payload.pageId ?? null, children: [],
        }));
        api.updateWikiCategory.mockImplementation(async (_id, payload) => ({
            id: 1, parentId: payload.parentId ?? null,
            sortOrder: payload.sortOrder ?? 0, name: payload.name,
            description: payload.description ?? null,
            pageId: payload.pageId ?? null, children: [],
        }));
        api.deleteWikiCategory.mockResolvedValue(undefined);
    });

    it("renders title, description, and the category tree", async () => {
        renderPage();

        expect(await screen.findByText("Wiki 分类管理")).toBeInTheDocument();
        expect(
            screen.getByText(/管理 Wiki 页面的分类目录/),
        ).toBeInTheDocument();

        await waitFor(() => {
            const titles = Array.from(document.querySelectorAll(".ant-tree-title"))
                .map((el) => (el.textContent ?? "").trim());
            expect(titles).toContain("采购管理");
            expect(titles).toContain("质量管理");
            expect(titles).toContain("供应商准入流程");
        });
    });

    it("shows the empty state when the tree has no categories", async () => {
        api.listWikiCategoryTree.mockResolvedValue([]);
        renderPage();

        expect(
            await screen.findByText(/暂无分类，点右上角「新建分类」/),
        ).toBeInTheDocument();
    });

    it("selects a tree node and shows its details on the right panel", async () => {
        renderPage();
        await selectTreeNode("采购管理");

        // selectTreeNode 内部已 await 右侧 h3 出现；此处只断言详情字段
        expect(screen.getByText("采购相关知识")).toBeInTheDocument();
        // antd Button 汉字之间会插 U+0020（"编 辑"），用 regex 匹配
        expect(screen.getByRole("button", { name: /编\s*辑/ })).toBeInTheDocument();
        expect(screen.getByRole("button", { name: /删\s*除/ })).toBeInTheDocument();
    });

    it("opens the create modal and submits the form", async () => {
        renderPage();
        await waitFor(() => {
            expect(api.listWikiCategoryTree).toHaveBeenCalled();
        });

        await userEvent.click(
            screen.getByRole("button", { name: /新建分类/ }),
        );

        // Modal 标题 + 字段
        expect(
            screen.getByText("新建分类", { selector: ".ant-modal-title" }),
        ).toBeInTheDocument();
        const nameInput = screen.getByLabelText("分类名称");
        await userEvent.type(nameInput, "测试新分类");

        await userEvent.click(
            screen.getByRole("button", { name: /确\s*定/ }),
        );

        await waitFor(() => {
            expect(api.createWikiCategory).toHaveBeenCalledWith(
                expect.objectContaining({ name: "测试新分类" }),
            );
        });
        // 自动 refresh：调用数从 1 升到 2
        await waitFor(() => {
            expect(api.listWikiCategoryTree).toHaveBeenCalledTimes(2);
        });
    });

    it("opens the edit modal pre-filled with the selected category values", async () => {
        renderPage();
        await selectTreeNode("采购管理");

        await userEvent.click(screen.getByRole("button", { name: /^\s*编\s*辑\s*$/ }));

        // 编辑 Modal 标题
        expect(
            screen.getByText("编辑", { selector: ".ant-modal-title" }),
        ).toBeInTheDocument();

        // 名称预填
        const nameInput = screen.getByLabelText("分类名称") as HTMLInputElement;
        expect(nameInput.value).toBe("采购管理");

        // 父分类选项里**不能**出现自己 + 后代（id=1、4、5）
        // 否则就能把自己挂到自己下面 → 环。打开 Select 看选项 DOM。
        fireEvent.mouseDown(screen.getByRole("combobox", { name: "父分类" }));
        await waitFor(() => {
            const opts = Array.from(
                document.querySelectorAll(".ant-select-item-option-content"),
            ).map((el) => (el.textContent ?? "").trim());
            expect(opts.find((t) => t.endsWith("采购管理"))).toBeUndefined();
            expect(
                opts.find((t) => t.endsWith("供应商准入流程")),
            ).toBeUndefined();
            // 但**允许**选「质量管理」（同级不同根）
            expect(opts.find((t) => t.endsWith("质量管理"))).toBeDefined();
        });
    });

    it("requires confirmation before calling deleteWikiCategory", async () => {
        renderPage();
        await selectTreeNode("采购管理");

        await userEvent.click(screen.getByRole("button", { name: /^\s*删\s*除\s*$/ }));
        // Popconfirm 出现 → 确认前不能调 API
        await waitFor(() => {
            expect(api.deleteWikiCategory).not.toHaveBeenCalled();
        });
        await userEvent.click(
            screen.getByRole("button", { name: /^\s*确\s*定\s*$/ }),
        );

        await waitFor(() => {
            expect(api.deleteWikiCategory).toHaveBeenCalledWith(1);
        });
    });

    it("shows an alert when the initial tree fetch fails", async () => {
        api.listWikiCategoryTree.mockRejectedValue(new Error("boom"));

        renderPage();

        expect(
            await screen.findByText(/加载分类树失败/),
        ).toBeInTheDocument();
    });

    it("surfaces a page-level alert when createWikiCategory rejects", async () => {
        api.createWikiCategory.mockRejectedValue(new Error("conflict"));

        renderPage();
        await waitFor(() => {
            expect(api.listWikiCategoryTree).toHaveBeenCalled();
        });
        await userEvent.click(
            screen.getByRole("button", { name: /新建分类/ }),
        );
        await userEvent.type(screen.getByLabelText("分类名称"), "x");
        await userEvent.click(
            screen.getByRole("button", { name: /确\s*定/ }),
        );

        expect(
            await screen.findByText(/创建失败/),
        ).toBeInTheDocument();
    });
});