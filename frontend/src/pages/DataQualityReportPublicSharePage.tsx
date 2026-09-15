/** 数据质量评估报告 — 公开分享页（feat-dq-evaluation-report，Phase 8b）。
 *
 * URL: /data-quality/reports/share/:token
 * - 无登录访问，按 token 拉取报告
 * - 只读版 Detail：顶部 Alert 提示「共享报告」，无任何写按钮
 * - 与登录后 detail 的差异：无 Regenerate / Delete / Share / Export 按钮
 *
 * token 失效 / 过期 → 跳列表 + 错误提示（410 / 404）
 */

import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  Card,
  Col,
  Descriptions,
  Empty,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { getPublicReport } from "../api/evaluationReport";
import type { EvaluationReport } from "../types/evaluationReport";
import KpiCard from "../components/KpiCard";
import ReportDimensionBar from "../components/ReportDimensionBar";
import ReportPassFailPie from "../components/ReportPassFailPie";

interface RuleRow {
  // snapshot.rules[] 是 snake_case dict（dict[str,Any] 不走 Pydantic alias_generator），
  // pass_rate / violation_count 由 total_count - passed_count 前端算出。
  // Phase 4 强类型化后切 camelCase。详见 [[dq-eval-report-snapshot-snake-case]]。
  rule_id: number;
  rule_code: string;
  rule_type: string;
  target_table: string;
  target_column: string | null;
  severity: string | null;
  total_count: number;
  passed_count: number;
  pass_rate: number;
  violation_count: number;
  status: string;
}

interface TableRow {
  target_table: string;
  overall_score: number | null;
  rules: RuleRow[];
}

interface SnapshotShape {
  schema_version?: number;
  evaluated_at?: string;
  overall?: { score: number | null; status: string };
  dimensions?: Record<string, number | null>;
  tables?: TableRow[];
}

function statusColor(s: string): string {
  switch (s) {
    case "PASS":
      return "green";
    case "FAIL":
      return "red";
    default:
      return "default";
  }
}

