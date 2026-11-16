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

/**
 * 读取 JSON 响应；非 2xx 直接抛错。
 *
 * 为什么必须有这一步（2026-11-16 生产事故复盘）：
 * FastAPI 的 4xx/5xx 响应体**也是 JSON**（`{"detail": ...}`），
 * 直接 `resp.json()` 会 **resolve 而不是 reject** ⇒ 调用方永远进不了 catch
 * ⇒ 页面照样弹「添加成功」，但库里根本没有这条记录（静默假成功）。
 * 仓库其它裸 fetch 模块（document.ts / wikiChat.ts / chat.ts）都有这道判断。
 */
async function readJson<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}`);
  }
  return (await resp.json()) as T;
}

export async function listWikiLinks(
  filter: Partial<{ page_id: string; ontology_type: string }> = {},
): Promise<WikiLink[]> {
  const qs = new URLSearchParams(
    filter as Record<string, string>,
  ).toString();
  const resp = await fetch(`${BASE}${qs ? `?${qs}` : ""}`, {
    headers: authHeaders(),
  });
  return readJson<WikiLink[]>(resp);
}

export async function createWikiLink(
  body: CreateWikiLinkRequest,
): Promise<WikiLink> {
  const resp = await fetch(BASE, {
    method: "POST",
    // 裸 fetch 传字符串 body 时浏览器默认发 `text/plain;charset=UTF-8`
    // ⇒ FastAPI 不把 body 当 JSON 解析 ⇒ 任何 POST/PATCH 都 422。
    // 必须显式声明 JSON（同 chat.ts / document.ts / wikiChat.ts 的写法）。
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  return readJson<WikiLink>(resp);
}

export async function revokeWikiLink(id: number): Promise<WikiLink> {
  const resp = await fetch(`${BASE}/${id}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  return readJson<WikiLink>(resp);
}

export async function updateWikiLink(
  id: number,
  body: Partial<CreateWikiLinkRequest>,
): Promise<WikiLink> {
  const resp = await fetch(`${BASE}/${id}`, {
    method: "PATCH",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  return readJson<WikiLink>(resp);
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
  return readJson<WikiLinkableTarget[]>(resp);
}
