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

## 这个脚本做什么（默认纯静态，不需要 Docker）

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

`docker/suvex-sandbox-re/Dockerfile` 是 `FROM suvex-sandbox:latest`，
所以它继承 base 的全部包。只解析它自己那几段会得出"re 镜像缺 curl"
这种假报错。

## 用法

    python3 scripts/verify_sandbox_tools.py            # 检查，失败退出 1
    python3 scripts/verify_sandbox_tools.py --verbose  # 顺手列出每个镜像有什么
    python3 scripts/verify_sandbox_tools.py --probe    # ★ 再对真实镜像跑 command -v

## 局限（写在明处）

  1. **默认纯静态**：它证明的是"apt/pip 命令里写了这个包"，不是"包真的
     装成功了、命令真的在 PATH 里"。
     `--probe` 补上这一维（需要 Docker），并且能抓到静态解析**天生
     看不见**的第三种情况：**Dockerfile 改了、镜像没重建**。
     那一次实测就是这样的 —— `patchelf gdb` 写在 `2de97ce`（2026-10-18），
     而镜像是 2026-10-12 的，两个命令都不在里面，而静态检查报「pwn ✅」。
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
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

# ══════════════════════════════════════════════════════════
# 路径
# ══════════════════════════════════════════════════════════

#: 脚本在 `suvex-api/scripts/` 下，仓库根是它的上两级。
API_ROOT = Path(__file__).resolve().parent.parent

