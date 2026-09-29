/**
 * 知识图谱页（Phase 2）：Louvain 社区 + 4-Signal 相关性的可视化。
 *
 * 布局：左侧 ECharts 力导向图（节点按社区着色、大小映射度数、边宽映射
 * 相关性得分），右侧社区列表面板（内聚度 + top 条目）。
 *
 * 「重算社区」是幂等全量重算，结果被限流（与 detect/discover 同档），
 * 前端用 loading 禁连点。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
    Alert,
    App,
    Button,
    Card,
    Checkbox,
    Empty,
    List,
    Space,
    Spin,
    Tag,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import ReactECharts from "echarts-for-react";
import { useTranslation } from "../i18n";
import {
    getGraphView,
    listGraphCommunities,
    recomputeGraphCommunities,
} from "../api/wikiGraph";
import GraphInsightsPanel from "../components/wiki/GraphInsightsPanel";
import type { GraphView, KnowledgeCommunity } from "../types/wikiGraph";

/** 社区着色盘：按 communityKey 序号取色，无社区的孤立/未入社区节点用灰色 */
const COMMUNITY_PALETTE = [
    "#5470c6", "#91cc75", "#fac858", "#ee6666", "#73c0de",
    "#3ba272", "#fc8452", "#9a60b4", "#ea7ccc", "#48b0d6",
];
const NO_COMMUNITY_COLOR = "#bfbfbf";

/** 边得分 → 线宽。得分上限参考：直连3 + 同源4 + AA(≈2)×1.5 + 同维1 ≈ 11 */
const MAX_EDGE_SCORE = 12;

function communityColor(communityKey: string | null): string {
    if (!communityKey) return NO_COMMUNITY_COLOR;
    const seq = parseInt(communityKey.replace(/^C/, ""), 10);
    return COMMUNITY_PALETTE[(seq - 1) % COMMUNITY_PALETTE.length];
}

function buildChartOption(
    view: GraphView,
    communities: KnowledgeCommunity[],
    t: (key: string, params?: Readonly<Record<string, string | number | boolean>>) => string,
): Record<string, unknown> {
    const categoryNames = [
        ...communities.map((c) => c.communityKey),
        "", // 无社区节点
    ];
    return {
        tooltip: {
            trigger: "item",
            formatter: (params: {
                dataType?: string;
                data?: { name?: string; value?: string; lineStyle?: { _score?: number } };
            }) => {
                if (params.dataType === "edge") {
                    const score = (params.data as { _score?: number })?._score;
                    return t("wikiGraph.edgeTooltip", { score: score ?? 0 });
                }
                return params.data?.name ?? "";
            },
        },
        legend: {
            data: communities.map((c) => c.communityKey),
            textStyle: { fontSize: 11 },
            top: 8,
            left: 8,
            type: "scroll",
        },
        series: [
            {
                type: "graph",
                layout: "force",
                roam: true,
                draggable: true,
                force: { repulsion: 260, edgeLength: 110 },
                categories: categoryNames.map((key) => ({
                    name: key,
                    itemStyle: { color: key ? communityColor(key) : NO_COMMUNITY_COLOR },
                })),
                nodes: view.nodes.map((n) => ({
                    id: n.pageId,
                    name: n.title ?? n.pageId,
                    category: n.communityKey ?? "",
                    // 度数映射节点大小：8 ~ 36
                    symbolSize: Math.min(36, 8 + n.degree * 4),
                })),
                links: view.edges.map((e) => ({
                    source: e.source,
                    target: e.target,
                    _score: e.score,
                    lineStyle: {
                        width: 0.5 + (Math.min(e.score, MAX_EDGE_SCORE) / MAX_EDGE_SCORE) * 4,
                        opacity: 0.35 + (Math.min(e.score, MAX_EDGE_SCORE) / MAX_EDGE_SCORE) * 0.55,
                    },
                })),
                label: { show: true, position: "right", fontSize: 10 },
                emphasis: { focus: "adjacency", lineStyle: { width: 4 } },
            },
        ],
    };
}

