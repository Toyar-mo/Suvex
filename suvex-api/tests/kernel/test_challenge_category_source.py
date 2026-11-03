"""CTF 页「规范化分类 + 接入来源」的接口级测试（v1.55）。

## 为什么这些断言值钱

用户在 CTF 页上看到的是**平台给的原始分类**（`REVERSE` / `第01题 web入门` /
`ir-root-cause` / `basic`），大小写和命名都不统一，直接展示/筛选几乎没用。
v1.55 给每道题另加两个**只读投影**：

· `canonicalCategory` —— 归一到 web/crypto/reverse/pwn/misc/ir/other；
· `platformSlug` / `platformName` —— 这道题来自哪个接入。

最容易悄无声息坏掉的不是映射本身，而是：
1. **投影改写了原文** —— 库里的 `category` 一旦被改，`sandbox_profile`
   会跟着漂，同一道题昨天用 re 镜像、今天用 base 镜像（不可复现）。
   所以这里显式断言 `category` 原文一字未动。
2. **列表 N+1** —— 逐题去 ORM 懒加载 platform，几十道题就是上百次查询。
   这里断言列表接口对几十道题也只建一次索引（用查询计数）。
3. **内置题落成空来源** —— 内置 CTF2 没有 `KernelPlatform` 行，推到空时
   必须落回「CTF2（内置）」，而不是空串。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.kernel import service
from app.kernel.models import KernelChallenge, KernelCompetition, KernelPlatform
from app.kernel.schemas import CANONICAL_CATEGORIES, canonical_category

API = "/api/v1"
LOCAL_IR = f"{API}/kernel/ir/tasks"


# ══════════════════════════════════════════════════════════
# 一、纯函数：canonical_category 的映射表
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 标准值（含大小写不一致）
        ("web", "web"),
        ("WEB", "web"),
        ("crypto", "crypto"),
        ("reverse", "reverse"),
        ("REVERSE", "reverse"),
        ("pwn", "pwn"),
        ("PWN", "pwn"),
        ("misc", "misc"),
        ("MISC", "misc"),
        # 线上真出现过的脏值
        ("第01题 web入门", "web"),
        ("第02题 web入门", "web"),
        ("ir-root-cause", "ir"),
        ("basic", "misc"),
        ("real", "misc"),
        # 中文
        ("应急响应", "ir"),
        ("取证", "misc"),
        ("隐写", "misc"),
        ("逆向", "reverse"),
        ("密码学", "crypto"),
        # 兜底
        ("", "other"),
        (None, "other"),
        ("完全陌生的分类", "other"),
    ],
)
def test_canonical_category_mapping(raw: str | None, expected: str) -> None:
    assert canonical_category(raw) == expected


def test_canonical_category_always_in_known_set() -> None:
    """归一的返回值必须落在声明的七类里 —— 否则前端筛选器会出现
    "选不中"的幽灵键。"""
    samples = ["web", "REVERSE", "第01题 web入门", "ir-root-cause", "basic", "", "???"]
    for raw in samples:
        assert canonical_category(raw) in CANONICAL_CATEGORIES


# ══════════════════════════════════════════════════════════
# 二、接口：投影到位 + 原文不动 + 内置来源
# ══════════════════════════════════════════════════════════


def test_created_challenge_projects_canonical_and_keeps_raw(ready_client: TestClient) -> None:
    """建一道本地 IR 任务：投影归一到 ir，但 `category` 原文仍是 `ir-root-cause`。"""
    response = ready_client.post(LOCAL_IR, json={"title": "入侵复盘"})
    assert response.status_code == 201, response.text
    challenge = response.json()["challenge"]

    # 投影：归一
    assert challenge["canonicalCategory"] == "ir"
    # 原文：一字未动（sandbox_profile 依赖它）
    assert challenge["category"] == "ir-root-cause"
    # 来源：本地任务没有平台账号 → 落回内置
    assert challenge["platformSlug"] == "ctf2"
    assert challenge["platformName"] == "CTF2（内置）"


def test_list_endpoint_projects_every_row(ready_client: TestClient) -> None:
    """列表接口每行都带 canonicalCategory / platformSlug / platformName。"""
    for title in ("甲", "乙", "丙"):
        assert ready_client.post(LOCAL_IR, json={"title": title}).status_code == 201

    listing = ready_client.get(f"{API}/kernel/challenges")
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert len(rows) >= 3
    for row in rows:
        assert row["canonicalCategory"] in CANONICAL_CATEGORIES
        assert row["platformSlug"]
        assert row["platformName"]


def test_raw_category_is_never_mutated_in_db(ready_client: TestClient, db_session: Session) -> None:
    """★ 关键回归：拉一次接口后，库里的 `category` 原文必须不变。

    这条防的是"投影不小心写回原列"—— 那会让 `sandbox_profile`
    在后续同步时选错镜像，而且**没有报错**，只在做题时表现为"环境不对"。
    """
    created = ready_client.post(LOCAL_IR, json={"title": "原文守卫"}).json()["challenge"]
    challenge_id = created["id"]

    stored_before = db_session.get(KernelChallenge, challenge_id)
    assert stored_before is not None
    assert stored_before.category == "ir-root-cause"

    # 打两次读接口（列表 + 详情），再回看库
    ready_client.get(f"{API}/kernel/challenges")
    ready_client.get(f"{API}/kernel/challenges/{challenge_id}")

    db_session.expire_all()
    stored_after = db_session.get(KernelChallenge, challenge_id)
    assert stored_after is not None
    assert stored_after.category == "ir-root-cause"  # 原文未动


# ══════════════════════════════════════════════════════════
# 三、来源：配置平台 vs 内置
# ══════════════════════════════════════════════════════════


def _seed_platform_challenge(db_session: Session, slug: str, display_name: str) -> str:
    """造一个"来自某配置平台"的题目链：platform → account → competition → challenge。

    返回 challenge 的 id。为了让来源推导命中，需要 platform.driver != 'ctf2'。
    """
    from app.kernel.models import KernelAccount

    platform = KernelPlatform(
        id=f"knplat_{slug}",
        slug=slug,
        display_name=display_name,
        base_url="https://example.test/slab-match/api/v1/agent",
        driver="generic",
        source="generated",
        status="verified",
    )
    db_session.add(platform)
    db_session.flush()

    account = KernelAccount(
        id=f"ctf2acc_{slug}",
        platform_id=platform.id,
        label=f"acc-{slug}",
        token_encrypted="x",
        token_hint="****",
        status="active",
    )
    db_session.add(account)
    db_session.flush()

    competition = KernelCompetition(
        id=f"ctf2comp_{slug}",
        account_id=account.id,
        remote_id=f"remote-{slug}",
        kind="competition",
        name=f"赛事-{slug}",
    )
    db_session.add(competition)
    db_session.flush()

    challenge = KernelChallenge(
        id=f"knchal_{slug}",
        competition_id=competition.id,
        remote_id=f"ex-{slug}",
        title=f"题-{slug}",
        category="WEB",  # 脏值：大写
    )
    db_session.add(challenge)
    db_session.commit()
    return challenge.id


def test_challenge_from_configured_platform_reports_its_slug(
    ready_client: TestClient, db_session: Session
) -> None:
    """来自配置平台（slug=pro-dasctf-com）的题，来源应是该平台，而不是内置。"""
    challenge_id = _seed_platform_challenge(db_session, "pro-dasctf-com", "DASCTF Pro（slab-match）")

    row = ready_client.get(f"{API}/kernel/challenges/{challenge_id}")
    assert row.status_code == 200, row.text
    body = row.json()
    assert body["platformSlug"] == "pro-dasctf-com"
    assert body["platformName"] == "DASCTF Pro（slab-match）"
    # 脏分类照样归一
    assert body["canonicalCategory"] == "web"
    assert body["category"] == "WEB"  # 原文未动


def test_builtin_driver_platform_falls_back_to_ctf2_label(
    ready_client: TestClient, db_session: Session
) -> None:
    """即便库里有 `driver='ctf2'` 的平台行，展示也应落回「CTF2（内置）」。"""
    from app.kernel.models import KernelAccount

    platform = KernelPlatform(
        id="knplat_builtin_row",
        slug="ctf2-legacy",
        display_name="某个 CTF2 平台",
        driver="ctf2",  # 内置驱动
        source="builtin",
        status="verified",
    )
    db_session.add(platform)
    db_session.flush()
    account = KernelAccount(
        id="ctf2acc_builtin_row",
        platform_id=platform.id,
        label="acc-builtin",
        token_encrypted="x",
        token_hint="****",
        status="active",
    )
    db_session.add(account)
    db_session.flush()
    competition = KernelCompetition(
        id="ctf2comp_builtin_row",
        account_id=account.id,
        remote_id="r-builtin",
        kind="competition",
        name="内置赛事",
    )
    db_session.add(competition)
    db_session.flush()
    challenge = KernelChallenge(
        id="knchal_builtin_row",
        competition_id=competition.id,
        remote_id="ex-builtin",
        title="内置题",
        category="pwn",
    )
    db_session.add(challenge)
    db_session.commit()

    body = ready_client.get(f"{API}/kernel/challenges/knchal_builtin_row").json()
    assert body["platformSlug"] == "ctf2"
    assert body["platformName"] == "CTF2（内置）"


# ══════════════════════════════════════════════════════════
# 四、性能：列表不 N+1
# ══════════════════════════════════════════════════════════


def test_list_builds_platform_index_once(ready_client: TestClient, db_session: Session, monkeypatch) -> None:
    """★ 列表接口对多道题**只建一次**来源索引（否则就是 N+1）。

    做法：把 `_resolve_platform_info` 包一层计数器，断言它被调用 1 次
    （而不是每道题 1 次）。
    """
    from app.kernel import router as kernel_router

    for title in ("题1", "题2", "题3", "题4"):
        assert ready_client.post(LOCAL_IR, json={"title": title}).status_code == 201

    calls: list[int] = []
    original = kernel_router._resolve_platform_info

    def _counting(db, challenges):  # noqa: ANN001
        calls.append(len(challenges))
        return original(db, challenges)

    monkeypatch.setattr(kernel_router, "_resolve_platform_info", _counting)

    listing = ready_client.get(f"{API}/kernel/challenges")
    assert listing.status_code == 200, listing.text
    # 列表一次调用，且一次传入全部题
    assert len(calls) == 1
    assert calls[0] >= 4
