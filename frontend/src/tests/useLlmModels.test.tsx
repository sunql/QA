import { renderHook, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";

const listLlmModelsMock = vi.fn();
vi.mock("../api/dataQualityGenerate", () => ({
  listLlmModels: () => listLlmModelsMock(),
}));

import { useLlmModels } from "../hooks/useLlmModels";

describe("useLlmModels", () => {
  beforeEach(() => { listLlmModelsMock.mockReset(); });

  it("首次挂载 → loading=true → 列表填充", async () => {
    listLlmModelsMock.mockResolvedValue([{ id: 1, modelName: "deepseek-chat", provider: "OPENAI_COMPATIBLE_PROXY" }]);
    const { result } = renderHook(() => useLlmModels());
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toHaveLength(1);
    expect(result.current.data[0].modelName).toBe("deepseek-chat");
  });

  it("listLlmModels 抛错 → error 字段填充 + loading=false", async () => {
    listLlmModelsMock.mockRejectedValue(new Error("network"));
    const { result } = renderHook(() => useLlmModels());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.error?.message).toBe("network");
  });
});