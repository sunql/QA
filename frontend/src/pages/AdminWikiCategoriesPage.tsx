/**
 * Wiki 分类目录管理页（feat-wiki-category）。
 *
 * 设计：
 *  - 左侧：分类树（Tree）+ 顶部「新建顶级」按钮
 *  - 右侧：选中分类的详情 + 编辑 / 删除操作
 *  - 数据来源：GET /api/v1/wiki/categories/tree
 *  - 写路径：POST/PATCH/DELETE /api/v1/wiki/categories
 *
 * 与 AdminOrganizationsPage 同模式（树 + 详情侧栏），但分类更轻——只一个
 * name + 可选 parent + sort_order + 描述 + 概览 page_id，没有用户/角色。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
    Alert,
    App,
    Button,
    Empty,
    Form,
    Input,
    InputNumber,
    Modal,
    Popconfirm,
    Select,
    Space,
    Tree,
} from "antd";
import type { DataNode } from "antd/es/tree";
import { useTranslation } from "../i18n";
import {
    createWikiCategory,
    deleteWikiCategory,
    listWikiCategoryTree,
    listWikiPages,
    updateWikiCategory,
} from "../api/wikiPages";
import type { WikiCategoryNode, WikiPage } from "../types/wikiPages";

interface CategoryFormValues {
    name: string;
    parentId?: number | null;
    sortOrder?: number;
    description?: string | null;
    pageId?: string | null;
}

const ROOT_KEY = "__root__";

/** 树节点 → antd DataNode（递归）。所有分类节点都可点（选详情）。 */
function categoryToDataNode(cat: WikiCategoryNode): DataNode {
    return {
        key: String(cat.id),
        title: cat.name,
        children: cat.children.map(categoryToDataNode),
    };
}

/** 把平铺 page 列表 → 「id → page」查表（概览 page 选择器用）。 */
function pagesById(pages: WikiPage[]): Map<string, WikiPage> {
    const out = new Map<string, WikiPage>();
    for (const p of pages) out.set(p.pageId, p);
    return out;
}

/** 收集节点自身 + 所有后代的 id（编辑父分类时禁止自挂）。 */
function collectSubtreeIds(
    cats: WikiCategoryNode[],
    targetId: number,
): number[] | null {
    for (const c of cats) {
        if (c.id === targetId) {
            const out = [c.id];
            const collect = (nodes: WikiCategoryNode[]) => {
                for (const n of nodes) {
                    out.push(n.id);
                    collect(n.children);
                }
            };
            collect(c.children);
            return out;
        }
        const found = collectSubtreeIds(c.children, targetId);
        if (found) return found;
    }
    return null;
}

