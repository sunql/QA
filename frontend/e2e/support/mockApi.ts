import type { Page, Route } from "@playwright/test";

// =============================================================================
// E2E 模拟后端（5.9）：用 page.route 拦截浏览器层所有 /api/v1 请求，
// 以内存数据 + 预构建响应体模拟后端。不依赖真实后端/DB，CI 可复现。
// 契约对齐 src/api/*：
//   - httpClient 解包 { success, data, error, timestamp } 信封
//   - testDataSource 走原始 axios，直接返回 { success, message }
//   - sendMessageStream 用 fetch 消费 text/event-stream 的 SSE 帧
// 5.9 审查修复：dispatch/seed 拆分至 <50 行（H1/H2）、properties GET 补全（M3）、
// 路由注册 .catch 兜底 500（M1）、信封补 timestamp（L2）、EntityKind 类型别名（L1）。
// =============================================================================

// ===== 内存实体（对齐 src/types/* 字段名） =====

export interface MockDatasource {
  id: number;
  name: string;
  type: "mysql" | "postgresql" | "oracle";
  host: string;
  port: number;
  databaseName: string;
  username: string;
  description: string | null;
  isActive: boolean;
  isDefault: boolean;
  createdBy: string | null;
  createdTime: string;
  updatedTime: string;
}

