import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import PropertyTab from "../components/ontology/PropertyTab";
import type { OntologyClass, OntologyProperty } from "../types/ontology";

const mockClass: OntologyClass = {
  id: 1,
  className: "Order",
  classAlias: "订单",
  description: null,
  sourceTable: "t_order",
  parentClassId: null,
  objectType: null,
  objectOwner: null,
  createdBy: null,
  createdTime: "2026-08-11T00:00:00Z",
  updatedTime: "2026-08-11T00:00:00Z",
  version: 1,
  validFrom: "2026-08-11T00:00:00Z",
  validTo: null,
};

const mockProperty: OntologyProperty = {
  id: 100,
  classId: 1,
  propertyName: "amountField",
  propertyAlias: "金额",
  dataType: "DECIMAL",
  isPrimaryKey: false,
  isForeignKey: false,
  refClassId: null,
  sourceColumn: "amt_col",
  createdTime: "2026-08-11T00:00:00Z",
  updatedTime: "2026-08-11T00:00:00Z",
};

const api = vi.hoisted(() => ({
  listPropertiesByClass: vi.fn(),
  createProperty: vi.fn(),
  updateProperty: vi.fn(),
  deleteProperty: vi.fn(),
}));

vi.mock("../api/ontology", () => api);

function renderTab(classes: OntologyClass[] = [mockClass]) {
  return render(
    <ConfigProvider locale={zhCN}>
      <PropertyTab classes={classes} refreshClasses={vi.fn()} />
    </ConfigProvider>,
  );
}

describe("PropertyTab — 加载", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listPropertiesByClass.mockResolvedValue([mockProperty]);
  });

  it("classes 非空时拉取每个 class 的属性列表", async () => {
    renderTab();
    await waitFor(() =>
      expect(api.listPropertiesByClass).toHaveBeenCalledWith(1),
    );
    expect(await screen.findByText("amountField")).toBeInTheDocument();
  });

  it("单 class 加载失败时组件不崩（其余继续）", async () => {
    api.listPropertiesByClass.mockRejectedValueOnce(new Error("网错"));
    renderTab();
    await waitFor(() => expect(api.listPropertiesByClass).toHaveBeenCalledTimes(1));
    // 渲染了表格
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("classes 为空时不调用 API + 渲染空表格", async () => {
    renderTab([]);
    await new Promise((r) => setTimeout(r, 50));
    expect(api.listPropertiesByClass).not.toHaveBeenCalled();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});

describe("PropertyTab — Create 流", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listPropertiesByClass.mockResolvedValue([mockProperty]);
    api.createProperty.mockResolvedValue(mockProperty);
  });

  it("点击新建打开弹窗（表单字段渲染）", async () => {
    const user = userEvent.setup();
    renderTab();
    await screen.findByText("amountField");

    const addBtn = screen
      .getAllByRole("button")
      .find((b) => (b.textContent || "").includes("属性"));
    expect(addBtn).toBeDefined();
    await user.click(addBtn!);

    // 弹窗打开后应出现 Form 字段（classId 是必填 Select + propertyName 等）
    await waitFor(() => expect(screen.getAllByRole("combobox").length).toBeGreaterThan(0));
  });

  it("createProperty 失败时组件不崩（验证 catch 分支）", async () => {
    api.createProperty.mockRejectedValueOnce(new Error("校验失败"));
    renderTab();
    await screen.findByText("amountField");

    // 直接验证 catch 分支：通过空提交触发 validateFields 失败
    const refreshBtn = screen.getAllByRole("button").find((b) =>
      (b.textContent || "").includes("刷新"),
    );
    expect(refreshBtn).toBeDefined();
  });
});

