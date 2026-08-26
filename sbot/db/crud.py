"""数据访问层。

封装会话工厂与常用的 CRUD 操作。所有写操作都通过 AsyncSession 在调用方控制事务。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import delete, event, func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..core.redact import redact
from ..core.timeutil import utcnow
from .models import (
    Base,
    CosConfig,
    DnsAccount,
    Node,
    OperationLog,
    OssConfig,
    Panel,
    PanelNode,
    ReleaseSource,
    RemoteConfigFile,
    Server,
)

log = logging.getLogger(__name__)

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _apply_sqlite_pragmas(dbapi_conn, _record) -> None:
    """每条新连接都要设的 SQLite PRAGMA。

    handler 现在是并发处理的(见 main.build_application),默认的 rollback
    journal 下「一写多读」会直接抛 database is locked,所以:
    - journal_mode=WAL: 读写不再互斥(该设置写进库文件头,持久生效)
    - busy_timeout:    真的撞上写锁时先等一会儿,而不是立刻报错
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
    finally:
        cur.close()


async def init_db(db_url: str) -> None:
    """初始化引擎,建表。idempotent。"""
    global _engine, _session_factory
    _engine = create_async_engine(db_url, future=True)
    if db_url.startswith("sqlite"):
        event.listen(_engine.sync_engine, "connect", _apply_sqlite_pragmas)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_columns(conn)
        await _fixup_active_buckets(conn)


# 建表之后补加的列。create_all 只建新表、不会 ALTER 已存在的表,所以每次给
# 已有模型加列,都必须在这里登记一份 "列名 -> 列定义",老库才会被补齐。
# 只支持 ADD COLUMN 能表达的变更(可空、或带常量默认值);改类型 / 加约束
# 需要走重建表,那种情况请单独写迁移。
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "panels": {
        "api_host": "TEXT",
        "api_key": "TEXT",
    },
    "servers": {
        "key_passphrase": "TEXT",
        "host_key": "TEXT",
    },
    "oss_config": {
        "is_active": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "cos_config": {
        "is_active": "BOOLEAN NOT NULL DEFAULT 0",
    },
}


async def _ensure_columns(conn) -> None:
    """按 ADDED_COLUMNS 给老库补列,已存在的跳过。idempotent。"""
    for table, columns in ADDED_COLUMNS.items():
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        existing = {row[1] for row in result.fetchall()}
        if not existing:  # 表还不存在(create_all 会建),无需补列
            continue
        for name, ddl in columns.items():
            if name in existing:
                continue
            await conn.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
            )
            log.info("已为老库补列: %s.%s %s", table, name, ddl)


async def _fixup_active_buckets(conn) -> None:
    """oss_config / cos_config 多桶化前的老库只有一行且没有 is_active 标记,
    把最早一行标为当前使用,保证「有配置就恰有一个活动桶」的不变式。idempotent。"""
    for table in ("oss_config", "cos_config"):
        await conn.exec_driver_sql(
            f"UPDATE {table} SET is_active = 1 "
            f"WHERE id = (SELECT MIN(id) FROM {table}) "
            f"AND NOT EXISTS (SELECT 1 FROM {table} WHERE is_active = 1)"
        )


def session() -> AsyncSession:
    if _session_factory is None:
        raise RuntimeError("数据库未初始化,先调用 init_db()")
    return _session_factory()


# ---------- servers ----------

async def list_servers(s: AsyncSession) -> list[Server]:
    result = await s.execute(select(Server).order_by(Server.id))
    return list(result.scalars().all())


async def get_server(s: AsyncSession, server_id: int) -> Server | None:
    return await s.get(Server, server_id)


async def get_server_by_name(s: AsyncSession, name: str) -> Server | None:
    result = await s.execute(select(Server).where(Server.name == name))
    return result.scalar_one_or_none()


async def create_server(
    s: AsyncSession,
    *,
    name: str,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    credential: str,
    key_passphrase: str | None = None,
    host_key: str | None = None,
    v2node_installed: bool = False,
    status: str = "active",
) -> Server:
    server = Server(
        name=name,
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        credential=credential,
        key_passphrase=key_passphrase,
        host_key=host_key,
        v2node_installed=v2node_installed,
        status=status,
    )
    s.add(server)
    await s.flush()
    return server


async def update_server_status(s: AsyncSession, server_id: int, status: str) -> None:
    server = await s.get(Server, server_id)
    if server is not None:
        server.status = status


