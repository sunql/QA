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
import { useEffect, useState, useCallback, useMemo, useRef } from "react";
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
  Switch,
  InputNumber,
} from "antd";
import {
  ReloadOutlined,
  EditOutlined,
  PlusOutlined,
  CheckOutlined,
  SearchOutlined,
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
import { useTablePagination } from "../utils/useTablePagination";

const { Title } = Typography;
const { TextArea } = Input;

interface EditFormValues {
  description: string;
  allowedValues: string[];
  // 约束字段（feat-ontology-property-constraints）：任一非空表示已设置约束。
  isNotNull: boolean;
  minValue: number | null;
  maxValue: number | null;
  regexPattern: string | null;
}

export default function OntologyPropertyAdminPage(): JSX.Element {
  const { t } = useTranslation();
  const { pagination, setPage } = useTablePagination();
  const [classes, setClasses] = useState<OntologyClass[]>([]);
  const [properties, setProperties] = useState<OntologyProperty[]>([]);
  const [loading, setLoading] = useState(false);
  const [classFilter, setClassFilter] = useState<number | "all">("all");
  // 关键字过滤（feat-ontology-property-search）：对属性名 / 别名 / 物理列名 / 描述
  // 做大小写不敏感的子串匹配；空串视为无过滤。命中数从 N 跳到 M 时强制回到第 1 页。
  const [keyword, setKeyword] = useState("");

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
    const kw = keyword.trim().toLowerCase();
    return properties.filter((p) => {
      if (classFilter !== "all" && p.classId !== classFilter) return false;
      if (kw) {
        // 多字段 OR 命中：propertyName / propertyAlias / sourceColumn / description。
        // 任一字段包含关键字即视为命中（前端模糊搜，不是后端 ILIKE）。
        const haystack = [
          p.propertyName,
          p.propertyAlias,
          p.sourceColumn,
          p.description,
        ]
          .filter((s): s is string => Boolean(s))
          .join("")
          .toLowerCase();
        if (!haystack.includes(kw)) return false;
      }
      return true;
    });
  }, [properties, classFilter, keyword]);

  // 弹窗 destroyOnHidden 下，Modal 关闭态时 Form 未挂载，直接 setFieldsValue
  // 真机会触发 "useForm is not connected" 警告且回填可能被丢弃（jsdom 不复现）。
  // 与 ClassTab 同款：把写值时机挪到 Modal.afterOpenChange(true)。
  const pendingFormValues = useRef<Partial<EditFormValues> | null>(null);

  const openEdit = (rec: OntologyProperty) => {
    setEditing(rec);
    // minValue/maxValue 是 String（兼容日期 / 数字）；前端用 InputNumber 时先尝试数字转换，
    // 失败则置 null 让用户重填。regexPattern 直接字符串。
    const toNum = (s: string | null | undefined): number | null => {
      if (s === null || s === undefined || s === "") return null;
      const n = Number(s);
      return Number.isFinite(n) ? n : null;
    };
    pendingFormValues.current = {
      // 回填现有描述：硬编码空串会让用户「保存成功后重开仍为空」，误判为保存失败
      description: rec.description ?? "",
      allowedValues: rec.allowedValues ?? [],
      isNotNull: rec.isNotNull ?? false,
      minValue: toNum(rec.minValue),
      maxValue: toNum(rec.maxValue),
      regexPattern: rec.regexPattern ?? null,
    };
    setModalOpen(true);
  };

  const closeModal = () => {
    setModalOpen(false);
    setEditing(null);
    setAllowedValuesInput("");
    pendingFormValues.current = null;
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
      // 数值字段：null → 显式清空；数字 → 字符串存（后端是 VARCHAR(50)）
      const numToStr = (n: number | null | undefined): string | null => {
        if (n === null || n === undefined) return null;
        return String(n);
      };
      const payload: OntologyPropertyUpdate = {
        allowedValues: values.allowedValues ?? [],
        description: values.description || null,
        isNotNull: values.isNotNull,
        minValue: numToStr(values.minValue),
        maxValue: numToStr(values.maxValue),
        regexPattern: values.regexPattern || null,
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
      // 描述列：保存结果对用户可见（此前只有弹窗里能看，重开还是空串误判保存失败）
      title: t("ontologyPropertyAdmin.form.description"),
      dataIndex: "description",
      ellipsis: { showTitle: false },
      render: (v: string | null) =>
        v ? (
          <Popover content={<div style={{ maxWidth: 360 }}>{v}</div>} trigger="hover">
            {v}
          </Popover>
        ) : (
          <Tag color="default">{t("ontologyPropertyAdmin.values.none")}</Tag>
        ),
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
        <Space wrap>
          <Select
            value={classFilter}
            onChange={(v) => {
              setClassFilter(v as number | "all");
              setPage(1);
            }}
            style={{ minWidth: 240 }}
            options={[
              { value: "all", label: t("ontologyPropertyAdmin.filter.all") },
              ...classes.map((c) => ({ value: c.id, label: c.className })),
            ]}
          />
          <Input
            allowClear
            value={keyword}
            onChange={(e) => {
              setKeyword(e.target.value);
              setPage(1);
            }}
            placeholder={t("ontologyPropertyAdmin.filter.keywordPlaceholder")}
            prefix={<SearchOutlined />}
            style={{ minWidth: 280 }}
          />
        </Space>
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
        pagination={pagination}
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
        destroyOnHidden
        afterOpenChange={(open) => {
          // 弹窗完全打开、Form 子组件已挂载后再写值，避免 destroyOnHidden
          // 重挂载时序丢回填（同 ClassTab 的处理）
          if (open && pendingFormValues.current) {
            void form.setFieldsValue(pendingFormValues.current);
            pendingFormValues.current = null;
          }
        }}
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
          {/* 约束字段（feat-ontology-property-constraints）：
              与 description + allowedValues 同级编辑；任一非空都视作已沉淀约束。 */}
          <Form.Item
            name="isNotNull"
            label={t("ontologyPropertyAdmin.form.isNotNull")}
            tooltip={t("ontologyPropertyAdmin.form.isNotNullHint")}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="minValue"
            label={t("ontologyPropertyAdmin.form.minValue")}
            tooltip={t("ontologyPropertyAdmin.form.minValueHint")}
          >
            <InputNumber
              style={{ width: "100%" }}
              placeholder={t("ontologyPropertyAdmin.form.minValuePlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="maxValue"
            label={t("ontologyPropertyAdmin.form.maxValue")}
            tooltip={t("ontologyPropertyAdmin.form.maxValueHint")}
          >
            <InputNumber
              style={{ width: "100%" }}
              placeholder={t("ontologyPropertyAdmin.form.maxValuePlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="regexPattern"
            label={t("ontologyPropertyAdmin.form.regexPattern")}
            tooltip={t("ontologyPropertyAdmin.form.regexPatternHint")}
          >
            <Input
              allowClear
              placeholder={t("ontologyPropertyAdmin.form.regexPatternPlaceholder")}
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
