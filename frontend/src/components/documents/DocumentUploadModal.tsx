/** 文档上传（**批量**）：多选文件 + 共用元数据 + 串行上传 + 逐文件结果。
 *
 * 为什么串行：每个文件都要落 MinIO、解析、分块、向量化；并发会把后端/模型打满。
 * 失败不中断：逐条记录，可在弹窗里**只重试失败项**。
 *
 * 与后端契约：`POST /documents/upload`（单文件），本组件只是队列化调用。
 */

import { useState } from "react";
import {
  Alert,
  Form,
  Input,
  List,
  Modal,
  Progress,
  Select,
  Space,
  Tag,
  Upload,
  message,
} from "antd";
import { UploadOutlined } from "@ant-design/icons";
import { DatePicker } from "antd";
import type { UploadFile } from "antd/es/upload/interface";
import { uploadDocument } from "../../api/document";
import {
  DOCUMENT_SECURITY_OPTIONS,
  DOCUMENT_TYPE_OPTIONS,
} from "../../types/document";
import { DOCUMENT_UPLOAD_ACCEPT, DOCUMENT_UPLOAD_HINT } from "../../utils/uploadAccept";
import { toIsoDateParam } from "../../utils/dateParam";

interface FileOutcome {
  uid: string;
  name: string;
  status: "ok" | "fail";
  detail: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  onUploaded: () => void;
}

export default function DocumentUploadModal({ open, onClose, onUploaded }: Props) {
  const [form] = Form.useForm();
  const [files, setFiles] = useState<UploadFile[]>([]);
  const [outcomes, setOutcomes] = useState<FileOutcome[]>([]);
  const [uploading, setUploading] = useState(false);
  const [done, setDone] = useState(0);

  const reset = () => {
    setFiles([]);
    setOutcomes([]);
    setDone(0);
    form.resetFields();
  };

  const close = () => {
    if (uploading) return;
    reset();
    onClose();
  };

  const runQueue = async (queue: UploadFile[]) => {
    const values = await form.validateFields();
    const opts = {
      documentType: values.documentType,
      version: values.version,
      owner: values.owner,
      securityLevel: values.securityLevel,
      effectiveDate: toIsoDateParam(values.effectiveDate),
    };
    const collected: FileOutcome[] = [];
    setUploading(true);
    setDone(0);
    setOutcomes([]);
    try {
      for (const item of queue) {
        const file = item.originFileObj as File | undefined;
        if (!file) continue;
        try {
          const result = await uploadDocument(file, opts);
          collected.push({
            uid: item.uid,
            name: item.name,
            status: "ok",
            detail: `${result.documentId} · ${result.chunks} chunks`,
          });
        } catch (error: unknown) {
          collected.push({
            uid: item.uid,
            name: item.name,
            status: "fail",
            detail: errorText(error),
          });
        }
        setDone((prev) => prev + 1);
        setOutcomes([...collected]);
      }
    } finally {
      setUploading(false);
    }
    const failed = collected.filter((item) => item.status === "fail").length;
    if (failed === 0) {
      message.success(`批量上传完成：${collected.length} 个文件`);
      reset();
      onUploaded();
    } else {
      message.warning(`完成：成功 ${collected.length - failed} / 失败 ${failed}（可只重试失败项）`);
    }
  };

  const submit = async () => {
    const queue = files.filter((item) => item.originFileObj);
    if (queue.length === 0) {
      message.warning("请选择要上传的文件");
      return;
    }
    await runQueue(queue);
  };

  const retryFailed = async () => {
    const failedUids = new Set(
      outcomes.filter((item) => item.status === "fail").map((item) => item.uid),
    );
    const queue = files.filter((item) => failedUids.has(item.uid) && item.originFileObj);
    if (queue.length === 0) return;
    await runQueue(queue);
  };

  const hasFailures = outcomes.some((item) => item.status === "fail");

  return (
    <Modal
      title="上传文档（支持批量，自动 RAG 向量化）"
      open={open}
      onCancel={close}
      okText={hasFailures ? "重试失败项" : "开始上传"}
      onOk={() => void (hasFailures ? retryFailed() : submit())}
      confirmLoading={uploading}
      cancelText="关闭"
      width={720}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" disabled={uploading}>
        <Form.Item label="选择文件（可多选 / 拖拽）">
          <Upload.Dragger
            multiple
            accept={DOCUMENT_UPLOAD_ACCEPT}
            fileList={files}
            beforeUpload={(file) => {
              // rc-upload 给的是 RcFile（File 子类）；统一包成 UploadFile 并保留 originFileObj
              setFiles((prev) => [
                ...prev,
                {
                  uid: file.uid,
                  name: file.name,
                  status: "done",
                  originFileObj: file as File,
                } as UploadFile,
              ]);
              return false; // 阻止自动上传，由队列统一处理
            }}
            onRemove={(file) => setFiles((prev) => prev.filter((item) => item.uid !== file.uid))}
          >
            <p>
              <UploadOutlined />
            </p>
            <p>点击或拖拽上传 {DOCUMENT_UPLOAD_HINT} 文件（可多选）</p>
          </Upload.Dragger>
        </Form.Item>
        <Space.Compact style={{ width: "100%" }}>
          <Form.Item name="documentType" label="文档类型" style={{ width: "50%" }}>
            <Select options={DOCUMENT_TYPE_OPTIONS} />
          </Form.Item>
          <Form.Item name="securityLevel" label="安全级别" style={{ width: "50%" }}>
            <Select options={DOCUMENT_SECURITY_OPTIONS} />
          </Form.Item>
        </Space.Compact>
        <Space.Compact style={{ width: "100%" }}>
          <Form.Item name="version" label="版本（共用）" style={{ width: "50%" }}>
            <Input placeholder="v1.0" />
          </Form.Item>
          <Form.Item name="owner" label="Owner（共用）" style={{ width: "50%" }}>
            <Input placeholder="负责人" />
          </Form.Item>
        </Space.Compact>
        <Form.Item name="effectiveDate" label="生效日期（共用，可选）">
          <DatePicker style={{ width: "100%" }} format="YYYY-MM-DD" />
        </Form.Item>
      </Form>

      {uploading && (
        <Progress percent={Math.round((done / Math.max(files.length, 1)) * 100)} size="small" />
      )}
      {outcomes.length > 0 && (
        <List
          size="small"
          bordered
          dataSource={outcomes}
          style={{ marginTop: 8 }}
          renderItem={(item) => (
            <List.Item>
              <Space>
                <Tag color={item.status === "ok" ? "success" : "error"}>
                  {item.status === "ok" ? "成功" : "失败"}
                </Tag>
                <span>{item.name}</span>
                <span style={{ color: "#888" }}>{item.detail}</span>
              </Space>
            </List.Item>
          )}
        />
      )}

      <Alert
        type="info"
        showIcon
        style={{ marginTop: 8 }}
        message="逐份串行上传；成功项自动解析、分块并写入向量库。重复内容会跳过（不会重复入库）。"
      />
    </Modal>
  );
}

function errorText(error: unknown): string {
  const response = (error as { response?: { data?: { error?: string; detail?: string } } })
    ?.response;
  return response?.data?.error ?? response?.data?.detail ?? "上传失败";
}