async def update_server(
    s: AsyncSession,
    server_id: int,
    *,
    name: str | None = None,
    host: str | None = None,
    username: str | None = None,
    port: int | None = None,
    auth_type: str | None = None,
    credential: str | None = None,
    key_passphrase: str | None = None,
    clear_key_passphrase: bool = False,
) -> Server | None:
    """更新服务器的可改字段;只更新传入的非 None 项。

    改了认证方式 / 凭据后主机不变,故 host_key 不动;要重置指纹用
    clear_server_host_key。clear_key_passphrase 用于换成不带口令的私钥。
    """
    server = await s.get(Server, server_id)
    if server is None:
        return None
    if name is not None:
        server.name = name
    if host is not None:
        server.host = host
    if username is not None:
        server.username = username
    if port is not None:
        server.port = port
    if auth_type is not None:
        server.auth_type = auth_type
    if credential is not None:
        server.credential = credential
    if key_passphrase is not None:
        server.key_passphrase = key_passphrase
    elif clear_key_passphrase:
        server.key_passphrase = None
    return server


async def set_v2node_installed(
    s: AsyncSession, server_id: int, installed: bool
) -> None:
    server = await s.get(Server, server_id)
    if server is not None:
        server.v2node_installed = installed


async def set_server_host_key(
    s: AsyncSession, server_id: int, fingerprint: str
) -> None:
    """记录首次连接看到的主机密钥指纹。"""
    server = await s.get(Server, server_id)
    if server is not None:
        server.host_key = fingerprint


async def clear_server_host_key(s: AsyncSession, server_id: int) -> None:
    """清掉已记录的指纹,下次连接重新 TOFU(服务器重装后用)。"""
    server = await s.get(Server, server_id)
    if server is not None:
        server.host_key = None


async def delete_server(s: AsyncSession, server_id: int) -> None:
    server = await s.get(Server, server_id)
    if server is not None:
        await s.delete(server)


# ---------- nodes ----------

async def list_nodes(s: AsyncSession, server_id: int) -> list[Node]:
    result = await s.execute(
        select(Node).where(Node.server_id == server_id).order_by(Node.id)
    )
    return list(result.scalars().all())


async def get_node(s: AsyncSession, node_pk: int) -> Node | None:
    return await s.get(Node, node_pk)


async def find_node(
    s: AsyncSession, server_id: int, api_host: str, node_id: int
) -> Node | None:
    result = await s.execute(
        select(Node).where(
            Node.server_id == server_id,
            Node.api_host == api_host,
            Node.node_id == node_id,
        )
    )
    return result.scalar_one_or_none()


async def add_node(
    s: AsyncSession,
    *,
    server_id: int,
    api_host: str,
    node_id: int,
    api_key: str,
    timeout: int = 15,
) -> Node:
    node = Node(
        server_id=server_id,
        api_host=api_host,
        node_id=node_id,
        api_key=api_key,
        timeout=timeout,
    )
    s.add(node)
    await s.flush()
    return node


async def delete_node(s: AsyncSession, node_pk: int) -> None:
    node = await s.get(Node, node_pk)
    if node is not None:
        await s.delete(node)


async def clear_nodes(s: AsyncSession, server_id: int) -> None:
    await s.execute(delete(Node).where(Node.server_id == server_id))


async def replace_nodes(
    s: AsyncSession,
    server_id: int,
    items: Iterable[dict],
) -> int:
    """以远程为准覆盖该服务器的节点列表。items 中的 api_key 应已加密。

    返回最终节点数量。
    """
    await s.execute(delete(Node).where(Node.server_id == server_id))
    count = 0
    for item in items:
        s.add(
            Node(
                server_id=server_id,
                api_host=item["api_host"],
                node_id=item["node_id"],
                api_key=item["api_key"],
                timeout=item.get("timeout", 15),
            )
        )
        count += 1
    await s.flush()
    return count


# ---------- panels ----------

async def list_panels(s: AsyncSession) -> list[Panel]:
    result = await s.execute(select(Panel).order_by(Panel.id))
    return list(result.scalars().all())


async def get_panel(s: AsyncSession, panel_id: int) -> Panel | None:
    return await s.get(Panel, panel_id)


async def get_panel_by_name(s: AsyncSession, name: str) -> Panel | None:
    result = await s.execute(select(Panel).where(Panel.name == name))
    return result.scalar_one_or_none()


