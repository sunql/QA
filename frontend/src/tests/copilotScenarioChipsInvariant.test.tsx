/** 跨场景不变式：**凡 copilot_scenario 配了 chips 的场景，工作区都必须显示推荐问题**。
 *
 * 缺陷（2026-11-26 真实栈浏览器走查）：推荐问题 chips 原先只在「待到货」
 * 欢迎态里渲染 ⇒ 到货跟踪场景配的 4 条 chips 成了**死配置**（配置在、
 * UI 不显示）。既有测试是「按场景逐个断言配置」，跨文件的
 * 「配置 A 有 chips ⇒ UI 显示」不变式没人守，缺陷才漏过去。
 *
 * 本测试直接读 **后端种子**（backend/scripts/seed_copilot_scenarios.py），
 * 把每个配了 chips 的场景逐个跑一遍页面 ⇒ 页面里若再按 scenarioCode
 * 白名单过滤，本测试立刻变红（不是配置快照，是真实渲染断言）。
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import i18n from "../i18n/i18n";
import ProcurementCopilotPage from "../pages/ProcurementCopilotPage";
import type { CopilotScenario, CopilotScenarioChip } from "../api/copilotScenario";

/** 后端种子文件（相对本测试文件定位 ⇒ 与 cwd 无关）。 */
const SEED_FILE_PATH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../backend/scripts/seed_copilot_scenarios.py",
);

const authState = vi.hoisted(() => ({ roles: ["admin"] as string[] }));

const api = vi.hoisted(() => ({
  getAgentCatalog: vi.fn(),
  listModels: vi.fn(),
  streamCopilotMessage: vi.fn(),
  getAgentRunSteps: vi.fn(),
  listCopilotConversations: vi.fn(),
  getCopilotConversation: vi.fn(),
  deleteCopilotConversation: vi.fn(),
  pinCopilotConversation: vi.fn(),
  listDataSources: vi.fn(),
  getPendingDeliverySummary: vi.fn(),
  searchMappings: vi.fn(),
  listPendingDeliverySites: vi.fn(),
  listCopilotScenarios: vi.fn(),
}));

vi.mock("../api/procurementKnowledge", () => ({
  getAgentCatalog: api.getAgentCatalog,
}));
vi.mock("../api/modelConfig", () => ({ listModels: api.listModels }));
vi.mock("../api/datasource", () => ({ listDataSources: api.listDataSources }));
vi.mock("../api/entityMapping", () => ({ searchMappings: api.searchMappings }));
vi.mock("../api/procurementCopilot", () => ({
  streamCopilotMessage: api.streamCopilotMessage,
  listCopilotConversations: api.listCopilotConversations,
  getCopilotConversation: api.getCopilotConversation,
  deleteCopilotConversation: api.deleteCopilotConversation,
  pinCopilotConversation: api.pinCopilotConversation,
  getPendingDeliverySummary: api.getPendingDeliverySummary,
  listPendingDeliverySites: api.listPendingDeliverySites,
}));
vi.mock("../api/agentOrchestrationApi", () => ({
  getAgentRunSteps: api.getAgentRunSteps,
}));
vi.mock("../api/copilotScenario", () => ({
  listCopilotScenarios: api.listCopilotScenarios,
}));
vi.mock("../stores/authStore", () => ({
  useAuthStore: (selector: (state: { user: unknown }) => unknown) =>
    selector({ user: { roles: authState.roles } }),
}));

beforeEach(() => {
  api.listCopilotConversations.mockResolvedValue([]);
  api.listCopilotScenarios.mockResolvedValue([]);
  api.getAgentCatalog.mockResolvedValue([
    {
      agentCode: "PROCUREMENT_SOURCING",
      agentName: "采购寻源 Agent",
      runnable: true,
      status: "active",
      description: null,
      tool: null,
    },
  ]);
  api.listModels.mockResolvedValue([
    { id: 3, modelName: "Qwen3.8-27B-4bit", provider: "local", isActive: true },
  ]);
  api.listDataSources.mockResolvedValue([]);
  api.searchMappings.mockResolvedValue([]);
  api.listPendingDeliverySites.mockResolvedValue([]);
  api.getPendingDeliverySummary.mockResolvedValue({
    summary: {
      openLines: 0,
      openQty: 0,
      overdueLines: 0,
      excludedRows: 0,
    },
    bySupplier: [],
    topLines: [],
  });
  authState.roles = ["admin"];
  localStorage.clear();
});

