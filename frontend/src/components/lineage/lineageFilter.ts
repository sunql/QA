/** lineageFilter — 血缘图对象级筛选的纯函数（Step 5）。
 *
 * 纯函数、无副作用：输入 edges → 输出候选 / 过滤后 edges，
 * 与 LineageGraph.edgesToGraphOption 同风格，便于单测与复用。
 */
import type { LineageEdgeRead, LineageLayer } from "../../types/lineage";

/** 对象候选：某一层中的一个具体对象 + 相连边数（source 或 target 计一次）。 */
export interface ObjectCandidate {
  layer: LineageLayer;
  object: string;
  count: number;
}

/** (layer, object) 复合键：同一对象名可能跨层出现（如 PORDER vs ODS_PORDER）。 */
export function objectKey(layer: LineageLayer, object: string): string {
  return `${layer}/${object}`;
}

/** 从 edges 收集去重的 (layer, object) 候选（按层再按名排序），附带相连边数。 */
export function collectObjectCandidates(edges: LineageEdgeRead[]): ObjectCandidate[] {
  const byKey = new Map<string, ObjectCandidate>();
  const bump = (layer: LineageLayer, object: string) => {
    if (!object) return;
    const key = objectKey(layer, object);
    const existing = byKey.get(key);
    if (existing) {
      // 不可变更新：新建对象，不修改已累积条目
      byKey.set(key, { layer, object, count: existing.count + 1 });
    } else {
      byKey.set(key, { layer, object, count: 1 });
    }
  };
  for (const edge of edges) {
    bump(edge.sourceLayer, edge.sourceObject);
    // 自环边（source 与 target 同一 (layer, object)）只计一次，避免 count 重复
    if (objectKey(edge.targetLayer, edge.targetObject) !== objectKey(edge.sourceLayer, edge.sourceObject)) {
      bump(edge.targetLayer, edge.targetObject);
    }
  }
  return [...byKey.values()].sort(
    (a, b) => a.layer.localeCompare(b.layer) || a.object.localeCompare(b.object),
  );
}

/** 按选中的 (layer, object) 复合键过滤：保留 source 或 target 触及选中对象的边。 */
export function filterEdgesByObjects(
  edges: LineageEdgeRead[],
  selectedKeys: ReadonlySet<string>,
): LineageEdgeRead[] {
  if (selectedKeys.size === 0) {
    return edges;
  }
  return edges.filter(
    (e) =>
      selectedKeys.has(objectKey(e.sourceLayer, e.sourceObject)) ||
      selectedKeys.has(objectKey(e.targetLayer, e.targetObject)),
  );
}
