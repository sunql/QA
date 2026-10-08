/** KPI 指标卡（图表决策引擎，决策 7）。
 *
 * `chartType=kpi` 的负载不是 ECharts option，而是
 * `{kpi: {label, value, unit, delta}}` —— 单值没有坐标轴可画，画成图反而是噪音。
 * 这里用 antd `Statistic` 出卡，颜色走主题 token（与服务端无关：服务端只发结构）。
 *
 * `delta`（同环比差值）一期恒为 null，字段留着：前端按「有值才渲染」处理，
 * 后续引擎补上时这里零改动。
 */
import { Card, Statistic } from "antd";
import { useThemeStore } from "../../stores/themeStore";
import { DARK_TOKEN, LIGHT_TOKEN } from "../../theme/tokens";

export interface KpiPayload {
  label: string;
  /** 已被 parseKpiPayload 收窄为数值：没有值就整卡不渲染。 */
  value: number;
  unit?: string | null;
  delta?: number | null;
}

interface KpiCardProps {
  kpi: KpiPayload;
}

/**
 * 数值收窄：数字与十进制字符串都接受（后端是 Python，JSON 里可能是 `"0.954"`），
 * 非有限值（NaN / Infinity）一律 null —— 否则 antd Statistic 会画出「NaN」字样。
 */
function asFiniteNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/** 从 chartOption 里取 KPI 负载；形状不符返回 null（系统边界，不信任后端 JSON）。 */
export function parseKpiPayload(chartOption: Record<string, unknown> | null | undefined): KpiPayload | null {
  const raw = chartOption?.kpi;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  if (typeof record.label !== "string") return null;
  const value = asFiniteNumber(record.value);
  if (value === null) return null; // 无值不发空壳卡（与后端降级口径一致）
  const unit = typeof record.unit === "string" ? record.unit : null;
  const delta = asFiniteNumber(record.delta);
  return { label: record.label, value, unit, delta };
}

export default function KpiCard({ kpi }: KpiCardProps) {
  const isDark = useThemeStore((state) => state.isDark);
  const token = isDark ? DARK_TOKEN : LIGHT_TOKEN;

  return (
    <Card size="small" styles={{ body: { padding: "16px 20px" } }}>
      <Statistic
        title={kpi.label}
        value={kpi.value}
        suffix={kpi.unit ?? undefined}
        valueStyle={{ color: token.colorPrimary }}
      />
      {kpi.delta !== null && kpi.delta !== undefined ? (
        <div style={{ marginTop: 4, fontSize: 12, color: kpi.delta >= 0 ? token.colorSuccess : token.colorError }}>
          {kpi.delta >= 0 ? "+" : ""}
          {kpi.delta}
        </div>
      ) : null}
    </Card>
  );
}