export function AdminWikiCategoriesPage() {
    const { t } = useTranslation();
    const { message: msgApi } = App.useApp();

    const [tree, setTree] = useState<WikiCategoryNode[]>([]);
    const [pages, setPages] = useState<WikiPage[]>([]);
    const [loading, setLoading] = useState(true);
    const [errorMsg, setErrorMsg] = useState<string | null>(null);
    /** antd v5 Tree 的 defaultExpandAll 在「首次渲染时树为空、之后才有数据」会失效
     *  —— 切到 controlled 模式：树数据进 DOM 后再 setExpandedKeys。 */
    const [expandedKeys, setExpandedKeys] = useState<string[]>([]);

    const [selectedId, setSelectedId] = useState<number | null>(null);
    const [createOpen, setCreateOpen] = useState(false);
    const [editOpen, setEditOpen] = useState(false);
    const [createForm] = Form.useForm<CategoryFormValues>();
    const [editForm] = Form.useForm<CategoryFormValues>();

    const fetchAll = useCallback(async () => {
        setLoading(true);
        setErrorMsg(null);
        try {
            const [cats, pageList] = await Promise.all([
                listWikiCategoryTree(),
                listWikiPages({ limit: 200 }),
            ]);
            setTree(cats);
            setPages(pageList.rows);
        } catch {
            setErrorMsg(t("wikiLinks.treeLoadFailed"));
        } finally {
            setLoading(false);
        }
    }, [t]);

    useEffect(() => {
        void fetchAll();
    }, [fetchAll]);

    /** 树加载完后展开所有节点。 */
    useEffect(() => {
        if (tree.length > 0) {
            const keys: string[] = [];
            const visit = (cats: WikiCategoryNode[]) => {
                for (const c of cats) {
                    keys.push(String(c.id));
                    visit(c.children);
                }
            };
            visit(tree);
            setExpandedKeys(keys);
        }
    }, [tree]);

    const selectedNode = useMemo(() => {
        if (selectedId === null) return null;
        const findOne = (
            cats: WikiCategoryNode[],
        ): WikiCategoryNode | null => {
            for (const c of cats) {
                if (c.id === selectedId) return c;
                const child = findOne(c.children);
                if (child) return child;
            }
            return null;
        };
        return findOne(tree);
    }, [selectedId, tree]);

    const pageMap = useMemo(() => pagesById(pages), [pages]);

    /** 父分类选项：所有分类（含根用空 option 表示）。编辑时排除自身 + 后代。 */
    const parentOptions = useMemo(() => {
        const out: { value: number; label: string }[] = [];
        const visit = (cats: WikiCategoryNode[], depth: number) => {
            for (const c of cats) {
                out.push({
                    value: c.id,
                    label: `${"—— ".repeat(depth)}${c.name}`,
                });
                visit(c.children, depth + 1);
            }
        };
        visit(tree, 0);
        return out;
    }, [tree]);

    /** 编辑时排除自身 + 后代，防止循环。 */
    const editParentOptions = useMemo(() => {
        if (selectedId === null) return parentOptions;
        const blocked = new Set(collectSubtreeIds(tree, selectedId) ?? []);
        return parentOptions.filter((o) => !blocked.has(o.value));
    }, [parentOptions, tree, selectedId]);

    /** 概览页选项：所有 wiki page。 */
    const pageOptions = useMemo(
        () =>
            pages
                .filter((p) => p.status !== "EXPIRED")
                .map((p) => ({ value: p.pageId, label: `${p.title}（${p.pageId}）` })),
        [pages],
    );

    // ---- handlers ----

    const openCreate = (parentId: number | null = null) => {
        createForm.resetFields();
        if (parentId !== null) createForm.setFieldValue("parentId", parentId);
        setCreateOpen(true);
    };

    const submitCreate = async () => {
        const values = await createForm.validateFields();
        try {
            await createWikiCategory({
                name: values.name,
                parentId: values.parentId ?? null,
                sortOrder: values.sortOrder ?? 0,
                description: values.description ?? null,
                pageId: values.pageId ?? null,
            });
            msgApi.success(t("wikiCategories.messages.created"));
            setCreateOpen(false);
            void fetchAll();
        } catch {
            setErrorMsg(t("wikiCategories.messages.createFailed"));
        }
    };

    const openEdit = () => {
        if (!selectedNode) return;
        editForm.setFieldsValue({
            name: selectedNode.name,
            parentId: selectedNode.parentId,
            sortOrder: selectedNode.sortOrder,
            description: selectedNode.description,
            pageId: selectedNode.pageId,
        });
        setEditOpen(true);
    };

    const submitEdit = async () => {
        if (selectedId === null) return;
        const values = await editForm.validateFields();
        try {
            await updateWikiCategory(selectedId, {
                name: values.name,
                parentId: values.parentId ?? null,
                sortOrder: values.sortOrder ?? 0,
                description: values.description ?? null,
                pageId: values.pageId ?? null,
            });
            msgApi.success(t("wikiCategories.messages.updated"));
            setEditOpen(false);
            void fetchAll();
        } catch {
            setErrorMsg(t("wikiCategories.messages.updateFailed"));
        }
    };

    const doDelete = async () => {
        if (selectedId === null) return;
        try {
            await deleteWikiCategory(selectedId);
            msgApi.success(t("wikiCategories.messages.deleted"));
            setSelectedId(null);
            void fetchAll();
        } catch {
            setErrorMsg(t("wikiCategories.messages.deleteFailed"));
        }
    };

    return (
        <div style={{ padding: 24 }}>
            <h2>{t("wikiCategories.title")}</h2>
            <p style={{ color: "#888" }}>{t("wikiCategories.description")}</p>

            {errorMsg && (
                <Alert
                    type="error"
                    showIcon
                    closable
                    message={errorMsg}
                    onClose={() => setErrorMsg(null)}
                    style={{ marginBottom: 16 }}
                />
            )}

            <div style={{ display: "flex", gap: 16 }}>
                {/* 左侧：分类树 + 新建按钮 */}
                <div style={{ width: 320, border: "1px solid #f0f0f0", padding: 12 }}>
                    <Space style={{ marginBottom: 8, width: "100%" }}>
                        <Button
                            type="primary"
                            onClick={() => openCreate(null)}
                            block
                        >
                            {t("wikiCategories.actions.create")}
                        </Button>
                        <Button onClick={() => void fetchAll()}>
                            {t("wikiCategories.actions.refresh")}
                        </Button>
                    </Space>
                    {tree.length === 0 && !loading ? (
                        <Empty description={t("wikiCategories.empty")} />
                    ) : (
                        <Tree
                            treeData={tree.map(categoryToDataNode)}
                            expandedKeys={expandedKeys}
                            onExpand={(keys) => setExpandedKeys(keys.map(String))}
                            selectedKeys={selectedId !== null ? [String(selectedId)] : []}
                            onSelect={(keys) => {
                                const k = keys[0];
                                if (k && k !== ROOT_KEY) {
                                    setSelectedId(Number(k));
                                }
                            }}
                        />
                    )}
                </div>

                {/* 右侧：选中分类详情 */}
                <div style={{ flex: 1, border: "1px solid #f0f0f0", padding: 16 }}>
                    {selectedNode ? (
                        <>
                            <h3 style={{ marginTop: 0 }}>
                                {selectedNode.name}
                                <span
                                    style={{
                                        color: "#888",
                                        fontSize: 12,
                                        marginLeft: 8,
                                        fontWeight: "normal",
                                    }}
                                >
                                    id={selectedNode.id}
                                </span>
                            </h3>
                            <Space style={{ marginBottom: 12 }}>
                                <Button
                                    type="primary"
                                    onClick={openEdit}
                                >
                                    {t("wikiCategories.actions.edit")}
                                </Button>
                                <Popconfirm
                                    title={t(
                                        "wikiCategories.messages.confirmDelete",
                                        { name: selectedNode.name },
                                    )}
                                    okText={t("common.confirm")}
                                    cancelText={t("common.cancel")}
                                    onConfirm={() => void doDelete()}
                                >
                                    <Button danger>
                                        {t("wikiCategories.actions.delete")}
                                    </Button>
                                </Popconfirm>
                                <Button onClick={() => openCreate(selectedNode.id)}>
                                    + {t("wikiCategories.actions.create")}
                                </Button>
                            </Space>
                            <Descriptions
                                name={selectedNode.name}
                                parentId={selectedNode.parentId}
                                sortOrder={selectedNode.sortOrder}
                                description={selectedNode.description}
                                pageTitle={
                                    selectedNode.pageId
                                        ? pageMap.get(selectedNode.pageId)?.title ??
                                          selectedNode.pageId
                                        : null
                                }
                                tree={tree}
                            />
                        </>
                    ) : (
                        <Empty description={t("wikiLinks.selectPageHint")} />
                    )}
                </div>
            </div>

            {/* 新建 Modal */}
            <Modal
                title={t("wikiCategories.actions.create")}
                open={createOpen}
                onCancel={() => setCreateOpen(false)}
                onOk={() => void submitCreate()}
                destroyOnHidden
            >
                <Form form={createForm} layout="vertical" preserve={false}>
                    <Form.Item
                        name="name"
                        label={t("wikiCategories.form.name")}
                        rules={[{ required: true, message: "请输入名称" }]}
                    >
                        <Input maxLength={100} />
                    </Form.Item>
                    <Form.Item name="parentId" label={t("wikiCategories.form.parent")}>
                        <Select
                            allowClear
                            options={parentOptions}
                            placeholder={t("wikiCategories.form.parentPlaceholder")}
                        />
                    </Form.Item>
                    <Form.Item name="sortOrder" label={t("wikiCategories.form.sortOrder")}>
                        <InputNumber min={0} defaultValue={0} />
                    </Form.Item>
                    <Form.Item name="description" label={t("wikiCategories.form.description")}>
                        <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} maxLength={500} />
                    </Form.Item>
                    <Form.Item name="pageId" label={t("wikiCategories.form.pageId")}>
                        <Select
                            allowClear
                            showSearch
                            optionFilterProp="label"
                            options={pageOptions}
                            placeholder={t("wikiCategories.form.pageIdPlaceholder")}
                        />
                    </Form.Item>
                </Form>
            </Modal>

            {/* 编辑 Modal */}
            <Modal
                title={t("wikiCategories.actions.edit")}
                open={editOpen}
                onCancel={() => setEditOpen(false)}
                onOk={() => void submitEdit()}
                destroyOnHidden
            >
                <Form form={editForm} layout="vertical" preserve={false}>
                    <Form.Item
                        name="name"
                        label={t("wikiCategories.form.name")}
                        rules={[{ required: true, message: "请输入名称" }]}
                    >
                        <Input maxLength={100} />
                    </Form.Item>
                    <Form.Item name="parentId" label={t("wikiCategories.form.parent")}>
                        <Select
                            allowClear
                            options={editParentOptions}
                            placeholder={t("wikiCategories.form.parentPlaceholder")}
                        />
                    </Form.Item>
                    <Form.Item name="sortOrder" label={t("wikiCategories.form.sortOrder")}>
                        <InputNumber min={0} />
                    </Form.Item>
                    <Form.Item name="description" label={t("wikiCategories.form.description")}>
                        <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} maxLength={500} />
                    </Form.Item>
                    <Form.Item name="pageId" label={t("wikiCategories.form.pageId")}>
                        <Select
                            allowClear
                            showSearch
                            optionFilterProp="label"
                            options={pageOptions}
                            placeholder={t("wikiCategories.form.pageIdPlaceholder")}
                        />
                    </Form.Item>
                </Form>
            </Modal>
        </div>
    );
}

