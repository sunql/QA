/**
 * 结构化建议（机制 4）类型，对齐后端 `app/domain/wiki_schemas.py`。
 */

export const SUGGESTION_STATUSES = [
    "PENDING",
    "ACCEPTED",
    "REJECTED",
] as const;
export type SuggestionStatus = (typeof SUGGESTION_STATUSES)[number];

/** 建议的目标结构类型（由确定性预筛命中，见 structure_suggester）。 */
export type SuggestedKind = "RULE" | "PROCESS" | "METRIC" | "CONCEPT";

export interface StructureSuggestion {
    id: number;
    pageId: string;
    /**
     * 只有「建议工作台」（跨条目列表）才回填标题；条目详情里的子列表为 null。
     * 这不是可选装饰：没有标题的列表，审核人无法判断该不该接受。
     */
    pageTitle: string | null;
    suggestedDimension: string;
    /**
     * 形状随 `suggestedDimension` 变化，故是自由 JSON —— 形状来自 LLM，
     * DTO 约束不住，真正的校验在 structure_suggester 里做。
     */
    extractedStructure: Record<string, unknown> | null;
    confidence: number | null;
    status: SuggestionStatus;
    suggestedAt: string | null;
    resolvedAt: string | null;
    resolvedByUserId: number | null;
}

export interface WikiSuggestionList {
    rows: StructureSuggestion[];
    total: number;
}

/** 抽取结果；triggeredKind 为 null = 没命中任何触发器（因此压根没调模型）。 */
export interface SuggestionGenerateResult {
    suggestions: StructureSuggestion[];
    total: number;
    triggeredKind: SuggestedKind | null;
    extractionStatus: string;
}
