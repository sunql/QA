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
    Select,
    Space,
    Table,
    Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import {
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
            </Space>

            <Table
                rowKey="pageId"
                size="small"
                loading={loading}
                columns={columns}
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

            <Drawer
                width={640}
                open={detail !== null}
                loading={detailLoading}
                onClose={() => setDetail(null)}
                title={detail?.title}
                extra={
                    detail && (
                        <Button danger size="small" onClick={() => void handleDelete()}>
                            {t("common.delete")}
                        </Button>
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
