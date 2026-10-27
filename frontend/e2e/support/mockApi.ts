// =============================================================================
// E2E 模拟后端：用 page.route 拦截浏览器层所有 /api/v1 请求，
// 以内存数据 + 预构建响应体模拟后端。不依赖真实后端/DB，CI 可复现。
// 契约对齐 src/api/*：
//   - httpClient 解包 { success, data, error, timestamp } 信封
//   - testDataSource 走原始 axios，直接返回 { success, message }
//   - sendMessageStream 用 fetch 消费 text/event-stream 的 SSE 帧
// 各域实现分散在 entityMock / wikiMock / appMock / orchestrationMock，
// 本文件只负责：内存状态组装 + 顶层路由分发（唯一 catch-all）。
// =============================================================================

import type { Page, Route } from "@playwright/test";
import {
  fail,
  parsePath,
  parseQuery,
  respondJson,
  type EntityKind,
  type RouteCtx,
} from "./mockShared";
import {
  entitySeed,
  handleClassRoutes,
  handleDatasourceRoutes,
  handleLineageRoutes,
  handleLocalImportRoutes,
  handleMetricRoutes,
  handleModelRoutes,
  handlePropertyRoutes,
  type EntityState,
} from "./entityMock";
import {
  handleWikiCategoryRoutes,
  handleWikiLinkRoutes,
  handleWikiPageRoutes,
  wikiSeed,
  type WikiState,
} from "./wikiMock";
import {
  handleChatRoutes,
  handleDataQualityGenerateRoutes,
  handleMenuConfigRoutes,
} from "./appMock";
import {
  handleAgentRegistryRoutes,
  handleAgentRunRoutes,
  handleOrchestrationRoutes,
  orchestrationSeed,
  type OrchestrationState,
} from "./orchestrationMock";

// 后端暴露给 spec 断言/构造的内存存储（每个 page 独立实例）
export interface MockBackend extends EntityState, WikiState, OrchestrationState {
  page: Page;
}

/** 各域种子合并后的完整内存状态（不含 page / nextId）。 */
type MockState = Omit<EntityState, "nextId"> &
  Omit<WikiState, "nextId" | "classes" | "properties"> &
  OrchestrationState;

function seed(): MockState {
  return {
    ...entitySeed(),
    ...wikiSeed(),
    ...orchestrationSeed(),
  };
}

// ===== 顶层分发：按路径前缀委托给实体处理器 =====

async function dispatch(route: Route, backend: MockBackend): Promise<void> {
  const request = route.request();
  const ctx: RouteCtx<MockBackend> = {
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
  if (ctx.path.startsWith("/agent-orchestrations")) return handleOrchestrationRoutes(route, ctx);
  if (ctx.path.startsWith("/agent-runs")) return handleAgentRunRoutes(route, ctx);
  if (ctx.path.startsWith("/agents")) return handleAgentRegistryRoutes(route, ctx);
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
