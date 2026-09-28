/** RBAC 菜单授权弹窗（feat-rbac-identity Phase E）。
 *
 * 把「当前主体（用户/角色/组织）已授权的叶子菜单 code」渲染成可勾选的分组树
 * （一级类 → 叶子项），整体替换保存（set-replace 语义）。支持 readOnly 查看模式
 * （需求 #4：查看任意角色/组织当前授权）。load 数据统一走 GET /menu-config/admin
 * 全量扁平行，避免与侧边栏可见性（按人过滤）耦合。
 *
 * 树的 key = 菜单 code；授权粒度是叶子项（path 非空），父节点勾选 = 全选子叶子。
 */
import { useEffect, useMemo, useState } from "react";
import type { Key, ReactNode } from "react";
import { Modal, Tree, Empty, Spin } from "antd";
import type { TreeDataNode, TreeProps } from "antd";
import { useTranslation } from "react-i18next";
import { listMenuRows } from "../../api/menuConfig";
import type { MenuRow } from "../../types/menuConfig";

/** 从树里挑出「用户实际勾选的叶子 code」。
 *
 * antd 默认（非 checkStrictly）会在 onCheck 里返回全选父节点及其所有子叶子；
 * 授权只认叶子 code，故用 leafSet 过滤。
 */
export function pickLeafCodes(
    checked: readonly Key[],
    leafSet: ReadonlySet<string>,
): string[] {
    const seen: string[] = [];
    for (const k of checked) {
        const code = String(k);
        if (leafSet.has(code)) {
            seen.push(code);
        }
    }
    return seen;
}

/** code → 展示标签 的扁平查找表（含一级类与叶子），供 Tag / 列表列渲染。 */
export function buildMenuLabelMap(
    rows: readonly MenuRow[],
    resolveLabel: (labelKey: string) => string,
): Map<string, string> {
    const map = new Map<string, string>();
    for (const row of rows) {
        map.set(row.code, resolveLabel(row.labelKey));
    }
    return map;
}

/** 把全量扁平行聚成「一级类 → 叶子」的 antd TreeDataNode[]。
 *
 * isSection = path 为空（与后端 leaf_codes 判定一致：path 非空才可授权）。
 * label 用 resolveLabel(labelKey) 解析（i18n 缺失时返回 key 本身，渲染不炸）。
 */
export function buildMenuTree(
    rows: readonly MenuRow[],
    resolveLabel: (labelKey: string) => string,
): { treeData: TreeDataNode[]; leafSet: Set<string> } {
    const sections = rows.filter((r) => r.path == null);
    const leaves = rows.filter((r) => r.path != null);
    const childrenByParent = new Map<string, MenuRow[]>();
    for (const leaf of leaves) {
        const parent = leaf.parentCode ?? "";
        const bucket = childrenByParent.get(parent);
        if (bucket) {
            bucket.push(leaf);
        } else {
            childrenByParent.set(parent, [leaf]);
        }
    }
    const sortRows = (a: MenuRow, b: MenuRow) =>
        a.sortOrder - b.sortOrder || a.code.localeCompare(b.code);
    const leafSet = new Set<string>(leaves.map((l) => l.code));
    const treeData: TreeDataNode[] = [];
    // 优先按一级类分组；孤儿叶子（parentCode 未命中任何 section）兜底挂根。
    for (const section of [...sections].sort(sortRows)) {
        const kids = (childrenByParent.get(section.code) ?? [])
            .sort(sortRows)
            .map((leaf) => ({
                key: leaf.code,
                title: resolveLabel(leaf.labelKey),
            }));
        treeData.push({
            key: section.code,
            title: resolveLabel(section.labelKey),
            children: kids,
        });
    }
    const sectionCodes = new Set(sections.map((s) => s.code));
    const orphans = leaves
        .filter((l) => !sectionCodes.has(l.parentCode ?? ""))
        .sort(sortRows)
        .map((leaf) => ({
            key: leaf.code,
            title: resolveLabel(leaf.labelKey),
        }));
    for (const o of orphans) {
        treeData.push(o);
    }
    return { treeData, leafSet };
}

interface MenuGrantModalProps {
    open: boolean;
    /** Modal 标题（通常是「给 <名称> 授权」/「查看 <名称> 权限」）。 */
    title: ReactNode;
    /** 当前已授权的叶子 code（打开时作为初值；受控显示勾选）。 */
    value: string[];
    /** 编辑模式回调；readOnly 时不传则隐藏确定按钮。 */
    onSave?: (codes: string[]) => void;
    onCancel: () => void;
    /** 查看模式：禁止勾选，仅展示当前授权。 */
    readOnly?: boolean;
    /** 保存 / 打开期间的全局 loading 语义（外部刷新列表用）。 */
    saving?: boolean;
}

/** 单个弹窗状态：加载全量菜单行 → 勾选授权 / 只读展示。 */
export function MenuGrantModal({
    open,
    title,
    value,
    onSave,
    onCancel,
    readOnly = false,
    saving = false,
}: MenuGrantModalProps): JSX.Element {
    const { t } = useTranslation();
    const [rows, setRows] = useState<MenuRow[]>([]);
    const [loading, setLoading] = useState(false);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [checked, setChecked] = useState<string[]>([]);

    useEffect(() => {
        if (!open) {
            return;
        }
        setChecked(value);
        setLoadError(null);
        let alive = true;
        setLoading(true);
        listMenuRows()
            .then((data) => {
                if (alive) {
                    setRows(data);
                }
            })
            .catch((e: unknown) => {
                if (alive) {
                    const err = e as Error & { message?: string };
                    setLoadError(err.message ?? String(e));
                }
            })
            .finally(() => {
                if (alive) {
                    setLoading(false);
                }
            });
        return () => {
            alive = false;
        };
    }, [open, value]);

    const { treeData, leafSet } = useMemo(
        () => buildMenuTree(rows, (labelKey) => t(labelKey)),
        [rows, t],
    );

    const onCheck: TreeProps["onCheck"] = (checkedKeys) => {
        const raw = Array.isArray(checkedKeys)
            ? checkedKeys
            : checkedKeys.checked;
        setChecked(pickLeafCodes(raw, leafSet));
    };

    const ok = async () => {
        if (!onSave) {
            return;
        }
        await onSave(checked);
    };

    return (
        <Modal
            open={open}
            title={title}
            onCancel={onCancel}
            onOk={() => void ok()}
            okText={t("rbac.common.save")}
            cancelText={t("rbac.common.cancel")}
            okButtonProps={{ hidden: readOnly || !onSave }}
            confirmLoading={saving}
            width={560}
        >
            <Spin spinning={loading}>
                {loadError ? (
                    <Empty description={loadError} />
                ) : treeData.length === 0 && !loading ? (
                    <Empty description={t("rbac.menu.noMenus")} />
                ) : (
                    <Tree
                        checkable
                        disabled={readOnly}
                        defaultExpandAll
                        selectable={false}
                        checkedKeys={checked}
                        onCheck={onCheck}
                        treeData={treeData}
                    />
                )}
            </Spin>
        </Modal>
    );
}

export default MenuGrantModal;
