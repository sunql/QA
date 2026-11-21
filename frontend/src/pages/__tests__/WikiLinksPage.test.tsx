/**
 * WikiLinksPage 行为测试。
 *
 * 在现有骨架测试（标题 + Tabs + 空态）之上，补齐 6 个交互用例：
 *
 * 1. stub 树节点渲染
 * 2. 选页面后才启用「添加绑定」
 * 3. 打开 Modal + 加载 linkables
 * 4. 提交创建链接 + 自动 refresh
 * 5. 撤销调用 revokeWikiLink + refresh
 * 6. scope=chunk 时显形 chunk_id 字段
 *
 * 模式参考 AdminWikiPagesPage.test.tsx：vi.hoisted mock + vi.mock +
 * I18nextProvider + ConfigProvider 包装。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../../i18n";
import { WikiLinksPage } from "../WikiLinksPage";
import type { WikiLink, WikiLinkableTarget } from "../../types/wikiLink";
import type { WikiCategoryNode, WikiPage } from "../../types/wikiPages";

const api = vi.hoisted(() => ({
  listWikiLinks: vi.fn(),
  createWikiLink: vi.fn(),
  revokeWikiLink: vi.fn(),
  updateWikiLink: vi.fn(),
  listLinkableTargets: vi.fn(),
  listWikiCategoryTree: vi.fn(),
  listWikiPages: vi.fn(),
  searchWikiPages: vi.fn(),
}));

vi.mock("../../api/adminWikiLinks", () => api);
vi.mock("../../api/wikiPages", () => ({
  listWikiCategoryTree: api.listWikiCategoryTree,
  listWikiPages: api.listWikiPages,
  searchWikiPages: api.searchWikiPages,
  createWikiCategory: vi.fn(),
  updateWikiCategory: vi.fn(),
  deleteWikiCategory: vi.fn(),
}));

const SEED_TREE: WikiCategoryNode[] = [
  {
    id: 1, parentId: null, sortOrder: 0, name: "采购管理",
    description: null, pageId: null,
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
  {
    id: 3, parentId: null, sortOrder: 2, name: "仓储物流",
    description: null, pageId: null,
    children: [
      { id: 7, parentId: 3, sortOrder: 0, name: "入库作业",
        description: null, pageId: null, children: [] },
      { id: 8, parentId: 3, sortOrder: 1, name: "出库配送",
        description: null, pageId: null, children: [] },
    ],
  },
];

const SEED_PAGES: WikiPage[] = [
  { id: 101, pageId: "page-001", title: "采购管理",
    content: "", dimension: "PROCESS", structureStage: "MARKDOWN",
    autoClassification: null, status: "EFFECTIVE", authorityLevel: null,
    authorityDepartment: null, version: "v1.0", createdByUserId: null, validFrom: null, validTo: null,
    createdTime: null, updatedTime: null, categoryId: 1 },
  { id: 102, pageId: "page-001-01", title: "供应商准入流程",
    content: "", dimension: "PROCESS", structureStage: "MARKDOWN",
    autoClassification: null, status: "EFFECTIVE", authorityLevel: null,
    authorityDepartment: null, version: "v1.0", createdByUserId: null, validFrom: null, validTo: null,
    createdTime: null, updatedTime: null, categoryId: 4 },
  { id: 103, pageId: "page-001-02", title: "采购订单执行",
    content: "", dimension: "PROCESS", structureStage: "MARKDOWN",
    autoClassification: null, status: "EFFECTIVE", authorityLevel: null,
    authorityDepartment: null, version: "v1.0", createdByUserId: null, validFrom: null, validTo: null,
    createdTime: null, updatedTime: null, categoryId: 5 },
  { id: 104, pageId: "page-002", title: "质量管理",
    content: "", dimension: "PROCESS", structureStage: "MARKDOWN",
    autoClassification: null, status: "EFFECTIVE", authorityLevel: null,
    authorityDepartment: null, version: "v1.0", createdByUserId: null, validFrom: null, validTo: null,
    createdTime: null, updatedTime: null, categoryId: 2 },
  { id: 105, pageId: "page-002-01", title: "IQC 来料检验",
    content: "", dimension: "PROCESS", structureStage: "MARKDOWN",
    autoClassification: null, status: "EFFECTIVE", authorityLevel: null,
    authorityDepartment: null, version: "v1.0", createdByUserId: null, validFrom: null, validTo: null,
    createdTime: null, updatedTime: null, categoryId: 6 },
  { id: 106, pageId: "page-003-01", title: "入库作业",
    content: "", dimension: "PROCESS", structureStage: "MARKDOWN",
    autoClassification: null, status: "EFFECTIVE", authorityLevel: null,
    authorityDepartment: null, version: "v1.0", createdByUserId: null, validFrom: null, validTo: null,
    createdTime: null, updatedTime: null, categoryId: 7 },
  { id: 107, pageId: "page-003-02", title: "出库配送",
    content: "", dimension: "PROCESS", structureStage: "MARKDOWN",
    autoClassification: null, status: "EFFECTIVE", authorityLevel: null,
    authorityDepartment: null, version: "v1.0", createdByUserId: null, validFrom: null, validTo: null,
    createdTime: null, updatedTime: null, categoryId: 8 },
];

function makeLink(overrides: Partial<WikiLink> = {}): WikiLink {
  return {
    id: 1,
    page_id: "page-001",
    chunk_id: null,
    ontology_type: "class",
    ontology_id: 42,
    ontology_name: null,
    ontology_alias: null,
    weight: 0.8,
    note: null,
    created_by: 1,
    revoked_time: null,
    ...overrides,
  };
}

function makeLinkable(
  overrides: Partial<WikiLinkableTarget> = {},
): WikiLinkableTarget {
  return {
    id: 42,
    type: "class",
    name: "供应商",
    alias: "Supplier",
    description: null,
    ...overrides,
  };
}

function renderPage() {
    const result = render(
        <I18nextProvider i18n={i18n}>
            <ConfigProvider locale={zhCN}>
                <WikiLinksPage />
            </ConfigProvider>
        </I18nextProvider>,
    );
    return result;
}

/** 展开 antd Tree 的所有 switcher（递归），让叶子 page 进 DOM。
 *  实际已切到 expandedKeys controlled 模式（treeData 加载后 setExpandedKeys），
 *  不再需要手动展开 switcher；保留 helper 以备未来回归。
 */

