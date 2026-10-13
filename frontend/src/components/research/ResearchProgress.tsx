// 研究进度实时视图（feat-research-entry 修复轮）。
//
// 由 store.events 累积的 SSE 事件派生，流未结束（streaming）期间展示执行中的步骤，
// 补上原实现「只等流结束重拉时间线、流进行中一片空白」的盲区。checkpoint 由
// CheckpointCard 呈现，connected/done 无进度语义，故都不映射。
import { Empty, Timeline } from "antd";
import { useTranslation } from "react-i18next";
import type { ResearchEventName, ResearchSseEvent } from "../../types/research";

export interface ProgressItem {
  id: string;
  labelKey: string;
}

const PROGRESS_LABEL_KEYS: Partial<Record<ResearchEventName, string>> = {
  "research.intent": "research.progress.intent",
  "research.esl": "research.progress.esl",
  "research.plan": "research.progress.plan",
  "research.step.start": "research.progress.stepStart",
  "research.step.sql": "research.progress.stepSql",
  "research.step.data": "research.progress.stepData",
  "research.step.chart": "research.progress.stepChart",
  "research.step.done": "research.progress.stepDone",
  "research.hypothesis": "research.progress.hypothesis",
  "research.finding": "research.progress.finding",
  "research.report": "research.progress.report",
};

/** 单条事件 → 进度条目的 i18n key；无进度语义返回 `undefined`。 */
function progressLabelKey(event: ResearchSseEvent): string | undefined {
  const mapped = PROGRESS_LABEL_KEYS[event.name];
  if (mapped) return mapped;
  // `research.error` 的处置按 uiHint 的**类**分支，不按 code（仓库规则 ③）：只有
  // degraded 进时间线 —— 它是「本轮闸门降级放行、研究仍在推进」的进度语义。
  // terminal / conflict 各自已有自己的面（主区 error Alert / toast），进时间线会
  // 重复且误导（时间线读作「还在跑」）。
  if (event.name === "research.error" && event.payload.uiHint === "degraded") {
    return "research.progress.degraded";
  }
  return undefined;
}

// 事件流 → 进度条目（不可变：只读输入、返回新数组，绝不改 events）。
export function deriveProgressItems(events: ResearchSseEvent[]): ProgressItem[] {
  const items: ProgressItem[] = [];
  events.forEach((event, index) => {
    const labelKey = progressLabelKey(event);
    if (labelKey) items.push({ id: `${event.name}-${index}`, labelKey });
  });
  return items;
}

interface ResearchProgressProps {
  events: ResearchSseEvent[];
}

export function ResearchProgress({ events }: ResearchProgressProps) {
  const { t } = useTranslation();
  const items = deriveProgressItems(events);

  if (items.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={t("research.progress.empty")}
      />
    );
  }

  return (
    <Timeline
      style={{ marginBottom: 16 }}
      items={items.map((item) => ({
        key: item.id,
        children: <span data-testid={`progress-${item.id}`}>{t(item.labelKey)}</span>,
      }))}
    />
  );
}