export interface MockClass {
  id: number;
  className: string;
  classAlias: string | null;
  description: string | null;
  sourceTable: string | null;
  parentClassId: number | null;
  createdBy: string | null;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface MockProperty {
  id: number;
  classId: number;
  propertyName: string;
  propertyAlias: string | null;
  dataType: string;
  isPrimaryKey: boolean;
  isForeignKey: boolean;
  refClassId: number | null;
  sourceColumn: string | null;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface MockMetric {
  id: number;
  metricName: string;
  metricAlias: string | null;
  formula: string;
  aggFunction: string;
  targetClassId: number | null;
  dimensionDefaults: Record<string, string> | null;
  createdBy: string | null;
  createdTime: string | null;
  updatedTime: string | null;
}

type EntityKind =
  | "datasource"
  | "class"
  | "property"
  | "metric"
  | "lineageEdge"
  | "wikiLink"
  | "wikiCategory"
  | "wikiPage";

// 后端暴露给 spec 断言/构造的内存存储（每个 page 独立实例）
export interface MockLineageEdge {
  id: number;
  sourceLayer: string;
  sourceSystem: string;
  sourceObject: string;
  sourceField: string | null;
  targetLayer: string;
  targetSystem: string;
  targetObject: string;
  targetField: string | null;
  transformationRule: string | null;
  refreshFrequency: string;
  owner: string | null;
  description: string | null;
  isActive: boolean;
  createdTime: string | null;
  updatedTime: string | null;
}

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

export interface MockBackend {
  page: Page;
  datasources: MockDatasource[];
  classes: MockClass[];
  properties: MockProperty[];
  metrics: MockMetric[];
  lineageEdges: MockLineageEdge[];
  wikiLinks: MockWikiLink[];
  wikiCategories: MockWikiCategory[];
  wikiPages: MockWikiPage[];
  nextId: (kind: EntityKind) => number;
}

// ===== Seed 数据模板：mockApi 每次调用深拷贝，保证 page 间隔离（fullyParallel 安全） =====

const NOW = "2026-08-01T00:00:00";

const SEED_DATASOURCES: MockDatasource[] = [
  {
    id: 1,
    name: "RuoYi-MySQL",
    type: "mysql",
    host: "127.0.0.1",
    port: 3306,
    databaseName: "ry_wms",
    username: "root",
    description: "演示库",
    isActive: true,
    isDefault: true,
    createdBy: "system",
    createdTime: NOW,
    updatedTime: NOW,
  },
  {
    id: 2,
    name: "ZJTH-Oracle",
    type: "oracle",
    host: "10.0.0.8",
    port: 1521,
    databaseName: "ZJTH",
    username: "app",
    description: null,
    isActive: true,
    isDefault: false,
    createdBy: "system",
    createdTime: NOW,
    updatedTime: NOW,
  },
];

const SEED_CLASSES: MockClass[] = [
  {
    id: 1,
    className: "Order",
    classAlias: "订单",
    description: "订单实体",
    sourceTable: "t_order",
    parentClassId: null,
    createdBy: "system",
    createdTime: NOW,
    updatedTime: NOW,
  },
];

const SEED_PROPERTIES: MockProperty[] = [
  {
    id: 1,
    classId: 1,
    propertyName: "order_id",
    propertyAlias: "订单ID",
    dataType: "INT",
    isPrimaryKey: true,
    isForeignKey: false,
    refClassId: null,
    sourceColumn: "order_id",
    createdTime: NOW,
    updatedTime: NOW,
  },
];

const SEED_METRICS: MockMetric[] = [
  {
    id: 1,
    metricName: "sales_amount",
    metricAlias: "销售额",
    formula: "SUM(order_amount)",
    aggFunction: "SUM",
    targetClassId: 1,
    dimensionDefaults: null,
    createdBy: "system",
    createdTime: NOW,
    updatedTime: NOW,
  },
];

const SEED_LINEAGE_EDGES: MockLineageEdge[] = [
  {
    id: 1,
    sourceLayer: "SOURCE_SYSTEM",
    sourceSystem: "ERP",
    sourceObject: "PORDER",
    sourceField: "BPSNUM",
    targetLayer: "SOURCE_SYSTEM",
    targetSystem: "ERP",
    targetObject: "BPSUPPLIER",
    targetField: "BPSNUM",
    transformationRule: "JOIN[business]",
    refreshFrequency: "DAILY",
    owner: null,
    description: null,
    isActive: true,
    createdTime: NOW,
    updatedTime: NOW,
  },
  {
    id: 2,
    sourceLayer: "SOURCE_SYSTEM",
    sourceSystem: "ERP",
    sourceObject: "PORDER",
    sourceField: "ORDER_QTY",
    targetLayer: "KPI",
    targetSystem: "KPI",
    targetObject: "KPI_TOTAL_QTY",
    targetField: "TOTAL_QTY",
    transformationRule: "SUM(t.ORDER_QTY)",
    refreshFrequency: "DAILY",
    owner: null,
    description: null,
    isActive: true,
    createdTime: NOW,
    updatedTime: NOW,
  },
];

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

function seed() {
  return {
    datasources: SEED_DATASOURCES.map((d) => ({ ...d })),
    classes: SEED_CLASSES.map((c) => ({ ...c })),
    properties: SEED_PROPERTIES.map((p) => ({ ...p })),
    metrics: SEED_METRICS.map((m) => ({ ...m })),
    lineageEdges: SEED_LINEAGE_EDGES.map((e) => ({ ...e })),
    wikiLinks: SEED_WIKI_LINKS.map((w) => ({ ...w })),
    wikiCategories: SEED_WIKI_CATEGORIES.map(deepCloneCategory),
    wikiPages: SEED_WIKI_PAGES.map((p) => ({ ...p })),
  };
}

function deepCloneCategory(c: MockWikiCategory): MockWikiCategory {
  return {
    ...c,
    children: c.children.map(deepCloneCategory),
  };
}

// ===== SSE 构建（对齐 src/api/chat.ts consumeFrames 解析的帧格式） =====

// 与后端 IntentService 对齐：闲聊关键词 / 短消息 → chitchat，否则 query
function isChitchat(question: string): boolean {
  const normalized = question.trim().toLowerCase();
  const keywords = [
    "你好",
    "您好",
    "hi",
    "hello",
    "谢谢",
    "感谢",
    "再见",
    "拜拜",
    "帮助",
    "help",
    "能做什么",
    "你是谁",
  ];
  if (keywords.some((kw) => normalized.startsWith(kw))) return true;
  return normalized.length <= 3;
}

function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function buildStreamBody(question: string): string {
  if (isChitchat(question)) {
    return [
      sse("meta", { intent: "chitchat" }),
      sse("token", { content: "您好，我是智能问答助手。" }),
      sse("done", { tokensUsed: 0, cost: 0, modelName: null }),
    ].join("");
  }
  return [
    sse("meta", { intent: "query" }),
    sse("sql", {
      sql: "SELECT s.supplier, SUM(r.qty) AS total FROM t_receipt r JOIN t_supplier s ON r.supplier_id = s.id GROUP BY s.supplier",
    }),
    sse("chart", {
      chartType: "bar",
      chartOption: {
        xAxis: { type: "category", data: ["华东", "华南"] },
        yAxis: { type: "value" },
        series: [{ type: "bar", data: [120, 200] }],
      },
      data: [
        { region: "华东", total: 120 },
        { region: "华南", total: 200 },
      ],
    }),
    sse("token", { content: "已为您查询各供应商收货数量，" }),
    sse("token", { content: "共 2 条记录，以柱状图展示。" }),
    sse("done", { tokensUsed: 42, cost: 0.0002, modelName: "mock-model" }),
  ].join("");
}

// ===== 响应工具 =====

function ok(data: unknown): Record<string, unknown> {
  return { success: true, data, error: null, timestamp: new Date().toISOString() };
}

function fail(error: string): Record<string, unknown> {
  return { success: false, data: null, error, timestamp: new Date().toISOString() };
}

async function respondJson(route: Route, status: number, payload: unknown): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

// ===== 请求上下文 =====

interface RouteCtx {
  backend: MockBackend;
  method: string;
  path: string;
  query: URLSearchParams;
  body: Record<string, unknown>;
}

function parsePath(url: string): string {
  return new URL(url).pathname.replace(/^\/api\/v1/, "");
}

function parseQuery(url: string): URLSearchParams {
  return new URL(url).searchParams;
}

// ===== 各实体路由处理器（H1：dispatch 拆分，每个 <50 行） =====

// 本地导入：预览走 httpClient（信封解包），执行走原始 axios（直接返回本体）。
// 在 handleDatasourceRoutes 之前分发，避免被其 idMatch 兜底 404 吞掉。
async function handleLocalImportRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  const { method, path } = ctx;
  const previewMatch = path.match(/^\/datasources\/(\d+)\/import-preview$/);
  if (previewMatch && method === "POST") {
    const dsId = Number(previewMatch[1]);
    const preview = {
      datasourceId: dsId,
      proposedClasses: [
        {
          sourceTable: "orders",
          className: "orders",
          classAlias: null,
          description: null,
          isSelected: true,
          properties: [
            { sourceColumn: "id", propertyName: "id", propertyAlias: null, description: null, dataType: "INT", isPrimaryKey: true, isForeignKey: false, enumValues: null },
            { sourceColumn: "customer_id", propertyName: "customer_id", propertyAlias: null, description: null, dataType: "INT", isPrimaryKey: false, isForeignKey: true, enumValues: null },
          ],
        },
      ],
      proposedJoins: [],
      conflicts: [],
      filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
      llmUsage: { modelName: "mock-model", promptTokens: 0, completionTokens: 0 },
    };
    return respondJson(route, 200, ok(preview));
  }
  const importMatch = path.match(/^\/datasources\/(\d+)\/import$/);
  if (importMatch && method === "POST") {
    // executeImport 走原始 axios：返回 ImportExecuteResponse 本体，不包信封
    return respondJson(route, 200, {
      success: true,
      createdClasses: 1,
      createdProperties: 2,
      createdJoins: 0,
      skippedConflicts: 0,
      overwrittenConflicts: 0,
      errors: [],
    });
  }
  return respondJson(route, 404, fail(`模拟后端未实现 ${method} ${path}`));
}

async function handleDatasourceRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  const { method, path, body, query, backend } = ctx;
  if (path.startsWith("/datasources") && method === "GET") {
    const activeOnly = query.get("activeOnly") === "true";
    const list = activeOnly
      ? backend.datasources.filter((d) => d.isActive)
      : backend.datasources;
    return respondJson(route, 200, ok(list));
  }
  if (path === "/datasources" && method === "POST") {
    const created: MockDatasource = {
      id: backend.nextId("datasource"),
      name: String(body.name ?? ""),
      type: (body.type as MockDatasource["type"]) ?? "postgresql",
      host: String(body.host ?? ""),
      port: Number(body.port ?? 5432),
      databaseName: String(body.databaseName ?? ""),
      username: String(body.username ?? ""),
      description: body.description ? String(body.description) : null,
      isActive: body.isActive === false ? false : true,
      isDefault: body.isDefault === true,
      createdBy: "system",
      createdTime: NOW,
      updatedTime: NOW,
    };
    backend.datasources = [...backend.datasources, created];
    return respondJson(route, 200, ok(created));
  }
  if (path === "/datasources/test" && method === "POST") {
    // testDataSource 走原始 axios：直接返回 { success, message }，不包信封
    const host = String(body.host ?? "");
    const success = host !== "10.255.255.254";
    return respondJson(route, 200, { success, message: success ? "连接成功（模拟）" : "连接超时（模拟）" });
  }
  const idMatch = path.match(/^\/datasources\/(\d+)$/);
  if (!idMatch) return;
  const id = Number(idMatch[1]);
  const target = backend.datasources.find((d) => d.id === id);
  if (method === "DELETE") {
    backend.datasources = backend.datasources.filter((d) => d.id !== id);
    return respondJson(route, 200, ok(null));
  }
  if (method === "PUT" && target) {
    const updated: MockDatasource = {
      ...target,
      name: body.name !== undefined ? String(body.name) : target.name,
      host: body.host !== undefined ? String(body.host) : target.host,
      port: body.port !== undefined ? Number(body.port) : target.port,
      databaseName: body.databaseName !== undefined ? String(body.databaseName) : target.databaseName,
      username: body.username !== undefined ? String(body.username) : target.username,
      description: body.description !== undefined ? (body.description ? String(body.description) : null) : target.description,
      updatedTime: NOW,
    };
    backend.datasources = backend.datasources.map((d) => (d.id === id ? updated : d));
    return respondJson(route, 200, ok(updated));
  }
  if (method === "GET" && target) {
    return respondJson(route, 200, ok(target));
  }
  return respondJson(route, 404, fail("数据源不存在"));
}

