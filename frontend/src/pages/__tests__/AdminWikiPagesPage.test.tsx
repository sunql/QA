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
import type { WikiPage, WikiPageBatchDeleteResult } from "../../types/wikiPages";

const api = vi.hoisted(() => ({
    listWikiPages: vi.fn(),
    createWikiPage: vi.fn(),
    getWikiPage: vi.fn(),
    updateWikiPage: vi.fn(),
    deleteWikiPage: vi.fn(),
    reclassifyWikiPage: vi.fn(),
    batchDeleteWikiPages: vi.fn(),
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

/**
 * 表格正文里的行勾选框。
 *
 * 必须限定在 tbody：表头那个「全选」也是 `input[type=checkbox]`，用
 * `getAllByRole("checkbox")` 会把它算进来，下标就整体错一位。
 */
function rowCheckboxes(): HTMLInputElement[] {
    return Array.from(
        document.querySelectorAll<HTMLInputElement>(
            ".ant-table-tbody input[type='checkbox']",
        ),
    );
}

/** 批量删除结果的最小工厂（只填断言用得到的字段）。 */
function batchResult(overrides: Partial<WikiPageBatchDeleteResult> = {}) {
    return {
        requested: 2,
        deletedPageIds: ["wiki-1", "wiki-2"],
        notFound: [],
        cascade: {
            claims: 3,
            relations: 1,
            suggestions: 0,
            rules: 0,
            workflows: 0,
        },
        ...overrides,
    };
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

/**
 * 批量删除（多选 + 二次确认）。
 *
 * 重点不在「按钮点了会发请求」，而在**破坏性操作的三道闸**：
 * 没选东西不能删、删之前必须确认、删之后必须说清楚「哪些没删掉」。
 */
describe("AdminWikiPagesPage 批量删除", () => {
    /**
     * 必须自己清 mock。
     *
     * `vi.clearAllMocks()` 只注册在上面那个 describe 的 beforeEach 里，而
     * `beforeEach` 不跨 describe 生效 —— 少了这一步，`listWikiPages` 的调用次数会
     * 从上一个用例累加下来，`toHaveBeenCalledTimes(1)` 这类**前置**断言就随用例
     * 顺序/是否被 `-t` 过滤而飘（曾表现为单跑绿、全量跑红）。
     */
    beforeEach(() => {
        vi.clearAllMocks();
    });

    function twoRows() {
        api.listWikiPages.mockResolvedValue({
            rows: [
                makePage({ pageId: "wiki-1", title: "条目一" }),
                makePage({ pageId: "wiki-2", title: "条目二" }),
            ],
            total: 2,
        });
    }

    /**
     * 勾选若干行，并等到「批量删除」可用。
     *
     * 必须等到按钮可用：`userEvent.click` 只是派发事件，React 的 state 更新
     * 是异步的 —— 紧接着点「批量删除」时按钮还是 disabled，antd 会**静默**
     * 吞掉这次点击，弹窗永远不出现（这条件曾在 CI 上表现为随机的
     * 「找不到 确认删除 按钮」）。按钮可用就是「选中状态已落到 React 里」的可
     * 观测信号，比 sleep 可靠。
     */
    async function selectRows(...indexes: number[]): Promise<void> {
        await waitFor(() => {
            expect(rowCheckboxes().length).toBeGreaterThan(Math.max(...indexes));
        });
        for (const index of indexes) {
            await userEvent.click(rowCheckboxes()[index]);
        }
        await waitFor(() => {
            expect(screen.getByRole("button", { name: /批量删除/ })).toBeEnabled();
        });
    }

    /** 打开确认弹窗（不点确认）。 */
    async function openConfirm(): Promise<void> {
        await userEvent.click(screen.getByRole("button", { name: /批量删除/ }));
        await screen.findByText(/将删除 \d+ 条知识条目。/);
    }

    async function confirm(): Promise<void> {
        await openConfirm();
        await userEvent.click(screen.getByRole("button", { name: /确认删除/ }));
    }

    it("keeps the bulk-delete button disabled until a row is selected", async () => {
        twoRows();
        renderPage();

        expect(
            await screen.findByRole("button", { name: /批量删除/ }),
        ).toBeDisabled();

        await selectRows(0);

        expect(screen.getByText("已选 1 条")).toBeInTheDocument();
    });

    it("asks for confirmation and spells out the count and the cascade", async () => {
        twoRows();
        renderPage();

        await selectRows(0, 1);
        await openConfirm();

        // 二次确认：弹窗出现但还没点确认，请求绝不能已经发出去
        expect(api.batchDeleteWikiPages).not.toHaveBeenCalled();
        expect(screen.getByText("将删除 2 条知识条目。")).toBeInTheDocument();
        // 用户点的是「删 2 条知识」，实际连带被清掉的东西必须写在这里
        expect(
            screen.getByText(/事实原子、证据、知识关系、结构化建议与可执行规则/),
        ).toBeInTheDocument();
    });

    it("does nothing when the confirmation is cancelled", async () => {
        twoRows();
        renderPage();

        await selectRows(0);
        await openConfirm();
        await userEvent.click(screen.getByRole("button", { name: /取\s*消/ }));

        expect(api.batchDeleteWikiPages).not.toHaveBeenCalled();
        // 选中集合保留：取消是「再想想」，不是「重选一遍」
        expect(screen.getByText("已选 1 条")).toBeInTheDocument();
    });

    it("sends the selected page ids and refreshes the list on confirm", async () => {
        twoRows();
        api.batchDeleteWikiPages.mockResolvedValue(batchResult());

        renderPage();
        await waitFor(() => expect(api.listWikiPages).toHaveBeenCalledTimes(1));

        await selectRows(0, 1);
        await confirm();

        await waitFor(() => {
            expect(api.batchDeleteWikiPages).toHaveBeenCalledWith([
                "wiki-1",
                "wiki-2",
            ]);
        });
        // 删完必须重新拉列表，否则用户看到的是已经不存在的行
        await waitFor(() => {
            expect(api.listWikiPages).toHaveBeenCalledTimes(2);
        });
    });

    it("reports what was cascade-deleted on full success", async () => {
        twoRows();
        api.batchDeleteWikiPages.mockResolvedValue(batchResult());

        renderPage();
        await selectRows(0, 1);
        await confirm();

        expect(
            await screen.findByText(/已删除 2 条，连带清理事实 3 条、关系 1 条/),
        ).toBeInTheDocument();
        // 全成功就不该吓用户
        expect(screen.queryByText(/未删除/)).not.toBeInTheDocument();
    });

    it("surfaces a warning naming the pages that were not found", async () => {
        twoRows();
        // 后端部分成功：请求 200，但有一条不在库里
        api.batchDeleteWikiPages.mockResolvedValue(
            batchResult({
                requested: 2,
                deletedPageIds: ["wiki-1"],
                notFound: ["wiki-2"],
            }),
        );

        renderPage();
        await selectRows(0, 1);
        await confirm();

        const alert = await screen.findByText(/有 1 条未删除/);
        expect(alert.textContent).toContain("wiki-2");
        // 静默把「有一条没删掉」报成一片绿是最坏的失败方式
        expect(screen.getByText(/已删除 1 条/)).toBeInTheDocument();
    });

    it("keeps the selection and shows a page-level error when the request fails", async () => {
        twoRows();
        api.batchDeleteWikiPages.mockRejectedValue(new Error("network"));

        renderPage();
        await selectRows(0);
        await confirm();

        expect(
            await screen.findByText("批量删除失败，请重试"),
        ).toBeInTheDocument();
        // 选中集合保留，用户可以直接重试而不用重新勾
        expect(screen.getByText("已选 1 条")).toBeInTheDocument();
    });
});

/**
 * 单条删除的二次确认。
 *
 * 删除按钮就在详情抽屉右上角，误点代价是整个条目（连带 claim / 关系 / 产物）。
 * 批量删除早有确认弹窗，这里钉住「单条不再是无确认的一键删除」。
 */
describe("AdminWikiPagesPage 单条删除", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        api.listWikiPages.mockResolvedValue({ rows: [makePage()], total: 1 });
        api.getWikiPage.mockResolvedValue(makePage());
    });

    /** 抽屉 + Popconfirm 是 jsdom 下最重的渲染路径，全量跑（112 个文件抢 CPU）
     *  会从单跑的 ~2s 涨到 6s+，撞穿 vitest 默认 5s 的 testTimeout。断言没变，
     *  只是不再把机器负载当成被测行为。 */
    const FIND_TIMEOUT = 10_000;
    const TEST_TIMEOUT = 20_000;

    async function openDrawer(): Promise<void> {
        renderPage();
        await userEvent.click(await screen.findByText("供应商准入要求"));
        // antd 会在「两个汉字」的 Button 里插一个空格（可访问名是「删 除」），
        // 故锚定边界时必须容错 \s*；否则 /^删除$/ 匹配不到，而放宽成 /删除/
        // 又会同时命中「批量删除」。
        await screen.findByRole(
            "button",
            { name: /^删\s*除$/ },
            { timeout: FIND_TIMEOUT },
        );
    }

    it("does not delete straight away — it asks first", async () => {
        await openDrawer();

        await userEvent.click(screen.getByRole("button", { name: /^删\s*除$/ }));

        // 关键断言：点击后**没有**发请求，只是弹了确认框
        expect(api.deleteWikiPage).not.toHaveBeenCalled();
        expect(
            await screen.findByText(/确认删除「供应商准入要求」？/, undefined, {
                timeout: FIND_TIMEOUT,
            }),
        ).toBeInTheDocument();
    }, TEST_TIMEOUT);

    it("deletes only after the confirmation is accepted", async () => {
        api.deleteWikiPage.mockResolvedValue(undefined);
        await openDrawer();

        await userEvent.click(screen.getByRole("button", { name: /^删\s*除$/ }));
        // Popconfirm 的确认按钮文案复用批量删除的「确认删除」，避免两处同义不同词
        const confirmBtn = await screen.findByRole(
            "button",
            { name: /确认删除/ },
            { timeout: FIND_TIMEOUT },
        );
        await userEvent.click(confirmBtn);

        await waitFor(
            () => {
                expect(api.deleteWikiPage).toHaveBeenCalledWith("wiki-1");
            },
            { timeout: FIND_TIMEOUT },
        );
    }, TEST_TIMEOUT);

    it("sends only one request when 确认删除 is clicked again while in flight", async () => {
        /**
         * 钉住 `onConfirm={handleDelete}`（直传 async 函数）这个写法。
         *
         * antd 的 ActionButton 带 `quitOnNullishReturnValue`：onConfirm 若不返回
         * thenable，它会在调用后**立刻**关闭弹窗并把防重入标志 clickedRef 清回
         * false —— 于是请求在途期间第二次点击会真的再发一个请求。第二次要么
         * 404（首删已提交，getPage 抛 NotFound）弹一句误导性的「删除失败」，
         * 要么撞 StaleDataError 变 500。
         *
         * 这里用一个手动控制落定的 Promise 把「在途」这一瞬固定住：若哪天有人
         * 又把它写回 `() => void handleDelete()`，断言立刻红。
         */
        let release!: () => void;
        api.deleteWikiPage.mockImplementation(
            () =>
                new Promise<void>((resolve) => {
                    release = () => resolve();
                }),
        );

        await openDrawer();
        await userEvent.click(screen.getByRole("button", { name: /^删\s*除$/ }));

        const confirmBtn = await screen.findByRole(
            "button",
            { name: /确认删除/ },
            { timeout: FIND_TIMEOUT },
        );
        await userEvent.click(confirmBtn);
        // 第一个请求仍在途；此时再点一次不能产生第二个请求
        await userEvent.click(confirmBtn);

        expect(api.deleteWikiPage).toHaveBeenCalledTimes(1);

        release();
        await waitFor(
            () => {
                expect(api.deleteWikiPage).toHaveBeenCalledTimes(1);
            },
            { timeout: FIND_TIMEOUT },
        );
    }, TEST_TIMEOUT);
});
