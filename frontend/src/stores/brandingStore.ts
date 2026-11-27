/** 品牌配置 store（feat-configurable-brand）。
 *
 * 状态只有一份：`branding`（4 个展示用字符串）。
 *
 * 初始值 = 前端兜底默认值 ⇒ **首屏零闪烁**：不等网络往返就先渲染兜底名，
 * 后端回来后如果值不同再覆盖（那时用户已经看到一帧了，不会闪）。
 *
 * 取不到值**不抛错、不提示**：品牌名配错不该给用户一个红色报错条。
 */

import { create } from "zustand";

import { fetchBranding } from "../api/branding";
import { DEFAULT_BRANDING, sanitizeBranding, type Branding } from "../branding/defaults";

interface BrandingState {
    branding: Branding;
    /** 拉一次品牌配置（幂等）。失败时保留兜底默认值。 */
    loadBranding: () => Promise<void>;
}

export const useBrandingStore = create<BrandingState>()((set) => ({
    branding: DEFAULT_BRANDING,
    loadBranding: async () => {
        try {
            const remote = await fetchBranding();
            set({ branding: sanitizeBranding(remote) });
        } catch {
            // 后端不可达 ⇒ 保留兜底默认值。不 console.error：这是可预期的降级路径
            // （离线打开静态页 / 后端维护窗口），刷屏会污染控制台。
        }
    },
}));