async function handleClassRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  const { method, path, body, backend } = ctx;
  if (path.startsWith("/ontology/classes") && method === "GET") {
    return respondJson(route, 200, ok(backend.classes));
  }
  if (path.startsWith("/ontology/classes") && method === "POST") {
    const created: MockClass = {
      id: backend.nextId("class"),
      className: String(body.className ?? ""),
      classAlias: body.classAlias ? String(body.classAlias) : null,
      description: body.description ? String(body.description) : null,
      sourceTable: body.sourceTable ? String(body.sourceTable) : null,
      parentClassId: body.parentClassId ? Number(body.parentClassId) : null,
      createdBy: "system",
      createdTime: NOW,
      updatedTime: NOW,
    };
    backend.classes = [...backend.classes, created];
    return respondJson(route, 200, ok(created));
  }
  const propsMatch = path.match(/^\/ontology\/classes\/(\d+)\/properties$/);
  if (propsMatch && method === "GET") {
    const classId = Number(propsMatch[1]);
    return respondJson(route, 200, ok(backend.properties.filter((p) => p.classId === classId)));
  }
  const idMatch = path.match(/^\/ontology\/classes\/(\d+)$/);
  if (!idMatch) return;
  const id = Number(idMatch[1]);
  const target = backend.classes.find((c) => c.id === id);
  if (method === "DELETE") {
    backend.classes = backend.classes.filter((c) => c.id !== id);
    return respondJson(route, 200, ok(null));
  }
  if (method === "PUT" && target) {
    const updated: MockClass = {
      ...target,
      className: body.className !== undefined ? String(body.className) : target.className,
      classAlias: body.classAlias !== undefined ? (body.classAlias ? String(body.classAlias) : null) : target.classAlias,
      description: body.description !== undefined ? (body.description ? String(body.description) : null) : target.description,
      sourceTable: body.sourceTable !== undefined ? (body.sourceTable ? String(body.sourceTable) : null) : target.sourceTable,
      updatedTime: NOW,
    };
    backend.classes = backend.classes.map((c) => (c.id === id ? updated : c));
    return respondJson(route, 200, ok(updated));
  }
  if (method === "GET" && target) {
    return respondJson(route, 200, ok(target));
  }
  return respondJson(route, 404, fail("类不存在"));
}

