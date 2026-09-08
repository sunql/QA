/** 文档目录管理页（Phase 5.1 + 5.2 前端补全）。
 *
 * 三个 Tab：
 * 1. 文档目录 — 列表 + 创建 + 编辑 + 删除 + 上传文件
 * 2. 文档关联 — 将文档关联到供应商/物料/PO 等业务实体
 * 3. 语义检索 — 输入问题，从已上传文档中检索相关内容片段
 */

import { useEffect, useState, useCallback } from "react";
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
  Upload,
  Card,
  Typography,
  Descriptions,
  List,
  Alert,
} from "antd";
import {
  PlusOutlined,
  ReloadOutlined,
  UploadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import type { UploadFile } from "antd/es/upload/interface";
import {
  listDocuments,
  createDocument,
  updateDocument,
  deleteDocument,
  uploadDocument,
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
import type { FilterValues } from "../utils/ontologyFilter";
import type { EntityType } from "../types/entityMapping";
import { DocumentQaPanel } from "../components/documents/DocumentQaPanel";

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

interface DocFormValues {
  documentId: string;
  documentName: string;
  documentType: string;
  version: string;
  owner: string;
  effectiveDate: string;
  securityLevel: string;
}

const EMPTY_DOC_FORM: DocFormValues = {
  documentId: "",
  documentName: "",
  documentType: "CONTRACT",
  version: "v1.0",
  owner: "",
  effectiveDate: "",
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
}: {
  docs: DocumentRead[];
  loading: boolean;
  onEdit: (record: DocumentRead) => void;
  onDelete: (id: number) => void;
}) {
  const { t } = useTranslation();

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
      title: "操作",
      width: 140,
      render: (_: unknown, record: DocumentRead) => (
        <Space>
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
        pagination={{ pageSize: 20 }}
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
}: {
  relations: DocEntityRelationRead[];
  loading: boolean;
  onDelete: (id: number) => void;
}) {
  const { t } = useTranslation();

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
      width: 100,
      render: (_: unknown, record: DocEntityRelationRead) => (
        <Popconfirm
          title="确认删除此关联？"
          onConfirm={() => onDelete(record.id)}
        >
          <Button size="small" danger>
            {t("common.delete")}
          </Button>
        </Popconfirm>
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
        pagination={{ pageSize: 20 }}
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
  const [docModal, setDocModal] = useState(false);
  const [editingDoc, setEditingDoc] = useState<DocumentRead | null>(null);
  const [docForm] = Form.useForm<DocFormValues>();
  const [docFilters, setDocFilters] = useState<FilterValues>({});

  // Upload modal
  const [uploadModal, setUploadModal] = useState(false);
  const [uploadFile, setUploadFile] = useState<UploadFile | null>(null);
  const [uploadForm] = Form.useForm();
  const [uploading, setUploading] = useState(false);

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
      effectiveDate: record.effectiveDate ?? "",
      securityLevel: record.securityLevel,
    });
    setDocModal(true);
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
          effectiveDate: values.effectiveDate || undefined,
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
          effectiveDate: values.effectiveDate || undefined,
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

  // Upload handler
  const handleUpload = async () => {
    if (!uploadFile?.originFileObj) {
      void message.warning("请选择要上传的文件");
      return;
    }
    const values = await uploadForm.validateFields();
    setUploading(true);
    try {
      const result = await uploadDocument(uploadFile.originFileObj, {
        documentType: values.documentType,
        version: values.version,
        owner: values.owner,
        effectiveDate: values.effectiveDate,
        securityLevel: values.securityLevel,
      });
      void message.success(
        `上传成功：${result.documentId}（${result.chunks} 个 chunks 已向量化）`,
      );
      setUploadModal(false);
      setUploadFile(null);
      void uploadForm.resetFields();
      void loadDocs();
    } catch {
      // handled by interceptor
    } finally {
      setUploading(false);
    }
  };

  // Relation handlers
  const openCreateRel = () => {
    void relForm.resetFields();
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
      await createRelation(payload);
      void message.success(t("toast.created"));
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
        destroyOnClose
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
              <Input placeholder="YYYY-MM-DD" />
            </Form.Item>
          </Space.Compact>
        </Form>
      </Modal>

      {/* Upload modal */}
      <Modal
        title="上传文档（RAG 向量化）"
        open={uploadModal}
        onOk={() => void handleUpload()}
        onCancel={() => {
          setUploadModal(false);
          setUploadFile(null);
        }}
        okText={t("common.confirm")}
        cancelText={t("common.cancel")}
        width={560}
        destroyOnClose
        confirmLoading={uploading}
      >
        <Form form={uploadForm} layout="vertical">
          <Form.Item
            name="file"
            label="选择文件"
            rules={[{ required: true, message: "请选择文件" }]}
          >
            <Upload.Dragger
              accept=".pdf,.docx,.doc,.txt,.md"
              maxCount={1}
              fileList={uploadFile ? [uploadFile] : []}
              beforeUpload={(file) => {
                setUploadFile({
                  uid: "-1",
                  name: file.name,
                  status: "done",
                  originFileObj: file,
                });
                return false; // prevent auto upload
              }}
              onRemove={() => setUploadFile(null)}
            >
              <p>
                <UploadOutlined />
              </p>
              <p>点击或拖拽上传 PDF / DOCX / TXT / MD 文件</p>
            </Upload.Dragger>
          </Form.Item>
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item name="documentType" label="文档类型" style={{ width: "50%" }}>
              <Select options={DOCUMENT_TYPE_OPTIONS} />
            </Form.Item>
            <Form.Item name="securityLevel" label="安全级别" style={{ width: "50%" }}>
              <Select options={DOCUMENT_SECURITY_OPTIONS} />
            </Form.Item>
          </Space.Compact>
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item name="version" label="版本" style={{ width: "50%" }}>
              <Input placeholder="v1.0" />
            </Form.Item>
            <Form.Item name="owner" label="Owner" style={{ width: "50%" }}>
              <Input placeholder="负责人" />
            </Form.Item>
          </Space.Compact>
        </Form>
        <Alert
          type="info"
          showIcon
          message="上传后将自动解析文档内容、分块并生成向量，存入 Milvus 文档向量库。"
          style={{ marginTop: 8 }}
        />
      </Modal>

      {/* Relation create modal */}
      <Modal
        title="新建文档关联"
        open={relModal}
        onOk={() => void handleCreateRel()}
        onCancel={() => setRelModal(false)}
        okText={t("common.confirm")}
        cancelText={t("common.cancel")}
        width={480}
        destroyOnClose
      >
        <Form form={relForm} layout="vertical">
          <Form.Item
            name="documentId"
            label="文档编号"
            rules={[{ required: true, message: "必填" }]}
          >
            <Input placeholder="如 DOC-2026-001" />
          </Form.Item>
          <Form.Item
            name="entityType"
            label="实体类型"
            rules={[{ required: true, message: "必填" }]}
          >
            <Select
              options={[
                { value: "SUPPLIER", label: "供应商" },
                { value: "MATERIAL", label: "物料" },
                { value: "PO", label: "采购订单" },
                { value: "GR", label: "收货单" },
                { value: "IQC", label: "来料检验" },
                { value: "NCR", label: "不合格处理" },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="entityKey"
            label="实体键（enterprise_key）"
            rules={[{ required: true, message: "必填" }]}
          >
            <Input type="number" placeholder="如 100001" />
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
