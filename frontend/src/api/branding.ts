/** 品牌配置 API（feat-configurable-brand）。
 *
 *   GET /api/v1/branding  — 公开只读（无需登录）：登录页也要显示系统名称。
 *
 * 返回值只含产品命名（4 个短字符串），不含任何业务数据。
 */

import { httpClient } from "./client";
import type { Branding } from "../branding/defaults";

const PREFIX = "/branding";

/** 后端 JSON（camelCase）与 `Branding` 同形；此处独立声明以便接口变更时显式报错。 */
export type BrandingResponse = Branding;

/**
 * 读取当前生效的品牌配置。
 *
 * `silentErrors: true` —— 后端不可达时**不弹全局错误 toast**：品牌名取不到
 * 应该静默回落到前端兜底值，而不是给用户一个「网络异常」红条。
 */
export async function fetchBranding(): Promise<BrandingResponse> {
    const res = await httpClient.get<BrandingResponse>(PREFIX, {
        silentErrors: true,
    });
    return res.data;
}