async function handleWikiCategoryRoutes(route: Route, ctx: RouteCtx): Promise<void> {
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

async function handleWikiPageRoutes(route: Route, ctx: RouteCtx): Promise<void> {
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

async function handleWikiLinkRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  const { method, path, query, body, backend } = ctx;
  // 链接目标搜索（type 必填，q 可选）—— 必须早于 /admin/wiki-links/:id 匹配
  const linkablesMatch = path === "/admin/wiki-links/linkables";
  if (linkablesMatch && method === "GET") {
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

  // 列表 + 创建共享 /admin/wiki-links 路径前缀
  if (path === "/admin/wiki-links" && method === "GET") {
    const pageIdRaw = query.get("page_id");
    const pageId = pageIdRaw ? String(pageIdRaw) : null;
    const filtered = pageId
      ? backend.wikiLinks.filter((w) => w.page_id === pageId)
      : backend.wikiLinks;
    // 真后端 list_links 返回 raw array，无信封
    return respondJson(route, 200, filtered);
  }
  if (path === "/admin/wiki-links" && method === "POST") {
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

async function handlePropertyRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  const { method, path, body, backend } = ctx;
  if (path === "/ontology/properties" && method === "POST") {
    const created: MockProperty = {
      id: backend.nextId("property"),
      classId: Number(body.classId ?? 0),
      propertyName: String(body.propertyName ?? ""),
      propertyAlias: body.propertyAlias ? String(body.propertyAlias) : null,
      dataType: String(body.dataType ?? "STRING"),
      isPrimaryKey: body.isPrimaryKey === true,
      isForeignKey: body.isForeignKey === true,
      refClassId: body.refClassId ? Number(body.refClassId) : null,
      sourceColumn: body.sourceColumn ? String(body.sourceColumn) : null,
      createdTime: NOW,
      updatedTime: NOW,
    };
    backend.properties = [...backend.properties, created];
    return respondJson(route, 200, ok(created));
  }
  const idMatch = path.match(/^\/ontology\/properties\/(\d+)$/);
  if (!idMatch) return;
  const id = Number(idMatch[1]);
  const target = backend.properties.find((p) => p.id === id);
  if (method === "DELETE") {
    backend.properties = backend.properties.filter((p) => p.id !== id);
    return respondJson(route, 200, ok(null));
  }
  if (method === "PUT" && target) {
    const updated: MockProperty = {
      ...target,
      propertyName: body.propertyName !== undefined ? String(body.propertyName) : target.propertyName,
      propertyAlias: body.propertyAlias !== undefined ? (body.propertyAlias ? String(body.propertyAlias) : null) : target.propertyAlias,
      dataType: body.dataType !== undefined ? String(body.dataType) : target.dataType,
      sourceColumn: body.sourceColumn !== undefined ? (body.sourceColumn ? String(body.sourceColumn) : null) : target.sourceColumn,
      isPrimaryKey: body.isPrimaryKey !== undefined ? body.isPrimaryKey === true : target.isPrimaryKey,
      isForeignKey: body.isForeignKey !== undefined ? body.isForeignKey === true : target.isForeignKey,
      updatedTime: NOW,
    };
    backend.properties = backend.properties.map((p) => (p.id === id ? updated : p));
    return respondJson(route, 200, ok(updated));
  }
  if (method === "GET" && target) {
    return respondJson(route, 200, ok(target));
  }
  return respondJson(route, 404, fail("属性不存在"));
}

async function handleMetricRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  const { method, path, body, backend } = ctx;
  if (path === "/ontology/metrics" && method === "GET") {
    return respondJson(route, 200, ok(backend.metrics));
  }
  if (path === "/ontology/metrics" && method === "POST") {
    const created: MockMetric = {
      id: backend.nextId("metric"),
      metricName: String(body.metricName ?? ""),
      metricAlias: body.metricAlias ? String(body.metricAlias) : null,
      formula: String(body.formula ?? ""),
      aggFunction: String(body.aggFunction ?? "SUM"),
      targetClassId: body.targetClassId ? Number(body.targetClassId) : null,
      dimensionDefaults: body.dimensionDefaults ? (body.dimensionDefaults as Record<string, string>) : null,
      createdBy: "system",
      createdTime: NOW,
      updatedTime: NOW,
    };
    backend.metrics = [...backend.metrics, created];
    return respondJson(route, 200, ok(created));
  }
  const idMatch = path.match(/^\/ontology\/metrics\/(\d+)$/);
  if (!idMatch) return;
  const id = Number(idMatch[1]);
  const target = backend.metrics.find((m) => m.id === id);
  if (method === "DELETE") {
    backend.metrics = backend.metrics.filter((m) => m.id !== id);
    return respondJson(route, 200, ok(null));
  }
  if (method === "PUT" && target) {
    const updated: MockMetric = {
      ...target,
      metricName: body.metricName !== undefined ? String(body.metricName) : target.metricName,
      metricAlias: body.metricAlias !== undefined ? (body.metricAlias ? String(body.metricAlias) : null) : target.metricAlias,
      formula: body.formula !== undefined ? String(body.formula) : target.formula,
      aggFunction: body.aggFunction !== undefined ? String(body.aggFunction) : target.aggFunction,
      updatedTime: NOW,
    };
    backend.metrics = backend.metrics.map((m) => (m.id === id ? updated : m));
    return respondJson(route, 200, ok(updated));
  }
  if (method === "GET" && target) {
    return respondJson(route, 200, ok(target));
  }
  return respondJson(route, 404, fail("指标不存在"));
}

async function handleModelRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  if (ctx.path === "/models" && ctx.method === "GET") {
    return respondJson(route, 200, ok([]));
  }
  return respondJson(route, 404, fail(`模拟后端未实现 ${ctx.method} ${ctx.path}`));
}

async function handleLineageRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  const { method, path } = ctx;
  const idMatch = path.match(/^\/lineage\/edges\/(\d+)$/);
  if (idMatch && method === "GET") {
    const id = Number(idMatch[1]);
    const target = ctx.backend.lineageEdges.find((e) => e.id === id);
    if (!target) return respondJson(route, 404, fail("血缘边不存在"));
    return respondJson(route, 200, ok(target));
  }
  if (path === "/lineage/edges" && method === "GET") {
    let edges = ctx.backend.lineageEdges;
    const activeOnly = ctx.query.get("activeOnly") === "true";
    if (activeOnly) edges = edges.filter((e) => e.isActive);
    const sourceLayer = ctx.query.get("sourceLayer");
    const targetLayer = ctx.query.get("targetLayer");
    if (sourceLayer) edges = edges.filter((e) => e.sourceLayer === sourceLayer);
    if (targetLayer) edges = edges.filter((e) => e.targetLayer === targetLayer);
    return respondJson(route, 200, ok(edges));
  }
  if (path === "/lineage/edges" && method === "POST") {
    const body = ctx.body;
    const id = ctx.backend.nextId("lineageEdge");
    const edge: MockLineageEdge = {
      id,
      sourceLayer: String(body.sourceLayer ?? "SOURCE_SYSTEM"),
      sourceSystem: String(body.sourceSystem ?? ""),
      sourceObject: String(body.sourceObject ?? ""),
      sourceField: body.sourceField == null ? null : String(body.sourceField),
      targetLayer: String(body.targetLayer ?? "SOURCE_SYSTEM"),
      targetSystem: String(body.targetSystem ?? ""),
      targetObject: String(body.targetObject ?? ""),
      targetField: body.targetField == null ? null : String(body.targetField),
      transformationRule: body.transformationRule == null ? null : String(body.transformationRule),
      refreshFrequency: String(body.refreshFrequency ?? "DAILY"),
      owner: body.owner == null ? null : String(body.owner),
      description: body.description == null ? null : String(body.description),
      isActive: true,
      createdTime: NOW,
      updatedTime: NOW,
    };
    ctx.backend.lineageEdges.push(edge);
    return respondJson(route, 201, ok(edge));
  }
  return respondJson(route, 404, fail(`模拟后端未实现 ${method} ${path}`));
}

