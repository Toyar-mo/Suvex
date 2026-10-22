"""题目附件的下载（把清单变成磁盘上的真实文件）。

## 为什么这个文件必须存在

同步只把 `attachments` 清单存进 JSONB 时，解题的 agent 看到的是一个
**空目录** —— 而很多题的核心就在附件里（一个 ELF、一份流量包、一段密文）。
于是"模型太笨"和"它手里根本没有文件"这两件事在界面上长得一模一样。

## 测什么、为什么这么测

三条硬约束各有一条**反面**用例（不是"能下就行"）：

* 官方域名白名单 → 站外 URL 必须被拒，而且**一个请求都不许发出去**
  （只断言"没落盘"是不够的：请求发出去本身就已经打到了内网）；
* 大小上限 → 声明的 content-length 超限时**不写文件**；
* 文件名净化 → 路径穿越的附件名落不到目录外。

另外两条是关于**诚实**的：半截文件必须被删掉（留着比没有更坏 ——
agent 会把它当成完整附件去分析），以及"已存在且大小一致"要跳过
（同步会反复跑，几十 MB 的附件重下是真实成本）。
"""

from __future__ import annotations

from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel import attachments
from tests.ctf2.conftest import patch_ctf2_transport

ZIP_BODY = b"PK\x03\x04 not really a zip"


# ══════════════════════════════════════════════════════════
# 纯函数：名字净化与清单解析
# ══════════════════════════════════════════════════════════


def test_sanitize_filename_kills_directory_components() -> None:
    """★ 白名单式净化：只留基名。

    这三条都是**真实会遇到的**形状：平台可能把整个路径当名字给过来，
    而出题人完全可以在 `attachments` 里写 `../../etc/passwd`。
    """
    assert attachments.sanitize_filename("../../etc/passwd") == "passwd"
    assert attachments.sanitize_filename("/etc/shadow") == "shadow"
    # Windows 风格的分隔符也要处理：URL 里可能带 `\`
    assert attachments.sanitize_filename("a\\b\\c.txt") == "c.txt"


def test_sanitize_filename_never_returns_empty() -> None:
    """★ 净化后**不能**是空串。

    空串拼进路径得到的是**目录本身**，而 `write_bytes` 在目录上失败时
    报的是权限/IsADirectory 之类的错 —— 与"名字被净化没了"毫无关系，
    排查方向会完全跑偏。
    """
    for raw in ("..", ".", "   ", "...", ""):
        cleaned = attachments.sanitize_filename(raw)
        assert cleaned, f"{raw!r} 净化成了空串"
        assert cleaned not in {".", ".."}
        assert "/" not in cleaned


def test_sanitize_filename_keeps_chinese_names() -> None:
    """中文附件名要保留：题面里常常直接引用它（`题目描述.txt`）。"""
    assert attachments.sanitize_filename("题目描述.txt") == "题目描述.txt"
    # URL 编码的中文名要能解回来
    assert attachments.sanitize_filename("%E9%A2%98%E7%9B%AE.txt") == "题目.txt"


def test_plan_downloads_recognises_the_three_shapes() -> None:
    """平台可能给的三种形状都要认（裸字符串 / `name+url` / `filename+download_url`）。

    认不出来就**跳过那一条**，不猜 —— 一条附件认不出来只让那道题少一个
    文件，而"猜一个 URL"会让服务端去访问一个我们没打算访问的地址。
    """
    planned = attachments.plan_downloads(
        "chal_1",
        [
            "https://ctf2.dasctf.com/f/a.zip",
            {"name": "b.bin", "url": "https://ctf2.dasctf.com/f/b.bin"},
            {"filename": "c.txt", "download_url": "https://ctf2.dasctf.com/f/c.txt"},
            {"note": "没有 url，认不出来"},
            {"name": "只有名字"},
            42,
        ],
    )
    assert [item.name for item in planned] == ["a.zip", "b.bin", "c.txt"]
    assert all(item.challenge_id == "chal_1" for item in planned)