# ⚠️ Dockerfile 的路径不在这里硬编码 —— 由 `load_router()` 从
#    `KernelSandboxProfile` 的取值派生（profile 取值 == 镜像短名 ==
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
    "sleuthkit": ("fls", "icat", "mmls", "blkls", "fsstat", "mactime"),
    "libewf-utils": ("ewfinfo", "ewfexport", "ewfverify", "ewfacquire"),
    # ★ v0.41：Debian/Kali 的**真实包名**是 `ewf-tools`，不是 `libewf-utils`。
    #
    # 这一条是拿一次真实的构建失败换来的（2026-10-20，Kali rolling）：
    #   E: Unable to locate package libewf-utils
    # `libewf-utils` 这个包名在 Debian/Ubuntu 系根本不存在 —— 存在的三个是
    # `libewf2`（运行库，没有 CLI）、`libewf-dev`（头文件）、`ewf-tools`
    # （真正提供 ewfinfo/ewfacquire 的那个，`apt-file search bin/ewfinfo`
    # 直接指到它）。
    #
    # ⚠️ 保留上面那行 `libewf-utils` 是有意的：上游 `suvex-sandbox` 的
    #    Dockerfile 里写的就是它（那份是从别的发行版基础上抄来的），
    #    静态解析必须认得那个名字，否则会对 base 报"未知包"。两行并存
    #    等于把「历史写法」与「这台机器上真能装的那个」都记下来。
    "ewf-tools": ("ewfinfo", "ewfexport", "ewfverify", "ewfacquire"),
    "dnsutils": ("dig", "nslookup", "host"),
    "iputils-ping": ("ping",),
    # ★ 2026-10-20 实测修正：原来写的是 `("vim", "vi")`，其中 `vim` 是错的。
    #
    #   `vim-tiny` 只装 `/usr/bin/vim.tiny`，再加一条 `/usr/bin/vi` ——
    #   后者是 `/etc/alternatives/vi` 的软链（`dpkg -L vim-tiny` 实测）。
    #   `/usr/bin/vim` 在这三个镜像里**都不存在**（三镜像 `command -v vim`
    #   全 NO，`command -v vi` / `vim.tiny` 全 OK）。
    #   把 `vim` 留在这张表里，等于宣称"装了 vim-tiny 就有 vim 命令"，
    #   而这个宣称对解题是误导：提示词让模型用 `vim` 时会 command not found。
    "vim-tiny": ("vi", "vim.tiny"),
    "build-essential": ("gcc", "g++", "make"),
    "binutils": ("objdump", "nm", "readelf", "objcopy", "strings", "addr2line"),
    "binutils-multiarch": ("objdump", "nm", "readelf"),
    # ★ v0.41：`grep` / `sed` / `awk` 是**三个独立的包**，不是 coreutils 的一部分。
    #
    # 这条以前没人写下来，因为 base 里它们"反正都在"——但那是 **gcc /
    # build-essential 的依赖链**带来的，不是谁声明过的承诺。IR 提示词
    # 恰恰把它们当成主要工具（§三 决策表整段都在讲 grep/awk/sed），
    # 于是"提示词要求用、Dockerfile 里查不到"这个缺口第一次显了形。
    #
    # 修法沿用 `binutils` 那条先例：**写进 Dockerfile**（apt 见到已装会
    # 跳过，成本 0），让这条依赖从此是承诺而不是运气。
    "grep": ("grep", "egrep", "fgrep"),
    "sed": ("sed",),
    # Debian 的 `awk` 由 `mawk` 提供（`gawk` 是另一个可选实现）。
    # 两者装哪个都能跑 `awk`，这里登记 mawk 是因为 base 实际装的是它 ——
    # 登记错包名会让检查永远报"awk 未声明"，而那是假报错。
    "mawk": ("awk",),
    "coreutils": (
        "timeout",
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "sort",
        "base64",
        "uniq",
        "cut",
        "tr",
        "date",
        "mkdir",
        "chmod",
        "mv",
        "cp",
        "rm",
        "tee",
        "du",
        "stat",
    ),
    "radare2": ("r2", "radare2", "rasm2", "rahash2"),
    "mono-runtime": ("mono",),
    # ★ 2026-10-20 实测修正：原来还登记了 `csc`，删掉了。
    #   Kali rolling 的 `mono-devel` 只装 `/usr/bin/mcs`、`/usr/bin/mono-csc`、
    #   `/usr/bin/monodis`（`dpkg -L mono-devel | grep bin/` 实测），
    #   没有叫 `csc` 的入口 —— 上游 Mono 的 `csc` 是 .NET SDK 风格的名字。
    #   实测：re 镜像 `command -v mcs` / `mono-csc` / `monodis` 全 OK，
    #   `command -v csc` 是 NO。
    "mono-devel": ("monodis", "mcs", "mono-csc"),
    "upx-ucl": ("upx",),
    "ninja-build": ("ninja",),
    "qemu-user": ("qemu-arm", "qemu-x86_64", "qemu-mips"),
    # ★ 2026-10-20 实测修正：原来登记 `("qemu-arm-static", "qemu-x86_64-static")`，
    #   而这两个命令在**任何镜像里都不存在**，原因不是"镜像没跟上"，是这条
    #   登记本身反映了对 Kali 的误解。实测证据（缺一不可，逐条都要能复现）：
    #
    #     · `apt-cache showpkg qemu-user-static` → `Versions:` 空，
    #       `Reverse Provides: qemu-user-binfmt 1:11.1.0+ds-2 (= )`
    #       —— 它在 Kali rolling 里是**虚拟包**；
    #     · `apt-get install --dry-run qemu-user-static` → `0 newly installed`
    #       且 exit 0 —— 所以 Dockerfile 声明它**不会**让构建失败；
    #     · `dpkg-query -W -f='${Status}|${Provides}' qemu-user-binfmt`
    #       → `install ok installed|qemu-user-static|` —— 已被满足；
    #     · `find /usr /opt -name 'qemu*static*'` → **无结果**。
    #
    #   新版 qemu 走 binfmt 注册（`/usr/lib/binfmt.d/qemu-*.conf`），不再
    #   往 PATH 里放 `*-static` 后缀的二进制；真正能被 `command -v` 找到的
    #   是 `qemu-user` 提供的 `qemu-arm` / `qemu-x86_64` / …（已在上一行）。
    #   所以这里登记空元组：**"我查过了，这个包不提供自己的命令"** ——
    #   与 `ca-certificates` 那批同类，而不是"忘了翻译"。
    "qemu-user-static": (),
    "xvfb": ("xvfb-run", "Xvfb"),
    "gdb-multiarch": ("gdb-multiarch",),
    # ★ 2026-10-20 实测新增：`x11-utils` 只出现在 re 镜像的 Dockerfile 里，
    #   而它是个"包名 ≠ 命令名"的典型 —— 装的是 X 客户端工具集。
    #   实测（re 镜像全部 OK，base/ir 不声明这个包）：
    #     xdpyinfo xprop xwininfo xev xdriinfo
    "x11-utils": ("xdpyinfo", "xprop", "xwininfo", "xev", "xdriinfo"),
    # ★ 2026-10-20 实测修正：`python3-pip` 不装叫 `python3-pip` 的命令。
    #   `dpkg -L python3-pip` 给的是 `/usr/bin/pip` 与 `/usr/bin/pip3`；
    #   三个镜像 `command -v pip` / `pip3` 全 OK。
    #   ⚠️ 注意在**产品镜像内**这两个名字解析到 `/opt/venv/bin/pip*`
    #   （venv 在 PATH 前面），但那仍是"pip 命令存在"这件事的成立，
    #   与这里要表达的承诺一致。
    "python3-pip": ("pip", "pip3"),
    # 纯开发库 / 运行库 / 证书：不提供命令。
    "ca-certificates": (),
    "libssl-dev": (),
    "libffi-dev": (),
    "pkg-config": (),
    "python3-dev": (),
    "python3-venv": (),
    "libzbar0": (),
    # ★ 2026-10-20 实测新增：以下四个是"装了、但**纯运行库**"的包。
    #   它们原先没登记，于是走默认"包名 == 命令名"，被当成声明了一个
    #   同名命令 —— 全量实测时必然报"缺失"，而那是假缺口。
    #   实测 `dpkg-query -W -f='${Status}'` 三个镜像都是
    #   `install ok installed`（ocl/pocl/ruby-dev 在 base，re/ir 继承）。
    "ocl-icd-libopencl1": (),
    "pocl-opencl-icd": (),
    "ruby-dev": (),
    # ── pip：包名 ≠ 命令名 ──
    # pwntools 装完之后 `/opt/venv/bin` 下会多出这几个 —— 它们是
    # pwn/逆向题的主力，而"pwntools 装了"不等于"checksec 能跑"
    # （虽然实际上是这样，但把这个事实写下来比假设可靠）。
    "pwntools": ("checksec", "ROPgadget", "cyclic", "pwn", "asm", "disasm"),
    "msoffcrypto-tool": ("msoffcrypto-tool",),
    # ★ 2026-10-20 实测修正：原来还登记了 `oledump.py`，删掉了。
    #
    #   oletools 0.60.2 装完之后 `/opt/venv/bin` 里是：
    #     olebrowse oledir olefile oleid olemap olemeta oleobj oletimes
    #     olevba rtfobj
    #   没有 `oledump.py` —— 上游早已把那个脚本从 entry_points 里移除
    #   （`oledump.py` 现在是独立仓库）。三个镜像实测 `command -v oledump.py`
    #   全 NO，而 `olevba` / `rtfobj` / `oleobj` 全 OK。
    #   这正是"事故史里那条 steghide/pngcheck 只写在注释里"的同类：
    #   名字看着眼熟、实际不存在。
    "oletools": ("olevba", "rtfobj", "oleobj"),
    # ★ 2026-10-20 实测新增：以下是"装了、但纯 Python 库、不装命令"的包。
    #   原先没登记 → 走默认「包名 == 命令名」→ 全量实测必然报缺失（假缺口）。
    #   实测证据（镜像内 `/opt/venv/bin/pip list` 全部在册）：
    #     re 镜像：keystone-engine 0.9.2 / lief 1.0.0 / pyelftools 0.33 /
    #              unicorn 2.1.2
    #     ir 镜像：pandas 3.0.6 / openpyxl 3.1.5 / PyYAML 6.0.3 /
    #              python-dateutil 2.9.0.post0
    #   ⚠️ `lief` / `pyelftools` 确实在 venv/bin 里留了 `elfdiff` / `elfpatch` /
    #   `readelf.py`，但那是**附带脚本**、名字与包名无关；登记它们只会制造
    #   一条没人承诺过的断言。这里要表达的是"这些包装了、但不提供同名命令"。
    "keystone-engine": (),
    "lief": (),
    "pyelftools": (),
    "unicorn": (),
    "pandas": (),
    "openpyxl": (),
    "pyyaml": (),
    "python-dateutil": (),
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
    # ★ v0.41：应急响应 / 根因复盘，走 `suvex-sandbox-ir` 镜像。
    #
    # ## 这张表是从 `prompts/kernel_ir.md` 推导出来的
    #
    # 提示词 §三 决策表第一条要求"先 `ls -la` + 看文件大小 + 抽头尾各 20 行"
    # → `ls` / `wc` / `head` / `tail`；
    # 第二条要求"各抽 1 条样本对齐时间格式与时区" → `date` / `sort` / `uniq`；
    # "跨源关联"要按实体过滤 → `grep` / `awk` / `sed`；
    # "时间线重建"要按时刻排序 → `sort`（`mactime` 是 sleuthkit 的专用版本）；
    # §五 脱敏要求"只记形状" → `cut` / `tr`；
    # §六 处置建议要求"**不要**输出可直接粘贴执行的命令" → **不需要任何攻击工具**。
    #
    # ## ★ 为什么这里故意不含 curl / nmap / sqlmap / hashcat / 爆破类
    #
    # 这不是漏项，是设计。IR 镜像是**只读复盘**环境：要能读日志，不要能打。
    # 放攻击工具进来会做两件坏事：
    #   ① 让"复盘任务顺手打一下目标"在技术上变得可行 —— 而提示词明令禁止；
    #   ② IR 恰好是唯一会解析**攻击者构造的恶意文件**的场景（日志里能塞
    #      反序列化 payload、畸形 pcap、伪造的内存/磁盘镜像），多余工具
    #      纯属多出来的攻击面。
    #
    # ⚠️ 这条"故意不含"要和 `KernelSandboxProfile.IR` 的 docstring 一起读 ——
    #    那里写了为什么它必须独立成像，而不是 base 的超集。
    #
    # ## ★ 键用具体分类名 `ir-root-cause`，不用裸的 `ir`
    #
    # 这是踩过一次的：路由判据是**前缀** `ir-`（见 `profile_for_category`），
    # 所以裸 `ir` 不以 `ir-` 开头 → 路由算出来是 base → 脚本去 base
    # 镜像里找 `mactime`，报一个"未声明"的假缺失。假报错比没有检查更糟。
    #
    # 用具体分类名当键，顺带把这个校验也做掉了：如果哪天 `profile_for_category`
    # 的前缀判定写错，这一行会立刻路由到错误的镜像并露馅。
    # 三个 `ir-*` 分类共用同一个镜像，只列一个即可（契约按镜像算，不按分类算）。
    "ir-root-cause": (
        # 文本/日志基础盘（base 里都有，重复声明是有意的：
        # 这一栏是**契约**，不是"新增了什么"的清单）
        "grep",
        "awk",
        "sed",
        "sort",
        "uniq",
        "head",
        "tail",
        "wc",
        "cut",
        "tr",
        "date",
        "jq",
        "file",
        "strings",
        "xxd",
        "python3",
        # 证据解析（IR 镜像相对 base 真正多的那一批）
        "tshark",  # pcap 复盘
        "fls",  # sleuthkit：镜像里的文件列表
        "icat",  # sleuthkit：按 inode 取文件内容
        "mactime",  # sleuthkit：bodyfile → 时间线
        "binwalk",  # 固件 / 复合文件展开
        "unzip",
        "7z",
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

    def _translate(self, pkg: str, kind: str = "apt") -> tuple[str, tuple[str, ...]]:
        """把一个包名翻成 `(归一化包名, 它提供的命令)`。

        `kind` 只影响**查表**，不影响命令名本身：pip 包名大小写不敏感
        （`pip list` 打的是 `PyYAML`，Dockerfile 里可能写 `pyyaml`），
        而 apt 包名本来就是全小写。命令名一律保持表里写的样子 ——
        Linux 的命令名区分大小写，`Xvfb` 和 `xvfb` 是两个不同的东西。
        """
        # 包名可能带版本约束或 extras：`foo>=1.0` / `foo[bar]`
        base = re.split(r"[<>=!\[;]", pkg, maxsplit=1)[0].strip()
        if not base:
            return "", ()
        # 先按原样查（含 extras 的整串也可能是表里的键），再按 pip 的
        # 大小写不敏感查一次。
        candidates = [base]
        if kind == "pip":
            candidates.append(base.lower())
        for key in candidates:
            if key in PACKAGE_COMMANDS:
                return base, PACKAGE_COMMANDS[key]
        if pkg in PACKAGE_COMMANDS:
            return base, PACKAGE_COMMANDS[pkg]
        # 默认：包名 == 命令名
        return base, (base,)

    def commands(self) -> set[str]:
        """翻译成命令集合。"""
        cmds: set[str] = set()
        for kind, group in (("apt", self.apt), ("pip", self.pip)):
            for pkg in group:
                _, provided = self._translate(pkg, kind)
                cmds.update(provided)
        return cmds

    def owners(self) -> dict[str, list[tuple[str, str]]]:
        """反查表：命令 → [(声明它的包, 包来自 apt 还是 pip)]。

        ## 为什么需要反查（第二道筛子的一半）

        第一道只回答"名单里有没有写这个命令"。第二道问镜像之后会多出
        一个集合：**名单写了、`command -v` 却跑不到**。这个集合里混着
        两种完全不同的事，而它们的修法相反：

          · **名单写错** —— 包明明装好了，只是它不提供这个命令名
            （`vim-tiny` 只给 `vi`/`vim.tiny`；`oletools` 上游删了
            `oledump.py`）。修法是改 `PACKAGE_COMMANDS`，**不用碰镜像**。
          · **镜像没跟上** —— 包根本没装上（Dockerfile 改了、或者包里
            本来就没有这个包）。修法是改 Dockerfile 并**重建镜像**。

        要把两者分开，就必须能回答"这个命令是谁承诺的"，然后去问
        "那个包到底装没装"。没有反查表时只能猜，而猜错的代价是把
        「文本层的笔误」报成「镜像该重建了」—— 那会让人白重建一次。

        ## 为什么带 apt/pip 标记

        两个生态的"装没装"要用**完全不同的命令**问：apt 走 `dpkg-query`
        （还要处理虚拟包 Provides），pip 走 `pip show`。丢掉这个标记就得
        在探测时猜，而猜错的表现是"库包一律报缺失"。

        ## 为什么是 list 而不是单个包名

        同一个命令常被多个包提供（`python3` 在 `python3` / `python3-dev`
        等包里都能找到）。只要**其中任何一个**装好了，"包已装"就成立，
        所以这里保留全部候选，判定时取"任一已装"。
        """
        table: dict[str, list[tuple[str, str]]] = {}
        for kind, group in (("apt", self.apt), ("pip", self.pip)):
            for pkg in group:
                base, provided = self._translate(pkg, kind)
                if not base:
                    continue
                for cmd in provided:
                    bucket = table.setdefault(cmd, [])
                    if (base, kind) not in bucket:
                        bucket.append((base, kind))
        return table

    def declared(self) -> list[str]:
        """名单里声明的全部命令，按首次出现顺序（报告顺序要稳定）。"""
        seen: dict[str, None] = {}
        for kind, group in (("apt", self.apt), ("pip", self.pip)):
            for pkg in group:
                _, provided = self._translate(pkg, kind)
                for cmd in provided:
                    seen.setdefault(cmd, None)
        return list(seen)

    def packages(self) -> list[tuple[str, str]]:
        """名单里的包：`[(包名, "apt"|"pip")]`，已剥版本、保序去重。

        这是第二道筛子要问到镜像里的第二组问题：**这些包装了没有**。
        问它的理由是「命令缺失」有两种成因，而只有这一组答案能把它们
        分开（见 `owners()` 的说明）。
        """
        seen: dict[tuple[str, str], None] = {}
        for kind, group in (("apt", self.apt), ("pip", self.pip)):
            for pkg in group:
                base, _ = self._translate(pkg, kind)
                if base:
                    seen.setdefault((base, kind), None)
        return list(seen)


#: `FROM <image>`（只取第一个 —— 多阶段构建里第一个才是最终镜像的底座）。
_FROM_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)", re.IGNORECASE | re.MULTILINE)

