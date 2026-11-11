import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import TermDictionaryManager from "../components/chat/TermDictionaryManager";
import { useAuthStore } from "../stores/authStore";
import type { AuthMeRead } from "../types/auth";

const ADMIN_USER: AuthMeRead = {
  id: 1,
  username: "admin",
  displayName: "管理员",
  email: null,
  enabled: true,
  mustChangePassword: false,
  roles: ["admin"],
  organizations: [],
  tenantId: "default",
  lastLoginAt: null,
};

const api = vi.hoisted(() => ({
  listTerms: vi.fn(),
  createTerm: vi.fn(),
  deleteTerm: vi.fn(),
}));
vi.mock("../api/termDictionary", () => api);

function renderManager() {
  return render(
    <ConfigProvider locale={zhCN}>
      <TermDictionaryManager />
    </ConfigProvider>,
  );
}

async function openDrawerWithTerm(user: ReturnType<typeof userEvent.setup>) {
  renderManager();
  await user.click(screen.getByRole("button", { name: /术语词典/ }));
  await screen.findByText("供货量");
}

async function openDrawer(user: ReturnType<typeof userEvent.setup>) {
  renderManager();
  await user.click(screen.getByRole("button", { name: /术语词典/ }));
  await screen.findByRole("dialog");
}

describe("TermDictionaryManager — 写入口 admin 门禁", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ user: ADMIN_USER });
    api.listTerms.mockResolvedValue([
      {
        id: 1,
        term: "供货量",
        definition: "生产型物料的到货数量合计",
        mappedClassName: "DWD_GOODS_RECEIPT_DTL",
        mappedPropertyName: "RCV_QTY_PUU",
        formulaHint: null,
        createdBy: "admin",
        createdTime: "2026-11-10T00:00:00Z",
      },
    ]);
  });

  it("非管理员看不到删除入口（列表仍可读）", async () => {
    useAuthStore.setState({ user: { ...ADMIN_USER, roles: ["user"] } });
    const user = userEvent.setup();
    await openDrawerWithTerm(user);

    expect(screen.queryByRole("button", { name: /删\s?除/ })).not.toBeInTheDocument();
  });

  it("管理员看得到删除入口", async () => {
    const user = userEvent.setup();
    await openDrawerWithTerm(user);

    expect(screen.getByRole("button", { name: /删\s?除/ })).toBeInTheDocument();
  });
});

describe("TermDictionaryManager — 抽屉内新增入口", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ user: ADMIN_USER });
    api.listTerms.mockResolvedValue([]);
    api.createTerm.mockResolvedValue({
      id: 2,
      term: "OTD",
      definition: "On-Time Delivery",
      mappedClassName: null,
      mappedPropertyName: null,
      formulaHint: null,
      createdBy: "admin",
      createdTime: "2026-11-11T00:00:00Z",
      updatedTime: "2026-11-11T00:00:00Z",
    });
  });

  it("管理员看得到「新增」入口", async () => {
    const user = userEvent.setup();
    await openDrawer(user);

    expect(screen.getByRole("button", { name: /新\s?增/ })).toBeInTheDocument();
  });

  it("非管理员看不到「新增」入口", async () => {
    useAuthStore.setState({ user: { ...ADMIN_USER, roles: ["user"] } });
    const user = userEvent.setup();
    await openDrawer(user);

    expect(screen.queryByRole("button", { name: /新\s?增/ })).not.toBeInTheDocument();
  });

  it("新增提交后调用 createTerm 并重新加载列表", async () => {
    const user = userEvent.setup();
    await openDrawer(user);

    await user.click(screen.getByRole("button", { name: /新\s?增/ }));
    const termInput = await screen.findByPlaceholderText(/占比/);
    const defInput = screen.getByPlaceholderText(/占总量的比例/);
    await user.type(termInput, "OTD");
    await user.type(defInput, "On-Time Delivery");
    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.createTerm).toHaveBeenCalledTimes(1);
      expect(api.createTerm.mock.calls[0][0]).toMatchObject({
        term: "OTD",
        definition: "On-Time Delivery",
      });
      // 抽屉首次加载 + 创建成功后重载
      expect(api.listTerms).toHaveBeenCalledTimes(2);
    });
  });

  it("新增表单校验失败时不创建、不产生未处理的 rejection", async () => {
    const user = userEvent.setup();
    await openDrawer(user);

    await user.click(screen.getByRole("button", { name: /新\s?增/ }));
    await screen.findByPlaceholderText(/占比/);
    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    // 字段级红字由 antd 内联渲染；这里钉住「API 未被调用」且不抛到用例外
    expect(await screen.findByText(/必填项/)).toBeInTheDocument();
    expect(api.createTerm).not.toHaveBeenCalled();
  });
});
