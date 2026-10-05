/** 研究对比页（feat-research-entry Task 12）。
 *
 * 只做一件事：解析 ?ids=a,b 并把 sessionIds 下传给纯展示的 ResearchCompareView。
 * 路由在 App.tsx:134（Task 10 已注册）；此前路由元素是裸 <ResearchCompareView />，
 * 没人读 query —— 本页补上这一层。
 */
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { ResearchCompareView } from "../../components/research/ResearchCompareView";

/** ?ids=a,b → ["a", "b"]（按逗号切、trim、去空项）。 */
export function parseCompareIds(raw: string | null): string[] {
  if (!raw) return [];
  return raw
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

export default function ResearchComparePage() {
  const [searchParams] = useSearchParams();
  const idsParam = searchParams.get("ids");
  const sessionIds = useMemo(() => parseCompareIds(idsParam), [idsParam]);
  return <ResearchCompareView sessionIds={sessionIds} />;
}