function formatPct(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n.toFixed(2)}%`;
}

export default function DataQualityReportPublicSharePage() {
  const navigate = useNavigate();
  const { token } = useParams<{ token: string }>();
  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    setLoading(true);
    setErrorMsg(null);
    getPublicReport(token)
      .then(setReport)
      .catch((err: unknown) => {
        const m = err instanceof Error ? err.message : String(err);
        setErrorMsg(m);
        setReport(null);
      })
      .finally(() => setLoading(false));
  }, [token]);

  const snapshot: SnapshotShape | null = useMemo(() => {
    if (!report || typeof report.snapshot !== "object" || report.snapshot === null) {
      return null;
    }
    return report.snapshot as SnapshotShape;
  }, [report]);

  const ruleRows: RuleRow[] = useMemo(() => {
    if (!snapshot?.tables) return [];
    // snapshot.rules[] 是 snake_case dict（dict[str,Any] 不走 Pydantic alias_generator），
    // pass_rate / violation_count 由 total_count - passed_count 前端算出。
    // Phase 4 强类型化后切 camelCase。详见 [[dq-eval-report-snapshot-snake-case]]。
    const seen = new Set<number>();
    const out: RuleRow[] = [];
    for (const t of snapshot.tables) {
      const rulesList = Array.isArray(t.rules) ? t.rules : [];
      for (const r of rulesList) {
        const rid = Number(r.rule_id);
        if (!Number.isFinite(rid) || seen.has(rid)) continue;
        seen.add(rid);
        const total = Number(r.total_count ?? 0);
        const passed = Number(r.passed_count ?? 0);
        out.push({
          rule_id: rid,
          rule_code: String(r.rule_code ?? ""),
          rule_type: String(r.rule_type ?? ""),
          target_table: String(r.target_table ?? ""),
          target_column: (r.target_column as string | null) ?? null,
          severity: (r.severity as string | null) ?? null,
          total_count: total,
          passed_count: passed,
          pass_rate:
            total > 0
              ? Number(((passed / total) * 100).toFixed(2))
              : 0,
          violation_count: Math.max(total - passed, 0),
          status: String(r.status ?? "PENDING"),
        });
      }
    }
    return out;
  }, [snapshot]);

  const ruleColumns: ColumnsType<RuleRow> = useMemo(
    () => [
      { title: "Rule", dataIndex: "rule_code", key: "rule_code", width: 200 },
      { title: "Type", dataIndex: "rule_type", key: "rule_type", width: 130 },
      {
        title: "Target",
        key: "target",
        width: 240,
        render: (_, row) =>
          `${row.target_table}${row.target_column ? `.${row.target_column}` : ""}`,
      },
      {
        title: "Severity",
        dataIndex: "severity",
        key: "severity",
        width: 110,
        render: (v: string | null) => (v == null ? <Tag>—</Tag> : <Tag>{v}</Tag>),
      },
      {
        title: "Pass Rate",
        dataIndex: "pass_rate",
        key: "pass_rate",
        width: 120,
        render: (v: number) => formatPct(v),
      },
      {
        title: "Status",
        dataIndex: "status",
        key: "status",
        width: 110,
        render: (v: string) => <Tag color={statusColor(v)}>{v}</Tag>,
      },
    ],
    [],
  );

  if (loading) {
    return (
      <div style={{ padding: 60, textAlign: "center" }}>
        <Spin size="large" />
      </div>
    );
  }

  if (errorMsg || !report) {
    return (
      <div style={{ padding: 24 }}>
        <Alert
          type="error"
          message="共享链接无效或已过期"
          description={errorMsg ?? "无法加载报告"}
          showIcon
          action={
            <a onClick={() => navigate("/data-quality/reports")}>
              返回报告列表
            </a>
          }
        />
      </div>
    );
  }

  return (
    <div>
      <Alert
        type="info"
        message="共享报告 — 只读视图"
        description="该链接是受控分享，无需登录即可查看。报告内容由分享者创建时刻的快照决定。"
        showIcon
        style={{ marginBottom: 16 }}
      />

      <Typography.Title level={3} style={{ marginBottom: 4 }}>
        {report.name}
      </Typography.Title>
      {report.description && (
        <Typography.Paragraph type="secondary">
          {report.description}
        </Typography.Paragraph>
      )}
      <Space style={{ marginBottom: 16 }}>
        <Tag color={report.status === "PUBLISHED" ? "blue" : "default"}>
          {report.status}
        </Tag>
        <Typography.Text type="secondary">
          分享者：{report.createdBy}
        </Typography.Text>
      </Space>

      <Descriptions
        column={2}
        size="small"
        bordered
        style={{ marginBottom: 16 }}
      >
        <Descriptions.Item label="时间窗口">
          {new Date(report.timeWindowStart).toLocaleString()} ~{" "}
          {new Date(report.timeWindowEnd).toLocaleString()}
        </Descriptions.Item>
        <Descriptions.Item label="创建时间">
          {new Date(report.createdTime).toLocaleString()}
        </Descriptions.Item>
      </Descriptions>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} sm={12} md={6}>
          <KpiCard
            label="Overall"
            value={formatPct(snapshot?.overall?.score ?? null)}
            suffix={snapshot?.overall?.status ?? ""}
          />
        </Col>
        <Col xs={24} sm={12} md={6}>
          <KpiCard
            label="评估规则数"
            value={ruleRows.length}
          />
        </Col>
        <Col xs={24} sm={12} md={6}>
          <KpiCard
            label="Pass"
            value={ruleRows.filter((r) => r.status === "PASS").length}
          />
        </Col>
        <Col xs={24} sm={12} md={6}>
          <KpiCard
            label="Fail"
            value={ruleRows.filter((r) => r.status === "FAIL").length}
          />
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <Card title="维度得分">
            {snapshot?.dimensions ? (
              <ReportDimensionBar dimensions={snapshot.dimensions} />
            ) : (
              <Empty description="无维度数据" />
            )}
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card title="通过 / 失败">
            <ReportPassFailPie rules={ruleRows} />
          </Card>
        </Col>
      </Row>

      <Card title={`规则明细（${ruleRows.length}）`}>
        {ruleRows.length === 0 ? (
          <Empty description="无规则明细" />
        ) : (
          <Table
            size="small"
            dataSource={ruleRows}
            columns={ruleColumns}
            rowKey="rule_id"
            pagination={{ pageSize: 20 }}
          />
        )}
      </Card>
    </div>
  );
}