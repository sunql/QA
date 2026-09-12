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
