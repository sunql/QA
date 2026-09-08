/** MenuTree 纯函数（feat-menu-tree）。
 *
 * buildMenuTree: 把后端嵌套的 MenuConfigTreeNode[] 转换为 antd Tree 所需的
 * TreeDataNode[]。后端已经按 (sort_order, id) 排序+嵌套，函数只是
 * 类型/字段映射。
 *
 * isDescendant: 给定 rootCode 与 candidateCode，判断 candidateCode 是否是
 * rootCode 的后代（含自身）。拖拽时若 target 是 drag 的后代，需拒绝以免成环。
 *
 * 注意：菜单与组织树的差异——菜单的 id 在 schema 外不可见（API 用 code 作为
 * 路由键 + 权限授予键），所以这里 key/查找键都用 code（string），而不是 id。
 */
import type { TreeDataNode } from "antd";
import type { MenuConfigTreeNode } from "../../types/menuConfig";

export function buildMenuTree(
    nodes: readonly MenuConfigTreeNode[],
): TreeDataNode[] {
    return nodes.map((n) => ({
        key: n.code,
        title: n.path ? `${n.labelKey}（${n.path}）` : `${n.labelKey}（section）`,
        code: n.code,
        sortOrder: n.sortOrder,
        path: n.path,
        visible: n.visible,
        children:
            n.children && n.children.length > 0 ? buildMenuTree(n.children) : [],
    }));
}

/** 树内查找：找 code === nodeCode 的 MenuConfigTreeNode（DFS）。 */
export function findMenuNode(
    roots: readonly MenuConfigTreeNode[],
    nodeCode: string,
): MenuConfigTreeNode | null {
    for (const n of roots) {
        if (n.code === nodeCode) return n;
        if (n.children && n.children.length > 0) {
            const hit = findMenuNode(n.children, nodeCode);
            if (hit) return hit;
        }
    }
    return null;
}

/** candidate 是否是 root 的后代（含自身）。
 *  拖拽场景：把 root 拖到 candidate 下 → 若 candidate 是 root 的后代，会成环。
 */
export function isDescendant(
    roots: readonly MenuConfigTreeNode[],
    rootCode: string,
    candidateCode: string,
): boolean {
    const root = findMenuNode(roots, rootCode);
    if (!root) return false;
    if (rootCode === candidateCode) return true;
    return findMenuNode([root], candidateCode) !== null;
}
