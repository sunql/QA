import axios from "axios";
import { httpClient } from "./client";
import {
    API_BASE_URL,
    DEFAULT_TENANT_ID,
    DEFAULT_USER_ID,
} from "../config";
import type {
    WikiImportExecuteRequest,
    WikiImportFileParseResponse,
    WikiImportModel,
    WikiImportPreviewResponse,
    WikiImportTask,
    WikiImportTaskListResponse,
} from "../types/wikiImport";

const PREFIX = "/wiki/import";

/** 导入向导可选模型（含 usable 标注，让向导能解释「为什么这个模型不能选」） */
export async function listImportModels(): Promise<WikiImportModel[]> {
    const res = await httpClient.get<WikiImportModel[]>(`${PREFIX}/models`);
    return res.data;
}

/** 把原始 Markdown 切成草稿；纯解析，不落库、不调模型、不计费 */
export async function previewImport(
    source: string,
): Promise<WikiImportPreviewResponse> {
    const res = await httpClient.post<WikiImportPreviewResponse>(
        `${PREFIX}/preview`,
        { source },
    );
    return res.data;
}

/**
 * 上传文件 → 解析出的纯文本 + 来源类型（纯解析，不落库、不调模型、不计费）。
 *
 * 走裸 `axios.postForm` 而不是 `httpClient`：后者全局设了
 * `Content-Type: application/json`，会把 multipart 边界一起写死，
 * 服务端解析不出文件（对齐 `api/document.ts` 的上传约定）。
 */
export async function previewImportFile(
    file: File,
): Promise<WikiImportFileParseResponse> {
    const form = new FormData();
    form.append("file", file);

    const res = await axios.postForm<WikiImportFileParseResponse>(
        `${API_BASE_URL}${PREFIX}/preview-file`,
        form,
        {
            headers: {
                "X-Tenant-Id": DEFAULT_TENANT_ID,
                "X-User-Id": DEFAULT_USER_ID,
            },
        },
    );
    return res.data;
}

/** 执行导入：建知识条目 + 可选机制 1 分类 */
export async function executeImport(
    payload: WikiImportExecuteRequest,
): Promise<WikiImportTask> {
    const res = await httpClient.post<WikiImportTask>(
        `${PREFIX}/execute`,
        payload,
    );
    return res.data;
}

/** 导入作业台账（非 admin 只看到自己发起的任务） */
export async function listImportTasks(
    params: { limit?: number; offset?: number } = {},
): Promise<WikiImportTaskListResponse> {
    const res = await httpClient.get<WikiImportTaskListResponse>(
        `${PREFIX}/tasks`,
        { params: { limit: params.limit ?? 20, offset: params.offset ?? 0 } },
    );
    return res.data;
}

/**
 * 单 task 实时状态（Phase 4）。执行中的任务用它轮询；终态任务拉一次就够。
 *
 * 404 语义：不存在或非本人/非 admin（后端做了横向隔离）。调用方按字面
 * 404 处理即可，不要在这里去 `listImportTasks` 重查。
 */
export async function getImportTask(taskId: number): Promise<WikiImportTask> {
    const res = await httpClient.get<WikiImportTask>(`${PREFIX}/tasks/${taskId}`);
    return res.data;
}

/**
 * 重试一个失败/部分失败的任务（Phase 4）。
 *
 * 服务端从原 task 的 ``page_ids`` 反查 Page → 拼回 drafts → 走 execute。
 * 新 task 通过 ``retryOfTaskId`` 关联原 task；content_hash 命中的草稿
 * 计入 skipped（自动去重）。
 *
 * 前端不需要持有原 drafts —— 这正是 retry 走服务端的关键。
 */
export async function retryImportTask(taskId: number): Promise<WikiImportTask> {
    const res = await httpClient.post<WikiImportTask>(
        `${PREFIX}/tasks/${taskId}/retry`,
    );
    return res.data;
}
