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

    nodes: Mapped[list["Node"]] = relationship(
        back_populates="server",
        cascade="all, delete-orphan",
        lazy="selectin",
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

    panel_nodes: Mapped[list["PanelNode"]] = relationship(
        back_populates="panel",
        cascade="all, delete-orphan",
        lazy="selectin",
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
