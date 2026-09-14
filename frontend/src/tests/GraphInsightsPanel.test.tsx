/**
 * GraphInsightsPanel Modal 入口测试（Phase 5.5）。
 *
 * 覆盖三类 gap × 两步预览：
 * - MISSING_DIMENSION: 点击「重分类」→ Modal 展示 LLM 建议 → 确认 → PATCH
 * - ISOLATED_PAGE:    点击「查找关联」→ Modal 展示候选 → 确认 → POST /discover
 * - SPARSE_COMMUNITY: 点击「建议主题」→ Modal 展示主题 → 确认 → PATCH topic
 *
 * 全部用 vi.mock 把后端 API 隔离掉，断言「预览 → 用户确认 → 写库」三步串联。
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ThemedRoot from "../components/common/ThemedRoot";
import GraphInsightsPanel from "../components/wiki/GraphInsightsPanel";
import type { GraphInsights } from "../types/wikiGraph";

vi.mock("../api/wikiGraph", () => ({
    getGraphInsights: vi.fn(),
    rescanGraphInsights: vi.fn(),
    previewCommunityTopic: vi.fn(),
    updateCommunityTopic: vi.fn(),
}));

vi.mock("../api/wikiPages", () => ({
    previewWikiPageClassify: vi.fn(),
    suggestWikiRelations: vi.fn(),
    discoverWikiRelations: vi.fn(),
    updateWikiPage: vi.fn(),
}));

import { getGraphInsights, previewCommunityTopic, updateCommunityTopic } from "../api/wikiGraph";
import {
    discoverWikiRelations,
    previewWikiPageClassify,
    suggestWikiRelations,
    updateWikiPage,
} from "../api/wikiPages";

const mockedGetInsights = vi.mocked(getGraphInsights);
const mockedPreviewClassify = vi.mocked(previewWikiPageClassify);
const mockedUpdatePage = vi.mocked(updateWikiPage);
const mockedSuggestRelations = vi.mocked(suggestWikiRelations);
const mockedDiscoverRelations = vi.mocked(discoverWikiRelations);
const mockedPreviewTopic = vi.mocked(previewCommunityTopic);
const mockedUpdateTopic = vi.mocked(updateCommunityTopic);

const baseInsights: GraphInsights = {
    surprisingConnections: [],
    knowledgeGaps: [
        {
            key: "MISSING_DIMENSION|PAGE-A",
            kind: "MISSING_DIMENSION",
            headline: "条目 A 缺少维度",
            explanation: "",
            communityKey: null,
            communityName: null,
            pageCount: null,
            cohesionScore: null,
            pageId: "PAGE-A",
            title: "条目 A",
            degree: null,
            topic: null,
        },
        {
            key: "ISOLATED_PAGE|PAGE-B",
            kind: "ISOLATED_PAGE",
            headline: "条目 B 孤立",
            explanation: "",
            communityKey: null,
            communityName: null,
            pageCount: null,
            cohesionScore: null,
            pageId: "PAGE-B",
            title: "条目 B",
            degree: 0,
            topic: null,
        },
        {
            key: "SPARSE_COMMUNITY|C001",
            kind: "SPARSE_COMMUNITY",
            headline: "社区 C001 稀疏",
            explanation: "",
            communityKey: "C001",
            communityName: "社区 C001",
            pageCount: 3,
            cohesionScore: 0.1,
            pageId: null,
            title: null,
            degree: null,
            topic: null,
        },
    ],
    bridgeNodes: [],
    scannedAt: "2026-09-01T00:00:00Z",
};

function renderPanel() {
    return render(
        <ThemedRoot>
            <GraphInsightsPanel communities={[{ communityKey: "C001", name: "社区 C001" }]} />
        </ThemedRoot>,
    );
}

describe("GraphInsightsPanel — Phase 5.5 gap 操作入口", () => {
    it("MISSING_DIMENSION → 点重分类 → 预览 → 确认 → PATCH", async () => {
        mockedGetInsights.mockReset().mockResolvedValue(baseInsights);
        mockedPreviewClassify.mockReset().mockResolvedValue({
            primary: "POLICY",
            confidence: 0.92,
            alternatives: ["RULE"],
            reason: "测试建议",
            rawOutput: "{}",
        });
        mockedUpdatePage.mockReset().mockResolvedValue({} as never);

        renderPanel();

        // 切到「知识缺口」tab
        await userEvent.click(screen.getByRole("tab", { name: /知识缺口/ }));

        // 点「重分类」按钮
        const button = await screen.findByRole("button", { name: /重分类/ });
        await userEvent.click(button);

        // Modal 调预览 API（弹窗已开 + 拉到建议 = 预览阶段成功）
        await waitFor(() => expect(mockedPreviewClassify).toHaveBeenCalledWith("PAGE-A"));
        // 弹窗内至少有一个「确认」按钮（OK 按钮）；antd 在中文之间插入半角空格
        // 做字间距（"确 认"），所以正则放宽到「确」字匹配即可。
        const okButton = await screen.findByRole("button", { name: /^确\s*认$/ });
        await userEvent.click(okButton);

        await waitFor(() =>
            expect(mockedUpdatePage).toHaveBeenCalledWith("PAGE-A", {
                dimension: "POLICY",
                autoClassification: {
                    primary: "POLICY",
                    confidence: 0.92,
                    alternatives: ["RULE"],
                    reason: "测试建议",
                },
            }),
        );
    });

    it("ISOLATED_PAGE → 点查找关联 → 预览 → 确认 → POST /discover", async () => {
        mockedGetInsights.mockReset().mockResolvedValue(baseInsights);
        mockedSuggestRelations.mockReset().mockResolvedValue({
            candidates: [
                {
                    downstreamType: "PAGE",
                    downstreamId: "PAGE-C",
                    downstreamTitle: "条目 C",
                    relationType: "REFERENCES",
                    confidence: 0.9,
                    reason: "正文出现条目《条目 C》",
                },
            ],
            total: 1,
            classExtractionStatus: "SKIPPED",
            droppedGhosts: [],
        });
        mockedDiscoverRelations.mockReset().mockResolvedValue({} as never);

        renderPanel();

        await userEvent.click(screen.getByRole("tab", { name: /知识缺口/ }));
        await userEvent.click(await screen.findByRole("button", { name: /查找关联/ }));

        await waitFor(() => expect(mockedSuggestRelations).toHaveBeenCalledWith("PAGE-B"));
        const okButton = await screen.findByRole("button", { name: /^确\s*认$/ });
        await userEvent.click(okButton);

        await waitFor(() => expect(mockedDiscoverRelations).toHaveBeenCalledWith("PAGE-B"));
    });

    it("SPARSE_COMMUNITY → 点建议主题 → 预览 → 确认 → PATCH topic", async () => {
        mockedGetInsights.mockReset().mockResolvedValue(baseInsights);
        mockedPreviewTopic.mockReset().mockResolvedValue({
            topic: "采购供应商分级管理",
            pageCount: 3,
            pageTitles: ["采购供应商黑名单", "采购供应商评估", "采购供应商分级"],
        });
        mockedUpdateTopic.mockReset().mockResolvedValue({} as never);

        renderPanel();

        await userEvent.click(screen.getByRole("tab", { name: /知识缺口/ }));
        await userEvent.click(await screen.findByRole("button", { name: /建议主题/ }));

        await waitFor(() => expect(mockedPreviewTopic).toHaveBeenCalledWith("C001"));
        const okButton = await screen.findByRole("button", { name: /^确\s*认$/ });
        await userEvent.click(okButton);

        await waitFor(() =>
            expect(mockedUpdateTopic).toHaveBeenCalledWith("C001", "采购供应商分级管理"),
        );
    });
});
