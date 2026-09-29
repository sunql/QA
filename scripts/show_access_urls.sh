#!/usr/bin/env bash
# scripts/show_access_urls.sh - 枚举 qa-system 全部访问入口
#
# 背景（部署与访问规范 §四）：发布后操作者常「找不到对应地址」。
# 前端已走同源相对路径（127.0.0.1 / localhost / 局域网 IP / 域名都自动可用），
# 本脚本只负责把这些入口**打印**出来——不写任何配置（禁止枚举 IP 回写
# 配置，见规范 §二：IP 会变、多网卡多 IP，枚举永远覆盖不全）。
#
# 用法：./scripts/show_access_urls.sh [端口]   （默认 5173，frontend 容器映射口）

set -euo pipefail

PORT="${1:-5173}"

ips="127.0.0.1 localhost"
if hostname -I >/dev/null 2>&1; then
  # Linux / WSL2：hostname -I 列出全部 IPv4
  ips="$ips $(hostname -I)"
elif command -v ipconfig >/dev/null 2>&1; then
  # macOS：ipconfig getifaddr 逐网卡取
  for iface in en0 en1; do
    addr="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
    [ -n "$addr" ] && ips="$ips $addr"
  done
fi

echo "qa-system 访问入口（前端 :${PORT}）："
for ip in $ips; do
  echo "  http://$ip:$PORT"
done
