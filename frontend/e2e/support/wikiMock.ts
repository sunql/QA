// =============================================================================
// E2E mock 后端：Wiki 域（分类树 / 页面 / 链接）。
// 注意：真后端的 list_links / list_linkables 返回 raw array（无信封），
// 前端 api 客户端不解包，mock 必须跟真后端契约一致。
// =============================================================================

import type { Route } from "@playwright/test";
import { NOW, fail, respondJson, type BackendCore, type RouteCtx } from "./mockShared";
import type { OntologyState } from "./entityMock";

// ===== Wiki 内存实体类型 =====

export interface MockWikiLink {
  id: number;
  page_id: string;
  chunk_id: string | null;
  ontology_type: "class" | "property";
  ontology_id: number;
  weight: number;
  note: string | null;
  created_by: number;
  revoked_time: string | null;
}

/** feat-wiki-category：分类树节点（递归）。mock 端保持与后端 WikiCategoryRead 形状一致。 */
export interface MockWikiCategory {
  id: number;
  parent_id: number | null;
  sort_order: number;
  name: string;
  description: string | null;
  page_id: string | null;
  children: MockWikiCategory[];
}

/** feat-wiki-category：wiki_page 简化 mock（mockApi 不关心 content 等大字段）。 */
export interface MockWikiPage {
  id: number;
  page_id: string;
  title: string;
  dimension: string | null;
  status: string;
  category_id: number | null;
}


/** Wiki 域状态切片（链接的可选目标复用 ontology 的 class/property）。 */
export interface WikiState extends OntologyState, BackendCore {
  wikiLinks: MockWikiLink[];
  wikiCategories: MockWikiCategory[];
  wikiPages: MockWikiPage[];
}

// ===== Seed 数据模板 =====

const SEED_WIKI_LINKS: MockWikiLink[] = [
  // page-001「采购管理」已有 1 条 class 链接，page-002/003 空 — 让 spec 验证
  // 「选中不同页面 listWikiLinks 列表变化」与「撤销后端立即生效」两个差异点
  {
    id: 1,
    page_id: "page-001",
    chunk_id: null,
    ontology_type: "class",
    ontology_id: 1,
    weight: 0.8,
    note: "种子：采购 → 供应商类",
    created_by: 1,
    revoked_time: null,
  },
];

/** feat-wiki-category：seed 分类树（3 根 + 5 子，与后端 migration 0093 seed 对齐）。 */
const SEED_WIKI_CATEGORIES: MockWikiCategory[] = [
  {
    id: 1,
    parent_id: null,
    sort_order: 0,
    name: "采购管理",
    description: null,
    page_id: null,
    children: [
      {
        id: 4,
        parent_id: 1,
        sort_order: 0,
        name: "供应商准入流程",
        description: null,
        page_id: "page-001-01",
        children: [],
      },
      {
        id: 5,
        parent_id: 1,
        sort_order: 1,
        name: "采购订单执行",
        description: null,
        page_id: "page-001-02",
        children: [],
      },
    ],
  },
  {
    id: 2,
    parent_id: null,
    sort_order: 1,
    name: "质量管理",
    description: null,
    page_id: null,
    children: [
      {
        id: 6,
        parent_id: 2,
        sort_order: 0,
        name: "IQC 来料检验",
        description: null,
        page_id: "page-002-01",
        children: [],
      },
    ],
  },
  {
    id: 3,
    parent_id: null,
    sort_order: 2,
    name: "仓储物流",
    description: null,
    page_id: null,
    children: [
      {
        id: 7,
        parent_id: 3,
        sort_order: 0,
        name: "入库作业",
        description: null,
        page_id: "page-003-01",
        children: [],
      },
      {
        id: 8,
        parent_id: 3,
        sort_order: 1,
        name: "出库配送",
        description: null,
        page_id: "page-003-02",
        children: [],
      },
    ],
  },
];

