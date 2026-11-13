import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import ClassTab from "../components/ontology/ClassTab";
import type { OntologyClass } from "../types/ontology";
import { useAuthStore } from "../stores/authStore";
import type { AuthMeRead } from "../types/auth";
import * as api from "../api/ontology";

vi.mock("../api/ontology", () => ({
  listClasses: vi.fn(),
  listClassVersions: vi.fn(),
  createClass: vi.fn(),
  updateClass: vi.fn(),
  deleteClass: vi.fn(),
  restoreClass: vi.fn(),
  syncClassEmbedding: vi.fn(),
  syncMissingEmbeddings: vi.fn(),
  syncMissingGraph: vi.fn(),
}));

/** 「同步」写面仅 admin（feat-ontology-sync-admin-only）：后端 4 个端点已 403，
 *  UI 同步隐藏入口 —— 否则非 admin 会反复点到 403。 */
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

const NORMAL_USER: AuthMeRead = {
  ...ADMIN_USER,
  id: 2,
  username: "alice",
  displayName: "普通用户",
  roles: ["user"],
};

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

function renderTab() {
  return render(
    <ConfigProvider locale={zhCN}>
      <ClassTab classes={[mockClass]} refreshClasses={vi.fn()} />
    </ConfigProvider>
  );
}

/** 三个同步入口的可见文案（zh-CN 字典原文）。 */
const SYNC_LABELS = ["补同步缺失向量", "补图信息", "同步向量"] as const;

describe("ClassTab 同步入口仅 admin", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listClasses).mockResolvedValue([mockClass]);
  });

  it("非 admin 看不到任何同步入口", async () => {
    useAuthStore.setState({ user: NORMAL_USER });
    renderTab();

    // 等表格渲染出来，避免「还没渲染就断言不存在」的假绿
    expect(await screen.findByText("Order")).toBeInTheDocument();
    for (const label of SYNC_LABELS) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
  });

  it("非 admin 仍可见刷新/新增（不是把整块工具条藏掉）", async () => {
    useAuthStore.setState({ user: NORMAL_USER });
    renderTab();

    expect(await screen.findByText("Order")).toBeInTheDocument();
    expect(screen.getByText("刷新")).toBeInTheDocument();
    expect(screen.getByText("新增类")).toBeInTheDocument();
  });

  it("admin 三个同步入口全部可见", async () => {
    useAuthStore.setState({ user: ADMIN_USER });
    renderTab();

    expect(await screen.findByText("Order")).toBeInTheDocument();
    await waitFor(() => {
      for (const label of SYNC_LABELS) {
        expect(screen.getByText(label)).toBeInTheDocument();
      }
    });
  });
});
