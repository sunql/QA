/** KPI 卡（feat-dq-evaluation-report，Phase 6）。
 *
 * 用 antd `Card` + `Statistic` 实现；带可选 `trend` 描述相对前一份的变化
 * （Phase 8 对比场景会用；Phase 6 默认不显示）。
 */

import { ArrowDownOutlined, ArrowUpOutlined } from "@ant-design/icons";
import { Card, Statistic, Tag } from "antd";
import type { ReactNode } from "react";

interface KpiCardProps {
  label: string;
  value: number | string;
  suffix?: ReactNode;
  precision?: number;
  trend?: {
    delta: number;
    direction: "up" | "down";
  };
  testId?: string;
}

export default function KpiCard({
  label,
  value,
  suffix,
  precision,
  trend,
  testId,
}: KpiCardProps) {
  return (
    <Card size="small" data-testid={testId}>
      <Statistic title={label} value={value} suffix={suffix} precision={precision} />
      {trend ? (
        <div style={{ marginTop: 8 }}>
          <Tag
            color={trend.direction === "up" ? "green" : "red"}
            icon={
              trend.direction === "up" ? (
                <ArrowUpOutlined />
              ) : (
                <ArrowDownOutlined />
              )
            }
          >
            {trend.delta.toFixed(2)}
          </Tag>
          <span style={{ color: "#888", fontSize: 12, marginLeft: 4 }}>
            较前一份
          </span>
        </div>
      ) : null}
    </Card>
  );
}