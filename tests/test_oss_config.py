"""OSS 多存储桶配置页:列表、详情、切换、删除、load_oss 取活动桶。"""
from __future__ import annotations

from types import SimpleNamespace

from sbot.db import crud
from sbot.handlers import oss_config as oc
from sbot.handlers.common import (
    CB_MENU_OSS_CFG,
    CB_OSS_ACTIVE,
    CB_OSS_DEL,
    CB_OSS_DEL_OK,
    CB_OSS_DETAIL,
    CB_OSS_EDIT,
)
from tests.conftest import FakeContext, FakeQuery, FakeUpdate


def _app_ctx(crypto=None, oss_configured=False):
    """够 oss_config 用的 AppContext 替身(config + crypto)。"""
    cfg = SimpleNamespace(
        oss_configured=oss_configured,
        oss_region="ap-northeast-1",
        oss_bucket="env-bucket",
        oss_access_key_id="AKIDENVENVENV",
        oss_public_base_url=None,
        oss_prefix="releases",
    )
    return SimpleNamespace(config=cfg, crypto=crypto)


async def _seed(crypto, buckets=("b1", "b2")):
    """录入若干个桶,第一个自动成为当前使用。"""
    async with crud.session() as s:
        rows = [
            await crud.upsert_oss_config(
                s, region="ap-southeast-1", bucket=b,
                access_key_id=f"AKID-{b}-000000",
                access_key_secret=crypto.encrypt(f"sk-{b}"),
                public_base_url=None, prefix="releases",
            )
            for b in buckets
        ]
        await s.commit()
    return rows


async def test_list_shows_buckets_with_active_mark(db, crypto):
    first, second = await _seed(crypto)
    q = FakeQuery(CB_MENU_OSS_CFG)
    await oc.show_config(FakeUpdate(q), FakeContext(app_ctx=_app_ctx(crypto)))
    callbacks = q.callbacks()
    assert f"{CB_OSS_DETAIL}{first.id}" in callbacks
    assert f"{CB_OSS_DETAIL}{second.id}" in callbacks
    assert CB_OSS_EDIT in callbacks
    labels = q.labels()
    assert any(label.startswith("✅ b1") for label in labels)
    assert any("b2" in label and not label.startswith("✅") for label in labels)


async def test_detail_of_backup_offers_activation(db, crypto):
    _first, second = await _seed(crypto)
    q = FakeQuery(f"{CB_OSS_DETAIL}{second.id}")
    await oc.cb_detail(FakeUpdate(q), FakeContext(app_ctx=_app_ctx(crypto)))
    assert f"{CB_OSS_ACTIVE}{second.id}" in q.callbacks()
    assert f"{CB_OSS_DEL}{second.id}" in q.callbacks()
    assert "备用" in q.text
    assert "sk-b2" not in q.text  # Secret 不上屏


async def test_detail_of_active_has_no_activation_button(db, crypto):
    first, _second = await _seed(crypto)
    q = FakeQuery(f"{CB_OSS_DETAIL}{first.id}")
    await oc.cb_detail(FakeUpdate(q), FakeContext(app_ctx=_app_ctx(crypto)))
    assert q.callbacks(CB_OSS_ACTIVE) == []
    assert "当前使用" in q.text


async def test_set_active_switches_and_rerenders_list(db, crypto):
    _first, second = await _seed(crypto)
    q = FakeQuery(f"{CB_OSS_ACTIVE}{second.id}")
    await oc.cb_set_active(FakeUpdate(q), FakeContext(app_ctx=_app_ctx(crypto)))
    assert any(label.startswith("✅ b2") for label in q.labels())
    async with crud.session() as s:
        assert (await crud.get_active_oss_config(s)).bucket == "b2"


async def test_delete_flow_removes_bucket_and_promotes_next(db, crypto):
    first, _second = await _seed(crypto)
    q = FakeQuery(f"{CB_OSS_DEL}{first.id}")
    await oc.cb_del(FakeUpdate(q), FakeContext(app_ctx=_app_ctx(crypto)))
    assert f"{CB_OSS_DEL_OK}{first.id}" in q.callbacks()  # 先二次确认
    assert "自动启用另一个" in q.text

    q2 = FakeQuery(f"{CB_OSS_DEL_OK}{first.id}")
    await oc.cb_del_ok(FakeUpdate(q2), FakeContext(app_ctx=_app_ctx(crypto)))
    async with crud.session() as s:
        configs = await crud.list_oss_configs(s)
    assert [c.bucket for c in configs] == ["b2"]
    assert configs[0].is_active
    assert any(label.startswith("✅ b2") for label in q2.labels())


async def test_empty_list_prompts_setup(db):
    q = FakeQuery(CB_MENU_OSS_CFG)
    await oc.show_config(FakeUpdate(q), FakeContext(app_ctx=_app_ctx()))
    assert "尚未配置" in q.text
    assert q.callbacks() == [CB_OSS_EDIT]


async def test_env_fallback_shown_when_db_empty(db):
    q = FakeQuery(CB_MENU_OSS_CFG)
    await oc.show_config(
        FakeUpdate(q), FakeContext(app_ctx=_app_ctx(oss_configured=True)),
    )
    assert ".env" in q.text and "env-bucket" in q.text
    assert CB_OSS_EDIT in q.callbacks()


async def test_load_oss_uses_active_bucket(db, crypto):
    _first, second = await _seed(crypto)
    async with crud.session() as s:
        await crud.set_active_oss_config(s, second.id)
        await s.commit()
    pair = await oc.load_oss(_app_ctx(crypto))
    assert pair is not None
    client, prefix = pair
    assert client.bucket == "b2" and prefix == "releases"
