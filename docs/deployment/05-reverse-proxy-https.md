# 5. 反向代理与 HTTPS

生产部署**不建议**直接把 5173（前端）和 8000（后端）暴露到公网。常规做法是用一台独立的反向代理（Nginx / Caddy）统一 80/443，前端静态资源 + `/api/*` 一并接入。

---

## 5.1 方案对比

| 方案 | 优点 | 缺点 | 适用 |
|---|---|---|---|
| **Caddy 2** | 自动申请 Let's Encrypt / 自动续期、配置最少 | 国内网络拿证书偶尔超时 | 海外 / 公开域名 |
| **Nginx + certbot** | 资料最多、可控性最强 | 配置复杂、需手动续期 | 大流量 / 内网有 ACME 代理 |
| **Nginx + 自有证书** | 完全可控 | 需维护证书 | 内网 / 私有 CA |

**推荐 Caddy**：3 行配置就完事。

---

## 5.2 Caddy 配置

### 5.2.1 安装

```bash
# Ubuntu / Debian
sudo apt install -y caddy

# 或官方 repo
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy.list
sudo apt update && sudo apt install caddy
```

### 5.2.2 Caddyfile

```caddyfile
# /etc/caddy/Caddyfile
qa.example.com {
    encode zstd gzip

    # API 路由 → 后端
    reverse_proxy /api/* backend:8000

    # 默认 → 前端
    reverse_proxy frontend:5173

    log {
        output file /var/log/caddy/qa.log {
            roll_size 100mb
            roll_keep 10
        }
    }
}
```

> 关键：容器间用 docker 网络的 service 名（`backend` / `frontend`），不需要 `localhost`。

### 5.2.3 启动

```bash
# 验证
sudo caddy validate --config /etc/caddy/Caddyfile

# 重载
sudo systemctl reload caddy

# 查看证书
sudo caddy cert list
```

### 5.2.4 国内发布注意事项

Let's Encrypt 在国内偶发超时。可走 DNS-01 challenge：

```caddyfile
qa.example.com {
    tls {
        dns cloudflare {env.CF_API_TOKEN}
    }
    ...
}
```

或直接用国内 CA（阿里云 / 腾讯云）买的证书挂到 Caddy：

```bash
# 上传证书到 /etc/caddy/certs/
sudo mkdir -p /etc/caddy/certs

# Caddyfile
qa.example.com {
    tls /etc/caddy/certs/qa.example.com.crt /etc/caddy/certs/qa.example.com.key
    ...
}
```

然后用 `certbot` 写 cron 续期。

---

## 5.3 Nginx 配置

### 5.3.1 安装

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

### 5.3.2 /etc/nginx/sites-available/qa.example.com

```nginx
server {
    listen 80;
    server_name qa.example.com;

    # 强制 HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name qa.example.com;

    ssl_certificate     /etc/letsencrypt/live/qa.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/qa.example.com/privkey.pem;

    # 上传 / 长 query 调大
    client_max_body_size 20m;

    # 安全 header
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "no-referrer-when-downgrade" always;

    # 静态资源缓存
    location /assets/ {
        proxy_pass http://frontend:5173;
        proxy_set_header Host $host;
        proxy_cache_valid 200 1d;
        expires 1d;
    }

    # API 反代
    location /api/ {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Connection "";

        # 流式接口（SSE / NL2SQL stream）
        proxy_buffering off;
        proxy_read_timeout 300s;
    }

    # 前端 SPA
    location / {
        proxy_pass http://frontend:5173;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
```

启用：

```bash
sudo ln -s /etc/nginx/sites-available/qa.example.com /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# 申请证书
sudo certbot --nginx -d qa.example.com
```

自动续期（certbot 自带 hook）：

```bash
sudo certbot renew --dry-run
```

---

## 5.4 域名 / DNS

### A 记录

```
qa.example.com  →  1.2.3.4
```

### 多环境子域

| 子域 | 用途 |
|---|---|
| `qa.example.com` | 生产 |
| `staging.qa.example.com` | 预发 |
| `dev.qa.example.com` | 内部 PoC |

每个环境独立 Caddyfile / Nginx vhost。

---

## 5.5 CORS

后端 `CORS_ORIGINS` 环境变量决定允许的前端域名：

```bash
# 单域名
CORS_ORIGINS=https://qa.example.com

# 多域名（逗号分隔）
CORS_ORIGINS=https://qa.example.com,https://staging.qa.example.com
```

默认 `*` 仅用于开发，生产必须显式列名单。

---

## 5.6 限流 / 防滥用

### 5.6.1 Caddy

```caddyfile
qa.example.com {
    # 限流（需要 caddy-rate-limit 插件）
    rate_limit {
        zone qa_zone {
            key    {remote_host}
            events 100
            window 1m
        }
    }
    ...
}
```

### 5.6.2 Nginx

```nginx
# 需要 nginx-plus 或编译 http_limit_req
limit_req_zone $binary_remote_addr zone=api:10m rate=20r/s;

location /api/ {
    limit_req zone=api burst=50 nodelay;
    limit_req_status 429;
    ...
}
```

### 5.6.3 后端

FastAPI 用 `slowapi`：

```python
# 已在依赖中
from slowapi import Limiter
limiter = Limiter(key_func=get_remote_address)
```

⚠️ **坑位**：跑 pytest 必须用 `uv run pytest`，系统 Homebrew Python 缺 `slowapi` 会误报 `ModuleNotFoundError`，让测试看起来完全失败。

---

## 5.7 WebSocket / SSE 长连接

NL2SQL 流式接口用了 SSE。反代必须关 buffer：

```nginx
location /api/v1/chat/stream {
    proxy_pass http://backend:8000;
    proxy_buffering off;
    proxy_cache off;
    proxy_set_header Connection '';
    proxy_http_version 1.1;
    chunked_transfer_encoding on;
}
```

Caddy 默认 `flush_interval -1`（立即 flush），无需额外配置。

---

## 5.8 防火墙配合

```bash
# ufw
sudo ufw default deny incoming
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable

# 验证
sudo ufw status verbose
```

云厂商安全组（AWS / 阿里云 / 腾讯云）：入方向只开 22/80/443，源 IP 限制。

---

## 5.9 验收清单

```bash
# 1. HTTPS 可访问
curl -I https://qa.example.com
# 期望：HTTP/2 200，HSTS 头

# 2. API 路径
curl -I https://qa.example.com/api/v1/models
# 期望：HTTP/2 200

# 3. 安全 header
curl -I https://qa.example.com | grep -E 'Strict-Transport-Security|X-Frame'

# 4. SSL Labs 评分
# https://www.ssllabs.com/ssltest/analyze.html?d=qa.example.com
# 期望：A 或 A+

# 5. 限流生效（200 连发）
ab -n 200 -c 50 https://qa.example.com/api/v1/models
# 期望：< 5% 429
```

下一步：[06-distributed.md](06-distributed.md)。