#: `ARG BASE_IMAGE=suvex-sandbox:latest` —— `FROM ${BASE_IMAGE}` 的默认值。
_ARG_BASE_RE = re.compile(r"^\s*ARG\s+BASE_IMAGE=(?P<default>\S+)", re.IGNORECASE | re.MULTILINE)


def inherits_from(path: Path, parent: str) -> bool:
    """该 Dockerfile 是否 `FROM <parent>`（含 `ARG BASE_IMAGE=...` 间接写法）。

    ## 为什么要真的查一遍，而不是直接假设"反过来肯定继承 base"

    下面按"reverse 镜像 = base 的全部工具 + 自己那几段"来算，这个假设
    成立的前提是 `docker/suvex-sandbox-re/Dockerfile` 真的
    `FROM suvex-sandbox:latest`。现在它是 —— 写成
    `ARG BASE_IMAGE=suvex-sandbox:latest` + `FROM ${BASE_IMAGE}`。

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
    # 比较短名：`suvex-sandbox` 是 `suvex-sandbox:latest` 的子串
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

RUNNER_PY = API_ROOT / "app" / "kernel" / "runner.py"


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

    ## 为什么不 `from app.kernel.runner import ...`

    `app/kernel/runner.py` 顶层就 `from sqlalchemy import select`，往下还有
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
    #: ★ v0.41：`ir-*` 分类族的路由。默认值让 v0.41 之前的调用方
    #: （以及只关心两个镜像的用法）不需要改。
    #:
    #: 存三个字段而不是"把 ir 也塞进 reverse_categories"：两者的判据
    #: 不同 —— reverse 是**枚举集合**，ir 是**字符串前缀**。混在一起
    #: 会让 `profile_for` 的分支写成"先看集合再看前缀"，而顺序本身
    #: 在 `runner.profile_for_category` 里是有意义的（见那里的说明）。
    ir_profile: str = ""
    ir_prefix: str = ""

    def profile_for(self, category: str) -> str:
        """分类 → profile 取值。

        ## ★ 这段是从 `runner.py` 的 `profile_for_category` 抄的

        抄的是**逻辑**，不抄**数据** —— 数据（`_REVERSE_CATEGORIES`、
        `KernelSandboxProfile` 的三个取值、IR 前缀）每次运行都从源码现读。
        抄过来的只有"规范化 → 判前缀 → 查集合 → 三选一"这个形状，
        短到没有漂移空间。

        `runner.py` 里那条"必须精确匹配、不能子串匹配"的警告在这份复刻里
        同样成立：`"re"` 是 `_REVERSE_CATEGORIES` 的成员，但题目分类里有
        `"reverse"` 也有 `"web"`，用子串匹配会把 `"interface"` 判成逆向。

        ★ v0.41：**顺序与真实现一致 —— ir 先判**。真实现里那条注释
        解释了为什么：`ir-*` 是我们自己定义、名字可控的集合；而
        `_REVERSE_CATEGORIES` 混着平台可能传过来的别名。先判我们
        自己那批，将来平台传了个含 `re` 的别名时不会把 IR 题推进
        逆向镜像。这里抄顺序，是为了让"路由算错"能够显形 —— 顺序抄反
        的话，本脚本会去逆向镜像里找 `mactime`，然后报一个假缺失。

        如果哪天 `profile_for_category` 的逻辑真的变复杂了（比如按关键词
        模糊匹配），这里要跟着改 —— 而它会**立刻显形**：路由算错会导致
        下面冒出一堆莫名其妙的"缺工具"。
        """
        normalized = (category or "").strip().lower()
        if self.ir_prefix and normalized.startswith(self.ir_prefix):
            return self.ir_profile
        if normalized in self.reverse_categories:
            return self.reverse_profile
        return self.base_profile


def load_router() -> Router:
    """把「逆向分类集合 + 三个 profile 取值 + 各自的 Dockerfile」一次凑齐。

    ## 关键约束：**不 import `app.kernel.runner`**

    它顶层 `from sqlalchemy import select`，往下还有 SQLAlchemy / pydantic
    一大串。CI 里装着，但**本地改完 Dockerfile 想立刻验证时未必装着** ——
    而"本地跑不起来"的实际后果是开发者跳过验证直接提交，这个脚本就等于
    白写。实测本机 `python -c "import sqlalchemy"` 就是 ModuleNotFoundError。

    真正要避开的另一半是"抄一份可能过时的副本"：抄了的话 `runner.py` 改
    路由这边不会跟着改，这个检查守的就成了副本而不是真实行为。所以数据
    （`_REVERSE_CATEGORIES`、`KernelSandboxProfile` 三个取值、`IR_CATEGORY_PREFIX`）
    全部从源码现读，只有那 4 行控制流是复刻的。

    ★ v0.41：`ir` 的 Dockerfile 是**必须存在**的，不是可选的。
    理由：`CATEGORY_REQUIREMENTS["ir"]` 一旦在表里，能力契约就必须
    能算出它路由到哪个镜像，否则这个分类的检查会静默跳过 ——
    而"检查静默跳过"与"检查通过"在输出里长得一样。
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

    base_profile = vocab.KernelSandboxProfile.BASE.value
    reverse_profile = vocab.KernelSandboxProfile.REVERSE.value
    ir_profile = vocab.KernelSandboxProfile.IR.value

    profiles: dict[str, Path] = {}
    for name in (base_profile, reverse_profile, ir_profile):
        # profile 取值 == 镜像短名 == docker/ 下的目录名。
        # vocab 的注释明确说这是刻意的：不做"逻辑名 → 镜像名"的第二层映射，
        # 所以这里可以对上。
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
        ir_profile=ir_profile,
        ir_prefix=vocab.IR_CATEGORY_PREFIX,
    )


# ══════════════════════════════════════════════════════════
# 检查
# ══════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════
# 镜像实测（可选；需要 Docker，缺失时整段跳过）
# ══════════════════════════════════════════════════════════

