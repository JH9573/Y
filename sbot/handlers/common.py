"""handler 共享的工具与上下文容器。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from telegram import InlineKeyboardButton, KeyboardButton, ReplyKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import filters

from ..core.timeutil import utcnow

from ..config import Config
from ..core.crypto import Crypto
from ..core.ssh import SSHClient
from ..services.cloudflare_api import CloudflareClient
from ..services.github_release import GitHubReleaseClient
from ..services.v2board_api import V2BoardClient


@dataclass
class AppContext:
    """注入到 Telegram bot_data 中,供所有 handler 访问。

    OSS 客户端不在这里:其配置可在运行期通过 bot 修改(存库,.env 回退),
    由 handlers/oss_config.load_oss 按需构建。
    """

    config: Config
    crypto: Crypto
    ssh: SSHClient
    v2board: V2BoardClient
    cloudflare: CloudflareClient
    github: GitHubReleaseClient


CTX_KEY = "app_ctx"


def get_ctx(context) -> AppContext:
    return context.application.bot_data[CTX_KEY]


# 通用 callback_data 前缀,集中管理避免冲突
CB_SERVER_PREFIX = "srv:"  # srv:<id> -> 进入服务器菜单
CB_OPS_PREFIX = "ops:"  # ops:<id>:<action>
CB_OPS_CONFIRM = "opsc:"  # opsc:<id>:<action> -> 二次确认后真正执行
CB_DEL_SERVER = "delsrv:"  # delsrv:<id>
CB_DEL_SERVER_OK = "delsrvok:"  # delsrvok:<id>
CB_EDIT_SERVER = "edsrv:"  # edsrv:<id> -> 修改服务器信息菜单
CB_V2NODE_MENU = "v2nmenu:"  # v2nmenu:<id> -> v2node 管理子菜单
CB_FW_OPEN = "fwopen:"  # fwopen:<server_id> -> 放行 v2node 监听端口
CB_INSTALL_START = "inst:"  # inst:<id>
CB_INSTALL_PANEL = "instp:"  # instp:<server_id>:<panel_id> -> 安装流程选面板后
CB_INSTALL_NODE = "instn:"   # instn:<server_id>:<panel_id>:<node_id> -> 选节点后
CB_INSTALL_OK = "instok:"    # instok:<server_id>:<panel_id>:<node_id> -> 真正开装
CB_UNINSTALL_START = "uninst:"  # uninst:<id>
CB_NODE_MENU = "nodes:"  # nodes:<server_id>
CB_NODE_ADD = "nodeadd:"  # nodeadd:<server_id>
CB_NODE_DEL = "nodedel:"  # nodedel:<server_id>:<node_pk>
CB_NODE_DEL_OK = "nodedelok:"  # nodedelok:<server_id>:<node_pk>
CB_NODE_SYNC = "nodesync:"  # nodesync:<server_id>
CB_BACK_SERVERS = "back:servers"
CB_PANEL_PREFIX = "pnl:"  # pnl:<id> -> 进入面板菜单
CB_DEL_PANEL = "delpnl:"  # delpnl:<id>
CB_DEL_PANEL_OK = "delpnlok:"  # delpnlok:<id>
CB_BACK_PANELS = "back:panels"
CB_PANEL_NODES = "pnln:"  # pnln:<panel_id>[:<page>] -> v2node 列表(省略页码=上次浏览的页)
CB_PANEL_NODE = "pnldd:"  # pnldd:<panel_id>:<node_id> -> 节点详情
CB_PANEL_NODE_SHOW = "pnlsh:"  # pnlsh:<panel_id>:<node_id>:<0|1> -> 切换上下架
CB_PANEL_NODE_DROP = "pnldrop:"  # pnldrop:<panel_id>:<node_id> -> 删除二次确认
CB_PANEL_NODE_DROP_OK = "pnldropok:"  # pnldropok:<panel_id>:<node_id> -> 真正删除
CB_PANEL_NODE_SYNC = "pnlsync:"  # pnlsync:<panel_id> -> 从面板同步节点
CB_PANEL_NODE_ADD = "pnladd:"  # pnladd:<panel_id> -> 添加 shadowsocks 节点
CB_PANEL_NODE_EDIT = "pnledit:"  # pnledit:<panel_id>:<node_id> -> 编辑 shadowsocks 节点
CB_PANEL_NODE_COPY = "pnlcopy:"  # pnlcopy:<panel_id>:<node_id> -> 复制节点
CB_EDIT_PANEL = "epnl:"  # epnl:<panel_id> -> 编辑面板信息
CB_SYNC_PANEL_CREDS = "psync:"  # psync:<panel_id> -> 重拉 api_host/api_key
CB_NODE_ADD_PANEL = "naddp:"  # naddp:<server_id>:<panel_id> -> 添加节点二级:选面板后
CB_NODE_ADD_NODE = "naddn:"  # naddn:<server_id>:<panel_id>:<node_id> -> 添加节点三级:选节点后
CB_NODE_ADD_OK = "naddok:"  # naddok:<server_id>:<panel_id>:<node_id> -> 写远程
# 主菜单 reply keyboard 点「服务器管理 / 面板管理」后,在对话里弹出的二级 inline 菜单
CB_MENU_SRV_LIST = "msrvls"  # 进入服务器列表
CB_MENU_SRV_ADD = "msrvad"   # 进入添加服务器对话
CB_MENU_PNL_LIST = "mpnlls"  # 进入面板列表
CB_MENU_PNL_ADD = "mpnlad"   # 进入添加面板对话
CB_MENU_DNS_LIST = "mdnsls"  # 进入 DNS 账户列表
CB_MENU_DNS_ADD = "mdnsad"   # 进入添加 DNS 账户对话

# DNS 管理 ---- 账户层
CB_DNS_ACCOUNT = "dnsa:"      # dnsa:<id> -> 账户详情
CB_BACK_DNS_LIST = "back:dns"  # 返回 DNS 账户列表
CB_EDIT_DNS_ACCOUNT = "dnsae:"   # dnsae:<id> -> 编辑账户
CB_DEL_DNS_ACCOUNT = "dnsad:"    # dnsad:<id> -> 删除确认
CB_DEL_DNS_ACCOUNT_OK = "dnsadok:"  # dnsadok:<id> -> 真正删除
# DNS 管理 ---- zone 层(实时拉取,无本地缓存)
CB_DNS_ZONES = "dnsz:"        # dnsz:<account_id>:<page> -> zones 列表
CB_DNS_ZONE = "dnszd:"        # dnszd:<account_id>:<zone_id> -> 进入 zone(记录列表第 1 页)
# DNS 管理 ---- 记录层(account_id/zone_id 从 user_data["dns_ctx"] 读)
CB_DNS_RECORDS = "dnsrs:"     # dnsrs:<page> -> 记录列表分页
CB_DNS_RECORD = "dnsr:"       # dnsr:<record_id> -> 记录详情
CB_DNS_RECORD_DEL = "dnsrd:"     # dnsrd:<record_id> -> 删除确认
CB_DNS_RECORD_DEL_OK = "dnsrdok:"  # dnsrdok:<record_id> -> 真正删除
CB_DNS_RECORD_ADD = "dnsradd"     # 添加记录入口(无参数,从 user_data 取上下文)
CB_DNS_RECORD_EDIT = "dnsre:"     # dnsre:<record_id> -> 编辑记录

# 安装包分发(GitHub Release -> OSS)
CB_MENU_REL_LIST = "mrells"   # 进入分发仓库列表
CB_MENU_REL_ADD = "mrelad"    # 进入添加分发仓库对话
CB_REL_SRC = "rsrc:"          # rsrc:<id> -> 仓库详情
CB_BACK_REL_LIST = "back:rels"  # 返回仓库列表
CB_DEL_REL_SRC = "rsrcd:"     # rsrcd:<id> -> 删除确认
CB_DEL_REL_SRC_OK = "rsrcdok:"  # rsrcdok:<id> -> 真正删除
CB_REL_PICK = "rpick:"        # rpick:<source_id> -> 拉取 Release 列表
CB_REL_VER = "rver:"          # rver:<index> -> 选中版本(索引指向 user_data 缓存)
CB_REL_TOGGLE = "rtog:"       # rtog:<asset_index> -> 勾选/取消勾选某个文件
CB_REL_ALL = "rall"           # 全选当前版本的文件
CB_REL_NONE = "rnone"         # 清空当前版本的勾选
CB_REL_GO = "rgo:"            # rgo:<index> -> 确认后下载并上传已勾选的文件
# 远程配置(腾讯云 COS 上的 JSON 文件)
CB_MENU_RCFG_LIST = "mrcls"   # 进入远程配置文件列表
CB_MENU_RCFG_ADD = "mrcad"    # 进入添加远程配置文件对话
CB_MENU_COS_CFG = "mcoscfg"   # 查看 COS 配置
CB_COS_EDIT = "cose"          # 进入 COS 配置录入对话
CB_COS_SAVE = "cossave"       # 校验失败后仍要保存
CB_COS_DROP = "cosdrop"       # 校验失败后放弃保存
CB_COS_CLEAR = "cosclr"       # 清除 COS 配置
CB_COS_CLEAR_OK = "cosclrok"  # 清除二次确认
CB_RCFG_FILE = "rcf:"         # rcf:<id> -> 文件详情(拉取并展示内容)
CB_BACK_RCFG_LIST = "back:rcfg"  # 返回文件列表
CB_RCFG_DEL = "rcfd:"         # rcfd:<id> -> 移除文件确认(仅移出列表)
CB_RCFG_DEL_OK = "rcfdok:"    # rcfdok:<id> -> 真正移除
CB_RCFG_SET = "rcfs:"         # rcfs:<id> -> 修改单个字段对话入口
CB_RCFG_REPLACE = "rcfp:"     # rcfp:<id> -> 替换整个文件对话入口
CB_RCFG_ADD_FORCE = "rcfaf"   # 文件不存在时确认创建
CB_RCFG_ADD_DROP = "rcfad"    # 文件不存在时放弃添加

CB_MENU_OSS_CFG = "mosscfg"   # 查看 OSS 配置
CB_OSS_EDIT = "osse"          # 进入 OSS 配置录入对话
CB_OSS_SAVE = "osssave"       # 校验失败后仍要保存
CB_OSS_DROP = "ossdrop"       # 校验失败后放弃保存
CB_OSS_CLEAR = "ossclr"       # 清除数据库中的 OSS 配置(回退 .env)
CB_OSS_CLEAR_OK = "ossclrok"  # 清除二次确认

# 更新重启
CB_UPDATE_CONFIRM = "updok"   # 二次确认后:更新当前分支并重启
CB_UPDATE_CANCEL = "updno"    # 取消更新
CB_UPDATE_PICK = "updpick"    # 展示可切换的远程分支列表
CB_UPDATE_BRANCH = "updb:"    # updb:<index> -> 选中某个分支(索引指向 user_data 缓存)
CB_UPDATE_SWITCH = "updsw:"   # updsw:<index> -> 二次确认后切换到该分支并重启

CB_LOGS = "logs:"  # logs:<page>:<0|1 只看失败> -> 操作日志分页

CB_NOOP = "noop"


# ---------- 分页 ----------

# 一个条目占一行 inline 按钮,一页 20 条在手机上一屏多一点,再多就难翻了
PAGE_SIZE = 20


@dataclass(frozen=True)
class Page:
    """一页数据 + 渲染分页控件所需的全部信息。"""

    items: list
    number: int       # 当前页码,1 起
    total_pages: int
    total: int        # 全部条目数
    start: int        # 当前页首条在整表中的下标,0 起

    @property
    def multi(self) -> bool:
        return self.total_pages > 1

    @property
    def label(self) -> str:
        """「第 2 / 7 页(第 21-40 个)」,单页时为空串。"""
        if not self.multi:
            return ""
        return (
            f"第 {self.number} / {self.total_pages} 页"
            f"(第 {self.start + 1}-{self.start + len(self.items)} 个)"
        )


def paginate(items: Sequence, page: int | None, size: int = PAGE_SIZE) -> Page:
    """按页切分。page=None 视为第 1 页;越界一律夹回有效范围。

    夹回而不是报错,是因为按钮可能过期:上一屏还有 7 页,删掉一批之后只剩 3 页,
    用户点的仍是旧消息上的「第 6 页」。
    """
    total = len(items)
    total_pages = max(1, -(-total // size))
    number = max(1, min(page or 1, total_pages))
    start = (number - 1) * size
    return Page(
        items=list(items[start:start + size]),
        number=number,
        total_pages=total_pages,
        total=total,
        start=start,
    )


def page_slice(
    total: int, page: int | None, size: int = PAGE_SIZE
) -> tuple[int, int, int]:
    """数据在 SQL 侧分页时用:返回 (夹好的页码, 总页数, offset)。

    拿到结果后用 Page(...) 手工组装,就能复用 pager_row 渲染同一套翻页控件。
    """
    total_pages = max(1, -(-total // size))
    number = max(1, min(page or 1, total_pages))
    return number, total_pages, (number - 1) * size


def pager_row(
    cb_prefix: str, page: Page, *, suffix: str = ""
) -> list[InlineKeyboardButton]:
    """上一页 / 页码 / 下一页。单页时返回空列表。

    回调拼成 f"{cb_prefix}{页码}{suffix}",所以 cb_prefix 传如
    f"{CB_PANEL_NODES}{panel_id}:";页码后面还有参数的(如日志的筛选位)
    用 suffix 补。中间的页码按钮只做展示,回调是 CB_NOOP(已注册 handler,
    点了不会一直转圈)。
    """
    if not page.multi:
        return []
    return [
        InlineKeyboardButton(
            "⬅ 上一页", callback_data=f"{cb_prefix}{page.number - 1}{suffix}"
        )
        if page.number > 1
        else InlineKeyboardButton("·", callback_data=CB_NOOP),
        InlineKeyboardButton(
            f"{page.number}/{page.total_pages}", callback_data=CB_NOOP
        ),
        InlineKeyboardButton(
            "下一页 ➡", callback_data=f"{cb_prefix}{page.number + 1}{suffix}"
        )
        if page.number < page.total_pages
        else InlineKeyboardButton("·", callback_data=CB_NOOP),
    ]


def split_page_arg(payload: str, parts: int) -> tuple[list[str], int | None]:
    """拆 callback payload 尾部可选的页码。

    payload 形如 "12:34" 或 "12:34:3",parts 是页码之前的固定段数。
    返回 (固定段列表, 页码或 None)。
    """
    pieces = payload.split(":")
    fixed = pieces[:parts]
    page = int(pieces[parts]) if len(pieces) > parts and pieces[parts] else None
    return fixed, page


# Telegram 对「编辑成和原来一模一样的内容」会返回 400。这不是错误,是没变化。
NOT_MODIFIED = "message is not modified"


def is_not_modified(exc: BaseException) -> bool:
    return isinstance(exc, BadRequest) and NOT_MODIFIED in str(exc).lower()


async def safe_edit(target, text: str, **kwargs) -> bool:
    """编辑消息,把「内容没变」当作正常情况吞掉。

    target 传 CallbackQuery(走 edit_message_text)或 Message(走 edit_text)。
    返回 True 表示消息真的被改了,False 表示内容与原来一致、Telegram 拒绝了编辑。
    其余 BadRequest 照常抛出,不掩盖真问题。

    典型场景:一分钟内连点两次「🔄 同步」,节点数没变 -> banner、「最近同步:
    刚刚」、按钮全都逐字相同 -> 400 -> 以前会冒到全局 error handler,给用户
    弹一条误导的「操作出错」。
    """
    editor = getattr(target, "edit_message_text", None) or target.edit_text
    try:
        await editor(text, **kwargs)
        return True
    except BadRequest as exc:
        if is_not_modified(exc):
            return False
        raise


def truncate(text: str, limit: int = 3500) -> str:
    """长输出截断,保护 Telegram 消息长度上限(4096)。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(已截断,共 {len(text)} 字符)"


