import { useCallback, useEffect, useMemo, useState } from "react";
import { Modal, Steps, Button, message, Spin, Alert } from "antd";
import { useTranslation } from "../../i18n";
import RuleConfigStep from "./RuleConfigStep";
import PreviewStep from "./PreviewStep";
import ConfirmStep from "./ConfirmStep";
import { getImportPreview, executeImport } from "../../api/localImport";
import {
  getDatasourceSchema,
  introspectDatasource,
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
  const [schema, setSchema] = useState<SchemaIntrospectResponse | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [selectedTables, setSelectedTables] = useState<string[]>([]);
  const [columnSubset, setColumnSubset] = useState<ColumnSubset>({});

  const tableIndex = useMemo(
    () => tableNameIndex(schema ? schema.tables : []),
    [schema],
  );

  const loadSchema = useCallback(async () => {
    setSchemaLoading(true);
    try {
      let response: SchemaIntrospectResponse;
      try {
        response = await getDatasourceSchema(datasourceId);
      } catch (error: unknown) {
        // 尚未有缓存的 schema：触发一次发现并写缓存。
        if (isNotFound(error)) {
          response = await introspectDatasource(datasourceId);
        } else {
          throw error;
        }
      }
      setSchema(response);
    } catch {
      message.error(t("toast.schemaLoadFailed"));
    } finally {
      setSchemaLoading(false);
    }
  }, [datasourceId, t]);

  // 每次打开向导重置本地态，并拉取该数据源 schema。
  useEffect(() => {
    if (!open) return;
    setCurrent(0);
    setRules(defaultRules());
    setPreview(null);
    setResult(null);
    setSchema(null);
    setSelectedTables([]);
    setColumnSubset({});
    void loadSchema();
  }, [open, datasourceId, loadSchema]);

  const handleSelectedTablesChange = (tables: string[]) => {
    setSelectedTables(tables);
    // 表选变化时把列选白名单收敛到仍选中的表；未收窄/超集的条目被丢弃。
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
      const data = await getImportPreview(datasourceId, request);
      setPreview(data);
      setCurrent(1);
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
      setCurrent(2);
    } catch {
      message.error(t("toast.importFailed"));
    } finally {
      setLoading(false);
    }
  };

  const schemaReady = !schemaLoading && schema !== null;

  const steps = [
    {
      title: t("localImport.steps.rule"),
      content: schemaLoading ? (
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
      ),
    },
    {
      title: t("localImport.steps.preview"),
      content: preview ? <PreviewStep preview={preview} onExecute={handleExecute} /> : null,
    },
    {
      title: t("localImport.steps.confirm"),
      content: <ConfirmStep result={result} />,
    },
  ];

  return (
    <Modal open={open} onCancel={onClose} footer={null} width={960} destroyOnHidden>
      <Steps current={current} items={steps.map((s) => ({ title: s.title }))} />
      <div style={{ marginTop: 24 }}>{steps[current].content}</div>
      <div style={{ marginTop: 24, textAlign: "right" }}>
        {current > 0 && <Button onClick={() => setCurrent(current - 1)}>{t("common.prev")}</Button>}
        {current === 0 && (
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
