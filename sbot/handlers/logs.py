"""/logs — 查看最近的操作日志。"""
from __future__ import annotations

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from ..db import crud
from .common import truncate


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with crud.session() as s:
        logs = await crud.recent_logs(s, limit=20)
        # 取一份 server name 映射
        servers = {srv.id: srv.name for srv in await crud.list_servers(s)}

    if not logs:
        await update.effective_message.reply_text("尚无操作日志。")
        return

    lines = ["最近 20 条操作日志(从新到旧):", ""]
    for entry in logs:
        srv_name = servers.get(entry.server_id, "—") if entry.server_id else "—"
        ts = entry.created_at.strftime("%m-%d %H:%M:%S")
        mark = "✅" if entry.result == "success" else "❌"
        lines.append(f"{ts}  {mark}  user={entry.user_id}  {entry.action}  [{srv_name}]")
        if entry.detail:
            detail = entry.detail.replace("\n", " ")
            if len(detail) > 100:
                detail = detail[:100] + "…"
            lines.append(f"    {detail}")

    await update.effective_message.reply_text(truncate("\n".join(lines)))


def register(application, ctx) -> None:
    application.add_handler(CommandHandler("logs", cmd_logs))
