# ══════════════════════════════════════════════════════════
# ctf2-sandbox —— 通用解题沙箱（web / crypto / misc / 取证）
# ══════════════════════════════════════════════════════════
#
# ## 为什么**不**复用 strix-sandbox（已定设计决策 1）
#
# strix 的沙箱是为「对客户目标做渗透测试」调的：它按需装工具、
# 允许出网、以 root 跑、可写工作区。而 CTF2 的场景在三处都不同：
#
# | | strix-sandbox | ctf2-sandbox |
# | --- | --- | --- |
# | 跑什么 | 目标上抓回来的东西 | **题目给的**任意附件（含恶意样本） |
# | 出网 | 必须（要打目标） | 默认**不出**（见 runner 的 `--network`） |
# | 工具 | 按 agent 判断现装 | **预装死**，运行时不装任何东西 |
# | 用户 | root | **非 root**（见下） |
#
# 最后一行是这次分家的核心理由：strix 要 root 是因为它要装包、要发原始包；
# 而 CTF2 的解题**不需要** root —— 需要 root 的那部分能力（原始套接字、
# 内核接口）恰好也是**逃逸与提权**最常用的那部分。把 root 拿掉，
# 一个"题目利用了解题者工具链"的漏洞能到达的地方就少一大截。
#
# ## ⚠️ 如实记一条代价：非 root 拿不到 NET_RAW，nmap 的 SYN 扫描不可用
#
# 解题时用 `nmap -sT`（TCP connect，普通用户可跑）即可；要让
# SYN 扫描可用就得 `--cap-add NET_RAW`，那等于把刚收窄的边界又放开 ——
# 为了一个扫描方式的便利不值得。这条写在这里，免得下一个人
# 看到"nmap 报权限错误"就去加 cap。
#
# ## ★ 基础镜像选 kalilinux/kali-rolling
#
# 理由只有一个但是决定性的：**取证与 crypto 的工具生态**。
# `binwalk` / `foremost` / `sleuthkit` / `steghide` 这些在 Debian
# 官方源里要么没有、要么版本很旧，而 Kali 的源里是它们被维护的主场。
# 自己从 Debian 一个个编译是更大的维护负担，且每个包都要自己跟安全更新。
#
# 代价：rolling 意味着**基础镜像会漂移**（同一个 tag 今天和半年前装的
# 东西不一样）。缓解手段是构建时打一个带日期的 tag 并锁进部署配置
# （re 镜像就是这么做的），而不是依赖 `latest`。
# ══════════════════════════════════════════════════════════

FROM kalilinux/kali-rolling

# 非交互：不设的话 apt 会在某些包上停下来等 tzdata 的选择，
# 而构建时的表现是"卡在 100% 不动"，很难看出是在等输入。
ENV DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# ── apt 源（换成国内镜像）──────────────────────────────────
#
# ⚠️ 这一段是**实测**加的，不是"习惯性用国内源"。
#
# 用 kali 官方源构建时，apt 层直接失败：
#
#     E: Failed to fetch http://kali.download/kali/pool/main/c/cmake/cmake_4.3.4-1_amd64.deb
#        File has unexpected size (628 != 16189880). Mirror sync in progress?
#     E: Unable to fetch some archives, maybe run apt update or try with --fix-missing?
#
# 628 字节不是 deb，是 CDN 返回的错误页；而 `http.kali.org` 在这台机器
# 的网络下直接连不上（`Unable to connect to http.kali.org:80`）。
# 换清华源之后同一个 `apt-get update` 一次通过（实测）。
#
# ⛔ 用 **http** 而不是 https：kali-rolling 基础镜像里**没有
#    ca-certificates** —— 它正是下面要装的第一个包，所以 https 源会以
#    "certificate verify failed" 失败。这是个鸡生蛋问题，换 https 解决不了。
#    apt 对清单与包有 GPG 签名校验：http 只降低保密性，不降低完整性。
#
# ★ 用 ARG 而不是把镜像站写死：换机器/换源时
#    `docker build --build-arg APT_MIRROR=mirrors.ustc.edu.cn ...` 即可。
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN rm -f /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list \
    && printf 'deb http://%s/kali kali-rolling main contrib non-free non-free-firmware\n' \
        "$APT_MIRROR" > /etc/apt/sources.list

