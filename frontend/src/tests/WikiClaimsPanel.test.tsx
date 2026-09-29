/**
 * WikiClaimsPanel 双 badge 渲染测试（v3.1 任务 M5 / 蓝图 §4.13）。
 *
 * 验收：WikiClaimsPanel 同时展示 authorityLevel + authorityDepartment 两个 badge。
 *
 * 测三类数据：
 * 1. claim 带 authorityLevel + authorityDepartment → 两 badge 都渲染
 * 2. claim 不带这两字段（兼容旧数据）→ 两格走 "-" 占位，**不抛**
 * 3. 多 claim 时每行都各自展示自己的部门 + 等级
 *
 * 列表接口挂 mocked 网络（setup.ts 已全局 react-i18next 初始化注入 zh-CN 资源），
 * 用真实的 i18n t() 渲染真实中文文案 —— 这样既保证 i18n.ts 顶层 init 副作用跑通，
 * 也保证断言不依赖 key 自身。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { I18nextProvider } from "react-i18next";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";

import WikiClaimsPanel from "../components/wiki/WikiClaimsPanel";

// vi.hoisted：vi.mock 被提升到 import 之上，工厂里引用的变量必须先存在
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

// 极简 i18n init：真实 init 副作用（setI18n）让 useTranslation 能拿到资源
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
            },
        },
    },
});

function renderPanel(pageId: string) {
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
            },
        ]);

        renderPanel("PAGE-1");

        // 表头渲染了，但行内值是 "-"（不抛、不挂掉）
        await waitFor(() => {
            expect(screen.getByText("权威等级")).toBeInTheDocument();
            expect(screen.getByText("归属部门")).toBeInTheDocument();
        });

        // 表里至少要有两个 "-"：两列各一
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