import { Result } from "antd";
import { useTranslation } from "../../i18n";

// 导入完成：展示成功结果。
export default function ConfirmStep() {
  const { t } = useTranslation();
  return <Result status="success" title={t("localImport.steps.confirm")} />;
}
