"""统一的时间基准。

库里所有 DateTime 列都是「不带时区的 UTC」——SQLite 的 CURRENT_TIMESTAMP
就是这个语义,历史数据也按这个写入。所以这里返回 naive UTC,而不是
aware datetime,避免同一列里混进两种时间基准导致时间差算错。

datetime.utcnow() 自 Python 3.12 起已废弃,项目里一律改用本模块。
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """当前 UTC 时间(naive,与库中存量数据同基准)。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)
