"""`GET /kernel/recipes` 的 HTTP 契约测试。

## 与其他 tools 测试的分工

`test_recipes.py` 钉的是**配方库自身**（拼接、局限说明、自洽校验、接线存在性）。
这个文件钉的是**它到了 HTTP 层之后的形状** —— 而那是另一类失效：

    `default_limitation` 在 dataclass 里写得好好的，
    但 schema 没这个字段 → 响应里没有它 → 界面画不出来。

本项目在这个形状上栽过不止一次（`mcp_servers` v0.72、`accounts` v0.94 的
状态条），所以凡是"源码里有、界面要显示"的字段，都要有一条端点级断言，
而不是只断服务层的 dict。

## 为什么这些用例必须看**原始 JSON 文本**

前端的字段名是 camelCase，而 Pydantic 的别名是单向的：`model_dump()` 默认
按字段名出 snake_case。只断言 `resp.json()` 会漏掉"别名没生效"这个失败 ——
那时前端读到的是 `undefined`，界面上是一行空白，而接口测试全绿。
所以下面有几条专门读 `resp.text`。
"""

from __future__ import annotations

import json

import pytest

API = "/api/v1"


# ══════════════════════════════════════════════════════════
# 1. 端点存在且形状完整
# ══════════════════════════════════════════════════════════


def test_recipes_endpoint_returns_the_whole_library(ready_client) -> None:  # noqa: ANN001
    """端点要能一次给出全量配方 —— 界面靠它画整个列表。"""
    resp = ready_client.get(f"{API}/kernel/recipes")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["total"] > 0, "配方库不该是空的"
    assert len(body["recipes"]) == body["total"], (
        "total 必须等于 recipes 的条数；不一致时前端算出的「12 条里 X 条有局限」会是错的"
    )
    assert body["groups"], "分组顺序必须下发：前端按它排栏，不从 recipes 里二次推导"


def test_recipes_are_grouped_and_every_group_is_known(ready_client) -> None:  # noqa: ANN001
    """每条配方的 group 必须在 groups 里 —— 否则界面上那一栏永远是空的。"""
    body = ready_client.get(f"{API}/kernel/recipes").json()
    known = set(body["groups"])
    unknown = sorted({r["group"] for r in body["recipes"]} - known)
    assert not unknown, f"这些分组没有出现在 groups 里：{unknown}"


def test_a_recipe_carries_everything_the_page_needs(ready_client) -> None:  # noqa: ANN001
    """挑一条真实配方，逐字段确认界面要用的东西都在。"""
    body = ready_client.get(f"{API}/kernel/recipes").json()
    nmap = next((r for r in body["recipes"] if r["name"] == "nmap"), None)
    assert nmap is not None, "nmap 是最常用的一条，不该缺"

    assert nmap["summary"], "没有 summary 的话列表上那一行是空白"
    assert nmap["command"] == "nmap", "command 与 name 不同的情况（exec→sh）界面要能显示"
    assert nmap["args"] == ["-sT", "-sV", "-sC"], "无条件前置参数要能看到，人才能知道默认带了什么"

    params = {p["name"]: p for p in nmap["params"]}
    assert params["target"]["required"] is True
    assert params["target"]["format"] == "positional"
    assert params["ports"]["flag"] == "-p"


# ══════════════════════════════════════════════════════════
# 2. ★ 默认局限必须真的到得前端
# ══════════════════════════════════════════════════════════


def test_default_limitation_reaches_the_recipes_api(ready_client) -> None:  # noqa: ANN001
    """★ 这是整个配方库存在的主要理由，它必须出现在响应里。

    实测过：同一个 POC 工具不给模板目录时命中 0，补上目录就命中 2。
    模型看到那个 0 会写下"目标没有这个漏洞"。这句话如果不能显示，
    那这个模块就只是给源码写注释。
    """
    body = ready_client.get(f"{API}/kernel/recipes").json()
    with_limits = [r for r in body["recipes"] if r.get("defaultLimitation")]
    assert len(with_limits) >= 10, (
        f"只有 {len(with_limits)} 条配方写了局限说明 —— 太少了。"
        "默认参数会缩水的那些工具（nuclei/sqlmap/ffuf/gobuster/nmap/…）都该写"
    )


def test_default_limitation_key_is_camel_case_in_the_raw_json(ready_client) -> None:  # noqa: ANN001
    """字段名必须是 camelCase —— 前端读 `defaultLimitation`。

    只断言 `resp.json()` 抓不住这个：Pydantic 默认按字段名出 snake_case，
    那时前端读到 `undefined`、界面上是一行空白，而接口测试全绿。
    """
    raw = ready_client.get(f"{API}/kernel/recipes").text
    assert "defaultLimitation" in raw, "前端读的是 camelCase 的 defaultLimitation"
    assert '"default_limitation"' not in raw, (
        "响应里不该出现 snake_case 的 default_limitation —— 前端会读不到"
    )


