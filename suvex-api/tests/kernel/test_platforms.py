"""`ctf2.platforms`：平台的生命周期（生成 → 试跑 → 启用 → 删除）。

## 这组用例盯着的失败形态

平台接入是一条**有状态的长流程**（后台线程 + 轮询 + 凭据落库），
它坏起来全都很难看：

* 线程里抛异常 → 状态永远停在 `building`，用户一直转圈
* 生成失败把配置整份丢掉 → 用户看不到"差一点就成"的那份
* 启用时不验凭据 → 第一次同步才发现 key 是错的，而用户已以为配好了
* 删配置不处理账号 → 账号指向一个不存在的平台，同步时报看不懂的错

下面每条对着其中一种写。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.kernel import platforms
from app.kernel.adapters.builder import BuildAttempt, BuildResult, VerifyResult
from app.kernel.adapters.generic import GenericDriver
from app.kernel.adapters.spec import PlatformSpec
from app.kernel.models import KernelAccount, KernelPlatform
from tests.kernel.test_generic_driver import SLABMATCH_SPEC

BASE = SLABMATCH_SPEC["base_url"]


def _ready_platform(db: Session, *, status: str = "verified") -> KernelPlatform:
    row = platforms.create_building(db, base_url=BASE)
    row.spec = SLABMATCH_SPEC
    row.display_name = "比赛平台"
    row.status = status
    db.commit()
    return row


# ══════════════════════════════════════════════════════════
# 建行
# ══════════════════════════════════════════════════════════


def test_create_building_makes_a_pollable_row(db_session: Session) -> None:
    """★ 先落库再起线程 —— 否则后台写状态时行还不存在，结果会被丢掉，
    而用户看到的是"生成完了但什么都没有"。"""
    row = platforms.create_building(db_session, base_url=BASE)
    assert row.status == "building"
    assert row.driver == "generic"
    assert row.attempts == 0
    assert row.slug == "example-com"


def test_slugs_stay_unique(db_session: Session) -> None:
    first = platforms.create_building(db_session, base_url=BASE)
    second = platforms.create_building(db_session, base_url=BASE)
    assert first.slug != second.slug
    assert second.slug.startswith("example-com")


def test_spec_of_returns_none_on_a_broken_config(db_session: Session) -> None:
    """★ 坏掉的配置返回 `None`，**不抛**。

    抛出去会让整个平台列表接口 500 —— 那种"点开设置页就白屏"的故障，
    用户没有任何自救手段。返回 None 则退化成"这个平台用不了"，
    那是可解释、可操作的。
    """
    row = _ready_platform(db_session)
    row.spec = {"display_name": "缺了 list_challenges"}
    db_session.commit()
    assert platforms.spec_of(row) is None


def test_spec_of_returns_none_for_the_builtin_driver(db_session: Session) -> None:
    row = platforms.create_building(db_session, base_url=BASE)
    row.spec = None
    db_session.commit()
    assert platforms.spec_of(row) is None


# ══════════════════════════════════════════════════════════
# 后台生成
# ══════════════════════════════════════════════════════════


def test_a_successful_build_writes_the_spec_back(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = platforms.create_building(db_session, base_url=BASE)

    async def fake_build(**kwargs: object) -> BuildResult:
        return BuildResult(
            spec=PlatformSpec.from_dict(SLABMATCH_SPEC),
            attempts=[BuildAttempt(index=1, raw='{"ok": 1}')],
            ok=True,
            challenge_count=2,
        )

    monkeypatch.setattr(platforms, "build_spec", fake_build)
    monkeypatch.setattr(platforms, "_build_chat", lambda: (lambda *a, **k: None))

    platforms._run_build(row.id, BASE, "ak_test", "")

    db_session.expire_all()
    fresh = db_session.get(KernelPlatform, row.id)
    assert fresh is not None
    assert fresh.status == "verified"
    assert fresh.spec is not None
    assert fresh.display_name == "比赛平台"
    assert fresh.attempts == 1


def test_a_failed_build_keeps_the_last_valid_spec(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ 生成没通过时，**最后那份合法配置要留下来**。

    它离成功最近，用户可以在界面上看到它、手工改一处再试。
    整份丢掉等于把模型做出来的工作全扔了 —— 而"差一点"和"完全不行"
    对用户是两件完全不同的事。
    """
    row = platforms.create_building(db_session, base_url=BASE)

    async def fake_build(**kwargs: object) -> BuildResult:
        return BuildResult(
            spec=PlatformSpec.from_dict(SLABMATCH_SPEC),
            attempts=[BuildAttempt(index=1, raw="{}", error="取不到题")] * 3,
            ok=False,
            error="取题目列表失败：KernelResponseError: 404",
        )

    monkeypatch.setattr(platforms, "build_spec", fake_build)
    monkeypatch.setattr(platforms, "_build_chat", lambda: (lambda *a, **k: None))

    platforms._run_build(row.id, BASE, "ak_test", "")

    db_session.expire_all()
    fresh = db_session.get(KernelPlatform, row.id)
    assert fresh is not None
    assert fresh.status == "failed"
    assert fresh.spec is not None, "差一点就成的那份配置不该被丢掉"
    assert fresh.last_error and "404" in fresh.last_error
    assert fresh.last_raw == "{}"


