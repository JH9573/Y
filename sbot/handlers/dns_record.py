"""添加 / 编辑 DNS 记录对话流程。

字段流: 类型(按钮)→ 名称(text)→ 内容(text)→ TTL(按钮/text)
       → [MX] 优先级(text) → [A/AAAA/CNAME] proxied(按钮)→ 提交

编辑模式: account_id / zone_id / record_id 从 user_data["dns_ctx"] 取,
原值预填,每步可点「保留」按钮沿用。
添加模式: 不预填,提示用户从头输入。
"""
from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
)

from ..db import crud
from ..services.cloudflare_api import (
    CloudflareAPIError,
    PROXYABLE_TYPES,
    SUPPORTED_RECORD_TYPES,
    TTL_PRESETS,
    validate_priority,
    validate_record_content,
    validate_record_name,
    validate_ttl,
)
from .common import (
    ANY_MENU_TEXT_FILTER,
    CB_DNS_RECORD,
    CB_DNS_RECORD_ADD,
    CB_DNS_RECORD_EDIT,
    CB_DNS_RECORDS,
    NON_MENU_TEXT_FILTER,
    get_ctx,
)


log = logging.getLogger(__name__)


(
    TYPE,
    NAME,
    CONTENT,
    TTL,
    PRIORITY,
    PROXIED,
    CONFIRM,
) = range(7)

KEY = "dnsrec"

# 按钮 callback 前缀(仅在本对话内有效)
CB_TYPE = "dnsrec:t:"        # dnsrec:t:A
CB_TTL = "dnsrec:ttl:"       # dnsrec:ttl:3600
CB_PROXIED = "dnsrec:p:"     # dnsrec:p:1
CB_KEEP_NAME = "dnsrec:knm"
CB_KEEP_CONTENT = "dnsrec:kct"
CB_KEEP_TTL = "dnsrec:kttl"
CB_KEEP_PRIO = "dnsrec:kp"
CB_KEEP_PROXIED = "dnsrec:kpx"
CB_CONFIRM_OK = "dnsrec:ok"
CB_CONFIRM_CANCEL = "dnsrec:cancel"


# ---------- 工具 ----------

