"""`providers` 领域的接口行为（契约 §3.2，任务 2 的四条要求）。

对照任务原文逐条覆盖：

1. 增删改查 —— `test_create_*` / `test_update_*` / `test_delete_*`
2. 加密存储 + 只回显掩码 —— `test_*credential*` / `test_mask_*`
   （"日志与落盘不出明文"在 `test_secret_hygiene.py`）
3. 测试连接 —— `test_verify_*`
4. 默认唯一 + 事务保证 —— `test_only_one_default` / `test_database_rejects_two_defaults`
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import crypto
from app.providers import llm
from app.providers.models import ProviderAccount
from tests.factories import llm_ok, llm_status, make_provider, make_scan, patch_llm_transport

KEY_ONE = "sk-live-FIRSTKEY-2f8a1c4e9b7d30625a8c1f4e"
KEY_TWO = "sk-live-SECONDKEY-7b3d9f1a5c2e84061d7a3b9f"


def _create(client: TestClient, **overrides) -> dict:
    payload = {
        "label": "DeepSeek",
        "model": "deepseek-chat",
        "kind": "cloud",
        "apiKey": KEY_ONE,
        "baseUrl": "https://api.deepseek.com",
        "note": "按量计费",
        "isDefault": False,
    }
    payload.update(overrides)
    response = client.post("/api/v1/providers", json=payload)
    assert response.status_code in (200, 201), response.text
    return response.json()["provider"]


def _row(db: Session, provider_id: str) -> ProviderAccount:
    return db.execute(
        select(ProviderAccount).where(ProviderAccount.id == provider_id)
    ).scalar_one()


# ══════════════════════════════════════════════════════════
# 增删改查
# ══════════════════════════════════════════════════════════


def test_create_assigns_a_server_side_string_id(ready_client: TestClient) -> None:
    """id 由**服务端**分配，且是带前缀的字符串（契约 §1.1）。

    客户端不能自己指定 id：它的生成规则会随前端实现变化，
    而主键一旦写进数据库就再也改不动了。前端拿到响应里的 id 之后用它做后续操作。
    """
    provider = _create(ready_client)

    assert provider["id"].startswith("prov_")
    assert isinstance(provider["id"], str)
    assert not provider["id"].isdigit()


def test_list_reflects_created_providers_in_order(ready_client: TestClient) -> None:
    """默认的排最前，其余按创建时间 —— 列表第一项永远是"当前在用的那个"。"""
    first = _create(ready_client, label="DeepSeek")
    second = _create(ready_client, label="OpenAI", isDefault=True)

    listed = ready_client.get("/api/v1/providers").json()

    assert [item["id"] for item in listed] == [second["id"], first["id"]]
    assert listed[0]["isDefault"] is True
    assert listed[1]["isDefault"] is False


def test_update_changes_fields_but_not_identity(ready_client: TestClient) -> None:
    provider = _create(ready_client)

    response = ready_client.patch(
        f"/api/v1/providers/{provider['id']}",
        json={"label": "DeepSeek 生产", "model": "deepseek-reasoner", "baseUrl": "https://api.deepseek.com/v1/"},
    )

    assert response.status_code == 200
    updated = response.json()["provider"]
    assert updated["id"] == provider["id"]
    assert updated["label"] == "DeepSeek 生产"
    assert updated["model"] == "deepseek-reasoner"
    # 去尾斜杠：不去的话后面拼 endpoint 会得到 `//chat/completions`
    assert updated["baseUrl"] == "https://api.deepseek.com/v1"


def test_update_with_nothing_changed_reports_nochange(ready_client: TestClient) -> None:
    """★ `nochange` 是独立一态（契约 §3.7）。

    用户点了保存但没改过任何东西，**不能报"已保存"** —— 那是虚假的动作感。
    前端 `SaveState` 状态机有五个态，后端要能表达 `nochange` 才能驱动它。
    """
    provider = _create(ready_client, label="DeepSeek", model="deepseek-chat")

    response = ready_client.patch(
        f"/api/v1/providers/{provider['id']}",
        json={"label": "DeepSeek", "model": "deepseek-chat"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "nochange"
    # 没有发生动作，就不该有"动作发生时刻" —— 给了它，
    # 界面就有可能渲染出"已保存于 xx:xx"。
    assert "at" not in body


def test_delete_removes_the_provider(ready_client: TestClient, db_session: Session) -> None:
    provider = _create(ready_client)

    assert ready_client.delete(f"/api/v1/providers/{provider['id']}").status_code == 204
    assert ready_client.get("/api/v1/providers").json() == []


def test_unknown_provider_is_404(ready_client: TestClient) -> None:
    response = ready_client.patch("/api/v1/providers/prov_nope", json={"label": "x"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_cannot_set_server_owned_fields(ready_client: TestClient) -> None:
    """`verified` / `verifyError` 是**服务端**的，请求体里带上要被明确拒绝。

    静默忽略同样是漏洞：客户端会以为"我把 verified 设成 true 了"，
    而新建扫描向导的 `configured` 读的正是它 —— 于是它会列出一个打不通的模型。
    """
    response = ready_client.post(
        "/api/v1/providers",
        json={
            "label": "DeepSeek",
            "model": "deepseek-chat",
            "kind": "cloud",
            "apiKey": KEY_ONE,
            "verified": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_provider_writes_require_admin_scope(ready_client: TestClient) -> None:
    """provider 的**写**操作是 `admin` 级：只读令牌不能新建、也不能触发对外请求。

    ## v0.24：`GET /providers` 从 `admin` 放宽到 `read`

    这条测试原先把**读**也断言成 403，v0.24 起那个断言不再成立 ——
    放宽是**有意的**，理由写在 `app/providers/router.py` 的
    `list_providers` docstring 里：前端状态条要显示"当前模型 XXX"，
    而它读的就是这个端点；若要求 `admin`，一个 `viewer` 登录后
    状态条会报错，而那个错误看起来像"系统坏了"，不像权限问题。

    放宽的前提是该端点**不返回任何明文凭据**：`apiKey` 恒为空串，
    只有掩码 `apiKeyMask`（见 ADR-003）。

    ⚠️ 所以这里断言的是两件**不同**的事，别把它们合并成一句：
      · 读 → 允许（归 `READ_ONLY`，见 `tests/test_authorization_matrix.py`）
      · 写 → 仍然 `admin`（本测试盯的就是这条线）

    把"读"也一并断言成 403，会让这条边界看起来比实际更严 ——
    而那会掩盖"写权限真的被放松了"这种真正危险的变化。
    """
    created = ready_client.post(
        "/api/v1/settings/tokens",
        json={"label": "报表脚本", "scopes": ["read"], "expiresAt": None},
    ).json()
    ready_client.cookies.clear()
    headers = {"Authorization": f"Bearer {created['plaintext']}"}

    # 读：v0.24 起允许（只读角色要用它渲染状态条）
    assert ready_client.get("/api/v1/providers", headers=headers).status_code == 200
    # 写：仍然 admin —— 这条才是本测试要守的边界
    assert ready_client.post("/api/v1/providers", headers=headers).status_code == 403


# ══════════════════════════════════════════════════════════
# 凭据：加密存储与掩码回显
# ══════════════════════════════════════════════════════════


def test_create_encrypts_the_key(
    ready_client: TestClient, db_session: Session
) -> None:
    provider = _create(ready_client)
    row = _row(db_session, provider["id"])

    assert row.api_key_encrypted and KEY_ONE not in row.api_key_encrypted
    assert crypto.decrypt_secret(row.api_key_encrypted) == KEY_ONE
    assert row.api_key_hint == crypto.mask_secret(KEY_ONE)


def test_responses_never_carry_the_plaintext(ready_client: TestClient) -> None:
    """★ 新建、列表、更新的响应里都不能有明文。"""
    created = ready_client.post(
        "/api/v1/providers",
        json={
            "label": "DeepSeek",
            "model": "deepseek-chat",
            "kind": "cloud",
            "apiKey": KEY_ONE,
            "baseUrl": "https://api.deepseek.com",
        },
    )
    listed = ready_client.get("/api/v1/providers")
    updated = ready_client.patch(
        f"/api/v1/providers/{created.json()['provider']['id']}",
        json={"apiKey": KEY_TWO},
    )

    for response in (created, listed, updated):
        assert KEY_ONE not in response.text
        assert KEY_TWO not in response.text


def test_apiKey_is_always_empty_and_mask_carries_the_hint(ready_client: TestClient) -> None:
    """★ 三个字段各管一件事 —— 这是本任务最容易埋雷的地方。

    `apiKey` 恒为空串不是"被遮蔽的值"，而是**"不回填"**这个语义本身。
    前端保存时的 payload 拼装是
    `apiKey: keyEdited ? draft.apiKey : account.apiKey` ——
    如果这里返回掩码，用户只改一个显示名、点保存，掩码就会作为新的 key 存下去，
    真凭据被静默毁掉，而界面显示"已保存"。
    """
    provider = _create(ready_client)

    assert provider["apiKey"] == ""
    assert provider["hasKey"] is True
    assert provider["apiKeyMask"] == crypto.mask_secret(KEY_ONE)
    # 掩码里不能出现明文的中段
    assert KEY_ONE[8:24] not in provider["apiKeyMask"]


def test_local_provider_without_key_has_empty_mask(ready_client: TestClient) -> None:
    """本地模型不需要 key —— `hasKey: false` + 空掩码，不是错误。"""
    provider = _create(
        ready_client, label="本地模型（Ollama）", kind="local", apiKey="", baseUrl="http://127.0.0.1:11434"
    )

    assert provider["hasKey"] is False
    assert provider["apiKeyMask"] == ""
    assert provider["apiKey"] == ""


@pytest.mark.parametrize(
    "sent",
    [
        None,  # 省略：契约 §2.9 的部分更新语义
        "",  # 空串：前端"不回填"时输入框的值
        "   ",  # 纯空白：同上，只是用户手滑打了空格
        crypto.mask_secret(KEY_ONE),  # ★ 掩码：读出来又整体提交的客户端会这么干
    ],
)
def test_credential_survives_every_non_value_form(
    ready_client: TestClient, db_session: Session, sent: str | None
) -> None:
    """★ 四种"非新值"的写法都必须**保住**原凭据。

    漏掉其中任何一个，「用户只想改个显示名，结果把 key 弄没了」这个
    隐蔽 bug 就会回来（前端注释里明确记过它）。
    第三、四种尤其重要：它们不是设计路径，是**防御**。
    """
    provider = _create(ready_client)
    before = _row(db_session, provider["id"]).api_key_encrypted

    body: dict = {"label": "改个名字"}
    if sent is not None:
        body["apiKey"] = sent
    response = ready_client.patch(f"/api/v1/providers/{provider['id']}", json=body)

    assert response.status_code == 200, response.text
    after = _row(db_session, provider["id"])
    assert after.api_key_encrypted == before, f"凭据被改动了，输入是 {sent!r}"
    assert after.api_key_hint == crypto.mask_secret(KEY_ONE)
    assert response.json()["provider"]["hasKey"] is True


def test_new_key_replaces_the_credential(
    ready_client: TestClient, db_session: Session
) -> None:
    provider = _create(ready_client)

    ready_client.patch(f"/api/v1/providers/{provider['id']}", json={"apiKey": KEY_TWO})

    row = _row(db_session, provider["id"])
    assert crypto.decrypt_secret(row.api_key_encrypted) == KEY_TWO
    assert row.api_key_hint == crypto.mask_secret(KEY_TWO)


def test_changing_the_key_invalidates_verification(
    ready_client: TestClient, db_session: Session, monkeypatch
) -> None:
    """★ 换 key 必须作废"已验证"。

    沿用旧的 `verified: true` 会让一个**从未验证过**的新 key 显示成"已验证可用"，
    而新建扫描向导的 `configured` 读的正是它 —— 用户选中后会在
    **扫描跑到一半时**认证失败（前端注释里记过这个后果）。

    这里先把 provider 验成"可用"，再换 key，然后确认它回到未验证。
    """
    patch_llm_transport(monkeypatch, llm_ok())
    provider = _create(ready_client)
    verified = ready_client.post(f"/api/v1/providers/{provider['id']}/verify").json()
    assert verified["provider"]["verified"] is True

    updated = ready_client.patch(
        f"/api/v1/providers/{provider['id']}", json={"apiKey": KEY_TWO}
    ).json()["provider"]

    assert updated["verified"] is False
    # 不是 null —— 前端渲染的是 `verifyError ?? '尚未验证'`，
    # 置 null 会显示成"尚未验证"，而真实情况是"换过了、要重验"。
    assert updated["verifyError"] == "凭据已更改，需要重新验证"


def test_unreadable_ciphertext_is_reported_clearly(
    ready_client: TestClient, db_session: Session, monkeypatch
) -> None:
    """换了 `SUPEX_SECRET_KEY` 之后：**读接口仍然正常**（读不需要解密），
    只有真的要调 LLM 时才报错，而且报的是"凭据解不开、要重填"这类可操作的信息。

    这条覆盖的是一个真实的运维场景：密钥丢了，界面上那些 provider
    不该显示成"未配置"（那会让用户以为是自己没填），而该说明白发生了什么。
    """
    provider = _create(ready_client)

    from app.core import config as config_module

    monkeypatch.setenv("SUPEX_SECRET_KEY", crypto.generate_key())
    config_module.get_settings.cache_clear()
    try:
        # 读路径不碰明文 —— 所以它照样工作
        listed = ready_client.get("/api/v1/providers")
        assert listed.status_code == 200
        assert listed.json()[0]["hasKey"] is True

        failed = ready_client.post(f"/api/v1/providers/{provider['id']}/verify")
        assert failed.status_code == 200
        body = failed.json()
        assert body["status"] == "failed"
        assert "无法解密" in body["error"]
        assert body["provider"]["verified"] is False
    finally:
        config_module.get_settings.cache_clear()


# ══════════════════════════════════════════════════════════
# 默认唯一（任务要求 4：事务保证不出脏数据）
# ══════════════════════════════════════════════════════════


def _defaults(client: TestClient) -> list[str]:
    return [item["id"] for item in client.get("/api/v1/providers").json() if item["isDefault"]]


def test_only_one_default_across_many_switches(ready_client: TestClient) -> None:
    """反复切换默认，任何时刻都**恰好一个**。"""
    ids = [_create(ready_client, label=f"P{index}")["id"] for index in range(3)]

    for target in (ids[0], ids[2], ids[1], ids[0]):
        ready_client.post(f"/api/v1/providers/{target}/default")
        assert _defaults(ready_client) == [target]


def test_creating_a_default_demotes_the_previous_one(ready_client: TestClient) -> None:
    """新建时勾了"设为默认"，旧默认必须被降级 —— 而不是出现两个。"""
    first = _create(ready_client, label="A", isDefault=True)
    second = _create(ready_client, label="B", isDefault=True)

    assert _defaults(ready_client) == [second["id"]]
    listed = {item["id"]: item["isDefault"] for item in ready_client.get("/api/v1/providers").json()}
    assert listed[first["id"]] is False


def test_patching_isDefault_true_switches(ready_client: TestClient) -> None:
    _create(ready_client, label="A", isDefault=True)
    second = _create(ready_client, label="B")

    ready_client.patch(f"/api/v1/providers/{second['id']}", json={"isDefault": True})

    assert _defaults(ready_client) == [second["id"]]


def test_setting_default_twice_is_nochange(ready_client: TestClient) -> None:
    """已经是默认了再设一次 → `nochange`，不是"设置成功"。"""
    provider = _create(ready_client, isDefault=True)

    body = ready_client.post(f"/api/v1/providers/{provider['id']}/default").json()

    assert body["status"] == "nochange"


def test_creating_an_unrelated_provider_keeps_the_chosen_default(
    ready_client: TestClient,
) -> None:
    """★ 新建一个无关 provider **不能**把用户选好的默认换掉。

    这个缺陷是验收脚本抓到的：早先的实现在每次创建时都"清空所有默认再重挑"，
    于是用户把 A 设为默认、接着新建了一个没勾"设为默认"的 B ——
    这一次看似无关的创建把 A 的默认清掉、把 B 提为默认。
    用户明明做过一次明确的选择，却因为一个无关动作被换掉了，
    界面上没有任何提示。

    修法是把"切换"与"补空缺"拆成两个函数（见 `service._ensure_default`）。
    """
    chosen = _create(ready_client, label="A", isDefault=True)
    _create(ready_client, label="B")

    assert _defaults(ready_client) == [chosen["id"]]


def test_first_provider_becomes_default_when_none_was_chosen(
    ready_client: TestClient,
) -> None:
    """没有任何默认时，第一个 provider 被补上 —— 这是 `_ensure_default` 的职责。

    总览页的状态条要显示"当前模型"，而它读的是默认账号；
    允许"有账号但没有默认"会让那个位置空着。
    """
    first = _create(ready_client, label="A")

    assert _defaults(ready_client) == [first["id"]]


def test_unchecking_default_promotes_another(ready_client: TestClient) -> None:
    """取消默认时必须**立刻**换一个，而不是留下"没有默认"的状态。"""
    first = _create(ready_client, label="A", isDefault=True)
    second = _create(ready_client, label="B")

    ready_client.patch(f"/api/v1/providers/{first['id']}", json={"isDefault": False})

    assert _defaults(ready_client) == [second["id"]]


def test_unchecking_default_on_a_non_default_is_a_noop(ready_client: TestClient) -> None:
    """对一个本来就不是默认的 provider 传 `isDefault: false` 不该动别人。"""
    first = _create(ready_client, label="A", isDefault=True)
    second = _create(ready_client, label="B")

    ready_client.patch(f"/api/v1/providers/{second['id']}", json={"isDefault": False})

    assert _defaults(ready_client) == [first["id"]]


def test_database_rejects_two_defaults(db_session: Session) -> None:
    """★ 互斥性**在数据库层**成立，不只是应用层的判断。

    任务原文：「同一时间只能有一个默认，切换默认时要有事务保证不会出现
    两个同时为默认的脏数据」。

    关键在于约束在哪一层。只在 service 里判断的话，两个并发请求同时
    "设为默认"会各自看到"当前没有冲突"，然后都写下去 —— 留下两个 true。
    这条测试绕过 service 直接插入两行，验的正是那道数据库约束本身。

    （它是 `uq_provider_accounts_default` 这个**部分唯一索引**：
    只对 `is_default = true` 的行生效，所以多个 false 完全不冲突。）
    """
    make_provider(db_session, provider_id="prov_a", default=True)
    make_provider(db_session, provider_id="prov_b", default=False)

    other = db_session.get(ProviderAccount, "prov_b")
    other.is_default = True

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_multiple_non_defaults_are_fine(db_session: Session) -> None:
    """部分唯一索引只约束 true —— 一堆 false 是正常状态。

    这条是上一条的对照：如果索引写成了"整列唯一"，
    系统就只能存在一个 provider，而那种错会在建第二个的时候才暴露。
    """
    make_provider(db_session, provider_id="prov_a", default=True)
    make_provider(db_session, provider_id="prov_b", default=False)
    make_provider(db_session, provider_id="prov_c", default=False)

    rows = list(db_session.execute(select(ProviderAccount)).scalars())
    assert len(rows) == 3


# ══════════════════════════════════════════════════════════
# 删除的两道门
# ══════════════════════════════════════════════════════════


def test_deleting_the_default_is_rejected(ready_client: TestClient) -> None:
    """默认 provider 不可删（契约 §3.2）。

    与前端 mock 不同（它会自动挑一个替补默认），这里按契约走：
    默认决定了**扫描会真的用哪个模型**，自动替换意味着用户删掉 A 之后，
    下一次扫描悄悄用了 B —— 那是他不一定选过的那个。

    注意这里**必须有两个** provider：只有一个时"默认不可删"会有一个例外
    （否则单 provider 的用户永远删不掉它，见 `service.delete_provider`）。
    """
    provider = _create(ready_client, label="A", isDefault=True)
    _create(ready_client, label="B", isDefault=False)

    response = ready_client.delete(f"/api/v1/providers/{provider['id']}")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_IS_DEFAULT"
    assert "设为默认" in error["message"]
    assert len(ready_client.get("/api/v1/providers").json()) == 2


def test_the_last_provider_can_be_deleted_even_if_default(ready_client: TestClient) -> None:
    """★ 唯一一个 provider 即使被自动提为默认，也**必须能删掉**。

    没有这个例外就会死锁：新建的 provider 会被自动提为默认，而默认不可删 ——
    于是只有一个 provider 的用户永远删不掉它，系统也没法被清空。
    这个死锁是写 `test_delete_removes_the_provider` 时抓到的。
    """
    only = _create(ready_client, label="唯一的")
    assert ready_client.get("/api/v1/providers").json()[0]["isDefault"] is True

    assert ready_client.delete(f"/api/v1/providers/{only['id']}").status_code == 204
    assert ready_client.get("/api/v1/providers").json() == []


def test_deleting_a_provider_in_use_lists_the_scans(
    ready_client: TestClient, db_session: Session
) -> None:
    """被未结束的扫描引用 → 409，且**说清是哪几次扫描**（契约 §6）。

    只说"有扫描在用"会让用户去扫描列表里自己找，而那个列表可能很长。
    """
    provider = _create(ready_client, label="被占用的")
    other = _create(ready_client, isDefault=True)
    make_scan(db_session, provider_id=provider["id"], target="busy.internal")

    response = ready_client.delete(f"/api/v1/providers/{provider['id']}")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_IN_USE"
    assert "busy.internal" in error["message"]
    assert other["id"]  # 另一个 provider 无关，不该被牵连


def test_deleting_a_provider_used_only_by_finished_scans_works(
    ready_client: TestClient, db_session: Session
) -> None:
    """已结束的扫描不算"占用" —— 历史记录不该拦住清理。

    这是上一条的对照组：只拦 `queued`/`running`，因为只有它们
    会在之后真的用到这个 provider。已完成扫描引用的是一次**过去**的调用。
    """
    provider = _create(ready_client, label="用完的")
    _create(ready_client, isDefault=True)
    make_scan(db_session, provider_id=provider["id"], status="completed")

    assert ready_client.delete(f"/api/v1/providers/{provider['id']}").status_code == 204


# ══════════════════════════════════════════════════════════
# 测试连接（任务要求 3）
# ══════════════════════════════════════════════════════════


def test_verify_success_reports_latency_and_persists(
    ready_client: TestClient, monkeypatch
) -> None:
    """★ 成功：真的发了一次请求，返回真实耗时，并把 `verified` 写回。

    "能正确反映真实调用结果"这条验收，靠的是**不 mock `probe()`** ——
    假的是网络传输层，URL 构造、请求头、响应解析全都真的跑。
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "pong"}}]}, request=request
        )

    patch_llm_transport(monkeypatch, handler)
    provider = _create(ready_client)

    body = ready_client.post(f"/api/v1/providers/{provider['id']}/verify").json()

    assert body["status"] == "ok"
    assert body["durationMs"] >= 0
    assert body["reply"] == "pong"
    assert body["provider"]["verified"] is True
    assert body["provider"]["verifyError"] is None

    # 请求真的发出去了，而且打得对
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.deepseek.com/chat/completions"
    assert seen[0].headers["authorization"] == f"Bearer {KEY_ONE}"
    # 凭据只在请求头里 —— 不在 URL、不在查询串
    assert KEY_ONE not in str(seen[0].url)