interface SeedScenario {
  scenarioCode: string;
  name: string;
  agentCode: string;
  chips: CopilotScenarioChip[];
}

/** 解析种子里的字符串字段（取第一个匹配）。 */
function pickString(block: string, key: string): string {
  const match = block.match(new RegExp(`"${key}":\\s*"([^"]*)"`));
  return match ? match[1] : "";
}

/** 解析种子里的 chips 列表（扁平的 label + 槽位键值对）。 */
function parseChips(block: string): CopilotScenarioChip[] {
  const body = block.match(/"chips":\s*\[([\s\S]*?)\]/);
  if (!body) return [];
  const chips: CopilotScenarioChip[] = [];
  for (const entry of body[1].matchAll(/\{([^{}]*)\}/g)) {
    const chip: Record<string, unknown> = {};
    for (const field of entry[1].matchAll(
      /"([^"]+)"\s*:\s*("([^"]*)"|-?\d+(?:\.\d+)?|True|False|null)/g,
    )) {
      const key = field[1];
      const raw = field[2];
      chip[key] = raw.startsWith('"')
        ? raw.slice(1, -1)
        : raw === "True"
          ? true
          : raw === "False"
            ? false
            : raw === "null"
              ? null
              : Number(raw);
    }
    if (typeof chip.label === "string") chips.push(chip as CopilotScenarioChip);
  }
  return chips;
}

/** 从后端种子解析全部场景（scenario_code / name / agent_code / chips）。 */
function parseSeedScenarios(): SeedScenario[] {
  const source = readFileSync(SEED_FILE_PATH, "utf8");
  return source
    .split(/"scenario_code":\s*"/)
    .slice(1)
    .map((block) => ({
      scenarioCode: block.slice(0, block.indexOf('"')),
      name: pickString(block, "name"),
      agentCode: pickString(block, "agent_code"),
      chips: parseChips(block),
    }));
}

/** 种子场景 → 前端 API 契约（camelCase；槽位表单与本不变式无关，略）。 */
function toApiScenario(seed: SeedScenario): CopilotScenario {
  return {
    scenarioCode: seed.scenarioCode,
    name: seed.name,
    description: "",
    iconCode: "car",
    agentCode: seed.agentCode,
    slotSchema: [],
    questionTemplate: "",
    chips: seed.chips,
    resultRenderer: "none",
    requiredRole: null,
  };
}

const seedScenarios = parseSeedScenarios();
const seedScenariosWithChips = seedScenarios.filter((s) => s.chips.length > 0);

function renderPage() {
  render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <ConfigProvider locale={zhCN}>
          <ProcurementCopilotPage />
        </ConfigProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
}

describe("跨场景不变式：配了 chips 的场景都显示推荐问题", () => {
  it("种子里的每个场景都被解析出来（守卫本身不失效）", () => {
    const declared = readFileSync(SEED_FILE_PATH, "utf8").match(
      /"scenario_code":\s*"/g,
    );
    expect(declared).not.toBeNull();
    expect(seedScenarios).toHaveLength(declared?.length ?? 0);
    expect(seedScenariosWithChips.length).toBeGreaterThanOrEqual(2);
    expect(seedScenariosWithChips.map((s) => s.scenarioCode)).toContain(
      "delivery_tracking",
    );
  });

  it.each(seedScenariosWithChips)(
    "场景 $scenarioCode 的推荐问题全部可见",
    async (seed) => {
      api.listCopilotScenarios.mockResolvedValue([toApiScenario(seed)]);
      renderPage();

      const card = await waitFor(() => screen.getAllByText(seed.name)[0]);
      fireEvent.click(card);

      for (const chip of seed.chips) {
        await waitFor(() => expect(screen.getByText(chip.label)).toBeTruthy());
      }
    },
  );
});
