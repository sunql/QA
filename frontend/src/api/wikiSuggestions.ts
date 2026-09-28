/**
 * 结构化建议（机制 4）API。
 *
 * 两个列表端点视角不同，别混用：
 *   GET /api/v1/wiki/suggestions            — 建议工作台（跨条目，带标题）
 *   GET /api/v1/wiki/pages/{id}/suggestions — 条目详情里的子列表（不带标题）
 */

import { httpClient } from "./client";
import type {
    StructureSuggestion,
    SuggestionGenerateResult,
    SuggestionStatus,
    WikiSuggestionList,
} from "../types/wikiSuggestions";

const PREFIX = "/wiki";

export async function listAllWikiSuggestions(
    params: { status?: SuggestionStatus; limit?: number; offset?: number } = {},
): Promise<WikiSuggestionList> {
    const res = await httpClient.get<WikiSuggestionList>(`${PREFIX}/suggestions`, {
        params: {
            status: params.status,
            limit: params.limit ?? 20,
            offset: params.offset ?? 0,
        },
    });
    return res.data;
}

export async function listPageSuggestions(
    pageId: string,
    status?: SuggestionStatus,
): Promise<WikiSuggestionList> {
    const res = await httpClient.get<WikiSuggestionList>(
        `${PREFIX}/pages/${pageId}/suggestions`,
        { params: { status } },
    );
    return res.data;
}

/**
 * 跑一次结构化建议抽取。不给模型**也有用**：确定性预筛仍会跑出
 * `triggeredKind`，用户据此决定要不要配模型重跑 —— 而不是对着空列表猜。
 */
export async function generateWikiSuggestions(
    pageId: string,
    modelId?: number,
): Promise<SuggestionGenerateResult> {
    const res = await httpClient.post<SuggestionGenerateResult>(
        `${PREFIX}/pages/${pageId}/suggestions`,
        { modelId: modelId ?? null },
    );
    return res.data;
}

/**
 * 接受一条建议。
 *
 * **终态不可逆**：重复接受、或接受后再拒绝都返回 409 —— 接受会物化出可执行
 * 规则/流程草稿，静默翻转状态会让建议与产物脱节。调用方必须把 409 当正常
 * 业务反馈展示（「这条已被处置」），而不是当系统故障。
 */
export async function acceptWikiSuggestion(
    suggestionId: number,
): Promise<StructureSuggestion> {
    const res = await httpClient.post<StructureSuggestion>(
        `${PREFIX}/suggestions/${suggestionId}/accept`,
    );
    return res.data;
}

export async function rejectWikiSuggestion(
    suggestionId: number,
): Promise<StructureSuggestion> {
    const res = await httpClient.post<StructureSuggestion>(
        `${PREFIX}/suggestions/${suggestionId}/reject`,
    );
    return res.data;
}
