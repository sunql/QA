/**
 * 知识导入向导测试（feat-wiki-knowledge M2）。
 *
 * 覆盖四步主链路：粘贴原文 → 选模型 → 预览编辑草稿 → 入库结果，
 * 以及「模型不可用（503）把用户送回选模步」这条分支。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { I18nextProvider } from "react-i18next";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";

import AdminWikiImportPage from "../pages/AdminWikiImportPage";

// vi.hoisted：vi.mock 被提升到 import 之上，工厂里引用的变量必须先存在
const api = vi.hoisted(() => ({
    listImportModels: vi.fn(),
    previewImport: vi.fn(),
    previewImportFile: vi.fn(),
    executeImport: vi.fn(),
    listImportTasks: vi.fn(),
}));

vi.mock("../api/wikiImport", () => api);

// 极简 i18n init（仅本页用到的 keys），避免依赖全局 i18n 单例的初始化时序
i18next.use(initReactI18next).init({
    lng: "zh-CN",
    fallbackLng: "zh-CN",
    ns: ["translation"],
    defaultNS: "translation",
    resources: {
        "zh-CN": {
            translation: {
                "common.next": "下一步",
                "common.prev": "上一步",
                "wikiImport.title": "知识导入",
                "wikiImport.steps.source": "粘贴原文",
                "wikiImport.steps.model": "选择模型",
                "wikiImport.steps.preview": "预览草稿",
                "wikiImport.steps.confirm": "导入结果",
                "wikiImport.sourceLabel": "原始内容（Markdown，按最浅标题层级切分）",
                "wikiImport.sourcePlaceholder": "粘贴或输入待导入的知识原文",
                "wikiImport.uploadButton": "上传文件",
                "wikiImport.uploadHint": "支持 PDF / Word / Markdown / 纯文本",
                "wikiImport.sourceTypeLabel": "来源类型",
                "wikiImport.sourceRefLabel": "来源出处",
                "wikiImport.sourceRefPlaceholder": "可填文件名或链接",
                "wikiImport.autoClassifyLabel": "自动分类",
                "wikiImport.autoClassifyHint": "开启后对每条知识调用模型判断知识维度",
                "wikiImport.modelLabel": "处理模型",
                "wikiImport.modelPlaceholder": "请选择一个可用的模型",
                "wikiImport.noModels": "暂无可选模型",
                "wikiImport.modelUnusable": "不可用",
                "wikiImport.fallbackModelLabel": "备用模型（可选）",
                "wikiImport.fallbackModelPlaceholder": "主模型失败时降级到它",
                "wikiImport.previewSummary":
                    "已切分出 {count} 条知识草稿（上限 {max} 条），可编辑后再入库。",
                "wikiImport.draftIndex": "第 {index} 条",
                "wikiImport.columns.title": "标题",
                "wikiImport.columns.content": "正文",
                "wikiImport.columns.status": "状态",
                "wikiImport.columns.total": "总条数",
                "wikiImport.columns.success": "成功",
                "wikiImport.columns.skipped": "跳过",
                "wikiImport.columns.failed": "失败",
                "wikiImport.columns.errorMessage": "备注",
                "wikiImport.actions.removeDraft": "移除",
                "wikiImport.actions.repreview": "重新切分",
                "wikiImport.actions.execute": "确认入库",
                "wikiImport.actions.importAnother": "再导入一批",
                "wikiImport.resultMessage": "导入任务已完成，状态：{status}",
                "wikiImport.resultCounts":
                    "共 {total} 条，成功 {success} 条，跳过 {skipped} 条，失败 {failed} 条，花费 ${cost}",
                "wikiImport.tasksTitle": "最近导入任务",
                "wikiImport.noTasks": "暂无导入任务",
                "wikiImport.errors.previewFailed": "切分失败，请检查原文内容",
                "wikiImport.errors.parseFileFailed": "文件解析失败",
                "wikiImport.errors.fileTooLarge": "文件过大",
                "wikiImport.errors.modelUnusable": "所选模型不可用，请重新选择",
                "wikiImport.errors.modelsLoadFailed": "模型列表加载失败",
                "wikiImport.errors.tooManyDrafts":
                    "本次切分出 {count} 条，超过单次上限 {max} 条。",
                "wikiImport.actions.reloadModels": "重新加载",
            },
        },
    },
});

const MODELS = [
    {
        id: 1,
        modelName: "qwen-local",
        provider: "ollama",
        usable: true,
        isActive: true,
    },
    {
        id: 2,
        modelName: "deepseek",
        provider: "openai_compatible_proxy",
        usable: false,
        isActive: true,
    },
];

const DRAFTS = [
    {
        pageId: null,
        title: "供应商准入规则",
        content: "## 供应商准入规则\n\n注册资本 >= 1000 万。",
    },
    {
        pageId: null,
        title: "供应商分级规则",
        content: "## 供应商分级规则\n\n按年度采购额分 A/B/C 级。",
    },
];

const TASK = {
    id: 9,
    taskType: "BULK_IMPORT",
    sourceType: "MARKDOWN",
    sourceRef: null,
    selectedModelId: 1,
    fallbackModelId: null,
    status: "SUCCEEDED",
    pageIds: ["p-1", "p-2"],
    totalPages: 2,
    successPages: 2,
    skippedPages: 0,
    failedPages: 0,
    totalCostUsd: "0.003000",
    errorMessage: null,
    createdByUserId: 1,
    createdTime: "2026-09-11T00:00:00Z",
    finishedTime: "2026-09-11T00:01:00Z",
};

const renderPage = () =>
    render(
        <I18nextProvider i18n={i18next}>
            <ConfigProvider>
                <AdminWikiImportPage />
            </ConfigProvider>
        </I18nextProvider>,
    );

/** 在选模型步打开下拉并选中第一个可用模型 */
async function pickFirstModel(): Promise<void> {
    await waitFor(() => {
        expect(document.querySelector(".ant-select-selector")).toBeTruthy();
    });
    const selector = document.querySelector(".ant-select-selector") as HTMLElement;
    fireEvent.mouseDown(selector);

    const option = await screen.findByText(/qwen-local/, {
        selector: ".ant-select-item-option-content",
    });
    fireEvent.click(option);
}

