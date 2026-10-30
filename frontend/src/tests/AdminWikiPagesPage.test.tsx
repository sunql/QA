/**
 * 知识条目管理页（AdminWikiPagesPage）单测。
 *
 * 覆盖这一页的六条主交互链路：
 *   1. 列表 / 分页 / 刷新 / 维度与状态筛选
 *   2. 语义检索（含向量服务故障降级关键词）与关键词检索
 *   3. 新建条目（表单校验通过 / 校验失败 / 取消）
 *   4. 详情抽屉（标题/正文/权威等级/状态/分类的 blur 即存）
 *   5. 分类处置（确认 / 打回）与单条删除
 *   6. 批量删除（全成功 / 部分未找到 / 失败 / 整页删空回第一页）+ 冲突检测 Tab
 *
 * 断言全部落在**行为**上（点了什么 → 哪个 api 被调用、带了什么参数），
 * 不做「渲染没崩」「文本存在」式的空断言；每条「未调用」都配一条「被调用」的
 * 反向用例，防止断言恒真。
 *
 * antd Select 的两条坑（写这页测试时踩过，记录在此避免后人重踩）：
 *   1. rc-select 会额外渲染一份「a11y 影子列表」：`<div role="option" aria-label="业务对象">OBJECT</div>`，
 *      尺寸 0×0 但仍在可访问性树里。`getByRole("option", {name})` 命中的是它 ——
 *      点它不会触发 onChange。下拉里真正可点的是 `.ant-select-item-option`
 *      （title=文案，**没有** role="option"）。所以统一用 `pickOption()` 按 title 选。
 *   2. antd Button 会给两个汉字之间插空格（2 字按钮）："处置" → "处 置"、"删除" → "删 除"。
 *      按钮查询用 `/^处\s*置$/` 这类锚定正则，别用 `/处置/`（会顺带命中「批量删除」）。
 */

