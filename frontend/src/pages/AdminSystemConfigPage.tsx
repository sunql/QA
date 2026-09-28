/** system_config 管理页（feat-system-config-admin）。
 *
 * 列出所有 system_config 行（key / value / description / updatedTime），允许编辑 value。
 * key/description 是元数据，只读（修改需要 DDL 同步，不在 admin UI 范围）。
 *
 * 后端：
 *   GET /api/v1/admin/system-config                 全量列表（按 key 升序）
 *   PUT /api/v1/admin/system-config/{key}           更新 value（admin only）
 *
 * 错误处理：message.error 统一提示，复用 AdminToolsPage 的形态。
 */

import { useCallback, useEffect, useState } from "react";
import {
    Button,
    Form,
    Input,
    Modal,
    Space,
    Table,
    Tag,
    message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";

import {
    listSystemConfig,
    updateSystemConfig,
} from "../api/systemConfig";
import type { SystemConfig, SystemConfigUpdate } from "../api/systemConfig";

export default function AdminSystemConfigPage(): JSX.Element {
    const { t } = useTranslation();
    const [rows, setRows] = useState<SystemConfig[]>([]);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [modalOpen, setModalOpen] = useState(false);
    const [editing, setEditing] = useState<SystemConfig | null>(null);
    const [form] = Form.useForm<{ value: string }>();

    const fetchList = useCallback(async () => {
        setLoading(true);
        try {
            setRows(await listSystemConfig());
        } catch (e) {
            const err = e as Error;
            message.error(
                t("systemConfig.errors.loadFailed") + ": " +
                    (err.message ?? String(e)),
            );
        } finally {
            setLoading(false);
        }
    }, [t]);

    useEffect(() => {
        void fetchList();
    }, [fetchList]);

    const onEdit = (rec: SystemConfig) => {
        setEditing(rec);
        form.setFieldsValue({ value: rec.value ?? "" });
        setModalOpen(true);
    };

    const onSubmit = async (values: { value: string }) => {
        if (!editing) return;
        setSaving(true);
        try {
            const payload: SystemConfigUpdate = {
                // 空串 → null（允许业务表达"未配置/清空"）
                value: values.value === "" ? null : values.value,
            };
            await updateSystemConfig(editing.key, payload);
            message.success(t("systemConfig.messages.updated"));
            setModalOpen(false);
            void fetchList();
        } catch (e) {
            const err = e as Error;
            message.error(
                t("systemConfig.errors.updateFailed") + ": " +
                    (err.message ?? String(e)),
            );
        } finally {
            setSaving(false);
        }
    };

    const columns: ColumnsType<SystemConfig> = [
        {
            title: t("systemConfig.columns.key"),
            dataIndex: "key",
            render: (k: string) => <Tag color="blue">{k}</Tag>,
        },
        {
            title: t("systemConfig.columns.value"),
            dataIndex: "value",
            ellipsis: true,
            render: (v: string | null) =>
                v === null ? (
                    <span style={{ color: "#999" }}>（{t("systemConfig.empty")}）</span>
                ) : (
                    v
                ),
        },
        {
            title: t("systemConfig.columns.description"),
            dataIndex: "description",
            ellipsis: true,
            render: (v: string | null) => v || "—",
        },
        {
            title: t("systemConfig.columns.updatedTime"),
            dataIndex: "updatedTime",
            render: (v: string | null) => v || "—",
        },
        {
            title: t("systemConfig.columns.actions"),
            key: "actions",
            render: (_, rec) => (
                <Button size="small" onClick={() => onEdit(rec)}>
                    {t("systemConfig.actions.edit")}
                </Button>
            ),
        },
    ];

    return (
        <div style={{ padding: 24 }}>
            <Space style={{ marginBottom: 16 }}>
                <Button onClick={() => void fetchList()}>
                    {t("systemConfig.actions.refresh")}
                </Button>
            </Space>
            <Table
                rowKey="key"
                loading={loading}
                dataSource={rows}
                columns={columns}
                pagination={{ pageSize: 20, hideOnSinglePage: true }}
            />
            <Modal
                title={
                    editing
                        ? `${t("systemConfig.modal.editTitle")}: ${editing.key}`
                        : t("systemConfig.modal.editTitle")
                }
                open={modalOpen}
                confirmLoading={saving}
                onCancel={() => setModalOpen(false)}
                onOk={() => form.submit()}
                okText={t("systemConfig.actions.save")}
                cancelText={t("systemConfig.actions.cancel")}
                destroyOnHidden
            >
                {editing && (
                    <>
                        <Form
                            form={form}
                            layout="vertical"
                            onFinish={onSubmit}
                            preserve={false}
                        >
                            <Form.Item
                                label={t("systemConfig.columns.key")}
                                help={t("systemConfig.modal.keyReadonlyHelp")}
                            >
                                <Input value={editing.key} disabled />
                            </Form.Item>
                            <Form.Item
                                label={t("systemConfig.columns.description")}
                            >
                                <Input.TextArea
                                    value={editing.description ?? ""}
                                    disabled
                                    autoSize={{ minRows: 1, maxRows: 3 }}
                                />
                            </Form.Item>
                            <Form.Item
                                name="value"
                                label={t("systemConfig.columns.value")}
                                rules={[
                                    {
                                        max: 4096,
                                        message: t("systemConfig.errors.valueTooLong"),
                                    },
                                ]}
                            >
                                <Input.TextArea
                                    autoSize={{ minRows: 2, maxRows: 8 }}
                                    placeholder={t("systemConfig.modal.valuePlaceholder")}
                                />
                            </Form.Item>
                        </Form>
                    </>
                )}
            </Modal>
        </div>
    );
}