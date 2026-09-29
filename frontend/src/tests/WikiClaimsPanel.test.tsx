/**
 * WikiClaimsPanel 双 badge + 置信度徽标测试（v3.1 任务 M5 + B4）。
 *
 * 覆盖两类渲染：
 * 1. v3.1 §4.13 治理（M5）：authorityLevel + authorityDepartment 双 badge
 *    - claim 带两字段 → 两 badge 都渲染
 *    - claim 不带两字段（兼容旧数据）→ 两格走 "-" 占位，**不抛**
 *    - 多 claim 时每行都各自展示自己的部门 + 等级
 * 2. v3.1 §12.2 置信度徽标（B4）：HIGH=green / MEDIUM=blue / LOW=orange / REFUSE=red
 *    - REFUSE 悬浮展示具体拒绝原因
 *    - confidenceLevel 缺失 → "-"
 *    - confidenceTooltipText 工具函数正确返回级别话术或 refuseTooltip
 *
 * 列表接口挂 mocked 网络（setup.ts 已全局 react-i18next 初始化注入 zh-CN 资源）。
 *
 * 【i18n 测试隔离说明（F4 review fix）】
 * setup.ts 通过 `import "../i18n"` 加载生产 i18n 资源，但 production
 * zh-CN.ts / en-US.ts 的 `wikiPages.claims.*` 命名空间**还没有**
 * `authorityDepartment` / `authorityLevel` 两个键（M5 仅加在 alembic + ORM +
 * DTO + service 路径，前端组件按组件级 key 读取，跨任务未同步 i18n 资源）。
 * 因此本测试 inline i18next.init 提供完整 resources，覆盖 M5 + B4 全部断言
 * 需要的键，避免依赖生产资源时序 —— M5 + B4 在同一文件共存，inline 比
 * `import("../i18n")` 后再 patch 更稳。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import { I18nextProvider } from "react-i18next";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";

import WikiClaimsPanel, { confidenceTooltipText } from "../components/wiki/WikiClaimsPanel";
import type { Evidence, KnowledgeClaim } from "../types/wikiPages";

const api = vi.hoisted(() => ({
    listWikiClaims: vi.fn(),
    extractWikiClaims: vi.fn(),
    listImportModels: vi.fn(),
}));

vi.mock("../api/wikiPages", () => ({
    listWikiClaims: api.listWikiClaims,
    extractWikiClaims: api.extractWikiClaims,
}));

vi.mock("../api/wikiImport", () => ({
    listImportModels: api.listImportModels,
}));

// 极简 i18n init：完整覆盖 M5 + B4 测试断言所需的所有 wikiPages.claims.* 键。
// 见文件顶部【i18n 测试隔离说明】—— production i18n 暂缺 authorityDepartment /
// authorityLevel 两键，必须 inline 注入。
i18next.use(initReactI18next).init({
    lng: "zh-CN",
    fallbackLng: "zh-CN",
    ns: ["translation"],
    defaultNS: "translation",
    resources: {
        "zh-CN": {
            translation: {
                "wikiPages.claims.claimText": "事实内容",
                "wikiPages.claims.claimType": "类型",
                "wikiPages.claims.evidenceCount": "证据",
                "wikiPages.claims.evidences": "条证据",
                "wikiPages.claims.empty": "暂无事实原子",
                "wikiPages.claims.noEvidence": "无证据",
                "wikiPages.claims.reExtract": "重新抽取",
                "wikiPages.claims.forceReExtractTitle": "强制重新抽取？",
                "wikiPages.claims.forceReExtractDesc": "将删除当前 {count} 条事实原子",
                "wikiPages.claims.forceReExtractOk": "删除并重抽",
                "wikiPages.claims.extractDone": "已抽取 {count} 条",
                "wikiPages.claims.extractAlreadyDone": "已抽过",
                "wikiPages.claims.extractFailed": "失败 {status}",
                "wikiPages.claims.extractStatus": "状态 {status}",
                "wikiPages.claims.authorityLevel": "权威等级",
                "wikiPages.claims.authorityDepartment": "归属部门",
                // B4：4 级置信度徽标
                "wikiPages.claims.confidence": "置信度",
                "wikiPages.claims.confidenceShort": {
                    HIGH: "高",
                    MEDIUM: "中",
                    LOW: "低",
                    REFUSE: "无法判定",
                },
                "wikiPages.claims.confidenceLevels": {
                    HIGH: "高置信度，可作为决策依据",
                    MEDIUM: "中等置信度，建议复核",
                    LOW: "低置信度，仅供参考",
                },
                "wikiPages.claims.refuseTooltip": "无法判定，原因：{reason}",
            },
        },
    },
});

const EVIDENCE: Evidence = {
    id: 1,
    claimId: 1,
    sourceType: "DOCUMENT",
    sourceId: "DOC-1",
    pageNumber: null,
    sectionName: "5.1",
    paragraphNo: null,
    content: "注册资本一千万以上方可准入。",
    createdTime: null,
};

function makeClaim(overrides: Partial<KnowledgeClaim>): KnowledgeClaim {
    return {
        id: 1,
        pageId: "PAGE-A",
        claimText: "供应商注册资本不低于 1000 万",
        claimType: "STATISTIC",
        embeddingRef: null,
        authorityLevel: null,
        authorityDepartment: null,
        createdTime: null,
        evidences: [EVIDENCE],
        confidenceLevel: null,
        refuseReason: null,
        ...overrides,
    };
}

function renderPanel(pageId = "PAGE-A") {
    return render(
        <ConfigProvider>
            <I18nextProvider i18n={i18next}>
                <WikiClaimsPanel pageId={pageId} />
            </I18nextProvider>
        </ConfigProvider>,
    );
}

describe("WikiClaimsPanel 双 badge（v3.1 §4.13 治理）", () => {
    beforeEach(() => {
        api.listWikiClaims.mockReset();
        api.extractWikiClaims.mockReset();
        api.listImportModels.mockReset();
        // 默认模型列表空（避免触发 setState-on-unmounted 警告）
        api.listImportModels.mockResolvedValue([]);
    });

    it("claim 同时带 authorityLevel + authorityDepartment 时两 badge 都渲染", async () => {
        api.listWikiClaims.mockResolvedValue([
            {
                id: 1,
                pageId: "PAGE-1",
                claimText: "注册资本 >= 1000 万",
                claimType: "RULE",
                embeddingRef: null,
                authorityLevel: "L5",
                authorityDepartment: "FINANCE",
                createdTime: "2026-09-29T00:00:00Z",
                evidences: [],
                confidenceLevel: null,
                refuseReason: null,
            },
        ]);

        renderPanel("PAGE-1");

        // 表头两列各自的中文文案
        await waitFor(() => {
            expect(screen.getByText("权威等级")).toBeInTheDocument();
            expect(screen.getByText("归属部门")).toBeInTheDocument();
        });

        // 行内两 badge：FINANCE + L5
        await waitFor(() => {
            expect(screen.getByText("FINANCE")).toBeInTheDocument();
            expect(screen.getByText("L5")).toBeInTheDocument();
        });
    });

    it("claim 缺两字段时走 - 占位，不抛错（兼容迁移期旧数据）", async () => {
        api.listWikiClaims.mockResolvedValue([
            {
                id: 2,
                pageId: "PAGE-1",
                claimText: "成立 >= 3 年",
                claimType: "RULE",
                embeddingRef: null,
                authorityLevel: null,
                authorityDepartment: null,
                createdTime: "2026-09-29T00:00:00Z",
                evidences: [],
                confidenceLevel: null,
                refuseReason: null,
            },
        ]);

        renderPanel("PAGE-1");

        // 表头渲染了，但行内值是 "-"（不抛、不挂掉）
        await waitFor(() => {
            expect(screen.getByText("权威等级")).toBeInTheDocument();
            expect(screen.getByText("归属部门")).toBeInTheDocument();
        });

        // 表里至少要有两个 "-"：authorityDepartment 列 + authorityLevel 列
        await waitFor(() => {
            const cells = document.querySelectorAll("td");
            const dashes = Array.from(cells).filter(
                (c) => c.textContent === "-",
            );
            expect(dashes.length).toBeGreaterThanOrEqual(2);
        });
    });

    it("多 claim：每行都各自展示自己的部门 + 等级", async () => {
        api.listWikiClaims.mockResolvedValue([
            {
                id: 10,
                pageId: "PAGE-X",
                claimText: "claim-A",
                claimType: "RULE",
                embeddingRef: null,
                authorityLevel: "L4",
                authorityDepartment: "SALES_MGMT",
                createdTime: null,
                evidences: [],
                confidenceLevel: null,
                refuseReason: null,
            },
            {
                id: 11,
                pageId: "PAGE-X",
                claimText: "claim-B",
                claimType: "STATISTIC",
                embeddingRef: null,
                authorityLevel: "L2",
                authorityDepartment: "INDUSTRY_STANDARD",
                createdTime: null,
                evidences: [],
                confidenceLevel: null,
                refuseReason: null,
            },
        ]);

        renderPanel("PAGE-X");

        // 两个部门各自独立渲染
        await waitFor(() => {
            expect(screen.getByText("SALES_MGMT")).toBeInTheDocument();
            expect(screen.getByText("INDUSTRY_STANDARD")).toBeInTheDocument();
            // 两个等级
            expect(screen.getByText("L4")).toBeInTheDocument();
            expect(screen.getByText("L2")).toBeInTheDocument();
        });
    });
});

describe("WikiClaimsPanel 置信度徽标（v3.1 §12.2，B4）", () => {
    beforeEach(() => {
        api.listWikiClaims.mockReset();
        api.extractWikiClaims.mockReset();
        api.listImportModels.mockReset();
        api.listImportModels.mockResolvedValue([]);
    });

    it("四个级别分别渲染对应颜色 Tag 与短标签", async () => {
        api.listWikiClaims.mockResolvedValue([
            makeClaim({ id: 1, confidenceLevel: "HIGH", refuseReason: null }),
            makeClaim({ id: 2, confidenceLevel: "MEDIUM", refuseReason: null }),
            makeClaim({ id: 3, confidenceLevel: "LOW", refuseReason: null }),
            makeClaim({
                id: 4,
                confidenceLevel: "REFUSE",
                refuseReason: "存在未解决的多源知识冲突",
            }),
        ]);

        renderPanel();

        const high = await screen.findByTestId("confidence-tag-HIGH");
        expect(high).toBeTruthy();
        expect(high.className).toContain("ant-tag-green");
        expect(screen.getByTestId("confidence-tag-MEDIUM").className).toContain(
            "ant-tag-blue",
        );
        expect(screen.getByTestId("confidence-tag-LOW").className).toContain(
            "ant-tag-orange",
        );
        expect(screen.getByTestId("confidence-tag-REFUSE").className).toContain(
            "ant-tag-red",
        );
        // 短标签文案（zh-CN）
        expect(screen.getByTestId("confidence-tag-REFUSE").textContent).toBe(
            "无法判定",
        );
    });

    it("REFUSE 悬浮展示具体拒绝原因", async () => {
        const user = userEvent.setup();
        api.listWikiClaims.mockResolvedValue([
            makeClaim({
                id: 4,
                confidenceLevel: "REFUSE",
                refuseReason: "存在未解决的多源知识冲突",
            }),
        ]);

        renderPanel();
        await user.hover(await screen.findByTestId("confidence-tag-REFUSE"));

        expect(
            await screen.findByText(/无法判定，原因：存在未解决的多源知识冲突/),
        ).toBeInTheDocument();
    });

    it("confidenceLevel 缺失 → 显示 '-'", async () => {
        api.listWikiClaims.mockResolvedValue([
            makeClaim({ id: 5, confidenceLevel: null }),
        ]);

        renderPanel();
        expect(
            await screen.findByText("供应商注册资本不低于 1000 万"),
        ).toBeInTheDocument();
        // 表内至少有一个 "-"（confidenceLevel 列）
        const cells = document.querySelectorAll("td");
        const dashes = Array.from(cells).filter(
            (c) => c.textContent === "-",
        );
        expect(dashes.length).toBeGreaterThanOrEqual(1);
        expect(screen.queryByTestId("confidence-tag-HIGH")).toBeNull();
    });

    it("confidenceTooltipText：非 REFUSE 返回级别话术，REFUSE 返回 refuseTooltip", () => {
        const t = (key: string, vars?: Record<string, unknown>) =>
            vars && "reason" in vars
                ? `无法判定，原因：${vars.reason}`
                : key;
        // 非 REFUSE：返回级别话术 key
        expect(confidenceTooltipText("HIGH", null, t)).toBe(
            "wikiPages.claims.confidenceLevels.HIGH",
        );
        // REFUSE：返回 refuseTooltip 模板（含 reason）
        expect(confidenceTooltipText("REFUSE", "多源冲突", t)).toBe(
            "无法判定，原因：多源冲突",
        );
    });
});
