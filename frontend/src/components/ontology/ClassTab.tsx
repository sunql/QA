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
  Tooltip,
  Popconfirm,
  message,
} from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  createClass,
  updateClass,
  deleteClass,
  listClassVersions,
} from "../../api/ontology";
import type {
  OntologyClass,
  OntologyClassCreate,
  OntologyClassUpdate,
} from "../../types/ontology";
import { useTranslation } from "../../i18n";
import FilterBar from "./FilterBar";
import type { FilterField } from "./FilterBar";
import { filterClasses } from "../../utils/ontologyFilter";
import type { FilterValues } from "../../utils/ontologyFilter";
import { classOptions } from "./classOptions";

const { TextArea } = Input;

interface ClassFormValues {
  className: string;
  classAlias: string;
  description: string;
  sourceTable: string;
  parentClassId: number | undefined;
}

const EMPTY_CLASS_FORM: ClassFormValues = {
  className: "",
  classAlias: "",
  description: "",
  sourceTable: "",
  parentClassId: undefined,
};

export interface ClassTabProps {
  classes: OntologyClass[];
  refreshClasses: () => Promise<void>;
}

export default function ClassTab({ classes, refreshClasses }: ClassTabProps) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<OntologyClass | null>(null);
  const [form] = Form.useForm<ClassFormValues>();
  // 版本管理（Phase 6）：版本切换弹窗
  const [versionsModalOpen, setVersionsModalOpen] = useState(false);
  const [versions, setVersions] = useState<OntologyClass[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsOf, setVersionsOf] = useState<string | null>(null);
  const [filters, setFilters] = useState<FilterValues>({});
  const updateFilter = useCallback(
    (k: string, v: string) => setFilters((prev) => ({ ...prev, [k]: v })),
    []
  );
  const resetFilters = useCallback(() => setFilters({}), []);
  const filteredClasses = useMemo(() => filterClasses(classes, filters), [classes, filters]);
  const classFilterFields: FilterField[] = [
    { key: "className", label: t("forms.ontology.classLabels.className") },
    { key: "classAlias", label: t("forms.ontology.classLabels.classAlias") },
    { key: "sourceTable", label: t("forms.ontology.classLabels.sourceTable") },
    { key: "description", label: t("forms.ontology.classLabels.description") },
  ];

  const load = useCallback(async () => {
    setLoading(true);
    try {
      await refreshClasses();
    } catch {
      // 错误已由拦截器提示
    } finally {
      setLoading(false);
    }
  }, [refreshClasses]);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    setEditing(null);
    void form.setFieldsValue(EMPTY_CLASS_FORM);
    setModalOpen(true);
  };

  const openEdit = (record: OntologyClass) => {
    setEditing(record);
    void form.setFieldsValue({
      className: record.className,
      classAlias: record.classAlias ?? "",
      description: record.description ?? "",
      sourceTable: record.sourceTable ?? "",
      parentClassId: record.parentClassId ?? undefined,
    });
    setModalOpen(true);
  };

  /** 收集 classId 的所有后代 id（用于父类下拉排除，防止成环）。 */
  const collectDescendantIds = (classId: number): Set<number> => {
    const childrenMap = new Map<number, number[]>();
    for (const c of classes) {
      if (c.parentClassId != null) {
        const arr = childrenMap.get(c.parentClassId) ?? [];
        arr.push(c.id);
        childrenMap.set(c.parentClassId, arr);
      }
    }
    const result = new Set<number>();
    const stack = [classId];
    while (stack.length > 0) {
      const id = stack.pop() as number;
      for (const childId of childrenMap.get(id) ?? []) {
        if (!result.has(childId)) {
          result.add(childId);
          stack.push(childId);
        }
      }
    }
    return result;
  };

  /** 父类下拉选项：排除当前编辑类及其后代，避免成环。 */
  const parentClassOptions = (() => {
    const excluded = new Set<number>();
    if (editing) {
      excluded.add(editing.id);
      for (const id of collectDescendantIds(editing.id)) {
        excluded.add(id);
      }
    }
    return classOptions(t, classes.filter((c) => !excluded.has(c.id)));
  })();

  const handleDelete = async (id: number) => {
    try {
      await deleteClass(id);
      void message.success(t("toast.deleted"));
      void load();
    } catch {
      // 错误已由拦截器提示
    }
  };

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      const payload: OntologyClassCreate = {
        className: values.className,
        classAlias: values.classAlias || undefined,
        description: values.description || undefined,
        sourceTable: values.sourceTable || undefined,
        parentClassId: values.parentClassId,
      };
      if (editing) {
        const updatePayload: OntologyClassUpdate = { ...payload };
        await updateClass(editing.id, updatePayload);
        void message.success(t("toast.versionUpdated"));
      } else {
        await createClass(payload);
        void message.success(t("toast.created"));
      }
      setModalOpen(false);
      void load();
    } catch (err) {
      if (err instanceof Error && err.message.includes(t("forms.required"))) return;
      // 其他错误已由拦截器提示
    }
  };

  /** Phase 6：打开版本切换弹窗，列出该类全部历史版本（只读）。 */
  const openVersions = async (record: OntologyClass) => {
    setVersionsOf(record.className);
    setVersionsModalOpen(true);
    setVersionsLoading(true);
    try {
      setVersions(await listClassVersions(record.className));
    } catch {
      // 错误已由拦截器提示
      setVersions([]);
    } finally {
      setVersionsLoading(false);
    }
  };

  const columns = [
    { title: t("forms.ontology.classColumns.id"), dataIndex: "id", width: 60 },
    { title: t("forms.ontology.classColumns.className"), dataIndex: "className" },
    { title: t("forms.ontology.classColumns.classAlias"), dataIndex: "classAlias" },
    {
      title: t("forms.ontology.classColumns.version"),
      dataIndex: "version",
      width: 80,
      render: (v: number) =>
        v && v > 1 ? (
          <Tag color="blue">{t("forms.ontology.classColumns.versionTag", { version: v })}</Tag>
        ) : (
          t("forms.ontology.classColumns.versionTag", { version: 1 })
        ),
    },
    {
      title: t("forms.ontology.classColumns.validFrom"),
      dataIndex: "validFrom",
      width: 170,
      render: (s: string | null) => (s ? new Date(s).toLocaleString("zh-CN") : "-"),
    },
    {
      title: t("forms.ontology.classColumns.parentClassId"),
      dataIndex: "parentClassId",
      width: 120,
      render: (id: number | null) =>
        id ? classes.find((c) => c.id === id)?.className ?? `ID:${id}` : "-",
    },
    { title: t("forms.ontology.classColumns.sourceTable"), dataIndex: "sourceTable" },
    {
      title: t("forms.ontology.classColumns.description"),
      dataIndex: "description",
      width: 280,
      ellipsis: { showTitle: false },
      render: (v: string | null) =>
        v ? (
          <Tooltip placement="topLeft" title={v}>
            {v}
          </Tooltip>
        ) : (
          t("common.dash")
        ),
    },
    {
      title: t("forms.ontology.classColumns.actions"),
      width: 160,
      render: (_: unknown, record: OntologyClass) => (
        <Space>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          <Button size="small" onClick={() => void openVersions(record)}>
            {t("common.version")}
          </Button>
          <Popconfirm
            title={t("forms.ontology.deleteConfirm")}
            onConfirm={() => void handleDelete(record.id)}
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
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
        <span />
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            {t("common.refresh")}
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            {t("forms.ontology.addClassButton")}
          </Button>
        </Space>
      </div>
      <FilterBar
        fields={classFilterFields}
        values={filters}
        onChange={updateFilter}
        onReset={resetFilters}
      />
      <Table rowKey="id" loading={loading} dataSource={filteredClasses} columns={columns} />
      <Modal
        title={editing ? t("forms.ontology.editClassModalTitle") : t("forms.ontology.addClassModalTitle")}
        open={modalOpen}
        onOk={() => void handleSubmit()}
        onCancel={() => setModalOpen(false)}
        width={480}
        destroyOnClose
      >
        <Form form={form} layout="vertical" initialValues={EMPTY_CLASS_FORM}>
          <Form.Item
            name="className"
            label={t("forms.ontology.classLabels.className")}
            rules={[{ required: true, message: t("forms.required") }]}
          >
            <Input placeholder={t("forms.ontology.classPlaceholders.className")} />
          </Form.Item>
          <Form.Item name="classAlias" label={t("forms.ontology.classLabels.classAlias")}>
            <Input placeholder={t("forms.ontology.classPlaceholders.classAlias")} />
          </Form.Item>
          <Form.Item name="sourceTable" label={t("forms.ontology.classLabels.sourceTable")}>
            <Input placeholder={t("forms.ontology.classPlaceholders.sourceTable")} />
          </Form.Item>
          <Form.Item
            name="parentClassId"
            label={t("forms.ontology.classLabels.parentClassId")}
          >
            <Select
              allowClear
              placeholder={t("forms.ontology.classPlaceholders.parentClassId")}
              options={parentClassOptions}
            />
          </Form.Item>
          <Form.Item name="description" label={t("forms.ontology.classLabels.description")}>
            <TextArea rows={3} placeholder={t("forms.ontology.classPlaceholders.description")} />
          </Form.Item>
        </Form>
      </Modal>
      {/* Phase 6：版本切换弹窗（只读历史快照） */}
      <Modal
        title={t("forms.ontology.versionsModalTitle", { name: versionsOf ?? "" })}
        open={versionsModalOpen}
        onCancel={() => setVersionsModalOpen(false)}
        footer={<Button onClick={() => setVersionsModalOpen(false)}>{t("common.close")}</Button>}
        width={720}
      >
        <Table<OntologyClass>
          rowKey="id"
          loading={versionsLoading}
          dataSource={versions}
          pagination={false}
          size="small"
          columns={[
            {
              title: t("forms.ontology.versionColumns.version"),
              dataIndex: "version",
              width: 80,
              render: (v: number, row) =>
                row.validTo == null ? (
                  <Tag color="green">
                    {t("forms.ontology.classColumns.versionCurrentTag", { version: v })}
                  </Tag>
                ) : (
                  <Tag>{t("forms.ontology.classColumns.versionTag", { version: v })}</Tag>
                ),
            },
            {
              title: t("forms.ontology.versionColumns.validFrom"),
              dataIndex: "validFrom",
              width: 160,
              render: (s: string | null) => (s ? new Date(s).toLocaleString("zh-CN") : "-"),
            },
            {
              title: t("forms.ontology.versionColumns.validTo"),
              dataIndex: "validTo",
              width: 160,
              render: (s: string | null) => (s ? new Date(s).toLocaleString("zh-CN") : "-"),
            },
            { title: t("forms.ontology.versionColumns.description"), dataIndex: "description" },
          ]}
        />
      </Modal>
    </>
  );
}