/**
 * antd Select 由 mousedown 打开，选项与选中项各自渲染一份同名文案，
 * 用 role 查询会撞上重复元素。沿用 AdminWikiPagesPage 的封装。
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

/** 点击 antd Tree 的某个节点（按显示文案定位），并等 React state 落到 UI 层。
 *
 *  treeNode click → onSelect → setSelectedPageId → 「+ 添加绑定」按钮启用。
 *  锚定按钮启用比锚定 listWikiLinks 的具体参数可靠：前者是 UI 信号，与
 *  mock 实现的内部细节解耦；后者在测试间有遗留 promise / mock 状态时偶发
 *  失败（曾表现为 1st spy call 是 undefined 而非 "page-001"）。
 */
async function clickTreeNode(text: string): Promise<void> {
  // 等树数据加载（首次 useEffect 异步）
  await waitFor(() => {
    expect(api.listWikiCategoryTree).toHaveBeenCalled();
  });
  await waitFor(() => {
    const titles = Array.from(document.querySelectorAll(".ant-tree-title"));
    expect(titles.length).toBeGreaterThan(0);
  }, { timeout: 3000 });
  const titles = Array.from(document.querySelectorAll(".ant-tree-title"));
  const target = titles.find((el) => (el.textContent ?? "").trim() === text);
  if (!target) {
    const all = titles.map((el) => (el.textContent ?? "").trim());
    throw new Error(`tree node not found: ${text}; available: ${JSON.stringify(all)}`);
  }
  const wrapper = target.closest(".ant-tree-node-content-wrapper") as HTMLElement;
  await userEvent.click(wrapper);
  await waitFor(() => {
    expect(
      screen.getByRole("button", { name: /\+\s*添加绑定/ }),
    ).toBeEnabled();
  });
}

