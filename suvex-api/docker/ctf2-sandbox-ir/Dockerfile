# ══════════════════════════════════════════════════════════
# ctf2-sandbox-ir —— **应急响应 / 根因复盘专用**沙箱（v0.41）
# ══════════════════════════════════════════════════════════
#
# ## ★ 为什么它也必须单独一个镜像
#
# 与 `ctf2-sandbox-re` 同一个理由，但方向不同：
#
#   · re 的风险面是「**执行**攻击者给的代码」（逆向题必须把附件跑起来）；
#   · ir 的风险面是「**解析**攻击者构造的文件」—— 日志、内存镜像、磁盘
#     镜像、流量包、邮件原文。这些格式的解析器（binwalk / tshark /
#     sleuthkit）历史上是内存破坏漏洞的高发区，而它们恰好是你唯一
#     必须喂攻击者数据的那批工具。
#
# 所以这个镜像的第一原则是**窄**：只装"读"的工具，一个"打"的都不装。
#
# ## ★ 为什么**不**装 curl / nmap / sqlmap / hashcat / 爆破类
#
# 两个理由，第二个是硬的：
#
#   ① 提示词 `prompts/ctf2_ir.md` 明令禁止对目标做任何处置动作，也不许
#      输出可直接粘贴执行的命令。装着攻击工具等于让"顺手打一下"在技术上
#      可行 —— 而纪律如果只能靠提示词守，那它迟早会被一次误判绕过。
#   ② IR 任务拿到的证据**来自攻击者**。这个镜像里每多一个能发起网络
#      连接的工具，就多一条"从证据文件到外部网络"的路径。少装一个，
#      这条路径就断一条。
#
# ## ★ 为什么继承 base 而不是 FROM debian
#
# 与 re 一样 `FROM base`，因为复盘要用到 grep/awk/sed/sort/jq/python3
# 这一整盘文本工具，而 base 里全都有。从零写一份等于把 base 的
# 工具清单抄一遍再维护两份 —— 这份 Dockerfile 就只声明 **IR 特有**的那些。
#
# 代价如实记（与 re 的注释同一段话）：base 变了 ir 也得重建。
# 而 base 里那些 web/爆破工具在 ir 镜像里确实多出来了 —— 这是继承的
# 副作用，接受它是因为"从零构建 + 维护两份文本工具清单"的成本更高，
# 且更容易漂移。**运行参数那一层（`--network none`）才是真正拦得住的地方**，
# 见 `app/ctf2/runner.py` 里沙箱 argv 的构造。
# ══════════════════════════════════════════════════════════

ARG BASE_IMAGE=ctf2-sandbox:latest
FROM ${BASE_IMAGE}

USER root

# ── IR 相对 base **真正多出来**的那一批 ──────────────────
#
# 每一项都对应 `scripts/verify_sandbox_tools.py` 里
# `CATEGORY_REQUIREMENTS["ir"]` 的一行 —— 改这里时两处一起改。
#
#   · tshark：pcap 复盘。base 里已有，这里不重复装（见下方"不装"清单）。
#   · sleuthkit（fls/icat/mactime/bls/fsstat）：磁盘镜像取证。
#     `mactime` 尤其关键 —— 它把 bodyfile 转成时间线，是 `ir-timeline`
#     任务的唯一正经工具。base 里 fls/icat 已有（forensics 分类在用），
#     但 **mactime 是 sleuthkit 的独立可执行文件，单独的包不一定带**。
#     ★ 实测（Kali rolling，2026-10-20）：`mactime` 与 `fls`/`icat` 同在
#       `sleuthkit` 包里（`apt-file search bin/mactime` → `sleuthkit:
#       /usr/bin/mactime`），所以装 sleuthkit 就够，不需要额外的包。
#   · bodyfile 相关：`blkls` 从镜像里提取未分配空间（被删文件常在那）。
#   · `ewf-tools`：EWF/E01 证据文件的读写（`ewfinfo`/`ewfexport`/
#     `ewfverify`）。★ 包名是 **`ewf-tools`**，不是 `libewf-utils`
#     —— 后者在 Kali rolling 上不存在（`E: Unable to locate package
#     libewf-utils`，实测 2026-10-20 构建失败过一次）。`libewf2` 是
#     运行库、`libewf-dev` 是头文件，都不提供 CLI。
#   · `sqlite3`：浏览器历史 / 应用数据库是最常见的"时间线来源"之一，
#     而 base 里没有 CLI（只有 Python 的 sqlite3 模块）。
#   · `p7zip`：base 里是 `p7zip-full`（提供 `7z`），这里不重复。
#   · `xxd` / `hexdump`：base 里有 xxd。
#
# ⚠️ 一行装不完就分两行 —— 单向续行列表里夹注释会让 apt 直接报错
#    （注释行在续行中间是合法的，但**空行**不是）。
RUN apt-get update && apt-get install -y --no-install-recommends \
    sleuthkit \
    ewf-tools \
    sqlite3 \
    # ── 文本/日志主力：**显式声明**，不是"反正 base 里有" ──
    #
    # 这一批（grep / sed / mawk）在 base 里已经有，这里重复声明的
    # 理由与 base 里 `binutils` 那段相同：`CATEGORY_REQUIREMENTS["ir"]`
    # 把它们写成了**契约**，而契约必须能在 Dockerfile 里查到出处。
    #
    # 不写的话，`verify_sandbox_tools.py` 的静态解析会报
    # "提示词要让模型用、镜像里没有" —— 那个报错是**对的**：
    # 当时 base 里它们只是 build-essential 的传递依赖，没人承诺过。
    #
    # 成本 0（apt 见到已装跳过），收益是"IR 靠这些工具"这件事
    # 从"实际如此"变成"声明如此"。
    grep sed mawk coreutils \
    && rm -rf /var/lib/apt/lists/*

# ── Python 侧：日志/时间线处理库 ─────────────────────────
#
# 只加 base 里没有的。pandas 与 python-dateutil 是"把一堆日志行变成
# 可排序时间线"的最短路径 —— 而模型写这段脚本时如果没有 pandas，
# 会退化成一堆手写 dict 排序（更慢、更容易错，且 transcript 更长 =
# 更贵）。
#
# ⚠️ 不装 pytsk3 / python-evtx / dissect：它们要编译，且构建期的
#    失败会让整个镜像不可用。命令行工具（sleuthkit / tshark）已经
#    够用，而"够用且构建得出来"胜过"功能全但构建断掉"——这条与
#    re 镜像里 `ropper`/`filebytes` 那次事故是同一个教训。
RUN pip install --no-cache-dir \
        pandas \
        python-dateutil \
        openpyxl \
        pyyaml

# ── 只读边界 ──────────────────────────────────────────────
#
# base 已经把 `/opt/host-resources` 挂成只读卷、`/work` 是唯一可写位置。
# 这里再显式声明一次，因为 ir 的任务性质让它更容易出现"顺手把证据
# 写到原始目录"的操作 —— 而证据文件被写坏，整个复盘就不可复现了。
VOLUME ["/opt/host-resources"]

USER solver

WORKDIR /work
CMD ["tail", "-f", "/dev/null"]
