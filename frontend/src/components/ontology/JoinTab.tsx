import { useEffect, useState, useCallback, useMemo } from "react";
import {
  Table,
  Button,
  Checkbox,
  Modal,
  Form,
  Input,
  Select,
  Space,
  Tag,
  Tooltip,
  Popconfirm,
  message,
  Row,
  Col,
  Typography,
} from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { listJoins, createJoin, deleteJoin } from "../../api/ontology";
import type {
  OntologyClass,
  OntologyJoin,
  OntologyJoinCreate,
  JoinType,
  RelationType,
} from "../../types/ontology";
import { JOIN_TYPE_OPTIONS, RELATION_TYPE_OPTIONS } from "../../types/ontology";
import { useTranslation } from "../../i18n";
import FilterBar from "./FilterBar";
import type { FilterField } from "./FilterBar";
import { filterJoins } from "../../utils/ontologyFilter";
import type { FilterValues } from "../../utils/ontologyFilter";
import { classOptions } from "./classOptions";

const { TextArea } = Input;
const { Text } = Typography;

interface JoinFormValues {
  sourceClassId: number;
  sourceColumns: string;
  targetClassId: number;
  targetColumns: string;
  joinType: JoinType;
  relationType: RelationType;
  description: string;
}

const EMPTY_JOIN_FORM: JoinFormValues = {
  sourceClassId: 0,
  sourceColumns: "",
  targetClassId: 0,
  targetColumns: "",
  joinType: "INNER",
  relationType: "business",
  description: "",
};

/** 把逗号分隔的列名解析为去空格、去空项的非空数组。 */
function parseColumns(raw: string): string[] {
  return raw
    .split(",")
    .map((c) => c.trim())
    .filter((c) => c.length > 0);
}

export interface JoinTabProps {
  classes: OntologyClass[];
}

