# ══════════════════════════════════════════════════════════
# suvex-sandbox —— 通用解题沙箱（web / crypto / misc / 取证）
# ══════════════════════════════════════════════════════════
#
# ## 为什么**不**复用 strix-sandbox（已定设计决策 1）
#
# strix 的沙箱是为「对客户目标做渗透测试」调的：它按需装工具、
# 允许出网、以 root 跑、可写工作区。而 CTF2 的场景在三处都不同：
#
# | | strix-sandbox | suvex-sandbox |
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
    # ⚠️ `grep` / `sed` / `mawk`（提供 `awk`）**必须显式写出来**，理由和
    #    下面 `binutils` 那段一样：它们经由 build-essential 的依赖链其实
    #    早就装上了，实测 `suvex-sandbox:latest` 里 `/usr/bin/grep` 等都在。
    #    但"没写进 Dockerfile"= 没人知道这是承诺而不是巧合。
    #
    #    v0.41 加 IR 场景包时这个缺口第一次显形：`prompts/ctf2_ir.md`
    #    §三 决策表整段都靠 grep / awk / sed 过滤日志，而
    #    `scripts/verify_sandbox_tools.py` 静态解析 apt 列表时把这三个
    #    判成"提示词要让模型用、镜像里没有"——**检查是对的，缺口是真的**。
    #
    #    成本 0（apt 见到已装会跳过），收益是这条依赖从此可见。
    #    注意 Debian 的 `awk` 由 `mawk` 提供，不是叫 `awk` 的包。
    grep sed mawk \
    # ⚠️ `binutils` 必须有：`strings` / `objdump` / `readelf` / `nm` 在
    #    misc 与取证题里是第一步（`strings` 找线索、`objdump` 看汇编）。
    #
    #    它们**经 gcc / gdb 的依赖链其实已经装上了** —— 实测
    #    `suvex-sandbox:latest` 里 `/usr/bin/strings`、`/usr/bin/objdump`
    #    都在。但"没写进 Dockerfile"意味着**没人知道这是一条依赖而
    #    不是巧合**：`scripts/verify_sandbox_tools.py` 静态解析 apt
    #    列表时，就把 `strings` 判成了"提示词要让模型用、镜像里没有"。
    #
    #    显式写出来的成本是 0（apt 看到已装会跳过，不增加下载量），
    #    收益是这条依赖从此是**承诺**而不是运气 —— 哪天 gcc 换了依赖
    #    链、或者有个镜像不再装 gcc，这里会立刻在检查里露馅。
    binutils \
    # ── 网络 / web 题 ──
    netcat-traditional socat openssl dnsutils iputils-ping \
    nmap whatweb ffuf gobuster dirb sqlmap nikto \
    # ── ★ v0.87：按 CyberStrikeAI 的工具清单补齐（13 个，全部 apt 可装）──
    #
    # ## 为什么补这批
    #
    # 对照 CyberStrikeAI 的 90 个工具配方实测：我们的沙箱原本只能覆盖
    # **21/76** 个命令名。其中一批是 Web/审计的常用件，而它们的缺失
    # 属于"提示词教模型做的事、镜像里没工具"那类缺口 ——
    # 本项目为这类问题已经栽过五次（见 `verify_sandbox_tools.py` 文件头）。
    #
    # ## 每个都核过 apt 来源（Kali 源，实测 `apt-cache policy`）
    #
    #   · `nuclei`      —— 模板化漏洞验证，此前**完全没有**（镜像里查不到）
    #   · `feroxbuster` —— 递归内容发现，比 gobuster 更擅长深目录
    #   · `dirsearch`   —— 目录扫描（Python 实现，字典更偏 Web）
    #   · `wafw00f`     —— WAF 识别，决定后续要不要 `--tamper`
    #   · `arjun`       —— HTTP 参数发现（隐藏参数是很多注入的入口）
    #   · `dalfox`      —— XSS 扫描
    #   · `xsser`       —— XSS 验证（与 dalfox 互补）
    #   · `masscan`     —— 大网段快速端口扫描（nmap 慢一个量级）
    #   · `rustscan`    —— 单机全端口扫描（速度优势）
    #   · `hydra`       —— ⚠️ 见下方专门说明
    #   · `radare2`     —— 反汇编/逆向主力（re 镜像已有，base 一直缺）
    #
    # ⚠️ `ROPgadget` **不在这一批**：它随 `pwntools` 来（见下面 pip 那段，
    #    以及第 181 行那段说明）—— 再 apt 装一份会造出两个版本，
    #    报错时不知道该信哪个。第一版误加了，`apt-get install -s` 直接
    #    「Unable to locate package ropgadget」把它挡住了。
    #
    # ⚠️ **`hydra` 的边界**：提示词明令禁止口令爆破（`kernel_system.md`
    #    的「不爆破」纪律）。装它是因为**用户明确要求**，且审计/自查场景
    #    （对自己有权的系统做弱口令检查）确实会用；但**提示词里不教模型用它
    #    做爆破**，也不进 `CATEGORY_REQUIREMENTS` —— 那个契约表达的是
    #    "该分类需要什么"，把它列进去等于把"装了"当成"该用"。
    nuclei feroxbuster dirsearch wafw00f arjun dalfox xsser \
    masscan rustscan hydra radare2 \
    # ── ★ ProjectDiscovery 侦察三件套 + HTTP 断言（2026-10-26 补）──
    #
    # ## 为什么补：**我们自己的 skill 一直在教模型跑这些命令**
    #
    # 这不是"上游 strix 的 skill 引用了没装的东西"，而是**我方既有缺陷**。
    # 清点时按名字 grep 我方 `prompts/skills/`，命中数：
    #
    #   · `naabu`     3 篇（含 `reconnaissance/asset_discovery.md:61`）
    #   · `subfinder` 2 篇（含 `asset_discovery.md:59` 的完整命令行示例）
    #   · `katana`    1 篇（`methodology/attack-surface-recon.md`）
    #
    # `asset_discovery.md:59` 逐字写着：
    #   `subfinder -d example.com -all -recursive -silent -oJ -o subs.jsonl`
    # 而镜像里根本没有 `subfinder` 这个命令 —— 模型照着做只会拿到
    # `command not found`，然后**误以为是目标的问题**，转而去试别的方法。
    # 这比"没装工具"更糟：它消耗的是模型的排查预算。
    #
    # ## 每个都核过 Kali 源（`pkg.kali.org/pkg/<name>` 实测 HTTP 200）
    #
    #   · `katana`    —— 爬虫。`-jc`（解析 JS 里的端点）是它相对
    #                    `gospider`/`dirb` 的独有价值：现代 SPA 的路由全在 JS 里
    #   · `naabu`     —— 端口扫描。相对 nmap 的优势是**快**（`-top-ports 100 -verify`）
    #   · `subfinder` —— 被动子域枚举（证书透明日志等多源）。**不发包给目标**，
    #                    纯被动，比字典爆破静默
    #   · `hurl`      —— HTTP 请求链 + 响应断言，可复现、可 review。
    #                    与 `run_shell` + `curl` 的区别是**断言是一等公民**
    #                    （`[Asserts]` 段），不用自己写 `grep` 判断
    #   · `python3-hypothesis` —— 属性测试库（差分测试/找边界）。
    #                    ⚠️ 这是**库**不是 CLI，名字和上游 skill 撞车，
    #                    见下面 `tooling/hypothesis.md` 的说明
    #
    # ## ⚠️ 不装的两个，以及为什么
    #
    #   · `semgrep` —— Kali 源里**没有**（`pkg.kali.org/pkg/semgrep` → HTTP 404）。
    #     只能 pip 装，wheel 68.5 MB（有 cp314 轮子，能装）。
    #     但它**运行时默认要从官方规则注册表拉规则**，而容器是 `--network none`
    #     或 iptables 白名单 —— 拉了会超时。要装就得同时 vendor 一份规则集，
    #     成本远大于收益。**当前决定：不装，并在 skill 里写明替代路径。**
    #   · `amass` —— 我方 `asset_discovery.md` 提到 1 次，但它是子域枚举里
    #     最慢的一档（默认跑很久），而 `subfinder` 已覆盖同一需求。
    katana naabu subfinder hurl \
    python3-hypothesis \
    # ── ★ httpx：那个"两个同名工具"的坑（2026-10-26 补）──
    #
    # ## 坑在哪
    #
    # 镜像里早就有 `httpx` —— 但那是 `pip install httpx`（第 481 行
    # `requests httpx beautifulsoup4 lxml`），**Python 的 HTTP 客户端库**，
    # 没有 CLI 入口。而我方 `asset_discovery.md`、`attack-surface-recon.md`
    # 教的却是 `httpx -l hosts.txt -sc -title -td -tls-grab` ——
    # ProjectDiscovery 的 httpx **命令行工具**。两者只是恰好同名。
    #
    # 所以 `which httpx` 会失败，`python -c "import httpx"` 却成功 ——
    # 这正是最难自查的一类缺口：按名字 grep 会以为"装过了"。
    #
    # ## Kali 源里怎么解决的
    #
    # Kali 把 PD 的 httpx 打包成 **`httpx-toolkit`**（v1.9.0-0kali2），
    # 但**装出来的二进制仍叫 `/usr/bin/httpx`** —— 包名避让了 Python 的
    # `httpx`，二进制名没有。实测 `www.kali.org/tools/httpx-toolkit/`
    # 页面的 "Packages and Binaries" 只列 `/usr/bin/httpx`。
    #
    # 也就是说：装 `httpx-toolkit` 之后
    #   · `httpx -l hosts.txt -sc -title`  → PD 的 CLI（skill 教的用法）
    #   · `python3 -c "import httpx"`       → pip 的库（不变）
    # 两者并存，不冲突 —— pip 那个只进 site-packages，不占 /usr/bin。
    httpx-toolkit \
    # ── ★ 密钥/ secrets 扫描 + 爬虫（2026-10-26 补，用户指令"直接全装"）──
    #
    # 这三个是 skill 里提到、而镜像里没有的：
    #   · `gitleaks`   —— git 仓库 / 目录树里的密钥泄漏扫描（正则+熵）；
    #   · `trufflehog` —— 同上，但会**验证**密钥是否仍然有效（多打一次请求
    #                     确认活不活），这是它相对 gitleaks 的核心增量；
    #   · `gospider`   —— 快速爬链接，`tooling/katana.md` 里作为替代方案提到。
    #
    # 为什么值这个体积：CTF 和真实渗透里"源码/仓库泄露"都靠这类工具先捞。
    # 我方原本一个都没有 —— `semantic_confusion.md`、`source_aware_sast.md`
    # 教模型用 trufflehog/gitleaks，但 `which trufflehog` 是空的。
    #
    # `bandit` 是 Python 静态审计（`django.md` 提到）。Kali 里包名是
    # `python3-bandit`（`bandit` 是其依赖别名），装 `bandit` 会连带 Python 版。
    gitleaks trufflehog gospider \
    bandit \
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
    #   suvex-sandbox:latest     gdb → MISSING     ← 修的就是这个
    #   suvex-sandbox-re:latest  gdb → /usr/bin/gdb
    #
    # **为什么装在 base 而不是"让 pwn 题走逆向镜像"**：逆向镜像比 base
    # 大 1.35 GB（`radare2` + `mono` + `angr`），而 gdb 本身装完约
    # 12 MB、连依赖撑死几十 MB（`libpython3.14*` 这些 base 里本来就有）。
    # 为了一个 12 MB 的工具把每道 pwn 题都推到 4 GB 镜像上，是拿
    # 下载与启动时间换一点磁盘 —— 不划算。
    #
    # 也**不是**"pwn 也算逆向所以归到 re"：那是把"分类名字"当判据，
    # 而真正该看的是"这套题需要哪些工具"。`_REVERSE_CATEGORIES` 里
    # 没有 `pwn`，见 `app/kernel/runner.py` 的注释。
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
    # ── ★ 可选增强：one_gadget / seccomp-tools 的运行时（2026-10-19 补）──
    #
    # 这两个是 **Ruby gem**（不是 pip 包），apt 里只装解释器，
    # gem 本身在下面的 `RUN gem install` 里装。
    #
    # ## 为什么值得占一层
    #
    # pwn 题的两种常见形态直接对应它们：
    #
    # · **seccomp 沙箱**：题目放行 read/write 而禁掉 execve，要先
    #   `seccomp-tools dump ./chall` 看清哪些 syscall 能过，才能决定
    #   写 ORW 链而不是 getshell。没有这个工具，模型只能靠 `objdump`
    #   反汇编 `prctl` / `bpf` 的调用点去**猜**规则 —— 而猜错规则的
    #   症状是"ROP 链跑通了但没拿到 shell"，看起来像利用写错了。
    #
    # · **one_gadget**：栈溢出只够跳一次时，从 libc 里找一个
    #   "直接 execve('/bin/sh')" 的地址。手算要找 __malloc_hook 附近的
    #   固定约束（`rsp+0x40` 为 NULL、`rdx` 为 NULL 等），一个条件
    #   不满足就白写一次利用。
    #
    # ## 为什么用 ruby 而不是找 Python 替代
    #
    # 两者都**没有**可用的 Python 替代：`seccomp-tools` 的核心是
    # BPF 反汇编 + ptrace 注入，`one_gadget` 要扫 libc 里的
    # lea/mov 指令序列去反推约束。它们都是 Ruby 生态里唯一的实现。
    #
    # ⚠️ 实测（2026-10-19）：kali-rolling 的 ruby 是 **3.3.8**，
    #    `gem install one_gadget seccomp-tools` 两个都能装成
    #    （one_gadget-2.1.1 / seccomp-tools-1.7.1）。它们落在
    #    `/usr/local/bin`，而 `USER solver` 的默认 PATH 含那个目录 ——
    #    已实测 `su solver -s /bin/sh -c 'command -v one_gadget'` 命中。
    #
    # ⚠️ seccomp-tools 带 **native extension**，所以 `make` 必须在
    #    装它之前就位（本文件上面的 `build-essential` 提供）。
    ruby ruby-dev \
    # ── ★ 无头浏览器（2026-10-26 补）──
    #
    # ## 为什么补：这是"提示词教模型做的事、镜像里没工具"的第六例
    #
    # 本项目为这同一个结构性缺口已经栽过五次（`verify_sandbox_tools.py`
    # 文件头列了清单：steghide、hashcat、.NET、msoffcrypto-tool、patchelf）。
    # 这次清点镜像时发现的是：**三个镜像里一个浏览器都没有** ——
    # 而 `docker/suvex-sandbox-re/Dockerfile:17` 那句注释一直写着
    #
    #     "base 镜像里那些为 web/取证装的东西（浏览器、代理、...）"
    #
    # **那句话是假的**：base 里 grep 不到 chrom/firefox/webkit/playwright
    # 任何一个。注释里的"已有"从来没被真的装进来 —— 与 `steghide` 被写进
    # 注释却不装（见本文件第 32 行那处讽刺）是同一个形态。
    #
    # ## 为什么值得占这一层（约 350 MB）
    #
    # Web 题里有一整类**必须执行 JS 才看得见**的东西：
    #   · SPA / 前端路由（curl 只拿到 `<div id="app"></div>`）；
    #   · 客户端加密后再提交的口令、token 生成；
    #   · XSS 是否真的触发（要真渲染才知道 payload 有没有执行）；
    #   · 靠 JS 做的跳转与 `document.write` 拼出来的隐藏端点。
    # 这些用 `curl` / `http_request` **原理上看不到**，不是"慢一点"而是
    # "拿不到"。没有浏览器时模型只能猜端点，然后反复 404。
    #
    # ## 为什么是 `chromium` 而不是 puppeteer/playwright
    #
    # puppeteer 要在**运行时**下载一份 Chromium（`~/.cache/puppeteer`），
    # 而沙箱默认 `--network none` —— 那会在第一次调用时就失败，且失败
    # 信息是"下载超时"，看起来像网络问题而不是"镜像没预装"。
    # `apt install chromium` 把浏览器本体打进镜像，运行时零下载。
    #
    # `chromium-driver`（chromedriver）**不装**：那是 Selenium 用的，
    # 而我们是直接命令行驱动（`--headless=new --dump-dom` / `--screenshot`），
    # 用不到 WebDriver 协议，装了只是多开一个攻击面。
    #
    # ⚠️ 用户是 `solver`（uid 1000，见文件末的 `USER solver`）。
    #    Chromium 拒绝以 **root** 运行（除非 `--no-sandbox`），
    #    而我们**不给** `--no-sandbox`：容器本身就是隔离边界，
    #    再关掉 Chromium 自己的沙箱是拿纵深换省事。
    #    非 root 下不需要那个开关 —— 这也是坚持不用 root 跑的原因。
    #
    # ⚠️ 根文件系统是 `--read-only`（见 runner 的 `_argv`），所以
    #    Chromium 的 profile / cache 目录**必须**指到 `/work` 下的
    #    tmpfs，否则启动时报
    #    "Failed to create a ProcessSingleton for your profile directory"。
    #    那一句看起来像"权限问题"，实际是"只读根"—— 见 `browser` 工具的
    #    `--user-data-dir`。字体同理：不装 `fonts-liberation` 时截图里
    #    全是方块，而模型会以为自己拿到了乱码页面。
    chromium \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# ── ★ agent-browser（2026-10-26 补）：把 Chromium 变成模型能操作的页面 ──
