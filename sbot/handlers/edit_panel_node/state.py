"""流程编排与通用 helper。

各协议的字段顺序在 options._FLOW_BY_PROTOCOL 里,_advance 据此跳下一步:
它按字段名查 PROMPT_BY_FIELD,而这张表由各 steps_* 模块用 @prompt_for 登记。
拆包之后靠这层间接,steps_* 之间不需要互相 import。
"""
from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .options import KEEP_CB, KEY, _FLOW_BY_PROTOCOL

# ---------- 步骤登记表 ----------

# 字段名 -> 该字段的 _prompt_* 函数。由各 steps_* 模块在 import 时用
# @prompt_for 登记,_advance 只认这张表,于是各步骤模块彼此解耦
# (不然 steps_basic 要 import steps_tls,反过来也要,必然成环)。
PROMPT_BY_FIELD: dict[str, Any] = {}


def prompt_for(field: str):
    """把 _prompt_* 登记为某个字段的提示函数。"""

    def deco(fn):
        PROMPT_BY_FIELD[field] = fn
        return fn

    return deco


# ---------- 流程编排 ----------

def _flow_for(data: dict) -> list[str]:
    protocol = data["values"].get("protocol") or data["initial"].get("protocol") or "shadowsocks"
    return _FLOW_BY_PROTOCOL.get(protocol, _FLOW_BY_PROTOCOL["shadowsocks"])


async def _advance(
    after_field: str, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """跳到该协议下 after_field 的下一步。"""
    data = context.user_data[KEY]
    flow = _flow_for(data)
    try:
        idx = flow.index(after_field)
    except ValueError:
        idx = -1
    next_field = flow[idx + 1] if idx + 1 < len(flow) else "confirm"
    return await PROMPT_BY_FIELD[next_field](update, context)
async def _reply(
    update: Update,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """callback 上下文 edit 原消息;文本上下文回复新消息。"""
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=reply_markup
        )


def _keep_kb(value: Any) -> InlineKeyboardMarkup:
    """文本 state 的"保留当前值"按钮。"""
    label = f"保留 ({value})" if value not in (None, "") else "保留 (空)"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=KEEP_CB)]]
    )


def _is_edit(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data[KEY]["mode"] == "edit"
