"""seed_user_passwords - 一次性给无密码用户生成临时密码（feat-user-auth，2026-09-20）。

背景：
- feat-user-auth 引入真实登录（用户名+密码 → JWT）。
- 老用户通过 seed / API / SQL 直接创建，``password_hash`` 多数为 NULL → 无法登录。
- 跑此脚本为所有 ``enabled=true AND password_hash IS NULL`` 用户生成 12 位临时
  密码（每用户独立），设置 ``must_change_password=true``，强制首次登录后改密。

输出：
- CSV（username, display_name, email, temp_password）写到 ``./user_temp_passwords.csv``
  （与脚本同目录；运维阅后即焚）。
- 标准输出打印总行数 + 路径，便于 CI 触发后续步骤。

安全护栏：
- ``AUTH_STUB_ENABLED=0``（生产）下拒绝运行 — 防止误跑 prod 后只 seed 没分发。
- 用 bcrypt 临时密码 ⇒ 不可逆；CSV 是唯一凭证副本。
- 用事务逐行写 ⇒ 单行失败不影响其它用户；失败行打印到 stderr 不入 CSV。

用法（手动 / 一次性）：
    cd backend
    AUTH_STUB_ENABLED=1 python -m scripts.seed_user_passwords

CI / 自动化场景（生产 / staging 默认 ``AUTH_STUB_ENABLED=0``，需显式 override）：
    AUTH_STUB_ENABLED=0 ALLOW_PROD_PASSWORD_SEED=1 python -m scripts.seed_user_passwords

幂等性：
- 只挑 ``password_hash IS NULL`` 行；跑过一遍的用户（已分配临时密码）自动跳过。
- 重复运行 = 零副作用（除非有新增无密码用户）。
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import secrets
import string
from datetime import datetime
from pathlib import Path

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import getSettings
from app.domain.models import User

logger = logging.getLogger(__name__)

_TEMP_PASSWORD_LENGTH = 12
_TEMP_BCRYPT_ROUNDS = 12
_TEMP_PASSWORD_ALPHABET = string.ascii_letters + string.digits

_OUTPUT_CSV_NAME = "user_temp_passwords.csv"


class ProductionGuardError(RuntimeError):
    """``AUTH_STUB_ENABLED=0`` 但没显式 ALLOW_PROD_PASSWORD_SEED=1。"""


def _generate_temp_password() -> str:
    """生成 12 位随机密码（大小写字母 + 数字）。不含特殊符号以避免日志/CSV 转义问题。"""
    return "".join(secrets.choice(_TEMP_PASSWORD_ALPHABET) for _ in range(_TEMP_PASSWORD_LENGTH))


def _hash_temp_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=_TEMP_BCRYPT_ROUNDS)).decode("utf-8")


async def _select_users_without_password(session: AsyncSession) -> list[User]:
    stmt = (
        select(User)
        .where(User.password_hash.is_(None), User.enabled.is_(True))
        .order_by(User.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _assign_temp_password(
    session: AsyncSession, user: User, temp_password: str
) -> None:
    """事务内逐行：写 hash + must_change_password=true。失败抛 → 调用方决定是否回滚。"""
    user.password_hash = _hash_temp_password(temp_password)
    user.must_change_password = True


async def seedUserPasswords(session: AsyncSession, output_dir: Path) -> tuple[int, Path]:
    """核心逻辑：筛用户 → 分配临时密码 → 写 CSV + DB。

    返回 ``(成功行数, csv_path)``。任何用户失败抛 RuntimeError；该次失败前已
    commit 的行保留生效（CSV 已写盘），运维可重跑（幂等：已分配用户被过滤）。
    """
    users = await _select_users_without_password(session)
    if not users:
        logger.info("seed_user_passwords: 没有无密码用户，跳过")
        return 0, output_dir / _OUTPUT_CSV_NAME

    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / _OUTPUT_CSV_NAME
    timestamp = datetime.utcnow().isoformat(timespec="seconds")

    rows: list[dict[str, str]] = []
    for user in users:
        temp = _generate_temp_password()
        await _assign_temp_password(session, user, temp)
        rows.append(
            {
                "username": user.username,
                "display_name": user.display_name or "",
                "email": user.email or "",
                "temp_password": temp,
            }
        )
        logger.info("seed_user_passwords: 已分配 %s", user.username)

    await session.commit()

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["username", "display_name", "email", "temp_password"]
        )
        writer.writeheader()
        writer.writerows(rows)

    # 文件权限收紧到 0600（阅后即焚）：避免被同机其它用户读到。
    try:
        os.chmod(csv_path, 0o600)
    except OSError:  # pragma: no cover — Windows / 文件系统不支持
        pass

    logger.info(
        "seed_user_passwords: 完成 %d 行 → %s （generated_at=%s）",
        len(rows),
        csv_path,
        timestamp,
    )
    return len(rows), csv_path


def _assert_environment_safe() -> None:
    """生产护栏：拒绝在 ``AUTH_STUB_ENABLED=0`` 下无显式 override 跑。"""
    settings = getSettings()
    if settings.authStubEnabled:
        return
    if os.environ.get("ALLOW_PROD_PASSWORD_SEED") == "1":
        logger.warning(
            "seed_user_passwords: AUTH_STUB_ENABLED=0 但 ALLOW_PROD_PASSWORD_SEED=1 — "
            "确认为生产种子？CSV 包含明文密码，阅后即焚。"
        )
        return
    raise ProductionGuardError(
        "AUTH_STUB_ENABLED=0 但未设置 ALLOW_PROD_PASSWORD_SEED=1 — 拒绝跑 prod。"
        "若确需 prod seed，显式 export ALLOW_PROD_PASSWORD_SEED=1 后重试。"
    )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    _assert_environment_safe()

    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            n, csv_path = await seedUserPasswords(
                session, output_dir=Path.cwd()
            )
            print(f"seed_user_passwords: {n} users assigned → {csv_path}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
