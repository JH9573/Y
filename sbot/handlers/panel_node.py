"""面板 v2node 节点列表 / 详情 / 上下架 / 删除。

添加节点放在后续步骤里实现。
"""
from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..db import crud
from ..services.v2board_api import V2BoardAPIError
from .common import (
    CB_PANEL_NODE,
    CB_PANEL_NODE_DROP,
    CB_PANEL_NODE_DROP_OK,
    CB_PANEL_NODE_SHOW,
    CB_PANEL_NODES,
    CB_PANEL_PREFIX,
    get_ctx,
    truncate,
)


log = logging.getLogger(__name__)

# 防止 inline 按钮过多;v2board 一般不会超过这个量级
NODE_LIST_LIMIT = 50

AVAILABLE_STATUS_TEXT = {0: "离线", 1: "异常", 2: "正常"}


# ---------- 列表 ----------

async def cb_list_nodes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":", 1)[1])
    await _render_node_list(update, context, panel_id)


async def _render_node_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE, panel_id: int
) -> None:
    query = update.callback_query
    ctx = get_ctx(context)
    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板不存在。")
        return

    await query.edit_message_text(f"正在从面板「{panel.name}」拉取 v2node 节点…")
    try:
        nodes = await ctx.v2board.get_v2nodes(panel)
    except V2BoardAPIError as exc:
        back = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                "⬅ 返回", callback_data=f"{CB_PANEL_PREFIX}{panel_id}"
            )]]
        )
        await query.edit_message_text(f"❌ 拉取失败:{exc}", reply_markup=back)
        return

    nodes.sort(key=lambda n: (n.get("sort") or 0, n.get("id") or 0))

    if not nodes:
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(
                    "🔄 刷新", callback_data=f"{CB_PANEL_NODES}{panel_id}"
                )],
                [InlineKeyboardButton(
                    "⬅ 返回", callback_data=f"{CB_PANEL_PREFIX}{panel_id}"
                )],
            ]
        )
        await query.edit_message_text(
            f"面板「{panel.name}」暂无 v2node 节点。", reply_markup=kb
        )
        return

    lines = [f"面板「{panel.name}」的 v2node 节点(共 {len(nodes)} 个):"]
    rows: list[list[InlineKeyboardButton]] = []
    for n in nodes[:NODE_LIST_LIMIT]:
        nid = n.get("id")
        name = n.get("name") or "(未命名)"
        proto = n.get("protocol") or "?"
        show_mark = "✅" if n.get("show") else "❌"
        lines.append(f"#{nid} {name} [{proto}] {show_mark}")
        rows.append(
            [InlineKeyboardButton(
                f"{show_mark} #{nid} {name}",
                callback_data=f"{CB_PANEL_NODE}{panel_id}:{nid}",
            )]
        )
    if len(nodes) > NODE_LIST_LIMIT:
        lines.append(f"\n…仅显示前 {NODE_LIST_LIMIT} 个,共 {len(nodes)} 个")

    rows.append(
        [
            InlineKeyboardButton(
                "🔄 刷新", callback_data=f"{CB_PANEL_NODES}{panel_id}"
            ),
            InlineKeyboardButton(
                "⬅ 返回", callback_data=f"{CB_PANEL_PREFIX}{panel_id}"
            ),
        ]
    )
    await query.edit_message_text(
        truncate("\n".join(lines)), reply_markup=InlineKeyboardMarkup(rows)
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
    ctx = get_ctx(context)
    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板不存在。")
        return

    back_to_list = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "⬅ 返回列表", callback_data=f"{CB_PANEL_NODES}{panel_id}"
        )]]
    )

    try:
        nodes = await ctx.v2board.get_v2nodes(panel)
        groups = await ctx.v2board.get_groups(panel)
    except V2BoardAPIError as exc:
        await query.edit_message_text(
            f"❌ 拉取失败:{exc}", reply_markup=back_to_list
        )
        return

    node = next((n for n in nodes if n.get("id") == node_id), None)
    if node is None:
        await query.edit_message_text(
            "节点不存在或已被删除。", reply_markup=back_to_list
        )
        return

    text = _format_node(node, _group_name_map(groups))
    if banner:
        text = f"{banner}\n\n{text}"

    is_show = bool(node.get("show"))
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
        [
            InlineKeyboardButton(
                "🔄 刷新",
                callback_data=f"{CB_PANEL_NODE}{panel_id}:{node_id}",
            ),
            InlineKeyboardButton(
                "⬅ 返回列表",
                callback_data=f"{CB_PANEL_NODES}{panel_id}",
            ),
        ],
    ]
    await query.edit_message_text(
        truncate(text), reply_markup=InlineKeyboardMarkup(rows)
    )


def _group_name_map(groups: list[dict[str, Any]]) -> dict[int, str]:
    """{group_id: name} 字典,用于解析节点的 group_id。"""
    result: dict[int, str] = {}
    for g in groups:
        gid = g.get("id")
        try:
            gid_int = int(gid)
        except (TypeError, ValueError):
            continue
        result[gid_int] = str(g.get("name") or f"#{gid_int}")
    return result


def _format_node(node: dict[str, Any], group_names: dict[int, str]) -> str:
    nid = node.get("id")
    lines = [
        f"v2node #{nid}: {node.get('name', '')}",
        "",
        f"协议: {node.get('protocol', '?')}",
        f"地址: {node.get('host', '')}",
        f"连接端口: {node.get('port', '')}",
        f"后端端口: {node.get('server_port', '')}",
        f"传输: {node.get('network', '')}",
        f"TLS: {node.get('tls', '')}",
        f"倍率: {node.get('rate', '')}",
        f"排序: {node.get('sort', '')}",
        f"状态: {'已上架' if node.get('show') else '已下架'}",
    ]

    gids = node.get("group_id")
    if isinstance(gids, list) and gids:
        names = []
        for gid in gids:
            try:
                gid_int = int(gid)
            except (TypeError, ValueError):
                continue
            names.append(group_names.get(gid_int, f"#{gid_int}"))
        if names:
            lines.append(f"权限组: {', '.join(names)}")

    tags = node.get("tags")
    if isinstance(tags, list) and tags:
        lines.append(f"标签: {', '.join(str(t) for t in tags)}")

    for key, label in (
        ("cipher", "加密"),
        ("flow", "Flow"),
        ("encryption", "Encryption"),
        ("obfs", "Obfs"),
    ):
        v = node.get(key)
        if v:
            lines.append(f"{label}: {v}")

    advanced = [
        k for k in ("tls_settings", "network_settings", "encryption_settings")
        if node.get(k)
    ]
    if advanced:
        lines.append(f"高级配置: {', '.join(advanced)}")

    avail = node.get("available_status")
    if avail is not None:
        lines.append(f"健康: {AVAILABLE_STATUS_TEXT.get(avail, str(avail))}")

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
    if panel is None:
        await query.edit_message_text("面板不存在。")
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
        f"⚠️ 确认从面板「{panel.name}」删除 v2node #{node_id}?\n"
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
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "⬅ 返回列表", callback_data=f"{CB_PANEL_NODES}{panel_id}"
        )]]
    )
    await query.edit_message_text(f"{prefix} {msg}", reply_markup=kb)


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
