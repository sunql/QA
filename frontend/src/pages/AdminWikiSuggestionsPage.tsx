/**
 * 结构化建议工作台（feat-wiki-knowledge M7，机制 4）。
 *
 * 这是**跨条目**的总览：审核人打开就看到「全库有哪些建议待处置」，而不是
 * 逐个条目点进去找待办。条目标题由后端 join 回填（``pageTitle``）——列表里
 * 只有 pageId 的话，审核人根本没法判断该不该接受。
 *
 * 接受**不可逆**（终态）：接受会物化出可执行规则/流程草稿，所以重复接受或
 * 反向处置都会拿到 409。界面上必须把它当业务反馈展示（「这条已被处置」），
 * 而不是当系统故障。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Modal, Popconfirm, Select, Space, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import {
    acceptWikiSuggestion,
    generateWikiSuggestions,
    listAllWikiSuggestions,
    rejectWikiSuggestion,
} from "../api/wikiSuggestions";
import { listWikiPages } from "../api/wikiPages";
import { listImportModels } from "../api/wikiImport";
import type { WikiImportModel } from "../types/wikiImport";
import {
    SUGGESTION_STATUSES,
    type StructureSuggestion,
    type SuggestionStatus,
} from "../types/wikiSuggestions";

const STATUS_COLOR: Record<string, string> = {
    PENDING: "orange",
    ACCEPTED: "green",
    REJECTED: "red",
};

const PAGE_SIZE = 20;

/** 结构化结果的 JSON 摘要：列表里只需一眼看出「抽到什么形状」 */
function summarize(structure: Record<string, unknown> | null): string {
    if (structure === null) {
        return "-";
    }
    const body = JSON.stringify(structure);
    return body.length > 120 ? `${body.slice(0, 120)}…` : body;
}

