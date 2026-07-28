"""/logs —— 操作日志:分页浏览 + 只看失败。

分页在 SQL 侧做(表可能很大),翻页控件复用 common 里那套。
超过保留期的日志由 main._post_init 启动时清理。
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from ..db import crud
from .common import (
    CB_LOGS,
    Page,
    page_slice,
    pager_row,
    safe_edit,
    truncate,
)


# 每条日志占两行(标题 + detail),一页 10 条正好一屏
LOGS_PER_PAGE = 10
DETAIL_LIMIT = 100


def _cb(page: int, only_failed: bool) -> str:
    return f"{CB_LOGS}{page}:{1 if only_failed else 0}"


async def _render(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int | None,
    only_failed: bool,
) -> None:
    async with crud.session() as s:
        total = await crud.count_logs(s, only_failed=only_failed)
        number, total_pages, offset = page_slice(total, page, LOGS_PER_PAGE)
        entries = await crud.list_logs(
            s,
            limit=LOGS_PER_PAGE,
            offset=offset,
            only_failed=only_failed,
        )
        servers = {srv.id: srv.name for srv in await crud.list_servers(s)}

    scope = "失败记录" if only_failed else "操作日志"
    view = Page(
        items=entries,
        number=number,
        total_pages=total_pages,
        total=total,
        start=offset,
    )

    lines = [f"{scope}(共 {total} 条,从新到旧)"]
    if view.multi:
        lines.append(view.label)
    lines.append("")
    if not entries:
        lines.append("(没有符合条件的记录)")
    for entry in entries:
        srv_name = servers.get(entry.server_id, "—") if entry.server_id else "—"
        ts = entry.created_at.strftime("%m-%d %H:%M:%S")
        mark = "✅" if entry.result == "success" else "❌"
        lines.append(
            f"{ts}  {mark}  user={entry.user_id}  {entry.action}  [{srv_name}]"
        )
        if entry.detail:
            detail = entry.detail.replace("\n", " ")
            if len(detail) > DETAIL_LIMIT:
                detail = detail[:DETAIL_LIMIT] + "…"
            lines.append(f"    {detail}")

    rows: list[list[InlineKeyboardButton]] = []
    pager = pager_row(
        CB_LOGS, view, suffix=f":{1 if only_failed else 0}"
    )
    if pager:
        rows.append(pager)
    rows.append([
        InlineKeyboardButton(
            "📋 全部" if only_failed else "❌ 只看失败",
            callback_data=_cb(1, not only_failed),
        ),
        InlineKeyboardButton("🔄 刷新", callback_data=_cb(number, only_failed)),
    ])
    kb = InlineKeyboardMarkup(rows)
    text = truncate("\n".join(lines))

    if update.callback_query is not None:
        await safe_edit(update.callback_query, text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _render(update, context, page=1, only_failed=False)


async def cb_logs_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page_s, _, failed_s = query.data.split(":", 1)[1].partition(":")
    await _render(
        update, context, page=int(page_s), only_failed=failed_s == "1"
    )


def register(application, ctx) -> None:
    application.add_handler(CommandHandler("logs", cmd_logs))
    application.add_handler(
        CallbackQueryHandler(cb_logs_page, pattern=f"^{CB_LOGS}\\d+:[01]$")
    )
