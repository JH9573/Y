"""添加服务器对话流程。

/addserver
  → 别名 → host → port(可跳过)→ username → 认证方式 → 凭据
  → SSH 连通性测试 → 写库 → 自动导入节点
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

from ..core.ssh import (
    SSHError,
    check_key_passphrase,
    key_needs_passphrase,
    key_permission_warning,
    validate_key_path,
)
from ..db import crud
from ..db.models import Server
from ..services.v2node import INSTALLED_CHECK_CMD
from ..services.v2node_config import read_remote_nodes
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_MENU_SRV_ADD,
    NON_MENU_TEXT_FILTER,
    cancel_only_kb,
    get_ctx,
    main_menu_kb,
)

log = logging.getLogger(__name__)


# 对话状态
NAME, HOST, PORT, USERNAME, AUTH_TYPE, CREDENTIAL, KEY_PASSPHRASE = range(7)

# user_data 中保存中间数据所用的 key
KEY = "addserver"


async def cmd_addserver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data[KEY] = {}
    await update.effective_message.reply_text(
        "开始添加服务器。任意时候可点「❌ 取消」或发送 /cancel 中止。\n\n"
        "请输入服务器别名(例如 香港-1):",
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
        existing = await crud.get_server_by_name(s, name)
    if existing is not None:
        await update.message.reply_text(f"别名「{name}」已被使用,请换一个:")
        return NAME
    context.user_data[KEY]["name"] = name
    await update.message.reply_text("请输入服务器地址(IP 或域名):")
    return HOST


async def step_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    host = (update.message.text or "").strip()
    if not host:
        await update.message.reply_text("地址不能为空,请重新输入:")
        return HOST
    context.user_data[KEY]["host"] = host
    await update.message.reply_text("请输入 SSH 端口(直接回车或发送 / 使用默认 22):")
    return PORT


async def step_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text or "").strip()
    if raw in ("", "/"):
        port = 22
    else:
        try:
            port = int(raw)
        except ValueError:
            await update.message.reply_text("端口必须是整数,请重新输入:")
            return PORT
        if not (1 <= port <= 65535):
            await update.message.reply_text("端口范围 1-65535,请重新输入:")
            return PORT
    context.user_data[KEY]["port"] = port
    await update.message.reply_text("请输入 SSH 登录用户名:")
    return USERNAME


async def step_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = (update.message.text or "").strip()
    if not username:
        await update.message.reply_text("用户名不能为空,请重新输入:")
        return USERNAME
    context.user_data[KEY]["username"] = username
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("密钥", callback_data="auth:key"),
                InlineKeyboardButton("密码", callback_data="auth:password"),
            ]
        ]
    )
    await update.message.reply_text("选择认证方式:", reply_markup=kb)
    return AUTH_TYPE


async def step_auth_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    context.user_data[KEY]["auth_type"] = choice
    if choice == "key":
        await query.edit_message_text(
            "请输入私钥文件在 **bot 服务器上**的绝对路径(密钥内容不入库)。"
        )
    else:
        await query.edit_message_text(
            "请输入 SSH 登录密码。\n"
            "(收到后 bot 会立即从聊天记录中删除该条消息并加密入库)"
        )
    return CREDENTIAL


async def step_credential(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    auth_type = data["auth_type"]
    raw = (update.message.text or "").strip()

    if auth_type == "password":
        # 立刻删除聊天中明文密码,降低留存风险
        with suppress(BadRequest):
            await update.message.delete()
        if not raw:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="密码不能为空,请重新输入:",
            )
            return CREDENTIAL
        ctx = get_ctx(context)
        credential = ctx.crypto.encrypt(raw)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="已收到密码,聊天记录中的明文消息已删除。开始测试连通性…",
        )
    else:
        if not raw:
            await update.message.reply_text("路径不能为空,请重新输入:")
            return CREDENTIAL
        # 早点把路径问题暴露出来,别等到真去连服务器才报错
        try:
            credential = validate_key_path(raw)
            needs_pass = key_needs_passphrase(credential)
        except SSHError as exc:
            await update.message.reply_text(f"{exc}\n请重新输入私钥路径:")
            return CREDENTIAL
        data["credential"] = credential
        warning = key_permission_warning(credential)
        if warning:
            await update.message.reply_text(warning)
        if needs_pass:
            await update.message.reply_text(
                "该私钥有口令保护,请输入口令。\n"
                "(收到后 bot 会立即从聊天记录中删除该条消息并加密入库)"
            )
            return KEY_PASSPHRASE
        await update.message.reply_text("已记录密钥路径,开始测试连通性…")

    data["credential"] = credential
    return await _finalize(update, context)


async def step_key_passphrase(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    raw = (update.message.text or "").strip()
    with suppress(BadRequest):
        await update.message.delete()
    chat_id = update.effective_chat.id
    if not raw:
        await context.bot.send_message(chat_id=chat_id, text="口令不能为空,请重新输入:")
        return KEY_PASSPHRASE
    try:
        check_key_passphrase(data["credential"], raw)
    except SSHError as exc:
        await context.bot.send_message(chat_id=chat_id, text=f"{exc}\n请重新输入口令:")
        return KEY_PASSPHRASE
    data["key_passphrase"] = get_ctx(context).crypto.encrypt(raw)
    await context.bot.send_message(
        chat_id=chat_id,
        text="口令已加密保存,聊天记录中的明文消息已删除。开始测试连通性…",
    )
    return await _finalize(update, context)


async def _finalize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    ctx = get_ctx(context)
    chat_id = update.effective_chat.id

    # 用一个游离的 Server 对象做连通性测试,无需先写库
    trial = Server(
        name=data["name"],
        host=data["host"],
        port=data["port"],
        username=data["username"],
        auth_type=data["auth_type"],
        credential=data["credential"],
        key_passphrase=data.get("key_passphrase"),
        status="active",
        v2node_installed=False,
    )
    # 连通性测试 + 安装检测 + 读远程节点,原本要连三次;这里一条连接跑完,
    # 建连失败即等价于连通性测试不通过。
    remote_nodes: list | None = None
    v2node_installed = False
    try:
        async with ctx.ssh.connection(trial) as conn:
            try:
                check = await conn.run(trial, INSTALLED_CHECK_CMD)
                v2node_installed = check.stdout.strip() == "installed"
            except SSHError:
                v2node_installed = False
            if v2node_installed:
                try:
                    remote_nodes = await read_remote_nodes(conn, trial)
                except Exception:  # noqa: BLE001
                    log.exception("导入节点失败,服务器 %s", data["name"])
                    remote_nodes = None
    except SSHError as exc:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"SSH 连通性测试失败,服务器未登记:\n{exc}",
            reply_markup=main_menu_kb(),
        )
        context.user_data.pop(KEY, None)
        return ConversationHandler.END

    # 写库,并把刚读到的远程节点导入
    async with crud.session() as s:
        server = await crud.create_server(
            s,
            name=data["name"],
            host=data["host"],
            port=data["port"],
            username=data["username"],
            auth_type=data["auth_type"],
            credential=data["credential"],
            key_passphrase=data.get("key_passphrase"),
            # 首次连接时 TOFU 记下的指纹,建档时一并存下
            host_key=trial.host_key,
            v2node_installed=v2node_installed,
        )
        imported = 0
        if v2node_installed:
            if remote_nodes is None:
                imported = -1  # 标记为导入失败
            else:
                items = [
                    {
                        "api_host": n.api_host,
                        "node_id": n.node_id,
                        "api_key": ctx.crypto.encrypt(n.api_key),
                        "timeout": n.timeout,
                    }
                    for n in remote_nodes
                ]
                imported = await crud.replace_nodes(s, server.id, items)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server.id,
            action="server.add",
            result="success",
            detail=f"name={server.name}, v2node_installed={v2node_installed}, imported={imported}",
        )
        await s.commit()
        server_name = server.name

    if not v2node_installed:
        text = (
            f"✅ 服务器「{server_name}」已添加。\n"
            f"暂未检测到 v2node,可在 /server 对应菜单中选择「安装 v2node」。"
        )
    elif imported < 0:
        text = (
            f"✅ 服务器「{server_name}」已添加,但读取远程 v2node 节点失败。\n"
            f"稍后可在节点管理中点击「同步」重试。"
        )
    elif imported == 0:
        text = (
            f"✅ 服务器「{server_name}」已添加。\n"
            f"v2node 已安装,但暂未发现已配置的节点。可在节点管理中添加。"
        )
    else:
        text = f"✅ 服务器「{server_name}」已添加,已导入 {imported} 个节点。"

    await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=main_menu_kb(),
    )
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text(
        "已取消添加服务器。", reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("addserver", cmd_addserver),
            CallbackQueryHandler(cmd_addserver, pattern=f"^{CB_MENU_SRV_ADD}$"),
        ],
        states={
            NAME: [MessageHandler(NON_MENU_TEXT_FILTER, step_name)],
            HOST: [MessageHandler(NON_MENU_TEXT_FILTER, step_host)],
            PORT: [MessageHandler(NON_MENU_TEXT_FILTER, step_port)],
            USERNAME: [MessageHandler(NON_MENU_TEXT_FILTER, step_username)],
            AUTH_TYPE: [CallbackQueryHandler(step_auth_type, pattern=r"^auth:(key|password)$")],
            CREDENTIAL: [MessageHandler(NON_MENU_TEXT_FILTER, step_credential)],
            KEY_PASSPHRASE: [
                MessageHandler(NON_MENU_TEXT_FILTER, step_key_passphrase)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="addserver",
        persistent=False,
    )
    application.add_handler(conv)