async function handleMenuConfigRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  if (ctx.path === "/menu-config" && ctx.method === "GET") {
    return respondJson(route, 200, {
      version: "2026-09-01",
      sections: [
        {
          code: "section.dataQuality",
          labelKey: "menu.section.dataQuality",
          iconCode: "audit",
          sortOrder: 300,
          permissionCode: null,
          roles: [],
          path: null,
          children: [
            {
              code: "item.dataQuality",
              labelKey: "menu.item.dataQuality",
              iconCode: "audit",
              sortOrder: 320,
              permissionCode: null,
              roles: [],
              path: "/data-quality",
            },
            {
              code: "item.dataQualityGenerate",
              labelKey: "menu.item.dataQualityGenerate",
              iconCode: "thunderbolt",
              sortOrder: 325,
              permissionCode: null,
              roles: [],
              path: "/data-quality/generate",
            },
          ],
        },
      ],
    });
  }
  return respondJson(route, 404, fail(`模拟后端未实现 ${ctx.method} ${ctx.path}`));
}

async function handleDataQualityGenerateRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  const { method, path } = ctx;
  const BASE = "/data-quality/rules/generate";

  // GET /data-quality/rules/generate/options  → supported rule types / thresholds
  if (path === `${BASE}/options` && method === "GET") {
    return respondJson(route, 200, ok({
      ruleTypes: ["COMPLETENESS", "CONSISTENCY", "UNIQUENESS", "VALIDITY", "REFERENTIAL"],
      severities: ["HIGH", "MEDIUM", "LOW"],
      derivationTypes: ["PK_DERIVED", "FK_DERIVED", "DICT_REF", "ALLOWED_VALUES", "NOT_NULL", "JOIN_CONSISTENCY", "LLM_DERIVED", "MANUAL"],
    }));
  }

  // POST /data-quality/rules/generate/preview
  const previewMatch = path.match(/^\/data-quality\/rules\/generate\/preview$/);
  if (previewMatch && method === "POST") {
    return respondJson(route, 200, ok({
      classId: 1,
      className: "Order",
      sourceTable: "t_order",
      datasourceId: 1,
      suggestions: [
        {
          ruleCode: "ORDER_PK_001",
          ruleName: "订单主键完整性",
          ruleType: "COMPLETENESS",
          targetTable: "t_order",
          targetColumn: "order_id",
          ruleExpression: "order_id IS NOT NULL",
          threshold: 1.0,
          severity: "HIGH",
          derivationType: "PK_DERIVED",
          sourcePropertyId: 1,
          sourceClassId: 1,
          confidence: "HIGH",
          status: "NEW",
          reason: "主键列不允许为空",
        },
      ],
      blocked: [],
    }));
  }

  // POST /data-quality/rules/generate/confirm
  const confirmMatch = path.match(/^\/data-quality\/rules\/generate\/confirm$/);
  if (confirmMatch && method === "POST") {
    return respondJson(route, 200, ok({ created: [], skippedCodes: [] }));
  }

  // POST /data-quality/rules/generate/parse-descriptions
  const parseMatch = path.match(/^\/data-quality\/rules\/generate\/parse-descriptions$/);
  if (parseMatch && method === "POST") {
    return respondJson(route, 200, ok({ suggestions: [] }));
  }

  // POST /data-quality/rules/generate/apply-suggestion
  const applyMatch = path.match(/^\/data-quality\/rules\/generate\/apply-suggestion$/);
  if (applyMatch && method === "POST") {
    return respondJson(route, 200, ok(null));
  }

  return respondJson(route, 404, fail(`模拟后端未实现 ${method} ${path}`));
}

