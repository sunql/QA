/** 菜单管理页（feat-rbac-identity Phase E，需求 #3 动态增删改菜单）。
 *
 * 管理后端 GET /menu-config/admin 全量扁平行；新建/编辑按节点类型区分：
 *  - section：path 为空的一级类，只能挂叶子项
 *  - item：挂 parentCode（section）的叶子，path 为前端路由
 * 代码 code 创建后不可变（权限授予以 code 为键）。删除 section 含子项 → 409。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
    Table,
    Button,
    Modal,
    Form,
    Input,
    InputNumber,
    Radio,
    Select,
    Switch,
    Tabs,
    Tag,
    message,
    Popconfirm,
    Space,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";
import {
    listMenuRows,
    listMenuTree,
    createMenu,
    updateMenu,
    deleteMenu,
} from "../api/menuConfig";
import { buildMenuLabelMap } from "../components/rbac/MenuGrantModal";
import { ICON_REGISTRY, renderIcon } from "../components/common/menuIcons";
import { MenuTree } from "../components/rbac/MenuTree";
import type {
    MenuConfigTreeNode,
    MenuCreatePayload,
    MenuRow,
    MenuUpdatePayload,
} from "../types/menuConfig";

// 命名空间风格：首段小写开头，后续段允许 camelCase（与现有 menu seed 一致）。
// 不接受大写开头、下划线/数字开头、空段或连续点。
// 同步至后端 app/schemas/rbac.py::_CODE_PATTERN。
const CODE_RE = /^[a-z][a-zA-Z0-9_]*(?:\.[a-z][a-zA-Z0-9_]*)*$/;
const ICON_OPTIONS = Object.keys(ICON_REGISTRY).map((v) => ({ value: v }));

interface RowFormValues {
    nodeType: "section" | "item";
    code: string;
    labelKey: string;
    iconCode?: string | null;
    sortOrder?: number;
    path?: string | null;
    parentCode?: string | null;
    permissionCode?: string | null;
    visible?: boolean;
}

const toSection = (r: MenuRow): boolean => r.path == null;

export default function AdminMenusPage(): JSX.Element {
    const { t } = useTranslation();
    const [rows, setRows] = useState<MenuRow[]>([]);
    const [tree, setTree] = useState<MenuConfigTreeNode[]>([]);
    const [loading, setLoading] = useState(false);
    const [treeLoading, setTreeLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [modalOpen, setModalOpen] = useState(false);
    const [editing, setEditing] = useState<MenuRow | null>(null);
    const [nodeType, setNodeType] = useState<"section" | "item">("section");
    const [activeTab, setActiveTab] = useState<"list" | "tree">("tree");
    const [form] = Form.useForm();

    const fetchRows = useCallback(async () => {
        setLoading(true);
        try {
            setRows(await listMenuRows());
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.loadFailed")}: ${err.message ?? String(e)}`);
        } finally {
            setLoading(false);
        }
    }, [t]);

    const fetchTree = useCallback(async () => {
        setTreeLoading(true);
        try {
            setTree(await listMenuTree());
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.loadFailed")}: ${err.message ?? String(e)}`);
        } finally {
            setTreeLoading(false);
        }
    }, [t]);

    const refreshAll = useCallback(() => {
        void fetchRows();
        void fetchTree();
    }, [fetchRows, fetchTree]);

    useEffect(() => {
        void fetchRows();
        void fetchTree();
    }, [fetchRows, fetchTree]);

    // 展示顺序：一级类按 sortOrder → 其下叶子按 sortOrder；孤儿叶子兜底。
    const displayRows = useMemo(() => {
        const sortRows = (a: MenuRow, b: MenuRow) =>
            a.sortOrder - b.sortOrder || a.code.localeCompare(b.code);
        const sections = rows.filter(toSection).sort(sortRows);
        const leaves = rows.filter((r) => !toSection(r)).sort(sortRows);
        const byParent = new Map<string, MenuRow[]>();
        for (const leaf of leaves) {
            const bucket = byParent.get(leaf.parentCode ?? "");
            if (bucket) {
                bucket.push(leaf);
            } else {
                byParent.set(leaf.parentCode ?? "", [leaf]);
            }
        }
        const ordered: MenuRow[] = [];
        const sectionCodes = new Set(sections.map((s) => s.code));
        for (const s of sections) {
            ordered.push(s);
            ordered.push(...(byParent.get(s.code) ?? []));
        }
        ordered.push(...leaves.filter((l) => !sectionCodes.has(l.parentCode ?? "")));
        return ordered;
    }, [rows]);

    const labelMap = useMemo(
        () => buildMenuLabelMap(rows, (labelKey) => t(labelKey)),
        [rows, t],
    );

    const sectionOptions = useMemo(
        () =>
            rows
                .filter(toSection)
                .map((s) => ({ value: s.code, label: labelMap.get(s.code) ?? s.code })),
        [rows, labelMap],
    );

    const onCreate = () => {
        setEditing(null);
        setNodeType("section");
        form.resetFields();
        form.setFieldsValue({ nodeType: "section", sortOrder: 0, visible: true });
        setModalOpen(true);
    };

    const onEdit = (rec: MenuRow) => {
        const type = toSection(rec) ? "section" : "item";
        setEditing(rec);
        setNodeType(type);
        form.setFieldsValue({
            nodeType: type,
            code: rec.code,
            labelKey: rec.labelKey,
            iconCode: rec.iconCode ?? undefined,
            sortOrder: rec.sortOrder,
            path: rec.path ?? undefined,
            parentCode: rec.parentCode ?? undefined,
            permissionCode: rec.permissionCode ?? undefined,
            visible: rec.visible,
        });
        setModalOpen(true);
    };

    const onSubmit = async (values: RowFormValues) => {
        setSaving(true);
        try {
            const base = {
                labelKey: values.labelKey,
                iconCode: values.iconCode ?? null,
                sortOrder: values.sortOrder ?? 0,
                permissionCode: values.permissionCode ?? null,
                visible: values.visible ?? true,
            };
            if (values.nodeType === "item") {
                if (!values.path) {
                    message.error(t("rbac.menuPage.pathRequired"));
                    return;
                }
                if (!values.parentCode) {
                    message.error(t("rbac.menuPage.parentRequired"));
                    return;
                }
                if (editing) {
                    const payload: MenuUpdatePayload = {
                        ...base,
                        path: values.path,
                        parentCode: values.parentCode,
                    };
                    await updateMenu(editing.code, payload);
                    message.success(t("rbac.messages.updated"));
                } else {
                    const payload: MenuCreatePayload = {
                        code: values.code,
                        ...base,
                        path: values.path,
                        parentCode: values.parentCode,
                    };
                    await createMenu(payload);
                    message.success(t("rbac.messages.created"));
                }
            } else {
                if (editing) {
                    const payload: MenuUpdatePayload = { ...base };
                    await updateMenu(editing.code, payload);
                    message.success(t("rbac.messages.updated"));
                } else {
                    const payload: MenuCreatePayload = {
                        code: values.code,
                        ...base,
                    };
                    await createMenu(payload);
                    message.success(t("rbac.messages.created"));
                }
            }
            setModalOpen(false);
            refreshAll();
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
        } finally {
            setSaving(false);
        }
    };

    const onDelete = async (rec: MenuRow) => {
        try {
            await deleteMenu(rec.code);
            message.success(t("rbac.messages.deleted"));
            refreshAll();
        } catch (e) {
            const err = e as Error & { message?: string };
            message.error(`${t("rbac.errors.failed")}: ${err.message ?? String(e)}`);
        }
    };

    const columns: ColumnsType<MenuRow> = [
        { title: t("rbac.menuPage.code"), dataIndex: "code" },
        {
            title: t("rbac.menuPage.label"),
            dataIndex: "labelKey",
            render: (_: string, rec: MenuRow) => (
                <span>
                    {toSection(rec) ? <Tag color="blue">{t("rbac.menuPage.section")}</Tag> : null}
                    {labelMap.get(rec.code) ?? rec.code}
                </span>
            ),
        },
        {
            title: t("rbac.menuPage.icon"),
            dataIndex: "iconCode",
            render: (v: string | null) => renderIcon(v ?? undefined),
        },
        {
            title: t("rbac.menuPage.parent"),
            dataIndex: "parentCode",
            render: (v: string | null) => (v ? labelMap.get(v) ?? v : "—"),
        },
        {
            title: t("rbac.menuPage.path"),
            dataIndex: "path",
            render: (v: string | null) => v || "—",
        },
        {
            title: t("rbac.menuPage.sortOrder"),
            dataIndex: "sortOrder",
            width: 90,
        },
        {
            title: t("rbac.menuPage.visible"),
            dataIndex: "visible",
            render: (v: boolean) =>
                v ? (
                    <Tag color="green">{t("rbac.common.enabled")}</Tag>
                ) : (
                    <Tag color="default">{t("rbac.common.disabled")}</Tag>
                ),
        },
        {
            title: t("rbac.menuPage.hasChildren"),
            dataIndex: "hasChildren",
            render: (v: boolean) => (v ? <Tag color="orange">✓</Tag> : "—"),
        },
        {
            title: t("rbac.common.actions"),
            key: "actions",
            render: (_, rec) => (
                <Space>
                    <Button size="small" onClick={() => onEdit(rec)}>
                        {t("rbac.common.edit")}
                    </Button>
                    <Popconfirm
                        title={`${t("rbac.common.confirmDelete")} ${rec.code}?`}
                        onConfirm={() => void onDelete(rec)}
                    >
                        <Button size="small" danger disabled={rec.hasChildren}>
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
                    {t("rbac.menuPage.create")}
                </Button>
                <Button onClick={() => refreshAll()}>
                    {t("rbac.common.refresh")}
                </Button>
            </Space>
            <Tabs
                activeKey={activeTab}
                onChange={(k) => setActiveTab(k as "list" | "tree")}
                items={[
                    {
                        key: "tree",
                        label: t("rbac.menuPage.treeView"),
                        children: (
                            <MenuTree
                                tree={tree}
                                loading={treeLoading}
                                onChanged={refreshAll}
                            />
                        ),
                    },
                    {
                        key: "list",
                        label: t("rbac.menuPage.listView"),
                        children: (
                            <Table
                                rowKey="code"
                                loading={loading}
                                dataSource={displayRows}
                                columns={columns}
                                pagination={false}
                            />
                        ),
                    },
                ]}
            />

            <Modal
                open={modalOpen}
                title={
                    editing
                        ? t("rbac.menuPage.edit")
                        : t("rbac.menuPage.create")
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
                        name="nodeType"
                        label={t("rbac.menuPage.nodeType")}
                        rules={[{ required: true }]}
                    >
                        <Radio.Group
                            disabled={!!editing}
                            onChange={(e) => setNodeType(e.target.value)}
                            options={[
                                { label: t("rbac.menuPage.section"), value: "section" },
                                { label: t("rbac.menuPage.item"), value: "item" },
                            ]}
                        />
                    </Form.Item>
                    <Form.Item
                        name="code"
                        label={t("rbac.menuPage.code")}
                        rules={[{ required: true, pattern: CODE_RE }]}
                    >
                        <Input disabled={!!editing} />
                    </Form.Item>
                    <Form.Item
                        name="labelKey"
                        label={t("rbac.menuPage.labelKey")}
                        extra={t("rbac.menuPage.labelKeyHint")}
                        rules={[{ required: true }]}
                    >
                        <Input />
                    </Form.Item>
                    {nodeType === "item" && (
                        <Form.Item
                            name="parentCode"
                            label={t("rbac.menuPage.parent")}
                            rules={[{ required: true }]}
                        >
                            <Select options={sectionOptions} />
                        </Form.Item>
                    )}
                    {nodeType === "item" && (
                        <Form.Item
                            name="path"
                            label={t("rbac.menuPage.path")}
                            rules={[{ required: true }]}
                        >
                            <Input placeholder="/xxx" />
                        </Form.Item>
                    )}
                    <Form.Item name="iconCode" label={t("rbac.menuPage.icon")}>
                        <Select
                            allowClear
                            options={ICON_OPTIONS}
                            placeholder={t("rbac.common.selectPlaceholder")}
                        />
                    </Form.Item>
                    <Form.Item
                        name="permissionCode"
                        label={t("rbac.menuPage.permissionCode")}
                    >
                        <Input />
                    </Form.Item>
                    <Form.Item
                        name="sortOrder"
                        label={t("rbac.menuPage.sortOrder")}
                        initialValue={0}
                    >
                        <InputNumber min={0} style={{ width: "100%" }} />
                    </Form.Item>
                    <Form.Item
                        name="visible"
                        label={t("rbac.menuPage.visible")}
                        valuePropName="checked"
                        initialValue
                    >
                        <Switch />
                    </Form.Item>
                </Form>
            </Modal>
        </div>
    );
}
