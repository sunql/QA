import { Select } from "antd";
import { i18n } from "../../i18n";

const LANGS = [
  { value: "zh-CN", label: "中文" },
  { value: "en-US", label: "EN" },
];

export default function LanguageSwitch() {
  return (
    <Select
      value={i18n.language}
      options={LANGS}
      onChange={(value) => i18n.changeLanguage(value)}
      style={{ width: 80 }}
      variant="borderless"
    />
  );
}