async function handleChatRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  const question = String(ctx.body.question ?? "");
  if (ctx.path === "/chat/stream" && ctx.method === "POST") {
    return route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
      body: buildStreamBody(question),
    });
  }
  if (ctx.path === "/chat" && ctx.method === "POST") {
    if (isChitchat(question)) {
      return respondJson(route, 200, ok({
        answer: "您好，我是智能问答助手。",
        intent: "chitchat",
        sql: null,
        chartType: null,
        chartOption: null,
        data: null,
        tokensUsed: 0,
        cost: 0,
        modelName: null,
      }));
    }
    return respondJson(route, 200, ok({
      answer: "已为您查询各供应商收货数量，共 2 条记录。",
      intent: "query",
      sql: "SELECT s.supplier, SUM(r.qty) AS total FROM t_receipt r JOIN t_supplier s ON r.supplier_id = s.id GROUP BY s.supplier",
      chartType: "bar",
      chartOption: { xAxis: { type: "category", data: ["华东", "华南"] }, series: [{ type: "bar", data: [120, 200] }] },
      data: [{ region: "华东", total: 120 }, { region: "华南", total: 200 }],
      tokensUsed: 42,
      cost: 0.0002,
      modelName: "mock-model",
    }));
  }
  return respondJson(route, 404, fail(`模拟后端未实现 ${ctx.method} ${ctx.path}`));
}

