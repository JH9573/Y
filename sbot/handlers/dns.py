"""DNS 账户 / zones / records 的只读浏览与删除。

- 账户列表 / 详情 / 删除 走本地库
- zones / records 列表与详情实时调 Cloudflare API,不本地缓存
- 因 Cloudflare zone_id / record_id 是 32 字符 hex,无法把
  (account_id, zone_id, record_id) 三元组塞进 64 字节的 callback_data。
  约定:进入 zone 时把 (account_id, zone_id, zone_name) 存到
  context.user_data["dns_ctx"];记录级 callback 只带 record_id。
  会话过期(进程重启或用户清掉)时提示重新进入。
"""
from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from ..db import crud
from ..services.cloudflare_api import (
    CloudflareAPIError,
    PROXYABLE_TYPES,
    ttl_label,
)
from .common import (
    CB_BACK_DNS_LIST,
    CB_DEL_DNS_ACCOUNT,
    CB_DEL_DNS_ACCOUNT_OK,
    CB_DNS_ACCOUNT,
    CB_DNS_RECORD,
    CB_DNS_RECORD_ADD,
    CB_DNS_RECORD_DEL,
    CB_DNS_RECORD_DEL_OK,
    CB_DNS_RECORD_EDIT,
    CB_DNS_RECORDS,
    CB_DNS_ZONE,
    CB_DNS_ZONES,
    CB_EDIT_DNS_ACCOUNT,
    get_ctx,
    truncate,
)


log = logging.getLogger(__name__)


ZONES_PER_PAGE = 20
RECORDS_PER_PAGE = 10


# ---------- 账户列表 ----------

async def cmd_dns_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with crud.session() as s:
        accounts = await crud.list_dns_accounts(s)

    if not accounts:
        await update.effective_message.reply_text(
            "当前没有已登记的 DNS 账户。点「➕ 添加账户」或发送 /adddns 添加。"
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"[{a.provider}] {a.name}",
            callback_data=f"{CB_DNS_ACCOUNT}{a.id}",
        )]
        for a in accounts
    ]
    await update.effective_message.reply_text(
        "选择一个 DNS 账户:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_back_dns_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    async with crud.session() as s:
        accounts = await crud.list_dns_accounts(s)
    if not accounts:
        await query.edit_message_text("当前没有已登记的 DNS 账户。")
        return
    buttons = [
        [InlineKeyboardButton(
            f"[{a.provider}] {a.name}",
            callback_data=f"{CB_DNS_ACCOUNT}{a.id}",
        )]
        for a in accounts
    ]
    await query.edit_message_text(
        "选择一个 DNS 账户:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ---------- 账户详情 ----------

async def cb_open_account(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    account_id = int(query.data.split(":", 1)[1])
    # 进入账户时清理上一级记录上下文
    context.user_data.pop("dns_ctx", None)
    await _render_account_menu(update, context, account_id)


async def _render_account_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    account_id: int,
    *,
    banner: str | None = None,
) -> None:
    query = update.callback_query
    async with crud.session() as s:
        account = await crud.get_dns_account(s, account_id)
    if account is None:
        await query.edit_message_text("DNS 账户不存在(可能已被删除)。")
        return

    header_lines: list[str] = []
    if banner:
        header_lines.append(banner)
        header_lines.append("")
    header_lines.extend([
        f"DNS 账户「{account.name}」",
        f"服务商: {account.provider}",
    ])
    if account.email:
        header_lines.append(f"邮箱: {account.email}")
    header_lines.append("API Token: 已加密存储")

    kb = [
        [InlineKeyboardButton(
            "📋 域名列表",
            callback_data=f"{CB_DNS_ZONES}{account_id}:1",
        )],
        [
            InlineKeyboardButton(
                "✏️ 编辑账户",
                callback_data=f"{CB_EDIT_DNS_ACCOUNT}{account_id}",
            ),
            InlineKeyboardButton(
                "🗑 删除账户",
                callback_data=f"{CB_DEL_DNS_ACCOUNT}{account_id}",
            ),
        ],
        [InlineKeyboardButton("⬅ 返回列表", callback_data=CB_BACK_DNS_LIST)],
    ]
    await query.edit_message_text(
        "\n".join(header_lines),
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ---------- 删除账户 ----------

async def cb_delete_account_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    account_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        account = await crud.get_dns_account(s, account_id)
    if account is None:
        await query.edit_message_text("DNS 账户不存在。")
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "确认删除",
                callback_data=f"{CB_DEL_DNS_ACCOUNT_OK}{account_id}",
            ),
            InlineKeyboardButton(
                "取消", callback_data=f"{CB_DNS_ACCOUNT}{account_id}",
            ),
        ],
    ])
    await query.edit_message_text(
        f"⚠️ 确认从 bot 中删除 DNS 账户「{account.name}」?\n"
        f"该操作只从 bot 移除登记,不会触及 {account.provider} 上的任何记录。",
        reply_markup=kb,
    )


