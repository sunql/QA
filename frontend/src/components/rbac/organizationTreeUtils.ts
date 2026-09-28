/** OrganizationTree 纯函数（feat-org-tree）。
 *
 * buildOrgTree: 把后端嵌套的 OrganizationTreeNode[] 转换为 antd Tree 所需
 * 的 TreeDataNode[]。后端已经按 (parent_id, sort_order) 排序+嵌套，函数只是
 * 类型/字段映射。
 *
 * isDescendant: 给定 rootId 与 candidateId，判断 candidateId 是否是 rootId 的
 * 后代（含自身）。拖拽时若 target 是 drag 的后代，需拒绝以免成环。
 */
import type { TreeDataNode } from "antd";
import type { OrganizationTreeNode } from "../../types/rbac";

export function buildOrgTree(
    nodes: readonly OrganizationTreeNode[],
): TreeDataNode[] {
    return nodes.map((n) => ({
        key: String(n.id),
        title: `${n.name}（${n.code}）`,
        code: n.code,
        sortOrder: n.sortOrder,
        children:
            n.children && n.children.length > 0 ? buildOrgTree(n.children) : [],
    }));
}

/** 树内查找：找 id === nodeId 的 OrganizationTreeNode（DFS）。 */
export function findOrgNode(
    roots: readonly OrganizationTreeNode[],
    nodeId: number,
): OrganizationTreeNode | null {
    for (const n of roots) {
        if (n.id === nodeId) return n;
        if (n.children && n.children.length > 0) {
            const hit = findOrgNode(n.children, nodeId);
            if (hit) return hit;
        }
    }
    return null;
}

/** candidate 是否是 root 的后代（含自身）。
 *  拖拽场景：把 root 拖到 candidate 下 → 若 candidate 是 root 的后代，会成环。
 */
export function isDescendant(
    roots: readonly OrganizationTreeNode[],
    rootId: number,
    candidateId: number,
): boolean {
    const root = findOrgNode(roots, rootId);
    if (!root) return false;
    if (rootId === candidateId) return true;
    return findOrgNode([root], candidateId) !== null;
}