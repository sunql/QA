/** M4 Report 模板（A8）类型契约，与后端 report_schemas.py 对齐（camelCase）。 */

export type ReportStatus = "PENDING_REVIEW" | "APPROVED" | "REJECTED";

export interface ReportParamField {
  name: string;
  type: string;
  required: boolean;
  pattern?: string | null;
}

export interface ReportTemplate {
  code: string;
  title: string;
  description: string;
  paramsSchema: ReportParamField[];
}

export type ReportSectionKind = "table" | "kpi_cards" | "text";

export interface ReportSection {
  sectionId: string;
  title: string;
  kind: ReportSectionKind;
  data: Array<Record<string, unknown>> | null;
  renderError?: string | null;
}

export interface ReportInstance {
  id: number;
  templateCode: string;
  title: string;
  params: Record<string, unknown> | null;
  sections: ReportSection[];
  summary: string | null;
  status: ReportStatus;
  reviewNote: string | null;
  reviewedBy: string | null;
  createdBy: string;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface ReportListItem {
  id: number;
  templateCode: string;
  title: string;
  status: ReportStatus;
  createdBy: string;
  createdTime: string | null;
}

export interface ReportListPage {
  rows: ReportListItem[];
  total: number;
}

export type ReviewDecision = "APPROVE" | "REJECT";

export const REPORT_STATUS_META: Record<
  ReportStatus,
  { color: string; i18nKey: string }
> = {
  PENDING_REVIEW: { color: "orange", i18nKey: "reportsPage.status.pending" },
  APPROVED: { color: "green", i18nKey: "reportsPage.status.approved" },
  REJECTED: { color: "red", i18nKey: "reportsPage.status.rejected" },
};
