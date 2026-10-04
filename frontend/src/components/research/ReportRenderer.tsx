/** 研究报告渲染器（feat-research-entry Task 10）。
 *
 * 只读渲染 `payload.sections[].blocks[]`，块类型对齐 backend report_planner.py 的
 * JSON 形状（camelCase）：
 * - text → 段落
 * - table → 原生 <table>（可测、不依赖 antd Table 的 rowKey）
 * - chart → 表格兜底（v1 chartType=null，图表接入留后续）
 * - bullet_list → <ul>/<li>
 * 每个块带 data-testid="report-block" + data-block-type，供顺序断言。
 */
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

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

export interface ReportPayload {
  sessionId: string;
  turnId: string;
  mode: string;
  title: string;
  question: string;
  sections: ReportSection[];
  findingsRef?: unknown[];
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

function asChartContent(value: unknown): ChartContent {
  const table = asTableContent(value);
  const title = isRecord(value) && typeof value.title === "string" ? value.title : "";
  return { ...table, title };
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
      return (
        <div>
          {chart.title ? <p>{chart.title}</p> : null}
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
  };
}
