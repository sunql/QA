/**
 * 图表字段的**唯一收窄口径**（系统边界上的运行时校验）。
 *
 * 为什么单独一个模块而不是留在 `api/chat.ts`：收窄规则描述的是**线上契约**，不是
 * HTTP 传输。三个消费者分属三层 —— SSE/HTTP 响应（`api/chat.ts`）、历史回放
 * （`stores/chatStore.ts`）、导出前挑截图（`utils/collectExportCharts.ts`）。留在一个
 * 「谁都能 import 的纯函数模块」里，才不会出现「store 为了一个谓词反向依赖 HTTP 客户端」
 * 这种耦合（以及随之而来的一整类替身漂移）。
 */
import type { ChartType, TablePayload, VisualRationale } from "../types/chat";

/**
 * 与后端 `ChartType` 枚举对齐的运行时白名单。
 *
 * ⚠️ 后端新增图表类型时**必须同步这里**：漏同步的表现是「图不见了但没有任何报错」
 * ——chartType 被静默降级为 null，渲染门直接不放行。覆盖四条链路：SSE `chart` 事件、
 * 非流式响应、多步 `step_result`、历史回放。
 */
export const VALID_CHART_TYPES = new Set<string>([
  "table",
  "bar",
  "hbar",
  "pie",
  "donut",
  "line",
  "scatter",
  "heatmap",
  "kpi",
  "combo",
  "waterfall",
]);

export function isChartType(value: unknown): value is ChartType {
  return typeof value === "string" && VALID_CHART_TYPES.has(value);
}

/** 未知/非法类型一律 null（渲染门据此不画），绝不硬 cast 给渲染层。 */
export function normalizeChartType(value: unknown): ChartType | null {
  return isChartType(value) ? value : null;
}

/** 图表负载必须是**普通对象**；数组/标量/null 一律不算（负载里没有位置语义）。 */
export function asChartOption(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/**
 * 明细表负载收窄（0107）：columns/rows 都是数组才放行，其余一律 null。
 *
 * 返回**新对象**（不可变）：columns/rows 复用原数组引用（与 asChartOption 同口径，
 * 收窄器不原地改负载）。`truncated` 只在后端命中行数上限时为 `true` —— 按 wire 契约
 * absent 时不归一成 `false`（前端据此知道「是否被截断」，而非「是否完整」）。
 */
export function asTablePayload(value: unknown): TablePayload | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (!Array.isArray(record.columns) || !Array.isArray(record.rows)) return null;
  const payload: TablePayload = {
    columns: record.columns as string[],
    rows: record.rows as Record<string, unknown>[],
  };
  if (record.truncated === true) payload.truncated = true;
  return payload;
}

/**
 * 判断依据收窄（0107）：code 非空 string + params 普通对象才放行。
 *
 * **不内置 code 白名单** —— 后端可先发新 code，前端 i18n 缺 key 时显示 code 原文兜底，
 * 不静默吞。这与 `VALID_CHART_TYPES` 的严格白名单刻意不同：图型白名单漏同步的症状是
 * 「图静默消失」，而 rationale 漏一个 code 只是这行说明退化成英文码，代价不对称。
 */
export function asVisualRationale(value: unknown): VisualRationale | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.code !== "string" || record.code.length === 0) return null;
  const params = record.params;
  if (typeof params !== "object" || params === null || Array.isArray(params)) return null;
  return {
    code: record.code,
    params: params as Record<string, unknown>,
  };
}
