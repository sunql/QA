import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import LanguageSwitch from "../components/common/LanguageSwitch";

vi.mock("../i18n", () => ({
  i18n: {
    language: "zh-CN",
    changeLanguage: vi.fn(),
  },
}));

import { i18n } from "../i18n";

describe("LanguageSwitch", () => {
  beforeEach(() => {
    vi.mocked(i18n.changeLanguage).mockClear();
  });

  it("渲染下拉并显示当前语言", () => {
    render(
      <ConfigProvider locale={zhCN}>
        <LanguageSwitch />
      </ConfigProvider>,
    );
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });

  it("切换语言时调用 i18n.changeLanguage", async () => {
    const user = userEvent.setup();
    render(
      <ConfigProvider locale={zhCN}>
        <LanguageSwitch />
      </ConfigProvider>,
    );

    await user.click(screen.getByRole("combobox"));
    await user.click(screen.getByText("EN"));
    expect(i18n.changeLanguage).toHaveBeenCalledWith("en-US");
  });
});