async def cb_delete_account_do(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    account_id = int(query.data.split(":", 1)[1])
    async with crud.session() as s:
        account = await crud.get_dns_account(s, account_id)
        if account is None:
            await query.edit_message_text("DNS 账户不存在。")
            return
        name = account.name
        await crud.delete_dns_account(s, account_id)
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="dns.account.delete",
            result="success",
            detail=f"account_id={account_id}, name={name}",
        )
        await s.commit()
    context.user_data.pop("dns_ctx", None)
    await query.edit_message_text(f"已从 bot 删除 DNS 账户「{name}」。")


# ---------- zones 列表 ----------

async def cb_list_zones(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    account_id_s, page_s = payload.split(":", 1)
    await _render_zones(update, context, int(account_id_s), int(page_s))


async def _render_zones(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    account_id: int,
    page: int,
    *,
    banner: str | None = None,
) -> None:
    query = update.callback_query
    ctx = get_ctx(context)
    async with crud.session() as s:
        account = await crud.get_dns_account(s, account_id)
    if account is None:
        await query.edit_message_text("DNS 账户不存在。")
        return

    await query.edit_message_text(
        f"正在从 Cloudflare 拉取「{account.name}」的域名列表…"
    )
    try:
        zones, info = await ctx.cloudflare.list_zones(
            account, page=page, per_page=ZONES_PER_PAGE,
        )
    except CloudflareAPIError as exc:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "⬅ 返回账户", callback_data=f"{CB_DNS_ACCOUNT}{account_id}",
        )]])
        await query.edit_message_text(f"❌ 拉取失败:{exc}", reply_markup=kb)
        return

    total_pages = int(info.get("total_pages") or 1)
    total_count = info.get("total_count")

    lines: list[str] = []
    if banner:
        lines.append(banner)
        lines.append("")
    lines.append(
        f"账户「{account.name}」的域名"
        + (f"(共 {total_count})" if total_count is not None else "")
    )
    if total_pages > 1:
        lines.append(f"第 {page} / {total_pages} 页")

    rows: list[list[InlineKeyboardButton]] = []
    if not zones:
        lines.append("")
        lines.append("(此页无 zone)")
    else:
        for z in zones:
            zid = str(z.get("id") or "")
            zname = str(z.get("name") or "(unnamed)")
            zstatus = str(z.get("status") or "")
            label = f"{zname} ({zstatus})" if zstatus else zname
            rows.append([InlineKeyboardButton(
                label,
                callback_data=f"{CB_DNS_ZONE}{account_id}:{zid}",
            )])

    # 分页按钮
    pager: list[InlineKeyboardButton] = []
    if page > 1:
        pager.append(InlineKeyboardButton(
            "⬅ 上一页",
            callback_data=f"{CB_DNS_ZONES}{account_id}:{page - 1}",
        ))
    if page < total_pages:
        pager.append(InlineKeyboardButton(
            "下一页 ➡",
            callback_data=f"{CB_DNS_ZONES}{account_id}:{page + 1}",
        ))
    if pager:
        rows.append(pager)
    rows.append([InlineKeyboardButton(
        "⬅ 返回账户", callback_data=f"{CB_DNS_ACCOUNT}{account_id}",
    )])

    await query.edit_message_text(
        truncate("\n".join(lines)),
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ---------- 进入 zone(展示记录列表第 1 页)----------

async def cb_open_zone(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, payload = query.data.split(":", 1)
    account_id_s, zone_id = payload.split(":", 1)
    account_id = int(account_id_s)
    ctx = get_ctx(context)

    # 取 zone 详情拿名字,顺便验证 zone 归属;实际记录列表在 _render_records 拉
    async with crud.session() as s:
        account = await crud.get_dns_account(s, account_id)
    if account is None:
        await query.edit_message_text("DNS 账户不存在。")
        return

    await query.edit_message_text("正在加载 zone…")
    try:
        zone = await ctx.cloudflare.get_zone(account, zone_id)
    except CloudflareAPIError as exc:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "⬅ 返回域名列表",
            callback_data=f"{CB_DNS_ZONES}{account_id}:1",
        )]])
        await query.edit_message_text(f"❌ 加载 zone 失败:{exc}", reply_markup=kb)
        return

    zone_name = str(zone.get("name") or zone_id)
    # 写入导航上下文,后续记录级 callback 复用
    context.user_data["dns_ctx"] = {
        "account_id": account_id,
        "zone_id": zone_id,
        "zone_name": zone_name,
    }
    await _render_records(update, context, page=1)


