import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Form,
  Input,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
} from "antd";
import { useTranslation } from "react-i18next";
import { useTablePagination } from "../utils/useTablePagination";
import {
  type BusinessObjectCreate,
  type BusinessObject,
  type BusinessObjectUpdate,
} from "../types/businessObject";
import {
  createBusinessObject,
  deleteBusinessObject,
  listBusinessObjects,
  updateBusinessObject,
} from "../api/businessObject";
import { listClasses } from "../api/ontology";
import type { OntologyClass } from "../types/ontology";

export default function BusinessObjectPage() {
  const { t } = useTranslation();
  const { pagination } = useTablePagination();
  const [rows, setRows] = useState<BusinessObject[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<BusinessObject | null>(null);
  const [classes, setClasses] = useState<OntologyClass[]>([]);
  const [form] = Form.useForm();

  // 本体类选项（label 含别名，方便模糊匹配时一眼看出）
  const classOptions = useMemo(
    () =>
      classes.map((c) => ({
        value: c.id,
        label: c.classAlias ? `${c.className}（${c.classAlias}）` : c.className,
        data: c,
      })),
    [classes],
  );

  // 自定义过滤：大小写不敏感，对 className + classAlias 做子串匹配
  const filterClassOption = (input: string, option?: { data?: OntologyClass }) => {
    const cls = option?.data;
    if (!cls) return false;
    const needle = input.trim().toLowerCase();
    if (!needle) return true;
    return (
      (cls.className ?? "").toLowerCase().includes(needle) ||
      (cls.classAlias ?? "").toLowerCase().includes(needle)
    );
  };

  const load = async () => {
    setLoading(true);
    try {
      setRows(await listBusinessObjects());
    } finally {
      setLoading(false);
    }
  };

  const loadClasses = async () => {
    try {
      setClasses(await listClasses());
    } catch {
      // 加载失败时允许表单仍然打开（用户只能保存无本体类的业务对象）
      setClasses([]);
    }
  };

  useEffect(() => {
    void load();
    void loadClasses();
  }, []);

  const onCreate = () => {
    setEditing(null);
    form.resetFields();
    setModalOpen(true);
  };

  const onEdit = (row: BusinessObject) => {
    setEditing(row);
    // 若已选本体类但 graphLabel 与 className 不一致（历史脏数据），按现规则重新派生
    const linkedClass =
      row.headerClassId != null
        ? classes.find((c) => c.id === row.headerClassId)
        : undefined;
    const derivedGraphLabel = linkedClass?.className ?? row.graphLabel ?? undefined;
    form.setFieldsValue({
      code: row.code,
      name: row.name,
      headerClassId: row.headerClassId ?? undefined,
      graphLabel: derivedGraphLabel,
      description: row.description ?? undefined,
    });
    setModalOpen(true);
  };

  const onSubmit = async () => {
    let values;
    try {
      values = await form.validateFields();
    } catch {
      // antd validateFields 在校验失败时 reject；此时表单已经展示错误提示，无需再弹窗
      return;
    }
    try {
      if (editing) {
        const payload: BusinessObjectUpdate = {
          name: values.name,
          headerClassId: values.headerClassId ?? null,
          graphLabel: values.graphLabel ?? null,
          description: values.description ?? null,
        };
        await updateBusinessObject(editing.code, payload);
      } else {
        const payload: BusinessObjectCreate = {
          code: values.code,
          name: values.name,
          headerClassId: values.headerClassId ?? null,
          graphLabel: values.graphLabel ?? null,
          description: values.description ?? null,
        };
        await createBusinessObject(payload);
      }
      setModalOpen(false);
      void load();
    } catch (error: unknown) {
      // 提交失败时保留 modal，让用户看到错误并修正；antd toast 提示错误原因
      const detail = error instanceof Error ? error.message : String(error);
      void message.error(t("businessObject.messages.submitFailed", { detail }));
    }
  };

  const onDelete = async (code: string) => {
    await deleteBusinessObject(code);
    void load();
  };

  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ marginBottom: 16 }}>{t("businessObject.title")}</h2>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" onClick={onCreate}>
          {t("businessObject.newButton")}
        </Button>
      </Space>
      <Table
        loading={loading}
        dataSource={rows}
        pagination={pagination}
        rowKey="code"
        columns={[
          { title: t("businessObject.columns.code"), dataIndex: "code" },
          { title: t("businessObject.columns.name"), dataIndex: "name" },
          {
            title: t("businessObject.columns.graphLabel"),
            dataIndex: "graphLabel",
            render: (v: string | null) => (v ? <Tag>{v}</Tag> : "—"),
          },
          { title: t("businessObject.columns.description"), dataIndex: "description" },
          {
            title: "",
            key: "actions",
            render: (_, row) => (
              <Space>
                <Button size="small" onClick={() => onEdit(row)}>
                  {t("common.edit")}
                </Button>
                <Popconfirm
                  title={t("businessObject.modal.confirmDelete", { code: row.code })}
                  onConfirm={() => onDelete(row.code)}
                >
                  <Button size="small" danger>
                    {t("common.delete")}
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal
        open={modalOpen}
        title={editing ? t("businessObject.modal.editTitle") : t("businessObject.modal.createTitle")}
        onCancel={() => setModalOpen(false)}
        onOk={onSubmit}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="code"
            label={t("businessObject.columns.code")}
            // 与后端 _validateBusinessObjectCodeFormat 一致：大写字母 / 数字 / 下划线，1-20 字符
            rules={[
              { required: true },
              {
                pattern: /^[A-Z0-9_]+$/,
                message: t("businessObject.validation.codeFormat"),
              },
              { max: 20 },
            ]}
          >
            {editing ? (
              <Input disabled />
            ) : (
              <Input
                placeholder={t("businessObject.placeholders.codeNew")}
                maxLength={20}
              />
            )}
          </Form.Item>
          <Form.Item name="name" label={t("businessObject.columns.name")} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item
            name="headerClassId"
            label={t("businessObject.columns.headerClassId")}
            tooltip={t("businessObject.tooltips.headerClassId")}
          >
            <Select
              allowClear
              showSearch
              placeholder={t("businessObject.placeholders.selectHeaderClass")}
              options={classOptions}
              filterOption={filterClassOption}
              optionFilterProp="label"
              notFoundContent={t("businessObject.placeholders.noClass")}
              onChange={(_value, option) => {
                const cls = (option as { data?: OntologyClass } | undefined)?.data;
                // 选本体类 → 自动派生 graphLabel；清空 → 同步清空 graphLabel
                form.setFieldsValue({
                  graphLabel: cls?.className ?? undefined,
                });
              }}
            />
          </Form.Item>
          <Form.Item
            name="graphLabel"
            label={
              <Tooltip title={t("businessObject.tooltips.graphLabelAuto")}>
                {t("businessObject.columns.graphLabel")}
              </Tooltip>
            }
          >
            <Input
              disabled
              placeholder={t("businessObject.placeholders.graphLabelAuto")}
            />
          </Form.Item>
          <Form.Item name="description" label={t("businessObject.columns.description")}>
            <Input.TextArea rows={3} maxLength={4000} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