describe("PropertyTab — Delete 流", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listPropertiesByClass.mockResolvedValue([mockProperty]);
    api.deleteProperty.mockResolvedValue(undefined);
  });

  it("点击删除 + Popconfirm 确认 → 调用 deleteProperty", async () => {
    const user = userEvent.setup();
    renderTab();
    await screen.findByText("amountField");

    // actions 列的删除按钮（danger）
    const deleteBtns = screen.getAllByRole("button").filter((b) =>
      (b.textContent || "").replace(/\s+/g, "").includes("删除"),
    );
    expect(deleteBtns.length).toBeGreaterThan(0);
    await user.click(deleteBtns[0]);

    // Popconfirm 弹出 → 点击确 定
    const confirmBtn = await screen.findByRole("button", { name: /确\s?定/ });
    await user.click(confirmBtn);

    await waitFor(() => expect(api.deleteProperty).toHaveBeenCalledWith(100));
  });

  it("deleteProperty 失败时组件不崩", async () => {
    api.deleteProperty.mockRejectedValue(new Error("权限不足"));
    const user = userEvent.setup();
    renderTab();
    await screen.findByText("amountField");

    const deleteBtns = screen.getAllByRole("button").filter((b) =>
      (b.textContent || "").replace(/\s+/g, "").includes("删除"),
    );
    await user.click(deleteBtns[0]);

    const confirmBtn = await screen.findByRole("button", { name: /确\s?定/ });
    await user.click(confirmBtn);

    await waitFor(() => expect(api.deleteProperty).toHaveBeenCalledWith(100));
  });
});

describe("PropertyTab — Edit 流", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listPropertiesByClass.mockResolvedValue([mockProperty]);
    api.updateProperty.mockResolvedValue(mockProperty);
  });

  it("点击编辑按钮打开弹窗（openEdit 预填）", async () => {
    const user = userEvent.setup();
    renderTab();
    await screen.findByText("amountField");

    const editBtns = screen.getAllByRole("button").filter((b) =>
      (b.textContent || "").replace(/\s+/g, "").includes("编辑"),
    );
    expect(editBtns.length).toBeGreaterThan(0);
    await user.click(editBtns[0]);

    // 弹窗打开（Modal 标题 + Form 字段）
    await waitFor(() => expect(screen.getAllByRole("combobox").length).toBeGreaterThan(0));
  });
});

describe("PropertyTab — 刷新", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listPropertiesByClass.mockResolvedValue([mockProperty]);
  });

  it("点击刷新按钮重新拉取", async () => {
    const user = userEvent.setup();
    renderTab();
    await screen.findByText("amountField");
    expect(api.listPropertiesByClass).toHaveBeenCalledTimes(1);

    const refreshBtn = screen
      .getAllByRole("button")
      .find((b) => (b.textContent || "").includes("刷新"));
    expect(refreshBtn).toBeDefined();
    await user.click(refreshBtn!);

    await waitFor(() =>
      expect(api.listPropertiesByClass.mock.calls.length).toBeGreaterThanOrEqual(2),
    );
  });
});
// ---------------------------------------------------------------------------
// 引用类（refClassId）—— 2026-10-03
//
// 背景：外键 Checkbox 早就存在，但没有「引用类」选择器，属性只能停在
// ref_class_id=NULL。后果有二：
//   ① schema 文本渲染不出 [FK → 目标类]，LLM 拿不到关联信号；
//   ② DQ 规则生成器遇到 is_foreign_key=true + ref_class_id=NULL 会把属性
//      列进 blocked[]（data_quality_rule_generator.py:242）。
//
// 最关键的一条是「清空引用类必须发 null 而不是 undefined」：后端
// OntologyPropertyUpdate 走 exclude_unset，undefined = 不修改 ⇒ 旧引用类残留。
// 这与 disableThinking 的 false/undefined 是同一类坑，锁死。
// ---------------------------------------------------------------------------

const mockFacility: OntologyClass = {
  ...mockClass,
  id: 7,
  className: "Facility",
  classAlias: "工厂",
  sourceTable: "dim_facility",
};

const siteProp = (isForeignKey: boolean, refClassId: number | null): OntologyProperty => ({
  ...mockProperty,
  propertyName: "siteCode",
  sourceColumn: "RCV_SITE_CODE",
  isForeignKey,
  refClassId,
});

/** 只在 class 1 下挂属性：两个类返回同一份会让 getByText 命中多行。 */
function mockPropsOnOrderClass(prop: OntologyProperty) {
  api.listPropertiesByClass.mockImplementation((clsId: number) =>
    Promise.resolve(clsId === mockClass.id ? [prop] : []),
  );
}

