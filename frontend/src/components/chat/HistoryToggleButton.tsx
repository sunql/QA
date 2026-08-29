import { Button, Tooltip } from "antd";
import { MenuUnfoldOutlined } from "@ant-design/icons";
import { useTranslation } from "../../i18n";

interface HistoryToggleButtonProps {
  onClick: () => void;
}

// 折叠态浮起按钮：靠右贴边的 24px 宽小竖条，背景蓝色，点击展开右侧历史面板
export default function HistoryToggleButton({ onClick }: HistoryToggleButtonProps) {
  const { t } = useTranslation();
  return (
    <Tooltip title={t("chat.history.expand")} placement="left">
      <Button
        type="primary"
        shape="circle"
        size="small"
        icon={<MenuUnfoldOutlined />}
        onClick={onClick}
        aria-label={t("chat.history.expand")}
        style={{
          position: "absolute",
          right: 0,
          top: "50%",
          transform: "translateY(-50%)",
          zIndex: 10,
          boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
        }}
      />
    </Tooltip>
  );
}