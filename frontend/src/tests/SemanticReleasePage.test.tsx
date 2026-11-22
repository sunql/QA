/** G1 语义发布管理页：只读降级 + 校验详情 + 写操作入口。
 *
 * 判据：
 * 1. 非 admin **看不到**写操作（校验/激活/归档/回滚）——与后端 admin-only 同口径；
 * 2. 详情抽屉展示 **severity × result**（两者正交，不能只看一个）；
 * 3. 激活走二次确认（不可逆操作）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { describe, expect, it, vi, beforeEach } from "vitest";
import i18n from "../i18n/i18n";
import SemanticReleasePage from "../pages/SemanticReleasePage";
import { useAuthStore } from "../stores/authStore";

const api = vi.hoisted(() => ({
  listSemanticReleases: vi.fn(),
  getSemanticRelease: vi.fn(),
  listSemanticReleaseChecks: vi.fn(),
  createSemanticReleaseDraft: vi.fn(),
  validateSemanticRelease: vi.fn(),
  activateSemanticRelease: vi.fn(),
  archiveSemanticRelease: vi.fn(),
  rollbackSemanticRelease: vi.fn(),
}));
vi.mock("../api/semanticRelease", () => api);

const ROW = {
  id: 7,
  version: "v-2026.11.22-1",
  domainCode: "PROCUREMENT",
  status: "DRAFT",
  schemaVersion: "2",
  manifestHash: "abcdef1234567890",
  createdBy: "admin",
  createdTime: "2026-11-22T10:00:00Z",
  activatedTime: null,
};

function renderPage() {
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <SemanticReleasePage />
      </ConfigProvider>
    </I18nextProvider>,
  );
}

describe("SemanticReleasePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listSemanticReleases.mockResolvedValue({ rows: [ROW], total: 1 });
  });

  it("非 admin：可看列表，但没有写操作", async () => {
    useAuthStore.setState({ user: { username: "u", roles: ["analyst"] } as never });

    renderPage();

    await waitFor(() => expect(screen.getByText("v-2026.11.22-1")).toBeTruthy());
    expect(screen.getByText(/仅管理员可执行发布写操作/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /激\s*活/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /校\s*验/ })).toBeNull();
  });

  it("admin：详情抽屉展示 severity 与 result 两列", async () => {
    useAuthStore.setState({ user: { username: "admin", roles: ["admin"] } as never });
    api.getSemanticRelease.mockResolvedValue({
      ...ROW,
      manifest: { classes: [1, 2], joins: [1] },
    });
    api.listSemanticReleaseChecks.mockResolvedValue({
      rows: [
        {
          id: 1,
          releaseId: 7,
          checkCode: "METRIC_SOURCE_PRESENT",
          severity: "WARNING",
          result: "FAIL",
          detail: { metric: "交付及时率" },
        },
      ],
      total: 1,
    });

    renderPage();
    await waitFor(() => expect(screen.getByText("v-2026.11.22-1")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /详\s*情/ }));

    await waitFor(() => expect(screen.getByText("METRIC_SOURCE_PRESENT")).toBeTruthy());
    expect(screen.getByText("WARNING")).toBeTruthy();
    expect(screen.getByText("FAIL")).toBeTruthy();
  });

  it("admin：激活需二次确认", async () => {
    useAuthStore.setState({ user: { username: "admin", roles: ["admin"] } as never });
    api.activateSemanticRelease.mockResolvedValue({ release: ROW, blockers: [], warnings: [] });

    renderPage();
    await waitFor(() => expect(screen.getByText("v-2026.11.22-1")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /激\s*活/ }));

    await waitFor(() => expect(screen.getByText(/激活后该域内其余版本将被归档/)).toBeTruthy());
    expect(api.activateSemanticRelease).not.toHaveBeenCalled();
  });
});
