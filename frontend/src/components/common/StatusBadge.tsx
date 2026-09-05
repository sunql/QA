/** StatusBadge 状态徽章组件（Phase C）。
 *
 * 设计目标：
 * - 截图风格的小型状态徽章（带 icon 的 pill）
 * - 颜色由 status prop 驱动（success / warning / error / offline）
 * - 颜色与 cssVariables 的 --color-success / -warning / -error / -text-tertiary 单色源
 * - 未知 status 健壮回退到 offline（不抛错、不破坏 UI）
 *
 * 何时使用：
 * - 设备状态（生产中 / 停机 / 关机 / 断网）
 * - 任务状态（成功 / 失败 / 运行中）
 * - 任意需要快速视觉标识语义状态的场景
 *
 * 何时不用：
 * - 数据表行内的 chip（如绑定模具 `A52063719`）— 用 antd Tag 更合适
 */
import type { ReactNode } from "react";
import { CheckCircleFilled, PauseCircleFilled, CloseCircleFilled, MinusCircleFilled } from "@ant-design/icons";
import styles from "../../styles/dashboard.module.css";

export type StatusBadgeStatus = "success" | "warning" | "error" | "offline";

interface StatusBadgeProps {
  status: StatusBadgeStatus;
  children: ReactNode;
  /** 是否显示左侧图标（默认 true；icon=false 时仅显示文字） */
  icon?: boolean;
  /** 追加 className，便于外层覆盖 */
  className?: string;
}

const STATUS_CLASS: Record<StatusBadgeStatus, string> = {
  success: styles.statusBadgeSuccess,
  warning: styles.statusBadgeWarning,
  error: styles.statusBadgeError,
  offline: styles.statusBadgeOffline,
};

const STATUS_ICON: Record<StatusBadgeStatus, typeof CheckCircleFilled> = {
  success: CheckCircleFilled,
  warning: PauseCircleFilled,
  error: CloseCircleFilled,
  offline: MinusCircleFilled,
};

export default function StatusBadge({
  status,
  children,
  icon = true,
  className,
}: StatusBadgeProps) {
  // 未知 status 健壮回退（不破坏 UI）
  const resolvedStatus: StatusBadgeStatus =
    status in STATUS_CLASS ? status : "offline";

  const IconComp = STATUS_ICON[resolvedStatus];
  const cls = [styles.statusBadge, STATUS_CLASS[resolvedStatus], className]
    .filter(Boolean)
    .join(" ");

  return (
    <span className={cls} data-status={resolvedStatus}>
      {icon && <IconComp aria-hidden />}
      {children}
    </span>
  );
}