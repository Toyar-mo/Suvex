"""`POST /ctf2/ir/tasks` 的路由层测试（v0.43）。

## 为什么这组测试打真实 HTTP

与 `test_router_audit.py` 同一条纪律：直接调 `service.create_local_review()`
只能证明"服务函数能用"，证明不了"那个端点真的把它接上了、真的认领了附件、
真的写了审计"。而这类需求最常见的失败方式正是**端点漏了一步** ——
编译期不报错、服务函数测试全绿、界面上点完看着也成功。

具体到这个端点，漏一步的症状分别是：

· 漏 `_claim_ir_attachments` → 任务建出来了，但沙箱里没有证据包，
  模型会说"我没有看到任何日志"，而它说的**是实话**；
· 漏 `audit.record` → 界面正常，事后没人能回答"谁把一批生产日志交给了
  智能体"；
· 漏 `challenge.attachments` 回写 → 界面上"证据包 0 份"，
  而文件其实在盘上。

## ★ 附件为什么必须走暂存

任务一创建就会被调度器毫秒级认领，"建完再传"的窗口不存在。这条在
扫描附件上已经被实测抓过一次（第一版那样设计，测试当场拿到 409）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditLog
from app.ctf2 import attachments as attachments_mod
from app.ctf2.models import Ctf2Challenge, Ctf2Competition

API = "/api/v1"
IR = f"{API}/ctf2/ir/tasks"


def _stage(client: TestClient, files: list[tuple[str, bytes]]) -> str:
    """把文件暂存起来，返回 stagingId。

    ⚠️ 走的是 `POST /scans/attachments/staging` —— 不是另开一个
    `/ctf2/ir/attachments`。附件暂存是**跨域**能力（扫描与复盘都要），
    复制一份的话两条路的 TTL、上限、文件名净化迟早漂移。
    """
    response = client.post(
        f"{API}/scans/attachments/staging",
        files=[("files", (name, content, "application/octet-stream")) for name, content in files],
    )
    assert response.status_code == 201, response.text
    return response.json()["stagingId"]


@pytest.fixture(autouse=True)
def _never_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """★★ 把 `service._enqueue` 换成记录器 —— **绝不让真 worker 起来**。

    ## 为什么这三条用例必须这么做

    它们打的是 `POST .../solve`，而那个端点走到最后会调 `_enqueue`，
    真的把题丢给调度器、起一个跑 strix 的线程。本机没有 strix，
    那个线程会一直挂着等 —— 症状不是"用例失败"，而是**整个测试会话
    卡死**（实测：`test_ir_task_router.py` + `test_local_review.py`
    一起跑，跑过 560 秒仍不返回，最后被超时杀掉）。

    而这里要验证的东西**在入队之前就已经全部发生**了：账号检查、
    `_assert_submission_supported`、provider 检查。入队本身是另一件事，
    由 `test_scheduler.py` 单独测。

    记录而不是丢弃：断言"确实走到了入队"才证明四道闸真的过了 ——
    只看 200 也可能是因为别的原因提前返回。
    """
    calls: list[tuple] = []

    def _record(challenge_id: str, *, attempt_id: str | None = None) -> None:
        calls.append((challenge_id, attempt_id))

    monkeypatch.setattr("app.ctf2.service._enqueue", _record)
    return calls


def _create(client: TestClient, **body: object) -> dict:
    response = client.post(IR, json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ══════════════════════════════════════════════════════════
# 一、建任务：最少入参就能建出来
# ══════════════════════════════════════════════════════════


def test_create_task_with_only_a_title(ready_client: TestClient) -> None:
    """只给标题就能建 —— 其余全由服务端定。

    这是"入口可用"的最低标准：点进去、输入一句话、点创建。
    如果这里要求先选分类、先选镜像、先绑账号，那个入口在答辩现场
    是演示不出来的。
    """
    body = _create(ready_client, title="web-01 入侵复盘")

    challenge = body["challenge"]
    assert challenge["title"] == "web-01 入侵复盘"
    assert challenge["category"] == "ir-root-cause"
    assert challenge["sandboxProfile"] == "ctf2-sandbox-ir"
    assert challenge["status"] == "unsolved"


def test_create_task_does_not_require_ctf2_account(ready_client: TestClient, db_session: Session) -> None:
    """★★ 没有绑 CTF2 账号也能建任务。

    这是把 `account_id` 改成可空的全部理由：应急响应复盘看的是一批
    **本机上传的日志**，整个流程一次都不碰平台域名。要求先绑一个 CTF2
    PAT 才能复盘一台被入侵的机器，是把两件无关的事绑在一起。
    """
    from app.ctf2.models import Ctf2Account

    # 确认前提：这个库里真的没有账号
    assert db_session.execute(select(Ctf2Account)).scalars().first() is None

    body = _create(ready_client, title="没绑账号也能建")

    assert body["challenge"]["id"].startswith("ctf2chal_")


def test_create_task_places_it_in_local_batch(ready_client: TestClient, db_session: Session) -> None:
    """任务落在 `kind='local'` 的那个批次里。"""
    body = _create(ready_client, title="落在哪")

    challenge = db_session.get(Ctf2Challenge, body["challenge"]["id"])
    assert challenge is not None
    batch = db_session.get(Ctf2Competition, challenge.competition_id)
    assert batch is not None
    assert batch.kind == "local"
    assert batch.account_id is None


def test_two_tasks_share_one_batch(ready_client: TestClient, db_session: Session) -> None:
    """建两道题只产生**一个**批次。

    每道题单开一个批次的话，界面上"本地复盘"会越堆越多，而每个里面
    只有一道题 —— 不会报错，但很难收拾。
    """
    first = _create(ready_client, title="甲")
    second = _create(ready_client, title="乙")

    a = db_session.get(Ctf2Challenge, first["challenge"]["id"])
    b = db_session.get(Ctf2Challenge, second["challenge"]["id"])
    assert a is not None and b is not None
    assert a.competition_id == b.competition_id


def test_title_is_required(ready_client: TestClient) -> None:
    """空标题被拒 —— 它要进报告标题与工单文件名。

    ⚠️ 断言 400 而不是 FastAPI 默认的 422：本项目把请求体校验失败统一
       转成 400（`app/core/errors.py` 的 handler），因为对调用方来说
       "你给的字段不合规"和"你给的字段不存在"是同一类问题。
       写 422 的话这条测试会红，而红的原因与本测试要守的东西无关。
    """
    assert ready_client.post(IR, json={"title": ""}).status_code == 400


def test_unknown_field_is_rejected(ready_client: TestClient) -> None:
    """`StrictModel` 拒未知字段。

    这条不是洁癖：如果调用方能传 `sandboxProfile`，就会出现"界面挑了
    一个不含 tshark 的镜像"，而症状是模型说"我没有工具"，不是"镜像选错了"。
    """
    response = ready_client.post(IR, json={"title": "x", "sandboxProfile": "ctf2-sandbox"})

    # 400 而非 422，理由同上。
    assert response.status_code == 400


def test_brief_is_carried_into_description(ready_client: TestClient, db_session: Session) -> None:
    """`brief` 会落进 `description` —— 提示词从那里读"本次复盘目标"。"""
    body = _create(ready_client, title="带目标", brief="只关心横向移动那一段")

    challenge = db_session.get(Ctf2Challenge, body["challenge"]["id"])
    assert challenge is not None
    assert "横向移动" in (challenge.description or "")


# ══════════════════════════════════════════════════════════
# 二、附件认领：暂存 → 认领 → worker 能看见
# ══════════════════════════════════════════════════════════


def test_attachments_are_claimed_into_challenge_root(
    ready_client: TestClient, db_session: Session
) -> None:
    """★★ 认领的落点必须是 `attachment_root(challenge_id)`。

    这是整条链上最容易接错的一处：`uploads.claim()` 搬进的是**扫描**的
    附件目录，而 worker 读的是 `_ctf2_attachments/<challengeId>/`。
    用错了函数的表现是"上传成功、任务建成功、模型说没有证据"——
    三个绿色信号加一个错误结论。
    """
    staging = _stage(ready_client, [("auth.log", b"Accepted password for deploy\n")])

    body = _create(ready_client, title="带附件", attachmentStagingId=staging)

    challenge_id = body["challenge"]["id"]
    root = attachments_mod.attachment_root(challenge_id)
    assert (root / "auth.log").is_file()
    assert (root / "auth.log").read_bytes() == b"Accepted password for deploy\n"


def test_claimed_attachments_are_reported_back(ready_client: TestClient) -> None:
    """回执里要列出认领到哪几份 —— 界面靠它显示"证据包 N 份"。"""
    staging = _stage(ready_client, [("a.log", b"x"), ("b.txt", b"y")])

    body = _create(ready_client, title="两份", attachmentStagingId=staging)

    names = {item["name"] for item in body["attachments"]}
    assert names == {"a.log", "b.txt"}
    assert body["notice"] is None


def test_claimed_attachments_are_recorded_on_challenge(
    ready_client: TestClient, db_session: Session
) -> None:
    """附件元数据要回写进 `challenge.attachments`。

    不回写的话界面显示"证据包 0 份"，而文件其实在盘上 —— 用户会以为
    上传失败了，然后再传一遍。
    """
    staging = _stage(ready_client, [("auth.log", b"data")])

    body = _create(ready_client, title="回写", attachmentStagingId=staging)

    challenge = db_session.get(Ctf2Challenge, body["challenge"]["id"])
    assert challenge is not None
    names = [item.get("name") for item in (challenge.attachments or [])]
    assert "auth.log" in names


def test_fingerprint_sidecar_is_not_claimed(ready_client: TestClient) -> None:
    """★ `.sha256` 旁挂文件不能当成证据交给模型。

    `attachments.FINGERPRINT_SUFFIX` 是同步逻辑写下的哈希旁挂文件。
    不排除的话 `_attachment_paths` 会把它当一份证据，而模型会去读一个
    只有一行哈希的文件，然后报告"这份证据是空的"—— 它说的还是实话，
    但结论完全错。
    """
    staging = _stage(
        ready_client,
        [
            ("auth.log", b"real evidence"),
            (f"auth.log{attachments_mod.FINGERPRINT_SUFFIX}", b"sha256:deadbeef\n"),
        ],
    )

    body = _create(ready_client, title="排除指纹", attachmentStagingId=staging)

    names = {item["name"] for item in body["attachments"]}
    assert names == {"auth.log"}


def test_missing_staging_batch_still_creates_task(ready_client: TestClient) -> None:
    """★ 批次不存在/过期时**照建不误**，只在回执里说明。

    拒绝创建会让"先想起来再补附件"这条常见流程走不通 —— 而附件丢了
    是次要问题，让人建不出任务是主要问题（与 `uploads.claim` 的幂等
    语义同一条理由）。
    """
    body = _create(ready_client, title="批次过期", attachmentStagingId="stage_does_not_exist")

    assert body["challenge"]["id"]
    assert body["attachments"] == []
    assert body["notice"]
    assert "过期" in body["notice"] or "不存在" in body["notice"]


def test_task_without_attachments_is_allowed(ready_client: TestClient) -> None:
    """完全不传附件也能建 —— 用户可以建完再从题面补。"""
    body = _create(ready_client, title="先建后补")

    assert body["attachments"] == []
    assert body["notice"] is None


# ══════════════════════════════════════════════════════════
# 三、审计：谁把一批生产日志交给了智能体
# ══════════════════════════════════════════════════════════


def test_creating_a_task_is_audited(ready_client: TestClient, db_session: Session) -> None:
    """★★ 建复盘任务必须留痕，且用**专用**动作名。

    复用 `CTF2_SOLVE_START` 的话，审计列表上"应急响应"与"打 CTF 题"
    混成同一个动作 —— 而这两件事在追责时是两个不同的问题。
    """
    body = _create(ready_client, title="留痕")

    rows = list(
        db_session.execute(
            select(AuditLog).where(AuditLog.action == "ir.task_create")
        ).scalars()
    )
    assert len(rows) == 1
    row = rows[0]
    assert body["challenge"]["id"] in (row.target or "")
    assert "留痕" in row.summary


def test_audit_records_the_evidence_package_size(
    ready_client: TestClient, db_session: Session
) -> None:
    """审计详情里要有"带了几份证据"。

    事后回答"那次复盘看的是什么"需要这个数：一份也没带和带了三十份，
    是两次性质不同的操作。
    """
    staging = _stage(ready_client, [("a.log", b"1"), ("b.log", b"2")])
    _create(ready_client, title="带两份", attachmentStagingId=staging)

    row = db_session.execute(
        select(AuditLog).where(AuditLog.action == "ir.task_create")
    ).scalars().first()
    assert row is not None
    detail = row.detail or {}
    assert set(detail.get("attachments") or []) == {"a.log", "b.log"}


def test_audit_records_the_resolved_profile(
    ready_client: TestClient, db_session: Session
) -> None:
    """审计里记下用了哪个镜像 —— 复盘失败时第一个要问的就是它。"""
    _create(ready_client, title="镜像留痕")

    row = db_session.execute(
        select(AuditLog).where(AuditLog.action == "ir.task_create")
    ).scalars().first()
    assert row is not None
    assert (row.detail or {}).get("sandboxProfile") == "ctf2-sandbox-ir"


# ══════════════════════════════════════════════════════════
# 四、与既有 CTF2 路径的隔离
# ══════════════════════════════════════════════════════════


def test_local_batch_is_not_listed_as_platform_batch(
    ready_client: TestClient, ctf2_competition: Ctf2Competition
) -> None:
    """本地批次会出现在 `/ctf2/competitions` 里，但 `accountId` 是 null。

    这条测的是"界面能不能正确处理 null"：`Ctf2CompetitionOut.accountId`
    若坚持 `str`，序列化会抛 `ValidationError`，表现是整个端点 500、
    页面全白 —— 而它在加了 local 之前永远不会发生。
    """
    _create(ready_client, title="本地批次可见")

    response = ready_client.get(f"{API}/ctf2/competitions")
    assert response.status_code == 200, response.text

    rows = response.json()
    local = [row for row in rows if row["kind"] == "local"]
    assert len(local) == 1
    assert local[0]["accountId"] is None


def test_challenges_endpoint_shows_ir_task(ready_client: TestClient) -> None:
    """建出来的任务在 `/ctf2/challenges` 里可见（前端页面靠它列表）。"""
    body = _create(ready_client, title="列表可见")

    response = ready_client.get(f"{API}/ctf2/challenges")
    assert response.status_code == 200, response.text
    ids = {row["id"] for row in response.json()}
    assert body["challenge"]["id"] in ids


def test_solve_on_local_task_does_not_need_platform(
    ready_client: TestClient, provider_id: str, _never_enqueue: list[tuple]
) -> None:
    """★★ 本地任务能过 `start_solve` 的四道前置，并真的走到入队。

    这是本次改动真正要打通的那一段：建得出任务、点得动"开始复盘"。
    在此之前这里会得到 `CTF2_NOT_BOUND`（没绑账号）或
    `CTF2_COMPETITION_SUBMIT_UNSUPPORTED`（kind 不是 practice）。

    ⚠️ 断言"走到了入队"而不只是"返回 200"：200 也可能来自某个我没想到的
    提前返回，那样这道用例会在功能其实没接通时保持绿色。
    """
    body = _create(ready_client, title="能开跑")
    challenge_id = body["challenge"]["id"]

    response = ready_client.post(f"{API}/ctf2/challenges/{challenge_id}/solve", json={})

    assert response.status_code in (200, 202), response.text
    assert [c[0] for c in _never_enqueue] == [challenge_id]


def test_platform_challenge_still_needs_account(
    ready_client: TestClient,
    db_session: Session,
    ctf2_challenge: Ctf2Challenge,
    provider_id: str,
    _never_enqueue: list[tuple],
) -> None:
    """★★ 反向断言：**平台题照旧要求绑账号**。

    这是本次改动最危险的失败模式 —— 为了让复盘任务跑起来，很容易把
    "必须绑账号"一起改松。那不会报错，症状是"某道需要平台的题进到了
    一个没有凭据的流程里"，错误在很深的地方才出现。

    ## ⚠️ 为什么把账号置为 expired 而不是"没有账号"

    `ctf2_challenge` 夹具依赖 `ctf2_competition`，后者依赖 `ctf2_account`
    —— 也就是说用那道题的测试，库里**必然已经有一个 active 账号**。
    第一版这条测试就是这么写的（断言 409 `CTF2_NOT_BOUND`），结果拿到
    200：账号在，检查自然通过。

    这不是测试写得"不够严"，而是它指向了一个更真实的问题：
    **"有账号"与"账号能用"是两件事**。`get_account(active_only=True)`
    只认 `status='active'`，所以带一个过期账号跑平台题，仍然应该被挡。
    用过期账号测，比清空账号更接近生产会遇到的场景。
    """
    # 把账号置为 expired —— `get_account(active_only=True)` 就不认它了。
    from sqlalchemy import update

    from app.ctf2.models import Ctf2Account

    db_session.execute(update(Ctf2Account).values(status="expired"))
    db_session.commit()

    response = ready_client.post(
        f"{API}/ctf2/challenges/{ctf2_challenge.id}/solve", json={}
    )

    assert response.status_code == 409
    # ⚠️ 错误体的形状是 `{"error": {"code": ...}}`，不是平铺的 `{"code": ...}`
    #   —— 与 `test_router_auth_matrix.py` 的断言保持一致。
    assert response.json()["error"]["code"] == "CTF2_NOT_BOUND"
    # ★ 被挡下时**不能**已经入队 —— 否则 worker 会拿一个没有凭据的题开跑，
    #   而错误要到很深的沙箱启动阶段才出现。
    assert _never_enqueue == []


def test_local_task_runs_without_any_usable_account(
    ready_client: TestClient,
    db_session: Session,
    provider_id: str,
    _never_enqueue: list[tuple],
) -> None:
    """★★ 反向对照：账号**不可用**时，本地复盘任务照旧能开跑。

    这一条与上一条是同一个前提（没有可用账号）下的两个不同结果 ——
    合起来才证明 `_requires_platform` 真的在起作用，而不是"整体放松了"。
    """
    from sqlalchemy import update

    from app.ctf2.models import Ctf2Account

    db_session.execute(update(Ctf2Account).values(status="expired"))
    db_session.commit()

    body = _create(ready_client, title="账号过期也能复盘")
    response = ready_client.post(
        f"{API}/ctf2/challenges/{body['challenge']['id']}/solve", json={}
    )

    assert response.status_code in (200, 202), response.text
    assert len(_never_enqueue) == 1, "没走到入队 —— 四道前置里有一道没放行"

