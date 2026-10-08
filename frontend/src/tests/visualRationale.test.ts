/** 可视化输出策略（Task 8）的 rationale 文案本地化纯函数单测。
 *
 * 只验证「取什么 key、传什么 vars」这一层契约 —— 真实插值（单花括号 `{rows}`）与
 * zh/en 文案由 i18n 字典测试 + 组件测试（ChartRenderer/MessageItem 用真实 i18n 实例
 * 断言最终中文文案）覆盖。这里用 stub `t` 钉死取值链，避免依赖 i18next 内部实现。
 */
import { describe, it, expect, vi } from "vitest";
import { visualRationaleText } from "../utils/visualRationale";
import type { Vars } from "../i18n/types";

type T = (key: string, vars?: Vars) => string;

describe("visualRationaleText", () => {
  it("rationale 为 null/undefined 时返回 null（不调用 t）", () => {
    const t = vi.fn<T>();
    expect(visualRationaleText(null, t)).toBeNull();
    expect(visualRationaleText(undefined, t)).toBeNull();
    expect(t).not.toHaveBeenCalled();
  });

  it("拼出 chat.visual.<code> key 并原样透传 rows", () => {
    const t = vi.fn<T>((key) => key);
    visualRationaleText({ code: "R02_SHARE_DONUT", params: { rows: 3 } }, t);
    expect(t).toHaveBeenCalledWith("chat.visual.R02_SHARE_DONUT", { rows: 3 });
  });

  it("插值前先把 params.kind 本地化（DEGRADE_SPEC_INVALID）", () => {
    const t = vi.fn<T>((key) => {
      if (key === "chatPanel.chartTypes.bar") return "柱状图";
      return key;
    });
    visualRationaleText({ code: "DEGRADE_SPEC_INVALID", params: { kind: "bar" } }, t);
    expect(t).toHaveBeenCalledWith("chatPanel.chartTypes.bar");
    expect(t).toHaveBeenCalledWith("chat.visual.DEGRADE_SPEC_INVALID", { kind: "柱状图" });
  });

  it("图型名未知时 kind 回退原值（不硬塞 i18n key）", () => {
    const t = vi.fn<T>((key) => key);
    visualRationaleText({ code: "DEGRADE_SPEC_INVALID", params: { kind: "notachart" } }, t);
    expect(t).toHaveBeenCalledWith("chat.visual.DEGRADE_SPEC_INVALID", { kind: "notachart" });
  });

  it("i18n 缺 key 时返回 code 原文（不炸）", () => {
    const t = vi.fn<T>((key) => key);
    expect(visualRationaleText({ code: "R99_FUTURE", params: {} }, t)).toBe("R99_FUTURE");
  });

  it("丢弃非 string/number/boolean 的 params 值（不传给插值）", () => {
    const t = vi.fn<T>((key) => key);
    visualRationaleText(
      { code: "R_FORCED_CLIENT", params: { kind: "line", junk: { a: 1 } } },
      t
    );
    const call = t.mock.calls.find(([k]) => k === "chat.visual.R_FORCED_CLIENT");
    expect(call?.[1]).toEqual({ kind: "line" });
  });

  it("无 params 的 code 传空 vars", () => {
    const t = vi.fn<T>((key) => key);
    visualRationaleText({ code: "R13_RAW_DETAIL_TABLE", params: {} }, t);
    expect(t).toHaveBeenCalledWith("chat.visual.R13_RAW_DETAIL_TABLE", {});
  });
});
