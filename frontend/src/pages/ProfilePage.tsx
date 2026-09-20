// ProfilePage（feat-user-auth，2026-09-20）
//
// 只读个人信息：GET /auth/me。结果写 authStore.me 供 UserMenu 等复用。
// 渲染 Descriptions 表格。

import { useEffect, useState } from "react";
import { Card, Descriptions, Empty, Spin } from "antd";
import { authApi } from "../api/auth";
import type { AuthMeRead } from "../types/auth";
import { useAuthStore } from "../stores/authStore";
import { useTranslation } from "../i18n";

export function ProfilePage() {
  const { t } = useTranslation();
  const setMe = useAuthStore((s) => s.setMe);
  const [data, setData] = useState<AuthMeRead | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    authApi
      .me()
      .then((m) => {
        if (!cancelled) {
          setData(m);
          setMe(m);
        }
      })
      .catch(() => {
        // httpClient 拦截器已处理
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [setMe]);

  return (
    <Card title={t("auth.profile.title")}>
      {loading ? (
        <Spin />
      ) : data ? (
        <Descriptions column={1} bordered size="middle">
          <Descriptions.Item label={t("auth.profile.username")}>
            {data.username}
          </Descriptions.Item>
          <Descriptions.Item label={t("auth.profile.displayName")}>
            {data.displayName || t("auth.profile.none")}
          </Descriptions.Item>
          <Descriptions.Item label={t("auth.profile.email")}>
            {data.email || t("auth.profile.none")}
          </Descriptions.Item>
          <Descriptions.Item label={t("auth.profile.enabled")}>
            {data.enabled ? t("common.enabled") : t("common.disabled")}
          </Descriptions.Item>
          <Descriptions.Item label={t("auth.profile.mustChangePassword")}>
            {data.mustChangePassword ? t("common.enabled") : t("common.disabled")}
          </Descriptions.Item>
          <Descriptions.Item label={t("auth.profile.tenantId")}>
            {data.tenantId}
          </Descriptions.Item>
          <Descriptions.Item label={t("auth.profile.roles")}>
            {data.roles.length > 0 ? data.roles.join(", ") : t("auth.profile.none")}
          </Descriptions.Item>
          <Descriptions.Item label={t("auth.profile.organizations")}>
            {data.organizations.length > 0
              ? data.organizations.join(", ")
              : t("auth.profile.none")}
          </Descriptions.Item>
          <Descriptions.Item label={t("auth.profile.lastLoginAt")}>
            {data.lastLoginAt ?? t("auth.profile.none")}
          </Descriptions.Item>
        </Descriptions>
      ) : (
        <Empty />
      )}
    </Card>
  );
}
