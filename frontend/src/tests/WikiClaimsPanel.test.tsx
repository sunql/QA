/**
 * WikiClaimsPanel 置信度徽标测试（v3.1 任务 B4）。
 *
 * 覆盖 4 级渲染分支：HIGH=green / MEDIUM=blue / LOW=orange / REFUSE=red，
 * REFUSE 悬浮展示具体拒绝原因；confidenceLevel 缺失时显示 "-"。
 * 后端 API 全部 vi.mock 隔离。
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ThemedRoot from "../components/common/ThemedRoot";
import WikiClaimsPanel, { confidenceTooltipText } from "../components/wiki/WikiClaimsPanel";
import { listWikiClaims } from "../api/wikiPages";
import { listImportModels } from "../api/wikiImport";
import type { Evidence, KnowledgeClaim } from "../types/wikiPages";

vi.mock("../api/wikiPages", () => ({
    listWikiClaims: vi.fn(),
    extractWikiClaims: vi.fn(),
}));

vi.mock("../api/wikiImport", () => ({
    listImportModels: vi.fn(),
}));

const mockedListClaims = vi.mocked(listWikiClaims);
const mockedListModels = vi.mocked(listImportModels);

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
        createdTime: null,
        evidences: [EVIDENCE],
        confidenceLevel: null,
        refuseReason: null,
        ...overrides,
    };
}

function renderPanel() {
    return render(
        <ThemedRoot>
            <WikiClaimsPanel pageId="PAGE-A" />
        </ThemedRoot>
    );
}

describe("WikiClaimsPanel 置信度徽标", () => {
    it("四个级别分别渲染对应颜色 Tag 与短标签", async () => {
        mockedListClaims.mockResolvedValue([
            makeClaim({ id: 1, confidenceLevel: "HIGH", refuseReason: null }),
            makeClaim({ id: 2, confidenceLevel: "MEDIUM", refuseReason: null }),
            makeClaim({ id: 3, confidenceLevel: "LOW", refuseReason: null }),
            makeClaim({ id: 4, confidenceLevel: "REFUSE", refuseReason: "存在未解决的多源知识冲突" }),
        ]);
        mockedListModels.mockResolvedValue([]);

        renderPanel();

        const high = await screen.findByTestId("confidence-tag-HIGH");
        expect(high).toBeTruthy();
        expect(high.className).toContain("ant-tag-green");
        expect(screen.getByTestId("confidence-tag-MEDIUM").className).toContain("ant-tag-blue");
        expect(screen.getByTestId("confidence-tag-LOW").className).toContain("ant-tag-orange");
        expect(screen.getByTestId("confidence-tag-REFUSE").className).toContain("ant-tag-red");
        // 短标签文案（zh-CN）
        expect(screen.getByTestId("confidence-tag-REFUSE").textContent).toBe("无法判定");
    });

    it("REFUSE 悬浮展示具体拒绝原因", async () => {
        const user = userEvent.setup();
        mockedListClaims.mockResolvedValue([
            makeClaim({ id: 4, confidenceLevel: "REFUSE", refuseReason: "存在未解决的多源知识冲突" }),
        ]);
        mockedListModels.mockResolvedValue([]);

        renderPanel();
        await user.hover(await screen.findByTestId("confidence-tag-REFUSE"));

        expect(
            await screen.findByText(/无法判定，原因：存在未解决的多源知识冲突/)
        ).toBeInTheDocument();
    });

    it("confidenceLevel 缺失 → 显示 '-'", async () => {
        mockedListClaims.mockResolvedValue([makeClaim({ id: 5, confidenceLevel: null })]);
        mockedListModels.mockResolvedValue([]);

        renderPanel();
        expect(await screen.findByText("供应商注册资本不低于 1000 万")).toBeInTheDocument();
        expect(screen.getByText("-")).toBeInTheDocument();
        expect(screen.queryByTestId("confidence-tag-HIGH")).toBeNull();
    });

    it("confidenceTooltipText：非 REFUSE 返回级别话术，绝不输出数值", () => {
        const t = (key: string) => key;
        expect(confidenceTooltipText("HIGH", null, t)).toBe("wikiPages.claims.confidenceLevels.HIGH");
        expect(confidenceTooltipText("REFUSE", "多源冲突", t)).toBe(
            "wikiPages.claims.refuseTooltip"
        );
    });
});
