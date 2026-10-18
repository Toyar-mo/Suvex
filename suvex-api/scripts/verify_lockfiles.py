"""校验锁文件没有过期 —— 改了声明却忘重生成，是这类项目最常见的事故。

## 要拦的是什么

`requirements.lock` 是 `requirements.txt` 的生成物，`requirements-dev.lock`
是 `requirements-dev.in` 的生成物（都是 `pip-compile --generate-hashes`）。
生成物看起来总是"已经存在"，所以没有任何东西会提醒你它过期了 ——
改了声明、忘了重跑，CI 与生产装的还是旧的一套，而仓库里一切正常。

## 判据为什么不是"重生成后无 diff"

因为声明里全是**范围约束**（`fastapi>=0.115,<1.0`），`pip-compile` 每次都
解析**当时最新的**兼容版本。只要 PyPI 发了新版，重跑就产生 diff —— 那是
**正常**的，不是过期。用 diff 当判据，CI 会在每次上游发版时无故变红，
红几次之后这条检查就会被人关掉，然后它就不再拦住任何东西。

真正会炸的是这两件事：

A. 声明里写了某个包，锁文件里却没有 —— 加了依赖忘重生成，装不上。
B. 锁文件 pin 的版本不满足声明的约束 —— 收紧了范围忘重生成，装不上。

两件都与"上游发没发新版"无关，所以判据稳定。

C. 两份锁文件必须解析出同一套运行时版本。`requirements-dev.in` 头部
   写着这条要求：否则 CI 装的与生产装的是两套东西，而"CI 全绿、生产炸"
   正是这么来的。

## 用法

    python scripts/verify_lockfiles.py

退出码 0 = 同步；1 = 有不同步项（CI 里就是这个用法）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_ROOT = HERE.parent

#: Windows 控制台默认 GBK，`✅` / `❌` 会直接抛 UnicodeEncodeError —— 而这个
#: 脚本恰恰是给人本地跑的第一个版本。CI 的 Linux 本来就是 UTF-8，不受影响。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: （声明文件，锁文件）—— 顺序无关，只影响输出顺序。
PAIRS: list[tuple[str, str]] = [
    ("requirements.txt", "requirements.lock"),
    ("requirements-dev.in", "requirements-dev.lock"),
]

#: 锁文件里一行形如 `fastapi==0.143.0 \` —— 注意 `pip-compile` **会把 extras
#: 括号一起写进锁文件**（`uvicorn[standard]==0.54.0`、`psycopg[binary]==3.3.6`），
#: 所以 `==` 前面可能挂着一个 `[...]`，不认它就等于看不见这两个包。
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]*)")

#: 声明行形如 `uvicorn[standard]>=0.32,<1.0`。
_REQ = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?P<extras>\[[^\]]*\])?"
    r"(?P<spec>.*)$"
)

#: 约束子句，长的在前 —— 否则 `>=` 会被 `>` 先吃掉。
_OPS = (">=", "<=", "==", ">", "<")


def _canon(name: str) -> str:
    """PEP 503 归一化：`Foo_Bar`、`foo.bar`、`foo-bar` 是同一个包。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared(path: Path) -> dict[str, str]:
    """读出「包名 → 版本约束」。注释、空行、`-r`/`--` 选项行一律跳过。"""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ.match(line)
        if m is None:
            raise ValueError(f"{path.name}: 认不出这一行 → {raw!r}")
        out[_canon(m.group("name"))] = m.group("spec").strip()
    return out


def _pinned(path: Path) -> dict[str, str]:
    """读出「包名 → 锁定的版本」。"""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = _PIN.match(raw)
        if m is not None:
            out[_canon(m.group(1))] = m.group(2)
    return out


def _vt(version: str) -> tuple[int, ...]:
    """把版本号拆成可比较的数字元组。

    ⚠️ 简化实现：只认数字段，不处理 `rc` / `post` 之类的标记。
    这几份声明与锁文件里都没有那样的版本，所以够用；真要泛化，
    换成 `packaging.version.Version`（那会引入一个依赖，为这点事不值）。
    """
    return tuple(int(x) for x in re.findall(r"\d+", version))


def _satisfies(version: str, spec: str) -> bool:
    """`version` 是否落在 `spec`（`>=1.0,<2.0` 这样的串）里。"""
    if not spec:
        return True
    have = _vt(version)
    for clause in spec.split(","):
        clause = clause.strip()
        if not clause:
            continue
        for op in _OPS:
            if not clause.startswith(op):
                continue
            want = _vt(clause[len(op) :])
            bad = {
                ">=": have < want,
                "<=": have > want,
                "==": have != want,
                ">": have <= want,
                "<": have >= want,
            }[op]
            if bad:
                return False
            break
        else:
            raise ValueError(f"认不出的约束子句 → {clause!r}")
    return True


def _fix_hint() -> str:
    """把重新生成的完整命令打出来 —— 让人不必回去翻文档。"""
    return (
        '  docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c \'\n'
        "    pip install --quiet pip-tools\n"
        "    pip-compile --generate-hashes --output-file=requirements.lock requirements.txt\n"
        "    pip-compile --generate-hashes --output-file=requirements-dev.lock"
        " requirements-dev.in'\n"
    )


def main() -> int:
    problems: list[str] = []

    for decl_name, lock_name in PAIRS:
        decl_path = API_ROOT / decl_name
        lock_path = API_ROOT / lock_name
        if not decl_path.exists() or not lock_path.exists():
            missing = decl_name if not decl_path.exists() else lock_name
            problems.append(f"{missing} 不存在")
            continue

        declared = _declared(decl_path)
        pinned = _pinned(lock_path)
        print(f"── {decl_name} → {lock_name}（声明 {len(declared)} 个，锁定 {len(pinned)} 个）")

        for name, spec in declared.items():
            if name not in pinned:
                problems.append(f"{lock_name} 里没有 {name}（声明里写了，锁文件里缺）")
            elif not _satisfies(pinned[name], spec):
                problems.append(
                    f"{lock_name} 把 {name} 锁在 {pinned[name]}，不满足声明的 {spec}"
                )

    # ── C. 两份锁文件必须是同一套运行时版本 ──────────────────────
    runtime_lock = API_ROOT / "requirements.lock"
    dev_lock = API_ROOT / "requirements-dev.lock"
    if runtime_lock.exists() and dev_lock.exists():
        runtime = _pinned(runtime_lock)
        dev = _pinned(dev_lock)
        print(f"── 两份锁文件的运行时部分（{len(runtime)} 个）")
        for name, version in runtime.items():
            if name not in dev:
                problems.append(f"requirements-dev.lock 少了运行时依赖 {name}")
            elif dev[name] != version:
                problems.append(
                    f"{name} 在两份锁文件里版本不同："
                    f"运行时 {version} / 开发 {dev[name]}"
                )

    if problems:
        print()
        print("❌ 锁文件与声明不同步：")
        for p in problems:
            print(f"  · {p}")
        print()
        print("修法（在 suvex-api/ 下跑，需要 docker）：")
        print(_fix_hint())
        return 1

    print()
    print("✅ 锁文件与声明同步")
    return 0


if __name__ == "__main__":
    sys.exit(main())
