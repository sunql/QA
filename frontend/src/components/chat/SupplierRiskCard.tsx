import { Card, List, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { SupplierRiskKpiContribution, SupplierRiskRead, RiskLevel } from "../../types/supplierRisk";
import { useTranslation } from "../../i18n";

const { Text, Paragraph, Title } = Typography;

interface SupplierRiskCardProps {
  data: SupplierRiskRead;
}

interface ContributionRow extends SupplierRiskKpiContribution {
  key: string;
}

/** 风险等级 → antd Tag 颜色（与 supplier360 同视觉语言）。 */
const LEVEL_COLOR: Record<RiskLevel, string> = {
  high: "red",
  medium: "orange",
  low: "green",
  unknown: "default",
};

/** 风险等级 → 中文显示名。 */
const LEVEL_LABEL: Record<RiskLevel, string> = {
  high: "高风险",
  medium: "中风险",
  low: "低风险",
  unknown: "未知",
};

/**
 * Supplier Risk Agent 卡片（Phase 5.4）。
 *
 * - 头部：风险等级 Tag（颜色按 level）+ enterprise_code Tag + level_source 文本
 * - 主要风险点：LLM/fallback 标识 + Markdown-like 段落
 * - 推荐动作：List 列表（≥1 条）
 * - 输入特征贡献：4 行 Table（featureAlias / value / threshold / passed 标记）
 * - Tokens & Cost：底部 Tag（与 ChatMessage 一致）
 */
export default function SupplierRiskCard({ data }: SupplierRiskCardProps) {
  const { t } = useTranslation();
  const {
    profile,
    level,
    levelSource,
    contributions,
    riskPoints,
    riskPointsSource,
    recommendedActions,
    tokensUsed,
    promptTokens,
    completionTokens,
    cost,
    llmModelName,
    fetchedAt,
  } = data;

  const columns: ColumnsType<ContributionRow> = [
    {
      title: t("supplierRisk.contribution.feature"),
      dataIndex: "featureAlias",
      key: "featureAlias",
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{row.featureAlias ?? row.featureName}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {row.featureName}
          </Text>
        </Space>
      ),
    },
    {
      title: t("supplierRisk.contribution.value"),
      dataIndex: "value",
      key: "value",
      render: (_, row) =>
        row.value === null ? (
          <Tag color="default">{t("supplierRisk.contribution.placeholder")}</Tag>
        ) : (
          <Text>
            {row.value}
            {row.unit ? <Text type="secondary"> {row.unit}</Text> : null}
          </Text>
        ),
    },
    {
      title: t("supplierRisk.contribution.threshold"),
      dataIndex: "threshold",
      key: "threshold",
      render: (v: string | null) => (v === null ? "—" : <Text>{v}</Text>),
    },
    {
      title: t("supplierRisk.contribution.passed"),
      dataIndex: "passed",
      key: "passed",
      render: (passed: boolean) =>
        passed ? (
          <Tag color="green">{t("supplierRisk.contribution.passedOk")}</Tag>
        ) : (
          <Tag color="red">{t("supplierRisk.contribution.passedFail")}</Tag>
        ),
    },
    {
      title: t("supplierRisk.contribution.note"),
      dataIndex: "note",
      key: "note",
      render: (v: string | null) => (v ? <Text type="secondary">{v}</Text> : "—"),
    },
  ];

  const rows: ContributionRow[] = contributions.map((c) => ({
    ...c,
    key: c.featureName,
  }));

  return (
    <Card
      size="small"
      title={
        <Space wrap>
          <Title level={5} style={{ margin: 0 }}>
            {t("supplierRisk.cardTitle")}
          </Title>
          <Tag color={LEVEL_COLOR[level]}>{LEVEL_LABEL[level]}</Tag>
          <Tag color="blue">{profile.enterpriseCode}</Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("supplierRisk.levelSource", { source: levelSource })}
          </Text>
        </Space>
      }
      extra={
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("supplierRisk.fetchedAt", { time: fetchedAt })}
        </Text>
      }
      style={{ background: "#fff7e6", marginTop: 8 }}
    >
      <Paragraph style={{ marginBottom: 8 }}>
        <Space size={4}>
          <Text strong>{t("supplierRisk.riskPointsLabel")}</Text>
          {riskPointsSource === "llm" ? (
            <Tag color="blue">{t("supplierRisk.sourceLlm")}</Tag>
          ) : (
            <Tag>{t("supplierRisk.sourceFallback")}</Tag>
          )}
        </Space>
        <div style={{ marginTop: 4 }}>
          {riskPoints ? (
            <Text>{riskPoints}</Text>
          ) : (
            <Text type="secondary">{t("supplierRisk.empty.riskPoints")}</Text>
          )}
        </div>
      </Paragraph>

      <Title level={5} style={{ marginTop: 12, marginBottom: 8 }}>
        {t("supplierRisk.section.actions")}
      </Title>
      <List
        size="small"
        bordered
        dataSource={recommendedActions}
        renderItem={(item: string, idx: number) => (
          <List.Item>
            <Text>
              <Text strong style={{ marginRight: 8 }}>
                {idx + 1}.
              </Text>
              {item}
            </Text>
          </List.Item>
        )}
      />

      <Title level={5} style={{ marginTop: 12, marginBottom: 8 }}>
        {t("supplierRisk.section.contributions")}
      </Title>
      <Table<ContributionRow>
        size="small"
        columns={columns}
        dataSource={rows}
        pagination={false}
      />

      <Space size={4} wrap style={{ marginTop: 8 }}>
        <Tag>
          Tokens: {tokensUsed} (P {promptTokens} / C {completionTokens})
        </Tag>
        <Tag>{t("supplierRisk.cost", { amount: cost.toFixed(6) })}</Tag>
        {llmModelName ? <Tag color="blue">{llmModelName}</Tag> : null}
      </Space>
    </Card>
  );
}