/** 研究对比视图（feat-research-entry Task 12）。
 *
 * 纯展示组件：props 进 `sessionIds`（必填），并行拉各 session 的「最新 published」
 * 报告并排渲染四行（标题 / 执行摘要 / 关键发现 claim+confidence / 方法学）。
 * 某 session 无 published 报告时其列显示「未生成报告」占位，不破坏对齐。
 * 不读路由 —— 解析 query 是 ResearchComparePage 的事。
 */
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Empty, Spin } from "antd";
import { useTranslation } from "react-i18next";
import { listReports, getReport } from "../../api/research";
import type { ResearchReportSummary } from "../../types/research";
import { parseReportPayload } from "./ReportRenderer";
import type { ReportPayload } from "./ReportRenderer";

export interface ResearchCompareViewProps {
  sessionIds: string[];
}

interface CompareFinding {
  claim: string;
  confidence: number | null;
}

interface CompareRows {
  title: string;
  executiveSummary: string;
  findings: CompareFinding[];
  methodology: string;
}

interface CompareCell {
  sessionId: string;
  rows: CompareRows | null; // null = 无 published 报告
}

type RowKind = "title" | "executiveSummary" | "findings" | "methodology";

const ROWS: ReadonlyArray<{ kind: RowKind; labelKey: string }> = [
  { kind: "title", labelKey: "research.compare.row.title" },
  { kind: "executiveSummary", labelKey: "research.compare.row.executiveSummary" },
  { kind: "findings", labelKey: "research.compare.row.findings" },
  { kind: "methodology", labelKey: "research.compare.row.methodology" },
];

/** 最新 published 版本：status=published 的 version 最大者；无则 null。 */
export function latestPublishedVersion(summaries: ResearchReportSummary[]): number | null {
  const published = summaries.filter((item) => item.status === "published");
  if (published.length === 0) return null;
  return Math.max(...published.map((item) => item.version));
}

function sectionText(payload: ReportPayload, kind: string): string {
  const section = payload.sections.find((item) => item.kind === kind);
  if (!section) return "";
  return section.blocks
    .filter((block) => block.type === "text" && typeof block.content === "string")
    .map((block) => block.content as string)
    .join("\n");
}

function findingFrom(value: unknown): CompareFinding | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  return {
    claim: typeof item.claim === "string" ? item.claim : "",
    confidence: typeof item.confidence === "number" ? item.confidence : null,
  };
}

function extractRows(payload: ReportPayload): CompareRows {
  const findings = (payload.findingsRef ?? [])
    .map(findingFrom)
    .filter((item): item is CompareFinding => item !== null);
  return {
    title: payload.title,
    executiveSummary: sectionText(payload, "executive_summary"),
    findings,
    methodology: sectionText(payload, "methodology"),
  };
}

async function fetchCell(sessionId: string): Promise<CompareCell> {
  try {
    const summaries = await listReports(sessionId);
    const version = latestPublishedVersion(summaries);
    if (version === null) return { sessionId, rows: null };
    const report = await getReport(sessionId, version);
    return { sessionId, rows: extractRows(parseReportPayload(report.payload)) };
  } catch {
    return { sessionId, rows: null };
  }
}

export function ResearchCompareView({ sessionIds }: ResearchCompareViewProps) {
  const { t } = useTranslation();
  const [cells, setCells] = useState<CompareCell[]>([]);
  const [loading, setLoading] = useState(true);

  const idsKey = sessionIds.join(",");

  useEffect(() => {
    if (sessionIds.length === 0) {
      setCells([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    void (async () => {
      const results = await Promise.all(sessionIds.map((id) => fetchCell(id)));
      if (!cancelled) {
        setCells(results);
        setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // sessionIds 用 idsKey 做稳定依赖，避免父级每次渲染新建数组引用导致重复拉取。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey]);

  if (sessionIds.length === 0) {
    return <Empty description={t("research.compare.empty")} />;
  }

  if (loading) {
    return (
      <div style={{ padding: 48, textAlign: "center" }}>
        <Spin />
      </div>
    );
  }

  const renderCell = (cell: CompareCell, kind: RowKind): ReactNode => {
    if (cell.rows === null) {
      return <span>{t("research.compare.noReport")}</span>;
    }
    const rows = cell.rows;
    if (kind === "findings") {
      return (
        <ul>
          {rows.findings.map((finding, index) => {
            const text =
              finding.confidence !== null
                ? `${finding.claim}（${t("research.compare.confidence")} ${finding.confidence}）`
                : finding.claim;
            return <li key={index}>{text}</li>;
          })}
        </ul>
      );
    }
    return <span>{rows[kind]}</span>;
  };

  return (
    <table data-testid="research-compare">
      <thead>
        <tr>
          <th scope="col" aria-hidden="true" />
          {cells.map((cell) => (
            <th key={cell.sessionId} scope="col">
              {cell.sessionId}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {ROWS.map(({ kind, labelKey }) => (
          <tr key={kind}>
            <th scope="row">{t(labelKey)}</th>
            {cells.map((cell) => (
              <td key={cell.sessionId}>{renderCell(cell, kind)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
