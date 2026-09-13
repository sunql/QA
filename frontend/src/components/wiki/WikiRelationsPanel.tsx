/**
 * 知识关系（KnowledgeRelation）列表。
 * Tab 分组：待审核（autoDetected && !confirmed）| 已确认
 * 支持 confirm/reject/discover 操作。
 */

import { useCallback, useState } from "react";
import { App, Button, Empty, Popconfirm, Space, Table, Tabs, Tag } from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";
import { useTranslation } from "../../i18n";
import {
    confirmWikiRelation,
    discoverWikiRelations,
    listWikiRelations,
    rejectWikiRelation,
} from "../../api/wikiPages";
import type { WikiRelation } from "../../types/wikiPages";

interface WikiRelationsPanelProps {
    pageId: string;
}

export default function WikiRelationsPanel({ pageId }: WikiRelationsPanelProps) {
    const { t } = useTranslation();
    const { message } = App.useApp();
    const [relations, setRelations] = useState<WikiRelation[]>([]);
    const [loading, setLoading] = useState(false);
    const [activeTab, setActiveTab] = useState<"pending" | "confirmed">("pending");

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const data = await listWikiRelations(pageId, false);
            setRelations(data);
        } finally {
            setLoading(false);
        }
    }, [pageId]);

    void load;

    const pending = relations.filter((r) => r.autoDetected && !r.confirmed && !r.rejectedAt);
    const confirmed = relations.filter((r) => r.confirmed);

    const handleConfirm = async (relationId: number) => {
        try {
            await confirmWikiRelation(relationId);
            void load();
            message.success(t("wikiPages.relations.confirmSuccess"));
        } catch {
            // 拦截器已 toast
        }
    };

    const handleReject = async (relationId: number) => {
        try {
            await rejectWikiRelation(relationId);
            void load();
            message.success(t("wikiPages.relations.rejectSuccess"));
        } catch {
            // 拦截器已 toast
        }
    };

    const handleDiscover = async () => {
        try {
            const res = await discoverWikiRelations(pageId);
            void load();
            message.info(
                t("wikiPages.relations.discovered", { count: res.total }),
            );
        } catch {
            // 拦截器已 toast
        }
    };

    const baseColumns: TableProps<WikiRelation>["columns"] = [
        {
            title: t("wikiPages.relations.relationType"),
            dataIndex: "relationType",
            key: "relationType",
            width: 120,
            render: (v: string) => <Tag color="blue">{v}</Tag>,
        },
        {
            title: t("wikiPages.relations.downstreamType"),
            dataIndex: "downstreamType",
            key: "downstreamType",
            width: 140,
            render: (v: string) => <Tag>{v}</Tag>,
        },
        {
            title: t("wikiPages.relations.downstreamId"),
            dataIndex: "downstreamId",
            key: "downstreamId",
            ellipsis: true,
            render: (id: string) => <code style={{ fontSize: 12 }}>{id}</code>,
        },
        {
            title: t("wikiPages.relations.confidence"),
            dataIndex: "confidence",
            key: "confidence",
            width: 80,
            render: (v: number | null) =>
                v !== null ? `${(v * 100).toFixed(0)}%` : "-",
        },
    ];

    const pendingColumns: ColumnsType<WikiRelation> = [
        ...baseColumns,
        {
            title: t("wikiPages.relations.actions"),
            key: "actions",
            width: 120,
            render: (_: unknown, record: WikiRelation) => (
                <Space.Compact size="small">
                    <Popconfirm
                        title={t("wikiPages.relations.confirm")}
                        onConfirm={() => handleConfirm(record.id)}
                    >
                        <Button type="link" size="small">
                            {t("wikiPages.relations.confirm")}
                        </Button>
                    </Popconfirm>
                    <Popconfirm
                        title={t("wikiPages.relations.reject")}
                        onConfirm={() => handleReject(record.id)}
                    >
                        <Button type="link" danger size="small">
                            {t("wikiPages.relations.reject")}
                        </Button>
                    </Popconfirm>
                </Space.Compact>
            ),
        },
    ];

    const tabItems = [
        {
            key: "pending",
            label: `${t("wikiPages.relations.pending")} (${pending.length})`,
            children: (
                <>
                    <div style={{ marginBottom: 12, textAlign: "right" }}>
                        <Button size="small" onClick={handleDiscover}>
                            {t("wikiPages.relations.discover")}
                        </Button>
                    </div>
                    {pending.length === 0 ? (
                        <Empty
                            description={t("wikiPages.relations.empty")}
                            image={Empty.PRESENTED_IMAGE_SIMPLE}
                        />
                    ) : (
                        <Table
                            rowKey="id"
                            size="small"
                            loading={loading}
                            columns={pendingColumns}
                            dataSource={pending}
                            pagination={false}
                        />
                    )}
                </>
            ),
        },
        {
            key: "confirmed",
            label: `${t("wikiPages.relations.confirmed")} (${confirmed.length})`,
            children: (
                confirmed.length === 0 ? (
                    <Empty
                        description={t("wikiPages.relations.empty")}
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                    />
                ) : (
                    <Table
                        rowKey="id"
                        size="small"
                        columns={baseColumns}
                        dataSource={confirmed}
                        pagination={false}
                    />
                )
            ),
        },
    ];

    return (
        <Tabs
            activeKey={activeTab}
            onChange={(k) => setActiveTab(k as "pending" | "confirmed")}
            items={tabItems}
        />
    );
}
