/**
 * 知识图谱分析（Phase 2）API。
 *
 * 端点：
 *   GET  /api/v1/wiki/graph/relevance              — 两页相关性（4-Signal 分解）
 *   GET  /api/v1/wiki/graph/communities            — Louvain 社区列表
 *   POST /api/v1/wiki/graph/communities/recompute  — 全量重算社区（幂等）
 *   GET  /api/v1/wiki/graph/view                   — 图可视化载荷（节点 + 边）
 */

import { httpClient } from "./client";
import type {
    CommunityRecomputeResult,
    GraphInsights,
    GraphInsightsRescanResult,
    GraphRelevance,
    GraphView,
    KnowledgeCommunity,
} from "../types/wikiGraph";

const PREFIX = "/wiki/graph";

export async function getGraphRelevance(
    pageA: string,
    pageB: string,
): Promise<GraphRelevance> {
    const res = await httpClient.get<GraphRelevance>(`${PREFIX}/relevance`, {
        params: { pageA, pageB },
    });
    return res.data;
}

export async function listGraphCommunities(): Promise<KnowledgeCommunity[]> {
    const res = await httpClient.get<KnowledgeCommunity[]>(`${PREFIX}/communities`);
    return res.data;
}

/** 全量重算 Louvain 社区。限流与 detect/discover 同档，前端要禁连点。 */
export async function recomputeGraphCommunities(): Promise<CommunityRecomputeResult> {
    const res = await httpClient.post<CommunityRecomputeResult>(
        `${PREFIX}/communities/recompute`,
    );
    return res.data;
}

/** 图可视化载荷。默认不含孤立页（includeIsolated=true 才带）。 */
export async function getGraphView(includeIsolated = false): Promise<GraphView> {
    const res = await httpClient.get<GraphView>(`${PREFIX}/view`, {
        params: { includeIsolated },
    });
    return res.data;
}

/** 读取最新 Graph Insights（拓扑 + LLM 解读）。未扫描过返回三类空列表。 */
export async function getGraphInsights(): Promise<GraphInsights> {
    const res = await httpClient.get<GraphInsights>(`${PREFIX}/insights`);
    return res.data;
}

/** 全量重算 Graph Insights（含 LLM 解读）。限流与 detect/discover 同档。 */
export async function rescanGraphInsights(): Promise<GraphInsightsRescanResult> {
    const res = await httpClient.post<GraphInsightsRescanResult>(
        `${PREFIX}/insights/rescan`,
    );
    return res.data;
}

// ---------------------------------------------------------------------------
// Phase 5.5.3：SPARSE_COMMUNITY 缺口的两步预览（主题建议 + 写库）
// ---------------------------------------------------------------------------

/**
 * 主题预览（不写库）。用户确认后用 {@link updateCommunityTopic} 写入 topic。
 */
export interface WikiCommunityTopicPreview {
    topic: string;
    pageCount: number;
    pageTitles: string[];
}

export async function previewCommunityTopic(
    communityKey: string,
): Promise<WikiCommunityTopicPreview> {
    const res = await httpClient.post<WikiCommunityTopicPreview>(
        `${PREFIX}/communities/${encodeURIComponent(communityKey)}/topic-suggest`,
    );
    return res.data;
}

/**
 * 写社区主题（人工或 LLM 建议确认后）。``topic=null`` 显式清空；body 不含
 * ``topic`` 字段则保持原值（no-op）。
 */
export async function updateCommunityTopic(
    communityKey: string,
    topic: string | null,
    options: { onlyIfSet?: boolean } = {},
): Promise<KnowledgeCommunity> {
    const body: { topic?: string | null } = {};
    if (!options.onlyIfSet) {
        body.topic = topic;
    }
    const res = await httpClient.patch<{ community: KnowledgeCommunity }>(
        `${PREFIX}/communities/${encodeURIComponent(communityKey)}`,
        body,
    );
    return res.data.community;
}
