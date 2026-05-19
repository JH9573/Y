"""面板信息编辑 / 同步通信凭据。

编辑流程:走 ConversationHandler,逐字段询问,每步可点「保留」沿用旧值。
提交前用新字段重新 login 验证,失败不入库。

同步通信凭据:独立的 callback handler,调 config/fetch 取 api_host / api_key
回填 panels 表,不需要用户输入。
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
    filters,
)

from ..db import crud
from ..db.models import Panel
from ..services.v2board_api import (
    V2BoardAPIError,
    validate_base_url,
    validate_email,
    validate_secure_path,
)
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_EDIT_PANEL,
    CB_PANEL_PREFIX,
    CB_SYNC_PANEL_CREDS,
    NON_MENU_TEXT_FILTER,
    get_ctx,
)


log = logging.getLogger(__name__)


NAME, BASE_URL, SECURE_PATH, EMAIL, PASSWORD, CONFIRM = range(6)

KEY = "editpanel"
KEEP_CB = "editpanel:keep"


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


def _keep_kb(value: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"保留 ({value})", callback_data=KEEP_CB)]]
    )


def _keep_kb_password() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("保留(沿用旧密码)", callback_data=KEEP_CB)]]
    )


# ---------- 入口 ----------

async def cb_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":", 1)[1])

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板不存在。")
        return ConversationHandler.END

    context.user_data[KEY] = {
        "panel_id": panel_id,
        "initial": {
            "name": panel.name,
            "base_url": panel.base_url,
            "secure_path": panel.secure_path,
            "email": panel.email,
            "password": panel.password,  # 已加密;保留时直接复用
        },
        "values": {},
    }
    await query.edit_message_text(
        f"编辑面板「{panel.name}」。任意时刻可发送 /cancel 中止。\n"
        f"每步可点「保留 (xxx)」沿用旧值。\n\n"
        f"请输入新别名(当前: {panel.name}):",
        reply_markup=_keep_kb(panel.name),
    )
    return NAME


# ---------- NAME ----------

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
                existing = await crud.get_panel_by_name(s, name)
            if existing is not None and existing.id != data["panel_id"]:
                await update.message.reply_text(f"别名「{name}」已被使用,请换一个:")
                return NAME
    data["values"]["name"] = name
    await _reply(
        update,
        f"请输入新面板地址(当前: {data['initial']['base_url']}):",
        reply_markup=_keep_kb(data["initial"]["base_url"]),
    )
    return BASE_URL


# ---------- BASE_URL ----------

async def step_base_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        base_url = data["initial"]["base_url"]
    else:
        try:
            base_url = validate_base_url(update.message.text or "")
        except V2BoardAPIError as exc:
            await update.message.reply_text(f"{exc},请重新输入:")
            return BASE_URL
    data["values"]["base_url"] = base_url
    await _reply(
        update,
        f"请输入新后台路径(当前: {data['initial']['secure_path']}):",
        reply_markup=_keep_kb(data["initial"]["secure_path"]),
    )
    return SECURE_PATH


# ---------- SECURE_PATH ----------

async def step_secure_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        secure_path = data["initial"]["secure_path"]
    else:
        try:
            secure_path = validate_secure_path(update.message.text or "")
        except V2BoardAPIError as exc:
            await update.message.reply_text(f"{exc},请重新输入:")
            return SECURE_PATH
    data["values"]["secure_path"] = secure_path
    await _reply(
        update,
        f"请输入新管理员邮箱(当前: {data['initial']['email']}):",
        reply_markup=_keep_kb(data["initial"]["email"]),
    )
    return EMAIL


# ---------- EMAIL ----------

async def step_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        email = data["initial"]["email"]
    else:
        try:
            email = validate_email(update.message.text or "")
        except V2BoardAPIError as exc:
            await update.message.reply_text(f"{exc},请重新输入:")
            return EMAIL
    data["values"]["email"] = email
    await _reply(
        update,
        "请输入新管理员密码(收到后会立即从聊天中删除,加密入库),"
        "或点「保留」沿用旧密码:",
        reply_markup=_keep_kb_password(),
    )
    return PASSWORD


# ---------- PASSWORD ----------

async def step_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    ctx = get_ctx(context)
    if update.callback_query:
        await update.callback_query.answer()
        # 保留旧密码(已加密)
        data["values"]["password"] = data["initial"]["password"]
        data["values"]["password_changed"] = False
    else:
        raw = (update.message.text or "").strip()
        with suppress(BadRequest):
            await update.message.delete()
        if not raw:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="密码不能为空,请重新输入(或点上一条消息的「保留」):",
            )
            return PASSWORD
        data["values"]["password"] = ctx.crypto.encrypt(raw)
        data["values"]["password_changed"] = True

    return await _show_confirm(update, context)


# ---------- CONFIRM ----------

async def _show_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    v = data["values"]
    initial = data["initial"]

    def _diff(key, label):
        if v[key] == initial[key]:
            return f"{label}: {v[key]}(未改)"
        return f"{label}: {initial[key]} → {v[key]}"

    summary_lines = [
        "请确认下列变更(提交前会重新 login 验证):",
        "",
        _diff("name", "别名"),
        _diff("base_url", "面板地址"),
        _diff("secure_path", "后台路径"),
        _diff("email", "管理员邮箱"),
        ("密码: 已更改" if v.get("password_changed") else "密码: 沿用旧值"),
    ]
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ 提交", callback_data="editpanel:ok"),
                InlineKeyboardButton("❌ 取消", callback_data="editpanel:cancel"),
            ]
        ]
    )
    text = "\n".join(summary_lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, reply_markup=kb
        )
    return CONFIRM


async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "editpanel:cancel":
        context.user_data.pop(KEY, None)
        await query.edit_message_text("已取消编辑。")
        return ConversationHandler.END

    data = context.user_data[KEY]
    v = data["values"]
    panel_id = data["panel_id"]
    ctx = get_ctx(context)

    await query.edit_message_text("正在用新凭据登录测试…")

    trial = Panel(
        name=v["name"],
        base_url=v["base_url"],
        secure_path=v["secure_path"],
        email=v["email"],
        password=v["password"],
    )
    try:
        auth_data = await ctx.v2board.login(trial)
    except V2BoardAPIError as exc:
        async with crud.session() as s:
            await crud.add_log(
                s,
                user_id=update.effective_user.id,
                server_id=None,
                action="panel.edit",
                result="failed",
                detail=f"panel_id={panel_id}: {exc}",
            )
            await s.commit()
        await query.edit_message_text(
            f"❌ 用新凭据登录失败,未更新:{exc}"
        )
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    encrypted_auth = ctx.crypto.encrypt(auth_data)
    async with crud.session() as s:
        await crud.update_panel(
            s,
            panel_id,
            name=v["name"],
            base_url=v["base_url"],
            secure_path=v["secure_path"],
            email=v["email"],
            password=v["password"],
        )
        await crud.update_panel_auth(s, panel_id, encrypted_auth)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="panel.edit",
            result="success",
            detail=f"panel_id={panel_id}",
        )
        await s.commit()

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "⬅ 返回面板", callback_data=f"{CB_PANEL_PREFIX}{panel_id}"
        )]]
    )
    await query.edit_message_text(
        "✅ 面板信息已更新,登录验证通过。\n"
        "如修改了面板地址或后台路径,建议点「🔄 同步通信凭据」刷新 api_host/api_key。",
        reply_markup=kb,
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text("已取消编辑面板。")
    return ConversationHandler.END


# ---------- 同步通信凭据 ----------

async def cb_sync_creds(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":", 1)[1])
    ctx = get_ctx(context)

    async with crud.session() as s:
        panel = await crud.get_panel(s, panel_id)
    if panel is None:
        await query.edit_message_text("面板不存在。")
        return

    await query.edit_message_text(f"正在从面板「{panel.name}」拉取通信凭据…")
    try:
        api_host, api_key = await ctx.v2board.fetch_server_credentials(panel)
    except V2BoardAPIError as exc:
        async with crud.session() as s:
            await crud.add_log(
                s,
                user_id=update.effective_user.id,
                server_id=None,
                action="panel.creds.sync",
                result="failed",
                detail=f"panel_id={panel_id}: {exc}",
            )
            await s.commit()
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                "⬅ 返回面板", callback_data=f"{CB_PANEL_PREFIX}{panel_id}"
            )]]
        )
        await query.edit_message_text(
            f"❌ 拉取失败:{exc}", reply_markup=kb
        )
        return

    encrypted_key = ctx.crypto.encrypt(api_key) if api_key else None
    async with crud.session() as s:
        await crud.update_panel(
            s, panel_id, api_host=api_host, api_key=encrypted_key,
        )
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="panel.creds.sync",
            result="success",
            detail=(
                f"panel_id={panel_id}, api_host={api_host}, "
                f"api_key={'set' if api_key else 'empty'}"
            ),
        )
        await s.commit()

    msg = f"✅ 已更新:\napi_host = {api_host}\napi_key = "
    msg += "已记录(加密存储)" if api_key else "面板未配置 server_token,留空"
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "⬅ 返回面板", callback_data=f"{CB_PANEL_PREFIX}{panel_id}"
        )]]
    )
    await query.edit_message_text(msg, reply_markup=kb)


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                cb_edit_entry, pattern=f"^{CB_EDIT_PANEL}\\d+$"
            ),
        ],
        states={
            NAME: [
                CallbackQueryHandler(step_name, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_name),
            ],
            BASE_URL: [
                CallbackQueryHandler(step_base_url, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_base_url),
            ],
            SECURE_PATH: [
                CallbackQueryHandler(step_secure_path, pattern=f"^{KEEP_CB}$"),
                MessageHandler(
                    NON_MENU_TEXT_FILTER, step_secure_path
                ),
            ],
            EMAIL: [
                CallbackQueryHandler(step_email, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_email),
            ],
            PASSWORD: [
                CallbackQueryHandler(step_password, pattern=f"^{KEEP_CB}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_password),
            ],
            CONFIRM: [
                CallbackQueryHandler(
                    step_confirm, pattern=r"^editpanel:(ok|cancel)$"
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="editpanel",
        persistent=False,
    )
    application.add_handler(conv)
    application.add_handler(
        CallbackQueryHandler(
            cb_sync_creds, pattern=f"^{CB_SYNC_PANEL_CREDS}\\d+$"
        )
    )
