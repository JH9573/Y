"""协议特有的步骤:cipher(shadowsocks)、flow(vless)、上下行带宽(hysteria2)。"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .options import (
    CIPHER, CIPHER_OPTIONS, DOWN_MBPS, DOWN_MBPS_SKIP_CB, FLOW,
    FLOW_OPTIONS, FLOW_PICK_CB, KEEP_CB, KEY, UP_MBPS, UP_MBPS_SKIP_CB,
)
from .state import _advance, _is_edit, _reply, prompt_for

# ---------- step: CIPHER ----------

@prompt_for("cipher")
async def _prompt_cipher(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    rows = [
        [InlineKeyboardButton(c, callback_data=f"pnlsave:c:{c}")]
        for c in CIPHER_OPTIONS
    ]
    if _is_edit(context):
        current = data["initial"].get("cipher", "aes-128-gcm")
        rows.append([
            InlineKeyboardButton(f"保留 ({current})", callback_data=KEEP_CB),
        ])
    await _reply(
        update,
        "请选择加密方式 cipher:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return CIPHER


async def step_cipher(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]
    if query.data == KEEP_CB:
        cipher = str(data["initial"].get("cipher", "aes-128-gcm"))
    else:
        cipher = query.data.split(":", 2)[2]
    if cipher not in CIPHER_OPTIONS:
        await query.message.reply_text("无效的加密方式,请重新选择:")
        return CIPHER
    data["values"]["cipher"] = cipher
    return await _advance("cipher", update, context)
# ---------- step: FLOW (vless) ----------

@prompt_for("flow")
async def _prompt_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    rows = [[
        InlineKeyboardButton(
            label, callback_data=f"{FLOW_PICK_CB}{'vision' if value else 'none'}"
        )
        for value, label in FLOW_OPTIONS
    ]]
    if _is_edit(context):
        current = data["initial"].get("flow") or "(无)"
        rows.append([
            InlineKeyboardButton(f"保留 ({current})", callback_data=KEEP_CB),
        ])
    await _reply(
        update, "请选择 VLESS flow:", reply_markup=InlineKeyboardMarkup(rows)
    )
    return FLOW


async def step_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]
    if query.data == KEEP_CB:
        flow = str(data["initial"].get("flow") or "")
    else:
        token = query.data.split(":", 2)[2]
        flow = "xtls-rprx-vision" if token == "vision" else ""
    data["values"]["flow"] = flow
    return await _advance("flow", update, context)


# ---------- step: UP_MBPS / DOWN_MBPS (hysteria2) ----------

def _parse_mbps(text: str) -> float | None:
    try:
        v = float(text)
    except ValueError:
        return None
    if v < 0:
        return None
    return v


@prompt_for("up_mbps")
async def _prompt_up_mbps(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    rows = [[InlineKeyboardButton("跳过", callback_data=UP_MBPS_SKIP_CB)]]
    if _is_edit(context):
        val = data["initial"].get("up_mbps")
        label = f"保留 ({val})" if val not in (None, "") else "保留 (空)"
        rows.append([InlineKeyboardButton(label, callback_data=KEEP_CB)])
    await _reply(
        update,
        "请输入服务端上行带宽 up_mbps(数字,单位 Mbps),或点「跳过」不带:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return UP_MBPS


async def step_up_mbps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == KEEP_CB:
            val = data["initial"].get("up_mbps")
            if val not in (None, ""):
                data["values"]["up_mbps"] = val
        # 跳过:不写 values,payload 不带该字段
        return await _advance("up_mbps", update, context)
    mbps = _parse_mbps((update.message.text or "").strip())
    if mbps is None:
        await update.message.reply_text("up_mbps 必须是 ≥0 的数字,请重新输入:")
        return UP_MBPS
    data["values"]["up_mbps"] = mbps
    return await _advance("up_mbps", update, context)


@prompt_for("down_mbps")
async def _prompt_down_mbps(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    rows = [[InlineKeyboardButton("跳过", callback_data=DOWN_MBPS_SKIP_CB)]]
    if _is_edit(context):
        val = data["initial"].get("down_mbps")
        label = f"保留 ({val})" if val not in (None, "") else "保留 (空)"
        rows.append([InlineKeyboardButton(label, callback_data=KEEP_CB)])
    await _reply(
        update,
        "请输入服务端下行带宽 down_mbps(数字,单位 Mbps),或点「跳过」不带:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return DOWN_MBPS


async def step_down_mbps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == KEEP_CB:
            val = data["initial"].get("down_mbps")
            if val not in (None, ""):
                data["values"]["down_mbps"] = val
        return await _advance("down_mbps", update, context)
    mbps = _parse_mbps((update.message.text or "").strip())
    if mbps is None:
        await update.message.reply_text("down_mbps 必须是 ≥0 的数字,请重新输入:")
        return DOWN_MBPS
    data["values"]["down_mbps"] = mbps
    return await _advance("down_mbps", update, context)
