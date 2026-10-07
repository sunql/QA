/** 待决策检查点卡片（feat-research-entry Task 10；feat-research-entry-ux-fixes W1 结构化渲染）。
 *
 * 纯展示 + 回调组件：读取 `checkpoint.prompt` 作为问题文本，按 `checkpoint.phase`
 * 渲染「本次针对什么」目标行与相位相关明细（歧义 / 计划步骤 / 候选假设），
 * confirm / modify / reject 三动作经 `onAnswer(action, choice)` 上抛。错误处置
 * （uiHint 类分支）由 store 负责，本组件不落任何 code 分支。
 *
 * 结构：三个相位门控明细块各自是**本文件模块级**的子组件（`ConflictDetails` /
 * `PlanSteps` / `CandidateList`）。模块级是硬约束 —— 组件若定义在父函数内部，
 * 每次父渲染都会重挂载，勾选态会当场丢失。
 *
 * 键名坑（W1 的关键约束）：`options.plan.steps[].sub_question` 是 **snake_case**
 * （后端 normalizePlan 产物），而 `options.stepResults[].subQuestion` 是 camelCase。
 * 历史 checkpoint 的 options 已落库，不能靠改后端键名统一 ⇒ 这里双键回落读取。
 */
import { useState } from "react";
import { Button, Card, Checkbox, Input, Space, Tag, Tooltip, Typography } from "antd";
import { useTranslation } from "react-i18next";
import type { CheckpointAction, CheckpointPhase, ResearchCheckpoint } from "../../types/research";

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

interface CheckpointTargetLineProps {
  phase: CheckpointPhase;
}

// 「本次针对什么」目标行：相位表内给专属文案，表外相位回落通用文案。
function CheckpointTargetLine({ phase }: CheckpointTargetLineProps) {
  const { t } = useTranslation();
  const targetKey = CHECKPOINT_PHASES.has(phase)
    ? `research.checkpoint.target.${phase}`
    : "research.checkpoint.targetUnknown";
  return (
    <div style={{ marginBottom: 8 }}>
      <Tag color="blue">{t("research.checkpoint.targetLabel")}</Tag>
      <Typography.Text>{t(targetKey)}</Typography.Text>
    </div>
  );
}

interface MetricTagsProps {
  labels: string[];
}

// options.arms.metrics 的名称标签行（无标签时不渲染）。
function MetricTags({ labels }: MetricTagsProps) {
  if (labels.length === 0) return null;
  return (
    <Space wrap>
      {labels.map((label) => (
        <Tag key={label}>{label}</Tag>
      ))}
    </Space>
  );
}

interface ConflictItemProps {
  conflict: ConflictView;
}

