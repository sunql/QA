import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import {
  createMapping,
  deleteMapping,
  listMappings,
  updateMapping,
} from "../api/entityMapping";
import BulkImportModal from "../components/entity-mapping/BulkImportModal";
import type {
  EntityMappingCreate,
  EntityMappingRead,
  EntityMappingUpdate,
  EntityType,
  MatchRule,
  SourceSystem,
} from "../types/entityMapping";

const ENTITY_TYPES: EntityType[] = [
  "SUPPLIER",
  "MATERIAL",
  "PO",
  "GR",
  "IQC",
  "NCR",
];

const SOURCE_SYSTEMS: SourceSystem[] = ["ERP", "SRM", "QMS", "MDM", "PLM"];

const MATCH_RULES: MatchRule[] = ["MDM_MASTER", "BUSINESS_KEY", "MAPPING"];

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

/** 日期输入为空串 → null：创建时存 NULL，编辑时显式清除已有日期（后端 date|null 拒绝 ""）。 */
function normalizeDates<T extends EntityMappingCreate | EntityMappingUpdate>(
  values: T,
): T {
  return {
    ...values,
    effectiveDate: values.effectiveDate === "" ? null : values.effectiveDate,
    expiryDate: values.expiryDate === "" ? null : values.expiryDate,
  };
}

/** matchRule 语义色：MDM 主数据=绿 / 业务键=蓝 / 映射表=橙。 */
function matchRuleColor(rule: MatchRule): string {
  switch (rule) {
    case "MDM_MASTER":
      return "green";
    case "BUSINESS_KEY":
      return "blue";
    default:
      return "orange";
  }
}