/** feat-wiki-category：seed wiki_pages（与原 stub page_id 对齐）。 */
const SEED_WIKI_PAGES: MockWikiPage[] = [
  {
    id: 101,
    page_id: "page-001",
    title: "采购管理（顶层 page）",
    dimension: "PROCESS",
    status: "EFFECTIVE",
    category_id: 1,
  },
  {
    id: 102,
    page_id: "page-001-01",
    title: "供应商准入流程",
    dimension: "PROCESS",
    status: "EFFECTIVE",
    category_id: 4,
  },
  {
    id: 103,
    page_id: "page-001-02",
    title: "采购订单执行",
    dimension: "PROCESS",
    status: "EFFECTIVE",
    category_id: 5,
  },
  {
    id: 104,
    page_id: "page-002",
    title: "质量管理（顶层 page）",
    dimension: "PROCESS",
    status: "EFFECTIVE",
    category_id: 2,
  },
  {
    id: 105,
    page_id: "page-002-01",
    title: "IQC 来料检验",
    dimension: "PROCESS",
    status: "EFFECTIVE",
    category_id: 6,
  },
  {
    id: 106,
    page_id: "page-003-01",
    title: "入库作业",
    dimension: "PROCESS",
    status: "EFFECTIVE",
    category_id: 7,
  },
  {
    id: 107,
    page_id: "page-003-02",
    title: "出库配送",
    dimension: "PROCESS",
    status: "EFFECTIVE",
    category_id: 8,
  },
];


function deepCloneCategory(c: MockWikiCategory): MockWikiCategory {
  return {
    ...c,
    children: c.children.map(deepCloneCategory),
  };
}


// ===== Wiki 路由处理器 =====

export async function handleWikiCategoryRoutes(route: Route, ctx: RouteCtx<WikiState>): Promise<void> {
  const { method, path, body, backend } = ctx;

  // GET /wiki/categories/tree —— 全量分类树
  if (path === "/wiki/categories/tree" && method === "GET") {
    return respondJson(route, 200, backend.wikiCategories.map(deepCloneCategory));
  }

  const idMatch = path.match(/^\/wiki\/categories\/(\d+)$/);
  if (idMatch) {
    const id = Number(idMatch[1]);
    if (method === "PATCH") {
      const idx = backend.wikiCategories.findIndex((c) => c.id === id);
      if (idx < 0) return respondJson(route, 404, fail(`category ${id} not found`));
      const patch = (body ?? {}) as Partial<MockWikiCategory>;
      const updated: MockWikiCategory = {
        ...backend.wikiCategories[idx],
        ...patch,
        id: backend.wikiCategories[idx].id,  // id 不可改
      };
      backend.wikiCategories = backend.wikiCategories.map((c) =>
        c.id === id ? updated : c,
      );
      return respondJson(route, 200, updated);
    }
    if (method === "DELETE") {
      backend.wikiCategories = backend.wikiCategories.filter((c) => c.id !== id);
      return route.fulfill({ status: 204, body: "" });
    }
  }

  if (path === "/wiki/categories" && method === "POST") {
    const payload = (body ?? {}) as Partial<MockWikiCategory>;
    const created: MockWikiCategory = {
      id: backend.nextId("wikiCategory"),
      parent_id: payload.parent_id ?? null,
      sort_order: payload.sort_order ?? 0,
      name: payload.name ?? "",
      description: payload.description ?? null,
      page_id: payload.page_id ?? null,
      children: [],
    };
    backend.wikiCategories = [...backend.wikiCategories, created];
    return respondJson(route, 201, created);
  }

  return respondJson(route, 404, fail(`mockApi: 未实现 ${method} ${path}`));
}

export async function handleWikiPageRoutes(route: Route, ctx: RouteCtx<WikiState>): Promise<void> {
  const { method, path, query, body, backend } = ctx;

  // GET /wiki/pages —— 分页列表（mock 不分页，全量返回）
  if (path === "/wiki/pages" && method === "GET") {
    const limit = Number(query.get("limit") ?? "50");
    const rows = backend.wikiPages.slice(0, limit);
    return respondJson(route, 200, { rows, total: backend.wikiPages.length });
  }

  // PATCH /wiki/pages/{pageId} —— mock 只更新 category_id
  const idMatch = path.match(/^\/wiki\/pages\/([^/]+)$/);
  if (idMatch && method === "PATCH") {
    const pageId = idMatch[1];
    const idx = backend.wikiPages.findIndex((p) => p.page_id === pageId);
    if (idx < 0) return respondJson(route, 404, fail(`page ${pageId} not found`));
    const patch = (body ?? {}) as Partial<MockWikiPage>;
    const updated: MockWikiPage = {
      ...backend.wikiPages[idx],
      ...patch,
      page_id: backend.wikiPages[idx].page_id,  // 主键不可改
    };
    backend.wikiPages = backend.wikiPages.map((p) =>
      p.page_id === pageId ? updated : p,
    );
    return respondJson(route, 200, updated);
  }

  return respondJson(route, 404, fail(`mockApi: 未实现 ${method} ${path}`));
}

