"""在**真 Docker**上验证 CTF2 沙箱容器的运行参数。

## 为什么单测不够

`tests/kernel/test_runner.py` 断言的是 `SandboxContainer._argv()` 拼出来的
**字符串**：里面有 `--tmpfs /work:rw,exec,size=512m,uid=1000,gid=1000`、
`--cap-drop ALL`、`--network none`、`--read-only`。但"字符串里有"不等于
"Docker 收下了它、并且真的那样运行"。这几条只有真跑一次才知道：

* `--tmpfs` 的 `uid=1000` 写错时 Docker **照样启动**，只是容器里写不进去
  （报错是 Permission denied，指向的方向是"权限配错了"）；
* `--read-only` 是否真的挡住写根文件系统；
* `-v …:ro` 是否真的只读（"宿主上没多出文件"才是证据）；
* `--network none` 是否真的断网 —— 这是本模块**最重要**的安全参数；
* **login shell 里 `python3` 是不是 venv 的那个** —— 这条最隐蔽：
  kali-rolling 的 `/etc/profile` 会重置 PATH，而 `runner.shell()` 用的是
  `bash -lc`。实测过：不额外修的话容器里 `python3` 是**系统**那个，
  于是 `import pwn` 报 ModuleNotFoundError，而 `python3 -V` 照样打印
  同一个版本号 —— 看起来完全正常。

## 它需要什么

* 一台有 Docker 的机器（本项目的验证环境是 Kali）；
* 两个镜像已经构建好：`ctf2-sandbox` 与 `ctf2-sandbox-re`
  （构建脚本见 `docker/` 下两个 Dockerfile 的文件头说明）。

用法：

    python scripts/verify_ctf2_sandbox.py            # 两个镜像都验
    python scripts/verify_ctf2_sandbox.py base       # 只验 base
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from app.kernel import runner  # noqa: E402

# 宿主侧那个"只读资源目录"（验证 `-v …:ro` 真的只读）。
# 放在**项目外面**：它模拟的是"宿主上的字典目录"，不该混进仓库。
HOST_RES = Path("/tmp/ctf2-sandbox-hostres")

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, title: str, detail: str = "") -> None:
    RESULTS.append((ok, title, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {title}" + (f" — {detail}" if detail else ""), flush=True)


def probe(image: str, *, label: str) -> None:
    print(f"\n── {label}（{image}）──", flush=True)
    container = runner.SandboxContainer(
        name=f"ctf2smoke-{label}",
        image=image,
        host_resources=HOST_RES,
        # 与 worker 真实构造的那一份一致（见 `test_worker_only_sets_harmless_env`）
        extra_env={"PWNLIB_NOTERM": "1"},
        memory_mb=512,
        cpus=1,
    )
    try:
        try:
            container.start()
        except Exception as exc:  # noqa: BLE001
            check(False, f"{label}：容器能启动", str(exc)[:200])
            return
        check(True, f"{label}：容器能启动")

        # ① 非 root。它拿掉的那部分能力（原始套接字、内核接口）恰好是
        #    逃逸与提权最常用的那部分 —— 所以这条要验，不能只看 Dockerfile。
        uid = container.shell("id -u")
        check(uid.stdout.strip() == "1000", f"{label}：容器内是非 root（uid 1000）", uid.stdout.strip())

        # ② /work 可写（验证 `--tmpfs` 的 uid/gid 参数是对的）。
        wrote = container.shell("echo hi > /work/probe.txt && cat /work/probe.txt")
        check(wrote.stdout.strip() == "hi", f"{label}：/work 可写（tmpfs uid/gid 正确）")

        # ③ 根文件系统只读。
        readonly = container.shell("touch /probe-root-file 2>&1 || true")
        check("read-only" in readonly.stdout.lower(), f"{label}：根文件系统只读", readonly.stdout.strip()[:100])

        # ④ 宿主资源**只读**挂载 + 宿主上确实没被写入。
        write_host = container.shell(f"touch {runner.HOST_RESOURCES_PATH}/pwned 2>&1 || true")
        check(
            "read-only" in write_host.stdout.lower(),
            f"{label}：宿主资源挂载是只读的",
            write_host.stdout.strip()[:100],
        )
        check(
            not (HOST_RES / "pwned").exists(),
            f"{label}：宿主上没有被写进文件",
            str(HOST_RES / "pwned"),
        )

        # ⑤ 无网络。这是本模块最重要的一个安全参数。
        net = container.shell(
            "timeout 5 python3 -c \"import socket;socket.setdefaulttimeout(2);"
            "socket.create_connection(('1.1.1.1',80));print('CONNECTED')\" 2>&1 || echo BLOCKED"
        )
        check("BLOCKED" in net.stdout, f"{label}：容器内无出网（--network none 生效）")

        # ⑥ 容器内的 `timeout` 存在（runner 用它给 shell 工具限时；
        #    只靠宿主侧 subprocess timeout 杀不掉容器里的进程）。
        killed = container.shell("timeout --signal=KILL 1 sleep 10; echo code=$?")
        check("code=137" in killed.stdout, f"{label}：容器内 timeout 能 KILL", killed.stdout.strip()[:80])

        # ⑦ ★ login shell 里的 python3 必须是 venv 的那个。
        #    这条是最隐蔽的一条：`python3 -V` 在两种情况下**打印同一个版本号**
        #    （系统 python3 与 venv 是同一个 3.14），只有 import 才分得出来。
        which = container.shell("which python3")
        check(
            which.stdout.strip() == "/opt/venv/bin/python3",
            f"{label}：login shell 里的 python3 是 venv 的",
            which.stdout.strip()[:80],
        )

        # ⑧ 预装的解题库能导入。
        #
        # ⚠️ 判据用**退出码**，不用 `"OK" in stdout`：第一版就是那么写的，
        #    于是假通过了 —— import 失败时 Python 的 traceback 会**回显源码行**，
        #    而那行里正好有 `print("OK")` 这个字面量。
        #
        # ⚠️ 另外这里写的是 **import 名**，不是 pip 包名：
        #    pwntools→pwn、pycryptodome→Crypto、pillow→PIL、z3-solver→z3、
        #    pyelftools→elftools、keystone-engine→keystone。
        if label == "base":
            libs = "import pwn, z3, Crypto, PIL, sympy, capstone"
        else:
            libs = "import angr, capstone, lief, unicorn, keystone, elftools"
        imported = container.shell(f"python3 -c '{libs}'")
        check(
            imported.ok,
            f"{label}：预装的解题库可导入",
            (imported.stdout + imported.stderr).strip()[-160:],
        )

        # ⑨ `pyzbar` 要能找到系统的 libzbar（apt 的 libzbar0）——
        #    只装 pip 包时它 `import` 就报 "Unable to find zbar shared library"，
        #    而那是**运行期**才暴露的（构建完全成功）。
        if label == "base":
            zbar = container.shell("python3 -c 'import pyzbar.pyzbar'")
            check(zbar.ok, f"{label}：二维码库能找到 libzbar", zbar.stderr.strip()[-120:])

        if label != "base":
            tools = container.shell("which r2 gdb ltrace strace")
            found = all(name in tools.stdout for name in ("r2", "gdb"))
            check(found, f"{label}：逆向工具在 PATH 上", tools.stdout.split()[:4])

        # ⑩ 无残留：`destroy()` 之后容器不能再出现在 `docker ps -a` 里。
        #    没被销毁的容器会一直占着资源，而**没有任何界面能看到它** ——
        #    连续跑几十道题之后宿主会被拖垮，而排查者想不到去看 docker。
        container.destroy()
        leftover = runner._run(
            ["docker", "ps", "-a", "--filter", f"name=ctf2smoke-{label}", "--format", "{{.ID}}"],
            timeout=30,
        )
        check(leftover.stdout.strip() == "", f"{label}：destroy() 之后没有残留容器")
    finally:
        with contextlib.suppress(Exception):
            container.destroy()


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else ""

    HOST_RES.mkdir(parents=True, exist_ok=True)
    (HOST_RES / "wordlist.txt").write_text("admin\nroot\n", encoding="utf-8")

    print("CTF2 沙箱：真 Docker 上的运行参数验证", flush=True)
    if only != "re":
        probe(runner.IMAGE_BASE, label="base")
    if only != "base":
        probe(runner.IMAGE_REVERSE, label="re")

    print("\n" + "=" * 56, flush=True)
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [title for ok, title, _ in RESULTS if not ok]
    print(f"合计 {len(RESULTS)} 项：通过 {passed}，失败 {len(failed)}", flush=True)
    for title in failed:
        print(f"  ✗ {title}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