# ── 系统工具 ──────────────────────────────────────────────
#
# ⚠️ 一条规矩：**每个包都要能说出它服务哪一类题**。
# 一次性把 kali-linux-headless 全装上（那是几个 GB）会让镜像变大、
# 攻击面变宽，而解题时真正用到的可能只有十来个工具。
RUN apt-get update && apt-get install -y --no-install-recommends \
    # ── 底座：几乎所有题都会用到 ──
    ca-certificates curl wget git jq file xxd less vim-tiny \
    python3 python3-venv python3-pip python3-dev \
    build-essential pkg-config libssl-dev libffi-dev \
    # ── 网络 / web 题 ──
    netcat-traditional socat openssl dnsutils iputils-ping \
    nmap whatweb ffuf gobuster dirb sqlmap nikto \
    # ── 取证 / misc 题 ──
    binwalk foremost sleuthkit exiftool p7zip-full unzip \
    tcpdump tshark imagemagick \
    # ── ★ 隐写题的两个常用工具（2026-10-11 实测补）──
    #
    # 这两个是**实测发现的缺口**：清点镜像时 `steghide` / `pngcheck`
    # 都不在，而它们恰好是 Misc 隐写题最常用的两个：
    #   · `steghide` —— JPEG/BMP/WAV 里藏数据的经典工具；
    #   · `pngcheck` —— 看 PNG 块结构是否被改坏（图片隐写的前置步骤）。
    #
    # ⚠️ 讽刺的是 `steghide` 出现在**本文件第 32 行**的注释里，
    #    被列为"选 Kali 源的理由"之一 —— 而它从来没被真的装进来。
    #    这是"文档写了、实现没跟上"的又一例（与 CTF2 的 brief 同类）。
    steghide pngcheck \
    # ── crypto 题（命令行侧）──
    #
    # ⚠️ `pocl-opencl-icd` 这一条是**实测**加进来的，不是预防性添加。
    #
    # hashcat **v7 没有 CPU 后端**：它必须有 OpenCL 平台才能跑，否则连
    # `hashcat -b` 都直接拒："No OpenCL, HIP or CUDA compatible platform
    # found."。而 kali 的 `hashcat` 包**不会**自动带上 OpenCL 运行时。
    #
    # `pocl`（Portable Computing Language）就是给这种"只有 CPU"的场景用的
    # OpenCL 实现。实测（2026-09-30）：
    #   · `libpocl2t64` + `pocl-opencl-icd` 已在（hashcat 的依赖链带进来的），
    #     `/etc/OpenCL/vendors/pocl.icd` 也在；
    #   · 但 **`libOpenCL.so.1`（ICD loader）缺失** —— 于是一切照旧不可用。
    #     那个 loader 由 `ocl-icd-libopencl1` 提供，必须显式装。
    #
    # 不装的后果很隐蔽：hashcat 罢工，agent 退回 `john` 的 OpenMP CPU
    # 后端跑 Office 2010（100000 轮迭代）**1503 p/s** —— 六位数字要十几
    # 分钟，40 个回合根本跑不完，而看起来像"模型不会爆破"。
    hashcat john pocl-opencl-icd ocl-icd-libopencl1 \
    # ── ★ pwn 题 ──
    #
    # 先澄清一件事：pwn 常用的 `checksec` / `ROPgadget` / `cyclic` /
    # `fmtstr_payload` **不需要在这里装** —— 它们都随 `pwntools` 来
    # （见下面 pip 那一段）。实测 `/opt/venv/bin/{ROPgadget,checksec,cyclic}`
    # 都在，`from pwn import ROP, fmtstr_payload` 也正常。在 apt 里另装
    # 一份反而会造出两个不同版本的 `ROPgadget`，报错时不知道该信哪个。
    #
    # 真正缺的是 `patchelf`，而它**没有纯 Python 替代**：
    #   · pwn 题经常附一个**特定版本的 libc**，要求用它本地跑通再打远程。
    #     二进制默认指向镜像里的 ld.so，直接跑会加载错版本 —— 而症状是
    #     "本地打通、远程打不通"（或反过来），比报错难查得多：你看到的
    #     是"exploit 不 work"，而不是"libc 不对"。
    #   · 正确做法是
    #     `patchelf --set-interpreter <题目给的 ld-linux> \
    #               --set-rpath <题目给的 libc 目录> ./chall`
    #     改完再跑，加载的就是题目那套。
    #   · 只用 `LD_PRELOAD` 不行：那是**运行期**替换，改不了 interpreter
    #     本身，对"ld.so 也要换"的场景无效。
    #
    # ⚠️ 2026-10-12 实测：base 和 re 两个镜像里 `command -v patchelf`
    #    都是 MISSING，而 kali 源里有 `0.18.0-1.4`。这是"清点镜像才
    #    发现"的又一例 —— 与 `steghide` 那条同类。
    #
    # ## `gdb`：pwn 题的第二件必需品
    #
    # `gdb.attach(io)` 是 pwntools 调 exploit 的主要入口 —— 没有 gdb 时
    # 它直接抛错，而模型会把这当成"题目有问题"继续换方法硬试，从日志上
    # **看不出根因**（它不会报"缺 gdb"，只是每次调试都失败）。
    #
    # 实测（2026-10-12）：
    #   ctf2-sandbox:latest     gdb → MISSING     ← 修的就是这个
    #   ctf2-sandbox-re:latest  gdb → /usr/bin/gdb
    #
    # **为什么装在 base 而不是"让 pwn 题走逆向镜像"**：逆向镜像比 base
    # 大 1.35 GB（`radare2` + `mono` + `angr`），而 gdb 本身装完约
    # 12 MB、连依赖撑死几十 MB（`libpython3.14*` 这些 base 里本来就有）。
    # 为了一个 12 MB 的工具把每道 pwn 题都推到 4 GB 镜像上，是拿
    # 下载与启动时间换一点磁盘 —— 不划算。
    #
    # 也**不是**"pwn 也算逆向所以归到 re"：那是把"分类名字"当判据，
    # 而真正该看的是"这套题需要哪些工具"。`_REVERSE_CATEGORIES` 里
    # 没有 `pwn`，见 `app/ctf2/runner.py` 的注释。
    patchelf gdb \
    # ── 编译工具：**必须在**，不是可选项 ──
    #
    # ⚠️ 这一条是**实测**加进来的，不是预防性添加。
    #
    # kali-rolling 的 `python3` 现在是 **3.14**，而 `pwntools` 的
    # `unicorn`、`angr` 的 `pyvex` 这些包在 3.14 上**没有预编译 wheel** ——
    # pip 会退回源码构建，而源码构建要 cmake。不加的话构建在
    # `pip install pwntools` 那一步就断：
    #
    #     error: [Errno 2] No such file or directory: 'cmake'
    #     ERROR: Failed building wheel for unicorn
    #
    # `ninja-build` 是同一批包的常用构建后端（cmake 会优先用它）。
    # 顺带一提：这两个工具在**逆向题里本来也用得上**（题目给的源码要现编）。
    cmake ninja-build \
    # ⚠️ `libzbar0` 是**运行期**依赖，apt 不会自动带上：
    #    `pyzbar`（下面是预装的）是个 ctypes 包装，它在 `import` 时
    #    去找系统的 `libzbar.so`。不装的话症状是"二维码题一 import 就报
    #    ImportError: Unable to find zbar shared library" —— 而那是**运行期**
    #    才发现，构建完全成功。
    libzbar0 \
    # ⚠️ `timeout`（coreutils）必须有：runner 用它在容器**内部**
    #    给 shell 工具限时。只靠宿主侧的 subprocess timeout 不够 ——
    #    那杀的是 docker 客户端，容器里的进程会继续跑（见 runner 的说明）。
    coreutils \
    && rm -rf /var/lib/apt/lists/*

# ── Python 解题环境（venv，独立于系统 python）──────────────
#
# ## 为什么用 venv 而不是直接 pip install 到系统
#
# Kali 的 apt 会管一部分 python 包，而 pip 装到系统 site-packages 时
# 会与 apt 记的版本冲突（`pip` 在 Debian 系默认就该被拒 ——
# 报 externally-managed-environment）。venv 让两边彻底分开，
# 也让我们能干净地升级而不用担心动到系统工具。
#
# ## 为什么这些包值得预装（而不是让 agent 现装）
#
# 运行时的沙箱**默认无出网**（见 runner 的 `--network none`），
# 所以"现装"在大多数题上根本不可行。预装清单一句话概括：
# **每一类题最常用的那一个库**。
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir \
        # Pwn / 二进制交互（也常被 misc 与 web 题借用）
        pwntools \
        # crypto：对称/非对称原语、数论、SMT 求解
        pycryptodome cryptography sympy z3-solver gmpy2 \
        # ── ★ 数值计算（2026-10-11 实测补）──
        #
        # 这两个是**实测发现的缺口**：清点镜像时 numpy/scipy 都不在，
        # 而 crypto 与 misc 里相当多的题要它们：
        #   · 矩阵运算（线性代数题、格密码的 LLL 前置）；
        #   · 大数/多项式（RSA 变种、CRC 反推、有限域）；
        #   · 信号与图像（频谱图、音频隐写）。
        #
        # ⚠️ 没有它们时模型只能手写纯 Python 循环 —— 而 sympy 在
        #    大矩阵上慢到会把回合预算吃光，症状看起来像"模型不会做"。
        #
        # ★ 加之前**实测过轮子可用性**（这个项目被同类问题坑过：
        #   `ropper` 依赖的 `filebytes` 用了 Py3.12 移除的 `ast.Str`，
        #   导致 re 镜像构建直接断掉）。实测结果：
        #     numpy-2.5.4-cp314  manylinux_2_28_x86_64  ✅
        #     scipy-1.18.1-cp314 manylinux_2_28_x86_64  ✅
        #   两者在 Python 3.14 上都有预编译轮子，不会走源码构建。
        numpy scipy \
        # 取证 / misc：图像、二维码、常见编码
        pillow pyzbar python-magic \
        # 数据与网络
        requests httpx beautifulsoup4 lxml \
        # 逆向题的脚本侧（re 镜像还会加 angr / capstone）
        capstone \
        # ── ★ 文档/压缩包解密：**实测**加进来的两类题型 ──
        #
        # ## msoffcrypto-tool —— 拿到 Office 口令之后的最后一步
        #
        # 实测（2026-09-30，hashcat 题）：模型**已经把口令爆出来了**
        # （`office2john` 提 hash → `hashcat --increment` 跑出 `9919`），
        # 然后卡在"用口令解开文档"这一步 —— 因为它只能手写 Office
        # Agile Encryption 的 key derivation（SHA1 spin + blockKey +
        # AES-CBC），而那个推导有多个易错分支（spin 轮数、SHA1 再散列
        # 与否、blockKey 常量）。模型连着写了 5 个版本的解密脚本
        # （dec.py…dec4.py、decpkg.py），13 个回合全部算错
        # （`verifier match: False`），最后 40 轮耗尽。
        #
        # 而 `msoffcrypto-tool` 一条命令就够：
        #     msoffcrypto-tool -p 9919 enc.doc dec.doc
        #
        # ⚠️ 它还依赖 `olefile`（解析 OLE 复合文档）—— 一并显式装上，
        #    不要指望它被当依赖自动带进来。
        #
        # ## 其余两个是同一类"最后一公里"的补充
        # · `oletools`：OLE 宏/结构分析（`olevba`、`oledump`），misc 题常用
        # · `pyzipper`：能处理 AES 加密的 zip（标准库 `zipfile` 不支持）
        msoffcrypto-tool olefile oletools pyzipper

# ── ★ 让 **login shell** 也拿到 venv 的 PATH ────────────────
#
# ⚠️ 这一条是**实测**加进来的，而它挡的是一个会让解题直接失败的问题。
#
# 上面的 `ENV PATH` 只对**非 login** shell 生效。而 `runner.shell()`
# 用的是 `bash -lc`（见那个函数的说明），login shell 会读
# `/etc/profile` —— kali-rolling 的 `/etc/profile` **重置** PATH：
#
#     非 login：PATH=/opt/venv/bin:…                     → python3 = /opt/venv/bin/python3 → import pwn ✓
#     login   ：PATH=/home/solver/.local/bin:/usr/local/sbin:… → python3 = /usr/bin/python3
#                                                        → ModuleNotFoundError: No module named 'pwn'
#
# 也就是说：镜像里明明装了 pwntools，而**解题时 import 不到它**。
# 症状看起来像"包没装"，而人会把方向查到 pip 上去。
#
# 放进 `/etc/profile.d/` 是因为 login shell 一定会读那个目录，
# 且这里把 venv 放在 PATH **最前**（盖过 profile 里的默认值）。
# 文件名用 `00-` 前缀：`/etc/profile.d` 是按字典序 source 的，
# 排在前面就没有别的脚本能在它之后把它覆盖掉。
RUN printf 'export PATH="/opt/venv/bin:$PATH"\n' > /etc/profile.d/00-venv.sh \
    && chmod 0644 /etc/profile.d/00-venv.sh

# ── 宿主只读资源挂载点（已定设计决策 2）────────────────────
#
# VOLUME 在这里的作用是**声明**：镜像预期这个路径会被挂载，
# 且未挂载时它也是一个独立的空卷（而不是镜像层里的一份拷贝）。
#
# ## ★ 边界：只挂**数据**，绝不挂可执行文件
#
# 允许的：字典、YARA 规则、预置的解题笔记、离线文档。
# 禁止的：宿主的 `/usr/bin`、`/opt` 下任何可执行目录、
# `/var/run/docker.sock`（挂了等于容器能控制宿主 Docker）、
# `~/.ssh`、任何含凭据的目录。
#
# 这条边界在**运行参数**里也写了一遍（runner 只构造一个固定形式的
# `-v ...:ro`），两处都写是刻意的：镜像这一层拦不住别人用
# `docker run -v` 手挂，而运行参数那一层是唯一能强制的地方。
#
# 只读（`:ro`）不是可选项：可写挂载会让容器里的恶意样本
# **改写宿主上的字典/规则**，而那份数据是被信任的。
VOLUME ["/opt/host-resources"]

# ── 工作区与运行用户 ──────────────────────────────────────
#
# uid/gid 固定 1000：固定值让宿主侧的 `docker cp` 与权限检查可预测，
# 而"用随机 uid"会让每台机器上的文件属主都不一样。
#
# ⚠️ /work 是**唯一**的可写位置（配合 runner 的 `--read-only` +
#    `--tmpfs /work`）。根文件系统只读意味着一个被攻下的解题进程
#    无法持久化 —— 它写的东西随容器一起消失。
RUN useradd --create-home --uid 1000 --shell /bin/bash solver \
    && mkdir -p /work/attachments \
    && chown -R solver:solver /work

WORKDIR /work
USER solver

# 保持存活，等 runner 用 `docker exec` 送命令进来。
#
# ⚠️ 不用 `sleep infinity`：老版本 coreutils 的 sleep 不认 `infinity`，
#    会在容器启动时立刻退出，而症状是"容器起来了但 exec 报
#    No such container"（因为 --rm 已经把它清掉了）—— 这个错误
#    与真正的"Docker 不可用"长得完全一样，会浪费一整轮排查。
CMD ["tail", "-f", "/dev/null"]