def test_verify_failure_returns_200_with_a_readable_reason(
    ready_client: TestClient, monkeypatch
) -> None:
    """★ 失败也是 `200` + `status: "failed"`，不是 4xx。

    契约 §3.2 把「验证失败」定义为「存进去了但打不通」—— 它是一个
    **正常业务结果**。返回 4xx 会让前端走"保存失败"分支，
    而用户不知道该重填还是该重试。
    """
    patch_llm_transport(monkeypatch, llm_status(401, "invalid api key"))
    provider = _create(ready_client)

    response = ready_client.post(f"/api/v1/providers/{provider['id']}/verify")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "凭据被拒绝" in body["error"]
    assert "HTTP 401" in body["error"]
    # 对方的原文只在括号里作为补充，且核心结论是我们自己的中文完整句
    assert "invalid api key" in body["error"]
    assert body["provider"]["verified"] is False
    assert body["provider"]["verifyError"] == body["error"]


def test_verify_timeout_is_reported_as_timeout(ready_client: TestClient, monkeypatch) -> None:
    """超时要被明确识别出来，而不是笼统的"请求失败"。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    patch_llm_transport(monkeypatch, handler)
    provider = _create(ready_client)

    body = ready_client.post(f"/api/v1/providers/{provider['id']}/verify").json()

    assert body["status"] == "failed"
    assert "超时" in body["error"]
    assert body["durationMs"] >= 0


def test_verify_rejects_metadata_endpoints(ready_client: TestClient, monkeypatch) -> None:
    """云厂商元数据地址一律拦住，且给出可操作的说明。

    这道护栏与 `allow_private_llm_endpoints` 无关 —— 打这些地址的
    唯一用途就是取实例凭据，而本平台没有任何理由去取。
    """
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被执行
        nonlocal called
        called = True
        return httpx.Response(200, json={}, request=request)

    patch_llm_transport(monkeypatch, handler)
    provider = _create(ready_client, baseUrl="http://169.254.169.254")

    body = ready_client.post(f"/api/v1/providers/{provider['id']}/verify").json()

    assert body["status"] == "failed"
    assert "元数据" in body["error"]
    assert called is False, "请求不该真的发出去"


def test_verify_treats_200_with_unparsable_body_as_failure(
    ready_client: TestClient, monkeypatch
) -> None:
    """2xx 但没有可识别的回话 → **算失败**。

    报成成功会让用户以为配好了，而那个端点拿去跑扫描会在半路出错。
    """
    patch_llm_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"unexpected": "shape"}, request=request),
    )
    provider = _create(ready_client)

    body = ready_client.post(f"/api/v1/providers/{provider['id']}/verify").json()

    assert body["status"] == "failed"
    assert "没有产出任何内容" in body["error"]
    # ★ 文案**不许**把责任推给 baseUrl（v0.11）。
    # 走到这条分支的原因通常是"探测预算太小"，而 baseUrl 错的话
    # 前面早就以 404 / 非 JSON 的形式失败了。指错方向会让人白折腾半天。
    assert "baseUrl" not in body["error"], (
        f"失败文案又去怪 baseUrl 了：{body['error']}"
    )


# ── ★ 推理型模型（v0.11 由真实配置暴露的缺陷）──────────────

# 这一段是**真实抓到的原始响应**（截断），来自 `deepseek-flash`。
# 用真实形状而不是我编的形状：这个 bug 的全部意义就在于
# "真实响应长这样，而我们的解析没认它"。
REASONING_REPLY = {
    "id": "e1d85682-5681-4131-8c25-2860a075f128",
    "object": "chat.completion",
    "created": 1790268533,
    "model": "deepseek-flash",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning_content": 'The user just said "ping". This',
            },
            "logprobs": None,
            "finish_reason": "length",
        }
    ],
    "usage": {
        "prompt_tokens": 31,
        "completion_tokens": 8,
        "total_tokens": 39,
        "completion_tokens_details": {"reasoning_tokens": 8},
    },
}


def test_verify_accepts_a_reasoning_model(ready_client: TestClient, monkeypatch) -> None:
    """★ 推理型模型**必须验证通过** —— 它的 `content` 是空的，但它是通的。

    这是任务 8 收尾时由真实配置暴露的缺陷：配了 `deepseek-flash`
    （推理型），验证报「成功状态但响应内容无法识别，请检查 baseUrl」。
    而真实响应是 HTTP 200 + 空的 `content` + 非空的 `reasoning_content`
    + `finish_reason: "length"` + `reasoning_tokens: 8` ——
    也就是**探测自己给的 8 个 token 全被推理吃掉了**。

    凭据、接入点、模型三样全是好的，却被判成"配置有问题"。
    """
    patch_llm_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=REASONING_REPLY, request=request),
    )
    provider = _create(ready_client)

    body = ready_client.post(f"/api/v1/providers/{provider['id']}/verify").json()

    assert body["status"] == "ok", f"推理型模型被误判：{body.get('error')}"
    # 回话取的是推理内容（它就是模型真实产出），而不是一个空串
    assert body["reply"]
    assert "ping" in body["reply"]
    # 而且真的把 verified 落库了
    assert body["provider"]["verified"] is True


def test_probe_marks_a_reasoning_reply_as_such(monkeypatch) -> None:
    """`reply_source` 区分"正常回答"与"预算被推理吃掉"。

    调用方据此给出不同文案："模型回了 pong" 与
    "模型把预算用在推理上了" 是两件事，不该长得一样。
    """
    from app.providers import llm

    patch_llm_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=REASONING_REPLY, request=request),
    )
    outcome = llm.probe(
        base_url="https://api.deepseek.com",
        label="DeepSeek",
        model="deepseek-flash",
        api_key="sk-test-notreal-abcdefghijkl",
        kind="cloud",
    )

    assert outcome.ok is True
    assert outcome.reply_source == "reasoning"


def test_probe_marks_an_ordinary_reply_as_content(monkeypatch) -> None:
    """反面：正常回答仍要标成 `content`（否则上面那条可以被"永远标 reasoning"满足）。"""
    from app.providers import llm

    patch_llm_transport(monkeypatch, llm_ok("pong"))
    outcome = llm.probe(
        base_url="https://api.deepseek.com",
        label="DeepSeek",
        model="deepseek-chat",
        api_key="sk-test-notreal-abcdefghijkl",
        kind="cloud",
    )

    assert outcome.ok is True
    assert outcome.reply == "pong"
    assert outcome.reply_source == "content"


def test_probe_still_rejects_a_200_with_no_message_at_all(monkeypatch) -> None:
    """反面：**不能因为要兼容推理模型就把判定放松掉**。

    `content` 与 `reasoning_content` 都空（或者干脆没有 `message`）
    仍然算失败 —— 否则"随便回个 200"就能骗过验证，
    而那正是这条检查存在的理由。
    """
    from app.providers import llm

    for body in (
        {"choices": [{"message": {"role": "assistant", "content": ""}}]},
        {"choices": [{"index": 0, "finish_reason": "stop"}]},
        {"object": "chat.completion"},
        {"ok": True},
    ):
        patch_llm_transport(
            monkeypatch,
            lambda request, _b=body: httpx.Response(200, json=_b, request=request),
        )
        outcome = llm.probe(
            base_url="https://api.deepseek.com",
            label="DeepSeek",
            model="deepseek-chat",
            api_key="sk-test-notreal-abcdefghijkl",
            kind="cloud",
        )
        assert outcome.ok is False, f"这种响应不该算通过：{body}"
        assert "baseUrl" not in (outcome.error or "")


def test_probe_accepts_empty_reasoning_content_as_failure(monkeypatch) -> None:
    """`reasoning_content` 是空串（不是缺失）时同样算失败。

    有些网关会把这个字段填成 `""` 而 `content` 也是 `""` ——
    两个都空就是"什么都没产出"，不能因为**键存在**就放行。
    """
    from app.providers import llm

    patch_llm_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "",
                                           "reasoning_content": ""}}]},
            request=request,
        ),
    )
    outcome = llm.probe(
        base_url="https://api.deepseek.com",
        label="DeepSeek",
        model="deepseek-flash",
        api_key="sk-test-notreal-abcdefghijkl",
        kind="cloud",
    )

    assert outcome.ok is False


# ══════════════════════════════════════════════════════════
# 接入点拼装（纯函数，四条预设逐条钉住）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("base_url", "label", "kind", "expected"),
    [
        # 前端 PROVIDER_PRESETS 的四条，逐条对
        ("https://api.deepseek.com", "DeepSeek", "cloud", "https://api.deepseek.com/chat/completions"),
        ("https://api.openai.com/v1", "OpenAI", "cloud", "https://api.openai.com/v1/chat/completions"),
        (
            "https://api.anthropic.com",
            "Anthropic",
            "cloud",
            "https://api.anthropic.com/v1/messages",
        ),
        # 本地模型的裸 baseUrl 必须补 /v1，否则会打到 /chat/completions 上得到 404 ——
        # 而那个 404 会被用户理解成"Ollama 不支持"，方向完全错了
        (
            "http://127.0.0.1:11434",
            "本地模型（Ollama）",
            "local",
            "http://127.0.0.1:11434/v1/chat/completions",
        ),
        # 已经带 /v1 的本地地址不该被补成 /v1/v1
        (
            "http://127.0.0.1:11434/v1",
            "本地模型（Ollama）",
            "local",
            "http://127.0.0.1:11434/v1/chat/completions",
        ),
    ],
)
def test_endpoint_building(base_url: str, label: str, kind: str, expected: str) -> None:
    anthropic = llm.is_anthropic(base_url)
    assert llm.build_endpoint(base_url, anthropic=anthropic, local=kind == "local") == expected


def test_empty_base_url_falls_back_to_the_official_default() -> None:
    """契约 §2.9：`baseUrl` 空串 = 用官方默认地址。

    所以服务端必须知道这些默认值 —— 这份表与前端 `PROVIDER_PRESETS` 镜像，
    改一边必须改另一边。这条测试钉住的是"镜像关系真的存在"。
    """
    assert llm.resolve_base_url("", "DeepSeek") == "https://api.deepseek.com"
    assert llm.resolve_base_url("", "OpenAI") == "https://api.openai.com/v1"
    assert llm.resolve_base_url("", "Anthropic") == "https://api.anthropic.com"
    assert llm.resolve_base_url("", "本地模型（Ollama）") == "http://127.0.0.1:11434"
    # 认不出来就返回空串，由调用方给出"请填写 baseUrl"的明确错误
    assert llm.resolve_base_url("", "某不认识的网关") == ""


def test_unknown_label_with_no_base_url_gives_an_actionable_error(
    ready_client: TestClient,
) -> None:
    provider = _create(ready_client, label="某不认识的网关", baseUrl="")

    body = ready_client.post(f"/api/v1/providers/{provider['id']}/verify").json()

    assert body["status"] == "failed"
    assert "请填写 baseUrl" in body["error"]


def test_anthropic_uses_its_own_auth_header(ready_client: TestClient, monkeypatch) -> None:
    """Anthropic 用的是 `x-api-key` 而不是 `Authorization: Bearer`。

    发错头会得到 401，而用户会以为自己填错了 key —— 排查方向完全错了。
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "pong"}]}, request=request
        )

    patch_llm_transport(monkeypatch, handler)
    provider = _create(ready_client, label="Anthropic", baseUrl="https://api.anthropic.com")

    body = ready_client.post(f"/api/v1/providers/{provider['id']}/verify").json()

    assert body["status"] == "ok"
    assert body["reply"] == "pong"
    assert seen[0].headers["x-api-key"] == KEY_ONE
    assert "authorization" not in {key.lower() for key in seen[0].headers}
    assert seen[0].headers["anthropic-version"] == llm.ANTHROPIC_VERSION


