import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
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
