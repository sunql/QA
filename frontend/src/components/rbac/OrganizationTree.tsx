/** OrganizationTree 组件（feat-org-tree）。
 *
 * antd Tree 渲染组织嵌套结构，支持：
 *  - 多根节点（parentId=null 不唯一）
 *  - 拖拽改父级（onDrop → updateOrganization parentId）
 *  - 拖拽时循环检测（drop 到自身后代 → 拒绝 + toast）
 *  - 加载中 / 错误态
 *
 * 父组件负责把数据传入、save 后的刷新、toast 等副作用。
 */
import { useMemo, useState, useCallback } from "react";
import { Tree, Empty, Spin, App } from "antd";
import type { TreeProps } from "antd";
import { useTranslation } from "react-i18next";
import { updateOrganization } from "../../api/organizations";
import type { OrganizationTreeNode } from "../../types/rbac";
import { buildOrgTree, isDescendant } from "./organizationTreeUtils";

interface OrganizationTreeProps {
    tree: readonly OrganizationTreeNode[];
    loading: boolean;
    /** 拖拽落位后回调：让父组件决定是 reload 还是乐观更新。 */
    onChanged: () => void;
}

export function OrganizationTree({
    tree,
    loading,
    onChanged,
}: OrganizationTreeProps): JSX.Element {
    const { t } = useTranslation();
    const { message } = App.useApp();
    const [expanded, setExpanded] = useState<string[]>([]);
    const treeData = useMemo(() => buildOrgTree(tree), [tree]);

    const onDrop = useCallback<NonNullable<TreeProps["onDrop"]>>(
        (info) => {
            const dragKey = String(info.dragNode.key);
            const dropKey = String(info.node.key);
            const dragId = Number(dragKey);
            const newParentId = Number(dropKey);
            // dropToGap=true 表示 drop 在节点「上方/下方」（同级插入），目前不做；
            // 仅处理 dropToGap=false（作为子节点）= 移动到 dropKey 下
            if (info.dropToGap) {
                void message.info(
                    t("rbac.organization.moveSameLevelHint") ??
                    "同级拖拽暂未启用，请在 Modal 中改 parentId",
                );
                return;
            }
            if (newParentId === dragId) {
                void message.error(
                    t("rbac.organization.cycleError") ??
                    "不能将组织移动到自身下级（会导致循环引用）",
                );
                return;
            }
            if (isDescendant(tree, dragId, newParentId)) {
                void message.error(
                    t("rbac.organization.cycleError") ??
                    "不能将组织移动到自身下级（会导致循环引用）",
                );
                return;
            }
            updateOrganization(dragId, { parentId: newParentId })
                .then(() => {
                    void message.success(
                        t("rbac.organization.moved") ?? "已调整归属",
                    );
                    onChanged();
                })
                .catch((e: unknown) => {
                    const err = e as Error & { message?: string };
                    void message.error(
                        err.message ??
                        t("rbac.organization.moveFailed") ??
                        "调整失败",
                    );
                });
        },
        [tree, message, onChanged, t],
    );

    if (!loading && tree.length === 0) {
        return <Empty description={t("rbac.organization.empty")} />;
    }
    return (
        <Spin spinning={loading}>
            {treeData.length === 0 ? (
                <Empty description={t("rbac.organization.empty")} />
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

export default OrganizationTree;