"""出站 HTTP 客户端必须**不读环境变量里的代理**（`trust_env=False`）。

## 这组用例守的是什么

`httpx` 的 `trust_env` **默认是 `True`**，意思是每个客户端都会去读环境里的
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`（以及 `.netrc`），
并按它把请求发给那个代理。实测（本机起真转发代理，判据是**代理收到的
请求行**）：

    默认（trust_env=True）:
      代理命中 1，代理收到: GET http://rt-meta.test:8099/ HTTP/1.1   ★ 真的路由到了代理
    trust_env=False:
      代理命中 1->1（不经过代理），抛 ConnectError

对一个"让服务端替你打一个 URL"是**核心功能**的组件来说，这是两条不同的
风险，所以本文件的用例分两类：

* **A 档（防 SSRF）**：目标是**用户 / 管理员可控**的地址 ——
  `ctf2/adapters/transport.py`、`ctf2/adapters/probe.py`、
  `providers/llm.py` 的 `probe()`、`ctf2/runner.py` 的 LLM 网关与
  `_tool_http_request` 的 client、`scans/reachability.py` 的 `_probe()`。
  走代理等于"服务器上的环境变量能把出站流量改道"。
* **B 档（防凭据经代理泄漏）**：目标固定（官方域名 / 有白名单），
  **但请求带凭据** —— `ctf2/client.py`（PAT）、`ctf2/attachments.py`
  （`Authorization: <token>`）、`arena/client.py`（`X-Agent-AccessKey`）。
  走代理等于把凭据交给代理。

⚠️ 只按"是不是 SSRF 面"筛会漏掉 B 档那三处 —— 它们的危害不同（凭据外泄），
   但同样要关掉。

## ★ 为什么用"静态扫描源码"而不是逐处断言

因为**最容易出错的是将来新增的客户端**：只钉住现存 9 处的话，下一个人写
第 10 个 client 时不会有任何东西提醒他。所以这里直接扫 `app/` 下所有
`httpx.<方法>(...)` 构造，要求每一处都显式写 `trust_env=`。
（AST 而不是正则：参数常跨多行，正则容易看漏 —— 这里最初漏掉的那处
`httpx.get` 正说明"按形态搜"会漏。）
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

#: 会**发起出站请求**的 httpx 入口。只挑这些，避免把无关调用算进来。
OUTBOUND_CALLS = frozenset(
    {
        "Client",
        "AsyncClient",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "request",
        "stream",
    }
)


def _outbound_constructions() -> list[tuple[str, int, str, bool]]:
    """扫 `app/` 下每一处 `httpx.X(...)`，返回 (相对路径, 行, 调用名, 有没有 trust_env)。

    ## ★★ 已知边界：只认 `httpx.X(...)` 这一种写法（R-2，**有意不解别名**）

    判据要求"调用是 `httpx` 这个模块名上的属性访问"（见下面那个
    `isinstance(fn.value, ast.Name) and fn.value.id == "httpx"`），
    所以下面三类**等价写法扫不到**：

        from httpx import AsyncClient;  AsyncClient(...)      # 直接 import 名字
        import httpx as h;              h.AsyncClient(...)    # 模块别名
        C = httpx.AsyncClient;          C(...)                # 先赋给变量

    这是**有意保留的取舍**，不是漏做。理由三条：

    1. **要支持别名就得做作用域分析**：`h` 到底是 `httpx` 还是别的模块、
       `C` 在哪一层被赋成什么，都要跟着 import 与赋值语句走 ——
       那是一个简化版的名字解析器，而它自己会有一批新的漏报方式。
       为一条"当前命中 0 处"的缺口引入那层复杂度，代价大于收益。
    2. **它防的是"下一个人写第 10 个 client 时漏写"**，而这类人写的是
       `httpx.AsyncClient(...)` —— 与仓里现存 9 处**同一个写法**。
       真正要防的场景被覆盖了。
    3. **改了会被看见**：真要引入别名写法，`git diff` 里那一行本身很显眼；
       而这条测试的价值主要是"在 review 时提供一个检查点"，不是
       "穷举所有可能的语法形态"。

    ⚠️ 所以**别把这条测试当成语法层面的保证**。它的准确表述是：
    "在 `app/` 都用 `httpx.X(...)` 的前提下，每一处都显式写了 `trust_env`"。
    （实测确认：当前 `app/` 下这三类写法**各 0 处**。）
    """
    found: list[tuple[str, int, str, bool]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (
                isinstance(fn, ast.Attribute)
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "httpx"
                and fn.attr in OUTBOUND_CALLS
            ):
                continue
            kwargs = {k.arg for k in node.keywords if k.arg}
            found.append(
                (str(path.relative_to(APP_ROOT.parent)), node.lineno, f"httpx.{fn.attr}", "trust_env" in kwargs)
            )
    return found


def test_every_outbound_client_disables_trust_env() -> None:
    """★★ 全仓 `app/` 下**每一处**出站客户端都必须显式写 `trust_env=True/False`。

    这条是"防止下一个人漏掉"的闸：新增客户端而不写 `trust_env` 会直接让
    这条失败，并报出文件与行号。要求"显式写"而不是"必须是 False"，
    是因为将来若有正当理由要开它（比如真的支持用户配代理），
    也应该是一个**被看见的决定**，而不是继承一个默认值。
    """
    found = _outbound_constructions()
    assert found, "扫描没找到任何 httpx 客户端 —— 扫描逻辑本身坏了"

    missing = [(p, ln, call) for p, ln, call, has in found if not has]

    assert not missing, (
        "这些出站客户端没有显式设置 trust_env，会默认读环境里的 HTTP_PROXY 等：\n"
        + "\n".join(f"  {p}:{ln}  {call}" for p, ln, call in missing)
    )


def _trust_env_values(path: str) -> list[object]:
    """取某个文件里每一处 `httpx.X(...)` 的 `trust_env` 实参值。

    ⚠️ **必须是 AST 而不是 `"trust_env=False" in source`**：后者会被**注释**
    骗过 —— 本仓这些客户端上方都有一段解释 `trust_env=False` 的注释，
    于是把参数删掉之后，字符串断言仍然通过（反向验证时实测踩到了这一点：
    只有 AST 那条全局扫描抓到了它）。所以这里逐个取真实的实参值。
    """
    tree = ast.parse((APP_ROOT.parent / path).read_text(encoding="utf-8"))
    values: list[object] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (
            isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "httpx"
            and fn.attr in OUTBOUND_CALLS
        ):
            continue
        for kw in node.keywords:
            if kw.arg == "trust_env":
                values.append(ast.literal_eval(kw.value))
    return values


@pytest.mark.parametrize(
    "path",
    [
        "app/ctf2/adapters/transport.py",
        "app/ctf2/adapters/probe.py",
        "app/providers/llm.py",
        "app/ctf2/runner.py",
        "app/scans/reachability.py",
    ],
)
def test_ssrf_surface_clients_are_disabled(path: str) -> None:
    """★ A 档（防 SSRF）：目标可控的那几处必须关掉 `trust_env`。

    逐文件断言，而不是只看总数 —— 这样某一处被改回去时，
    失败信息直接指出是哪个文件。取值走 AST（见 `_trust_env_values`），
    所以注释里写多少遍都不算数。
    """
    values = _trust_env_values(path)

    assert values, f"{path} 里没有找到带 trust_env 的 httpx 客户端（是不是写漏了？）"
    assert all(v is False for v in values), (
        f"{path} 打的是用户 / 管理员可控的目标，必须 trust_env=False "
        f"（否则环境变量里的代理能把出站流量改道）；实测值 {values}"
    )


@pytest.mark.parametrize(
    "path",
    [
        "app/ctf2/client.py",
        "app/ctf2/attachments.py",
        "app/arena/client.py",
    ],
)
def test_credential_carrying_clients_are_disabled(path: str) -> None:
    """★ B 档（防凭据经代理泄漏）：带凭据那几处同样要关掉。

    它们**不是** SSRF 面（目标固定 / 有白名单），但如果只按"是不是 SSRF 面"
    筛就会漏掉它们 —— 而走代理的后果是**把凭据交给代理**（PAT / token /
    AccessKey），性质不同、同样不能接受。取值同样走 AST。
    """
    values = _trust_env_values(path)

    assert values, f"{path} 里没有找到带 trust_env 的 httpx 客户端（是不是写漏了？）"
    assert all(v is False for v in values), (
        f"{path} 的请求带凭据，必须 trust_env=False（否则凭据会经过环境变量指定的代理）；"
        f"实测值 {values}"
    )


# ══════════════════════════════════════════════════════════
# 行为验证：`trust_env=False` 真的让它不理环境变量
# ══════════════════════════════════════════════════════════


def test_trust_env_false_ignores_proxy_environment(monkeypatch) -> None:
    """★★ 钉住"这条参数确实起作用"这件事本身。

    上面几条只是**源码里有这几个字**；如果 `httpx` 哪天改了语义
    （比如这个参数失效），源码断言仍会全绿而风险已经回来了。
    所以这里用 httpx 自己的内部结构验证：`trust_env=True` 会为环境里的
    代理建 transport，`False` 不会。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9/")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9/")
    monkeypatch.delenv("NO_PROXY", raising=False)

    with httpx.Client() as trusting:
        assert trusting.trust_env is True
        trusting_mounts = len(dict(trusting._mounts))

    with httpx.Client(trust_env=False) as ignoring:
        assert ignoring.trust_env is False
        ignoring_mounts = len(dict(ignoring._mounts))

    assert trusting_mounts > 0, "trust_env=True 时应当为环境代理建 transport（说明这条参数有意义）"
    assert ignoring_mounts == 0, "trust_env=False 时不该为环境代理建 transport"


def test_reachability_probe_passes_trust_env_false(monkeypatch) -> None:
    """★ `scans/reachability._probe` 这一处是**用户可控目标**（原清单漏掉的第 8 处）。

    它用的是**模块级 `httpx.get`** 而不是 `httpx.AsyncClient(...)` ——
    所以"搜 AsyncClient"式的检查看不见它。这里按调用参数断言，
    防止它被改回去。
    """
    from app.scans import reachability

    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        captured["url"] = url
        captured.update(kwargs)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    assert reachability._probe("http://example.com/", timeout=1.0) is True
    assert captured["trust_env"] is False, (
        "目标是用户填的扫描目标（scan scope 就能触发），必须 trust_env=False"
    )
    assert captured["follow_redirects"] is False