def test_plan_downloads_uses_the_url_tail_when_no_name_is_given() -> None:
    planned = attachments.plan_downloads(
        "chal_1", ["https://ctf2.dasctf.com/files/deep/path/real-name.bin"]
    )
    assert [item.name for item in planned] == ["real-name.bin"]


def test_plan_downloads_caps_the_count() -> None:
    """一个附件字段里塞一千条 URL 时不能真的下一千个。"""
    urls = [f"https://ctf2.dasctf.com/f/{index}.bin" for index in range(200)]
    planned = attachments.plan_downloads("chal_1", urls)
    assert len(planned) == attachments.MAX_FILES_PER_CHALLENGE


def test_plan_downloads_tolerates_a_non_list() -> None:
    """平台把 `attachments` 给成字符串/对象时返回空表，而不是抛。"""
    assert attachments.plan_downloads("chal_1", None) == []
    assert attachments.plan_downloads("chal_1", "https://ctf2.dasctf.com/a.zip") == []
    assert attachments.plan_downloads("chal_1", {"url": "https://x"}) == []


# ══════════════════════════════════════════════════════════
# 下载：三条硬约束的反面用例
# ══════════════════════════════════════════════════════════


async def test_foreign_host_is_refused_without_any_request(monkeypatch) -> None:
    """★ 站外附件必须被拒，而且**一个请求都不许发**。

    只断言"没落盘"是不够的：请求一旦发出去，我们就已经替出题人
    访问了那个地址（内网、云元数据、我们自己的 API 都在射程内）。
    所以这里断言 `len(recorded) == 0` —— 那才是这条约束的真正内容。
    """
    recorded = patch_ctf2_transport(monkeypatch, lambda request: httpx.Response(200))

    result = await attachments.download_all(
        "pat_x",
        [
            attachments.PendingDownload(
                challenge_id="chal_evil",
                url="http://169.254.169.254/latest/meta-data/",
                name="meta",
            )
        ],
    )

    assert len(recorded) == 0, f"竟然发出了请求：{recorded.urls}"
    assert result.downloaded == 0
    assert result.failed and "官方域名" in result.failed[0]
    assert not (attachments.attachment_root("chal_evil") / "meta").exists()


async def test_lookalike_host_is_refused(monkeypatch) -> None:
    """看起来像官方域名的站外地址同样被拒（后缀匹配的经典坑）。"""
    recorded = patch_ctf2_transport(monkeypatch, lambda request: httpx.Response(200))
    result = await attachments.download_all(
        "pat_x",
        [
            attachments.PendingDownload(
                challenge_id="chal_lookalike",
                url="https://ctf2.dasctf.com.evil.example/a.zip",
                name="a.zip",
            )
        ],
    )
    assert len(recorded) == 0
    assert result.failed


async def test_oversized_attachment_is_refused_before_writing(monkeypatch) -> None:
    """声明的大小超上限时**不写文件**（而不是下完再判）。"""
    too_big = attachments.MAX_FILE_BYTES + 1
    patch_ctf2_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=b"x", headers={"content-length": str(too_big)}, request=request
        ),
    )

    result = await attachments.download_all(
        "pat_x",
        [
            attachments.PendingDownload(
                challenge_id="chal_big",
                url="https://ctf2.dasctf.com/f/big.bin",
                name="big.bin",
            )
        ],
    )

    assert result.downloaded == 0
    assert result.failed and "上限" in result.failed[0]
    assert not (attachments.attachment_root("chal_big") / "big.bin").exists()


async def test_partial_file_is_deleted_when_the_transfer_breaks(monkeypatch) -> None:
    """★ 半截文件比没有更坏，所以必须删掉。

    留着它的后果不是"少一个文件"，而是"agent 把一个损坏的文件当成
    完整附件去分析"—— 它给出的结论会看起来很有道理，而方向完全错。
    """

    class ExplodingStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield b"PK\x03\x04 partial"
            raise RuntimeError("连接中断")

    patch_ctf2_transport(
        monkeypatch,
        lambda request: httpx.Response(200, stream=ExplodingStream(), request=request),
    )

    result = await attachments.download_all(
        "pat_x",
        [
            attachments.PendingDownload(
                challenge_id="chal_broken",
                url="https://ctf2.dasctf.com/f/broken.zip",
                name="broken.zip",
            )
        ],
    )

    assert result.downloaded == 0
    assert result.failed
    assert not (attachments.attachment_root("chal_broken") / "broken.zip").exists()


