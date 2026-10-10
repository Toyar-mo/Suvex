# ══════════════════════════════════════════════════════════
# ctf2-sandbox-re —— **逆向专用**沙箱（已定设计决策 1）
# ══════════════════════════════════════════════════════════
#
# ## ★ 为什么逆向要单独一个镜像，而不是把工具加进 base
#
# 一句话：**逆向是唯一一类会主动执行攻击者给的代码的题型。**
#
# 别的题里，附件是被**分析**的对象（一张图、一个流量包、一段密文）；
# 而逆向题里，附件就是**要跑起来的东西** —— 你必须在沙箱里执行它
# 才能观察它的行为。所以逆向容器的实际风险面 = "运行一个未知二进制"，
# 而 base 镜像里那些为 web/取证装的东西（浏览器、代理、一大堆解析器、
# 图像库）在这条路径上全都是**额外的攻击面**，一个都不需要。
#
# 收窄的收益是具体的：镜像越小 → 可被未知二进制利用的库越少 →
# 一次成功利用能到达的地方越浅。
#
# 代价如实记：两个镜像各自要跟安全更新，而"同一个 CVE 在两个镜像里
# 的修复时间不同"这种漂移是真实存在的。所以这里**只加不减**：
# re 镜像继承 base 的全部内容，再加逆向工具。不减是因为减掉的东西
# 会让"这道题其实需要 binwalk"这类情况直接卡死，而 re 镜像里
# 多一个 binwalk 带来的额外风险，远小于"题做不了"的确定性损失。
# ══════════════════════════════════════════════════════════

# 用**带日期**的具体 tag 而不是裸 `ctf2-sandbox`：
# 裸 tag 意味着 `docker build` re 镜像时拿到的是"当前 latest"，
# 而两次构建之间 base 变了的话，re 里嵌的是一份**没人记得版本**的底。
# 构建流程：先构建 base 并打 `ctf2-sandbox:<date>`，
# 再把下面这行改成同一个 date。
ARG BASE_IMAGE=ctf2-sandbox:latest
FROM ${BASE_IMAGE}

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    # ── 静态/动态分析主力 ──
    radare2 \
    gdb gdb-multiarch \
    binutils binutils-multiarch \
    ltrace strace \
    # ── 跨架构（ARM / MIPS 的固件与 IoT 题）──
    qemu-user qemu-user-static \
    # ── 脱壳与打包格式 ──
    upx-ucl \
    # ── 反编译辅助：Ghidra 刻意不装 ──
    #     ⚠️ 它要 JDK + 2GB，而 radare2 的 `r2 -A` / `pdc` 与 angr
    #     覆盖了大部分场景。真需要它时用 host-resources 挂一份只读的，
    #     别打进镜像。
    && rm -rf /var/lib/apt/lists/*

# ── Python 侧的逆向库 ─────────────────────────────────────
#
# ⚠️ angr 是这里最重的一个依赖（连 z3、unicorn、claripy 一起装）。
#    它值得：符号执行是"没有明显漏洞但要找输入"这类题的唯一可行路径，
#    而它在运行时装不上（沙箱默认无出网）。
#
# ## ★ 为什么没有 `ropper`（实测删掉的，不是漏了）
#
# ropper 依赖 `filebytes`，而 `filebytes` 的 setup.py 用了 `ast.Str` ——
# 那个 API 在 **Python 3.12 就被移除**了（本镜像的 python3 是 3.14），
# 于是连元数据都算不出来，构建直接失败：
#
#     File "<string>", line 11, in extractMetaInfo
#     AttributeError: module 'ast' has no attribute 'Str'
#     ERROR: Failed to build 'filebytes' when getting requirements to build wheel
#
# 这不是"换个版本就能好"的问题：`filebytes` 最后一个发布版（0.10.2）
# 就是这个状态。而它带来的能力（找 ROP gadget）在本镜像里有**两个**
# 不依赖它的替代：
#   · radare2 的 `/R`（上面 apt 层已装）；
#   · pwntools 的 `ROP` 模块（base 镜像里已装）。
#
# ⛔ 不要为了"凑齐工具清单"把它加回来 —— 加了构建就断，而断了之后
#    整个 re 镜像都没有（连带 radare2 / gdb / angr 一起没有）。
RUN pip install --no-cache-dir \
        angr \
        capstone keystone-engine unicorn \
        lief \
        pyelftools

# ── 反调试相关的便利设置 ──────────────────────────────────
#
# ⚠️ 这里**刻意不做** ptrace 的放宽（`--cap-add SYS_PTRACE` 之类）。
#    在同一个容器里用 gdb 调试自己的进程不需要额外能力；
#    需要 SYS_PTRACE 的场合是"调试别人的进程"，而那不是解题的场景 ——
#    那是逃逸的场景。把这条写下来，免得有人为了"gdb 报 ptrace 错误"
#    就去加 cap（真正的错因通常是 ptrace_scope，而 gdb 对同容器内
#    自己启动的子进程是能用的）。
USER solver

WORKDIR /work
CMD ["tail", "-f", "/dev/null"]
