/**
 * Graph Insights 面板（Phase 3 + Phase 5.5）。
 *
 * 三个 Tab：意外连接 / 知识缺口 / 桥接节点，每条带 LLM 解读（如已生成）。
 * 重算由「重算洞察」按钮触发（与社区重算各自独立）。
 *
 * Phase 5.5：知识缺口 tab 的每条 gap 加上「操作入口」按钮 —— 三类 gap 各
 * 一个 Modal：
 * - MISSING_DIMENSION → 「重分类」预览 + 用户确认 → PATCH dimension
 * - ISOLATED_PAGE    → 「查找关联」预览 + 用户确认 → POST /relations/discover
 * - SPARSE_COMMUNITY → 「建议主题」预览 + 用户确认 → PATCH community topic
 *
 * 两步预览原则：LLM 调一次但**不写库**；用户在 Modal 看一眼再决定。避免
 * 自动污染知识网络 —— 用户看不懂的 gap 不会因为系统自动「修一下」反而
 * 变得更糟。
 */

import { useCallback, useEffect, useState } from "react";
import {
    Alert,
    App,
    Button,
    Empty,
    List,
    Modal,
    Space,
    Spin,
    Tabs,
    Tag,
    Tooltip,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { useTranslation } from "../../i18n";
import {
    getGraphInsights,
    previewCommunityTopic,
    rescanGraphInsights,
    updateCommunityTopic,
} from "../../api/wikiGraph";
import {
    previewWikiPageClassify,
    suggestWikiRelations,
    discoverWikiRelations,
    updateWikiPage,
} from "../../api/wikiPages";
import { showMessageError } from "../../api/client";
import type {
    BridgeNode as BridgeNodeItem,
    GraphInsights,
    KnowledgeGap,
    SurprisingConnection,
} from "../../types/wikiGraph";
import type { WikiClassifyPreview, WikiRelationSuggestCandidate } from "../../api/wikiPages";
import type { Vars } from "../../i18n/types";

interface GraphInsightsPanelProps {
    communities: { communityKey: string; name: string }[];
}

// ---- Modal target：用一个 discriminated union 表达「现在打开的是哪类 gap」 ----

type ModalTarget =
    | { kind: "MISSING_DIMENSION"; pageId: string; title: string }
    | { kind: "ISOLATED_PAGE"; pageId: string; title: string }
    | { kind: "SPARSE_COMMUNITY"; communityKey: string; communityName: string }
    | null;

function gapKindLabel(kind: KnowledgeGap["kind"], t: (k: string) => string): string {
    if (kind === "ISOLATED_PAGE") return t("wikiGraph.insights.gapIsolated");
    if (kind === "MISSING_DIMENSION") return t("wikiGraph.insights.gapMissingDimension");
    return t("wikiGraph.insights.gapSparse");
}

function Explanation({ text, t }: { text: string; t: (k: string) => string }) {
    if (text) {
        return (
            <div style={{ marginTop: 6, color: "#555", fontSize: 13, lineHeight: 1.6 }}>
                {text}
            </div>
        );
    }
    return (
        <div style={{ marginTop: 6, color: "#bbb", fontSize: 12, fontStyle: "italic" }}>
            {t("wikiGraph.insights.noExplanation")}
        </div>
    );
}

export default function GraphInsightsPanel({ communities }: GraphInsightsPanelProps) {
    const { t } = useTranslation();
    const { message } = App.useApp();
    const [data, setData] = useState<GraphInsights | null>(null);
    const [loading, setLoading] = useState(false);
    const [rescanning, setRescanning] = useState(false);
    const [target, setTarget] = useState<ModalTarget>(null);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            setData(await getGraphInsights());
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    const handleRescan = async () => {
        setRescanning(true);
        try {
            const result = await rescanGraphInsights();
            message.success(
                t("wikiGraph.insights.rescanDone", {
                    surprising: result.surprisingCount,
                    gaps: result.gapCount,
                    bridges: result.bridgeCount,
                    failures: result.explanationFailures,
                }),
            );
            await load();
        } finally {
            setRescanning(false);
        }
    };

    const communityName = (key: string | null): string => {
        if (!key) return "—";
        const hit = communities.find((c) => c.communityKey === key);
        return hit?.name ?? key;
    };

    const renderSurprising = (items: SurprisingConnection[]) => {
        if (items.length === 0) {
            return <Empty description="无意外连接" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
        }
        return (
            <List
                size="small"
                dataSource={items}
                rowKey={(c) => `SURPRISING|${c.sourcePageId}|${c.targetPageId}`}
                renderItem={(c) => (
                    <List.Item>
                        <div style={{ width: "100%" }}>
                            <Space wrap>
                                <Tag color="orange">{c.relationType}</Tag>
                                <span style={{ color: "#888" }}>
                                    {t("wikiGraph.insights.surprisingSource", {
                                        comm: `${c.sourceCommunity ?? "—"} ↔ ${c.targetCommunity ?? "—"}`,
                                        dim: `${c.sourceDimension ?? "—"} ↔ ${c.targetDimension ?? "—"}`,
                                    })}
                                </span>
                            </Space>
                            <div style={{ marginTop: 4, fontWeight: 500 }}>{c.headline}</div>
                            <Explanation text={c.explanation} t={t} />
                        </div>
                    </List.Item>
                )}
            />
        );
    };

    // 每条 gap 的右侧「操作入口」按钮 —— 按 kind 决定按钮文案与点击行为
    const renderGapAction = (g: KnowledgeGap): React.ReactNode => {
        if (g.kind === "MISSING_DIMENSION" && g.pageId) {
            return (
                <Button
                    size="small"
                    type="link"
                    onClick={() => setTarget({
                        kind: "MISSING_DIMENSION",
                        pageId: g.pageId!,
                        title: g.title ?? g.pageId!,
                    })}
                >
                    {t("wikiGraph.insights.actionReclassify")}
                </Button>
            );
        }
        if (g.kind === "ISOLATED_PAGE" && g.pageId) {
            return (
                <Button
                    size="small"
                    type="link"
                    onClick={() => setTarget({
                        kind: "ISOLATED_PAGE",
                        pageId: g.pageId!,
                        title: g.title ?? g.pageId!,
                    })}
                >
                    {t("wikiGraph.insights.actionFindRelations")}
                </Button>
            );
        }
        if (g.kind === "SPARSE_COMMUNITY" && g.communityKey) {
            return (
                <Button
                    size="small"
                    type="link"
                    onClick={() => setTarget({
                        kind: "SPARSE_COMMUNITY",
                        communityKey: g.communityKey!,
                        communityName: g.communityName ?? g.communityKey!,
                    })}
                >
                    {t("wikiGraph.insights.actionSuggestTopic")}
                </Button>
            );
        }
        return null;
    };

    const renderGaps = (items: KnowledgeGap[]) => {
        if (items.length === 0) {
            return <Empty description="无知识缺口" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
        }
        return (
            <List
                size="small"
                dataSource={items}
                rowKey={(g) => `${g.kind}|${g.pageId ?? ""}|${g.communityKey ?? ""}`}
                renderItem={(g) => (
                    <List.Item
                        actions={[renderGapAction(g)].filter(Boolean) as React.ReactElement[]}
                    >
                        <div style={{ width: "100%" }}>
                            <Space>
                                <Tag color="red">{gapKindLabel(g.kind, t)}</Tag>
                                {g.degree !== null && g.degree !== undefined && (
                                    <Tag>度={g.degree}</Tag>
                                )}
                                {g.cohesionScore !== null && g.cohesionScore !== undefined && (
                                    <Tag>内聚度={g.cohesionScore}</Tag>
                                )}
                                {g.topic && <Tag color="green">{g.topic}</Tag>}
                            </Space>
                            <div style={{ marginTop: 4, fontWeight: 500 }}>{g.headline}</div>
                        </div>
                    </List.Item>
                )}
            />
        );
    };

    const renderBridges = (items: BridgeNodeItem[]) => {
        if (items.length === 0) {
            return <Empty description="无桥接节点" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
        }
        return (
            <List
                size="small"
                dataSource={items}
                rowKey={(b) => `BRIDGE|${b.pageId}`}
                renderItem={(b) => (
                    <List.Item>
                        <div style={{ width: "100%" }}>
                            <Space wrap>
                                {b.communities.map((c) => (
                                    <Tooltip key={c} title={communityName(c)}>
                                        <Tag color="purple">{c}</Tag>
                                    </Tooltip>
                                ))}
                                <Tag>度={b.degree}</Tag>
                            </Space>
                            <div style={{ marginTop: 4, fontWeight: 500 }}>{b.title}</div>
                            <Explanation text={b.explanation} t={t} />
                        </div>
                    </List.Item>
                )}
            />
        );
    };

    const items = [
        {
            key: "surprising",
            label: `${t("wikiGraph.insights.tabSurprising")} (${data?.surprisingConnections.length ?? 0})`,
            children: renderSurprising(data?.surprisingConnections ?? []),
        },
        {
            key: "gaps",
            label: `${t("wikiGraph.insights.tabGaps")} (${data?.knowledgeGaps.length ?? 0})`,
            children: renderGaps(data?.knowledgeGaps ?? []),
        },
        {
            key: "bridges",
            label: `${t("wikiGraph.insights.tabBridges")} (${data?.bridgeNodes.length ?? 0})`,
            children: renderBridges(data?.bridgeNodes ?? []),
        },
    ];

    const never = data && (
        data.surprisingConnections.length === 0
        && data.knowledgeGaps.length === 0
        && data.bridgeNodes.length === 0
        && !data.scannedAt
    );

    return (
        <div>
            <Space style={{ marginBottom: 12, width: "100%", justifyContent: "space-between" }}>
                <h3 style={{ margin: 0 }}>{t("wikiGraph.insights.title")}</h3>
                <Button
                    icon={<ReloadOutlined />}
                    loading={rescanning}
                    onClick={() => void handleRescan()}
                >
                    {rescanning
                        ? t("wikiGraph.insights.rescanning")
                        : t("wikiGraph.insights.rescan")}
                </Button>
            </Space>

            <Spin spinning={loading}>
                {never && (
                    <Alert
                        type="info"
                        showIcon
                        message={t("wikiGraph.insights.never")}
                        style={{ marginBottom: 12 }}
                    />
                )}
                <Tabs items={items} />
            </Spin>

            {target && (
                <GapActionModal
                    target={target}
                    onClose={() => setTarget(null)}
                    onConfirmed={async () => {
                        setTarget(null);
                        await load();
                    }}
                    t={t}
                    onError={(msg) => showMessageError(msg)}
                    onSuccess={(msg) => message.success(msg)}
                />
            )}
        </div>
    );
}

// ---- 缺口操作 Modal ----

interface GapActionModalProps {
    target: Exclude<ModalTarget, null>;
    onClose: () => void;
    onConfirmed: () => void | Promise<void>;
    t: (k: string, vars?: Vars) => string;
    onError: (msg: string) => void;
    onSuccess: (msg: string) => void;
}

function GapActionModal({
    target, onClose, onConfirmed, t, onError, onSuccess,
}: GapActionModalProps) {
    const [previewLoading, setPreviewLoading] = useState(false);
    const [confirming, setConfirming] = useState(false);
    // 三类 preview 的局部 state（互斥）
    const [classifyPreview, setClassifyPreview] = useState<WikiClassifyPreview | null>(null);
    const [selectedDimension, setSelectedDimension] = useState<string | null>(null);
    const [relationsPreview, setRelationsPreview] = useState<WikiRelationSuggestCandidate[] | null>(null);
    const [topicPreview, setTopicPreview] = useState<{ topic: string; titles: string[] } | null>(null);

    const title = (() => {
        if (target.kind === "SPARSE_COMMUNITY") {
            return `${t("wikiGraph.insights.previewTitleTopic")}：${target.communityName}`;
        }
        return `${t(
            target.kind === "MISSING_DIMENSION"
                ? "wikiGraph.insights.previewTitleClassify"
                : "wikiGraph.insights.previewTitleRelations",
        )}：${target.title}`;
    })();

    // 进入 Modal 时拉一次预览
    useEffect(() => {
        void (async () => {
            setPreviewLoading(true);
            try {
                if (target.kind === "MISSING_DIMENSION") {
                    const p = await previewWikiPageClassify(target.pageId);
                    setClassifyPreview(p);
                    setSelectedDimension(p.primary);
                } else if (target.kind === "ISOLATED_PAGE") {
                    const r = await suggestWikiRelations(target.pageId);
                    setRelationsPreview(r.candidates);
                } else {
                    const tp = await previewCommunityTopic(target.communityKey);
                    setTopicPreview({ topic: tp.topic, titles: tp.pageTitles });
                }
            } catch (err: unknown) {
                onError((err as { detail?: string })?.detail ?? "预览失败");
                onClose();
            } finally {
                setPreviewLoading(false);
            }
        })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [target]);

    const handleConfirm = async () => {
        setConfirming(true);
        try {
            if (target.kind === "MISSING_DIMENSION") {
                if (!selectedDimension) return;
                await updateWikiPage(target.pageId, {
                    dimension: selectedDimension as never,
                    autoClassification: classifyPreview
                        ? {
                            primary: classifyPreview.primary,
                            confidence: classifyPreview.confidence,
                            alternatives: classifyPreview.alternatives,
                            reason: classifyPreview.reason,
                        }
                        : null,
                });
                onSuccess(t("wikiGraph.insights.successReclassify", { dimension: selectedDimension }));
            } else if (target.kind === "ISOLATED_PAGE") {
                if (!relationsPreview || relationsPreview.length === 0) return;
                await discoverWikiRelations(target.pageId);
                onSuccess(t("wikiGraph.insights.successRelations", { count: relationsPreview.length }));
            } else {
                if (!topicPreview?.topic) return;
                await updateCommunityTopic(target.communityKey, topicPreview.topic);
                onSuccess(t("wikiGraph.insights.successTopic", { topic: topicPreview.topic }));
            }
            await onConfirmed();
        } catch (err: unknown) {
            onError((err as { detail?: string })?.detail ?? "操作失败");
        } finally {
            setConfirming(false);
        }
    };

    return (
        <Modal
            open
            title={title}
            onCancel={onClose}
            onOk={() => void handleConfirm()}
            okButtonProps={{
                loading: confirming,
                disabled:
                    previewLoading
                    || (target.kind === "MISSING_DIMENSION" && !selectedDimension)
                    || (target.kind === "ISOLATED_PAGE" && (!relationsPreview || relationsPreview.length === 0))
                    || (target.kind === "SPARSE_COMMUNITY" && !topicPreview?.topic),
            }}
            cancelText={t("wikiGraph.insights.cancel")}
            okText={t("wikiGraph.insights.confirm")}
        >
            <Spin spinning={previewLoading}>
                {target.kind === "MISSING_DIMENSION" && classifyPreview && (
                    <div>
                        {classifyPreview.primary ? (
                            <>
                                <p>
                                    {t("wikiGraph.insights.primarySuggestion", {
                                        primary: classifyPreview.primary,
                                    })}
                                </p>
                                <p style={{ color: "#888" }}>
                                    {t("wikiGraph.insights.confidence", {
                                        value: classifyPreview.confidence.toFixed(2),
                                    })}
                                </p>
                                {classifyPreview.alternatives.length > 0 && (
                                    <p style={{ color: "#888" }}>
                                        {t("wikiGraph.insights.alternatives", {
                                            list: classifyPreview.alternatives.join("、"),
                                        })}
                                    </p>
                                )}
                                <div style={{ marginTop: 8 }}>
                                    {[
                                        classifyPreview.primary,
                                        ...classifyPreview.alternatives,
                                    ]
                                        .filter((d): d is string => Boolean(d))
                                        .map((d) => (
                                            <Button
                                                key={d}
                                                type={selectedDimension === d ? "primary" : "default"}
                                                onClick={() => setSelectedDimension(d)}
                                                style={{ marginRight: 8, marginBottom: 8 }}
                                            >
                                                {d}
                                            </Button>
                                        ))}
                                </div>
                            </>
                        ) : (
                            <Alert
                                type="warning"
                                showIcon
                                message={t("wikiGraph.insights.noSuggestion")}
                            />
                        )}
                    </div>
                )}

                {target.kind === "ISOLATED_PAGE" && relationsPreview && (
                    <div>
                        {relationsPreview.length === 0 ? (
                            <Alert
                                type="info"
                                showIcon
                                message={t("wikiGraph.insights.emptyCandidates")}
                            />
                        ) : (
                            <List
                                size="small"
                                dataSource={relationsPreview}
                                rowKey={(c) => `${c.relationType}|${c.downstreamId}`}
                                renderItem={(c) => (
                                    <List.Item>
                                        <div style={{ width: "100%" }}>
                                            <Space>
                                                <Tag color="blue">{c.relationType}</Tag>
                                                <span>{c.downstreamTitle || c.downstreamId}</span>
                                                <Tag>{c.confidence.toFixed(2)}</Tag>
                                            </Space>
                                            <div style={{ color: "#888", fontSize: 12 }}>
                                                {c.reason}
                                            </div>
                                        </div>
                                    </List.Item>
                                )}
                            />
                        )}
                    </div>
                )}

                {target.kind === "SPARSE_COMMUNITY" && topicPreview && (
                    <div>
                        {topicPreview.topic ? (
                            <>
                                <p style={{ fontSize: 16, fontWeight: 500 }}>
                                    {t("wikiGraph.insights.primarySuggestion", {
                                        primary: topicPreview.topic,
                                    })}
                                </p>
                                {topicPreview.titles.length > 0 && (
                                    <>
                                        <p style={{ color: "#888" }}>
                                            {t("wikiGraph.insights.basedOnTitles", {
                                                count: topicPreview.titles.length,
                                            })}
                                        </p>
                                        <ul style={{ color: "#666", fontSize: 12 }}>
                                            {topicPreview.titles.slice(0, 10).map((t2) => (
                                                <li key={t2}>{t2}</li>
                                            ))}
                                        </ul>
                                    </>
                                )}
                            </>
                        ) : (
                            <Alert
                                type="warning"
                                showIcon
                                message={t("wikiGraph.insights.noSuggestion")}
                            />
                        )}
                    </div>
                )}
            </Spin>
        </Modal>
    );
}
