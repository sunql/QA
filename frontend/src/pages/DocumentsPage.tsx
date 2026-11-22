/** 文档目录管理页（Phase 5.1 + 5.2 前端补全）。
 *
 * 三个 Tab：
 * 1. 文档目录 — 列表 + 创建 + 编辑 + 删除 + 上传文件
 * 2. 文档关联 — 将文档关联到供应商/物料/PO 等业务实体
 * 3. 语义检索 — 输入问题，从已上传文档中检索相关内容片段
 */

import { useEffect, useState, useCallback, useRef } from "react";
import {
  Table,
  Button,
  Modal,
  Form,
  Input,
  Select,
  Space,
  Tag,
  Popconfirm,
  message,
  Tabs,
  Card,
  Typography,
  Descriptions,
  List,
  Alert,
  DatePicker,
  Switch,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
import {
  PlusOutlined,
  ReloadOutlined,
  UploadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import {
  listDocuments,
  createDocument,
  updateDocument,
  fetchDocumentSource,
  fetchDocumentPreview,
  vectorizeDocument,
  listRelationTargets,
  updateRelation,
  type RelationTarget,
  deleteDocument,
  listRelations,
  createRelation,
  deleteRelation,
  searchDocuments,
} from "../api/document";
import type {
  DocumentRead,
  DocumentCreate,
  DocumentUpdate,
  DocEntityRelationRead,
  DocEntityRelationCreate,
  RagSearchResult,
} from "../types/document";
import {
  DOCUMENT_TYPE_OPTIONS,
  DOCUMENT_SECURITY_OPTIONS,
  DOC_RELATION_TYPE_OPTIONS,
} from "../types/document";
import { useTranslation } from "../i18n";
import FilterBar from "../components/ontology/FilterBar";
import type { FilterField } from "../components/ontology/FilterBar";
import { contains, matchSelect } from "../utils/ontologyFilter";
import { useTablePagination } from "../utils/useTablePagination";
import { listBusinessObjects } from "../api/businessObject";
import { toIsoDateParam } from "../utils/dateParam";
import type { FilterValues } from "../utils/ontologyFilter";
import type { EntityType } from "../types/entityMapping";
import { DocumentQaPanel } from "../components/documents/DocumentQaPanel";
import DocumentUploadModal from "../components/documents/DocumentUploadModal";

const { TextArea } = Input;
const { TabPane } = Tabs;
const { Paragraph } = Typography;

// ---------------------------------------------------------------------------
// Document status tag color
// ---------------------------------------------------------------------------

const STATUS_COLOR: Record<string, string> = {
  ACTIVE: "green",
  EXPIRED: "orange",
};

// ---------------------------------------------------------------------------
// Document form
// ---------------------------------------------------------------------------

/** 关联对象类型 → 输入提示（告诉用户「该填什么」而不是让他猜）。 */
function targetHintFor(entityType: string): string {
  switch (entityType) {
    case "ONTOLOGY_CLASS":
      return "选择本体类（如 DIM_SUPPLIER）；制度/规范类文档通常关联到类";
    case "ONTOLOGY_METRIC":
      return "选择指标；指标口径类文档通常关联到指标";
    case "ONTOLOGY_PROPERTY":
      return "选择属性（类名.属性名）；字段说明类文档通常关联到属性";
    default:
      return "选择具体业务对象实例（编码/名称可搜）";
  }
}

interface DocFormValues {
  documentId: string;
  documentName: string;
  documentType: string;
  version: string;
  owner: string;
  /** DatePicker 选中值是 Dayjs；服务端回填值是 ISO 字符串 */
  effectiveDate: Dayjs | string | undefined;
  securityLevel: string;
}

const EMPTY_DOC_FORM: DocFormValues = {
  documentId: "",
  documentName: "",
  documentType: "CONTRACT",
  version: "v1.0",
  owner: "",
  effectiveDate: undefined,
  securityLevel: "L1",
};

// ---------------------------------------------------------------------------
// Document list columns
// ---------------------------------------------------------------------------

function DocList({
  docs,
  loading,
  onEdit,
  onDelete,
  onOpenSource,
  onPreview,
  onVectorize,
  vectorizingId,
  onToggleAi,
  togglingAiId,
}: {
  docs: DocumentRead[];
  loading: boolean;
  onEdit: (record: DocumentRead) => void;
  onDelete: (id: number) => void;
  onOpenSource: (record: DocumentRead) => void;
  onPreview: (record: DocumentRead) => void;
  onVectorize: (record: DocumentRead) => void;
  vectorizingId: number | null;
  onToggleAi: (record: DocumentRead, enabled: boolean) => void;
  togglingAiId: number | null;
}) {
  const { t } = useTranslation();
  // 分页与本体管理各 tab 页同口径（useTablePagination：可切页大小 + 快速跳转）
  const { pagination } = useTablePagination();

  const columns = [
    {
      title: "文档编号",
      dataIndex: "documentId",
      width: 180,
    },
    {
      title: "文档名称",
      dataIndex: "documentName",
      width: 220,
      ellipsis: true,
    },
    {
      title: "类型",
      dataIndex: "documentType",
      width: 130,
      render: (v: string) => {
        const opt = DOCUMENT_TYPE_OPTIONS.find((o) => o.value === v);
        return opt ? opt.label : v;
      },
    },
    {
      title: "安全级别",
      dataIndex: "securityLevel",
      width: 100,
      render: (v: string) => {
        const opt = DOCUMENT_SECURITY_OPTIONS.find((o) => o.value === v);
        return opt ? opt.label : v;
      },
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (v: string) => <Tag color={STATUS_COLOR[v] ?? "default"}>{v}</Tag>,
    },
    { title: "版本", dataIndex: "version", width: 80 },
    { title: "Owner", dataIndex: "owner", width: 120 },
    {
      title: "生效日期",
      dataIndex: "effectiveDate",
      width: 110,
      render: (v: string | null) => v ?? "—",
    },
    {
      title: "AI 可用",
      dataIndex: "aiContextEnabled",
      width: 100,
      render: (v: boolean | undefined, record: DocumentRead) => (
        <Switch
          size="small"
          checked={v !== false}
          loading={togglingAiId === record.id}
          onChange={(checked) => onToggleAi(record, checked)}
        />
      ),
    },
    {
      title: "向量化",
      dataIndex: "vectorizedAt",
      width: 110,
      render: (v: string | null) =>
        v ? (
          <Tag color="success">{t("document.vectorized")}</Tag>
        ) : (
          <Tag color="default">{t("document.notVectorized")}</Tag>
        ),
    },
    {
      title: "操作",
      width: 300,
      render: (_: unknown, record: DocumentRead) => (
        <Space wrap>
          <Button size="small" onClick={() => onOpenSource(record)}>
            {t("document.viewSource")}
          </Button>
          <Button size="small" onClick={() => onPreview(record)}>
            {t("document.preview")}
          </Button>
          {!record.vectorizedAt && (
            <Button
              size="small"
              loading={vectorizingId === record.id}
              onClick={() => onVectorize(record)}
            >
              {t("document.vectorize")}
            </Button>
          )}
          <Button size="small" onClick={() => onEdit(record)}>
            {t("common.edit")}
          </Button>
          <Popconfirm
            title="确认删除此文档？"
            onConfirm={() => onDelete(record.id)}
          >
            <Button size="small" danger>
              {t("common.delete")}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={docs}
        columns={columns}
        scroll={{ x: 1000 }}
        pagination={pagination}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Relations list
// ---------------------------------------------------------------------------

function RelationList({
  relations,
  loading,
  onDelete,
  onEdit,
}: {
  relations: DocEntityRelationRead[];
  loading: boolean;
  onDelete: (id: number) => void;
  onEdit: (record: DocEntityRelationRead) => void;
}) {
  const { t } = useTranslation();
  // 与本体管理各 tab 页同口径
  const { pagination } = useTablePagination();

  const columns = [
    { title: "文档编号", dataIndex: "documentId", width: 180 },
    {
      title: "实体类型",
      dataIndex: "entityType",
      width: 120,
    },
    { title: "实体键", dataIndex: "entityKey", width: 120 },
    {
      title: "关联类型",
      dataIndex: "relationType",
      width: 130,
      render: (v: string) => {
        const opt = DOC_RELATION_TYPE_OPTIONS.find((o) => o.value === v);
        return opt ? opt.label : v;
      },
    },
    {
      title: "操作",
      width: 160,
      render: (_: unknown, record: DocEntityRelationRead) => (
        <Space>
          <Button size="small" onClick={() => onEdit(record)}>
            {t("common.edit")}
          </Button>
          <Popconfirm
            title="确认删除此关联？"
            onConfirm={() => onDelete(record.id)}
          >
            <Button size="small" danger>
              {t("common.delete")}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={relations}
        columns={columns}
        scroll={{ x: 700 }}
        pagination={pagination}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// RAG Search
// ---------------------------------------------------------------------------

function RagSearchPanel() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<RagSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);

  const handleSearch = useCallback(async () => {
    if (!query.trim()) return;
    setLoading(true);
    try {
      const res = await searchDocuments(query, { topK: 10 });
      setResults(res);
      setSearched(true);
    } catch {
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, [query]);

  return (
    <div>
      <Card style={{ marginBottom: 16 }}>
        <Space direction="vertical" style={{ width: "100%" }}>
          <TextArea
            placeholder="输入问题，从已上传文档中检索相关内容..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            rows={2}
            onPressEnter={(e) => {
              if (!e.shiftKey) void handleSearch();
            }}
          />
          <div>
            <Button
              type="primary"
              icon={<SearchOutlined />}
              onClick={() => void handleSearch()}
              loading={loading}
            >
              检索
            </Button>
          </div>
        </Space>
      </Card>

      {searched && results.length === 0 && (
        <Alert type="warning" message="未检索到相关文档内容，请尝试其他关键词。" />
      )}

      {results.length > 0 && (
        <List
          header={<strong>检索结果（{results.length} 条）</strong>}
          dataSource={results}
          itemLayout="vertical"
          renderItem={(item) => (
            <List.Item>
              <Card size="small" title={item.document_name}>
                <Descriptions size="small" column={2}>
                  <Descriptions.Item label="文档编号">{item.document_id}</Descriptions.Item>
                  <Descriptions.Item label="相似度">
                    {(item.score * 100).toFixed(1)}%
                  </Descriptions.Item>
                </Descriptions>
                <Paragraph
                  ellipsis={{ rows: 4, expandable: true, symbol: "展开" }}
                  style={{ marginTop: 8, fontSize: 13 }}
                >
                  {item.chunk_text}
                </Paragraph>
              </Card>
            </List.Item>
          )}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function DocumentsPage() {
  const { t } = useTranslation();

  // Tab state
  const [activeTab, setActiveTab] = useState("documents");

  // Documents tab
  const [docs, setDocs] = useState<DocumentRead[]>([]);
  const [docLoading, setDocLoading] = useState(false);
  const [vectorizingId, setVectorizingId] = useState<number | null>(null);
  // —— 新建关联：可搜索下拉所需状态 ——
  const [businessObjects, setBusinessObjects] = useState<
    { code: string; name: string }[]
  >([]);
  const [relEntityType, setRelEntityType] = useState<string | undefined>();
  const [targets, setTargets] = useState<RelationTarget[]>([]);
  const [editingRel, setEditingRel] = useState<DocEntityRelationRead | null>(null);
  const [togglingAiId, setTogglingAiId] = useState<number | null>(null);
  const [targetLoading, setTargetLoading] = useState(false);
  const [docModal, setDocModal] = useState(false);
  const [editingDoc, setEditingDoc] = useState<DocumentRead | null>(null);
  const [docForm] = Form.useForm<DocFormValues>();
  const [docFilters, setDocFilters] = useState<FilterValues>({});

  // Upload modal
  const [uploadModal, setUploadModal] = useState(false);

  // Relations tab
  const [relations, setRelations] = useState<DocEntityRelationRead[]>([]);
  const [relLoading, setRelLoading] = useState(false);
  const [relModal, setRelModal] = useState(false);
  const [relForm] = Form.useForm();

  // Load documents
  const loadDocs = useCallback(async () => {
    setDocLoading(true);
    try {
      setDocs(await listDocuments());
    } catch {
      // error toast handled by axios interceptor
    } finally {
      setDocLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadDocs();
  }, [loadDocs]);

  // 业务对象码（关联对象类型下拉的「业务对象」分组）
  useEffect(() => {
    void (async () => {
      try {
        const rows = await listBusinessObjects();
        setBusinessObjects(rows.map((row) => ({ code: row.code, name: row.name })));
      } catch {
        // 拦截器已提示；类型下拉退化为只有知识对象分组
      }
    })();
  }, []);

  // Load relations
  const loadRels = useCallback(async () => {
    setRelLoading(true);
    try {
      setRelations(await listRelations({}));
    } catch {
      // error toast handled by axios interceptor
    } finally {
      setRelLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeTab === "relations") {
      void loadRels();
    }
  }, [activeTab, loadRels]);

  // Document CRUD handlers
  const openCreateDoc = () => {
    setEditingDoc(null);
    void docForm.setFieldsValue(EMPTY_DOC_FORM);
    setDocModal(true);
  };

  const openEditDoc = (record: DocumentRead) => {
    setEditingDoc(record);
    void docForm.setFieldsValue({
      documentId: record.documentId,
      documentName: record.documentName,
      documentType: record.documentType,
      version: record.version,
      owner: record.owner ?? "",
      effectiveDate: record.effectiveDate ? dayjs(record.effectiveDate) : undefined,
      securityLevel: record.securityLevel,
    });
    setDocModal(true);
  };

  /** 打开原件（浏览器内联预览/下载）。 */
  const handleOpenSource = async (record: DocumentRead) => {
    try {
      const { url, filename, revoke } = await fetchDocumentSource(record.id);
      const opened = window.open(url, "_blank");
      if (!opened) {
        // 弹窗被拦 ⇒ 退化成下载，用户至少拿到文件
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        anchor.click();
      }
      // 延迟回收：浏览器读完 blob 后即可释放
      window.setTimeout(() => revoke(), 60_000);
    } catch {
      // handled by interceptor
    }
  };

  /** 统一预览（**必须带鉴权**取 blob：裸 URL 会被后端判「请先登录」）。 */
  const handlePreview = async (record: DocumentRead) => {
    try {
      const { url, revoke } = await fetchDocumentPreview(record.id);
      const opened = window.open(url, "_blank");
      if (!opened) {
        message.warning(t("document.previewBlocked"));
      }
      window.setTimeout(() => revoke(), 60_000);
    } catch {
      // handled by interceptor
    }
  };

  /** 向量化：把已入库文档的原文灌进向量库。 */
  const handleVectorize = async (record: DocumentRead) => {
    setVectorizingId(record.id);
    try {
      const result = await vectorizeDocument(record.id);
      void message.success(
        t("document.vectorizeSuccess", { chunks: result.chunks }),
      );
      void loadDocs();
    } catch {
      // handled by interceptor
    } finally {
      setVectorizingId(null);
    }
  };

  /** AI 上下文开关（0165 人工）：关闭后该文档不再参与知识检索 / LLM 注入。 */
  const handleToggleAi = async (record: DocumentRead, enabled: boolean) => {
    setTogglingAiId(record.id);
    try {
      await updateDocument(record.id, {
        aiContextEnabled: enabled,
        aiContextNote: enabled ? null : "运营手工关闭（文本质量/适用范围待确认）",
      });
      void message.success(
        enabled ? t("document.aiEnabled") : t("document.aiDisabled"),
      );
      void loadDocs();
    } catch {
      // handled by interceptor
    } finally {
      setTogglingAiId(null);
    }
  };

  const handleDeleteDoc = async (id: number) => {
    try {
      await deleteDocument(id);
      void message.success(t("toast.deleted"));
      void loadDocs();
    } catch {
      // handled by interceptor
    }
  };

  const handleDocSubmit = async () => {
    const values = await docForm.validateFields();
    try {
      if (editingDoc) {
        const payload: DocumentUpdate = {
          documentName: values.documentName,
          documentType: values.documentType as DocumentRead["documentType"],
          version: values.version,
          owner: values.owner || undefined,
          // DatePicker 给 Dayjs ⇒ 后端 date 只收 YYYY-MM-DD（2026-11-21 实测 422 根因）
          effectiveDate: toIsoDateParam(values.effectiveDate),
          securityLevel: values.securityLevel,
        };
        await updateDocument(editingDoc.id, payload);
        void message.success(t("toast.updated"));
      } else {
        const payload: DocumentCreate = {
          documentId: values.documentId,
          documentName: values.documentName,
          documentType: values.documentType as DocumentRead["documentType"],
          version: values.version,
          owner: values.owner || undefined,
          effectiveDate: toIsoDateParam(values.effectiveDate),
          securityLevel: (values.securityLevel as DocumentRead["securityLevel"]) as DocumentRead["securityLevel"],
        };
        await createDocument(payload);
        void message.success(t("toast.created"));
      }
      setDocModal(false);
      void loadDocs();
    } catch {
      // handled by interceptor
    }
  };

  // Relation handlers
  const openCreateRel = () => {
    setEditingRel(null);
    void relForm.resetFields();
    setRelEntityType(undefined);
    setTargets([]);
    setRelModal(true);
  };

  /** 拉候选：类型必传（避免依赖还没更新的 state），关键词可空 ⇒ 可浏览。 */
  const loadTargets = useCallback((entityType: string, keyword: string) => {
    setTargetLoading(true);
    void listRelationTargets({ entityType, q: keyword })
      .then(setTargets)
      .catch(() => setTargets([]))
      .finally(() => setTargetLoading(false));
  }, []);

  /** 搜索框输入：当前类型 + 关键词（**不做防抖定时器**——antd Select 每次输入都会
   *  触发；这里用 requestId 丢弃过期响应，避免慢请求覆盖新结果）。 */
  const targetsRequestId = useRef(0);
  const searchTargets = useCallback(
    (keyword: string) => {
      if (!relEntityType) return;
      const requestId = ++targetsRequestId.current;
      setTargetLoading(true);
      void listRelationTargets({ entityType: relEntityType, q: keyword })
        .then((rows) => {
          if (requestId === targetsRequestId.current) setTargets(rows);
        })
        .catch(() => {
          if (requestId === targetsRequestId.current) setTargets([]);
        })
        .finally(() => {
          if (requestId === targetsRequestId.current) setTargetLoading(false);
        });
    },
    [relEntityType],
  );

  /** 打开「编辑关联」：回填现有值，并拉该类型下的候选（保证当前值有候选）。 */
  const openEditRel = (record: DocEntityRelationRead) => {
    setEditingRel(record);
    setRelEntityType(record.entityType);
    relForm.setFieldsValue({
      documentId: record.documentId,
      entityType: record.entityType,
      entityKey: record.entityKey,
      relationType: record.relationType,
    });
    loadTargets(record.entityType, record.entityKey);
    setRelModal(true);
  };

  const handleCreateRel = async () => {
    const values = await relForm.validateFields();
    try {
      const payload: DocEntityRelationCreate = {
        documentId: values.documentId,
        entityType: values.entityType as EntityType,
        entityKey: values.entityKey,
        relationType: values.relationType,
      };
      if (editingRel) {
        // 编辑：文档编号不可改（换了文档就是另一条关联，应新增而非改）
        await updateRelation(editingRel.id, {
          entityType: payload.entityType,
          entityKey: payload.entityKey,
          relationType: payload.relationType,
        });
        void message.success(t("toast.updated"));
      } else {
        await createRelation(payload);
        void message.success(t("toast.created"));
      }
      setRelModal(false);
      void loadRels();
    } catch {
      // handled by interceptor
    }
  };

  const handleDeleteRel = async (id: number) => {
    try {
      await deleteRelation(id);
      void message.success(t("toast.deleted"));
      void loadRels();
    } catch {
      // handled by interceptor
    }
  };

  // Filter documents
  const filteredDocs = docs.filter((d) => {
    if (!contains(d.documentId, docFilters.documentId as string ?? "")) return false;
    if (!contains(d.documentType, docFilters.documentType as string ?? "")) return false;
    if (!matchSelect(docFilters.status as string ?? "", d.status)) return false;
    return true;
  });

  const filterFields: FilterField[] = [
    { key: "documentId", label: "文档编号" },
    { key: "documentType", label: "类型", type: "select" },
    { key: "status", label: "状态", type: "select" },
  ];

  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginBottom: 16,
        }}
      >
        <span />
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void loadDocs()}>
            {t("common.refresh")}
          </Button>
          <Button icon={<UploadOutlined />} onClick={() => setUploadModal(true)}>
            上传文档
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreateDoc}>
            新建文档
          </Button>
        </Space>
      </div>

      <Tabs activeKey={activeTab} onChange={setActiveTab}>
        <TabPane tab="文档目录" key="documents">
          <FilterBar
            fields={filterFields}
            values={docFilters}
            onChange={(k, v) =>
              setDocFilters((prev) => ({ ...prev, [k]: v }))
            }
            onReset={() => setDocFilters({})}
          />
          <DocList
            docs={filteredDocs}
            loading={docLoading}
            onEdit={openEditDoc}
            onDelete={handleDeleteDoc}
            onOpenSource={(record) => void handleOpenSource(record)}
            onPreview={(record) => void handlePreview(record)}
            onVectorize={(record) => void handleVectorize(record)}
            vectorizingId={vectorizingId}
            onToggleAi={(record, enabled) => void handleToggleAi(record, enabled)}
            togglingAiId={togglingAiId}
          />
        </TabPane>

        <TabPane tab="文档关联" key="relations">
          <div
            style={{
              display: "flex",
              justifyContent: "flex-end",
              marginBottom: 16,
            }}
          >
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={openCreateRel}
            >
              新建关联
            </Button>
          </div>
          <RelationList
            relations={relations}
            loading={relLoading}
            onDelete={handleDeleteRel}
            onEdit={openEditRel}
          />
        </TabPane>

        <TabPane tab="语义检索" key="search">
          <RagSearchPanel />
        </TabPane>

        <TabPane tab="知识问答" key="qa">
          <DocumentQaPanel />
        </TabPane>
      </Tabs>

      {/* Document create/edit modal */}
      <Modal
        title={editingDoc ? "编辑文档" : "新建文档"}
        open={docModal}
        onOk={() => void handleDocSubmit()}
        onCancel={() => setDocModal(false)}
        okText={t("common.confirm")}
        cancelText={t("common.cancel")}
        width={640}
        destroyOnHidden
      >
        <Form form={docForm} layout="vertical">
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item
              name="documentId"
              label="文档编号"
              rules={[{ required: true, message: "必填" }]}
              style={{ width: "50%" }}
            >
              <Input placeholder="如 DOC-2026-001" disabled={!!editingDoc} />
            </Form.Item>
            <Form.Item
              name="documentName"
              label="文档名称"
              rules={[{ required: true, message: "必填" }]}
              style={{ width: "50%" }}
            >
              <Input placeholder="如 供应商质量协议 V2" />
            </Form.Item>
          </Space.Compact>
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item
              name="documentType"
              label="文档类型"
              style={{ width: "33%" }}
            >
              <Select options={DOCUMENT_TYPE_OPTIONS} />
            </Form.Item>
            <Form.Item name="version" label="版本" style={{ width: "33%" }}>
              <Input placeholder="v1.0" />
            </Form.Item>
            <Form.Item
              name="securityLevel"
              label="安全级别"
              style={{ width: "33%" }}
            >
              <Select options={DOCUMENT_SECURITY_OPTIONS} />
            </Form.Item>
          </Space.Compact>
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item name="owner" label="Owner" style={{ width: "50%" }}>
              <Input placeholder="负责人" />
            </Form.Item>
            <Form.Item
              name="effectiveDate"
              label="生效日期"
              style={{ width: "50%" }}
            >
              <DatePicker style={{ width: "100%" }} format="YYYY-MM-DD" />
            </Form.Item>
          </Space.Compact>
        </Form>
      </Modal>

      <DocumentUploadModal
        open={uploadModal}
        onClose={() => setUploadModal(false)}
        onUploaded={() => {
          setUploadModal(false);
          void loadDocs();
        }}
      />

      {/* Relation create modal */}
      <Modal
        title={editingRel ? "编辑文档关联" : "新建文档关联"}
        open={relModal}
        onOk={() => void handleCreateRel()}
        onCancel={() => setRelModal(false)}
        okText={t("common.confirm")}
        cancelText={t("common.cancel")}
        width={560}
        destroyOnHidden
      >
        <Form
          form={relForm}
          layout="vertical"
          onValuesChange={(changed: Record<string, unknown>) => {
            if ("entityType" in changed) {
              const nextType = changed.entityType as string | undefined;
              setRelEntityType(nextType);
              // 换类型 ⇒ 旧的目标键一定无效（不同命名空间）
              relForm.setFieldValue("entityKey", undefined);
              setTargets([]);
              if (nextType) loadTargets(nextType, "");
            }
          }}
        >
          {/* 文档：可搜下拉（按编号或名称），不再让用户手打编号 */}
          <Form.Item
            name="documentId"
            label="文档"
            rules={[{ required: true, message: "请选择文档" }]}
            extra="可输入文档编号或名称过滤"
          >
            <Select
              showSearch
              optionFilterProp="label"
              disabled={editingRel !== null}
              placeholder="搜索并选择文档"
              options={docs.map((doc) => ({
                value: doc.documentId,
                label: `${doc.documentId} · ${doc.documentName}`,
              }))}
            />
          </Form.Item>

          {/* 关联对象类型：业务对象码（动态）+ 三类知识对象 */}
          <Form.Item
            name="entityType"
            label="关联对象类型"
            rules={[{ required: true, message: "请选择类型" }]}
            extra="一份制度/合同通常关联「本体类 / 指标 / 属性」，而不是某个具体业务单据"
          >
            <Select
              placeholder="选择关联对象类型"
              options={[
                {
                  label: "本体类（知识）",
                  options: [
                    { value: "ONTOLOGY_CLASS", label: "本体类" },
                    { value: "ONTOLOGY_METRIC", label: "指标" },
                    { value: "ONTOLOGY_PROPERTY", label: "属性" },
                  ],
                },
                {
                  label: "业务对象",
                  options: businessObjects.map((item) => ({
                    value: item.code,
                    label: item.name ? `${item.name}（${item.code}）` : item.code,
                  })),
                },
              ]}
            />
          </Form.Item>

          {/* 目标键：按类型给可搜索候选（不再让用户猜 enterprise_key） */}
          <Form.Item
            name="entityKey"
            label="关联对象"
            rules={[{ required: true, message: "请选择关联对象" }]}
            extra={
              relEntityType
                ? targetHintFor(relEntityType)
                : "先选类型，再从这里搜索选择"
            }
          >
            <Select
              showSearch
              filterOption={false}
              onSearch={(value) => void searchTargets(value)}
              placeholder={relEntityType ? "输入关键词搜索，或留空浏览前 20 项" : "先选择关联对象类型"}
              disabled={!relEntityType}
              loading={targetLoading}
              options={targets.map((target) => ({
                value: target.key,
                label: `${target.label}${target.hint && target.hint !== target.label ? `（${target.hint}）` : ""}`,
              }))}
              notFoundContent={targetLoading ? "搜索中…" : "没有匹配项"}
            />
          </Form.Item>

          <Form.Item name="relationType" label="关联类型">
            <Select
              options={DOC_RELATION_TYPE_OPTIONS}
              placeholder="默认合同"
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
