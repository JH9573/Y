"""数据访问层。

封装会话工厂与常用的 CRUD 操作。所有写操作都通过 AsyncSession 在调用方控制事务。
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from datetime import datetime

from .models import Base, Node, OperationLog, Panel, PanelNode, Server


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(db_url: str) -> None:
    """初始化引擎,建表。idempotent。"""
    global _engine, _session_factory
    _engine = create_async_engine(db_url, future=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def session() -> AsyncSession:
    if _session_factory is None:
        raise RuntimeError("数据库未初始化,先调用 init_db()")
    return _session_factory()


# ---------- servers ----------

async def list_servers(s: AsyncSession) -> list[Server]:
    result = await s.execute(select(Server).order_by(Server.id))
    return list(result.scalars().all())


async def get_server(s: AsyncSession, server_id: int) -> Optional[Server]:
    return await s.get(Server, server_id)


async def get_server_by_name(s: AsyncSession, name: str) -> Optional[Server]:
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


async def set_v2node_installed(
    s: AsyncSession, server_id: int, installed: bool
) -> None:
    server = await s.get(Server, server_id)
    if server is not None:
        server.v2node_installed = installed


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


async def get_node(s: AsyncSession, node_pk: int) -> Optional[Node]:
    return await s.get(Node, node_pk)


async def find_node(
    s: AsyncSession, server_id: int, api_host: str, node_id: int
) -> Optional[Node]:
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


async def get_panel(s: AsyncSession, panel_id: int) -> Optional[Panel]:
    return await s.get(Panel, panel_id)


async def get_panel_by_name(s: AsyncSession, name: str) -> Optional[Panel]:
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
    auth_data: str | None = None,
) -> Panel:
    panel = Panel(
        name=name,
        base_url=base_url,
        secure_path=secure_path,
        email=email,
        password=password,
        auth_data=auth_data,
        auth_data_updated_at=datetime.utcnow() if auth_data else None,
    )
    s.add(panel)
    await s.flush()
    return panel


async def update_panel_auth(s: AsyncSession, panel_id: int, auth_data: str) -> None:
    panel = await s.get(Panel, panel_id)
    if panel is not None:
        panel.auth_data = auth_data
        panel.auth_data_updated_at = datetime.utcnow()


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
) -> Optional[PanelNode]:
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
    now = datetime.utcnow()
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
) -> Optional[datetime]:
    """最近一次成功同步的时间,空表返回 None。"""
    result = await s.execute(
        select(PanelNode.synced_at)
        .where(PanelNode.panel_id == panel_id)
        .order_by(PanelNode.synced_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


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
    s.add(
        OperationLog(
            user_id=user_id,
            server_id=server_id,
            action=action,
            result=result,
            detail=detail,
        )
    )


async def recent_logs(s: AsyncSession, limit: int = 20) -> list[OperationLog]:
    result = await s.execute(
        select(OperationLog).order_by(OperationLog.id.desc()).limit(limit)
    )
    return list(result.scalars().all())
