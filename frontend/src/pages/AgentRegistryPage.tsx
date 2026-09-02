import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Drawer,
  Form,
  Input,
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
import { useAgentOptions } from "../hooks/useAgentOptions";
import {
  addAgentPolicy,
  createAgent,
  deleteAgentPolicy,
  deprecateAgent,
  getAgent,
  listAgentPolicies,
  listAgents,
  updateAgent,
  updateAgentPolicy,
} from "../api/agentRegistry";
import type {
  AgentAccessPolicy,
  AgentAccessPolicyCreate,
  AgentAccessPolicyUpdate,
  AgentDefinition,
  AgentDefinitionCreate,
  AgentDefinitionUpdate,
  AgentPermission,
  AgentResponseLatency,
  AgentStatus,
  AgentTriggerType,
} from "../types/agentRegistry";

const STATUSES: AgentStatus[] = ["active", "draft", "deprecated"];
const TRIGGER_TYPES: AgentTriggerType[] = ["user_question", "scheduled", "event"];
const RESPONSE_LATENCIES: AgentResponseLatency[] = ["realtime", "batch"];
const PERMISSIONS: AgentPermission[] = [
  "read",
  "masked_read",
  "forbidden",
  "forbidden_write",
];

/** status 语义色：active=绿 / draft=蓝 / deprecated=灰。 */
function statusColor(status: AgentStatus): string {
  switch (status) {
    case "active":
      return "green";
    case "draft":
      return "blue";
    case "deprecated":
    default:
      return "default";
  }
}

/** permission 语义色：read=绿 / masked_read=橙 / forbidden=红 / forbidden_write=紫。 */
function permissionColor(p: AgentPermission): string {
  switch (p) {
    case "read":
      return "green";
    case "masked_read":
      return "orange";
    case "forbidden":
      return "red";
    case "forbidden_write":
    default:
      return "purple";
  }
}