#: 单个镜像实测的默认超时（秒）。`docker run` 冷启通常 1–3 秒，
#: 给到 60 秒是留给"磁盘很慢"与"本地正好没有这个 tag、要回退"的余量。
PROBE_TIMEOUT_SEC = 60.0


class ProbeStatus(StrEnum):
    """第二道「为什么没拿到证据」的**枚举**，不再是笼统的 `None`。

    ## 为什么 `None` 不够（这次补的）

    原先 `probe_image_full` 没证据时统统一律 `return None`，而 `None` 其实
    混了**五种**完全不同的情况：本机没装 docker / 镜像没构建 / 容器起不来 /
    超时 / 容器跑完但没输出。

    它们的共同点只有"没证据"，**下一步该做什么却完全相反**：

      · 没 docker      → 换一台装了 Docker 的机器（本机改不了）
      · 镜像没构建      → `docker build`（本机改得了）
      · 超时 / 起不来   → 加超时或去看镜像本身（可能是它真坏了）

    压成一句「这次没有证据」之后，读的人**没法从输出里知道下一步**，只能
    回来翻代码 —— 而这正是这次要修的那类问题：一个专门用来抓"镜像没跟上"
    的脚本，不该让人靠读它的源码才能判断自己该做什么。

    ## 每个成员都带"下一步"

    `hint` 给出可执行的下一步，`reason` 给出发生了什么。两者一起由
    `ProbeResult.reason_line()` 渲染，输出本身就把"该做什么"说完。
    """

    #: 真的问到了 —— 唯一可以据此下结论的状态。
    OK = "ok"
    #: 这台机器上没有 `docker` 命令。
    NO_DOCKER = "no_docker"
    #: 有 docker，但本地没有 `<image>:latest`（还没构建 / tag 不是 latest）。
    IMAGE_MISSING = "image_missing"
    #: 镜像在，但容器起不来（退出非 0、entrypoint 不对、Dockerfile 坏了）。
    START_FAILED = "start_failed"
    #: 容器在超时内没跑完。
    TIMEOUT = "timeout"
    #: 容器跑完了，但一条命令结果都没有 —— 探针脚本大概没被执行。
    NO_OUTPUT = "no_output"
    #: 注入的探测函数只回了裸 `None`，原因不明（测试/旧调用方的兼容路径）。
    NO_EVIDENCE = "no_evidence"

    @property
    def is_evidence(self) -> bool:
        """这个状态算不算"拿到了证据"。

        只有 `OK` 算。别的**一律不是通过** —— 把它们渲染成 ✅ 就是
        重复本项目历史上那个 6 天漂移窗口的错误。
        """
        return self is ProbeStatus.OK

    @property
    def reason(self) -> str:
        """发生了什么（一句话，不带修法）。"""
        return _PROBE_REASON.get(self, "没有拿到证据")

    @property
    def hint(self) -> str:
        """**下一步该做什么** —— 让输出自己把行动说清楚。"""
        return _PROBE_HINT.get(self, "去看上面的告警")


#: 每个没证据状态对应的一句话说明。与枚举分开写是为了让 `ProbeStatus`
#: 保持成一个纯枚举（可比较、可序列化），文案集中在一处便于对齐。
_PROBE_REASON: dict[ProbeStatus, str] = {
    ProbeStatus.OK: "探测成功",
    ProbeStatus.NO_DOCKER: "本机无 docker",
    ProbeStatus.IMAGE_MISSING: "本地没有这个镜像（还没构建？）",
    ProbeStatus.START_FAILED: "镜像在，但容器起不来",
    ProbeStatus.TIMEOUT: "容器在超时内没跑完",
    ProbeStatus.NO_OUTPUT: "容器跑完了但没有任何命令输出",
    ProbeStatus.NO_EVIDENCE: "没拿到证据（原因未记录）",
}

#: 每个没证据状态对应的**下一步动作**。这一段是这次拆枚举的主要收益：
#: 「该重建镜像」和「该换台机器」是两件完全不同的事，不该共用一句话。
_PROBE_HINT: dict[ProbeStatus, str] = {
    ProbeStatus.OK: "—",
    ProbeStatus.NO_DOCKER: "换一台装了 Docker 的机器跑；本机只信第一道（文本层）",
    ProbeStatus.IMAGE_MISSING: "先 docker build -f docker/<镜像>/Dockerfile -t <镜像>:latest .",
    ProbeStatus.START_FAILED: "看上面的 stderr；镜像本身可能已经坏了",
    ProbeStatus.TIMEOUT: "加大超时或先确认镜像没卡住",
    ProbeStatus.NO_OUTPUT: "探针脚本大概没被执行；检查镜像的 ENTRYPOINT/权限",
    ProbeStatus.NO_EVIDENCE: "这是注入路径，检查探测函数为什么没给结果",
}


@dataclass
class ProbeResult:
    """一次镜像实测的**全部**结果，含"为什么没拿到"。

    取代原先的 `tuple | None`：`None` 丢失了原因，而原因决定下一步。
    `status.is_evidence` 为真时 `commands` / `packages` 才有意义。
    """

    status: ProbeStatus
    image: str = ""
    #: 命令 → 镜像里在不在。仅 `status.is_evidence` 时有意义。
    commands: dict[str, bool] = field(default_factory=dict)
    #: (包名, "apt"|"pip") → 装没装。仅 `status.is_evidence` 时有意义。
    packages: dict[tuple[str, str], bool] = field(default_factory=dict)
    #: 附加细节（超时秒数、stderr 末行等），拼进输出用。
    detail: str = ""

    @property
    def ok(self) -> bool:
        """拿到证据了没有。**别名**，读起来比 `.status.is_evidence` 顺。"""
        return self.status.is_evidence

    def reason_line(self) -> str:
        """渲染成一行：发生了什么 + 下一步做什么。"""
        head = f"{self.image}：{self.status.reason}" if self.image else self.status.reason
        if self.detail:
            head = f"{head}（{self.detail}）"
        return f"{head} → {self.status.hint}"


def _as_probe_result(got: ProbeResult | tuple[dict[str, bool], dict[tuple[str, str], bool]] | None,
                     image: str) -> ProbeResult:
    """把注入函数可能返回的三种形状归一成 `ProbeResult`。

    `check(probe_fn=...)` 是测试注入点，允许它返回：
      · `ProbeResult` —— 新写法，带原因；
      · `(命令结果, 包结果)` 元组 —— 旧写法，等价于"探测成功"；
      · `None` —— 旧写法，等价于"没证据但说不出为什么"。
    """
    if isinstance(got, ProbeResult):
        return got
    if got is None:
        return ProbeResult(status=ProbeStatus.NO_EVIDENCE, image=image)
    cmd_result, pkg_result = got
    return ProbeResult(status=ProbeStatus.OK, image=image, commands=cmd_result, packages=pkg_result)


#: 第二道探测函数的形状：`(镜像名, 要问的命令, 要问的包) -> ProbeResult`。
#: 也接受旧的 `tuple | None` 写法（`_as_probe_result` 会归一）。
#: `check(probe_fn=...)` 允许测试替换它。
ProbeFn = Callable[
    [str, Sequence[str], Sequence[tuple[str, str]]],
    "ProbeResult | tuple[dict[str, bool], dict[tuple[str, str], bool]] | None",
]


def probe_image(
    image: str,
    commands: Sequence[str],
    *,
    timeout: float = PROBE_TIMEOUT_SEC,
) -> ProbeResult:
    """在**真实镜像**里对每个命令跑 `command -v`，返回「存在与否」。

    ## 为什么需要这一步（静态解析证明不了的事）

    上面那套解析证明的是「Dockerfile 的 apt/pip 命令里写了这个包」。
    而下面这两种情况它一个字都看不出：

      1. **包没装成**（依赖断了、源里没有、pip 轮子编译失败）——
         `ropper` 就是这样：`filebytes` 用了 Py3.12 移除的 `ast.Str`，
         构建直接断在一行 error 上；
      2. ★ **Dockerfile 改了、镜像没重建** —— 这一种最阴：仓库里
         `patchelf gdb \\` 明明写着（`2de97ce`，2026-10-18），而镜像
         是 2026-10-12 的，两个命令都不在。静态检查报「✅ pwn 全绿」，
         真实镜像里 `command -v gdb` 是 MISSING，模型每次
         `gdb.attach()` 都失败而日志上不出现"缺 gdb"。

    这两种都只能靠"对产物本身提问"才能发现，所以这个函数去问镜像。

    ## 返回值的三态是刻意的

      · `ProbeStatus.OK`  —— 镜像跑起来了，这是每个命令的真结果；
      · 其它任何状态      —— **验不了**，且 `status.reason` / `status.hint`
        会说明**是哪一种**验不了、下一步该做什么。

    第二种**不许**当成"全过"。它的语义是"这一维没有证据"，调用方要把它
    和"验过且通过"分开报 —— 否则又会回到"看起来在守、其实守不住"。

    ⚠️ 这里刻意不用 `None`（改前用的是 `None`）：`None` 把"没装 docker"和
    "镜像没建"压成同一个值，而这两件事的下一步完全相反（换机器 vs 重建
    镜像）。用一个带原因的枚举，读输出的人就不用回来读代码。

    ## 为什么一次 `docker run` 跑完所有命令

    每个命令单起一个容器，在 2.8 GB 的镜像上就是 40 次容器创建；
    而 `command -v` 本身是微秒级的。拼成一条 `sh -c` 只用付一次
    启动成本。

    ## 为什么用镜像自带的 USER（不 `-u root`）

    `USER solver` 的 PATH 与解题时**完全一致**，而这正是要验的东西：
    包装在 `/usr/local/bin`、而 solver 的 PATH 不含它是完全可能的
    （`one_gadget` / `seccomp-tools` 就装在 `/usr/local/bin`）。
    用 root 验会得出"有"，而解题时是"没有"。
    """
    docker = shutil.which("docker")
    if docker is None:
        return ProbeResult(status=ProbeStatus.NO_DOCKER, image=image)

    # 委托给 `probe_image_full`：只问命令、不问包。
    # ⚠️ 这里**不要**再抄一份 `docker run` 拼装逻辑 —— 两份实现一定
    # 会漂移，而漂移的样子是"同一个镜像，一个函数说 OK、另一个说 NO"。
    got = probe_image_full(image, commands, (), timeout=timeout)
    if not got.ok:
        return got
    return ProbeResult(
        status=ProbeStatus.OK, image=image, commands=got.commands, packages=got.packages, detail=got.detail
    )


