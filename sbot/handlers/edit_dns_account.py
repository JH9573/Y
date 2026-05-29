"""编辑 DNS 账户对话流程。

入口: DNS 账户详情页 → 「✏️ 编辑账户」按钮
字段: 别名 → API Token(可保留旧值)→ 测试 → 入库
"""
from __future__ import annotations

import logging
from contextlib import suppress

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
)

from ..db import crud
from ..db.models import DnsAccount
from ..services.cloudflare_api import CloudflareAPIError
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_DNS_ACCOUNT,
    CB_EDIT_DNS_ACCOUNT,
    NON_MENU_TEXT_FILTER,
    get_ctx,
)


log = logging.getLogger(__name__)


NAME, TOKEN, CONFIRM = range(3)

KEY = "editdns"
KEEP_CB = "editdns:keep"


def _keep_kb(value: str | None = None) -> InlineKeyboardMarkup:
    label = f"保留 ({value})" if value else "保留(沿用旧值)"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=KEEP_CB)]]
    )


async def _reply(
    update: Update,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=reply_markup
        )


async def cb_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    account_id = int(query.data.split(":", 1)[1])

    async with crud.session() as s:
        account = await crud.get_dns_account(s, account_id)
    if account is None:
        await query.edit_message_text("DNS 账户不存在。")
        return ConversationHandler.END

    context.user_data[KEY] = {
        "account_id": account_id,
        "initial": {
            "name": account.name,
            "api_token": account.api_token,  # 已加密
        },
        "values": {},
    }
    await query.edit_message_text(
        f"编辑 DNS 账户「{account.name}」。任意时刻可发送 /cancel 中止。\n"
        f"每步可点「保留」沿用旧值。\n\n"
        f"请输入新别名(当前: {account.name}):",
        reply_markup=_keep_kb(account.name),
    )
    return NAME


async def step_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        name = data["initial"]["name"]
    else:
        name = (update.message.text or "").strip()
        if not name:
            await update.message.reply_text("别名不能为空,请重新输入:")
            return NAME
        if len(name) > 64:
            await update.message.reply_text("别名过长(最多 64 字符),请重新输入:")
            return NAME
        if name != data["initial"]["name"]:
            async with crud.session() as s:
                existing = await crud.get_dns_account_by_name(s, name)
            if existing is not None and existing.id != data["account_id"]:
                await update.message.reply_text(
                    f"别名「{name}」已被使用,请换一个:"
                )
                return NAME
    data["values"]["name"] = name
    await _reply(
        update,
        "请输入新的 Cloudflare API Token,或点「保留」沿用旧值。\n"
        "(若输入新值,会立即删除聊天中的明文消息并加密入库)",
        reply_markup=_keep_kb("沿用旧 Token"),
    )
    return TOKEN


async def step_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    ctx = get_ctx(context)
    if update.callback_query:
        await update.callback_query.answer()
        data["values"]["api_token"] = data["initial"]["api_token"]
        data["values"]["token_changed"] = False
    else:
        raw = (update.message.text or "").strip()
        with suppress(BadRequest):
            await update.message.delete()
        if not raw:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Token 不能为空,请重新输入(或点上一条消息的「保留」):",
            )
            return TOKEN
        data["values"]["api_token"] = ctx.crypto.encrypt(raw)
        data["values"]["token_changed"] = True

    return await _show_confirm(update, context)


async def _show_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    v = data["values"]
    initial = data["initial"]

    lines = ["请确认下列变更(提交前会重新校验 Token):", ""]
    if v["name"] == initial["name"]:
        lines.append(f"别名: {v['name']}(未改)")
    else:
        lines.append(f"别名: {initial['name']} → {v['name']}")
    lines.append("Token: 已更改" if v.get("token_changed") else "Token: 沿用旧值")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 提交", callback_data="editdns:ok"),
        InlineKeyboardButton("❌ 取消", callback_data="editdns:cancel"),
    ]])
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, reply_markup=kb,
        )
    return CONFIRM


async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "editdns:cancel":
        context.user_data.pop(KEY, None)
        await query.edit_message_text("已取消编辑。")
        return ConversationHandler.END

    data = context.user_data[KEY]
    v = data["values"]
    account_id = data["account_id"]
    ctx = get_ctx(context)

    await query.edit_message_text("正在校验 Token…")

    trial = DnsAccount(
        provider="cloudflare",
        name=v["name"],
        api_token=v["api_token"],
    )
    try:
        await ctx.cloudflare.verify_token(trial)
    except CloudflareAPIError as exc:
        async with crud.session() as s:
            await crud.add_log(
                s,
                user_id=update.effective_user.id,
                server_id=None,
                action="dns.account.edit",
                result="failed",
                detail=f"account_id={account_id}: {exc}",
            )
            await s.commit()
        await query.edit_message_text(f"❌ Token 校验失败,未更新:{exc}")
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    async with crud.session() as s:
        await crud.update_dns_account(
            s,
            account_id,
            name=v["name"],
            api_token=v["api_token"],
        )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="dns.account.edit",
            result="success",
            detail=(
                f"account_id={account_id}, "
                f"token_changed={v.get('token_changed', False)}"
            ),
        )
        await s.commit()

    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "⬅ 返回账户", callback_data=f"{CB_DNS_ACCOUNT}{account_id}",
    )]])
    await query.edit_message_text(
        "✅ DNS 账户已更新,Token 校验通过。", reply_markup=kb,
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text("已取消编辑 DNS 账户。")
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                cb_edit_entry, pattern=f"^{CB_EDIT_DNS_ACCOUNT}\\d+$",
            ),
        ],
        states={
            NAME: [
                CallbackQueryHandler(step_name, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_name),
            ],
            TOKEN: [
                CallbackQueryHandler(step_token, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_token),
            ],
            CONFIRM: [
                CallbackQueryHandler(
                    step_confirm, pattern=r"^editdns:(ok|cancel)$",
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="editdns",
        persistent=False,
    )
    application.add_handler(conv)
