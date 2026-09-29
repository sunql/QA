/** 数据质量评估报告 — 规则通过情况饼图（feat-dq-evaluation-report，Phase 6）。
 *
 * 数据：snapshot.tables[].rules[] 中按 status 计数。
 * status ∈ {PASS, FAIL, ERROR, PENDING}，PASS 绿色 / FAIL 红色 / 其他灰。
 */

import { useMemo } from "react";
import { Card, Empty } from "antd";
import EChart from "./EChart";
import type { EChartsOption } from "echarts";
import { useTranslation } from "../i18n";

interface RuleLite {
  status: string;
}

interface ReportPassFailPieProps {
  rules: RuleLite[];
  height?: number;
}

const COLOR_MAP: Record<string, string> = {
  PASS: "#52c41a",
  FAIL: "#f5222d",
  ERROR: "#faad14",
  PENDING: "#bfbfbf",
};

const LABEL_KEY_MAP: Record<string, string> = {
  PASS: "dataQuality.evalResult.pass",
  FAIL: "dataQuality.evalResult.fail",
  ERROR: "common.error",
  PENDING: "dataQuality.reports.detail.overallStatusLabels.PENDING",
};

export default function ReportPassFailPie({
  rules,
  height = 280,
}: ReportPassFailPieProps) {
  const { t } = useTranslation();

  const option: EChartsOption = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const r of rules) {
      counts[r.status] = (counts[r.status] ?? 0) + 1;
    }
    const data = Object.entries(counts).map(([status, value]) => ({
      name: t(LABEL_KEY_MAP[status] ?? status),
      value,
      itemStyle: { color: COLOR_MAP[status] ?? "#bfbfbf" },
    }));

    return {
      tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
      legend: { bottom: 0 },
      series: [
        {
          type: "pie",
          radius: ["40%", "70%"],
          avoidLabelOverlap: true,
          label: { show: true, formatter: "{b}\n{c}" },
          data,
        },
      ],
    };
  }, [rules, t]);

  if (rules.length === 0) {
    return (
      <Card
        size="small"
        title={t("dataQuality.reports.detail.charts.passFailPie")}
      >
        <Empty description={t("dataQuality.reports.detail.charts.empty")} />
      </Card>
    );
  }

  return (
    <Card
      size="small"
      title={t("dataQuality.reports.detail.charts.passFailPie")}
    >
      <EChart option={option} height={height} testId="chart-pass-fail-pie" />
    </Card>
  );
}