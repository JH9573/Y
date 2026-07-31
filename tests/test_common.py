"""分页组件、safe_edit、时间与脱敏工具。"""
from __future__ import annotations

from datetime import UTC

import pytest
from telegram.error import BadRequest

from sbot.core.redact import redact
from sbot.core.timeutil import utcnow
from sbot.handlers.common import (
    CB_NOOP,
    Page,
    human_size,
    humanize_age,
    is_not_modified,
    page_slice,
    pager_row,
    paginate,
    safe_edit,
    split_page_arg,
    truncate,
)

# ---------- paginate ----------

def test_paginate_slices_by_page():
    view = paginate(list(range(1, 138)), 4)
    assert view.items == list(range(61, 81))
    assert (view.number, view.total_pages, view.total, view.start) == (4, 7, 137, 60)
    assert view.label == "第 4 / 7 页(第 61-80 个)"


def test_paginate_last_page_is_partial():
    view = paginate(list(range(137)), 7)
    assert len(view.items) == 17
    assert view.label == "第 7 / 7 页(第 121-137 个)"


@pytest.mark.parametrize("page,expected", [(None, 1), (0, 1), (-5, 1), (99, 7)])
def test_paginate_clamps_out_of_range(page, expected):
    # 按钮会过期:上一屏还有 7 页,删掉一批后只剩 3 页,点旧按钮不能炸
    assert paginate(list(range(137)), page).number == expected


def test_paginate_empty_list():
    view = paginate([], 3)
    assert view.items == [] and view.total_pages == 1 and not view.multi


def test_paginate_exactly_one_page_is_not_multi():
    assert paginate(list(range(20)), 1).multi is False
    assert paginate(list(range(21)), 1).multi is True


# ---------- pager_row ----------

def test_pager_row_hidden_when_single_page():
    assert pager_row("x:", paginate([1, 2], 1)) == []


def test_pager_row_middle_page_has_both_directions():
    row = pager_row("pnln:7:", paginate(list(range(137)), 4))
    assert [b.callback_data for b in row] == ["pnln:7:3", CB_NOOP, "pnln:7:5"]
    assert row[1].text == "4/7"


def test_pager_row_edges_are_placeholders():
    first = pager_row("p:", paginate(list(range(137)), 1))
    last = pager_row("p:", paginate(list(range(137)), 7))
    assert first[0].callback_data == CB_NOOP and first[2].callback_data == "p:2"
    assert last[0].callback_data == "p:6" and last[2].callback_data == CB_NOOP


def test_pager_row_suffix_keeps_extra_args():
    row = pager_row("logs:", Page([], 2, 5, 45, 10), suffix=":1")
    assert [b.callback_data for b in row] == ["logs:1:1", CB_NOOP, "logs:3:1"]


def test_pager_callbacks_fit_telegram_limit():
    row = pager_row("naddp:999999:888888:", paginate(list(range(1000)), 25))
    assert all(len(b.callback_data.encode()) <= 64 for b in row)


# ---------- page_slice / split_page_arg ----------

@pytest.mark.parametrize("total,page,expected", [
    (45, 2, (2, 5, 10)), (45, None, (1, 5, 0)), (45, 99, (5, 5, 40)), (0, 3, (1, 1, 0)),
])
def test_page_slice(total, page, expected):
    assert page_slice(total, page, 10) == expected


@pytest.mark.parametrize("payload,parts,expected", [
    ("12:34", 2, (["12", "34"], None)),
    ("12:34:7", 2, (["12", "34"], 7)),
    ("12", 1, (["12"], None)),
    ("12:3", 1, (["12"], 3)),
])
def test_split_page_arg(payload, parts, expected):
    assert split_page_arg(payload, parts) == expected


# ---------- safe_edit ----------

async def test_safe_edit_swallows_not_modified(query):
    assert await safe_edit(query, "hello") is True
    assert await safe_edit(query, "hello") is False   # 内容没变
    assert query.edits == 1


async def test_safe_edit_reraises_other_bad_requests():
    class Boom:
        async def edit_message_text(self, *a, **kw):
            raise BadRequest("Message to edit not found")

    with pytest.raises(BadRequest):
        await safe_edit(Boom(), "x")


async def test_safe_edit_works_on_messages():
    from tests.conftest import FakeMessage

    msg = FakeMessage()
    assert await safe_edit(msg, "a") is True
    assert await safe_edit(msg, "a") is False


def test_is_not_modified_only_matches_that_error():
    assert is_not_modified(BadRequest("Message is not modified: xxx"))
    assert not is_not_modified(BadRequest("Message to edit not found"))
    assert not is_not_modified(ValueError("boom"))
    assert not is_not_modified(None)


# ---------- 其它小工具 ----------

def test_truncate_marks_the_cut():
    out = truncate("x" * 4000, limit=100)
    assert out.startswith("x" * 100) and "已截断" in out
    assert truncate("short") == "short"


def test_human_size():
    assert human_size(12) == "12 B"
    assert human_size(1536) == "1.5 KB"


def test_humanize_age():
    from datetime import timedelta

    assert humanize_age(None) == "从未"
    assert humanize_age(utcnow()) == "刚刚"
    assert humanize_age(utcnow() - timedelta(minutes=5)) == "5 分钟前"
    assert humanize_age(utcnow() - timedelta(days=3)) == "3 天前"
    # 时钟漂移导致的「未来时间」不能出现负数
    assert humanize_age(utcnow() + timedelta(minutes=5)) == "刚刚"


def test_utcnow_is_naive_utc():
    from datetime import datetime

    now = utcnow()
    assert now.tzinfo is None
    drift = abs((now - datetime.now(UTC).replace(tzinfo=None)).total_seconds())
    assert drift < 2


# ---------- 脱敏 ----------

@pytest.mark.parametrize("raw,leaked", [
    ("panel login failed: password=hunter2xx", "hunter2xx"),
    ('{"api_key": "abcd1234efgh"}', "abcd1234efgh"),
    ("Authorization: Bearer abcdefghijklmn", "abcdefghijklmn"),
    ("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
    ("auth_data eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM", "eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM"),
    ("https://admin:s3cr3t@panel.example.com/api", "s3cr3t"),
    ("access_key_secret: LTAI5tAbCdEfGhIjKlMn", "LTAI5tAbCdEfGhIjKlMn"),
])
def test_redact_removes_credentials(raw, leaked):
    out = redact(raw)
    assert leaked not in out, out
    assert "***" in out


def test_redact_keeps_useful_context():
    out = redact("panel_id=3, node_id=17: HTTP 500 from https://panel.example.com")
    assert "panel_id=3" in out and "node_id=17" in out
    assert "HTTP 500" in out and "panel.example.com" in out


def test_redact_passthrough():
    assert redact(None) is None
    assert redact("") == ""
