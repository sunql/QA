import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const src = readFileSync(
  join(process.cwd(), "src/pages/DataQualityRuleGeneratePage.tsx"),
  "utf-8"
);

const apiSrc = readFileSync(
  join(process.cwd(), "src/api/dataQualityGenerate.ts"),
  "utf-8"
);

const typesSrc = readFileSync(
  join(process.cwd(), "src/types/dataQualityGenerate.ts"),
  "utf-8"
);

/**
 * 回归测试：LlmPanel 必须携带 LLM 模型选择器。
 *
 * 背景：Task #26 implementer 重写 LlmPanel 时把整个模型选择器删了，导致
 *  parseDescriptions(classId) 不传 modelId → 后端走默认 env 路径 → 503。
 *
 * 修复契约（必须全部满足）：
 *  1. LlmPanel 从 api/dataQualityGenerate 导入 listLlmModels
 *  2. 使用 LlmModelOption 类型
 *  3. 挂载时调用 listLlmModels() 拉取模型列表
 *  4. modelsAttempted race gate
 *  5. modelsLoadFailed 错误展示
 *  6. effectiveModelId = selectedModelId ?? models[0]?.id 回退
 *  7. parseDescriptions 接收 effectiveModelId
 *  8. Collapse header 渲染 Select 下拉
 *  9. 默认选第一个非 ollama 模型
 */
describe("LlmPanel LLM model selector", () => {
  it("imports listLlmModels from api/dataQualityGenerate", () => {
    expect(src).toMatch(
      /import\s*\{[^}]*\blistLlmModels\b[^}]*\}\s*from\s*["']\.\.\/api\/dataQualityGenerate["']/
    );
  });

  it("imports LlmModelOption type", () => {
    expect(src).toMatch(/LlmModelOption/);
  });

  it("defines LlmModelOption in types/dataQualityGenerate", () => {
    expect(typesSrc).toMatch(/LlmModelOption/);
  });

  it("exports listLlmModels from api/dataQualityGenerate", () => {
    expect(apiSrc).toMatch(/export\s+(?:async\s+)?function\s+listLlmModels/);
  });

  it("loads models on mount via listLlmModels", () => {
    expect(src).toContain("listLlmModels()");
  });

  it("tracks modelsAttempted race gate", () => {
    expect(src).toMatch(/modelsAttempted/);
    expect(src).toMatch(/setModelsAttempted/);
  });

  it("tracks modelsLoadFailed for silent failure UI", () => {
    expect(src).toMatch(/modelsLoadFailed/);
    expect(src).toMatch(/setModelsLoadFailed/);
  });

  it("uses effectiveModelId fallback (selectedModelId ?? models[0]?.id)", () => {
    expect(src).toMatch(/effectiveModelId\s*=\s*selectedModelId\s*\?\?\s*models\[0\]/);
  });

  it("passes modelId to parseDescriptions call", () => {
    expect(src).toMatch(/parseDescriptions\(\s*classId\s*,\s*effectiveModelId\s*\)/);
  });

  it("renders Select dropdown with model options in Collapse extra", () => {
    expect(src).toMatch(/extra:\s*headerExtra/);
    expect(src).toMatch(/models\.map\(\(m\)/);
  });

  it("defaults to first non-ollama model", () => {
    expect(src).toMatch(/provider\s*!==\s*["']ollama["']/);
  });
});
