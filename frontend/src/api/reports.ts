/** M4 Report 模板（A8）API。全部走 /api/v1/reports，鉴权由 httpClient 拦截器统一注入。 */

import { httpClient } from "./client";
import type {
  ReportInstance,
  ReportListItem,
  ReportListPage,
  ReportTemplate,
  ReviewDecision,
} from "../types/reports";

const BASE = "/reports";

export async function listReportTemplates(): Promise<ReportTemplate[]> {
  const res = await httpClient.get<ReportTemplate[]>(`${BASE}/templates`);
  return res.data;
}

export async function generateReport(input: {
  templateCode: string;
  params: Record<string, string>;
}): Promise<ReportInstance> {
  const res = await httpClient.post<ReportInstance>(`${BASE}/generate`, input);
  return res.data;
}

export async function listReports(query?: {
  status?: string;
  limit?: number;
  offset?: number;
}): Promise<ReportListPage> {
  const res = await httpClient.get<ReportListPage>(BASE, { params: query });
  return res.data;
}

export async function getReport(id: number): Promise<ReportInstance> {
  const res = await httpClient.get<ReportInstance>(`${BASE}/${id}`);
  return res.data;
}

export async function reviewReport(
  id: number,
  input: { decision: ReviewDecision; note?: string | null },
): Promise<ReportInstance> {
  const res = await httpClient.post<ReportInstance>(`${BASE}/${id}/review`, input);
  return res.data;
}

export type { ReportListItem };
