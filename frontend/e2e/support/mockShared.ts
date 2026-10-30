// =============================================================================
// E2E mock 后端共享原语：响应信封、路径解析、请求上下文类型、实体种类。
// 各域 mock 模块（entityMock / wikiMock / appMock / orchestrationMock）共用，
// 保证所有 mock 域产出的响应契约完全一致。
// =============================================================================

import type { Route } from "@playwright/test";

// 内存实体的自增 id 类别（与后端实体一一对应）。
export type EntityKind =
  | "datasource"
  | "class"
  | "property"
  | "metric"
  | "lineageEdge"
  | "wikiLink"
  | "wikiCategory"
  | "wikiPage";

/** 固定的种子时间戳，保证 seed 数据在多次运行间一致。 */
export const NOW = "2026-08-01T00:00:00";

/** 所有 mock 域共享的内存状态骨架（自增 id 分配器）。 */
export interface BackendCore {
  nextId: (kind: EntityKind) => number;
}

/**
 * 各 mock 处理器共享的请求上下文。
 * backend 为该处理器关心的状态切片：每个域只声明自己读写的字段，
 * 由 mockApi 的 MockBackend（各切片之和）传入。
 */
export interface RouteCtx<TBackend> {
  backend: TBackend;
  method: string;
  path: string;
  query: URLSearchParams;
  body: Record<string, unknown>;
}

export function ok(data: unknown): Record<string, unknown> {
  return { success: true, data, error: null, timestamp: new Date().toISOString() };
}

export function fail(error: string): Record<string, unknown> {
  return { success: false, data: null, error, timestamp: new Date().toISOString() };
}

export async function respondJson(route: Route, status: number, payload: unknown): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}


export function parsePath(url: string): string {
  return new URL(url).pathname.replace(/^\/api\/v1/, "");
}

export function parseQuery(url: string): URLSearchParams {
  return new URL(url).searchParams;
}