def test_network_tools_state_the_container_has_no_network(ready_client) -> None:  # noqa: ANN001
    """容器是 `--network none`。`nc`/`socat` 必须写明"连不上不是目标的问题"。

    这条是本轮实测出来的：沙箱里对任何外部地址都 `Network is unreachable`。
    不写明的话，模型会把"连不上"读成"目标端口没开"。
    """
    body = ready_client.get(f"{API}/kernel/recipes").json()
    by_name = {r["name"]: r for r in body["recipes"]}
    for name in ("nc", "socat"):
        assert name in by_name, f"{name} 应该在配方库里"
        limitation = by_name[name].get("defaultLimitation", "")
        assert "无网络" in limitation or "network none" in limitation, (
            f"{name} 在容器里连不上任何外部地址，说明里必须写清这一点；"
            f"现在是：{limitation[:60]!r}"
        )


# ══════════════════════════════════════════════════════════
# 3. allowed_exit_codes —— "没命中"不等于"失败"
# ══════════════════════════════════════════════════════════


def test_allowed_exit_codes_are_exposed_for_tools_that_exit_nonzero_on_no_match(
    ready_client,  # noqa: ANN001
) -> None:
    """`ffuf`/`gobuster` 没命中时退出码非 0。这个白名单要能显示出来。"""
    body = ready_client.get(f"{API}/kernel/recipes").json()
    by_name = {r["name"]: r for r in body["recipes"]}
    for name in ("ffuf", "gobuster"):
        codes = by_name[name].get("allowedExitCodes", [])
        assert codes and codes != [0], (
            f"{name} 没命中时退出码非 0；allowedExitCodes 必须表达出来，"
            "否则界面会把「空结果」显示成「命令失败」，诱导用户去修一个正常工作的工具"
        )


def test_a_tool_that_only_fails_on_error_keeps_the_default_allowlist(ready_client) -> None:  # noqa: ANN001
    """不是所有工具都该放行非 0 —— 那会让真失败也被当成成功。"""
    body = ready_client.get(f"{API}/kernel/recipes").json()
    by_name = {r["name"]: r for r in body["recipes"]}
    assert by_name["nmap"]["allowedExitCodes"] == [0], (
        "nmap 的非 0 退出码是真的出错，不该被放行"
    )


# ══════════════════════════════════════════════════════════
# 4. 鉴权与只读
# ══════════════════════════════════════════════════════════


def test_recipes_requires_authentication(client) -> None:  # noqa: ANN001
    """未登录不该看到工具清单 —— 它暴露了攻击面（镜像里有什么）。"""
    resp = client.get(f"{API}/kernel/recipes")
    assert resp.status_code in (401, 403), (
        f"未鉴权访问配方库应被拒，实际 {resp.status_code}"
    )


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_recipes_is_read_only(ready_client, method: str) -> None:  # noqa: ANN001
    """没有写端点。

    ★ 这是个**设计决定**，不是还没做：配方描述的是"镜像里装着的二进制
    怎么调"，装没装由镜像决定。给它加开关会造出一个假的权力 ——
    用户关掉 `nmap`，实际只是让文档里少一行，`run_shell nmap` 照样能跑。
    能关的是 `/tools` 那十个引擎动作。
    """
    resp = getattr(ready_client, method)(f"{API}/kernel/recipes")
    assert resp.status_code in (404, 405), (
        f"{method.upper()} /kernel/recipes 应该不存在（配方是只读的），"
        f"实际 {resp.status_code}"
    )


def test_recipes_does_not_leak_host_paths(ready_client) -> None:  # noqa: ANN001
    """配方里不该出现**宿主机**路径。

    ★ 这条最初写成「不含 `/home/`」，而它误报了 —— 命中的是 nuclei 那条说明：

        nuclei 的局限里写着「2026-10-25 实测 /home/solver、/root、/usr/share、/opt
        下都没有 nuclei-templates」

    那些是**容器内**的路径，而且这句话是有价值的证据（它解释了为什么
    nuclei 在这个镜像里默认跑不起来）。把它删掉等于为了过测试而毁掉信息。

    真正该防的是**部署机**的路径：仓库位置、虚拟环境、Windows 用户目录。
    那类泄漏会暴露部署目录结构，与 `KernelSkillOut` 不下发 `path` 同一条纪律。
    """
    raw = ready_client.get(f"{API}/kernel/recipes").text
    genuine_leaks = (
        "/root/suvex",  # Kali 上的仓库根
        "/opt/suvex",  # Kali 上的部署根
        "C:/Users",  # Windows 开发机的用户目录
        "site-packages",  # 虚拟环境位置
        ".venv",
    )
    found = [leak for leak in genuine_leaks if leak in raw]
    assert not found, f"响应里泄露了宿主/部署路径：{found}"


def test_recipes_payload_is_valid_json_and_nonempty(ready_client) -> None:  # noqa: ANN001
    """基本健全性：能解析、条数合理。"""
    body = json.loads(ready_client.get(f"{API}/kernel/recipes").text)
    assert 20 <= body["total"] <= 200, (
        f"配方数 {body['total']} 看起来不对（镜像里实测有 30+ 个可用二进制）"
    )
