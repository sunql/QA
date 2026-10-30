import { useCallback, useEffect, useRef, useState } from "react";
import {
  createAgentRun,
  listOrchestrations,
} from "../../../api/agentOrchestrationApi";
import { listModels } from "../../../api/modelConfig";
import type { ModelConfig } from "../../../types/modelConfig";
import type { AgentOrchestrationProfileRead } from "../../../types/agentOrchestration";
import { describeError } from "./errors";
import type { Translate } from "./errors";
import type { CreateRunForm } from "../../../types/agentTask";

/** 任务页需要的可选项：启用中的编排配置与启用中的模型。 */
interface TaskOptions {
  profiles: AgentOrchestrationProfileRead[];
  models: ModelConfig[];
  loadError: string | null;
}

/**
 * 加载可用编排配置与启用中的模型。
 *
 * 只列出 enabled 的编排配置——停用的配置不该出现在任务页上。
 */
function useTaskOptions(t: Translate): TaskOptions {
  const tRef = useRef(t);
  tRef.current = t;
  const [profiles, setProfiles] = useState<AgentOrchestrationProfileRead[]>([]);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fail = (message: string): void => {
      if (!cancelled) setLoadError(tRef.current(message));
    };
    listOrchestrations()
      .then((rows) => {
        if (!cancelled) {
          setProfiles(rows.filter((row) => row.enabled === true));
        }
      })
      .catch(() => fail("agentTask.messages.loadProfilesFailed"));
    listModels(true)
      .then((rows) => {
        if (!cancelled) setModels(rows);
      })
      .catch(() => fail("agentTask.messages.loadModelsFailed"));
    return () => {
      cancelled = true;
    };
  }, []);

  return { profiles, models, loadError };
}

/**
 * 创建运行的动作：逐项校验后提交后端。
 *
 * 提交给后端的载荷只有 agentCode / input / modelConfigId 三项——
 * 用户选中的 modelConfigId 原样提交，绝不按内容自动改派模型。
 */
function useCreateRunAction(
  t: Translate,
  agentCode: string | undefined,
  modelConfigId: number | undefined,
  input: string,
  openRun: (runId: string) => Promise<void>,
  reportError: (message: string) => void,
  clearError: () => void,
): { creating: boolean; handleCreate: () => Promise<void> } {
  const [creating, setCreating] = useState(false);

  const handleCreate = useCallback(async (): Promise<void> => {
    if (!agentCode) {
      reportError(t("agentTask.messages.profileRequired"));
      return;
    }
    if (modelConfigId === undefined) {
      reportError(t("agentTask.messages.modelRequired"));
      return;
    }
    const trimmed = input.trim();
    if (!trimmed) {
      reportError(t("agentTask.messages.inputRequired"));
      return;
    }
    setCreating(true);
    clearError();
    try {
      const created = await createAgentRun({
        agentCode,
        input: trimmed,
        modelConfigId,
      });
      await openRun(created.runId);
    } catch (error: unknown) {
      reportError(describeError(t, error));
    } finally {
      setCreating(false);
    }
  }, [agentCode, modelConfigId, input, t, openRun, reportError, clearError]);

  return { creating, handleCreate };
}

/** 创建运行表单：加载可选项、维护字段、暴露提交动作。 */
export function useCreateRunForm(
  t: Translate,
  openRun: (runId: string) => Promise<void>,
  reportError: (message: string) => void,
  clearError: () => void,
): CreateRunForm {
  const options = useTaskOptions(t);
  const [agentCode, setAgentCode] = useState<string | undefined>(undefined);
  const [modelConfigId, setModelConfigId] = useState<number | undefined>(
    undefined,
  );
  const [input, setInput] = useState("");
  const action = useCreateRunAction(
    t,
    agentCode,
    modelConfigId,
    input,
    openRun,
    reportError,
    clearError,
  );

  return {
    profiles: options.profiles,
    models: options.models,
    loadError: options.loadError,
    agentCode,
    modelConfigId,
    input,
    creating: action.creating,
    onAgentCodeChange: setAgentCode,
    onModelChange: setModelConfigId,
    onInputChange: setInput,
    handleCreate: action.handleCreate,
  };
}
