import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Form,
  Input,
  message,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  TreeSelect,
} from "antd";
import { useTranslation } from "react-i18next";
import { useTablePagination } from "../utils/useTablePagination";
import type {
  DataDomain,
  DataDomainCreate,
  DataDomainUpdate,
} from "../types/dataDomain";
import {
  createDataDomain,
  deleteDataDomain,
  listDataDomains,
  updateDataDomain,
} from "../api/dataDomain";
import { listOrganizationTree } from "../api/organizations";
import type { OrganizationTreeNode } from "../types/rbac";

// 与后端 _normalizeDataDomainCode 一致：大写字母 / 数字 / 下划线，1-20 字符
const CODE_PATTERN = /^[A-Z0-9_]+$/;

interface OrgSelectNode {
  value: number;
  title: string;
  children?: OrgSelectNode[];
}

function toOrgTreeOptions(
  nodes: OrganizationTreeNode[],
): OrgSelectNode[] {
  return nodes.map((n) => ({
    value: n.id,
    title: n.name,
    children: n.children?.length ? toOrgTreeOptions(n.children) : undefined,
  }));
}

export default function DataDomainPage() {
  const { t } = useTranslation();
  const { pagination } = useTablePagination();
  const [rows, setRows] = useState<DataDomain[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<DataDomain | null>(null);
  const [orgTree, setOrgTree] = useState<OrganizationTreeNode[]>([]);
  const [form] = Form.useForm();

  const orgOptions = useMemo(() => toOrgTreeOptions(orgTree), [orgTree]);

  const load = async () => {
    setLoading(true);
    try {
      setRows(await listDataDomains());
    } finally {
      setLoading(false);
    }
  };

  const loadOrgTree = async () => {
    try {
      setOrgTree(await listOrganizationTree());
    } catch {
      // 组织树加载失败不阻断页面：主管理组织保持可选（后端允许 NULL）
      setOrgTree([]);
    }
  };

  useEffect(() => {
    void load();
    void loadOrgTree();
  }, []);

  const onCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ enabled: true });
    setModalOpen(true);
  };

  const onEdit = (row: DataDomain) => {
    setEditing(row);
    form.setFieldsValue({
      code: row.code,
      name: row.name,
      primaryOrgId: row.primaryOrgId ?? undefined,
      description: row.description ?? undefined,
      enabled: row.enabled,
    });
    setModalOpen(true);
  };

  const onSubmit = async () => {
    let values;
    try {
      values = await form.validateFields();
    } catch {
      // antd validateFields 在校验失败时 reject；错误提示已由表单展示
      return;
    }
    try {
      if (editing) {
        const payload: DataDomainUpdate = {
          name: values.name,
          primaryOrgId: values.primaryOrgId ?? null,
          description: values.description ?? null,
          enabled: values.enabled,
        };
        await updateDataDomain(editing.code, payload);
      } else {
        const payload: DataDomainCreate = {
          code: values.code,
          name: values.name,
          primaryOrgId: values.primaryOrgId ?? null,
          description: values.description ?? null,
          enabled: values.enabled,
        };
        await createDataDomain(payload);
      }
      setModalOpen(false);
      void load();
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error);
      void message.error(t("dataDomain.messages.submitFailed", { detail }));
    }
  };

  const onDelete = async (code: string) => {
    try {
      await deleteDataDomain(code);
      void load();
    } catch (error: unknown) {
      // 被业务对象 / Agent 引用时后端 409：提示用户先解绑
      const detail = error instanceof Error ? error.message : String(error);
      void message.error(t("dataDomain.messages.deleteFailed", { detail }));
    }
  };

  const onToggleEnabled = async (row: DataDomain, enabled: boolean) => {
    try {
      await updateDataDomain(row.code, { enabled });
      void load();
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error);
      void message.error(t("dataDomain.messages.submitFailed", { detail }));
    }
  };

  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ marginBottom: 16 }}>{t("dataDomain.title")}</h2>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" onClick={onCreate}>
          {t("dataDomain.newButton")}
        </Button>
      </Space>
      <Table
        loading={loading}
        dataSource={rows}
        pagination={pagination}
        rowKey="code"
        columns={[
          { title: t("dataDomain.columns.code"), dataIndex: "code" },
          { title: t("dataDomain.columns.name"), dataIndex: "name" },
          {
            title: t("dataDomain.columns.primaryOrg"),
            dataIndex: "primaryOrgName",
            render: (v: string | null) => (v ? <Tag>{v}</Tag> : "—"),
          },
          { title: t("dataDomain.columns.description"), dataIndex: "description" },
          {
            title: t("dataDomain.columns.enabled"),
            dataIndex: "enabled",
            render: (v: boolean, row: DataDomain) => (
              <Switch
                checked={v}
                onChange={(checked) => void onToggleEnabled(row, checked)}
              />
            ),
          },
          {
            title: "",
            key: "actions",
            render: (_, row) => (
              <Space>
                <Button size="small" onClick={() => onEdit(row)}>
                  {t("common.edit")}
                </Button>
                <Popconfirm
                  title={t("dataDomain.modal.confirmDelete", { code: row.code })}
                  onConfirm={() => void onDelete(row.code)}
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
        title={editing ? t("dataDomain.modal.editTitle") : t("dataDomain.modal.createTitle")}
        onCancel={() => setModalOpen(false)}
        onOk={onSubmit}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="code"
            label={t("dataDomain.columns.code")}
            rules={[
              { required: true },
              { pattern: CODE_PATTERN, message: t("dataDomain.validation.codeFormat") },
              { max: 20 },
            ]}
          >
            {editing ? (
              <Input disabled />
            ) : (
              <Input
                placeholder={t("dataDomain.placeholders.codeNew")}
                maxLength={20}
              />
            )}
          </Form.Item>
          <Form.Item name="name" label={t("dataDomain.columns.name")} rules={[{ required: true }]}>
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item
            name="primaryOrgId"
            label={t("dataDomain.columns.primaryOrg")}
            tooltip={t("dataDomain.tooltips.primaryOrg")}
          >
            <TreeSelect
              allowClear
              treeData={orgOptions}
              placeholder={t("dataDomain.placeholders.selectOrg")}
              treeDefaultExpandAll
            />
          </Form.Item>
          <Form.Item name="description" label={t("dataDomain.columns.description")}>
            <Input.TextArea rows={3} maxLength={2000} />
          </Form.Item>
          <Form.Item
            name="enabled"
            label={t("dataDomain.columns.enabled")}
            valuePropName="checked"
            tooltip={t("dataDomain.tooltips.enabled")}
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
