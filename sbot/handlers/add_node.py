"""添加节点:从已登记的面板和已缓存的 v2node 节点里二级选择。

入口仍是节点管理菜单的 [➕ 添加节点] 按钮(CB_NODE_ADD)。
流程:
  1. 选面板(过滤掉缺通信凭据的)
  2. 选该面板下的 v2node(从 panel_nodes 表读)
  3. 摘要 + 确认
  4. 用 panel.api_host / api_key / node.node_id 直接写远程 config.json
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..core.ssh import SSHError
from ..db import crud
from ..services.v2node_config import (
    NodeEntry,
    V2NodeConfigError,
    add_node_to_config,
)
from .common import (
    CB_NODE_ADD,
    CB_NODE_ADD_NODE,
    CB_NODE_ADD_OK,
    CB_NODE_ADD_PANEL,
    CB_NODE_MENU,
    get_ctx,
)
from .firewall import port_check_block


log = logging.getLogger(__name__)


# 列表上限,避免按钮过多
PANEL_LIMIT = 30
NODE_LIST_LIMIT = 50


# ---------- step 1: 选面板 ----------

async def cb_addnode_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """节点管理菜单点了「➕ 添加节点」,展示面板列表。"""
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
        if server is None:
            await query.edit_message_text("服务器不存在。")
            return
        panels = await crud.list_panels(s)

    back_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "⬅ 返回节点菜单",
            callback_data=f"{CB_NODE_MENU}{server_id}",
        )]]
    )
    if not panels:
        await query.edit_message_text(
            "尚未登记任何面板。请先使用 /addpanel 添加。",
            reply_markup=back_kb,
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    skipped: list[str] = []
    for p in panels[:PANEL_LIMIT]:
        if not p.api_host or not p.api_key:
            skipped.append(p.name)
            continue
        rows.append([InlineKeyboardButton(
            f"{p.name} ({p.api_host})",
            callback_data=f"{CB_NODE_ADD_PANEL}{server_id}:{p.id}",
        )])
    rows.append([InlineKeyboardButton(
        "⬅ 返回",
        callback_data=f"{CB_NODE_MENU}{server_id}",
    )])

    lines = [
        f"为服务器「{server.name}」添加 v2node 节点。",
        "",
        "请选择面板:",
    ]
    if skipped:
        lines.append("")
        lines.append("以下面板因缺通信凭据(api_host / api_key)被跳过:")
        for n in skipped:
            lines.append(f"  • {n}")
        lines.append("可去 /panel 对应面板点「🔄 同步通信凭据」补上。")
    if not any(r for r in rows[:-1]):
        # 全部都被跳过
        await query.edit_message_text(
            "\n".join(lines), reply_markup=back_kb
        )
        return
    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)
    )


# ---------- step 2: 选节点 ----------

async def cb_addnode_pick_panel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    server_id_s, panel_id_s = payload.split(":", 1)
    server_id = int(server_id_s)
    panel_id = int(panel_id_s)

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
        panel = await crud.get_panel(s, panel_id)
        if server is None or panel is None:
            await query.edit_message_text("服务器或面板不存在。")
            return
        nodes = await crud.list_panel_nodes(s, panel_id)

    back_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "⬅ 重选面板",
            callback_data=f"{CB_NODE_ADD}{server_id}",
        )]]
    )
    if not nodes:
        await query.edit_message_text(
            f"面板「{panel.name}」本地未缓存 v2node 节点。\n"
            "请先 /panel 进入该面板,点「📋 节点列表」→「🔄 同步」。",
            reply_markup=back_kb,
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    for n in nodes[:NODE_LIST_LIMIT]:
        relay = "🔁" if n.parent_id else ""
        show = "✅" if n.show else "❌"
        label = f"{show}{relay} #{n.node_id} {n.name}"
        rows.append([InlineKeyboardButton(
            label,
            callback_data=(
                f"{CB_NODE_ADD_NODE}{server_id}:{panel_id}:{n.node_id}"
            ),
        )])
    if len(nodes) > NODE_LIST_LIMIT:
        rows.append([InlineKeyboardButton(
            f"(共 {len(nodes)},仅显示前 {NODE_LIST_LIMIT})",
            callback_data="noop",
        )])
    rows.append([InlineKeyboardButton(
        "⬅ 重选面板",
        callback_data=f"{CB_NODE_ADD}{server_id}",
    )])

    await query.edit_message_text(
        f"面板「{panel.name}」下的 v2node 节点(共 {len(nodes)}),"
        f"选一个加到服务器「{server.name}」:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ---------- step 3: 摘要确认 ----------

async def cb_addnode_pick_node(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    server_id_s, panel_id_s, node_id_s = payload.split(":", 2)
    server_id = int(server_id_s)
    panel_id = int(panel_id_s)
    node_id = int(node_id_s)

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
        panel = await crud.get_panel(s, panel_id)
        node = await crud.get_panel_node(s, panel_id, node_id)
        if server is None or panel is None or node is None:
            await query.edit_message_text("服务器/面板/节点不存在。")
            return
        existing = await crud.find_node(s, server_id, panel.api_host, node_id)

    if existing is not None:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "⬅ 返回",
            callback_data=f"{CB_NODE_ADD_PANEL}{server_id}:{panel_id}",
        )]])
        await query.edit_message_text(
            "bot 已记录该服务器对接同 ApiHost / NodeID 的节点。\n"
            "若实际不一致,请先点节点菜单的「🔄 同步」校正。",
            reply_markup=kb,
        )
        return

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ 确认写入",
                    callback_data=(
                        f"{CB_NODE_ADD_OK}{server_id}:{panel_id}:{node_id}"
                    ),
                ),
                InlineKeyboardButton(
                    "❌ 取消",
                    callback_data=f"{CB_NODE_ADD_PANEL}{server_id}:{panel_id}",
                ),
            ]
        ]
    )
    await query.edit_message_text(
        f"将为服务器「{server.name}」对接面板「{panel.name}」的 v2node:\n\n"
        f"ApiHost: {panel.api_host}\n"
        f"NodeID:  {node_id}\n"
        f"节点:    {node.name} ({node.protocol})\n"
        f"ApiKey:  已记录(加密)\n"
        f"Timeout: 15(默认)\n\n"
        "确认后会写远程 config.json 并重启 v2node。",
        reply_markup=kb,
    )


# ---------- step 4: 执行写入 ----------

async def cb_addnode_do(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    server_id_s, panel_id_s, node_id_s = payload.split(":", 2)
    server_id = int(server_id_s)
    panel_id = int(panel_id_s)
    node_id = int(node_id_s)
    ctx = get_ctx(context)

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
        panel = await crud.get_panel(s, panel_id)
        node = await crud.get_panel_node(s, panel_id, node_id)
        if server is None or panel is None or node is None:
            await query.edit_message_text("服务器/面板/节点不存在。")
            return
        if not panel.api_host or not panel.api_key:
            await query.edit_message_text(
                "该面板缺通信凭据 (api_host / api_key),请先在面板菜单点「🔄 同步通信凭据」。"
            )
            return

    try:
        api_key_plain = ctx.crypto.decrypt(panel.api_key)
    except ValueError as exc:
        await query.edit_message_text(
            f"❌ api_key 解密失败,请重新同步通信凭据:{exc}"
        )
        return

    await query.edit_message_text(
        f"正在写入 {server.name} 的配置并重启 v2node…"
    )

    entry = NodeEntry(
        api_host=panel.api_host,
        node_id=node_id,
        api_key=api_key_plain,
        timeout=15,
    )
    try:
        ok, msg = await add_node_to_config(ctx.ssh, server, entry)
    except (V2NodeConfigError, SSHError) as exc:
        ok, msg = False, str(exc)

    async with crud.session() as s:
        if ok:
            await crud.add_node(
                s,
                server_id=server_id,
                api_host=entry.api_host,
                node_id=entry.node_id,
                api_key=ctx.crypto.encrypt(entry.api_key),
                timeout=entry.timeout,
            )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server_id,
            action="node.add",
            result="success" if ok else "failed",
            detail=(
                f"panel_id={panel_id}, panel_node_id={node_id}, "
                f"api_host={entry.api_host}: {msg}"
            ),
        )
        await s.commit()

    prefix = "✅" if ok else "❌"
    body = f"{prefix} {msg}"
    rows: list[list[InlineKeyboardButton]] = []
    if ok:
        fw_text, fw_buttons = await port_check_block(ctx.ssh, server)
        body = f"{body}\n\n{fw_text}"
        if fw_buttons:
            rows.append(fw_buttons)
    rows.append([InlineKeyboardButton(
        "⬅ 返回节点列表", callback_data=f"{CB_NODE_MENU}{server_id}",
    )])
    await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup(rows))


def register(application, ctx) -> None:
    application.add_handler(
        CallbackQueryHandler(
            cb_addnode_start, pattern=f"^{CB_NODE_ADD}\\d+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_addnode_pick_panel,
            pattern=f"^{CB_NODE_ADD_PANEL}\\d+:\\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_addnode_pick_node,
            pattern=f"^{CB_NODE_ADD_NODE}\\d+:\\d+:\\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_addnode_do,
            pattern=f"^{CB_NODE_ADD_OK}\\d+:\\d+:\\d+$",
        )
    )
