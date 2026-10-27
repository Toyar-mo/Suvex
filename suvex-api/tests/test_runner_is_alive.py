"""`is_alive()` 的守卫：Windows 上不要因为**解码**而崩。

## 这条测试是为一个真实事故写的（2026-10-27）

跑后端全量测试时（那一次用了 `PYTHONUTF8=1`），`tests/test_scans.py` 一族
共 33 个用例同时变红，报错都指向同一行：

    app/scans/runner.py:1094: TypeError: argument of type 'NoneType' is not iterable
        return str(pid) in completed.stdout

而真正的起因藏在 pytest 打印的一条**看起来无关**的 warning 里：

    UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd0 in position 0
      File "subprocess.py", line 1599, in _readerthread

链路是这样的：

1. `subprocess.run(text=True)` 会在**独立的读取线程**里用
   `locale.getpreferredencoding()` 解码子进程输出；
2. 中文 Windows 上 `tasklist` 输出的是控制台代码页（CP936/GBK）字节，
   含 `0xd0` 这种在 UTF-8 里非法的高位字节；
3. 进程编码被环境变量改成 UTF-8 时，解码线程抛 `UnicodeDecodeError` 并死掉，
   异常被吞进线程里（所以主线程看不到 traceback）；
4. `CompletedProcess.stdout` 于是是 `None`；
5. `str(pid) in None` → `TypeError`。

## 为什么必须守住，而不是"CI 里别设那个变量就行"

`is_alive()` 被 `recover_orphans()` 在 **lifespan 里**调用（服务启动时做孤儿
回收）。它一抛异常，整个服务起不来；而报错信息指向一句 `in` 比较，
跟"编码"毫无关系 —— 现场排查成本极高（本次实测：65 分钟的全量测试才把它逼出来，
单跑那个用例时因为环境变量不同反而**是绿的**）。

## 判据为什么不写成"断言 stdout 是 bytes"

那是在断言实现细节。这里断言的是**行为**：给定一个"输出是非 UTF-8 字节"
的子进程（`tasklist` 的真实行为），`is_alive()` 必须给出一个布尔值、不许抛。
这样将来无论是换 `errors="replace"`、换 `encoding=`、还是干脆改用
`ctypes` 调 `OpenProcess`，只要不崩就都算过。
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from app.scans import runner


pytestmark = pytest.mark.skipif(
    __import__("os").name != "nt",
    reason="这条守的是 Windows 的 tasklist 分支；POSIX 走 os.kill，没有解码这一步",
)


#: 中文 Windows 上 `tasklist` 输出的真实开头：`信息: 没有运行的任务匹配指定标准。`
#: 用 GBK 编码后就是下面这些字节（`0xd0` 开头 —— 正是事故里那个字节）。
_GBK_TASKLIST_OUTPUT = "信息: 没有运行的任务匹配指定标准。".encode("gbk")


def test_is_alive_survives_non_utf8_tasklist_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ `tasklist` 吐出 GBK 字节时，`is_alive()` 必须返回 False 而不是抛异常。

    事故复现条件：`text=True` + 进程编码 != 控制台代码页。
    这里直接伪造"字节输出"，把解码这一步暴露出来。
    """

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        # 关键断言 1：**不许**传 text=True / universal_newlines=True。
        # 传了就等于把解码交给 locale，而这正是事故的入口。
        assert not kwargs.get("text"), (
            "is_alive() 又用 text=True 让 subprocess 自己解码了 —— "
            "中文 Windows 上 tasklist 输出的是 GBK，进程编码是 UTF-8 时"
            "解码线程会抛 UnicodeDecodeError，stdout 变成 None，"
            "接着 `str(pid) in None` 抛 TypeError，服务启动时的孤儿回收直接崩。"
            "请拿字节自己用 errors='replace' 解。"
        )
        assert not kwargs.get("universal_newlines"), (
            "universal_newlines=True 与 text=True 等价，同样会引入 locale 解码。"
        )
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=_GBK_TASKLIST_OUTPUT,
            stderr=b"",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    # 不许抛。这个断言本身就是守的重点。
    assert runner.is_alive(4321) is False


def test_is_alive_treats_missing_stdout_as_not_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`stdout` 为 `None` 时返回 False，而不是 `TypeError`。

    这是事故的**第二道**防线：即使将来有人把 `text=True` 加回去、解码又失败了，
    也不该让整个服务陪葬。当时线上的表现就是 `str(pid) in None` 抛 TypeError。
    """

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=None, stderr=None
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner.is_alive(4321) is False


def test_is_alive_finds_the_pid_in_utf8_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常情况：输出里带着 PID 就算活着。

    这条防的是"为了不崩而永远返回 False"—— 那样孤儿回收会把**还活着**的扫描
    判成死掉并标记失败，比崩更糟（用户看到"扫描莫名失败"）。
    """

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        # tasklist /NH 的输出形如：
        #   python.exe                   4321 Console                    1     12,345 K
        line = "python.exe                   4321 Console       1     12,345 K\n"
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=line.encode("ascii"), stderr=b""
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner.is_alive(4321) is True
    # 换个 pid 就不该命中 —— 否则说明判据是"有输出就算活着"。
    assert runner.is_alive(9999) is False
