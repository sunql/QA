/**
 * 冲突检测（机制 3）类型，对齐后端 `app/domain/wiki_schemas.py`。
 */

/** 冲突四类。GAP/OVERLAP 是「缺」与「重」，不是错，但一样要人处置。 */
export const CONFLICT_TYPES = [
    "CONTRADICTION",
    "STALENESS",
    "GAP",
    "OVERLAP",
] as const;
export type ConflictType = (typeof CONFLICT_TYPES)[number];

export const CONFLICT_SEVERITIES = [
    "CRITICAL",
    "HIGH",
    "MEDIUM",
    "LOW",
] as const;
export type ConflictSeverity = (typeof CONFLICT_SEVERITIES)[number];

/**
 * 处置动作白名单（与后端 `wiki_conflict_service._ACTION_TO_FEEDBACK` 同表）。
 *
 * `IGNORED` 唯一表达「系统误报」，所以判误报时不能猜 —— 猜错会污染机制 3 的
 * 准确率统计（那个统计正是用来决定要不要改规则 / 改 prompt 的输入）。
 * `RESOLVED` 与 `MERGED` 都算「系统报得对」，区别是人工改了还是人工合了。
 */
export const CONFLICT_ACTIONS = ["RESOLVED", "MERGED", "IGNORED"] as const;
export type ConflictAction = (typeof CONFLICT_ACTIONS)[number];

/**
 * 列表的「状态」筛选轴。刻意只有这两种措辞：冲突没有「进行中」这类中间态，
 * 只有「还没处理」与「处理过了」。
 */
export const CONFLICT_LIST_STATUSES = ["OPEN", "RESOLVED"] as const;
export type ConflictListStatus = (typeof CONFLICT_LIST_STATUSES)[number];

export interface KnowledgeConflict {
    id: number;
    conflictType: ConflictType;
    pageIds: string[];
    description: string | null;
    severity: ConflictSeverity;
    /** RULE = 确定性三路找的（改规则）；LLM = 模型判的（改 prompt）。 */
    detectedBy: "RULE" | "LLM";
    autoDetectedAt: string | null;
    /** 为空 = 未解决。 */
    resolvedAt: string | null;
    resolutionAction: string | null;
    resolvedByUserId: number | null;
}

export interface WikiConflictList {
    rows: KnowledgeConflict[];
    total: number;
}

/** 跑一次检测；modelId 为空 = 只跑确定性三路，零 token 成本。 */
export interface ConflictDetectResult {
    conflicts: KnowledgeConflict[];
    total: number;
    /** 把「没要求跑模型」与「跑了没成」分开报，避免静默失败被读成「确实没矛盾」。 */
    llmStatus: string;
}
