/** 数据质量评估报告 — 对比页（feat-dq-evaluation-report，Phase 8a）。
 *
 * URL: /data-quality/reports/compare?ids=1,2
 * - 两 Select 选报告（URL 优先）
 * - 4 个对比 KPI（左右 + Δ）
 * - 维度 delta 表（6 行）
 * - 规则 delta 表（按 status_change 着色）
 * - 错误状态：左右任一缺失 / loading
 */

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Alert,
  App,
  Card,
  Col,
  Empty,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  compareReports,
  listReports,
} from "../api/evaluationReport";
import type {
  DimensionDeltaRead,
  EvaluationReport,
  EvaluationReportCompareRead,
  RuleDeltaRead,
  RuleStatusChange,
} from "../types/evaluationReport";
import KpiCard from "../components/KpiCard";

function statusTagColor(s: RuleStatusChange): string {
  switch (s) {
    case "IMPROVED":
      return "green";
    case "REGRESSED":
      return "red";
    case "NEW":
      return "blue";
    case "REMOVED":
      return "default";
    default:
      return "default";
  }
}

function formatDelta(d: number | null | undefined): string {
  if (d == null || Number.isNaN(d)) return "—";
  const sign = d > 0 ? "+" : "";
  return `${sign}${d.toFixed(2)}`;
}

