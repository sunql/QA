/**
 * 知识条目相关类型（feat-wiki-knowledge M7 前端）。
 * 字段与后端 `app/domain/wiki_schemas.py` 的 CamelModel 一一对应
 * （后端 snake_case → JSON camelCase）。
 */

/** 知识维度（机制 1 自动填，业务专家可调整）。 */
export const KNOWLEDGE_DIMENSIONS = [
    "OBJECT",
    "RULE",
    "PROCESS",
    "CONCEPT",
    "METRIC",
    "POLICY",
    "DOCUMENT",
    "FAQ",
] as const;
export type KnowledgeDimension = (typeof KNOWLEDGE_DIMENSIONS)[number];

/** 条目生命周期状态。 */
export const WIKI_PAGE_STATUSES = [
    "DRAFT",
    "REVIEW",
    "APPROVED",
    "EFFECTIVE",
    "EXPIRED",
] as const;
export type WikiPageStatus = (typeof WIKI_PAGE_STATUSES)[number];

/** 结构演进阶段（机制 5）。 */
export type StructureStage =
    | "MARKDOWN"
    | "SEMI_STRUCTURED"
    | "FULLY_STRUCTURED";

/** 机制 1 自动分类的结论（dimension 是它给出的建议值）。 */
export interface AutoClassification {
    primary?: string;
    confidence?: number;
    alternatives?: { dimension: string; confidence: number }[];
}

export interface WikiPage {
    id: number;
    pageId: string;
    title: string;
    content: string;
    dimension: KnowledgeDimension | null;
    structureStage: StructureStage;
    autoClassification: AutoClassification | null;
    status: WikiPageStatus;
    authorityLevel: string | null;
    version: string;
    createdByUserId: number | null;
    validFrom: string | null;
    validTo: string | null;
    createdTime: string | null;
    updatedTime: string | null;
}

export interface WikiPageList {
    rows: WikiPage[];
    total: number;
}

export interface WikiPageCreate {
    title: string;
    content: string;
}

/** PATCH 语义：只传要改的字段（后端用 UNSET 哨兵区分「没传」与「传了 null」）。 */
export interface WikiPageUpdate {
    title?: string;
    content?: string;
    dimension?: KnowledgeDimension | null;
    status?: WikiPageStatus;
    authorityLevel?: string | null;
    version?: string;
}

/** 调整分类的结果（action 是本次记录的反馈动作）。 */
export interface WikiReclassifyResult {
    page: WikiPage;
    action: "CONFIRM" | "REJECT" | "MODIFY";
}

/** 批量删除的连带给删行数（后端删除前统计，只报数不回传内容）。 */
export interface WikiPageBatchDeleteCascade {
    claims: number;
    relations: number;
    suggestions: number;
    rules: number;
    workflows: number;
}

/**
 * 批量删除结果。
 *
 * `notFound` 非空**不是错误**（并发下别的用户先删了同一条很常见），
 * 后端也不会为此回滚整批 —— 调用方必须据它给用户提示，不能当成成功吞掉。
 */
export interface WikiPageBatchDeleteResult {
    /** 去重后的目标条数（入参有重复时与数组长度不同）。 */
    requested: number;
    deletedPageIds: string[];
    notFound: string[];
    cascade: WikiPageBatchDeleteCascade;
}

/** 关系目标类型：知识条目 / 本体类 / 指标 / 实体映射。 */
export type RelationTargetType =
    | "PAGE"
    | "ONTOLOGY_CLASS"
    | "ONTOLOGY_METRIC"
    | "ENTITY_MAPPING";

/**
 * 知识关系三态由两个字段共同表达（见后端 KnowledgeRelationRead）：
 * 待审核 `confirmed=false, rejectedAt=null`；已确认 `true, null`；
 * 已打回 `false, 有时间戳`。
 */
export interface WikiRelation {
    id: number;
    upstreamPageId: string;
    downstreamType: RelationTargetType;
    downstreamId: string;
    relationType: string;
    confidence: number | null;
    autoDetected: boolean;
    confirmed: boolean;
    rejectedAt: string | null;
    createdTime: string | null;
}

/** 机制 2 的发现结果（classExtractionStatus 说明 LLM 那一路跑没跑、成没成）。 */
export interface WikiRelationDiscoverResult {
    total: number;
    candidates: WikiRelation[];
    classExtractionStatus: string;
}

/** 「有维度但没挂到任何已确认业务对象」—— 覆盖度看板用的缺口类型。 */
export const GAP_UNLINKED = "UNLINKED";

/** Evidence：Claim 的证据 5 元组。 */
export interface Evidence {
    id: number;
    claimId: number;
    sourceType: string;
    sourceId: string | null;
    pageNumber: string | null;
    sectionName: string | null;
    paragraphNo: string | null;
    content: string | null;
    createdTime: string | null;
}

/** KnowledgeClaim：从 WikiPage 抽取的事实原子。 */
export interface KnowledgeClaim {
    id: number;
    pageId: string;
    claimText: string;
    claimType: string | null;
    embeddingRef: string | null;
    createdTime: string | null;
    evidences: Evidence[];
}
