import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, message, Modal, Spin, Steps } from "antd";
import { useTranslation } from "../../i18n";
import RuleConfigStep from "./RuleConfigStep";
import PreviewStep from "./PreviewStep";
import ConfirmStep from "./ConfirmStep";
import SchemaStep from "./SchemaStep";
import { getImportPreview, executeImport } from "../../api/localImport";
import {
  getDatasourceSchema,
  introspectDatasource,
  listDatasourceSchemas,
} from "../../api/datasource";
import type { SchemaIntrospectResponse } from "../../types/datasource";
import type {
  ImportExecuteRequest,
  ImportExecuteResponse,
  ImportPreviewResponse,
  ImportPreviewRequest,
  ImportRuleConfig,
} from "../../types/localImport";
import {
  DEFAULT_JOIN_INFERENCE,
  pruneColumnSubset,
  tableNameIndex,
  toPreviewRequest,
  type ColumnSubset,
} from "./importSelection";

interface Props {
  open: boolean;
  datasourceId: number;
  onClose: () => void;
}

interface HttpError extends Error {
  status?: number;
}

const isNotFound = (error: unknown): boolean =>
  error instanceof Error && (error as HttpError).status === 404;

// 初始规则：关联推断两开关默认开启（与后端 JoinInferenceRules 默认一致）。
function defaultRules(): ImportRuleConfig {
  return { joinInference: { ...DEFAULT_JOIN_INFERENCE } };
}