export default function AdminWikiSuggestionsPage() {
    const { t } = useTranslation();

    const [rows, setRows] = useState<StructureSuggestion[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(0);
    const [loading, setLoading] = useState(false);
    const [statusFilter, setStatusFilter] = useState<SuggestionStatus | undefined>(
        "PENDING",
    );
    const [errorMsg, setErrorMsg] = useState<string | null>(null);

    const [generateOpen, setGenerateOpen] = useState(false);
    const [pageOptions, setPageOptions] = useState<{ value: string; label: string }[]>(
        [],
    );
    const [models, setModels] = useState<WikiImportModel[]>([]);
    const [generatePageId, setGeneratePageId] = useState<string | undefined>();
    const [generateModelId, setGenerateModelId] = useState<number | undefined>();
    const [generating, setGenerating] = useState(false);

    const fetchList = useCallback(async () => {
        setLoading(true);
        try {
            const res = await listAllWikiSuggestions({
                status: statusFilter,
                limit: PAGE_SIZE,
                offset: page * PAGE_SIZE,
            });
            setRows(res.rows);
            setTotal(res.total);
        } catch {
            // 拦截器已 toast 后端原因；保留旧数据避免误读为「建议都没了」
        } finally {
            setLoading(false);
        }
    }, [statusFilter, page]);

    useEffect(() => {
        void fetchList();
    }, [fetchList]);

    /** 打开生成弹窗时才去拉条目与模型：不开就不花这两个请求 */
    const openGenerate = useCallback(async () => {
        setGenerateOpen(true);
        setErrorMsg(null);
        try {
            const [pages, modelList] = await Promise.all([
                listWikiPages({ limit: 100, offset: 0 }),
                listImportModels(),
            ]);
            setPageOptions(
                pages.rows.map((p) => ({ value: p.pageId, label: p.title })),
            );
            setModels(modelList);
        } catch {
            // 下拉为空时用户能看出「加载失败」，并可通过重开弹窗重试
        }
    }, []);

    const handleGenerate = useCallback(async () => {
        if (generatePageId === undefined) {
            return;
        }
        setGenerating(true);
        setErrorMsg(null);
        try {
            await generateWikiSuggestions(generatePageId, generateModelId);
            setGenerateOpen(false);
            setGeneratePageId(undefined);
            setGenerateModelId(undefined);
            void fetchList();
        } catch {
            setErrorMsg(t("wikiSuggestions.errors.generateFailed"));
        } finally {
            setGenerating(false);
        }
    }, [generatePageId, generateModelId, fetchList, t]);

    const handleResolve = useCallback(
        async (suggestion: StructureSuggestion, accept: boolean) => {
            setErrorMsg(null);
            try {
                if (accept) {
                    await acceptWikiSuggestion(suggestion.id);
                } else {
                    await rejectWikiSuggestion(suggestion.id);
                }
                void fetchList();
            } catch (e) {
                const err = e as Error & { status?: number };
                setErrorMsg(
                    err.status === 409
                        ? t("wikiSuggestions.errors.alreadyResolved")
                        : t("wikiSuggestions.errors.resolveFailed"),
                );
                void fetchList();
            }
        },
        [fetchList, t],
    );

    const columns: ColumnsType<StructureSuggestion> = useMemo(
        () => [
            {
                title: t("wikiSuggestions.columns.pageTitle"),
                dataIndex: "pageTitle",
                key: "pageTitle",
                render: (title: string | null, record) =>
                    // 孤儿建议（条目已删）没有标题。不隐藏它：一条谁也不处理的
                    // 待办，比一条标题为空的待办危险得多。
                    title ?? (
                        <span style={{ color: "#d46b08" }}>
                            {t("wikiSuggestions.orphan", { pageId: record.pageId })}
                        </span>
                    ),
            },
            {
                title: t("wikiSuggestions.columns.dimension"),
                dataIndex: "suggestedDimension",
                key: "suggestedDimension",
                width: 130,
                render: (v: string) => (
                    <Tag color="geekblue">{t(`wikiPages.dimensions.${v}`)}</Tag>
                ),
            },
            {
                title: t("wikiSuggestions.columns.structure"),
                dataIndex: "extractedStructure",
                key: "extractedStructure",
                render: (v: Record<string, unknown> | null) => (
                    <code style={{ fontSize: 12 }}>{summarize(v)}</code>
                ),
            },
            {
                title: t("wikiSuggestions.columns.confidence"),
                dataIndex: "confidence",
                key: "confidence",
                width: 100,
                render: (v: number | null) =>
                    v === null ? t("common.dash") : v.toFixed(2),
            },
            {
                title: t("wikiSuggestions.columns.status"),
                dataIndex: "status",
                key: "status",
                width: 110,
                render: (v: string) => (
                    <Tag color={STATUS_COLOR[v] ?? "default"}>
                        {t(`wikiSuggestions.statuses.${v}`)}
                    </Tag>
                ),
            },
            {
                title: t("common.actions"),
                key: "actions",
                width: 160,
                render: (_, record) =>
                    record.status !== "PENDING" ? (
                        <span style={{ color: "#888" }}>{t("common.emDash")}</span>
                    ) : (
                        <Space>
                            <Popconfirm
                                title={t("wikiSuggestions.confirmAccept")}
                                okText={t("common.confirm")}
                                cancelText={t("common.cancel")}
                                onConfirm={() => void handleResolve(record, true)}
                            >
                                <Button size="small" type="primary">
                                    {t("wikiSuggestions.actions.accept")}
                                </Button>
                            </Popconfirm>
                            <Popconfirm
                                title={t("wikiSuggestions.confirmReject")}
                                okText={t("common.confirm")}
                                cancelText={t("common.cancel")}
                                onConfirm={() => void handleResolve(record, false)}
                            >
                                <Button size="small" danger>
                                    {t("wikiSuggestions.actions.reject")}
                                </Button>
                            </Popconfirm>
                        </Space>
                    ),
            },
        ],
        [t, handleResolve],
    );

    return (
        <div style={{ padding: 24 }}>
            <h2 style={{ marginBottom: 16 }}>{t("wikiSuggestions.title")}</h2>

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
                    aria-label={t("wikiSuggestions.filters.status")}
                    style={{ width: 160 }}
                    placeholder={t("wikiSuggestions.filters.status")}
                    value={statusFilter}
                    onChange={(v: SuggestionStatus | undefined) => {
                        setPage(0);
                        setStatusFilter(v);
                    }}
                    options={SUGGESTION_STATUSES.map((s) => ({
                        value: s,
                        label: t(`wikiSuggestions.statuses.${s}`),
                    }))}
                />
                <Button onClick={() => void fetchList()}>{t("common.refresh")}</Button>
                <Button type="primary" onClick={() => void openGenerate()}>
                    {t("wikiSuggestions.actions.generate")}
                </Button>
            </Space>

            <Table
                rowKey="id"
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
                locale={{ emptyText: t("wikiSuggestions.empty") }}
            />

            <Modal
                open={generateOpen}
                title={t("wikiSuggestions.actions.generate")}
                okText={t("common.confirm")}
                cancelText={t("common.cancel")}
                confirmLoading={generating}
                okButtonProps={{ disabled: generatePageId === undefined }}
                onCancel={() => setGenerateOpen(false)}
                onOk={() => void handleGenerate()}
            >
                <p style={{ marginBottom: 8 }}>{t("wikiSuggestions.generatePageLabel")}</p>
                <Select
                    aria-label={t("wikiSuggestions.generatePageLabel")}
                    style={{ width: "100%", marginBottom: 16 }}
                    showSearch
                    optionFilterProp="label"
                    placeholder={t("wikiSuggestions.generatePagePlaceholder")}
                    value={generatePageId}
                    onChange={setGeneratePageId}
                    options={pageOptions}
                />

                <p style={{ marginBottom: 8 }}>{t("wikiSuggestions.generateModelLabel")}</p>
                <Select
                    aria-label={t("wikiSuggestions.generateModelLabel")}
                    style={{ width: "100%" }}
                    allowClear
                    placeholder={t("wikiSuggestions.generateModelPlaceholder")}
                    value={generateModelId}
                    onChange={setGenerateModelId}
                    options={models.map((m) => ({
                        value: m.id,
                        label: m.usable
                            ? `${m.modelName} (${m.provider})`
                            : `${m.modelName} (${m.provider}) — ${t("wikiImport.modelUnusable")}`,
                        disabled: !m.usable,
                    }))}
                />
                <p style={{ color: "#888", fontSize: 12, marginTop: 8 }}>
                    {t("wikiSuggestions.generateHint")}
                </p>
            </Modal>
        </div>
    );
}