/** 关联目录（运行时 JOIN 唯一真源）：增/删 join 边（改 = 删 + 建）。 */
export default function JoinTab({ classes }: JoinTabProps) {
  const { t } = useTranslation();
  const [joins, setJoins] = useState<OntologyJoin[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm<JoinFormValues>();

  const joinTypeOptions = JOIN_TYPE_OPTIONS.map((o) => ({ value: o.value, label: o.labelKey }));
  const relationTypeOptions = RELATION_TYPE_OPTIONS.map((o) => ({
    value: o.value,
    label: o.labelKey,
  }));

  const className = (id: number) => classes.find((c) => c.id === id)?.className ?? `ID:${id}`;

  const [filters, setFilters] = useState<FilterValues>({});
  const updateFilter = useCallback(
    (k: string, v: string) => setFilters((prev) => ({ ...prev, [k]: v })),
    []
  );
  const resetFilters = useCallback(() => setFilters({}), []);
  // 「仅外来键」：只显示 relation_type=foreign_key 的边——这些多由本地导入
  // （声明外键 / Sage 列名约定）自动推断生成，聚焦核查机器推断是否正确。
  const [onlyForeignKeys, setOnlyForeignKeys] = useState(false);
  const filteredJoins = useMemo(() => {
    const byFilters = filterJoins(joins, filters);
    if (!onlyForeignKeys) return byFilters;
    return byFilters.filter((j) => j.relationType === "foreign_key");
  }, [joins, filters, onlyForeignKeys]);
  const joinClassFilterOptions = classOptions(t, classes).map((o) => ({
    value: String(o.value),
    label: o.label,
  }));
  const joinFilterFields: FilterField[] = [
    {
      key: "sourceClassId",
      label: t("forms.ontology.joinLabels.sourceClassId"),
      type: "select",
      options: joinClassFilterOptions,
    },
    {
      key: "targetClassId",
      label: t("forms.ontology.joinLabels.targetClassId"),
      type: "select",
      options: joinClassFilterOptions,
    },
    { key: "sourceColumns", label: t("forms.ontology.joinLabels.sourceColumns") },
    { key: "targetColumns", label: t("forms.ontology.joinLabels.targetColumns") },
  ];

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setJoins(await listJoins());
    } catch {
      // 错误已由拦截器提示
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    void form.setFieldsValue(EMPTY_JOIN_FORM);
    setModalOpen(true);
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteJoin(id);
      void message.success(t("toast.deleted"));
      void load();
    } catch {
      // 错误已由拦截器提示
    }
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      const payload: OntologyJoinCreate = {
        sourceClassId: values.sourceClassId,
        sourceColumns: parseColumns(values.sourceColumns),
        targetClassId: values.targetClassId,
        targetColumns: parseColumns(values.targetColumns),
        joinType: values.joinType,
        relationType: values.relationType,
        description: values.description || undefined,
      };
      await createJoin(payload);
      void message.success(t("toast.created"));
      setModalOpen(false);
      void load();
    } catch (err) {
      if (err instanceof Error && err.message.includes(t("forms.required"))) return;
    }
  };

  const columns = [
    { title: t("forms.ontology.joinColumns.id"), dataIndex: "id", width: 60 },
    {
      title: t("forms.ontology.joinColumns.source"),
      dataIndex: "sourceClassId",
      render: (_: unknown, record: OntologyJoin) => (
        <span>
          {className(record.sourceClassId)}
          <span style={{ color: "#999", margin: "0 6px" }}>→</span>
          {className(record.targetClassId)}
        </span>
      ),
    },
    {
      title: t("forms.ontology.joinColumns.sourceColumns"),
      dataIndex: "sourceColumns",
      width: 160,
      render: (cols: string[]) => cols.join(" + "),
    },
    {
      title: t("forms.ontology.joinColumns.targetColumns"),
      dataIndex: "targetColumns",
      width: 160,
      render: (cols: string[]) => cols.join(" + "),
    },
    {
      title: t("forms.ontology.joinColumns.joinType"),
      dataIndex: "joinType",
      width: 100,
      render: (v: string) => <Tag color="blue">{v}</Tag>,
    },
    {
      title: t("forms.ontology.joinColumns.relationType"),
      dataIndex: "relationType",
      width: 120,
      render: (v: string) =>
        v === "foreign_key" ? <Tag color="green">{v}</Tag> : <Tag>{v}</Tag>,
    },
    {
      title: t("forms.ontology.joinColumns.description"),
      dataIndex: "description",
      width: 280,
      ellipsis: { showTitle: false },
      render: (v: string | null) =>
        v ? (
          <Tooltip placement="topLeft" title={v}>
            {v}
          </Tooltip>
        ) : (
          t("common.emDash")
        ),
    },
    {
      title: t("forms.ontology.joinColumns.actions"),
      width: 100,
      render: (_: unknown, record: OntologyJoin) => (
        <Popconfirm
          title={t("forms.ontology.deleteConfirm")}
          onConfirm={() => void handleDelete(record.id)}
        >
          <Button size="small" danger>
            {t("common.delete")}
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
        <Space size="middle">
          <Checkbox
            checked={onlyForeignKeys}
            onChange={(e) => setOnlyForeignKeys(e.target.checked)}
          >
            {t("forms.ontology.joinOnlyForeignKeys")}
          </Checkbox>
          <Tooltip title={t("forms.ontology.joinOnlyForeignKeysHint")}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("forms.ontology.joinOnlyForeignKeysHint")}
            </Text>
          </Tooltip>
        </Space>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            {t("common.refresh")}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={openCreate}
            disabled={classes.length === 0}
          >
            {t("forms.ontology.addJoinButton")}
          </Button>
        </Space>
      </div>
      <FilterBar
        fields={joinFilterFields}
        values={filters}
        onChange={updateFilter}
        onReset={resetFilters}
      />
      <Table rowKey="id" loading={loading} dataSource={filteredJoins} columns={columns} />
      <Modal
        title={t("forms.ontology.addJoinModalTitle")}
        open={modalOpen}
        onOk={() => void handleSubmit()}
        onCancel={() => setModalOpen(false)}
        width={560}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={EMPTY_JOIN_FORM}>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="sourceClassId"
                label={t("forms.ontology.joinLabels.sourceClassId")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Select
                  placeholder={t("forms.ontology.joinPlaceholders.sourceClassId")}
                  options={classOptions(t, classes)}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="sourceColumns"
                label={t("forms.ontology.joinLabels.sourceColumns")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Input placeholder={t("forms.ontology.joinPlaceholders.sourceColumns")} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="targetClassId"
                label={t("forms.ontology.joinLabels.targetClassId")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Select
                  placeholder={t("forms.ontology.joinPlaceholders.targetClassId")}
                  options={classOptions(t, classes)}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="targetColumns"
                label={t("forms.ontology.joinLabels.targetColumns")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Input placeholder={t("forms.ontology.joinPlaceholders.targetColumns")} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="joinType"
                label={t("forms.ontology.joinLabels.joinType")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Select options={joinTypeOptions} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="relationType"
                label={t("forms.ontology.joinLabels.relationType")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Select options={relationTypeOptions} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="description" label={t("forms.ontology.joinLabels.description")}>
            <TextArea rows={3} placeholder={t("forms.ontology.joinPlaceholders.description")} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
