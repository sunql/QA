/** 启用中的业务数据源清单（feat-research-entry-ux-fixes W4）。
 *
 * 用途有二：新建研究时选源（默认选 `isDefault`），以及列表 / 会话页把会话上的
 * `datasourceId` 映射成可读名称（治「不知道对哪个库研究」）。
 *
 * 失败或形状异常一律回落空清单：数据源名只是**辅助信息**，不该成为页面可用性的
 * 前提（本页不做任何依赖它的写操作）。故此处的降级是有意的，且仅限于展示层；
 * 名称取不到时 `datasourceName` 仍回落 `#id`，用户至少能看到「某个库」。
 */
import { useEffect, useState } from "react";
import { listDataSources } from "../api/datasource";
import type { DataSource } from "../types/datasource";

export function useDatasourceOptions(): DataSource[] {
  const [sources, setSources] = useState<DataSource[]>([]);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const list = await listDataSources(true);
        if (alive && Array.isArray(list)) setSources(list);
      } catch {
        // 展示层降级：清单回落为空，名称查不到时由 datasourceName 回落 `#id`（见文件头注释）。
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return sources;
}

/** 会话上的 datasourceId → 可读名称；id 缺失返回空串，清单里查不到回落 `#id`。 */
export function datasourceName(
  sources: DataSource[],
  id: number | null | undefined,
): string {
  if (id === null || id === undefined) return "";
  return sources.find((source) => source.id === id)?.name ?? `#${id}`;
}
