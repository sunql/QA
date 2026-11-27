/** 侧边栏品牌**渲染**取自配置（feat-configurable-brand）。
 *
 * 守卫（源码文本）与 store 单测都盖不到这一层：真正要证明的是
 * 「UI 显示的是配置里的名字，而不是写死的字面量」——
 * 把 store 里的品牌换成别的名字，侧边栏必须跟着变。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";

import AppLayout from "../components/common/AppLayout";

const menuConfigMock = vi.hoisted(() =>
  vi.fn().mockResolvedValue({ version: "2026-11-27", sections: [] }),
);
vi.mock("../api/menuConfig", () => ({ fetchMenuConfig: menuConfigMock }));

import { useBrandingStore } from "../stores/brandingStore";

const CUSTOM_BRANDING = {
  brandName: "泰鸿智云",
  brandNameEn: "Taihong Cloud",
  brandShortName: "TC",
  brandTitle: "泰鸿智云 · 数据智能",
};

function renderLayout() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/"]}>
        <AppLayout />
      </MemoryRouter>
    </ConfigProvider>,
  );
}

describe("侧边栏品牌取自配置", () => {
  beforeEach(() => {
    menuConfigMock.mockClear();
    useBrandingStore.setState({ branding: CUSTOM_BRANDING });
  });

  it("展开态显示配置里的品牌名（不是写死的字面量）", () => {
    renderLayout();
    expect(screen.getByText("泰鸿智云")).toBeInTheDocument();
    expect(screen.queryByText("泰鸿智脑")).not.toBeInTheDocument();
  });

  it("折叠态显示配置里的缩写", async () => {
    const user = userEvent.setup();
    const { container } = renderLayout();
    const trigger = container.querySelector(".ant-layout-sider-trigger");
    expect(trigger).not.toBeNull();

    await user.click(trigger as Element);

    expect(screen.getByText("TC")).toBeInTheDocument();
    expect(screen.queryByText("泰鸿智云")).not.toBeInTheDocument();
  });
});