def _get_dns_ctx(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    raw = context.user_data.get("dns_ctx")
    if not isinstance(raw, dict):
        return None
    if not raw.get("account_id") or not raw.get("zone_id"):
        return None
    return raw


def _initial_data(
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    if record is None:
        return {}
    return {
        "type": record.get("type"),
        "name": record.get("name"),
        "content": record.get("content"),
        "ttl": record.get("ttl"),
        "proxied": bool(record.get("proxied")),
        "priority": record.get("priority"),
    }


# ---------- 入口 ----------

async def cb_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    dns_ctx = _get_dns_ctx(context)
    if dns_ctx is None:
        await query.edit_message_text(
            "会话已过期,请回到 DNS 账户列表重新进入。"
        )
        return ConversationHandler.END

    context.user_data[KEY] = {
        "mode": "add",
        "record_id": None,
        "initial": {},
        "values": {},
    }
    return await _prompt_type(update, context)


async def cb_edit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    dns_ctx = _get_dns_ctx(context)
    if dns_ctx is None:
        await query.edit_message_text(
            "会话已过期,请回到 DNS 账户列表重新进入。"
        )
        return ConversationHandler.END
    record_id = query.data.split(":", 1)[1]
    ctx = get_ctx(context)

    async with crud.session() as s:
        account = await crud.get_dns_account(s, dns_ctx["account_id"])
    if account is None:
        await query.edit_message_text("DNS 账户不存在。")
        return ConversationHandler.END

    try:
        record = await ctx.cloudflare.get_record(
            account, dns_ctx["zone_id"], record_id,
        )
    except CloudflareAPIError as exc:
        await query.edit_message_text(f"❌ 拉取记录失败:{exc}")
        return ConversationHandler.END

    context.user_data[KEY] = {
        "mode": "edit",
        "record_id": record_id,
        "initial": _initial_data(record),
        "values": {},
    }
    return await _prompt_type(update, context)


# ---------- 步骤: 类型 ----------

async def _prompt_type(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    rows = [
        [InlineKeyboardButton(
            t + (
                " (当前)"
                if data["mode"] == "edit" and data["initial"].get("type") == t
                else ""
            ),
            callback_data=f"{CB_TYPE}{t}",
        )]
        for t in SUPPORTED_RECORD_TYPES
    ]
    title = "选择记录类型:" if data["mode"] == "add" else "选择记录类型(可改):"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            title, reply_markup=InlineKeyboardMarkup(rows),
        )
    else:
        await update.effective_message.reply_text(
            title, reply_markup=InlineKeyboardMarkup(rows),
        )
    return TYPE


async def step_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    rtype = query.data.split(":", 2)[2]
    if rtype not in SUPPORTED_RECORD_TYPES:
        await query.edit_message_text("不支持的记录类型。")
        return ConversationHandler.END
    data = context.user_data[KEY]
    data["values"]["type"] = rtype
    return await _prompt_name(update, context)


# ---------- 步骤: 名称 ----------

async def _prompt_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    rtype = data["values"]["type"]
    cur = data["initial"].get("name")
    lines = [
        f"类型: {rtype}",
        "",
        "请输入记录名称(子域名,根域名用 @):",
    ]
    if cur:
        lines.append(f"当前: {cur}")
    kb = (
        InlineKeyboardMarkup([[InlineKeyboardButton(
            f"保留 ({cur})", callback_data=CB_KEEP_NAME,
        )]])
        if cur and data["mode"] == "edit"
        else None
    )
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)
    return NAME


async def step_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        name = data["initial"]["name"]
    else:
        raw = update.message.text or ""
        try:
            name = validate_record_name(raw)
        except CloudflareAPIError as exc:
            await update.message.reply_text(f"{exc},请重新输入:")
            return NAME
    data["values"]["name"] = name
    return await _prompt_content(update, context)


# ---------- 步骤: 内容 ----------

async def _prompt_content(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    rtype = data["values"]["type"]
    cur = data["initial"].get("content")

    hint = {
        "A": "IPv4 地址,例如 1.2.3.4",
        "AAAA": "IPv6 地址,例如 2606:4700::1",
        "CNAME": "目标主机名,例如 target.example.com",
        "TXT": "任意文本,例如 v=spf1 ~all",
        "MX": "邮件服务器主机名,例如 mail.example.com",
    }.get(rtype, "记录内容")

    lines = [f"请输入内容({hint}):"]
    if cur:
        lines.append(f"当前: {cur}")
    kb = (
        InlineKeyboardMarkup([[InlineKeyboardButton(
            "保留(当前内容)", callback_data=CB_KEEP_CONTENT,
        )]])
        if cur and data["mode"] == "edit"
        else None
    )
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)
    return CONTENT


async def step_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    rtype = data["values"]["type"]
    if update.callback_query:
        await update.callback_query.answer()
        content = data["initial"]["content"]
    else:
        raw = update.message.text or ""
        try:
            content = validate_record_content(rtype, raw)
        except CloudflareAPIError as exc:
            await update.message.reply_text(f"{exc},请重新输入:")
            return CONTENT
    data["values"]["content"] = content
    return await _prompt_ttl(update, context)


# ---------- 步骤: TTL ----------

async def _prompt_ttl(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    cur = data["initial"].get("ttl")
    rows: list[list[InlineKeyboardButton]] = []
    for v, label in TTL_PRESETS:
        mark = " ✅" if cur == v else ""
        rows.append([InlineKeyboardButton(
            f"{label}{mark}", callback_data=f"{CB_TTL}{v}",
        )])
    if cur and data["mode"] == "edit":
        rows.append([InlineKeyboardButton(
            f"保留 ({cur})", callback_data=CB_KEEP_TTL,
        )])

    lines = [
        "选择 TTL 或直接输入秒数(1 = 自动,其它范围 60-86400):",
    ]
    if cur:
        lines.append(f"当前: {cur}")
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(rows),
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(rows),
        )
    return TTL


async def step_ttl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == CB_KEEP_TTL:
            ttl = data["initial"]["ttl"]
        else:
            ttl = int(query.data.split(":", 2)[2])
    else:
        raw = update.message.text or ""
        try:
            ttl = validate_ttl(raw)
        except CloudflareAPIError as exc:
            await update.message.reply_text(f"{exc},请重新输入:")
            return TTL
    data["values"]["ttl"] = ttl

    rtype = data["values"]["type"]
    if rtype == "MX":
        return await _prompt_priority(update, context)
    if rtype in PROXYABLE_TYPES:
        return await _prompt_proxied(update, context)
    return await _show_confirm(update, context)


# ---------- 步骤: 优先级(仅 MX) ----------

async def _prompt_priority(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    cur = data["initial"].get("priority")
    lines = ["请输入 MX 优先级(整数 0-65535,数字越小越优先):"]
    if cur is not None:
        lines.append(f"当前: {cur}")
    kb = (
        InlineKeyboardMarkup([[InlineKeyboardButton(
            f"保留 ({cur})", callback_data=CB_KEEP_PRIO,
        )]])
        if cur is not None and data["mode"] == "edit"
        else None
    )
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)
    return PRIORITY


async def step_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        await update.callback_query.answer()
        prio = data["initial"]["priority"]
    else:
        raw = update.message.text or ""
        try:
            prio = validate_priority(raw)
        except CloudflareAPIError as exc:
            await update.message.reply_text(f"{exc},请重新输入:")
            return PRIORITY
    data["values"]["priority"] = prio
    return await _show_confirm(update, context)


# ---------- 步骤: proxied(仅 A/AAAA/CNAME) ----------

async def _prompt_proxied(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    cur = data["initial"].get("proxied")
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "☁️ Proxied" + (" ✅" if cur is True else ""),
                callback_data=f"{CB_PROXIED}1",
            ),
            InlineKeyboardButton(
                "🌫 仅 DNS" + (" ✅" if cur is False else ""),
                callback_data=f"{CB_PROXIED}0",
            ),
        ],
    ]
    if data["mode"] == "edit":
        rows.append([InlineKeyboardButton(
            f"保留 ({'Proxied' if cur else '仅 DNS'})",
            callback_data=CB_KEEP_PROXIED,
        )])
    text = "是否启用 Cloudflare 代理(橙云)?"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(rows),
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(rows),
        )
    return PROXIED


