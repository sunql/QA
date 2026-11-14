import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Row,
  Space,
  Spin,
  Statistic,
  Tag,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { getKpiGovernanceSummary } from "../api/kpiGovernance";
import type { KpiGovernanceSummary } from "../types/kpiGovernance";
import { useTranslation } from "../i18n";
import { useAuthStore } from "../stores/authStore";

const KPI_STATUS_TAG_COLOR: Record<string, string> = {
  DRAFT: "default",
  PUBLISHED: "green",
  DEPRECATED: "orange",
};

const EXECUTION_SOURCE_TAG_COLOR: Record<string, string> = {
  COMPILED: "green",
  ESCAPE_HATCH: "orange",
  NONE: "default",
};

/** 每张统计卡带 data-testid：测试按卡片断言字段映射，避免相同数值互相掩盖。 */
function StatItem({
  testId,
  label,
  value,
}: {
  testId: string;
  label: ReactNode;
  value: string | number;
}) {
  return (
    <div data-testid={testId}>
      <Statistic title={label} value={value} />
    </div>
  );
}

/** 外部计数 best-effort，失败时后端给 null —— 页面显式显示「—」，不伪装成 0。 */
function displayCount(value: number | null, fallback: string): string | number {
  return value ?? fallback;
}

/** 仅做显示裁剪，不引入时区换算：后端给 ISO-8601 UTC 串。 */
function formatTimestamp(value: string): string {
  return value.replace("T", " ").replace("Z", "").slice(0, 19);
}