/**
 * 走到「预览草稿」这一步：粘贴原文 → 下一步 → 选模型 → 下一步。
 *
 * `expectTitle` 是「已到预览步」的凭据——草稿标题/正文落在受控 Input 的 value 里，
 * 不是文本节点，所以用 displayValue 定位。断言可覆盖（超限用例的切分结果不同）。
 */
async function gotoPreview(expectTitle = "供应商准入规则"): Promise<void> {
    fireEvent.change(screen.getByLabelText(/原始内容/), {
        target: { value: "## A\n\n正文" },
    });
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));

    await pickFirstModel();
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));

    await screen.findByDisplayValue(expectTitle);
}

describe("AdminWikiImportPage", () => {
    beforeEach(() => {
        vi.clearAllMocks();
        api.listImportModels.mockResolvedValue(MODELS);
        api.previewImport.mockResolvedValue({ drafts: DRAFTS, total: 2 });
        api.executeImport.mockResolvedValue(TASK);
        api.listImportTasks.mockResolvedValue({ rows: [TASK], total: 1 });
    });

    it("renders the four wizard steps", () => {
        renderPage();
        expect(screen.getByText("粘贴原文")).toBeTruthy();
        expect(screen.getByText("选择模型")).toBeTruthy();
        expect(screen.getByText("预览草稿")).toBeTruthy();
        expect(screen.getByText("导入结果")).toBeTruthy();
    });

    it("loads selectable models on mount", async () => {
        renderPage();
        await waitFor(() => {
            expect(api.listImportModels).toHaveBeenCalledTimes(1);
        });
    });

    it("shows existing import tasks from the ledger", async () => {
        renderPage();
        await waitFor(() => {
            expect(api.listImportTasks).toHaveBeenCalledWith({
                limit: 20,
                offset: 0,
            });
        });
    });

    it("keeps next disabled until source text is entered", () => {
        renderPage();
        const next = screen.getByRole("button", {
            name: /下一步/,
        }) as HTMLButtonElement;
        expect(next.disabled).toBe(true);
    });

    it("blocks next on the model step when auto-classify is on and no model picked", () => {
        renderPage();
        fireEvent.change(screen.getByLabelText(/原始内容/), {
            target: { value: "## A\n\n正文" },
        });
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));

        // 自动分类默认开启 → 未选模型不得进入下一步
        expect(screen.getByText("处理模型")).toBeTruthy();
        const next = screen.getByRole("button", {
            name: /下一步/,
        }) as HTMLButtonElement;
        expect(next.disabled).toBe(true);
    });

    it("previews drafts after source and model are chosen", async () => {
        renderPage();
        await gotoPreview();

        expect(api.previewImport).toHaveBeenCalledWith("## A\n\n正文");
        expect(screen.getByDisplayValue("供应商分级规则")).toBeTruthy();
        expect(screen.getByText(/已切分出/)).toBeTruthy();
    });

    it("executes the import with chosen drafts and model", async () => {
        renderPage();
        await gotoPreview();

        fireEvent.click(screen.getByRole("button", { name: /确认入库/ }));

        await waitFor(() => {
            expect(api.executeImport).toHaveBeenCalledWith({
                drafts: DRAFTS,
                modelId: 1,
                fallbackModelId: null,
                autoClassify: true,
                sourceType: "MARKDOWN",
                sourceRef: null,
            });
        });
        // 结果步展示状态
        await screen.findByText(/导入任务已完成，状态：SUCCEEDED/);
    });

    it("carries the chosen source type and source ref into the payload", async () => {
        renderPage();

        // 来源类型下拉是第 1 步唯一的 Select
        fireEvent.mouseDown(
            document.querySelector(".ant-select-selector") as HTMLElement,
        );
        fireEvent.click(
            await screen.findByText("PDF", {
                selector: ".ant-select-item-option-content",
            }),
        );
        fireEvent.change(screen.getByPlaceholderText(/可填文件名或链接/), {
            target: { value: "供应商管理办法.pdf" },
        });
        fireEvent.change(screen.getByLabelText(/原始内容/), {
            target: { value: "## A\n\n正文" },
        });
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));

        await pickFirstModel();
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
        await screen.findByDisplayValue("供应商准入规则");

        fireEvent.click(screen.getByRole("button", { name: /确认入库/ }));

        await waitFor(() => {
            expect(api.executeImport).toHaveBeenCalledWith(
                expect.objectContaining({
                    sourceType: "PDF",
                    sourceRef: "供应商管理办法.pdf",
                }),
            );
        });
    });

    it("navigates back through the wizard and re-splits on demand", async () => {
        renderPage();
        await gotoPreview();

        // 预览 → 选模
        fireEvent.click(screen.getByRole("button", { name: /上一步/ }));
        expect(screen.getByText("处理模型")).toBeTruthy();

        // 选模 → 原文（原文仍在，可改了重切）
        fireEvent.click(screen.getByRole("button", { name: /上一步/ }));
        const textarea = screen.getByLabelText(/原始内容/) as HTMLTextAreaElement;
        expect(textarea.value).toBe("## A\n\n正文");

        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
        await pickFirstModel();
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
        await screen.findByDisplayValue("供应商准入规则");

        // 重新切分：再调一次 preview，且草稿重新拉取
        const before = api.previewImport.mock.calls.length;
        fireEvent.click(screen.getByRole("button", { name: /重新切分/ }));
        await waitFor(() => {
            expect(api.previewImport.mock.calls.length).toBe(before + 1);
        });
        expect(screen.getByDisplayValue("供应商准入规则")).toBeTruthy();
    });

    it("lets the user remove a draft before executing", async () => {
        renderPage();
        await gotoPreview();

        fireEvent.click(screen.getAllByRole("button", { name: /移\s*除/ })[0]);

        expect(screen.queryByDisplayValue("供应商准入规则")).toBeNull();
        expect(screen.getByDisplayValue("供应商分级规则")).toBeTruthy();
    });

    it("sends the edited title and content, not the originally parsed ones", async () => {
        renderPage();
        await gotoPreview();

        fireEvent.change(screen.getAllByLabelText("标题")[0], {
            target: { value: "改过的标题" },
        });
        fireEvent.change(screen.getAllByLabelText("正文")[0], {
            target: { value: "改过的正文" },
        });
        fireEvent.click(screen.getByRole("button", { name: /确认入库/ }));

        await waitFor(() => {
            expect(api.executeImport).toHaveBeenCalledWith(
                expect.objectContaining({
                    drafts: [
                        {
                            pageId: null,
                            title: "改过的标题",
                            content: "改过的正文",
                        },
                        DRAFTS[1],
                    ],
                }),
            );
        });
    });

    it("disables execute when every draft has been removed", async () => {
        renderPage();
        await gotoPreview();

        // 每次重查：移除后 React 重建了草稿列表，旧节点已脱离文档
        fireEvent.click(screen.getAllByRole("button", { name: /移\s*除/ })[0]);
        fireEvent.click(screen.getAllByRole("button", { name: /移\s*除/ })[0]);
        expect(screen.queryAllByRole("button", { name: /移\s*除/ })).toHaveLength(0);

        const execute = screen.getByRole("button", {
            name: /确认入库/,
        }) as HTMLButtonElement;
        expect(execute.disabled).toBe(true);
    });

    it("hides the model select when auto-classify is turned off", async () => {
        renderPage();
        fireEvent.change(screen.getByLabelText(/原始内容/), {
            target: { value: "## A\n\n正文" },
        });
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));

        fireEvent.click(document.querySelector(".ant-switch") as HTMLElement);

        await waitFor(() => {
            expect(screen.queryByText("处理模型")).toBeNull();
        });
    });

    it("returns to the model step when the model is unusable (503)", async () => {
        // 拦截器产出的形状：message 是后端原文，status 单独挂上
        api.executeImport.mockRejectedValue(
            Object.assign(new Error("模型无可用凭据"), { status: 503 }),
        );

        renderPage();
        await gotoPreview();
        fireEvent.click(screen.getByRole("button", { name: /确认入库/ }));

        // 回到选模步，且缺口提示是可操作的那句（不是后端原文）
        await screen.findByText("处理模型");
        expect(screen.getByText("所选模型不可用，请重新选择")).toBeTruthy();
        expect(screen.queryByText("模型无可用凭据")).toBeNull();
    });

    it("stays on preview and surfaces an alert on non-actionable failures", async () => {
        // 真实拦截器的产出：message 是通用兜底文案，detail 是后端原文
        api.executeImport.mockRejectedValue(
            Object.assign(new Error("请求失败 (HTTP 422)"), {
                status: 422,
                detail: "content 超长",
            }),
        );

        renderPage();
        await gotoPreview();
        fireEvent.click(screen.getByRole("button", { name: /确认入库/ }));

        const alertMsg = await screen.findByText("请求失败 (HTTP 422)");
        // 未跳转：预览步的草稿仍在（用户可改了重试）
        expect(screen.getByDisplayValue("供应商准入规则")).toBeTruthy();
        expect(screen.queryByText("处理模型")).toBeNull();

        // 报错可关闭，不挡后续重试
        const alert = alertMsg.closest(".ant-alert") as HTMLElement;
        fireEvent.click(alert.querySelector(".ant-alert-close-icon") as HTMLElement);
        await waitFor(() => {
            expect(screen.queryByText("请求失败 (HTTP 422)")).toBeNull();
        });
    });

    it("blocks execute and explains when drafts exceed the per-import cap", async () => {
        // 后端 execute 的 drafts 上限（MAX_IMPORT_DRAFTS=200）在发请求前就该挡住：
        // 让它打过去只会换来一条 Pydantic 422，而 detail 是数组、前端拿不到可读原因
        const tooMany = Array.from({ length: 201 }, (_, i) => ({
            pageId: null,
            title: `草稿 ${i}`,
            content: `## 草稿 ${i}\n\n正文`,
        }));
        api.previewImport.mockResolvedValue({ drafts: tooMany, total: 201 });

        renderPage();
        await gotoPreview("草稿 0");

        expect(screen.getByText(/超过单次上限 200 条/)).toBeTruthy();
        const execute = screen.getByRole("button", {
            name: /确认入库/,
        }) as HTMLButtonElement;
        expect(execute.disabled).toBe(true);
        expect(api.executeImport).not.toHaveBeenCalled();

        // 删到不超限即可放行
        fireEvent.click(screen.getAllByRole("button", { name: /移\s*除/ })[0]);
        expect(
            (screen.getByRole("button", {
                name: /确认入库/,
            }) as HTMLButtonElement).disabled,
        ).toBe(false);
    });

    it("offers a retry when the model list fails to load", async () => {
        api.listImportModels.mockRejectedValue(new Error("boom"));

        renderPage();
        fireEvent.change(screen.getByLabelText(/原始内容/), {
            target: { value: "## A\n\n正文" },
        });
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));

        // 拉模型失败时下拉为空 → 自动分类开着则下一步永久禁用，必须给出重试入口
        await screen.findByText("模型列表加载失败");
        const next = screen.getByRole("button", {
            name: /下一步/,
        }) as HTMLButtonElement;
        expect(next.disabled).toBe(true);

        // 重试成功后恢复可用
        api.listImportModels.mockResolvedValue(MODELS);
        fireEvent.click(screen.getByRole("button", { name: /重新加载/ }));

        await waitFor(() => {
            expect(api.listImportModels).toHaveBeenCalledTimes(2);
        });
        await pickFirstModel();
        expect(
            (screen.getByRole("button", {
                name: /下一步/,
            }) as HTMLButtonElement).disabled,
        ).toBe(false);
    });

    it("clears the model-load error once a retry succeeds", async () => {
        api.listImportModels
            .mockRejectedValueOnce(new Error("boom"))
            .mockResolvedValueOnce(MODELS);

        renderPage();
        fireEvent.change(screen.getByLabelText(/原始内容/), {
            target: { value: "## A\n\n正文" },
        });
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
        await screen.findByText("模型列表加载失败");

        fireEvent.click(screen.getByRole("button", { name: /重新加载/ }));

        await waitFor(() => {
            expect(screen.queryByText("模型列表加载失败")).toBeNull();
        });
    });

    it("still renders when the task ledger fails to load", async () => {
        api.listImportTasks.mockRejectedValue(new Error("boom"));

        renderPage();

        // 台账是附加信息，拉取失败应降级为空表而不是整页报错
        await screen.findByText("暂无导入任务");
        expect(screen.getByText("粘贴原文")).toBeTruthy();
    });

    it("resets the wizard after a successful import", async () => {
        renderPage();
        await gotoPreview();
        fireEvent.click(screen.getByRole("button", { name: /确认入库/ }));
        await screen.findByText(/导入任务已完成，状态：SUCCEEDED/);

        fireEvent.click(screen.getByRole("button", { name: /再\s*导入一批/ }));

        // 回到第 1 步且原文清空 → 下一步重新禁用
        const textarea = screen.getByLabelText(/原始内容/) as HTMLTextAreaElement;
        expect(textarea.value).toBe("");
        const next = screen.getByRole("button", {
            name: /下一步/,
        }) as HTMLButtonElement;
        expect(next.disabled).toBe(true);
    });

    it("parses an uploaded file and fills the raw content box", async () => {
        api.previewImportFile.mockResolvedValue({
            text: "## 供应商准入规则\n\n注册资本 >= 1000 万。",
            sourceType: "PDF",
        });

        const { container } = renderPage();

        // antd Upload 的真实入口是一个隐藏的 file input；走它才测到
        // beforeUpload→onChange→originFileObj 这条真链路，而不是直接调 handler
        const input = container.querySelector(
            'input[type="file"]',
        ) as HTMLInputElement;
        const file = new File(["%PDF-1.4"], "供应商管理办法.pdf", {
            type: "application/pdf",
        });
        fireEvent.change(input, { target: { files: [file] } });

        await waitFor(() => {
            expect(api.previewImportFile).toHaveBeenCalledTimes(1);
        });
        // 解析出的文本回填原文框 → 「下一步」解锁（证明文本真的进了 state）
        await waitFor(() => {
            expect(
                (screen.getByLabelText(/原始内容/) as HTMLTextAreaElement).value,
            ).toContain("注册资本 >= 1000 万。");
        });
        expect(
            (screen.getByRole("button", {
                name: /下一步/,
            }) as HTMLButtonElement).disabled,
        ).toBe(false);
    });

    it("carries the server-decided source type into the execute payload", async () => {
        // 来源类型由后端按扩展名判定，不是前端自报 —— 台账溯源要可信
        api.previewImportFile.mockResolvedValue({
            text: "## A\n\n正文",
            sourceType: "WORD",
        });

        const { container } = renderPage();
        fireEvent.change(
            container.querySelector('input[type="file"]') as HTMLInputElement,
            {
                target: {
                    files: [
                        new File(["x"], "制度.docx", {
                            type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        }),
                    ],
                },
            },
        );
        await waitFor(() => expect(api.previewImportFile).toHaveBeenCalled());

        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
        await pickFirstModel();
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
        await screen.findByDisplayValue("供应商准入规则");

        fireEvent.click(screen.getByRole("button", { name: /确认入库/ }));

        await waitFor(() => {
            expect(api.executeImport).toHaveBeenCalledWith(
                expect.objectContaining({
                    sourceType: "WORD",
                    sourceRef: "制度.docx",
                }),
            );
        });
    });

    it("keeps the user on the source step when the file cannot be parsed", async () => {
        api.previewImportFile.mockRejectedValue(new Error("Unsupported file type"));

        const { container } = renderPage();
        fireEvent.change(
            container.querySelector('input[type="file"]') as HTMLInputElement,
            {
                target: {
                    files: [new File(["a,b"], "台账.csv", { type: "text/csv" })],
                },
            },
        );

        // 失败的提示要能行动（换格式），而不是把后端原文（英文异常）糊上来
        await screen.findByText("文件解析失败");
        expect(screen.queryByText("Unsupported file type")).toBeNull();
        // 原文仍为空 → 不得跳步
        expect(
            (screen.getByLabelText(/原始内容/) as HTMLTextAreaElement).value,
        ).toBe("");
        expect(screen.queryByDisplayValue("供应商准入规则")).toBeNull();
    });

    it("tells the user to shrink an oversized file instead of checking its format", async () => {
        // 413 与「解析失败」的处置动作不同：前者换小文件，后者换格式。
        // 都提示「请确认格式」会让用户拿着一个 50 MB 的合法 PDF 反复换扩展名。
        const tooLarge = Object.assign(new Error("Request Entity Too Large"), {
            status: 413,
        });
        api.previewImportFile.mockRejectedValue(tooLarge);

        const { container } = renderPage();
        fireEvent.change(
            container.querySelector('input[type="file"]') as HTMLInputElement,
            {
                target: {
                    files: [
                        new File(["%PDF-1.4"], "大部头制度.pdf", {
                            type: "application/pdf",
                        }),
                    ],
                },
            },
        );

        await screen.findByText("文件过大");
        expect(screen.queryByText("文件解析失败")).toBeNull();
    });

    it("resets the source type to MARKDOWN when the wizard is restarted", async () => {
        // 回归点：上传把 sourceType 置成 PDF 后，若不重置，下一批粘贴的
        // Markdown 会带着「PDF」落进台账，溯源就错了
        api.previewImportFile.mockResolvedValue({
            text: "## A\n\n正文",
            sourceType: "PDF",
        });

        const { container } = renderPage();
        fireEvent.change(
            container.querySelector('input[type="file"]') as HTMLInputElement,
            {
                target: {
                    files: [new File(["x"], "制度.pdf", { type: "application/pdf" })],
                },
            },
        );
        await waitFor(() => expect(api.previewImportFile).toHaveBeenCalled());

        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
        await pickFirstModel();
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
        await screen.findByDisplayValue("供应商准入规则");
        fireEvent.click(screen.getByRole("button", { name: /确认入库/ }));
        await screen.findByText(/导入任务已完成，状态：SUCCEEDED/);

        fireEvent.click(screen.getByRole("button", { name: /再\s*导入一批/ }));

        // 重新粘贴一份 Markdown 再走一遍：来源类型必须回到 MARKDOWN
        fireEvent.change(screen.getByLabelText(/原始内容/), {
            target: { value: "## A\n\n正文" },
        });
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
        await pickFirstModel();
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
        await screen.findByDisplayValue("供应商准入规则");
        fireEvent.click(screen.getByRole("button", { name: /确认入库/ }));

        await waitFor(() => {
            expect(api.executeImport).toHaveBeenLastCalledWith(
                expect.objectContaining({ sourceType: "MARKDOWN" }),
            );
        });
    });

    it("keeps the user on the source step when preview fails", async () => {
        api.previewImport.mockRejectedValue(new Error("boom"));

        renderPage();
        fireEvent.change(screen.getByLabelText(/原始内容/), {
            target: { value: "## A\n\n正文" },
        });
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
        await pickFirstModel();
        fireEvent.click(screen.getByRole("button", { name: /下一步/ }));

        await screen.findByText("切分失败，请检查原文内容");
        // 仍在选模步（草稿未生成，不跳预览）
        expect(screen.queryByDisplayValue("供应商准入规则")).toBeNull();
    });

    it("台账展示跳过数（P1：重复 ≠ 失败）", async () => {
        // Arrange：一条「整批都是重跑」的任务 —— 成功 0 / 跳过 3 / 失败 0。
        // 这条数据里有两个 0（成功列与失败列都渲染 0），单靠「0 出现了」或
        // 「3 出现了」都证明不了 3 落在「跳过」这一列；必须按列头定位到
        // 具体单元格，钉死「跳过=3 / 失败=0」，才防得住两列被写反。
        api.listImportTasks.mockResolvedValue({
            rows: [
                { ...TASK, successPages: 0, skippedPages: 3, failedPages: 0 },
            ],
            total: 1,
        });

        // Act
        renderPage();

        // Assert：按列头文本求列下标，再按行取单元格（成功/失败两列都是 0，
        // 若只用 getByText 会撞「多个 0」，若只查「3 出现过」则两列写反也照样过）。
        const table = await screen.findByRole("table");
        const headers = within(table)
            .getAllByRole("columnheader")
            .map((h) => h.textContent);
        const skippedIdx = headers.indexOf("跳过");
        const failedIdx = headers.indexOf("失败");
        expect(skippedIdx).not.toBe(-1);
        expect(failedIdx).not.toBe(-1);

        const row = within(table).getAllByRole("row")[1];
        const cells = within(row).getAllByRole("cell");
        expect(cells[skippedIdx]).toHaveTextContent("3");
        expect(cells[failedIdx]).toHaveTextContent("0");
    });
});