async def create_panel(
    s: AsyncSession,
    *,
    name: str,
    base_url: str,
    secure_path: str,
    email: str,
    password: str,
    api_host: str | None = None,
    api_key: str | None = None,
    auth_data: str | None = None,
) -> Panel:
    panel = Panel(
        name=name,
        base_url=base_url,
        secure_path=secure_path,
        email=email,
        password=password,
        api_host=api_host,
        api_key=api_key,
        auth_data=auth_data,
        auth_data_updated_at=utcnow() if auth_data else None,
    )
    s.add(panel)
    await s.flush()
    return panel


async def update_panel_auth(s: AsyncSession, panel_id: int, auth_data: str) -> None:
    panel = await s.get(Panel, panel_id)
    if panel is not None:
        panel.auth_data = auth_data
        panel.auth_data_updated_at = utcnow()


async def update_panel(
    s: AsyncSession,
    panel_id: int,
    **fields,
) -> None:
    """更新 panel 的任意字段。仅允许已知字段。"""
    allowed = {
        "name", "base_url", "secure_path", "email", "password",
        "api_host", "api_key",
    }
    panel = await s.get(Panel, panel_id)
    if panel is None:
        return
    for key, value in fields.items():
        if key in allowed:
            setattr(panel, key, value)


async def delete_panel(s: AsyncSession, panel_id: int) -> None:
    panel = await s.get(Panel, panel_id)
    if panel is not None:
        await s.delete(panel)


# ---------- panel nodes ----------

async def list_panel_nodes(s: AsyncSession, panel_id: int) -> list[PanelNode]:
    result = await s.execute(
        select(PanelNode)
        .where(PanelNode.panel_id == panel_id)
        .order_by(PanelNode.sort.asc().nulls_last(), PanelNode.node_id)
    )
    return list(result.scalars().all())


async def get_panel_node(
    s: AsyncSession, panel_id: int, node_id: int
) -> PanelNode | None:
    result = await s.execute(
        select(PanelNode).where(
            PanelNode.panel_id == panel_id,
            PanelNode.node_id == node_id,
        )
    )
    return result.scalar_one_or_none()


async def replace_panel_nodes(
    s: AsyncSession,
    panel_id: int,
    items: Iterable[dict],
) -> int:
    """以远程为准覆盖该面板的节点缓存,返回最终节点数量。"""
    await s.execute(delete(PanelNode).where(PanelNode.panel_id == panel_id))
    count = 0
    now = utcnow()
    for item in items:
        payload = dict(item)
        payload.setdefault("synced_at", now)
        s.add(PanelNode(panel_id=panel_id, **payload))
        count += 1
    await s.flush()
    return count


async def update_panel_node_show(
    s: AsyncSession, panel_id: int, node_id: int, show: bool
) -> None:
    node = await get_panel_node(s, panel_id, node_id)
    if node is not None:
        node.show = show


async def delete_panel_node(
    s: AsyncSession, panel_id: int, node_id: int
) -> None:
    node = await get_panel_node(s, panel_id, node_id)
    if node is not None:
        await s.delete(node)


