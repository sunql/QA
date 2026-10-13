/** 报告执行证据面板（feat-research-session-evidence Task 8）。
 *
 * 只读渲染 `payload.evidence.stepResults[]`：每步「步骤 N：type」/ 状态标签 /
 * SQL（`<pre>`）/ 摘要（标量键值对）。`evidence` 缺省或 `stepResults` 为空时**不渲染**
 * —— 旧 payload 无 `evidence`（Task 8 之前归档的报告），绝不因此炸渲染。
 *
 * 数据来源：`ReportPlanner.compose` 自查 `research_step_result`（跨本 turn 的所有
 * run_state 版本聚合，见后端 `loadStepResultsByTurn`）。面板**不含**原始行（只在 DB）。
 */
import { Tag } from "antd";
import { useTranslation } from "react-i18next";
import type { ReportEvidence, StepEvidence } from "./ReportRenderer";
import { timeRangeLabels, topNTableShape } from "./evidenceSummary";

interface EvidencePanelProps {
  evidence?: ReportEvidence;
}

// 状态 → Tag 颜色（success / failed / empty 三分支，对齐后端 STEP_STATUS_*）。
const STATUS_COLORS: Readonly<Record<string, string>> = {
  success: "green",
  failed: "red",
  empty: "default",
};

/** 摘要里只取标量键值（列表 / 嵌套对象跳过）—— 面板展示用。 */
function summaryPairs(summary: Record<string, unknown> | null): Array<[string, string]> {
  if (!summary) return [];
  return Object.entries(summary)
    .filter(([, value]) => value !== null && value !== undefined && typeof value !== "object")
    .map(([key, value]) => [key, String(value)]);
}

/** 状态中文标签；未知状态回落原始状态串（不渲染 key 本身）。 */
function statusLabel(t: (key: string) => string, status: string): string {
  const key = `research.evidence.status.${status}`;
  const label = t(key);
  return label === key ? status : label;
}

/** `summary.topN` ⇒ 紧凑只读表；如实标注这是**结果行按度量排名**（非原图）。
 *
 * 后端 `_topNRows` 是「按度量列**全排序**取前 N 并打 `rank`」的**确定性排名**，**不是采样**
 * —— 真正的采样是同 payload 里的 `sample_rows`（未排序的前 N 行）。文案与注释都不得再把
 * topN 说成「采样」。
 */
function TopNTable({ table, caption }: { table: ReturnType<typeof topNTableShape>; caption: string }) {
  if (!table) return null;
  return (
    <div data-testid="evidence-topn">
      <p>{caption}</p>
      <table>
        <thead>
          <tr>
            {table.columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {table.columns.map((column) => (
                <td key={column}>{row[column] ?? ""}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EvidenceStep({ step }: { step: StepEvidence }) {
  const { t } = useTranslation();
  const pairs = summaryPairs(step.summary);
  const topN = topNTableShape(step.summary);
  const timeRange = timeRangeLabels(step.summary);
  return (
    <div data-testid="evidence-step">
      <p>{`${t("research.evidence.step", { index: step.index })}：${step.type}`}</p>
      <Tag color={STATUS_COLORS[step.status] ?? "default"}>{statusLabel(t, step.status)}</Tag>
      {step.sql ? <pre>{step.sql}</pre> : null}
      {step.error ? <p>{step.error}</p> : null}
      {pairs.length > 0 ? (
        <ul>
          {pairs.map(([key, value]) => (
            <li key={key}>{`${key}: ${value}`}</li>
          ))}
        </ul>
      ) : null}
      <TopNTable table={topN} caption={t("research.evidence.topNReplay")} />
      {timeRange ? (
        <p data-testid="evidence-time-range">
          {t("research.evidence.timeRange", { start: timeRange.start, end: timeRange.end })}
        </p>
      ) : null}
    </div>
  );
}

export function EvidencePanel({ evidence }: EvidencePanelProps) {
  const { t } = useTranslation();
  const steps = evidence?.stepResults ?? [];
  if (steps.length === 0) return null;
  return (
    <section data-testid="evidence-panel">
      <h3>{t("research.evidence.title")}</h3>
      {steps.map((step, index) => (
        <EvidenceStep key={`${step.index}-${index}`} step={step} />
      ))}
    </section>
  );
}
