/** 数据质量评估报告 — 详情页（feat-dq-evaluation-report，Phase 5）。
 *
 * 布局：
 *  - 顶部 header：标题 + 描述 + status Tag + 操作按钮（返回/重新生成/删除）
 *  - 基本信息（Descriptions）：class/rule/window/createdBy/createdTime/owner/tags
 *  - 评估摘要（占位 KPI 卡 + 6 维度 Table）：Phase 6 接 echarts
 *  - 规则明细 Table：每规则一行（rule_code / target_table / target_column /
 *    rule_type / severity / pass_rate / status）
 *  - 违规样本 Accordion：嵌入 ViolationSampleTable
 *
 * snapshot 类型 Phase 6 强类型化；本 phase 用 `Record<string, unknown>` 软读。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Empty,
  InputNumber,
  Modal,
  Progress,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import {
  createShare,
  deleteReport,
  getReport,
  getReportProgress,
  listSamples,
  listShares,
  regenerateReport,
  revokeShare,
} from "../api/evaluationReport";
import type {
  EvaluationReport,
  EvaluationReportProgress,
  EvaluationReportShareRead,
  ViolationSampleRead,
} from "../types/evaluationReport";
import ViolationSampleTable from "../components/ViolationSampleTable";
import KpiCard from "../components/KpiCard";
import ReportDimensionBar from "../components/ReportDimensionBar";
import ReportPassFailPie from "../components/ReportPassFailPie";
import ReportTrendLine from "../components/ReportTrendLine";

interface RuleRow {
  // snapshot 是 dict[str,Any]，Pydantic alias_generator 不会递归改写内层 key，
  // 所以 rules[] 用 snake_case 读。Phase 4 强类型化后切 camelCase。
  // 详见 [[dq-eval-report-snapshot-snake-case]]。
  rule_id: number;
  rule_code: string;
  // feat-report-rules-zh-name (2026-09-15)：前端展示用 rule_name（业务可读名），
  // 缺省时降级到 rule_code 兼容老 snapshot。
  rule_name: string | null;
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

interface TrendPoint {
  date: string;
  overall_score: number | null;
}

interface TrendShape {
  available: boolean;
  window_days?: number;
  series: TrendPoint[];
}

interface SnapshotShape {
  schema_version?: number;
  evaluated_at?: string;
  overall?: { score: number | null; status: string };
  dimensions?: Record<string, number | null>;
  tables?: TableRow[];
  trend?: TrendShape;
}

function isSnapshotShape(v: unknown): v is SnapshotShape {
  return typeof v === "object" && v !== null;
}

function statusColor(status: string): string {
  switch (status) {
    case "PASS":
    case "COMPLETED":
    case "PUBLISHED":
      return "green";
    case "FAIL":
    case "FAILED":
      return "red";
    case "RUNNING":
      return "blue";
    case "PENDING":
    case "DRAFT":
      return "default";
    default:
      return "default";
  }
}

/** 异步评估是否仍在进行（PENDING / RUNNING）；用于决定是否显示进度条 + 轮询。 */
function isInProgress(status: string): boolean {
  return status === "PENDING" || status === "RUNNING";
}

