import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Card,
  Col,
  Input,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import {
  getVectorStats,
  listEmbeddings,
  type VectorEmbedding,
  type VectorStats,
} from "../api/systemViewer";

const { Text } = Typography;

const TYPE_OPTIONS = [
  { label: "全部", value: "" },
  { label: "Class", value: "class" },
  { label: "Property", value: "property" },
  { label: "Metric", value: "metric" },
];

const TYPE_COLORS: Record<string, string> = {
  class: "purple",
  property: "cyan",
  metric: "orange",
};

export default function MilvusVectorsPage() {
  const [stats, setStats] = useState<VectorStats | null>(null);
  const [typeFilter, setTypeFilter] = useState("");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [rows, setRows] = useState<VectorEmbedding[]>([]);
  const [loading, setLoading] = useState(false);

  // debounce search 300ms
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const loadStats = useCallback(async () => {
    try {
      const data = await getVectorStats();
      setStats(data);
    } catch {
      setStats(null);
    }
  }, []);

  const loadRows = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listEmbeddings(
        typeFilter || undefined,
        debouncedSearch,
      );
      setRows(data);
    } catch {
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [typeFilter, debouncedSearch]);

  useEffect(() => {
    void loadStats();
  }, [loadStats]);

  useEffect(() => {
    void loadRows();
  }, [loadRows]);

  const columns: ColumnsType<VectorEmbedding> = [
    {
      title: "ontology_id",
      dataIndex: "ontology_id",
      width: 100,
    },
    {
      title: "type",
      dataIndex: "type",
      width: 90,
      render: (v: string) => (
        <Tag color={TYPE_COLORS[v] ?? "default"}>{v}</Tag>
      ),
    },
    { title: "名称", dataIndex: "name", width: 200 },
    { title: "别名", dataIndex: "alias", width: 160 },
    {
      title: "描述",
      dataIndex: "description",
      ellipsis: true,
      render: (v) => v || "—",
    },
    {
      title: "向量维度",
      dataIndex: "dim",
      width: 100,
      render: (v: number) => <Text type="secondary">{v}</Text>,
    },
  ];

  return (
    <div style={{ overflow: "hidden" }}>
      <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
        <Col>
          <Card bodyStyle={{ padding: "12px 24px" }}>
            <Statistic
              title="Class"
              value={stats?.class ?? 0}
              valueStyle={{ color: "#722ed1" }}
            />
          </Card>
        </Col>
        <Col>
          <Card bodyStyle={{ padding: "12px 24px" }}>
            <Statistic
              title="Property"
              value={stats?.property ?? 0}
              valueStyle={{ color: "#0891b2" }}
            />
          </Card>
        </Col>
        <Col>
          <Card bodyStyle={{ padding: "12px 24px" }}>
            <Statistic
              title="Metric"
              value={stats?.metric ?? 0}
              valueStyle={{ color: "#d46b08" }}
            />
          </Card>
        </Col>
      </Row>

      <Space style={{ marginBottom: 12 }} wrap>
        <Select
          value={typeFilter}
          options={TYPE_OPTIONS}
          onChange={(v) => setTypeFilter(v)}
          style={{ width: 120 }}
        />
        <Input.Search
          placeholder="搜索名称 / 别名"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ width: 200 }}
        />
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void loadRows()} />
      </Space>

      <Card bodyStyle={{ padding: 0 }} style={{ overflow: "hidden" }}>
        <Table
          rowKey="ontology_id"
          loading={loading}
          dataSource={rows}
          columns={columns}
          pagination={{ pageSize: 20, showSizeChanger: false }}
        />
      </Card>
    </div>
  );
}
