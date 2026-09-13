/**
 * 知识条目（机制 1/2 的落地面）API。
 *
 * 端点：
 *   GET    /api/v1/wiki/pages                          — 分页列表（dimension/status 过滤）
 *   POST   /api/v1/wiki/pages                          — 新建条目
 *   GET    /api/v1/wiki/pages/{pageId}                 — 详情
 *   PATCH  /api/v1/wiki/pages/{pageId}                 — 局部更新
 *   DELETE /api/v1/wiki/pages/{pageId}                 — 删除（204）
 *   POST   /api/v1/wiki/pages/batch-delete             — 批量删除（200 + 逐条结果）
 *   POST   /api/v1/wiki/pages/{pageId}/reclassify      — 调整/确认/打回分类
 *   GET    /api/v1/wiki/pages/{pageId}/relations       — 传出关系
 *   POST   /api/v1/wiki/pages/{pageId}/relations/discover — 机制 2 发现候选
 *   POST   /api/v1/wiki/relations/{relationId}/confirm|reject — 关系审核
 */

import { httpClient } from "./client";
import type {
    KnowledgeClaim,
    WikiPage,
    WikiPageBatchDeleteResult,
    WikiPageCreate,
    WikiPageList,
    WikiPageUpdate,
    WikiReclassifyResult,
    WikiRelation,
    WikiRelationDiscoverResult,
    KnowledgeDimension,
    WikiPageStatus,
} from "../types/wikiPages";

const PREFIX = "/wiki";

export interface ListWikiPagesParams {
    dimension?: KnowledgeDimension;
    status?: WikiPageStatus;
    limit?: number;
    offset?: number;
}

export async function listWikiPages(
    params: ListWikiPagesParams = {},
): Promise<WikiPageList> {
    const res = await httpClient.get<WikiPageList>(`${PREFIX}/pages`, {
        // 空串会被后端当成「按空值过滤」而非「不过滤」，故只在有值时下发
        params: {
            dimension: params.dimension,
            status: params.status,
            limit: params.limit ?? 20,
            offset: params.offset ?? 0,
        },
    });
    return res.data;
}

export async function createWikiPage(
    payload: WikiPageCreate,
): Promise<WikiPage> {
    const res = await httpClient.post<WikiPage>(`${PREFIX}/pages`, payload);
    return res.data;
}

export async function getWikiPage(pageId: string): Promise<WikiPage> {
    const res = await httpClient.get<WikiPage>(`${PREFIX}/pages/${pageId}`);
    return res.data;
}

export async function updateWikiPage(
    pageId: string,
    payload: WikiPageUpdate,
): Promise<WikiPage> {
    const res = await httpClient.patch<WikiPage>(
        `${PREFIX}/pages/${pageId}`,
        payload,
    );
    return res.data;
}

export async function deleteWikiPage(pageId: string): Promise<void> {
    await httpClient.delete(`${PREFIX}/pages/${pageId}`);
}

/**
 * 批量删除知识条目。
 *
 * 刻意用 POST 而非 `DELETE` 带 body：DELETE 的请求体在部分代理/客户端上会被
 * 丢掉，而 pageIds 正是「要删哪些」的全部信息，丢了就变成删错或删不掉。
 *
 * 后端是**部分成功**语义（不存在的 id 回在 `notFound` 里，其余照删），
 * 所以调用方必须读返回值，不能只看 Promise 有没有 reject。
 */
export async function batchDeleteWikiPages(
    pageIds: string[],
): Promise<WikiPageBatchDeleteResult> {
    const res = await httpClient.post<WikiPageBatchDeleteResult>(
        `${PREFIX}/pages/batch-delete`,
        { pageIds },
    );
    return res.data;
}

/**
 * 处置机制 1 的分类结论。
 *
 * `dimension` 是**必传键**（可以是 null）：漏传与「显式传 null」在后端语义
 * 相反 —— 前者是调用方写错，后者是「打回这个分类」。所以这里不做
 * `undefined` 的剔除处理，调用方必须显式表态。
 */
export async function reclassifyWikiPage(
    pageId: string,
    dimension: KnowledgeDimension | null,
): Promise<WikiReclassifyResult> {
    const res = await httpClient.post<WikiReclassifyResult>(
        `${PREFIX}/pages/${pageId}/reclassify`,
        { dimension },
    );
    return res.data;
}

export async function listWikiRelations(
    pageId: string,
    confirmedOnly = false,
): Promise<WikiRelation[]> {
    const res = await httpClient.get<WikiRelation[]>(
        `${PREFIX}/pages/${pageId}/relations`,
        { params: { confirmedOnly } },
    );
    return res.data;
}

/**
 * 跑一次关系发现。`modelId` 为空只跑确定性路径（零 token 成本）；
 * 给了模型才额外跑 LLM 实体抽取。
 */
export async function discoverWikiRelations(
    pageId: string,
    modelId?: number,
): Promise<WikiRelationDiscoverResult> {
    const res = await httpClient.post<WikiRelationDiscoverResult>(
        `${PREFIX}/pages/${pageId}/relations/discover`,
        { modelId: modelId ?? null },
    );
    return res.data;
}

export async function confirmWikiRelation(
    relationId: number,
): Promise<WikiRelation> {
    const res = await httpClient.post<WikiRelation>(
        `${PREFIX}/relations/${relationId}/confirm`,
    );
    return res.data;
}

export async function rejectWikiRelation(
    relationId: number,
): Promise<WikiRelation> {
    const res = await httpClient.post<WikiRelation>(
        `${PREFIX}/relations/${relationId}/reject`,
    );
    return res.data;
}

export async function listWikiClaims(pageId: string): Promise<KnowledgeClaim[]> {
    const res = await httpClient.get<KnowledgeClaim[]>(
        `${PREFIX}/pages/${pageId}/claims`,
    );
    return res.data;
}

/**
 * 跑一次事实原子抽取（机制 6，幂等：已抽过则 ALREADY_DONE）。
 *
 * 抽取由后端完整负责：含 LLM 调用、evidence 落库、token 计量。
 * 前端只等结果 + 刷新列表；不传 modelId 表示「本次不跑 LLM」
 * （实际生产环境一般必传，模型是知识质量的核心）。
 * force=true 跳过幂等保护：先删除已有 claims 再重抽（换模型/内容修订场景）。
 */
export async function extractWikiClaims(
    pageId: string,
    modelId?: number,
    force?: boolean,
): Promise<{ status: string; claimCount: number }> {
    const res = await httpClient.post<{ status: string; claimCount: number }>(
        `${PREFIX}/pages/${pageId}/claims/extract`,
        { modelId: modelId ?? null, force: force ?? false },
    );
    return res.data;
}