import { describe, it, expect, vi, beforeEach, afterEach, beforeAll, afterAll } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { App, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import AdminWikiPagesPage from "../pages/AdminWikiPagesPage";
import type { WikiCategoryNode, WikiPage } from "../types/wikiPages";
import type { KnowledgeConflict } from "../types/wikiConflicts";
import type { WikiSemanticHit } from "../api/wikiPages";

// =============================================================================
// Mocks
// =============================================================================

// vi.hoisted：vi.mock 工厂被提升到 import 之上，工厂里引用的变量必须先存在。
// 这里的 api 对象必须覆盖 `../api/wikiPages` 的**全部**导出 —— 因为
// WikiClaimsPanel / WikiRelationsPanel 也从同一个模块取函数，缺一个就会在
// 切到对应 Tab 时炸 "x is not a function"。
const api = vi.hoisted(() => ({
  listWikiPages: vi.fn(),
  searchWikiPages: vi.fn(),
  semanticSearchWikiPages: vi.fn(),
  createWikiPage: vi.fn(),
  getWikiPage: vi.fn(),
  updateWikiPage: vi.fn(),
  deleteWikiPage: vi.fn(),
  batchDeleteWikiPages: vi.fn(),
  reclassifyWikiPage: vi.fn(),
  listWikiRelations: vi.fn(),
  discoverWikiRelations: vi.fn(),
  confirmWikiRelation: vi.fn(),
  rejectWikiRelation: vi.fn(),
  listWikiClaims: vi.fn(),
  extractWikiClaims: vi.fn(),
  previewWikiPageClassify: vi.fn(),
  suggestWikiRelations: vi.fn(),
  listWikiCategoryTree: vi.fn(),
  createWikiCategory: vi.fn(),
  updateWikiCategory: vi.fn(),
  deleteWikiCategory: vi.fn(),
}));

const conflictsApi = vi.hoisted(() => ({
  listWikiConflicts: vi.fn(),
  detectWikiConflicts: vi.fn(),
  resolveWikiConflict: vi.fn(),
}));

// WikiClaimsPanel 会拉可用模型（用于「重新抽取」）
const importApi = vi.hoisted(() => ({
  listImportModels: vi.fn(),
}));

vi.mock("../api/wikiPages", () => api);
vi.mock("../api/wikiConflicts", () => conflictsApi);
vi.mock("../api/wikiImport", () => importApi);

// -----------------------------------------------------------------------------
// 未处理 rejection 的收集器
//
// 监听器会接管 Vitest 的全局上报，因此 afterEach 必须对本文件每个用例断言
// 没有未处理 rejection，不能只检查主动触发表单校验失败的用例。
// handleCreate 的 validateFields 已在 try 内早退（不再拒绝）；本守卫防回归 ——
// 若谁把 catch 删掉，这里会让对应用例显式变红，而不是静默吞掉。
// -----------------------------------------------------------------------------
const unhandledRejections: unknown[] = [];
const collectUnhandledRejection = (reason: unknown) => {
  unhandledRejections.push(reason);
};

// =============================================================================
// Fixtures
// =============================================================================

const mockPage: WikiPage = {
  id: 1,
  pageId: "page-001",
  title: "供应商准入规则",
  content: "注册资本 >= 1000 万。",
  dimension: "RULE",
  structureStage: "MARKDOWN",
  autoClassification: { primary: "RULE", confidence: 0.82 },
  status: "DRAFT",
  // 留空以便「权威等级」下拉显示 placeholder（否则选不中新值）
  authorityLevel: null,
  authorityDepartment: null,
  version: "1",
  createdByUserId: null,
  validFrom: null,
  validTo: null,
  createdTime: "2026-08-11T00:00:00Z",
  updatedTime: "2026-08-11T00:00:00Z",
  categoryId: 1,
};

const mockCategoryTree: WikiCategoryNode[] = [
  {
    id: 1,
    parentId: null,
    sortOrder: 1,
    name: "采购",
    description: null,
    pageId: null,
    children: [
      {
        id: 2,
        parentId: 1,
        sortOrder: 1,
        name: "供应商管理",
        description: null,
        pageId: null,
        children: [],
      },
    ],
  },
];

const mockSemanticHit: WikiSemanticHit = {
  pageId: "page-001",
  title: "供应商准入规则",
  status: "DRAFT",
  dimension: "RULE",
  chunkText: "注册资本 >= 1000 万",
  chunkSequence: 0,
  distance: 0.5,
  score: 0.6667,
};

const mockConflict: KnowledgeConflict = {
  id: 7,
  conflictType: "CONTRADICTION",
  pageIds: ["page-001"],
  description: "两条知识说法冲突",
  severity: "HIGH",
  detectedBy: "RULE",
  autoDetectedAt: "2026-08-11T00:00:00Z",
  resolvedAt: null,
  resolutionAction: null,
  resolvedByUserId: null,
};

const mockCascade = {
  claims: 1,
  relations: 2,
  suggestions: 3,
  rules: 4,
  workflows: 5,
};

// =============================================================================
// Helpers
// =============================================================================

function renderPage() {
  // <App> 是必须的：页面用 App.useApp() 取 message（语义检索降级提示、
  // 冲突检测提示都走它）。不在 <App> 上下文里时 antd 返回的是空对象
  // `{}`，调 message.warning 会抛 TypeError，把降级链路整个吞掉。
  return render(
    <ConfigProvider locale={zhCN}>
      <App>
        <MemoryRouter initialEntries={["/admin/wiki/pages"]}>
          <Routes>
            <Route
              path="/admin/wiki/pages"
              element={<AdminWikiPagesPage />}
            />
          </Routes>
        </MemoryRouter>
      </App>
    </ConfigProvider>
  );
}

/**
 * 在**当前打开**的下拉里按文案选中一项。
 *
 * 只认 `.ant-select-item-option`（rc-select 真正可点的项，title=文案），
 * 排除 rc-select 那份 0×0 的 a11y 影子列表（点它不会触发 onChange）。
 */
async function pickOption(label: string): Promise<HTMLElement> {
  return await waitFor(() => {
    const allDropdowns = Array.from(
      document.querySelectorAll<HTMLElement>(".ant-select-dropdown"),
    );
    const open = allDropdowns.filter(
      (d) => !d.classList.contains("ant-select-dropdown-hidden"),
    );
    const scope = open.length > 0 ? open : allDropdowns;
    const hits = scope
      .flatMap((d) =>
        Array.from(d.querySelectorAll<HTMLElement>(".ant-select-item-option")),
      )
      .filter((el) => el.getAttribute("title") === label);
    if (hits.length !== 1) {
      throw new Error(
        `打开的下拉里没有唯一的「${label}」选项（命中 ${hits.length} 个）`,
      );
    }
    return hits[0];
  });
}

/** 打开某条目的详情抽屉，返回抽屉作用域（用于 within 限定查询范围）。 */
async function openDetailDrawer(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => {
    expect(
      screen.getByRole("button", { name: "供应商准入规则" }),
    ).toBeInTheDocument();
  });
  await user.click(screen.getByRole("button", { name: "供应商准入规则" }));
  const drawer = await screen.findByRole("dialog");
  await waitFor(() => {
    expect(within(drawer).getByText(/条目号 page-001/)).toBeInTheDocument();
  });
  return drawer;
}

