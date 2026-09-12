/** LineagePage — 血缘可视化 + CRUD 管理（Phase 2.3 + Step 5 对象级筛选）。
 *
 * Tab 0「可视化」：原有 ECharts 血缘图（LayerFilter → 对象候选 → ObjectFilter → 图），
 * Tab 1「管理」：血缘边 CRUD —— 新建 / 编辑 / 删除（软删除）。
 *
 * 「自动抽取血缘」按钮挂在 Card.extra 上而**不在** GraphTab 里：它是页面级动作
 * （抽出来的边两个 Tab 都要看到），且既有测试按可访问名 /自动抽取血缘/ 直接取它。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  message,
} from "antd";
import { PlusOutlined, ReloadOutlined, SyncOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import {
  createEdge,
  deleteEdge,
  extractLineage,
  listEdges,
  updateEdge,
} from "../api/lineage";
import type {
  LineageEdgeCreate,
  LineageEdgeRead,
  LineageEdgeUpdate,
  LineageLayer,
  RefreshFrequency,
} from "../types/lineage";
import LayerFilter, { ALL_LAYERS } from "../components/lineage/LayerFilter";
import ObjectFilter from "../components/lineage/ObjectFilter";
import {
  collectObjectCandidates,
  filterEdgesByObjects,
  objectKey,
} from "../components/lineage/lineageFilter";
import LineageGraph from "../components/lineage/LineageGraph";

function errorMessageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

const LAYER_OPTIONS: { label: string; value: LineageLayer }[] = ALL_LAYERS.map((l) => ({
  label: l,
  value: l,
}));

/** 血缘边刷新频率可选值。
 *
 * 与后端 ``app/domain/enums.py`` 的 ``RefreshFrequency`` 对齐 —— **四档，含 WEEKLY**。
 * 这里是枚举的完整枚举而非「界面上常见的三档」：漏掉 WEEKLY 的话，编辑一条 WEEKLY
 * 的边时下拉里没有当前值，antd 会把原值当裸文本显示，用户一改动就可能把它写成别的档。
 */
const REFRESH_FREQUENCIES: RefreshFrequency[] = [
  "REALTIME",
  "HOURLY",
  "DAILY",
  "WEEKLY",
];

const REFRESH_OPTIONS: { label: string; value: RefreshFrequency }[] =
  REFRESH_FREQUENCIES.map((f) => ({ label: f, value: f }));

function isActiveColor(active: boolean): string {
  return active ? "green" : "default";
}

// ---------------------------------------------------------------------------
// Tab 0：可视化
// ---------------------------------------------------------------------------

interface GraphTabProps {
  edges: LineageEdgeRead[];
  loading: boolean;
  loadError: string | null;
}

function GraphTab({ edges, loading, loadError }: GraphTabProps) {
  const { t } = useTranslation();
  const [selectedLayers, setSelectedLayers] = useState<Set<LineageLayer>>(
    () => new Set(ALL_LAYERS),
  );
  const [selectedObjects, setSelectedObjects] = useState<Set<string>>(new Set());

  const layerFilteredEdges = useMemo(
    () =>
      edges.filter(
        (e) => selectedLayers.has(e.sourceLayer) && selectedLayers.has(e.targetLayer),
      ),
    [edges, selectedLayers],
  );

  // 对象候选：来自按层过滤后的 edges（随层筛选联动）
  const objectCandidates = useMemo(
    () => collectObjectCandidates(layerFilteredEdges),
    [layerFilteredEdges],
  );
  const candidateKeys = useMemo(
    () => new Set(objectCandidates.map((c) => objectKey(c.layer, c.object))),
    [objectCandidates],
  );

  // 级联裁剪：选中对象必须仍属于当前层过滤后的候选（取消某层 → 该层对象选择失效）
  const effectiveSelected = useMemo(
    () => new Set([...selectedObjects].filter((k) => candidateKeys.has(k))),
    [selectedObjects, candidateKeys],
  );

  const filteredEdges = useMemo(
    () => filterEdgesByObjects(layerFilteredEdges, effectiveSelected),
    [layerFilteredEdges, effectiveSelected],
  );

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <div>
        <strong style={{ marginRight: 8 }}>{t("lineage.filter.layers")}:</strong>
        <LayerFilter value={selectedLayers} onChange={setSelectedLayers} />
      </div>
      <div>
        <strong style={{ marginRight: 8 }}>{t("lineage.filter.objects")}:</strong>
        <ObjectFilter
          candidates={objectCandidates}
          value={effectiveSelected}
          onChange={setSelectedObjects}
        />
      </div>
      <div style={{ color: "#666", fontSize: 12 }}>
        {t("lineage.filter.summary", {
          selected: selectedLayers.size,
          total: ALL_LAYERS.length,
          objects: effectiveSelected.size,
          edges: filteredEdges.length,
          allEdges: edges.length,
        })}
      </div>
      {loadError ? <Alert type="error" message={loadError} showIcon /> : null}
      <Spin spinning={loading}>
        {filteredEdges.length > 0 ? (
          <LineageGraph edges={filteredEdges} height={820} />
        ) : (
          <Empty
            description={
              edges.length === 0
                ? t("lineage.empty.noData")
                : t("lineage.empty.filteredOut")
            }
          />
        )}
      </Spin>
    </Space>
  );
}

