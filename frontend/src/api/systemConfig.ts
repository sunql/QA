/** system_config admin API（feat-system-config-admin）。
 *
 * 端点：
 *   GET /api/v1/admin/system-config            — 全量列表（任何登录用户）
 *   GET /api/v1/admin/system-config/{key}      — 按 key 查询（admin-only）
 *   PUT /api/v1/admin/system-config/{key}      — 更新 value（admin-only）
 *
 * 复用 httpClient，与 agentTools.ts 同模式。
 */

import { httpClient } from "./client";

const PREFIX = "/admin/system-config";

export interface SystemConfig {
    key: string;
    value: string | null;
    description: string | null;
    updatedTime: string; // ISO8601
}

export interface SystemConfigUpdate {
    value: string | null;
}

export async function listSystemConfig(): Promise<SystemConfig[]> {
    const res = await httpClient.get<SystemConfig[]>(PREFIX);
    return res.data;
}

export async function getSystemConfig(key: string): Promise<SystemConfig> {
    const res = await httpClient.get<SystemConfig>(`${PREFIX}/${key}`);
    return res.data;
}

export async function updateSystemConfig(
    key: string,
    payload: SystemConfigUpdate,
): Promise<SystemConfig> {
    const res = await httpClient.put<SystemConfig>(
        `${PREFIX}/${key}`,
        payload,
    );
    return res.data;
}