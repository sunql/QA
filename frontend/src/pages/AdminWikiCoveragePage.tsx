/**
 * 覆盖度看板（feat-wiki-knowledge M7，机制 6）。
 *
 * 看板回答的是「**哪些业务对象还没有可用的知识**」——所以主视觉是缺口清单，
 * 不是那堆绿色的格子。绿色只说明「这块有人写了且审过了」，红/黄才是待办。
 *
 * 数据来源说明：矩阵读的是**上一次刷新**的快照（每次打开都全量重算会让一个
 * 只读页面变成写操作），所以「刷新覆盖度」是一个显式动作，刷新后才会看到
 * 软删的类、摘掉的域标注从矩阵里消失（这是自愈，不是新增）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
    Alert,
    Button,
    Card,
    Col,
    Modal,
    Popconfirm,
    Row,
    Select,
    Space,
    Statistic,
    Table,
    Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import {
    createDomainMapping,
    deleteDomainMapping,
    getCoverageOverview,
    listCoverageDomains,
    listDomainMappings,
    refreshCoverage,
} from "../api/wikiCoverage";
import { listClasses } from "../api/ontology";
import {
    UNASSIGNED,
    type ClassDomainMapping,
    type CoverageGap,
    type CoverageOverview,
} from "../types/wikiCoverage";

const STATUS_COLOR: Record<string, string> = {
    COMPLETE: "green",
    PARTIAL: "gold",
    OUTDATED: "orange",
    MISSING: "red",
};

/** 默认缺口上限。看板不是审计报表，给个够看的量就够，全量靠接口的 gapLimit。 */
const DEFAULT_GAP_LIMIT = 50;

