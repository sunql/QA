/** 数据质量评估报告 — 综合评分趋势折线图（feat-dq-evaluation-report，Phase 6）。
 *
 * 数据：snapshot.trend.series[]（date / overall_score）；snapshot.trend.available=false 时显示空态。
 *
 * trend 来自 `EvaluationReportService.getTrendSeries`——按 window_days 回看历史报告，
 * 不足 N 份则 series 为空数组，available=false。
 */

import { useMemo } from "react";
import { Card, Empty } from "antd";
import EChart from "./EChart";
import type { EChartsOption } from "echarts";
import { useTranslation } from "../i18n";

interface TrendPoint {
  // snapshot.trend.series[] 是 snake_case（dict[str,Any] 不走 Pydantic alias_generator），
  // 详见 [[dq-eval-report-snapshot-snake-case]]。
  date: string;
  overall_score: number | null;
}

interface TrendShape {
  available: boolean;
  window_days?: number;
  series: TrendPoint[];
}

interface ReportTrendLineProps {
  trend: TrendShape | null;
  height?: number;
}

export default function ReportTrendLine({
  trend,
  height = 280,
}: ReportTrendLineProps) {
  const { t } = useTranslation();

  const option: EChartsOption | null = useMemo(() => {
    if (!trend || !trend.available || trend.series.length === 0) return null;
    const xData = trend.series.map((p) => p.date);
    const series = trend.series.map((p) =>
      typeof p.overall_score === "number"
        ? Number(p.overall_score.toFixed(2))
        : null,
    );

    return {
      tooltip: {
        trigger: "axis",
        formatter: (params) => {
          const arr = params as unknown as Array<{
            axisValue: string;
            data: number | null;
          }>;
          const item = arr[0];
          const v = item.data;
          return `${item.axisValue}<br/>评分: ${v == null ? "—" : `${v}%`}`;
        },
      },
      grid: { left: 50, right: 30, top: 30, bottom: 40 },
      xAxis: { type: "category", data: xData, boundaryGap: false },
      yAxis: { type: "value", max: 100, axisLabel: { formatter: "{value}%" } },
      series: [
        {
          type: "line",
          data: series,
          smooth: true,
          itemStyle: { color: "#1677ff" },
          connectNulls: true,
        },
      ],
    };
  }, [trend]);

  if (!option) {
    return (
      <Card
        size="small"
        title={t("dataQuality.reports.detail.charts.trendLine")}
      >
        <Empty
          description={
            trend?.available === false
              ? t("dataQuality.reports.detail.charts.trendUnavailable")
              : t("dataQuality.reports.detail.charts.empty")
          }
        />
      </Card>
    );
  }

  return (
    <Card
      size="small"
      title={t("dataQuality.reports.detail.charts.trendLine")}
    >
      <EChart option={option} height={height} testId="chart-trend-line" />
    </Card>
  );
}