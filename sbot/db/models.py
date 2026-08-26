"""SQLAlchemy 模型定义。

三张核心表:servers / nodes / operation_logs。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(16), nullable=False)  # key / password
    credential: Mapped[str] = mapped_column(Text, nullable=False)
    # 私钥口令(仅 auth_type=key 且私钥加密时有值,加密存储)
    key_passphrase: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 首次连接记录的主机密钥指纹(SHA256:...),之后每次连接都要求一致
    host_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    v2node_installed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    # lazy="select"(默认):没有任何代码用 server.nodes 读数据,节点一律走
    # crud.list_nodes 显式查。之前是 selectin,导致每次 get_server /
    # list_servers 都顺带把全部 nodes 拉进内存。保留 ORM cascade,删除
    # 服务器时 AsyncSession.delete 会自行加载子表完成级联。
    nodes: Mapped[list[Node]] = relationship(
        back_populates="server",
        cascade="all, delete-orphan",
    )


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (
        UniqueConstraint("server_id", "api_host", "node_id", name="uq_node_per_server"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=False
    )
    api_host: Mapped[str] = mapped_column(String(255), nullable=False)
    node_id: Mapped[int] = mapped_column(Integer, nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)  # 加密存储
    timeout: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    server: Mapped[Server] = relationship(back_populates="nodes")


class Panel(Base):
    __tablename__ = "panels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    secure_path: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(128), nullable=False)
    password: Mapped[str] = mapped_column(Text, nullable=False)  # 加密存储
    api_host: Mapped[str | None] = mapped_column(Text, nullable=True)  # 节点通信地址
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # 节点通信密钥(加密存储)
    auth_data: Mapped[str | None] = mapped_column(Text, nullable=True)  # 加密缓存的 JWT
    auth_data_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    # 同 Server.nodes:节点走 crud.list_panel_nodes 显式查。这里若用 selectin,
    # 每次 get_panel(几乎每个 handler 都会调)都会把该面板所有 panel_nodes
    # 连同 raw_json 一起载入——500 个节点时单次 get_panel 从 2.4ms 涨到 11.6ms。
    panel_nodes: Mapped[list[PanelNode]] = relationship(
        back_populates="panel",
        cascade="all, delete-orphan",
    )


class PanelNode(Base):
    """面板 v2node 节点的本地缓存。

    v2board 端的 type 字段恒为 'v2node';协议差异落在 protocol 字段上。
    raw_json 保留完整原始 dict,详情页用来解析嵌套字段(tls_settings 等)。
    """

    __tablename__ = "panel_nodes"
    __table_args__ = (
        UniqueConstraint("panel_id", "node_id", name="uq_panel_node"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("panels.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    protocol: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    server_port: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    network: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sort: Mapped[int | None] = mapped_column(Integer, nullable=True)
    show: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    synced_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )

    panel: Mapped[Panel] = relationship(back_populates="panel_nodes")


class DnsAccount(Base):
    """DNS 服务商账户。

    provider 区分服务商('cloudflare' 等),目前只实现 cloudflare。
    api_token 加密存储;email 仅 cloudflare 老式 Global Key 鉴权用得到,
    现在用 API Token 模式时可为空,保留字段方便未来兼容。
    """

    __tablename__ = "dns_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="cloudflare")
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_token: Mapped[str] = mapped_column(Text, nullable=False)  # 加密存储
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class ReleaseSource(Base):
    """GitHub Release 分发源(私有仓库)。

    repo 形如 owner/name,作为唯一标识与展示名。
    token 为 fine-grained PAT,加密存储,仓库权限至少需要 Contents:Read。
    """

    __tablename__ = "release_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String(140), unique=True, nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)  # 加密存储
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class OssConfig(Base):
    """阿里云 OSS 分发配置(可存多个存储桶,is_active 标记当前发布使用的那个)。

    通过 bot 交互录入,优先于 .env 中的 OSS_* 配置。
    (region, bucket) 视为同一配置,重复录入按更新处理(应用层保证,不设表约束,
    老库 ALTER 补不了约束)。
    access_key_secret 加密存储;access_key_id 明文,展示时打码。
    """

    __tablename__ = "oss_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    access_key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    access_key_secret: Mapped[str] = mapped_column(Text, nullable=False)  # 加密存储
    public_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prefix: Mapped[str] = mapped_column(String(64), nullable=False, default="releases")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class CosConfig(Base):
    """腾讯云 COS 配置(单行表,取第一条生效)。

    远程配置(JSON 文件)功能使用。通过 bot 交互录入。
    secret_key 加密存储;secret_id 明文,展示时打码。
    bucket 需带 APPID 后缀(如 mycfg-1250000000)。
    """

    __tablename__ = "cos_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_id: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_key: Mapped[str] = mapped_column(Text, nullable=False)  # 加密存储
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class RemoteConfigFile(Base):
    """受管的远程 JSON 配置文件(COS 上的 object key)。"""

    __tablename__ = "remote_config_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("servers.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
