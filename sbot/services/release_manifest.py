"""安装包清单:把上传结果整理成远程配置 JSON 的补丁。

「安装包分发」把 GitHub Release 的 assets 上传到 OSS 之后,客户端要靠远程
配置(COS 上的 config.json)知道去哪下载、下完校验什么。这里只做纯计算:

1. 按文件名识别平台与架构(UUCNet-3.1.5-macos-arm64.dmg → macos/arm64);
2. 把识别结果拼成 config.json 的补丁(tag / download_urls / download_assets),
   再合并进原文件,原有的其它字段(如 api、download_url)保持不动。

与客户端约定:

- 链接一律用带版本号的地址(<prefix>/<tag>/...),不用 latest/,
  这样 sha256 与 URL 永远对得上;
- download_urls 每个平台只有一个链接,macOS 给 arm64 的 dmg,
  Intel 机器从 download_assets 里取 amd64;
- REQUIRED_SLOTS 四个包必须齐全才允许同步,避免半套配置上线。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import os
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


# 平台识别: 先看扩展名,再看文件名里的关键字。顺序即优先级。
_PLATFORM_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("android", (".apk", ".aab"), ("android",)),
    ("windows", (".exe", ".msi"), ("windows", "win64", "win32", "win-")),
    ("macos", (".dmg", ".pkg"), ("macos", "darwin", "osx", "mac-")),
    ("linux", (".appimage", ".deb", ".rpm"), ("linux",)),
    ("ios", (".ipa",), ("ios",)),
)

# 架构识别。顺序重要: x86_64 里含 "x86",universal 要先于 arm64 判定。
_ARCH_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("universal", ("universal",)),
    ("arm64", ("arm64", "aarch64", "armv8")),
    ("amd64", ("amd64", "x86_64", "x86-64", "x64")),
    ("arm", ("armeabi", "armv7", "arm32")),
    ("x86", ("x86", "i386", "ia32")),
)

# download_assets 里的展示顺序(与手写配置的习惯一致)
_ARCH_ORDER = ("universal", "arm64", "amd64", "arm", "x86")

# download_urls 每个平台挑哪个架构做唯一链接,按优先级取第一个存在的
_PRIMARY_ARCH: dict[str, tuple[str, ...]] = {
    "android": ("universal", "arm64", "amd64"),
    "windows": ("amd64", "arm64", "x86"),
    "macos": ("arm64", "amd64", "universal"),
    "linux": ("amd64", "arm64"),
    "ios": ("universal", "arm64"),
}

# download_urls 的键顺序(新建时用;已有文件保持原顺序)
_URL_PLATFORM_ORDER = ("android", "windows", "macos", "linux", "ios")

# 同步前必须凑齐的四个安装包
REQUIRED_SLOTS: tuple[tuple[str, str], ...] = (
    ("android", "universal"),
    ("windows", "amd64"),
    ("macos", "arm64"),
    ("macos", "amd64"),
)

_PLATFORM_NAMES = {
    "android": "安卓",
    "windows": "Windows",
    "macos": "macOS",
    "linux": "Linux",
    "ios": "iOS",
}


@dataclass(frozen=True)
class ManifestAsset:
    """一个已上传的安装包,以及写进 config.json 需要的全部信息。"""

    name: str
    platform: str
    arch: str
    url: str
    sha256: str
    size: int

    @property
    def slot(self) -> tuple[str, str]:
        return (self.platform, self.arch)


def classify(name: str) -> tuple[str, str] | None:
    """按文件名判定 (平台, 架构);任一项判不出来返回 None。"""
    lowered = os.path.basename(name).lower()
    platform = None
    for candidate, suffixes, keywords in _PLATFORM_RULES:
        if lowered.endswith(suffixes) or any(k in lowered for k in keywords):
            platform = candidate
            break
    if platform is None:
        return None
    for candidate, keywords in _ARCH_RULES:
        if any(k in lowered for k in keywords):
            return platform, candidate
    return None


def slot_label(slot: tuple[str, str]) -> str:
    """(macos, arm64) → 「macOS arm64」,用于提示缺哪个包。"""
    platform, arch = slot
    return f"{_PLATFORM_NAMES.get(platform, platform)} {arch}"


def missing_slots(assets: Iterable[ManifestAsset]) -> list[tuple[str, str]]:
    """返回 REQUIRED_SLOTS 里还缺的槽位,空列表表示齐全。"""
    have = {a.slot for a in assets}
    return [slot for slot in REQUIRED_SLOTS if slot not in have]


def _sorted_assets(assets: Sequence[ManifestAsset]) -> list[ManifestAsset]:
    def key(a: ManifestAsset) -> tuple[str, int, str]:
        arch_rank = (
            _ARCH_ORDER.index(a.arch) if a.arch in _ARCH_ORDER
            else len(_ARCH_ORDER)
        )
        return (a.platform, arch_rank, a.arch)

    return sorted(assets, key=key)


def build_patch(
    *, tag: str, assets: Sequence[ManifestAsset],
) -> dict[str, Any]:
    """生成待写入 config.json 的字段。同一槽位重复时后者覆盖前者。"""
    ordered = _sorted_assets(assets)
    by_slot = {a.slot: a for a in ordered}

    download_assets: dict[str, dict[str, Any]] = {}
    for asset in ordered:
        download_assets.setdefault(asset.platform, {})[asset.arch] = {
            "url": asset.url,
            "sha256": asset.sha256,
            "size": asset.size,
        }

    download_urls: dict[str, str] = {}
    for platform in _URL_PLATFORM_ORDER:
        for arch in _PRIMARY_ARCH.get(platform, ()):
            asset = by_slot.get((platform, arch))
            if asset is not None:
                download_urls[platform] = asset.url
                break

    return {
        "tag": tag,
        "download_urls": download_urls,
        "download_assets": download_assets,
    }


def apply_patch(data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把补丁合并进原配置,返回新对象(不改原对象)。

    tag 直接覆盖;download_urls 按平台逐键覆盖;download_assets 按平台整体
    替换(同平台的旧架构条目不保留,避免新旧版本混在一个平台下)。
    其余字段一概不动,包括 download_url。
    """
    out = copy.deepcopy(data)
    out["tag"] = patch["tag"]

    urls = out.get("download_urls")
    if not isinstance(urls, dict):
        urls = {}
    urls.update(patch["download_urls"])
    out["download_urls"] = urls

    assets = out.get("download_assets")
    if not isinstance(assets, dict):
        assets = {}
    assets.update(patch["download_assets"])
    out["download_assets"] = assets
    return out


async def sha256_file(path: str, chunk: int = 1024 * 1024) -> str:
    """算本地文件的 sha256(读盘放到线程池,不阻塞事件循环)。"""
    loop = asyncio.get_running_loop()
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = await loop.run_in_executor(None, f.read, chunk)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()
