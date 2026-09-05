/** MetricCard 指标卡组件（Phase C）。
 *
 * 设计目标：
 * - 截图风格的指标卡：顶部 3px 渐变光带 + 大数字 + 标签
 * - 颜色由 status 驱动（success / warning / error / offline），对应 CSS 变量
 * - 支持可选 icon（左上角图标）+ unit（数字后的单位）
 * - 数字默认千分位格式化（format=false 可关闭）
 *
 * 何时使用：
 * - Dashboard 顶部 KPI 数字（设备总数 / 总冲次数 / 在线率等）
 * - 任意需要"大数字 + 标签 + 状态色"组合的场景
 *
 * 何时不用：
 * - 已有 antd Statistic 包装的简单数字（避免重复抽象）
 * - 需要图表/趋势的指标（用 ECharts）
 */
import type { ReactNode } from "react";
import styles from "../../styles/dashboard.module.css";
import type { StatusBadgeStatus } from "./StatusBadge";

type MetricStatus = StatusBadgeStatus;

interface MetricCardProps {
  value: number;
  label: ReactNode;
  unit?: ReactNode;
  /** 顶部渐变光带颜色对应的语义状态，默认 success（青色） */
  status?: MetricStatus;
  /** 数字是否按千分位格式化（默认 true） */
  format?: boolean;
  /** 左上角图标（可选） */
  icon?: ReactNode;
  /** 追加 className */
  className?: string;
}

const STATUS_CLASS: Record<MetricStatus, string> = {
  success: styles.metricCardSuccess,
  warning: styles.metricCardWarning,
  error: styles.metricCardError,
  offline: styles.metricCardOffline,
};

function formatNumber(value: number, enabled: boolean): string {
  if (!enabled) return String(value);
  return value.toLocaleString("en-US");
}

export default function MetricCard({
  value,
  label,
  unit,
  status = "success",
  format = true,
  icon,
  className,
}: MetricCardProps) {
  const statusClass = STATUS_CLASS[status] ?? STATUS_CLASS.success;
  const cls = [styles.metricCard, statusClass, className].filter(Boolean).join(" ");

  return (
    <div className={cls} data-status={status}>
      {icon && (
        <div data-testid="metric-card-icon" style={{ marginBottom: 8 }}>
          {icon}
        </div>
      )}
      <p className={styles.metricCardNumber}>
        {formatNumber(value, format)}
        {unit && <span className={styles.metricCardUnit}>{unit}</span>}
      </p>
      <div className={styles.metricCardLabel}>{label}</div>
    </div>
  );
}