#: 在镜像里问「这些包装了没有」时用的探针。
#:
#: ## 为什么不是"每个包一次 `dpkg -s`"
#:
#: 一是慢（100+ 个包就是 100+ 次进程启动），二是**会误判虚拟包**：
#: `p7zip-full` / `dnsutils` / `exiftool` 在 Kali rolling 里都是被别的
#: 实包 Provides 的虚拟名，`dpkg-query -W p7zip-full` 直接返回
#: "no packages found"，而 `7z` 明明能跑。实测就踩到了：
#: `p7zip-full` 报"没装"、同时 `7z` 是 OK —— 两个结论互相打架。
#:
#: 所以整份快照只取一次，再在本地对 `Provides` 反查。
_PKG_PROBE = r"""
echo "===DPKG==="
dpkg-query -W -f='${Package}\t${Status}\t${Provides}\n' 2>/dev/null
echo "===PIP==="
/opt/venv/bin/pip list --format=freeze 2>/dev/null || pip list --format=freeze 2>/dev/null || true
"""

#: 命令名里允许出现的字符。超出范围的一律不塞进 `sh -c` 脚本 ——
#: 这个脚本是拼给容器里的 `sh` 执行的，命令名来自 Dockerfile 文本，
#: 不能假设它一定干净。
_SAFE_CMD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _norm_pip(name: str) -> str:
    """PEP 503 归一化：`PyYAML` / `python-dateutil` / `pyyaml` 视为同一个。"""
    return re.sub(r"[-_.]+", "_", name.strip().lower())


