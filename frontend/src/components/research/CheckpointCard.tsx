/** 待决策检查点卡片（feat-research-entry Task 10）。
 *
 * 纯展示 + 回调组件：读取 `checkpoint.prompt` 作为问题文本、`checkpoint.options`
 * 里的三臂候选做摘要 Tag，confirm / modify / reject 三动作经 `onAnswer(action, choice)`
 * 上抛。错误处置（uiHint 类分支）由 store 负责，本组件不落任何 code 分支。
 */
import { useState } from "react";
import { Button, Card, Input, Space, Tag } from "antd";
import { useTranslation } from "react-i18next";
import type { CheckpointAction, ResearchCheckpoint } from "../../types/research";

interface CheckpointCardProps {
  checkpoint: ResearchCheckpoint;
  onAnswer: (action: CheckpointAction, choice: Record<string, unknown>) => void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// options.arms.metrics[] 的 displayName 摘要（逐字来自后端三臂载荷，不本地构造文案）。
function metricLabels(options: Record<string, unknown>): string[] {
  const arms = options.arms;
  if (!isRecord(arms) || !Array.isArray(arms.metrics)) return [];
  const labels: string[] = [];
  for (const item of arms.metrics) {
    if (isRecord(item) && typeof item.displayName === "string") {
      labels.push(item.displayName);
    }
  }
  return labels;
}

export function CheckpointCard({ checkpoint, onAnswer }: CheckpointCardProps) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const labels = metricLabels(checkpoint.options);

  const submitModify = () => {
    const question = draft.trim();
    if (question.length === 0) return;
    onAnswer("modify", { question });
    setEditing(false);
    setDraft("");
  };

  return (
    <Card size="small" title={t("research.checkpoint.title")}>
      <p>{checkpoint.prompt}</p>
      {labels.length > 0 && (
        <Space wrap>
          {labels.map((label) => (
            <Tag key={label}>{label}</Tag>
          ))}
        </Space>
      )}
      <Space style={{ marginTop: 12 }}>
        <Button type="primary" onClick={() => onAnswer("confirm", {})}>
          {t("research.checkpoint.confirm")}
        </Button>
        <Button onClick={() => setEditing((value) => !value)}>
          {t("research.checkpoint.modify")}
        </Button>
        <Button danger onClick={() => onAnswer("reject", {})}>
          {t("research.checkpoint.reject")}
        </Button>
      </Space>
      {editing && (
        <div style={{ marginTop: 12 }}>
          <Input.TextArea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={t("research.checkpoint.modifyPlaceholder")}
            autoSize={{ minRows: 2 }}
          />
          <Button
            type="primary"
            onClick={submitModify}
            style={{ marginTop: 8 }}
            disabled={draft.trim().length === 0}
          >
            {t("research.checkpoint.submitModify")}
          </Button>
        </div>
      )}
    </Card>
  );
}