function formatPct(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n.toFixed(2)}%`;
}

export default function DataQualityReportComparePage() {
  const { message } = App.useApp();
  const [searchParams, setSearchParams] = useSearchParams();

  const idsFromUrl = useMemo(() => {
    const raw = searchParams.get("ids") ?? "";
    return raw
      .split(",")
      .map((s) => Number(s))
      .filter((n) => Number.isFinite(n) && n > 0);
  }, [searchParams]);

  const [allReports, setAllReports] = useState<EvaluationReport[]>([]);
  const [loadingReports, setLoadingReports] = useState(false);
  const [leftId, setLeftId] = useState<number | null>(idsFromUrl[0] ?? null);
  const [rightId, setRightId] = useState<number | null>(idsFromUrl[1] ?? null);
  const [compare, setCompare] = useState<EvaluationReportCompareRead | null>(
    null,
  );
  const [loadingCompare, setLoadingCompare] = useState(false);

  // 加载全部报告列表（用于下拉）
  useEffect(() => {
    setLoadingReports(true);
    listReports({ limit: 200 })
      .then((r) => setAllReports(r.rows))
      .catch((err: unknown) => {
        const m = err instanceof Error ? err.message : String(err);
        void message.error(`加载报告列表失败: ${m}`);
        setAllReports([]);
      })
      .finally(() => setLoadingReports(false));
  }, [message]);

  // URL ↔ state 同步
  useEffect(() => {
    if (!leftId || !rightId) return;
    const next = `ids=${leftId},${rightId}`;
    if (searchParams.get("ids") !== next.replace("ids=", "")) {
      setSearchParams({ ids: `${leftId},${rightId}` }, { replace: true });
    }
  }, [leftId, rightId, searchParams, setSearchParams]);

  // 触发对比
  useEffect(() => {
    if (!leftId || !rightId) {
      setCompare(null);
      return;
    }
    setLoadingCompare(true);
    compareReports(leftId, rightId)
      .then(setCompare)
      .catch((err: unknown) => {
        const m = err instanceof Error ? err.message : String(err);
        void message.error(`对比失败: ${m}`);
        setCompare(null);
      })
      .finally(() => setLoadingCompare(false));
  }, [leftId, rightId, message]);

  const reportOptions = useMemo(
    () =>
      allReports.map((r) => ({
        value: r.id,
        label: `#${r.id} ${r.name}`,
      })),
    [allReports],
  );

  const dimColumns: ColumnsType<DimensionDeltaRead> = [
    { title: "维度", dataIndex: "name", key: "name", width: 140 },
    {
      title: "左报告",
      dataIndex: "left",
      key: "left",
      align: "right",
      render: (v: number | null) => formatPct(v),
    },
    {
      title: "右报告",
      dataIndex: "right",
      key: "right",
      align: "right",
      render: (v: number | null) => formatPct(v),
    },
    {
      title: "Δ",
      dataIndex: "delta",
      key: "delta",
      align: "right",
      render: (v: number | null) => formatDelta(v),
    },
  ];

  const ruleColumns: ColumnsType<RuleDeltaRead> = [
    { title: "规则", dataIndex: "ruleCode", key: "ruleCode", width: 160 },
    {
      title: "左通过率",
      dataIndex: "passRateLeft",
      key: "l",
      align: "right",
      width: 110,
      render: (v: number | null) => formatPct(v),
    },
    {
      title: "右通过率",
      dataIndex: "passRateRight",
      key: "r",
      align: "right",
      width: 110,
      render: (v: number | null) => formatPct(v),
    },
    {
      title: "Δ",
      dataIndex: "delta",
      key: "delta",
      align: "right",
      width: 90,
      render: (v: number | null) => formatDelta(v),
    },
    {
      title: "状态变化",
      dataIndex: "statusChange",
      key: "statusChange",
      width: 120,
      render: (s: RuleStatusChange) => (
        <Tag color={statusTagColor(s)}>{s}</Tag>
      ),
    },
  ];

  return (
    <div>
      <Typography.Title level={3}>报告对比</Typography.Title>
      <Space wrap style={{ marginBottom: 16 }}>
        <Select
          placeholder="左侧报告"
          value={leftId ?? undefined}
          onChange={(v) => setLeftId(v)}
          options={reportOptions}
          loading={loadingReports}
          style={{ width: 280 }}
          showSearch
          optionFilterProp="label"
        />
        <Select
          placeholder="右侧报告"
          value={rightId ?? undefined}
          onChange={(v) => setRightId(v)}
          options={reportOptions}
          loading={loadingReports}
          style={{ width: 280 }}
          showSearch
          optionFilterProp="label"
        />
      </Space>

      {(!leftId || !rightId) && (
        <Empty description="请选择两份报告" style={{ padding: 60 }} />
      )}

      {leftId && rightId && loadingCompare && (
        <div style={{ padding: 60, textAlign: "center" }}>
          <Spin size="large" />
        </div>
      )}

      {leftId && rightId && !loadingCompare && !compare && (
        <Alert
          type="error"
          message="无法对比这两份报告，请检查 ID 是否有效"
          showIcon
        />
      )}

      {compare && (
        <>
          <Row gutter={16} style={{ marginBottom: 16 }}>
            <Col xs={24} md={8}>
              <KpiCard
                label="Overall 分数"
                value={formatPct(compare.overallRight)}
                suffix={`${compare.overallDelta != null ? `Δ ${formatDelta(compare.overallDelta)}` : ""}`}
                trend={
                  compare.overallDelta != null
                    ? {
                        delta: compare.overallDelta,
                        direction:
                          compare.overallDelta >= 0 ? "up" : "down",
                      }
                    : undefined
                }
              />
            </Col>
            <Col xs={24} md={8}>
              <KpiCard
                label="维度变化数量"
                value={`${compare.dimensionDeltas.filter((d) => (d.delta ?? 0) > 0).length} ↑ / ${compare.dimensionDeltas.filter((d) => (d.delta ?? 0) < 0).length} ↓`}
              />
            </Col>
            <Col xs={24} md={8}>
              <KpiCard
                label="规则状态变化"
                value={`${compare.ruleDeltas.filter((r) => r.statusChange === "IMPROVED").length} 改善 / ${compare.ruleDeltas.filter((r) => r.statusChange === "REGRESSED").length} 退化`}
              />
            </Col>
          </Row>

          <Card title="维度对比" style={{ marginBottom: 16 }}>
            <Table
              size="small"
              dataSource={compare.dimensionDeltas}
              columns={dimColumns}
              rowKey="name"
              pagination={false}
            />
          </Card>

          <Card title="规则对比">
            <Table
              size="small"
              dataSource={compare.ruleDeltas}
              columns={ruleColumns}
              rowKey="ruleId"
              pagination={{ pageSize: 20 }}
            />
          </Card>
        </>
      )}
    </div>
  );
}