async def test_same_size_file_is_skipped(monkeypatch) -> None:
    """已存在且大小一致 → 跳过（同步会反复跑，重下几十 MB 是真实成本）。"""
    pending = [
        attachments.PendingDownload(
            challenge_id="chal_skip",
            url="https://ctf2.dasctf.com/f/a.zip",
            name="a.zip",
        )
    ]
    patch_ctf2_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=ZIP_BODY,
            headers={"content-length": str(len(ZIP_BODY))},
            request=request,
        ),
    )

    first = await attachments.download_all("pat_x", pending)
    assert first.downloaded == 1

    second = await attachments.download_all("pat_x", pending)
    assert second.downloaded == 0
    assert second.skipped == 1


async def test_one_failure_does_not_stop_the_batch(monkeypatch) -> None:
    """一条失败不中断整批 —— 否则"某个附件 404"会让这道题一个文件都没有。"""
    good = b"ok"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("bad.zip"):
            return httpx.Response(404, request=request)
        return httpx.Response(
            200, content=good, headers={"content-length": str(len(good))}, request=request
        )

    patch_ctf2_transport(monkeypatch, handler)

    result = await attachments.download_all(
        "pat_x",
        [
            attachments.PendingDownload(
                challenge_id="chal_mixed",
                url="https://ctf2.dasctf.com/f/bad.zip",
                name="bad.zip",
            ),
            attachments.PendingDownload(
                challenge_id="chal_mixed",
                url="https://ctf2.dasctf.com/f/good.zip",
                name="good.zip",
            ),
        ],
    )

    assert result.downloaded == 1
    assert len(result.failed) == 1
    assert (attachments.attachment_root("chal_mixed") / "good.zip").read_bytes() == good
    assert not (attachments.attachment_root("chal_mixed") / "bad.zip").exists()


async def test_token_only_travels_in_the_authorization_header(monkeypatch) -> None:
    """★ 凭据纪律：token 只在请求头里，不进 URL、不进名字。

    与 `test_client.py` 里那条同一条理由 —— 附件下载也必须守它，
    否则一次同步的日志里就会出现明文 PAT。
    """
    secret = "pat_ctf2_SECRET-abcdef"
    recorded = patch_ctf2_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=ZIP_BODY,
            headers={"content-length": str(len(ZIP_BODY))},
            request=request,
        ),
    )

    await attachments.download_all(
        secret,
        [
            attachments.PendingDownload(
                challenge_id="chal_secret",
                url="https://ctf2.dasctf.com/f/a.zip",
                name="a.zip",
            )
        ],
    )

    assert recorded.auth_headers() == [f"Bearer {secret}"]
    for url in recorded.urls:
        assert secret not in url


# ══════════════════════════════════════════════════════════
# 接线：`service.sync` 真的把附件拉下来了
# ══════════════════════════════════════════════════════════


