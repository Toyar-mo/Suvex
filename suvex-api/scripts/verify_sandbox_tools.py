#!/usr/bin/env python3
"""检查「提示词里承诺的工具，在它被路由到的镜像里真的存在」。

## 这个脚本要堵的是什么

同一个失败模式，这个项目已经踩过**五次**：

  · `steghide` / `pngcheck` —— 一直写在 Dockerfile 第 32 行的**注释里**，
    被列为"选 Kali 源的理由"，从来没被真的装进来；
  · `hashcat` —— 装了，但没有 OpenCL 运行时，于是它罢工、agent 退回
    john 慢 100 倍，看起来像"模型不会爆破"；
  · `.NET` 工具 —— 两个镜像里一个都没有，一道 .NET 题烧掉 40 回合；
  · `msoffcrypto-tool` —— 口令已经爆出来了，模型却手写解密器烧光回合；
  · `patchelf` / `gdb` —— pwn 题走 base 镜像，而 base 里两个都没有。

这五个不是五个独立的 bug，是**同一个结构性缺口**：

    **没有任何机制校验「提示词说可以用的工具」和「镜像里真的有的工具」
      是不是同一个集合。**

症状因此总是"运行时模型默默卡住、日志看不出原因" —— 它不会报
"缺 gdb"，它只是每次调试都失败，然后换个方法再试一次。

## 这个脚本做什么（纯静态，不需要 Docker）

  1. 解析两个 Dockerfile 的 `apt-get install` / `pip install` 列表，
     得到「每个镜像实际会装哪些包」；
  2. 把包名翻译成它提供的**命令名**（`netcat-traditional` → `nc`）；
  3. 按 `runner.profile_for_category()` 把每个分类映射到镜像 ——
     **复用真实的路由函数**，不另抄一份映射；
  4. 断言「该分类需要的命令」⊆「该镜像提供的命令」。

## 为什么是"每个分类需要什么"而不是"提示词里提到了什么"

从 markdown 里抠命令名不可靠：反引号里既有命令（`cyclic`）也有
参数（`-u`）、路径（`/work`）、概念（`Canary found`），而漏掉一个
并不会让脚本失败 —— 那这个检查就变成了"看起来在守、其实守不住"。

所以这里用的是**能力契约**：`CATEGORY_REQUIREMENTS` 显式声明每类题
至少要有什么命令。它是**从提示词推导出来的**，改提示词时如果引入了
新工具，也要同步加到这里 —— 而"加到这里"这个动作本身就会逼你想
"镜像里到底有没有它"。

## re 镜像的工具集是 base ∪ 自己

`docker/ctf2-sandbox-re/Dockerfile` 是 `FROM ctf2-sandbox:latest`，
所以它继承 base 的全部包。只解析它自己那几段会得出"re 镜像缺 curl"
这种假报错。

## 用法

    python3 scripts/verify_sandbox_tools.py            # 检查，失败退出 1
    python3 scripts/verify_sandbox_tools.py --verbose  # 顺手列出每个镜像有什么

## 局限（写在明处）

  1. **纯静态**：它证明的是"apt/pip 命令里写了这个包"，不是"包真的装
     成功了、命令真的在 PATH 里"。后者要在镜像构建时 `command -v` 才算
     数 —— 那一步需要 Docker，放不进这个仓库的 CI（见文件末尾的说明）。
  2. **包名 → 命令名**靠 `PACKAGE_COMMANDS` 这张显式表。表里没有的包
     默认「包名 == 命令名」，这个默认对 apt 的大多数工具成立，但
     `netcat-traditional` 这种不成立 —— 所以**新增包时若它提供的命令
     与包名不同，必须登记**，否则这里会误报"缺命令"（那正是想要的：
     它逼你把这个事实写下来）。
  3. 它不知道 `/opt/venv/bin` 与 `/usr/bin` 的差别 —— 对"命令存在与否"
     这个问题，两者等价。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ══════════════════════════════════════════════════════════
# 路径
# ══════════════════════════════════════════════════════════

#: 脚本在 `suvex-api/scripts/` 下，仓库根是它的上两级。
API_ROOT = Path(__file__).resolve().parent.parent

# ⚠️ Dockerfile 的路径不在这里硬编码 —— 由 `load_router()` 从
#    `Ctf2SandboxProfile` 的取值派生（profile 取值 == 镜像短名 ==
#    `docker/` 下的目录名）。硬编码两份路径就等于又造了一份"可能与
#    枚举脱节的副本"。


# ══════════════════════════════════════════════════════════
# 包名 → 它提供的命令
# ══════════════════════════════════════════════════════════
#
# 只登记「包名 ≠ 命令名」的。没登记的包按「包名就是命令名」处理
# （`nmap` / `curl` / `sqlmap` 这些都成立）。
#
# ⚠️ 值是**元组**而不是字符串，因为一个包经常提供好几个命令
#    （`p7zip-full` 同时给 `7z` / `7za` / `7zr`；`sleuthkit` 给
#    `fls` / `icat` / `mmls`）。空元组 = 这个包不提供任何命令
#    （纯开发库/运行库/证书），登记它只是为了让"我知道它没有命令"
#    这件事显式化，而不是被当成"忘了翻译"。
PACKAGE_COMMANDS: dict[str, tuple[str, ...]] = {
    # ── apt：包名 ≠ 命令名 ──
    "netcat-traditional": ("nc",),
    "p7zip-full": ("7z", "7za", "7zr"),
    "imagemagick": ("convert", "identify", "mogrify", "montage"),
    "sleuthkit": ("fls", "icat", "mmls", "blkls", "fsstat"),
    "dnsutils": ("dig", "nslookup", "host"),
    "iputils-ping": ("ping",),
    "vim-tiny": ("vim", "vi"),
    "build-essential": ("gcc", "g++", "make"),
    "binutils": ("objdump", "nm", "readelf", "objcopy", "strings", "addr2line"),
    "binutils-multiarch": ("objdump", "nm", "readelf"),
    "coreutils": ("timeout", "ls", "cat", "head", "tail", "wc", "sort", "base64"),
    "radare2": ("r2", "radare2", "rasm2", "rahash2"),
    "mono-runtime": ("mono",),
    "mono-devel": ("monodis", "mcs", "mono-csc", "csc"),
    "upx-ucl": ("upx",),
    "ninja-build": ("ninja",),
    "qemu-user": ("qemu-arm", "qemu-x86_64", "qemu-mips"),
    "qemu-user-static": ("qemu-arm-static", "qemu-x86_64-static"),
    "xvfb": ("xvfb-run", "Xvfb"),
    "gdb-multiarch": ("gdb-multiarch",),
    # 纯开发库 / 运行库 / 证书：不提供命令。
    "ca-certificates": (),
    "libssl-dev": (),
    "libffi-dev": (),
    "pkg-config": (),
    "python3-dev": (),
    "python3-venv": (),
    "libzbar0": (),
    # ── pip：包名 ≠ 命令名 ──
    # pwntools 装完之后 `/opt/venv/bin` 下会多出这几个 —— 它们是
    # pwn/逆向题的主力，而"pwntools 装了"不等于"checksec 能跑"
    # （虽然实际上是这样，但把这个事实写下来比假设可靠）。
    "pwntools": ("checksec", "ROPgadget", "cyclic", "pwn", "asm", "disasm"),
    "msoffcrypto-tool": ("msoffcrypto-tool",),
    "oletools": ("olevba", "oledump.py", "rtfobj", "oleobj"),
    # 纯 Python 库，不装命令。
    "pycryptodome": (),
    "cryptography": (),
    "sympy": (),
    "z3-solver": (),
    "gmpy2": (),
    "numpy": (),
    "scipy": (),
    "pillow": (),
    "pyzbar": (),
    "python-magic": (),
    "requests": (),
    "httpx": (),
    "beautifulsoup4": (),
    "lxml": (),
    "capstone": (),
    "angr": (),
    "olefile": (),
    "pyzipper": (),
    # pip 自身与其构建链，出现在 `--upgrade` 那一行
    "pip": (),
    "setuptools": (),
    "wheel": (),
}


# ══════════════════════════════════════════════════════════
# 能力契约：每个分类至少要有什么命令
# ══════════════════════════════════════════════════════════
#
# ⚠️ 这张表是**从提示词推导出来的**，不是凭直觉列的。
#    提示词第四节说"pwn 题用 checksec / cyclic / ROP / gdb / patchelf"，
#    那 pwn 分类就必须至少能跑这些 —— 否则提示词在教模型用不存在的东西。
#
# 改提示词里某个方向的工作流时，记得回来同步这里。
CATEGORY_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "web": (
        "curl",
        "nmap",
        "ffuf",
        "gobuster",
        "dirb",
        "sqlmap",
        "nikto",
        "whatweb",
        "python3",
    ),
    # pwn 走 base 镜像（`_REVERSE_CATEGORIES` 里没有它，是刻意的）。
    # 这些命令提示词第四节与第六节都点名要用。
    "pwn": (
        "checksec",  # pwntools
        "ROPgadget",  # pwntools
        "cyclic",  # pwntools
        "patchelf",  # apt（2026-10-12 补）
        "gdb",  # apt（2026-10-12 补）
        "nc",
        "socat",
        "python3",
    ),
    "crypto": (
        "hashcat",
        "john",
        "msoffcrypto-tool",  # pip
        "7z",
        "python3",
    ),
    # reverse 走 re 镜像（base 的超集）
    "reverse": (
        "gdb",
        "r2",
        "objdump",
        "ltrace",
        "strace",
        "mono",
        "monodis",
        "checksec",
        "xvfb-run",
        "python3",
    ),
    "misc": (
        "binwalk",
        "exiftool",
        "foremost",
        "steghide",
        "pngcheck",
        "7z",
        "unzip",
        "tshark",
        "file",
        "strings",
    ),
    "forensics": (
        "binwalk",
        "exiftool",
        "tshark",
        "foremost",
        "fls",
        "icat",
        "file",
        "strings",
    ),
}


# ══════════════════════════════════════════════════════════
# Dockerfile 解析
# ══════════════════════════════════════════════════════════

#: `apt-get install` 之后、到下一个 shell 分隔符之前的那段是包名。
#: 用 `[^&|;]+` 而不是 `[\s\S]*?` 是因为 apt 块后面紧跟着
#: `&& rm -rf /var/lib/apt/lists/*` —— 到 `&` 就该停。
_APT_INSTALL_RE = re.compile(r"apt-get\s+install\b([^&|;]*)")
_PIP_INSTALL_RE = re.compile(r"\bpip3?\s+install\b([^&|;]*)")

#: `install` 后面可能跟的选项，都不是包名。
_OPTION_PREFIX = "-"


def _logical_lines(text: str) -> list[str]:
    """把 Dockerfile 拆成"逻辑行"：丢掉注释行，把 `\\` 续行拼起来。

    ## 为什么不能直接 `splitlines()` 再逐行正则

    这些 RUN 指令都写成这样（注释夹在续行中间是**合法**的，
    Docker 预处理时会丢掉注释行然后照常拼续行）：

        RUN apt-get install -y --no-install-recommends \\
            # ── 网络 / web 题 ──
            nmap whatweb ffuf \\
            patchelf gdb \\
            && rm -rf /var/lib/apt/lists/*

    逐行看的话，包名散在三行里，而每一行单独都不构成一条完整命令。
    """
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        # 注释行：Docker 会在预处理阶段丢掉，这里照样丢。
        # ⚠️ 必须在"判断续行"**之前**判断注释 —— 否则一条以 `\`
        #    结尾的注释会把下一行也并进来。
        if stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buf += stripped[:-1].rstrip() + " "
        else:
            out.append(buf + stripped)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _packages_from(match_text: str) -> list[str]:
    """把 `install` 后面那串参数滤成包名列表。"""
    packages: list[str] = []
    for token in match_text.split():
        token = token.strip()
        if not token or token.startswith(_OPTION_PREFIX):
            continue
        # 去掉可能残留的引号
        token = token.strip("'\"")
        if not token:
            continue
        packages.append(token)
    return packages


@dataclass
class ImagePackages:
    """一个镜像装了哪些包。"""

    name: str
    apt: list[str]
    pip: list[str]

    def commands(self) -> set[str]:
        """翻译成命令集合。"""
        cmds: set[str] = set()
        for pkg in self.apt + self.pip:
            # 包名可能带版本约束或 extras：`foo>=1.0` / `foo[bar]`
            base = re.split(r"[<>=!\[;]", pkg, maxsplit=1)[0].strip()
            if not base:
                continue
            if base in PACKAGE_COMMANDS:
                cmds.update(PACKAGE_COMMANDS[base])
            elif pkg in PACKAGE_COMMANDS:
                cmds.update(PACKAGE_COMMANDS[pkg])
            else:
                # 默认：包名 == 命令名
                cmds.add(base)
        return cmds


#: `FROM <image>`（只取第一个 —— 多阶段构建里第一个才是最终镜像的底座）。
_FROM_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)", re.IGNORECASE | re.MULTILINE)

#: `ARG BASE_IMAGE=ctf2-sandbox:latest` —— `FROM ${BASE_IMAGE}` 的默认值。
_ARG_BASE_RE = re.compile(r"^\s*ARG\s+BASE_IMAGE=(?P<default>\S+)", re.IGNORECASE | re.MULTILINE)


def inherits_from(path: Path, parent: str) -> bool:
    """该 Dockerfile 是否 `FROM <parent>`（含 `ARG BASE_IMAGE=...` 间接写法）。

    ## 为什么要真的查一遍，而不是直接假设"反过来肯定继承 base"

    下面按"reverse 镜像 = base 的全部工具 + 自己那几段"来算，这个假设
    成立的前提是 `docker/ctf2-sandbox-re/Dockerfile` 真的
    `FROM ctf2-sandbox:latest`。现在它是 —— 写成
    `ARG BASE_IMAGE=ctf2-sandbox:latest` + `FROM ${BASE_IMAGE}`。

    但如果哪天有人把 reverse 改成独立的基础镜像（比如真的需要不同发行版），
    这个假设就破了，而破的方式是**脚本继续按"继承"算、于是漏报
    "reverse 缺 curl"**。静默漏报比误报危险，所以这里显式查一次；
    查不到就当"不继承"，让上层如实报缺失。
    """
    text = path.read_text(encoding="utf-8")
    match = _FROM_RE.search(text)
    if match is None:
        return False
    image = match.group("image").strip()
    if "$" in image:
        # `FROM ${BASE_IMAGE}` —— 去 ARG 的默认值里找
        arg = _ARG_BASE_RE.search(text)
        if arg is None:
            # ARG 默认值查不到 = 构建时由外面传入 = 父镜像未知
            return False
        image = arg.group("default").strip().strip("'\"")
    # 比较短名：`ctf2-sandbox` 是 `ctf2-sandbox:latest` 的子串
    return parent in image


def parse_image(path: Path, name: str) -> ImagePackages:
    """解析一个 Dockerfile 的 apt / pip 安装列表。"""
    if not path.is_file():
        raise SystemExit(f"❌ 找不到 Dockerfile：{path}")
    text = path.read_text(encoding="utf-8")

    apt: list[str] = []
    pip: list[str] = []
    for line in _logical_lines(text):
        for match in _APT_INSTALL_RE.finditer(line):
            apt.extend(_packages_from(match.group(1)))
        for match in _PIP_INSTALL_RE.finditer(line):
            pip.extend(_packages_from(match.group(1)))

    # 去重但保序（报错信息里的顺序稳定，diff 才好看）
    return ImagePackages(name=name, apt=list(dict.fromkeys(apt)), pip=list(dict.fromkeys(pip)))


# ══════════════════════════════════════════════════════════
# 分类 → 镜像：数据取真实源码，逻辑不抄
# ══════════════════════════════════════════════════════════

RUNNER_PY = API_ROOT / "app" / "ctf2" / "runner.py"


def _literal_set(node: ast.expr) -> frozenset[str] | None:
    """把 `{...}` / `frozenset({...})` / `[...]` 这类字面量集合取出来。

    `_REVERSE_CATEGORIES` 现在写成 `frozenset({...})`（一个调用而不是
    字面量），所以不能只认 `ast.Set`，得往里剥一层。
    """
    # `frozenset({...})` / `set([...])`：剥掉调用，看第一个实参
    if isinstance(node, ast.Call):
        if node.args:
            return _literal_set(node.args[0])
        return None
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        try:
            values = ast.literal_eval(node)
        except (ValueError, SyntaxError):
            return None
        return frozenset(str(v) for v in values)
    return None


def load_reverse_categories(path: Path = RUNNER_PY) -> frozenset[str]:
    """从 `runner.py` 源码里取出 `_REVERSE_CATEGORIES` 这个集合。

    ## 为什么不 `from app.ctf2.runner import ...`

    `app/ctf2/runner.py` 顶层就 `from sqlalchemy import select`，往下还有
    SQLAlchemy / pydantic 一大串。CI 里装着这些，但**本地改完 Dockerfile
    想立刻验证时未必装着** —— 而"本地跑不起来"的实际后果是开发者跳过
    验证直接提交，这个脚本就等于白写。

    ## 为什么也不在这里抄一份分类表

    抄一份的话，`runner.py` 改了路由这边不会跟着改 —— 那这个检查守的
    就是"一份可能过时的副本"，而不是真实行为。而这个脚本存在的全部
    意义就是"别让文档和实现对不上"，抄副本正好把病治成了症。

    ## 所以：用 ast 读源码里的那个字面量集合

    它是**数据**（哪几个分类算逆向），不是**逻辑**（怎么用这个集合）。
    数据从源码提取，就不存在漂移。
    """
    if not path.is_file():
        raise SystemExit(f"❌ 找不到 {path} —— 无法确定分类路由")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_REVERSE_CATEGORIES" not in targets:
            continue
        values = _literal_set(node.value)
        if values is None:
            raise SystemExit(
                f"❌ `_REVERSE_CATEGORIES`（{path}:{node.lineno}）不再是字面量集合，"
                f"本脚本的 ast 提取需要更新。"
            )
        return values
    raise SystemExit(f"❌ 在 {path} 里没找到 `_REVERSE_CATEGORIES` 赋值。")


@dataclass
class Router:
    """本脚本用到的路由信息：分类 → profile → Dockerfile。"""

    reverse_categories: frozenset[str]
    base_profile: str
    reverse_profile: str
    profiles: dict[str, Path]

    def profile_for(self, category: str) -> str:
        """分类 → profile 取值。

        ## ★ 这 3 行是从 `runner.py:437 profile_for_category` 抄的

        抄的是**逻辑**，不抄**数据** —— 数据（`_REVERSE_CATEGORIES`、
        `Ctf2SandboxProfile` 的两个取值）每次运行都从源码现读。抄过来的
        只有"规范化 → 查集合 → 二选一"这个形状，短到没有漂移空间。

        `runner.py` 里那条"必须精确匹配、不能子串匹配"的警告在这份复刻里
        同样成立：`"re"` 是 `_REVERSE_CATEGORIES` 的成员，但题目分类里有
        `"reverse"` 也有 `"web"`，用子串匹配会把 `"interface"` 判成逆向。

        如果哪天 `profile_for_category` 的逻辑真的变复杂了（比如按关键词
        模糊匹配），这里要跟着改 —— 而它会**立刻显形**：路由算错会导致
        下面冒出一堆莫名其妙的"缺工具"。
        """
        normalized = (category or "").strip().lower()
        if normalized in self.reverse_categories:
            return self.reverse_profile
        return self.base_profile


def load_router() -> Router:
    """把「逆向分类集合 + 两个 profile 取值 + 各自的 Dockerfile」一次凑齐。

    ## 关键约束：**不 import `app.ctf2.runner`**

    它顶层 `from sqlalchemy import select`，往下还有 SQLAlchemy / pydantic
    一大串。CI 里装着，但**本地改完 Dockerfile 想立刻验证时未必装着** ——
    而"本地跑不起来"的实际后果是开发者跳过验证直接提交，这个脚本就等于
    白写。实测本机 `python -c "import sqlalchemy"` 就是 ModuleNotFoundError。

    真正要避开的另一半是"抄一份可能过时的副本"：抄了的话 `runner.py` 改
    路由这边不会跟着改，这个检查守的就成了副本而不是真实行为。所以数据
    （`_REVERSE_CATEGORIES`、`Ctf2SandboxProfile` 两个取值）全部从源码现读，
    只有 3 行控制流是复刻的。
    """
    if str(API_ROOT) not in sys.path:
        sys.path.insert(0, str(API_ROOT))
    try:
        from app.core import vocab  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001  —— 这里要的是"哪种错都别静默"
        raise SystemExit(
            f"❌ 导不进 `app.core.vocab`（{exc}）。这个模块只有标准库的 "
            f"StrEnum，导不进来说明环境本身有问题，不降级。"
        ) from exc

    base_profile = vocab.Ctf2SandboxProfile.BASE.value
    reverse_profile = vocab.Ctf2SandboxProfile.REVERSE.value

    profiles: dict[str, Path] = {}
    for name in (base_profile, reverse_profile):
        # profile 取值 == 镜像短名 == docker/ 下的目录名。
        # vocab 的注释（app/core/vocab.py:796）明确说这是刻意的：
        # 不做"逻辑名 → 镜像名"的第二层映射，所以这里可以对上。
        path = API_ROOT / "docker" / name / "Dockerfile"
        if not path.is_file():
            raise SystemExit(
                f"❌ profile {name!r} 对应的 Dockerfile 不存在：{path}\n"
                f"   （约定：profile 取值 == 镜像短名 == docker/ 下的目录名）"
            )
        profiles[name] = path

    return Router(
        reverse_categories=load_reverse_categories(),
        base_profile=base_profile,
        reverse_profile=reverse_profile,
        profiles=profiles,
    )


# ══════════════════════════════════════════════════════════
# 检查
# ══════════════════════════════════════════════════════════


@dataclass
class Finding:
    category: str
    image: str
    missing: list[str]


def check(verbose: bool = False) -> list[Finding]:
    """主体检查。返回所有"缺工具"的发现。"""

    router = load_router()
    base_path = router.profiles[router.base_profile]
    reverse_path = router.profiles[router.reverse_profile]

    base = parse_image(base_path, router.base_profile)
    reverse_own = parse_image(reverse_path, router.reverse_profile)

    # ★ reverse 镜像通常 `FROM <base>`，于是**继承** base 的全部包。
    #   只算它自己那几段会得出"reverse 缺 curl / python3"这种假报错 ——
    #   而假报错比没有检查更糟：它会让真报错被淹没，然后大家开始无视这个
    #   脚本的输出。
    #
    #   但"通常"不能当前提，所以真去 FROM 行查一遍（见 inherits_from）。
    inherits = inherits_from(reverse_path, router.base_profile)
    if inherits:
        reverse = ImagePackages(
            name=reverse_own.name,
            apt=base.apt + [p for p in reverse_own.apt if p not in base.apt],
            pip=base.pip + [p for p in reverse_own.pip if p not in base.pip],
        )
    else:
        # 不再是继承关系 —— 那 reverse 自己的列表就是全集，缺什么会被
        # 如实报出来，而不是被 base 的包悄悄填上。
        reverse = reverse_own

    images = {
        router.base_profile: base,
        router.reverse_profile: reverse,
    }

    if verbose:
        print(f"\n   逆向分类（{len(router.reverse_categories)} 个）：")
        print("     " + ", ".join(sorted(router.reverse_categories)))
        print(f"   reverse 镜像继承 base：{'是' if inherits else '否（独立基础镜像）'}")
        for name, image in images.items():
            cmds = sorted(image.commands())
            print(f"\n── {name} ──")
            print(f"   apt 包 {len(image.apt)} 个 / pip 包 {len(image.pip)} 个")
            print(f"   命令 {len(cmds)} 个：")
            for i in range(0, len(cmds), 6):
                print("     " + "  ".join(f"{c:<18}" for c in cmds[i : i + 6]))

    findings: list[Finding] = []
    print("\n── 能力契约 ──\n")
    header = f"{'分类':<12} {'镜像':<20} {'需要':<6} {'结果':<8} 缺什么"
    print(header)
    print("-" * len(header) * 2)
    for category, required in sorted(CATEGORY_REQUIREMENTS.items()):
        profile = router.profile_for(category)
        if profile not in images:
            raise SystemExit(
                f"❌ 分类 {category!r} 路由到 profile {profile!r}，"
                f"但本脚本只解析了 {sorted(images)} —— "
                f"新增镜像时要同步更新 load_router 里的映射。"
            )
        available = images[profile].commands()
        missing = [c for c in required if c not in available]
        status = "✅" if not missing else "❌"
        print(f"{category:<12} {profile:<20} {len(required):<6} {status:<8} {', '.join(missing) or '—'}")
        if missing:
            findings.append(Finding(category=category, image=profile, missing=missing))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="列出每个镜像实际提供的命令（排查误报时用）",
    )
    args = parser.parse_args()

    findings = check(verbose=args.verbose)

    print()
    if findings:
        print(f"❌ {len(findings)} 个分类缺工具：\n")
        for f in findings:
            print(f"  · {f.category}（{f.image}）缺：{', '.join(f.missing)}")
        print(
            "\n   两种修法（按情况选）：\n"
            "     1. 镜像里该有的 → 加到对应 Dockerfile 的 apt/pip 列表；\n"
            "     2. 提示词里写错了/不该承诺的 → 改 prompts/ctf2_system.md，\n"
            "        并同步 CATEGORY_REQUIREMENTS。\n"
        )
        return 1

    print(
        "✅ 每个分类需要的工具，在它被路由到的镜像里都有声明"
        f"（{len(CATEGORY_REQUIREMENTS)} 个分类）。\n"
    )
    print(
        "⚠️ 这个脚本证明的是「Dockerfile 的 apt/pip 命令里写了这个包」，\n"
        "   **不**证明「包真的装成功了、命令真的在 PATH 里」。后者要在\n"
        "   镜像构建时 `command -v` 才算数 —— 那一步需要 Docker，不在\n"
        "   CI 里跑（镜像构建本来就会暴露安装失败）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
