/**
 * 冲突检测（机制 3）API。
 *
 * 端点：
 *   GET  /api/v1/wiki/conflicts                    — 冲突看板列表
 *   POST /api/v1/wiki/pages/{pageId}/conflicts/detect — 为条目跑一次检测
 *   POST /api/v1/wiki/conflicts/{conflictId}/resolve  — 处置
 */

import { httpClient } from "./client";
import type {
    ConflictAction,
    ConflictDetectResult,
    ConflictListStatus,
    ConflictSeverity,
    ConflictType,
    WikiConflictList,
} from "../types/wikiConflicts";

const PREFIX = "/wiki";

export interface ListConflictsParams {
    /** `OPEN` = 只看未解决；后端按 resolvedAt 是否为空解释该值。 */
    status?: ConflictListStatus;
    severity?: ConflictSeverity;
    conflictType?: ConflictType;
    limit?: number;
    offset?: number;
}

export async function listWikiConflicts(
    params: ListConflictsParams = {},
): Promise<WikiConflictList> {
    const res = await httpClient.get<WikiConflictList>(`${PREFIX}/conflicts`, {
        params: {
            status: params.status,
            severity: params.severity,
            conflictType: params.conflictType,
            limit: params.limit ?? 20,
            offset: params.offset ?? 0,
        },
    });
    return res.data;
}

/**
 * 跑一次冲突检测，返回**本次新增**的冲突（已存在的不会重复冒出来）。
 * `modelId` 为空 = 只跑确定性三路（失效/悬空/重复），零 token 成本。
 */
export async function detectWikiConflicts(
    pageId: string,
    modelId?: number,
): Promise<ConflictDetectResult> {
    const res = await httpClient.post<ConflictDetectResult>(
        `${PREFIX}/pages/${pageId}/conflicts/detect`,
        { modelId: modelId ?? null },
    );
    return res.data;
}

/** 处置一条冲突。`action` 必传：只有 IGNORED 表示误报，猜错会污染准确率统计。 */
export async function resolveWikiConflict(
    conflictId: number,
    action: ConflictAction,
): Promise<void> {
    await httpClient.post(`${PREFIX}/conflicts/${conflictId}/resolve`, {
        action,
    });
}