function formatPct(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n.toFixed(2)}%`;
}

export default function DataQualityReportDetailPage() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const reportId = useMemo(() => {
    const n = Number(id);
    return Number.isFinite(n) && n > 0 ? n : 0;
  }, [id]);

  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [loading, setLoading] = useState(false);
  /** 异步评估进度；与 report.status 同步切换（feat-dq-evaluation-report-progress）。 */
  const [progress, setProgress] = useState<EvaluationReportProgress | null>(null);
  const [samples, setSamples] = useState<ViolationSampleRead[]>([]);
  const [samplesLoading, setSamplesLoading] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [shares, setShares] = useState<EvaluationReportShareRead[]>([]);
  const [sharesLoading, setSharesLoading] = useState(false);
  const [newExpiresInDays, setNewExpiresInDays] = useState<number>(7);
  const [creatingShare, setCreatingShare] = useState(false);

  const refresh = useCallback(async () => {
    if (!reportId) return;
    setLoading(true);
    try {
      const r = await getReport(reportId);
      setReport(r);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(`${t("dataQuality.reports.actions.loadFailed")}: ${msg}`);
    } finally {
      setLoading(false);
    }
  }, [reportId, message, t]);

  const loadSamples = useCallback(async () => {
    if (!reportId) return;
    setSamplesLoading(true);
    try {
      const rows = await listSamples(reportId, { limit: 50 });
      setSamples(rows);
    } catch (err: unknown) {
      // 样本为空时不报错——可能没有违规样本或采样失败（见 dq-eval-report-defensive-sampler）。
      setSamples([]);
    } finally {
      setSamplesLoading(false);
    }
  }, [reportId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    void loadSamples();
  }, [loadSamples]);

  // 异步评估进度轮询（feat-dq-evaluation-report-progress，2026-09-15）。
  // 仅当 report.status ∈ {PENDING, RUNNING} 时轮询；COMPLETED/FAILED 拉一次完整
  // report 后停止轮询，保证 snapshot 字段被前端正确渲染。
  useEffect(() => {
    if (!report || !isInProgress(report.status)) {
      return;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const p = await getReportProgress(report.id);
        if (cancelled) return;
        setProgress(p);
        if (p.stage === "COMPLETED" || p.stage === "FAILED") {
          // 拉最新完整 report（snapshot 已写完）
          const fresh = await getReport(report.id);
          if (!cancelled) setReport(fresh);
        }
      } catch {
        // 静默：轮询失败由下一轮继续尝试
      }
    };
    // 立即拉一次，然后 1s 间隔
    void poll();
    const handle = window.setInterval(() => void poll(), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(handle);
    };
  }, [report]);

  const handleDelete = useCallback(() => {
    if (!report) return;
    Modal.confirm({
      title: t("dataQuality.reports.actions.confirmDelete", { name: report.name }),
      okText: t("common.confirm", { defaultValue: "确认" }),
      cancelText: t("common.cancel", { defaultValue: "取消" }),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await deleteReport(report.id);
          void message.success(t("dataQuality.reports.actions.deleteSuccess"));
          navigate("/data-quality/reports");
        } catch (err: unknown) {
          const msg = err instanceof Error ? err.message : String(err);
          void message.error(msg);
        }
      },
    });
  }, [message, navigate, report, t]);

  const handleRegenerate = useCallback(async () => {
    if (!report) return;
    try {
      const updated = await regenerateReport(report.id);
      setReport(updated);
      void message.success(t("dataQuality.reports.actions.regenerateSuccess"));
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    }
  }, [message, report, t]);

  const loadShares = useCallback(async () => {
    if (!report) return;
    setSharesLoading(true);
    try {
      const rows = await listShares(report.id);
      setShares(rows);
    } catch (err: unknown) {
      // 静默：详情页不强制展示分享列表
      setShares([]);
    } finally {
      setSharesLoading(false);
    }
  }, [report]);

  const handleOpenShareModal = useCallback(() => {
    setShareOpen(true);
    void loadShares();
  }, [loadShares]);

  const handleCreateShare = useCallback(async () => {
    if (!report) return;
    setCreatingShare(true);
    try {
      const share = await createShare(report.id, {
        expiresInDays: newExpiresInDays,
      });
      const fullUrl = `${window.location.origin}${share.shareUrl ?? ""}`;
      void message.success(`分享链接已创建：${fullUrl}`);
      await loadShares();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setCreatingShare(false);
    }
  }, [report, newExpiresInDays, loadShares, message]);

  const handleRevokeShare = useCallback(
    async (shareId: number) => {
      try {
        await revokeShare(shareId);
        void message.success("已撤销分享");
        await loadShares();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        void message.error(msg);
      }
    },
    [loadShares, message],
  );

  const handleCompareWith = useCallback(() => {
    if (!report) return;
    navigate(`/data-quality/reports/compare?leftId=${report.id}`);
  }, [navigate, report]);

  const snapshot: SnapshotShape | null = useMemo(() => {
    if (!report || !isSnapshotShape(report.snapshot)) return null;
    return report.snapshot;
  }, [report]);

  const ruleRows: RuleRow[] = useMemo(() => {
    if (!snapshot?.tables) return [];
    // 去重：同一 rule_id 出现在多个 tables[] 时只保留首条；
    // 后端按 target_table 分组时偶有重叠（cross-table 规则、重复 rule_id），
    // 不去重会导致 antd Table rowKey 重复 → React key warning。
    // snapshot.rules[] 是 snake_case dict（dict[str,Any] 不走 Pydantic alias_generator），
    // pass_rate / violation_count 由 total_count - passed_count 前端算出。
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
          // feat-report-rules-zh-name (2026-09-15)：读 snapshot.rule_name（业务可读名），
          // null/空时降级到 rule_code，老 snapshot 不破表。
          rule_name: ((r.rule_name as string | null | undefined) ?? "").trim() || null,
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
      // feat-report-rules-zh-name (2026-09-15)：表头全 i18n；Rule 列用 rule_name 业务可读名
      // （rule_code 仅作系统唯一性 ID，无业务含义，不应展示给业务用户）；
      // Type 列映射到 ruleTypeLabels（COMPLETENESS → 完整性）；Severity 走 severityLabels。
      // feat-report-kpi-data-count (2026-09-15)：rule_name 缺失时降级显示 rule_code + 「系统编码」徽标
      // + tooltip 提示「老 snapshot 无 rule_name 字段，请重新生成报告」—— 比纯显示 code 更明确。
      {
        title: t("dataQuality.reports.detail.ruleTable.rule"),
        dataIndex: "rule_name",
        key: "rule",
        width: 280,
        render: (v: string | null, row) => {
          if (v) return v;
          return (
            <Tooltip
              title={t("dataQuality.reports.detail.ruleTable.ruleFallbackTooltip")}
            >
              <Space size={6}>
                <Tag color="default" style={{ marginInlineEnd: 0 }}>
                  {t("dataQuality.reports.detail.ruleTable.ruleFallbackBadge")}
                </Tag>
                <span>{row.rule_code}</span>
              </Space>
            </Tooltip>
          );
        },
      },
      {
        title: t("dataQuality.reports.detail.ruleTable.type"),
        dataIndex: "rule_type",
        key: "rule_type",
        width: 120,
        render: (v: string) => {
          const labels = t("dataQuality.ruleTypeLabels", {
            returnObjects: true,
          }) as unknown as Record<string, string>;
          return labels[v] ?? v;
        },
      },
      {
        title: t("dataQuality.reports.detail.ruleTable.target"),
        key: "target",
        width: 240,
        render: (_, row) =>
          `${row.target_table}${row.target_column ? `.${row.target_column}` : ""}`,
      },
      {
        title: t("dataQuality.reports.detail.ruleTable.severity"),
        dataIndex: "severity",
        key: "severity",
        width: 110,
        render: (v: string | null) => {
          if (v == null) return <Tag>—</Tag>;
          const labels = t("dataQuality.severityLabels", {
            returnObjects: true,
          }) as unknown as Record<string, string>;
          return <Tag>{labels[v] ?? v}</Tag>;
        },
      },
      {
        title: t("dataQuality.reports.detail.ruleTable.passRate"),
        dataIndex: "pass_rate",
        key: "pass_rate",
        width: 120,
        render: (v: number) => formatPct(v),
      },
      {
        title: t("dataQuality.reports.detail.ruleTable.status"),
        dataIndex: "status",
        key: "status",
        width: 110,
        render: (v: string) => (
          <Tag color={statusColor(v)}>{v}</Tag>
        ),
      },
    ],
    [t],
  );

  if (loading && !report) {
    return (
      <div style={{ padding: 24, textAlign: "center" }}>
        <Spin />
      </div>
    );
  }

  if (!report) {
    return (
      <div style={{ padding: 24 }}>
        <Alert type="error" message={t("dataQuality.reports.actions.loadFailed")} />
        <Button
          style={{ marginTop: 16 }}
          onClick={() => navigate("/data-quality/reports")}
        >
          {t("dataQuality.reports.detail.backToList")}
        </Button>
      </div>
    );
  }

  return (
    <div style={{ padding: 24 }}>
      <Space style={{ marginBottom: 16 }}>
        <Button onClick={() => navigate("/data-quality/reports")}>
          {t("dataQuality.reports.detail.backToList")}
        </Button>
        <Button onClick={() => void handleRegenerate()} loading={loading}>
          {t("dataQuality.reports.actions.regenerate")}
        </Button>
        <Button onClick={handleOpenShareModal}>分享</Button>
        <Button onClick={handleCompareWith}>对比</Button>
        <Button danger onClick={handleDelete}>
          {t("dataQuality.reports.actions.delete")}
        </Button>
      </Space>

      <Card style={{ marginBottom: 16 }}>
        <Space align="start" style={{ width: "100%", justifyContent: "space-between" }}>
          <div>
            <h2 style={{ marginTop: 0 }}>{report.name}</h2>
            {report.description ? (
              <div style={{ color: "#888" }}>{report.description}</div>
            ) : null}
          </div>
          <Tag color={statusColor(report.status)} style={{ fontSize: 14 }}>
            {t(`dataQuality.reports.statusLabels.${report.status}`)}
          </Tag>
        </Space>
      </Card>

      {/* 异步评估进度（feat-dq-evaluation-report-progress，2026-09-15） */}
      {(isInProgress(report.status) || progress) && (
        <Card style={{ marginBottom: 16 }} data-testid="dq-eval-progress-card">
          {(() => {
            const p = progress ?? report.progress ?? null;
            const total = p?.total ?? report.ruleIds.length;
            const completed = p?.completed ?? 0;
            const percent =
              total > 0
                ? Math.min(100, Math.round((completed / total) * 100))
                : isInProgress(report.status)
                  ? 0
                  : 100;
            const stage = p?.stage ?? "PENDING";
            return (
              <Space direction="vertical" style={{ width: "100%" }} size={12}>
                <Space size="large" wrap>
                  <Typography.Text strong>评估进度</Typography.Text>
                  <Tag color={statusColor(stage)}>{stage}</Tag>
                  <Typography.Text type="secondary">
                    {completed} / {total} 条规则
                  </Typography.Text>
                  {p?.currentRuleCode ? (
                    <Typography.Text type="secondary">
                      正在评估：{p.currentRuleCode}
                    </Typography.Text>
                  ) : null}
                </Space>
                <Progress
                  percent={percent}
                  status={
                    report.status === "FAILED"
                      ? "exception"
                      : isInProgress(report.status)
                        ? "active"
                        : "success"
                  }
                  showInfo
                />
                {p?.message ? (
                  <Typography.Text type="secondary">{p.message}</Typography.Text>
                ) : null}
              </Space>
            );
          })()}
        </Card>
      )}

      <Card
        title={t("dataQuality.reports.detail.sections.basic")}
        style={{ marginBottom: 16 }}
      >
        <Descriptions column={2} bordered size="small">
          <Descriptions.Item label={t("dataQuality.reports.detail.fields.id")}>
            {report.id}
          </Descriptions.Item>
          <Descriptions.Item label={t("dataQuality.reports.detail.fields.status")}>
            {t(`dataQuality.reports.statusLabels.${report.status}`)}
          </Descriptions.Item>
          <Descriptions.Item
            label={t("dataQuality.reports.detail.fields.classes")}
          >
            {report.classIds.length} 项
          </Descriptions.Item>
          <Descriptions.Item label={t("dataQuality.reports.detail.fields.rules")}>
            {report.ruleIds.length} 条
          </Descriptions.Item>
          <Descriptions.Item
            label={t("dataQuality.reports.detail.fields.timeWindow")}
          >
            {new Date(report.timeWindowStart).toLocaleString()} →{" "}
            {new Date(report.timeWindowEnd).toLocaleString()}
          </Descriptions.Item>
          <Descriptions.Item label={t("dataQuality.reports.detail.fields.createdBy")}>
            {report.createdBy}
          </Descriptions.Item>
          <Descriptions.Item label={t("dataQuality.reports.detail.fields.createdTime")}>
            {report.createdTime
              ? new Date(report.createdTime).toLocaleString()
              : "—"}
          </Descriptions.Item>
          <Descriptions.Item label={t("dataQuality.reports.detail.fields.owner")}>
            {report.owner ?? "—"}
          </Descriptions.Item>
          <Descriptions.Item
            label={t("dataQuality.reports.detail.fields.tags")}
            span={2}
          >
            {report.tags.length === 0
              ? "—"
              : report.tags.map((tag) => (
                  <Tag key={tag}>{tag}</Tag>
                ))}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <Card
        title={t("dataQuality.reports.detail.sections.summary")}
        style={{ marginBottom: 16 }}
      >
        {snapshot ? (
          <>
            <Space size="large" wrap style={{ marginBottom: 16 }}>
              <KpiCard
                testId="kpi-overall"
                label={t("dataQuality.reports.detail.kpi.overallScore")}
                value={
                  typeof snapshot.overall?.score === "number"
                    ? snapshot.overall.score
                    : "—"
                }
                precision={2}
                suffix={typeof snapshot.overall?.score === "number" ? "%" : ""}
              />
              <KpiCard
                testId="kpi-rule-count"
                label={t("dataQuality.reports.detail.kpi.ruleCount")}
                value={ruleRows.length}
              />
              <KpiCard
                testId="kpi-pass-count"
                label={t("dataQuality.reports.detail.kpi.passCount")}
                value={ruleRows.filter((r) => r.status === "PASS").length}
              />
              <KpiCard
                testId="kpi-fail-count"
                label={t("dataQuality.reports.detail.kpi.failCount")}
                value={ruleRows.filter((r) => r.status === "FAIL").length}
              />
              {/* feat-report-kpi-data-count (2026-09-15)：评估摘要增加「数据条目数」卡。
                  全部规则 total_count 求和；按千分位格式化（antd Statistic 内置支持）。
                  全为 0 时显示 0，不显示「—」（无数据时仍给一个明确的 0）。 */}
              <KpiCard
                testId="kpi-data-item-count"
                label={t("dataQuality.reports.detail.kpi.dataItemCount")}
                value={ruleRows.reduce((sum, r) => sum + (r.total_count || 0), 0)}
                precision={0}
              />
            </Space>
            <Row gutter={16} style={{ marginTop: 8 }}>
              <Col xs={24} md={12}>
                <ReportDimensionBar
                  dimensions={snapshot.dimensions ?? {}}
                />
              </Col>
              <Col xs={24} md={12}>
                <ReportPassFailPie rules={ruleRows} />
              </Col>
            </Row>
            <div style={{ marginTop: 16 }}>
              <ReportTrendLine trend={snapshot.trend ?? null} />
            </div>
          </>
        ) : (
          <Empty description="—" />
        )}
      </Card>

      <Card
        title={t("dataQuality.reports.detail.sections.rules")}
        style={{ marginBottom: 16 }}
      >
        {ruleRows.length === 0 ? (
          <Empty description="—" />
        ) : (
          <Table<RuleRow>
            rowKey="rule_id"
            dataSource={ruleRows}
            columns={ruleColumns}
            pagination={{ pageSize: 10, showSizeChanger: false }}
            size="small"
          />
        )}
      </Card>

      <Card title={t("dataQuality.reports.detail.sections.samples")}>
        <Collapse
          items={[
            {
              key: "samples",
              label: `${t("dataQuality.reports.detail.sections.samples")} (${samples.length})`,
              children: (
                <ViolationSampleTable
                  samples={samples}
                  loading={samplesLoading}
                />
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title="分享报告"
        open={shareOpen}
        onCancel={() => setShareOpen(false)}
        footer={null}
        width={600}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Space>
            <Typography.Text>有效期（天）：</Typography.Text>
            <InputNumber
              min={1}
              max={30}
              value={newExpiresInDays}
              onChange={(v) => setNewExpiresInDays(typeof v === "number" ? v : 7)}
            />
            <Button
              type="primary"
              loading={creatingShare}
              onClick={() => void handleCreateShare()}
            >
              创建分享链接
            </Button>
          </Space>

          <Typography.Title level={5} style={{ marginTop: 8 }}>
            现存分享
          </Typography.Title>
          {sharesLoading ? (
            <Spin />
          ) : shares.length === 0 ? (
            <Empty description="暂无分享" />
          ) : (
            <Table
              size="small"
              dataSource={shares}
              rowKey="id"
              pagination={false}
              columns={[
                {
                  title: "Token",
                  dataIndex: "shareToken",
                  key: "token",
                  width: 200,
                  render: (tok: string) => (
                    <Typography.Text code style={{ fontSize: 12 }}>
                      {tok.slice(0, 8)}…
                    </Typography.Text>
                  ),
                },
                {
                  title: "过期时间",
                  dataIndex: "expiresAt",
                  key: "expiresAt",
                  render: (v: string) => new Date(v).toLocaleString(),
                },
                {
                  title: "访问次数",
                  dataIndex: "accessCount",
                  key: "accessCount",
                  width: 90,
                },
                {
                  title: "操作",
                  key: "action",
                  width: 200,
                  render: (_, row) => (
                    <Space>
                      <Button
                        size="small"
                        onClick={() => {
                          const url = `${window.location.origin}/data-quality/reports/share/${row.shareToken}`;
                          void navigator.clipboard.writeText(url);
                          void message.success("已复制");
                        }}
                      >
                        复制
                      </Button>
                      <Button
                        size="small"
                        danger
                        onClick={() => {
                          Modal.confirm({
                            title: "撤销该分享？撤销后链接立即失效",
                            onOk: () => handleRevokeShare(row.id),
                          });
                        }}
                      >
                        撤销
                      </Button>
                    </Space>
                  ),
                },
              ]}
            />
          )}
        </Space>
      </Modal>
    </div>
  );
}