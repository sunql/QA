import { useCallback, useState } from "react";
import {
  Button,
  Input,
  InputNumber,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  Empty,
} from "antd";
import { ApartmentOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import {
  BUSINESS_ENTITY_TYPES,
  type GraphTraversalHop,
  type GraphTraversalRead,
} from "../../types/graphTraversal";
import { traverseGraph } from "../../api/graphTraversal";
import { useTranslation } from "../../i18n";

const { Text } = Typography;

const DEFAULT_START_TYPE = "Supplier";
const DEFAULT_MAX_HOPS = 3;

interface HopRow extends GraphTraversalHop {
  key: string;
}

/**
 * 业务知识图谱多跳遍历面板（Phase 6.3，Neo4jGraphPage 的业务图 Tab）。
 *
 * - 查询表单：起点类型（白名单 Select）+ 实体键 + 最大跳数（1..5）
 * - 结果：可达类型徽标 + 逐跳可达链 Table
 * - 空结果 / 失败：显式提示（失败不静默吞掉）
 */
export default function GraphTraversalPanel() {
  const { t } = useTranslation();
  const [startType, setStartType] = useState<string>(DEFAULT_START_TYPE);
  const [startKey, setStartKey] = useState("");
  const [maxHops, setMaxHops] = useState<number>(DEFAULT_MAX_HOPS);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<GraphTraversalRead | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  const rows: HopRow[] = (result?.hops ?? []).map((h, idx) => ({
    ...h,
    key: `${idx}-${h.toKey}`,
  }));

  const onTraverse = useCallback(async () => {
    const key = startKey.trim();
    if (!key) {
      setError(t("graphTraversalPage.invalidKey"));
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await traverseGraph(startType, key, maxHops);
      setResult(data);
    } catch (e) {
      setResult(null);
      setError(
        t("graphTraversalPage.requestFailed", {
          message: e instanceof Error ? e.message : String(e),
        }),
      );
    } finally {
      setLoading(false);
    }
  }, [startType, startKey, maxHops, t]);

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          value={startType}
          onChange={setStartType}
          options={BUSINESS_ENTITY_TYPES.map((tp) => ({ label: tp, value: tp }))}
          style={{ width: 180 }}
          aria-label={t("graphTraversalPage.startType")}
        />
        <Input
          placeholder={t("graphTraversalPage.startKeyPlaceholder")}
          value={startKey}
          onChange={(e) => setStartKey(e.target.value)}
          onPressEnter={() => void onTraverse()}
          style={{ width: 200 }}
          allowClear
        />
        <InputNumber
          min={1}
          max={5}
          value={maxHops}
          onChange={(v) => setMaxHops(v ?? DEFAULT_MAX_HOPS)}
          style={{ width: 90 }}
          addonAfter={t("graphTraversalPage.maxHops")}
        />
        <Button
          type="primary"
          icon={<ApartmentOutlined />}
          loading={loading}
          onClick={() => void onTraverse()}
        >
          {t("graphTraversalPage.query")}
        </Button>
      </Space>

      {error ? (
        <Text type="danger">{error}</Text>
      ) : result === null ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t("graphTraversalPage.empty")}
        />
      ) : result.hops.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t("graphTraversal.empty.hops", {
            maxHops: String(result.maxHops),
          })}
        />
      ) : (
        <>
          <Space wrap style={{ marginBottom: 8 }}>
            <Text type="secondary">{t("graphTraversal.reachable")}</Text>
            {result.reachableTypes.map((tp) => (
              <Tag key={tp} color="geekblue">
                {tp}
              </Tag>
            ))}
          </Space>
          <Table<HopRow>
            rowKey="key"
            loading={loading}
            size="small"
            columns={columns}
            dataSource={rows}
            pagination={{ pageSize: 20, showSizeChanger: false }}
          />
        </>
      )}
    </div>
  );
}
