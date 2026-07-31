"""对话流的常量:状态码、各步骤选项表、callback 前缀、字段白名单。

不 import 任何同级模块,是整个包的叶子。
"""
from __future__ import annotations


(
    PROTOCOL,
    NAME,
    HOST,
    PORT,
    SERVER_PORT,
    CIPHER,
    FLOW,
    TLS,
    # tls_settings 引导式子步骤
    TS_SERVER_NAME,
    TS_CERT_MODE,
    TS_CERT_FILE,
    TS_KEY_FILE,
    TS_PROVIDER,
    TS_DNS_ENV,
    TS_DEST,
    TS_PRIVATE_KEY,
    TS_SHORT_ID,
    TS_XVER,
    TS_ECH,
    NETWORK,
    NET_SETTINGS,
    UP_MBPS,
    DOWN_MBPS,
    RATE,
    PARENT,
    GROUPS,
    ADVANCED,
    CONFIRM,
) = range(28)


KEY = "pnlsave"

# v2board V2nodeController::save 接受的协议
PROTOCOL_OPTIONS: list[tuple[str, str]] = [
    ("shadowsocks", "Shadowsocks"),
    ("vless", "VLESS"),
    ("anytls", "AnyTLS"),
    ("hysteria2", "Hysteria2"),
]
PROTOCOL_VALUES = {v for v, _ in PROTOCOL_OPTIONS}

CIPHER_OPTIONS = [
    "aes-128-gcm",
    "aes-192-gcm",
    "aes-256-gcm",
    "chacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
]
# v2board 校验:tls ∈ {0, 1, 2}。1 = TLS, 2 = REALITY (vless 常用)
TLS_OPTIONS: list[tuple[int, str]] = [(0, "关闭"), (1, "TLS"), (2, "REALITY")]
# v2board 校验:network ∈ {tcp, ws, grpc, http, httpupgrade, xhttp}
NETWORK_OPTIONS_SS: list[tuple[str, str]] = [("tcp", "tcp"), ("http", "http伪装")]
NETWORK_OPTIONS_FULL: list[tuple[str, str]] = [
    ("tcp", "tcp"),
    ("ws", "ws"),
    ("grpc", "grpc"),
    ("http", "http"),
    ("httpupgrade", "httpupgrade"),
    ("xhttp", "xhttp"),
]
FLOW_OPTIONS: list[tuple[str, str]] = [
    ("", "(无)"),
    ("xtls-rprx-vision", "xtls-rprx-vision"),
]

# v2board V2nodeController::save 接受的字段白名单
SAVE_FIELDS = {
    "group_id", "route_id", "name", "parent_id", "host", "listen_ip",
    "port", "server_port", "protocol", "tls", "tls_settings", "flow",
    "network", "network_settings", "encryption", "encryption_settings",
    "disable_sni", "udp_relay_mode", "zero_rtt_handshake",
    "congestion_control", "cipher", "up_mbps", "down_mbps", "obfs",
    "obfs_password", "padding_scheme", "tags", "rate", "show", "sort",
}

KEEP_CB = "pnlsave:keep"
HOST_PICK_CB = "pnlsave:hs:"  # pnlsave:hs:<server_id>
HOST_MANUAL_CB = "pnlsave:hm"
NS_CLEAR_CB = "pnlsave:nsclear"
PARENT_PAGE_CB = "pnlsave:pp:"  # pnlsave:pp:<page> -> 父节点列表翻页
PROTOCOL_PICK_CB = "pnlsave:proto:"  # pnlsave:proto:<value>
FLOW_PICK_CB = "pnlsave:flow:"  # pnlsave:flow:none | pnlsave:flow:vision
UP_MBPS_SKIP_CB = "pnlsave:upskip"
DOWN_MBPS_SKIP_CB = "pnlsave:dnskip"
# tls_settings 引导
TS_SKIP_CB = "pnlsave:tssk"  # 各文本子字段通用的「跳过」
CERT_MODE_CB = "pnlsave:cm:"  # pnlsave:cm:<mode>
XVER_CB = "pnlsave:xver:"  # pnlsave:xver:<0|1|2>
ECH_CB = "pnlsave:ech:"  # pnlsave:ech:<off|cloudflare>

# tls_settings.ech 取值(目前仅 vless + tls=1)。""=关闭(不写该字段)。
ECH_OPTIONS: list[tuple[str, str]] = [("", "关闭"), ("cloudflare", "Cloudflare")]

# tls=1 时证书获取方式
CERT_MODE_OPTIONS: list[tuple[str, str]] = [
    ("http", "http"),
    ("dns", "dns"),
    ("self", "self"),
    ("file", "file"),
    ("none", "none"),
]
CERT_MODE_VALUES = {v for v, _ in CERT_MODE_OPTIONS}
_FLOW_BY_PROTOCOL: dict[str, list[str]] = {
    "shadowsocks": [
        "protocol", "name", "host", "port", "server_port",
        "cipher", "tls", "tls_settings", "network", "net_settings",
        "rate", "parent", "groups", "advanced", "confirm",
    ],
    "vless": [
        "protocol", "name", "host", "port", "server_port",
        "flow", "tls", "tls_settings", "network", "net_settings",
        "rate", "parent", "groups", "advanced", "confirm",
    ],
    # anytls / hysteria2 服务端会强制 tls=1,bot 不再让用户选,
    # 但 tls_settings 仍然要给用户机会填(证书 / SNI / REALITY 等)。
    "anytls": [
        "protocol", "name", "host", "port", "server_port",
        "tls_settings", "network", "net_settings",
        "rate", "parent", "groups", "advanced", "confirm",
    ],
    "hysteria2": [
        "protocol", "name", "host", "port", "server_port",
        "tls_settings", "up_mbps", "down_mbps",
        "rate", "parent", "groups", "advanced", "confirm",
    ],
}
