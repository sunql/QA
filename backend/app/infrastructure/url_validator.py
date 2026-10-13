"""端点 URL 校验：生产环境阻断 http + 私有 IP/loopback。"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse


def validateEndpointUrl(url: str) -> None:
    """通过则 None；不通过抛 ValueError。"""
    parsed = urlparse(url)
    env = os.getenv("APP_ENV", "development")
    if env != "production":
        return
    if parsed.scheme != "https":
        raise ValueError("生产环境 endpoint 必须为 https")
    host = parsed.hostname or ""
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"生产环境 endpoint 禁止指向私有地址 {host}")
    except ValueError as exc:
        if "私有地址" in str(exc):
            raise
        # host 不是 IP，DNS 解析后再判（这里简化：trust 域名）
