import { useState } from "react";
import { Form, Input, Modal, message } from "antd";
import { createTerm } from "../../api/termDictionary";
import type { TermDictionaryCreate } from "../../types/termDictionary";
import { useTranslation } from "../../i18n";

interface TermDictionaryFormModalProps {
  open: boolean;
  // 可选：预填术语（如从失败回答旁的入口进来时带上的词）
  initialTerm?: string;
  onCancel: () => void;
  // 创建成功后的回调（管理抽屉用它刷新列表）
  onCreated?: () => void;
}

/** 术语新增表单（Modal）。由对话气泡入口与术语词典抽屉共用。 */
export default function TermDictionaryFormModal({
  open,
  initialTerm,
  onCancel,
  onCreated,
}: TermDictionaryFormModalProps) {
  const { t } = useTranslation();
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm<TermDictionaryCreate>();

  const handleSubmit = async () => {
    // 校验失败时 antd 已内联显示红字；这里吞掉 rejection，避免未处理的 Promise。
    const values = await form.validateFields().catch(() => null);
    if (!values) return;

    setSubmitting(true);
    try {
      await createTerm(values);
      void message.success(t("termDictionary.created"));
      form.resetFields();
      onCreated?.();
      onCancel();
    } catch {
      // 错误已由 client 拦截器 toast 展示，这里静默即可
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title={t("termDictionary.createTitle")}
      open={open}
      onOk={handleSubmit}
      confirmLoading={submitting}
      onCancel={onCancel}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" initialValues={{ term: initialTerm }}>
        <Form.Item
          name="term"
          label={t("termDictionary.labels.term")}
          rules={[{ required: true, message: t("forms.required") }]}
        >
          <Input placeholder={t("termDictionary.placeholders.term")} />
        </Form.Item>
        <Form.Item
          name="definition"
          label={t("termDictionary.labels.definition")}
          rules={[{ required: true, message: t("forms.required") }]}
        >
          <Input.TextArea placeholder={t("termDictionary.placeholders.definition")} rows={2} />
        </Form.Item>
        <Form.Item name="mappedClassName" label={t("termDictionary.labels.mappedClassName")}>
          <Input placeholder={t("termDictionary.placeholders.mappedClassName")} />
        </Form.Item>
        <Form.Item name="mappedPropertyName" label={t("termDictionary.labels.mappedPropertyName")}>
          <Input placeholder={t("termDictionary.placeholders.mappedPropertyName")} />
        </Form.Item>
        <Form.Item name="formulaHint" label={t("termDictionary.labels.formulaHint")}>
          <Input.TextArea placeholder={t("termDictionary.placeholders.formulaHint")} rows={2} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
