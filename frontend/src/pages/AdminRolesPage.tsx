/** 角色管理页（feat-rbac-identity Phase E）。
 *
 * 能力（需求 #2/#3/#4）：
 *  - 角色 CRUD（code 创建后不可变；code=admin 为内置超管角色，服务端限制删除）
 *  - 角色菜单授权（MenuGrantModal set-replace）
 *  - 查看任意角色当前权限（readOnly，需求 #4）
 */
import { useCallback, useEffect, useState } from "react";
import {
    Table,
    Button,
    Modal,
    Form,
    Input,
    Tag,
    message,
    Popconfirm,
    Space,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";
import {
    listRoles,
    createRole,
    updateRole,
    deleteRole,
    setRolePermissions,
    getRolePermissions,
} from "../api/roles";
import { MenuGrantModal } from "../components/rbac/MenuGrantModal";
import type {
    RoleRow,
    RoleCreatePayload,
    RoleUpdatePayload,
} from "../types/rbac";

const { TextArea } = Input;
// 同步后端 app/schemas/rbac.py::_CODE_PATTERN（命名空间风格）
const CODE_RE = /^[a-z][a-zA-Z0-9_]*(?:\.[a-z][a-zA-Z0-9_]*)*$/;

interface PermSelection {
    role: RoleRow;
    codes: string[];
    readOnly: boolean;
}

export default function AdminRolesPage(): JSX.Element {
    const { t } = useTranslation();
    const [roles, setRoles] = useState<RoleRow[]>([]);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [modalOpen, setModalOpen] = useState(false);
    const [editing, setEditing] = useState<RoleRow | null>(null);
    const [perm, setPerm] = useState<PermSelection | null>(null);
    const [permSaving, setPermSaving] = useState(false);
    const [form] = Form.useForm();

    const fetchRoles = useCallback(async () => {
        setLoading(true);
        try {
            setRoles(await listRoles());
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.loadFailed")}: ${err.message ?? String(e)}`);
        } finally {
            setLoading(false);
        }
    }, [t]);

    useEffect(() => {
        void fetchRoles();
    }, [fetchRoles]);

    const onCreate = () => {
        setEditing(null);
        form.resetFields();
        setModalOpen(true);
    };

    const onEdit = (rec: RoleRow) => {
        setEditing(rec);
        form.setFieldsValue({
            code: rec.code,
            name: rec.name,
            description: rec.description ?? undefined,
        });
        setModalOpen(true);
    };

    const onSubmit = async (values: Record<string, unknown>) => {
        setSaving(true);
        try {
            if (editing) {
                const payload: RoleUpdatePayload = {
                    name: values.name as string,
                    description: (values.description as string | undefined) || null,
                };
                await updateRole(editing.id, payload);
                message.success(t("rbac.messages.updated"));
            } else {
                const payload: RoleCreatePayload = {
                    code: values.code as string,
                    name: values.name as string,
                    description: (values.description as string | undefined) || null,
                };
                await createRole(payload);
                message.success(t("rbac.messages.created"));
            }
            setModalOpen(false);
            void fetchRoles();
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
        } finally {
            setSaving(false);
        }
    };

    const onDelete = async (rec: RoleRow) => {
        try {
            await deleteRole(rec.id);
            message.success(t("rbac.messages.deleted"));
            void fetchRoles();
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
        }
    };

    const openPerm = async (rec: RoleRow, readOnly: boolean) => {
        try {
            const perms = await getRolePermissions(rec.id);
            setPerm({ role: rec, codes: perms.menuCodes, readOnly });
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.loadFailed")}: ${err.message ?? String(e)}`);
        }
    };

    const savePerm = async (codes: string[]) => {
        if (!perm) {
            return;
        }
        setPermSaving(true);
        try {
            await setRolePermissions(perm.role.id, { menuCodes: codes });
            message.success(t("rbac.messages.granted"));
            setPerm(null);
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
        } finally {
            setPermSaving(false);
        }
    };

    const columns: ColumnsType<RoleRow> = [
        { title: t("rbac.role.code"), dataIndex: "code" },
        { title: t("rbac.role.name"), dataIndex: "name" },
        {
            title: t("rbac.role.description"),
            dataIndex: "description",
            ellipsis: true,
            render: (v: string | null) => v || "—",
        },
        {
            title: t("rbac.role.builtin"),
            dataIndex: "isBuiltin",
            render: (v: boolean) =>
                v ? (
                    <Tag color="gold">{t("rbac.role.isBuiltin")}</Tag>
                ) : (
                    <Tag>{t("rbac.role.notBuiltin")}</Tag>
                ),
        },
        {
            title: t("rbac.common.actions"),
            key: "actions",
            render: (_, rec) => (
                <Space>
                    <Button size="small" onClick={() => onEdit(rec)}>
                        {t("rbac.common.edit")}
                    </Button>
                    <Button size="small" onClick={() => void openPerm(rec, false)}>
                        {t("rbac.common.grant")}
                    </Button>
                    <Button size="small" onClick={() => void openPerm(rec, true)}>
                        {t("rbac.common.view")}
                    </Button>
                    <Popconfirm
                        title={`${t("rbac.common.confirmDelete")} ${rec.name}?`}
                        onConfirm={() => void onDelete(rec)}
                    >
                        <Button size="small" danger disabled={rec.isBuiltin}>
                            {t("rbac.common.delete")}
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
                    {t("rbac.role.create")}
                </Button>
                <Button onClick={() => void fetchRoles()}>
                    {t("rbac.common.refresh")}
                </Button>
            </Space>
            <Table
                rowKey="id"
                loading={loading}
                dataSource={roles}
                columns={columns}
                pagination={{ pageSize: 20 }}
            />

            <Modal
                open={modalOpen}
                title={
                    editing ? t("rbac.role.edit") : t("rbac.role.create")
                }
                onCancel={() => setModalOpen(false)}
                onOk={() => form.submit()}
                confirmLoading={saving}
                destroyOnHidden
            >
                <Form
                    form={form}
                    layout="vertical"
                    onFinish={(v) => void onSubmit(v)}
                >
                    <Form.Item
                        name="code"
                        label={t("rbac.role.code")}
                        rules={[{ required: true, pattern: CODE_RE }]}
                    >
                        <Input disabled={!!editing} />
                    </Form.Item>
                    <Form.Item
                        name="name"
                        label={t("rbac.role.name")}
                        rules={[{ required: true }]}
                    >
                        <Input />
                    </Form.Item>
                    <Form.Item
                        name="description"
                        label={t("rbac.role.description")}
                    >
                        <TextArea maxLength={1000} />
                    </Form.Item>
                </Form>
            </Modal>

            {perm && (
                <MenuGrantModal
                    open
                    title={
                        perm.readOnly
                            ? `${t("rbac.role.viewPerm")}: ${perm.role.name}`
                            : `${t("rbac.role.grantMenu")}: ${perm.role.name}`
                    }
                    value={perm.codes}
                    readOnly={perm.readOnly}
                    onSave={(codes) => void savePerm(codes)}
                    onCancel={() => setPerm(null)}
                    saving={permSaving}
                />
            )}
        </div>
    );
}
