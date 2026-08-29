import type {
  ImportExecuteRequest,
  ImportPreviewResponse,
} from "../../types/localImport";

interface PreviewStepProps {
  preview: ImportPreviewResponse;
  onExecute: (request: ImportExecuteRequest) => void;
}

// 骨架：预览确认内容将在后续任务中完善。
export default function PreviewStep(_props: PreviewStepProps) {
  return <div>预览内容将在后续任务中完善</div>;
}
