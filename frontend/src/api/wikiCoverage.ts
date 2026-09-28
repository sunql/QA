/**
 * 覆盖度自感知（机制 6）API。
 *
 * 端点：
 *   GET    /api/v1/wiki/coverage            — 矩阵（上一次刷新的快照）
 *   GET    /api/v1/wiki/coverage/overview   — 汇总 + 缺口 + 孤儿条目
 *   POST   /api/v1/wiki/coverage/refresh    — 全量重算（幂等，用于自愈）
 *   GET    /api/v1/wiki/coverage/domains    — 已用过的业务域词表
 *   GET    /api/v1/wiki/coverage/mappings   — 全部 class→domain 标注
 *   POST   /api/v1/wiki/coverage/mappings   — 新增标注（幂等）
 *   DELETE /api/v1/wiki/coverage/mappings   — 摘除标注（query 参数）
 */

import { httpClient } from "./client";
import type {
    ClassDomainMapping,
    CoverageCell,
    CoverageOverview,
    CoverageRefreshResult,
    CoverageStatus,
} from "../types/wikiCoverage";

const PREFIX = "/wiki/coverage";

export async function listCoverageCells(
    params: { domain?: string; dimension?: string; coverageStatus?: CoverageStatus } = {},
): Promise<CoverageCell[]> {
    const res = await httpClient.get<CoverageCell[]>(PREFIX, { params });
    return res.data;
}

/**
 * 看板一次取齐。三块数据是同时渲染的，后端合成一个响应正是为了避免前端并发
 * 三请求再拼装（那样三份数据可能来自不同刷新时刻）。
 */
export async function getCoverageOverview(
    params: { domain?: string; includeUnassigned?: boolean; gapLimit?: number } = {},
): Promise<CoverageOverview> {
    const res = await httpClient.get<CoverageOverview>(`${PREFIX}/overview`, {
        params: {
            domain: params.domain,
            includeUnassigned: params.includeUnassigned ?? false,
            gapLimit: params.gapLimit,
        },
    });
    return res.data;
}

/** 全量重算（幂等）。覆盖度是派生快照，重刷是自愈手段，不会「越刷越高」。 */
export async function refreshCoverage(): Promise<CoverageRefreshResult> {
    const res = await httpClient.post<CoverageRefreshResult>(
        `${PREFIX}/refresh`,
    );
    return res.data;
}

export async function listCoverageDomains(): Promise<string[]> {
    const res = await httpClient.get<{ domains: string[] }>(
        `${PREFIX}/domains`,
    );
    return res.data.domains;
}

export async function listDomainMappings(): Promise<ClassDomainMapping[]> {
    const res = await httpClient.get<ClassDomainMapping[]>(
        `${PREFIX}/mappings`,
    );
    return res.data;
}

export async function createDomainMapping(
    ontologyClassId: number,
    domain: string,
): Promise<ClassDomainMapping> {
    const res = await httpClient.post<ClassDomainMapping>(
        `${PREFIX}/mappings`,
        { ontologyClassId, domain },
    );
    return res.data;
}

/**
 * 摘掉一个域标注。
 *
 * 参数走 query 而非请求体：DELETE 带 body 在部分代理/客户端上会被丢掉，而
 * 这两个参数正是「要删哪一行」的全部信息 —— 丢了就变成删错行或删不掉。
 * 目标不存在时后端 404，不静默成功。
 */
export async function deleteDomainMapping(
    ontologyClassId: number,
    domain: string,
): Promise<void> {
    await httpClient.delete(`${PREFIX}/mappings`, {
        params: { ontologyClassId, domain },
    });
}
