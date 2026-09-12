/** RoutingMetricsPage — 路由分层指标监控面板（Task 5.3 — feat-complex-metric-pipeline）。
 *
 * UI:
 * - 4 个统计卡片：L1/L2/L3/L4 hit count + avg duration
 * - ECharts 饼图：各层命中率占比
 * - ECharts 折线图：时间趋势（mock 数据，TODO: 真实 timeseries endpoint）
 * - 时间窗口选择器：1d / 7d / 30d（UI 占位，不驱动真实过滤）
 *
 * 后端 endpoint 未实现（见 TODO 注释），前端走 mock 数据。
 */
import { useCallback, useEffect, useState } from "react";
import { Card, Col, Row, Select, Space, Spin, Alert, Empty } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";
import { useTranslation } from "../i18n";
import type { LayerMetric, RoutingMetricsSnapshot } from "../types/routingMetrics";

// TODO: Wire to real endpoint GET /api/v1/routing-metrics/snapshot?since=...
// when backend implements RoutingMetricsService.get_snapshot().
async function fetchRoutingMetrics(since: Date): Promise<RoutingMetricsSnapshot> {
  // Mock: in real implementation, replace with httpClient.get("/routing-metrics/snapshot", { params: { since } })
  // eslint-disable-next-line no-console
  console.warn("TODO: replace mock with real /api/v1/routing-metrics/snapshot endpoint");
  const params = new URLSearchParams({ since: since.toISOString() });
  const res = await global.fetch(`/api/v1/routing-metrics/snapshot?${params}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<RoutingMetricsSnapshot>;
}

type TimeWindow = "1d" | "7d" | "30d";

const TIME_WINDOW_OPTIONS = [
  { value: "1d", label: "routingMetrics.timeWindow.1d" },
  { value: "7d", label: "routingMetrics.timeWindow.7d" },
  { value: "30d", label: "routingMetrics.timeWindow.30d" },
] as const;

const LAYER_COLORS: Record<"L1" | "L2" | "L3" | "L4", string> = {
  L1: "#52c41a",
  L2: "#1890ff",
  L3: "#fa8c16",
  L4: "#f5222d",
};

function buildPieOption(snapshot: RoutingMetricsSnapshot): EChartsOption {
  return {
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    legend: { bottom: 0, type: "scroll" },
    series: [
      {
        name: "routingMetrics.chart.pieTitle",
        type: "pie",
        radius: ["40%", "70%"],
        data: snapshot.layerDistribution.map((l) => ({
          name: l.layer,
          value: l.hitCount,
          itemStyle: { color: LAYER_COLORS[l.layer] },
        })),
      },
    ],
  };
}

/** Mock trend data — TODO: replace with real timeseries endpoint response. */
function buildLineOption(_snapshot: RoutingMetricsSnapshot, t: (key: string) => string): EChartsOption {
  const days = 7;
  const date: string[] = [];
  const l1: number[] = [];
  const l2: number[] = [];
  const l3: number[] = [];
  const l4: number[] = [];

  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86_400_000);
    date.push(d.toLocaleDateString("en-US", { month: "short", day: "numeric" }));
    // Mock: slight random variation per day
    const base = 1000 + Math.floor(Math.random() * 200);
    l1.push(Math.floor(base * 0.52));
    l2.push(Math.floor(base * 0.35));
    l3.push(Math.floor(base * 0.09));
    l4.push(Math.floor(base * 0.04));
  }

  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["L1", "L2", "L3", "L4"], bottom: 0 },
    xAxis: { type: "category", data: date },
    yAxis: { type: "value", name: t("routingMetrics.chart.yAxisLabel") },
    series: [
      { name: "L1", type: "line", smooth: true, data: l1, itemStyle: { color: LAYER_COLORS.L1 } },
      { name: "L2", type: "line", smooth: true, data: l2, itemStyle: { color: LAYER_COLORS.L2 } },
      { name: "L3", type: "line", smooth: true, data: l3, itemStyle: { color: LAYER_COLORS.L3 } },
      { name: "L4", type: "line", smooth: true, data: l4, itemStyle: { color: LAYER_COLORS.L4 } },
    ],
  };
}

interface LayerCardProps {
  layer: LayerMetric;
  t: (key: string) => string;
}

function LayerCard({ layer, t }: LayerCardProps) {
  const label = t(`routingMetrics.layer.${layer.layer}` as const);
  const color = LAYER_COLORS[layer.layer] ?? "#1890ff";

  return (
    <Card
      size="small"
      style={{ borderTop: `3px solid ${color}` }}
      data-testid={`layer-card-${layer.layer}`}
    >
      <p style={{ fontSize: 12, color: "#888", marginBottom: 4 }}>{label}</p>
      <p style={{ fontSize: 28, fontWeight: 700, margin: "0 0 4px", color }}>
        {layer.hitCount.toLocaleString("en-US")}
      </p>
      <p style={{ fontSize: 12, color: "#555", margin: 0 }}>
        {t("routingMetrics.metric.avgDuration")}: {layer.avgDurationMs.toFixed(1)} ms
      </p>
      <p style={{ fontSize: 12, color: "#555", margin: "2px 0 0" }}>
        {t("routingMetrics.metric.avgTokenCost")}: ${layer.avgTokenCost.toFixed(3)}
      </p>
    </Card>
  );
}

export default function RoutingMetricsPage(): JSX.Element {
  const { t } = useTranslation();
  const [snapshot, setSnapshot] = useState<RoutingMetricsSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [timeWindow, setTimeWindow] = useState<TimeWindow>("7d");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const now = new Date();
      const since = new Date(now.getTime() - (timeWindow === "1d" ? 1 : timeWindow === "7d" ? 7 : 30) * 86_400_000);
      const data = await fetchRoutingMetrics(since);
      setSnapshot(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [timeWindow]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div style={{ padding: 16 }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <div>
          <h2 style={{ margin: 0 }}>{t("routingMetrics.title")}</h2>
          <p style={{ margin: "4px 0 0", color: "#888", fontSize: 13 }}>
            {t("routingMetrics.subtitle")}
          </p>
        </div>
        <Space>
          <Select
            value={timeWindow}
            onChange={(v) => setTimeWindow(v as TimeWindow)}
            style={{ width: 120 }}
            options={TIME_WINDOW_OPTIONS.map((o) => ({
              value: o.value,
              label: t(o.label),
            }))}
          />
          <ReloadOutlined onClick={() => void load()} style={{ fontSize: 16, cursor: "pointer" }} />
        </Space>
      </div>

      {/* Error state */}
      {error !== null && (
        <Alert
          type="error"
          message={t("routingMetrics.error")}
          description={error}
          style={{ marginBottom: 16 }}
          showIcon
        />
      )}

      {/* Loading */}
      {loading && <Spin style={{ display: "block", textAlign: "center", margin: "40px 0" }} />}

      {/* Content */}
      {!loading && error === null && snapshot !== null && (
        <>
          {/* 4 layer cards */}
          <Row gutter={[12, 12]} style={{ marginBottom: 20 }}>
            {snapshot.layerDistribution.map((layer) => (
              <Col key={layer.layer} xs={12} sm={12} md={6}>
                <LayerCard layer={layer} t={t} />
              </Col>
            ))}
          </Row>

          {/* Charts row */}
          <Row gutter={[12, 12]}>
            <Col xs={24} md={12}>
              <Card size="small" title={t("routingMetrics.chart.pieTitle")}>
                <ReactECharts
                  option={buildPieOption(snapshot)}
                  style={{ height: 280 }}
                  opts={{ renderer: "canvas" }}
                />
              </Card>
            </Col>
            <Col xs={24} md={12}>
              <Card size="small" title={t("routingMetrics.chart.lineTitle")}>
                <ReactECharts
                  option={buildLineOption(snapshot, t)}
                  style={{ height: 280 }}
                  opts={{ renderer: "canvas" }}
                />
              </Card>
            </Col>
          </Row>
        </>
      )}

      {/* Empty state */}
      {!loading && error === null && snapshot !== null && snapshot.totalQueries === 0 && (
        <Empty description={t("routingMetrics.empty")} style={{ marginTop: 60 }} />
      )}
    </div>
  );
}
