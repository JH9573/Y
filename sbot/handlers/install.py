"""v2node 首次安装 + 首节点配置:从已登记的面板和已缓存的 v2node 节点二级选择。

入口仍是服务器菜单的「安装 v2node」按钮(CB_INSTALL_START)。
流程:
  1. 选面板(过滤掉缺通信凭据的)
  2. 选该面板下的 v2node(从 panel_nodes 表读)
  3. 摘要 + 确认
  4. 串流执行真正的安装并把节点登记入库
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..core.ssh import SSHError
from ..db import crud
from ..services.v2node_config import NodeEntry
from ..services.v2node_install import InstallError, InstallParams, install_v2node
from .common import (
    CB_INSTALL_NODE,
    CB_INSTALL_OK,
    CB_INSTALL_PANEL,
    CB_INSTALL_START,
    CB_SERVER_PREFIX,
    get_ctx,
    safe_edit,
    split_page_arg,
)
from .firewall import port_check_block
from .pickers import render_node_picker, render_panel_picker

log = logging.getLogger(__name__)


# ---------- step 1: 选面板 ----------

async def cb_install_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """点了「安装 v2node」,展示面板列表。"""
    query = update.callback_query
    await query.answer()
    (server_id_s,), page = split_page_arg(query.data.split(":", 1)[1], 1)
    server_id = int(server_id_s)

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
        if server is None:
            await query.edit_message_text("服务器不存在。")
            return
        panels = await crud.list_panels(s)

    back = InlineKeyboardButton(
        "⬅ 返回服务器菜单", callback_data=f"{CB_SERVER_PREFIX}{server_id}"
    )
    await render_panel_picker(
        query,
        panels=panels,
        page=page,
        intro=[
            f"将在服务器「{server.name}」上首次安装 v2node。",
            "bot 会按步骤检测依赖 → 下载二进制 → 注册 systemd → 启动。",
            "",
            "请选择首节点所属面板:",
        ],
        pick_cb=lambda p: f"{CB_INSTALL_PANEL}{server_id}:{p.id}",
        page_cb_prefix=f"{CB_INSTALL_START}{server_id}:",
        back=back,
        empty_text="尚未登记任何面板。请先在「面板管理 → 添加面板」登记一个。",
        missing_creds_hint="可去「面板管理」对应面板点「🔄 同步通信凭据」补上。",
    )


# ---------- step 2: 选节点 ----------

async def cb_install_pick_panel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    (server_id_s, panel_id_s), page = split_page_arg(
        query.data.split(":", 1)[1], 2
    )
    server_id = int(server_id_s)
    panel_id = int(panel_id_s)

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
        panel = await crud.get_panel(s, panel_id)
        if server is None or panel is None:
            await query.edit_message_text("服务器或面板不存在。")
            return
        nodes = await crud.list_panel_nodes(s, panel_id)

    back = InlineKeyboardButton(
        "⬅ 重选面板", callback_data=f"{CB_INSTALL_START}{server_id}"
    )
    await render_node_picker(
        query,
        nodes=nodes,
        page=page,
        intro=[
            f"面板「{panel.name}」下的 v2node 节点(共 {len(nodes)}),"
            f"为服务器「{server.name}」选首节点:",
        ],
        pick_cb=lambda n: f"{CB_INSTALL_NODE}{server_id}:{panel_id}:{n.node_id}",
        page_cb_prefix=f"{CB_INSTALL_PANEL}{server_id}:{panel_id}:",
        back=back,
        empty_text=(
            f"面板「{panel.name}」本地未缓存 v2node 节点。\n"
            "请先进「面板管理」对应面板,点「📋 节点列表」→「🔄 同步」。"
        ),
    )


# ---------- step 3: 摘要确认 ----------

async def cb_install_pick_node(
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

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ 开始安装",
                callback_data=(
                    f"{CB_INSTALL_OK}{server_id}:{panel_id}:{node_id}"
                ),
            ),
            InlineKeyboardButton(
                "❌ 取消",
                callback_data=f"{CB_INSTALL_PANEL}{server_id}:{panel_id}",
            ),
        ]
    ])
    await query.edit_message_text(
        f"准备在「{server.name}」首次安装 v2node 并对接面板「{panel.name}」:\n\n"
        f"ApiHost: {panel.api_host}\n"
        f"NodeID:  {node_id}\n"
        f"节点:    {node.name} ({node.protocol})\n"
        f"ApiKey:  已记录(加密)\n"
        f"Timeout: 15(默认)\n\n"
        "点「开始安装」后会连接服务器并按步骤执行:"
        "检测依赖 → 下载二进制 → 写 config → 注册 systemd → 启动。",
        reply_markup=kb,
    )


# ---------- step 4: 执行安装 ----------

async def cb_install_do(
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

    first_node = NodeEntry(
        api_host=panel.api_host,
        node_id=node_id,
        api_key=api_key_plain,
        timeout=15,
    )
    params = InstallParams(first_node=first_node)

    progress_lines = [f"在 {server.name} 上安装 v2node…"]
    await query.edit_message_text("\n".join(progress_lines))

    failure_detail: str | None = None
    fw_block: tuple[str, list[InlineKeyboardButton]] | None = None
    try:
        # 整个安装(十余条命令)+ 装完的端口体检共用一条 SSH 连接,
        # 而不是每条命令重新握手一次
        async with ctx.ssh.connection(server) as conn:
            async for step in install_v2node(conn, server, params):
                progress_lines.append(f"• {step.step}: {step.detail}")
                await safe_edit(query, "\n".join(progress_lines))
            success = True
            result_text = (
                "\n".join(progress_lines) + "\n\n✅ 安装完成,v2node 已启动。"
            )
            fw_block = await port_check_block(conn, server)
    except InstallError as exc:
        success = False
        failure_detail = str(exc)
        progress_lines.append(f"❌ 失败:{exc}")
        result_text = "\n".join(progress_lines)
    except SSHError as exc:
        success = False
        failure_detail = str(exc)
        progress_lines.append(f"❌ SSH 错误:{exc}")
        result_text = "\n".join(progress_lines)

    async with crud.session() as s:
        if success:
            await crud.set_v2node_installed(s, server_id, True)
            await crud.add_node(
                s,
                server_id=server_id,
                api_host=first_node.api_host,
                node_id=first_node.node_id,
                api_key=ctx.crypto.encrypt(first_node.api_key),
                timeout=first_node.timeout,
            )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server_id,
            action="v2node.install",
            result="success" if success else "failed",
            detail=(
                failure_detail
                or f"panel_id={panel_id}, node={first_node.api_host}/NodeID={first_node.node_id}"
            ),
        )
        await s.commit()

    back_btn = InlineKeyboardButton(
        "⬅ 返回菜单", callback_data=f"{CB_SERVER_PREFIX}{server_id}"
    )
    rows: list[list[InlineKeyboardButton]] = []
    if success and fw_block is not None:
        fw_text, fw_buttons = fw_block
        result_text = f"{result_text}\n\n{fw_text}"
        if fw_buttons:
            rows.append(fw_buttons)
    rows.append([back_btn])
    await safe_edit(
        query, result_text, reply_markup=InlineKeyboardMarkup(rows)
    )


def register(application, ctx) -> None:
    application.add_handler(
        CallbackQueryHandler(
            cb_install_start, pattern=f"^{CB_INSTALL_START}\\d+(:\\d+)?$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_install_pick_panel,
            pattern=f"^{CB_INSTALL_PANEL}\\d+:\\d+(:\\d+)?$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_install_pick_node,
            pattern=f"^{CB_INSTALL_NODE}\\d+:\\d+:\\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_install_do,
            pattern=f"^{CB_INSTALL_OK}\\d+:\\d+:\\d+$",
        )
    )
