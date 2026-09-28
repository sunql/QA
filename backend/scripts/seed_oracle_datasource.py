"""seed_oracle_datasource - 创建/校验 THBI Oracle 数据源（idempotent）。

通过走 POST/PUT /api/v1/datasources（数据源功能本身的入口）写入，避免直接 SQL
绕过 service 层的加密/审计/校验。这样写：
- 密码走 encryptApiKey（Fernet）服务端加密，与管理页路径一致
- 触发 service 层 audit_log 写入
- 复用 stub auth（X-User-Id: admin），与本地 dev 默认鉴权兼容

依赖：
- 本机 uvicorn 在 8000 端口跑（run_local.sh 启动，必要时 SKIP_SCHEMA_CHECK=1）
- backend/.env 含 ORACLE_ZJTH_PASSWORD（Fernet 加密前明文）
- 容器内 qa-backend 已停（避免端口冲突）
- curl 命令可用（macOS 自带）

幂等：按 name='THBI Oracle' 预查；存在则更新（PUT），不存在则创建（POST）。

注：本地 dev 经验证 httpx/requests 对 uvicorn 偶发 502（uvicorn 自身记 200，
   但客户端库拿到 transport-level 502；裸 socket/curl 正常），改用 subprocess
   调 curl 避开。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BACKEND_BASE_URL = os.environ.get("QA_BACKEND_URL", "http://localhost:8000")
DATASOURCE_NAME = "THBI Oracle"
DATASOURCE_PAYLOAD = {
    "name": DATASOURCE_NAME,
    "type": "oracle",
    "host": "192.168.205.70",
    "port": 1521,
    "databaseName": "X3V71ORA",   # Oracle service_name（_OracleAdapter 走 service_name）
    "username": "ZJTH",
    "description": "THBI 业务库 Oracle 数据源（schema=THBI，由 SQL 查询引用 THBI.*）",
    "isActive": True,
    "isDefault": False,
    # Oracle 19c 默认支持 FETCH FIRST；写死避免自动探测往返业务库
    "oracleVersion": "19c",
}


def _passwordFromEnv() -> str:
    pw = os.environ.get("ORACLE_ZJTH_PASSWORD")
    if not pw:
        sys.exit(
            "[seed_oracle_datasource] 缺少 ORACLE_ZJTH_PASSWORD 环境变量。"
            "请在 backend/.env 中设置（.gitignore 已屏蔽）。"
        )
    return pw


def _curl(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    """调 curl；返回 (status_code, json_body)。status_code 0 表示连接失败。"""
    cmd = [
        "curl", "-sS", "-o", "/tmp/seed_oracle_resp.json",
        "-w", "%{http_code}",
        "-X", method,
        "-H", "X-User-Id: admin",
        "-H", "X-User-Roles: admin",
        "-H", "X-Tenant-Id: default",
    ]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    status = int(result.stdout.strip() or 0)
    try:
        with open("/tmp/seed_oracle_resp.json") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        payload = {}
    return status, payload


def run() -> None:
    password = _passwordFromEnv()
    payload = {**DATASOURCE_PAYLOAD, "password": password}

    # 1) 幂等查重
    status, items = _curl("GET", f"{BACKEND_BASE_URL}/api/v1/datasources")
    if status != 200:
        sys.exit(f"[seed_oracle_datasource] GET datasources 失败 status={status} body={items}")
    existing = next((d for d in items if d.get("name") == DATASOURCE_NAME), None)

    # 2) POST 或 PUT
    if existing:
        ds_id = existing["id"]
        print(f"[seed_oracle_datasource] 已存在 id={ds_id}，更新密码/版本")
        status, ds = _curl("PUT", f"{BACKEND_BASE_URL}/api/v1/datasources/{ds_id}", payload)
    else:
        print("[seed_oracle_datasource] 不存在，创建")
        status, ds = _curl("POST", f"{BACKEND_BASE_URL}/api/v1/datasources", payload)

    if status not in (200, 201):
        sys.exit(f"[seed_oracle_datasource] 失败 status={status} body={ds}")

    print(
        f"[seed_oracle_datasource] 完成 id={ds['id']} name={ds['name']} "
        f"type={ds['type']} host={ds['host']}:{ds['port']} "
        f"service={ds['databaseName']} user={ds['username']} "
        f"oracle_version={ds.get('oracleVersion')}"
    )


if __name__ == "__main__":
    run()