export default function KpiGovernancePage() {
  const { t } = useTranslation();
  // 后端 requireAdmin 一律 403：非 admin 不发请求，避免每次进页面刷错误。
  const user = useAuthStore((s) => s.user);
  const isAdmin = (user?.roles ?? []).includes("admin");
  const [data, setData] = useState<KpiGovernanceSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await getKpiGovernanceSummary());
      setLoadFailed(false);
    } catch {
      // 错误已由 axios 拦截器 toast；页面保留内联 Alert（可读 + 可重试）。
      setLoadFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) void load();
  }, [isAdmin, load]);

  if (!isAdmin) {
    return (
      <Alert
        type="warning"
        showIcon
        message={t("kpiGovernance.adminOnly")}
        description={t("kpiGovernance.adminOnlyHint")}
      />
    );
  }

  /** 后端状态集合可能新增 —— 缺 i18n key 时回退显示原始 code。 */
  const statusLabel = (status: string): string => {
    const key = `enums.kpiStatus.${status}`;
    const label = t(key);
    return label === key ? status : label;
  };

  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <h2 style={{ margin: 0 }}>{t("kpiGovernance.title")}</h2>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
          {t("common.refresh")}
        </Button>
      </div>

      {loadFailed ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("kpiGovernance.loadFailed")}
          action={
            <Button size="small" onClick={() => void load()}>
              {t("common.retry")}
            </Button>
          }
        />
      ) : null}

      {!data ? (
        <Spin />
      ) : (
        <>
          <Row gutter={[16, 16]}>
            <Col xs={24} lg={12}>
              <Card title={t("kpiGovernance.kpiStatusTitle")} size="small">
                <Row gutter={[8, 8]}>
                  <Col span={8}>
                    <StatItem
                      testId="stat-kpi-total"
                      label={t("kpiGovernance.kpiTotal")}
                      value={data.kpis.total}
                    />
                  </Col>
                  {Object.entries(data.kpis.byStatus).map(([status, count]) => (
                    <Col span={8} key={status}>
                      <StatItem
                        testId={`stat-kpi-status-${status}`}
                        label={statusLabel(status)}
                        value={count}
                      />
                    </Col>
                  ))}
                </Row>
              </Card>
            </Col>

            <Col xs={24} lg={12}>
              <Card title={t("kpiGovernance.metricsTitle")} size="small">
                <Row gutter={[8, 8]}>
                  <Col span={8}>
                    <StatItem
                      testId="stat-metric-total"
                      label={t("kpiGovernance.metricsTotal")}
                      value={data.metrics.total}
                    />
                  </Col>
                  {Object.entries(data.metrics.byExecutionSource).map(([source, count]) => (
                    <Col span={8} key={source}>
                      <StatItem
                        testId={`stat-metric-source-${source}`}
                        label={
                          <Tag color={EXECUTION_SOURCE_TAG_COLOR[source] ?? "default"}>
                            {source}
                          </Tag>
                        }
                        value={count}
                      />
                    </Col>
                  ))}
                </Row>
                <Space size={[4, 8]} wrap style={{ marginTop: 12 }}>
                  {Object.entries(data.metrics.byStatus).map(([status, count]) => (
                    <Tag key={status} color={KPI_STATUS_TAG_COLOR[status] ?? "default"}>
                      {`${statusLabel(status)} ${count}`}
                    </Tag>
                  ))}
                </Space>
              </Card>
            </Col>

            <Col xs={24} lg={12}>
              <Card
                title={t("kpiGovernance.reconcileTitle", {
                  days: data.reconcile.windowDays,
                })}
                size="small"
              >
                <Row gutter={[8, 8]}>
                  <Col span={8}>
                    <StatItem
                      testId="stat-mismatch-count"
                      label={t("kpiGovernance.mismatchCount")}
                      value={data.reconcile.mismatchCount}
                    />
                  </Col>
                  <Col span={8}>
                    <StatItem
                      testId="stat-unknown-count"
                      label={t("kpiGovernance.unknownCount")}
                      value={data.reconcile.unknownCount}
                    />
                  </Col>
                  <Col span={8}>
                    <StatItem
                      testId="stat-pass-count"
                      label={t("kpiGovernance.passCount")}
                      value={data.reconcile.passCount}
                    />
                  </Col>
                </Row>
                <div style={{ marginTop: 8, color: "rgba(0, 0, 0, 0.45)" }}>
                  {t("kpiGovernance.passCountHint")}
                </div>
                <div style={{ marginTop: 8 }}>
                  {`${t("kpiGovernance.lastMismatchAt")}: ${
                    data.reconcile.lastMismatchAt
                      ? formatTimestamp(data.reconcile.lastMismatchAt)
                      : t("common.emDash")
                  }`}
                </div>
                {data.reconcile.reconcileError ? (
                  <Alert
                    type="warning"
                    showIcon
                    style={{ marginTop: 8 }}
                    message={t("kpiGovernance.reconcileError", {
                      error: data.reconcile.reconcileError,
                    })}
                  />
                ) : null}
              </Card>
            </Col>

            <Col xs={24} lg={12}>
              <Card title={t("kpiGovernance.syncHealthTitle")} size="small">
                <Row gutter={[8, 8]}>
                  <Col span={6}>
                    <StatItem
                      testId="stat-published-count"
                      label={t("kpiGovernance.publishedKpiCount")}
                      value={displayCount(
                        data.syncHealth.publishedKpiCount,
                        t("common.emDash"),
                      )}
                    />
                  </Col>
                  <Col span={6}>
                    <StatItem
                      testId="stat-milvus-count"
                      label={t("kpiGovernance.milvusKpiCount")}
                      value={displayCount(
                        data.syncHealth.milvusKpiCount,
                        t("common.emDash"),
                      )}
                    />
                  </Col>
                  <Col span={6}>
                    <StatItem
                      testId="stat-neo4j-count"
                      label={t("kpiGovernance.neo4jKpiCount")}
                      value={displayCount(
                        data.syncHealth.neo4jKpiCount,
                        t("common.emDash"),
                      )}
                    />
                  </Col>
                  <Col span={6}>
                    <StatItem
                      testId="stat-sync-kpi-total"
                      label={t("kpiGovernance.syncKpiTotal")}
                      value={data.syncHealth.kpiTotal}
                    />
                  </Col>
                </Row>
                {data.syncHealth.syncHealthError ? (
                  <Alert
                    type="warning"
                    showIcon
                    style={{ marginTop: 8 }}
                    message={t("kpiGovernance.syncHealthError", {
                      error: data.syncHealth.syncHealthError,
                    })}
                  />
                ) : null}
              </Card>
            </Col>

            <Col xs={24} lg={12}>
              <Card title={t("kpiGovernance.gatesTitle")} size="small">
                <Space direction="vertical" size={4}>
                  {[
                    {
                      key: "publishGate",
                      label: t("kpiGovernance.publishGate"),
                      enabled: data.gates.publishGateEnabled,
                    },
                    {
                      key: "tierA",
                      label: t("kpiGovernance.tierA"),
                      enabled: data.gates.tierAEnabled,
                    },
                    {
                      key: "syncGate",
                      label: t("kpiGovernance.syncGate"),
                      enabled: data.gates.syncEnabled,
                    },
                  ].map((gate) => (
                    <div key={gate.key} data-testid={`gate-${gate.key}`}>
                      {`${gate.label}: `}
                      <Tag color={gate.enabled ? "green" : "red"}>
                        {gate.enabled
                          ? t("kpiGovernance.enabled")
                          : t("kpiGovernance.disabled")}
                      </Tag>
                    </div>
                  ))}
                </Space>
              </Card>
            </Col>
          </Row>

          <div style={{ marginTop: 16, color: "rgba(0, 0, 0, 0.45)" }}>
            {t("kpiGovernance.generatedAt", {
              time: formatTimestamp(data.generatedAt),
            })}
          </div>
        </>
      )}
    </div>
  );
}