def probe_image_full(
    image: str,
    commands: Sequence[str],
    packages: Sequence[tuple[str, str]],
    *,
    timeout: float = PROBE_TIMEOUT_SEC,
) -> ProbeResult:
    """一次容器里同时问两个问题：命令在不在、包装没装。

    ## 为什么两问必须同一次 `docker run`

    分开跑就是两次冷启动（2.8 GB 镜像上各 1–3 秒），更要紧的是
    **两问必须看到同一个镜像状态** —— 中间如果有人重建了镜像，
    分开问会得出"命令缺失 + 包已装"这种自相矛盾的组合，而它恰好
    会被归类成"名单写错"，指向一个错误的修法。

    ## 返回 `ProbeResult`

      · `status=OK`     —— 两问都拿到了，`commands` / `packages` 可用；
      · 其它任何 status —— **没证据**，不许当"全过"；`status.hint` 说明
        下一步该做什么（换机器 / 重建镜像 / 加超时）。

    包结果的键是 `(包名, "apt"|"pip")`，因为两个生态的"装没装"
    是两套独立事实。
    """
    docker = shutil.which("docker")
    if docker is None:
        return ProbeResult(status=ProbeStatus.NO_DOCKER, image=image)

    try:
        inspect = subprocess.run(  # noqa: S603  —— argv 本地构造
            [docker, "image", "inspect", f"{image}:latest"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(
            status=ProbeStatus.TIMEOUT, image=image, detail="docker image inspect 超过 15s"
        )
    except OSError as exc:
        return ProbeResult(status=ProbeStatus.START_FAILED, image=image, detail=f"docker 调不起来：{exc}")
    if inspect.returncode != 0:
        return ProbeResult(status=ProbeStatus.IMAGE_MISSING, image=image)

    lines: list[str] = []
    probed_cmds: list[str] = []
    for name in commands:
        if not _SAFE_CMD_RE.match(name):
            continue
        probed_cmds.append(name)
        lines.append(
            f'command -v {name} >/dev/null 2>&1 && echo "C_OK:{name}" || echo "C_NO:{name}"'
        )
    lines.append(_PKG_PROBE)
    script = "\n".join(lines)

    argv = [docker, "run", "--rm", "--entrypoint", "sh", f"{image}:latest", "-c", script]
    try:
        done = subprocess.run(  # noqa: S603  —— argv 是本地构造的，无 shell 介入
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(status=ProbeStatus.TIMEOUT, image=image, detail=f"{timeout:.0f}s")
    except OSError as exc:
        return ProbeResult(status=ProbeStatus.START_FAILED, image=image, detail=str(exc))

    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()
        hint = tail[-1] if tail else f"exit {done.returncode}"
        return ProbeResult(status=ProbeStatus.START_FAILED, image=image, detail=hint)

    cmd_result: dict[str, bool] = {}
    installed_pkgs: set[str] = set()
    pip_names: set[str] = set()
    section = "cmd"
    for raw in done.stdout.splitlines():
        line = raw.rstrip()
        if line.strip() == "===DPKG===":
            section = "dpkg"
            continue
        if line.strip() == "===PIP===":
            section = "pip"
            continue
        if section == "cmd":
            if line.startswith("C_OK:"):
                cmd_result[line[5:]] = True
            elif line.startswith("C_NO:"):
                cmd_result[line[5:]] = False
        elif section == "dpkg":
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            pkg, status = parts[0].strip(), parts[1].strip()
            if not pkg or "install ok installed" not in status:
                continue
            installed_pkgs.add(pkg)
            if len(parts) >= 3:
                # `Provides` 是逗号分隔的虚拟名列表，每项可能带版本
                for virtual in parts[2].split(","):
                    name = virtual.strip().split("(")[0].strip()
                    if name:
                        installed_pkgs.add(name)
        else:
            if "==" in line:
                pip_names.add(_norm_pip(line.split("==", 1)[0]))

    if not cmd_result:
        return ProbeResult(status=ProbeStatus.NO_OUTPUT, image=image)

    pkg_result: dict[tuple[str, str], bool] = {}
    for pkg, kind in packages:
        if kind == "apt":
            pkg_result[(pkg, kind)] = pkg in installed_pkgs
        else:
            pkg_result[(pkg, kind)] = _norm_pip(pkg) in pip_names
    return ProbeResult(status=ProbeStatus.OK, image=image, commands=cmd_result, packages=pkg_result)


@dataclass
class ImageDrift:
    """一个镜像的第二道结果：**名单写错** 与 **镜像没跟上** 分开放。

    这两栏永远不合并成一句"有缺失"。原因是修法相反、责任人也不同：

      · `naming_errors` —— 包装好了，只是它不提供这个命令名。
        改 `PACKAGE_COMMANDS`，**不用重建镜像**。审代码就能改完。
      · `image_lagging` —— 包根本没装上。改 Dockerfile 并**重建镜像**，
        要等几分钟构建、还可能要改 CI 的构建流程。

    合成一句"镜像有问题"会让人默认去重建镜像 —— 而实测中 30 项里
    有 28 项是前者，重建多少次都不会好。
    """

    image: str
    #: (命令, 已装的包) —— 名单写错
    naming_errors: list[tuple[str, str]] = field(default_factory=list)
    #: (命令, 一个都没装的候选包) —— 镜像没跟上
    image_lagging: list[tuple[str, str]] = field(default_factory=list)
    #: 判不了（包状态没问到）。**不许**当成通过，也不许猜成上面两类。
    unknown: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.naming_errors or self.image_lagging or self.unknown)


def classify_drift(
    image: str,
    image_pkgs: ImagePackages,
    cmd_probe: dict[str, bool],
    pkg_probe: dict[tuple[str, str], bool],
) -> ImageDrift:
    """把「命令跑不到」按成因二分。**纯函数**，不碰 docker。

    判据只有一条，而它需要先知道"这个命令是谁承诺的"：

        命令 `command -v` 失败
          ├─ 承诺它的包**至少一个装了**   → 名单写错（包在，命令名不对）
          ├─ 承诺它的包**一个都没装**      → 镜像没跟上（该重建）
          └─ 包状态问不到                  → unknown（不下结论）

    历史教训支撑这条判据：`patchelf`/`gdb` 那次是"包没装"（镜像停在
    6 天前），`steghide`/`pngcheck` 那次是"名字写在注释里"（包根本没声明），
    `oledump.py` 那次是"包装了、上游删了那个脚本"。三种的修法各不相同，
    而只有第一种需要重建镜像。
    """
    drift = ImageDrift(image=image)
    owners = image_pkgs.owners()
    for cmd in image_pkgs.declared():
        if cmd_probe.get(cmd) is not False:
            continue
        candidates = owners.get(cmd, [])
        if not candidates:
            drift.unknown.append(cmd)
            continue
        states = [(pkg, kind, pkg_probe.get((pkg, kind))) for pkg, kind in candidates]
        installed = [(pkg, kind) for pkg, kind, ok in states if ok is True]
        if installed:
            drift.naming_errors.append((cmd, installed[0][0]))
            continue
        if any(ok is None for _, _, ok in states):
            drift.unknown.append(cmd)
            continue
        drift.image_lagging.append((cmd, candidates[0][0]))
    return drift


@dataclass
class Finding:
    category: str
    image: str
    missing: list[str]
    #: 声明里有、但实测镜像里 `command -v` 跑不到 —— 这两类要分开报，
    #: 因为修法完全不同（前者改 Dockerfile，后者**重建镜像**）。
    missing_in_image: list[str] = field(default_factory=list)


class CheckOutcome(list):
    """`check()` 的返回值：**是** `list[Finding]`，另外挂第二道的结果。

    ## 为什么继承 list 而不是换成 dataclass

    第一道的返回类型被 `main()` 和未来的测试直接当列表用（`if findings:`
    / 列表推导 / `len()`）。改成 dataclass 就得同步改所有调用点，而其中
    任何一处漏改都会变成"检查永远报绿"——静默失败，最坏的一种。

    继承 `list` 之后，所有列表用法原样成立，第二道的结果只是多挂几个
    属性。**第一道的语义一个字都没动**，这是兼容性的最低风险走法。

    ## 为什么第二道的结果不并进列表本身

    `Finding` 的粒度是「某个**分类**需要的工具凑不齐」；第二道回答的是
    「名单里声明的某个**命令**在镜像里存不存在」。粒度、主体、修法全都
    不同。塞进同一个列表之后就再也分不开了，而"能分开"正是这次改动的
    全部意义。
    """

    def __init__(self, findings: list[Finding] | None = None) -> None:
        super().__init__(findings or [])
        #: 每个镜像的第二道结果。**只有真的探测过的镜像才会在这里**。
        self.drifts: dict[str, ImageDrift] = {}
        #: 每个**没探成**的镜像及其原因（`ProbeStatus`）。键是镜像/profile 名。
        #:
        #: 与 `probe_not_run_reason`（一句话总结）并存：这句总结是给人读的，
        #: 而这个字典是给程序按状态分支用的（"镜像没建"就值得非零退出码，
        #: "本机没 docker"则不该怪这条流水线）。
        self.probe_failures: dict[str, ProbeResult] = {}
        #: 是否请求了 `--probe`（不代表跑成功）。
        self.probe_attempted: bool = False
        #: 第二道没跑的原因（空串 = 跑了）。
        self.probe_not_run_reason: str = ""
        #: 解析出来的镜像表，报告"某命令属于哪个包"时要用。
        self.images: dict[str, ImagePackages] = {}

    @property
    def probe_checked(self) -> bool:
        """第二道到底有没有拿到证据。"""
        return bool(self.drifts)

    def probe_skipped_for(self, status: ProbeStatus) -> list[str]:
        """因为某个具体原因没探成的镜像。按状态分支时用。"""
        return sorted(name for name, r in self.probe_failures.items() if r.status is status)

    @property
    def drift_confirmed(self) -> list[ImageDrift]:
        """拿到证据、且**确实有问题**的那些镜像。"""
        return [d for d in self.drifts.values() if d.naming_errors or d.image_lagging]

    @property
    def drift_unknown(self) -> list[ImageDrift]:
        """拿到了命令结果，但包的安装状态问不到 —— 不下结论。"""
        return [d for d in self.drifts.values() if d.unknown]


def print_second_screen(
    drifts: dict[str, ImageDrift],
    not_run_reason: str,
    failures: dict[str, ProbeResult] | None = None,
) -> None:
    """打印第二道的独立报表面。

    ## 为什么必须是**独立的一面**，而不是往第一道表里加一列

    两张表回答两个不同的问题，而且它们**可以互相矛盾**：

      · 第一道说"名单里声明了 checksec"（文本层成立）；
      · 第二道说"镜像里 `command -v checksec` 跑不到"（产物层不成立）。

    合成一格 ✅/❌ 时，这个矛盾会被压成一个字，读的人无法判断该改文本
    还是该重建镜像。历史上 5 次事故里，28 项是文本层笔误（改文本即可）、
    2 项是产物层滞后（必须重建镜像），把它们都写成一个 ❌ 的结果，就是
    每次都先浪费一轮重建。

    ## 三栏是刻意分开的

      A 名单写错（包已装、命令名不对）→ 改 `PACKAGE_COMMANDS`，**不重建**
      B 镜像没跟上（包没装）          → 改 Dockerfile 并**重建镜像**
      C 判不了（包状态没问到）        → 不下结论，去查

    C 单独一栏是因为"不知道"和"没问题"必须分开 —— 把它并进 A 或 B 就是
    编造结论；把它藏起来就是回到"看起来在守、其实守不住"。
    """
    print("\n── 第二道：镜像实测结论 ──\n")

    if not drifts:
        # ★ 这一段是这次改动要堵的核心漏洞。
        #
        #   旧版本在没有 docker 时**什么都不打印**，然后 `main()` 照样打
        #   `✅ … 声明与产物一致`。于是"没验"被读成了"验过了、没问题"。
        #   一个专门用来抓"镜像没跟上"的脚本，自己在没有 docker 的机器上
        #   谎报通过 —— 这是同一个错误的第二次发生。
        #
        #   ★ 并**按原因分开报**： "本机无 docker"(换机器) 与 "镜像没建"
        #     (去 build) 的下一步相反，压成一句就把下一步丢了。
        if failures:
            print("   ⚠️ 第二道未跑 —— 逐镜像原因和下一步：")
            for image in sorted(failures):
                print(f"     · {failures[image].reason_line()}")
        else:
            print(f"   ⚠️ 第二道未跑：{not_run_reason or '没有拿到任何镜像证据'}。")
        print("   ⚠️ 这不是「通过」，是「没验」—— 上面只有第一道（文本层）的结论，")
        print("      镜像里到底有没有这些命令，这次**没有任何证据**。\n")
        return

    okay = [d for d in drifts.values() if d.clean]
    naming = [d for d in drifts.values() if d.naming_errors]
    lagging = [d for d in drifts.values() if d.image_lagging]
    unknown = [d for d in drifts.values() if d.unknown]

    print("   ★ 下面两栏是**两件不同的事**，修法相反，不要合起来看：")
    print("     A 名单写错   → 包装好了，只是它不提供这个命令名 → 改 PACKAGE_COMMANDS，")
    print("                    **不用重建镜像**，改完提交即可。")
    print("     B 镜像没跟上 → 包根本没装上 → 改 Dockerfile 并**重建镜像**。")
    print()

    if naming:
        total = sum(len(d.naming_errors) for d in naming)
        print(f"   A 名单写错（{total} 项，改文本即可，无需重建）")
        for drift in naming:
            for cmd, pkg in drift.naming_errors:
                print(f"     · {drift.image:<20} {cmd:<20} 包 {pkg} 已装，但它不提供该命令")
        print()

    if lagging:
        total = sum(len(d.image_lagging) for d in lagging)
        print(f"   B 镜像没跟上（{total} 项，必须重建镜像）")
        for drift in lagging:
            for cmd, pkg in drift.image_lagging:
                print(f"     · {drift.image:<20} {cmd:<20} 包 {pkg} 根本没装")
        print("     修法：docker build -f docker/<镜像>/Dockerfile -t <镜像>:latest .")
        print()

    if unknown:
        total = sum(len(d.unknown) for d in unknown)
        print(f"   C 判不了（{total} 项：命令跑不到，但包的安装状态没问到）")
        for drift in unknown:
            print(f"     · {drift.image:<20} {', '.join(drift.unknown)}")
        print("     ↑ 这一栏**不是通过**，是证据不足。别猜成 A 或 B。\n")

    if not (naming or lagging or unknown):
        for drift in okay:
            print(f"   ✅ {drift.image:<20} 声明的每个命令在镜像里都能 `command -v` 到")
        print()


def _resolve_image(
    router: Router, profile: str, base: ImagePackages
) -> tuple[ImagePackages, bool]:
    """解析一个派生镜像，返回 `(合并后的包清单, 是否继承 base)`。

    ## 为什么抽成函数（v0.41）

    在只有 reverse 一个派生镜像时，这段逻辑内联在 `check()` 里挺好读。
    加上 ir 之后就有两段近乎相同的代码，而**第三段很快也会来**
    （这是本项目第三次加场景包）。三段并列时，加第四个镜像的那次改动
    几乎必然漏掉其中一段，而漏掉的两个症状都是假报错：

      · 没做继承合并 → "这个镜像缺 curl / python3"（其实 base 里有）；
      · 没去重 → 同一批包算两遍，能力表里看不出问题但输出重复。

    假报错比没有检查更糟 —— 它会让真报错被淹没，然后大家开始无视
    这个脚本的输出。这是这个脚本存在的唯一意义所在。

    ## 为什么"是否继承"要真去 FROM 行查，而不是当前提

    `inherits_from` 读的是 Dockerfile 的 FROM 行。写死"派生镜像一定
    FROM base"会让"哪天有人把 ir 改成独立基础镜像"这种情况被悄悄
    掩盖 —— 那时 ir 里真的缺 curl，而脚本说它有。
    """
    path = router.profiles[profile]
    own = parse_image(path, profile)
    inherits = inherits_from(path, router.base_profile)
    if not inherits:
        # 不是继承关系 —— 那它自己的列表就是全集，缺什么会被如实
        # 报出来，而不是被 base 的包悄悄填上。
        return own, False
    merged = ImagePackages(
        name=own.name,
        apt=base.apt + [p for p in own.apt if p not in base.apt],
        pip=base.pip + [p for p in own.pip if p not in base.pip],
    )
    return merged, True


def _summarize_probe_failures(failures: dict[str, ProbeResult]) -> str:
    """把"哪些镜像因为什么没跑成"压成一句**带原因**的总结。

    ## 为什么不是一句"三个镜像都没跑起来"

    改前所有失败共用一句「本机无 docker / 镜像没跑起来」，读输出的人
    无法区分这两种。而它们要做的**下一件事完全相反**：

      · 本机没 docker → 这台机器上做什么都没用，得换机器；
      · 镜像没建      → 就在这台机器上 `docker build` 即可。

    所以这里按状态分组，状态名 + 下一步都写进同一句。
    """
    by_status: dict[ProbeStatus, list[str]] = {}
    for image, result in failures.items():
        by_status.setdefault(result.status, []).append(image)

    parts: list[str] = []
    for status, names in by_status.items():
        parts.append(f"{status.reason}（{', '.join(sorted(names))}）→ {status.hint}")
    return "；".join(parts)


def check(
    verbose: bool = False,
    probe: bool = False,
    *,
    probe_fn: ProbeFn | None = None,
) -> list[Finding]:
    """主体检查。返回所有"缺工具"的发现。

    `probe=True` 时额外对**真实镜像**跑一遍 `command -v`
    （需要 Docker；跑不起来就退化成纯静态，不会因此判失败）。

    ## `probe_fn` 是**测试注入点**，生产调用一律不传

    第二道的判据（"这个命令跑不到，是因为包没装、还是因为名字写错了"）是
    整个改动里最容易写错的一处，而它只有在"命令缺失"时才被触发 ——
    真实镜像要制造一次缺失就得重建镜像，不可能放进单元测试。

    所以这里留一个可替换的探测函数：测试传一个假的进去，就能精确构造
    "命令缺失 + 包已装"（→ 名单写错）和"命令缺失 + 包没装"（→ 镜像没跟上）
    两种现场，验证两栏**不会**被合并。

    默认 `None` 表示用真的 `probe_image_full`。**没有环境变量开关、没有
    命令行参数**指向它 —— 免得有人拿它去"跑出一个绿的 CI"。
    """

    router = load_router()
    base_path = router.profiles[router.base_profile]
    base = parse_image(base_path, router.base_profile)

    # ## ★ 每个派生镜像都单独判一次"是否 FROM base"
    #
    # v0.41 起有三个镜像，未来还会有第四个。这里不把 reverse / ir 各自
    # 写死一段，而是走同一个 `_resolve_image` —— 三段近乎相同的代码
    # 摆在一起时，加第四个镜像的那次改动**一定**会漏掉其中一段，
    # 而漏掉的样子是"那个镜像的工具全部报缺失"（因为没继承 base），
    # 或者更糟："明明不缺也报缺"（因为没做去重）。两种都是假报错，
    # 而假报错会让这个脚本被彻底无视。
    reverse, reverse_inherits = _resolve_image(
        router, router.reverse_profile, base
    )
    ir, ir_inherits = _resolve_image(router, router.ir_profile, base)

    images = {
        router.base_profile: base,
        router.reverse_profile: reverse,
    }
    # ir 只在真的配了 Dockerfile 时进表 —— `load_router` 已经保证它在，
    # 但这里多一道判空是为了让"把 IR 摘掉"这件事在测试里可行
    # （见 `tests/test_verify_sandbox_tools.py` 里那条"缺 Dockerfile 时报错"）。
    if router.ir_profile:
        images[router.ir_profile] = ir

    if verbose:
        print(f"\n   逆向分类（{len(router.reverse_categories)} 个）：")
        print("     " + ", ".join(sorted(router.reverse_categories)))
        print(f"   reverse 镜像继承 base：{'是' if reverse_inherits else '否（独立基础镜像）'}")
        if router.ir_profile:
            print(f"   {router.ir_profile} 镜像继承 base：{'是' if ir_inherits else '否（独立基础镜像）'}")
            print(f"   应急响应分类前缀：{router.ir_prefix!r}")
        for name, image in images.items():
            cmds = sorted(image.commands())
            print(f"\n── {name} ──")
            print(f"   apt 包 {len(image.apt)} 个 / pip 包 {len(image.pip)} 个")
            print(f"   命令 {len(cmds)} 个：")
            for i in range(0, len(cmds), 6):
                print("     " + "  ".join(f"{c:<18}" for c in cmds[i : i + 6]))

    # ── 可选：对真实镜像实测（第二道筛子）──────────────────
    #
    # 每个 profile 只起**一次**容器，一次问完两件事：
    #   ① 名单里声明的**每一个**命令在不在（不只是必需命令）；
    #   ② 声明里涉及的**每一个包**装了没有。
    #
    # ② 是这次新增的：没有它就只能说"某命令不存在"，说不出**为什么**，
    # 于是"名单写错"和"镜像没跟上"被迫混成一条，而这两者的修法相反。
    probes: dict[str, dict[str, bool]] = {}
    drifts: dict[str, ImageDrift] = {}
    no_probe_reason = ""
    #: 每个镜像**为什么**没拿到证据。这一条是这次拆枚举的主要收益：
    #: 输出里能直接读到"没装 docker"还是"镜像没建"，下一步动作随之不同。
    probe_failures: dict[str, ProbeResult] = {}
    if probe:
        do_probe = probe_fn if probe_fn is not None else probe_image_full
        print("\n── 第二道：镜像实测（command -v + 包安装状态，需要 Docker）──\n")
        for profile, image in sorted(images.items()):
            commands = image.declared()
            packages = image.packages()
            started = time.monotonic()
            got = _as_probe_result(do_probe(profile, commands, packages), profile)
            elapsed = time.monotonic() - started
            if not got.ok:
                # ★ 这里必须**明说没跑、且说清是哪一种没跑**，不能说"通过"。
                #   旧版本在这条路径上什么都不打印，而 `main()` 末尾
                #   照样打 ✅ —— 于是"没验"被读成"验过了、没问题"。
                #   那正是这个脚本要防的事，却发生在脚本自己身上。
                probe_failures[profile] = got
                print(f"   ⚠️ {got.reason_line()}")
                continue
            probes[profile] = got.commands
            hit = sum(1 for ok in got.commands.values() if ok)
            print(
                f"   {profile:<20} 命令 {hit}/{len(got.commands)} 命中"
                f"，包 {sum(1 for v in got.packages.values() if v)}/{len(got.packages)} 已装"
                f"（{elapsed:.1f}s）"
            )
            drifts[profile] = classify_drift(profile, image, got.commands, got.packages)

        # 汇总"没跑"的原因。**按状态分别说**，不再压成一句话 ——
        # "本机无 docker"(换机器) 与 "镜像没建"(去 build) 的下一步相反。
        if probe_failures:
            no_probe_reason = _summarize_probe_failures(probe_failures)

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

        # ★ 静态说"有"、实测说"没有" —— 这一类静态检查**永远**看不见，
        #   而它恰恰是实际踩到的那个（Dockerfile 改了、镜像没重建）。
        #   只有 probe 真的跑起来了才谈得上"没有"，否则这一栏是"未知"。
        probed = probes.get(profile)
        vanished = (
            [c for c in required if c not in missing and probed.get(c) is False]
            if probed is not None
            else []
        )

        notes: list[str] = []
        if missing:
            notes.append("未声明：" + ", ".join(missing))
        if vanished:
            notes.append("声明有、镜像里没有：" + ", ".join(vanished))
        status = "✅" if not notes else "❌"
        print(f"{category:<12} {profile:<20} {len(required):<6} {status:<8} {'; '.join(notes) or '—'}")
        if notes:
            findings.append(
                Finding(
                    category=category,
                    image=profile,
                    missing=missing,
                    missing_in_image=vanished,
                )
            )

    # ★ 第二道的结论**不塞进 `findings`**。
    #
    #   `findings` 的语义是「某个分类需要的工具凑不齐」，而第二道回答的是
    #   另一个问题：「Dockerfile 里声明的命令，在镜像里到底存不存在」。
    #   两者的粒度都不同（分类 vs 命令），塞在一起就再也分不开 ——
    #   而"能分开"正是这个改动的全部意义。
    #
    #   但 `--probe` 发现真漂移时**必须影响退出码**，否则 CI 加不加它
    #   都一样绿，等于没接上。这一向由 `CheckOutcome` 携带。
    if probe:
        print_second_screen(drifts, no_probe_reason, probe_failures)

    outcome = CheckOutcome(findings)
    outcome.drifts = drifts
    outcome.probe_attempted = probe
    outcome.probe_not_run_reason = no_probe_reason
    outcome.probe_failures = probe_failures
    outcome.images = images
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="列出每个镜像实际提供的命令（排查误报时用）",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help=(
            "额外对真实镜像跑 command -v（需要 Docker）。"
            "能抓到静态检查看不见的一类：Dockerfile 声明了、镜像里没有。"
        ),
    )
    args = parser.parse_args()

    findings = check(verbose=args.verbose, probe=args.probe)

    print()
    if findings:
        declared_gap = [f for f in findings if f.missing]
        image_gap = [f for f in findings if f.missing_in_image]
        print(f"❌ {len(findings)} 个分类缺工具：\n")
        for f in declared_gap:
            print(f"  · {f.category}（{f.image}）Dockerfile 里就没声明：{', '.join(f.missing)}")
        for f in image_gap:
            print(
                f"  · {f.category}（{f.image}）Dockerfile 声明了、"
                f"**镜像里没有**：{', '.join(f.missing_in_image)}"
            )
        print("\n   对应的修法：\n")
        if declared_gap:
            print(
                "     1. 镜像里该有的 → 加到对应 Dockerfile 的 apt/pip 列表；\n"
                "     2. 提示词里写错了/不该承诺的 → 改 prompts/kernel_system.md，\n"
                "        并同步 CATEGORY_REQUIREMENTS。"
            )
        if image_gap:
            print(
                "     3. ★ 「声明有、镜像里没有」只有一种修法：**重建镜像**。\n"
                "        仓库里的 Dockerfile 已经不是镜像的来源了 ——\n"
                "            docker build -f docker/<镜像>/Dockerfile -t <镜像>:latest .\n"
                "        静态检查永远看不见这一类，先跑 --probe 再谈「通过」。"
            )
        if args.probe and findings.drift_confirmed:
            print_second_repair(drift=findings.drift_confirmed)
        print()
        return 1

    print(
        "✅ 每个分类需要的工具，在它被路由到的镜像里都有声明"
        f"（{len(CATEGORY_REQUIREMENTS)} 个分类）。\n"
    )
    if not args.probe:
        print(
            "⚠️ 这个脚本证明的是「Dockerfile 的 apt/pip 命令里写了这个包」，\n"
            "   **不**证明「包真的装成功了、命令真的在 PATH 里」。有两种情况\n"
            "   静态解析天生看不见：\n"
            "     · 包没装成（依赖断掉 / 源里没有 / 轮子编译失败）；\n"
            "     · ★ Dockerfile 改了、**镜像没重建** —— 仓库里写着 `patchelf gdb`\n"
            "       的那次提交是 2026-10-18，而镜像是 2026-10-12 的，两个命令都\n"
            "       不在里面，而这次检查报的是「pwn ✅」。\n"
            "   加 `--probe` 就会去问镜像本身（需要 Docker）。\n"
        )
        return 0

    # ── 到这里：第一道全绿 + 请求了 --probe ──────────────────
    #
    # ★ 这一段是整个改动要堵的洞。
    #
    #   旧代码在这里无条件打印「✅ 并且 --probe 在真实镜像里逐个 command -v
    #   过 —— 声明与产物一致」。可是 `--probe` 在三种情况下**跑都没跑**：
    #   本机没有 docker、镜像还没构建、容器超时。那三种情况下它打印的仍然是
    #   ✅，于是"没验"被读成"验过了、没问题"。
    #
    #   一个专门用来抓"镜像没跟上"的脚本，自己在没有 docker 的机器上谎报
    #   通过 —— 这是同一个错误在脚本自己身上又发生了一次。
    #
    #   现在只有**真的拿到证据、且证据干净**时才说一致。
    if not findings.probe_checked:
        # 可跳过、且**不返回失败退出码**（CI 上没有 docker 是常态）。
        # 但必须明说"没验" —— 不能静默，也不能说成通过。
        #
        # ⚠️ 这里的措辞刻意**不含**那句结论原文（"声明与产物"…"一致"）。
        #    含了的话，`grep` 这类检查会把"没有证据"的告警当成"通过"的
        #    凭据 —— 这正是这个脚本要防的误读，不能在告警里自己制造一次。
        print("⚠️ 第二道未跑 —— 这不是「通过」，是「没验」。")
        if findings.probe_failures:
            # ★ 逐镜像给出「为什么没跑」+「下一步做什么」。
            #   改前这里只有一句笼统的"本机无 docker / 镜像没跑起来"，
            #   而"换一台机器"和"在这台机器上 build"是相反的行动。
            for image in sorted(findings.probe_failures):
                print(f"   · {findings.probe_failures[image].reason_line()}")
        else:
            print(f"   · {findings.probe_not_run_reason or '没拿到任何镜像证据'}")
        print(
            "   所以「文本层与产物层对得上」这个结论**这次没有证据**；"
            "上面的 ✅ 只覆盖第一道（文本层）。\n"
        )
        return 0

    if findings.drift_confirmed:
        print_second_repair(drift=findings.drift_confirmed)
        return 1

    if findings.drift_unknown:
        print(
            "⚠️ 第二道跑到了，但有命令既 `command -v` 不到、又查不清它所属包装没装 ——"
            "这一维**不下结论**（见上面的 C 栏）。\n"
            "   上面第一道全绿；第二道对这部分是「证据不足」，不是「通过」。\n"
        )
        return 0

    print(
        "✅ 并且 `--probe` 在真实镜像里逐个 `command -v` 过 —— 声明与产物一致。\n"
    )
    return 0


def print_second_repair(*, drift: list[ImageDrift]) -> None:
    """第二道查出真问题时的修法说明（与第一道的修法分开列）。

    这里要把**具体哪个命令、在哪个镜像里**再说一遍 —— 上面那两栏是分区
    报告的，但结论区只有一段，读者可能直接从结论开始看。少写这一遍，
    结论区就只剩一个数字，还得往上翻才能知道缺的是什么。
    """
    naming: list[tuple[str, str, str]] = [
        (d.image, cmd, pkg) for d in drift for cmd, pkg in d.naming_errors
    ]
    lagging: list[tuple[str, str, str]] = [
        (d.image, cmd, pkg) for d in drift for cmd, pkg in d.image_lagging
    ]
    print("\n   第二道（镜像实测）另外发现了漂移：\n")
    if naming:
        print(f"     A 名单写错 {len(naming)} 项 —— 包装好了但命令名不对，**不用重建镜像**：")
        for image, cmd, pkg in naming:
            print(f"        · {image:<20} {cmd:<20}（包 {pkg} 已装）")
        print("        修法：改 scripts/verify_sandbox_tools.py 的 PACKAGE_COMMANDS。")
    if lagging:
        print(f"     B 镜像没跟上 {len(lagging)} 项 —— 包根本没装上，**必须重建镜像**：")
        for image, cmd, pkg in lagging:
            print(f"        · {image:<20} {cmd:<20}（包 {pkg} 没装）")
        print("        修法：docker build -f docker/<镜像>/Dockerfile -t <镜像>:latest .")


if __name__ == "__main__":
    raise SystemExit(main())
