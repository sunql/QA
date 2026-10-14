/** 研究报告渲染器（feat-research-entry Task 10 + R3-B chart 接入）。
 *
 * 只读渲染 `payload.sections[].blocks[]`，块类型对齐 backend report_planner.py 的
 * JSON 形状（camelCase）：
 * - text → 段落
 * - table → 原生 <table>（可测、不依赖 antd Table 的 rowKey）
 * - chart → chartType ∈ {bar, pie, line, hbar, donut, scatter, heatmap, combo, waterfall}
 *          时走 `<EChart option={block.content.echartsOption} />` 真实渲染；
 *          table/kpi 或 echartsOption 为 null 时走表格兜底（前端契约不变）。
 * - bullet_list → <ul>/<li>
 *
 * 每个块带 data-testid="report-block" + data-block-type，供顺序断言。
 *
 * R3-B 字段约定：chart 块 content 的数据键是 `data`（不是 `rows`），前端兼容旧
 * rows 也兼容新 data —— 后端 R3 之后只发 data，但解析器对历史 payload 仍然不破。
 */
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import EChart from "../EChart";
import { isChartType } from "../../utils/chartContract";
import { EvidencePanel } from "./EvidencePanel";

export interface ReportSourceRef {
  kind: string;
  refId: string;
  label: string;
}

export interface ReportBlock {
  type: "text" | "chart" | "table" | "bullet_list";
  content: unknown;
  sourceRefs?: ReportSourceRef[];
}

export interface ReportSection {
  id: string;
  kind: string;
  title: string;
  blocks: ReportBlock[];
}

/** Task 8：单步执行证据（对齐 backend `_stepEvidence` 的 payload 形状，camelCase）。 */
export interface StepEvidence {
  index: number;
  type: string;
  sql: string | null;
  status: string;
  error: string | null;
  summary: Record<string, unknown> | null;
  schemaVersion: number | null;
}

/** Task 8：报告执行证据（跨本 turn 所有 run_state 版本的步结果聚合）。 */
export interface ReportEvidence {
  stepResults: StepEvidence[];
}

export interface ReportPayload {
  sessionId: string;
  turnId: string;
  mode: string;
  title: string;
  question: string;
  sections: ReportSection[];
  findingsRef?: unknown[];
  /** Task 8：可缺省 —— Task 8 之前归档的报告 payload 无此键。 */
  evidence?: ReportEvidence;
}

interface ReportRendererProps {
  payload: ReportPayload;
}

interface TableContent {
  columns: string[];
  rows: Record<string, unknown>[];
}

interface ChartContent extends TableContent {
  title: string;
  /** R3-B：决策引擎给定的图型（"table"/"bar"/...）；为 null 时是历史 v1 数据，表格兜底. */
  chartType: string | null;
  /** R3-B：报告侧 buildReportChart 预渲染的 ECharts option dict；TABLE 或失败时为 null. */
  echartsOption: Record<string, unknown> | null;
}

