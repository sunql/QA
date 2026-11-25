/** Copilot 场景卡片墙（P4）：卡片来自 copilot_scenario 配置，不再硬编码。
 *
 * 加场景 = 插一行配置，前端零改动。自由对话卡不是配置场景
 * （它走通用 Agent 目录），固定追加在最后。
 */

import { Button, Card, Col, Row, Typography } from "antd";
import {
  ArrowRightOutlined,
  CarOutlined,
  CommentOutlined,
  RocketOutlined,
  SearchOutlined,
  SettingOutlined,
  DatabaseOutlined,
  TeamOutlined,
  RobotOutlined,
} from "@ant-design/icons";
import { useTranslation } from "../../i18n";
import type { CopilotScenario } from "../../api/copilotScenario";

const { Text } = Typography;

/** icon_code → 图标（配置未知码兜底 RobotOutlined）。 */
type IconComponent = React.ComponentType<{ style?: React.CSSProperties }>;
const ICON_BY_CODE: Record<string, IconComponent> = {
  car: CarOutlined,
  comment: CommentOutlined,
  rocket: RocketOutlined,
  search: SearchOutlined,
  setting: SettingOutlined,
  database: DatabaseOutlined,
  team: TeamOutlined,
};

const GRADIENTS = [
  "linear-gradient(135deg, #e6f4ff 0%, #f0f7ff 100%)",
  "linear-gradient(135deg, #f0f5ff 0%, #f9f0ff 100%)",
  "linear-gradient(135deg, #f6ffed 0%, #fcffe6 100%)",
  "linear-gradient(135deg, #fff7e6 0%, #fffbe6 100%)",
];

const ICON_COLORS = ["#1677ff", "#2f54eb", "#389e0d", "#d46b08"];

interface CopilotScenarioCardsProps {
  scenarios: CopilotScenario[];
  onEnterScenario: (scenario: CopilotScenario) => void;
  onEnterFree: () => void;
}

export default function CopilotScenarioCards({
  scenarios,
  onEnterScenario,
  onEnterFree,
}: CopilotScenarioCardsProps) {
  const { t } = useTranslation();

  const cardBody = {
    padding: 16,
    display: "flex",
    flexDirection: "column",
    gap: 10,
  } as const;

  return (
    <Row gutter={[16, 16]}>
      {scenarios.map((scenario, index) => {
        const Icon = ICON_BY_CODE[scenario.iconCode] ?? RobotOutlined;
        const gradient = GRADIENTS[index % GRADIENTS.length];
        const iconColor = ICON_COLORS[index % ICON_COLORS.length];
        return (
          <Col xs={24} md={12} lg={8} key={scenario.scenarioCode}>
            <Card
              hoverable
              style={{ height: "100%", borderRadius: 12 }}
              styles={{ body: cardBody }}
              onClick={() => onEnterScenario(scenario)}
            >
              <div
                style={{
                  height: 110,
                  borderRadius: 8,
                  background: gradient,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <Icon style={{ fontSize: 48, color: iconColor }} />
              </div>
              <Text strong style={{ fontSize: 16 }}>{scenario.name}</Text>
              <Text type="secondary" style={{ minHeight: 44 }}>{scenario.description}</Text>
              <Button type="primary" block icon={<ArrowRightOutlined />} iconPosition="end">
                {t("procurement.copilot.enterScenario")}
              </Button>
            </Card>
          </Col>
        );
      })}
      <Col xs={24} md={12} lg={8}>
        <Card
          hoverable
          style={{ height: "100%", borderRadius: 12 }}
          styles={{ body: cardBody }}
          onClick={onEnterFree}
        >
          <div
            style={{
              height: 110,
              borderRadius: 8,
              background: "linear-gradient(135deg, #f0f5ff 0%, #f9f0ff 100%)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <CommentOutlined style={{ fontSize: 48, color: "#2f54eb" }} />
          </div>
          <Text strong style={{ fontSize: 16 }}>
            {t("procurement.copilot.scenarioFree")}
          </Text>
          <Text type="secondary" style={{ minHeight: 44 }}>
            {t("procurement.copilot.scenarioFreeDesc")}
          </Text>
          <Button type="primary" block icon={<ArrowRightOutlined />} iconPosition="end">
            {t("procurement.copilot.enterScenario")}
          </Button>
        </Card>
      </Col>
    </Row>
  );
}
