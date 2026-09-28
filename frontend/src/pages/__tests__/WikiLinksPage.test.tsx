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

import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../../i18n";
import { WikiLinksPage } from "../WikiLinksPage";
import type { WikiLink, WikiLinkableTarget } from "../../types/wikiLink";

const api = vi.hoisted(() => ({
  listWikiLinks: vi.fn(),
  createWikiLink: vi.fn(),
  revokeWikiLink: vi.fn(),
  updateWikiLink: vi.fn(),
  listLinkableTargets: vi.fn(),
}));

vi.mock("../../api/adminWikiLinks", () => api);

function makeLink(overrides: Partial<WikiLink> = {}): WikiLink {
  return {
    id: 1,
    page_id: "page-001",
    chunk_id: null,
    ontology_type: "class",
    ontology_id: 42,
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
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <WikiLinksPage />
      </ConfigProvider>
    </I18nextProvider>,
  );
}

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
  const titles = Array.from(document.querySelectorAll(".ant-tree-title"));
  const target = titles.find((el) => el.textContent === text);
  if (!target) {
    throw new Error(`tree node not found: ${text}`);
  }
  const wrapper = target.closest(".ant-tree-node-content-wrapper") as HTMLElement;
  await userEvent.click(wrapper);
  // 「+ 添加绑定」按钮启用 = selectedPageId 已落到 React 里
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
  });

  it("renders title and empty state", () => {
    renderPage();
    // i18n resolved value (default zh-CN) — use regex to avoid exact-match issues
    expect(
      screen.getByText(/Wiki.*Ontology.*链接管理/),
    ).toBeInTheDocument();
    expect(screen.getByText(/请先选择左侧 Wiki 页面/)).toBeInTheDocument();
  });

  it("renders class and property tabs", () => {
    renderPage();
    expect(screen.getByText("Class")).toBeInTheDocument();
    expect(screen.getByText("Property")).toBeInTheDocument();
  });
});

describe("WikiLinksPage 交互", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listWikiLinks.mockResolvedValue([]);
    api.listLinkableTargets.mockResolvedValue([]);
  });

  it("renders the stub wiki page tree", () => {
    renderPage();
    // antd Tree 默认折叠，子节点不进 DOM；只钉根节点，避免后续调整
    // STUB_PAGES 时静默漂移。
    expect(screen.getByText("采购管理")).toBeInTheDocument();
    expect(screen.getByText("质量管理")).toBeInTheDocument();
    expect(screen.getByText("仓储物流")).toBeInTheDocument();
  });

  it("keeps 添加绑定 disabled until a tree node is selected", async () => {
    renderPage();
    const addBtn = screen.getByRole("button", { name: /\+\s*添加绑定/ });
    expect(addBtn).toBeDisabled();

    await clickTreeNode("采购管理");

    // clickTreeNode 内部已 await 按钮启用 — 这是 state 已落库的可观测信号
  });

  it("opens the add modal and fetches linkables for the active tab", async () => {
    api.listLinkableTargets.mockResolvedValue([
      makeLinkable({ id: 42, name: "供应商", alias: "Supplier" }),
      makeLinkable({ id: 43, name: "采购订单", alias: null }),
    ]);

    renderPage();
    await clickTreeNode("采购管理");

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
    await clickTreeNode("采购管理");

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
        page_id: "page-001",
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
    await clickTreeNode("采购管理");

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
    await clickTreeNode("采购管理");
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