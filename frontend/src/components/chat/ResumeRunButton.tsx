import { useState } from "react";
import { Button, Popover, Select, Space, Typography } from "antd";
import { useTranslation } from "../../i18n";
import { useLlmModels } from "../../hooks/useLlmModels";

interface ResumeRunButtonProps {
  runId: string;
  fromStepIndex: number;
  originalModelId: number; // 原 run 使用的模型（默认 modelId，不改派）
  onResume: (runId: string, fromStepIndex: number, modelId: number | null) => void;
  disabled?: boolean;
}

/**
 * 失败步骤的续跑按钮（spec §N4）。
 *
 * 点击 → 弹 Popover 让用户选模型 → onResume(runId, fromStep, modelId)
 * 默认 modelId = originalModelId（同模型续跑，不改派）。
 */
export default function ResumeRunButton({
  runId,
  fromStepIndex,
  originalModelId,
  onResume,
  disabled,
}: ResumeRunButtonProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<number | null>(null);
  const { data: models } = useLlmModels();

  const effectiveModelId = selectedModelId ?? originalModelId;
  const isOriginalModel = effectiveModelId === originalModelId;
  const options = models.map((m) => ({
    value: m.id,
    label: `${m.modelName} (${m.provider})`,
  }));

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      content={
        <Space direction="vertical" style={{ width: 240 }}>
          <Typography.Text strong>{t("multiStep.resumeChooseModel")}</Typography.Text>
          <Select
            value={effectiveModelId}
            onChange={setSelectedModelId}
            options={options}
            style={{ width: "100%" }}
            data-testid="resume-model-select"
          />
          {isOriginalModel && (
            <Typography.Text type="secondary" data-testid="resume-same-model-hint">
              {t("multiStep.resumeSameModelHint")}
            </Typography.Text>
          )}
          <Space>
            <Button
              type="primary"
              onClick={() => {
                onResume(runId, fromStepIndex, effectiveModelId);
                setOpen(false);
              }}
              data-testid="resume-with-selected"
            >
              {t("multiStep.resumeWithSelected")}
            </Button>
            <Button
              onClick={() => {
                onResume(runId, fromStepIndex, null); // null = 用原 model
                setOpen(false);
              }}
              data-testid="resume-with-original"
            >
              {t("multiStep.resumeWithOriginal")}
            </Button>
            <Button onClick={() => setOpen(false)}>{t("common.cancel")}</Button>
          </Space>
        </Space>
      }
    >
      <Button
        size="small"
        type="primary"
        disabled={disabled}
        data-testid="resume-run"
      >
        {t("multiStep.resume")}
      </Button>
    </Popover>
  );
}