def test_sync_downloads_attachments(
    monkeypatch, db_session: Session, ctf2_account
) -> None:
    """★ 端到端的那一条：一次同步之后磁盘上真的有附件。

    这个用例是**唯一**能证明 `service.sync` 与 `attachments` 接上了的
    测试。没有它的话，"下载模块写好了"与"同步里调了它"是两件事 ——
    而缺口正是后者（模块写完了但没接线）。
    """
    from app.kernel import service
    from app.kernel.models import Ctf2Challenge

    url = "https://ctf2.dasctf.com/files/readme.txt"
    body = b"attachment content"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/practice/"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "p1",
                        "name": "练习集",
                        "challenges": [
                            {"id": "c1", "title": "题一", "attachments": [url]}
                        ],
                    }
                ],
                request=request,
            )
        if "/practice/p1/challenges/c1/" in path:
            return httpx.Response(
                200,
                json={"id": "c1", "title": "题一", "attachments": [url]},
                request=request,
            )
        if path.endswith("/files/readme.txt"):
            return httpx.Response(
                200,
                content=body,
                headers={"content-length": str(len(body))},
                request=request,
            )
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}, request=request)

    patch_ctf2_transport(monkeypatch, handler)

    result = service.sync(db_session)

    assert result.challenges_created == 1, result.summary()
    assert result.attachments.downloaded == 1, result.summary()
    assert result.attachments.failed == []
    # 附件摘要在同步结论里可见（用户据此判断"解题失败是不是因为没附件"）
    assert "附件 1" in result.summary()

    challenge = db_session.execute(select(Ctf2Challenge)).scalars().one()
    target: Path = attachments.attachment_root(challenge.id) / "readme.txt"
    assert target.read_bytes() == body, "文件没落在那道题的目录里"


def test_sync_survives_attachment_failure(
    monkeypatch, db_session: Session, ctf2_account
) -> None:
    """★ 附件全失败**不能**让同步失败。

    题面已经落库了，而附件失败（站外链接、磁盘满）是可重试的。
    反过来做（下载放进事务里）会让一次附件失败把整场赛事的题目
    全部回滚掉 —— 那是"为了干净而丢掉已完成的工作"。
    """
    from app.kernel import service
    from app.kernel.models import Ctf2Challenge

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/practice/"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "p1",
                        "name": "练习集",
                        "challenges": [
                            {
                                "id": "c1",
                                "title": "题一",
                                "attachments": ["http://127.0.0.1/secret.zip"],
                            }
                        ],
                    }
                ],
                request=request,
            )
        if "/practice/p1/challenges/c1/" in path:
            return httpx.Response(200, json={"id": "c1", "title": "题一"}, request=request)
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}, request=request)

    patch_ctf2_transport(monkeypatch, handler)

    result = service.sync(db_session)

    assert result.challenges_created == 1
    assert result.attachments.downloaded == 0
    assert result.attachments.failed, "站外附件应当被记为失败"
    # 题面照样落库了
    assert db_session.execute(select(Ctf2Challenge)).scalars().one().title == "题一"
    # 失败也进了同步的 errors，前端能提示"附件没下全"
    assert any("附件" in item for item in result.errors)


def test_sync_explains_why_nothing_arrived(
    monkeypatch, db_session: Session, ctf2_account
) -> None:
    """★ 一道题都没同步回来时，必须说清**为什么**、以及下一步怎么办。

    这里模拟的是**两条路都断了**：平台的练习集对象里没有题目清单
    （只有 `challenge_count`），而平时用来补题的「公开解法」侧门
    也取不到内容（handler 对 `/writeups/` 回 404）。

    ⚠️ 判据必须是"**一道都没碰到**"（created 与 updated 双双为 0），
    而不是"新增 0"。第二版只看 created，于是**幂等的第二次同步**
    （`新增 0 · 更新 98`）会弹出一句"侧门没取到内容" —— 自相矛盾。
    见 `service.sync` 里那段说明。
    """
    from app.kernel import service

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/practice/"):
            # ⚠️ 刻意**不给** `challenges` 数组 —— 这正是真实平台的形状
            #    （练习集对象里有 challenge_count，但没有清单）。
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {"id": "p1", "name": "BUUCTF", "type": "ctf", "challenge_count": 5857}
                        ],
                        "total": 1,
                    },
                    "success": True,
                },
                request=request,
            )
        # 侧门也拉不到（模拟端点抖动 / PAT 缺 writeup:read）
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}, request=request)

    patch_ctf2_transport(monkeypatch, handler)

    result = service.sync(db_session)

    assert result.challenges_created == 0
    assert result.challenges_updated == 0
    assert result.note, "一道题都没有时必须给出解释"
    # 解释要给出**可执行的下一步**，而不只是一句"平台没开放"
    assert "重试" in result.note, f"没有给出可执行的下一步：{result.note}"
    assert "writeup:read" in result.note
    # 解释会被拼进给用户看的那句话里
    assert result.note in result.message()


