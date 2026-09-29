/** 通用「批量关系引擎」弹窗（本体页头部按钮打开）。
 *
 * 可任选其一或多个动作批量建立关系：
 *  - 本体入图 syncGraph    PG 类/属性全量 upsert 成 Neo4j 节点 + 边（幂等补图）
 *  - 共享列推断 inferJoins X3 命名约定 + 通用共享列（一方 PK）推断 JOIN，先预览后执行
 *  - 应用清单 applyManifest  JSON 粘贴或 CSV 上传，新增/更新 JOIN 与语义关系
 * 已存在关系按 onConflict：skip（跳过）/ overwrite（覆盖差异字段）。
 * 「预览」走只读端点不落库；「执行」才真正写入。
 *
 * 错误处理：清单 JSON 客户端先校验（错误不提交）；后端行级错误进入 errors 折叠展示；
 * api 层失败已由 axios 拦截器 toast，这里仅清 loading。
 */
import { useEffect, useRef, useState } from "react";
import {
  Modal,
  Button,
  Checkbox,
  Radio,
  Segmented,
  Space,
  Input,
  Upload,
  Alert,
  Descriptions,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import {
  EyeOutlined,
  ThunderboltOutlined,
  DownloadOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import type { UploadFile } from "antd/es/upload/interface";
import {
  runOntologyBatch,
  previewOntologyBatch,
  parseBatchCsv,
  downloadBatchTemplate,
} from "../../api/ontology";
import type {
  BatchCounts,
  BatchRelationRequest,
  BatchRelationResult,
  BatchRowError,
  BatchTemplateKind,
  OnConflictPolicy,
  RelationManifest,
} from "../../types/ontology";
import { useTranslation } from "../../i18n";
import { downloadBlob } from "../../utils/download";

const { TextArea } = Input;
const { Text } = Typography;

interface BatchRelationModalProps {
  open: boolean;
  onClose: () => void;
}

interface BatchActionState {
  syncGraph: boolean;
  inferJoins: boolean;
  applyManifest: boolean;
}

type RunMode = "none" | "preview" | "execute";

type ClientResult =
  | { kind: "error"; message: string }
  | { kind: "result"; mode: RunMode; result: BatchRelationResult };

const EMPTY_ACTIONS: BatchActionState = {
  syncGraph: false,
  inferJoins: false,
  applyManifest: false,
};

type ParseResult = { manifest: RelationManifest } | { empty: true } | { error: string };

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null;
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isStr = (v: unknown): v is string => typeof v === "string";
const isStrArray = (v: unknown): v is string[] =>
  Array.isArray(v) && v.every(isStr);

/** 校验单个 join 清单行；返回错误文案或 null。 */
function validateJoinRow(v: unknown, i: number): string | null {
  if (!isRecord(v)) return `joins[${i}] 须为对象`;
  if (!isNum(v.sourceClassId) || !isNum(v.targetClassId))
    return `joins[${i}] sourceClassId/targetClassId 须为数字`;
  if (!isStrArray(v.sourceColumns) || v.sourceColumns.length === 0)
    return `joins[${i}] sourceColumns 须为非空字符串数组`;
  if (!isStrArray(v.targetColumns) || v.targetColumns.length === 0)
    return `joins[${i}] targetColumns 须为非空字符串数组`;
  if (v.joinType !== undefined && !isStr(v.joinType))
    return `joins[${i}] joinType 须为字符串`;
  if (v.relationType !== undefined && !isStr(v.relationType))
    return `joins[${i}] relationType 须为字符串`;
  return null;
}

/** 校验单个语义关系行；返回错误文案或 null。 */
function validateRelationRow(v: unknown, i: number): string | null {
  if (!isRecord(v)) return `relations[${i}] 须为对象`;
  if (!isNum(v.sourceClassId) || !isNum(v.targetClassId))
    return `relations[${i}] sourceClassId/targetClassId 须为数字`;
  if (!isStr(v.relationType) || v.relationType.trim() === "")
    return `relations[${i}] relationType 须为非空字符串`;
  return null;
}

/** JSON 粘贴 → RelationManifest。空输入返回 { empty: true }，结构/字段非法返回 { error }。 */
function parseJsonManifest(text: string): ParseResult {
  const trimmed = text.trim();
  if (!trimmed) return { empty: true };
  let raw: unknown;
  try {
    raw = JSON.parse(trimmed);
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
  if (!isRecord(raw)) return { error: "manifest must be an object" };
  const { joins, relations } = raw as { joins?: unknown; relations?: unknown };
  if (joins !== undefined && !Array.isArray(joins))
    return { error: "manifest.joins must be an array" };
  if (relations !== undefined && !Array.isArray(relations))
    return { error: "manifest.relations must be an array" };
  const joinRows = joins as unknown[] | undefined;
  const relationRows = relations as unknown[] | undefined;
  for (let i = 0; joinRows !== undefined && i < joinRows.length; i++) {
    const err = validateJoinRow(joinRows[i], i);
    if (err) return { error: err };
  }
  for (let i = 0; relationRows !== undefined && i < relationRows.length; i++) {
    const err = validateRelationRow(relationRows[i], i);
    if (err) return { error: err };
  }
  return {
    manifest: {
      joins: (joinRows ?? []) as RelationManifest["joins"],
      relations: (relationRows ?? []) as RelationManifest["relations"],
    },
  };
}

/** 行错误/后端 errors → 扁平错误消息列表（用于折叠展示）。 */
function flattenRowErrors(joins: BatchCounts, relations: BatchCounts): BatchRowError[] {
  return [
    ...joins.errors.map((e) => ({ ...e, message: `joins[${e.index}] ${e.message}` })),
    ...relations.errors.map((e) => ({ ...e, message: `relations[${e.index}] ${e.message}` })),
  ];
}

const EMPTY_FILE_LIST: UploadFile[] = [];

/** 批量关系引擎弹窗：勾选动作 → （选填清单）→ 预览 / 执行。 */
export default function BatchRelationModal({ open, onClose }: BatchRelationModalProps) {
  const { t } = useTranslation();
  const batch = "forms.ontology.batchRelations";
  const [actions, setActions] = useState<BatchActionState>(EMPTY_ACTIONS);
  const [onConflict, setOnConflict] = useState<OnConflictPolicy>("skip");
  const [sourceMode, setSourceMode] = useState<"json" | "csv">("json");
  const [csvKind, setCsvKind] = useState<BatchTemplateKind>("relations");
  const [jsonText, setJsonText] = useState("");
  const [csvManifest, setCsvManifest] = useState<RelationManifest | null>(null);
  const [parsingCsv, setParsingCsv] = useState(false);
  const [downloadKind, setDownloadKind] = useState<BatchTemplateKind | null>(null);
  const [csvFileList, setCsvFileList] = useState<UploadFile[]>(EMPTY_FILE_LIST);
  const [mode, setMode] = useState<RunMode>("none");
  const [clientResult, setClientResult] = useState<ClientResult | null>(null);
  const inFlightRef = useRef(false);

  const anyAction = actions.syncGraph || actions.inferJoins || actions.applyManifest;
  const showManifest = actions.applyManifest;

  const setAction = (key: keyof BatchActionState, value: boolean) => {
    setActions((prev) => ({ ...prev, [key]: value }));
    setClientResult(null);
  };

  const resetAll = () => {
    setActions(EMPTY_ACTIONS);
    setOnConflict("skip");
    setSourceMode("json");
    setJsonText("");
    setCsvManifest(null);
    setCsvFileList(EMPTY_FILE_LIST);
    setMode("none");
    setClientResult(null);
  };

  const close = () => {
    resetAll();
    onClose();
  };

  // 每次打开都重置状态，避免上次会话的残留/进行中的异步结果串到本次
  useEffect(() => {
    if (open) resetAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  /** 依据当前勾选动作 + 清单来源构造请求；不合法则返回错误文案。 */
  const buildRequest = (): { request: BatchRelationRequest } | { error: string } => {
    if (!anyAction) return { error: t(`${batch}.selectActionFirst`) };
    const request: BatchRelationRequest = {
      syncGraph: actions.syncGraph,
      inferJoins: actions.inferJoins,
      applyManifest: actions.applyManifest,
      onConflict,
    };
    if (!actions.applyManifest) return { request };
    if (sourceMode === "json") {
      const parsed = parseJsonManifest(jsonText);
      if ("error" in parsed) {
        return { error: t(`${batch}.jsonInvalid`, { detail: parsed.error }) };
      }
      if ("empty" in parsed) return { error: t(`${batch}.manifestRequired`) };
      request.manifest = parsed.manifest;
    } else if (csvManifest) {
      request.manifest = csvManifest;
    } else {
      return { error: t(`${batch}.manifestRequired`) };
    }
    return { request };
  };

  const run = async (runMode: RunMode) => {
    if (inFlightRef.current) return; // 进行中禁止并发预览/执行
    const built = buildRequest();
    if ("error" in built) {
      setClientResult({ kind: "error", message: built.error });
      return;
    }
    inFlightRef.current = true;
    setMode(runMode);
    setClientResult(null);
    try {
      const result =
        runMode === "preview"
          ? await previewOntologyBatch(built.request)
          : await runOntologyBatch(built.request);
      const errors = flattenRowErrors(result.joins, result.relations);
      setClientResult({ kind: "result", mode: runMode, result });
      if (runMode === "execute") {
        const changed =
          result.joins.created +
          result.joins.overwritten +
          result.relations.created +
          result.relations.overwritten;
        if (changed > 0 || errors.length > 0) {
          void message.success(t(`${batch}.successToast`));
        } else {
          void message.info(t(`${batch}.noChangeToast`));
        }
      }
    } catch {
      // api 失败已由 axios 拦截器 message.error
    } finally {
      inFlightRef.current = false;
      setMode("none");
    }
  };

  const saveTemplate = async (kind: BatchTemplateKind) => {
    setDownloadKind(kind);
    try {
      const blob = await downloadBatchTemplate(kind);
      downloadBlob(blob, kind === "joins" ? "ontology-joins-template.csv" : "ontology-relations-template.csv");
    } catch {
      // 拦截器已提示
    } finally {
      setDownloadKind(null);
    }
  };

  const handleCsvFile = async (file: File) => {
    setParsingCsv(true);
    try {
      const parsed = await parseBatchCsv(file, csvKind);
      setCsvManifest(parsed.manifest);
      setClientResult(null);
      const rowCount = parsed.manifest.joins.length + parsed.manifest.relations.length;
      if (parsed.errors.length > 0) {
        void message.warning(t(`${batch}.csvHasErrors`, { n: parsed.errors.length }));
      } else {
        void message.success(t(`${batch}.csvParsed`, { n: rowCount }));
      }
    } catch {
      // 拦截器已提示
    } finally {
      setParsingCsv(false);
      setCsvFileList(EMPTY_FILE_LIST);
    }
  };

  const renderCounts = (label: string, counts: BatchCounts) => {
    const total = counts.created + counts.skipped + counts.overwritten;
    if (total === 0) return null;
    return (
      <div key={label} style={{ marginTop: 8 }}>
        <Text strong>{label}</Text>
        <div style={{ color: "#888", fontSize: 12 }}>
          {t(`${batch}.countCreated`, { n: counts.created })} ·{" "}
          {t(`${batch}.countSkipped`, { n: counts.skipped })} ·{" "}
          {t(`${batch}.countOverwritten`, { n: counts.overwritten })}
        </div>
      </div>
    );
  };

  const renderResult = () => {
    if (!clientResult) return null;
    if (clientResult.kind === "error") {
      return (
        <Alert
          type="error"
          showIcon
          message={clientResult.message}
          style={{ marginTop: 16 }}
        />
      );
    }
    const { result, mode: resultMode } = clientResult;
    const inferred = result.inferredJoins;
    const rowErrors = flattenRowErrors(result.joins, result.relations);
    const hasCounts =
      result.joins.created +
        result.joins.skipped +
        result.joins.overwritten +
        result.relations.created +
        result.relations.skipped +
        result.relations.overwritten >
      0;
    const hasAnything =
      result.syncGraph !== null || inferred.length > 0 || hasCounts || rowErrors.length > 0;
    if (!hasAnything) {
      return (
        <Alert
          type="info"
          showIcon
          message={t(`${batch}.emptyCounts`)}
          style={{ marginTop: 16 }}
        />
      );
    }
    return (
      <div
        style={{
          marginTop: 16,
          maxHeight: 300,
          overflow: "auto",
          background: "#fafafa",
          padding: 12,
          borderRadius: 6,
        }}
      >
        <Text strong>{t(`${batch}.${resultMode === "preview" ? "previewTitle" : "resultTitle"}`)}</Text>
        {result.syncGraph !== null && (
          <Descriptions size="small" column={1} style={{ marginTop: 8 }}>
            <Descriptions.Item label={t(`${batch}.graphCountsTitle`)}>
              {t(`${batch}.syncGraphCounts`, {
                classes: result.syncGraph.classes,
                properties: result.syncGraph.properties,
                hasPropertyEdges: result.syncGraph.hasPropertyEdges,
                referenceEdges: result.syncGraph.referenceEdges,
              })}
            </Descriptions.Item>
          </Descriptions>
        )}
        {inferred.length > 0 && (
          <div style={{ marginTop: 8 }}>
            <Text strong>
              {t(`${batch}.inferredTitle`)}（{inferred.length}）
            </Text>
            <Table
              size="small"
              rowKey={(r) => `${r.sourceClassId}-${r.sourceColumns.join("|")}-${r.targetClassId}-${r.targetColumns.join("|")}`}
              pagination={false}
              dataSource={inferred}
              columns={[
                {
                  title: t(`${batch}.colSource`),
                  render: (_, r) => r.sourceClassName,
                },
                {
                  title: t(`${batch}.colColumns`),
                  render: (_, r) => `${r.sourceColumns.join(";")} → ${r.targetColumns.join(";")}`,
                },
                {
                  title: t(`${batch}.colTarget`),
                  render: (_, r) => r.targetClassName,
                },
                {
                  title: t(`${batch}.inferredBy`),
                  width: 150,
                  render: (_, r) => (
                    <Tag color={r.inferredBy === "name_convention" ? "blue" : "cyan"}>
                      {t(`${batch}.${r.inferredBy === "name_convention" ? "inferredByConvention" : "inferredBySharedColumn"}`)}
                    </Tag>
                  ),
                },
              ]}
            />
          </div>
        )}
        <div style={{ marginTop: 8 }}>
          {renderCounts(t(`${batch}.joinsLabel`), result.joins)}
          {renderCounts(t(`${batch}.relationsLabel`), result.relations)}
        </div>
        {rowErrors.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 8 }}
            message={t(`${batch}.errorsTitle`, { n: rowErrors.length })}
            description={
              <ul style={{ margin: 0, paddingLeft: 16 }}>
                {rowErrors.map((e) => (
                  <li key={`${e.index}-${e.message}`}>{e.message}</li>
                ))}
              </ul>
            }
          />
        )}
      </div>
    );
  };

  return (
    <Modal
      title={t(`${batch}.modalTitle`)}
      open={open}
      onCancel={close}
      width={720}
      destroyOnHidden
      footer={
        <Space>
          <Button onClick={close}>{t("common.cancel")}</Button>
          <Button
            icon={<EyeOutlined />}
            loading={mode === "preview"}
            disabled={!anyAction || mode !== "none"}
            onClick={() => void run("preview")}
          >
            {t(`${batch}.preview`)}
          </Button>
          <Button
            type="primary"
            icon={<ThunderboltOutlined />}
            loading={mode === "execute"}
            disabled={!anyAction || mode !== "none"}
            onClick={() => void run("execute")}
          >
            {t(`${batch}.execute`)}
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t(`${batch}.subtitle`)}
        </Text>

        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          <Checkbox
            checked={actions.syncGraph}
            onChange={(e) => setAction("syncGraph", e.target.checked)}
          >
            {t(`${batch}.syncGraph`)}
          </Checkbox>
          <div style={{ color: "#999", fontSize: 12, marginLeft: 24, marginTop: -4 }}>
            {t(`${batch}.syncGraphHint`)}
          </div>
          <Checkbox
            checked={actions.inferJoins}
            onChange={(e) => setAction("inferJoins", e.target.checked)}
          >
            {t(`${batch}.inferJoins`)}
          </Checkbox>
          <div style={{ color: "#999", fontSize: 12, marginLeft: 24, marginTop: -4 }}>
            {t(`${batch}.inferJoinsHint`)}
          </div>
          <Checkbox
            checked={actions.applyManifest}
            onChange={(e) => setAction("applyManifest", e.target.checked)}
          >
            {t(`${batch}.applyManifest`)}
          </Checkbox>
          <div style={{ color: "#999", fontSize: 12, marginLeft: 24, marginTop: -4 }}>
            {t(`${batch}.applyManifestHint`)}
          </div>
        </Space>

        <div>
          <Text strong>{t(`${batch}.onConflictLabel`)}</Text>
          <Radio.Group
            style={{ marginTop: 8 }}
            value={onConflict}
            onChange={(e) => setOnConflict(e.target.value as OnConflictPolicy)}
          >
            <Radio value="skip">{t(`${batch}.conflictSkip`)}</Radio>
            <Radio value="overwrite">{t(`${batch}.conflictOverwrite`)}</Radio>
          </Radio.Group>
        </div>

        {showManifest && (
          <>
            <div>
              <Text strong>{t(`${batch}.sourceLabel`)}</Text>
              <Segmented
                block
                style={{ marginTop: 8 }}
                value={sourceMode}
                onChange={(v) => setSourceMode(v as "json" | "csv")}
                options={[
                  { value: "json", label: t(`${batch}.jsonLabel`) },
                  { value: "csv", label: t(`${batch}.csvLabel`) },
                ]}
              />
            </div>
            {sourceMode === "json" ? (
              <TextArea
                rows={5}
                spellCheck={false}
                style={{ fontFamily: "monospace", fontSize: 12 }}
                value={jsonText}
                onChange={(e) => setJsonText(e.target.value)}
                placeholder={t(`${batch}.jsonPlaceholder`)}
              />
            ) : (
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {t(`${batch}.csvKindLabel`)}
                  </Text>
                  <Radio.Group
                    size="small"
                    value={csvKind}
                    onChange={(e) => {
                      const kind = e.target.value as BatchTemplateKind;
                      setCsvKind(kind);
                      setCsvManifest(null); // 换清单类型后旧解析结果失效
                      setCsvFileList(EMPTY_FILE_LIST);
                    }}
                    options={[
                      { label: t(`${batch}.relationsLabel`), value: "relations" },
                      { label: t(`${batch}.joinsLabel`), value: "joins" },
                    ]}
                  />
                </div>
                <Upload.Dragger
                  accept=".csv,text/csv"
                  maxCount={1}
                  multiple={false}
                  disabled={parsingCsv}
                  fileList={csvFileList}
                  beforeUpload={() => false}
                  onChange={(info) => {
                    const origin = info.fileList[0]?.originFileObj ?? info.file.originFileObj;
                    setCsvFileList(info.fileList);
                    // 仅文件被新增时解析；移除（removed）不触发
                    if (origin && info.file.status !== "removed") {
                      void handleCsvFile(origin);
                    }
                  }}
                >
                  <p className="ant-upload-drag-icon">
                    <UploadOutlined />
                  </p>
                  <p className="ant-upload-text">{t(`${batch}.uploadHint`)}</p>
                  <p className="ant-upload-hint">{t(`${batch}.uploadExtra`)}</p>
                </Upload.Dragger>
                <Space wrap>
                  <Button
                    size="small"
                    icon={<DownloadOutlined />}
                    loading={downloadKind === "relations"}
                    onClick={() => void saveTemplate("relations")}
                  >
                    {t(`${batch}.templateRelations`)}
                  </Button>
                  <Button
                    size="small"
                    icon={<DownloadOutlined />}
                    loading={downloadKind === "joins"}
                    onClick={() => void saveTemplate("joins")}
                  >
                    {t(`${batch}.templateJoins`)}
                  </Button>
                </Space>
              </Space>
            )}
          </>
        )}

        {renderResult()}
      </Space>
    </Modal>
  );
}
