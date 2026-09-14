/**
 * 知识导入向导类型（feat-wiki-knowledge M2）。
 *
 * 字段与后端 `app/domain/wiki_schemas.py` 的 CamelModel 一一对应
 * （后端 snake_case → JSON camelCase）。
 */

/** 导入可选模型；usable=false 的模型不可选，仅作解释性展示 */
export interface WikiImportModel {
    id: number;
    modelName: string;
    provider: string;
    usable: boolean;
    isActive: boolean;
}

/** 切分出的知识条目草稿（可编辑后再提交） */
export interface WikiImportDraft {
    pageId: string | null;
    title: string;
    content: string;
}

export interface WikiImportPreviewResponse {
    drafts: WikiImportDraft[];
    total: number;
}

/**
 * 上传文件的解析结果。`sourceType` 由后端判定（按扩展名/MIME）而不是前端自报，
 * 若要提交可直接回填到 execute 的 `sourceType`，台账才可信。
 */
export interface WikiImportFileParseResponse {
    text: string;
    sourceType: string;
}

export interface WikiImportExecuteRequest {
    drafts: WikiImportDraft[];
    /** 开启自动分类时必填；关闭时可省 */
    modelId?: number | null;
    fallbackModelId?: number | null;
    /** 是否对每条跑机制 1 自动分类（默认开启） */
    autoClassify?: boolean;
    /**
     * 是否启用 Two-Step CoT 摄取（借鉴 llm_wiki-main）。
     * 开启后会先深度分析内容（实体/概念/本体关联/冲突），再基于分析结果导入。
     * 能显著提升知识抽取质量，但 LLM 调用成本翻倍。
     */
    useTwoStep?: boolean;
    sourceType?: string | null;
    sourceRef?: string | null;
    taskType?: string;
    /**
     * 重试某个失败/部分失败的任务（Phase 4）：新 task 写入 ``retry_of_task_id``
     * 关联原 task。content_hash 命中的草稿计入 ``skippedPages`` 而非重跑。
     */
    retryOfTaskId?: number | null;
}

export type WikiImportTaskStatus = "PENDING" | "RUNNING" | "SUCCEEDED" | "PARTIAL" | "FAILED";

export interface WikiImportTask {
    id: number;
    taskType: string;
    sourceType: string | null;
    sourceRef: string | null;
    selectedModelId: number | null;
    fallbackModelId: number | null;
    status: WikiImportTaskStatus;
    pageIds: string[] | null;
    totalPages: number;
    successPages: number;
    /**
     * P1 起：重复项（同 page_id 且正文一致）计入跳过而非失败。
     * 与 successPages 分开下发，否则「整批都是重跑」看起来像任务没干活。
     */
    skippedPages: number;
    failedPages: number;
    /**
     * 后端是 `DECIMAL`，走 FastAPI 的 `jsonable_encoder` 会出成 JSON number。
     * 与 `types/modelConfig.ts` 的金额字段保持同一种联合类型，避免哪天
     * 拿到 number 却按 string 处理（或反之）。
     */
    totalCostUsd: number | string;
    errorMessage: string | null;
    createdByUserId: number | null;
    createdTime: string | null;
    finishedTime: string | null;
    /** 重试链：本次任务是哪个 taskId 的重跑（Phase 4） */
    retryOfTaskId: number | null;
}

export interface WikiImportTaskListResponse {
    rows: WikiImportTask[];
    total: number;
}
