import { Row } from "antd";
import { useTranslation } from "../../i18n";
import OrchestrationProfileNumberField from "./OrchestrationProfileNumberField";
import {
  MAX_COST_USD,
  MAX_ITERATIONS,
  MAX_OBSERVATION_CHARS,
  MAX_TIMEOUT_SECONDS,
  MAX_TOOL_CALLS,
  MIN_OBSERVATION_CHARS,
} from "./orchestrationProfileLimits";

/** 预算字段区：迭代次数、工具调用次数、成本上限、超时与观测字符数。 */
export default function OrchestrationProfileBudgetFields() {
  const { t } = useTranslation();
  return (
    <Row gutter={16}>
      <OrchestrationProfileNumberField
        name="defaultMaxIterations"
        label={t("agentOrchestration.form.defaultMaxIterations")}
        min={1}
        max={MAX_ITERATIONS}
      />
      <OrchestrationProfileNumberField
        name="defaultMaxToolCalls"
        label={t("agentOrchestration.form.defaultMaxToolCalls")}
        min={1}
        max={MAX_TOOL_CALLS}
      />
      <OrchestrationProfileNumberField
        name="defaultMaxCostUsd"
        label={t("agentOrchestration.form.defaultMaxCostUsd")}
        min={0}
        max={MAX_COST_USD}
        step={0.1}
      />
      <OrchestrationProfileNumberField
        name="defaultTimeoutSeconds"
        label={t("agentOrchestration.form.defaultTimeoutSeconds")}
        min={1}
        max={MAX_TIMEOUT_SECONDS}
      />
      <OrchestrationProfileNumberField
        name="maxObservationChars"
        label={t("agentOrchestration.form.maxObservationChars")}
        min={MIN_OBSERVATION_CHARS}
        max={MAX_OBSERVATION_CHARS}
        step={1000}
      />
    </Row>
  );
}
