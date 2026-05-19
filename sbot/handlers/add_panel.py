"""添加面板对话流程。

/addpanel
  → 别名 → base_url → secure_path → email → password
  → 测试登录 → 写库(同时缓存 auth_data)
"""
from __future__ import annotations

import logging
from contextlib import suppress

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..db import crud
from ..db.models import Panel
from ..services.v2board_api import (
    V2BoardAPIError,
    v2node_to_db_row,
    validate_base_url,
    validate_email,
    validate_secure_path,
)
from .common import get_ctx


log = logging.getLogger(__name__)


NAME, BASE_URL, SECURE_PATH, EMAIL, PASSWORD = range(5)

KEY = "addpanel"


async def cmd_addpanel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data[KEY] = {}
    await update.effective_message.reply_text(
        "开始添加面板。任意时候可发送 /cancel 中止。\n\n"
        "请输入面板别名(例如 主站):"
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
        existing = await crud.get_panel_by_name(s, name)
    if existing is not None:
        await update.message.reply_text(f"别名「{name}」已被使用,请换一个:")
        return NAME
    context.user_data[KEY]["name"] = name
    await update.message.reply_text(
        "请输入面板地址(以 http:// 或 https:// 开头,不含后台路径):"
    )
    return BASE_URL


async def step_base_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        base_url = validate_base_url(update.message.text or "")
    except V2BoardAPIError as exc:
        await update.message.reply_text(f"{exc},请重新输入:")
        return BASE_URL
    context.user_data[KEY]["base_url"] = base_url
    await update.message.reply_text(
        "请输入后台路径(secure_path,即登录后台 URL 里 /api/v1/ 后面那一段,不含斜杠):"
    )
    return SECURE_PATH


async def step_secure_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        secure_path = validate_secure_path(update.message.text or "")
    except V2BoardAPIError as exc:
        await update.message.reply_text(f"{exc},请重新输入:")
        return SECURE_PATH
    context.user_data[KEY]["secure_path"] = secure_path
    await update.message.reply_text("请输入管理员邮箱:")
    return EMAIL


async def step_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        email = validate_email(update.message.text or "")
    except V2BoardAPIError as exc:
        await update.message.reply_text(f"{exc},请重新输入:")
        return EMAIL
    context.user_data[KEY]["email"] = email
    await update.message.reply_text(
        "请输入管理员密码。\n"
        "(收到后 bot 会立即从聊天记录中删除该条消息并加密入库)"
    )
    return PASSWORD


async def step_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    # 立刻删除聊天中明文密码,降低留存风险
    with suppress(BadRequest):
        await update.message.delete()
    if not raw:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="密码不能为空,请重新输入:",
        )
        return PASSWORD
    ctx = get_ctx(context)
    context.user_data[KEY]["password"] = ctx.crypto.encrypt(raw)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="已收到密码,聊天记录中的明文消息已删除。开始测试登录…",
    )
    return await _finalize(update, context)


async def _finalize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    ctx = get_ctx(context)
    chat_id = update.effective_chat.id

    # 用一个游离的 Panel 对象做登录测试,无需先写库
    trial = Panel(
        name=data["name"],
        base_url=data["base_url"],
        secure_path=data["secure_path"],
        email=data["email"],
        password=data["password"],
    )
    try:
        auth_data = await ctx.v2board.login(trial)
    except V2BoardAPIError as exc:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ 面板登录失败,面板未登记:{exc}",
        )
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    encrypted_auth = ctx.crypto.encrypt(auth_data)
    async with crud.session() as s:
        panel = await crud.create_panel(
            s,
            name=data["name"],
            base_url=data["base_url"],
            secure_path=data["secure_path"],
            email=data["email"],
            password=data["password"],
            auth_data=encrypted_auth,
        )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="panel.add",
            result="success",
            detail=f"panel_id={panel.id}, name={panel.name}",
        )
        await s.commit()
        panel_id = panel.id
        panel_name = panel.name

    # 自动拉一次 server_api_url / server_token,失败不阻塞 panel 注册
    creds_tail = ""
    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
        try:
            api_host, api_key = await ctx.v2board.fetch_server_credentials(panel)
        except V2BoardAPIError as exc:
            log.warning("初始化拉取通信凭据失败 panel_id=%s: %s", panel_id, exc)
            creds_tail = (
                f"\n⚠️ 通信凭据拉取失败:{exc}"
                f"\n  服务器添加节点会用到,稍后请在面板菜单点「🔄 同步通信凭据」补上。"
            )
        else:
            encrypted_key = ctx.crypto.encrypt(api_key) if api_key else None
            await crud.update_panel(
                s, panel_id, api_host=api_host, api_key=encrypted_key,
            )
            if api_key:
                creds_tail = f"\n通信凭据已记录(api_host={api_host})。"
            else:
                creds_tail = (
                    f"\n⚠️ 面板未配置 server_token,无法用于服务器添加节点。"
                    f"\n  请到面板「系统配置 → 节点通信」填写后,在面板菜单点「🔄 同步通信凭据」。"
                )
        await s.commit()

    # panel 已落库,再拉一次节点存入本地缓存。失败不阻塞 panel 注册。
    sync_tail = ""
    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
        try:
            nodes = await ctx.v2board.get_v2nodes(panel)
        except V2BoardAPIError as exc:
            log.warning("初始化拉取节点失败 panel_id=%s: %s", panel_id, exc)
            sync_tail = (
                f"\n⚠️ 节点拉取失败:{exc}"
                f"\n  稍后可在节点列表点「🔄 同步」重试。"
            )
            await crud.add_log(
                s,
                user_id=update.effective_user.id,
                server_id=None,
                action="panel.node.sync",
                result="failed",
                detail=f"panel_id={panel_id}: {exc}",
            )
            await s.commit()
        else:
            items = [v2node_to_db_row(n) for n in nodes]
            count = await crud.replace_panel_nodes(s, panel_id, items)
            sync_tail = f"\n已导入 {count} 个 v2node 节点。"
            await crud.add_log(
                s,
                user_id=update.effective_user.id,
                server_id=None,
                action="panel.node.sync",
                result="success",
                detail=f"panel_id={panel_id}, count={count}",
            )
            await s.commit()

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ 面板「{panel_name}」已添加,登录测试通过。"
            f"{creds_tail}{sync_tail}\n"
            f"使用 /panel 查看面板列表。"
        ),
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text("已取消添加面板。")
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[CommandHandler("addpanel", cmd_addpanel)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_name)],
            BASE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_base_url)],
            SECURE_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_secure_path)],
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_email)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_password)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="addpanel",
        persistent=False,
    )
    application.add_handler(conv)
