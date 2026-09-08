/** OrganizationTree 纯函数单测（feat-org-tree）。
 *
 * 覆盖：buildOrgTree 字段映射 + children 递归 + 空 children；
 * findOrgNode 顶层/深层/不存在；
 * isDescendant 自身/后代/兄弟/不存在。
 */
import { describe, expect, it } from "vitest";
import type { OrganizationTreeNode } from "../types/rbac";
import {
    buildOrgTree,
    findOrgNode,
    isDescendant,
} from "../components/rbac/organizationTreeUtils";

const tree: OrganizationTreeNode[] = [
    {
        id: 1,
        code: "rootA",
        name: "Root A",
        sortOrder: 0,
        description: null,
        children: [
            {
                id: 11,
                code: "a.child1",
                name: "A-Child-1",
                sortOrder: 0,
                description: null,
                children: [
                    {
                        id: 111,
                        code: "a.c1.g1",
                        name: "A-C1-Grand",
                        sortOrder: 0,
                        description: null,
                        children: [],
                    },
                ],
            },
            {
                id: 12,
                code: "a.child2",
                name: "A-Child-2",
                sortOrder: 1,
                description: null,
                children: [],
            },
        ],
    },
    {
        id: 2,
        code: "rootB",
        name: "Root B",
        sortOrder: 1,
        description: null,
        children: [],
    },
];

describe("buildOrgTree", () => {
    it("maps nested OrganizationTreeNode to antd TreeDataNode", () => {
        const data = buildOrgTree(tree);
        expect(data).toHaveLength(2);
        expect(data[0]?.key).toBe("1");
        expect(data[0]?.title).toBe("Root A（rootA）");
        expect(data[1]?.children).toEqual([]);
    });

    it("preserves sortOrder透传字段", () => {
        const data = buildOrgTree(tree);
        // antd DataNode 没有 sortOrder 字段，但 buildOrgTree 把它挂在节点上
        // 供拖拽回调透传使用；这里断言原始属性可读。
        expect((data[0] as unknown as { sortOrder?: number }).sortOrder).toBe(0);
        expect((data[1] as unknown as { sortOrder?: number }).sortOrder).toBe(1);
    });

    it("递归处理 grandchildren children 数组", () => {
        const data = buildOrgTree(tree);
        const aChild1 = data[0]?.children?.[0];
        expect(aChild1?.key).toBe("11");
        expect(aChild1?.children).toHaveLength(1);
        expect(aChild1?.children?.[0]?.key).toBe("111");
    });

    it("空 children 数组保留为 undefined 友好（不渲染展开箭头）", () => {
        const data = buildOrgTree(tree);
        const grand = data[0]?.children?.[0]?.children?.[0];
        expect(grand?.children).toEqual([]);
    });

    it("空输入 → 空数组", () => {
        expect(buildOrgTree([])).toEqual([]);
    });
});

describe("findOrgNode", () => {
    it("顶层命中", () => {
        const hit = findOrgNode(tree, 1);
        expect(hit?.code).toBe("rootA");
    });

    it("深层命中", () => {
        const hit = findOrgNode(tree, 111);
        expect(hit?.code).toBe("a.c1.g1");
    });

    it("不存在 → null", () => {
        expect(findOrgNode(tree, 999)).toBeNull();
    });
});

describe("isDescendant", () => {
    it("同一 id 是自身（true，阻止 drop 到自身下）", () => {
        expect(isDescendant(tree, 1, 1)).toBe(true);
    });

    it("直接子节点是真后代", () => {
        expect(isDescendant(tree, 1, 11)).toBe(true);
    });

    it("孙节点是真后代", () => {
        expect(isDescendant(tree, 1, 111)).toBe(true);
    });

    it("另一根节点不是后代", () => {
        expect(isDescendant(tree, 1, 2)).toBe(false);
    });

    it("根 id 不存在 → false", () => {
        expect(isDescendant(tree, 999, 1)).toBe(false);
    });
});