import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Table,
  Button,
  Space,
  Tag,
  Tooltip,
  Card,
  Col,
  Row,
  Statistic,
  Descriptions,
  Typography,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import ReactECharts from "echarts-for-react";
import {
  listSessions,
  getSessionUsageSummary,
  listSessionUsage,
  getGlobalUsageSummary,
  getDailyUsageTrends,
  getModelUsage,
} from "../api/session";
import type {
  DailyUsageTrend,
  GlobalUsageSummary,
  ModelUsageAggregate,
  SessionListItem,
  SessionTokenUsage,
  TokenUsageSummary,
} from "../types/session";
import { useTranslation } from "../i18n";

const { Text } = Typography;

// 选中会话的明细：按模型聚合卡片 + 流水明细表
interface SessionDetail {
  summary: TokenUsageSummary;
  rows: SessionTokenUsage[];
}

export default function UsagePage() {
  const { t } = useTranslation();
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  // 全局用量（顶部汇总卡片 + 趋势图）
  const [global, setGlobal] = useState<GlobalUsageSummary | null>(null);
  const [daily, setDaily] = useState<DailyUsageTrend[]>([]);
  const [byModel, setByModel] = useState<ModelUsageAggregate[]>([]);
  const [globalLoading, setGlobalLoading] = useState(false);

  const loadGlobal = useCallback(async () => {
    setGlobalLoading(true);
    try {
      const [summary, trends, models] = await Promise.all([
        getGlobalUsageSummary(),
        getDailyUsageTrends(),
        getModelUsage(),
      ]);
      setGlobal(summary);
      setDaily(trends);
      setByModel(models);
    } catch {
      // 拦截器已提示；看板主体（会话表/明细）保持可读
    } finally {
      setGlobalLoading(false);
    }
  }, []);

  const loadSessions = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listSessions();
      setSessions(data);
    } catch {
      // 拦截器已提示
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (sessionId: string) => {
    setDetailLoading(true);
    try {
      const [summary, rows] = await Promise.all([
        getSessionUsageSummary(sessionId),
        listSessionUsage(sessionId),
      ]);
      setDetail({ summary, rows });
    } catch {
      // 拦截器已提示
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSessions();
    void loadGlobal();
  }, [loadSessions, loadGlobal]);

  // 首次加载列表后自动选中第一个会话（独立 effect，避免与列表加载耦合导致重复请求）
  useEffect(() => {
    if (sessions.length > 0 && selectedId === null) {
      setSelectedId(sessions[0].sessionId);
    }
  }, [sessions, selectedId]);

  useEffect(() => {
    if (selectedId) {
      void loadDetail(selectedId);
    } else {
      setDetail(null);
    }
  }, [selectedId, loadDetail]);

  const sessionColumns = [
    {
      title: t("forms.usage.columns.lastQuestion"),
      dataIndex: "lastQuestion",
      width: 320,
      ellipsis: { showTitle: false },
      render: (v: string | null) =>
        v ? (
          <Tooltip placement="topLeft" title={v}>
            {v}
          </Tooltip>
        ) : (
          <Text type="secondary">{t("common.none")}</Text>
        ),
    },
    { title: t("forms.usage.columns.sessionId"), dataIndex: "sessionId", width: 180, ellipsis: true },
    {
      title: t("forms.usage.columns.totalRequests"),
      dataIndex: "totalRequests",
      width: 90,
      align: "right" as const,
    },
    {
      title: t("forms.usage.columns.totalTokens"),
      dataIndex: "totalTokens",
      width: 110,
      align: "right" as const,
    },
    {
      title: t("forms.usage.columns.totalCost"),
      dataIndex: "totalCost",
      width: 110,
      align: "right" as const,
      render: (v: string | number) => Number(v).toFixed(6),
    },
    {
      title: t("forms.usage.columns.lastRequestTime"),
      dataIndex: "lastRequestTime",
      width: 180,
      render: (v: string) => new Date(v).toLocaleString(),
    },
  ];

  // 近 30 天 Token 趋势折线图
  const dailyOption = useMemo(
    () => ({
      tooltip: { trigger: "axis" as const },
      grid: { left: 48, right: 24, top: 32, bottom: 32 },
      xAxis: { type: "category" as const, data: daily.map((d) => d.date) },
      yAxis: { type: "value" as const },
      series: [
        {
          name: t("forms.usage.chartLegendTokens"),
          type: "line" as const,
          smooth: true,
          data: daily.map((d) => d.tokens),
        },
      ],
    }),
    [daily, t]
  );

  // 模型用量分布饼图（按 Token 占比）
  const modelOption = useMemo(
    () => ({
      tooltip: { trigger: "item" as const },
      legend: { bottom: 0 },
      series: [
        {
          type: "pie" as const,
          radius: ["40%", "70%"],
          data: byModel.map((m) => ({ name: m.modelName, value: m.tokens })),
        },
      ],
    }),
    [byModel]
  );

  const usageColumns = [
    {
      title: t("forms.usage.columns.requestTime"),
      dataIndex: "requestTime",
      width: 180,
      render: (v: string) => new Date(v).toLocaleString(),
    },
    {
      title: t("forms.usage.columns.purpose"),
      dataIndex: "purpose",
      width: 140,
      render: (v: string | null) => (v ? <Tag>{v}</Tag> : <Text type="secondary">{t("common.emDash")}</Text>),
    },
    {
      title: t("forms.usage.columns.modelName"),
      dataIndex: "modelName",
      width: 160,
      render: (v: string | null) => v ?? <Text type="secondary">{t("common.emDash")}</Text>,
    },
    {
      title: t("forms.usage.columns.promptTokens"),
      dataIndex: "promptTokens",
      width: 90,
      align: "right" as const,
    },
    {
      title: t("forms.usage.columns.completionTokens"),
      dataIndex: "completionTokens",
      width: 110,
      align: "right" as const,
    },
    {
      title: t("forms.usage.columns.actions"),
      dataIndex: "totalTokens",
      width: 90,
      align: "right" as const,
    },
    {
      title: t("forms.usage.columns.totalCost"),
      dataIndex: "cost",
      width: 110,
      align: "right" as const,
      render: (v: string | number) => Number(v).toFixed(6),
    },
  ];

  return (
    <div style={{ overflow: "hidden" }}>
      <Space style={{ marginBottom: 12 }}>
        <Button
          icon={<ReloadOutlined />}
          loading={loading || globalLoading}
          onClick={() => {
            void loadSessions();
            void loadGlobal();
          }}
        >
          {t("common.refresh")}
        </Button>
      </Space>

      {/* 全局用量汇总卡片（4 张） */}
      <Row gutter={12} style={{ marginBottom: 12 }}>
        <Col span={6}>
          <Card size="small" loading={globalLoading}>
            <Statistic title={t("forms.usage.globalCards.sessions")} value={global?.totalSessions ?? 0} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" loading={globalLoading}>
            <Statistic title={t("forms.usage.globalCards.requests")} value={global?.totalRequests ?? 0} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" loading={globalLoading}>
            <Statistic title={t("forms.usage.globalCards.totalTokens")} value={global?.totalTokens ?? 0} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" loading={globalLoading}>
            <Statistic
              title={t("forms.usage.globalCards.totalCost")}
              value={global?.totalCost ?? 0}
              precision={6}
            />
          </Card>
        </Col>
      </Row>

      {/* 全局趋势图表：Token 日趋势 + 模型分布 */}
      <Row gutter={12} style={{ marginBottom: 12 }}>
        <Col span={14}>
          <Card size="small" title={t("forms.usage.trendCards.tokenTrend")}>
            <ReactECharts option={dailyOption} style={{ height: 260 }} notMerge />
          </Card>
        </Col>
        <Col span={10}>
          <Card size="small" title={t("forms.usage.trendCards.modelDistribution")}>
            <ReactECharts option={modelOption} style={{ height: 260 }} notMerge />
          </Card>
        </Col>
      </Row>

      <Table<SessionListItem>
        rowKey="sessionId"
        columns={sessionColumns}
        dataSource={sessions}
        loading={loading}
        size="small"
        pagination={{ pageSize: 10, showSizeChanger: false }}
        rowSelection={{
          type: "radio",
          selectedRowKeys: selectedId ? [selectedId] : [],
          onChange: (keys) => setSelectedId(keys[0] as string),
        }}
        onRow={(r) => ({ onClick: () => setSelectedId(r.sessionId) })}
      />

      {detail ? (
        <div style={{ marginTop: 16 }}>
          <Card
            size="small"
            loading={detailLoading}
            title={t("forms.usage.detailCardTitle", { sessionId: selectedId ?? "" })}
          >
            <Space size="large" wrap>
              <Statistic title={t("forms.usage.detailCards.requests")} value={detail.summary.totalRequests} />
              <Statistic title={t("forms.usage.detailCards.totalTokens")} value={detail.summary.totalTokens} />
              <Statistic
                title={t("forms.usage.detailCards.totalCost")}
                value={Number(detail.summary.totalCost)}
                precision={6}
              />
            </Space>
            {detail.summary.byModel.length > 0 ? (
              <Descriptions
                size="small"
                column={1}
                style={{ marginTop: 12 }}
                title={t("forms.usage.byModelSummary")}
              >
                {detail.summary.byModel.map((m) => (
                  <Descriptions.Item
                    key={m.modelName}
                    label={
                      <Space>
                        <Tag color="blue">{m.modelName}</Tag>
                      </Space>
                    }
                  >
                    {t("forms.usage.byModelRow", {
                      requests: m.requests,
                      totalTokens: m.totalTokens,
                      cost: Number(m.totalCost).toFixed(6),
                    })}
                  </Descriptions.Item>
                ))}
              </Descriptions>
            ) : null}
          </Card>

          <Table<SessionTokenUsage>
            style={{ marginTop: 12 }}
            rowKey="id"
            columns={usageColumns}
            dataSource={detail.rows}
            loading={detailLoading}
            size="small"
            pagination={{ pageSize: 20, showSizeChanger: false }}
          />
        </div>
      ) : null}
    </div>
  );
}