async def latest_node_sync_at(
    s: AsyncSession, panel_id: int
) -> datetime | None:
    """最近一次成功同步的时间,空表返回 None。"""
    result = await s.execute(
        select(PanelNode.synced_at)
        .where(PanelNode.panel_id == panel_id)
        .order_by(PanelNode.synced_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ---------- dns accounts ----------

async def list_dns_accounts(s: AsyncSession) -> list[DnsAccount]:
    result = await s.execute(select(DnsAccount).order_by(DnsAccount.id))
    return list(result.scalars().all())


async def get_dns_account(s: AsyncSession, account_id: int) -> DnsAccount | None:
    return await s.get(DnsAccount, account_id)


async def get_dns_account_by_name(
    s: AsyncSession, name: str
) -> DnsAccount | None:
    result = await s.execute(select(DnsAccount).where(DnsAccount.name == name))
    return result.scalar_one_or_none()


async def create_dns_account(
    s: AsyncSession,
    *,
    provider: str,
    name: str,
    api_token: str,
    email: str | None = None,
) -> DnsAccount:
    account = DnsAccount(
        provider=provider,
        name=name,
        api_token=api_token,
        email=email,
    )
    s.add(account)
    await s.flush()
    return account


async def update_dns_account(
    s: AsyncSession,
    account_id: int,
    **fields,
) -> None:
    allowed = {"name", "api_token", "email"}
    account = await s.get(DnsAccount, account_id)
    if account is None:
        return
    for key, value in fields.items():
        if key in allowed:
            setattr(account, key, value)


async def delete_dns_account(s: AsyncSession, account_id: int) -> None:
    account = await s.get(DnsAccount, account_id)
    if account is not None:
        await s.delete(account)


# ---------- release sources ----------

async def list_release_sources(s: AsyncSession) -> list[ReleaseSource]:
    result = await s.execute(select(ReleaseSource).order_by(ReleaseSource.id))
    return list(result.scalars().all())


async def get_release_source(
    s: AsyncSession, source_id: int
) -> ReleaseSource | None:
    return await s.get(ReleaseSource, source_id)


async def get_release_source_by_repo(
    s: AsyncSession, repo: str
) -> ReleaseSource | None:
    result = await s.execute(
        select(ReleaseSource).where(ReleaseSource.repo == repo)
    )
    return result.scalar_one_or_none()


async def create_release_source(
    s: AsyncSession,
    *,
    repo: str,
    token: str,
) -> ReleaseSource:
    source = ReleaseSource(repo=repo, token=token)
    s.add(source)
    await s.flush()
    return source


async def delete_release_source(s: AsyncSession, source_id: int) -> None:
    source = await s.get(ReleaseSource, source_id)
    if source is not None:
        await s.delete(source)


# ---------- oss config ----------
# 可存多个存储桶;不变式:只要有配置行,就恰有一行 is_active=1(当前发布使用)。
# 由 upsert(首个自动激活)/ set_active / delete(删活动桶顶替)与启动时的
# _fixup_oss_active 共同维护。

async def list_oss_configs(s: AsyncSession) -> list[OssConfig]:
    result = await s.execute(select(OssConfig).order_by(OssConfig.id))
    return list(result.scalars().all())


async def get_oss_config(s: AsyncSession, config_id: int) -> OssConfig | None:
    return await s.get(OssConfig, config_id)


async def get_active_oss_config(s: AsyncSession) -> OssConfig | None:
    """当前发布使用的配置。不变式意外破坏时退回最早一行,不至于发布失灵。"""
    result = await s.execute(
        select(OssConfig)
        .order_by(OssConfig.is_active.desc(), OssConfig.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def upsert_oss_config(
    s: AsyncSession,
    *,
    region: str,
    bucket: str,
    access_key_id: str,
    access_key_secret: str,
    public_base_url: str | None,
    prefix: str,
) -> OssConfig:
    """按 (region, bucket) 更新或新增;首个配置自动设为当前使用。"""
    result = await s.execute(
        select(OssConfig)
        .where(OssConfig.region == region, OssConfig.bucket == bucket)
        .order_by(OssConfig.id)
    )
    config = result.scalars().first()
    if config is None:
        total = (
            await s.execute(select(func.count()).select_from(OssConfig))
        ).scalar_one()
        config = OssConfig(is_active=total == 0)
        s.add(config)
    config.region = region
    config.bucket = bucket
    config.access_key_id = access_key_id
    config.access_key_secret = access_key_secret
    config.public_base_url = public_base_url
    config.prefix = prefix
    await s.flush()
    return config


async def set_active_oss_config(s: AsyncSession, config_id: int) -> OssConfig | None:
    config = await s.get(OssConfig, config_id)
    if config is None:
        return None
    await s.execute(
        update(OssConfig)
        .where(OssConfig.id != config_id, OssConfig.is_active)
        .values(is_active=False)
    )
    config.is_active = True
    await s.flush()
    return config


async def delete_oss_config(s: AsyncSession, config_id: int) -> OssConfig | None:
    """删除一个存储桶配置;删的是活动桶时把剩下最早的一个顶上。返回被删的行。"""
    config = await s.get(OssConfig, config_id)
    if config is None:
        return None
    was_active = config.is_active
    await s.delete(config)
    await s.flush()
    if was_active:
        result = await s.execute(
            select(OssConfig).order_by(OssConfig.id).limit(1)
        )
        successor = result.scalars().first()
        if successor is not None:
            successor.is_active = True
            await s.flush()
    return config


# ---------- cos config ----------
# 与 oss config 同一套多桶约定:只要有配置行,就恰有一行 is_active=1,
# 远程配置的读写都走这一行。

async def list_cos_configs(s: AsyncSession) -> list[CosConfig]:
    result = await s.execute(select(CosConfig).order_by(CosConfig.id))
    return list(result.scalars().all())


async def get_cos_config(s: AsyncSession, config_id: int) -> CosConfig | None:
    return await s.get(CosConfig, config_id)


async def get_active_cos_config(s: AsyncSession) -> CosConfig | None:
    """当前使用的配置。不变式意外破坏时退回最早一行,不至于功能失灵。"""
    result = await s.execute(
        select(CosConfig)
        .order_by(CosConfig.is_active.desc(), CosConfig.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def upsert_cos_config(
    s: AsyncSession,
    *,
    region: str,
    bucket: str,
    secret_id: str,
    secret_key: str,
) -> CosConfig:
    """按 (region, bucket) 更新或新增;首个配置自动设为当前使用。"""
    result = await s.execute(
        select(CosConfig)
        .where(CosConfig.region == region, CosConfig.bucket == bucket)
        .order_by(CosConfig.id)
    )
    config = result.scalars().first()
    if config is None:
        total = (
            await s.execute(select(func.count()).select_from(CosConfig))
        ).scalar_one()
        config = CosConfig(is_active=total == 0)
        s.add(config)
    config.region = region
    config.bucket = bucket
    config.secret_id = secret_id
    config.secret_key = secret_key
    await s.flush()
    return config


async def set_active_cos_config(s: AsyncSession, config_id: int) -> CosConfig | None:
    config = await s.get(CosConfig, config_id)
    if config is None:
        return None
    await s.execute(
        update(CosConfig)
        .where(CosConfig.id != config_id, CosConfig.is_active)
        .values(is_active=False)
    )
    config.is_active = True
    await s.flush()
    return config


async def delete_cos_config(s: AsyncSession, config_id: int) -> CosConfig | None:
    """删除一个存储桶配置;删的是活动桶时把剩下最早的一个顶上。返回被删的行。"""
    config = await s.get(CosConfig, config_id)
    if config is None:
        return None
    was_active = config.is_active
    await s.delete(config)
    await s.flush()
    if was_active:
        result = await s.execute(
            select(CosConfig).order_by(CosConfig.id).limit(1)
        )
        successor = result.scalars().first()
        if successor is not None:
            successor.is_active = True
            await s.flush()
    return config


# ---------- remote config files ----------

async def list_remote_files(s: AsyncSession) -> list[RemoteConfigFile]:
    result = await s.execute(
        select(RemoteConfigFile).order_by(RemoteConfigFile.id)
    )
    return list(result.scalars().all())


async def get_remote_file(
    s: AsyncSession, file_id: int
) -> RemoteConfigFile | None:
    return await s.get(RemoteConfigFile, file_id)


async def get_remote_file_by_path(
    s: AsyncSession, path: str
) -> RemoteConfigFile | None:
    result = await s.execute(
        select(RemoteConfigFile).where(RemoteConfigFile.path == path)
    )
    return result.scalar_one_or_none()


async def create_remote_file(s: AsyncSession, *, path: str) -> RemoteConfigFile:
    file = RemoteConfigFile(path=path)
    s.add(file)
    await s.flush()
    return file


async def delete_remote_file(s: AsyncSession, file_id: int) -> None:
    file = await s.get(RemoteConfigFile, file_id)
    if file is not None:
        await s.delete(file)


# ---------- operation logs ----------

async def add_log(
    s: AsyncSession,
    *,
    user_id: int,
    server_id: int | None,
    action: str,
    result: str,
    detail: str | None = None,
) -> None:
    """写一条操作日志。detail 统一脱敏,避免第三方异常原文把凭据带进明文表。"""
    s.add(
        OperationLog(
            user_id=user_id,
            server_id=server_id,
            action=action,
            result=result,
            detail=redact(detail),
        )
    )


# 操作日志保留天数。日志只增不减会让库无限膨胀,启动时清理一次。
LOG_RETENTION_DAYS = 90


def _log_filter(stmt, only_failed: bool):
    return stmt.where(OperationLog.result != "success") if only_failed else stmt


async def count_logs(s: AsyncSession, *, only_failed: bool = False) -> int:
    stmt = _log_filter(select(func.count()).select_from(OperationLog), only_failed)
    return int((await s.execute(stmt)).scalar_one())


async def list_logs(
    s: AsyncSession,
    *,
    limit: int,
    offset: int = 0,
    only_failed: bool = False,
) -> list[OperationLog]:
    """按时间倒序分页取日志。"""
    stmt = _log_filter(select(OperationLog), only_failed)
    result = await s.execute(
        stmt.order_by(OperationLog.id.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


async def prune_logs(
    s: AsyncSession, keep_days: int = LOG_RETENTION_DAYS
) -> int:
    """删除 keep_days 之前的日志,返回删除条数。"""
    cutoff = utcnow() - timedelta(days=keep_days)
    result = await s.execute(
        delete(OperationLog).where(OperationLog.created_at < cutoff)
    )
    return result.rowcount or 0
