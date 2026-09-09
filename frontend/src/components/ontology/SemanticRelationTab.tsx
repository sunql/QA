import { useEffect, useState, useCallback } from "react";
import {
  Table,
  Button,
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
import {
  ThunderboltOutlined,
  PlusOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import {
  listSemanticRelations,
  createSemanticRelation,
  deleteSemanticRelation,
  backfillRelations,
} from "../../api/ontology";
import type {
  OntologyClass,
  OntologySemanticRelation,
  OntologySemanticRelationCreate,
  SemanticRelationType,
} from "../../types/ontology";
import { SEMANTIC_RELATION_TYPE_OPTIONS } from "../../types/ontology";
import { useTranslation } from "../../i18n";
import { classOptions } from "./classOptions";

const { TextArea } = Input;
const { Text } = Typography;

/** 语义关系类型 → Tag 颜色（新增类型需在此补充；默认无色）。 */
const RELATION_TAG_COLORS: Partial<Record<SemanticRelationType, string>> = {
  SUPPLIES: "green",
  CONTAINS: "blue",
  GENERATES: "geekblue",
  INSPECTED_BY: "purple",
  GENERATED: "orange",
};

interface SemanticRelationFormValues {
  sourceClassId: number;
  targetClassId: number;
  relationType: SemanticRelationType;
  description: string;
}

/** 新增表单默认值：源/目标类留空（0 不是合法类 id，且 required 把 0 视为已填）。 */
const NEW_RELATION_FORM_DEFAULTS: Partial<SemanticRelationFormValues> = {
  relationType: "SUPPLIES",
  description: "",
};

export interface SemanticRelationTabProps {
  classes: OntologyClass[];
}

/** 类 × 类语义关系（PG SSOT + Neo4j 镜像边）：表格 + 增删弹窗 + 一键补关系。 */
export default function SemanticRelationTab({ classes }: SemanticRelationTabProps) {
  const { t } = useTranslation();
  const [relations, setRelations] = useState<OntologySemanticRelation[]>([]);
  const [loading, setLoading] = useState(false);
  const [backfilling, setBackfilling] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [form] = Form.useForm<SemanticRelationFormValues>();

  const relationTypeOptions = SEMANTIC_RELATION_TYPE_OPTIONS.map((o) => ({
    value: o.value,
    label: t(`enums.semanticRelationType.${o.labelKey}`),
  }));

  const className = (id: number) => classes.find((c) => c.id === id)?.className ?? `ID:${id}`;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRelations(await listSemanticRelations());
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
    form.resetFields();
    void form.setFieldsValue(NEW_RELATION_FORM_DEFAULTS);
    setModalOpen(true);
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteSemanticRelation(id);
      void message.success(t("toast.deleted"));
      void load();
    } catch {
      // 错误已由拦截器提示
    }
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      const payload: OntologySemanticRelationCreate = {
        sourceClassId: values.sourceClassId,
        targetClassId: values.targetClassId,
        relationType: values.relationType,
        description: values.description || undefined,
      };
      await createSemanticRelation(payload);
      void message.success(t("toast.created"));
      setModalOpen(false);
      void load();
    } catch (err) {
      if (err instanceof Error && err.message.includes(t("forms.required"))) return;
    }
  };

  const handleBackfill = async () => {
    setBackfilling(true);
    try {
      const result = await backfillRelations();
      void message.success(
        t("forms.ontology.backfillRelationsSuccess", {
          syncedJoins: result.syncedJoins,
          backfilledReferences: result.backfilledReferences,
        })
      );
      void load();
    } catch {
      // 错误已由拦截器提示
    } finally {
      setBackfilling(false);
    }
  };

  const columns = [
    { title: t("forms.ontology.semanticRelationColumns.id"), dataIndex: "id", width: 60 },
    {
      title: t("forms.ontology.semanticRelationColumns.source"),
      dataIndex: "source",
      render: (_: unknown, record: OntologySemanticRelation) => (
        <span>
          {className(record.sourceClassId)}
          <span style={{ color: "#999", margin: "0 6px" }}>→</span>
          {className(record.targetClassId)}
        </span>
      ),
    },
    {
      title: t("forms.ontology.semanticRelationColumns.relationType"),
      dataIndex: "relationType",
      width: 180,
      render: (v: SemanticRelationType) => (
        <Tag color={RELATION_TAG_COLORS[v]}>{t(`enums.semanticRelationType.${v}`)}</Tag>
      ),
    },
    {
      title: t("forms.ontology.semanticRelationColumns.description"),
      dataIndex: "description",
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
      title: t("forms.ontology.semanticRelationColumns.actions"),
      width: 100,
      render: (_: unknown, record: OntologySemanticRelation) => (
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
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("forms.ontology.backfillRelationsHint")}
        </Text>
        <Space>
          <Popconfirm
            title={t("forms.ontology.backfillRelationsConfirm")}
            onConfirm={() => void handleBackfill()}
          >
            <Button icon={<ThunderboltOutlined />} loading={backfilling}>
              {t("forms.ontology.backfillRelationsButton")}
            </Button>
          </Popconfirm>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            {t("common.refresh")}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={openCreate}
            disabled={classes.length === 0}
          >
            {t("forms.ontology.addSemanticRelationButton")}
          </Button>
        </Space>
      </div>
      <Table rowKey="id" loading={loading} dataSource={relations} columns={columns} />
      <Modal
        title={t("forms.ontology.addSemanticRelationModalTitle")}
        open={modalOpen}
        onOk={() => void handleSubmit()}
        onCancel={() => setModalOpen(false)}
        width={560}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={NEW_RELATION_FORM_DEFAULTS}>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="sourceClassId"
                label={t("forms.ontology.semanticRelationLabels.sourceClassId")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Select
                  placeholder={t("forms.ontology.semanticRelationPlaceholders.sourceClassId")}
                  options={classOptions(t, classes)}
                />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="targetClassId"
                label={t("forms.ontology.semanticRelationLabels.targetClassId")}
                rules={[{ required: true, message: t("forms.required") }]}
              >
                <Select
                  placeholder={t("forms.ontology.semanticRelationPlaceholders.targetClassId")}
                  options={classOptions(t, classes)}
                />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item
            name="relationType"
            label={t("forms.ontology.semanticRelationLabels.relationType")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Select options={relationTypeOptions} />
          </Form.Item>
          <Form.Item
            name="description"
            label={t("forms.ontology.semanticRelationLabels.description")}
          >
            <TextArea
              rows={3}
              placeholder={t("forms.ontology.semanticRelationPlaceholders.description")}
            />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
