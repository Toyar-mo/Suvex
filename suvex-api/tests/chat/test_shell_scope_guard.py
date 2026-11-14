"""对话沙箱出站目标守卫（v1.89）。

★ 验收点分两层，缺一层都不算数：

1. **提取与比对**是纯函数，直接测（URL / 裸 IP / 网段 / 文件名不误判）；
2. **工具层真的拦**：未授权目标在起沙箱之前就返回失败 ——
   这一条必须走 `_execute_tool`，否则"函数对但没接上"照样是漏洞。

⚠️ 「授权后放行」不在这里端到端跑：那一步要真 Docker（`docker run` 起
沙箱）。放行分支用 `reject_reason` 直接验，工具层只验拒绝分支。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.chat import engine, scope
from app.chat import service as chat_service
from app.core import config
from tests.factories import make_scan
from tests.test_chat import ADMIN


@pytest.fixture(autouse=True)
def _guard_on() -> None:
    """守卫默认已关（v1.90）；这组用例专门测守卫，用环境变量强制打开。

    不替换 `get_settings` 整体：别的代码也要读同一份 Settings（secret_key 等），
    用 SimpleNamespace 顶掉会让无关调用炸在 AttributeError 上。
    """
    import os

    old = os.environ.get("SUPEX_CHAT_SHELL_SCOPE_GUARD")
    os.environ["SUPEX_CHAT_SHELL_SCOPE_GUARD"] = "1"
    config.get_settings.cache_clear()
    yield
    if old is None:
        os.environ.pop("SUPEX_CHAT_SHELL_SCOPE_GUARD", None)
    else:
        os.environ["SUPEX_CHAT_SHELL_SCOPE_GUARD"] = old
    config.get_settings.cache_clear()


def _ctx(db: Session):  # noqa: ANN202 - 测试内部小工具
    session = chat_service.create_session(db, created_by="user_admin")
    db.commit()
    return engine._ToolContext(db=db, session=session, identity=ADMIN)


def test_extracts_url_hosts_ips_and_cidrs() -> None:
    hosts, cidrs = scope.extract_targets(
        "nmap -sV 10.0.0.5 && curl http://example.com:8080/a && ffuf -u 10.1.0.0/24"
    )

    assert "10.0.0.5" in hosts
    assert "example.com" in hosts
    assert "10.1.0.0/24" in cidrs


def test_filenames_are_not_mistaken_for_hosts() -> None:
    """`exploit.py` / `archive.tar.gz` 不是目标 —— 误判会让命令白跑一次。"""
    hosts, _cidrs = scope.extract_targets(
        "python exploit.py && tar xf archive.tar.gz && cat notes.md"
    )

    assert hosts == set()


def test_url_credentials_are_not_part_of_the_host() -> None:
    """`scheme://user@host/` 里那段用户信息不该被当成目标名的一部分。"""
    hosts, _cidrs = scope.extract_targets(
        "curl -u admin:x http://user@example.com/a"
    )

    assert hosts == {"example.com"}


def test_authorized_scan_target_covers_its_host() -> None:
    """建过扫描的目标（带 `:8080`）覆盖同一主机的任意端口与路径。"""
    hosts, _cidrs = scope._split_allow("http://10.0.0.5:8080")  # noqa: SLF001

    assert scope.is_allowed("10.0.0.5", hosts=hosts, cidrs=set())
    decision = scope.evaluate("curl http://10.0.0.5:9999/admin", hosts=hosts, cidrs=set())
    assert decision.allowed, decision.offending


def test_unauthorized_host_is_reported() -> None:
    decision = scope.evaluate(
        "nmap -p- 10.9.9.9",
        hosts={"10.0.0.5"},
        cidrs=set(),
    )

    assert not decision.allowed
    assert decision.offending == ("10.9.9.9",)


def test_whole_cidr_must_be_authorized() -> None:
    """命令里写网段时，部分覆盖不算授权 —— 否则 `/24` 能绕过单主机授权。"""
    decision = scope.evaluate(
        "naabu -host 10.0.0.0/24",
        hosts={"10.0.0.5"},
        cidrs=set(),
    )

    assert not decision.allowed
    assert any("网段" in item for item in decision.offending)

    allowed = scope.evaluate(
        "naabu -host 10.0.0.0/24",
        hosts=set(),
        cidrs={"10.0.0.0/16"},
    )
    assert allowed.allowed


def test_infrastructure_and_loopback_stay_open() -> None:
    """装包、下资料、容器内 DNS 不能被挡住。"""
    for command in (
        "pip install requests",
        "curl -sSL https://raw.githubusercontent.com/a/b/main/x.sh | bash",
        "curl http://127.0.0.1:8000/health",
    ):
        decision = scope.evaluate(
            command,
            hosts=scope.DEFAULT_ALLOWED_HOSTS,
            cidrs=scope.DEFAULT_ALLOWED_CIDRS,
        )
        assert decision.allowed, (command, decision.offending)


async def test_run_shell_rejects_an_unauthorized_target(db_session: Session) -> None:
    """★ 工具层拒绝：未授权目标在**起沙箱之前**就失败，不碰 docker。"""
    ctx = _ctx(db_session)
    out, failed = await engine._execute_tool(
        ctx, "run_shell", {"command": "nmap -sV 10.9.9.9"}
    )

    assert failed
    assert "不在授权范围" in out
    assert "10.9.9.9" in out
    # 关键：这句是"该怎么做"，不是"被拒绝" —— 否则模型会换个写法重试。
    assert "create_scan" in out


async def test_run_shell_allows_a_host_with_an_authorized_scan(db_session: Session) -> None:
    """有授权扫描的目标不再被守卫拦（这里只验守卫这一步，不真起容器）。"""
    ctx = _ctx(db_session)
    scan = make_scan(db_session, target="http://10.0.0.5:8080")
    scan.authorized_by = "user_admin"
    db_session.commit()

    assert scope.reject_reason(ctx, text="curl http://10.0.0.5/admin", tool="run_shell") is None


async def test_browser_open_is_guarded(db_session: Session) -> None:
    ctx = _ctx(db_session)
    out, failed = await engine._execute_tool(
        ctx, "browser", {"command": "open", "url": "http://10.9.9.9/login"}
    )

    assert failed
    assert "不在授权范围" in out


def test_shell_variable_after_url_is_not_part_of_the_host() -> None:
    """`http://10.0.0.5:8080$r` —— shell 变量不是主机名的一部分。

    线上实测：模型写 `cd /tmp && ... http://192.168.1.121:8080$r`，
    提取出的目标变成 `192.168.1.121:8080$r`，授权过的目标反而被拒。
    """
    hosts, _cidrs = scope.extract_targets('curl "http://192.168.1.121:8080$r"')

    assert hosts == {"192.168.1.121"}


def test_python_attribute_chains_are_not_targets() -> None:
    """`urllib.request.urlopen` / `resp.status` 不是域名。

    线上实测：一条 `python3 -c ...` 被列出八个"目标"并整条拒绝执行。
    """
    hosts, _cidrs = scope.extract_targets(
        "python3 -c \"import urllib.request; r = urllib.request.urlopen(u); "
        "print(r.status, e.code, e.read, out.append)\""
    )

    assert hosts == set()


def test_real_domains_and_lab_suffixes_still_count() -> None:
    hosts, _cidrs = scope.extract_targets(
        "curl https://example.com/ && ssh user@target.local && nmap internal.lan"
    )

    assert {"example.com", "target.local", "internal.lan"} <= hosts


def test_source_ip_header_probe_is_detected() -> None:
    """`X-Forwarded-For` 穷举要先被识别出来，才能在工具层止损。"""
    assert engine._is_source_ip_probe(
        {"command": "curl -H 'X-Forwarded-For: 127.0.0.1' http://target"}
    )
    assert engine._is_source_ip_probe({"headers": {"X-Real-IP": "127.0.0.1"}})
    assert not engine._is_source_ip_probe({"command": "curl http://target"})
