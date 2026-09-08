/** 用户管理页（feat-rbac-identity Phase E）。
 *
 * 能力（需求 #1/#2/#3/#4）：
 *  - 用户 CRUD（username 创建后不可变）
 *  - 授予角色 + 分配组织（set-replace PUT /users/{id}/roles|organizations）
 *  - 直接菜单授权（MenuGrantModal set-replace）
 *  - 查看任意用户有效权限 Drawer：角色/组织来源 + 三来源合集拆分（#4）
 *
 * 全部走 httpClient（X-User-Id: system → stub admin 兜底），admin-only 后端放行。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
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
    Drawer,
    Descriptions,
    Alert,
    Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";
import {
    listUsers,
    createUser,
    updateUser,
    deleteUser,
    setUserRoles,
    setUserOrganizations,
    setUserPermissions,
    getUserEffectivePermissions,
} from "../api/users";
import { listRoles } from "../api/roles";
import { listOrganizations } from "../api/organizations";
import { listMenuRows } from "../api/menuConfig";
import { MenuGrantModal, buildMenuLabelMap } from "../components/rbac/MenuGrantModal";
import type {
    UserRow,
    UserEffectivePermissions,
    UserCreatePayload,
    UserUpdatePayload,
} from "../types/rbac";
import type { MenuRow } from "../types/menuConfig";

const USERNAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

interface GrantSelection {
    user: UserRow;
    directGrants: string[];
}

export default function AdminUsersPage(): JSX.Element {
    const { t } = useTranslation();
    const [users, setUsers] = useState<UserRow[]>([]);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [modalOpen, setModalOpen] = useState(false);
    const [editing, setEditing] = useState<UserRow | null>(null);
    const [roleOptions, setRoleOptions] = useState<{ value: number; label: string }[]>([]);
    const [orgOptions, setOrgOptions] = useState<{ value: number; label: string }[]>([]);
    const [grant, setGrant] = useState<GrantSelection | null>(null);
    const [grantSaving, setGrantSaving] = useState(false);
    const [viewUser, setViewUser] = useState<UserRow | null>(null);
    const [effective, setEffective] = useState<UserEffectivePermissions | null>(null);
    const [menuRows, setMenuRows] = useState<MenuRow[]>([]);
    const [form] = Form.useForm();

    const fetchOptions = useCallback(async () => {
        const [roles, orgs] = await Promise.all([listRoles(), listOrganizations()]);
        setRoleOptions(roles.map((r) => ({ value: r.id, label: `${r.name}(${r.code})` })));
        setOrgOptions(orgs.map((o) => ({ value: o.id, label: `${o.name}(${o.code})` })));
    }, []);

    const fetchUsers = useCallback(async () => {
        setLoading(true);
        try {
            setUsers(await listUsers());
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.loadFailed")}: ${err.message ?? String(e)}`);
        } finally {
            setLoading(false);
        }
    }, [t]);

    useEffect(() => {
        void Promise.all([fetchUsers(), fetchOptions()]);
    }, [fetchUsers, fetchOptions]);

    const onCreate = () => {
        setEditing(null);
        form.resetFields();
        setModalOpen(true);
    };

    const onEdit = (rec: UserRow) => {
        setEditing(rec);
        form.setFieldsValue({
            username: rec.username,
            displayName: rec.displayName,
            email: rec.email ?? undefined,
            enabled: rec.enabled,
            roleIds: rec.roleIds,
            organizationIds: rec.organizationIds,
        });
        setModalOpen(true);
    };

    const onSubmit = async (values: Record<string, unknown>) => {
        setSaving(true);
        try {
            const roleIds = (values.roleIds as number[] | undefined) ?? [];
            const organizationIds =
                (values.organizationIds as number[] | undefined) ?? [];
            if (editing) {
                const payload: UserUpdatePayload = {
                    displayName: values.displayName as string,
                    email: values.email as string | undefined,
                    enabled: values.enabled as boolean,
                };
                await updateUser(editing.id, payload);
                await setUserRoles(editing.id, { roleIds });
                await setUserOrganizations(editing.id, { organizationIds });
                message.success(t("rbac.messages.updated"));
                setModalOpen(false);
            } else {
                const payload: UserCreatePayload = {
                    username: values.username as string,
                    displayName: values.displayName as string,
                    email: values.email as string | undefined,
                    enabled: (values.enabled as boolean | undefined) ?? true,
                };
                const created = await createUser(payload);
                // createUser 已成功 → 后续关联失败不能让弹窗停留（重试会 409 重复用户名）。
                // 关弹窗 + 刷新 + 明确提示：用户已建，可编辑补设角色/组织。
                try {
                    await setUserRoles(created.id, { roleIds });
                    await setUserOrganizations(created.id, { organizationIds });
                    message.success(t("rbac.messages.created"));
                } catch (e) {
                    const err = e as Error & { message?: string };
                    message.error(
                        `${t("rbac.errors.partialCreated")}: ${err.message ?? String(e)}`,
                    );
                    setModalOpen(false);
                    void fetchUsers();
                    return;
                }
                setModalOpen(false);
            }
            void fetchUsers();
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
        } finally {
            setSaving(false);
        }
    };

    const onDelete = async (rec: UserRow) => {
        try {
            await deleteUser(rec.id);
            message.success(t("rbac.messages.deleted"));
            void fetchUsers();
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
        }
    };

    const openGrant = async (rec: UserRow) => {
        try {
            const eff = await getUserEffectivePermissions(rec.id);
            setGrant({ user: rec, directGrants: eff.directGrants });
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.loadFailed")}: ${err.message ?? String(e)}`);
        }
    };

    const saveGrant = async (codes: string[]) => {
        if (!grant) {
            return;
        }
        setGrantSaving(true);
        try {
            await setUserPermissions(grant.user.id, { menuCodes: codes });
            message.success(t("rbac.messages.granted"));
            setGrant(null);
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
        } finally {
            setGrantSaving(false);
        }
    };

    const openEffective = async (rec: UserRow) => {
        setViewUser(rec);
        setEffective(null);
        setMenuRows([]);
        try {
            const [eff, rows] = await Promise.all([
                getUserEffectivePermissions(rec.id),
                listMenuRows(),
            ]);
            setEffective(eff);
            setMenuRows(rows);
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.loadFailed")}: ${err.message ?? String(e)}`);
        }
    };

    const labelMap = useMemo(
        () => buildMenuLabelMap(menuRows, (labelKey) => t(labelKey)),
        [menuRows, t],
    );

    const columns: ColumnsType<UserRow> = [
        { title: t("rbac.user.username"), dataIndex: "username" },
        { title: t("rbac.user.displayName"), dataIndex: "displayName" },
        {
            title: t("rbac.user.email"),
            dataIndex: "email",
            render: (v: string | null) => v || "—",
        },
        {
            title: t("rbac.user.enabled"),
            dataIndex: "enabled",
            render: (v: boolean) =>
                v ? (
                    <Tag color="green">{t("rbac.common.enabled")}</Tag>
                ) : (
                    <Tag color="default">{t("rbac.common.disabled")}</Tag>
                ),
        },
        {
            title: t("rbac.user.roles"),
            dataIndex: "roleCodes",
            render: (vs: string[]) =>
                vs.length === 0 ? "—" : vs.map((c) => <Tag key={c}>{c}</Tag>),
        },
        {
            title: t("rbac.user.organizations"),
            dataIndex: "organizationCodes",
            render: (vs: string[]) =>
                vs.length === 0 ? "—" : vs.map((c) => <Tag key={c}>{c}</Tag>),
        },
        {
            title: t("rbac.common.actions"),
            key: "actions",
            render: (_, rec) => (
                <Space>
                    <Button size="small" onClick={() => onEdit(rec)}>
                        {t("rbac.common.edit")}
                    </Button>
                    <Button size="small" onClick={() => void openGrant(rec)}>
                        {t("rbac.common.grant")}
                    </Button>
                    <Button size="small" onClick={() => void openEffective(rec)}>
                        {t("rbac.common.view")}
                    </Button>
                    <Popconfirm
                        title={`${t("rbac.common.confirmDelete")} ${rec.username}?`}
                        onConfirm={() => void onDelete(rec)}
                    >
                        <Button size="small" danger>
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
                    {t("rbac.user.create")}
                </Button>
                <Button onClick={() => void fetchUsers()}>
                    {t("rbac.common.refresh")}
                </Button>
            </Space>
            <Table
                rowKey="id"
                loading={loading}
                dataSource={users}
                columns={columns}
                pagination={{ pageSize: 20 }}
            />

            <Modal
                open={modalOpen}
                title={
                    editing ? t("rbac.user.edit") : t("rbac.user.create")
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
                        name="username"
                        label={t("rbac.user.username")}
                        rules={[{ required: true, pattern: USERNAME_RE }]}
                    >
                        <Input disabled={!!editing} />
                    </Form.Item>
                    <Form.Item
                        name="displayName"
                        label={t("rbac.user.displayName")}
                        rules={[{ required: true }]}
                    >
                        <Input />
                    </Form.Item>
                    <Form.Item name="email" label={t("rbac.user.email")}>
                        <Input />
                    </Form.Item>
                    <Form.Item
                        name="enabled"
                        label={t("rbac.user.enabled")}
                        valuePropName="checked"
                        initialValue
                    >
                        <Switch />
                    </Form.Item>
                    <Form.Item name="roleIds" label={t("rbac.user.selectRoles")}>
                        <Select
                            mode="multiple"
                            allowClear
                            options={roleOptions}
                            placeholder={t("rbac.common.selectPlaceholder")}
                        />
                    </Form.Item>
                    <Form.Item
                        name="organizationIds"
                        label={t("rbac.user.selectOrgs")}
                    >
                        <Select
                            mode="multiple"
                            allowClear
                            options={orgOptions}
                            placeholder={t("rbac.common.selectPlaceholder")}
                        />
                    </Form.Item>
                </Form>
            </Modal>

            {grant && (
                <MenuGrantModal
                    open
                    title={`${t("rbac.user.grantMenu")}: ${grant.user.username}`}
                    value={grant.directGrants}
                    onSave={(codes) => void saveGrant(codes)}
                    onCancel={() => setGrant(null)}
                    saving={grantSaving}
                />
            )}

            <Drawer
                open={!!viewUser}
                title={`${t("rbac.user.effectiveTitle")}: ${viewUser?.username ?? ""}`}
                onClose={() => setViewUser(null)}
                width={640}
            >
                {effective?.isSuperuser && (
                    <Alert
                        type="info"
                        showIcon
                        message={t("rbac.user.superuserHint")}
                        style={{ marginBottom: 16 }}
                    />
                )}
                <Descriptions
                    column={1}
                    bordered
                    size="small"
                    style={{ marginBottom: 16 }}
                >
                    <Descriptions.Item label={t("rbac.user.roles")}>
                        {effective?.roleCodes.length
                            ? effective.roleCodes.map((c) => <Tag key={c}>{c}</Tag>)
                            : "—"}
                    </Descriptions.Item>
                    <Descriptions.Item label={t("rbac.user.organizations")}>
                        {effective?.organizationCodes.length
                            ? effective.organizationCodes.map((c) => <Tag key={c}>{c}</Tag>)
                            : "—"}
                    </Descriptions.Item>
                </Descriptions>

                <Typography.Title level={5}>
                    {t("rbac.user.effectiveMenu")}
                </Typography.Title>
                <Space wrap style={{ marginBottom: 16 }}>
                    {effective?.menuCodes.length
                        ? effective.menuCodes.map((c) => (
                              <Tag key={c} color="blue">
                                  {labelMap.get(c) ?? c}
                              </Tag>
                          ))
                        : t("rbac.common.none")}
                </Space>

                <Typography.Title level={5}>
                    {t("rbac.user.directGrant")}
                </Typography.Title>
                <Space wrap style={{ marginBottom: 16 }}>
                    {effective?.directGrants.length
                        ? effective.directGrants.map((c) => (
                              <Tag key={c}>{labelMap.get(c) ?? c}</Tag>
                          ))
                        : t("rbac.common.none")}
                </Space>

                <Typography.Title level={5}>
                    {t("rbac.user.roleGrant")}
                </Typography.Title>
                {effective?.roleGrants.length ? (
                    effective.roleGrants.map((g) => (
                        <Space wrap key={g.subjectId} style={{ marginBottom: 8 }}>
                            <Tag color="purple">{g.name}</Tag>
                            {g.menuCodes.length ? (
                                g.menuCodes.map((c) => (
                                    <Tag key={c}>{labelMap.get(c) ?? c}</Tag>
                                ))
                            ) : (
                                <span>{t("rbac.common.none")}</span>
                            )}
                        </Space>
                    ))
                ) : (
                    <Typography.Paragraph>{t("rbac.common.none")}</Typography.Paragraph>
                )}

                <Typography.Title level={5}>
                    {t("rbac.user.orgGrant")}
                </Typography.Title>
                {effective?.organizationGrants.length ? (
                    effective.organizationGrants.map((g) => (
                        <Space wrap key={g.subjectId} style={{ marginBottom: 8 }}>
                            <Tag color="cyan">{g.name}</Tag>
                            {g.menuCodes.length ? (
                                g.menuCodes.map((c) => (
                                    <Tag key={c}>{labelMap.get(c) ?? c}</Tag>
                                ))
                            ) : (
                                <span>{t("rbac.common.none")}</span>
                            )}
                        </Space>
                    ))
                ) : (
                    <Typography.Paragraph>{t("rbac.common.none")}</Typography.Paragraph>
                )}
            </Drawer>
        </div>
    );
}