def test_sync_harvests_challenges_from_writeups(
    monkeypatch, db_session: Session, ctf2_account
) -> None:
    """★★ 解法侧门：**这是 CTF2 上唯一能批量拿到题的路**。

    `writeups` 每条公开解法都带着 `challenge_id`，拿到 id 再去撞练习集的
    详情端点，命中的就是可提交的真题。

    这条用例把整条链钉住 —— 它比"练习集只能同步元信息"重要得多，
    因为后者曾经让整个流程看起来"拉不了题目"（用户原话），而真相是
    题目一直都在，只是要从侧门进。

    实测（2026-09-30，真平台）：98 个 id **98/98 全中**，一次同步
    拉进 97 道题 + 71 个附件。
    """
    from app.kernel import service

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/practice/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {"id": "p1", "name": "BUUCTF", "type": "ctf", "challenge_count": 5857}
                        ],
                        "total": 1,
                    },
                    "success": True,
                },
                request=request,
            )
        if path.endswith("/writeups/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {"challenge_id": "c-hit", "title": "一道有解法的题"},
                            {"challenge_id": "c-miss", "title": "配不上练习集的题"},
                        ],
                        "total": 2,
                    },
                    "success": True,
                },
                request=request,
            )
        if path.endswith("/practice/p1/challenges/c-hit/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "c-hit",
                        "name": "一道有解法的题",
                        "category": "MISC",
                        "description": "题面",
                        "files": [],
                    },
                    "success": True,
                },
                request=request,
            )
        # `c-miss` 落在所有练习集外面 —— 这是**正常**的（daily 的 25 道
        # 就是 0 命中），必须逐题容错、不中断整次同步。
        return httpx.Response(
            404, json={"error": {"code": "NOT_FOUND", "params": {"resource": "practice_challenge"}}}, request=request
        )

    patch_ctf2_transport(monkeypatch, handler)

    result = service.sync(db_session)

    assert result.challenges_created == 1, "侧门命中的那道必须落库"
    assert not result.note, "拉到题了就不该再解释'为什么没有题'"


def test_repeat_sync_reports_already_up_to_date(
    monkeypatch, db_session: Session, ctf2_account
) -> None:
    """★ 侧门有货、但一道新的都没有 —— 要说「**已是最新**」，不能说「侧门没取到内容」。

    这一条是 note 判据的第三次修正，每次都被一个真实的误导逼回来：

    · 只看 `created == 0` → 幂等同步（`新增 0 · 更新 98`）也弹"侧门没取到内容"；
    · 改成 `created == 0 and updated == 0` → 侧门补了"跳过已在本地的"之后，
      重复同步正好是 `新增 0 · 更新 0`（**这是最好的结果**），又弹同一句。

    根因：`created/updated` 描述的是"这次改了几行"，而 note 要回答的是
    "**为什么没题**" —— 两者不是一回事，得直接看侧门到底有没有货。

    误导的代价是实打实的：用户看到"没取到内容"就会反复点同步，
    而每次其实都是对的 —— 他永远停在一个错觉里。
    """
    from app.kernel import service

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/practice/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "id": "p1",
                                "name": "BUUCTF",
                                "type": "ctf",
                                "challenge_count": 5857,
                            }
                        ],
                        "total": 1,
                    },
                    "success": True,
                },
                request=request,
            )
        if path.endswith("/writeups/"):
            # 侧门**有货** —— 这正是与"侧门空"的关键区别
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [{"challenge_id": "c-known", "title": "早就拉过的题"}],
                        "total": 1,
                    },
                    "success": True,
                },
                request=request,
            )
        if path.endswith("/practice/p1/challenges/c-known/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "c-known",
                        "name": "早就拉过的题",
                        "category": "MISC",
                        "description": "题面",
                        "files": [],
                    },
                    "success": True,
                },
                request=request,
            )
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}, request=request)

    patch_ctf2_transport(monkeypatch, handler)

    # 第一次：侧门把它拉进来
    first = service.sync(db_session)
    assert first.challenges_created == 1

    # 第二次：同一个 id 已在库 → 跳过 → 一道都不新增，但**侧门本身是好用的**
    second = service.sync(db_session)
    assert second.challenges_created == 0
    assert second.challenges_updated == 0
    assert second.note, "重复同步也该给一句说明"
    assert "已是最新" in second.note, f"说的是'没取到内容'而不是'已是最新'：{second.note}"
    assert "writeup:read" not in second.note, "侧门是好用的，不该让人去查权限"


