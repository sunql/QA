import { useState } from "react";
import { Button, Collapse, Space, message } from "antd";
import { CopyOutlined } from "@ant-design/icons";
import Editor from "@monaco-editor/react";
import { useTranslation } from "../../i18n";

interface SqlPreviewProps {
  sql: string;
}

/**
 * 只读 SQL 预览（Phase 8）：Monaco 高亮 + 自定义复制按钮。
 *
 * 暗色主题（vs-dark）匹配应用暗色模式；readOnly=true 确保仅展示，不可编辑。
 * 不引入"重新执行"入口——SQL 仍然仅由 NL2SQL 流水线生成，前端不可触发重跑。
 */
export default function SqlPreview({ sql }: SqlPreviewProps) {
  const [copied, setCopied] = useState(false);
  const { t } = useTranslation();

  const handleCopy = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(sql);
      } else {
        // 兼容旧浏览器/非安全上下文：使用临时 textarea 降级
        const textarea = document.createElement("textarea");
        textarea.value = sql;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      setCopied(true);
      void message.success(t("sqlPreview.copiedToast"));
      window.setTimeout(() => setCopied(false), 1500);
    } catch (err: unknown) {
      const text = err instanceof Error ? err.message : t("sqlPreview.unknownError");
      void message.error(t("sqlPreview.copyFailedToast", { message: text }));
    }
  };

  return (
    <Collapse
      size="small"
      items={[
        {
          key: "sql",
          label: t("sqlPreview.viewSql"),
          children: (
            <div style={{ position: "relative" }}>
              <Space style={{ position: "absolute", top: 8, right: 8, zIndex: 1 }}>
                <Button
                  size="small"
                  icon={<CopyOutlined />}
                  onClick={() => void handleCopy()}
                  data-testid="sql-copy-button"
                >
                  {copied ? t("sqlPreview.copied") : t("sqlPreview.copy")}
                </Button>
              </Space>
              <Editor
                height="180px"
                defaultLanguage="sql"
                theme="vs-dark"
                value={sql}
                options={{
                  readOnly: true,
                  minimap: { enabled: false },
                  fontSize: 12,
                  wordWrap: "on",
                  scrollBeyondLastLine: false,
                  renderLineHighlight: "none",
                  lineNumbers: "off",
                  folding: false,
                }}
              />
            </div>
          ),
        },
      ]}
    />
  );
}