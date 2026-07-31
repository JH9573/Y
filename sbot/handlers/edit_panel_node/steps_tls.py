"""TLS 及 tls_settings 的引导式子步骤(证书 / SNI / REALITY / ECH)。"""
from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .options import (
    CERT_MODE_CB, CERT_MODE_OPTIONS, CERT_MODE_VALUES, ECH_CB, ECH_OPTIONS,
    KEEP_CB, KEY, TLS, TS_CERT_FILE, TS_CERT_MODE, TS_DEST, TS_DNS_ENV,
    TS_ECH, TS_KEY_FILE, TS_PRIVATE_KEY, TS_PROVIDER, TS_SERVER_NAME,
    TS_SHORT_ID, TS_SKIP_CB, TS_XVER, XVER_CB,
)
from .state import _advance, _is_edit, _reply, prompt_for

# ---------- step: TLS ----------

def _tls_options_for(data: dict) -> list[tuple[int, str]]:
    """vless 必须在 TLS / REALITY 间二选一,不给「关闭」;其余给 关闭 / TLS。"""
    protocol = data["values"].get("protocol", "shadowsocks")
    if protocol == "vless":
        return [(1, "TLS"), (2, "REALITY")]
    return [(0, "关闭"), (1, "TLS")]


@prompt_for("tls")
async def _prompt_tls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data[KEY]
    options = _tls_options_for(data)
    rows = [[
        InlineKeyboardButton(f"{label} ({v})", callback_data=f"pnlsave:t:{v}")
        for v, label in options
    ]]
    if _is_edit(context):
        current = data["initial"].get("tls", 0)
        rows.append([
            InlineKeyboardButton(f"保留 ({current})", callback_data=KEEP_CB),
        ])
    await _reply(
        update, "请选择安全性:", reply_markup=InlineKeyboardMarkup(rows)
    )
    return TLS


async def step_tls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data[KEY]
    allowed = {v for v, _ in _tls_options_for(data)}
    if query.data == KEEP_CB:
        tls = int(data["initial"].get("tls", 0))
    else:
        tls = int(query.data.split(":", 2)[2])
    if tls not in allowed:
        await query.message.reply_text("无效的安全性选择,请重选:")
        return TLS
    data["values"]["tls"] = tls
    return await _advance("tls", update, context)
# ---------- step: TLS_SETTINGS(引导式) ----------

def _resolve_tls(data: dict) -> int:
    """取当前生效的 tls 值。vless/ss 走过选项以 values 为准;
    anytls/hysteria2 服务端强制 1。"""
    tls = data["values"].get("tls")
    if tls is not None:
        return int(tls)
    protocol = data["values"].get("protocol", "shadowsocks")
    if protocol in ("anytls", "hysteria2"):
        return 1
    return int(data["initial"].get("tls", 0) or 0)


def _ts_sequence(data: dict) -> list[str]:
    """根据 tls 值(及已选的 cert_mode)算出 tls_settings 子字段顺序。"""
    tls = _resolve_tls(data)
    if tls == 2:  # REALITY
        return ["dest", "server_name", "private_key", "short_id", "xver"]
    if tls == 1:  # TLS
        seq = ["server_name", "cert_mode"]
        cert_mode = data.get("ts", {}).get("cert_mode")
        if cert_mode in ("file", "self"):
            seq += ["cert_file", "key_file"]
        elif cert_mode == "dns":
            seq += ["provider", "dns_env"]
        # ECH 目前只对 vless 暴露
        if data["values"].get("protocol") == "vless":
            seq += ["ech"]
        return seq
    return []  # tls=0:不配 tls_settings


def _ts_skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("跳过", callback_data=TS_SKIP_CB)]]
    )