def test_attachment_root_is_outside_the_scan_directories() -> None:
    """附件的落盘位置与扫描产物**分开**。

    附件的生命周期跟着题目走（重跑还要用），而扫描产物会随保留期被清掉。
    放在同一个目录下的话，某次清理会把下次解题要用的附件一起删掉 ——
    而征兆是"这道题昨天能解、今天解不了"。
    """
    root = attachments.attachment_root("ctf2chal_probe")
    from app.core.config import get_settings

    scan_root = get_settings().scan_root
    assert root.parent.name == "_ctf2_attachments"
    assert root.parent.parent == scan_root
    assert root.name == "ctf2chal_probe"


# ══════════════════════════════════════════════════════════
# 赛事同步：一场失败不该拖垮整次
# ══════════════════════════════════════════════════════════


def test_a_failing_competition_does_not_sink_the_whole_sync(
    monkeypatch, db_session: Session, ctf2_account
) -> None:
    """★ 勾了一场拉不动的赛事时，**练习集照样要落库**。

    ## 这条是被真实事故逼出来的

    用户勾了一场自己还没加入战队的赛事，平台回 403
    `TEAM_MEMBERSHIP_REQUIRED`。而原来的 `_fetch` 是一路 `await` 到底 ——
    异常直接冒到 `sync` 外面，于是**前面已经拉好的练习集也一起落不了库**。
    用户看到"整次同步失败"，而他只勾错了一场。

    与 `sync` 的 docstring 承诺的"一条失败不中断整次同步"是同一条规矩，
    只是那句话原本只说在题目层面，没有落到赛事层面。
    """
    from app.kernel import service
    from app.kernel.models import Ctf2Challenge, Ctf2Competition

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/practice/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        # ⚠️ 练习集的题目清单只能来自这个内嵌数组
                        #    （平台没有"列出练习集题目"的端点 —— 实测 11 条
                        #    候选路径全部 `resource: route`）。少了它就同步成
                        #    "0 道题"，而那正是这条用例要区分的东西。
                        "items": [
                            {
                                "id": "p1",
                                "name": "练习集",
                                "challenges": [{"id": "c1", "title": "练习题"}],
                            }
                        ],
                        "total": 1,
                    },
                    "success": True,
                },
                request=request,
            )
        if "/practice/p1/challenges/" in path:
            return httpx.Response(200, json={"id": "c1", "title": "练习题"}, request=request)
        # 赛事这一场：平台说"你还没加入战队"——实测过的那种 403
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "TEAM_MEMBERSHIP_REQUIRED",
                    "key": "errors.team.membership_required",
                    "params": {},
                },
                "success": False,
            },
            request=request,
        )

    patch_ctf2_transport(monkeypatch, handler)

    result = service.sync(db_session, competition_ids=["comp-1"])

    # ① 练习集落库了（这是最要紧的一条）
    competitions = list(db_session.execute(select(Ctf2Competition)).scalars())
    assert [c.kind for c in competitions] == ["practice"]
    assert db_session.execute(select(Ctf2Challenge)).scalars().all() != []
    # ② 失败的那场记进了 errors，而且**说的是人话**
    assert result.errors, "赛事失败没有进同步的账"
    assert any("战队" in item for item in result.errors), (
        f"错误文案没有告诉用户该做什么：{result.errors}"
    )
