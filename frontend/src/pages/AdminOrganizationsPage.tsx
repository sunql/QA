/** 组织管理页（feat-rbac-identity + feat-org-tree）。
 *
 * 能力（需求 #2/#3/#4 + feat-org-tree）：
 *  - Tab 切换：树形视图（OrganizationTree 拖拽改父级）/ 列表视图（Table）
 *  - 组织 CRUD（code 创建后不可变；parent_id 可改；sortOrder 同级排序）
 *  - Modal 增加 parentId Select（不强制 TreeSelect——拖拽改 parent 用 Tree UI）
 *    和 sortOrder InputNumber（默认 0）
 *  - 组织菜单授权（MenuGrantModal set-replace）
 *  - 查看任意组织赋予的权限（readOnly，需求 #4）
 */
import { useCallback, useEffect, useState } from "react";
import {
    Table,
    Button,
    Modal,
    Form,
    Input,
    InputNumber,
    Select,
    message,
    Popconfirm,
    Space,
    Tabs,
    Alert,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";
import {
    listOrganizations,
    listOrganizationTree,
    createOrganization,
    updateOrganization,
    deleteOrganization,
    setOrganizationPermissions,
    getOrganizationPermissions,
} from "../api/organizations";
import { OrganizationTree } from "../components/rbac/OrganizationTree";
import { MenuGrantModal } from "../components/rbac/MenuGrantModal";
import type {
    OrganizationRow,
    OrganizationTreeNode,
    OrganizationCreatePayload,
    OrganizationUpdatePayload,
} from "../types/rbac";

const { TextArea } = Input;
// 同步后端 app/schemas/rbac.py::_CODE_PATTERN（命名空间风格）
const CODE_RE = /^[a-z][a-zA-Z0-9_]*(?:\.[a-z][a-zA-Z0-9_]*)*$/;

interface PermSelection {
    org: OrganizationRow;
    codes: string[];
    readOnly: boolean;
}

export default function AdminOrganizationsPage(): JSX.Element {
    const { t } = useTranslation();
    const [orgs, setOrgs] = useState<OrganizationRow[]>([]);
    const [tree, setTree] = useState<OrganizationTreeNode[]>([]);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [modalOpen, setModalOpen] = useState(false);
    const [editing, setEditing] = useState<OrganizationRow | null>(null);
    const [perm, setPerm] = useState<PermSelection | null>(null);
    const [permSaving, setPermSaving] = useState(false);
    const [form] = Form.useForm();

    const fetchOrgs = useCallback(async () => {
        setLoading(true);
        try {
            setOrgs(await listOrganizations());
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.loadFailed")}: ${err.message ?? String(e)}`);
        } finally {
            setLoading(false);
        }
    }, [t]);

    const fetchTree = useCallback(async () => {
        setLoading(true);
        try {
            setTree(await listOrganizationTree());
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.loadFailed")}: ${err.message ?? String(e)}`);
        } finally {
            setLoading(false);
        }
    }, [t]);

    useEffect(() => {
        void fetchOrgs();
        void fetchTree();
    }, [fetchOrgs, fetchTree]);

    const onCreate = () => {
        setEditing(null);
        form.resetFields();
        form.setFieldsValue({ parentId: null, sortOrder: 0 });
        setModalOpen(true);
    };

    const onEdit = (rec: OrganizationRow) => {
        setEditing(rec);
        form.setFieldsValue({
            code: rec.code,
            name: rec.name,
            description: rec.description ?? undefined,
            parentId: rec.parentId ?? null,
            sortOrder: rec.sortOrder ?? 0,
        });
        setModalOpen(true);
    };

    const onSubmit = async (values: Record<string, unknown>) => {
        setSaving(true);
        try {
            if (editing) {
                const payload: OrganizationUpdatePayload = {
                    name: values.name as string,
                    description: (values.description as string | undefined) || null,
                    parentId:
                        "parentId" in values
                            ? ((values.parentId as number | null | undefined) ?? null)
                            : undefined,
                    sortOrder:
                        "sortOrder" in values
                            ? (values.sortOrder as number)
                            : undefined,
                };
                await updateOrganization(editing.id, payload);
                message.success(t("rbac.messages.updated"));
            } else {
                const payload: OrganizationCreatePayload = {
                    code: values.code as string,
                    name: values.name as string,
                    description: (values.description as string | undefined) || null,
                    parentId:
                        (values.parentId as number | null | undefined) ?? null,
                    sortOrder:
                        (values.sortOrder as number | undefined) ?? 0,
                };
                await createOrganization(payload);
                message.success(t("rbac.messages.created"));
            }
            setModalOpen(false);
            void fetchOrgs();
            void fetchTree();
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
        } finally {
            setSaving(false);
        }
    };

    const onDelete = async (rec: OrganizationRow) => {
        try {
            await deleteOrganization(rec.id);
            message.success(t("rbac.messages.deleted"));
            void fetchOrgs();
            void fetchTree();
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
        }
    };

    const openPerm = async (rec: OrganizationRow, readOnly: boolean) => {
        try {
            const perms = await getOrganizationPermissions(rec.id);
            setPerm({ org: rec, codes: perms.menuCodes, readOnly });
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
            await setOrganizationPermissions(perm.org.id, { menuCodes: codes });
            message.success(t("rbac.messages.granted"));
            setPerm(null);
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
        } finally {
            setPermSaving(false);
        }
    };

    const parentOptions = orgs
        .filter((o) => o.id !== editing?.id)
        .map((o) => ({ value: o.id, label: `${o.name}（${o.code}）` }));

    const parentLabelOf = (id: number | null | undefined): string => {
        if (id == null) {
            return t("rbac.organization.rootOrg");
        }
        const hit = orgs.find((o) => o.id === id);
        return hit ? `${hit.name}（${hit.code}）` : `#${id}`;
    };

    const columns: ColumnsType<OrganizationRow> = [
        { title: t("rbac.organization.code"), dataIndex: "code" },
        { title: t("rbac.organization.name"), dataIndex: "name" },
        {
            title: t("rbac.organization.parent"),
            dataIndex: "parentId",
            render: (v: number | null) => parentLabelOf(v),
        },
        {
            title: t("rbac.organization.sortOrder"),
            dataIndex: "sortOrder",
            width: 90,
        },
        {
            title: t("rbac.organization.description"),
            dataIndex: "description",
            ellipsis: true,
            render: (v: string | null) => v || "—",
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
                    {t("rbac.organization.create")}
                </Button>
                <Button
                    onClick={() => {
                        void fetchOrgs();
                        void fetchTree();
                    }}
                >
                    {t("rbac.common.refresh")}
                </Button>
            </Space>
            <Tabs
                defaultActiveKey="tree"
                items={[
                    {
                        key: "tree",
                        label: t("rbac.organization.treeView"),
                        children: (
                            <>
                                <Alert
                                    type="info"
                                    showIcon
                                    message={t("rbac.organization.dragHint")}
                                    style={{ marginBottom: 12 }}
                                />
                                <OrganizationTree
                                    tree={tree}
                                    loading={loading}
                                    onChanged={() => {
                                        void fetchOrgs();
                                        void fetchTree();
                                    }}
                                />
                            </>
                        ),
                    },
                    {
                        key: "list",
                        label: t("rbac.organization.listView"),
                        children: (
                            <Table
                                rowKey="id"
                                loading={loading}
                                dataSource={orgs}
                                columns={columns}
                                pagination={{ pageSize: 20 }}
                            />
                        ),
                    },
                ]}
            />

            <Modal
                open={modalOpen}
                title={
                    editing
                        ? t("rbac.organization.edit")
                        : t("rbac.organization.create")
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
                        label={t("rbac.organization.code")}
                        rules={[{ required: true, pattern: CODE_RE }]}
                    >
                        <Input disabled={!!editing} />
                    </Form.Item>
                    <Form.Item
                        name="name"
                        label={t("rbac.organization.name")}
                        rules={[{ required: true }]}
                    >
                        <Input />
                    </Form.Item>
                    <Form.Item
                        name="parentId"
                        label={t("rbac.organization.selectParent")}
                    >
                        <Select
                            allowClear
                            placeholder={t("rbac.organization.rootOrg")}
                            options={parentOptions}
                        />
                    </Form.Item>
                    <Form.Item
                        name="sortOrder"
                        label={t("rbac.organization.sortOrder")}
                        rules={[{ type: "number", min: 0 }]}
                    >
                        <InputNumber min={0} style={{ width: "100%" }} />
                    </Form.Item>
                    <Form.Item
                        name="description"
                        label={t("rbac.organization.description")}
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
                            ? `${t("rbac.organization.viewPerm")}: ${perm.org.name}`
                            : `${t("rbac.organization.grantMenu")}: ${perm.org.name}`
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