// =============================================================================
// E2E mock 后端：应用级 mock（菜单配置 / 数据质量规则生成 / 聊天 SSE）。
// 这三个域不持有内存状态，响应体全部是静态预构建数据。
// SSE 帧格式对齐 src/api/chat.ts consumeFrames 的解析约定。
// =============================================================================

import type { Route } from "@playwright/test";
import { fail, ok, respondJson, type RouteCtx } from "./mockShared";

// ===== SSE 构建（对齐 src/api/chat.ts consumeFrames 解析的帧格式） =====

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


// ===== 菜单配置 / 数据质量规则生成 / 聊天路由处理器 =====

export async function handleMenuConfigRoutes(route: Route, ctx: RouteCtx<unknown>): Promise<void> {
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

export async function handleDataQualityGenerateRoutes(route: Route, ctx: RouteCtx<unknown>): Promise<void> {
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
  return handleDataQualityRuleRoutes(route, ctx);
}

export async function handleDataQualityRuleRoutes(
  route: Route,
  ctx: RouteCtx<unknown>,
): Promise<void> {
  const { method, path } = ctx;
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

export async function handleChatRoutes(route: Route, ctx: RouteCtx<unknown>): Promise<void> {
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

