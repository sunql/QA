import { useEffect, useState } from "react";
import { getAgentOptions } from "../api/agentOptions";
import type { AgentOptions } from "../types/agentOptions";

// 模块级缓存：跨组件共享同一份词表，避免每处 Select 都重发请求。
// 失败时不缓存，下次调用重试。简洁优先——暂不上 React Query（项目无该依赖）。
let _cache: AgentOptions | null = null;
let _inflight: Promise<AgentOptions> | null = null;

// 仅供测试重置缓存使用
export function _resetCache(): void {
    _cache = null;
    _inflight = null;
}

export function useAgentOptions(): {
    domains: string[];
    layers: string[];
    loading: boolean;
    error: string | null;
} {
    const [data, setData] = useState<AgentOptions>(
        _cache ?? { domains: [], layers: [] }
    );
    const [loading, setLoading] = useState(_cache === null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (_cache !== null) return;
        if (_inflight === null) {
            _inflight = getAgentOptions()
                .then((opts) => {
                    _cache = opts;
                    return opts;
                })
                .catch((err: unknown) => {
                    _inflight = null;
                    throw err;
                });
        }
        let cancelled = false;
        _inflight
            .then((opts) => {
                if (!cancelled) {
                    setData(opts);
                    setLoading(false);
                }
            })
            .catch((err: unknown) => {
                if (!cancelled) {
                    setError(err instanceof Error ? err.message : String(err));
                    setLoading(false);
                }
            });
        return () => {
            cancelled = true;
        };
    }, []);

    return {
        domains: data.domains,
        layers: data.layers,
        loading,
        error,
    };
}
