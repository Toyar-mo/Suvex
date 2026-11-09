"""首轮预检：在花模型回合之前先把文件里「像 flag」的字符串摊出来。

对应 `ctf-agent-workstation` 的 `ctfgrep` 预检。这里钉三件事：

1. 扫到的东西**去重、限量**，并以可读的一行一条返回；
2. 扫描命令**排除** `/work/persist`（历史运行的猜测会误导这一轮）；
3. 扫描失败/没结果是**空串**，不是异常 —— 预检是加速项，不是判据。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.kernel import runner


class _FakeSandbox:
    def __init__(self, *, stdout: str = "", ok: bool = True) -> None:
        self.stdout = stdout
        self.ok = ok
        self.calls: list[str] = []

    def shell(self, command: str, *, timeout: float = 120) -> SimpleNamespace:
        self.calls.append(command)
        return SimpleNamespace(ok=self.ok, stdout=self.stdout)


async def test_preflight_returns_deduped_candidates() -> None:
    sandbox = _FakeSandbox(
        stdout="flag{abcd}\nDASCTF{real_one}\nflag{abcd}\n\nnot-a-flag\n",
    )

    out = await runner._preflight_flag_scan(sandbox)  # noqa: SLF001

    assert "- `flag{abcd}`" in out
    assert "- `DASCTF{real_one}`" in out
    assert out.count("flag{abcd}") == 1, "重复命中要合并，否则首轮提示被噪音占满"
    assert "not-a-flag" not in out


async def test_preflight_excludes_the_persist_volume() -> None:
    sandbox = _FakeSandbox(stdout="")

    await runner._preflight_flag_scan(sandbox)  # noqa: SLF001

    assert sandbox.calls, "没有执行扫描命令"
    assert "--exclude-dir=persist" in sandbox.calls[0], (
        "必须排除 /work/persist：上一轮的猜测不该被当成本轮线索"
    )
    assert "/work" in sandbox.calls[0]


async def test_preflight_empty_output_is_empty_string() -> None:
    assert await runner._preflight_flag_scan(_FakeSandbox(stdout="")) == ""  # noqa: SLF001


async def test_preflight_failure_is_swallowed() -> None:
    sandbox = _FakeSandbox(stdout="flag{abcd}\n", ok=False)
    assert await runner._preflight_flag_scan(sandbox) == ""  # noqa: SLF001


async def test_preflight_caps_the_number_of_lines() -> None:
    stdout = "\n".join(f"flag{{n{i:03d}}}" for i in range(50))
    out = await runner._preflight_flag_scan(_FakeSandbox(stdout=stdout), limit=3)  # noqa: SLF001
    assert len(out.splitlines()) == 3