/** R3-B：兼容旧 payload（无 chartType / 无 echartsOption）+ 新 payload. */
function asChartContent(value: unknown): ChartContent {
  if (!isRecord(value)) {
    return { columns: [], rows: [], title: "", chartType: null, echartsOption: null };
  }
  const rows = Array.isArray(value.data)
    ? value.data.filter(isRecord)
    : Array.isArray(value.rows)
      ? value.rows.filter(isRecord)
      : [];
  const columns = asStringArray(value.columns ?? (rows[0] ? Object.keys(rows[0]) : []));
  const title = typeof value.title === "string" ? value.title : "";
  const chartType =
    typeof value.chartType === "string" && value.chartType.length > 0 ? value.chartType : null;
  const echartsOption = isRecord(value.echartsOption) ? value.echartsOption : null;
  return { columns, rows, title, chartType, echartsOption };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function asTableContent(value: unknown): TableContent {
  if (!isRecord(value)) return { columns: [], rows: [] };
  const rows = Array.isArray(value.rows)
    ? value.rows.filter(isRecord)
    : [];
  return { columns: asStringArray(value.columns), rows };
}

function cellText(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

function DataTable({ content }: { content: TableContent }) {
  const { columns, rows } = content;
  if (columns.length === 0) return null;
  return (
    <table>
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column}>{column}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={index}>
            {columns.map((column) => (
              <td key={column}>{cellText(row[column])}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function renderBlock(block: ReportBlock): ReactNode {
  switch (block.type) {
    case "text":
      return <p>{typeof block.content === "string" ? block.content : ""}</p>;
    case "bullet_list":
      return (
        <ul>
          {asStringArray(block.content).map((item, index) => (
            <li key={index}>{item}</li>
          ))}
        </ul>
      );
    case "table":
      return <DataTable content={asTableContent(block.content)} />;
    case "chart": {
      const chart = asChartContent(block.content);
      // R3-B：非 TABLE 且有预渲染 echartsOption ⇒ 真画 ECharts；其他（v1 历史
      // chartType=null、TABLE/kpi、报错降级）保持表格兜底，保证前端契约不变。
      const canRenderChart =
        isChartType(chart.chartType) &&
        chart.chartType !== "table" &&
        chart.chartType !== "kpi" &&
        chart.echartsOption !== null;
      return (
        <div>
          {chart.title ? <p>{chart.title}</p> : null}
          {canRenderChart ? (
            <EChart
              option={chart.echartsOption as unknown as Parameters<typeof EChart>[0]["option"]}
              testId={`report-chart-${chart.chartType ?? "unknown"}`}
            />
          ) : null}
          <DataTable content={chart} />
        </div>
      );
    }
    default:
      return null;
  }
}

export function ReportRenderer({ payload }: ReportRendererProps) {
  const { t } = useTranslation();
  return (
    <article>
      <h2>{payload.title}</h2>
      <p>{payload.question}</p>
      {payload.sections.map((section) => (
        <section key={section.id}>
          <h3>{section.title}</h3>
          {section.blocks.length === 0 ? <p>{t("research.report.empty")}</p> : null}
          {section.blocks.map((block, index) => (
            <div key={index} data-testid="report-block" data-block-type={block.type}>
              {renderBlock(block)}
            </div>
          ))}
        </section>
      ))}
      <EvidencePanel evidence={payload.evidence} />
    </article>
  );
}

// ---------------------------------------------------------------------------
// opaque payload → ReportPayload 收窄（feat-research-entry Task 12）
// ---------------------------------------------------------------------------
// 报告 payload 以 `Record<string, unknown>` 存于 ResearchReport（JSONB），渲染前
// 必须收窄成类型安全的 ReportPayload。本函数是唯一收窄点：ResearchReportPage
// 与 ResearchCompareView 都经它收窄，避免两套解析漂移。

const BLOCK_TYPES: ReadonlySet<string> = new Set<string>([
  "text",
  "chart",
  "table",
  "bullet_list",
]);

function parseSourceRef(value: unknown): ReportSourceRef | null {
  if (!isRecord(value)) return null;
  return {
    kind: typeof value.kind === "string" ? value.kind : "",
    refId: typeof value.refId === "string" ? value.refId : "",
    label: typeof value.label === "string" ? value.label : "",
  };
}

function parseBlock(value: unknown): ReportBlock | null {
  if (!isRecord(value)) return null;
  if (typeof value.type !== "string" || !BLOCK_TYPES.has(value.type)) return null;
  return {
    type: value.type as ReportBlock["type"],
    content: value.content,
    sourceRefs: Array.isArray(value.sourceRefs)
      ? value.sourceRefs
          .map(parseSourceRef)
          .filter((ref): ref is ReportSourceRef => ref !== null)
      : [],
  };
}

function parseSection(value: unknown): ReportSection | null {
  if (!isRecord(value)) return null;
  const blocks = Array.isArray(value.blocks)
    ? value.blocks.map(parseBlock).filter((block): block is ReportBlock => block !== null)
    : [];
  return {
    id: typeof value.id === "string" ? value.id : "",
    kind: typeof value.kind === "string" ? value.kind : "",
    title: typeof value.title === "string" ? value.title : "",
    blocks,
  };
}

function parseStepEvidence(value: unknown): StepEvidence | null {
  if (!isRecord(value)) return null;
  return {
    index: typeof value.index === "number" ? value.index : 0,
    type: typeof value.type === "string" ? value.type : "",
    sql: typeof value.sql === "string" ? value.sql : null,
    status: typeof value.status === "string" ? value.status : "",
    error: typeof value.error === "string" ? value.error : null,
    summary: isRecord(value.summary) ? value.summary : null,
    schemaVersion: typeof value.schemaVersion === "number" ? value.schemaVersion : null,
  };
}

/** Task 8：evidence 收窄；缺省 / 非法 → undefined（旧 payload 不破）。 */
function parseEvidence(value: unknown): ReportEvidence | undefined {
  if (!isRecord(value)) return undefined;
  const stepResults = Array.isArray(value.stepResults)
    ? value.stepResults
        .map(parseStepEvidence)
        .filter((step): step is StepEvidence => step !== null)
    : [];
  return { stepResults };
}

export function parseReportPayload(value: Record<string, unknown>): ReportPayload {
  const sections = Array.isArray(value.sections)
    ? value.sections
        .map(parseSection)
        .filter((section): section is ReportSection => section !== null)
    : [];
  return {
    sessionId: typeof value.sessionId === "string" ? value.sessionId : "",
    turnId: typeof value.turnId === "string" ? value.turnId : "",
    mode: typeof value.mode === "string" ? value.mode : "research",
    title: typeof value.title === "string" ? value.title : "",
    question: typeof value.question === "string" ? value.question : "",
    sections,
    findingsRef: Array.isArray(value.findingsRef) ? value.findingsRef : [],
    evidence: parseEvidence(value.evidence),
  };
}
