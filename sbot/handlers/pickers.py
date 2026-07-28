"""「选面板 → 选节点」的共用列表渲染。

安装 v2node(install.py)和给服务器加节点(add_node.py)是同一套两级选择,
以前两边各抄了一份、各自截断在前 30 / 前 50 个。这里合成一份并统一分页,
差异(标题、回调前缀、返回按钮)通过参数传入。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..db.models import Panel, PanelNode
from .common import pager_row, paginate, safe_edit


def node_label(node: PanelNode) -> str:
    """列表按钮上的节点标签:上架状态 + 中转标记 + 编号 + 名字。"""
    relay = "🔁" if node.parent_id else ""
    show = "✅" if node.show else "❌"
    return f"{show}{relay} #{node.node_id} {node.name}"


async def render_panel_picker(
    query,
    *,
    panels: Sequence[Panel],
    page: int | None,
    intro: Sequence[str],
    pick_cb: Callable[[Panel], str],
    page_cb_prefix: str,
    back: InlineKeyboardButton,
    empty_text: str,
    missing_creds_hint: str,
) -> None:
    """渲染面板选择页。缺 api_host / api_key 的面板不可选,单独列出来提示。"""
    back_kb = InlineKeyboardMarkup([[back]])
    if not panels:
        await safe_edit(query, empty_text, reply_markup=back_kb)
        return

    usable = [p for p in panels if p.api_host and p.api_key]
    skipped = [p.name for p in panels if not (p.api_host and p.api_key)]

    lines = list(intro)
    if skipped:
        lines.append("")
        lines.append("以下面板因缺通信凭据(api_host / api_key)被跳过:")
        lines.extend(f"  • {name}" for name in skipped)
        lines.append(missing_creds_hint)

    if not usable:
        await safe_edit(query, "\n".join(lines), reply_markup=back_kb)
        return

    view = paginate(usable, page)
    if view.multi:
        lines.insert(len(intro), view.label)

    rows = [
        [InlineKeyboardButton(
            f"{p.name} ({p.api_host})", callback_data=pick_cb(p)
        )]
        for p in view.items
    ]
    pager = pager_row(page_cb_prefix, view)
    if pager:
        rows.append(pager)
    rows.append([back])
    await safe_edit(
        query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)
    )


async def render_node_picker(
    query,
    *,
    nodes: Sequence[PanelNode],
    page: int | None,
    intro: Sequence[str],
    pick_cb: Callable[[PanelNode], str],
    page_cb_prefix: str,
    back: InlineKeyboardButton,
    empty_text: str,
) -> None:
    """渲染节点选择页(数据来自本地 panel_nodes 缓存)。"""
    back_kb = InlineKeyboardMarkup([[back]])
    if not nodes:
        await safe_edit(query, empty_text, reply_markup=back_kb)
        return

    view = paginate(nodes, page)
    lines = list(intro)
    if view.multi:
        lines.append(view.label)

    rows = [
        [InlineKeyboardButton(node_label(n), callback_data=pick_cb(n))]
        for n in view.items
    ]
    pager = pager_row(page_cb_prefix, view)
    if pager:
        rows.append(pager)
    rows.append([back])
    await safe_edit(
        query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows)
    )
