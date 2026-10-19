"""真的 pwn 靶机：源码、编译、以及把二进制挂到 TCP 上的驱动。

## 为什么需要这个包

`scripts/mock_arena_server.py` 里原来的两道 pwn 题是**假的**：

* `#1003` 的附件 `baby` 是 `b"\\x7fELF" + bytes(range(256)) * 2` 伪装的
  噪声，flag 直接明文躺在里面 —— `strings` 一把捞出来；
* `#1006` 的 `fmt` 把 flag 切成 `token_part1` / `token_part2` 两段埋进去 ——
  把两段拼起来就完事。

用这种题跑"pwn 工具补强前后对比"，两个臂（装 `patchelf`/`gdb`/`one_gadget`
之前 / 之后）**必然跑出一样的结果**，因为它根本不需要写 exploit。那不是
"补强没用"，是题目测不出补强 —— 拿它当基线会把"测不了"说成"没用"。

这里的题目是真的：flag **不在二进制里**，只在**运行时**从
`PWN_FLAG_FILE` 指向的文件读出，唯一拿到它的路径是写一个真的 exploit。
于是要用上 `checksec`（看保护）、`cyclic` + `gdb`（算偏移）、
`ROPgadget`（找 gadget）、`p64`（拼 payload）—— 正好是这次补强装的那些。

## 四道题覆盖的能力

| 题 | 二进制 | 需要的东西 | 有远程靶机 |
|---|---|---|---|
| `#1003` ret2win | `pwn_baby.c` | `checksec` → `cyclic` → `gdb` 反查偏移 → `p64` | 有 |
| `#1006` 带参 ROP | `pwn_rop.c` | 上面全部 **+** `ROPgadget` 找 `pop rdi; ret` **+** 找字符串地址 | 有 |
| `#1011` 改解释器 | `pwn_patch.c` | **`patchelf --set-interpreter`** —— 不改就一步都走不下去 | 无（纯附件） |
| `#1012` 看运行时 | `pwn_debug.c` | **`gdb` 断在进程里读运行时随机数** —— 静态读不到 | 无（纯附件） |

`#1006` 比 `#1003` 多两个真难点（见 `pwn_rop.c` 顶部注释）：链里必须有
`ret` 垫片（否则 `movaps` 栈未对齐直接 SIGSEGV），以及 `win()` 要一个
指向 `"unlock"` 的参数。

### 后两道是为什么加的（与「heap 题不主动造」相反的判断）

`#1003`/`#1006` 里，`patchelf` 和 `gdb` **都不是唯一路径**：偏移能从
`objdump main` 的栈帧直接算，符号能从 `nm` 拿，gadget 能从 `ROPgadget`
搜 —— 一个只肯读反汇编的模型两道都能做。所以在它们上面跑「装 patchelf
和 gdb 之前 / 之后」，两臂必然一样。**那不是补强没用，是题目里没有那一环。**

「这道题需不需要 patchelf」是设计题目时自己说了算的，二元可控；不像
「题目要多难才能测出省了多少探索」那样难校准。所以这里值得主动造：

* `pwn_patch`：`PT_INTERP` 指向不存在的 `/pwn64/ld-linux-x86-64.so.2`，
  直接执行报 `No such file or directory`，只有 `patchelf --set-interpreter`
  能救。用不用得上 patchelf 从运气变成了确定条件。
* `pwn_debug`：正确输入是每次运行才由 `getrandom()` 生成的 8 字节，
  一个字节都不落进二进制、也不发给客户端，只在进程内存里。
  `strings`、`objdump`、静态分析全断，只能 gdb 断在那个进程里读。

两道都是**纯附件**（没有远程靶机）：真 CTF 里这两类题目通常也是本地
附件题，模型在自己的沙箱里 `patchelf` / `gdb`，那正是要测的地方。

## 为什么靶机跑在**宿主**上，而不是自己起一个容器

`suvex-ctf2` 这个 docker 网络建的时候带 `ICC=false`（见
`app/ctf2/runner.py` 的 `ensure_ctf2_network`），容器之间**打不通**。
实测过：沙箱容器连 `172.18.0.1:31001` 通（那是 bridge 网关），连
`127.0.0.1:31001` 是 `ConnectionRefusedError`（那是容器自己的回环）。

所以靶机只能**绑宿主机的 `0.0.0.0`**，让沙箱经网关连过来。副作用是好的：
靶机进程跟着 mock 的生命周期走（mock 停 → 靶机停），不会留下野进程。

⚠️ 因此 `endpoints` 里给的 IP 必须是**网关地址**，不能是 `127.0.0.1`
—— 后者在容器里指的是容器自己。取法见 `scripts/mock_arena_server.py`
的 `sandbox_gateway()`。

## 为什么编译而不是把二进制提交进仓库

`.gitattributes` 里 `* text=auto eol=lf` 会把所有文件当文本，二进制入库
要么加 `binary` 属性、要么每次 checkout 冒被换行符改坏的风险；而
"这个二进制是从哪份源码编出来的"也会变成一个没人能回答的问题。

所以提交**源码**，运行时用 `gcc` 编译，产物落在 `_tmp/pwn_targets-build/`
（`_tmp/` 已被 `.gitignore` 排除）。编译一次约 0.1 秒，且按 mtime 跳过。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

#: 题目名 → 源码文件名。名字就是编译产物的文件名。
SOURCES: dict[str, str] = {
    "pwn_baby": "pwn_baby.c",
    "pwn_rop": "pwn_rop.c",
    "pwn_patch": "pwn_patch.c",
    "pwn_debug": "pwn_debug.c",
}

#: 这道题的 `PT_INTERP` 要指到哪。
#:
#: ★ 这个路径**故意不存在** —— 那是 `pwn_patch` 的全部内容：直接执行会
#:   报 `No such file or directory`（一个 `ls` 看得见的文件"不存在"），
#:   唯一出路是 `patchelf --set-interpreter /lib64/ld-linux-x86-64.so.2`。
#:   不改这道题这一步，就一步都走不下去。
BROKEN_INTERP = "/pwn64/ld-linux-x86-64.so.2"

#: 每道题**额外**的编译参数（叠在 `CFLAGS` 上）。不在表里的题就用纯 `CFLAGS`。
EXTRA_FLAGS: dict[str, tuple[str, ...]] = {
    "pwn_patch": (f"-Wl,--dynamic-linker={BROKEN_INTERP}",),
}

#: 编译产物与 flag 文件的落点。**必须在 `_tmp/` 下** ——
#: `.gitattributes` 的 `* text=auto eol=lf` 会把所有文件当文本，
#: 二进制入库要么加 `binary` 属性、要么每次 checkout 冒被改坏的风险。
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
BUILD_DIR = PACKAGE_ROOT / "_tmp" / "pwn_targets-build"
FLAG_DIR = PACKAGE_ROOT / "_tmp" / "pwn_targets-flags"

#: 编译参数。三道题都要**关掉 canary / 关掉 PIE**，否则偏移与地址
#: 每次都变，题目就不再是"算一次能用一辈子"的那类，模型也只能靠泄露
#: 才能做题 —— 那是另一类题，不是我们要测的工具链。
#:
#: `-O0` 是必须的：`-O2` 会把 `main` 里的栈帧重排，实测偏移从 80 变成 88，
#: 而 `pwn_baby.c` 顶部注释写的那个 80 就会变成错的。
CFLAGS: tuple[str, ...] = (
    "-O0",
    "-fno-stack-protector",
    "-no-pie",
    "-fno-strict-aliasing",
)


class BuildError(RuntimeError):
    """编译不出来。**不静默降级** —— 靶机起不来时应当明确报错。"""


def _source_path(name: str) -> Path:
    if name not in SOURCES:
        raise BuildError(f"没有这道题的源码：{name}（有：{', '.join(SOURCES)}）")
    return Path(__file__).resolve().parent / SOURCES[name]


def build(name: str, *, force: bool = False) -> Path:
    """编译一道题，返回二进制路径。已是最新就跳过。

    判据是 **mtime** 而不是内容哈希：源码是手改的，改一下就该重编；
    而内容哈希要读全部文件才能比，收益不成比例。
    """
    source = _source_path(name)
    if not source.is_file():
        raise BuildError(f"源码不存在：{source}")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    target = BUILD_DIR / name
    if not force and target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target

    compiler = shutil.which("gcc") or shutil.which("cc")
    if compiler is None:
        raise BuildError(
            "找不到 gcc/cc —— 这道靶机需要在**宿主机**上编译（不是沙箱镜像里编）。"
            "Kali 自带 gcc；Windows 上跑 mock 时 pwn 题会起不来。"
        )

    completed = subprocess.run(  # noqa: S603 - 参数是常量 + 本地路径，无 shell
        [compiler, *CFLAGS, *EXTRA_FLAGS.get(name, ()), "-o", str(target), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not target.is_file():
        raise BuildError(
            f"编译 {name} 失败（{compiler} 返回 {completed.returncode}）：\n"
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return target


def _flag_file(name: str, flag: str) -> Path:
    """把 flag 写进一个文件 —— 二进制里**没有**它，只有运行时读得到。"""
    FLAG_DIR.mkdir(parents=True, exist_ok=True)
    path = FLAG_DIR / f"{name}.flag"
    text = flag if flag.endswith("\n") else flag + "\n"
    if not path.is_file() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return path


def _pump(src: object, conn: socket.socket) -> None:
    """把靶机进程的 stdout **逐字节**转发给客户端。

    逐字节而不是 `read(4096)`：`input: ` 这种提示没有换行、也还没填满
    缓冲区，按块读会让客户端一直等（它以为靶机没吐东西）。这是
    pwn 交互里最常见的坑之一。
    """
    stream = src
    try:
        while True:
            chunk = stream.read(1)  # type: ignore[attr-defined]
            if not chunk:
                break
            conn.sendall(chunk)
    except OSError:
        pass


class Target:
    """一道题的真靶机：TCP 监听 + 每次连接起一个靶机进程。"""

    def __init__(self, name: str, *, port: int, flag: str, host: str = "0.0.0.0") -> None:
        self.name = name
        self.port = port
        self.host = host
        self.flag = flag
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    # ── 生命周期 ──────────────────────────────────────
    def start(self) -> None:
        binary = build(self.name)
        flag_file = _flag_file(self.name, self.flag)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 重启 mock 时上一轮的 TIME_WAIT 还占着端口 —— 不设这个会
        # `OSError: Address already in use`，而且报错发生在起靶机这一步，
        # 看起来像"端口被别的程序占了"。
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(16)
        self._sock = sock
        self._thread = threading.Thread(
            target=self._accept_loop,
            args=(sock, binary, flag_file),
            daemon=True,
            name=f"pwn-target-{self.name}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    @property
    def running(self) -> bool:
        return self._sock is not None

    # ── 内部 ──────────────────────────────────────────
    def _accept_loop(self, sock: socket.socket, binary: Path, flag_file: Path) -> None:
        while not self._stopping.is_set():
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve,
                args=(conn, binary, flag_file),
                daemon=True,
                name=f"pwn-target-{self.name}-conn",
            ).start()

    def _serve(self, conn: socket.socket, binary: Path, flag_file: Path) -> None:
        env = dict(os.environ)
        # ⚠️ flag 走**环境变量**指路径，不走命令行参数 —— 参数会出现在
        # `/proc/<pid>/cmdline` 里，模型 `ps aux` 一下就能看到文件名
        # （虽然还得读文件，但那已经不算"写 exploit 才拿得到"了）。
        env["PWN_FLAG_FILE"] = str(flag_file)
        proc = subprocess.Popen(  # noqa: S603 - 本地常量路径
            [str(binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env,
        )
        assert proc.stdout is not None
        out_thread = threading.Thread(
            target=_pump,
            args=(proc.stdout, conn),
            daemon=True,
            name=f"pwn-target-{self.name}-out",
        )
        out_thread.start()

        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                assert proc.stdin is not None
                proc.stdin.write(data)
                proc.stdin.flush()
        except OSError:
            pass
        finally:
            # 关 stdin 让靶机的 `read()` 返回 EOF，它才会往下跑并退出。
            # 不关的话 `proc.wait()` 会一直等 —— 而这个线程是 daemon，
            # 泄漏的进程不会被回收，多跑几轮就是一堆僵尸。
            with contextlib.suppress(OSError):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            with contextlib.suppress(OSError):
                conn.close()


class Farm:
    """一组靶机。`start()` 起全部、`stop()` 停全部。"""

    def __init__(self) -> None:
        self._targets: dict[str, Target] = {}
        self.errors: dict[str, str] = {}

    def add(self, name: str, *, port: int, flag: str) -> Target:
        # 幂等：`build_app` 可能被调用多次（测试、TestClient），
        # 重建 Target 会让已经 listen 的 socket 失去引用而泄漏端口。
        existing = self._targets.get(name)
        if existing is not None:
            existing.flag = flag
            return existing
        target = Target(name, port=port, flag=flag)
        self._targets[name] = target
        return target

    def start(self) -> None:
        for name, target in self._targets.items():
            if target.running:
                continue
            try:
                target.start()
            # 靶机起不来**不该**让整个 mock 起不来：其余八道题还能跑，
            # 而"pwn 题没起来"会以"连接被拒"的形式明确出现在解题过程里。
            # 收进 errors 是为了能在启动日志里说清楚，而不是静默跳过。
            except (BuildError, OSError) as exc:
                self.errors[name] = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        for target in self._targets.values():
            target.stop()

    def describe(self) -> str:
        lines: list[str] = []
        for name, target in self._targets.items():
            if name in self.errors:
                lines.append(f"  {name}: FAILED {self.errors[name]}")
            else:
                lines.append(f"  {name}: listening {target.host}:{target.port}")
        return "\n".join(lines)
