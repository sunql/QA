/** M4-3 采购知识检索页（消费 M2 四端点）。
 *
 * 四个通道一个页面：统一查询（P4 薄别名）+ 实体解析 + 文档检索 + 受控邻接；
 * 证据卡片渲染 M2-1 契约全字段（channel/kind/confidence/security/sourceRef/版本）。
 * 页面不做权限二次过滤——后端按登录 + 域可见性裁决（不可见 = 查不到）。
 */

import { useCallback, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Input,
  Space,
  Table,
  Tabs,
  Tag,
  message,
} from "antd";
import { useTranslation } from "../i18n";
import {
  getGraphNeighbors,
  queryKnowledge,
  searchEntities,
  searchProcurementDocuments,
  type EntityMatch,
  type GraphNeighbors,
  type KnowledgeEvidence,
} from "../api/procurementKnowledge";

const CHANNEL_COLOR: Record<string, string> = {
  GRAPH: "purple",
  SQL: "blue",
  RAG: "green",
  METRIC: "orange",
  DOCUMENT: "cyan",
};

function EvidenceCard({ e }: { e: KnowledgeEvidence }) {
  return (
    <Card size="small" title={`${e.kind} · ${e.title}`} style={{ marginBottom: 12 }}>
      {e.snippet && <p>{e.snippet}</p>}
      <Descriptions size="small" column={2}>
        <Descriptions.Item label="通道">
          <Tag color={CHANNEL_COLOR[e.channel] ?? "default"}>{e.channel}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="置信度">
          {e.confidence != null ? e.confidence.toFixed(2) : "-"}
        </Descriptions.Item>
        <Descriptions.Item label="引用">{e.ref}</Descriptions.Item>
        <Descriptions.Item label="出处">
          {e.source}
          {e.releaseVersion ? ` @${e.releaseVersion}` : ""}
        </Descriptions.Item>
        {e.security?.level && (
          <Descriptions.Item label="密级">
            <Tag>{e.security.level}</Tag>
          </Descriptions.Item>
        )}
        {e.sourceRef?.documentId != null && (
          <Descriptions.Item label="文档">{e.sourceRef.documentId}</Descriptions.Item>
        )}
        {e.validAt && (
          <Descriptions.Item label="生效日">{e.validAt.slice(0, 10)}</Descriptions.Item>
        )}
      </Descriptions>
    </Card>
  );
}

