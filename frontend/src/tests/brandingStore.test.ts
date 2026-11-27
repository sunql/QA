/** 品牌配置 store 单测（feat-configurable-brand）。
 *
 * 钉住三件事：
 * 1. **首屏零闪烁**：初始值 = 前端兜底默认值（不等网络往返）。
 * 2. **后端配了就生效**：拉到远端值 ⇒ store 更新。
 * 3. **取不到不炸**：接口失败 ⇒ 保留兜底默认值。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";

import {
  DEFAULT_BRANDING,
  DEFAULT_BRAND_NAME,
  sanitizeBranding,
} from "../branding/defaults";

const fetchBrandingMock = vi.fn();

vi.mock("../api/branding", () => ({
  fetchBranding: (...args: unknown[]) => fetchBrandingMock(...args),
}));

const { useBrandingStore } = await import("../stores/brandingStore");

describe("sanitizeBranding", () => {
  it("缺字段 / 非字符串 ⇒ 回落兜底默认值", () => {
    const result = sanitizeBranding({
      brandName: undefined,
      brandNameEn: 123 as unknown as string,
    });
    expect(result.brandName).toBe(DEFAULT_BRANDING.brandName);
    expect(result.brandNameEn).toBe(DEFAULT_BRANDING.brandNameEn);
    expect(result.brandShortName).toBe(DEFAULT_BRANDING.brandShortName);
  });

  it("空串 / 纯空白 ⇒ 回落兜底默认值", () => {
    const result = sanitizeBranding({ brandName: "   ", brandShortName: "" });
    expect(result.brandName).toBe(DEFAULT_BRANDING.brandName);
    expect(result.brandShortName).toBe(DEFAULT_BRANDING.brandShortName);
  });

  it("超长值截断（防撑爆侧边栏）", () => {
    const result = sanitizeBranding({ brandName: "x".repeat(500) });
    expect(result.brandName.length).toBe(64);
  });

  it("标题未配置 ⇒ 跟随主名称（与后端回落链一致）", () => {
    const result = sanitizeBranding({ brandName: "泰鸿智云" });
    expect(result.brandTitle).toBe("泰鸿智云");
  });
});

describe("useBrandingStore", () => {
  beforeEach(() => {
    fetchBrandingMock.mockReset();
    useBrandingStore.setState({ branding: DEFAULT_BRANDING });
  });

  it("初始值就是兜底默认值（首屏零闪烁）", () => {
    expect(useBrandingStore.getState().branding).toEqual(DEFAULT_BRANDING);
  });

  it("拉到远端值 ⇒ store 更新", async () => {
    fetchBrandingMock.mockResolvedValue({
      brandName: "泰鸿智云",
      brandNameEn: "Taihong Cloud",
      brandShortName: "TC",
      brandTitle: "泰鸿智云 · 数据智能",
    });

    await useBrandingStore.getState().loadBranding();

    const state = useBrandingStore.getState();
    expect(state.branding.brandName).toBe("泰鸿智云");
    expect(state.branding.brandShortName).toBe("TC");
    expect(state.branding.brandTitle).toBe("泰鸿智云 · 数据智能");
  });

  it("接口失败 ⇒ 保留兜底默认值（不抛错）", async () => {
    fetchBrandingMock.mockRejectedValue(new Error("network down"));

    await expect(useBrandingStore.getState().loadBranding()).resolves.toBeUndefined();

    expect(useBrandingStore.getState().branding.brandName).toBe(DEFAULT_BRAND_NAME);
  });

  it("远端返回空品牌名 ⇒ 回落兜底（不渲染空白标题）", async () => {
    fetchBrandingMock.mockResolvedValue({
      brandName: "",
      brandNameEn: "",
      brandShortName: "",
      brandTitle: "",
    });

    await useBrandingStore.getState().loadBranding();

    expect(useBrandingStore.getState().branding).toEqual(DEFAULT_BRANDING);
  });
});