export default function AgentRegistryPage() {
  const { t } = useTranslation();
  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [loading, setLoading] = useState(false);
  const [filterStatus, setFilterStatus] = useState<AgentStatus | undefined>();
  const [filterDomains, setFilterDomains] = useState<string[]>([]);
  const [detailAgent, setDetailAgent] = useState<AgentDefinition | null>(null);
  const [detailPolicies, setDetailPolicies] = useState<AgentAccessPolicy[]>([]);
  const [editing, setEditing] = useState<AgentDefinition | null>(null);
  const [creating, setCreating] = useState(false);
  const [createForm] = Form.useForm<AgentDefinitionCreate>();
  const [editForm] = Form.useForm<AgentDefinitionUpdate>();
  const [policyForm] = Form.useForm<AgentAccessPolicyCreate>();
  const [editingPolicy, setEditingPolicy] =
    useState<AgentAccessPolicy | null>(null);

  const { domains, layers, tools } = useAgentOptions();

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const params: { status?: AgentStatus; dataDomain?: string[] } = {};
      if (filterStatus) params.status = filterStatus;
      if (filterDomains.length > 0) params.dataDomain = filterDomains;
      const list = await listAgents(params);
      setAgents(list);
    } catch (err: unknown) {
      message.error(
        t("agentRegistry.messages.listFailed") + ": " + String(err),
      );
    } finally {
      setLoading(false);
    }
  }, [filterStatus, filterDomains, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openDetail = useCallback(
    async (agent: AgentDefinition) => {
      setDetailAgent(agent);
      setEditingPolicy(null);
      policyForm.resetFields();
      try {
        const [fresh, policies] = await Promise.all([
          getAgent(agent.agentCode),
          listAgentPolicies(agent.agentCode),
        ]);
        setDetailAgent(fresh);
        setDetailPolicies(policies);
      } catch (err: unknown) {
        message.error(
          t("agentRegistry.messages.detailFailed") + ": " + String(err),
        );
      }
    },
    [policyForm, t],
  );

  const submitCreate = useCallback(async () => {
    const values = await createForm.validateFields();
    try {
      await createAgent({
        ...values,
        policies: (values.policies ?? []).map((p) => ({
          ...p,
          dataLayer: p.dataLayer ?? null,
          notes: p.notes ?? null,
        })),
      });
      message.success(t("agentRegistry.messages.created"));
      setCreating(false);
      createForm.resetFields();
      void refresh();
    } catch (err: unknown) {
      message.error(
        t("agentRegistry.messages.createFailed") + ": " + String(err),
      );
    }
  }, [createForm, refresh, t]);

  const submitEdit = useCallback(async () => {
    if (!editing) return;
    const values = await editForm.validateFields();
    try {
      await updateAgent(editing.agentCode, values);
      message.success(t("agentRegistry.messages.updated"));
      setEditing(null);
      void refresh();
    } catch (err: unknown) {
      message.error(
        t("agentRegistry.messages.updateFailed") + ": " + String(err),
      );
    }
  }, [editForm, editing, refresh, t]);

  const submitDeprecate = useCallback(
    async (agent: AgentDefinition) => {
      try {
        await deprecateAgent(agent.agentCode);
        message.success(t("agentRegistry.messages.deprecated"));
        void refresh();
      } catch (err: unknown) {
        message.error(
          t("agentRegistry.messages.deprecateFailed") + ": " + String(err),
        );
      }
    },
    [refresh, t],
  );

  const submitAddPolicy = useCallback(async () => {
    if (!detailAgent) return;
    const values = await policyForm.validateFields();
    try {
      await addAgentPolicy(detailAgent.agentCode, {
        ...values,
        dataLayer: values.dataLayer ?? null,
        notes: values.notes ?? null,
      });
      message.success(t("agentRegistry.messages.policyAdded"));
      policyForm.resetFields();
      const policies = await listAgentPolicies(detailAgent.agentCode);
      setDetailPolicies(policies);
    } catch (err: unknown) {
      message.error(
        t("agentRegistry.messages.policyAddFailed") + ": " + String(err),
      );
    }
  }, [detailAgent, policyForm, t]);

  const submitEditPolicy = useCallback(
    async (values: AgentAccessPolicyUpdate) => {
      if (!detailAgent || !editingPolicy) return;
      try {
        await updateAgentPolicy(
          detailAgent.agentCode,
          editingPolicy.id,
          values,
        );
        message.success(t("agentRegistry.messages.policyUpdated"));
        setEditingPolicy(null);
        policyForm.resetFields();
        const policies = await listAgentPolicies(detailAgent.agentCode);
        setDetailPolicies(policies);
      } catch (err: unknown) {
        message.error(
          t("agentRegistry.messages.policyUpdateFailed") + ": " + String(err),
        );
      }
    },
    [detailAgent, editingPolicy, policyForm, t],
  );

  const submitDeletePolicy = useCallback(
    async (policyId: number) => {
      if (!detailAgent) return;
      try {
        await deleteAgentPolicy(detailAgent.agentCode, policyId);
        message.success(t("agentRegistry.messages.policyDeleted"));
        const policies = await listAgentPolicies(detailAgent.agentCode);
        setDetailPolicies(policies);
      } catch (err: unknown) {
        message.error(
          t("agentRegistry.messages.policyDeleteFailed") + ": " + String(err),
        );
      }
    },
    [detailAgent, t],
  );

  const columns: ColumnsType<AgentDefinition> = [
    {
      title: t("agentRegistry.columns.code"),
      dataIndex: "agentCode",
      key: "agentCode",
      width: 200,
      render: (code: string, row) => (
        <a onClick={() => void openDetail(row)}>{code}</a>
      ),
    },
    {
      title: t("agentRegistry.columns.name"),
      dataIndex: "agentName",
      key: "agentName",
    },
    {
      title: t("agentRegistry.columns.status"),
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (status: AgentStatus) => (
        <Tag color={statusColor(status)}>{status}</Tag>
      ),
    },
    {
      title: t("agentRegistry.columns.triggerType"),
      dataIndex: "triggerType",
      key: "triggerType",
      width: 140,
    },
    {
      title: t("agentRegistry.columns.responseLatency"),
      dataIndex: "responseLatency",
      key: "responseLatency",
      width: 140,
    },
    {
      title: t("agentRegistry.columns.dataDomains"),
      dataIndex: "dataDomains",
      key: "dataDomains",
      render: (domains: string[]) =>
        domains.length > 0 ? (
          <Space size={4} wrap>
            {domains.map((d) => (
              <Tag key={d}>{d}</Tag>
            ))}
          </Space>
        ) : (
          <span style={{ color: "#999" }}>—</span>
        ),
    },
    {
      title: t("agentRegistry.columns.owner"),
      dataIndex: "owner",
      key: "owner",
      width: 120,
      render: (owner: string | null) => owner ?? <Tag>unassigned</Tag>,
    },
    {
      title: t("agentRegistry.columns.version"),
      dataIndex: "version",
      key: "version",
      width: 90,
    },
    {
      title: t("agentRegistry.columns.actions"),
      key: "actions",
      width: 200,
      render: (_: unknown, row) => (
        <Space size="small">
          <Button
            size="small"
            onClick={() => {
              setEditing(row);
              editForm.setFieldsValue({
                agentName: row.agentName,
                description: row.description,
                triggerType: row.triggerType,
                responseLatency: row.responseLatency,
                dataDomains: row.dataDomains,
                dataLayers: row.dataLayers,
                status: row.status,
                version: row.version,
                toolName: row.toolName,
              });
            }}
          >
            {t("agentRegistry.actions.edit")}
          </Button>
          {row.status !== "deprecated" && (
            <Popconfirm
              title={t("agentRegistry.confirmDeprecate")}
              onConfirm={() => void submitDeprecate(row)}
            >
              <Button size="small" danger>
                {t("agentRegistry.actions.deprecate")}
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Select
          allowClear
          placeholder={t("agentRegistry.filter.statusPlaceholder")}
          style={{ width: 180 }}
          value={filterStatus}
          onChange={(v) => setFilterStatus(v)}
        >
          {STATUSES.map((s) => (
            <Select.Option key={s} value={s}>
              {s}
            </Select.Option>
          ))}
        </Select>
        <Select
          mode="multiple"
          allowClear
          placeholder={t("agentRegistry.filter.domainPlaceholder")}
          style={{ width: 240 }}
          value={filterDomains}
          onChange={(v) => setFilterDomains(v)}
          options={domains.map((d) => ({ value: d, label: d }))}
        />
        <Button
          type="primary"
          onClick={() => {
            setCreating(true);
            createForm.resetFields();
          }}
        >
          {t("agentRegistry.actions.create")}
        </Button>
        <Button onClick={() => void refresh()}>{t("agentRegistry.actions.refresh")}</Button>
      </Space>

      <Table
        rowKey="agentCode"
        loading={loading}
        columns={columns}
        dataSource={agents}
        pagination={{ pageSize: 20 }}
      />

      {/* 创建对话框 */}
      <Modal
        open={creating}
        title={t("agentRegistry.createModal.title")}
        onCancel={() => setCreating(false)}
        onOk={() => void submitCreate()}
        okText={t("agentRegistry.createModal.ok")}
        cancelText={t("agentRegistry.createModal.cancel")}
        width={720}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="agentCode"
            label={t("agentRegistry.fields.agentCode")}
            rules={[
              { required: true, pattern: /^[A-Z][A-Z0-9_]*$/, message: t("agentRegistry.fields.agentCodeRule") },
            ]}
          >
            <Input placeholder="SUPPLIER_RISK_AGENT" />
          </Form.Item>
          <Form.Item
            name="agentName"
            label={t("agentRegistry.fields.agentName")}
            rules={[{ required: true }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="description" label={t("agentRegistry.fields.description")}>
            <Input.TextArea rows={3} />
          </Form.Item>
          <Space>
            <Form.Item
              name="triggerType"
              label={t("agentRegistry.fields.triggerType")}
              initialValue="user_question"
            >
              <Select style={{ width: 180 }}>
                {TRIGGER_TYPES.map((x) => (
                  <Select.Option key={x} value={x}>
                    {x}
                  </Select.Option>
                ))}
              </Select>
            </Form.Item>
            <Form.Item
              name="responseLatency"
              label={t("agentRegistry.fields.responseLatency")}
              initialValue="realtime"
            >
              <Select style={{ width: 180 }}>
                {RESPONSE_LATENCIES.map((x) => (
                  <Select.Option key={x} value={x}>
                    {x}
                  </Select.Option>
                ))}
              </Select>
            </Form.Item>
            <Form.Item
              name="status"
              label={t("agentRegistry.fields.status")}
              initialValue="draft"
            >
              <Select style={{ width: 180 }}>
                {STATUSES.map((x) => (
                  <Select.Option key={x} value={x}>
                    {x}
                  </Select.Option>
                ))}
              </Select>
            </Form.Item>
          </Space>
          <Form.Item name="dataDomains" label={t("agentRegistry.fields.dataDomains")}>
            <Select
              mode="multiple"
              options={domains.map((d) => ({ value: d, label: d }))}
              placeholder={t("agentRegistry.filter.domainPlaceholder")}
            />
          </Form.Item>
          <Form.Item name="dataLayers" label={t("agentRegistry.fields.dataLayers")}>
            <Select
              mode="multiple"
              options={layers.map((l) => ({ value: l, label: l }))}
              placeholder={t("agentRegistry.filter.layerPlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="toolName"
            label={t("agentRegistry.fields.toolName")}
            rules={[
              {
                validator: (_: unknown, value: string | undefined) => {
                  if (!value) return Promise.resolve();
                  const tool = tools.find((x) => x.name === value);
                  if (!tool) {
                    return Promise.reject(
                      new Error(t("agentRegistry.errors.toolUnknown")),
                    );
                  }
                  const covered = createForm.getFieldValue("dataLayers") || [];
                  const missing = tool.dataLayers.filter(
                    (layer) => !covered.includes(layer),
                  );
                  if (missing.length > 0) {
                    return Promise.reject(
                      new Error(
                        t("agentRegistry.errors.toolLayerMismatch", {
                          tool: value,
                          missing: missing.join(","),
                        }),
                      ),
                    );
                  }
                  return Promise.resolve();
                },
              },
            ]}
          >
            <Select
              allowClear
              placeholder={t("agentRegistry.fields.toolNamePlaceholder")}
              options={tools.map((tool) => ({
                value: tool.name,
                label: `${tool.name} — ${tool.description}`,
              }))}
              showSearch
              optionFilterProp="label"
              onChange={() => createForm.validateFields(["dataLayers"])}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑对话框 */}
      <Modal
        open={!!editing}
        title={editing ? t("agentRegistry.editModal.title", { code: editing.agentCode }) : ""}
        onCancel={() => setEditing(null)}
        onOk={() => void submitEdit()}
        okText={t("agentRegistry.editModal.ok")}
        cancelText={t("agentRegistry.editModal.cancel")}
      >
        <Form form={editForm} layout="vertical">
          <Form.Item name="agentName" label={t("agentRegistry.fields.agentName")}>
            <Input />
          </Form.Item>
          <Form.Item name="description" label={t("agentRegistry.fields.description")}>
            <Input.TextArea rows={3} />
          </Form.Item>
          <Form.Item name="dataDomains" label={t("agentRegistry.fields.dataDomains")}>
            <Select
              mode="multiple"
              options={domains.map((d) => ({ value: d, label: d }))}
            />
          </Form.Item>
          <Form.Item name="dataLayers" label={t("agentRegistry.fields.dataLayers")}>
            <Select
              mode="multiple"
              options={layers.map((l) => ({ value: l, label: l }))}
            />
          </Form.Item>
          <Form.Item
            name="toolName"
            label={t("agentRegistry.fields.toolName")}
            rules={[
              {
                validator: (_: unknown, value: string | undefined) => {
                  if (!value) return Promise.resolve();
                  const tool = tools.find((x) => x.name === value);
                  if (!tool) {
                    return Promise.reject(
                      new Error(t("agentRegistry.errors.toolUnknown")),
                    );
                  }
                  const covered = editForm.getFieldValue("dataLayers") || [];
                  const missing = tool.dataLayers.filter(
                    (layer) => !covered.includes(layer),
                  );
                  if (missing.length > 0) {
                    return Promise.reject(
                      new Error(
                        t("agentRegistry.errors.toolLayerMismatch", {
                          tool: value,
                          missing: missing.join(","),
                        }),
                      ),
                    );
                  }
                  return Promise.resolve();
                },
              },
            ]}
          >
            <Select
              allowClear
              placeholder={t("agentRegistry.fields.toolNamePlaceholder")}
              options={tools.map((tool) => ({
                value: tool.name,
                label: `${tool.name} — ${tool.description}`,
              }))}
              showSearch
              optionFilterProp="label"
              onChange={() => editForm.validateFields(["dataLayers"])}
            />
          </Form.Item>
          <Form.Item name="status" label={t("agentRegistry.fields.status")}>
            <Select>
              {STATUSES.map((x) => (
                <Select.Option key={x} value={x}>
                  {x}
                </Select.Option>
              ))}
            </Select>
          </Form.Item>
          <Form.Item name="version" label={t("agentRegistry.fields.version")}>
            <Input />
          </Form.Item>
        </Form>
      </Modal>

      {/* 详情抽屉（含 policies 子表） */}
      <Drawer
        open={!!detailAgent}
        onClose={() => setDetailAgent(null)}
        title={detailAgent ? detailAgent.agentName : ""}
        width={760}
      >
        {detailAgent && (
          <Space direction="vertical" style={{ width: "100%" }} size="middle">
            <div>
              <strong>{t("agentRegistry.detail.code")}:</strong>{" "}
              {detailAgent.agentCode} ·{" "}
              <Tag color={statusColor(detailAgent.status)}>
                {detailAgent.status}
              </Tag>{" "}
              <Tag>v{detailAgent.version}</Tag>
            </div>
            <div>
              <strong>{t("agentRegistry.detail.description")}:</strong>{" "}
              {detailAgent.description ?? "—"}
            </div>
            <div>
              <strong>{t("agentRegistry.detail.triggerType")}:</strong>{" "}
              {detailAgent.triggerType} ·{" "}
              <strong>{t("agentRegistry.detail.responseLatency")}:</strong>{" "}
              {detailAgent.responseLatency}
            </div>
            <div>
              <strong>{t("agentRegistry.detail.dataDomains")}:</strong>{" "}
              {detailAgent.dataDomains.join(", ") || "—"} ·{" "}
              <strong>{t("agentRegistry.detail.dataLayers")}:</strong>{" "}
              {detailAgent.dataLayers.join(", ") || "—"}
            </div>
            <div>
              <strong>{t("agentRegistry.detail.owner")}:</strong>{" "}
              {detailAgent.owner ?? "unassigned"}
            </div>
            {detailAgent.toolName && (
              <div>
                <strong>{t("agentRegistry.fields.toolName")}:</strong>{" "}
                <Tag color="blue">{detailAgent.toolName}</Tag>
              </div>
            )}

            <div>
              <h4>{t("agentRegistry.policies.title")}</h4>
              <Form
                form={policyForm}
                layout="inline"
                style={{ marginBottom: 12 }}
              >
                <Form.Item
                  name="dataObject"
                  rules={[{ required: true }]}
                  style={{ width: 180 }}
                >
                  <Input placeholder={t("agentRegistry.policies.dataObjectPlaceholder")} />
                </Form.Item>
                <Form.Item
                  name="permission"
                  initialValue="read"
                  style={{ width: 140 }}
                >
                  <Select>
                    {PERMISSIONS.map((p) => (
                      <Select.Option key={p} value={p}>
                        {p}
                      </Select.Option>
                    ))}
                  </Select>
                </Form.Item>
                <Form.Item name="dataLayer" style={{ width: 140 }}>
                  <Select
                    allowClear
                    options={layers.map((l) => ({ value: l, label: l }))}
                    placeholder={t("agentRegistry.policies.dataLayerPlaceholder")}
                  />
                </Form.Item>
                <Form.Item name="notes" style={{ width: 200 }}>
                  <Input placeholder={t("agentRegistry.policies.notesPlaceholder")} />
                </Form.Item>
                <Form.Item>
                  {editingPolicy ? (
                    <Space>
                      <Button
                        type="primary"
                        onClick={() =>
                          void policyForm
                            .validateFields()
                            .then((values) =>
                              void submitEditPolicy(values as AgentAccessPolicyUpdate),
                            )
                        }
                      >
                        {t("agentRegistry.policies.saveEdit")}
                      </Button>
                      <Button
                        onClick={() => {
                          setEditingPolicy(null);
                          policyForm.resetFields();
                        }}
                      >
                        {t("agentRegistry.policies.cancelEdit")}
                      </Button>
                    </Space>
                  ) : (
                    <Button type="primary" onClick={() => void submitAddPolicy()}>
                      {t("agentRegistry.policies.add")}
                    </Button>
                  )}
                </Form.Item>
              </Form>

              <Table
                size="small"
                rowKey="id"
                dataSource={detailPolicies}
                pagination={false}
                columns={[
                  {
                    title: t("agentRegistry.policies.dataObject"),
                    dataIndex: "dataObject",
                  },
                  {
                    title: t("agentRegistry.policies.permission"),
                    dataIndex: "permission",
                    render: (p: AgentPermission) => (
                      <Tag color={permissionColor(p)}>{p}</Tag>
                    ),
                  },
                  {
                    title: t("agentRegistry.policies.dataLayer"),
                    dataIndex: "dataLayer",
                    render: (l: string | null) => l ?? "—",
                  },
                  {
                    title: t("agentRegistry.policies.notes"),
                    dataIndex: "notes",
                    render: (n: string | null) => n ?? "—",
                  },
                  {
                    title: t("agentRegistry.columns.actions"),
                    key: "policyActions",
                    width: 160,
                    render: (_: unknown, row) => (
                      <Space size="small">
                        <Button
                          size="small"
                          onClick={() => {
                            setEditingPolicy(row);
                            policyForm.setFieldsValue({
                              dataObject: row.dataObject,
                              permission: row.permission,
                              dataLayer: row.dataLayer,
                              notes: row.notes,
                            });
                          }}
                        >
                          {t("agentRegistry.actions.edit")}
                        </Button>
                        <Popconfirm
                          title={t("agentRegistry.confirmDeletePolicy")}
                          onConfirm={() => void submitDeletePolicy(row.id)}
                        >
                          <Button size="small" danger>
                            {t("agentRegistry.actions.delete")}
                          </Button>
                        </Popconfirm>
                      </Space>
                    ),
                  },
                ]}
              />
            </div>
          </Space>
        )}
      </Drawer>
    </div>
  );
}