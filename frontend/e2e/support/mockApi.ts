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

type EntityKind = "datasource" | "class" | "property" | "metric";

// 后端暴露给 spec 断言/构造的内存存储（每个 page 独立实例）
export interface MockBackend {
  page: Page;
  datasources: MockDatasource[];
  classes: MockClass[];
  properties: MockProperty[];
  metrics: MockMetric[];
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

function seed() {
  return {
    datasources: SEED_DATASOURCES.map((d) => ({ ...d })),
    classes: SEED_CLASSES.map((c) => ({ ...c })),
    properties: SEED_PROPERTIES.map((p) => ({ ...p })),
    metrics: SEED_METRICS.map((m) => ({ ...m })),
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

async function handleDatasourceRoutes(route: Route, ctx: RouteCtx): Promise<void> {
  const { method, path, body, query, backend } = ctx;
  if (path === "/datasources" && method === "GET") {
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
  if (path === "/ontology/classes" && method === "GET") {
    return respondJson(route, 200, ok(backend.classes));
  }
  if (path === "/ontology/classes" && method === "POST") {
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
  if (ctx.path.startsWith("/datasources")) return handleDatasourceRoutes(route, ctx);
  if (ctx.path.startsWith("/ontology/classes")) return handleClassRoutes(route, ctx);
  if (ctx.path.startsWith("/ontology/properties")) return handlePropertyRoutes(route, ctx);
  if (ctx.path.startsWith("/ontology/metrics")) return handleMetricRoutes(route, ctx);
  if (ctx.path.startsWith("/models")) return handleModelRoutes(route, ctx);
  if (ctx.path.startsWith("/chat")) return handleChatRoutes(route, ctx);
  return respondJson(route, 404, fail(`模拟后端未实现 ${ctx.method} ${ctx.path}`));
}

// 注册路由拦截并返回内存后端（每个 page 独立，spec 可直接读写断言）
export async function mockApi(page: Page): Promise<MockBackend> {
  const counters: Record<EntityKind, number> = {
    datasource: 100,
    class: 100,
    property: 100,
    metric: 100,
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