describe("WikiLinksPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listWikiLinks.mockResolvedValue([]);
    api.listLinkableTargets.mockResolvedValue([]);
    api.listWikiCategoryTree.mockResolvedValue(SEED_TREE);
    api.listWikiPages.mockResolvedValue({ rows: SEED_PAGES, total: SEED_PAGES.length });
    api.searchWikiPages.mockResolvedValue({ rows: [], total: 0 });
  });

  it("renders title and empty state", () => {
    renderPage();
    // i18n resolved value (default zh-CN) — use regex to avoid exact-match issues
    expect(
      screen.getByText(/Wiki.*Ontology.*链接管理/),
    ).toBeInTheDocument();
    expect(screen.getByText(/请先选择左侧 Wiki 页面/)).toBeInTheDocument();
  });

  it("renders class and property tabs", async () => {
    // 标签走 i18n（与本体管理同词）：zh 下显示「类」「属性」
    await i18n.changeLanguage("zh-CN");
    renderPage();
    expect(await screen.findByRole("tab", { name: "类" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "属性" })).toBeInTheDocument();
  });
});

describe("WikiLinksPage 交互", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listWikiLinks.mockResolvedValue([]);
    api.listLinkableTargets.mockResolvedValue([]);
    api.listWikiCategoryTree.mockResolvedValue(SEED_TREE);
    api.listWikiPages.mockResolvedValue({ rows: SEED_PAGES, total: SEED_PAGES.length });
    api.searchWikiPages.mockResolvedValue({ rows: [], total: 0 });
  });

  it("renders the wiki category tree", async () => {
    renderPage();
    // 树节点 title 带子页计数「采购管理（N）」——直接读 .ant-tree-title。
    // 注意：分类节点不可点，可点的是 page 叶子（pageId 作为 key）。
    await waitFor(() => {
      const titles = Array.from(document.querySelectorAll(".ant-tree-title"))
        .map((el) => (el.textContent ?? "").trim());
      expect(titles.some((t) => t.startsWith("采购管理"))).toBe(true);
      expect(titles.some((t) => t.startsWith("质量管理"))).toBe(true);
      expect(titles.some((t) => t.startsWith("仓储物流"))).toBe(true);
    });
  });

  it("keeps 添加绑定 disabled until a tree node is selected", async () => {
    renderPage();
    const addBtn = screen.getByRole("button", { name: /\+\s*添加绑定/ });
    expect(addBtn).toBeDisabled();

    await clickTreeNode("供应商准入流程");

    // clickTreeNode 内部已 await 按钮启用 — 这是 state 已落库的可观测信号
  });

  it("opens the add modal and fetches linkables for the active tab", async () => {
    api.listLinkableTargets.mockResolvedValue([
      makeLinkable({ id: 42, name: "供应商", alias: "Supplier" }),
      makeLinkable({ id: 43, name: "采购订单", alias: null }),
    ]);

    renderPage();
    await clickTreeNode("供应商准入流程");

    const addBtn = screen.getByRole("button", { name: /\+\s*添加绑定/ });
    await waitFor(() => expect(addBtn).toBeEnabled());
    await userEvent.click(addBtn);

    await waitFor(() => {
      expect(api.listLinkableTargets).toHaveBeenCalledWith("class");
    });
    // Modal 标题（来自 t("wikiLinks.addBinding")="添加绑定"；page-level H2 用
    // "Wiki ↔ Ontology 链接管理" 必须用 .ant-modal-title 锁住避免混淆）
    expect(
      await screen.findByText("添加绑定", { selector: ".ant-modal-title" }),
    ).toBeInTheDocument();
    // Select 字段已渲染（具体选项需要打开下拉才进 DOM，跳过）
    expect(screen.getByLabelText("本体对象")).toBeInTheDocument();
  });

  it("creates a link on submit and refreshes the list", async () => {
    api.listLinkableTargets.mockResolvedValue([
      makeLinkable({ id: 42, name: "供应商", alias: null }),
    ]);
    api.createWikiLink.mockResolvedValue(makeLink({ id: 100 }));

    renderPage();
    await clickTreeNode("供应商准入流程");

    const addBtn = screen.getByRole("button", { name: /\+\s*添加绑定/ });
    await waitFor(() => expect(addBtn).toBeEnabled());
    await userEvent.click(addBtn);

    // 选 ontology 对象
    await selectOption(
      screen.getByRole("combobox", { name: "本体对象" }),
      "供应商",
    );
    // weight 默认 1.0，Slider 不动；note 留空
    await userEvent.click(screen.getByRole("button", { name: /提\s*交/ }));

    await waitFor(() => {
      expect(api.createWikiLink).toHaveBeenCalledWith({
        page_id: "page-001-01",
        ontology_type: "class",
        ontology_id: 42,
        weight: 1,
        chunk_id: null,
        note: null,
      });
    });
    // 提交后必须重新拉列表，否则用户看到的是过时数据
    await waitFor(() => {
      expect(api.listWikiLinks.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    // Modal 关闭
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("revokes a link and refreshes the list", async () => {
    // listWikiLinks 返回 1 条 class 链接
    api.listWikiLinks.mockResolvedValue([
      makeLink({ id: 1, ontology_id: 42, weight: 0.8 }),
    ]);
    api.revokeWikiLink.mockResolvedValue(
      makeLink({ id: 1, revoked_time: "now" }),
    );

    renderPage();
    await clickTreeNode("供应商准入流程");

    // 撤销按钮：antd 在两个汉字之间插一个空格（可访问名是「撤 销」）
    const revokeBtn = await screen.findByRole("button", { name: /撤\s*销/ });
    await userEvent.click(revokeBtn);

    await waitFor(() => {
      expect(api.revokeWikiLink).toHaveBeenCalledWith(1);
    });
    // 撤销后也要刷新，否则页面上还显示这条已撤销的记录
    await waitFor(() => {
      const callsAfterRevoke = api.listWikiLinks.mock.calls.length;
      expect(callsAfterRevoke).toBeGreaterThanOrEqual(2);
    });
  });

  it("shows the chunk_id input only when scope=chunk is selected", async () => {
    api.listLinkableTargets.mockResolvedValue([makeLinkable({ id: 42 })]);

    renderPage();
    await clickTreeNode("供应商准入流程");
    await userEvent.click(
      await screen.findByRole("button", { name: /\+\s*添加绑定/ }),
    );

    // 默认 scope=page：chunk_id 字段不可见
    expect(screen.queryByLabelText("段落 ID")).not.toBeInTheDocument();

    // 切换到 chunk
    await userEvent.click(screen.getByLabelText("仅限此段落"));

    expect(await screen.findByLabelText("段落 ID")).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText("请输入 chunk_id"),
    ).toBeInTheDocument();
  });
});

describe("链接列表显示对象名", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await i18n.changeLanguage("zh-CN");
    api.listWikiLinks.mockResolvedValue([]);
    api.listLinkableTargets.mockResolvedValue([]);
    api.listWikiCategoryTree.mockResolvedValue(SEED_TREE);
    api.listWikiPages.mockResolvedValue({ rows: SEED_PAGES, total: SEED_PAGES.length });
    api.searchWikiPages.mockResolvedValue({ rows: [], total: 0 });
  });

  afterEach(async () => {
    await i18n.changeLanguage("zh-CN");
  });

  it("有别名时显示「别名（物理名）」", async () => {
    api.listWikiLinks.mockResolvedValue([
      makeLink({
        id: 1,
        ontology_id: 42,
        ontology_name: "DWD_ARRIVAL_ORDER_DTL",
        ontology_alias: "到货单",
      }),
    ]);
    renderPage();
    await clickTreeNode("供应商准入流程");
    expect(
      await screen.findByText("到货单（DWD_ARRIVAL_ORDER_DTL）"),
    ).toBeInTheDocument();
  });

  it("无别名时只显示物理名", async () => {
    api.listWikiLinks.mockResolvedValue([
      makeLink({
        id: 1,
        ontology_id: 42,
        ontology_name: "DIM_SUPPLIER",
        ontology_alias: null,
      }),
    ]);
    renderPage();
    await clickTreeNode("供应商准入流程");
    expect(await screen.findByText("DIM_SUPPLIER")).toBeInTheDocument();
  });

  it("对象已删（ontology_name 为 null）时显示 ID:ontology_id", async () => {
    api.listWikiLinks.mockResolvedValue([
      makeLink({ id: 1, ontology_id: 42 }),
    ]);
    renderPage();
    await clickTreeNode("供应商准入流程");
    expect(await screen.findByText("ID:42")).toBeInTheDocument();
  });
});