export default function ImportWizard({ open, datasourceId, onClose }: Props) {
  const { t } = useTranslation();
  const [current, setCurrent] = useState(0);
  const [rules, setRules] = useState<ImportRuleConfig>(defaultRules);
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null);
  const [result, setResult] = useState<ImportExecuteResponse | null>(null);
  const [loading, setLoading] = useState(false);
  // 当前所选 schema（owner）下的表内容
  const [schema, setSchema] = useState<SchemaIntrospectResponse | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(false);
  // 数据源可选 schema 列表；null = 尚未加载（向导顶部转圈直到确定）。
  const [schemas, setSchemas] = useState<string[] | null>(null);
  const [schemasFailed, setSchemasFailed] = useState(false);
  const [selectedSchema, setSelectedSchema] = useState<string | null>(null);
  const [selectedTables, setSelectedTables] = useState<string[]>([]);
  const [columnSubset, setColumnSubset] = useState<ColumnSubset>({});

  const tableIndex = useMemo(
    () => tableNameIndex(schema ? schema.tables : []),
    [schema],
  );

  // 按 owner 加载该 schema 的表：先读缓存，404 时触发一次内省写缓存。
  // 返回是否加载成功，供调用方在失败时复位选择（否则 schema===null 且下次重选
  // 同 owner 无事件可触发，用户会被困在空表步）。
  const loadSchema = useCallback(
    async (owner?: string | null): Promise<boolean> => {
      setSchemaLoading(true);
      try {
        let response: SchemaIntrospectResponse;
        try {
          response = owner
            ? await getDatasourceSchema(datasourceId, owner)
            : await getDatasourceSchema(datasourceId);
        } catch (error: unknown) {
          if (isNotFound(error)) {
            response = owner
              ? await introspectDatasource(datasourceId, owner)
              : await introspectDatasource(datasourceId);
          } else {
            throw error;
          }
        }
        setSchema(response);
        return true;
      } catch {
        message.error(t("toast.schemaLoadFailed"));
        return false;
      } finally {
        setSchemaLoading(false);
      }
    },
    [datasourceId, t],
  );

  // 拉取数据源可选 schema 列表（Oracle owner；PG/MySQL 后端返回空数组）。
  const loadSchemas = useCallback(async () => {
    setSchemasFailed(false);
    setSchemas(null);
    try {
      const owners = await listDatasourceSchemas(datasourceId);
      setSchemas(owners ?? []);
    } catch {
      setSchemasFailed(true);
    }
  }, [datasourceId]);

  // 每次打开向导重置本地态，并拉取该数据源可选 schema。
  useEffect(() => {
    if (!open) return;
    setCurrent(0);
    setRules(defaultRules());
    setPreview(null);
    setResult(null);
    setSchema(null);
    setSelectedTables([]);
    setColumnSubset({});
    setSelectedSchema(null);
    setSchemaLoading(false);
    void loadSchemas();
  }, [open, datasourceId, loadSchemas]);

  // 无多 owner（PG/MySQL 或 owner 仅连接默认）→ 不显示 schema 步，直接加载默认 schema 的表。
  useEffect(() => {
    if (!open || schemas === null || schemasFailed) return;
    if (schemas.length === 0) {
      void loadSchema();
    }
  }, [open, schemas, schemasFailed, loadSchema]);

  const schemaMode = schemas !== null && schemas.length > 0;
  const ruleIdx = schemaMode ? 1 : 0;
  const previewIdx = ruleIdx + 1;
  const confirmIdx = ruleIdx + 2;

  const handleSchemaSelect = async (owner: string) => {
    // schema 已加载时重选同 owner 无意义；schema 未加载（失败待重试）时放行。
    if (owner === selectedSchema && schema !== null) return;
    setSelectedSchema(owner);
    // 换 schema 即换表空间：清空表/列选与旧预览，重新加载该 owner 的表。
    setSchema(null);
    setPreview(null);
    setResult(null);
    setSelectedTables([]);
    setColumnSubset({});
    const ok = await loadSchema(owner);
    if (!ok) {
      // 加载失败：复位选中回到占位态，让「重选同一 owner」成为一次新选择以触发重试。
      setSelectedSchema(null);
    }
  };

  const handleSelectedTablesChange = (tables: string[]) => {
    setSelectedTables(tables);
    setColumnSubset((prev) => pruneColumnSubset(prev, tables, tableIndex));
  };

  const handleColumnSubsetChange = (subset: ColumnSubset) => {
    setColumnSubset(subset);
  };

  const handlePreview = async () => {
    setLoading(true);
    try {
      const request: ImportPreviewRequest = toPreviewRequest(
        rules,
        selectedTables,
        columnSubset,
        tableIndex,
      );
      const data = await getImportPreview(datasourceId, {
        ...request,
        schema: selectedSchema ?? undefined,
      });
      setPreview(data);
      setCurrent(previewIdx);
    } catch {
      message.error(t("toast.previewFailed"));
    } finally {
      setLoading(false);
    }
  };

  const handleExecute = async (request: ImportExecuteRequest) => {
    setLoading(true);
    try {
      const executeResult = await executeImport(datasourceId, request);
      setResult(executeResult);
      setCurrent(confirmIdx);
    } catch {
      message.error(t("toast.importFailed"));
    } finally {
      setLoading(false);
    }
  };

  const schemaReady = !schemaLoading && schema !== null;

  // Schema 列表加载失败：阻断并给重试，避免在未知 owner 集下误走默认。
  if (schemasFailed) {
    return (
      <Modal open={open} onCancel={onClose} footer={null} width={960} destroyOnHidden>
        <Alert
          type="error"
          message={t("localImport.schema.loadFailed")}
          showIcon
          action={
            <Button size="small" onClick={() => void loadSchemas()}>
              {t("common.retry")}
            </Button>
          }
        />
      </Modal>
    );
  }

  // 列表加载中：整窗转圈，不渲染步骤，避免「无 schema」判定闪烁。
  if (schemas === null) {
    return (
      <Modal open={open} onCancel={onClose} footer={null} width={960} destroyOnHidden>
        <div style={{ textAlign: "center", padding: "48px 0" }}>
          {/* Spin 仅在有子内容时展示 tip，故包一个空块让「正在加载 Schema 列表…」可见 */}
          <Spin tip={t("localImport.schema.loading")}>
            <div style={{ minHeight: 96 }} />
          </Spin>
        </div>
      </Modal>
    );
  }

  const ruleContent = schemaLoading ? (
    <Spin />
  ) : schema ? (
    <RuleConfigStep
      tables={schema.tables}
      rules={rules}
      selectedTables={selectedTables}
      columnSubset={columnSubset}
      onRulesChange={setRules}
      onSelectedTablesChange={handleSelectedTablesChange}
      onColumnSubsetChange={handleColumnSubsetChange}
    />
  ) : (
    <Alert type="warning" message={t("localImport.config.schemaLoadWarn")} />
  );

  const schemaStep = schemaMode ? (
    <SchemaStep
      schemas={schemas}
      selected={selectedSchema}
      disabled={schemaLoading}
      onSelect={(owner) => void handleSchemaSelect(owner)}
    />
  ) : null;

  const steps = schemaMode
    ? [
        { title: t("localImport.steps.schema"), content: schemaStep },
        { title: t("localImport.steps.rule"), content: ruleContent },
        {
          title: t("localImport.steps.preview"),
          content: preview ? <PreviewStep preview={preview} onExecute={handleExecute} /> : null,
        },
        { title: t("localImport.steps.confirm"), content: <ConfirmStep result={result} /> },
      ]
    : [
        { title: t("localImport.steps.rule"), content: ruleContent },
        {
          title: t("localImport.steps.preview"),
          content: preview ? <PreviewStep preview={preview} onExecute={handleExecute} /> : null,
        },
        { title: t("localImport.steps.confirm"), content: <ConfirmStep result={result} /> },
      ];

  return (
    <Modal open={open} onCancel={onClose} footer={null} width={960} destroyOnHidden>
      <Steps current={current} items={steps.map((s) => ({ title: s.title }))} />
      <div style={{ marginTop: 24 }}>{steps[current].content}</div>
      <div style={{ marginTop: 24, textAlign: "right" }}>
        {current > 0 && (
          <Button onClick={() => setCurrent(current - 1)}>{t("common.prev")}</Button>
        )}
        {current === 0 && schemaMode && (
          <Button
            type="primary"
            onClick={() => setCurrent(ruleIdx)}
            disabled={!selectedSchema || !schemaReady}
          >
            {t("common.next")}
          </Button>
        )}
        {current === ruleIdx && (
          <Button
            type="primary"
            loading={loading}
            onClick={handlePreview}
            disabled={!schemaReady || selectedTables.length === 0}
          >
            {t("common.next")}
          </Button>
        )}
      </div>
    </Modal>
  );
}
