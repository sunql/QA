import type { ImportRuleConfig } from "../../types/localImport";

interface RuleConfigStepProps {
  rules: ImportRuleConfig;
  onChange: (rules: ImportRuleConfig) => void;
}

// 骨架：规则设置表单将在后续任务中完善。
export default function RuleConfigStep(_props: RuleConfigStepProps) {
  return <div>规则设置表单将在后续任务中完善</div>;
}