# ---------- 记录列表 ----------

def _get_dns_ctx(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    raw = context.user_data.get("dns_ctx")
    if not isinstance(raw, dict):
        return None
    if not raw.get("account_id") or not raw.get("zone_id"):
        return None
    return raw


async def _session_lost(query) -> None:
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "⬅ 返回 DNS 账户列表", callback_data=CB_BACK_DNS_LIST,
    )]])
    await query.edit_message_text(
        "会话已过期(进程可能已重启)。请回到 DNS 账户列表重新进入。",
        reply_markup=kb,
    )


async def cb_records_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":", 1)[1])
    await _render_records(update, context, page=page)


async def _render_records(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int,
    banner: str | None = None,
) -> None:
    query = update.callback_query
    dns_ctx = _get_dns_ctx(context)
    if dns_ctx is None:
        await _session_lost(query)
        return
    account_id = dns_ctx["account_id"]
    zone_id = dns_ctx["zone_id"]
    zone_name = dns_ctx.get("zone_name") or zone_id
    ctx = get_ctx(context)

    async with crud.session() as s:
        account = await crud.get_dns_account(s, account_id)
    if account is None:
        await query.edit_message_text("DNS 账户不存在。")
        return

    await query.edit_message_text(f"正在拉取 {zone_name} 的记录(第 {page} 页)…")
    try:
        records, info = await ctx.cloudflare.list_records(
            account, zone_id, page=page, per_page=RECORDS_PER_PAGE,
        )
    except CloudflareAPIError as exc:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "⬅ 返回域名列表",
            callback_data=f"{CB_DNS_ZONES}{account_id}:1",
        )]])
        await query.edit_message_text(f"❌ 拉取失败:{exc}", reply_markup=kb)
        return

    total_pages = int(info.get("total_pages") or 1)
    total_count = info.get("total_count")

    lines: list[str] = []
    if banner:
        lines.append(banner)
        lines.append("")
    lines.append(
        f"{zone_name} 的 DNS 记录"
        + (f"(共 {total_count})" if total_count is not None else "")
    )
    if total_pages > 1:
        lines.append(f"第 {page} / {total_pages} 页")
    lines.append("图例: ☁️ 已代理 / ❌ 仅 DNS")

    rows: list[list[InlineKeyboardButton]] = []
    if not records:
        lines.append("")
        lines.append("(此页无记录) 点「➕ 添加记录」新建。")
    else:
        for r in records:
            rid = str(r.get("id") or "")
            rtype = str(r.get("type") or "?")
            rname = str(r.get("name") or "?")
            short_name = _shorten_name(rname, zone_name)
            proxied = bool(r.get("proxied"))
            badge = "☁️" if proxied else ("❌" if rtype in PROXYABLE_TYPES else "")
            label = f"{rtype} {short_name}".strip()
            if badge:
                label = f"{badge} {label}"
            rows.append([InlineKeyboardButton(
                label, callback_data=f"{CB_DNS_RECORD}{rid}",
            )])

    pager: list[InlineKeyboardButton] = []
    if page > 1:
        pager.append(InlineKeyboardButton(
            "⬅ 上一页", callback_data=f"{CB_DNS_RECORDS}{page - 1}",
        ))
    if page < total_pages:
        pager.append(InlineKeyboardButton(
            "下一页 ➡", callback_data=f"{CB_DNS_RECORDS}{page + 1}",
        ))
    if pager:
        rows.append(pager)
    rows.append([
        InlineKeyboardButton("➕ 添加记录", callback_data=CB_DNS_RECORD_ADD),
        InlineKeyboardButton(
            "⬅ 返回域名",
            callback_data=f"{CB_DNS_ZONES}{account_id}:1",
        ),
    ])

    await query.edit_message_text(
        truncate("\n".join(lines)),
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _shorten_name(record_name: str, zone_name: str) -> str:
    if record_name == zone_name:
        return "@"
    suffix = "." + zone_name
    if record_name.endswith(suffix):
        return record_name[: -len(suffix)]
    return record_name


# ---------- 记录详情 ----------

async def cb_record_detail(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    record_id = query.data.split(":", 1)[1]
    await _render_record(update, context, record_id=record_id)


async def _render_record(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    record_id: str,
    banner: str | None = None,
) -> None:
    query = update.callback_query
    dns_ctx = _get_dns_ctx(context)
    if dns_ctx is None:
        await _session_lost(query)
        return
    account_id = dns_ctx["account_id"]
    zone_id = dns_ctx["zone_id"]
    ctx = get_ctx(context)

    async with crud.session() as s:
        account = await crud.get_dns_account(s, account_id)
    if account is None:
        await query.edit_message_text("DNS 账户不存在。")
        return

    try:
        record = await ctx.cloudflare.get_record(account, zone_id, record_id)
    except CloudflareAPIError as exc:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "⬅ 返回记录列表", callback_data=f"{CB_DNS_RECORDS}1",
        )]])
        await query.edit_message_text(f"❌ 拉取记录失败:{exc}", reply_markup=kb)
        return

    text = _format_record(record, banner=banner)
    kb_rows = [
        [
            InlineKeyboardButton(
                "✏️ 编辑",
                callback_data=f"{CB_DNS_RECORD_EDIT}{record_id}",
            ),
            InlineKeyboardButton(
                "🗑 删除",
                callback_data=f"{CB_DNS_RECORD_DEL}{record_id}",
            ),
        ],
        [InlineKeyboardButton(
            "⬅ 返回记录列表", callback_data=f"{CB_DNS_RECORDS}1",
        )],
    ]
    await query.edit_message_text(
        truncate(text), reply_markup=InlineKeyboardMarkup(kb_rows),
    )


