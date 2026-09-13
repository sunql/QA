/**
 * 知识导入向导页（feat-wiki-knowledge M2）。
 *
 * 四步：粘贴原文 → 选模型 → 预览/编辑草稿 → 入库。
 *
 * 「先预览再入库」是刻意设计：切分规则是确定性的，但用户最清楚一段文字
 * 该是三条知识还是一条，所以草稿在落库前必须可编辑、可删。
 *
 * 错误处理约定：`httpClient` 的响应拦截器已经 `message.error` 过并 reject 出
 * 带 `status`/`detail` 的 Error，所以这里**不再重复 toast**，只对可采取行动的
 * 状态（503 选错模型）给出针对性提示，其余用页面级 Alert 常驻展示。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Input,
  Progress,
  Select,
  Space,
  Steps,
  Switch,
  Table,
  Tag,
  Upload,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { UploadOutlined } from "@ant-design/icons";
import { useTranslation } from "../i18n";
import {
  executeImport,
  listImportModels,
  listImportTasks,
  previewImport,
  previewImportFile,
} from "../api/wikiImport";
import type {
  WikiImportDraft,
  WikiImportModel,
  WikiImportTask,
} from "../types/wikiImport";

const STEP_SOURCE = 0;
const STEP_MODEL = 1;
const STEP_PREVIEW = 2;
const STEP_CONFIRM = 3;

/** 与后端 `MAX_IMPORT_DRAFTS` 对齐：超过会被 422 拒绝 */
const MAX_DRAFTS = 200;

const SOURCE_TYPES = ["MARKDOWN", "PDF", "WORD", "CSV", "API"] as const;

/**
 * 允许上传的格式。与后端 `document_parser.parse_document` 的白名单一致：
 * CSV/API 已从下拉里保留（历史台账要能显示），但**不支持上传**——CSV 需要
 * 独立的「列 → 标题/正文」映射规则，不是解析器能顺带解决的事。
 */
const UPLOAD_ACCEPT = ".pdf,.docx,.txt,.md,.markdown";

/** 任务状态 → antd Tag 颜色 */
const STATUS_COLOR: Record<string, string> = {
  SUCCEEDED: "green",
  PARTIAL: "orange",
  FAILED: "red",
  RUNNING: "blue",
  PENDING: "default",
};

// ---------------------------------------------------------------------------
// 子组件：草稿编辑器
// ---------------------------------------------------------------------------

interface DraftsEditorProps {
  drafts: WikiImportDraft[];
  onChange: (index: number, patch: Partial<WikiImportDraft>) => void;
  onRemove: (index: number) => void;
}

