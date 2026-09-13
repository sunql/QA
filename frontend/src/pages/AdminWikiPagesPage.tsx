/**
 * 知识条目管理页（feat-wiki-knowledge M7）。
 *
 * 这一页是机制 1「自动分类 → 业务专家可调整」的落地面：列表展示机器给出的
 * 维度建议，详情里一键确认/改判/打回。之所以不做「静默分类」，是因为错分的
 * 条目会一路污染机制 2/3/6 的结果（关系挂错类、冲突判错对、覆盖度假绿），
 * 而人在页面上一眼就能看出「这条明明是流程」。
 *
 * 错误处理约定同 ``AdminWikiImportPage``：``httpClient`` 的响应拦截器已经
 * toast 过后端原因，这里**不重复 toast**，只把可采取行动的状态留在页面级 Alert。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
    Alert,
    App,
    Button,
    Drawer,
    Empty,
    Form,
    Input,
    Modal,
    Popconfirm,
    Select,
    Space,
    Table,
    Tabs,
    Tag,
} from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";
import { useTranslation } from "../i18n";
import {
    batchDeleteWikiPages,
    createWikiPage,
    deleteWikiPage,
    getWikiPage,
    listWikiPages,
    reclassifyWikiPage,
    updateWikiPage,
} from "../api/wikiPages";
import {
    KNOWLEDGE_DIMENSIONS,
    WIKI_PAGE_STATUSES,
    type KnowledgeDimension,
    type WikiPage,
    type WikiPageStatus,
} from "../types/wikiPages";
import WikiClaimsPanel from "../components/wiki/WikiClaimsPanel";
import WikiRelationsPanel from "../components/wiki/WikiRelationsPanel";
import {
    detectWikiConflicts,
    listWikiConflicts,
    resolveWikiConflict,
} from "../api/wikiConflicts";
import type { KnowledgeConflict } from "../types/wikiConflicts";

/** 状态 → antd Tag 颜色（EFFECTIVE 才是真正生效的那一档） */
const STATUS_COLOR: Record<string, string> = {
    DRAFT: "default",
    REVIEW: "blue",
    APPROVED: "cyan",
    EFFECTIVE: "green",
    EXPIRED: "red",
};

/** 结构演进阶段 → 展示文案后缀（机制 5 的进度条语义） */
const STAGE_LABEL: Record<string, string> = {
    MARKDOWN: "① 文档",
    SEMI_STRUCTURED: "② 半结构",
    FULLY_STRUCTURED: "③ 结构化",
};

// 状态可**自由切换**（不做 ladder 限制）：原设计按 DRAFT→REVIEW→APPROVED→
// EFFECTIVE→EXPIRED 一档一档推，目的是审计清晰，但业务专家日常用太繁琐——
// 一条已经 EXPIRED 的条目若想拿回来复用，要点 4 下才行。
// 现状：status 字段不参与 learning_feedback 写回，所以「跳过中间档」不会污染
// 学习闭环；历史状态变迁在 wiki_page 表的 updated_at 上有迹可查，
// 不依赖强约束的状态机来兜底（见 wiki_page_service.updatePage）。
const PAGE_SIZE = 20;

interface CreateFormValues {
    title: string;
    content: string;
    dimension?: KnowledgeDimension;
    authorityLevel?: string;
}

/** 页面级提示条（成功摘要 / 部分失败）。批量删除是破坏性操作，只 toast 会飘走。 */
interface Notice {
    type: "success" | "warning";
    text: string;
}