describe("标签本地化", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listWikiLinks.mockResolvedValue([]);
    api.listLinkableTargets.mockResolvedValue([]);
    api.listWikiCategoryTree.mockResolvedValue(SEED_TREE);
    api.listWikiPages.mockResolvedValue({ rows: SEED_PAGES, total: SEED_PAGES.length });
    api.searchWikiPages.mockResolvedValue({ rows: [], total: 0 });
  });

  // 语言是模块级全局状态：用 afterEach 复位，避免英文用例中途失败时把 en 泄漏给后续用例
  afterEach(async () => {
    await i18n.changeLanguage("zh-CN");
  });

  it("切到英文后 Tab 标签变成 Class / Property", async () => {
    await i18n.changeLanguage("en-US");
    renderPage();
    expect(await screen.findByRole("tab", { name: "Class" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Property" })).toBeInTheDocument();
  });

  it("中文下存在「指标」Tab，切换后用 metric 拉取可链接目标", async () => {
    await i18n.changeLanguage("zh-CN");
    api.listLinkableTargets.mockResolvedValue([]);
    renderPage();

    const metricTab = await screen.findByRole("tab", { name: "指标" });
    await userEvent.click(metricTab);

    // 切 Tab 本身**不**触发拉取 —— `listLinkableTargets` 只在 `openAddModal`
    // （WikiLinksPage.tsx:193）里调用，必须先选树节点让按钮可用再点它。
    // 初稿漏了这一步，按原样写这条用例永远不可能变绿。
    await clickTreeNode("供应商准入流程");
    const addBtn = screen.getByRole("button", { name: /\+\s*添加绑定/ });
    await waitFor(() => expect(addBtn).toBeEnabled());
    await userEvent.click(addBtn);

    await waitFor(() => {
      expect(api.listLinkableTargets).toHaveBeenCalledWith("metric");
    });
  });
});
// ---------------------------------------------------------------------------
// 2026-11-21 可用性修复：
//   1) 本体对象下拉走服务端检索（默认 50 条，可检索全表）
//   2) 左树默认 50 页 + 全量页面检索
//   3) 链接行编辑入口（PATCH weight / note）
// ---------------------------------------------------------------------------
describe("WikiLinksPage 可用性修复", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("zh-CN");
    vi.clearAllMocks();
    api.listWikiLinks.mockResolvedValue([]);
    api.listLinkableTargets.mockResolvedValue([]);
    api.listWikiCategoryTree.mockResolvedValue(SEED_TREE);
    api.listWikiPages.mockResolvedValue({ rows: SEED_PAGES, total: SEED_PAGES.length });
    api.searchWikiPages.mockResolvedValue({ rows: [], total: 0 });
  });

  it("默认只加载 50 页", async () => {
    renderPage();
    await waitFor(() => {
      expect(api.listWikiPages).toHaveBeenCalledWith({ limit: 50 });
    });
  });

  it("总页数超过已加载页数时给出提示", async () => {
    api.listWikiPages.mockResolvedValue({ rows: SEED_PAGES, total: 120 });
    renderPage();
    expect(await screen.findByText(/已显示 7\/120 页/)).toBeInTheDocument();
  });

  it("搜索全部页面并展示扁平结果（树被替换）", async () => {
    api.searchWikiPages.mockResolvedValue({
      rows: [{
        ...SEED_PAGES[0], id: 999, pageId: "page-999",
        title: "远端的页面", categoryId: 7,
      }],
      total: 1,
    });
    renderPage();
    await waitFor(() => {
      expect(document.querySelector(".ant-tree")).toBeInTheDocument();
    });

    const box = screen.getByPlaceholderText("搜索全部页面（标题/正文）");
    fireEvent.change(box, { target: { value: "远端" } });
    fireEvent.keyDown(box, { key: "Enter", code: "Enter", keyCode: 13 });

    await waitFor(() => {
      expect(api.searchWikiPages).toHaveBeenCalledWith({ query: "远端", limit: 50 });
    });
    expect(await screen.findByText("远端的页面")).toBeInTheDocument();
    await waitFor(() => {
      expect(document.querySelector(".ant-tree")).toBeNull();
    });
  });

  it("空白搜索不发请求并保持分类树", async () => {
    renderPage();
    await waitFor(() => {
      expect(document.querySelector(".ant-tree")).toBeInTheDocument();
    });
    const box = screen.getByPlaceholderText("搜索全部页面（标题/正文）");
    fireEvent.change(box, { target: { value: "   " } });
    fireEvent.keyDown(box, { key: "Enter", code: "Enter", keyCode: 13 });

    await waitFor(() => {
      expect(api.searchWikiPages).not.toHaveBeenCalled();
    });
    expect(document.querySelector(".ant-tree")).toBeInTheDocument();
  });

  it("搜索结果标注分类名（无分类显示未分类）", async () => {
    api.searchWikiPages.mockResolvedValue({
      rows: [
        { ...SEED_PAGES[0], id: 901, pageId: "p-901", title: "甲页面", categoryId: 7 },
        { ...SEED_PAGES[0], id: 902, pageId: "p-902", title: "乙页面", categoryId: null },
      ],
      total: 2,
    });
    renderPage();
    const box = screen.getByPlaceholderText("搜索全部页面（标题/正文）");
    fireEvent.change(box, { target: { value: "页面" } });
    fireEvent.keyDown(box, { key: "Enter", code: "Enter", keyCode: 13 });

    expect(await screen.findByText("甲页面")).toBeInTheDocument();
    expect(screen.getByText("入库作业")).toBeInTheDocument();
    expect(screen.getByText("未分类")).toBeInTheDocument();
  });

  it("本体对象搜索走后端，防抖后只发最终关键词", async () => {
    renderPage();
    await clickTreeNode("供应商准入流程");
    await userEvent.click(screen.getByRole("button", { name: /\+\s*添加绑定/ }));
    await waitFor(() => {
      expect(api.listLinkableTargets).toHaveBeenCalledWith("class");
    });

    const combo = screen.getByRole("combobox", { name: "本体对象" });
    fireEvent.change(combo, { target: { value: "S" } });
    fireEvent.change(combo, { target: { value: "SU" } });

    await waitFor(() => {
      expect(api.listLinkableTargets).toHaveBeenCalledWith("class", "SU");
    }, { timeout: 2000 });
    // 中间态 "S" 不应被请求（防抖合并）
    expect(
      api.listLinkableTargets.mock.calls.filter((c) => c[1] === "S"),
    ).toHaveLength(0);
  });

  it("清空本体对象搜索后回到默认列表（不带空串 q）", async () => {
    renderPage();
    await clickTreeNode("供应商准入流程");
    await userEvent.click(screen.getByRole("button", { name: /\+\s*添加绑定/ }));
    await waitFor(() => {
      expect(api.listLinkableTargets).toHaveBeenCalledWith("class");
    });

    const combo = screen.getByRole("combobox", { name: "本体对象" });
    fireEvent.change(combo, { target: { value: "SU" } });
    await waitFor(() => {
      expect(api.listLinkableTargets).toHaveBeenCalledWith("class", "SU");
    }, { timeout: 2000 });

    fireEvent.change(combo, { target: { value: "" } });
    await waitFor(() => {
      const calls = api.listLinkableTargets.mock.calls;
      expect(calls[calls.length - 1]).toEqual(["class"]);
    });
  });

  it("搜索结果替换选项后仍保留已选对象的 label", async () => {
    api.listLinkableTargets
      .mockResolvedValueOnce([makeLinkable({ id: 42, name: "供应商", alias: null })])
      .mockResolvedValueOnce([makeLinkable({ id: 99, name: "XYZ 对象", alias: null })]);
    api.createWikiLink.mockResolvedValue(makeLink({ id: 100 }));

    renderPage();
    await clickTreeNode("供应商准入流程");
    await userEvent.click(screen.getByRole("button", { name: /\+\s*添加绑定/ }));
    const combo = screen.getByRole("combobox", { name: "本体对象" });
    await selectOption(combo, "供应商");

    fireEvent.change(combo, { target: { value: "XYZ" } });
    await waitFor(() => {
      expect(api.listLinkableTargets).toHaveBeenCalledWith("class", "XYZ");
    }, { timeout: 2000 });

    await waitFor(() => {
      expect(
        document.querySelector(".ant-select-selection-item")?.textContent,
      ).toBe("供应商");
    });

    await userEvent.click(screen.getByRole("button", { name: /提\s*交/ }));
    await waitFor(() => {
      expect(api.createWikiLink).toHaveBeenCalledWith(
        expect.objectContaining({ ontology_id: 42 }),
      );
    });
  });

  it("编辑弹窗预填权重与备注", async () => {
    api.listWikiLinks.mockResolvedValue([
      makeLink({ id: 7, weight: 0.8, note: "旧备注", ontology_name: "DIM_X", ontology_alias: null }),
    ]);
    renderPage();
    await clickTreeNode("供应商准入流程");
    await userEvent.click(await screen.findByRole("button", { name: /编\s*辑/ }));

    expect(
      await screen.findByText("编辑绑定", { selector: ".ant-modal-title" }),
    ).toBeInTheDocument();
    const note = await screen.findByLabelText("备注");
    await waitFor(() => {
      expect((note as HTMLTextAreaElement).value).toBe("旧备注");
    });
    // 权重也必须预填（否则用户一提交就把原权重改成默认值）
    expect(screen.getByRole("slider")).toHaveAttribute("aria-valuenow", "0.8");
  });

  it("提交编辑调用 PATCH 并刷新列表", async () => {
    api.listWikiLinks.mockResolvedValue([
      makeLink({ id: 7, weight: 0.8, note: "旧备注", ontology_name: "DIM_X", ontology_alias: null }),
    ]);
    api.updateWikiLink.mockResolvedValue(makeLink({ id: 7 }));

    renderPage();
    await clickTreeNode("供应商准入流程");
    await userEvent.click(await screen.findByRole("button", { name: /编\s*辑/ }));

    const note = await screen.findByLabelText("备注");
    await waitFor(() => {
      expect((note as HTMLTextAreaElement).value).toBe("旧备注");
    });
    fireEvent.change(note, { target: { value: "新备注" } });
    await userEvent.click(screen.getByRole("button", { name: /提\s*交/ }));

    await waitFor(() => {
      expect(api.updateWikiLink).toHaveBeenCalledWith(7, {
        weight: 0.8,
        note: "新备注",
      });
    });
    await waitFor(() => {
      expect(api.listWikiLinks.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("取消编辑不发 PATCH", async () => {
    api.listWikiLinks.mockResolvedValue([
      makeLink({ id: 7, weight: 0.8, note: "旧备注", ontology_name: "DIM_X", ontology_alias: null }),
    ]);
    renderPage();
    await clickTreeNode("供应商准入流程");
    await userEvent.click(await screen.findByRole("button", { name: /编\s*辑/ }));
    await screen.findByText("编辑绑定", { selector: ".ant-modal-title" });

    await userEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
    expect(api.updateWikiLink).not.toHaveBeenCalled();
  });

  it("清空备注时提交空串（null 会被后端当「不修改」）", async () => {
    api.listWikiLinks.mockResolvedValue([
      makeLink({ id: 7, weight: 0.8, note: "旧备注", ontology_name: "DIM_X", ontology_alias: null }),
    ]);
    api.updateWikiLink.mockResolvedValue(makeLink({ id: 7 }));

    renderPage();
    await clickTreeNode("供应商准入流程");
    await userEvent.click(await screen.findByRole("button", { name: /编\s*辑/ }));

    const note = await screen.findByLabelText("备注");
    await waitFor(() => {
      expect((note as HTMLTextAreaElement).value).toBe("旧备注");
    });
    fireEvent.change(note, { target: { value: "" } });
    await userEvent.click(screen.getByRole("button", { name: /提\s*交/ }));

    await waitFor(() => {
      expect(api.updateWikiLink).toHaveBeenCalledWith(7, {
        weight: 0.8,
        note: "",
      });
    });
  });

  it("关闭并重开添加弹窗后，遗留防抖请求不覆盖默认列表", async () => {
    renderPage();
    await clickTreeNode("供应商准入流程");
    const addBtn = screen.getByRole("button", { name: /\+\s*添加绑定/ });
    await userEvent.click(addBtn);
    await waitFor(() => {
      expect(api.listLinkableTargets).toHaveBeenCalledWith("class");
    });

    // 输入后 300ms 内关闭再打开：旧定时器必须被清掉
    const combo = screen.getByRole("combobox", { name: "本体对象" });
    fireEvent.change(combo, { target: { value: "AB" } });
    // 用同步 fireEvent 完成「关闭→重开」：userEvent 的异步等待可能超过 300ms
    // 防抖窗口，让定时器在关闭前就自然触发，测不到竞态。
    fireEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
    fireEvent.click(addBtn);

    // 等过一个完整防抖窗口
    await new Promise((resolve) => setTimeout(resolve, 400));

    const calls = api.listLinkableTargets.mock.calls;
    expect(calls[calls.length - 1]).toEqual(["class"]);
    expect(calls.filter((c) => c[1] === "AB")).toHaveLength(0);
  });
});
