/**
 * 冲突检测页（feat-wiki-knowledge M7，机制 3）。
 *
 * 列表的默认筛选是 ``OPEN``：这是一个**待办工作台**，不是审计流水。已处置的
 * 冲突保留下来是为了给机制 3 算准确率（``IGNORED`` 占比 = 误报率），但默认
 * 铺满屏幕只会把还没处理的那些挤到后面去。
 *
 * 处置动作三选一，刻意不做默认值：``IGNORED`` 唯一表达「系统误报」，猜错会
 * 污染那条准确率统计，而那个统计正是决定「改规则还是改 prompt」的输入。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Modal, Select, Space, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import { listWikiConflicts, resolveWikiConflict } from "../api/wikiConflicts";
import {
    CONFLICT_ACTIONS,
    CONFLICT_LIST_STATUSES,
    CONFLICT_SEVERITIES,
    CONFLICT_TYPES,
    type ConflictAction,
    type ConflictListStatus,
    type ConflictSeverity,
    type ConflictType,
    type KnowledgeConflict,
} from "../types/wikiConflicts";

const SEVERITY_COLOR: Record<string, string> = {
    CRITICAL: "red",
    HIGH: "orange",
    MEDIUM: "gold",
    LOW: "default",
};

const PAGE_SIZE = 20;

export default function AdminWikiConflictsPage() {
    const { t } = useTranslation();

    const [rows, setRows] = useState<KnowledgeConflict[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(0);
    const [loading, setLoading] = useState(false);

    const [statusFilter, setStatusFilter] = useState<ConflictListStatus | undefined>(
        "OPEN",
    );
    const [severityFilter, setSeverityFilter] = useState<ConflictSeverity | undefined>();
    const [typeFilter, setTypeFilter] = useState<ConflictType | undefined>();

    /** 待处置的冲突（非空 = 处置弹窗打开） */
    const [target, setTarget] = useState<KnowledgeConflict | null>(null);
    const [action, setAction] = useState<ConflictAction | undefined>(undefined);
    const [errorMsg, setErrorMsg] = useState<string | null>(null);

    const fetchList = useCallback(async () => {
        setLoading(true);
        try {
            const res = await listWikiConflicts({
                status: statusFilter,
                severity: severityFilter,
                conflictType: typeFilter,
                limit: PAGE_SIZE,
                offset: page * PAGE_SIZE,
            });
            setRows(res.rows);
            setTotal(res.total);
        } catch {
            // 拦截器已 toast 后端原因；保留旧数据，避免误读成「冲突都没了」
        } finally {
            setLoading(false);
        }
    }, [statusFilter, severityFilter, typeFilter, page]);

    useEffect(() => {
        void fetchList();
    }, [fetchList]);

    const openResolve = useCallback((record: KnowledgeConflict) => {
        setTarget(record);
        // 不给默认值：三个动作语义不同，预选一个会让人闭眼点确定
        setAction(undefined);
    }, []);

    const handleResolve = useCallback(async () => {
        if (target === null || action === undefined) {
            return;
        }
        try {
            await resolveWikiConflict(target.id, action);
            setTarget(null);
            void fetchList();
        } catch (e) {
            const err = e as Error & { status?: number };
            // 409 = 已被别人处置（终态不可逆）。这不是系统故障，是可预期的
            // 并发结果 —— 用页面级提示说清楚，并按「已处置」刷新掉这一行。
            setErrorMsg(
                err.status === 409
                    ? t("wikiConflicts.errors.alreadyResolved")
                    : t("wikiConflicts.errors.resolveFailed"),
            );
            setTarget(null);
            void fetchList();
        }
    }, [target, action, fetchList, t]);

    const columns: ColumnsType<KnowledgeConflict> = useMemo(
        () => [
            {
                title: t("wikiConflicts.columns.type"),
                dataIndex: "conflictType",
                key: "conflictType",
                width: 140,
                render: (v: string) => (
                    <Tag>{t(`wikiConflicts.types.${v}`)}</Tag>
                ),
            },
            {
                title: t("wikiConflicts.columns.severity"),
                dataIndex: "severity",
                key: "severity",
                width: 110,
                render: (v: string) => (
                    <Tag color={SEVERITY_COLOR[v] ?? "default"}>
                        {t(`wikiConflicts.severities.${v}`)}
                    </Tag>
                ),
            },
            {
                title: t("wikiConflicts.columns.pageIds"),
                dataIndex: "pageIds",
                key: "pageIds",
                width: 220,
                render: (ids: string[]) => ids.join(t("wikiConflicts.idSeparator")),
            },
            {
                title: t("wikiConflicts.columns.description"),
                dataIndex: "description",
                key: "description",
                render: (v: string | null) => v ?? t("common.dash"),
            },
            {
                title: t("wikiConflicts.columns.detectedBy"),
                dataIndex: "detectedBy",
                key: "detectedBy",
                width: 110,
                render: (v: string) => t(`wikiConflicts.detectors.${v}`),
            },
            {
                title: t("common.actions"),
                key: "actions",
                width: 110,
                render: (_, record) =>
                    record.resolvedAt === null ? (
                        <Button size="small" onClick={() => openResolve(record)}>
                            {t("wikiConflicts.actions.resolve")}
                        </Button>
                    ) : (
                        <Tag color="green">
                            {t(`wikiConflicts.actions.${record.resolutionAction}`)}
                        </Tag>
                    ),
            },
        ],
        [t, openResolve],
    );

    return (
        <div style={{ padding: 24 }}>
            <h2 style={{ marginBottom: 16 }}>{t("wikiConflicts.title")}</h2>

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
                    aria-label={t("wikiConflicts.filters.status")}
                    style={{ width: 160 }}
                    placeholder={t("wikiConflicts.filters.status")}
                    value={statusFilter}
                    onChange={(v: ConflictListStatus | undefined) => {
                        setPage(0);
                        setStatusFilter(v);
                    }}
                    options={CONFLICT_LIST_STATUSES.map((s) => ({
                        value: s,
                        label: t(`wikiConflicts.statuses.${s}`),
                    }))}
                />
                <Select
                    allowClear
                    aria-label={t("wikiConflicts.filters.severity")}
                    style={{ width: 160 }}
                    placeholder={t("wikiConflicts.filters.severity")}
                    value={severityFilter}
                    onChange={(v: ConflictSeverity | undefined) => {
                        setPage(0);
                        setSeverityFilter(v);
                    }}
                    options={CONFLICT_SEVERITIES.map((s) => ({
                        value: s,
                        label: t(`wikiConflicts.severities.${s}`),
                    }))}
                />
                <Select
                    allowClear
                    aria-label={t("wikiConflicts.filters.type")}
                    style={{ width: 180 }}
                    placeholder={t("wikiConflicts.filters.type")}
                    value={typeFilter}
                    onChange={(v: ConflictType | undefined) => {
                        setPage(0);
                        setTypeFilter(v);
                    }}
                    options={CONFLICT_TYPES.map((s) => ({
                        value: s,
                        label: t(`wikiConflicts.types.${s}`),
                    }))}
                />
                <Button onClick={() => void fetchList()}>{t("common.refresh")}</Button>
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
                locale={{ emptyText: t("wikiConflicts.empty") }}
            />

            <Modal
                open={target !== null}
                title={t("wikiConflicts.actions.resolve")}
                okText={t("common.confirm")}
                cancelText={t("common.cancel")}
                okButtonProps={{ disabled: action === undefined }}
                onCancel={() => setTarget(null)}
                onOk={() => void handleResolve()}
            >
                <p>{target?.description ?? t("common.dash")}</p>
                <Select
                    aria-label={t("wikiConflicts.actions.resolve")}
                    style={{ width: "100%" }}
                    placeholder={t("wikiConflicts.resolvePlaceholder")}
                    value={action}
                    onChange={(v: ConflictAction) => setAction(v)}
                    options={CONFLICT_ACTIONS.map((a) => ({
                        value: a,
                        label: t(`wikiConflicts.actions.${a}`),
                    }))}
                />
                <p style={{ color: "#888", fontSize: 12, marginTop: 8 }}>
                    {t("wikiConflicts.resolveHint")}
                </p>
            </Modal>
        </div>
    );
}