async def step_proxied(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]
    if query.data == CB_KEEP_PROXIED:
        proxied = bool(data["initial"].get("proxied"))
    else:
        proxied = query.data.endswith(":1")
    data["values"]["proxied"] = proxied
    return await _show_confirm(update, context)


# ---------- 确认提交 ----------

async def _show_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    v = data["values"]
    rtype = v["type"]

    lines = ["请确认下列记录:", ""]
    lines.append(f"类型: {rtype}")
    lines.append(f"名称: {v['name']}")
    lines.append(f"内容: {v['content']}")
    lines.append(f"TTL: {v['ttl']}")
    if rtype == "MX":
        lines.append(f"优先级: {v.get('priority')}")
    if rtype in PROXYABLE_TYPES:
        lines.append(f"代理: {'☁️ Proxied' if v.get('proxied') else '🌫 仅 DNS'}")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 提交", callback_data=CB_CONFIRM_OK),
        InlineKeyboardButton("❌ 取消", callback_data=CB_CONFIRM_CANCEL),
    ]])
    text = "\n".join(lines)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)
    return CONFIRM


def _build_payload(values: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": values["type"],
        "name": values["name"],
        "content": values["content"],
        "ttl": values["ttl"],
    }
    if values["type"] in PROXYABLE_TYPES:
        body["proxied"] = bool(values.get("proxied"))
    if values["type"] == "MX":
        body["priority"] = values.get("priority")
    return body