// ---------------------------------------------------------------------------
// Tab 1：管理（边 CRUD）
// ---------------------------------------------------------------------------

function ManageTab() {
  const { t } = useTranslation();
  const [edges, setEdges] = useState<LineageEdgeRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editing, setEditing] = useState<LineageEdgeRead | null>(null);
  const [creating, setCreating] = useState(false);
  const [form] = Form.useForm<LineageEdgeCreate>();

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      // 管理视图要看到**全部**边（含已停用），与可视化 Tab 的 activeOnly 不同
      const data = await listEdges({});
      setEdges(data);
    } catch (err: unknown) {
      setLoadError(errorMessageOf(err));
      message.error(t("lineage.manage.listFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleCreate = async () => {
    let values: LineageEdgeCreate;
    try {
      values = await form.validateFields();
    } catch {
      return; // 表单校验失败：antd 已在字段上标红，不要关弹窗丢掉已填内容
    }
    try {
      await createEdge(values);
      message.success(t("lineage.manage.createSuccess"));
      setCreating(false);
      form.resetFields();
      await refresh();
    } catch (err: unknown) {
      message.error(errorMessageOf(err));
    }
  };

  const handleUpdate = async () => {
    if (!editing) return;
    let values: LineageEdgeUpdate;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    try {
      await updateEdge(editing.id, values);
      message.success(t("lineage.manage.updateSuccess"));
      setEditing(null);
      form.resetFields();
      await refresh();
    } catch (err: unknown) {
      message.error(errorMessageOf(err));
    }
  };

  /** 删除单条边。**返回 Promise**，供 Popconfirm 的 ActionButton 做 loading + 防重入
   *  （返回值不是 thenable 时 antd 会立刻关弹窗并清掉 clickedRef，请求在途期间再点一次
   *  会真的发出第二个 DELETE）。内部已 try/catch 落定、不会 reject。 */
  const handleDelete = async (edge: LineageEdgeRead) => {
    try {
      await deleteEdge(edge.id);
      message.success(t("lineage.manage.deleteSuccess"));
      await refresh();
    } catch (err: unknown) {
      message.error(errorMessageOf(err));
    }
  };

  const openEdit = (edge: LineageEdgeRead) => {
    setEditing(edge);
    form.setFieldsValue({
      sourceLayer: edge.sourceLayer,
      sourceSystem: edge.sourceSystem,
      sourceObject: edge.sourceObject,
      sourceField: edge.sourceField ?? undefined,
      targetLayer: edge.targetLayer,
      targetSystem: edge.targetSystem,
      targetObject: edge.targetObject,
      targetField: edge.targetField ?? undefined,
      transformationRule: edge.transformationRule ?? undefined,
      refreshFrequency: edge.refreshFrequency,
      owner: edge.owner ?? undefined,
      description: edge.description ?? undefined,
    });
  };

  // 普通 const 而非 useMemo：handleDelete 每次渲染都是新函数，memo 的依赖恒变、等于没记忆
  const columns: ColumnsType<LineageEdgeRead> = [
    {
      title: t("lineage.manage.columns.id"),
      dataIndex: "id",
      key: "id",
      width: 60,
    },
    {
      title: t("lineage.manage.columns.sourceLayer"),
      dataIndex: "sourceLayer",
      key: "sourceLayer",
      width: 110,
      render: (v: LineageLayer) => <Tag>{v}</Tag>,
    },
    {
      title: t("lineage.manage.columns.sourceSystem"),
      dataIndex: "sourceSystem",
      key: "sourceSystem",
      width: 100,
    },
    {
      title: t("lineage.manage.columns.sourceObject"),
      dataIndex: "sourceObject",
      key: "sourceObject",
      width: 150,
    },
    {
      title: t("lineage.manage.columns.sourceField"),
      dataIndex: "sourceField",
      key: "sourceField",
      width: 120,
      render: (v: string | null) => v ?? t("common.dash"),
    },
    {
      title: t("lineage.manage.columns.targetLayer"),
      dataIndex: "targetLayer",
      key: "targetLayer",
      width: 110,
      render: (v: LineageLayer) => <Tag>{v}</Tag>,
    },
    {
      title: t("lineage.manage.columns.targetSystem"),
      dataIndex: "targetSystem",
      key: "targetSystem",
      width: 100,
    },
    {
      title: t("lineage.manage.columns.targetObject"),
      dataIndex: "targetObject",
      key: "targetObject",
      width: 150,
    },
    {
      title: t("lineage.manage.columns.targetField"),
      dataIndex: "targetField",
      key: "targetField",
      width: 120,
      render: (v: string | null) => v ?? t("common.dash"),
    },
    {
      title: t("lineage.manage.columns.refreshFrequency"),
      dataIndex: "refreshFrequency",
      key: "refreshFrequency",
      width: 100,
    },
    {
      title: t("lineage.manage.columns.owner"),
      dataIndex: "owner",
      key: "owner",
      width: 100,
      render: (o: string | null) => (o ? <Tag>{o}</Tag> : t("common.dash")),
    },
    {
      title: t("lineage.manage.columns.isActive"),
      dataIndex: "isActive",
      key: "isActive",
      width: 70,
      render: (active: boolean) => (
        <Tag color={isActiveColor(active)}>
          {active ? t("common.active") : t("common.disabled")}
        </Tag>
      ),
    },
    {
      title: t("lineage.manage.columns.createdTime"),
      dataIndex: "createdTime",
      key: "createdTime",
      width: 140,
      render: (v: string | null) =>
        v ? v.slice(0, 19).replace("T", " ") : t("common.dash"),
    },
    {
      title: t("lineage.manage.columns.actions"),
      key: "actions",
      width: 120,
      render: (_: unknown, record: LineageEdgeRead) => (
        <Space>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          <Popconfirm
            title={t("lineage.manage.deleteConfirm")}
            okText={t("common.delete")}
            cancelText={t("common.cancel")}
            okButtonProps={{ danger: true }}
            onConfirm={() => handleDelete(record)}
          >
            <Button size="small" danger>
              {t("common.delete")}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>
          {t("lineage.manage.newButton")}
        </Button>
        <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>
          {t("common.refresh")}
        </Button>
      </Space>

      {loadError ? <Alert type="error" message={loadError} showIcon /> : null}

      <Table
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={edges}
        pagination={{ pageSize: 20 }}
        scroll={{ x: 1500 }}
      />

      <Modal
        title={editing ? t("lineage.manage.editTitle") : t("lineage.manage.createTitle")}
        open={creating || editing !== null}
        onCancel={() => {
          setCreating(false);
          setEditing(null);
          form.resetFields();
        }}
        onOk={editing ? handleUpdate : handleCreate}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        width={680}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" preserve={false}>
          {/* 源端 */}
          <Form.Item
            name="sourceLayer"
            label={t("lineage.manage.form.sourceLayer")}
            rules={[{ required: true }]}
          >
            <Select options={LAYER_OPTIONS} />
          </Form.Item>
          <Form.Item
            name="sourceSystem"
            label={t("lineage.manage.form.sourceSystem")}
            rules={[{ required: true }]}
          >
            <Input placeholder={t("lineage.manage.placeholders.sourceSystem")} />
          </Form.Item>
          <Form.Item
            name="sourceObject"
            label={t("lineage.manage.form.sourceObject")}
            rules={[{ required: true }]}
          >
            <Input placeholder={t("lineage.manage.placeholders.sourceObject")} />
          </Form.Item>
          <Form.Item name="sourceField" label={t("lineage.manage.form.sourceField")}>
            <Input placeholder={t("lineage.manage.placeholders.sourceField")} />
          </Form.Item>

          {/* 目标端 */}
          <Form.Item
            name="targetLayer"
            label={t("lineage.manage.form.targetLayer")}
            rules={[{ required: true }]}
          >
            <Select options={LAYER_OPTIONS} />
          </Form.Item>
          <Form.Item
            name="targetSystem"
            label={t("lineage.manage.form.targetSystem")}
            rules={[{ required: true }]}
          >
            <Input placeholder={t("lineage.manage.placeholders.targetSystem")} />
          </Form.Item>
          <Form.Item
            name="targetObject"
            label={t("lineage.manage.form.targetObject")}
            rules={[{ required: true }]}
          >
            <Input placeholder={t("lineage.manage.placeholders.targetObject")} />
          </Form.Item>
          <Form.Item name="targetField" label={t("lineage.manage.form.targetField")}>
            <Input placeholder={t("lineage.manage.placeholders.targetField")} />
          </Form.Item>

          {/* 可选 */}
          <Form.Item
            name="transformationRule"
            label={t("lineage.manage.form.transformationRule")}
          >
            <Input placeholder={t("lineage.manage.placeholders.transformationRule")} />
          </Form.Item>
          <Form.Item
            name="refreshFrequency"
            label={t("lineage.manage.form.refreshFrequency")}
            rules={[{ required: true }]}
            initialValue="DAILY"
          >
            <Select options={REFRESH_OPTIONS} />
          </Form.Item>
          <Form.Item name="owner" label={t("lineage.manage.form.owner")}>
            <Input placeholder={t("lineage.manage.placeholders.owner")} />
          </Form.Item>
          <Form.Item name="description" label={t("lineage.manage.form.description")}>
            <Input.TextArea
              rows={2}
              placeholder={t("lineage.manage.placeholders.description")}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 页面
// ---------------------------------------------------------------------------

export default function LineagePage() {
  const { t } = useTranslation();
  const [edges, setEdges] = useState<LineageEdgeRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const data = await listEdges({ activeOnly: true });
      setEdges(data);
    } catch (error: unknown) {
      const msg = errorMessageOf(error);
      setLoadError(msg);
      message.error(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  // 自动抽取：POST /lineage/edges/extract（幂等，来源与 lineage_auto_extract.py 一致），
  // 成功后 toast 新增数并刷新列表；created=0（已是最新）走 info 而非成功。
  const handleExtract = useCallback(async () => {
    setExtracting(true);
    try {
      const result = await extractLineage();
      if (result.created > 0) {
        message.success(t("lineage.extract.created", { count: result.created }));
      } else {
        message.info(t("lineage.extract.noNew"));
      }
      await refresh();
    } catch (error: unknown) {
      message.error(errorMessageOf(error));
    } finally {
      setExtracting(false);
    }
  }, [refresh, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div style={{ padding: 16 }}>
      <Card
        title={t("lineage.page.title")}
        extra={
          <Space>
            <Button
              icon={<SyncOutlined />}
              onClick={() => void handleExtract()}
              loading={extracting}
            >
              {t("lineage.extract.button")}
            </Button>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => void refresh()}
              loading={loading}
            >
              {t("common.refresh")}
            </Button>
          </Space>
        }
      >
        <Tabs
          defaultActiveKey="graph"
          items={[
            {
              key: "graph",
              label: t("lineage.page.tabs.graph"),
              children: (
                <GraphTab edges={edges} loading={loading} loadError={loadError} />
              ),
            },
            {
              // 不给 ManageTab 挂 key={edges.length} 之类的重挂载键：切到管理页时
              // 若 edges 变化会把组件整个重建，正在填的新建/编辑弹窗会被清空
              key: "manage",
              label: t("lineage.page.tabs.manage"),
              children: <ManageTab />,
            },
          ]}
        />
      </Card>
    </div>
  );
}
