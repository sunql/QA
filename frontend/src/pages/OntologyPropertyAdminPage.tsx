/**
 * 本体属性管理页（/ontology-properties）
 *
 * 独立于 /ontology 的 PropertyTab，提供跨类的「全部本体属性」视图与编辑能力，
 * 重点暴露 LLM 采纳的 allowed_values（用户「我哪里去看」的入口）。
 *
 * 设计取舍：
 * - 不重复 create/delete，由 /ontology 的 PropertyTab 处理（CRUD SSOT 在那里）；
 * - 只做 list + edit (description + allowedValues)，
 *   这两个字段是「LLM 采纳并沉淀」闭环的核心元数据。
 * - 列表来源：GET /ontology/properties（一次性返回全部，避免 N+1）。
 */
import { useEffect, useState, useCallback, useMemo } from "react";
import {
  Table,
  Button,
  Modal,
  Form,
  Input,
  Select,
  Space,
  Tag,
  message,
  Typography,
  Popover,
} from "antd";
import {
  ReloadOutlined,
  EditOutlined,
  PlusOutlined,
  CheckOutlined,
} from "@ant-design/icons";
import {
  listAllProperties,
  listClasses,
  updateProperty,
} from "../api/ontology";
import type {
  OntologyProperty,
  OntologyPropertyUpdate,
  OntologyClass,
} from "../types/ontology";
import { useTranslation } from "../i18n";

const { Title } = Typography;
const { TextArea } = Input;

interface EditFormValues {
  description: string;
  allowedValues: string[];
}

