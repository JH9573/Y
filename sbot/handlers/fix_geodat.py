"""【临时功能 — 后续删除】把 geoip.dat / geosite.dat 从配置目录补到运行目录。

历史上某些已安装的服务器只在配置目录 /etc/v2node 有这两个 .dat 文件,
运行目录 /usr/local/v2node 缺失。本按钮做一次性补齐:运行目录缺哪个就从
配置目录复制哪个,已存在则跳过。

⚠️ 这是过渡用的临时入口,等所有服务器都补齐后整文件删除。
删除时同时清理:
  - common.py 的 CB_FIX_GEODAT 常量
  - server.py v2node 菜单里的「🩹 补齐 geo 数据」按钮及其 import
  - main.py 的 fix_geodat.register(...) 调用
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ..core.ssh import SSHError
from ..db import crud
from ..services.v2node_install import CONFIG_DIR, INSTALL_DIR
from .common import CB_FIX_GEODAT, CB_V2NODE_MENU, get_ctx, truncate


log = logging.getLogger(__name__)


# 运行目录缺则从配置目录复制,已存在则跳过;配置目录也没有则报告。
_FIX_CMD = (
    f"mkdir -p {INSTALL_DIR} && "
    "for f in geoip.dat geosite.dat; do "
    f'  if [ -f "{INSTALL_DIR}/$f" ]; then echo "$f: 运行目录已存在,跳过"; '
    f'  elif [ -f "{CONFIG_DIR}/$f" ]; then '
    f'    cp -f "{CONFIG_DIR}/$f" "{INSTALL_DIR}/$f" && echo "$f: 已从配置目录复制到运行目录"; '
    f'  else echo "$f: 配置目录也缺失,无法补齐"; fi; '
    "done"
)


async def cb_fix_geodat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    server_id = int(query.data.split(":", 1)[1])
    ctx = get_ctx(context)

    async with crud.session() as s:
        server = await crud.get_server(s, server_id)
    if server is None:
        await query.edit_message_text("服务器不存在。")
        return

    await query.edit_message_text(f"在 {server.name} 上补齐 geo 数据文件…")

    try:
        result = await ctx.ssh.run(server, _FIX_CMD, timeout=30)
        success = result.ok
        output = result.combined or "(无输出)"
        result_text = (
            f"{server.name} · 补齐 geo 数据\n"
            f"exit={result.exit_status}\n"
            f"---\n{truncate(output)}"
        )
    except SSHError as exc:
        success = False
        result_text = f"{server.name} · 补齐 geo 数据失败:{exc}"

    async with crud.session() as s:
        await crud.add_log(
            s,
            user_id=update.effective_user.id,
            server_id=server_id,
            action="v2node.fix_geodat",
            result="success" if success else "failed",
            detail=truncate(result_text, 500),
        )
        await s.commit()

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "⬅ 返回 v2node 管理", callback_data=f"{CB_V2NODE_MENU}{server_id}"
        )]]
    )
    await query.edit_message_text(result_text, reply_markup=kb)


def register(application, ctx) -> None:
    application.add_handler(
        CallbackQueryHandler(cb_fix_geodat, pattern=f"^{CB_FIX_GEODAT}\\d+$")
    )
