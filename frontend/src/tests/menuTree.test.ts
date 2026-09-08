/** MenuTree 纯函数单测（feat-menu-tree）。镜像 organizationTree.test.ts 13 用例。
 *
 * 覆盖：buildMenuTree 字段映射 + children 递归 + 空 children；
 * findMenuNode 顶层/深层/不存在；
 * isDescendant 自身/后代/兄弟/不存在。
 */
import { describe, expect, it } from "vitest";
import type { MenuConfigTreeNode } from "../types/menuConfig";
import {
    buildMenuTree,
    findMenuNode,
    isDescendant,
} from "../components/rbac/menuTreeUtils";

const tree: MenuConfigTreeNode[] = [
    {
        code: "section.systemConfig",
        labelKey: "menu.section.systemConfig",
        iconCode: "setting",
        sortOrder: 500,
        path: null,
        permissionCode: null,
        visible: true,
        parentCode: null,
        children: [
            {
                code: "adminUsers",
                labelKey: "menu.admin.users",
                iconCode: "user",
                sortOrder: 540,
                path: "/admin/users",
                permissionCode: "admin.users.read",
                visible: true,
                parentCode: "section.systemConfig",
                children: [],
            },
            {
                code: "adminRoles",
                labelKey: "menu.admin.roles",
                iconCode: "team",
                sortOrder: 550,
                path: "/admin/users/roles",
                permissionCode: "admin.roles.read",
                visible: true,
                parentCode: "section.systemConfig",
                children: [],
            },
        ],
    },
    {
        code: "section.auditSecurity",
        labelKey: "menu.section.auditSecurity",
        iconCode: "safety",
        sortOrder: 600,
        path: null,
        permissionCode: null,
        visible: true,
        parentCode: null,
        children: [
            {
                code: "auditLog",
                labelKey: "menu.audit.log",
                iconCode: "file",
                sortOrder: 600,
                path: "/audit/log",
                permissionCode: "audit.log.read",
                visible: true,
                parentCode: "section.auditSecurity",
                children: [],
            },
        ],
    },
];

describe("buildMenuTree", () => {
    it("maps nested MenuConfigTreeNode to antd TreeDataNode", () => {
        const data = buildMenuTree(tree);
        expect(data).toHaveLength(2);
        expect(data[0]?.key).toBe("section.systemConfig");
        expect(data[0]?.title).toBe(
            "menu.section.systemConfig（section）",
        );
        // data[1] = section.auditSecurity → 1 child (auditLog)
        expect(data[1]?.children).toHaveLength(1);
        expect(data[1]?.children?.[0]?.key).toBe("auditLog");
    });

    it("preserves sortOrder + visible + path 透传字段", () => {
        const data = buildMenuTree(tree);
        const section = data[0] as unknown as {
            sortOrder?: number;
            visible?: boolean;
            path?: string | null;
        };
        expect(section.sortOrder).toBe(500);
        expect(section.visible).toBe(true);
        expect(section.path).toBeNull();
    });

    it("递归处理 children 数组（section → item 2 层）", () => {
        const data = buildMenuTree(tree);
        const section = data[0];
        expect(section?.children).toHaveLength(2);
        expect(section?.children?.[0]?.key).toBe("adminUsers");
        expect(section?.children?.[0]?.title).toBe(
            "menu.admin.users（/admin/users）",
        );
    });

    it("空 children 数组保留为 []（不渲染展开箭头）", () => {
        const data = buildMenuTree(tree);
        const item = data[0]?.children?.[0];
        expect(item?.children).toEqual([]);
    });

    it("空输入 → 空数组", () => {
        expect(buildMenuTree([])).toEqual([]);
    });
});

describe("findMenuNode", () => {
    it("顶层命中", () => {
        const hit = findMenuNode(tree, "section.systemConfig");
        expect(hit?.code).toBe("section.systemConfig");
    });

    it("深层命中（叶子项）", () => {
        const hit = findMenuNode(tree, "adminUsers");
        expect(hit?.path).toBe("/admin/users");
    });

    it("不存在 → null", () => {
        expect(findMenuNode(tree, "nonexistent")).toBeNull();
    });
});

describe("isDescendant", () => {
    it("同一 code 是自身（true，阻止 drop 到自身下）", () => {
        expect(
            isDescendant(tree, "section.systemConfig", "section.systemConfig"),
        ).toBe(true);
    });

    it("直接子节点是真后代", () => {
        expect(
            isDescendant(tree, "section.systemConfig", "adminUsers"),
        ).toBe(true);
    });

    it("另一根 section 下的子节点不是当前 section 的后代", () => {
        expect(
            isDescendant(tree, "section.systemConfig", "auditLog"),
        ).toBe(false);
    });

    it("另一根节点不是后代", () => {
        expect(
            isDescendant(tree, "section.systemConfig", "section.auditSecurity"),
        ).toBe(false);
    });

    it("根 code 不存在 → false", () => {
        expect(isDescendant(tree, "nonexistent", "adminUsers")).toBe(false);
    });
});