export default function OntologyPropertyAdminPage(): JSX.Element {
  const { t } = useTranslation();
  const [classes, setClasses] = useState<OntologyClass[]>([]);
  const [properties, setProperties] = useState<OntologyProperty[]>([]);
  const [loading, setLoading] = useState(false);
  const [classFilter, setClassFilter] = useState<number | "all">("all");

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<OntologyProperty | null>(null);
  const [form] = Form.useForm<EditFormValues>();
  const [allowedValuesInput, setAllowedValuesInput] = useState("");

  const classNameById = useMemo(() => {
    const m = new Map<number, string>();
    for (const c of classes) m.set(c.id, c.className);
    return m;
  }, [classes]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [props, cls] = await Promise.all([listAllProperties(), listClasses()]);
      setProperties(props);
      setClasses(cls);
    } catch (e) {
      const err = e as Error & { message?: string };
      message.error(
        t("ontologyPropertyAdmin.messages.loadFailed") + ": " + (err.message ?? String(e))
      );
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const filtered = useMemo(() => {
    if (classFilter === "all") return properties;
    return properties.filter((p) => p.classId === classFilter);
  }, [properties, classFilter]);

  const openEdit = (rec: OntologyProperty) => {
    setEditing(rec);
    form.setFieldsValue({
      description: "", // 后端 OntologyPropertyRead 当前不返回 description（不影响编辑：默认空，submit 时不修改）
      allowedValues: rec.allowedValues ?? [],
    });
    setModalOpen(true);
  };

  const closeModal = () => {
    setModalOpen(false);
    setEditing(null);
    setAllowedValuesInput("");
    form.resetFields();
  };

  const addAllowedValue = (current: string[] | undefined): string[] => {
    const v = allowedValuesInput.trim();
    if (!v) return current ?? [];
    if ((current ?? []).includes(v)) return current ?? [];
    return [...(current ?? []), v];
  };

  const onSubmit = async () => {
    if (!editing) return;
    try {
      const values = await form.validateFields();
      const payload: OntologyPropertyUpdate = {
        allowedValues: values.allowedValues ?? [],
        description: values.description || null,
      };
      await updateProperty(editing.id, payload);
      message.success(t("ontologyPropertyAdmin.messages.updated"));
      closeModal();
      void load();
    } catch (e) {
      const err = e as Error & { message?: string };
      // 422 from Pydantic (single-quote guard etc.)
      const status = (err as Error & { status?: number }).status;
      if (status === 422) {
        message.error(t("ontologyPropertyAdmin.messages.badValue"));
        return;
      }
      message.error(
        t("ontologyPropertyAdmin.messages.updateFailed") + ": " + (err.message ?? String(e))
      );
    }
  };

  const columns = [
    {
      title: t("ontologyPropertyAdmin.columns.className"),
      dataIndex: "classId",
      width: 160,
      render: (id: number) => classNameById.get(id) ?? `ID:${id}`,
    },
    {
      title: t("ontologyPropertyAdmin.columns.propertyName"),
      dataIndex: "propertyName",
      width: 200,
    },
    {
      title: t("ontologyPropertyAdmin.columns.dataType"),
      dataIndex: "dataType",
      width: 110,
    },
    {
      title: t("ontologyPropertyAdmin.columns.allowedValues"),
      dataIndex: "allowedValues",
      render: (vals: string[] | null) => {
        if (!vals || vals.length === 0) {
          return <Tag color="default">{t("ontologyPropertyAdmin.values.none")}</Tag>;
        }
        return (
          <Popover
            content={
              <div style={{ maxWidth: 320 }}>
                {vals.map((v) => (
                  <Tag key={v} style={{ margin: 2 }}>{v}</Tag>
                ))}
              </div>
            }
            title={t("ontologyPropertyAdmin.columns.allowedValues")}
            trigger="hover"
          >
            {vals.slice(0, 3).map((v) => (
              <Tag key={v} color="blue" style={{ margin: 2 }}>{v}</Tag>
            ))}
            {vals.length > 3 && (
              <Tag style={{ margin: 2 }}>+{vals.length - 3}</Tag>
            )}
          </Popover>
        );
      },
    },
    {
      title: t("ontologyPropertyAdmin.columns.actions"),
      width: 110,
      render: (_: unknown, rec: OntologyProperty) => (
        <Button
          size="small"
          icon={<EditOutlined />}
          onClick={() => openEdit(rec)}
        >
          {t("common.edit")}
        </Button>
      ),
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <Title level={3}>{t("ontologyPropertyAdmin.title")}</Title>
      <div style={{ marginBottom: 16, color: "#666" }}>
        {t("ontologyPropertyAdmin.subtitle")}
      </div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginBottom: 16,
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        <Select
          value={classFilter}
          onChange={(v) => setClassFilter(v as number | "all")}
          style={{ minWidth: 240 }}
          options={[
            { value: "all", label: t("ontologyPropertyAdmin.filter.all") },
            ...classes.map((c) => ({ value: c.id, label: c.className })),
          ]}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>
          {t("common.refresh")}
        </Button>
      </div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={filtered}
        columns={columns}
        size="middle"
        pagination={{ pageSize: 20, showSizeChanger: true }}
      />
      <Modal
        title={
          editing
            ? t("ontologyPropertyAdmin.modal.editTitle", { name: editing.propertyName })
            : ""
        }
        open={modalOpen}
        onOk={() => void onSubmit()}
        onCancel={closeModal}
        width={560}
        destroyOnClose
        okText={t("common.save")}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="description"
            label={t("ontologyPropertyAdmin.form.description")}
            tooltip={t("ontologyPropertyAdmin.form.descriptionHint")}
          >
            <TextArea
              rows={3}
              placeholder={t("ontologyPropertyAdmin.form.descriptionPlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="allowedValues"
            label={t("ontologyPropertyAdmin.form.allowedValues")}
            tooltip={t("ontologyPropertyAdmin.form.allowedValuesHint")}
            valuePropName="value"
            trigger="onChange"
            getValueFromEvent={(vals: string[]) => vals}
            normalize={(v: unknown) => (Array.isArray(v) ? v : [])}
          >
            <Select
              mode="tags"
              style={{ width: "100%" }}
              placeholder={t("ontologyPropertyAdmin.form.allowedValuesPlaceholder")}
              tokenSeparators={[",", " ", "\n"]}
              searchValue={allowedValuesInput}
              onSearch={setAllowedValuesInput}
              onChange={(_, _option) => {
                // Select mode=tags already appends the input on Enter/blur,
                // so we just clear the search box to keep input clean.
                setAllowedValuesInput("");
              }}
              suffixIcon={
                <Button
                  type="link"
                  size="small"
                  icon={<PlusOutlined />}
                  onClick={(e) => {
                    e.preventDefault();
                    const current = form.getFieldValue("allowedValues") as string[] | undefined;
                    const next = addAllowedValue(current);
                    if (next.length !== (current ?? []).length) {
                      form.setFieldValue("allowedValues", next);
                      setAllowedValuesInput("");
                    }
                  }}
                  style={{ marginRight: -8 }}
                >
                  <CheckOutlined />
                </Button>
              }
              tagRender={(props) => (
                <Tag closable={props.closable} onClose={props.onClose} style={{ margin: 2 }}>
                  {props.label}
                </Tag>
              )}
            />
          </Form.Item>
          <div style={{ color: "#999", fontSize: 12 }}>
            <Space direction="vertical" size={2}>
              <span>{t("ontologyPropertyAdmin.form.allowedValuesHelp1")}</span>
              <span>{t("ontologyPropertyAdmin.form.allowedValuesHelp2")}</span>
            </Space>
          </div>
        </Form>
      </Modal>
    </div>
  );
}
