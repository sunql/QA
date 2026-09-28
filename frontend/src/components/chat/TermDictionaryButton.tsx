import { useState } from "react";
import { Button, Form, Input, Modal, message } from "antd";
import { createTerm } from "../../api/termDictionary";
import type { TermDictionaryCreate } from "../../types/termDictionary";
import { useTranslation } from "../../i18n";

interface TermDictionaryButtonProps {
  // 可选：预填术语（当前从对话中取不到精确选词时留空，由用户填写）
  initialTerm?: string;
}

/** 在对话中（尤其是失败/卡住的回答旁）提供「添加到术语词典」入口。 */
export default function TermDictionaryButton({ initialTerm }: TermDictionaryButtonProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm<TermDictionaryCreate>();

  const handleSubmit = async () => {
    const values = await form.validateFields();
    setSubmitting(true);
    try {
      await createTerm(values);
      void message.success(t("termDictionary.created"));
      form.resetFields();
      setOpen(false);
    } catch {
      // 错误已由 client 拦截器 toast 展示，这里静默即可
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <Button size="small" onClick={() => setOpen(true)}>
        {t("termDictionary.addButton")}
      </Button>
      <Modal
        title={t("termDictionary.createTitle")}
        open={open}
        onOk={handleSubmit}
        confirmLoading={submitting}
        onCancel={() => setOpen(false)}
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
    </>
  );
}