/** 右侧只读详情（不引 antd Descriptions：保持小而内聚）。 */
function Descriptions({
    name,
    parentId,
    sortOrder,
    description,
    pageTitle,
    tree,
}: {
    name: string;
    parentId: number | null;
    sortOrder: number;
    description: string | null;
    pageTitle: string | null;
    tree: WikiCategoryNode[];
}) {
    const { t } = useTranslation();
    const parentName = useMemo(() => {
        if (parentId === null) return "—";
        const find = (cats: WikiCategoryNode[]): string | null => {
            for (const c of cats) {
                if (c.id === parentId) return c.name;
                const f = find(c.children);
                if (f) return f;
            }
            return null;
        };
        return find(tree) ?? `#${parentId}`;
    }, [parentId, tree]);

    return (
        <div>
            <p>
                <strong>{t("wikiCategories.columns.name")}：</strong>
                {name}
            </p>
            <p>
                <strong>{t("wikiCategories.columns.parent")}：</strong>
                {parentName}
            </p>
            <p>
                <strong>{t("wikiCategories.columns.sortOrder")}：</strong>
                {sortOrder}
            </p>
            <p>
                <strong>{t("wikiCategories.columns.description")}：</strong>
                {description ?? "—"}
            </p>
            <p>
                <strong>{t("wikiCategories.columns.page")}：</strong>
                {pageTitle ?? "—"}
            </p>
        </div>
    );
}

export default AdminWikiCategoriesPage;