export default function AdminWikiGraphPage() {
    const { t } = useTranslation();
    const { message } = App.useApp();
    const [view, setView] = useState<GraphView | null>(null);
    const [communities, setCommunities] = useState<KnowledgeCommunity[]>([]);
    const [loading, setLoading] = useState(false);
    const [recomputing, setRecomputing] = useState(false);
    const [includeIsolated, setIncludeIsolated] = useState(false);

    const load = useCallback(async (withIsolated: boolean) => {
        setLoading(true);
        try {
            const [graphView, communityList] = await Promise.all([
                getGraphView(withIsolated),
                listGraphCommunities(),
            ]);
            setView(graphView);
            setCommunities(communityList);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load(includeIsolated);
    }, [includeIsolated, load]);

    const handleRecompute = async () => {
        setRecomputing(true);
        try {
            const result = await recomputeGraphCommunities();
            message.success(
                t("wikiGraph.recomputeDone", {
                    communities: result.communityCount,
                    pages: result.memberPageCount,
                }),
            );
            await load(includeIsolated);
        } finally {
            setRecomputing(false);
        }
    };

    const option = useMemo(
        () => (view ? buildChartOption(view, communities, t) : null),
        [view, communities, t],
    );

    return (
        <div style={{ padding: 24 }}>
            <Space style={{ marginBottom: 16, width: "100%", justifyContent: "space-between" }}>
                <h2 style={{ margin: 0 }}>{t("wikiGraph.title")}</h2>
                <Space>
                    <Checkbox
                        checked={includeIsolated}
                        onChange={(e) => setIncludeIsolated(e.target.checked)}
                    >
                        {t("wikiGraph.includeIsolated")}
                    </Checkbox>
                    <Button
                        type="primary"
                        icon={<ReloadOutlined />}
                        loading={recomputing}
                        onClick={() => void handleRecompute()}
                    >
                        {t("wikiGraph.recompute")}
                    </Button>
                </Space>
            </Space>

            {view?.truncated && (
                <Alert
                    type="info"
                    showIcon
                    message={t("wikiGraph.truncatedHint")}
                    style={{ marginBottom: 16 }}
                />
            )}

            <div style={{ display: "flex", gap: 16 }}>
                <Card style={{ flex: 1, minWidth: 0 }} styles={{ body: { padding: 8 } }}>
                    <Spin spinning={loading}>
                        {view && view.nodes.length > 0 && option ? (
                            <ReactECharts option={option} style={{ height: 640 }} notMerge />
                        ) : (
                            !loading && (
                                <Empty
                                    description={t("wikiGraph.empty")}
                                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                                    style={{ padding: "120px 0" }}
                                />
                            )
                        )}
                    </Spin>
                </Card>

                <Card title={t("wikiGraph.communities")} style={{ width: 320 }}>
                    <List
                        size="small"
                        dataSource={communities}
                        locale={{ emptyText: t("wikiGraph.noCommunities") }}
                        renderItem={(c) => (
                            <List.Item>
                                <div style={{ width: "100%" }}>
                                    <Space>
                                        <Tag color={communityColor(c.communityKey)}>
                                            {c.communityKey}
                                        </Tag>
                                        <span style={{ fontWeight: 500 }}>{c.name}</span>
                                    </Space>
                                    <div style={{ color: "#888", fontSize: 12, marginTop: 4 }}>
                                        {t("wikiGraph.communityMeta", {
                                            pages: c.pageCount,
                                            cohesion: c.cohesionScore,
                                        })}
                                    </div>
                                </div>
                            </List.Item>
                        )}
                    />
                </Card>
            </div>

            <Card style={{ marginTop: 16 }}>
                <GraphInsightsPanel
                    communities={communities.map((c) => ({
                        communityKey: c.communityKey,
                        name: c.name,
                    }))}
                />
            </Card>
        </div>
    );
}
