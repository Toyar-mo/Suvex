"""`artifact`（只有附件、没有地址）建扫描的完整链路（任务 17）。

## 这个文件走的是**整条路**，不是单个函数

从「暂存一个 zip」→ `POST /scans` → 库里那一行 → `build_spec` →
最终的 argv。中间任何一环断了，用户看到的都是同一句话：
「这题建不出来」。而原因可能在上传、在校验、在解压、在路径拼接、
在启动参数 —— 分散在四个模块里。

所以这里刻意**一条测试穿到底**，把"用户交上来一个 zip，最后 strix 到底
拿到了什么"这件事钉死。

## 隔离：扫描根目录指到临时目录

附件与 `challenge/` 的落点都由 `settings.scan_root` 决定。不指走的话，
测试会把文件写进项目里真实的 `_scan_runs/` —— 那既污染开发环境，
也让"跑完测试后有没有残留"这件事没法断言。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.vocab import TargetKind
from app.scans import runner, service, uploads
from app.scans.models import Scan
from tests.factories import make_provider

API = "/api/v1"
AUTH_STATEMENT = "我有权对此目标进行安全测试"
VALID_AUTH = {"confirmed": True, "statement": AUTH_STATEMENT}


@pytest.fixture()
def scan_root(scan_config, tmp_path) -> Path:
    """把扫描根目录指到临时目录 —— 附件与 `challenge/` 全落在它下面。

    `scan_config` 会清 `get_settings` 的 lru_cache（它是单例），
    不清的话改了环境变量也不生效 —— 症状是"测试跑在了真实目录上"。
    """
    scan_config(SUPEX_SCAN_ROOT=str(tmp_path / "runs"))
    return tmp_path / "runs"


@pytest.fixture()
def provider_id(db_session: Session) -> str:
    return make_provider(db_session)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def _stage(client: TestClient, filename: str, content: bytes) -> str:
    response = client.post(
        f"{API}/scans/attachments/staging",
        files=[("files", (filename, content, "application/octet-stream"))],
    )
    assert response.status_code == 201, response.text
    return response.json()["stagingId"]


def _body(provider_id: str, staging_id: str | None = None, **overrides) -> dict:
    body = {
        "name": "",
        # ★ `artifact` 的目标**必须是空串** —— 这是它与其他四种最本质的区别
        "target": "",
        "targetKind": TargetKind.ARTIFACT.value,
        "skills": ["web"],
        "providerId": provider_id,
        "authorization": dict(VALID_AUTH),
    }
    if staging_id:
        body["attachmentStagingId"] = staging_id
    body.update(overrides)
    return body


# ══════════════════════════════════════════════════════════
# 一、没有附件就不该建出扫描
# ══════════════════════════════════════════════════════════


def test_creating_without_any_attachment_is_refused(
    ready_client: TestClient, provider_id: str, scan_root: Path, db_session: Session
) -> None:
    """★ 这类题的全部素材都在附件里 —— 没有附件连"要分析什么"都不知道。

    必须**当场拒绝**，而不是建一条跑不出东西的扫描：
    后者会让用户以为在跑，然后拿到一份"什么都没发现"的结论 ——
    而它看起来和"题目确实没问题"一模一样。
    """
    response = ready_client.post(f"{API}/scans", json=_body(provider_id))

    assert response.status_code == 400, response.text
    assert "附件" in response.json()["error"]["message"]

    total = db_session.execute(select(func.count()).select_from(Scan)).scalar_one()
    assert total == 0, "被拒绝的请求竟然建出了扫描"


def test_creating_with_an_address_is_refused(
    ready_client: TestClient, provider_id: str, scan_root: Path
) -> None:
    """填了地址也拒 —— 该模式下地址会被静默丢弃，而那是不能接受的
    （见 `targets._validate_artifact` 的说明）。"""
    staging = _stage(ready_client, "tryre.zip", _zip_bytes({"tryre": b"\x7fELF"}))

    response = ready_client.post(
        f"{API}/scans",
        json=_body(provider_id, staging, target="http://example.com"),
    )

    assert response.status_code == 400, response.text
    assert "不需要目标地址" in response.json()["error"]["message"]


# ══════════════════════════════════════════════════════════
# 二、正常路径：一个 zip 进去，一个可分析的目标出来
# ══════════════════════════════════════════════════════════


def test_a_zip_becomes_the_scan_target(
    ready_client: TestClient, provider_id: str, scan_root: Path
) -> None:
    """★ 建立成功，且 `target` 是**附件名**、`name` 由它推导。

    `target` 存附件名而不是留空：它是列表里那一行的主信息，也是
    "这是个什么目标"的标识。留空的话用户认不出哪条是哪道题 ——
    而这恰恰是他同时开着好几道题时最需要看的一眼。
    """
    staging = _stage(ready_client, "tryre.zip", _zip_bytes({"tryre": b"\x7fELF"}))

    response = ready_client.post(f"{API}/scans", json=_body(provider_id, staging))
    assert response.status_code == 201, response.text
    scan = response.json()

    assert scan["targetKind"] == "artifact"
    assert scan["target"] == "tryre.zip"
    # 名字去掉扩展名 —— `.zip` 对"这是哪道题"不提供任何信息
    assert scan["name"] == "tryre"
    assert scan["status"] == "queued"


def test_explicit_name_still_wins(
    ready_client: TestClient, provider_id: str, scan_root: Path
) -> None:
    """用户填了名字就用他的 —— 从附件名推导只是留空时的兜底。"""
    staging = _stage(ready_client, "tryre.zip", _zip_bytes({"tryre": b"\x7fELF"}))

    response = ready_client.post(
        f"{API}/scans", json=_body(provider_id, staging, name="逆向第一题")
    )

    assert response.json()["name"] == "逆向第一题"


def test_the_archive_is_unpacked_into_the_challenge_dir(
    ready_client: TestClient, provider_id: str, scan_root: Path
) -> None:
    """★ zip 被解开到 `challenge/`，而且**内容正确**。

    执行后端要的是一个**目录**（对单个文件它会直接拒绝：
    `Path exists but is not a directory`），所以这一步是"附件变成目标"的实体。
    """
    staging = _stage(
        ready_client,
        "tryre.zip",
        _zip_bytes({"tryre": b"\x7fELF binary", "readme.txt": b"hint: check main"}),
    )

    scan_id = ready_client.post(f"{API}/scans", json=_body(provider_id, staging)).json()["id"]

    challenge = runner.challenge_dir(scan_id)
    assert challenge.is_dir()
    assert (challenge / "tryre").read_bytes() == b"\x7fELF binary"
    assert (challenge / "readme.txt").read_bytes() == b"hint: check main"


def test_a_plain_binary_is_placed_as_is(
    ready_client: TestClient, provider_id: str, scan_root: Path
) -> None:
    """★ 附件**不是压缩包**时原样铺进去 —— 这是 CTF 的常态，不是异常。

    少了这条，一个只会处理 zip 的实现也能让上面的测试全绿，
    而用户传一个裸 ELF 时会拿到"题目附件里没有任何文件"。
    """
    staging = _stage(ready_client, "chall", b"\x7fELF raw binary")

    scan_id = ready_client.post(f"{API}/scans", json=_body(provider_id, staging)).json()["id"]

    assert (runner.challenge_dir(scan_id) / "chall").read_bytes() == b"\x7fELF raw binary"


def test_the_original_attachment_survives_for_a_rerun(
    ready_client: TestClient, provider_id: str, scan_root: Path
) -> None:
    """★ 原件留在 `_uploads/<scan_id>/` —— 重跑同一道题还要用。

    `challenge/` 是从原件**派生**出来的，两者生命周期不同：
    派生物丢了可以从原件再解一次，原件丢了就得让用户重新上传。
    所以这条测试盯的是"别为了建 challenge 把原件搬走或删掉"。
    """
    staging = _stage(ready_client, "tryre.zip", _zip_bytes({"tryre": b"\x7fELF"}))

    scan_id = ready_client.post(f"{API}/scans", json=_body(provider_id, staging)).json()["id"]

    originals = uploads.list_for(scan_id)
    assert [row["name"] for row in originals] == ["tryre.zip"]
    # 而暂存批次已经被认领走了（认领 = 移动，见 `uploads.claim`）
    assert not uploads.staging_dir(staging).exists()


# ══════════════════════════════════════════════════════════
# 三、交给执行后端的到底是什么
# ══════════════════════════════════════════════════════════


def test_build_spec_hands_strix_a_local_directory(
    ready_client: TestClient, provider_id: str, scan_root: Path, db_session: Session
) -> None:
    """★★ 这是整个功能的**接缝**，也是最容易悄悄错的一处。

    两件事必须同时成立：

    1. `target` 是 `challenge/` 目录的**绝对路径**（不是 `tryre.zip`、
       不是空串）—— 否则 strix 会 `Invalid target` 退出；
    2. `target_kind` 报成 `"path"` 而**不是** `"artifact"`。

    第 2 条尤其反直觉，所以单独说：runner 只认识它那几种 kind，
    报 `"artifact"` 的话那句"目标是一个本地代码目录，属于白盒测试"
    就不会被加进指令 —— 于是 agent 会以为自己在测一个网站，又一轮白跑。

    反过来说，这也正是**执行层零改动**的证据：strix 那边收到的
    与用户手填一条「本地代码路径」**完全一样**。
    """
    staging = _stage(ready_client, "tryre.zip", _zip_bytes({"tryre": b"\x7fELF"}))
    scan_id = ready_client.post(f"{API}/scans", json=_body(provider_id, staging)).json()["id"]

    scan = db_session.get(Scan, scan_id)
    spec = service.build_spec(db_session, scan)

    assert spec.target == str(runner.challenge_dir(scan_id))
    assert Path(spec.target).is_dir()
    assert spec.target_kind == TargetKind.PATH.value, "报成 artifact 的话白盒说明就没了"


def test_the_command_line_gets_the_directory(
    ready_client: TestClient, provider_id: str, scan_root: Path, db_session: Session
) -> None:
    """argv 上必须是 `--target <目录>`，而且**不能**出现 `--resume`。

    与「续跑」那条硬约束同一个道理：这两个参数是互斥的，
    而这里多一个少一个都不会在 HTTP 层暴露出来，只能测拼出来的 argv。
    """
    staging = _stage(ready_client, "tryre.zip", _zip_bytes({"tryre": b"\x7fELF"}))
    scan_id = ready_client.post(f"{API}/scans", json=_body(provider_id, staging)).json()["id"]

    spec = service.build_spec(db_session, db_session.get(Scan, scan_id))
    argv = runner.StrixRunner().command(spec)

    assert argv[argv.index("--target") + 1] == str(runner.challenge_dir(scan_id))
    assert "--resume" not in argv


def test_missing_challenge_dir_fails_with_an_actionable_reason(
    ready_client: TestClient, provider_id: str, scan_root: Path, db_session: Session
) -> None:
    """★ 素材目录在"建好"与"启动"之间消失了（被手工清过）→ **报错，不照跑**。

    照跑的话 strix 会以 `Invalid target` 退出，而那句话读起来像
    "目标填错了"，与真正的原因差得很远。错误信息必须给出替代动作
    （重跑会从保存的附件重新铺一次），否则用户只知道不行、不知道怎么办。
    """
    staging = _stage(ready_client, "tryre.zip", _zip_bytes({"tryre": b"\x7fELF"}))
    scan_id = ready_client.post(f"{API}/scans", json=_body(provider_id, staging)).json()["id"]

    import shutil

    shutil.rmtree(runner.challenge_dir(scan_id))

    with pytest.raises(Exception) as caught:  # ApiError
        service.build_spec(db_session, db_session.get(Scan, scan_id))

    assert "重跑" in str(caught.value), "只说了不行，没说那该怎么办"


# ══════════════════════════════════════════════════════════
# 四、恶意附件：拒绝，且**什么都不留下**
# ══════════════════════════════════════════════════════════


def test_a_traversing_archive_is_refused_and_leaves_no_trace(
    ready_client: TestClient, provider_id: str, scan_root: Path, db_session: Session
) -> None:
    """★★ zip slip 的包：400 拒绝，且**没有扫描记录、没有半成品目录**。

    这是"先解压、成功了再入库"这个顺序的全部价值 —— 顺序反过来的话，
    守卫拒绝时库里已经有一条 `queued` 的扫描，而调度器会在毫秒级
    认领并启动它（`_nudge_scheduler`），对着一个不存在的路径跑。

    ⚠️ 三件事都要断言。只断言"返回了 400"是不够的：
    记录建出来了但接口报错，同样能通过那条断言。
    """
    staging = _stage(
        ready_client, "evil.zip", _zip_bytes({"../../evil.txt": b"pwned"})
    )

    response = ready_client.post(f"{API}/scans", json=_body(provider_id, staging))
    assert response.status_code == 400, response.text

    # ① 没有扫描记录
    total = db_session.execute(select(func.count()).select_from(Scan)).scalar_one()
    assert total == 0, "被拒绝的包竟然建出了扫描"

    # ② 没有半成品素材目录（解了一半比什么都没有更坏：
    #    下次启动会拿到一份**看起来正常**的残缺题目）
    leftovers = list((scan_root).glob("*/challenge")) if scan_root.exists() else []
    assert leftovers == [], f"留下了半成品目录：{leftovers}"

    # ③ ★ 用户的附件**还在暂存区** —— 他改完就能重试，不必重新上传
    assert uploads.staging_dir(staging).is_dir(), "附件被吃掉了，用户得重传"
    assert not (scan_root.parent / "evil.txt").exists()
