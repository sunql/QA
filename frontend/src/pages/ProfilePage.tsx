/** ProfilePage — 个人中心（2026-09-16）
 *
 * 展示当前调用方身份（GET /users/me）：
 * - DB 命中：显示名称/邮箱/角色/组织（getCurrentUser Phase D 以 DB 为准）
 * - 桩回退：明确提示当前是桩认证、未命中数据库用户
 *
 * 背景：menu_config 里的 /profile 菜单项此前无对应路由，点击白屏
 * （控制台 "No routes matched location"）。/change-password 因无登录体系
 * 已在 DB 中隐藏（visible=false）。
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert, Card, Descriptions, Spin, Tag, message } from "antd";
import { fetchCurrentUserMe } from "../api/userProfile";
import type { CurrentUserMe } from "../api/userProfile";

export function ProfilePage(): JSX.Element {
  const { t } = useTranslation();
  const [me, setMe] = useState<CurrentUserMe | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await fetchCurrentUserMe();
        if (!cancelled) setMe(data);
      } catch (e: unknown) {
        if (!cancelled) {
          message.error(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div style={{ padding: 24, maxWidth: 800 }}>
      <h2>{t("profile.title")}</h2>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t("profile.authModeStubNotice")}
      />
      <Card>
        {loading ? (
          <Spin />
        ) : me ? (
          <Descriptions column={1} bordered size="small">
            <Descriptions.Item label={t("profile.fields.userId")}>
              {me.userId}
            </Descriptions.Item>
            <Descriptions.Item label={t("profile.fields.displayName")}>
              {me.displayName}
            </Descriptions.Item>
            <Descriptions.Item label={t("profile.fields.email")}>
              {me.email ?? "-"}
            </Descriptions.Item>
            <Descriptions.Item label={t("profile.fields.roles")}>
              {me.roleCodes.length > 0
                ? me.roleCodes.map((r) => <Tag key={r}>{r}</Tag>)
                : "-"}
            </Descriptions.Item>
            <Descriptions.Item label={t("profile.fields.departments")}>
              {me.departmentCodes.length > 0
                ? me.departmentCodes.map((d) => <Tag key={d}>{d}</Tag>)
                : "-"}
            </Descriptions.Item>
            <Descriptions.Item label={t("profile.fields.identitySource")}>
              <Tag color={me.dbUserId != null ? "green" : "orange"}>
                {me.dbUserId != null
                  ? t("profile.identityDb")
                  : t("profile.identityStub")}
              </Tag>
            </Descriptions.Item>
          </Descriptions>
        ) : (
          "-"
        )}
      </Card>
    </div>
  );
}
