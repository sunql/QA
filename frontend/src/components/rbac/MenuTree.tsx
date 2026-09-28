/** MenuTree 组件（feat-menu-tree）。
 *
 * antd Tree 渲染菜单嵌套结构，支持：
 *  - 多根节点（所有 section 都是根）
 *  - 拖拽改父级（onDrop → updateMenu parentCode）
 *  - 拖拽时循环检测（drop 到自身后代 → 拒绝 + toast）
 *  - 叶子项只能 drop 到 section 下（drop 到 item 下 → 拒绝 + toast）
 *  - section 只能 drop 到根（drop 到任意 section 下 → 拒绝，drop 到 item 下 → 拒绝）
 *  - 加载中 / 错误态
 *
 * 与 OrganizationTree 的差异：菜单的「父子约束」更严（叶子不能挂叶子，
 * section 不能挂 section），需要在客户端预检，后端 _would_create_menu_cycle
 * 是双层防御。
 *
 * 父组件负责把数据传入、save 后的刷新、toast 等副作用。
 */
import { useMemo, useState, useCallback } from "react";
import { Tree, Empty, Spin, App } from "antd";
import type { TreeProps } from "antd";
import { useTranslation } from "react-i18next";
import { updateMenu } from "../../api/menuConfig";
import type { MenuConfigTreeNode } from "../../types/menuConfig";
import { buildMenuTree, isDescendant } from "./menuTreeUtils";

interface MenuTreeProps {
    tree: readonly MenuConfigTreeNode[];
    loading: boolean;
    /** 拖拽落位后回调：让父组件决定是 reload 还是乐观更新。 */
    onChanged: () => void;
}

export function MenuTree({
    tree,
    loading,
    onChanged,
}: MenuTreeProps): JSX.Element {
    const { t } = useTranslation();
    const { message } = App.useApp();
    const [expanded, setExpanded] = useState<string[]>([]);
    const treeData = useMemo(() => buildMenuTree(tree), [tree]);

    /** drop key → MenuConfigTreeNode 查找 helper。 */
    const findByCode = useCallback(
        (code: string): MenuConfigTreeNode | null => {
            const walk = (
                nodes: readonly MenuConfigTreeNode[],
            ): MenuConfigTreeNode | null => {
                for (const n of nodes) {
                    if (n.code === code) return n;
                    if (n.children && n.children.length > 0) {
                        const hit = walk(n.children);
                        if (hit) return hit;
                    }
                }
                return null;
            };
            return walk(tree);
        },
        [tree],
    );

    const onDrop = useCallback<NonNullable<TreeProps["onDrop"]>>(
        (info) => {
            const dragKey = String(info.dragNode.key);
            const dropKey = String(info.node.key);
            const dragNode = findByCode(dragKey);
            const dropNode = findByCode(dropKey);
            if (!dragNode || !dropNode) {
                void message.error(t("rbac.menuPage.moveFailed") ?? "调整失败");
                return;
            }
            const dragIsSection = dragNode.path === null;
            const dropIsSection = dropNode.path === null;
            // 同级拖拽暂不启用（与 OrganizationTree 一致）
            if (info.dropToGap) {
                void message.info(
                    t("rbac.menuPage.moveSameLevelHint") ??
                    "同级拖拽暂未启用，请在 Modal 中改 parentCode",
                );
                return;
            }
            // 叶子 → 叶子：拒绝
            if (!dragIsSection && !dropIsSection) {
                void message.error(
                    t("rbac.menuPage.cycleError") ??
                    "叶子项只能挂在一级类下",
                );
                return;
            }
            // section → section：拒绝（与 OrganizationTree 不同——菜单的
            // 后端规则禁止 section 嵌套到 section 下，避免层级无界）
            if (dragIsSection && dropIsSection && dragKey !== dropKey) {
                void message.error(
                    t("rbac.menuPage.sectionNoNest") ??
                    "一级类不能挂到另一个一级类下",
                );
                return;
            }
            if (dragKey === dropKey) {
                void message.error(
                    t("rbac.menuPage.cycleError") ??
                    "不能将节点移动到自身下级（会导致循环引用）",
                );
                return;
            }
            if (isDescendant(tree, dragKey, dropKey)) {
                void message.error(
                    t("rbac.menuPage.cycleError") ??
                    "不能将节点移动到自身下级（会导致循环引用）",
                );
                return;
            }
            updateMenu(dragKey, { parentCode: dropKey })
                .then(() => {
                    void message.success(
                        t("rbac.menuPage.moved") ?? "已调整归属",
                    );
                    onChanged();
                })
                .catch((e: unknown) => {
                    const err = e as Error & { message?: string };
                    void message.error(
                        err.message ??
                        t("rbac.menuPage.moveFailed") ??
                        "调整失败",
                    );
                });
        },
        [tree, findByCode, message, onChanged, t],
    );

    if (!loading && tree.length === 0) {
        return <Empty description={t("rbac.menuPage.empty")} />;
    }
    return (
        <Spin spinning={loading}>
            {treeData.length === 0 ? (
                <Empty description={t("rbac.menuPage.empty")} />
            ) : (
                <Tree
                    treeData={treeData}
                    draggable
                    blockNode
                    defaultExpandAll
                    expandedKeys={expanded}
                    onExpand={(keys) => setExpanded(keys as string[])}
                    onDrop={onDrop}
                />
            )}
        </Spin>
    );
}

export default MenuTree;
