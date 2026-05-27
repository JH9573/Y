"""端口体检展示 + 一键放行 v2node 监听端口。

`port_check_block` 供安装 / 加节点成功后调用,拼出体检文本 + 可选的「放行」按钮;
`cb_fw_open` 处理放行点击(重新体检后放行,避免 callback 携带端口)。
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..db import crud
from ..services import firewall
from .common import CB_FW_OPEN, CB_SERVER_PREFIX, get_ctx


log = logging.getLogger(__name__)

_CLOUD_NOTE = "⚠ 云厂商安全组在服务器之外,SSH 改不了,需自行在控制台放行。"


def _render_check(check: firewall.PortCheck) -> str:
    lines = ["🔍 端口检测"]
    if check.note:
        lines.append(f"(已跳过:{check.note})")
        return "\n".join(lines)
    if not check.listen_ports:
        lines.append("未发现 v2node 监听端口(服务可能尚未就绪,可稍后在节点菜单复查)。")
        return "\n".join(lines)

    listen_str = "、".join(p.label() for p in check.listen_ports)
    lines.append(f"监听: {listen_str}")
    if not (check.manager and check.active):
        lines.append("防火墙: 未检测到启用的 ufw / firewalld")
        lines.append("请自行确认上述端口已放行。")
    else:
        lines.append(f"防火墙: {check.manager} (active)")
        for lp in check.listen_ports:
            ok = check.allowed.get((lp.proto, lp.port))
            mark = "已放行 ✓" if ok else "未放行 ⚠"
            lines.append(f"  • {lp.label()} {mark}")
    lines.append(_CLOUD_NOTE)
    return "\n".join(lines)


async def port_check_block(
    ssh, server
) -> tuple[str, list[InlineKeyboardButton]]:
    """返回 (体检文本, 额外按钮行)。额外按钮里有未放行端口时含「放行」键。"""
    try:
        check = await firewall.check_ports(ssh, server)
    except Exception as exc:  # noqa: BLE001
        log.exception("端口体检失败 server=%s", getattr(server, "id", "?"))
        return f"🔍 端口检测\n(已跳过:{exc})", []

    text = _render_check(check)
    buttons: list[InlineKeyboardButton] = []
    if check.unallowed:
        buttons.append(InlineKeyboardButton(
            f"🔓 放行 {len(check.unallowed)} 个端口",
            callback_data=f"{CB_FW_OPEN}{server.id}",
        ))
    return text, buttons


async def cb_fw_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])
    ctx = get_ctx(context)

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        await query.edit_message_text("服务器不存在。")
        return

    await query.edit_message_text("正在放行端口…")
    manager, opened, failed = await firewall.open_unallowed(ctx.ssh, server)

    lines: list[str] = []
    if manager is None:
        lines.append("未检测到启用的 ufw / firewalld,无需放行。")
    elif not opened and not failed:
        lines.append("没有需要放行的端口(可能已全部放行)。")
    else:
        if opened:
            lines.append("✅ 已放行:" + "、".join(p.label() for p in opened))
        for lp, msg in failed:
            lines.append(f"❌ {lp.label()} 放行失败:{msg}")
    lines.append("")
    lines.append(_CLOUD_NOTE)

    async with crud.session() as s:
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server_id,
            action="firewall.open",
            result="success" if (opened and not failed) else "failed" if failed else "success",
            detail=(
                f"manager={manager}, opened="
                + ",".join(p.label() for p in opened)
                + "; failed="
                + ",".join(lp.label() for lp, _ in failed)
            ),
        )
        await s.commit()

    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "⬅ 返回服务器", callback_data=f"{CB_SERVER_PREFIX}{server_id}"
    )]])
    await query.edit_message_text("\n".join(lines), reply_markup=kb)


def register(application, ctx) -> None:
    application.add_handler(
        CallbackQueryHandler(cb_fw_open, pattern=rf"^{CB_FW_OPEN}\d+$")
    )
