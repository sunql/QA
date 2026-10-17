import { useEffect, useState, useCallback } from "react";
import { listLlmModels } from "../api/dataQualityGenerate";
import type { LlmModelOption } from "../types/dataQualityGenerate";

/**
 * 加载活跃 LLM 模型列表（spec §N4）。
 *
 * 复用 listLlmModels() 已有数据源（per memory qa-system-llm-model-selector）。
 * Race-safe：loadId 守卫避免晚到的 setState 覆盖新请求结果。
 */
export function useLlmModels(): {
  data: LlmModelOption[];
  loading: boolean;
  error: Error | null;
  reload: () => void;
} {
  const [data, setData] = useState<LlmModelOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const reload = useCallback(() => {
    let active = true;
    setLoading(true);
    listLlmModels()
      .then((models) => {
        if (active) {
          setData(models);
          setError(null);
        }
      })
      .catch((e: unknown) => {
        if (active) {
          setError(e instanceof Error ? e : new Error(String(e)));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);

  useEffect(reload, [reload]);
  return { data, loading, error, reload };
}