// 单条歧义：种类标签 + detail + 候选（名称 + 置信度）。
function ConflictItem({ conflict }: ConflictItemProps) {
  const { t } = useTranslation();
  const kindKey = CONFLICT_KIND_KEYS.has(conflict.kind)
    ? `research.checkpoint.conflictKind.${conflict.kind}`
    : "research.checkpoint.conflictKindFallback";
  return (
    <li>
      <Tag>{t(kindKey)}</Tag>
      {conflict.detail.length > 0 && <span>{conflict.detail}</span>}
      {conflict.candidates.length > 0 && (
        <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
          {conflict.candidates.map((candidate, index) => (
            <li key={index}>
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
  );
}

interface ConflictDetailsProps {
  conflicts: ConflictView[];
}

// 歧义明细块（相位门控：仅 runtime_dynamic 相位渲染）。
function ConflictDetails({ conflicts }: ConflictDetailsProps) {
  const { t } = useTranslation();
  return (
    <div style={{ marginTop: 8 }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {t("research.checkpoint.conflicts")}
      </Typography.Text>
      {conflicts.length > 0 ? (
        <ul style={{ margin: "4px 0 0", paddingLeft: 20 }}>
          {conflicts.map((conflict, index) => (
            <ConflictItem key={index} conflict={conflict} />
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
  );
}

interface PlanStepsProps {
  steps: string[];
}

// 计划步骤块（相位门控：仅 planning 相位渲染）。
function PlanSteps({ steps }: PlanStepsProps) {
  const { t } = useTranslation();
  return (
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
  );
}

interface CandidateListProps {
  statements: string[];
  selectedIndexes: number[];
  onToggle: (index: number, checked: boolean) => void;
}

// 候选假设勾选块（相位门控：仅 hypothesis 相位渲染）。下标即提交给后端的
// selectedIndexes，故列表顺序必须与 `options.candidates` 数组顺序一致。
function CandidateList({ statements, selectedIndexes, onToggle }: CandidateListProps) {
  const { t } = useTranslation();
  return (
    <div style={{ marginTop: 8 }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {t("research.checkpoint.candidates")}
      </Typography.Text>
      {statements.length > 0 ? (
        <div>
          <div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {t("research.checkpoint.hypothesisSelectHint")}
            </Typography.Text>
          </div>
          <div style={{ display: "flex", flexDirection: "column", marginTop: 4 }}>
            {statements.map((statement, index) => (
              <Checkbox
                key={index}
                checked={selectedIndexes.includes(index)}
                onChange={(event) => onToggle(index, event.target.checked)}
              >
                {statement || t("research.checkpoint.unnamedItem")}
              </Checkbox>
            ))}
          </div>
        </div>
      ) : (
        <div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("research.checkpoint.emptyCandidates")}
          </Typography.Text>
        </div>
      )}
    </div>
  );
}

interface CheckpointActionsProps {
  onConfirm: () => void;
  onReject: () => void;
  onModify: (question: string) => void;
}

// 三动作区（确认 / 修改 / 拒绝）。修改草稿与展开态是纯局部 UI 状态：留在这里
// 才能与引入前一致地「收起再展开仍保留草稿」，同时随卡片重挂载（key）清零。
function CheckpointActions({
  onConfirm,
  onReject,
  onModify,
  phase,
}: CheckpointActionsProps & { phase: CheckpointPhase }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  // 上下文化 hint key 选择
  const modifyHintKey =
    phase === "planning"
      ? "research.checkpoint.modifyPlanningHint"
      : "research.checkpoint.modifyHint";
  const rejectHintKey =
    phase === "runtime_dynamic" || phase === "low_confidence_step"
      ? "research.checkpoint.rejectAbortHint"
      : "research.checkpoint.rejectHint";
  const confirmHintKey =
    phase === "hypothesis"
      ? "research.checkpoint.hypothesisConfirmHint"
      : "research.checkpoint.confirmHint";

  const submitModify = () => {
    const question = draft.trim();
    if (question.length === 0) return;
    onModify(question);
    setEditing(false);
    setDraft("");
  };

  return (
    <>
      <Space style={{ marginTop: 12 }}>
        <Tooltip title={t(confirmHintKey)}>
          <Button type="primary" onClick={onConfirm}>
            {t("research.checkpoint.confirm")}
          </Button>
        </Tooltip>
        <Tooltip title={t(modifyHintKey)}>
          <Button onClick={() => setEditing((value) => !value)}>
            {t("research.checkpoint.modify")}
          </Button>
        </Tooltip>
        <Tooltip title={t(rejectHintKey)}>
          <Button danger onClick={onReject}>
            {t("research.checkpoint.reject")}
          </Button>
        </Tooltip>
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
    </>
  );
}

export function CheckpointCard({ checkpoint, onAnswer }: CheckpointCardProps) {
  const { t } = useTranslation();
  const [selectedIndexes, setSelectedIndexes] = useState<number[]>([]);

  const labels = metricLabels(checkpoint.options);
  const conflicts = conflictViews(checkpoint.options);
  const steps = planSteps(checkpoint.options);
  const candidates = hypothesisStatements(checkpoint.options);

  const submitConfirm = () => {
    // 勾了候选才带上 selectedIndexes；未勾选 = 空 choice（与既有行为一致）。
    onAnswer("confirm", selectedIndexes.length > 0 ? { selectedIndexes } : {});
  };

  const toggleCandidate = (index: number, checked: boolean) => {
    setSelectedIndexes((prev) =>
      checked ? [...prev, index] : prev.filter((item) => item !== index),
    );
  };

  // 明细块按相位出：只有对应相位才可能承载该数据，其他相位不渲染（避免每张卡片都挂
  // 一句与本相位无关的空态文案）。
  return (
    <Card size="small" title={t("research.checkpoint.title")}>
      <CheckpointTargetLine phase={checkpoint.phase} />
      <p>{checkpoint.prompt}</p>
      <MetricTags labels={labels} />

      {checkpoint.phase === "runtime_dynamic" && <ConflictDetails conflicts={conflicts} />}
      {checkpoint.phase === "planning" && <PlanSteps steps={steps} />}
      {checkpoint.phase === "hypothesis" && (
        <CandidateList
          statements={candidates}
          selectedIndexes={selectedIndexes}
          onToggle={toggleCandidate}
        />
      )}

      <CheckpointActions
        phase={checkpoint.phase}
        onConfirm={submitConfirm}
        onReject={() => onAnswer("reject", {})}
        onModify={(question) => onAnswer("modify", { question })}
      />
    </Card>
  );
}
