/** 品牌配置兜底默认值（feat-configurable-brand）。
 *
 * 系统标题 / 名称（浏览器标签页、侧边栏展开与折叠态、登录页标题）的**运行时
 * SSOT 是后端 `system_config` 表的 4 个品牌键**，通过 `GET /api/v1/branding`
 * 读取。本文件是**最后一道兜底**：后端不可达时用它渲染。
 *
 * 为什么前端也要有一份：首屏必须立刻有名字（不能等一次网络往返才显示标题）。
 * 先用兜底值渲染、后端回来再覆盖 ⇒ **零闪烁**。
 *
 * ⚠️ 本文件的常量与后端 `app/services/branding_service.py` 的 `DEFAULT_*`
 * **必须逐字一致** —— 由 `backend/app/tests/unit/test_branding_service.py::
 * test_frontend_fallback_defaults_match_backend` 跨栈钉住（改一处不改另一处
 * 会让「后端返回 A、前端兜底 B」在接口失败时露出旧名字）。
 */

/** 与后端 `GET /api/v1/branding` 的 JSON 字段一一对应（camelCase）。 */
export interface Branding {
  /** 品牌主名称（中文）：侧边栏展开态、登录页标题。 */
  brandName: string;
  /** 品牌英文名：英文界面下的侧边栏展开态。 */
  brandNameEn: string;
  /** 品牌缩写：侧边栏折叠态（宽度有限，只放得下 2-4 个字符）。 */
  brandShortName: string;
  /** 浏览器标签页标题。 */
  brandTitle: string;
}

export const DEFAULT_BRAND_NAME = "泰鸿智脑";
export const DEFAULT_BRAND_NAME_EN = "Taihong ZhiNao";
export const DEFAULT_BRAND_SHORT_NAME = "TH";
export const DEFAULT_BRAND_TITLE = "泰鸿智脑";

/** 兜底品牌配置（后端不可达时使用）。 */
export const DEFAULT_BRANDING: Branding = {
  brandName: DEFAULT_BRAND_NAME,
  brandNameEn: DEFAULT_BRAND_NAME_EN,
  brandShortName: DEFAULT_BRAND_SHORT_NAME,
  brandTitle: DEFAULT_BRAND_TITLE,
};

/** 长度上限（与后端 `branding_service` 一致）：超长值截断，防止撑爆侧边栏。 */
export const MAX_BRAND_NAME_LENGTH = 64;
export const MAX_BRAND_SHORT_NAME_LENGTH = 16;
export const MAX_BRAND_TITLE_LENGTH = 128;

/** 清洗后端返回值：缺字段 / 非字符串 / 空串 ⇒ 回落到兜底；超长 ⇒ 截断。 */
export function sanitizeBranding(raw: Partial<Branding> | null | undefined): Branding {
  const pick = (
    value: unknown,
    fallback: string,
    maxLength: number,
  ): string => {
    if (typeof value !== "string") return fallback;
    const trimmed = value.trim();
    if (!trimmed) return fallback;
    return trimmed.slice(0, maxLength);
  };
  const brandName = pick(raw?.brandName, DEFAULT_BRAND_NAME, MAX_BRAND_NAME_LENGTH);
  return {
    brandName,
    brandNameEn: pick(
      raw?.brandNameEn,
      DEFAULT_BRAND_NAME_EN,
      MAX_BRAND_NAME_LENGTH,
    ),
    brandShortName: pick(
      raw?.brandShortName,
      DEFAULT_BRAND_SHORT_NAME,
      MAX_BRAND_SHORT_NAME_LENGTH,
    ),
    // 标题回落链：brandTitle → brandName → 兜底（与后端一致）
    brandTitle: pick(raw?.brandTitle, brandName, MAX_BRAND_TITLE_LENGTH),
  };
}
