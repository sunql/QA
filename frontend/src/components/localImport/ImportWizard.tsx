import { useState } from "react";
import { Modal, Steps, Button, message } from "antd";
import { useTranslation } from "../../i18n";
import RuleConfigStep from "./RuleConfigStep";
import PreviewStep from "./PreviewStep";
import ConfirmStep from "./ConfirmStep";
import { getImportPreview, executeImport } from "../../api/localImport";
import type {
  ImportExecuteRequest,
  ImportExecuteResponse,
  ImportPreviewResponse,
  ImportRuleConfig,
} from "../../types/localImport";

interface Props {
  open: boolean;
  datasourceId: number;
  onClose: () => void;
}

export default function ImportWizard({ open, datasourceId, onClose }: Props) {
  const { t } = useTranslation();
  const [current, setCurrent] = useState(0);
  const [rules, setRules] = useState<ImportRuleConfig>({});
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null);
  const [result, setResult] = useState<ImportExecuteResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const handlePreview = async () => {
    setLoading(true);
    try {
      const data = await getImportPreview(datasourceId, rules);
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

  const steps = [
    {
      title: t("localImport.steps.rule"),
      content: <RuleConfigStep rules={rules} onChange={setRules} />,
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
          <Button type="primary" loading={loading} onClick={handlePreview}>
            {t("common.next")}
          </Button>
        )}
      </div>
    </Modal>
  );
}
