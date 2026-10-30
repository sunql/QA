/** 图表骨架：chart SSE 事件先于答案 token 到达时的等待占位。
 *
 * 没有它，「空气泡里先蹦出整张图」——图比文字先出现，观感是内容乱序。
 * 门控在 MessageItem（`chartReady`）：流式中且正文尚无 token 时渲染本骨架，
 * 首个 token 到达后让位于真正的 ChartRenderer。
 */
import { Skeleton, Typography } from "antd";
import { useTranslation } from "../../i18n";

export default function ChartSkeleton() {
  const { t } = useTranslation();
  return (
    <div role="status" data-testid="chart-skeleton" style={{ padding: "8px 0" }}>
      <Skeleton active title={{ width: "40%" }} paragraph={{ rows: 5 }} />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {t("chat.visual.chartLoading")}
      </Typography.Text>
    </div>
  );
}