function DraftsEditor({ drafts, onChange, onRemove }: DraftsEditorProps) {
  const { t } = useTranslation();

  return (
    <div>
      {drafts.map((draft, index) => (
        <div
          // 草稿没有稳定 id（用户可能在预览页新增/删除），用下标作 key 是
          // 这里唯一可行的选择：受控输入的值都来自 state，不依赖 DOM 状态。
          key={index}
          style={{
            border: "1px solid #f0f0f0",
            borderRadius: 6,
            padding: 16,
            marginBottom: 16,
          }}
        >
          <Space style={{ marginBottom: 8 }} align="center">
            <span style={{ fontWeight: 500 }}>
              {t("wikiImport.draftIndex", { index: index + 1 })}
            </span>
            <Button size="small" danger onClick={() => onRemove(index)}>
              {t("wikiImport.actions.removeDraft")}
            </Button>
          </Space>
          <div style={{ marginBottom: 4, fontSize: 14, color: "rgba(0, 0, 0, 0.88)" }}>
            {t("wikiImport.columns.title")}
          </div>
          <Input
            aria-label={t("wikiImport.columns.title")}
            value={draft.title}
            maxLength={200}
            onChange={(e) => onChange(index, { title: e.target.value })}
            style={{ marginBottom: 8 }}
          />
          <Input.TextArea
            aria-label={t("wikiImport.columns.content")}
            value={draft.content}
            autoSize={{ minRows: 4, maxRows: 12 }}
            onChange={(e) => onChange(index, { content: e.target.value })}
          />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主页面
// ---------------------------------------------------------------------------

export default function AdminWikiImportPage() {
  const { t } = useTranslation();

  const [current, setCurrent] = useState(STEP_SOURCE);

  const [source, setSource] = useState("");
  const [sourceType, setSourceType] = useState<string>("MARKDOWN");
  const [sourceRef, setSourceRef] = useState("");

  const [models, setModels] = useState<WikiImportModel[]>([]);
  const [modelId, setModelId] = useState<number | null>(null);
  const [fallbackModelId, setFallbackModelId] = useState<number | null>(null);
  const [autoClassify, setAutoClassify] = useState(true);
  const [useTwoStep, setUseTwoStep] = useState(false);

  const [drafts, setDrafts] = useState<WikiImportDraft[]>([]);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<WikiImportTask | null>(null);
  const [tasks, setTasks] = useState<WikiImportTask[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [modelsError, setModelsError] = useState(false);
  // 与 `loading`（切分）分开：上传解析是另一件事，共用一个 loading 会让
  // 「解析中」的按钮转圈出现在错误的按钮上。
  const [uploading, setUploading] = useState(false);

  const fetchModels = useCallback(async () => {
    try {
      setModels(await listImportModels());
      setModelsError(false);
    } catch {
      // 拦截器已 toast 过后端原因。这里必须落一个状态：拉不到模型时下拉是空的，
      // 若自动分类开着，「下一步」会永久禁用 —— 用户会被困死在选模步，
      // 所以选模步要据此给出重试入口。
      setModelsError(true);
    }
  }, []);

  const fetchTasks = useCallback(async () => {
    try {
      const res = await listImportTasks({ limit: 20, offset: 0 });
      setTasks(res.rows);
    } catch {
      // 台账是附加信息，拉取失败不该打断导入主流程
    }
  }, []);

  useEffect(() => {
    void fetchModels();
    void fetchTasks();
  }, [fetchModels, fetchTasks]);

  /** 关闭自动分类时模型可省；开启则必须选一个能调的 */
  const canLeaveModelStep = !autoClassify || modelId !== null;

  /** 后端 `execute` 对 drafts 有 `MAX_IMPORT_DRAFTS` 上限，超了必 422 —— 在发请求前就挡住 */
  const isOverDraftCap = drafts.length > MAX_DRAFTS;

  const handlePreview = useCallback(async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await previewImport(source);
      setDrafts(res.drafts);
      setCurrent(STEP_PREVIEW);
    } catch {
      setErrorMsg(t("wikiImport.errors.previewFailed"));
    } finally {
      setLoading(false);
    }
  }, [source, t]);

  /**
   * 上传文件 → 解析文本回填到原文框。
   *
   * 刻意**不**直接跳到预览步：解析出的文本是 PDF/Word 的有损抽取（版式、
   * 表格会丢），用户需要先核对；而且中间还夹着「选模型」步不能跳。
   * 回填后走的是与粘贴完全相同的链路（下一步 → `/preview` 切分）。
   */
  const handleUpload = useCallback(
    async (raw: File) => {
      setUploading(true);
      setErrorMsg(null);
      try {
        const res = await previewImportFile(raw);
        setSource(res.text);
        setSourceType(res.sourceType);
        // 文件名回填来源出处：这是溯源信息，默认值比让用户再手打一遍合理，
        // 但仍可编辑（后端 sourceRef 允许 500 字符，不强制是文件名）。
        setSourceRef(raw.name);
      } catch (e) {
        // 拦截器已 toast。这里给页面级常驻提示，因为上传失败时用户最容易
        // 怀疑「是不是文件本身有问题」，需要一句能行动的指引。
        // 413 单独分流：让用户去「确认格式」是错的指引——他要做的是换个小文件。
        const err = e as Error & { status?: number };
        setErrorMsg(
          err.status === 413
            ? t("wikiImport.errors.fileTooLarge")
            : t("wikiImport.errors.parseFileFailed"),
        );
      } finally {
        setUploading(false);
      }
    },
    [t],
  );

  const handleExecute = useCallback(async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const task = await executeImport({
        drafts,
        modelId: autoClassify ? modelId : null,
        fallbackModelId,
        autoClassify,
        useTwoStep,
        sourceType,
        sourceRef: sourceRef.trim() || null,
      });
      setResult(task);
      setCurrent(STEP_CONFIRM);
      void fetchTasks();
    } catch (e) {
      const err = e as Error & { status?: number };
      // 503 = 模型不可用（没配 key / 已停用）。这是**可行动**的错误：把用户送回
      // 选模步，并把可操作的提示留在那里的页面级 Alert 上。
      // 不再额外 toast —— 拦截器已经用后端原文 toast 过一次，再弹一条是同一件事
      // 说两遍，且留在原地也帮不上用户。
      if (err.status === 503) {
        setErrorMsg(t("wikiImport.errors.modelUnusable"));
        setCurrent(STEP_MODEL);
        return;
      }
      setErrorMsg(err.message);
    } finally {
      setLoading(false);
    }
  }, [
    drafts,
    modelId,
    fallbackModelId,
    autoClassify,
    useTwoStep,
    sourceType,
    sourceRef,
    t,
    fetchTasks,
  ]);

  const handleDraftChange = useCallback(
    (index: number, patch: Partial<WikiImportDraft>) => {
      setDrafts((prev) =>
        prev.map((d, i) => (i === index ? { ...d, ...patch } : d)),
      );
    },
    [],
  );

  const handleDraftRemove = useCallback((index: number) => {
    setDrafts((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const resetWizard = useCallback(() => {
    setCurrent(STEP_SOURCE);
    setSource("");
    setSourceRef("");
    // sourceType 也要回默认：上传 PDF 会把它置成 PDF，不重置的话下一批
    // 粘贴的 Markdown 会带着上一批的「PDF」落进台账，溯源就错了。
    setSourceType("MARKDOWN");
    setDrafts([]);
    setResult(null);
    setErrorMsg(null);
  }, []);

  const modelOptions = useMemo(
    () =>
      models.map((m) => ({
        value: m.id,
        label: m.usable
          ? `${m.modelName} (${m.provider})`
          : `${m.modelName} (${m.provider}) — ${t("wikiImport.modelUnusable")}`,
        // 不可用模型保留在下拉里但不可选：直接消失会让用户以为「模型没配」
        disabled: !m.usable,
      })),
    [models, t],
  );

  const taskColumns: ColumnsType<WikiImportTask> = useMemo(
    () => [
      { title: "ID", dataIndex: "id", key: "id", width: 80 },
      {
        title: t("wikiImport.columns.status"),
        dataIndex: "status",
        key: "status",
        render: (status: string) => (
          <Tag color={STATUS_COLOR[status] ?? "default"}>{status}</Tag>
        ),
      },
      { title: t("wikiImport.columns.total"), dataIndex: "totalPages", key: "totalPages", width: 80 },
      { title: t("wikiImport.columns.success"), dataIndex: "successPages", key: "successPages", width: 80 },
      { title: t("wikiImport.columns.skipped"), dataIndex: "skippedPages", key: "skippedPages", width: 80 },
      { title: t("wikiImport.columns.failed"), dataIndex: "failedPages", key: "failedPages", width: 80 },
      {
        title: t("wikiImport.columns.errorMessage"),
        dataIndex: "errorMessage",
        key: "errorMessage",
        render: (v: string | null) => v ?? "-",
      },
    ],
    [t],
  );

  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ marginBottom: 24 }}>{t("wikiImport.title")}</h2>

      <Steps
        current={current}
        items={[
          { title: t("wikiImport.steps.source") },
          { title: t("wikiImport.steps.model") },
          { title: t("wikiImport.steps.preview") },
          { title: t("wikiImport.steps.confirm") },
        ]}
        style={{ marginBottom: 32 }}
      />

      {errorMsg && (
        <Alert
          type="error"
          showIcon
          message={errorMsg}
          style={{ marginBottom: 16 }}
          closable
          onClose={() => setErrorMsg(null)}
        />
      )}

      {/* 步骤 1：来源 */}
      {current === STEP_SOURCE && (
        <div>
          <Upload
            accept={UPLOAD_ACCEPT}
            // LIST_IGNORE 一举三得：不自动 POST（我们要的是「解析成文本」
            // 而不是「上传到某处」）、不进文件列表、**且 rc-upload 会清空
            // input.value** —— 否则用户重新选同一个文件时浏览器不发 change，
            // 表现为「点了没反应」。
            beforeUpload={(file) => {
              void handleUpload(file);
              return Upload.LIST_IGNORE;
            }}
          >
            <Button icon={<UploadOutlined />} loading={uploading} style={{ marginBottom: 8 }}>
              {uploading ? t("wikiImport.uploadButtonUploading") : t("wikiImport.uploadButton")}
            </Button>
          </Upload>
          {uploading && (
            <Alert
              type="info"
              showIcon
              message={t("wikiImport.uploadProgress")}
              description={<Progress percent={99} status="active" size="small" />}
              style={{ marginBottom: 8 }}
            />
          )}
          <div style={{ color: "#888", marginBottom: 8 }}>
            {t("wikiImport.uploadHint")}
          </div>

          <label style={{ fontWeight: 500, display: "block", marginBottom: 8 }}>
            {t("wikiImport.sourceLabel")}
          </label>
          <Input.TextArea
            aria-label={t("wikiImport.sourceLabel")}
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder={t("wikiImport.sourcePlaceholder")}
            autoSize={{ minRows: 10, maxRows: 24 }}
            style={{ marginBottom: 16 }}
          />
          <Space style={{ marginBottom: 16 }} wrap>
            <span>{t("wikiImport.sourceTypeLabel")}</span>
            <Select
              style={{ width: 160 }}
              value={sourceType}
              onChange={setSourceType}
              options={SOURCE_TYPES.map((s) => ({ value: s, label: s }))}
            />
            <span>{t("wikiImport.sourceRefLabel")}</span>
            <Input
              style={{ width: 320 }}
              value={sourceRef}
              maxLength={500}
              onChange={(e) => setSourceRef(e.target.value)}
              placeholder={t("wikiImport.sourceRefPlaceholder")}
            />
          </Space>
          <div>
            <Button
              type="primary"
              disabled={!source.trim()}
              onClick={() => setCurrent(STEP_MODEL)}
            >
              {t("common.next")}
            </Button>
          </div>
        </div>
      )}

      {/* 步骤 2：选模型 */}
      {current === STEP_MODEL && (
        <div>
          <Space style={{ marginBottom: 16 }}>
            <span>{t("wikiImport.autoClassifyLabel")}</span>
            <Switch checked={autoClassify} onChange={setAutoClassify} />
            <span style={{ color: "#888" }}>{t("wikiImport.autoClassifyHint")}</span>
          </Space>

          {autoClassify && (
            <Space style={{ marginBottom: 16 }}>
              <span>{t("wikiImport.useTwoStepLabel")}</span>
              <Switch checked={useTwoStep} onChange={setUseTwoStep} />
              <span style={{ color: "#888" }}>{t("wikiImport.useTwoStepHint")}</span>
            </Space>
          )}

          {autoClassify && modelsError && (
            <Alert
              type="warning"
              showIcon
              message={t("wikiImport.errors.modelsLoadFailed")}
              action={
                <Button size="small" onClick={() => void fetchModels()}>
                  {t("wikiImport.actions.reloadModels")}
                </Button>
              }
              style={{ marginBottom: 16 }}
            />
          )}

          {autoClassify && (
            <div>
              <label style={{ fontWeight: 500, display: "block", marginBottom: 8 }}>
                {t("wikiImport.modelLabel")}
              </label>
              <Select
                style={{ width: 360, marginBottom: 16 }}
                value={modelId}
                onChange={setModelId}
                options={modelOptions}
                placeholder={t("wikiImport.modelPlaceholder")}
                notFoundContent={t("wikiImport.noModels")}
              />

              <label style={{ fontWeight: 500, display: "block", marginBottom: 8 }}>
                {t("wikiImport.fallbackModelLabel")}
              </label>
              <Select
                style={{ width: 360 }}
                value={fallbackModelId}
                onChange={setFallbackModelId}
                options={modelOptions}
                allowClear
                placeholder={t("wikiImport.fallbackModelPlaceholder")}
              />
            </div>
          )}

          <div style={{ marginTop: 24 }}>
            <Space>
              <Button onClick={() => setCurrent(STEP_SOURCE)}>{t("common.prev")}</Button>
              <Button
                type="primary"
                disabled={!canLeaveModelStep}
                loading={loading}
                onClick={() => void handlePreview()}
              >
                {t("common.next")}
              </Button>
            </Space>
          </div>
        </div>
      )}

      {/* 步骤 3：预览/编辑草稿 */}
      {current === STEP_PREVIEW && (
        <div>
          <p>
            {t("wikiImport.previewSummary", {
              count: drafts.length,
              max: MAX_DRAFTS,
            })}
          </p>
          {isOverDraftCap && (
            <Alert
              type="warning"
              showIcon
              message={t("wikiImport.errors.tooManyDrafts", {
                count: drafts.length,
                max: MAX_DRAFTS,
              })}
              style={{ marginBottom: 16 }}
            />
          )}
          <DraftsEditor
            drafts={drafts}
            onChange={handleDraftChange}
            onRemove={handleDraftRemove}
          />
          <Space>
            <Button onClick={() => setCurrent(STEP_MODEL)}>{t("common.prev")}</Button>
            <Button
              onClick={() => void handlePreview()}
              loading={loading}
            >
              {t("wikiImport.actions.repreview")}
            </Button>
            <Button
              type="primary"
              loading={loading}
              disabled={drafts.length === 0 || isOverDraftCap}
              onClick={() => void handleExecute()}
            >
              {t("wikiImport.actions.execute")}
            </Button>
          </Space>
        </div>
      )}

      {/* 步骤 4：结果 */}
      {current === STEP_CONFIRM && result && (
        <div>
          <Alert
            type={result.status === "SUCCEEDED" ? "success" : "warning"}
            showIcon
            message={t("wikiImport.resultMessage", { status: result.status })}
            description={
              <div>
                <div>
                  {t("wikiImport.resultCounts", {
                    total: result.totalPages,
                    success: result.successPages,
                    skipped: result.skippedPages,
                    failed: result.failedPages,
                    cost: result.totalCostUsd,
                  })}
                </div>
                {result.errorMessage && <div>{result.errorMessage}</div>}
              </div>
            }
            style={{ marginBottom: 16 }}
          />
          <Button type="primary" onClick={resetWizard}>
            {t("wikiImport.actions.importAnother")}
          </Button>
        </div>
      )}

      <h3 style={{ marginTop: 40 }}>{t("wikiImport.tasksTitle")}</h3>
      <Table
        rowKey="id"
        size="small"
        columns={taskColumns}
        dataSource={tasks}
        pagination={false}
        locale={{ emptyText: t("wikiImport.noTasks") }}
      />
    </div>
  );
}
