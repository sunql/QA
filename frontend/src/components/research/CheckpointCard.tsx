/** 待决策检查点卡片（feat-research-entry Task 10；feat-research-entry-ux-fixes W1 结构化渲染）。
 *
 * 纯展示 + 回调组件：读取 `checkpoint.prompt` 作为问题文本，按 `checkpoint.phase`
 * 渲染「本次针对什么」目标行与相位相关明细（歧义 / 计划步骤 / 候选假设），
 * confirm / modify / reject 三动作经 `onAnswer(action, choice)` 上抛。错误处置
 * （uiHint 类分支）由 store 负责，本组件不落任何 code 分支。
 *
 * 键名坑（W1 的关键约束）：`options.plan.steps[].sub_question` 是 **snake_case**
 * （后端 normalizePlan 产物），而 `options.stepResults[].subQuestion` 是 camelCase。
 * 历史 checkpoint 的 options 已落库，不能靠改后端键名统一 ⇒ 这里双键回落读取。
 */
import { useState } from "react";
import { Button, Card, Checkbox, Input, Space, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import type { CheckpointAction, ResearchCheckpoint } from "../../types/research";

interface CheckpointCardProps {
  checkpoint: ResearchCheckpoint;
  onAnswer: (action: CheckpointAction, choice: Record<string, unknown>) => void;
}

interface ConflictCandidateView {
  label: string;
  confidence: string;
}

interface ConflictView {
  kind: string;
  detail: string;
  candidates: ConflictCandidateView[];
}

// 已知冲突种类（后端 enterprise_semantic_layer._detectConflicts 的产出集）；
// 表外种类回落通用标签，不构造文案、不抛错。
const CONFLICT_KIND_KEYS: ReadonlySet<string> = new Set<string>([
  "metric_ambiguous",
  "wiki_disagree",
]);

// 已知检查点相位（与后端 CHECKPOINT_* 常量集一致）；表外相位回落通用目标文案。
const CHECKPOINT_PHASES: ReadonlySet<string> = new Set<string>([
  "intent",
  "planning",
  "hypothesis",
  "runtime_dynamic",
  "low_confidence_step",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// 冲突候选 / 假设候选的统一显示名：指标歧义给 displayName，知识冲突给 title，
// 假设给 statement（后端假说的 dataclass 字段）。
function candidateLabel(item: Record<string, unknown>): string {
  for (const key of ["displayName", "title", "statement"]) {
    const value = item[key];
    if (typeof value === "string" && value.trim().length > 0) return value;
  }
  return "";
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

function conflictViews(options: Record<string, unknown>): ConflictView[] {
  const raw = options.conflicts;
  if (!Array.isArray(raw)) return [];
  const views: ConflictView[] = [];
  for (const item of raw) {
    if (!isRecord(item)) continue;
    const candidates: ConflictCandidateView[] = [];
    if (Array.isArray(item.candidates)) {
      for (const candidate of item.candidates) {
        if (!isRecord(candidate)) continue;
        candidates.push({
          label: candidateLabel(candidate),
          confidence:
            typeof candidate.confidence === "number" ? candidate.confidence.toFixed(2) : "",
        });
      }
    }
    views.push({
      kind: typeof item.kind === "string" ? item.kind : "",
      detail: typeof item.detail === "string" ? item.detail : "",
      candidates,
    });
  }
  return views;
}

// 计划步骤描述：先 camelCase 再 snake_case 回落（见文件头「键名坑」）。
function planSteps(options: Record<string, unknown>): string[] {
  const plan = options.plan;
  if (!isRecord(plan) || !Array.isArray(plan.steps)) return [];
  const steps: string[] = [];
  for (const step of plan.steps) {
    if (!isRecord(step)) continue;
    const camel = step.subQuestion;
    const snake = step.sub_question;
    if (typeof camel === "string") {
      steps.push(camel);
    } else {
      steps.push(typeof snake === "string" ? snake : "");
    }
  }
  return steps;
}

// 候选假设描述。**不按下标过滤空串** —— 下标即提交给后端的 selectedIndexes。
function hypothesisStatements(options: Record<string, unknown>): string[] {
  const raw = options.candidates;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter(isRecord)
    .map((item) => (typeof item.statement === "string" ? item.statement : ""));
}

export function CheckpointCard({ checkpoint, onAnswer }: CheckpointCardProps) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [selectedIndexes, setSelectedIndexes] = useState<number[]>([]);

  const labels = metricLabels(checkpoint.options);
  const conflicts = conflictViews(checkpoint.options);
  const steps = planSteps(checkpoint.options);
  const candidates = hypothesisStatements(checkpoint.options);

  // 明细块按相位出：只有对应相位才可能承载该数据，其他相位不渲染（避免每张卡片都挂
  // 一句与本相位无关的空态文案）。
  const showConflicts = checkpoint.phase === "runtime_dynamic";
  const showPlanSteps = checkpoint.phase === "planning";
  const showCandidates = checkpoint.phase === "hypothesis";

  const targetKey = CHECKPOINT_PHASES.has(checkpoint.phase)
    ? `research.checkpoint.target.${checkpoint.phase}`
    : "research.checkpoint.targetUnknown";

  const kindKey = (kind: string): string =>
    CONFLICT_KIND_KEYS.has(kind)
      ? `research.checkpoint.conflictKind.${kind}`
      : "research.checkpoint.conflictKindFallback";

  const submitModify = () => {
    const question = draft.trim();
    if (question.length === 0) return;
    onAnswer("modify", { question });
    setEditing(false);
    setDraft("");
  };

  const submitConfirm = () => {
    // 勾了候选才带上 selectedIndexes；未勾选 = 空 choice（与既有行为一致）。
    onAnswer("confirm", selectedIndexes.length > 0 ? { selectedIndexes } : {});
  };

  const toggleCandidate = (index: number, checked: boolean) => {
    setSelectedIndexes((prev) =>
      checked ? [...prev, index] : prev.filter((item) => item !== index),
    );
  };

  return (
    <Card size="small" title={t("research.checkpoint.title")}>
      <div style={{ marginBottom: 8 }}>
        <Tag color="blue">{t("research.checkpoint.targetLabel")}</Tag>
        <Typography.Text>{t(targetKey)}</Typography.Text>
      </div>
      <p>{checkpoint.prompt}</p>
      {labels.length > 0 && (
        <Space wrap>
          {labels.map((label) => (
            <Tag key={label}>{label}</Tag>
          ))}
        </Space>
      )}

      {showConflicts && (
        <div style={{ marginTop: 8 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("research.checkpoint.conflicts")}
          </Typography.Text>
          {conflicts.length > 0 ? (
            <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
              {conflicts.map((conflict, index) => (
                <li key={index}>
                  <Tag>{t(kindKey(conflict.kind))}</Tag>
                  {conflict.detail.length > 0 && <span>{conflict.detail}</span>}
                  {conflict.candidates.length > 0 && (
                    <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
                      {conflict.candidates.map((candidate, candidateIndex) => (
                        <li key={candidateIndex}>
                          {candidate.label || t("research.checkpoint.unnamedItem")}
                          {candidate.confidence.length > 0 && (
                            <span>
                              {" "}
                              · {t("research.checkpoint.confidence")} {candidate.confidence}
                            </span>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t("research.checkpoint.emptyConflicts")}
              </Typography.Text>
            </div>
          )}
        </div>
      )}

      {showPlanSteps && (
        <div style={{ marginTop: 8 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("research.checkpoint.planSteps")}
          </Typography.Text>
          {steps.length > 0 ? (
            <ol style={{ margin: "4px 0 0", paddingLeft: 20 }}>
              {steps.map((step, index) => (
                <li key={index}>{step || t("research.checkpoint.unnamedItem")}</li>
              ))}
            </ol>
          ) : (
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t("research.checkpoint.emptyPlanSteps")}
              </Typography.Text>
            </div>
          )}
        </div>
      )}

      {showCandidates && (
        <div style={{ marginTop: 8 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("research.checkpoint.candidates")}
          </Typography.Text>
          {candidates.length > 0 ? (
            <div style={{ display: "flex", flexDirection: "column", marginTop: 4 }}>
              {candidates.map((statement, index) => (
                <Checkbox
                  key={index}
                  checked={selectedIndexes.includes(index)}
                  onChange={(event) => toggleCandidate(index, event.target.checked)}
                >
                  {statement || t("research.checkpoint.unnamedItem")}
                </Checkbox>
              ))}
            </div>
          ) : (
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t("research.checkpoint.emptyCandidates")}
              </Typography.Text>
            </div>
          )}
        </div>
      )}

      <Space style={{ marginTop: 12 }}>
        <Button type="primary" onClick={submitConfirm}>
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