def human_size(num: int) -> str:
    """字节数转可读大小(1.5 MB / 320.0 KB / 12 B)。"""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def humanize_age(when: datetime | None) -> str:
    """把 UTC 时间点转换为相对当前的中文描述(刚刚 / X 分钟前 / X 小时前 / X 天前)。"""
    if when is None:
        return "从未"
    sec = int((utcnow() - when).total_seconds())
    if sec < 0:
        sec = 0
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    if sec < 86400:
        return f"{sec // 3600} 小时前"
    return f"{sec // 86400} 天前"


# ---------- Reply keyboard 菜单 ----------

# 一级菜单(只在 reply keyboard 里出现)
MENU_SERVER_GROUP = "🖥 服务器管理"
MENU_PANEL_GROUP = "🎛 面板管理"
MENU_DNS_GROUP = "🌐 DNS 管理"
MENU_RELEASE_GROUP = "📦 安装包分发"
MENU_REMOTE_GROUP = "🛠 远程配置"
MENU_LOGS = "📜 操作日志"
MENU_UPDATE = "🔄 更新重启"
MENU_CANCEL = "❌ 取消"

ALL_MENU_TEXTS: frozenset[str] = frozenset({
    MENU_SERVER_GROUP, MENU_PANEL_GROUP, MENU_DNS_GROUP, MENU_RELEASE_GROUP,
    MENU_REMOTE_GROUP, MENU_LOGS, MENU_UPDATE, MENU_CANCEL,
})

# ConversationHandler 内部用,排除菜单按钮文本以免被 state 误吃
NON_MENU_TEXT_FILTER = (
    filters.TEXT & ~filters.COMMAND & ~filters.Text(list(ALL_MENU_TEXTS))
)
# 用于 ConversationHandler fallback:把任何菜单按钮当成取消,
# 避免用户对话中途点别处时既不被 conversation 消费、又被 menu 处理。
ANY_MENU_TEXT_FILTER = filters.Text(list(ALL_MENU_TEXTS))


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(MENU_SERVER_GROUP), KeyboardButton(MENU_PANEL_GROUP)],
            [KeyboardButton(MENU_DNS_GROUP), KeyboardButton(MENU_RELEASE_GROUP)],
            [KeyboardButton(MENU_REMOTE_GROUP), KeyboardButton(MENU_LOGS)],
            [KeyboardButton(MENU_UPDATE), KeyboardButton(MENU_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def cancel_only_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(MENU_CANCEL)]],
        resize_keyboard=True,
        is_persistent=True,
    )