def _format_record(record: dict[str, Any], *, banner: str | None = None) -> str:
    lines: list[str] = []
    if banner:
        lines.append(banner)
        lines.append("")
    rtype = str(record.get("type") or "?")
    name = str(record.get("name") or "?")
    content = str(record.get("content") or "")
    ttl = record.get("ttl")
    proxied = record.get("proxied")
    prio = record.get("priority")

    lines.append(f"DNS 记录 {rtype} {name}")
    lines.append("")
    lines.append(f"类型: {rtype}")
    lines.append(f"名称: {name}")
    lines.append(f"内容: {content}")
    lines.append(f"TTL: {ttl_label(ttl) if isinstance(ttl, int) else ttl}")
    if rtype in PROXYABLE_TYPES:
        lines.append(f"代理: {'☁️ 已代理' if proxied else '❌ 仅 DNS'}")
    if rtype == "MX" and prio is not None:
        lines.append(f"优先级: {prio}")
    comment = record.get("comment")
    if comment:
        lines.append(f"备注: {comment}")
    return "\n".join(lines)


# ---------- 删除记录 ----------

async def cb_record_delete_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    record_id = query.data.split(":", 1)[1]
    dns_ctx = _get_dns_ctx(context)
    if dns_ctx is None:
        await _session_lost(query)
        return
    ctx = get_ctx(context)

    async with crud.session() as s:
        account = await crud.get_dns_account(s, dns_ctx["account_id"])
    if account is None:
        await query.edit_message_text("DNS 账户不存在。")
        return

    try:
        record = await ctx.cloudflare.get_record(
            account, dns_ctx["zone_id"], record_id,
        )
    except CloudflareAPIError as exc:
        await query.edit_message_text(f"❌ 拉取记录失败:{exc}")
        return

    rtype = record.get("type")
    name = record.get("name")
    content = record.get("content")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "确认删除",
            callback_data=f"{CB_DNS_RECORD_DEL_OK}{record_id}",
        ),
        InlineKeyboardButton(
            "取消", callback_data=f"{CB_DNS_RECORD}{record_id}",
        ),
    ]])
    await query.edit_message_text(
        f"⚠️ 确认从 Cloudflare 删除该记录?\n"
        f"{rtype} {name} → {content}\n"
        f"该操作不可撤销。",
        reply_markup=kb,
    )


