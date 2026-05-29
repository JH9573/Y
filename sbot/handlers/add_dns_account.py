"""添加 DNS 账户对话流程(Cloudflare)。

/adddns 或主菜单 → DNS 管理 → ➕ 添加账户

流程: 服务商 → 别名 → API Token → 调 verify 测试 → 入库。
现阶段只支持 cloudflare,服务商步骤直接跳过。
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
    CB_MENU_DNS_ADD,
    NON_MENU_TEXT_FILTER,
    cancel_only_kb,
    get_ctx,
    main_menu_kb,
)


log = logging.getLogger(__name__)


NAME, TOKEN = range(2)

KEY = "adddns"


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data[KEY] = {"provider": "cloudflare"}
    await update.effective_message.reply_text(
        "开始添加 DNS 账户(Cloudflare)。任意时候可点「❌ 取消」或发 /cancel 中止。\n\n"
        "请输入账户别名(例如 主域名):",
        reply_markup=cancel_only_kb(),
    )
    return NAME


async def step_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("别名不能为空,请重新输入:")
        return NAME
    if len(name) > 64:
        await update.message.reply_text("别名过长(最多 64 字符),请重新输入:")
        return NAME
    async with crud.session() as s:
        existing = await crud.get_dns_account_by_name(s, name)
    if existing is not None:
        await update.message.reply_text(f"别名「{name}」已被使用,请换一个:")
        return NAME
    context.user_data[KEY]["name"] = name
    await update.message.reply_text(
        "请输入 Cloudflare API Token(权限至少需要 Zone:Read + DNS:Edit)。\n"
        "获取入口: dashboard → My Profile → API Tokens → Create Token。\n"
        "(收到后 bot 会立即从聊天中删除该消息并加密入库)"
    )
    return TOKEN


async def step_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    # 立刻删除聊天中明文 token
    with suppress(BadRequest):
        await update.message.delete()
    if not raw:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="API Token 不能为空,请重新输入:",
        )
        return TOKEN
    ctx = get_ctx(context)
    context.user_data[KEY]["api_token"] = ctx.crypto.encrypt(raw)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="已收到 Token,聊天中明文已删除。正在测试…",
    )
    return await _finalize(update, context)


async def _finalize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    ctx = get_ctx(context)
    chat_id = update.effective_chat.id

    trial = DnsAccount(
        provider=data["provider"],
        name=data["name"],
        api_token=data["api_token"],
    )
    try:
        verify = await ctx.cloudflare.verify_token(trial)
    except CloudflareAPIError as exc:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Token 校验失败,账户未登记:{exc}",
            reply_markup=main_menu_kb(),
        )
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    status = str(verify.get("status") or "")
    if status and status.lower() not in ("active", "ok"):
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Token 状态非 active,账户未登记: status={status}",
            reply_markup=main_menu_kb(),
        )
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    async with crud.session() as s:
        account = await crud.create_dns_account(
            s,
            provider=data["provider"],
            name=data["name"],
            api_token=data["api_token"],
        )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="dns.account.add",
            result="success",
            detail=(
                f"account_id={account.id}, name={account.name}, "
                f"provider={account.provider}"
            ),
        )
        await s.commit()
        account_id = account.id
        account_name = account.name

    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "进入账户", callback_data=f"{CB_DNS_ACCOUNT}{account_id}",
    )]])
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ DNS 账户「{account_name}」已添加,Token 校验通过。\n"
            f"点下方按钮进入账户查看域名列表。"
        ),
        reply_markup=kb,
    )
    await context.bot.send_message(
        chat_id=chat_id, text="(回到主菜单)", reply_markup=main_menu_kb(),
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text(
        "已取消添加 DNS 账户。", reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("adddns", cmd_add),
            CallbackQueryHandler(cmd_add, pattern=f"^{CB_MENU_DNS_ADD}$"),
        ],
        states={
            NAME: [MessageHandler(NON_MENU_TEXT_FILTER, step_name)],
            TOKEN: [MessageHandler(NON_MENU_TEXT_FILTER, step_token)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="adddns",
        persistent=False,
    )
    application.add_handler(conv)