@prompt_for("tls_settings")
async def _prompt_tls_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """tls_settings 引导入口:按 tls 值进入对应子流程。"""
    data = context.user_data[KEY]
    tls = _resolve_tls(data)
    initial_tls = int(data["initial"].get("tls", 0) or 0)
    initial_ts = data["initial"].get("tls_settings")
    # 编辑且 tls 没变,用现有值打底,逐项可改;否则从空开始
    if _is_edit(context) and tls == initial_tls and isinstance(initial_ts, dict):
        data["ts"] = dict(initial_ts)
    else:
        data["ts"] = {}

    seq = _ts_sequence(data)
    if not seq:  # tls=0
        return await _advance("tls_settings", update, context)
    return await _TS_PROMPT_BY_FIELD[seq[0]](update, context)


async def _ts_advance(
    after_subfield: str, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    seq = _ts_sequence(data)
    try:
        idx = seq.index(after_subfield)
    except ValueError:
        idx = len(seq)
    if idx + 1 < len(seq):
        return await _TS_PROMPT_BY_FIELD[seq[idx + 1]](update, context)
    return await _ts_finish(update, context)


async def _ts_finish(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    data = context.user_data[KEY]
    ts = {k: v for k, v in data.get("ts", {}).items() if v not in (None, "")}
    if ts:
        data["values"]["tls_settings"] = ts
    data.pop("ts", None)
    return await _advance("tls_settings", update, context)


def _ts_cur(data: dict, key: str) -> str:
    val = data.get("ts", {}).get(key)
    return str(val) if val not in (None, "") else ""


async def _ts_text_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    *, prompt: str, key: str, state: int,
) -> int:
    cur = _ts_cur(context.user_data[KEY], key)
    suffix = f"(当前: {cur})" if cur else ""
    await _reply(update, f"{prompt}{suffix},或点「跳过」:", reply_markup=_ts_skip_kb())
    return state


async def _ts_text_step(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    *, key: str, after: str,
) -> int:
    """文本子字段通用处理:有输入则写入 ts[key],「跳过」按钮保持原值。"""
    data = context.user_data[KEY]
    if update.callback_query:  # 跳过
        await update.callback_query.answer()
    else:
        val = (update.message.text or "").strip()
        if val:
            data["ts"][key] = val
    return await _ts_advance(after, update, context)


# server_name(TLS 与 REALITY 共用)

async def _ts_prompt_server_name(update, context):
    return await _ts_text_prompt(
        update, context,
        prompt="请输入 server_name / SNI 域名",
        key="server_name", state=TS_SERVER_NAME,
    )


async def step_ts_server_name(update, context):
    return await _ts_text_step(
        update, context, key="server_name", after="server_name"
    )


# cert_mode(TLS)

async def _ts_prompt_cert_mode(update, context):
    rows = [[
        InlineKeyboardButton(label, callback_data=f"{CERT_MODE_CB}{val}")
        for val, label in CERT_MODE_OPTIONS
    ]]
    await _reply(
        update,
        "请选择证书获取方式 cert_mode:\n"
        "http=ACME http 验证 / dns=ACME dns 验证 / self=自签 / "
        "file=手动指定证书路径 / none=不自动签发",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return TS_CERT_MODE


async def step_ts_cert_mode(update, context):
    query = update.callback_query
    await query.answer()
    cm = query.data.split(":", 2)[2]
    if cm not in CERT_MODE_VALUES:
        await query.message.reply_text("无效的 cert_mode,请重选:")
        return TS_CERT_MODE
    context.user_data[KEY]["ts"]["cert_mode"] = cm
    return await _ts_advance("cert_mode", update, context)


# cert_file / key_file(cert_mode=file/self)

async def _ts_prompt_cert_file(update, context):
    return await _ts_text_prompt(
        update, context, prompt="请输入证书文件路径 cert_file",
        key="cert_file", state=TS_CERT_FILE,
    )


async def step_ts_cert_file(update, context):
    return await _ts_text_step(
        update, context, key="cert_file", after="cert_file"
    )


async def _ts_prompt_key_file(update, context):
    return await _ts_text_prompt(
        update, context, prompt="请输入私钥文件路径 key_file",
        key="key_file", state=TS_KEY_FILE,
    )


async def step_ts_key_file(update, context):
    return await _ts_text_step(
        update, context, key="key_file", after="key_file"
    )


# provider / dns_env(cert_mode=dns)

async def _ts_prompt_provider(update, context):
    return await _ts_text_prompt(
        update, context, prompt="请输入 DNS provider(如 cloudflare / aliyun)",
        key="provider", state=TS_PROVIDER,
    )


async def step_ts_provider(update, context):
    return await _ts_text_step(
        update, context, key="provider", after="provider"
    )


async def _ts_prompt_dns_env(update, context):
    return await _ts_text_prompt(
        update, context,
        prompt="请输入 dns_env(逗号分隔 KEY=VALUE,如 CF_Token=xxx,CF_Account_ID=yyy)",
        key="dns_env", state=TS_DNS_ENV,
    )


async def step_ts_dns_env(update, context):
    return await _ts_text_step(
        update, context, key="dns_env", after="dns_env"
    )


# REALITY 专属:dest / private_key / short_id / xver

async def _ts_prompt_dest(update, context):
    return await _ts_text_prompt(
        update, context,
        prompt="请输入 dest 偷取握手的目标(如 www.cloudflare.com:443)",
        key="dest", state=TS_DEST,
    )


async def step_ts_dest(update, context):
    return await _ts_text_step(update, context, key="dest", after="dest")


async def _ts_prompt_private_key(update, context):
    return await _ts_text_prompt(
        update, context, prompt="请输入 REALITY private_key",
        key="private_key", state=TS_PRIVATE_KEY,
    )


async def step_ts_private_key(update, context):
    return await _ts_text_step(
        update, context, key="private_key", after="private_key"
    )


async def _ts_prompt_short_id(update, context):
    return await _ts_text_prompt(
        update, context, prompt="请输入 short_id",
        key="short_id", state=TS_SHORT_ID,
    )


async def step_ts_short_id(update, context):
    return await _ts_text_step(
        update, context, key="short_id", after="short_id"
    )


async def _ts_prompt_xver(update, context):
    cur = _ts_cur(context.user_data[KEY], "xver") or "0"
    rows = [[
        InlineKeyboardButton(str(i), callback_data=f"{XVER_CB}{i}")
        for i in (0, 1, 2)
    ]]
    await _reply(
        update,
        f"请选择 xver(proxy protocol 版本,默认 0;当前: {cur}):",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return TS_XVER


async def step_ts_xver(update, context):
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 2)[2]
    # node.go 里 xver 是 json:"xver,string",存成字符串
    context.user_data[KEY]["ts"]["xver"] = token
    return await _ts_advance("xver", update, context)


# ECH(Encrypted Client Hello,仅 vless + tls=1)

async def _ts_prompt_ech(update, context):
    cur = _ts_cur(context.user_data[KEY], "ech")
    rows = [[
        InlineKeyboardButton(
            label + (" ✅" if val == cur else ""),
            callback_data=f"{ECH_CB}{val or 'off'}",
        )
        for val, label in ECH_OPTIONS
    ]]
    await _reply(
        update,
        "请选择 ECH(Encrypted Client Hello)配置"
        f"(当前: {cur or '关闭'}):",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return TS_ECH


async def step_ts_ech(update, context):
    query = update.callback_query
    await query.answer()
    token = query.data.split(":", 2)[2]
    # off -> 空串,_ts_finish 会过滤掉,等价于不写 ech 字段(关闭)
    context.user_data[KEY]["ts"]["ech"] = "" if token == "off" else token
    return await _ts_advance("ech", update, context)


# 子字段 -> prompt 映射(供 _ts_advance 用)
_TS_PROMPT_BY_FIELD: dict[str, Any] = {
    "server_name": _ts_prompt_server_name,
    "cert_mode": _ts_prompt_cert_mode,
    "cert_file": _ts_prompt_cert_file,
    "key_file": _ts_prompt_key_file,
    "provider": _ts_prompt_provider,
    "dns_env": _ts_prompt_dns_env,
    "dest": _ts_prompt_dest,
    "private_key": _ts_prompt_private_key,
    "short_id": _ts_prompt_short_id,
    "xver": _ts_prompt_xver,
    "ech": _ts_prompt_ech,
}
