// =============================================================================
// E2E mock 后端：实体域（数据源 / 本体类 / 属性 / 指标 / 血缘 / 模型）。
// 契约对齐 src/api/* ：httpClient 解包 { success, data, error, timestamp } 信封；
// testDataSource 与 executeImport 走原始 axios，直接返回本体。
// =============================================================================

import type { Route } from "@playwright/test";
import {
  NOW,
  fail,
  ok,
  respondJson,
  type BackendCore,
  type RouteCtx,
} from "./mockShared";

// ===== 内存实体类型（对齐 src/types/* 字段名） =====

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

/** 模型配置（mock）。 */
export interface MockModelConfig {
  id: number;
  modelName: string;
  provider: string;
  apiEndpoint: string;
  isActive: boolean;
  costPer1KInput: number;
  costPer1KOutput: number;
  maxInputTokens: number;
  weight: number;
  costThreshold: number;
  createdAt: string;
  updatedAt: string;
}


// ===== 实体域状态切片 =====

/** 本体/属性子集：wiki 链接的可选目标需要复用 class/property 种子。 */
export interface OntologyState {
  classes: MockClass[];
  properties: MockProperty[];
}

/** 实体域的全部内存状态（每个 page 独立实例）。 */
export interface EntityState extends OntologyState, BackendCore {
  datasources: MockDatasource[];
  metrics: MockMetric[];
  lineageEdges: MockLineageEdge[];
  models: MockModelConfig[];
}

// ===== Seed 数据模板（每次调用深拷贝，保证 page 间隔离 / fullyParallel 安全） =====

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

const SEED_MODELS: MockModelConfig[] = [
  {
    id: 1,
    modelName: "mock-model",
    provider: "openai",
    apiEndpoint: "http://mock.local/v1",
    isActive: true,
    costPer1KInput: 0.001,
    costPer1KOutput: 0.002,
    maxInputTokens: 8000,
    weight: 1,
    costThreshold: 10,
    createdAt: NOW,
    updatedAt: NOW,
  },
];


// ===== 实体域路由处理器 =====

export async function handleLocalImportRoutes(route: Route, ctx: RouteCtx<EntityState>): Promise<void> {
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

export async function handleDatasourceRoutes(route: Route, ctx: RouteCtx<EntityState>): Promise<void> {
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
  return handleDatasourceItemRoutes(route, ctx);
}

export async function handleDatasourceItemRoutes(
  route: Route,
  ctx: RouteCtx<EntityState>,
): Promise<void> {
  const { method, path, body, backend } = ctx;
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

export async function handleClassRoutes(route: Route, ctx: RouteCtx<EntityState>): Promise<void> {
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
  return handleClassItemRoutes(route, ctx);
}

export async function handleClassItemRoutes(
  route: Route,
  ctx: RouteCtx<EntityState>,
): Promise<void> {
  const { method, path, body, backend } = ctx;
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

export async function handlePropertyRoutes(route: Route, ctx: RouteCtx<EntityState>): Promise<void> {
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

export async function handleMetricRoutes(route: Route, ctx: RouteCtx<EntityState>): Promise<void> {
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

export async function handleModelRoutes(route: Route, ctx: RouteCtx<EntityState>): Promise<void> {
  if (ctx.path === "/models" && ctx.method === "GET") {
    // 返回种子模型：任务页的模型下拉需要至少一个启用中的模型。
    const activeOnly = ctx.query.get("activeOnly") === "true";
    const models = activeOnly
      ? ctx.backend.models.filter((m) => m.isActive)
      : ctx.backend.models;
    return respondJson(route, 200, ok(models));
  }
  return respondJson(route, 404, fail(`模拟后端未实现 ${ctx.method} ${ctx.path}`));
}

export async function handleLineageRoutes(route: Route, ctx: RouteCtx<EntityState>): Promise<void> {
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


/** 实体域种子（深拷贝）。 */
export function entitySeed(): Omit<EntityState, "nextId"> {
  return {
    datasources: SEED_DATASOURCES.map((d) => ({ ...d })),
    classes: SEED_CLASSES.map((c) => ({ ...c })),
    properties: SEED_PROPERTIES.map((p) => ({ ...p })),
    metrics: SEED_METRICS.map((m) => ({ ...m })),
    lineageEdges: SEED_LINEAGE_EDGES.map((e) => ({ ...e })),
    models: SEED_MODELS.map((m) => ({ ...m })),
  };
}
