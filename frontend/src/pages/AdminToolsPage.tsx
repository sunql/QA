/** AgentToolConfig 管理页（feat-agent-tool-config-db, T15）。
 *
 * 顶部：新建 / 刷新 按钮。
 * 主体：Table 列出所有 AgentToolConfig，列含 name / description / dataObject /
 *       dataLayers / handlerKind / handlerRef / enabled(Switch) / updatedTime / 操作。
 * Modal：创建 + 编辑（name 创建后不可改）。
 * 行操作：编辑按钮 + Popconfirm 删除 + Switch 翻转 enabled。
 *
 * 错误处理（统一 message.error）：
 * - 409 + referencingAgents → agentTools.errors.inUseByAgent
 * - 409（其他）              → agentTools.errors.versionConflict
 * - 其他                      → agentTools.messages.failed
 */
import { useEffect, useState, useCallback } from "react";
import {
    Table,
    Button,
    Modal,
    Form,
    Input,
    Select,
    Switch,
    Tag,
    message,
    Popconfirm,
    Space,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";
import {
    listAgentTools,
    createAgentTool,
    updateAgentTool,
    deleteAgentTool,
    toggleAgentTool,
} from "../api/agentTools";
import type {
    AgentToolConfig,
    AgentToolConfigCreate,
    AgentToolConfigUpdate,
} from "../types/agentTool";

const { TextArea } = Input;

const DATA_LAYER_OPTIONS = ["DIM", "DWD", "FEATURE"].map((v) => ({ value: v }));
const HANDLER_KIND_OPTIONS = ["BUILTIN", "NL2SQL"].map((v) => ({ value: v }));

export default function AdminToolsPage(): JSX.Element {
    const { t } = useTranslation();
    const [tools, setTools] = useState<AgentToolConfig[]>([]);
    const [loading, setLoading] = useState(false);
    const [modalOpen, setModalOpen] = useState(false);
    const [editing, setEditing] = useState<AgentToolConfig | null>(null);
    const [form] = Form.useForm();

    const fetchList = useCallback(async () => {
        setLoading(true);
        try {
            setTools(await listAgentTools());
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(
                t("agentTools.messages.failed") + ": " + (err.message ?? String(e)),
            );
        } finally {
            setLoading(false);
        }
    }, [t]);

    useEffect(() => {
        void fetchList();
    }, [fetchList]);

    const onCreate = () => {
        setEditing(null);
        form.resetFields();
        setModalOpen(true);
    };

    const onEdit = (rec: AgentToolConfig) => {
        setEditing(rec);
        form.setFieldsValue(rec);
        setModalOpen(true);
    };

    const onSubmit = async (
        values: AgentToolConfigCreate | AgentToolConfigUpdate,
    ) => {
        try {
            if (editing) {
                await updateAgentTool(editing.name, {
                    ...values,
                    version: editing.version,
                });
                message.success(t("agentTools.messages.updated"));
            } else {
                await createAgentTool(values as AgentToolConfigCreate);
                message.success(t("agentTools.messages.created"));
            }
            setModalOpen(false);
            void fetchList();
        } catch (e) {
            const err = e as Error & {
                status?: number;
                detail?: { referencingAgents?: string[] } | string;
            };
            if (
                err.status === 409 &&
                typeof err.detail === "object" &&
                err.detail?.referencingAgents
            ) {
                message.error(
                    t("agentTools.errors.inUseByAgent", {
                        agentCodes: err.detail.referencingAgents.join(", "),
                    }),
                );
            } else if (err.status === 409) {
                message.error(t("agentTools.errors.versionConflict"));
            } else {
                message.error(
                    t("agentTools.messages.failed") + ": " + (err.message ?? ""),
                );
            }
        }
    };

    const onDelete = async (name: string) => {
        try {
            await deleteAgentTool(name);
            message.success(t("agentTools.messages.deleted"));
            void fetchList();
        } catch (e) {
            const err = e as Error & {
                status?: number;
                detail?: { referencingAgents?: string[] } | string;
            };
            if (
                err.status === 409 &&
                typeof err.detail === "object" &&
                err.detail?.referencingAgents
            ) {
                message.error(
                    t("agentTools.errors.inUseByAgent", {
                        agentCodes: err.detail.referencingAgents.join(", "),
                    }),
                );
            } else {
                message.error(t("agentTools.messages.failed"));
            }
        }
    };

    const onToggle = async (rec: AgentToolConfig, enabled: boolean) => {
        try {
            await toggleAgentTool(rec.name, enabled);
            message.success(t("agentTools.messages.toggled"));
            void fetchList();
        } catch {
            message.error(t("agentTools.messages.failed"));
        }
    };

    const columns: ColumnsType<AgentToolConfig> = [
        { title: t("agentTools.columns.name"), dataIndex: "name" },
        {
            title: t("agentTools.columns.description"),
            dataIndex: "description",
            ellipsis: true,
            render: (v: string | null) => v || "—",
        },
        {
            title: t("agentTools.columns.dataObject"),
            dataIndex: "dataObject",
            render: (v: string) => <Tag color="blue">{v}</Tag>,
        },
        {
            title: t("agentTools.columns.dataLayers"),
            dataIndex: "dataLayers",
            render: (vs: string[]) =>
                vs.slice(0, 3).map((l) => <Tag key={l}>{l}</Tag>),
        },
        {
            title: t("agentTools.columns.handlerKind"),
            dataIndex: "handlerKind",
            render: (k: string) => (
                <Tag color={k === "BUILTIN" ? "green" : "orange"}>{k}</Tag>
            ),
        },
        {
            title: t("agentTools.columns.handlerRef"),
            dataIndex: "handlerRef",
        },
        {
            title: t("agentTools.columns.enabled"),
            dataIndex: "enabled",
            render: (v: boolean, rec: AgentToolConfig) => (
                <Switch
                    checked={v}
                    onChange={(checked) => void onToggle(rec, checked)}
                />
            ),
        },
        {
            title: t("agentTools.columns.updatedTime"),
            dataIndex: "updatedTime",
            render: (v: string | null) => v || "—",
        },
        {
            title: "Actions",
            key: "actions",
            render: (_, rec) => (
                <Space>
                    <Button size="small" onClick={() => onEdit(rec)}>
                        {t("agentTools.actions.edit")}
                    </Button>
                    <Popconfirm
                        title={t("agentTools.actions.delete") + " " + rec.name + "?"}
                        onConfirm={() => void onDelete(rec.name)}
                    >
                        <Button size="small" danger>
                            {t("agentTools.actions.delete")}
                        </Button>
                    </Popconfirm>
                </Space>
            ),
        },
    ];

    return (
        <div style={{ padding: 24 }}>
            <Space style={{ marginBottom: 16 }}>
                <Button type="primary" onClick={onCreate}>
                    {t("agentTools.actions.create")}
                </Button>
                <Button onClick={() => void fetchList()}>
                    {t("agentTools.actions.refresh")}
                </Button>
            </Space>
            <Table
                rowKey="id"
                loading={loading}
                dataSource={tools}
                columns={columns}
                pagination={{ pageSize: 20 }}
            />

            <Modal
                open={modalOpen}
                title={
                    editing
                        ? t("agentTools.actions.edit")
                        : t("agentTools.actions.create")
                }
                onCancel={() => setModalOpen(false)}
                onOk={() => form.submit()}
                destroyOnHidden
            >
                <Form
                    form={form}
                    layout="vertical"
                    onFinish={(v) => void onSubmit(v)}
                >
                    <Form.Item
                        name="name"
                        label={t("agentTools.form.name")}
                        rules={[
                            { required: true, pattern: /^[a-z][a-z0-9_]*$/ },
                        ]}
                    >
                        <Input disabled={!!editing} />
                    </Form.Item>
                    <Form.Item
                        name="description"
                        label={t("agentTools.form.description")}
                    >
                        <TextArea maxLength={2000} />
                    </Form.Item>
                    <Form.Item
                        name="dataObject"
                        label={t("agentTools.form.dataObject")}
                        rules={[{ required: true }]}
                    >
                        <Input />
                    </Form.Item>
                    <Form.Item
                        name="dataLayers"
                        label={t("agentTools.form.dataLayers")}
                    >
                        <Select
                            mode="multiple"
                            options={DATA_LAYER_OPTIONS}
                        />
                    </Form.Item>
                    <Form.Item
                        name="inputSchema"
                        label={t("agentTools.form.inputSchema")}
                    >
                        <TextArea placeholder='{"type":"object"}' />
                    </Form.Item>
                    <Form.Item
                        name="handlerKind"
                        label={t("agentTools.form.handlerKind")}
                        rules={[{ required: true }]}
                    >
                        <Select options={HANDLER_KIND_OPTIONS} />
                    </Form.Item>
                    <Form.Item
                        name="handlerRef"
                        label={t("agentTools.form.handlerRef")}
                        rules={[{ required: true }]}
                    >
                        <Input />
                    </Form.Item>
                    <Form.Item
                        name="argExtractorKind"
                        label={t("agentTools.form.argExtractorKind")}
                        initialValue="supplier_key"
                    >
                        <Input />
                    </Form.Item>
                </Form>
            </Modal>
        </div>
    );
}