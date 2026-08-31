import { Card, Empty, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { GraphTraversalHop, GraphTraversalRead } from "../../types/graphTraversal";
import { useTranslation } from "../../i18n";

const { Text, Title } = Typography;

interface GraphTraversalCardProps {
  data: GraphTraversalRead;
}

interface HopRow extends GraphTraversalHop {
  key: string;
}

/**
 * 知识图谱多跳推理卡片（Phase 6.3）。
 *
 * - 头部：startType / startKey / maxHops Tag + 可达类型徽标
 * - 正文：逐跳可达链 Table（depth / relType / from → to）
 * - 空结果：无关联实体提示（非 404，业务图确实无连接）
 */
export default function GraphTraversalCard({ data }: GraphTraversalCardProps) {
  const { t } = useTranslation();
  const { startType, startKey, maxHops, hops, reachableTypes, fetchedAt } = data;

  const columns: ColumnsType<HopRow> = [
    {
      title: t("graphTraversal.table.depth"),
      dataIndex: "depth",
      key: "depth",
      width: 70,
      render: (d: number) => <Tag color="blue">Hop {d}</Tag>,
    },
    {
      title: t("graphTraversal.table.relation"),
      dataIndex: "relType",
      key: "relType",
      width: 140,
      render: (r: string) => <Text code>{r}</Text>,
    },
    {
      title: t("graphTraversal.table.from"),
      key: "from",
      width: 200,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>
            {row.fromType} · {row.fromCode}
          </Text>
          {row.fromName && row.fromName !== row.fromCode ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {row.fromName}
            </Text>
          ) : null}
        </Space>
      ),
    },
    {
      title: t("graphTraversal.table.to"),
      key: "to",
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>
            {row.toType} · {row.toCode}
          </Text>
          {row.toName && row.toName !== row.toCode ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {row.toName}
            </Text>
          ) : null}
        </Space>
      ),
    },
  ];

  const rows: HopRow[] = hops.map((h, idx) => ({ ...h, key: `${idx}-${h.toKey}` }));

  return (
    <Card
      size="small"
      title={
        <Space wrap>
          <Title level={5} style={{ margin: 0 }}>
            {t("graphTraversal.cardTitle")}
          </Title>
          <Tag color="purple">{startType}</Tag>
          <Tag>{startKey}</Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("graphTraversal.maxHops", { hops: maxHops })}
          </Text>
        </Space>
      }
      extra={
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("graphTraversal.fetchedAt", { time: fetchedAt })}
        </Text>
      }
      style={{ background: "#f6ffed", marginTop: 8 }}
    >
      {hops.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t("graphTraversal.empty.hops")}
        />
      ) : (
        <>
          <Space wrap style={{ marginBottom: 8 }}>
            <Text type="secondary">{t("graphTraversal.reachable")}</Text>
            {reachableTypes.length === 0 ? (
              <Tag>{t("graphTraversal.empty.types")}</Tag>
            ) : (
              reachableTypes.map((tp) => <Tag key={tp} color="geekblue">{tp}</Tag>)
            )}
          </Space>
          <Table<HopRow>
            size="small"
            columns={columns}
            dataSource={rows}
            pagination={{ pageSize: 10, showSizeChanger: false }}
          />
        </>
      )}
    </Card>
  );
}
