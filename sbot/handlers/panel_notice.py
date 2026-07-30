"""面板公告列表 / 详情。

公告不做本地缓存:数量少、且发布后要立刻看到面板上的真实状态,
所以每次都实时调 notice/fetch,分页策略与 DNS 记录一致。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..db import crud
from ..db.models import Panel
from ..services.v2board_api import V2BoardAPIError
from .common import (
    CB_PANEL_NOTICE,
    CB_PANEL_NOTICE_DEL,
    CB_PANEL_NOTICE_DEL_OK,
    CB_PANEL_NOTICE_SHOW,
    CB_PANEL_NOTICES,
    CB_PANEL_PREFIX,
    get_ctx,
    html_to_text,
    truncate,
)


log = logging.getLogger(__name__)


NOTICES_PER_PAGE = 8
# inline 按钮文字过长会被 Telegram 挤成一坨,标题按这个长度截断
TITLE_BTN_LIMIT = 32


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _notice_id(item: dict[str, Any]) -> int | None:
    try:
        return int(item["id"])
    except (KeyError, TypeError, ValueError):
        return None


def _is_shown(item: dict[str, Any]) -> bool:
    """v2board 的 show 可能是 0/1、true/false 或字符串,统一成 bool。"""
    value = item.get("show")
    if isinstance(value, str):
        return value not in ("", "0", "false", "False")
    return bool(value)


def _fmt_ts(value: Any) -> str:
    """公告的 created_at / updated_at 是 unix 时间戳,按 bot 所在时区展示。"""
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def _fmt_tags(value: Any) -> str:
    if isinstance(value, list) and value:
        return ", ".join(str(t) for t in value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "-"


def _total_pages(total: int) -> int:
    return max(1, -(-total // NOTICES_PER_PAGE))


async def _load_panel(update: Update, panel_id: int) -> Panel | None:
    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await update.callback_query.edit_message_text("面板不存在(可能已被删除)。")
    return panel


def _back_to_list_kb(panel_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "⬅ 返回列表",
            callback_data=f"{CB_PANEL_NOTICES}{panel_id}:{page}",
        )]]
    )


# ---------- 列表 ----------

async def cb_list_notices(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    panel_id_s, page_s = payload.split(":", 1)
    await _render_notice_list(update, context, int(panel_id_s), int(page_s))


async def _render_notice_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    panel_id: int,
    page: int,
    *,
    banner: str | None = None,
) -> None:
    query = update.callback_query
    panel = await _load_panel(update, panel_id)
    if panel is None:
        return
    ctx = get_ctx(context)

    page = max(1, page)
    # 先落一条过渡文案:既是等待提示,也保证下面的 edit 一定与当前内容不同
    # (内容完全一致时 Telegram 会报 Message is not modified)
    await query.edit_message_text(f"正在拉取「{panel.name}」的公告(第 {page} 页)…")

    try:
        items, total = await ctx.v2board.get_notices(
            panel, current=page, page_size=NOTICES_PER_PAGE
        )
    except V2BoardAPIError as exc:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                "⬅ 返回面板", callback_data=f"{CB_PANEL_PREFIX}{panel_id}"
            )]]
        )
        await query.edit_message_text(f"❌ 拉取公告失败:{exc}", reply_markup=kb)
        return

    total_pages = _total_pages(total)
    if page > total_pages:  # 删到最后一页空了,回退一页重渲染
        await _render_notice_list(
            update, context, panel_id, total_pages, banner=banner
        )
        return

    lines: list[str] = []
    if banner:
        lines.append(banner)
        lines.append("")
    lines.append(f"面板「{panel.name}」的公告(共 {total} 条)")
    if total_pages > 1:
        lines.append(f"第 {page} / {total_pages} 页")

    rows: list[list[InlineKeyboardButton]] = []
    if not items:
        lines.append("")
        lines.append("(暂无公告)")
    else:
        lines.append("")
        lines.append("图例: ✅已发布 ❌未发布")
        for item in items:
            nid = _notice_id(item)
            if nid is None:
                continue
            mark = "✅" if _is_shown(item) else "❌"
            title = _short(str(item.get("title") or "(无标题)"), TITLE_BTN_LIMIT)
            rows.append([InlineKeyboardButton(
                f"{mark} #{nid} {title}",
                callback_data=f"{CB_PANEL_NOTICE}{panel_id}:{page}:{nid}",
            )])

    pager: list[InlineKeyboardButton] = []
    if page > 1:
        pager.append(InlineKeyboardButton(
            "⬅ 上一页",
            callback_data=f"{CB_PANEL_NOTICES}{panel_id}:{page - 1}",
        ))
    if page < total_pages:
        pager.append(InlineKeyboardButton(
            "下一页 ➡",
            callback_data=f"{CB_PANEL_NOTICES}{panel_id}:{page + 1}",
        ))
    if pager:
        rows.append(pager)

    rows.append([
        InlineKeyboardButton(
            "🔄 刷新", callback_data=f"{CB_PANEL_NOTICES}{panel_id}:{page}"
        ),
        InlineKeyboardButton(
            "⬅ 返回", callback_data=f"{CB_PANEL_PREFIX}{panel_id}"
        ),
    ])

    await query.edit_message_text(
        truncate("\n".join(lines)), reply_markup=InlineKeyboardMarkup(rows)
    )


# ---------- 详情 ----------

async def cb_notice_detail(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    panel_id_s, page_s, notice_id_s = payload.split(":", 2)
    await _render_notice_detail(
        update, context, int(panel_id_s), int(page_s), int(notice_id_s)
    )


async def _render_notice_detail(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    panel_id: int,
    page: int,
    notice_id: int,
    *,
    banner: str | None = None,
) -> None:
    query = update.callback_query
    panel = await _load_panel(update, panel_id)
    if panel is None:
        return
    ctx = get_ctx(context)

    await query.edit_message_text(f"正在拉取公告 #{notice_id}…")
    try:
        item = await ctx.v2board.get_notice(panel, notice_id)
    except V2BoardAPIError as exc:
        await query.edit_message_text(
            f"❌ 拉取公告失败:{exc}",
            reply_markup=_back_to_list_kb(panel_id, page),
        )
        return

    if item is None:
        await query.edit_message_text(
            f"公告 #{notice_id} 不存在或已被删除。",
            reply_markup=_back_to_list_kb(panel_id, page),
        )
        return

    text = _format_notice(item, notice_id)
    if banner:
        text = f"{banner}\n\n{text}"

    suffix = f"{panel_id}:{page}:{notice_id}"
    show_label = "🔻 取消发布" if _is_shown(item) else "🔺 发布"
    rows = [
        [
            InlineKeyboardButton(
                show_label, callback_data=f"{CB_PANEL_NOTICE_SHOW}{suffix}"
            ),
            InlineKeyboardButton(
                "🗑 删除", callback_data=f"{CB_PANEL_NOTICE_DEL}{suffix}"
            ),
        ],
        [InlineKeyboardButton(
            "⬅ 返回列表",
            callback_data=f"{CB_PANEL_NOTICES}{panel_id}:{page}",
        )],
    ]
    await query.edit_message_text(
        truncate(text), reply_markup=InlineKeyboardMarkup(rows)
    )


def _format_notice(item: dict[str, Any], notice_id: int) -> str:
    state = "✅ 已发布" if _is_shown(item) else "❌ 未发布"
    lines = [
        f"公告 #{notice_id}  {state}",
        "",
        f"标题: {item.get('title') or '(无标题)'}",
        f"标签: {_fmt_tags(item.get('tags'))}",
        f"图片: {item.get('img_url') or '-'}",
        f"创建: {_fmt_ts(item.get('created_at'))}",
        f"更新: {_fmt_ts(item.get('updated_at'))}",
        "",
        "正文:",
        html_to_text(str(item.get("content") or "")) or "(空)",
    ]
    return "\n".join(lines)


# ---------- 发布 / 取消发布 ----------

async def cb_notice_show_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    panel_id_s, page_s, notice_id_s = payload.split(":", 2)
    panel_id, page, notice_id = int(panel_id_s), int(page_s), int(notice_id_s)

    panel = await _load_panel(update, panel_id)
    if panel is None:
        return
    ctx = get_ctx(context)

    try:
        await ctx.v2board.toggle_notice_show(panel, notice_id)
        ok, msg = True, "已切换发布状态"
    except V2BoardAPIError as exc:
        ok, msg = False, str(exc)

    await _log(update, "panel.notice.show", ok, panel_id, notice_id, msg)
    # notice/show 是取反语义,不能本地推断结果,详情页会重新拉取真实状态
    await _render_notice_detail(
        update, context, panel_id, page, notice_id,
        banner=f"{'✅' if ok else '❌'} {msg}",
    )


# ---------- 删除 ----------

async def cb_notice_drop_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    panel_id_s, page_s, notice_id_s = payload.split(":", 2)
    panel_id, page, notice_id = int(panel_id_s), int(page_s), int(notice_id_s)

    panel = await _load_panel(update, panel_id)
    if panel is None:
        return

    suffix = f"{panel_id}:{page}:{notice_id}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "确认删除", callback_data=f"{CB_PANEL_NOTICE_DEL_OK}{suffix}"
        ),
        InlineKeyboardButton(
            "取消", callback_data=f"{CB_PANEL_NOTICE}{suffix}"
        ),
    ]])
    await query.edit_message_text(
        f"⚠️ 确认从面板「{panel.name}」删除公告 #{notice_id}?\n该操作不可撤销。",
        reply_markup=kb,
    )


async def cb_notice_drop_do(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    panel_id_s, page_s, notice_id_s = payload.split(":", 2)
    panel_id, page, notice_id = int(panel_id_s), int(page_s), int(notice_id_s)

    panel = await _load_panel(update, panel_id)
    if panel is None:
        return
    ctx = get_ctx(context)

    try:
        await ctx.v2board.drop_notice(panel, notice_id)
        ok, msg = True, f"已删除公告 #{notice_id}"
    except V2BoardAPIError as exc:
        ok, msg = False, str(exc)

    await _log(update, "panel.notice.drop", ok, panel_id, notice_id, msg)
    banner = f"{'✅' if ok else '❌'} {msg}"
    if ok:
        await _render_notice_list(update, context, panel_id, page, banner=banner)
    else:
        await _render_notice_detail(
            update, context, panel_id, page, notice_id, banner=banner
        )


async def _log(
    update: Update,
    action: str,
    ok: bool,
    panel_id: int,
    notice_id: int | None,
    msg: str,
) -> None:
    async with crud.session() as s:
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action=action,
            result="success" if ok else "failed",
            detail=f"panel_id={panel_id}, notice_id={notice_id}: {msg}",
        )
        await s.commit()


def register(application, ctx) -> None:
    application.add_handler(
        CallbackQueryHandler(
            cb_list_notices, pattern=f"^{CB_PANEL_NOTICES}\\d+:\\d+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_notice_detail, pattern=f"^{CB_PANEL_NOTICE}\\d+:\\d+:\\d+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_notice_show_toggle,
            pattern=f"^{CB_PANEL_NOTICE_SHOW}\\d+:\\d+:\\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_notice_drop_confirm,
            pattern=f"^{CB_PANEL_NOTICE_DEL}\\d+:\\d+:\\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_notice_drop_do,
            pattern=f"^{CB_PANEL_NOTICE_DEL_OK}\\d+:\\d+:\\d+$",
        )
    )
