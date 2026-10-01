import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useTranslation, zhCN } from "./index";
import { enUS } from "./index";

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

  it("zh-CN dictionary covers all chat-panel chart type labels", () => {
    // auto + 后端 ChartType 全集（决策引擎扩容后 11 类）——缺一个下拉框就少一项
    const chartTypes = [
      "auto",
      "table",
      "bar",
      "hbar",
      "pie",
      "donut",
      "line",
      "scatter",
      "heatmap",
      "kpi",
      "combo",
      "waterfall",
    ] as const;
    for (const ct of chartTypes) {
      expect(zhCN.chatPanel.chartTypes[ct]).toBeTruthy();
    }
  });

  it("en-US dictionary covers all chat-panel chart type labels", () => {
    // 两个字典必须同时齐全：缺一边编译期就报错（keyof typeof zhCN 的约束）
    for (const key of Object.keys(zhCN.chatPanel.chartTypes)) {
      expect(enUS.chatPanel.chartTypes).toHaveProperty(key);
    }
  });

  it("zh-CN / en-US 两个字典都覆盖全部 21 条 visual rationale code", () => {
    // 可视化输出策略（Task 8）：21 个 code 一个不少 —— 缺 key 前端回退显示 code 原文，
    // 不炸但难看（尤其英文用户看到裸 R02_SHARE_DONUT）。
    const codes = [
      "R00_EMPTY_TABLE",
      "R01_SINGLE_VALUE_KPI",
      "R01S_SINGLE_ROW_TABLE",
      "R02_SHARE_DONUT",
      "R03_SHARE_OVERFLOW_HBAR",
      "R04_TOPN_HBAR",
      "R05_WATERFALL",
      "R06_COMBO",
      "R07_TREND_LINE",
      "R08_RELATION_SCATTER",
      "R09_MULTIDIM_HEATMAP",
      "R10_MULTIDIM_BAR",
      "R11_HBAR_MANY_ROWS",
      "R12S_QUESTION_SHARE_DONUT",
      "R12S_QUESTION_SHARE_HBAR",
      "R12_CATEGORY_BAR",
      "R13_RAW_DETAIL_TABLE",
      "R14_DEFAULT_TABLE",
      "R_FORCED_CLIENT",
      "DEGRADE_SPEC_INVALID",
      "SUMMARY_TEXT_ONLY",
    ];
    for (const code of codes) {
      expect(zhCN.chat.visual[code as keyof typeof zhCN.chat.visual]).toBeTruthy();
      expect(enUS.chat.visual[code as keyof typeof enUS.chat.visual]).toBeTruthy();
    }
  });

  it("en-US visual 文案用单花括号占位符（与后端 params 的 rows/kind 逐字对齐）", () => {
    expect(enUS.chat.visual.R02_SHARE_DONUT).toBe(
      "Share breakdown ({rows} items) as a donut chart, with data table"
    );
    expect(enUS.chat.visual.DEGRADE_SPEC_INVALID).toBe(
      "Data does not meet the requirements for {kind}; downgraded to a table"
    );
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