export default function AdminWikiCoveragePage() {
    const { t } = useTranslation();

    const [overview, setOverview] = useState<CoverageOverview | null>(null);
    const [loading, setLoading] = useState(false);
    const [refreshing, setRefreshing] = useState(false);

    const [domains, setDomains] = useState<string[]>([]);
    const [mappings, setMappings] = useState<ClassDomainMapping[]>([]);
    const [classOptions, setClassOptions] = useState<
        { value: number; label: string }[]
    >([]);

    const [addOpen, setAddOpen] = useState(false);
    const [newClassId, setNewClassId] = useState<number | undefined>();
    const [newDomain, setNewDomain] = useState<string | undefined>();
    const [errorMsg, setErrorMsg] = useState<string | null>(null);

    /** 三块数据一次取齐（后端合成一个响应正是为此，避免三份数据来自不同时刻） */
    const fetchAll = useCallback(async () => {
        setLoading(true);
        try {
            const [ov, domainList, mappingList] = await Promise.all([
                getCoverageOverview({ gapLimit: DEFAULT_GAP_LIMIT }),
                listCoverageDomains(),
                listDomainMappings(),
            ]);
            setOverview(ov);
            setDomains(domainList);
            setMappings(mappingList);
        } catch {
            // 拦截器已 toast 后端原因；保留旧数据，避免误读成「覆盖度全没了」
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void fetchAll();
    }, [fetchAll]);

    const handleRefresh = useCallback(async () => {
        setRefreshing(true);
        setErrorMsg(null);
        try {
            const result = await refreshCoverage();
            // 刷新结果本身是信息：removedCount > 0 说明有格子被清理掉了
            // （类软删 / 域标注摘除），这正是「派生快照」的自愈行为。
            setErrorMsg(
                t("wikiCoverage.refreshDone", {
                    cells: result.cellCount,
                    classes: result.classCount,
                    mapped: result.classCount - result.unmappedClassCount,
                    removed: result.removedCount,
                }),
            );
            await fetchAll();
        } catch {
            setErrorMsg(t("wikiCoverage.errors.refreshFailed"));
        } finally {
            setRefreshing(false);
        }
    }, [fetchAll, t]);

    const openAdd = useCallback(async () => {
        setAddOpen(true);
        setErrorMsg(null);
        try {
            const classes = await listClasses();
            setClassOptions(
                classes.map((c) => ({
                    value: c.id,
                    label: `${c.className}${c.classAlias ? ` (${c.classAlias})` : ""}`,
                })),
            );
        } catch {
            // 类列表拉不到时下拉为空，用户能看出失败并可重开弹窗重试
        }
    }, []);

    const handleAdd = useCallback(async () => {
        if (newClassId === undefined || newDomain === undefined) {
            return;
        }
        try {
            await createDomainMapping(newClassId, newDomain);
            setAddOpen(false);
            setNewClassId(undefined);
            setNewDomain(undefined);
            await fetchAll();
        } catch {
            // 422（域为空/超长）拦截器已提示；保留弹窗让用户改
        }
    }, [newClassId, newDomain, fetchAll]);

    const handleRemove = useCallback(
        async (record: ClassDomainMapping) => {
            setErrorMsg(null);
            try {
                await deleteDomainMapping(record.ontologyClassId, record.domain);
                await fetchAll();
            } catch (e) {
                const err = e as Error & { status?: number };
                // 404 = 这条标注已经不在了（别人刚摘掉）。是可预期的并发结果，
                // 不是故障——刷新即可对齐，不该报成「删除失败」吓人。
                setErrorMsg(
                    err.status === 404
                        ? t("wikiCoverage.errors.mappingGone")
                        : t("wikiCoverage.errors.removeFailed"),
                );
                await fetchAll();
            }
        },
        [fetchAll, t],
    );

    const gapColumns: ColumnsType<CoverageGap> = useMemo(
        () => [
            {
                title: t("wikiCoverage.columns.dimension"),
                dataIndex: "dimension",
                key: "dimension",
                width: 140,
                render: (v: string) => (
                    <Tag color="geekblue">{t(`wikiPages.dimensions.${v}`)}</Tag>
                ),
            },
            {
                title: t("wikiCoverage.columns.className"),
                dataIndex: "className",
                key: "className",
                render: (v: string | null) => v ?? t("common.dash"),
            },
            {
                title: t("wikiCoverage.columns.domain"),
                dataIndex: "domain",
                key: "domain",
                width: 150,
                render: (v: string) =>
                    v === UNASSIGNED ? (
                        <Tag color="red">{t("wikiCoverage.unassigned")}</Tag>
                    ) : (
                        v
                    ),
            },
            {
                title: t("wikiCoverage.columns.status"),
                dataIndex: "status",
                key: "status",
                width: 110,
                render: (v: string) => (
                    <Tag color={STATUS_COLOR[v] ?? "default"}>
                        {t(`wikiCoverage.statuses.${v}`)}
                    </Tag>
                ),
            },
            {
                title: t("wikiCoverage.columns.pages"),
                key: "pages",
                width: 140,
                render: (_, record) =>
                    t("wikiCoverage.pageCounts", {
                        total: record.pageCount,
                        approved: record.approvedCount,
                    }),
            },
        ],
        [t],
    );

    const mappingColumns: ColumnsType<ClassDomainMapping> = useMemo(
        () => [
            {
                title: t("wikiCoverage.columns.className"),
                dataIndex: "className",
                key: "className",
            },
            {
                title: t("wikiCoverage.columns.domain"),
                dataIndex: "domain",
                key: "domain",
            },
            {
                title: t("common.actions"),
                key: "actions",
                width: 90,
                render: (_, record) => (
                    <Popconfirm
                        title={t("wikiCoverage.confirmRemove")}
                        okText={t("common.confirm")}
                        cancelText={t("common.cancel")}
                        onConfirm={() => void handleRemove(record)}
                    >
                        <Button size="small" danger>
                            {t("common.delete")}
                        </Button>
                    </Popconfirm>
                ),
            },
        ],
        [t, handleRemove],
    );

    const summary = overview?.summary;
    const unlinked = overview?.unlinked;

    return (
        <div style={{ padding: 24 }}>
            <h2 style={{ marginBottom: 16 }}>{t("wikiCoverage.title")}</h2>

            {errorMsg && (
                <Alert
                    type="info"
                    showIcon
                    closable
                    message={errorMsg}
                    onClose={() => setErrorMsg(null)}
                    style={{ marginBottom: 16 }}
                />
            )}

            <Space style={{ marginBottom: 16 }} wrap>
                <Button onClick={() => void fetchAll()}>{t("common.refresh")}</Button>
                <Button
                    type="primary"
                    loading={refreshing}
                    onClick={() => void handleRefresh()}
                >
                    {t("wikiCoverage.actions.refresh")}
                </Button>
            </Space>

            <Row gutter={16} style={{ marginBottom: 16 }}>
                <Col span={6}>
                    <Card>
                        <Statistic
                            title={t("wikiCoverage.summary.totalCells")}
                            value={summary?.totalCells ?? 0}
                        />
                    </Card>
                </Col>
                <Col span={6}>
                    <Card>
                        <Statistic
                            title={t("wikiCoverage.summary.missing")}
                            value={summary?.byStatus?.MISSING ?? 0}
                            valueStyle={{ color: "#cf1322" }}
                        />
                    </Card>
                </Col>
                <Col span={6}>
                    <Card>
                        <Statistic
                            title={t("wikiCoverage.summary.complete")}
                            value={summary?.byStatus?.COMPLETE ?? 0}
                            valueStyle={{ color: "#3f8600" }}
                        />
                    </Card>
                </Col>
                <Col span={6}>
                    <Card>
                        <Statistic
                            title={t("wikiCoverage.summary.unassigned")}
                            value={summary?.unassignedCells ?? 0}
                        />
                    </Card>
                </Col>
            </Row>

            {unlinked && unlinked.pageCount > 0 && (
                <Alert
                    type="warning"
                    showIcon
                    style={{ marginBottom: 16 }}
                    message={t("wikiCoverage.unlinkedTitle", {
                        count: unlinked.pageCount,
                    })}
                    description={t("wikiCoverage.unlinkedHint")}
                />
            )}

            <h3>{t("wikiCoverage.gapsTitle")}</h3>
            <Table
                rowKey={(record) =>
                    `${record.dimension}-${record.ontologyClassId ?? "none"}-${record.domain}`
                }
                size="small"
                loading={loading}
                columns={gapColumns}
                dataSource={overview?.gaps ?? []}
                pagination={false}
                locale={{ emptyText: t("wikiCoverage.noGaps") }}
            />

            <h3 style={{ marginTop: 32 }}>{t("wikiCoverage.mappingsTitle")}</h3>
            <Space style={{ marginBottom: 12 }} wrap>
                <Button onClick={() => void openAdd()}>
                    {t("wikiCoverage.actions.addMapping")}
                </Button>
                <span style={{ color: "#888" }}>
                    {t("wikiCoverage.domainVocabulary", {
                        domains: domains.length > 0 ? domains.join(" / ") : t("common.none"),
                    })}
                </span>
            </Space>
            <Table
                rowKey={(record) => `${record.ontologyClassId}-${record.domain}`}
                size="small"
                columns={mappingColumns}
                dataSource={mappings}
                pagination={{ pageSize: 10, showSizeChanger: false }}
                locale={{ emptyText: t("wikiCoverage.noMappings") }}
            />

            <Modal
                open={addOpen}
                title={t("wikiCoverage.actions.addMapping")}
                okText={t("common.save")}
                cancelText={t("common.cancel")}
                okButtonProps={{
                    disabled: newClassId === undefined || !newDomain?.trim(),
                }}
                onCancel={() => setAddOpen(false)}
                onOk={() => void handleAdd()}
            >
                <p style={{ marginBottom: 8 }}>{t("wikiCoverage.columns.className")}</p>
                <Select
                    aria-label={t("wikiCoverage.columns.className")}
                    style={{ width: "100%", marginBottom: 16 }}
                    showSearch
                    optionFilterProp="label"
                    value={newClassId}
                    onChange={setNewClassId}
                    options={classOptions}
                    placeholder={t("wikiCoverage.classPlaceholder")}
                />
                <p style={{ marginBottom: 8 }}>{t("wikiCoverage.columns.domain")}</p>
                <Select
                    aria-label={t("wikiCoverage.columns.domain")}
                    style={{ width: "100%" }}
                    showSearch
                    allowClear
                    mode="tags"
                    maxCount={1}
                    value={newDomain === undefined ? [] : [newDomain]}
                    onChange={(v: string[]) => setNewDomain(v[0])}
                    options={domains.map((d) => ({ value: d, label: d }))}
                    placeholder={t("wikiCoverage.domainPlaceholder")}
                />
                <p style={{ color: "#888", fontSize: 12, marginTop: 8 }}>
                    {t("wikiCoverage.domainHint")}
                </p>
            </Modal>
        </div>
    );
}