#
# ## 为什么装它，而不是自己写 CDP 驱动
#
# 上一轮（v0.102）我先写了 `app/kernel/browser_driver.py`：一个纯标准库
# 手写 CDP 客户端。写完发现两件事：
#
#   1. 那份代码在**本机无法执行**（宿主没有 docker），也就是说它交付时
#      一次都没跑过 —— 而"没跑过的浏览器驱动"必定有 bug（CDP 的会话
#      初始化、target 附着、事件流都有非平凡时序）。
#   2. `agent-browser` 已经在**本机实测跑通**（见下），且它做的事
#      正是我们需要的：把可访问性树压成 `- button "Sign in" [ref=e2]`
#      这种 200-400 token 的可操作文本。
#
# 所以选择**用它**、删掉手写驱动。判断依据是"哪个真的被验证过"，
# 不是"哪个是我们自己写的"。
#
# ## 它是什么（三个容易搞错的点，都已实测确认）
#
# 1. **它不需要 node。** 这是个反直觉的结论，值得写下来：npm 包
#    `agent-browser` 里的 `bin/agent-browser.js` 只是个 wrapper，它会
#    `spawn` 同目录下的 `agent-browser-linux-x64` —— 而那个是
#    **Bun 编译的独立 ELF**（`file` 报 "ELF 64-bit LSB pie executable"，
#    动态依赖只有 libc/libdl/libm/libpthread）。实测 `env -i`（清空环境）
#    直接执行它能输出 `agent-browser 0.26.0`，而走 wrapper 则报
#    `env: 'node': No such file or directory`。
#    → 所以下面**不装 node**，只取那个二进制。少一个运行时依赖，
#      就少一个失败点。
#
# 2. **Chrome 的沙箱由它自己关。** 二进制里有 3 处写死的 `--no-sandbox`
#    提示串，其中一条明写 "(required in containers, VMs, and some Linux
#    setups)"。这条非常重要：它意味着**为 Chrome 沙箱去给容器加
#    `SYS_ADMIN` 是白付代价** —— Chrome 根本不会启用那个沙箱。
#    证据对照：strix 的 `strix/runtime/docker_client.py:250-257` 给容器
#    加的 capabilities 只有 `NET_ADMIN`/`NET_RAW`（为 nmap 的原始套接字），
#    并没有为 Chrome 加 SYS_ADMIN，而它的 Chrome 在非 root（`pentester`）
#    下正常工作。我们据此**不给容器加任何 capability**。
#
# 3. **它自带技能文档**（`agent-browser skills get core`，15971 字符）。
#    这是它相对"裸 CDP"最大的价值：说明书与 CLI 版本永远匹配。
#
# ## 版本与完整性
#
# 钉死 0.26.0 并校验 sha256 —— 版本与 strix 一致（它的 Dockerfile 里
# 也是 `npm install -g agent-browser@0.26.0`）。用 `curl` 取 tarball 再
# 手工解包，而不跑 `npm install`：既省掉 node，也省掉 postinstall 的
# 联网下载（postinstall 会去 GitHub releases 抓二进制，构建期多一个
# 外部依赖点）。
#
# ⚠️ 镜像里只放 **linux-x64** 那一个二进制（10.9MB），tarball 里另外 6 个
#    平台的文件（~70MB）不落盘。
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
#: 空闲回收：与 strix 同值。模型跑完一轮不会主动 close，靠它收尸。
ENV AGENT_BROWSER_IDLE_TIMEOUT_MS=180000
#: profile 必须落在可写处（根是只读的）。`/work` 是 tmpfs，见 runner 的 `--tmpfs`。
ENV AGENT_BROWSER_PROFILE=/work/.browser/profile
#: socket 目录同理；默认在 $XDG_RUNTIME_DIR 或 /tmp，都要显式指到可写处。
ENV AGENT_BROWSER_SOCKET_DIR=/work/.browser/sock
ENV AGENT_BROWSER_SCREENSHOT_DIR=/work/.browser/shots
#: ★ 容器里必须带 `--no-sandbox`（见上面第 2 点）。写进 ENV 而不是让
#:   模型每次自己加：模型不知道这是容器的约束，而漏了它的症状是
#:   "Chrome 静默崩溃"，看起来像镜像坏了。
ENV AGENT_BROWSER_ARGS="--no-sandbox,--disable-dev-shm-usage,--disable-gpu"

