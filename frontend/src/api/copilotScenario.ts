/** P4 场景配置客户端：`GET /procurement/copilot/scenarios`。

场景从硬编码（ProcurementCopilotPage）变为 DB 业务资产：卡片墙 / 槽位表单 /
推荐问题 chips / 结果渲染器全部由配置驱动；加场景 = 插一行配置。
字段契约与后端 `CopilotScenarioRead`（camelCase）一一对应。
 */

import { httpClient } from "./client";

/** 槽位定义（type 白名单见后端 SLOT_TYPE_WHITELIST；未知 type 前端不渲染）。 */
export interface CopilotScenarioSlot {
  key: string;
  type: "number" | "dateRange" | "entityMulti" | "dictMulti" | "text";
  label: string;
  required?: boolean;
  conflictKeywords?: string[];
  /** 编码让位模式（后端渲染用；前端只透传展示，不做判断） */
  codePattern?: string | null;
  displayTemplate?: string | null;
  /** 隐藏槽位：前端表单不渲染（由专用控件写入，如日期区间/数据源）；
   * 后端渲染照常进尾注与工具入参。 */
  hidden?: boolean;
}

/** 推荐问题 chip = 预设槽位组合（label + 若干槽位键值）。 */
export interface CopilotScenarioChip {
  label: string;
  [slotKey: string]: unknown;
}

/** 前端支持专用结构化结果卡的渲染器（后端白名单还含 "none" 等
 * 无需专用卡的取值 ⇒ API 字段本身仍是 string，只在前端可渲染处收敛）。 */
export type StructuredResultRenderer =
  | "pending_delivery"
  | "supplier_risk"
  | "delivery_tracking";

export interface CopilotScenario {
  scenarioCode: string;
  name: string;
  description: string;
  iconCode: string;
  /** 场景钉死的 Agent（发消息必须用它，否则后端 422） */
  agentCode: string;
  slotSchema: CopilotScenarioSlot[];
  questionTemplate: string;
  chips: CopilotScenarioChip[];
  resultRenderer: string;
  requiredRole: string | null;
}

/** 列出当前用户可见的启用场景（admin 专属场景后端已按角色过滤）。 */
export async function listCopilotScenarios(): Promise<CopilotScenario[]> {
  const res = await httpClient.get<CopilotScenario[]>(
    "/procurement/copilot/scenarios",
  );
  return Array.isArray(res.data) ? res.data : [];
}
