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

const PAGE_SIZE = 20;

interface CreateFormValues {
    title: string;
    content: string;
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
                destroyOnClose
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
                destroyOnClose
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
                width={640}
                open={detail !== null}
                loading={detailLoading}
                onClose={() => setDetail(null)}
                title={detail?.title}
                extra={
                    detail && (
                        // 二次确认：删除是不可逆的（连带清掉 claim / 关系 / 产物），
                        // 而按钮就在 Drawer 右上角，误点代价是整个条目。批量删除
                        // 早有确认弹窗，单条不该更宽松。
                        <Popconfirm
                            title={t("wikiPages.deleteConfirm", { title: detail.title })}
                            okText={t("wikiPages.batchDelete.confirm")}
                            cancelText={t("common.cancel")}
                            okButtonProps={{ danger: true }}
                            // 直传 async 函数，**不要**包成 `() => void handleDelete()`。
                            // antd 的 ActionButton 带 `quitOnNullishReturnValue`：返回值不是
                            // thenable 时它会立刻关闭弹窗并清掉防重入标志 clickedRef，于是
                            // 请求在途期间再点一次「确认删除」会真的发出第二个请求（第二次
                            // 要么 404 报出「删除失败」误导用户，要么撞 StaleDataError 500）。
                            // 返回 Promise 才会 loading + 保持弹窗 + 锁住重入。
                            // handleDelete 内部 try/catch 已落定、不会 reject，故不会走到
                            // ActionButton 的 Promise.reject 分支。
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
                    <div>
                        <p style={{ color: "#888" }}>
                            {t("wikiPages.detail.meta", {
                                pageId: detail.pageId,
                                version: detail.version,
                                stage: STAGE_LABEL[detail.structureStage] ?? detail.structureStage,
                            })}
                        </p>

                        <h4>{t("wikiPages.detail.dimension")}</h4>
                        <p style={{ color: "#888" }}>
                            {detail.autoClassification === null
                                ? t("wikiPages.detail.noSuggestion")
                                : t("wikiPages.detail.suggestion", {
                                      dimension: detail.autoClassification.primary ?? "-",
                                      confidence:
                                          detail.autoClassification.confidence ?? 0,
                                  })}
                        </p>
                        <Space wrap style={{ marginBottom: 8 }}>
                            <Select
                                aria-label={t("wikiPages.detail.dimension")}
                                style={{ width: 200 }}
                                value={currentDimension ?? undefined}
                                onChange={(v: KnowledgeDimension | undefined) =>
                                    setPendingDimension(v ?? null)
                                }
                                options={dimensionOptions}
                                placeholder={t("wikiPages.undetermined")}
                            />
                            <Button
                                type="primary"
                                disabled={!isDimensionDirty}
                                onClick={() =>
                                    void handleReclassify(currentDimension)
                                }
                            >
                                {t("wikiPages.actions.saveDimension")}
                            </Button>
                            <Button
                                danger
                                onClick={() => {
                                    setPendingDimension(null);
                                    void handleReclassify(null);
                                }}
                            >
                                {t("wikiPages.actions.rejectDimension")}
                            </Button>
                        </Space>
                        <p style={{ color: "#888", fontSize: 12 }}>
                            {t("wikiPages.detail.rejectHint")}
                        </p>

                        <h4>{t("wikiPages.detail.statusTitle")}</h4>
                        <Select
                            aria-label={t("wikiPages.detail.statusTitle")}
                            style={{ width: 200 }}
                            value={detail.status}
                            onChange={(v: WikiPageStatus) => {
                                void updateWikiPage(detail.pageId, { status: v })
                                    .then((updated) => setDetail(updated))
                                    .catch(() =>
                                        setErrorMsg(
                                            t("wikiPages.errors.statusUpdateFailed"),
                                        ),
                                    );
                            }}
                            options={WIKI_PAGE_STATUSES.map((s) => ({
                                value: s,
                                label: t(`wikiPages.statuses.${s}`),
                            }))}
                        />

                        <h4 style={{ marginTop: 24 }}>
                            {t("wikiPages.detail.content")}
                        </h4>
                        <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                            {detail.content}
                        </pre>
                    </div>
                )}
            </Drawer>
        </div>
    );
}
