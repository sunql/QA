import { Result, Table } from "antd";
import type { TableColumnsType } from "antd";
import { useTranslation } from "../../i18n";
import type { ImportExecuteResponse } from "../../types/localImport";

interface ConfirmStepProps {
  result: ImportExecuteResponse | null;
}

interface ErrorRow {
  type: string;
  name: string;
  message: string;
}

// 导入完成：按结果展示成功或错误明细（错误逐项列出 type/name/message）。
export default function ConfirmStep({ result }: ConfirmStepProps) {
  const { t } = useTranslation();

  if (!result) {
    return null;
  }

  if (result.success) {
    return <Result status="success" title={t("localImport.steps.confirm")} />;
  }

  const errors: ErrorRow[] = result.errors;
  const columns: TableColumnsType<ErrorRow> = [
    { title: t("localImport.confirm.errorType"), dataIndex: "type" },
    { title: t("localImport.confirm.errorName"), dataIndex: "name" },
    { title: t("localImport.confirm.errorMessage"), dataIndex: "message" },
  ];

  return (
    <Result
      status="error"
      title={t("localImport.confirm.errorTitle")}
      subTitle={t("localImport.confirm.errorSummary", {
        createdClasses: result.createdClasses,
        createdProperties: result.createdProperties,
        createdJoins: result.createdJoins,
        errorCount: errors.length,
      })}
    >
      <Table<ErrorRow>
        rowKey={(row) => `${row.type}:${row.name}`}
        dataSource={errors}
        columns={columns}
        pagination={false}
        size="small"
      />
    </Result>
  );
}