async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == CB_CONFIRM_CANCEL:
        context.user_data.pop(KEY, None)
        await query.edit_message_text("已取消。")
        return ConversationHandler.END

    data = context.user_data[KEY]
    dns_ctx = _get_dns_ctx(context)
    if dns_ctx is None:
        context.user_data.pop(KEY, None)
        await query.edit_message_text("会话已过期,提交失败。请重新进入再试。")
        return ConversationHandler.END
    ctx = get_ctx(context)

    async with crud.session() as s:
        account = await crud.get_dns_account(s, dns_ctx["account_id"])
    if account is None:
        context.user_data.pop(KEY, None)
        await query.edit_message_text("DNS 账户不存在。")
        return ConversationHandler.END

    body = _build_payload(data["values"])
    is_edit = data["mode"] == "edit"
    action = "dns.record.edit" if is_edit else "dns.record.add"
    await query.edit_message_text(
        "正在提交…" if not is_edit else "正在更新…"
    )

    try:
        if is_edit:
            new_record = await ctx.cloudflare.update_record(
                account, dns_ctx["zone_id"], data["record_id"], body,
            )
        else:
            new_record = await ctx.cloudflare.create_record(
                account, dns_ctx["zone_id"], body,
            )
        ok, msg = True, "✅ 成功"
        new_id = str(new_record.get("id") or data["record_id"] or "")
    except CloudflareAPIError as exc:
        ok, msg = False, f"❌ 失败:{exc}"
        new_id = data["record_id"] or ""

    async with crud.session() as s:
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action=action,
            result="success" if ok else "failed",
            detail=(
                f"account_id={dns_ctx['account_id']}, "
                f"zone_id={dns_ctx['zone_id']}, "
                f"type={body.get('type')}, name={body.get('name')}: {msg}"
            ),
        )
        await s.commit()

    if ok:
        target = (
            f"{CB_DNS_RECORD}{new_id}" if new_id else f"{CB_DNS_RECORDS}1"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 查看记录", callback_data=target),
            InlineKeyboardButton(
                "⬅ 返回列表", callback_data=f"{CB_DNS_RECORDS}1",
            ),
        ]])
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "⬅ 返回列表", callback_data=f"{CB_DNS_RECORDS}1",
        )]])
    await query.edit_message_text(msg, reply_markup=kb)
    context.user_data.pop(KEY, None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(KEY, None)
    await update.effective_message.reply_text("已取消。")
    return ConversationHandler.END


def register(application, ctx) -> None:
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_add_entry, pattern=f"^{CB_DNS_RECORD_ADD}$"),
            CallbackQueryHandler(
                cb_edit_entry, pattern=f"^{CB_DNS_RECORD_EDIT}[0-9a-f]+$",
            ),
        ],
        states={
            TYPE: [CallbackQueryHandler(step_type, pattern=f"^{CB_TYPE}")],
            NAME: [
                CallbackQueryHandler(step_name, pattern=f"^{CB_KEEP_NAME}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_name),
            ],
            CONTENT: [
                CallbackQueryHandler(
                    step_content, pattern=f"^{CB_KEEP_CONTENT}$",
                ),
                MessageHandler(NON_MENU_TEXT_FILTER, step_content),
            ],
            TTL: [
                CallbackQueryHandler(
                    step_ttl, pattern=f"^{CB_TTL}\\d+$",
                ),
                CallbackQueryHandler(step_ttl, pattern=f"^{CB_KEEP_TTL}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_ttl),
            ],
            PRIORITY: [
                CallbackQueryHandler(step_priority, pattern=f"^{CB_KEEP_PRIO}$"),
                MessageHandler(NON_MENU_TEXT_FILTER, step_priority),
            ],
            PROXIED: [
                CallbackQueryHandler(
                    step_proxied, pattern=f"^({CB_PROXIED}[01]|{CB_KEEP_PROXIED})$",
                ),
            ],
            CONFIRM: [
                CallbackQueryHandler(
                    step_confirm,
                    pattern=f"^({CB_CONFIRM_OK}|{CB_CONFIRM_CANCEL})$",
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(ANY_MENU_TEXT_FILTER, cmd_cancel),
        ],
        name="dnsrec",
        persistent=False,
    )
    application.add_handler(conv)
