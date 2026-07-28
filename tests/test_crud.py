"""数据层:PRAGMA、补列迁移、级联删除、预加载、日志分页与保留、脱敏落库。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text

from sbot.core.timeutil import utcnow
from sbot.db import crud
from sbot.db.models import Node, PanelNode


def _row(**kw) -> dict:
    """panel_nodes 的一行,字段齐全但都是占位值。"""
    return kw


async def _panel(s, name="p1"):
    return await crud.create_panel(
        s, name=name, base_url="https://x", secure_path="a",
        email="a@b.c", password="enc",
    )


async def _server(s, name="s1"):
    return await crud.create_server(
        s, name=name, host="h", port=22, username="root",
        auth_type="password", credential="enc",
    )


# ---------- 连接参数 ----------

async def test_sqlite_pragmas_applied(db):
    async with crud.session() as s:
        assert str((await s.execute(text("PRAGMA journal_mode"))).scalar_one()).lower() == "wal"
        assert int((await s.execute(text("PRAGMA busy_timeout"))).scalar_one()) == 5000


# ---------- 补列迁移 ----------

async def test_ensure_columns_adds_registered_columns(db):
    crud.ADDED_COLUMNS.setdefault("panels", {})["probe_col"] = "TEXT"
    try:
        async with crud._engine.begin() as conn:
            await crud._ensure_columns(conn)
            cols = {r[1] for r in (await conn.exec_driver_sql(
                "PRAGMA table_info(panels)")).fetchall()}
            assert "probe_col" in cols
            await crud._ensure_columns(conn)      # idempotent
    finally:
        crud.ADDED_COLUMNS["panels"].pop("probe_col")


async def test_old_database_gets_new_columns(tmp_path):
    """真实场景:线上老库的 servers 表没有 host_key / key_passphrase。"""
    import aiosqlite

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "CREATE TABLE servers ("
            " id INTEGER PRIMARY KEY, name VARCHAR(64), host VARCHAR(255),"
            " port INTEGER, username VARCHAR(64), auth_type VARCHAR(16),"
            " credential TEXT, status VARCHAR(16), v2node_installed BOOLEAN,"
            " created_at DATETIME, updated_at DATETIME)"
        )
        await conn.execute(
            "INSERT INTO servers (name, host, port, username, auth_type,"
            " credential, status, v2node_installed) VALUES"
            " ('老机器', 'h', 22, 'root', 'password', 'enc', 'active', 0)"
        )
        await conn.commit()

    await crud.init_db(f"sqlite+aiosqlite:///{path}")
    try:
        async with crud._engine.begin() as conn:
            cols = {r[1] for r in (await conn.exec_driver_sql(
                "PRAGMA table_info(servers)")).fetchall()}
        assert {"host_key", "key_passphrase"} <= cols
        async with crud.session() as s:
            srv = await crud.get_server(s, 1)
            assert srv.name == "老机器"          # 原有数据没丢
            assert srv.host_key is None
    finally:
        await crud._engine.dispose()
        crud._engine = crud._session_factory = None


async def test_new_server_columns_present(db):
    async with crud._engine.begin() as conn:
        cols = {r[1] for r in (await conn.exec_driver_sql(
            "PRAGMA table_info(servers)")).fetchall()}
    assert {"host_key", "key_passphrase"} <= cols


# ---------- 关系加载 ----------

async def test_get_panel_does_not_load_nodes(db, caplog):
    async with crud.session() as s:
        p = await _panel(s)
        await s.commit()
        await crud.replace_panel_nodes(s, p.id, [
            _row(node_id=i, name=f"n{i}", protocol="ss", host="h", port=1,
                 server_port=1, network=None, tls=None, rate=None, sort=i,
                 show=True, parent_id=None, available_status=2, raw_json="{}")
            for i in range(1, 51)
        ])
        await s.commit()

    import logging
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")
    async with crud.session() as s:
        await crud.get_panel(s, p.id)
        await crud.list_servers(s)
    queries = " ".join(r.getMessage() for r in caplog.records)
    assert "FROM panel_nodes" not in queries
    assert "FROM nodes" not in queries


async def test_delete_cascades_children(db):
    async with crud.session() as s:
        p, srv = await _panel(s), await _server(s)
        await s.commit()
        await crud.replace_panel_nodes(s, p.id, [
            _row(node_id=1, name="n", protocol="ss", host="h", port=1,
                 server_port=1, network=None, tls=None, rate=None, sort=1,
                 show=True, parent_id=None, available_status=2, raw_json="{}")
        ])
        await crud.add_node(s, server_id=srv.id, api_host="https://x",
                            node_id=1, api_key="k")
        await s.commit()
        await crud.delete_panel(s, p.id)
        await crud.delete_server(s, srv.id)
        await s.commit()

    async with crud.session() as s:
        assert (await s.execute(select(PanelNode))).scalars().all() == []
        assert (await s.execute(select(Node))).scalars().all() == []


# ---------- 面板节点缓存 ----------

async def test_replace_panel_nodes_is_full_overwrite(db):
    async with crud.session() as s:
        p = await _panel(s)
        await s.commit()
        rows = lambda ids: [  # noqa: E731
            _row(node_id=i, name=f"n{i}", protocol="ss", host="h", port=1,
                 server_port=1, network=None, tls=None, rate=None, sort=i,
                 show=True, parent_id=None, available_status=2, raw_json="{}")
            for i in ids
        ]
        assert await crud.replace_panel_nodes(s, p.id, rows([1, 2, 3])) == 3
        await s.commit()
        assert await crud.replace_panel_nodes(s, p.id, rows([2, 9])) == 2
        await s.commit()
        left = await crud.list_panel_nodes(s, p.id)
        assert [n.node_id for n in left] == [2, 9]
        assert await crud.latest_node_sync_at(s, p.id) is not None


async def test_list_panel_nodes_sorted_by_sort_then_id(db):
    async with crud.session() as s:
        p = await _panel(s)
        await s.commit()
        await crud.replace_panel_nodes(s, p.id, [
            _row(node_id=5, name="a", protocol="ss", host="h", port=1, server_port=1,
                 network=None, tls=None, rate=None, sort=2, show=True,
                 parent_id=None, available_status=2, raw_json="{}"),
            _row(node_id=3, name="b", protocol="ss", host="h", port=1, server_port=1,
                 network=None, tls=None, rate=None, sort=1, show=True,
                 parent_id=None, available_status=2, raw_json="{}"),
            _row(node_id=9, name="c", protocol="ss", host="h", port=1, server_port=1,
                 network=None, tls=None, rate=None, sort=None, show=True,
                 parent_id=None, available_status=2, raw_json="{}"),
        ])
        await s.commit()
        assert [n.node_id for n in await crud.list_panel_nodes(s, p.id)] == [3, 5, 9]


# ---------- 主机密钥 ----------

async def test_host_key_set_and_clear(db):
    async with crud.session() as s:
        srv = await _server(s)
        await s.commit()
        assert srv.host_key is None
        await crud.set_server_host_key(s, srv.id, "SHA256:abc")
        await s.commit()
        assert (await crud.get_server(s, srv.id)).host_key == "SHA256:abc"
        await crud.clear_server_host_key(s, srv.id)
        await s.commit()
        assert (await crud.get_server(s, srv.id)).host_key is None


async def test_update_server_can_clear_passphrase(db):
    async with crud.session() as s:
        srv = await crud.create_server(
            s, name="k", host="h", port=22, username="root",
            auth_type="key", credential="/k", key_passphrase="enc",
        )
        await s.commit()
        await crud.update_server(s, srv.id, credential="/k2",
                                 clear_key_passphrase=True)
        await s.commit()
        got = await crud.get_server(s, srv.id)
        assert got.credential == "/k2" and got.key_passphrase is None


# ---------- 操作日志 ----------

async def test_log_pagination_and_filter(db):
    async with crud.session() as s:
        for i in range(45):
            await crud.add_log(s, user_id=1, server_id=None, action=f"a{i}",
                               result="success" if i % 3 else "failed")
        await s.commit()
        assert await crud.count_logs(s) == 45
        assert await crud.count_logs(s, only_failed=True) == 15
        page2 = await crud.list_logs(s, limit=10, offset=10)
        assert [e.action for e in page2][0] == "a34"      # 倒序:a44..a35 是第一页
        assert len(page2) == 10
        failed = await crud.list_logs(s, limit=100, only_failed=True)
        assert all(e.result != "success" for e in failed)


async def test_prune_logs_only_removes_expired(db):
    async with crud.session() as s:
        await crud.add_log(s, user_id=1, server_id=None, action="new", result="success")
        await crud.add_log(s, user_id=1, server_id=None, action="old", result="success")
        await s.flush()
        await s.execute(
            text("UPDATE operation_logs SET created_at = :t WHERE action = 'old'"),
            {"t": utcnow() - timedelta(days=crud.LOG_RETENTION_DAYS + 1)},
        )
        await s.commit()
        assert await crud.prune_logs(s) == 1
        await s.commit()
        assert [e.action for e in await crud.list_logs(s, limit=10)] == ["new"]


async def test_add_log_redacts_detail(db):
    async with crud.session() as s:
        await crud.add_log(
            s, user_id=1, server_id=None, action="panel.sync", result="failed",
            detail='登录失败 password=hunter2xx api_key=abcd1234efgh',
        )
        await s.commit()
        entry = (await crud.list_logs(s, limit=1))[0]
    assert "hunter2xx" not in entry.detail
    assert "abcd1234efgh" not in entry.detail
    assert "panel.sync" == entry.action


@pytest.mark.parametrize("only_failed", [True, False])
async def test_count_matches_list_length(db, only_failed):
    async with crud.session() as s:
        for i in range(7):
            await crud.add_log(s, user_id=1, server_id=None, action="x",
                               result="failed" if i % 2 else "success")
        await s.commit()
        total = await crud.count_logs(s, only_failed=only_failed)
        rows = await crud.list_logs(s, limit=100, only_failed=only_failed)
        assert total == len(rows)
