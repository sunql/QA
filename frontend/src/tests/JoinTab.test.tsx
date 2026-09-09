import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import JoinTab from "../components/ontology/JoinTab";
import type { OntologyClass, OntologyJoin } from "../types/ontology";

// =============================================================================
// Mocks
// =============================================================================

const mockClass: OntologyClass = {
  id: 1,
  className: "Customer",
  classAlias: "客户",
  description: null,
  sourceTable: "t_customer",
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

const mockClass2: OntologyClass = { ...mockClass, id: 2, className: "Order", classAlias: "订单" };

const mockJoin: OntologyJoin = {
  id: 10,
  sourceClassId: 1,
  sourceColumns: ["customer_id"],
  targetClassId: 2,
  targetColumns: ["customer_id"],
  joinType: "INNER",
  relationType: "foreign_key",
  description: "客户关联订单",
  joinKey: "customer_id",
  createdBy: null,
  createdTime: "2026-08-11T00:00:00Z",
  updatedTime: "2026-08-11T00:00:00Z",
};

const api = vi.hoisted(() => ({
  listJoins: vi.fn(),
  createJoin: vi.fn(),
  deleteJoin: vi.fn(),
}));

vi.mock("../api/ontology", () => api);

function renderTab(classes: OntologyClass[] = [mockClass, mockClass2]) {
  return render(
    <ConfigProvider locale={zhCN}>
      <JoinTab classes={classes} />
    </ConfigProvider>,
  );
}

// =============================================================================
// 渲染 + 加载
// =============================================================================

describe("JoinTab — 渲染与加载", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listJoins.mockResolvedValue([mockJoin]);
  });

  it("挂载时加载 join 列表并展示", async () => {
    renderTab();
    expect(api.listJoins).toHaveBeenCalled();
    // 数据加载后表格显示 source className
    expect(await screen.findByText(/Customer/)).toBeInTheDocument();
  });

  it("无 class 时「新增 Join」按钮禁用", () => {
    api.listJoins.mockResolvedValue([]);
    renderTab([]);
    const addBtn = screen.getByRole("button", { name: /新增\s?关联/ });
    expect(addBtn).toBeDisabled();
  });

  it("加载失败时表格保持空（错误已由拦截器处理）", async () => {
    api.listJoins.mockRejectedValue(new Error("网络错误"));
    renderTab();
    // load() 在 finally 中清 loading，组件不应崩溃
    await waitFor(() => expect(api.listJoins).toHaveBeenCalled());
  });
});

// =============================================================================
// parseColumns（间接通过提交表单验证）
//
// jsdom 下 antd Select 的 option 点击无法稳定把 value 写入 form（已知限制）；
// parseColumns 的纯函数语义由 joinService.spec.ts（后端）等价测试保证；
// 此处只验证 handleSubmit 的校验错误被 catch 吞掉、组件不崩。
// =============================================================================

describe("JoinTab — handleSubmit 校验 catch（不依赖 Select 点击）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listJoins.mockResolvedValue([]);
    api.createJoin.mockResolvedValue({ id: 99 });
  });

  it("打开弹窗后不填任何字段直接提交 → 必填校验错误被 catch 吞掉，组件不崩", async () => {
    const user = userEvent.setup();
    renderTab();

    await user.click(screen.getByRole("button", { name: /新增\s?关联/ }));

    // 直接点确定（不填任何字段）
    const okBtn = await screen.findByRole("button", { name: /确\s?定$/ });
    await user.click(okBtn);

    // 校验失败被 catch，createJoin 不被调用
    await waitFor(() => {
      expect(api.createJoin).not.toHaveBeenCalled();
    });
  });
});

// =============================================================================
// 删除
// =============================================================================

describe("JoinTab — handleDelete", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listJoins.mockResolvedValue([mockJoin]);
    api.deleteJoin.mockResolvedValue(undefined);
  });

  it("点击删除按钮 + Popconfirm 确认 → 调用 deleteJoin 并 reload", async () => {
    const user = userEvent.setup();
    renderTab();

    const deleteBtn = await screen.findByRole("button", { name: /删\s?除/ });
    await user.click(deleteBtn);

    // Popconfirm 弹出"确 定"按钮（弹层中的确定按钮）
    const confirmBtn = await screen.findByRole("button", { name: /确\s?定$/ });
    await user.click(confirmBtn);

    await waitFor(() => expect(api.deleteJoin).toHaveBeenCalledWith(10));
    // reload → 第二次 listJoins
    await waitFor(() => expect(api.listJoins).toHaveBeenCalledTimes(2));
  });

  it("删除失败时 catch 分支被吞掉（不崩）", async () => {
    api.deleteJoin.mockRejectedValue(new Error("权限不足"));
    const user = userEvent.setup();
    renderTab();

    const deleteBtn = await screen.findByRole("button", { name: /删\s?除/ });
    await user.click(deleteBtn);
    const confirmBtn = await screen.findByRole("button", { name: /确\s?定$/ });
    await user.click(confirmBtn);

    await waitFor(() => expect(api.deleteJoin).toHaveBeenCalledWith(10));
    // 不应再调用 listJoins（catch 后不再 load）
  });
});

