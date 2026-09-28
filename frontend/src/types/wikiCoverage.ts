/**
 * 覆盖度自感知（机制 6）类型，对齐后端 `app/domain/wiki_schemas.py`。
 */

/**
 * 一格的覆盖状态。**只有 APPROVED/EFFECTIVE 的条目才让格子变绿**（见
 * coverage_tracker）：宁可先全红，也不要假绿 —— 假绿会让看板失去信号价值。
 */
export const COVERAGE_STATUSES = [
    "COMPLETE",
    "PARTIAL",
    "OUTDATED",
    "MISSING",
] as const;
export type CoverageStatus = (typeof COVERAGE_STATUSES)[number];

/** 未标业务域的哨兵值（不是 null：UNIQUE 约束下 null 不参与去重）。 */
export const UNASSIGNED = "UNASSIGNED";

export interface CoverageCell {
    dimension: string;
    ontologyClassId: number;
    className: string;
    domain: string;
    pageCount: number;
    approvedCount: number;
    coverageStatus: CoverageStatus;
    lastRefreshedAt: string;
}

export interface CoverageSummary {
    totalCells: number;
    /** 用字典而非固定四字段：状态词表未来可能增补，写死字段会变成契约变更。 */
    byStatus: Record<string, number>;
    unassignedCells: number;
}

/** 「有维度但没挂到任何已确认业务对象」—— 矩阵看不见，却常是初始期最大的缺口。 */
export interface CoverageUnlinked {
    gapType: string;
    pageCount: number;
    dimensions: Record<string, number>;
}

export interface CoverageGap {
    dimension: string;
    ontologyClassId: number | null;
    className: string | null;
    domain: string;
    status: CoverageStatus;
    pageCount: number;
    approvedCount: number;
}

/** 看板一次取齐：汇总 + 缺口 + 孤儿条目（三块同时渲染，故合成一个响应）。 */
export interface CoverageOverview {
    summary: CoverageSummary;
    gaps: CoverageGap[];
    unlinked: CoverageUnlinked;
}

export interface CoverageRefreshResult {
    cellCount: number;
    classCount: number;
    unmappedClassCount: number;
    removedCount: number;
    refreshedAt: string;
}

export interface ClassDomainMapping {
    ontologyClassId: number;
    className: string;
    domain: string;
    createdByUserId: number | null;
    createdTime: string;
}