export default function ProcurementKnowledgePage() {
  const { t } = useTranslation();
  const [question, setQuestion] = useState("");
  const [queryResult, setQueryResult] = useState<Awaited<
    ReturnType<typeof queryKnowledge>
  > | null>(null);
  const [entities, setEntities] = useState<EntityMatch[]>([]);
  const [neighbors, setNeighbors] = useState<GraphNeighbors | null>(null);
  const [docs, setDocs] = useState<KnowledgeEvidence[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [entityQ, setEntityQ] = useState("");
  const [docQ, setDocQ] = useState("");
  const [graphType, setGraphType] = useState("Supplier");
  const [graphKey, setGraphKey] = useState("");

  const runQuery = useCallback(async () => {
    if (!question.trim()) return;
    setBusy("query");
    try {
      setQueryResult(await queryKnowledge(question.trim()));
    } catch (err) {
      message.error(
        err instanceof Error ? err.message : t("procurement.knowledge.queryFailed"),
      );
    } finally {
      setBusy(null);
    }
  }, [question, t]);

  const runEntities = useCallback(async () => {
    if (!entityQ.trim()) return;
    setBusy("entities");
    try {
      setEntities(await searchEntities(entityQ.trim()));
    } catch {
      message.error(t("procurement.knowledge.queryFailed"));
    } finally {
      setBusy(null);
    }
  }, [entityQ, t]);

  const runDocs = useCallback(async () => {
    if (!docQ.trim()) return;
    setBusy("docs");
    try {
      setDocs(await searchProcurementDocuments(docQ.trim()));
    } catch {
      message.error(t("procurement.knowledge.queryFailed"));
    } finally {
      setBusy(null);
    }
  }, [docQ, t]);

  const runGraph = useCallback(async () => {
    if (!graphKey.trim()) return;
    setBusy("graph");
    try {
      setNeighbors(await getGraphNeighbors(graphType, graphKey.trim()));
    } catch {
      message.error(t("procurement.knowledge.queryFailed"));
    } finally {
      setBusy(null);
    }
  }, [graphKey, graphType, t]);

  const entityColumns = [
    {
      title: t("procurement.knowledge.entity.code"),
      dataIndex: "enterpriseCode",
      key: "code",
    },
    {
      title: t("procurement.knowledge.entity.name"),
      dataIndex: "name",
      key: "name",
    },
    {
      title: t("procurement.knowledge.entity.score"),
      dataIndex: "matchScore",
      key: "score",
    },
    {
      title: t("procurement.knowledge.entity.sources"),
      key: "sources",
      render: (_: unknown, r: EntityMatch) =>
        r.sourceMappings.map((m) => m.sourceSystem).join(", "),
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <h2>{t("procurement.knowledge.title")}</h2>
      <Tabs
        items={[
          {
            key: "query",
            label: t("procurement.knowledge.tab.query"),
            children: (
              <Space direction="vertical" style={{ width: "100%" }} size="middle">
                <Space.Compact style={{ width: "100%" }}>
                  <Input
                    value={question}
                    onChange={(e) => setQuestion(e.target.value)}
                    placeholder={t("procurement.knowledge.queryPlaceholder")}
                    onPressEnter={() => void runQuery()}
                  />
                  <Button
                    type="primary"
                    loading={busy === "query"}
                    onClick={() => void runQuery()}
                  >
                    {t("procurement.knowledge.search")}
                  </Button>
                </Space.Compact>
                {queryResult != null && (
                  <>
                    {queryResult.routeResults?.map((r) => (
                      <Alert
                        key={r.channel}
                        type={r.status === "OK" ? "success" : "warning"}
                        message={`${r.channel}: ${r.status}（${r.itemCount} 条）`}
                        style={{ marginBottom: 8 }}
                      />
                    ))}
                    {queryResult.evidence.length === 0 && (
                      <Alert type="info" message={t("procurement.knowledge.noEvidence")} />
                    )}
                    {queryResult.evidence.map((e) => (
                      <EvidenceCard key={e.evidenceId} e={e} />
                    ))}
                  </>
                )}
              </Space>
            ),
          },
          {
            key: "entities",
            label: t("procurement.knowledge.tab.entities"),
            children: (
              <Space direction="vertical" style={{ width: "100%" }} size="middle">
                <Space.Compact style={{ width: "100%" }}>
                  <Input
                    value={entityQ}
                    onChange={(e) => setEntityQ(e.target.value)}
                    placeholder={t("procurement.knowledge.entity.placeholder")}
                    onPressEnter={() => void runEntities()}
                  />
                  <Button loading={busy === "entities"} onClick={() => void runEntities()}>
                    {t("procurement.knowledge.search")}
                  </Button>
                </Space.Compact>
                <Table
                  rowKey="enterpriseKey"
                  size="small"
                  columns={entityColumns}
                  dataSource={entities}
                  pagination={{ pageSize: 10 }}
                />
              </Space>
            ),
          },
          {
            key: "graph",
            label: t("procurement.knowledge.tab.graph"),
            children: (
              <Space direction="vertical" style={{ width: "100%" }} size="middle">
                <Space>
                  <Input
                    value={graphType}
                    onChange={(e) => setGraphType(e.target.value)}
                    style={{ width: 140 }}
                    placeholder="Supplier"
                  />
                  <Input
                    value={graphKey}
                    onChange={(e) => setGraphKey(e.target.value)}
                    style={{ width: 200 }}
                    placeholder={t("procurement.knowledge.graph.keyPlaceholder")}
                  />
                  <Button loading={busy === "graph"} onClick={() => void runGraph()}>
                    {t("procurement.knowledge.search")}
                  </Button>
                </Space>
                {neighbors != null && (
                  <>
                    <Alert
                      type={neighbors.truncated ? "warning" : "info"}
                      message={
                        neighbors.truncated
                          ? t("procurement.knowledge.graph.truncated")
                          : `${neighbors.edges.length} 条边 / ${neighbors.nodes.length} 个节点`
                      }
                    />
                    <Table
                      rowKey={(r, i) => `${r.fromKey}-${r.relType}-${r.toKey}-${i}`}
                      size="small"
                      pagination={false}
                      dataSource={neighbors.edges}
                      columns={[
                        { title: "Depth", dataIndex: "depth", width: 70 },
                        { title: "From", dataIndex: "fromKey" },
                        { title: "关系", dataIndex: "relType" },
                        { title: "To", dataIndex: "toKey" },
                      ]}
                    />
                  </>
                )}
              </Space>
            ),
          },
          {
            key: "docs",
            label: t("procurement.knowledge.tab.documents"),
            children: (
              <Space direction="vertical" style={{ width: "100%" }} size="middle">
                <Space.Compact style={{ width: "100%" }}>
                  <Input
                    value={docQ}
                    onChange={(e) => setDocQ(e.target.value)}
                    placeholder={t("procurement.knowledge.doc.placeholder")}
                    onPressEnter={() => void runDocs()}
                  />
                  <Button loading={busy === "docs"} onClick={() => void runDocs()}>
                    {t("procurement.knowledge.search")}
                  </Button>
                </Space.Compact>
                {docs.length === 0 && busy !== "docs" && (
                  <Alert type="info" message={t("procurement.knowledge.noDocuments")} />
                )}
                {docs.map((e) => (
                  <EvidenceCard key={e.evidenceId} e={e} />
                ))}
              </Space>
            ),
          },
        ]}
      />
    </div>
  );
}
