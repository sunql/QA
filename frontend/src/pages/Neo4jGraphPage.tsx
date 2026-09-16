import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Card,
  Input,
  Segmented,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { listGraphNodes, getGraphRelations, type GraphNode, type GraphRelation } from "../api/systemViewer";
import GraphTraversalPanel from "../components/graph/GraphTraversalPanel";

const { Text } = Typography;

const LABELS = ["Class", "Property", "Metric", "业务图"] as const;

function RelationPanel({ label, nodeId }: { label: string; nodeId: number }) {
  const [rels, setRels] = useState<GraphRelation[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    void getGraphRelations(label, nodeId)
      .then(setRels)
      .catch(() => setRels([]))
      .finally(() => setLoading(false));
  }, [label, nodeId]);

  if (loading) return <Text type="secondary">加载中...</Text>;
  if (rels.length === 0) return <Text type="secondary">（无关联）</Text>;
  return (
    <Space wrap>
      {rels.map((r) => (
        <Tag key={`${r.relType}-${r.targetId}`} color="blue">
          {r.relType} → {r.targetLabel}({r.targetName})
        </Tag>
      ))}
    </Space>
  );
}

export default function Neo4jGraphPage() {
  const [activeLabel, setActiveLabel] = useState<string>("Class");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [loading, setLoading] = useState(false);

  // debounce search 300ms
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const load = useCallback(async () => {
    // 「业务图」tab 无节点列表可加载，后端 label 白名单只认 Class/Property/Metric
    if (activeLabel === "业务图") return;
    setLoading(true);
    try {
      const data = await listGraphNodes(activeLabel, debouncedSearch);
      setNodes(data);
    } catch {
      setNodes([]);
    } finally {
      setLoading(false);
    }
  }, [activeLabel, debouncedSearch]);

  useEffect(() => {
    void load();
  }, [load]);

  const classColumns: ColumnsType<GraphNode> = [
    { title: "ID", dataIndex: "id", width: 60 },
    { title: "名称", dataIndex: "name", width: 160 },
    { title: "别名", dataIndex: "alias", width: 140 },
    { title: "源表", dataIndex: "sourceTable", width: 120 },
    {
      title: "描述",
      dataIndex: "description",
      ellipsis: true,
      render: (v) => v || "—",
    },
  ];

  const propertyColumns: ColumnsType<GraphNode> = [
    { title: "ID", dataIndex: "id", width: 60 },
    { title: "名称", dataIndex: "name", width: 160 },
    { title: "别名", dataIndex: "alias", width: 140 },
    { title: "数据类型", dataIndex: "dataType", width: 100 },
    { title: "源列", dataIndex: "sourceColumn", width: 120 },
    {
      title: "PK",
      dataIndex: "isPrimaryKey",
      width: 60,
      render: (v) => (v ? <Tag color="gold">PK</Tag> : null),
    },
    {
      title: "FK",
      dataIndex: "isForeignKey",
      width: 60,
      render: (v) => (v ? <Tag color="blue">FK</Tag> : null),
    },
    {
      title: "描述",
      dataIndex: "description",
      ellipsis: true,
      render: (v) => v || "—",
    },
  ];

  const metricColumns: ColumnsType<GraphNode> = [
    { title: "ID", dataIndex: "id", width: 60 },
    { title: "名称", dataIndex: "name", width: 160 },
    { title: "别名", dataIndex: "alias", width: 140 },
    { title: "聚合函数", dataIndex: "aggFunction", width: 100 },
    { title: "公式", dataIndex: "formula", ellipsis: true, render: (v) => v || "—" },
    {
      title: "描述",
      dataIndex: "description",
      ellipsis: true,
      render: (v) => v || "—",
    },
  ];

  const columns: ColumnsType<GraphNode> =
    activeLabel === "Class"
      ? classColumns
      : activeLabel === "Property"
      ? propertyColumns
      : metricColumns;

  // 业务图 Tab：直接渲染多跳遍历面板，跳过本体节点表
  if (activeLabel === "业务图") {
    return (
      <div style={{ overflow: "hidden" }}>
        <Space style={{ marginBottom: 12 }} wrap>
          <Segmented
            options={[...LABELS]}
            value={activeLabel}
            onChange={(v) => {
              setActiveLabel(String(v));
              setSearch("");
            }}
          />
        </Space>
        <Card styles={{ body: { padding: 16 } }} style={{ overflow: "hidden" }}>
          <GraphTraversalPanel />
        </Card>
      </div>
    );
  }

  return (
    <div style={{ overflow: "hidden" }}>
      <Space style={{ marginBottom: 12 }} wrap>
        <Segmented
          options={[...LABELS]}
          value={activeLabel}
          onChange={(v) => {
            setActiveLabel(String(v));
            setSearch("");
          }}
        />
        <Input.Search
          placeholder="搜索名称 / 别名"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ width: 200 }}
        />
        <Space.Compact>
          <Button
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => void load()}
          />
        </Space.Compact>
      </Space>

      <Card styles={{ body: { padding: 0 } }} style={{ overflow: "hidden" }}>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={nodes}
          columns={columns}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          expandable={{
            expandedRowRender: (record) => (
              <RelationPanel label={activeLabel} nodeId={record.id} />
            ),
          }}
        />
      </Card>
    </div>
  );
}