def test_a_crashing_build_still_clears_the_building_flag(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ 后台线程抛异常时，状态必须从 `building` 走到 `failed`。

    这是这条流程里最难查的一种故障：线程死了，而状态永远停在
    `building` —— 界面上一直转圈，用户只会以为"还在跑"，
    然后一次次重试（每次都会创建一个新平台行）。
    """
    row = platforms.create_building(db_session, base_url=BASE)

    async def boom(**kwargs: object) -> BuildResult:
        raise RuntimeError("provider 挂了")

    monkeypatch.setattr(platforms, "build_spec", boom)
    monkeypatch.setattr(platforms, "_build_chat", lambda: (lambda *a, **k: None))

    platforms._run_build(row.id, BASE, "ak_test", "")

    db_session.expire_all()
    fresh = db_session.get(KernelPlatform, row.id)
    assert fresh is not None
    assert fresh.status == "failed"
    assert fresh.last_error and "provider 挂了" in fresh.last_error


def test_start_build_refuses_a_second_concurrent_run() -> None:
    """同一个平台不允许两轮生成同时在跑 —— 后一轮会覆盖前一轮的产物，
    而用户看到的结果是"随机的"。"""
    with platforms._BUILD_GUARD:
        platforms._BUILDING.add("plat-x")
    try:
        assert platforms.start_build("plat-x", base_url=BASE, access_key="k", document="") is False
    finally:
        with platforms._BUILD_GUARD:
            platforms._BUILDING.discard("plat-x")


# ══════════════════════════════════════════════════════════
# 启用
# ══════════════════════════════════════════════════════════


def test_activate_rejects_an_empty_key(db_session: Session) -> None:
    row = _ready_platform(db_session)
    with pytest.raises(ApiError) as exc_info:
        platforms.activate_platform(db_session, platform_id=row.id, access_key="  ")
    assert exc_info.value.status_code == 400


def test_activate_rejects_a_platform_without_a_spec(db_session: Session) -> None:
    row = platforms.create_building(db_session, base_url=BASE)
    with pytest.raises(ApiError) as exc_info:
        platforms.activate_platform(db_session, platform_id=row.id, access_key="ak")
    assert exc_info.value.status_code == 409
    assert "重新生成" in exc_info.value.message


def test_activate_rejects_a_failed_verification(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 生成阶段验过一次，启用时**再验一次**。

    中间隔着一个不确定的窗口：用户可能改了配置、平台可能挂了、key 可能
    过期。而"启用"的语义是"以后就用它了" —— 不验等于把问题推到第一次
    同步时，那时用户已经以为配好了。
    """
    row = _ready_platform(db_session)

    async def fake_verify(*args: object, **kwargs: object) -> VerifyResult:
        return VerifyResult(ok=False, error="401 凭据无效")

    monkeypatch.setattr(platforms, "verify_spec", fake_verify)

    with pytest.raises(ApiError) as exc_info:
        platforms.activate_platform(db_session, platform_id=row.id, access_key="bad")
    assert exc_info.value.status_code == 400
    assert "401" in exc_info.value.message


def test_activate_stores_the_key_and_points_at_the_platform(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _ready_platform(db_session)

    async def fake_verify(*args: object, **kwargs: object) -> VerifyResult:
        return VerifyResult(ok=True, challenge_count=3)

    monkeypatch.setattr(platforms, "verify_spec", fake_verify)

    account = platforms.activate_platform(
        db_session, platform_id=row.id, access_key="ak_secret", label="比赛号"
    )
    assert account.platform_id == row.id
    assert account.label == "比赛号"
    # ★ 明文**不进库**：密文列有值、掩码列能给人看，而原文不在任何列里。
    assert account.token_encrypted and "ak_secret" not in account.token_encrypted
    assert account.token_hint
    assert account.has_token is True


# ══════════════════════════════════════════════════════════
# 删除与驱动选择
# ══════════════════════════════════════════════════════════


def test_deleting_a_platform_returns_the_account_to_builtin(
    db_session: Session, ctf2_account: KernelAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 删配置**不动账号**，但必须把账号退回内置驱动。

    不退回的话，账号会留下一个悬空的 `platform_id` ——
    症状是"同步时找不到平台，报一个看不懂的错"，而用户刚刚只是删掉了
    一份用不上的配置。
    """
    row = _ready_platform(db_session)

    async def fake_verify(*args: object, **kwargs: object) -> VerifyResult:
        return VerifyResult(ok=True, challenge_count=1)

    monkeypatch.setattr(platforms, "verify_spec", fake_verify)
    platforms.activate_platform(db_session, platform_id=row.id, access_key="ak")

    platforms.delete_platform(db_session, row.id)

    db_session.expire_all()
    assert db_session.get(KernelPlatform, row.id) is None
    account = db_session.get(KernelAccount, ctf2_account.id)
    assert account is not None
    assert account.platform_id is None, "账号应该退回内置驱动"
    assert account.token_encrypted, "删配置不该把凭据一起删掉"


def test_driver_for_returns_none_for_the_builtin_driver(
    db_session: Session, ctf2_account: KernelAccount
) -> None:
    """内置驱动返回 `None` —— 调用方据此继续用 `KernelClient`。"""
    assert platforms.driver_for(db_session, ctf2_account) is None


def test_driver_for_builds_a_generic_driver_for_a_generated_platform(
    db_session: Session, ctf2_account: KernelAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _ready_platform(db_session)
    ctf2_account.platform_id = row.id
    db_session.commit()
    db_session.refresh(ctf2_account)

    driver = platforms.driver_for(db_session, ctf2_account)
    assert isinstance(driver, GenericDriver)
    assert driver.spec.base_url == BASE


def test_driver_for_refuses_a_broken_config(
    db_session: Session, ctf2_account: KernelAccount
) -> None:
    """账号指向一个坏掉的配置时，**明确报错**而不是静默退回内置驱动。

    静默退回会让提交打到另一个平台上 —— 那是"用 A 平台的凭据去 B 平台
    提交"，后果比一个报错严重得多。
    """
    row = _ready_platform(db_session)
    row.spec = {"broken": True}
    ctf2_account.platform_id = row.id
    db_session.commit()

    with pytest.raises(ApiError) as exc_info:
        platforms.driver_for(db_session, ctf2_account)
    assert exc_info.value.status_code == 409