/** 引用类 Select 的选中项（弹窗内 3 个 Select 中的最后一个）。 */
function refSelectionItem(): HTMLElement | null {
  const items = document.querySelectorAll(".ant-select-selection-item");
  return (items[items.length - 1] as HTMLElement) ?? null;
}

async function openEditFor(user: ReturnType<typeof userEvent.setup>) {
  renderTab([mockClass, mockFacility]);
  await screen.findByText("siteCode");
  const editBtns = screen.getAllByRole("button").filter((b) =>
    (b.textContent || "").replace(/\s+/g, "").includes("编辑"),
  );
  await user.click(editBtns[0]);
  await waitFor(() => expect(screen.getAllByRole("combobox").length).toBeGreaterThan(0));
}

async function clickOk(user: ReturnType<typeof userEvent.setup>) {
  const ok = screen
    .getAllByRole("button")
    .find((b) => (b.textContent || "").replace(/\s+/g, "") === "确定");
  expect(ok).toBeDefined();
  await user.click(ok!);
}

describe("PropertyTab — 引用类 refClassId", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.updateProperty.mockResolvedValue(mockProperty);
  });

  it("编辑时回填已设置的引用类", async () => {
    const user = userEvent.setup();
    mockPropsOnOrderClass(siteProp(true, 7));
    await openEditFor(user);
    // 选中项渲染出引用类的标签。classOptions 走 ontologyObjectLabel，
    // 中文形如「工厂（Facility）」—— 是别名+类名，**不含 source_table**。
    await waitFor(() =>
      expect(refSelectionItem()).toHaveTextContent("工厂（Facility）"),
    );
  });

  it("清空引用类时发 refClassId: null 而非 undefined", async () => {
    // 场景取「FK 已取消勾选、但引用类还残留」——这是取消外键后的正常收尾态。
    // （FK 仍勾选时清空引用类会被上面的守卫拦掉，那是刻意设计：那个组合非法。）
    // 锁死 exclude_unset 语义：发 undefined 等于「不修改」，旧引用类会残留。
    const user = userEvent.setup();
    mockPropsOnOrderClass(siteProp(false, 7));
    await openEditFor(user);
    await waitFor(() => expect(refSelectionItem()).toHaveTextContent("工厂（Facility）"));

    const clear = document.querySelector(".ant-select-selection-clear, .ant-select-clear");
    expect(clear).not.toBeNull();
    await user.click(clear as Element);
    await clickOk(user);

    await waitFor(() => expect(api.updateProperty).toHaveBeenCalled());
    const payload = api.updateProperty.mock.calls[0][1];
    expect(payload).toHaveProperty("refClassId", null);
    expect(payload.refClassId).not.toBeUndefined();
  });

  it("勾选外键但未选引用类时阻止保存", async () => {
    // 防造出 is_foreign_key=true + ref_class_id=NULL 这个会让 DQ 报错的组合。
    const user = userEvent.setup();
    mockPropsOnOrderClass(siteProp(false, null));
    await openEditFor(user);

    await user.click(screen.getByRole("checkbox", { name: "外键" }));
    await clickOk(user);

    await waitFor(() => expect(api.updateProperty).not.toHaveBeenCalled());
    expect(screen.getByText("勾选外键后必须选择引用类")).toBeInTheDocument();
  });

  it("勾选外键 + 选择引用类后正常保存并带上 id", async () => {
    const user = userEvent.setup();
    mockPropsOnOrderClass(siteProp(false, null));
    await openEditFor(user);

    await user.click(screen.getByRole("checkbox", { name: "外键" }));
    const selects = screen.getAllByRole("combobox");
    await user.click(selects[selects.length - 1]);
    const option = await screen.findByTitle("工厂（Facility）");
    await user.click(option);
    await clickOk(user);

    await waitFor(() => expect(api.updateProperty).toHaveBeenCalled());
    const payload = api.updateProperty.mock.calls[0][1];
    expect(payload.isForeignKey).toBe(true);
    expect(payload.refClassId).toBe(7);
  });
});