export default function EntityMappingPage() {
  const { t } = useTranslation();
  const [mappings, setMappings] = useState<EntityMappingRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [filterType, setFilterType] = useState<EntityType | undefined>();
  const [editing, setEditing] = useState<EntityMappingRead | null>(null);
  const [creating, setCreating] = useState(false);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [form] = Form.useForm<EntityMappingCreate>();

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listMappings(
        filterType ? { entityType: filterType } : undefined,
      );
      setMappings(data);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(msg);
    } finally {
      setLoading(false);
    }
  }, [filterType]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleCreate = async () => {
    let values: EntityMappingCreate;
    try {
      values = normalizeDates(await form.validateFields());
    } catch {
      return; // 校验失败：antd 已展示行内错误，无需 toast
    }
    try {
      await createMapping(values);
      message.success(t("entityMapping.createSuccess"));
      setCreating(false);
      form.resetFields();
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(msg);
    }
  };

  const handleUpdate = async () => {
    if (!editing) return;
    let values: EntityMappingUpdate;
    try {
      values = normalizeDates(await form.validateFields());
    } catch {
      return; // 校验失败：antd 已展示行内错误，无需 toast
    }
    try {
      await updateMapping(editing.id, values);
      message.success(t("entityMapping.updateSuccess"));
      setEditing(null);
      form.resetFields();
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(msg);
    }
  };

  const handleDelete = async (mapping: EntityMappingRead) => {
    try {
      await deleteMapping(mapping.id);
      message.success(t("toast.deleted"));
      await refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      message.error(msg);
    }
  };

  const openEdit = (mapping: EntityMappingRead) => {
    setEditing(mapping);
    form.setFieldsValue({
      entityType: mapping.entityType,
      enterpriseKey: mapping.enterpriseKey,
      enterpriseCode: mapping.enterpriseCode,
      sourceSystem: mapping.sourceSystem,
      sourceKey: mapping.sourceKey,
      sourceCode: mapping.sourceCode,
      matchRule: mapping.matchRule,
      effectiveDate: mapping.effectiveDate ?? undefined,
      expiryDate: mapping.expiryDate ?? undefined,
    });
  };

  const columns: ColumnsType<EntityMappingRead> = [
    {
      title: t("entityMapping.id"),
      dataIndex: "id",
      key: "id",
      width: 70,
    },
    {
      title: t("entityMapping.entityType"),
      dataIndex: "entityType",
      key: "entityType",
      width: 120,
    },
    {
      title: t("entityMapping.enterpriseKey"),
      dataIndex: "enterpriseKey",
      key: "enterpriseKey",
      width: 110,
    },
    {
      title: t("entityMapping.enterpriseCode"),
      dataIndex: "enterpriseCode",
      key: "enterpriseCode",
      width: 140,
    },
    {
      title: t("entityMapping.sourceSystem"),
      dataIndex: "sourceSystem",
      key: "sourceSystem",
      width: 100,
    },
    {
      title: t("entityMapping.sourceKey"),
      dataIndex: "sourceKey",
      key: "sourceKey",
      width: 130,
    },
    {
      title: t("entityMapping.sourceCode"),
      dataIndex: "sourceCode",
      key: "sourceCode",
      width: 130,
    },
    {
      title: t("entityMapping.matchRule"),
      dataIndex: "matchRule",
      key: "matchRule",
      width: 120,
      render: (rule: MatchRule) => (
        <Tag color={matchRuleColor(rule)}>{rule}</Tag>
      ),
    },
    {
      title: t("entityMapping.effectiveDate"),
      dataIndex: "effectiveDate",
      key: "effectiveDate",
      width: 110,
      render: (d: string | null) => d ?? t("common.dash"),
    },
    {
      title: t("entityMapping.expiryDate"),
      dataIndex: "expiryDate",
      key: "expiryDate",
      width: 110,
      render: (d: string | null) => d ?? t("common.dash"),
    },
    {
      // 服务端按 actor.departments[0] 派生，仅展示（不可编辑）
      title: t("entityMapping.owner"),
      dataIndex: "owner",
      key: "owner",
      width: 110,
      render: (o: string | null) => (o ? <Tag>{o}</Tag> : t("common.dash")),
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 140,
      render: (_: unknown, record: EntityMappingRead) => (
        <Space>
          <Button size="small" onClick={() => openEdit(record)}>
            {t("common.edit")}
          </Button>
          <Popconfirm
            title={t("entityMapping.deleteConfirm")}
            onConfirm={() => void handleDelete(record)}
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
        <Select
          allowClear
          placeholder={t("entityMapping.filterType")}
          style={{ width: 200 }}
          value={filterType}
          onChange={(v) => setFilterType(v as EntityType | undefined)}
          options={ENTITY_TYPES.map((et) => ({ label: et, value: et }))}
        />
        <Button type="primary" onClick={() => setCreating(true)}>
          {t("entityMapping.createMapping")}
        </Button>
        <Button onClick={() => setBulkOpen(true)}>
          批量导入
        </Button>
        <Button onClick={() => void refresh()}>
          {t("common.refresh")}
        </Button>
      </Space>

      <Table
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={mappings}
        pagination={{ pageSize: 20 }}
      />

      <Modal
        title={
          editing
            ? t("entityMapping.editMapping")
            : t("entityMapping.createMapping")
        }
        open={creating || editing !== null}
        onCancel={() => {
          setCreating(false);
          setEditing(null);
          form.resetFields();
        }}
        onOk={editing ? handleUpdate : handleCreate}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        width={640}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item
            name="entityType"
            label={t("entityMapping.entityType")}
            rules={[{ required: true }]}
          >
            <Select
              disabled={!!editing}
              options={ENTITY_TYPES.map((et) => ({ label: et, value: et }))}
            />
          </Form.Item>
          <Form.Item
            name="enterpriseKey"
            label={t("entityMapping.enterpriseKey")}
            rules={[{ required: true }]}
          >
            <InputNumber
              disabled={!!editing}
              min={1}
              style={{ width: "100%" }}
            />
          </Form.Item>
          <Form.Item
            name="enterpriseCode"
            label={t("entityMapping.enterpriseCode")}
            rules={[{ required: true }]}
          >
            <Input disabled={!!editing} />
          </Form.Item>
          <Form.Item
            name="sourceSystem"
            label={t("entityMapping.sourceSystem")}
            rules={[{ required: true }]}
          >
            <Select
              disabled={!!editing}
              options={SOURCE_SYSTEMS.map((s) => ({ label: s, value: s }))}
            />
          </Form.Item>
          <Form.Item
            name="sourceKey"
            label={t("entityMapping.sourceKey")}
            rules={[{ required: true }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="sourceCode"
            label={t("entityMapping.sourceCode")}
            rules={[{ required: true }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="matchRule"
            label={t("entityMapping.matchRule")}
          >
            <Select options={MATCH_RULES.map((r) => ({ label: r, value: r }))} />
          </Form.Item>
          <Form.Item
            name="effectiveDate"
            label={t("entityMapping.effectiveDate")}
            rules={[{ pattern: DATE_PATTERN, message: t("entityMapping.datePattern") }]}
          >
            <Input placeholder={t("entityMapping.datePlaceholder")} />
          </Form.Item>
          <Form.Item
            name="expiryDate"
            label={t("entityMapping.expiryDate")}
            rules={[{ pattern: DATE_PATTERN, message: t("entityMapping.datePattern") }]}
          >
            <Input placeholder={t("entityMapping.datePlaceholder")} />
          </Form.Item>
        </Form>
      </Modal>

      <BulkImportModal
        open={bulkOpen}
        onClose={() => setBulkOpen(false)}
        onSuccess={() => void refresh()}
      />
    </div>
  );
}