def test_local_provider_sends_no_auth_header(ready_client: TestClient, monkeypatch) -> None:
    """没有凭据时不发认证头。

    发一个空的 `Authorization: Bearer `（尾部空格）会让某些实现直接 400，
    而用户看到的是"本地服务不支持" —— 又是一个方向错误的报错。
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]}, request=request)

    patch_llm_transport(monkeypatch, handler)
    provider = _create(
        ready_client, label="本地模型（Ollama）", kind="local", apiKey="", baseUrl="http://127.0.0.1:11434"
    )

    ready_client.post(f"/api/v1/providers/{provider['id']}/verify")

    assert "authorization" not in {key.lower() for key in seen[0].headers}


# ══════════════════════════════════════════════════════════
# 探测预算（v0.11：个位数对推理型模型等于"不让它说话"）
# ══════════════════════════════════════════════════════════


def test_probe_budget_is_large_enough_for_reasoning_models() -> None:
    """★ 默认预算必须够推理型模型**给出回答**，而不只是"不算失败"。

    这条挡的是"有人觉得 256 太多、顺手调回 8"。
    实测（同一个推理型模型，prompt 只有 `ping`）：

    | max_tokens | content | reasoning_tokens | finish_reason |
    | ---------- | ------- | ---------------- | ------------- |
    | 8          | `""`    | 8                | length        |
    | 64         | `""`    | 64               | length        |
    | **256**    | `"Pong! 🏓 …"` | 36        | **stop**      |

    8 与 64 下模型根本没机会回答 —— 探测只能靠"认 reasoning_content"
    勉强判成功，而用户看到的 `reply` 是一段被截断的思考碎片。
    """
    from app.core.config import Settings

    # ⚠️ 断言**字段默认值**，不是 `Settings()`/`get_settings()`。
    # 后两者会读 `.env`，而 `.env` 是**每个部署自己的配置** ——
    # 用它做断言，等于"谁的 .env 里写小了谁就把测试跑红"，
    # 而那条信息对代码没有意义（代码该给的默认值仍然是 256）。
    # 写这段时正是 `.env` 里还钉着 8，测试一眼指出了这个落差。
    default = Settings.model_fields["llm_probe_max_tokens"].default

    assert default >= 256, (
        "探测预算又变小了 —— 推理型模型会把预算全用在 reasoning 上，"
        "content 永远是空的。详见 app/core/config.py 的说明。"
    )


def test_env_example_matches_the_probe_budget_default() -> None:
    """`.env.example` 里的值与代码默认值必须一致。

    两处漂移的后果很具体：照 `.env.example` 部署的人拿到的是旧值，
    而"代码里已经修好了"这件事对**他们**不成立 —— 他们仍会看到
    推理型模型验证失败，然后来报同一个 bug。
    """
    from app.core.config import PROJECT_ROOT, Settings

    default = Settings.model_fields["llm_probe_max_tokens"].default
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    line = next(
        (ln for ln in text.splitlines() if ln.strip().startswith("SUPEX_LLM_PROBE_MAX_TOKENS=")),
        None,
    )
    assert line is not None, ".env.example 里没有 SUPEX_LLM_PROBE_MAX_TOKENS"
    assert line.split("=", 1)[1].strip() == str(default), (
        f".env.example 写的是 {line!r}，而代码默认值是 {default} —— 两边要一致"
    )
