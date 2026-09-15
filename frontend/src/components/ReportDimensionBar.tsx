/** 数据质量评估报告 — 6 维度分柱形图（feat-dq-evaluation-report，Phase 6）。
 *
 * 数据：`Record<string, number | null>`，键为 completeness/validity/...
 * 空值（timeliness=null 等）用 0 显示并标注"—"。
 */

import { useMemo } from "react";
import { Card, Empty } from "antd";
import EChart from "./EChart";
import type { EChartsOption } from "echarts";
import { useTranslation } from "../i18n";

interface ReportDimensionBarProps {
  dimensions: Record<string, number | null>;
  height?: number;
}

const DIMENSION_KEYS = [
  "completeness",
  "validity",
  "uniqueness",
  "consistency",
  "timeliness",
  "referential",
] as const;

export default function ReportDimensionBar({
  dimensions,
  height = 280,
}: ReportDimensionBarProps) {
  const { t } = useTranslation();

  const option: EChartsOption = useMemo(() => {
    const labels = DIMENSION_KEYS.map((k) =>
      t(`dataQuality.scores.dimensions.${k}`),
    );
    const values = DIMENSION_KEYS.map((k) => {
      const v = dimensions[k];
      return typeof v === "number" ? Number(v.toFixed(2)) : 0;
    });

    return {
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: (params) => {
          const arr = params as unknown as Array<{
            axisValue: string;
            data: number;
          }>;
          const item = arr[0];
          const realValue =
            dimensions[DIMENSION_KEYS[labels.indexOf(item.axisValue)]];
          const display = realValue == null ? "—" : `${realValue.toFixed(2)}%`;
          return `${item.axisValue}<br/>得分: ${display}`;
        },
      },
      grid: { left: 50, right: 30, top: 30, bottom: 40 },
      xAxis: {
        type: "category",
        data: labels,
        axisLabel: { interval: 0, rotate: 0 },
      },
      yAxis: {
        type: "value",
        max: 100,
        axisLabel: { formatter: "{value}%" },
      },
      series: [
        {
          type: "bar",
          data: values,
          itemStyle: { color: "#1677ff" },
          barWidth: 32,
        },
      ],
    };
  }, [dimensions, t]);

  const allEmpty = DIMENSION_KEYS.every((k) => dimensions[k] == null);

  return (
    <Card
      size="small"
      title={t("dataQuality.reports.detail.charts.dimensionBar")}
    >
      {allEmpty ? (
        <Empty description={t("dataQuality.reports.detail.charts.empty")} />
      ) : (
        <EChart option={option} height={height} testId="chart-dimension-bar" />
      )}
    </Card>
  );
}