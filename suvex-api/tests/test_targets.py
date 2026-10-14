"""目标归一化与校验（契约 §2.8）。

这些用例与前端 `src/lib/validate-target.ts` **是同一张表的两份实现** ——
契约要求后端用同一套规则，否则同一个目标会在库里存成多条记录。
所以下面的每个用例都刻意照着前端那份写，改一边就要改另一边。
"""

from __future__ import annotations

import pytest

from app.scans import targets

# ══════════════════════════════════════════════════════════
# 归一化
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 同一目标的三种写法必须收敛成一条（契约 §2.8 的核心论点）
        ("https://api.example.com/", "api.example.com"),
        ("https://api.example.com", "api.example.com"),
        ("api.example.com", "api.example.com"),
        # ⚠️ 端口必须保留：同一主机的不同端口是两个不同目标
        # （`:8443` 是另一个服务）
        ("https://api.example.com:8443/", "api.example.com:8443"),
        # 路径与查询串丢掉 —— 它们是"目标上的位置"，不是目标本身
        ("https://api.example.com/admin/login?next=/", "api.example.com"),
        # 大小写由 URL 规则处理（主机名不区分大小写）
        ("https://API.Example.COM/", "api.example.com"),
        # localhost 明确放行：本地服务是合理目标
        ("http://localhost:8000/x", "localhost:8000"),
    ],
)
def test_url_normalization(raw: str, expected: str) -> None:
    assert targets.normalize("url", raw) == expected


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        # ⚠️ 仓库与路径**原样保留**，不小写、不去尾斜杠：
        #   · 路径大小写在 Windows 上不敏感、在 Linux 上敏感；
        #   · 仓库地址的 `.git` 后缀会影响克隆行为。
        # 这两类"看起来不统一"是**正确的**，不要去"修"它。
        ("repo", "https://github.com/Acme/Backend.git"),
        ("repo", "git@github.com:Acme/Backend.git"),
        ("repo", "https://github.com/acme/backend/"),
        ("path", "D:\\Projects\\Acme"),
        ("path", "D:\\projects\\acme"),
        ("path", "/opt/Acme/"),
    ],
)
def test_repo_and_path_are_kept_verbatim(kind: str, raw: str) -> None:
    assert targets.normalize(kind, raw) == raw


def test_normalize_leaves_unparsable_url_alone() -> None:
    """解析不了就原样返回 —— 错误由 `validate` 报，归一化不该吞掉输入。

    原样返回而不是返回空串：空串会让记录里出现一个"没有目标"的扫描，
    而真正的问题是"这个目标写错了"，那是校验该说的话。
    """
    assert targets.normalize("url", "http://[bad") == "http://[bad"


# ══════════════════════════════════════════════════════════
# 校验
# ══════════════════════════════════════════════════════════


def test_empty_target_is_rejected() -> None:
    check = targets.validate("url", "   ")
    assert check.ok is False
    assert check.error == "请填写目标"


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        ("url", "api"),  # 主机名不含 . 也不是 localhost
        ("url", "https://localhost-api"),  # 同理
        ("url", "https://"),  # 没有主机
        ("url", "https://api example.com"),  # 含空格 —— 一定是误输入
        ("url", "https://api.example.com:99999"),  # 端口越界
        ("url", "https://api.example.com:abc"),  # 端口不是数字
        ("repo", "github.com/acme/backend"),  # 缺协议也缺 git@
        ("repo", "ftp://host/path"),
        ("path", "relative/path"),  # 不是绝对路径
        ("path", "~/projects"),
    ],
)
def test_obviously_wrong_targets_are_rejected(kind: str, raw: str) -> None:
    assert targets.validate(kind, raw).ok is False


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        # ★ 刻意**不做得太严**（前端注释里的原话）：
        # 这是安全测试工具，用户会填内网 IP、带端口主机、非标准 TLD、IP 段。
        # 过严的正则会拦掉合法输入，而用户无法绕过，只能改工具。
        ("url", "192.168.1.10"),
        ("url", "10.0.0.5:8080"),
        ("url", "api.corp"),  # 非标准 TLD，公司内网真实存在
        ("url", "http://127.0.0.1:8000"),
        ("url", "localhost:5173"),
        ("repo", "https://gitlab.internal/team/app.git"),
        ("repo", "git@10.0.0.1:team/app.git"),
        ("path", "C:\\anything"),
        ("path", "/anything"),
    ],
)
def test_plausible_targets_are_accepted(kind: str, raw: str) -> None:
    assert targets.validate(kind, raw).ok is True, f"{kind} {raw} 不该被拒"


def test_url_without_scheme_gets_a_hint_not_an_error() -> None:
    """没写协议时给的是**提示**而不是错误 —— 自动补 https 是常见习惯。

    这条提示在确认页上有用（"将按 https:// 访问"），
    而把它塞进 error 会让用户以为要改。
    """
    check = targets.validate("url", "api.example.com")

    assert check.ok is True
    assert check.error is None
    assert check.hint == "将按 https:// 访问"


def test_explicit_scheme_has_no_hint() -> None:
    check = targets.validate("url", "https://api.example.com")
    assert check.ok is True
    assert check.hint is None


def test_repo_and_path_hints_explain_what_will_happen() -> None:
    """提示语告诉用户"会发生什么"，不只是格式说明。

    "将以只读方式克隆"与"不会修改文件"分别是选仓库和选本地路径时
    最关心的两件事 —— 它们的措辞与前端 `validate-target.ts` 一致。
    """
    assert "只读" in (targets.validate("repo", "https://github.com/a/b").hint or "")
    assert "不会修改文件" in (targets.validate("path", "D:/a").hint or "")


# ══════════════════════════════════════════════════════════
# 默认名称
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        ("url", "https://api.example.com/", "api.example.com"),
        # host 保留端口 —— 同一主机不同端口是两个不同目标，不能丢
        ("url", "https://api.example.com:8443", "api.example.com:8443"),
        ("repo", "https://github.com/acme/backend.git", "acme/backend"),
        ("repo", "git@github.com:acme/backend.git", "acme/backend"),
        ("path", "D:\\projects\\acme", "acme"),
        ("path", "/opt/acme/", "acme"),
        ("url", "", "未命名扫描"),
    ],
)
def test_default_name_from_target(kind: str, raw: str, expected: str) -> None:
    assert targets.default_name(kind, raw) == expected


def test_unknown_kind_is_rejected() -> None:
    assert targets.validate("nonsense", "whatever").ok is False
