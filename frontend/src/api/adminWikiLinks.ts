/**
 * Wiki ↔ Ontology 链接管理 API。
 *
 * 端点：
 *   GET    /api/v1/admin/wiki-links            — 列表（page_id / ontology_type 过滤）
 *   POST   /api/v1/admin/wiki-links             — 创建
 *   DELETE /api/v1/admin/wiki-links/:id         — 撤销
 *   PATCH  /api/v1/admin/wiki-links/:id         — 更新 weight / note
 *   GET    /api/v1/admin/wiki-links/linkables   — 可链接目标搜索
 */

import { authHeaders } from "./authHeaders";
import type {
  WikiLink,
  WikiLinkableTarget,
  WikiLinkType,
  CreateWikiLinkRequest,
} from "../types/wikiLink";

const BASE = "/api/v1/admin/wiki-links";

export async function listWikiLinks(
  filter: Partial<{ page_id: string; ontology_type: string }> = {},
): Promise<WikiLink[]> {
  const qs = new URLSearchParams(
    filter as Record<string, string>,
  ).toString();
  const resp = await fetch(`${BASE}${qs ? `?${qs}` : ""}`, {
    headers: authHeaders(),
  });
  return resp.json();
}

export async function createWikiLink(
  body: CreateWikiLinkRequest,
): Promise<WikiLink> {
  const resp = await fetch(BASE, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
  return resp.json();
}

export async function revokeWikiLink(id: number): Promise<WikiLink> {
  const resp = await fetch(`${BASE}/${id}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  return resp.json();
}

export async function updateWikiLink(
  id: number,
  body: Partial<CreateWikiLinkRequest>,
): Promise<WikiLink> {
  const resp = await fetch(`${BASE}/${id}`, {
    method: "PATCH",
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
  return resp.json();
}

export async function listLinkableTargets(
  type: WikiLinkType,
  query?: string,
): Promise<WikiLinkableTarget[]> {
  const qs = new URLSearchParams({
    type,
    ...(query ? { q: query } : {}),
  }).toString();
  const resp = await fetch(`${BASE}/linkables?${qs}`, {
    headers: authHeaders(),
  });
  return resp.json();
}
