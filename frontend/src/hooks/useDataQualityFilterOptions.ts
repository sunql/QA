import { useEffect, useState } from "react";
import { listRuleOptions } from "../api/dataQuality";
import type { RuleOptions } from "../types/dataQuality";

// 模块级缓存（feat-dq-rule-list-filters）：5 字段筛选下拉的选项数据，
// 跨组件共享同一份，避免每处 Select 都重发 /rules/options。
// 失败时不缓存，下次调用重试。简洁优先——暂不上 React Query。
let _cache: RuleOptions | null = null;
let _inflight: Promise<RuleOptions> | null = null;

// 仅供测试重置缓存使用
export function _resetCache(): void {
    _cache = null;
    _inflight = null;
}

const EMPTY: RuleOptions = {
    ruleNames: [],
    datasourceIds: [],
    targetTables: [],
    severities: [],
    classOptions: [],
};

export function useDataQualityFilterOptions(): {
    options: RuleOptions;
    loading: boolean;
    error: string | null;
    reload: () => void;
} {
    const [data, setData] = useState<RuleOptions>(_cache ?? EMPTY);
    const [loading, setLoading] = useState(_cache === null);
    const [error, setError] = useState<string | null>(null);
    // bump 计数器触发重新加载
    const [reloadTick, setReloadTick] = useState(0);

    useEffect(() => {
        // 显式 reload：清缓存 + 重发
        if (reloadTick > 0) {
            _cache = null;
            _inflight = null;
        }
        if (_cache !== null) {
            setData(_cache);
            setLoading(false);
            return;
        }
        if (_inflight === null) {
            _inflight = listRuleOptions()
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
    }, [reloadTick]);

    return {
        options: data,
        loading,
        error,
        reload: () => setReloadTick((n) => n + 1),
    };
}