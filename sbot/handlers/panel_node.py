"""面板 v2node 节点列表 / 详情 / 上下架 / 删除 / 同步。

列表与详情从本地 panel_nodes 表读取(添加面板时已初始化拉取);
上下架 / 删除调远端 API,成功后乐观更新本地缓存;
列表底部的「🔄 同步」按钮可手动从面板覆盖整张缓存表。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..db import crud
from ..db.models import PanelNode
from ..services.v2board_api import V2BoardAPIError, v2node_to_db_row
from .common import (
    CB_PANEL_NODE,
    CB_PANEL_NODE_ADD,
    CB_PANEL_NODE_DROP,
    CB_PANEL_NODE_DROP_OK,
    CB_PANEL_NODE_EDIT,
    CB_PANEL_NODE_SHOW,
    CB_PANEL_NODE_SYNC,
    CB_PANEL_NODES,
    CB_PANEL_PREFIX,
    get_ctx,
    humanize_age,
    truncate,
)


log = logging.getLogger(__name__)

# 防止 inline 按钮过多;v2board 一般不会超过这个量级
NODE_LIST_LIMIT = 50

# v2board ServerService::mergeData 里的 available_status 取值
AVAILABLE_STATUS_TEXT = {0: "离线", 1: "异常", 2: "正常"}
HEALTH_EMOJI = {0: "🔴", 1: "🟡", 2: "🟢"}


def _health_emoji(status: int | None) -> str:
    return HEALTH_EMOJI.get(status, "⚪")


def _health_text(status: int | None) -> str:
    return AVAILABLE_STATUS_TEXT.get(status, "未知")


# ---------- 列表 ----------

async def cb_list_nodes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":", 1)[1])
    await _render_node_list(update, context, panel_id)


async def _render_node_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    panel_id: int,
    *,
    banner: str | None = None,
) -> None:
    query = update.callback_query
    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
        if panel is None:
            await query.edit_message_text("面板不存在。")
            return
        nodes = await crud.list_panel_nodes(s, panel_id)
        latest_sync = await crud.latest_node_sync_at(s, panel_id)

    header_lines: list[str] = []
    if banner:
        header_lines.append(banner)
        header_lines.append("")
    header_lines.append(f"面板「{panel.name}」的 v2node 节点(共 {len(nodes)} 个)")
    header_lines.append(f"最近同步: {humanize_age(latest_sync)}")

    if not nodes:
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(
                    "➕ 添加 shadowsocks",
                    callback_data=f"{CB_PANEL_NODE_ADD}{panel_id}",
                )],
                [InlineKeyboardButton(
                    "🔄 同步", callback_data=f"{CB_PANEL_NODE_SYNC}{panel_id}"
                )],
                [InlineKeyboardButton(
                    "⬅ 返回", callback_data=f"{CB_PANEL_PREFIX}{panel_id}"
                )],
            ]
        )
        header_lines.append("")
        header_lines.append("(暂无节点) 点「🔄 同步」从面板拉取或「➕ 添加」新建。")
        await query.edit_message_text("\n".join(header_lines), reply_markup=kb)
        return

    header_lines.append("")
    header_lines.append("图例: 🟢正常 🟡异常 🔴离线 ⚪未知 / ✅上架 ❌下架 / 🔁中转")

    rows: list[list[InlineKeyboardButton]] = []
    for n in nodes[:NODE_LIST_LIMIT]:
        health = _health_emoji(n.available_status)
        show_mark = "✅" if n.show else "❌"
        relay = "🔁" if n.parent_id else ""
        label = f"{health}{show_mark}{relay} #{n.node_id} {n.name}"
        rows.append(
            [InlineKeyboardButton(
                label,
                callback_data=f"{CB_PANEL_NODE}{panel_id}:{n.node_id}",
            )]
        )
    if len(nodes) > NODE_LIST_LIMIT:
        header_lines.append(f"…仅显示前 {NODE_LIST_LIMIT} 个,共 {len(nodes)}")

    rows.append(
        [InlineKeyboardButton(
            "➕ 添加 shadowsocks",
            callback_data=f"{CB_PANEL_NODE_ADD}{panel_id}",
        )]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "🔄 同步", callback_data=f"{CB_PANEL_NODE_SYNC}{panel_id}"
            ),
            InlineKeyboardButton(
                "⬅ 返回", callback_data=f"{CB_PANEL_PREFIX}{panel_id}"
            ),
        ]
    )
    await query.edit_message_text(
        truncate("\n".join(header_lines)),
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ---------- 详情 ----------

async def cb_node_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    panel_id_s, node_id_s = payload.split(":", 1)
    await _render_node_detail(
        update, context, int(panel_id_s), int(node_id_s)
    )


async def _render_node_detail(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    panel_id: int,
    node_id: int,
    *,
    banner: str | None = None,
) -> None:
    query = update.callback_query
    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
        if panel is None:
            await query.edit_message_text("面板不存在。")
            return
        node = await crud.get_panel_node(s, panel_id, node_id)
        if node is None:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(
                    "⬅ 返回列表", callback_data=f"{CB_PANEL_NODES}{panel_id}"
                )]]
            )
            await query.edit_message_text(
                "节点不存在或已被删除。", reply_markup=kb
            )
            return
        parent = (
            await crud.get_panel_node(s, panel_id, node.parent_id)
            if node.parent_id else None
        )

    text = _format_node(node, parent)
    if banner:
        text = f"{banner}\n\n{text}"

    is_show = bool(node.show)
    show_label = "🔻 下架" if is_show else "🔺 上架"
    new_show = 0 if is_show else 1
    rows = [
        [
            InlineKeyboardButton(
                show_label,
                callback_data=f"{CB_PANEL_NODE_SHOW}{panel_id}:{node_id}:{new_show}",
            ),
            InlineKeyboardButton(
                "🗑 删除",
                callback_data=f"{CB_PANEL_NODE_DROP}{panel_id}:{node_id}",
            ),
        ],
    ]
    if node.protocol == "shadowsocks":
        rows.append([
            InlineKeyboardButton(
                "✏️ 编辑",
                callback_data=f"{CB_PANEL_NODE_EDIT}{panel_id}:{node_id}",
            ),
        ])
    rows.append([
        InlineKeyboardButton(
            "⬅ 返回列表",
            callback_data=f"{CB_PANEL_NODES}{panel_id}",
        ),
    ])
    await query.edit_message_text(
        truncate(text), reply_markup=InlineKeyboardMarkup(rows)
    )


def _format_node(node: PanelNode, parent: PanelNode | None) -> str:
    relay_suffix = "(中转节点)" if node.parent_id else ""
    health = _health_emoji(node.available_status)

    raw: dict[str, Any] = {}
    if node.raw_json:
        try:
            parsed = json.loads(node.raw_json)
            if isinstance(parsed, dict):
                raw = parsed
        except (json.JSONDecodeError, TypeError):
            pass

    lines = [
        f"v2node #{node.node_id}: {node.name} {relay_suffix}".rstrip(),
        "",
        f"协议: {node.protocol or '?'}",
        f"地址: {node.host}",
        f"连接端口: {node.port}",
        f"后端端口: {node.server_port}",
        f"传输: {node.network or '-'}",
        f"TLS: {node.tls if node.tls is not None else '-'}",
        f"倍率: {node.rate if node.rate is not None else '-'}",
        f"排序: {node.sort if node.sort is not None else '-'}",
        f"状态: {'已上架' if node.show else '已下架'}",
        f"健康: {health} {_health_text(node.available_status)}",
    ]

    if node.parent_id:
        if parent is not None:
            lines.append(f"父节点: #{node.parent_id} ({parent.name})")
        else:
            lines.append(f"父节点: #{node.parent_id} (本地无缓存)")

    group_id = raw.get("group_id")
    if isinstance(group_id, list) and group_id:
        lines.append(f"权限组: {', '.join(str(g) for g in group_id)}")
    tags = raw.get("tags")
    if isinstance(tags, list) and tags:
        lines.append(f"标签: {', '.join(str(t) for t in tags)}")

    for key, label in (
        ("cipher", "加密"),
        ("flow", "Flow"),
        ("encryption", "Encryption"),
        ("obfs", "Obfs"),
    ):
        v = raw.get(key)
        if v:
            lines.append(f"{label}: {v}")

    advanced = [
        k for k in ("tls_settings", "network_settings", "encryption_settings")
        if raw.get(k)
    ]
    if advanced:
        lines.append(f"高级配置: {', '.join(advanced)}")

    lines.append("")
    lines.append(f"快照: {humanize_age(node.synced_at)}")
    return "\n".join(lines)


# ---------- 上下架 ----------

async def cb_node_show_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    panel_id_s, node_id_s, show_s = payload.split(":", 2)
    panel_id = int(panel_id_s)
    node_id = int(node_id_s)
    show = int(show_s)
    ctx = get_ctx(context)

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板不存在。")
        return

    try:
        await ctx.v2board.update_v2node_show(panel, node_id, show)
        ok, msg = True, "已上架" if show else "已下架"
    except V2BoardAPIError as exc:
        ok, msg = False, str(exc)

    async with crud.session() as s:
        if ok:
            await crud.update_panel_node_show(
                s, panel_id, node_id, bool(show)
            )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="panel.node.show",
            result="success" if ok else "failed",
            detail=(
                f"panel_id={panel_id}, node_id={node_id}, "
                f"show={show}: {msg}"
            ),
        )
        await s.commit()

    prefix = "✅" if ok else "❌"
    await _render_node_detail(
        update, context, panel_id, node_id, banner=f"{prefix} {msg}"
    )


# ---------- 删除 ----------

async def cb_node_drop_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    panel_id_s, node_id_s = payload.split(":", 1)
    panel_id = int(panel_id_s)
    node_id = int(node_id_s)

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
        node = await crud.get_panel_node(s, panel_id, node_id)
    if panel is None or node is None:
        await query.edit_message_text("面板或节点不存在。")
        return

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "确认删除",
                    callback_data=f"{CB_PANEL_NODE_DROP_OK}{panel_id}:{node_id}",
                ),
                InlineKeyboardButton(
                    "取消",
                    callback_data=f"{CB_PANEL_NODE}{panel_id}:{node_id}",
                ),
            ]
        ]
    )
    await query.edit_message_text(
        f"⚠️ 确认从面板「{panel.name}」删除 v2node #{node_id}「{node.name}」?\n"
        "该操作不可撤销。",
        reply_markup=kb,
    )


async def cb_node_drop_do(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    panel_id_s, node_id_s = payload.split(":", 1)
    panel_id = int(panel_id_s)
    node_id = int(node_id_s)
    ctx = get_ctx(context)

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板不存在。")
        return

    try:
        await ctx.v2board.drop_v2node(panel, node_id)
        ok, msg = True, f"已删除 v2node #{node_id}"
    except V2BoardAPIError as exc:
        ok, msg = False, str(exc)

    async with crud.session() as s:
        if ok:
            await crud.delete_panel_node(s, panel_id, node_id)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="panel.node.drop",
            result="success" if ok else "failed",
            detail=f"panel_id={panel_id}, node_id={node_id}: {msg}",
        )
        await s.commit()

    prefix = "✅" if ok else "❌"
    if ok:
        await _render_node_list(
            update, context, panel_id, banner=f"{prefix} {msg}"
        )
    else:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                "⬅ 返回列表",
                callback_data=f"{CB_PANEL_NODES}{panel_id}",
            )]]
        )
        await query.edit_message_text(f"{prefix} {msg}", reply_markup=kb)


# ---------- 同步 ----------

async def cb_sync_nodes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":", 1)[1])
    ctx = get_ctx(context)

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板不存在。")
        return

    await query.edit_message_text(f"正在从「{panel.name}」同步 v2node 节点…")
    try:
        nodes = await ctx.v2board.get_v2nodes(panel)
    except V2BoardAPIError as exc:
        async with crud.session() as s:
            await crud.add_log(
                s,
                user_id=update.effective_user.id,
                server_id=None,
                action="panel.node.sync",
                result="failed",
                detail=f"panel_id={panel_id}: {exc}",
            )
            await s.commit()
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                "⬅ 返回", callback_data=f"{CB_PANEL_PREFIX}{panel_id}"
            )]]
        )
        await query.edit_message_text(
            f"❌ 同步失败:{exc}", reply_markup=kb
        )
        return

    items = [v2node_to_db_row(n) for n in nodes]
    async with crud.session() as s:
        count = await crud.replace_panel_nodes(s, panel_id, items)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="panel.node.sync",
            result="success",
            detail=f"panel_id={panel_id}, count={count}",
        )
        await s.commit()

    await _render_node_list(
        update, context, panel_id, banner=f"✅ 已同步 {count} 个节点"
    )


def register(application, ctx) -> None:
    application.add_handler(
        CallbackQueryHandler(cb_list_nodes, pattern=f"^{CB_PANEL_NODES}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_node_detail, pattern=f"^{CB_PANEL_NODE}\\d+:\\d+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_node_show_toggle,
            pattern=f"^{CB_PANEL_NODE_SHOW}\\d+:\\d+:[01]$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_node_drop_confirm,
            pattern=f"^{CB_PANEL_NODE_DROP}\\d+:\\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_node_drop_do,
            pattern=f"^{CB_PANEL_NODE_DROP_OK}\\d+:\\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_sync_nodes,
            pattern=f"^{CB_PANEL_NODE_SYNC}\\d+$",
        )
    )
