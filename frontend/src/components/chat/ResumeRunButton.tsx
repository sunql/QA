import { Button } from "antd";
import { useTranslation } from "../../i18n";

interface ResumeRunButtonProps {
  runId: string;
  fromStepIndex: number;
  disabled?: boolean;
  onResume: (runId: string, fromStepIndex: number) => void;
}

/**
 * 失败步骤的续跑按钮（spec §8.2）。
 *
 * 只负责「点击时把 runId + 起始步号交出去」，不发请求 —— 请求由 store 的
 * resumeRun 统一发起，与首发共用同一套 SSE handler。
 */
export default function ResumeRunButton({
  runId,
  fromStepIndex,
  disabled,
  onResume,
}: ResumeRunButtonProps) {
  const { t } = useTranslation();
  return (
    <Button
      size="small"
      type="primary"
      disabled={disabled}
      onClick={() => onResume(runId, fromStepIndex)}
      data-testid="resume-run"
    >
      {t("multiStep.resume")}
    </Button>
  );
}