RUN set -eux; \
    # ⚠️ 架构门：只在 x86_64 上装。arm64 的二进制 tarball 里也有，
    #    但本项目三个镜像目前都是 amd64。写死这条以防"在 arm 机器上
    #    build 出没有浏览器的镜像"这种静默降级。
    test "$(dpkg --print-architecture)" = "amd64" \
      || { echo "agent-browser: 本段只处理 amd64，arm64 需换二进制名" >&2; exit 1; }; \
    mkdir -p /opt/agent-browser; \
    curl -fsSL -o /tmp/ab.tgz \
      "https://registry.npmjs.org/agent-browser/-/agent-browser-0.26.0.tgz"; \
    # 校验 sha256（钉死字节）。npm 公布的 integrity 是 sha512-base64，
    # 也可以直接用，但 sha256 在 Dockerfile 里更省事且足够。
    printf '%s  %s\n' \
      "8a48cf4110d7dc2c12c1c4d6d25e0babdfa31604b537f05df0dc835fe2f854bf" /tmp/ab.tgz \
      | sha256sum -c -; \
    tar -xzf /tmp/ab.tgz -C /tmp \
      package/bin/agent-browser-linux-x64 \
      package/LICENSE \
      package/skill-data \
      package/skills; \
    install -m 0755 /tmp/package/bin/agent-browser-linux-x64 /opt/agent-browser/agent-browser; \
    ln -sf /opt/agent-browser/agent-browser /usr/local/bin/agent-browser; \
    # 技能文档跟着二进制走：CLI 的 `skills` 子命令按 ../skill-data 推算路径，
    # 所以两个目录必须同父级。
    mv /tmp/package/skill-data /opt/agent-browser/skill-data; \
    mv /tmp/package/skills /opt/agent-browser/skills; \
    cp /tmp/package/LICENSE /opt/agent-browser/LICENSE; \
    rm -rf /tmp/package /tmp/ab.tgz; \
    # ⚠️ 构建期自检：这一步失败就等于"装了但用不了"，必须 build 时就炸。
    #    不用 `doctor`：实测它会挂住（本机跑 25s 超时都没退出来），
    #    而 `--version` 足够证明"二进制能在这台机器上执行"。
    agent-browser --version; \
    test -d /opt/agent-browser/skill-data/core

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

