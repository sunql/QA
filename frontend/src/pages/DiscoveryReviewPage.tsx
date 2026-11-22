/** G5 前台签收：发现评审页。
 *
 * 业务在这里逐条判定 AI 候选与主数据问题（采纳/驳回/待证；确认/已解决/忽略），
 * 备注随判定一起落库（`decision_note`），历史由 audit_log 承担。
 *
 * 口径：
 * - 整组接口 **admin-only**（后端 `requireAdmin`）；
 * - 候选是**未确认结论**：只有判定「采纳」并走提案→四眼审核→发布后才可能生效；
 * - 判定**可改判**（待证 → 采纳是正常流程）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  Modal,
  Progress,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "../i18n";
import { useAuthStore } from "../stores/authStore";
import {
  decideCandidate,
  decideFinding,
  listCandidates,
  listDiscoveryRuns,
  listFindings,
  type DiscoveryCandidate,
  type DiscoveryFinding,
  type DiscoveryRunSummary,
} from "../api/discoveryReview";
import {
  listOntologyClasses,
  listOntologyMetrics,
  listOntologyProperties,
  type OntologyClassOption,
  type OntologyMetricOption,
  type OntologyPropertyOption,
} from "../api/ontologyTargets";

const { Text } = Typography;

const STATUS_COLOR: Record<string, string> = {
  CANDIDATE: "default",
  NEEDS_EVIDENCE: "warning",
  ACCEPTED: "success",
  REJECTED: "error",
  MERGED: "processing",
  OPEN: "default",
  ACKNOWLEDGED: "processing",
  RESOLVED: "success",
  IGNORED: "default",
};
const SEVERITY_COLOR: Record<string, string> = {
  ERROR: "error",
  WARNING: "warning",
  INFO: "default",
};

type DecideKind = "candidate" | "finding";
interface DecideTarget {
  kind: DecideKind;
  id: number;
  label: string;
  status: string;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export default function DiscoveryReviewPage() {
  const { t } = useTranslation();
  const user = useAuthStore((state) => state.user);
  const isAdmin = (user?.roles ?? []).includes("admin");

  const [runs, setRuns] = useState<DiscoveryRunSummary[]>([]);
  const [runId, setRunId] = useState<number | undefined>();
  const [candidates, setCandidates] = useState<DiscoveryCandidate[]>([]);
  const [findings, setFindings] = useState<DiscoveryFinding[]>([]);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const [target, setTarget] = useState<DecideTarget | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [linkTarget, setLinkTarget] = useState<DecideTarget | null>(null);
  const [linkType, setLinkType] = useState("ONTOLOGY_CLASS");
  const [linkClassId, setLinkClassId] = useState<number | undefined>();
  const [linkCode, setLinkCode] = useState<string | undefined>();
  const [ontologyClasses, setOntologyClasses] = useState<OntologyClassOption[]>([]);
  const [ontologyProperties, setOntologyProperties] = useState<OntologyPropertyOption[]>([]);
  const [ontologyMetrics, setOntologyMetrics] = useState<OntologyMetricOption[]>([]);
  const [linkBusy, setLinkBusy] = useState(false);

  const loadRuns = useCallback(async () => {
    try {
      const page = await listDiscoveryRuns({ limit: 50 });
      setRuns(page.rows);
      setRunId((current) => {
        if (current !== undefined) return current;
        const firstDone = page.rows.find((row) => row.status === "SUCCEEDED");
        return (firstDone ?? page.rows[0])?.id;
      });
    } catch {
      // interceptor 已提示
    }
  }, []);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  useEffect(() => {
    void (async () => {
      try {
        const [classes, properties, metrics] = await Promise.all([
          listOntologyClasses(),
          listOntologyProperties(),
          listOntologyMetrics(),
        ]);
        setOntologyClasses(classes);
        setOntologyProperties(properties);
        setOntologyMetrics(metrics);
      } catch {
        // interceptor 已提示
      }
    })();
  }, []);

  const loadRows = useCallback(async () => {
    if (runId === undefined) {
      setCandidates([]);
      setFindings([]);
      return;
    }
    setLoading(true);
    try {
      const [candidatePage, findingPage] = await Promise.all([
        listCandidates({ runId }),
        listFindings({ runId }),
      ]);
      setCandidates(candidatePage.rows);
      setFindings(findingPage.rows);
    } catch {
      // interceptor 已提示
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    void loadRows();
  }, [loadRows]);

  const selectedRun = useMemo(
    () => runs.find((run) => run.id === runId),
    [runs, runId],
  );

  const judged = useMemo(() => {
    const candidateJudged = candidates.filter((row) => row.status !== "CANDIDATE").length;
    const findingJudged = findings.filter((row) => row.status !== "OPEN").length;
    return candidateJudged + findingJudged;
  }, [candidates, findings]);
  const total = candidates.length + findings.length;

  const openLink = (kind: DecideKind, row: DiscoveryCandidate | DiscoveryFinding, label: string) => {
    setLinkTarget({ kind, id: row.id, label, status: row.status });
    setLinkType(row.linkedEntityType ?? "ONTOLOGY_CLASS");
    setLinkCode(row.linkedEntityCode ?? undefined);
    setLinkClassId(undefined);
  };

  const saveLink = async () => {
    if (!linkTarget) return;
    if (!linkCode) {
      message.warning(t("discoveryReview.linkPickFirst"));
      return;
    }
    setLinkBusy(true);
    try {
      const payload = {
        status: linkTarget.status,
        linkedEntityType: linkType,
        linkedEntityCode: linkCode,
      };
      if (linkTarget.kind === "candidate") {
        await decideCandidate(linkTarget.id, payload);
      } else {
        await decideFinding(linkTarget.id, payload);
      }
      message.success(t("discoveryReview.linkSaved"));
      setLinkTarget(null);
      await loadRows();
    } catch {
      // interceptor 已提示
    } finally {
      setLinkBusy(false);
    }
  };

  const linkOptions = (() => {
    if (linkType === "ONTOLOGY_METRIC") {
      return ontologyMetrics.map((item) => ({
        value: item.metricName,
        label: item.metricAlias ? `${item.metricName}（${item.metricAlias}）` : item.metricName,
      }));
    }
    if (linkType === "ONTOLOGY_PROPERTY") {
      const cls = ontologyClasses.find((item) => item.id === linkClassId);
      return ontologyProperties
        .filter((item) => item.classId === linkClassId)
        .map((item) => ({
          value: `${cls?.className ?? item.classId}.${item.propertyName}`,
          label: item.propertyAlias ? `${item.propertyName}（${item.propertyAlias}）` : item.propertyName,
        }));
    }
    return ontologyClasses.map((item) => ({
      value: item.className,
      label: item.classAlias ? `${item.className}（${item.classAlias}）` : item.className,
    }));
  })();

  const matchedClass = (() => {
    if (!linkTarget || linkTarget.kind !== "candidate") return undefined;
    const targetCode = linkTarget.label.split(" ").slice(1).join(" ").trim();
    return ontologyClasses.find(
      (item) => item.className.toUpperCase() === targetCode.toUpperCase(),
    );
  })();

  const openDecide = (kind: DecideKind, row: DiscoveryCandidate | DiscoveryFinding, label: string) => {
    setTarget({ kind, id: row.id, label, status: row.status });
    setNote(row.decisionNote ?? "");
  };

  const submitDecide = async (status: string) => {
    if (!target) return;
    setBusy(true);
    try {
      if (target.kind === "candidate") {
        await decideCandidate(target.id, { status, note: note.trim() || null });
      } else {
        await decideFinding(target.id, { status, note: note.trim() || null });
      }
      message.success(t("discoveryReview.decided"));
      setTarget(null);
      setNote("");
      await loadRows();
    } catch {
      // interceptor 已提示
    } finally {
      setBusy(false);
    }
  };

  const candidateColumns: ColumnsType<DiscoveryCandidate> = [
    { title: t("discoveryReview.candidateType"), dataIndex: "candidateType", width: 130 },
    {
      title: t("discoveryReview.target"),
      width: 260,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text>{row.tableName ?? "—"}{row.columnName ? `.${row.columnName}` : ""}</Text>
          <Text type="secondary">{row.targetCode ?? "—"}</Text>
        </Space>
      ),
    },
    {
      title: t("discoveryReview.confidence"),
      dataIndex: "confidence",
      width: 100,
      render: (value: number | null) => (value === null ? "—" : value.toFixed(2)),
    },
    {
      title: t("discoveryReview.evidence"),
      width: 90,
      render: (_, row) => <Tag>{row.evidenceRefs.length}</Tag>,
    },
    {
      title: t("discoveryReview.status"),
      dataIndex: "status",
      width: 140,
      render: (value: string) => <Tag color={STATUS_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    { title: t("discoveryReview.note"), dataIndex: "decisionNote", width: 180, render: (value: string | null) => value ?? "—" },
    {
      title: t("discoveryReview.linked"),
      width: 200,
      render: (_, row) =>
        row.linkedEntityCode ? <Text code>{row.linkedEntityType}:{row.linkedEntityCode}</Text> : "—",
    },
    {
      title: t("discoveryReview.action"),
      width: 300,
      render: (_, row) => (
        <Space wrap>
          <Button size="small" type="primary" disabled={!isAdmin} onClick={() => openDecide("candidate", row, `${row.candidateType} ${row.targetCode ?? ""}`)}>
            {t("discoveryReview.accept")}
          </Button>
          <Button size="small" danger disabled={!isAdmin} onClick={() => openDecide("candidate", row, `${row.candidateType} ${row.targetCode ?? ""}`)}>
            {t("discoveryReview.reject")}
          </Button>
          <Button size="small" disabled={!isAdmin} onClick={() => openDecide("candidate", row, `${row.candidateType} ${row.targetCode ?? ""}`)}>
            {t("discoveryReview.needsEvidence")}
          </Button>
          <Button size="small" disabled={!isAdmin} onClick={() => openLink("candidate", row, `${row.candidateType} ${row.targetCode ?? ""}`)}>
            {t("discoveryReview.linkOntology")}
          </Button>
        </Space>
      ),
    },
  ];

  const findingColumns: ColumnsType<DiscoveryFinding> = [
    { title: t("discoveryReview.findingType"), dataIndex: "findingType", width: 120 },
    {
      title: t("discoveryReview.severity"),
      dataIndex: "severity",
      width: 100,
      render: (value: string) => <Tag color={SEVERITY_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    {
      title: t("discoveryReview.target"),
      width: 220,
      render: (_, row) => (
        <Text>{row.tableName ?? "—"}{row.columnName ? `.${row.columnName}` : ""}</Text>
      ),
    },
    { title: t("discoveryReview.detail"), dataIndex: "detail", render: (value: string) => <Text>{value}</Text> },
    {
      title: t("discoveryReview.status"),
      dataIndex: "status",
      width: 140,
      render: (value: string) => <Tag color={STATUS_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    { title: t("discoveryReview.note"), dataIndex: "decisionNote", width: 160, render: (value: string | null) => value ?? "—" },
    {
      title: t("discoveryReview.linked"),
      width: 200,
      render: (_, row) =>
        row.linkedEntityCode ? <Text code>{row.linkedEntityType}:{row.linkedEntityCode}</Text> : "—",
    },
    {
      title: t("discoveryReview.action"),
      width: 300,
      render: (_, row) => (
        <Space wrap>
          <Button size="small" disabled={!isAdmin} onClick={() => openDecide("finding", row, `${row.findingType} ${row.tableName ?? ""}`)}>
            {t("discoveryReview.acknowledge")}
          </Button>
          <Button size="small" type="primary" disabled={!isAdmin} onClick={() => openDecide("finding", row, `${row.findingType} ${row.tableName ?? ""}`)}>
            {t("discoveryReview.resolve")}
          </Button>
          <Button size="small" disabled={!isAdmin} onClick={() => openDecide("finding", row, `${row.findingType} ${row.tableName ?? ""}`)}>
            {t("discoveryReview.ignore")}
          </Button>
          <Button size="small" disabled={!isAdmin} onClick={() => openLink("finding", row, `${row.findingType} ${row.tableName ?? ""}`)}>
            {t("discoveryReview.linkOntology")}
          </Button>
        </Space>
      ),
    },
  ];

  const decideOptions: { key: string; labelKey: string }[] =
    target?.kind === "candidate"
      ? [
          { key: "ACCEPTED", labelKey: "discoveryReview.accept" },
          { key: "REJECTED", labelKey: "discoveryReview.reject" },
          { key: "NEEDS_EVIDENCE", labelKey: "discoveryReview.needsEvidence" },
        ]
      : [
          { key: "ACKNOWLEDGED", labelKey: "discoveryReview.acknowledge" },
          { key: "RESOLVED", labelKey: "discoveryReview.resolve" },
          { key: "IGNORED", labelKey: "discoveryReview.ignore" },
          { key: "OPEN", labelKey: "discoveryReview.reopen" },
        ];

  return (
    <div style={{ padding: 24 }}>
      <h2>{t("discoveryReview.title")}</h2>
      {!isAdmin && (
        <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t("discoveryReview.adminOnly")} />
      )}
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <Select
            style={{ width: 420 }}
            placeholder={t("discoveryReview.runPlaceholder")}
            value={runId}
            onChange={(value) => setRunId(value)}
            options={runs.map((run) => ({
              value: run.id,
              label: `#${run.id} · ${run.status} · ${formatTime(run.startedAt)} · ${run.modelVersion ?? "—"}`,
            }))}
          />
          <Button onClick={() => { void loadRuns(); void loadRows(); }}>
            {t("common.refresh")}
          </Button>
          {total > 0 && (
            <Space size={8} style={{ minWidth: 280 }}>
              <Text>{t("discoveryReview.progress", { judged, total })}</Text>
              <Progress percent={Math.round((judged / total) * 100)} size="small" style={{ width: 160 }} />
            </Space>
          )}
        </Space>
      </Card>

      {selectedRun && (
        <Card size="small" style={{ marginBottom: 12 }} title={t("discoveryReview.runDetail")}>
          <Descriptions size="small" column={4}>
            <Descriptions.Item label="run">#{selectedRun.id}</Descriptions.Item>
            <Descriptions.Item label={t("discoveryReview.status")}>
              <Tag color={STATUS_COLOR[selectedRun.status] ?? "default"}>{selectedRun.status}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t("discoveryReview.started")}>{formatTime(selectedRun.startedAt)}</Descriptions.Item>
            <Descriptions.Item label={t("discoveryReview.model")}>{selectedRun.modelVersion ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="tokens">{selectedRun.promptTokens} / {selectedRun.completionTokens}</Descriptions.Item>
            <Descriptions.Item label="cost">{selectedRun.cost}</Descriptions.Item>
            <Descriptions.Item label="source">#{selectedRun.sourceId}</Descriptions.Item>
            <Descriptions.Item label="snapshot">#{selectedRun.snapshotId ?? "—"}</Descriptions.Item>
          </Descriptions>
          {selectedRun.errorMessage && (
            <Alert type="error" showIcon message={selectedRun.errorMessage} style={{ marginTop: 8 }} />
          )}
        </Card>
      )}

      {runId === undefined ? (
        <Empty
          description={
            runs.length === 0 ? t("discoveryReview.noRunsHint") : t("discoveryReview.noRun")
          }
        />
      ) : (
        <Tabs
          items={[
            {
              key: "candidates",
              label: `${t("discoveryReview.candidates")} (${candidates.length})`,
              children: (
                <Table
                  rowKey="id"
                  size="small"
                  loading={loading}
                  dataSource={candidates}
                  columns={candidateColumns}
                  pagination={{ pageSize: 20 }}
                />
              ),
            },
            {
              key: "findings",
              label: `${t("discoveryReview.findings")} (${findings.length})`,
              children: (
                <Table
                  rowKey="id"
                  size="small"
                  loading={loading}
                  dataSource={findings}
                  columns={findingColumns}
                  pagination={{ pageSize: 20 }}
                />
              ),
            },
          ]}
        />
      )}

      <Modal
        title={t("discoveryReview.decideTitle")}
        open={target !== null}
        onCancel={() => setTarget(null)}
        footer={null}
        destroyOnHidden
      >
        <Descriptions size="small" column={1} style={{ marginBottom: 12 }}>
          <Descriptions.Item label={t("discoveryReview.target")}>{target?.label}</Descriptions.Item>
          <Descriptions.Item label={t("discoveryReview.status")}>
            <Tag color={STATUS_COLOR[target?.status ?? ""] ?? "default"}>{target?.status}</Tag>
          </Descriptions.Item>
        </Descriptions>
        <Input.TextArea
          rows={3}
          value={note}
          placeholder={t("discoveryReview.notePlaceholder")}
          onChange={(event) => setNote(event.target.value)}
          style={{ marginBottom: 12 }}
        />
        <Space wrap>
          {decideOptions.map((option) => (
            <Button
              key={option.key}
              type={option.key === "ACCEPTED" || option.key === "RESOLVED" ? "primary" : "default"}
              loading={busy}
              onClick={() => void submitDecide(option.key)}
            >
              {t(option.labelKey)}
            </Button>
          ))}
        </Space>
      </Modal>

      <Modal
        title={`${t("discoveryReview.linkTitle")} · ${linkTarget?.label ?? ""}`}
        open={linkTarget !== null}
        onCancel={() => setLinkTarget(null)}
        onOk={() => void saveLink()}
        confirmLoading={linkBusy}
        destroyOnHidden
      >
        {matchedClass && (
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 12 }}
            message={t("discoveryReview.existingMatch", { name: matchedClass.className })}
            action={
              <Button size="small" onClick={() => navigate("/ontology")}>
                {t("discoveryReview.openOntology")}
              </Button>
            }
          />
        )}
        {!matchedClass && linkTarget?.kind === "candidate" && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t("discoveryReview.noMatchHint")}
            action={
              <Button size="small" onClick={() => navigate("/ontology")}>
                {t("discoveryReview.openOntology")}
              </Button>
            }
          />
        )}
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Select
            style={{ width: "100%" }}
            value={linkType}
            onChange={(value) => {
              setLinkType(value);
              setLinkClassId(undefined);
              setLinkCode(undefined);
            }}
            options={[
              { value: "ONTOLOGY_CLASS", label: t("discoveryReview.linkClass") },
              { value: "ONTOLOGY_PROPERTY", label: t("discoveryReview.linkProperty") },
              { value: "ONTOLOGY_METRIC", label: t("discoveryReview.linkMetric") },
            ]}
          />
          {linkType === "ONTOLOGY_PROPERTY" && (
            <Select
              style={{ width: "100%" }}
              placeholder={t("discoveryReview.linkClassFirst")}
              value={linkClassId}
              onChange={(value) => {
                setLinkClassId(value);
                setLinkCode(undefined);
              }}
              showSearch
              optionFilterProp="label"
              options={ontologyClasses.map((item) => ({
                value: item.id,
                label: item.classAlias ? `${item.className}（${item.classAlias}）` : item.className,
              }))}
            />
          )}
          <Select
            style={{ width: "100%" }}
            placeholder={t("discoveryReview.linkPick")}
            value={linkCode}
            onChange={(value) => setLinkCode(value)}
            showSearch
            optionFilterProp="label"
            options={linkOptions}
          />
        </Space>
      </Modal>
    </div>
  );
}