async def cb_record_delete_do(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    record_id = query.data.split(":", 1)[1]
    dns_ctx = _get_dns_ctx(context)
    if dns_ctx is None:
        await _session_lost(query)
        return
    ctx = get_ctx(context)

    async with crud.session() as s:
        account = await crud.get_dns_account(s, dns_ctx["account_id"])
    if account is None:
        await query.edit_message_text("DNS 账户不存在。")
        return

    try:
        await ctx.cloudflare.delete_record(
            account, dns_ctx["zone_id"], record_id,
        )
        ok, msg = True, "✅ 记录已删除"
    except CloudflareAPIError as exc:
        ok, msg = False, f"❌ 删除失败:{exc}"

    async with crud.session() as s:
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=None,
            action="dns.record.delete",
            result="success" if ok else "failed",
            detail=(
                f"account_id={dns_ctx['account_id']}, "
                f"zone_id={dns_ctx['zone_id']}, record_id={record_id}: {msg}"
            ),
        )
        await s.commit()

    if ok:
        await _render_records(update, context, page=1, banner=msg)
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "⬅ 返回记录详情", callback_data=f"{CB_DNS_RECORD}{record_id}",
        )]])
        await query.edit_message_text(msg, reply_markup=kb)


# ---------- 注册 ----------

def register(application, ctx) -> None:
    application.add_handler(CommandHandler("dns", cmd_dns_list))
    application.add_handler(
        CallbackQueryHandler(cb_back_dns_list, pattern=f"^{CB_BACK_DNS_LIST}$")
    )
    application.add_handler(
        CallbackQueryHandler(cb_open_account, pattern=f"^{CB_DNS_ACCOUNT}\\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_delete_account_confirm, pattern=f"^{CB_DEL_DNS_ACCOUNT}\\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_delete_account_do, pattern=f"^{CB_DEL_DNS_ACCOUNT_OK}\\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_list_zones, pattern=f"^{CB_DNS_ZONES}\\d+:\\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_open_zone, pattern=f"^{CB_DNS_ZONE}\\d+:[0-9a-f]+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_records_page, pattern=f"^{CB_DNS_RECORDS}\\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_record_detail, pattern=f"^{CB_DNS_RECORD}[0-9a-f]+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_record_delete_confirm,
            pattern=f"^{CB_DNS_RECORD_DEL}[0-9a-f]+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cb_record_delete_do,
            pattern=f"^{CB_DNS_RECORD_DEL_OK}[0-9a-f]+$",
        )
    )