# ── ★ Ruby 生态的两个 pwn 工具（2026-10-19 补）─────────────
#
# `seccomp-tools` 与 `one_gadget` 各自解决什么、以及为什么没有 Python
# 替代，写在上面 apt 那一段 `ruby ruby-dev` 的注释里。这里只记**取舍**。
#
# ## 为什么明知道提示词没点名也要装
#
# 提示词第四节承诺的 pwn 工具是 `checksec / cyclic / ROP / gdb / patchelf`
# —— 那是"能开工"的下限。这两个补的是下限之上、但**很常见**的场景
# （seccomp 题、只能跳一次的溢出题）。
#
# 反过来，`ropper` 是**明确不加**的：它的依赖 `filebytes` 用了
# Python 3.12 已移除的 `ast.Str`，会让整个构建断掉；而功能上
# `ROPgadget`（pwntools 自带，已在）已经覆盖。区别在于
# **能不能装成**，而不在于想不想要 —— 这条边界在 re 镜像的
# Dockerfile 里也记了一遍。
#
# ## 为什么要 `--no-document`
#
# 不生成 ri/rdoc。那是给"在本机查 gem 文档"的人用的，而容器里没有
# 这种使用场景，白占几十 MB。
#
# ⚠️ 这一层会**访问 rubygems.org**（apt 那层访问的是镜像源）。
#    两者都不通时构建会失败 —— 这是刻意的：宁可构建报错，
#    也不要镜像起来了而工具是空的。
RUN gem install --no-document one_gadget seccomp-tools

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
    # ⚠️ `/work/.browser/*` 的三个子目录是给 agent-browser 用的
    #    （profile / socket / screenshot，见上面的 ENV）。**必须在镜像里
    #    就建好并 chown**，因为运行时 `/work` 是 tmpfs，一挂上来
    #    镜像里的内容就被盖掉了 —— 那时目录不存在、属主是 root，
    #    而 `AGENT_BROWSER_PROFILE=/work/.browser/profile` 写不进去的
    #    报错是 "Failed to create a ProcessSingleton"，看不出是属主问题。
    #    所以真正的创建动作在 runner 启动容器之后（`_argv` 的注释里
    #    写了 /work 必须 uid=1000），这里建一份是给"手动 docker run
    #    调试"用的，两者都留着。
    && mkdir -p /work/attachments /work/.browser/profile /work/.browser/sock /work/.browser/shots \
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