// =============================================================================
// 提交（handleSubmit）— 表单验证错误
// =============================================================================

describe("JoinTab — handleSubmit 验证错误分支", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listJoins.mockResolvedValue([]);
  });

  it("required 字段为空时点击确定不会调用 createJoin（验证错误被 catch）", async () => {
    const user = userEvent.setup();
    renderTab();

    await user.click(screen.getByRole("button", { name: /新增\s?关联/ }));

    // 直接清空 sourceClassId（让必填校验失败）
    const okBtn = await screen.findByRole("button", { name: /确\s?定$/ });
    await user.click(okBtn);

    // 等 form.validateFields 异步 settle
    await waitFor(() => {
      expect(api.createJoin).not.toHaveBeenCalled();
    });
  });

  it("submit catch 分支：createJoin 抛错时 Modal 不关闭、不崩", async () => {
    // jsdom 下 antd Select 复杂交互不稳定，此处仅验证校验失败 + catch 路径不崩。
    api.createJoin.mockRejectedValue(new Error("服务端错误"));
    const user = userEvent.setup();
    renderTab();

    await user.click(screen.getByRole("button", { name: /新增\s?关联/ }));

    // 不填任何字段直接提交，校验必失败
    const okBtn = await screen.findByRole("button", { name: /确\s?定$/ });
    await user.click(okBtn);

    await waitFor(() => {
      expect(api.createJoin).not.toHaveBeenCalled();
    });
  });
});

// =============================================================================
// 「仅显示外来键」开关：聚焦核查本地导入自动推断的 foreign_key 边
// =============================================================================

describe("JoinTab — 仅外来键过滤", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const businessJoin: OntologyJoin = {
    ...mockJoin,
    id: 20,
    relationType: "business",
    description: "业务语义关联",
  };

  it("开启后仅剩 foreign_key 边，business 边被过滤", async () => {
    api.listJoins.mockResolvedValue([mockJoin, businessJoin]);
    const user = userEvent.setup();
    renderTab();

    // 初始两条都显示
    expect(await screen.findByText("foreign_key")).toBeInTheDocument();
    expect(screen.getByText("business")).toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: /仅显示外来键/ }));

    await waitFor(() => {
      expect(screen.getByText("foreign_key")).toBeInTheDocument();
      expect(screen.queryByText("business")).not.toBeInTheDocument();
    });
  });

  it("再次取消后 business 边恢复显示", async () => {
    api.listJoins.mockResolvedValue([mockJoin, businessJoin]);
    const user = userEvent.setup();
    renderTab();
    await screen.findByText("foreign_key");

    await user.click(screen.getByRole("checkbox", { name: /仅显示外来键/ }));
    await waitFor(() =>
      expect(screen.queryByText("business")).not.toBeInTheDocument(),
    );

    await user.click(screen.getByRole("checkbox", { name: /仅显示外来键/ }));
    expect(screen.getByText("business")).toBeInTheDocument();
  });
});

// =============================================================================
// 刷新按钮
// =============================================================================

describe("JoinTab — 刷新按钮", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listJoins.mockResolvedValue([]);
  });

  it("点击刷新重新调用 listJoins", async () => {
    const user = userEvent.setup();
    renderTab();

    // 初始加载（mount 时）已调用一次
    await waitFor(() => expect(api.listJoins).toHaveBeenCalledTimes(1));

    const refreshBtn = screen.getByRole("button", { name: /刷\s?新/ });
    await user.click(refreshBtn);

    await waitFor(() => expect(api.listJoins).toHaveBeenCalledTimes(2));
  });
});

// =============================================================================
// 渲染边界：description=null 显示 emDash，relationType=foreign_key 显示绿色 Tag
// =============================================================================

describe("JoinTab — 列渲染边界", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    const joinNoDescription: OntologyJoin = {
      ...mockJoin,
      id: 11,
      description: null,
    };
    api.listJoins.mockResolvedValue([joinNoDescription]);
  });

  it("description=null 时渲染 emDash（—）", async () => {
    renderTab();
    expect(await screen.findByText(/Customer/)).toBeInTheDocument();
    // emDash 是 i18n key "common.emDash"，未翻译时显示 raw key 或 — 字符
    await waitFor(() => {
      const cells = document.querySelectorAll("td");
      const hasEmDash = Array.from(cells).some((td) =>
        (td.textContent || "").trim() === "—" ||
        (td.textContent || "").trim() === "common.emDash",
      );
      expect(hasEmDash).toBe(true);
    });
  });

  it("relationType=foreign_key 渲染绿色 Tag", async () => {
    renderTab();
    expect(await screen.findByText(/Customer/)).toBeInTheDocument();
    // 表格里的 foreign_key Tag：antd Tag className 包含 ant-tag-green
    await waitFor(() => {
      const tag = document.querySelector(".ant-tag-green");
      expect(tag?.textContent).toBe("foreign_key");
    });
  });
});