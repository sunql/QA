/**
 * 预算上限：与后端系统天花板一致（backend/app/config.py 的
 * AGENT_ORCHESTRATION_MAX_*）。超过上限后端一律 422，前端先行拦一道。
 */
export const MAX_ITERATIONS = 20;
export const MAX_TOOL_CALLS = 30;
export const MAX_COST_USD = 5;
export const MAX_TIMEOUT_SECONDS = 900;

/** 观测字符数上下限：与后端 OBSERVATION_CHARS_MIN/MAX 一致。 */
export const MIN_OBSERVATION_CHARS = 1000;
export const MAX_OBSERVATION_CHARS = 50000;
