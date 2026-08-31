import { Card, Descriptions, Empty, Space, Statistic, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { Supplier360Kpi, Supplier360Read } from "../../types/supplier";
import { useTranslation } from "../../i18n";

const { Text, Title } = Typography;

interface Supplier360CardProps {
  data: Supplier360Read;
}

interface KpiRow extends Supplier360Kpi {
  key: string;
}

/**
 * Supplier 360° ADS 视图卡片（Phase 5.3）。
 *
 * - 主数据：来自 entity_mapping 的 enterpriseKey + enterpriseCode + owner + matchRule
 * - 跨系统编码：EntityMapping 的 sourceSystem/sourceCode 一行一条
 * - KPI：Feature 默认 4 项（OTD_3M / DEFECT_RATE_3M / PRICE_VARIANCE_3M / RISK_SCORE）；
 *   latest=false 表示 feature 已定义但禁用 / 无最新值，用 placeholder Tag 区分。
 *
 * 数据契约来自后端 Supplier360Read（CamelModel alias_generator → camelCase JSON）。
 */
export default function Supplier360Card({ data }: Supplier360CardProps) {
  const { t } = useTranslation();
  const { profile, entityCodes, kpis, fetchedAt } = data;

  const kpiColumns: ColumnsType<KpiRow> = [
    {
      title: t("supplier360.kpi.name"),
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
      title: t("supplier360.kpi.value"),
      dataIndex: "value",
      key: "value",
      render: (_, row) =>
        row.latest ? (
          <Statistic
            value={Number(row.value ?? 0)}
            precision={Number.isInteger(Number(row.value)) ? 0 : 2}
            suffix={row.unit ?? undefined}
            valueStyle={{ fontSize: 16 }}
          />
        ) : (
          <Tag color="default">{t("supplier360.kpi.placeholder")}</Tag>
        ),
    },
    {
      title: t("supplier360.kpi.window"),
      dataIndex: "windowSize",
      key: "windowSize",
      render: (v: string | null) => v ?? "—",
    },
    {
      title: t("supplier360.kpi.validAt"),
      dataIndex: "validAt",
      key: "validAt",
      render: (v: string | null) => v ?? "—",
    },
  ];

  const rows: KpiRow[] = kpis.map((k) => ({ ...k, key: k.featureName }));

  return (
    <Card
      size="small"
      title={
        <Space>
          <Title level={5} style={{ margin: 0 }}>
            {t("supplier360.cardTitle")}
          </Title>
          <Tag color="blue">{profile.enterpriseCode}</Tag>
        </Space>
      }
      extra={
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("supplier360.fetchedAt", { time: fetchedAt })}
        </Text>
      }
      style={{ background: "#fafafa", marginTop: 8 }}
    >
      <Descriptions
        size="small"
        column={2}
        items={[
          {
            key: "enterpriseKey",
            label: t("supplier360.field.enterpriseKey"),
            children: <Text code>{profile.enterpriseKey}</Text>,
          },
          {
            key: "owner",
            label: t("supplier360.field.owner"),
            children: profile.owner ?? "—",
          },
          {
            key: "matchRule",
            label: t("supplier360.field.matchRule"),
            children: profile.matchRule ? (
              <Tag color="geekblue">{profile.matchRule}</Tag>
            ) : (
              "—"
            ),
          },
          {
            key: "effective",
            label: t("supplier360.field.effective"),
            children:
              profile.effectiveDate || profile.expiryDate
                ? `${profile.effectiveDate ?? "—"} → ${profile.expiryDate ?? "∞"}`
                : "—",
          },
        ]}
      />

      <Title level={5} style={{ marginTop: 16, marginBottom: 8 }}>
        {t("supplier360.section.entityCodes")}
      </Title>
      {entityCodes.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t("supplier360.empty.entityCodes")}
        />
      ) : (
        <Space wrap>
          {entityCodes.map((code) => (
            <Tag key={`${code.sourceSystem}-${code.sourceKey}`} color="cyan">
              <strong>{code.sourceSystem}</strong>: {code.sourceCode}
            </Tag>
          ))}
        </Space>
      )}

      <Title level={5} style={{ marginTop: 16, marginBottom: 8 }}>
        {t("supplier360.section.kpis")}
      </Title>
      {rows.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t("supplier360.empty.kpis")}
        />
      ) : (
        <Table<KpiRow>
          size="small"
          columns={kpiColumns}
          dataSource={rows}
          pagination={false}
        />
      )}
    </Card>
  );
}