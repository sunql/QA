import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useTranslation, zhCN } from "./index";

describe("i18n/useTranslation", () => {
  // renderHook + I18nextProvider：react-i18next 的 useTranslation 依赖 React context，
  // 直接在测试顶层调用会失败；用 renderHook 确保上下文就绪。
  const render = () => renderHook(() => useTranslation());

  it("returns Chinese value for known key", () => {
    const { result } = render();
    expect(result.current.t("common.refresh")).toBe("刷新");
  });

  it("interpolates {var} placeholders", () => {
    const { result } = render();
    expect(result.current.t("toast.connectSuccess", { message: "ok" })).toBe("连接成功：ok");
  });

  it("leaves placeholder when var missing", () => {
    const { result } = render();
    expect(result.current.t("toast.connectSuccess")).toBe("连接成功：{message}");
  });

  it("walks nested keys via dot path", () => {
    const { result } = render();
    expect(result.current.t("appLayout.menu.chat")).toBe("AIChatService");
    expect(result.current.t("forms.usage.globalCards.sessions")).toBe("会话数");
  });

  it("returns key itself when path missing", () => {
    const { result } = render();
    expect(result.current.t("nope.nada")).toBe("nope.nada");
  });

  it("exposes locale constant", () => {
    const { result } = render();
    expect(result.current.locale).toBe("zh-CN");
  });

  it("zh-CN dictionary covers all 7 chat-panel chart type labels", () => {
    const chartTypes = ["auto", "table", "bar", "pie", "line", "scatter"] as const;
    for (const ct of chartTypes) {
      expect(zhCN.chatPanel.chartTypes[ct]).toBeTruthy();
    }
  });

  it("zh-CN dictionary covers 5 ontology tabs/columns/min enums", () => {
    expect(zhCN.forms.ontology.tabs.classes).toBe("类");
    expect(zhCN.enums.aggFunction.SUM).toBe("求和 SUM");
    expect(zhCN.enums.dataType.STRING).toBe("字符串 STRING");
  });

  it("returns key itself for missing path (missingKeyHandler does not throw)", () => {
    const { result } = render();
    expect(result.current.t("nonexistent.key")).toBe("nonexistent.key");
  });

  it("keeps t referentially stable across re-renders", () => {
    // 回归契约：t 必须引用稳定。页面普遍把依赖 t 的回调放进 useEffect 依赖
    // （如 AgentRegistryPage 的 refresh），t 每次渲染变新引用会触发无限
    // 请求循环 → ERR_INSUFFICIENT_RESOURCES。
    const { result, rerender } = render();
    const first = result.current.t;
    rerender();
    expect(result.current.t).toBe(first);
  });
});
