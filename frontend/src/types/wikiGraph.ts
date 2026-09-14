/** 知识图谱分析（Phase 2）类型定义，与后端 wiki_schemas 对齐。 */

/** 两页相关性的 4-Signal 分解 */
export interface GraphRelevance {
    pageA: string;
    pageB: string;
    total: number;
    directLink: boolean;
    sourceOverlap: boolean;
    adamicAdar: number;
    typeAffinity: boolean;
}

/** Louvain 社区（已落库形态） */
export interface KnowledgeCommunity {
    id: number;
    communityKey: string;
    name: string;
    cohesionScore: number;
    pageCount: number;
    topPages: Array<{ pageId: string; title: string; degree: number }>;
    topic: string | null;
    createdTime: string;
}

/** 社区重算结果摘要 */
export interface CommunityRecomputeResult {
    communityCount: number;
    memberPageCount: number;
}

/** 图可视化节点。communityKey 为 null = 不属于任何社区（孤立页） */
export interface GraphNode {
    pageId: string;
    title: string | null;
    dimension: string | null;
    degree: number;
    communityKey: string | null;
}

/** 图可视化边：已确认 Page↔Page 关系 + 相关性得分 */
export interface GraphEdge {
    source: string;
    target: string;
    relationType: string;
    score: number;
}

/** 图可视化载荷 */
export interface GraphView {
    nodes: GraphNode[];
    edges: GraphEdge[];
    truncated: boolean;
}

/** Graph Insights（Phase 3） */

export type KnowledgeGapKind =
    | "ISOLATED_PAGE"
    | "MISSING_DIMENSION"
    | "SPARSE_COMMUNITY";

export interface SurprisingConnection {
    key: string;
    headline: string;
    sourcePageId: string;
    sourceTitle: string;
    sourceCommunity: string | null;
    sourceDimension: string | null;
    targetPageId: string;
    targetTitle: string;
    targetCommunity: string | null;
    targetDimension: string | null;
    relationType: string;
    explanation: string;
}

export interface KnowledgeGap {
    key: string;
    kind: KnowledgeGapKind;
    headline: string;
    explanation: string;
    communityKey: string | null;
    communityName: string | null;
    pageCount: number | null;
    cohesionScore: number | null;
    pageId: string | null;
    title: string | null;
    degree: number | null;
    topic: string | null;
}

export interface BridgeNode {
    key: string;
    pageId: string;
    title: string;
    degree: number;
    communities: string[];
    explanation: string;
}

export interface GraphInsights {
    surprisingConnections: SurprisingConnection[];
    knowledgeGaps: KnowledgeGap[];
    bridgeNodes: BridgeNode[];
    scannedAt: string;
}

export interface GraphInsightsRescanResult {
    surprisingCount: number;
    gapCount: number;
    bridgeCount: number;
    explanationAttempts: number;
    explanationFailures: number;
}
