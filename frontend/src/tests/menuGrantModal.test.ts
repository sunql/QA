/** MenuGrantModal 纯函数单测（feat-rbac-identity Phase E）。
 *
 * 只测无 DOM 依赖的纯逻辑：菜单行 → antd 树 / 叶子集 / 标签表 / 勾选过滤。
 * 树结构契约与后端 leaf_codes 判定一致：path 为空 = 一级类，path 非空 = 可授权叶子。
 */
import { describe, expect, it } from "vitest";
import {
    buildMenuLabelMap,
    buildMenuTree,
    pickLeafCodes,
} from "../components/rbac/MenuGrantModal";
import type { MenuRow } from "../types/menuConfig";

function row(partial: Partial<MenuRow> & { code: string }): MenuRow {
    const code = partial.code;
    const isSection = partial.path == null;
    return {
        id: partial.id ?? 0,
        code,
        path: isSection ? null : (partial.path ?? null),
        parentId: isSection ? null : (partial.parentId ?? 1),
        parentCode: isSection ? null : (partial.parentCode ?? null),
        labelKey: partial.labelKey ?? code + ".label",
        iconCode: partial.iconCode ?? null,
        sortOrder: partial.sortOrder ?? 0,
        permissionCode: partial.permissionCode ?? null,
        visible: partial.visible ?? true,
        hasChildren: partial.hasChildren ?? false,
    };
}

const SECTIONS: MenuRow[] = [
    row({ code: "section.a", labelKey: "menu.section.a", sortOrder: 200 }),
    row({ code: "section.b", labelKey: "menu.section.b", sortOrder: 100 }),
];
const LEAVES: MenuRow[] = [
    row({
        code: "item.a1",
        path: "/a1",
        parentCode: "section.a",
        sortOrder: 20,
    }),
    row({
        code: "item.a0",
        path: "/a0",
        parentCode: "section.a",
        sortOrder: 10,
    }),
    row({ code: "item.orphan", path: "/orphan", parentCode: "gone" }),
];

describe("buildMenuTree", () => {
    it("groups leaf rows under their section and sorts by sortOrder", () => {
        const { treeData } = buildMenuTree(
            [...LEAVES, ...SECTIONS],
            (key) => key,
        );
        // 一级类按 sortOrder 升序 → section.b 在前
        expect(treeData[0].key).toBe("section.b");
        expect(treeData[1].key).toBe("section.a");
        // 每个一级类下的叶子按 sortOrder
        const sectionA = treeData.find((n) => n.key === "section.a");
        expect(sectionA?.children?.map((c) => c.key)).toEqual(["item.a0", "item.a1"]);
        expect(treeData).toHaveLength(3);
    });

    it("keeps only leaf codes in leafSet and appends orphan leaves at root", () => {
        const { treeData, leafSet } = buildMenuTree(
            [...SECTIONS, ...LEAVES],
            (key) => key,
        );
        expect(leafSet.has("item.a1")).toBe(true);
        expect(leafSet.has("section.a")).toBe(false);
        const orphan = treeData.find((n) => n.key === "item.orphan");
        expect(orphan).toBeDefined();
    });
});

describe("pickLeafCodes", () => {
    const leafSet = new Set(["item.a1", "item.a0"]);

    it("drops section codes and only keeps leaf codes", () => {
        const picked = pickLeafCodes(
            ["section.a", "item.a1", "section.b", "item.a0"],
            leafSet,
        );
        expect(picked).toEqual(["item.a1", "item.a0"]);
    });
});

describe("buildMenuLabelMap", () => {
    it("maps every code to resolved label", () => {
        const map = buildMenuLabelMap([...SECTIONS, ...LEAVES], (key) => `L:${key}`);
        expect(map.get("section.a")).toBe("L:menu.section.a");
        expect(map.get("item.a1")).toBe("L:item.a1.label");
    });
});