export default function AdminWikiPagesPage() {
    const { t } = useTranslation();

    const [rows, setRows] = useState<WikiPage[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(0);
    const [loading, setLoading] = useState(false);

    const [dimensionFilter, setDimensionFilter] = useState<
        KnowledgeDimension | undefined
    >();
    const [statusFilter, setStatusFilter] = useState<WikiPageStatus | undefined>();

    const [createOpen, setCreateOpen] = useState(false);
    const [createForm] = Form.useForm<CreateFormValues>();

    const [detail, setDetail] = useState<WikiPage | null>(null);
    const [detailLoading, setDetailLoading] = useState(false);
    /** 详情抽屉里待提交的维度（undefined = 用户没动过，沿用原值） */
    const [pendingDimension, setPendingDimension] = useState<
        KnowledgeDimension | null | undefined
    >(undefined);
    const [errorMsg, setErrorMsg] = useState<string | null>(null);

    /** 批量删除：选中的 pageId（跨页保留，见 rowSelection 说明） */
    const [selectedPageIds, setSelectedPageIds] = useState<string[]>([]);
    const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
    const [batchDeleting, setBatchDeleting] = useState(false);
    const [notice, setNotice] = useState<Notice | null>(null);

    const fetchList = useCallback(async () => {
        setLoading(true);
        try {
            const res = await listWikiPages({
                dimension: dimensionFilter,
                status: statusFilter,
                limit: PAGE_SIZE,
                offset: page * PAGE_SIZE,
            });
            setRows(res.rows);
            setTotal(res.total);
        } catch {
            // 拦截器已提示过后端原因。列表拉取失败时保留旧数据：
            // 清空表格会让用户以为「知识都没了」，比看到过期数据更吓人。
        } finally {
            setLoading(false);
        }
    }, [dimensionFilter, statusFilter, page]);

    useEffect(() => {
        void fetchList();
    }, [fetchList]);

    const openDetail = useCallback(
        async (pageId: string) => {
            setDetailLoading(true);
            setErrorMsg(null);
            try {
                const full = await getWikiPage(pageId);
                setDetail(full);
                setPendingDimension(undefined);
            } catch {
                // 拦截器已提示；抽屉保持关闭，用户可重试
            } finally {
                setDetailLoading(false);
            }
        },
        [],
    );

    const handleCreate = useCallback(async () => {
        const values = await createForm.validateFields();
        try {
            await createWikiPage(values);
            setCreateOpen(false);
            createForm.resetFields();
            void fetchList();
        } catch {
            // 校验失败（表单）或接口失败（拦截器已提示）都不关弹窗：
            // 关掉会让用户丢掉刚敲的正文。
        }
    }, [createForm, fetchList]);

    /**
     * 提交分类处置。``dimension`` 为 ``null`` 即「打回这个分类」——
     * 与「没改动」是两件不同的事，所以不能用 ``undefined`` 代替。
     */
    const handleReclassify = useCallback(
        async (dimension: KnowledgeDimension | null) => {
            if (detail === null) {
                return;
            }
            setErrorMsg(null);
            try {
                const res = await reclassifyWikiPage(detail.pageId, dimension);
                setDetail(res.page);
                setPendingDimension(undefined);
                void fetchList();
            } catch {
                setErrorMsg(t("wikiPages.errors.reclassifyFailed"));
            }
        },
        [detail, fetchList, t],
    );

    const handleDelete = useCallback(async () => {
        if (detail === null) {
            return;
        }
        try {
            await deleteWikiPage(detail.pageId);
            setDetail(null);
            void fetchList();
        } catch {
            setErrorMsg(t("wikiPages.errors.deleteFailed"));
        }
    }, [detail, fetchList, t]);

    /**
     * 批量删除。
     *
     * 后端是**部分成功**语义：不存在的 id 回在 `notFound` 里、其余照删，请求本身
     * 仍是 200。所以成功分支必须读返回值分两类提示 —— 把「删了 5 条但 2 条没找到」
     * 报成一片绿，用户会以为全都删干净了。
     */
    const handleBatchDelete = useCallback(async () => {
        if (selectedPageIds.length === 0) {
            return;
        }
        setBatchDeleting(true);
        setErrorMsg(null);
        setNotice(null);
        try {
            const res = await batchDeleteWikiPages(selectedPageIds);
            setBatchDeleteOpen(false);
            setSelectedPageIds([]);
            const summary = t("wikiPages.batchDelete.result", {
                deleted: res.deletedPageIds.length,
                claims: res.cascade.claims,
                relations: res.cascade.relations,
                suggestions: res.cascade.suggestions,
                rules: res.cascade.rules,
                workflows: res.cascade.workflows,
            });
            const missing =
                res.notFound.length > 0
                    ? ` ${t("wikiPages.batchDelete.partialNotFound", {
                          count: res.notFound.length,
                          ids: res.notFound.join(
                              t("wikiPages.batchDelete.idSeparator"),
                          ),
                      })}`
                    : "";
            setNotice({
                type: res.notFound.length > 0 ? "warning" : "success",
                text: `${summary}${missing}`,
            });
            if (page > 0 && res.deletedPageIds.length >= rows.length) {
                // 当前页被整页删空 → 回第一页。只 setPage 不 fetchList：page 变化会
                // 触发 useEffect 重新拉取，两个都做会多发一次请求。
                setPage(0);
            } else {
                void fetchList();
            }
        } catch {
            // 拦截器已提示过后端原因；关弹窗 + 页面级提示，选中集合保留以便重试
            setBatchDeleteOpen(false);
            setErrorMsg(t("wikiPages.errors.batchDeleteFailed"));
        } finally {
            setBatchDeleting(false);
        }
    }, [selectedPageIds, page, rows.length, fetchList, t]);

    /**
     * 行多选。
     *
     * `preserveSelectedRowKeys`：翻页/筛选后已勾选的行仍留在选中集合里。批量删除
     * 的对象是「用户勾过的那些」，不是「当前页可见的那些」；翻一页就清空会让用户
     * 以为白选了，也逼着他一页一页删。
     */
    const rowSelection: TableProps<WikiPage>["rowSelection"] = useMemo(
        () => ({
            selectedRowKeys: selectedPageIds,
            onChange: (keys) => setSelectedPageIds(keys.map(String)),
            preserveSelectedRowKeys: true,
        }),
        [selectedPageIds],
    );

    const dimensionOptions = useMemo(
        () =>
            KNOWLEDGE_DIMENSIONS.map((d) => ({
                value: d,
                label: t(`wikiPages.dimensions.${d}`),
            })),
        [t],
    );

    const columns: ColumnsType<WikiPage> = useMemo(
        () => [
            {
                title: t("wikiPages.columns.pageId"),
                dataIndex: "pageId",
                key: "pageId",
                width: 160,
                render: (id: string) => (
                    <code style={{ fontSize: 11 }}>{id}</code>
                ),
            },
            {
                title: t("wikiPages.columns.title"),
                dataIndex: "title",
                key: "title",
                render: (title: string, record) => (
                    <Button
                        type="link"
                        style={{ padding: 0 }}
                        onClick={() => void openDetail(record.pageId)}
                    >
                        {title}
                    </Button>
                ),
            },
            {
                title: t("wikiPages.columns.dimension"),
                dataIndex: "dimension",
                key: "dimension",
                width: 140,
                render: (dimension: string | null) =>
                    dimension === null ? (
                        <Tag>{t("wikiPages.undetermined")}</Tag>
                    ) : (
                        <Tag color="geekblue">
                            {t(`wikiPages.dimensions.${dimension}`)}
                        </Tag>
                    ),
            },
            {
                title: t("wikiPages.columns.status"),
                dataIndex: "status",
                key: "status",
                width: 110,
                render: (status: string) => (
                    <Tag color={STATUS_COLOR[status] ?? "default"}>
                        {t(`wikiPages.statuses.${status}`)}
                    </Tag>
                ),
            },
            {
                title: t("wikiPages.columns.structureStage"),
                dataIndex: "structureStage",
                key: "structureStage",
                width: 130,
                render: (stage: string) => STAGE_LABEL[stage] ?? stage,
            },
            {
                title: t("wikiPages.columns.version"),
                dataIndex: "version",
                key: "version",
                width: 90,
            },
            {
                title: t("wikiPages.columns.createdAt"),
                dataIndex: "createdTime",
                key: "createdTime",
                width: 160,
                render: (ts: string | null) =>
                    ts ? new Date(ts).toLocaleString("zh-CN") : "-",
            },
        ],
        [t, openDetail],
    );

    /** 详情抽屉里当前的维度：用户动过就用他的选择，没动过用后端值 */
    const currentDimension =
        pendingDimension === undefined ? detail?.dimension ?? null : pendingDimension;
    const isDimensionDirty =
        pendingDimension !== undefined && pendingDimension !== detail?.dimension;

    return (
        <div style={{ padding: 24 }}>
            <h2 style={{ marginBottom: 16 }}>{t("wikiPages.title")}</h2>

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

            {notice && (
                <Alert
                    type={notice.type}
                    showIcon
                    closable
                    message={notice.text}
                    onClose={() => setNotice(null)}
                    style={{ marginBottom: 16 }}
                />
            )}

            <Space style={{ marginBottom: 16 }} wrap>
                <Select
                    allowClear
                    aria-label={t("wikiPages.filters.dimension")}
                    style={{ width: 180 }}
                    placeholder={t("wikiPages.filters.dimension")}
                    value={dimensionFilter}
                    onChange={(v: KnowledgeDimension | undefined) => {
                        setPage(0);
                        setDimensionFilter(v);
                    }}
                    options={dimensionOptions}
                />
                <Select
                    allowClear
                    aria-label={t("wikiPages.filters.status")}
                    style={{ width: 160 }}
                    placeholder={t("wikiPages.filters.status")}
                    value={statusFilter}
                    onChange={(v: WikiPageStatus | undefined) => {
                        setPage(0);
                        setStatusFilter(v);
                    }}
                    options={WIKI_PAGE_STATUSES.map((s) => ({
                        value: s,
                        label: t(`wikiPages.statuses.${s}`),
                    }))}
                />
                <Button onClick={() => void fetchList()}>{t("common.refresh")}</Button>
                <Button type="primary" onClick={() => setCreateOpen(true)}>
                    {t("wikiPages.actions.create")}
                </Button>
                <Button
                    danger
                    disabled={selectedPageIds.length === 0}
                    onClick={() => setBatchDeleteOpen(true)}
                >
                    {t("wikiPages.batchDelete.action")}
                </Button>
                {selectedPageIds.length > 0 && (
                    <span style={{ color: "#888" }}>
                        {t("wikiPages.batchDelete.selected", {
                            count: selectedPageIds.length,
                        })}
                    </span>
                )}
            </Space>

            <Table
                rowKey="pageId"
                size="small"
                loading={loading}
                columns={columns}
                rowSelection={rowSelection}
                dataSource={rows}
                pagination={{
                    current: page + 1,
                    pageSize: PAGE_SIZE,
                    total,
                    showSizeChanger: false,
                    onChange: (p) => setPage(p - 1),
                }}
                locale={{ emptyText: t("wikiPages.empty") }}
            />

            <Modal
                title={t("wikiPages.actions.create")}
                open={createOpen}
                onCancel={() => setCreateOpen(false)}
                onOk={() => void handleCreate()}
                okText={t("common.save")}
                cancelText={t("common.cancel")}
                destroyOnHidden
            >
                <Form form={createForm} layout="vertical" preserve={false}>
                    <Form.Item
                        name="title"
                        label={t("wikiPages.columns.title")}
                        rules={[
                            {
                                required: true,
                                message: t("wikiPages.errors.titleRequired"),
                            },
                        ]}
                    >
                        <Input maxLength={200} />
                    </Form.Item>
                    <Form.Item
                        name="content"
                        label={t("wikiPages.form.content")}
                        rules={[
                            {
                                required: true,
                                message: t("wikiPages.errors.contentRequired"),
                            },
                        ]}
                    >
                        <Input.TextArea autoSize={{ minRows: 6, maxRows: 16 }} />
                    </Form.Item>
                    <Form.Item
                        name="dimension"
                        label={t("wikiPages.form.dimension")}
                    >
                        <Select
                            allowClear
                            options={dimensionOptions}
                            placeholder={t("wikiPages.undetermined")}
                        />
                    </Form.Item>
                    <Form.Item
                        name="authorityLevel"
                        label={t("wikiPages.form.authorityLevel")}
                    >
                        <Select
                            allowClear
                            options={["L0","L1","L2","L3","L4","L5"].map((l) => ({
                                value: l,
                                label: l,
                            }))}
                            placeholder={t("wikiPages.form.authorityLevelPlaceholder")}
                        />
                    </Form.Item>
                </Form>
            </Modal>

            <Modal
                title={t("wikiPages.batchDelete.title")}
                open={batchDeleteOpen}
                onCancel={() => setBatchDeleteOpen(false)}
                onOk={() => void handleBatchDelete()}
                okText={t("wikiPages.batchDelete.confirm")}
                cancelText={t("common.cancel")}
                okButtonProps={{ danger: true, loading: batchDeleting }}
                cancelButtonProps={{ disabled: batchDeleting }}
                destroyOnHidden
            >
                <p>
                    {t("wikiPages.batchDelete.count", {
                        count: selectedPageIds.length,
                    })}
                </p>
                <p style={{ color: "#888", fontSize: 12 }}>
                    {t("wikiPages.batchDelete.cascadeHint")}
                </p>
            </Modal>

            <Drawer
                width={720}
                open={detail !== null}
                loading={detailLoading}
                onClose={() => setDetail(null)}
                title={detail?.title}
                extra={
                    detail && (
                        <Popconfirm
                            title={t("wikiPages.deleteConfirm", { title: detail.title })}
                            okText={t("wikiPages.batchDelete.confirm")}
                            cancelText={t("common.cancel")}
                            okButtonProps={{ danger: true }}
                            onConfirm={handleDelete}
                        >
                            <Button danger size="small">
                                {t("common.delete")}
                            </Button>
                        </Popconfirm>
                    )
                }
            >
                {detail && (
                    <Tabs
                        items={[
                            {
                                key: "basic",
                                label: t("wikiPages.detail.tabs.basic"),
                                children: (
                                    <BasicInfoTab
                                        detail={detail}
                                        currentDimension={currentDimension}
                                        isDimensionDirty={isDimensionDirty}
                                        onDimensionChange={setPendingDimension}
                                        onReclassify={handleReclassify}
                                        dimensionOptions={dimensionOptions}
                                        onRefresh={async () => {
                                            const updated = await getWikiPage(detail.pageId);
                                            setDetail(updated);
                                            void fetchList();
                                        }}
                                        errorMsg={errorMsg}
                                        setErrorMsg={setErrorMsg}
                                    />
                                ),
                            },
                            {
                                key: "claims",
                                label: t("wikiPages.detail.tabs.claims"),
                                children: (
                                    <WikiClaimsPanel pageId={detail.pageId} />
                                ),
                            },
                            {
                                key: "relations",
                                label: t("wikiPages.detail.tabs.relations"),
                                children: (
                                    <WikiRelationsPanel pageId={detail.pageId} />
                                ),
                            },
                            {
                                key: "conflicts",
                                label: t("wikiPages.detail.tabs.conflicts"),
                                children: (
                                    <ConflictTab pageId={detail.pageId} />
                                ),
                            },
                        ]}
                    />
                )}
            </Drawer>
        </div>
    );
}

/* ---------- Sub-components ---------- */

/** 基本信息 Tab：标题/正文/维度/权威等级编辑 + 状态流转按钮 */
interface BasicInfoTabProps {
    detail: WikiPage;
    currentDimension: KnowledgeDimension | null | undefined;
    isDimensionDirty: boolean;
    onDimensionChange: (v: KnowledgeDimension | null | undefined) => void;
    onReclassify: (dimension: KnowledgeDimension | null) => Promise<void>;
    dimensionOptions: { value: string; label: string }[];
    onRefresh: () => Promise<void>;
    errorMsg: string | null;
    setErrorMsg: (v: string | null) => void;
}

function BasicInfoTab({
    detail,
    currentDimension,
    isDimensionDirty,
    onDimensionChange,
    onReclassify,
    dimensionOptions,
    onRefresh,
    errorMsg,
    setErrorMsg,
}: BasicInfoTabProps) {
    const { t } = useTranslation();
    const [title, setTitle] = useState(detail.title);
    const [content, setContent] = useState(detail.content);
    const [authorityLevel, setAuthorityLevel] = useState<string | null>(
        detail.authorityLevel,
    );
    const [saving, setSaving] = useState(false);

    // 同步 detail 变化到本地 state
    useEffect(() => {
        setTitle(detail.title);
        setContent(detail.content);
        setAuthorityLevel(detail.authorityLevel);
    }, [detail]);

    const handleSaveField = async (
        field: "title" | "content",
        value: string,
    ) => {
        setSaving(true);
        try {
            await updateWikiPage(detail.pageId, { [field]: value });
            void onRefresh();
        } finally {
            setSaving(false);
        }
    };

    const handleSaveAuthority = async (level: string | null) => {
        setSaving(true);
        try {
            await updateWikiPage(detail.pageId, { authorityLevel: level });
            setAuthorityLevel(level);
            void onRefresh();
        } finally {
            setSaving(false);
        }
    };

    const handleStatusChange = async (next: WikiPageStatus) => {
        // 选同一个值（无变更）直接跳过 —— Select onChange 在「不动」时也会触发
        if (next === detail.status) {
            return;
        }
        try {
            await updateWikiPage(detail.pageId, { status: next });
            void onRefresh();
        } catch {
            setErrorMsg(t("wikiPages.errors.statusUpdateFailed"));
        }
    };

    return (
        <div>
            {/* 元数据行 */}
            <p style={{ color: "#888", fontSize: 12, marginBottom: 16 }}>
                {t("wikiPages.detail.meta", {
                    pageId: detail.pageId,
                    version: detail.version,
                    stage: STAGE_LABEL[detail.structureStage] ?? detail.structureStage,
                })}
            </p>

            {/* 标题 */}
            <h4>{t("wikiPages.columns.title")}</h4>
            <Input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                onBlur={() => {
                    if (title !== detail.title) {
                        void handleSaveField("title", title);
                    }
                }}
                style={{ marginBottom: 12 }}
                maxLength={200}
            />

            {/* 正文 */}
            <h4>{t("wikiPages.detail.content")}</h4>
            <Input.TextArea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                autoSize={{ minRows: 6, maxRows: 16 }}
                style={{ marginBottom: 12 }}
            />
            <Button
                size="small"
                loading={saving}
                disabled={content === detail.content}
                onClick={() => void handleSaveField("content", content)}
                style={{ marginBottom: 16 }}
            >
                {t("common.save")}
            </Button>

            {/* 权威等级 */}
            <h4>{t("wikiPages.detail.authorityLevel")}</h4>
            <Space style={{ marginBottom: 16 }}>
                <Select
                    allowClear
                    value={authorityLevel ?? undefined}
                    onChange={(v) => {
                        const level = v ?? null;
                        void handleSaveAuthority(level);
                    }}
                    options={["L0","L1","L2","L3","L4","L5"].map((l) => ({
                        value: l,
                        label: l,
                    }))}
                    style={{ width: 160 }}
                    placeholder={t("wikiPages.form.authorityLevelPlaceholder")}
                />
            </Space>

            {/* 维度 */}
            <h4>{t("wikiPages.detail.dimension")}</h4>
            <p style={{ color: "#888", fontSize: 12 }}>
                {detail.autoClassification === null
                    ? t("wikiPages.detail.noSuggestion")
                    : t("wikiPages.detail.suggestion", {
                          dimension: detail.autoClassification.primary ?? "-",
                          confidence: detail.autoClassification.confidence ?? 0,
                      })}
            </p>
            <Space wrap style={{ marginBottom: 8 }}>
                <Select
                    aria-label={t("wikiPages.detail.dimension")}
                    style={{ width: 200 }}
                    value={currentDimension ?? undefined}
                    onChange={(v: KnowledgeDimension | undefined) =>
                        onDimensionChange(v ?? null)
                    }
                    options={dimensionOptions}
                    placeholder={t("wikiPages.undetermined")}
                />
                <Button
                    type="primary"
                    disabled={!isDimensionDirty}
                    loading={saving}
                    onClick={() => void onReclassify(currentDimension ?? null)}
                >
                    {t("wikiPages.actions.saveDimension")}
                </Button>
                <Button
                    danger
                    disabled={detail.dimension === null && currentDimension !== null}
                    onClick={() => void onReclassify(null)}
                >
                    {t("wikiPages.actions.rejectDimension")}
                </Button>
            </Space>
            <p style={{ color: "#888", fontSize: 12, marginBottom: 16 }}>
                {t("wikiPages.detail.rejectHint")}
            </p>

            {/* 状态流转：自由下拉，不做 ladder 限制。
             *
             * 原设计按 DRAFT→REVIEW→APPROVED→EFFECTIVE→EXPIRED 一档一档点
             * 按钮是为了审计清晰，但日常把条目从 EXPIRED 复用要连点 4 下；
             * 而且状态变更不写 learning_feedback（见 wiki_page_service.updatePage
             * 的 dimension 路径才写），强约束的状态机没换来等价的可追溯收益。
             * 现在直接一个 Select 列出全部 5 档，可任意切换。
             */}
            <h4>{t("wikiPages.detail.statusTitle")}</h4>
            <div style={{ marginBottom: 16 }}>
                <Select
                    aria-label={t("wikiPages.detail.statusTitle")}
                    style={{ width: 200 }}
                    value={detail.status}
                    onChange={(v: WikiPageStatus) => void handleStatusChange(v)}
                    options={WIKI_PAGE_STATUSES.map((s) => ({
                        value: s,
                        label: t(`wikiPages.statuses.${s}`),
                    }))}
                />
                <p style={{ color: "#888", fontSize: 12, marginTop: 6, marginBottom: 0 }}>
                    {t("wikiPages.detail.statusHint")}
                </p>
            </div>

            {/* 错误提示 */}
            {errorMsg && (
                <Alert
                    type="error"
                    showIcon
                    closable
                    message={errorMsg}
                    onClose={() => setErrorMsg(null)}
                    style={{ marginTop: 8 }}
                />
            )}
        </div>
    );
}

/** 冲突检测 Tab */
interface ConflictTabProps {
    pageId: string;
}

function ConflictTab({ pageId }: ConflictTabProps) {
    const { t } = useTranslation();
    const { message } = App.useApp();
    const [detecting, setDetecting] = useState(false);
    const [conflicts, setConflicts] = useState<KnowledgeConflict[]>([]);
    const [loadingConflicts, setLoadingConflicts] = useState(false);

    const loadConflicts = useCallback(async () => {
        setLoadingConflicts(true);
        try {
            const data = await listWikiConflicts({ conflictType: undefined });
            setConflicts(data.rows.filter((c) => c.pageIds.includes(pageId)));
        } finally {
            setLoadingConflicts(false);
        }
    }, [pageId]);

    useEffect(() => {
        void loadConflicts();
    }, [loadConflicts]);

    const handleDetect = async () => {
        setDetecting(true);
        try {
            const result = await detectWikiConflicts(pageId);
            message.info(
                result.total > 0
                    ? t("wikiPages.conflicts.found", { count: result.total })
                    : t("wikiPages.conflicts.noneFound"),
            );
            void loadConflicts();
        } finally {
            setDetecting(false);
        }
    };

    const handleResolve = async (conflictId: number) => {
        try {
            await resolveWikiConflict(conflictId, "RESOLVED");
            message.success(t("wikiPages.conflicts.resolved"));
            void loadConflicts();
        } catch {
            // 拦截器已 toast
        }
    };

    return (
        <div>
            <div style={{ marginBottom: 12 }}>
                <Button onClick={handleDetect} loading={detecting}>
                    {t("wikiPages.conflicts.detect")}
                </Button>
            </div>
            {loadingConflicts ? (
                <Alert type="info" message={t("common.loading")} />
            ) : conflicts.length === 0 ? (
                <Empty
                    description={t("wikiPages.conflicts.noneFound")}
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                />
            ) : (
                <Table
                    rowKey="id"
                    size="small"
                    dataSource={conflicts}
                    columns={[
                        {
                            title: t("wikiPages.conflicts.type"),
                            dataIndex: "conflictType",
                            key: "conflictType",
                        },
                        {
                            title: t("wikiPages.conflicts.severity"),
                            dataIndex: "severity",
                            key: "severity",
                        },
                        {
                            title: t("wikiPages.conflicts.description"),
                            dataIndex: "description",
                            key: "description",
                        },
                        {
                            title: t("wikiPages.conflicts.actions"),
                            key: "actions",
                            render: (_: unknown, record: KnowledgeConflict) =>
                                !record.resolvedAt ? (
                                    <Button
                                        size="small"
                                        onClick={() => void handleResolve(record.id)}
                                    >
                                        {t("wikiPages.conflicts.resolve")}
                                    </Button>
                                ) : null,
                        },
                    ]}
                    pagination={false}
                />
            )}
        </div>
    );
}
