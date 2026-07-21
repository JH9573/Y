"""配置加载与校验。

从项目根目录的 .env 文件读取环境变量,校验必填项,缺失则启动失败。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent
BACKUPS_DIR = ROOT_DIR / "backups"


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_ids: frozenset[int]
    cred_encryption_key: str
    db_path: str
    ssh_timeout: int
    log_level: str
    # 阿里云 OSS(安装包分发用,可选;不配则该功能提示未启用)
    oss_region: str | None
    oss_endpoint: str | None
    oss_bucket: str | None
    oss_access_key_id: str | None
    oss_access_key_secret: str | None
    oss_public_base_url: str | None
    oss_prefix: str

    @property
    def db_url(self) -> str:
        path = self.db_path
        if not os.path.isabs(path):
            path = str(ROOT_DIR / path)
        return f"sqlite+aiosqlite:///{path}"

    @property
    def oss_configured(self) -> bool:
        return all((
            self.oss_region,
            self.oss_bucket,
            self.oss_access_key_id,
            self.oss_access_key_secret,
        ))


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"必填配置项缺失: {name}")
    return value


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _parse_user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ids.add(int(piece))
        except ValueError as exc:
            raise RuntimeError(f"ALLOWED_USER_IDS 含非整数项: {piece!r}") from exc
    if not ids:
        raise RuntimeError("ALLOWED_USER_IDS 至少需要一个用户 id")
    return frozenset(ids)


def load_config() -> Config:
    load_dotenv(ROOT_DIR / ".env")
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    return Config(
        bot_token=_required("BOT_TOKEN"),
        allowed_user_ids=_parse_user_ids(_required("ALLOWED_USER_IDS")),
        cred_encryption_key=_required("CRED_ENCRYPTION_KEY"),
        db_path=os.getenv("DB_PATH", "./sbot.db").strip() or "./sbot.db",
        ssh_timeout=int(os.getenv("SSH_TIMEOUT", "15")),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        oss_region=_optional("OSS_REGION"),
        oss_endpoint=_optional("OSS_ENDPOINT"),
        oss_bucket=_optional("OSS_BUCKET"),
        oss_access_key_id=_optional("OSS_ACCESS_KEY_ID"),
        oss_access_key_secret=_optional("OSS_ACCESS_KEY_SECRET"),
        oss_public_base_url=(_optional("OSS_PUBLIC_BASE_URL") or "").rstrip("/") or None,
        oss_prefix=(_optional("OSS_PREFIX") or "releases").strip("/") or "releases",
    )
