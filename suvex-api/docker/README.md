# 沙箱镜像

三个镜像，**后两个都依赖第一个**：

```
suvex-sandbox:latest          ← 基础镜像，服务 pwn / web / crypto / misc / forensics
    ├── suvex-sandbox-re:latest   ← 逆向镜像，`FROM suvex-sandbox:latest`
    └── suvex-sandbox-ir:latest   ← 应急响应镜像，`FROM suvex-sandbox:latest`
```

逆向镜像是**加层**而不是并列：它在 base 之上多装 `radare2` / `mono` /
`angr` / `.NET` 那套（体积从 2.8 GB 涨到 4.2 GB）。所以

> **base 变了，re 与 ir 也都要重建。**

`app/kernel/runner.py` 的 `profile_for_category()` 决定一道题走哪个镜像：
`ir-` 前缀的先判（走 ir），`_REVERSE_CATEGORIES` 里的走 re，其余走 base。

## `suvex-sandbox-ir`：应急响应镜像

与 re 的关键差别：**它装的是"读"工具，不是"打"工具**。

| 类 | 工具 |
| --- | --- |
| 时间线 / 取证 | `sleuthkit`（含 `fls` `icat` `mactime` `blkls`）、`libewf-utils`、`foremost`、`binwalk`、`exiftool` |
| 日志 / 文本 | `grep` `sed` `mawk`、`jq`、`xxd`、`p7zip-full`、`unzip`、`file` |
| 流量 / 协议 | `tshark`、`tcpdump` |
| 哈希 / 校验 | `hashdeep`、`sha256sum`（coreutils） |

**刻意不装**（`prompts/ctf2_ir.md` 与提示词层共同保证）：`sqlmap`、`nmap`、
`nikto`、`gobuster`、`ffuf`、`hashcat`、`john`、`metasploit` 之类 —— 复盘场景
里它们没有用，而它们在场会诱导模型去"打一下看看"。

⚠️ 校验器 `scripts/verify_sandbox_tools.py` 的 `CATEGORY_REQUIREMENTS["ir-root-cause"]`
声明了 23 项契约，改 Dockerfile 后**必须**重建镜像再跑一次校验 —— 校验器读的是
Dockerfile 名单，**镜像没重建它照样全 ✅**（这个坑真踩过，见 `docker/README.md`
下面的"改完 Dockerfile 之后必须重建"）。

## 构建

两个 Dockerfile 都**没有 `COPY` / `ADD`**，所以构建上下文可以是空目录。

```bash
# 1) 基础镜像（先）
docker build -f docker/suvex-sandbox/Dockerfile -t suvex-sandbox:latest . 

# 2) 逆向镜像（后；它 FROM suvex-sandbox:latest）
docker build -f docker/suvex-sandbox-re/Dockerfile -t suvex-sandbox-re:latest .

# 3) 应急响应镜像（后；也 FROM suvex-sandbox:latest）
docker build -f docker/suvex-sandbox-ir/Dockerfile -t suvex-sandbox-ir:latest .
```

首次构建约 10–20 分钟（apt 全量 + `unicorn` 要本地编译 —— Python 3.14
上没有它的预编译轮子），其中导出镜像层那一步在大盘上也占一两分钟。

### 想镜像小一点、构建快一点

构建完成后可以合并成一次（两条 `-t`），但**别**用 `--squash`：
那会把层叠信息压平，之后 `docker history` 就再也看不出哪个工具是哪层
装的了 —— 这个仓库排查"工具缺失"靠的就是能一层层往回看。

## ★ 改完 Dockerfile 之后**必须**重建

这一条不是流程洁癖，是踩过的坑：

> `patchelf gdb` 在 `2de97ce`（2026-10-18）就写进 Dockerfile 了，而镜像
> 是 2026-10-12 构建的。于是仓库里的名单是对的、**实际镜像里两个命令都
> 不存在**，而所有静态检查都报「pwn ✅」。模型那边每次 `gdb.attach()`
> 都失败，日志上却不出现"缺 gdb"。

`scripts/verify_sandbox_tools.py` 默认只看 Dockerfile，**天生看不见
这一类**。所以：

```bash
python3 scripts/verify_sandbox_tools.py            # 查「名单对不对」
python3 scripts/verify_sandbox_tools.py --probe    # ★ 查「名单和产物一不一致」
```

`--probe` 会对真实镜像跑一遍 `command -v`（需要 Docker / 镜像在本地）。
它报「声明有、镜像里没有」时，修法只有一条：**重建**。

⚠️ 一次构建的副作用：kali-rolling 是滚动发行版，重建会把 apt/pip 包
顺带升到当天版本。所以"重建后行为变了"未必是你改的那一行造成的 ——
`git log docker/` 与当天的 kali 索引都可能有关。

## 清理镜像时不要用 `-a`

`docker system prune -af` 会删掉**所有**没有运行中容器引用的镜像，
包括带 tag 的。这个仓库因此丢过一次 `suvex-sandbox-re:latest`（4.2 GB、
重建 20 分钟）。

要回收空间就只删悬空层：

```bash
docker image prune          # 只删 <none>:<none>
docker builder prune        # 只删构建缓存
```

## 验证清单

重建之后按这个顺序过一遍：

```bash
# 1) 名单 vs 产物（会真去镜像里 command -v）
python3 scripts/verify_sandbox_tools.py --probe

# 2) base 里那几个"装了但容易装不全"的
docker run --rm suvex-sandbox:latest sh -c \
  'command -v patchelf gdb one_gadget seccomp-tools'

# 3) re 镜像（它的工具集 = base ∪ 自己）
docker run --rm suvex-sandbox-re:latest sh -c \
  'command -v r2 radare2 rabin2 monodis ildasm'

# 4) 端到端：跑一遍基线（默认就是全部 10 道题）
python3 scripts/ctf2_baseline.py run --out /tmp/baseline.json
```

第 4 步才是真正的验收 —— 前三步证明"工具在"，它证明"题能做"。

## 为什么 `ropper` 不在里面

它的依赖 `filebytes` 的 `setup.py` 用了 Python 3.12 已移除的 `ast.Str`,
装到一半整个构建就断了。功能上 `ROPgadget`（pwntools 自带）已经覆盖
"找 gadget"这件事，所以这里的选择是**不带 ropper**，而不是"忘了"。

同理，`one_gadget` / `seccomp-tools` 是 **Ruby gem** 而不是 pip 包 ——
它们没有可用的 Python 替代，所以镜像里 `apt install ruby ruby-dev`
之后再 `gem install`。
