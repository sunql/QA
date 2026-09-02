import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useAgentOptions, _resetCache } from "../hooks/useAgentOptions";

const api = vi.hoisted(() => ({
    getAgentOptions: vi.fn(),
}));
vi.mock("../api/agentOptions", () => api);

describe("useAgentOptions", () => {
    beforeEach(() => {
        api.getAgentOptions.mockReset();
        _resetCache();
    });

    it("初次调用拉取并返回词表", async () => {
        api.getAgentOptions.mockResolvedValue({
            domains: ["PROCUREMENT", "QUALITY", "LOGISTICS"],
            layers: ["DIM", "DWD", "FEATURE"],
        });
        const { result } = renderHook(() => useAgentOptions());
        await waitFor(() => expect(result.current.loading).toBe(false));
        expect(result.current.domains).toEqual(["PROCUREMENT", "QUALITY", "LOGISTICS"]);
        expect(result.current.layers).toEqual(["DIM", "DWD", "FEATURE"]);
        expect(result.current.error).toBeNull();
    });

    it("接口失败 → error 文案", async () => {
        api.getAgentOptions.mockRejectedValue(new Error("network"));
        const { result } = renderHook(() => useAgentOptions());
        await waitFor(() => expect(result.current.loading).toBe(false));
        expect(result.current.error).toBeTruthy();
        expect(result.current.domains).toEqual([]);
    });
});
