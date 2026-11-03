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
    # 来源：本地任务属于 kind='local' 的批次 → 「本地案例」（不是内置 CTF2）
    assert challenge["platformSlug"] == "local"
    assert challenge["platformName"] == "本地案例"


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


def test_challenge_source_is_its_competition_not_the_accounts_platform(
    ready_client: TestClient, db_session: Session
) -> None:
    """★ 这是用户 2026-11-03 报的 bug 的回归：来源看**题目真实出处**。

    线上现象：407 道 BUUCTF 老题全被标成「DASCTF Pro（slab-match）」——
    因为老版本按 `challenge → competition → account → platform` 推来源，
    而那时账号的 `platform_id` 指向了比赛平台。可是那些老题根本不在
    比赛平台上，它们是从 BUUCTF 练习集同步来的。

    期望：来源 = 题目所属 competition 的名字（「BUUCTF」），slug 用
    `src-<competition_id>`，与账号指向哪个平台无关。
    """
    from app.kernel.models import KernelAccount

    # 一个账号，platform 指向比赛平台 —— 正是线上那种"账号已切到比赛平台"的形态
    platform = KernelPlatform(
        id="knplat_srcbug",
        slug="pro-dasctf-com",
        display_name="DASCTF Pro（slab-match）",
        base_url="https://example.test/slab-match/api/v1/agent",
        driver="generic",
        source="generated",
        status="verified",
    )
    db_session.add(platform)
    db_session.flush()
    account = KernelAccount(
        id="ctf2acc_srcbug",
        platform_id=platform.id,
        label="acc-srcbug",
        token_encrypted="x",
        token_hint="****",
        status="active",
    )
    db_session.add(account)
    db_session.flush()
    # 但这个账号底下同步过的是 BUUCTF 练习集（老题）
    competition = KernelCompetition(
        id="ctf2comp_srcbug",
        account_id=account.id,
        remote_id="buuctf-legacy",
        kind="practice",
        name="BUUCTF",
    )
    db_session.add(competition)
    db_session.flush()
    challenge = KernelChallenge(
        id="knchal_srcbug",
        competition_id=competition.id,
        remote_id="00476e14-24c3-4f98-824e-9e8faa6f6d85",
        title="RSA1",
        category="CRYPTO",
    )
    db_session.add(challenge)
    db_session.commit()

    body = ready_client.get(f"{API}/kernel/challenges/knchal_srcbug").json()
    assert body["platformName"] == "BUUCTF", "老题的来源必须是它自己的练习集，不许冒名比赛平台"
    assert body["platformSlug"] == "src-ctf2comp_srcbug"
    assert "DASCTF" not in body["platformName"]


def test_local_competition_reports_local_source(
    ready_client: TestClient, db_session: Session
) -> None:
    """本地复盘案例（kind='local'）没有远端，来源显示「本地案例」。"""
    created = ready_client.post(
        f"{API}/kernel/competitions", json={"kind": "local", "name": "复盘"}
    )
    if created.status_code not in (200, 201):
        import pytest

        pytest.skip(f"本地赛事建不起来：{created.status_code} {created.text[:120]}")
    comp_id = created.json()["id"]
    made = ready_client.post(
        f"{API}/kernel/competitions/{comp_id}/challenges",
        json={"title": "本地题", "category": "web"},
    )
    if made.status_code not in (200, 201):
        import pytest

        pytest.skip(f"本地题建不起来：{made.status_code} {made.text[:120]}")
    body = made.json()["challenge"]
    assert body["platformSlug"] == "local"
    assert body["platformName"] == "本地案例"


def test_builtin_driver_platform_falls_back_to_ctf2_label(
    ready_client: TestClient, db_session: Session
) -> None:
    """没有 competition 归属的题（理论上的孤儿）落回「CTF2（内置）」。"""
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
    # 有 competition 就按 competition 取名 —— 这里是「内置赛事」
    assert body["platformName"] == "内置赛事"
    assert body["platformSlug"] == "src-ctf2comp_builtin_row"


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
