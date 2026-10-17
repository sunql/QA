/**
 * 检查点决策弹窗（含模型切换 dropdown）（N4 qwen resume Task 9）。
 *
 * 封装 CheckpointCard：默认 modelId = turnModelId（不改派），
 * 用户切换模型后 submit 时将 modelId 传入 answerCheckpoint payload。
 * 用户不改 → payload 不含 modelId 字段，后端走默认模型。
 */
import { useState } from "react";
import { Button, Modal, Select, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { useLlmModels } from "../../hooks/useLlmModels";
import { answerCheckpoint } from "../../api/research";
import type { CheckpointAction } from "../../types/research";

/**
 * Build the answerCheckpoint payload from current modal state.
 * Pure function (N4): easy to test without driving antd Select state in jsdom.
 *
 * @param action       CheckpointAction ("confirm" | "modify" | "reject")
 * @param choice       user choice payload
 * @param modelId      currently-selected model id (null = "no choice made")
 * @param turnModelId  original turn model id (used to detect "user changed model")
 * @returns the payload object, with modelId only if user changed model
 */
export function buildCheckpointAnswerPayload(
  action: CheckpointAction,
  choice: Record<string, unknown>,
  modelId: number | null,
  turnModelId: number,
): { action: CheckpointAction; choice: Record<string, unknown>; modelId?: number } {
  const payload: { action: CheckpointAction; choice: Record<string, unknown>; modelId?: number } = { action, choice };
  if (modelId !== null && modelId !== turnModelId) {
    payload.modelId = modelId;
  }
  return payload;
}

interface CheckpointDecisionModalProps {
  checkpointId: string;
  turnModelId: number;
  action: CheckpointAction;
  onSubmit: () => void;
  onClose: () => void;
}

/**
 * 检查点决策弹窗。
 * - 默认 modelId = turnModelId（用户不切换 = 沿用原模型）
 * - 切换模型后 submit → payload.modelId = 新模型 id
 * - 不改模型 submit → payload 不含 modelId（后端走默认）
 */
export default function CheckpointDecisionModal({
  checkpointId,
  turnModelId,
  action,
  onSubmit,
  onClose,
}: CheckpointDecisionModalProps) {
  const { t } = useTranslation();
  const [modelId, setModelId] = useState<number | null>(turnModelId);
  const { data: models } = useLlmModels();

  const options = models.map((m) => ({
    value: m.id,
    label: `${m.modelName} (${m.provider})`,
  }));

  const handleSubmit = async () => {
    const payload = buildCheckpointAnswerPayload(action, {}, modelId, turnModelId);
    await answerCheckpoint(checkpointId, payload);
    onSubmit();
    onClose();
  };

  return (
    <Modal
      open
      title={t("research.checkpoint.decisionTitle", "Choose Model to Continue")}
      onCancel={onClose}
      footer={
        <Space>
          <Button
            type="primary"
            onClick={handleSubmit}
            data-testid="checkpoint-submit"
          >
            {t("research.checkpoint.confirm", "Confirm")}
          </Button>
          <Button onClick={onClose}>{t("common.cancel", "Cancel")}</Button>
        </Space>
      }
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        <div>
          <Typography.Text strong>{t("research.checkpoint.selectModel", "Model")}</Typography.Text>
          <Select
            data-testid="checkpoint-model-select"
            value={modelId}
            onChange={setModelId}
            options={options}
            style={{ width: "100%", marginTop: 8 }}
            placeholder={t("research.checkpoint.selectModelPlaceholder", "Select a model")}
          />
        </div>
      </Space>
    </Modal>
  );
}
