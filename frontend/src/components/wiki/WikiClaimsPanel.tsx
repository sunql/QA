/**
 * 知识条目的事实原子（Claim）+ 证据（Evidence）列表。
 * 可展开行：展开后显示 Evidence 5 元组详情。
 */

import { useCallback, useEffect, useState } from "react";
import { App, Badge, Button, Descriptions, Empty, Popconfirm, Select, Table } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import type { TableProps } from "antd/es/table";
import { useTranslation } from "../../i18n";
import { extractWikiClaims, listWikiClaims } from "../../api/wikiPages";
import { listImportModels } from "../../api/wikiImport";
import type { Evidence, KnowledgeClaim } from "../../types/wikiPages";
import type { WikiImportModel } from "../../types/wikiImport";

interface WikiClaimsPanelProps {
    pageId: string;
}

export default function WikiClaimsPanel({ pageId }: WikiClaimsPanelProps) {
    const { t } = useTranslation();
    const { message } = App.useApp();
    const [claims, setClaims] = useState<KnowledgeClaim[]>([]);
    const [loading, setLoading] = useState(false);
    const [extracting, setExtracting] = useState(false);
    const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
    const [models, setModels] = useState<WikiImportModel[]>([]);
    const [selectedModelId, setSelectedModelId] = useState<number | null>(null);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const data = await listWikiClaims(pageId);
            setClaims(data);
        } finally {
            setLoading(false);
        }
    }, [pageId]);

    // 加载可用模型
    useEffect(() => {
        void listImportModels().then(setModels).catch(() => {
            // 模型列表加载失败不影响主流程
        });
    }, []);

    // 首次挂载时拉取
    useEffect(() => {
        void load();
    }, [load]);

    /**
     * 触发后端跑一次 claim 抽取。需要选择模型，否则后端返回 SKIPPED。
     * force=true：后端先删除该页已有 claims 再重抽（换模型/内容修订场景）。
     */
    const handleExtract = async (force: boolean) => {
        if (!selectedModelId) {
            message.warning("请先选择一个模型");
            return;
        }
        setExtracting(true);
        try {
            const result = await extractWikiClaims(pageId, selectedModelId, force);
            if (result.status === "SUCCEEDED") {
                message.success(t("wikiPages.claims.extractDone", { count: result.claimCount }));
            } else if (result.status === "ALREADY_DONE") {
                message.info(t("wikiPages.claims.extractAlreadyDone"));
            } else if (result.status === "FAILED" || result.status === "INVALID") {
                message.warning(t("wikiPages.claims.extractFailed", { status: result.status }));
            } else {
                // SKIPPED 等中间态
                message.info(t("wikiPages.claims.extractStatus", { status: result.status }));
            }
            void load(); // 无论成功失败都刷新，让用户看到最终状态
        } finally {
            setExtracting(false);
        }
    };

    const toggleExpand = (id: number) => {
        setExpandedIds((prev) => {
            const next = new Set(prev);
            if (next.has(id)) {
                next.delete(id);
            } else {
                next.add(id);
            }
            return next;
        });
    };

    const columns: TableProps<KnowledgeClaim>["columns"] = [
        {
            title: t("wikiPages.claims.claimText"),
            dataIndex: "claimText",
            key: "claimText",
            ellipsis: true,
            render: (text: string) => (
                <span title={text}>
                    {text.length > 100 ? `${text.slice(0, 100)}…` : text}
                </span>
            ),
        },
        {
            title: t("wikiPages.claims.claimType"),
            dataIndex: "claimType",
            key: "claimType",
            width: 100,
            render: (type: string | null) =>
                type ? <Badge status="processing" text={type} /> : "-",
        },
        {
            title: t("wikiPages.claims.evidenceCount"),
            dataIndex: "evidences",
            key: "evidences",
            width: 120,
            render: (evs: Evidence[]) =>
                evs.length === 0 ? (
                    <span style={{ color: "#888" }}>0 {t("wikiPages.claims.evidences")}</span>
                ) : (
                    <Badge count={evs.length} style={{ userSelect: "none" }} />
                ),
        },
    ];

    const expandedRowRender = (record: KnowledgeClaim) => {
        if (!record.evidences || record.evidences.length === 0) {
            return <Empty description={t("wikiPages.claims.noEvidence")} image={Empty.PRESENTED_IMAGE_SIMPLE} />;
        }
        return (
            <Descriptions size="small" column={2} bordered style={{ marginTop: 8 }}>
                <Descriptions.Item label={t("wikiPages.claims.sourceType")}>
                    {t("wikiPages.claims.sourceType")}
                </Descriptions.Item>
                <Descriptions.Item label={t("wikiPages.claims.sourceId")}>
                    {t("wikiPages.claims.sourceId")}
                </Descriptions.Item>
                {record.evidences.map((ev: Evidence) => (
                    <Descriptions.Item key={ev.id} label={ev.sourceType} span={2}>
                        {ev.sectionName && <>{ev.sectionName} / </>}
                        {ev.paragraphNo && <>{t("wikiPages.claims.paragraph")} {ev.paragraphNo} / </>}
                        {ev.sourceId && <code>{ev.sourceId}</code>}
                        {ev.content && (
                            <blockquote style={{ margin: "4px 0 0 0", fontSize: 12, color: "#555" }}>
                                {ev.content}
                            </blockquote>
                        )}
                    </Descriptions.Item>
                ))}
            </Descriptions>
        );
    };

    const modelOptions = models
        .filter((m) => m.usable)
        .map((m) => ({
            value: m.id,
            label: `${m.modelName} (${m.provider})`,
        }));

    const hasClaims = claims.length > 0;

    const extractControls = (
        <div style={{ marginBottom: 12, display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Select
                style={{ width: 240 }}
                placeholder="选择模型"
                value={selectedModelId}
                onChange={setSelectedModelId}
                options={modelOptions}
                allowClear
            />
            {hasClaims ? (
                <Popconfirm
                    title={t("wikiPages.claims.forceReExtractTitle")}
                    description={t("wikiPages.claims.forceReExtractDesc", { count: claims.length })}
                    okText={t("wikiPages.claims.forceReExtractOk")}
                    okButtonProps={{ danger: true }}
                    onConfirm={() => void handleExtract(true)}
                >
                    <Button
                        icon={<ReloadOutlined />}
                        loading={extracting}
                        danger
                        disabled={!selectedModelId}
                    >
                        {t("wikiPages.claims.reExtract")}
                    </Button>
                </Popconfirm>
            ) : (
                <Button
                    icon={<ReloadOutlined />}
                    loading={extracting}
                    onClick={() => void handleExtract(false)}
                    disabled={!selectedModelId}
                >
                    {t("wikiPages.claims.reExtract")}
                </Button>
            )}
        </div>
    );

    if (!loading && claims.length === 0) {
        return (
            <div style={{ padding: "16px 0" }}>
                {extractControls}
                <Empty description={t("wikiPages.claims.empty")} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            </div>
        );
    }

    return (
        <>
            {extractControls}
            <Table<KnowledgeClaim>
                rowKey="id"
                size="small"
                loading={loading}
                columns={columns}
                dataSource={claims}
                expandable={{
                    expandedRowRender,
                    expandedRowKeys: Array.from(expandedIds),
                    onExpand: (_expanded, record) => toggleExpand(record.id),
                }}
                pagination={false}
                locale={{ emptyText: t("wikiPages.claims.empty") }}
            />
        </>
    );
}
