import { useState } from "react";
import { Button } from "antd";
import { useTranslation } from "../../i18n";
import { useAuthStore } from "../../stores/authStore";
import TermDictionaryFormModal from "./TermDictionaryFormModal";

interface TermDictionaryButtonProps {
  // 可选：预填术语（当前从对话中取不到精确选词时留空，由用户填写）
  initialTerm?: string;
}

/** 在对话中（尤其是失败/卡住的回答旁）提供「添加到术语词典」入口。 */
export default function TermDictionaryButton({ initialTerm }: TermDictionaryButtonProps) {
  const { t } = useTranslation();
  const user = useAuthStore((s) => s.user);
  const isAdmin = (user?.roles ?? []).includes("admin");
  const [open, setOpen] = useState(false);

  // 写入口限 admin（后端已 403 兜底）：非管理员不展示入口，避免点了才报错。
  if (!isAdmin) return null;

  return (
    <>
      <Button size="small" onClick={() => setOpen(true)}>
        {t("termDictionary.addButton")}
      </Button>
      <TermDictionaryFormModal
        open={open}
        initialTerm={initialTerm}
        onCancel={() => setOpen(false)}
      />
    </>
  );
}