/**
 * 抽屉里唯一没有 aria-label 的下拉 = 权威等级
 * （知识维度 / 生命周期状态 / 归属分类三个都带 aria-label）。
 */
function authorityLevelCombo(drawer: HTMLElement): HTMLElement {
  const target = within(drawer)
    .getAllByRole("combobox")
    .find((el) => !el.getAttribute("aria-label"));
  if (!target) {
    throw new Error("找不到权威等级下拉");
  }
  return target;
}

beforeAll(() => {
  process.on("unhandledRejection", collectUnhandledRejection);
});

afterAll(() => {
  process.off("unhandledRejection", collectUnhandledRejection);
});

afterEach(async () => {
  // node 在微任务队列清空后才派发 unhandledRejection —— 让出一轮事件循环再断言
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(
    unhandledRejections,
    "本文件用例不得泄漏 unhandled rejection（validateFields / 接口失败的 rejection 都必须有人接管）",
  ).toHaveLength(0);
});

// =============================================================================
// Tests
// =============================================================================

describe("AdminWikiPagesPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    unhandledRejections.length = 0;

    api.listWikiPages.mockResolvedValue({ rows: [mockPage], total: 1 });
    api.searchWikiPages.mockResolvedValue({ rows: [mockPage], total: 1 });
    api.semanticSearchWikiPages.mockResolvedValue([mockSemanticHit]);
    api.createWikiPage.mockResolvedValue(mockPage);
    api.getWikiPage.mockResolvedValue(mockPage);
    api.updateWikiPage.mockResolvedValue(mockPage);
    api.deleteWikiPage.mockResolvedValue(undefined);
    api.batchDeleteWikiPages.mockResolvedValue({
      requested: 1,
      deletedPageIds: ["page-001"],
      notFound: [],
      cascade: mockCascade,
    });
    api.reclassifyWikiPage.mockResolvedValue({
      page: mockPage,
      action: "MODIFY",
    });
    api.listWikiCategoryTree.mockResolvedValue(mockCategoryTree);
    api.listWikiClaims.mockResolvedValue([]);
    api.extractWikiClaims.mockResolvedValue({ status: "OK", claimCount: 0 });
    api.listWikiRelations.mockResolvedValue([]);
    api.confirmWikiRelation.mockResolvedValue({
      id: 1,
      upstreamPageId: "page-001",
      downstreamType: "PAGE",
      downstreamId: "page-002",
      relationType: "REFERS_TO",
      confidence: 0.9,
      autoDetected: true,
      confirmed: true,
      rejectedAt: null,
      createdTime: null,
    });
    api.rejectWikiRelation.mockResolvedValue({
      id: 1,
      upstreamPageId: "page-001",
      downstreamType: "PAGE",
      downstreamId: "page-002",
      relationType: "REFERS_TO",
      confidence: 0.9,
      autoDetected: true,
      confirmed: false,
      rejectedAt: "2026-08-11T00:00:00Z",
      createdTime: null,
    });
    api.discoverWikiRelations.mockResolvedValue({
      total: 0,
      candidates: [],
      classExtractionStatus: "SKIPPED",
    });

    conflictsApi.listWikiConflicts.mockResolvedValue({ rows: [], total: 0 });
    conflictsApi.detectWikiConflicts.mockResolvedValue({
      conflicts: [],
      total: 0,
      llmStatus: "SKIPPED",
    });
    conflictsApi.resolveWikiConflict.mockResolvedValue(undefined);

    importApi.listImportModels.mockResolvedValue([]);
  });

  // ---------------------------------------------------------------------------
  // 列表 / 分页 / 刷新
  // ---------------------------------------------------------------------------

  it("挂载即拉列表：带默认分页参数，并把行渲染成表格", async () => {
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledWith({
        dimension: undefined,
        status: undefined,
        limit: 20,
        offset: 0,
      });
    });

    // 行内容（不只是「渲染没崩」）。限定在行内断言，避免与分页器的页码「1」撞车。
    const row = screen.getByRole("row", { name: /供应商准入规则/ });
    expect(within(row).getByText("page-001")).toBeInTheDocument();
    expect(within(row).getByText("① 文档")).toBeInTheDocument();
    expect(within(row).getByText("业务规则")).toBeInTheDocument();
    expect(within(row).getByText("草稿")).toBeInTheDocument();
    expect(within(row).getByText("1")).toBeInTheDocument();
  });

  it("列表拉取失败时不崩溃、也不误走检索端点", async () => {
    api.listWikiPages.mockRejectedValue(new Error("服务不可用"));
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalled();
    });
    // 失败后不应切到检索端点（关键词/语义检索都不是列表拉取的失败兜底）
    expect(api.searchWikiPages).not.toHaveBeenCalled();
    expect(api.semanticSearchWikiPages).not.toHaveBeenCalled();
    expect(
      screen.getByText(/暂无知识条目，先点「新建」或到「知识导入」批量入库/),
    ).toBeInTheDocument();
  });

  it("翻到第 2 页后按新 offset 重新拉取", async () => {
    api.listWikiPages.mockResolvedValue({ rows: [mockPage], total: 45 });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledWith(
        expect.objectContaining({ offset: 0 }),
      );
    });

    await user.click(screen.getByTitle("2"));

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenLastCalledWith(
        expect.objectContaining({ offset: 20 }),
      );
    });
  });

  it("点「刷新」重新拉一次列表", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledTimes(1);
    });

    await user.click(screen.getByRole("button", { name: /刷\s*新/ }));

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledTimes(2);
    });
  });

  // ---------------------------------------------------------------------------
  // 筛选
  // ---------------------------------------------------------------------------

  it("按维度过滤：重新拉列表并带上选中的维度", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledTimes(1);
    });

    await user.click(screen.getByRole("combobox", { name: "按知识维度过滤" }));
    await user.click(await pickOption("业务流程"));

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenLastCalledWith(
        expect.objectContaining({ dimension: "PROCESS" }),
      );
    });
    // 反向：状态过滤没动过，不应被带上
    expect(api.listWikiPages.mock.calls[api.listWikiPages.mock.calls.length - 1]?.[0].status).toBeUndefined();
  });

  it("按状态过滤：重新拉列表并带上选中的状态", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledTimes(1);
    });

    await user.click(screen.getByRole("combobox", { name: "按状态过滤" }));
    await user.click(await pickOption("待审"));

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: "REVIEW" }),
      );
    });
    // 反向：维度过滤没动过
    expect(api.listWikiPages.mock.calls[api.listWikiPages.mock.calls.length - 1]?.[0].dimension).toBeUndefined();
  });

  // ---------------------------------------------------------------------------
  // 检索
  // ---------------------------------------------------------------------------

  it("语义模式：检索走 semanticSearchWikiPages 并切到语义结果视图", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalled();
    });

    const searchInput = screen.getByLabelText("搜索标题 / 正文");
    await user.type(searchInput, "供应商{enter}");

    await waitFor(() => {
      expect(api.semanticSearchWikiPages).toHaveBeenCalledWith({
        query: "供应商",
        dimension: undefined,
      });
    });
    // 语义命中视图：相似度百分比 + 命中片段
    expect(await screen.findByText("66.7%")).toBeInTheDocument();
    expect(screen.getByText("注册资本 >= 1000 万")).toBeInTheDocument();
    // 反向：语义成功时不走关键词检索
    expect(api.searchWikiPages).not.toHaveBeenCalled();
  });

  it("语义检索失败：提示降级并改走关键词检索", async () => {
    api.semanticSearchWikiPages.mockRejectedValue(new Error("向量服务不可用"));
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalled();
    });

    const searchInput = screen.getByLabelText("搜索标题 / 正文");
    await user.type(searchInput, "供应商{enter}");

    await waitFor(() => {
      expect(api.searchWikiPages).toHaveBeenCalledWith({
        query: "供应商",
        dimension: undefined,
        limit: 20,
        offset: 0,
      });
    });
    expect(
      await screen.findByText("向量检索暂不可用，已降级为关键词检索"),
    ).toBeInTheDocument();
  });

  it("关键词模式：检索走 searchWikiPages，不碰语义端点", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalled();
    });

    await user.click(screen.getByText("关键词"));
    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledTimes(2);
    });

    const searchInput = screen.getByLabelText("搜索标题 / 正文");
    await user.type(searchInput, "供应商{enter}");

    await waitFor(() => {
      expect(api.searchWikiPages).toHaveBeenCalledWith({
        query: "供应商",
        dimension: undefined,
        limit: 20,
        offset: 0,
      });
    });
    // 反向：关键词模式全程不调语义端点
    expect(api.semanticSearchWikiPages).not.toHaveBeenCalled();
  });

  it("检索生效后状态下拉被禁用；未检索时可用", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalled();
    });

    // 反向用例（未检索）：状态下拉可用
    expect(
      screen.getByRole("combobox", { name: "按状态过滤" }),
    ).not.toBeDisabled();

    const searchInput = screen.getByLabelText("搜索标题 / 正文");
    await user.type(searchInput, "供应商{enter}");

    await waitFor(() => {
      expect(
        screen.getByRole("combobox", { name: "按状态过滤" }),
      ).toBeDisabled();
    });
  });

  // ---------------------------------------------------------------------------
  // 新建条目
  // ---------------------------------------------------------------------------

  it("新建条目：表单值原样提交给 createWikiPage，成功后关闭弹窗并刷新列表", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledTimes(1);
    });

    await user.click(screen.getByRole("button", { name: "新建条目" }));

    await user.type(
      await screen.findByRole("textbox", { name: "标题" }),
      "新条目",
    );
    await user.type(
      screen.getByRole("textbox", { name: "正文（Markdown）" }),
      "新正文",
    );
    // 知识维度
    await user.click(screen.getByRole("combobox", { name: "知识维度" }));
    await user.click(await pickOption("业务规则"));
    // 权威等级
    await user.click(screen.getByRole("combobox", { name: "权威等级" }));
    await user.click(await pickOption("L3"));
    // 归属分类（分类树是递归的，二级节点带「— 」缩进前缀）
    await user.click(screen.getByRole("combobox", { name: "归属分类" }));
    await user.click(await pickOption("— 供应商管理"));

    await user.click(screen.getByRole("button", { name: /保\s*存/ }));

    await waitFor(() => {
      expect(api.createWikiPage).toHaveBeenCalledTimes(1);
    });
    const payload = api.createWikiPage.mock.calls[0][0];
    expect(payload.title).toBe("新条目");
    expect(payload.content).toBe("新正文");
    expect(payload.dimension).toBe("RULE");
    expect(payload.authorityLevel).toBe("L3");
    expect(payload.categoryId).toBe(2);

    // 成功后弹窗关闭（destroyOnHidden → 卸载）
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    // 并触发列表刷新
    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledTimes(2);
    });
  });

  it("新建条目：标题必填校验失败时不调用 createWikiPage", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalled();
    });

    await user.click(screen.getByRole("button", { name: "新建条目" }));
    // 只填正文，标题留空 → 校验失败
    await user.type(
      await screen.findByRole("textbox", { name: "正文（Markdown）" }),
      "只有正文",
    );
    await user.click(screen.getByRole("button", { name: /保\s*存/ }));

    expect(await screen.findByText("请输入标题")).toBeInTheDocument();
    expect(api.createWikiPage).not.toHaveBeenCalled();
    // 校验失败被 handleCreate 的 try 接管（字段红字内联渲染）⇒ 不产生 rejection。
    // afterEach 的全局守卫会再兜一遍；这里显式断言，让失败落在用例上。
    expect(unhandledRejections).toHaveLength(0);
  });

  it("新建条目：取消弹窗不调用 createWikiPage", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalled();
    });

    await user.click(screen.getByRole("button", { name: "新建条目" }));
    await screen.findByRole("textbox", { name: "标题" });
    await user.click(screen.getByRole("button", { name: /取\s*消/ }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.createWikiPage).not.toHaveBeenCalled();
    // 反向：没有触发任何表单校验失败 ⇒ 无未处理 rejection
    expect(unhandledRejections).toHaveLength(0);
  });

  // ---------------------------------------------------------------------------
  // 详情抽屉：打开
  // ---------------------------------------------------------------------------

  it("点行标题打开详情抽屉并拉取该条目详情", async () => {
    const user = userEvent.setup();
    renderPage();

    const drawer = await openDetailDrawer(user);

    expect(api.getWikiPage).toHaveBeenCalledWith("page-001");
    // 抽屉里能看到机器分类建议与元信息
    expect(
      within(drawer).getByText("机器建议：RULE（置信度 0.82）"),
    ).toBeInTheDocument();
    expect(within(drawer).getByText(/版本 1/)).toBeInTheDocument();
  });

  it("详情抽屉拉取失败时保持关闭且不报错", async () => {
    api.getWikiPage.mockRejectedValue(new Error("详情接口挂了"));
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "供应商准入规则" }),
      ).toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: "供应商准入规则" }));

    await waitFor(() => {
      expect(api.getWikiPage).toHaveBeenCalledWith("page-001");
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // 反向：拉详情失败不影响列表本身
    expect(api.listWikiPages).toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------------
  // 详情抽屉：字段编辑（blur 即存）
  // ---------------------------------------------------------------------------

  it("详情内改标题并失焦：调用 updateWikiPage 且只带 title", async () => {
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    const titleInput = within(drawer).getByDisplayValue("供应商准入规则");
    await user.clear(titleInput);
    await user.type(titleInput, "新标题");
    await user.tab(); // 失焦触发保存

    await waitFor(() => {
      expect(api.updateWikiPage).toHaveBeenCalledWith("page-001", {
        title: "新标题",
      });
    });
    // 反向：只改标题不该带上 content
    expect(api.updateWikiPage.mock.calls[0][1].content).toBeUndefined();
  });

  it("详情内改正文并点保存：调用 updateWikiPage 且只带 content", async () => {
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    const contentArea = within(drawer).getByDisplayValue("注册资本 >= 1000 万。");
    await user.clear(contentArea);
    await user.type(contentArea, "新正文");
    await user.click(within(drawer).getByRole("button", { name: /保\s*存/ }));

    await waitFor(() => {
      expect(api.updateWikiPage).toHaveBeenCalledWith("page-001", {
        content: "新正文",
      });
    });
    expect(api.updateWikiPage.mock.calls[0][1].title).toBeUndefined();
  });

  it("详情内改权威等级：调用 updateWikiPage 且带 authorityLevel", async () => {
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    await user.click(authorityLevelCombo(drawer));
    await user.click(await pickOption("L3"));

    await waitFor(() => {
      expect(api.updateWikiPage).toHaveBeenCalledWith("page-001", {
        authorityLevel: "L3",
      });
    });
    // 反向：没带 status / categoryId
    expect(api.updateWikiPage.mock.calls[0][1].status).toBeUndefined();
  });

  it("详情内改生命周期状态：调用 updateWikiPage 且带 status", async () => {
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    await user.click(
      within(drawer).getByRole("combobox", { name: "生命周期状态" }),
    );
    await user.click(await pickOption("待审"));

    await waitFor(() => {
      expect(api.updateWikiPage).toHaveBeenCalledWith("page-001", {
        status: "REVIEW",
      });
    });
  });

  it("详情内改归属分类：调用 updateWikiPage 且带 categoryId", async () => {
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    await user.click(
      within(drawer).getByRole("combobox", { name: "归属分类" }),
    );
    await user.click(await pickOption("— 供应商管理"));

    await waitFor(() => {
      expect(api.updateWikiPage).toHaveBeenCalledWith("page-001", {
        categoryId: 2,
      });
    });
  });

  it("详情内改状态失败：页面级 Alert 提示失败且抽屉不关闭", async () => {
    api.updateWikiPage.mockRejectedValue(new Error("状态更新失败"));
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    await user.click(
      within(drawer).getByRole("combobox", { name: "生命周期状态" }),
    );
    await user.click(await pickOption("待审"));

    expect(
      await within(drawer).findByText("状态更新失败，请重试"),
    ).toBeInTheDocument();
    expect(api.updateWikiPage).toHaveBeenCalledWith("page-001", {
      status: "REVIEW",
    });
    // 反向：失败不影响抽屉本身
    expect(within(drawer).getByText(/条目号 page-001/)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // 详情抽屉：分类处置
  // ---------------------------------------------------------------------------

  it("改维度后点「确认分类」：调用 reclassifyWikiPage 并带上新维度", async () => {
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    const confirmBtn = within(drawer).getByRole("button", {
      name: "确认分类",
    });
    // 反向：没动维度时按钮是禁用的
    expect(confirmBtn).toBeDisabled();

    await user.click(
      within(drawer).getByRole("combobox", { name: "知识维度" }),
    );
    await user.click(await pickOption("业务流程"));

    await waitFor(() => {
      expect(confirmBtn).not.toBeDisabled();
    });

    await user.click(confirmBtn);

    await waitFor(() => {
      expect(api.reclassifyWikiPage).toHaveBeenCalledWith("page-001", "PROCESS");
    });
  });

  it("点「打回分类」：调用 reclassifyWikiPage 且维度显式为 null", async () => {
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    await user.click(
      within(drawer).getByRole("button", { name: "打回分类" }),
    );

    await waitFor(() => {
      expect(api.reclassifyWikiPage).toHaveBeenCalledWith("page-001", null);
    });
  });

  it("分类处置失败：页面级 Alert 提示失败", async () => {
    api.reclassifyWikiPage.mockRejectedValue(new Error("分类处置失败"));
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    await user.click(
      within(drawer).getByRole("button", { name: "打回分类" }),
    );

    // 页面级 Alert 与抽屉内 Alert 会同时渲染（父子各一份），用 findAllByText
    expect(
      await screen.findAllByText("分类处置失败，请重试"),
    ).not.toHaveLength(0);
    expect(api.reclassifyWikiPage).toHaveBeenCalledWith("page-001", null);
  });

  // ---------------------------------------------------------------------------
  // 详情抽屉：删除
  // ---------------------------------------------------------------------------

  it("点「删除」并在二次确认后调用 deleteWikiPage 并关闭抽屉", async () => {
    const user = userEvent.setup();
    renderPage();
    await openDetailDrawer(user);

    // 抽屉里的删除按钮文案是「删 除」（antd 两字插空格）；用锚定正则避开「批量删除」
    await user.click(screen.getByRole("button", { name: /^删\s*除$/ }));
    await user.click(await screen.findByRole("button", { name: "确认删除" }));

    await waitFor(() => {
      expect(api.deleteWikiPage).toHaveBeenCalledWith("page-001");
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    // 反向：删除后列表重新拉取
    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledTimes(2);
    });
  });

  it("删除失败：页面级 Alert 提示失败且抽屉保持打开", async () => {
    api.deleteWikiPage.mockRejectedValue(new Error("删除失败"));
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    await user.click(screen.getByRole("button", { name: /^删\s*除$/ }));
    await user.click(await screen.findByRole("button", { name: "确认删除" }));

    expect(await screen.findAllByText("删除失败，请重试")).not.toHaveLength(0);
    expect(api.deleteWikiPage).toHaveBeenCalledWith("page-001");
    // 反向：失败时抽屉不关
    expect(within(drawer).getByText(/条目号 page-001/)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // 批量删除
  // ---------------------------------------------------------------------------

  it("未选中行时「批量删除」禁用；选中后启用并显示已选条数", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "供应商准入规则" }),
      ).toBeInTheDocument();
    });

    const batchBtn = screen.getByRole("button", { name: "批量删除" });
    expect(batchBtn).toBeDisabled();
    expect(screen.queryByText(/已选 1 条/)).not.toBeInTheDocument();

    // 勾选第一行（checkboxes[0] 是表头全选）
    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[1]);

    await waitFor(() => {
      expect(batchBtn).not.toBeDisabled();
    });
    expect(screen.getByText("已选 1 条")).toBeInTheDocument();
  });

  it("批量删除：确认后调用 batchDeleteWikiPages 并显示成功摘要", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "供应商准入规则" }),
      ).toBeInTheDocument();
    });
    await user.click(screen.getAllByRole("checkbox")[1]);
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "批量删除" }),
      ).not.toBeDisabled();
    });

    await user.click(screen.getByRole("button", { name: "批量删除" }));
    expect(await screen.findByText("将删除 1 条知识条目。")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => {
      expect(api.batchDeleteWikiPages).toHaveBeenCalledWith(["page-001"]);
    });
    expect(
      await screen.findByText(
        "已删除 1 条，连带清理事实 1 条、关系 2 条、建议 3 条、规则 4 条、流程 5 条。",
      ),
    ).toBeInTheDocument();
  });

  it("批量删除：有 id 未找到时摘要升级为警告并列出缺失 id", async () => {
    api.batchDeleteWikiPages.mockResolvedValue({
      requested: 2,
      deletedPageIds: ["page-001"],
      notFound: ["page-999"],
      cascade: mockCascade,
    });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "供应商准入规则" }),
      ).toBeInTheDocument();
    });
    await user.click(screen.getAllByRole("checkbox")[1]);
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "批量删除" }),
      ).not.toBeDisabled();
    });

    await user.click(screen.getByRole("button", { name: "批量删除" }));
    await screen.findByText("将删除 1 条知识条目。");
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => {
      expect(api.batchDeleteWikiPages).toHaveBeenCalledWith(["page-001"]);
    });
    expect(
      await screen.findByText(/有 1 条未删除（条目不存在或已被删除）：page-999/),
    ).toBeInTheDocument();
    // 反向：成功摘要仍然存在（不是被警告覆盖掉）
    expect(screen.getByText(/已删除 1 条/)).toBeInTheDocument();
  });

  it("批量删除失败：页面级 Alert 提示失败", async () => {
    api.batchDeleteWikiPages.mockRejectedValue(new Error("批量删除失败"));
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "供应商准入规则" }),
      ).toBeInTheDocument();
    });
    await user.click(screen.getAllByRole("checkbox")[1]);
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "批量删除" }),
      ).not.toBeDisabled();
    });

    await user.click(screen.getByRole("button", { name: "批量删除" }));
    await screen.findByText("将删除 1 条知识条目。");
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    expect(
      await screen.findAllByText("批量删除失败，请重试"),
    ).not.toHaveLength(0);
    expect(api.batchDeleteWikiPages).toHaveBeenCalledWith(["page-001"]);
  });

  it("在第 2 页整页删空后回到第 1 页重新拉取", async () => {
    api.listWikiPages.mockResolvedValue({ rows: [mockPage], total: 45 });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledWith(
        expect.objectContaining({ offset: 0 }),
      );
    });
    await user.click(screen.getByTitle("2"));
    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenLastCalledWith(
        expect.objectContaining({ offset: 20 }),
      );
    });

    await user.click(screen.getAllByRole("checkbox")[1]);
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "批量删除" }),
      ).not.toBeDisabled();
    });
    await user.click(screen.getByRole("button", { name: "批量删除" }));
    await screen.findByText("将删除 1 条知识条目。");
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => {
      expect(api.batchDeleteWikiPages).toHaveBeenCalledWith(["page-001"]);
    });
    // 当前页被删空 ⇒ 回第 1 页（offset 0）
    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenLastCalledWith(
        expect.objectContaining({ offset: 0 }),
      );
    });
  });

  // ---------------------------------------------------------------------------
  // 冲突检测 Tab
  // ---------------------------------------------------------------------------

  it("冲突检测 Tab：拉取冲突列表并渲染冲突行", async () => {
    conflictsApi.listWikiConflicts.mockResolvedValue({
      rows: [mockConflict],
      total: 1,
    });
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    await user.click(within(drawer).getByRole("tab", { name: "冲突检测" }));

    await waitFor(() => {
      expect(conflictsApi.listWikiConflicts).toHaveBeenCalledWith({
        conflictType: undefined,
      });
    });
    expect(await screen.findByText("CONTRADICTION")).toBeInTheDocument();
    expect(screen.getByText("HIGH")).toBeInTheDocument();
    expect(screen.getByText("两条知识说法冲突")).toBeInTheDocument();
    // 反向：处置按钮在未解决时可见
    expect(
      screen.getByRole("button", { name: /^处\s*置$/ }),
    ).toBeInTheDocument();
  });

  it("冲突检测 Tab：无冲突时显示空态", async () => {
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    await user.click(within(drawer).getByRole("tab", { name: "冲突检测" }));

    expect(await screen.findByText("未发现冲突")).toBeInTheDocument();
    expect(conflictsApi.listWikiConflicts).toHaveBeenCalled();
    // 反向：没有冲突就没有「处置」按钮
    expect(screen.queryByRole("button", { name: /^处\s*置$/ })).toBeNull();
  });

  it("点「检测冲突」：调用 detectWikiConflicts 并提示冲突数量", async () => {
    conflictsApi.detectWikiConflicts.mockResolvedValue({
      conflicts: [mockConflict],
      total: 1,
      llmStatus: "SKIPPED",
    });
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    await user.click(within(drawer).getByRole("tab", { name: "冲突检测" }));
    await screen.findByText("未发现冲突");

    await user.click(
      within(drawer).getByRole("button", { name: "检测冲突" }),
    );

    await waitFor(() => {
      expect(conflictsApi.detectWikiConflicts).toHaveBeenCalledWith("page-001");
    });
    expect(await screen.findByText("发现 1 个冲突")).toBeInTheDocument();
  });

  it("点「处置」：调用 resolveWikiConflict 并提示已处置", async () => {
    conflictsApi.listWikiConflicts.mockResolvedValue({
      rows: [mockConflict],
      total: 1,
    });
    const user = userEvent.setup();
    renderPage();
    const drawer = await openDetailDrawer(user);

    await user.click(within(drawer).getByRole("tab", { name: "冲突检测" }));
    await screen.findByText("CONTRADICTION");

    await user.click(screen.getByRole("button", { name: /^处\s*置$/ }));

    await waitFor(() => {
      expect(conflictsApi.resolveWikiConflict).toHaveBeenCalledWith(
        7,
        "RESOLVED",
      );
    });
    expect(await screen.findByText("冲突已处置")).toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // 分类树
  // ---------------------------------------------------------------------------

  it("挂载即拉分类树，分类下拉里能看到带层级前缀的选项", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(api.listWikiCategoryTree).toHaveBeenCalled();
    });

    await user.click(screen.getByRole("button", { name: "新建条目" }));
    await user.click(
      await screen.findByRole("combobox", { name: "归属分类" }),
    );

    const root = await pickOption("采购");
    expect(root).toBeInTheDocument();
    // 二级节点带「— 」缩进前缀（flattenCategoryOptions 的递归分支）
    const child = await pickOption("— 供应商管理");
    expect(child).toBeInTheDocument();
  });

  it("分类树拉取失败不阻塞列表渲染", async () => {
    api.listWikiCategoryTree.mockRejectedValue(new Error("分类树挂了"));
    renderPage();

    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalled();
    });
    expect(
      screen.getByRole("button", { name: "供应商准入规则" }),
    ).toBeInTheDocument();
    expect(api.listWikiCategoryTree).toHaveBeenCalled();
  });
});