// ===== 顶层分发：按路径前缀委托给实体处理器 =====

async function dispatch(route: Route, backend: MockBackend): Promise<void> {
  const request = route.request();
  const ctx: RouteCtx = {
    backend,
    method: request.method(),
    path: parsePath(request.url()),
    query: parseQuery(request.url()),
    body: (request.postDataJSON() ?? {}) as Record<string, unknown>,
  };
  if (ctx.path.startsWith("/datasources") && /\/import(-preview)?$/.test(ctx.path))
    return handleLocalImportRoutes(route, ctx);
  if (ctx.path.startsWith("/datasources")) return handleDatasourceRoutes(route, ctx);
  if (ctx.path.startsWith("/ontology/classes")) return handleClassRoutes(route, ctx);
  if (ctx.path.startsWith("/ontology/properties")) return handlePropertyRoutes(route, ctx);
  if (ctx.path.startsWith("/ontology/metrics")) return handleMetricRoutes(route, ctx);
  if (ctx.path.startsWith("/models")) return handleModelRoutes(route, ctx);
  if (ctx.path.startsWith("/lineage")) return handleLineageRoutes(route, ctx);
  if (ctx.path.startsWith("/chat")) return handleChatRoutes(route, ctx);
  if (ctx.path.startsWith("/data-quality")) return handleDataQualityGenerateRoutes(route, ctx);
  if (ctx.path.startsWith("/menu-config")) return handleMenuConfigRoutes(route, ctx);
  if (ctx.path.startsWith("/admin/wiki-links")) return handleWikiLinkRoutes(route, ctx);
  if (ctx.path.startsWith("/wiki/categories")) return handleWikiCategoryRoutes(route, ctx);
  if (ctx.path.startsWith("/wiki/pages")) return handleWikiPageRoutes(route, ctx);
  return respondJson(route, 404, fail(`模拟后端未实现 ${ctx.method} ${ctx.path}`));
}

// 注册路由拦截并返回内存后端（每个 page 独立，spec 可直接读写断言）
export async function mockApi(page: Page): Promise<MockBackend> {
  const counters: Record<EntityKind, number> = {
    datasource: 100,
    class: 100,
    property: 100,
    metric: 100,
    lineageEdge: 100,
    wikiLink: 100,
    wikiCategory: 100,
    wikiPage: 100,
  };
  const backend: MockBackend = {
    page,
    ...seed(),
    nextId: (kind) => {
      const id = counters[kind];
      counters[kind] = id + 1;
      return id;
    },
  };
  // dispatch 失败兜底：fulfill 500 而非挂起（审查 M1 修复）；日志便于定位 mock 缺陷
  await page.route("**/api/v1/**", (route) => {
    void dispatch(route, backend).catch((err: unknown) => {
      console.error("[mockApi] dispatch 失败:", err);
      void route
        .fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({ success: false, error: "模拟后端分发异常" }),
        })
        .catch(() => {});
    });
  });
  return backend;
}
