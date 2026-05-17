"""节点管理菜单 / 删除节点 / 同步。

添加节点在 add_node.py 中,这里只提供列表、删除、同步三种入口。
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..core.ssh import SSHError
from ..db import crud
from ..services.v2node_config import (
    V2NodeConfigError,
    read_remote_nodes,
    remove_node_from_config,
)
from .common import (
    CB_NODE_ADD,
    CB_NODE_DEL,
    CB_NODE_DEL_OK,
    CB_NODE_MENU,
    CB_NODE_SYNC,
    CB_SERVER_PREFIX,
    get_ctx,
)


log = logging.getLogger(__name__)


async def cb_node_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])
    await _render_node_menu(update, context, server_id)


async def _render_node_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, server_id: int
) -> None:
    query = update.callback_query
    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
        if server is None:
            await query.edit_message_text("服务器不存在。")
            return
        nodes = await crud.list_nodes(s, server_id)

    lines = [f"{server.name} 的节点列表:"]
    if not nodes:
        lines.append("(暂无节点)")
    else:
        for i, n in enumerate(nodes, 1):
            lines.append(f"{i}) {n.api_host}  NodeID {n.node_id}")
    text = "\n".join(lines)

    rows = [
        [
            InlineKeyboardButton("➕ 添加节点", callback_data=f"{CB_NODE_ADD}{server_id}"),
            InlineKeyboardButton("🔄 同步", callback_data=f"{CB_NODE_SYNC}{server_id}"),
        ]
    ]
    if nodes:
        del_row: list[InlineKeyboardButton] = []
        for i, n in enumerate(nodes, 1):
            del_row.append(
                InlineKeyboardButton(
                    f"🗑 删除 #{i}",
                    callback_data=f"{CB_NODE_DEL}{server_id}:{n.id}",
                )
            )
            if len(del_row) == 3:
                rows.append(del_row)
                del_row = []
        if del_row:
            rows.append(del_row)

    rows.append(
        [InlineKeyboardButton("⬅ 返回", callback_data=f"{CB_SERVER_PREFIX}{server_id}")]
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


# ---------- 删除节点 ----------

async def cb_node_delete_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    server_id_str, node_pk_str = payload.split(":", 1)
    server_id = int(server_id_str)
    node_pk = int(node_pk_str)

    async with crud.session() as s:
        node = await crud.get_node(s, node_pk)
        server = await crud.get_server(s, server_id)
    if node is None or server is None or node.server_id != server_id:
        await query.edit_message_text("节点不存在。")
        return

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "确认", callback_data=f"{CB_NODE_DEL_OK}{server_id}:{node_pk}"
                ),
                InlineKeyboardButton("取消", callback_data=f"{CB_NODE_MENU}{server_id}"),
            ]
        ]
    )
    await query.edit_message_text(
        f"⚠️ 确认从 {server.name} 删除节点 {node.api_host} / NodeID {node.node_id}?\n"
        f"该操作会修改远程 config.json 并重启 v2node。",
        reply_markup=kb,
    )


async def cb_node_delete_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    server_id_str, node_pk_str = payload.split(":", 1)
    server_id = int(server_id_str)
    node_pk = int(node_pk_str)
    ctx = get_ctx(context)

    async with crud.session() as s:
        node = await crud.get_node(s, node_pk)
        server = await crud.get_server(s, server_id)
    if node is None or server is None or node.server_id != server_id:
        await query.edit_message_text("节点不存在。")
        return

    await query.edit_message_text(
        f"正在从 {server.name} 删除节点 {node.api_host} / NodeID {node.node_id}…"
    )

    api_host = node.api_host
    node_id = node.node_id

    try:
        ok, msg = await remove_node_from_config(ctx.ssh, server, api_host, node_id)
    except (V2NodeConfigError, SSHError) as exc:
        ok, msg = False, str(exc)

    async with crud.session() as s:
        if ok:
            await crud.delete_node(s, node_pk)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server_id,
            action="node.delete",
            result="success" if ok else "failed",
            detail=f"{api_host}/NodeID={node_id}: {msg}",
        )
        await s.commit()

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅ 返回节点列表", callback_data=f"{CB_NODE_MENU}{server_id}")]]
    )
    prefix = "✅" if ok else "❌"
    await query.edit_message_text(f"{prefix} {msg}", reply_markup=kb)


# ---------- 同步 ----------

async def cb_node_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])
    ctx = get_ctx(context)

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        await query.edit_message_text("服务器不存在。")
        return

    await query.edit_message_text(f"正在从 {server.name} 同步节点…")

    try:
        remote = await read_remote_nodes(ctx.ssh, server)
    except SSHError as exc:
        await query.edit_message_text(f"❌ 同步失败:{exc}")
        return

    async with crud.session() as s:
        items = [
            {
                "api_host": n.api_host,
                "node_id": n.node_id,
                "api_key": ctx.crypto.encrypt(n.api_key),
                "timeout": n.timeout,
            }
            for n in remote
        ]
        count = await crud.replace_nodes(s, server_id, items)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server_id,
            action="node.sync",
            result="success",
            detail=f"count={count}",
        )
        await s.commit()

    await query.edit_message_text(
        f"✅ 已同步,当前 {count} 个节点。",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("查看节点", callback_data=f"{CB_NODE_MENU}{server_id}")]]
        ),
    )


def register(application, ctx) -> None:
    application.add_handler(
        CallbackQueryHandler(cb_node_menu, pattern=f"^{CB_NODE_MENU}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_node_delete_confirm, pattern=f"^{CB_NODE_DEL}\\d+:\\d+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_node_delete_do, pattern=f"^{CB_NODE_DEL_OK}\\d+:\\d+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(cb_node_sync, pattern=f"^{CB_NODE_SYNC}\\d+$")
    )