export async function handleWikiLinkRoutes(
  route: Route,
  ctx: RouteCtx<WikiState>,
): Promise<void> {
  const { method, path } = ctx;
  // 链接目标搜索（type 必填，q 可选）—— 必须早于 /admin/wiki-links/:id 匹配
  if (path === "/admin/wiki-links/linkables" && method === "GET") {
    return handleWikiLinkTargetsRoutes(route, ctx);
  }
  if (path === "/admin/wiki-links" && method === "GET") {
    return handleWikiLinkListRoutes(route, ctx);
  }
  if (path === "/admin/wiki-links" && method === "POST") {
    return handleWikiLinkCreateRoutes(route, ctx);
  }
  return handleWikiLinkItemRoutes(route, ctx);
}

async function handleWikiLinkTargetsRoutes(
  route: Route,
  ctx: RouteCtx<WikiState>,
): Promise<void> {
  const { query, backend } = ctx;
  const type = query.get("type") === "property" ? "property" : "class";
  // 复用 ontology class/property 种子做「可选目标」—— 与真后端 listLinkableTargets
  // 同源：class 用 backend.classes，property 用 backend.properties
  const targets =
    type === "class"
      ? backend.classes.map((c) => ({
          id: c.id,
          type: "class" as const,
          name: c.className,
          alias: c.classAlias,
          description: c.description,
        }))
      : backend.properties.map((p) => ({
          id: p.id,
          type: "property" as const,
          name: p.propertyName,
          alias: p.propertyAlias,
          description: null,
        }));
  // 注意：真后端 list_linkables 返回 raw array（无信封），前端 api 客户端不
  // 解包，page 直接 setLinkables(targets)。mock 必须跟真后端契约一致。
  return respondJson(route, 200, targets);
}

async function handleWikiLinkListRoutes(
  route: Route,
  ctx: RouteCtx<WikiState>,
): Promise<void> {
  const { query, backend } = ctx;
  const pageIdRaw = query.get("page_id");
  const pageId = pageIdRaw ? String(pageIdRaw) : null;
  const filtered = pageId
    ? backend.wikiLinks.filter((w) => w.page_id === pageId)
    : backend.wikiLinks;
  // 真后端 list_links 返回 raw array，无信封
  return respondJson(route, 200, filtered);
}

async function handleWikiLinkCreateRoutes(
  route: Route,
  ctx: RouteCtx<WikiState>,
): Promise<void> {
  const { body, backend } = ctx;
  const created: MockWikiLink = {
    id: backend.nextId("wikiLink"),
    page_id: String(body.page_id ?? ""),
    chunk_id: body.chunk_id ? String(body.chunk_id) : null,
    ontology_type: body.ontology_type === "property" ? "property" : "class",
    ontology_id: Number(body.ontology_id ?? 0),
    weight: typeof body.weight === "number" ? body.weight : 1,
    note: body.note ? String(body.note) : null,
    created_by: 1,
    revoked_time: null,
  };
  backend.wikiLinks = [...backend.wikiLinks, created];
  return respondJson(route, 200, created);
}

export async function handleWikiLinkItemRoutes(
  route: Route,
  ctx: RouteCtx<WikiState>,
): Promise<void> {
  const { method, path, body, backend } = ctx;
  // 撤销 / 更新：/admin/wiki-links/:id
  const idMatch = path.match(/^\/admin\/wiki-links\/(\d+)$/);
  if (!idMatch) return;
  const id = Number(idMatch[1]);
  const target = backend.wikiLinks.find((w) => w.id === id);
  if (method === "DELETE") {
    if (!target) return respondJson(route, 404, fail("链接不存在"));
    backend.wikiLinks = backend.wikiLinks.map((w) =>
      w.id === id ? { ...w, revoked_time: NOW } : w,
    );
    return respondJson(route, 200, { ...target, revoked_time: NOW });
  }
  if (method === "PATCH" && target) {
    const updated: MockWikiLink = {
      ...target,
      weight: typeof body.weight === "number" ? body.weight : target.weight,
      note: body.note !== undefined ? (body.note ? String(body.note) : null) : target.note,
    };
    backend.wikiLinks = backend.wikiLinks.map((w) => (w.id === id ? updated : w));
    return respondJson(route, 200, updated);
  }
  return respondJson(route, 404, fail("wiki-link 路由不匹配"));
}


/** Wiki 域种子（深拷贝）。 */
export function wikiSeed(): Omit<WikiState, "nextId" | "classes" | "properties"> {
  return {
    wikiLinks: SEED_WIKI_LINKS.map((w) => ({ ...w })),
    wikiCategories: SEED_WIKI_CATEGORIES.map(deepCloneCategory),
    wikiPages: SEED_WIKI_PAGES.map((p) => ({ ...p })),
  };
}
