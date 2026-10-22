"""CTF2 解题方法卡：从一次解题里提炼可跨题复用的知识（v0.32）。

## ★ 这个模块的**唯一**硬约束：注入内容绝不能带 flag

卡片会被拼进**另一道题**的 prompt。一旦带上旧 flag，就变成
"把 A 题的答案泄漏到 B 题的上下文里" —— 而 B 题的模型完全可能
把它当成 B 题的答案交上去。

所以这里的每个字段都过一遍 `sanitize_for_reuse`，而那个函数是
**白名单式**的：不是"删掉我知道的敏感词"，而是"只留下我确认安全的
形状"。两者的区别在遇到没见过的东西时显现 —— 黑名单会漏，白名单会挡。

## 为什么不直接塞 transcript

transcript 动辄 60–115 KB，绝大部分是工具原始输出。塞回去会把预算
吃光，然后模型又开始"忘记题目要求" —— 那把"复用"变成了"更糟的重来"。
所以这里只提炼**方法**（几步梗概 + 工具名 + 死路），不搬原文。
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache

logger = logging.getLogger("suvex.ctf2")

# ── 上限（都要能说出为什么）────────────────────────────────

#: `approach` / `dead_ends` 各自的字符上限。
#:
#: 600 字 ≈ 中文 300 字左右，够说清"用什么思路、卡在哪"，而
#: 两张卡 + 题面 + 附件清单拼起来仍在 prompt 的合理范围内。
#: 再长就会挤占解题本身需要的上下文 —— 那正是 `digest` 要避免的事。
MAX_NOTE_CHARS = 600

#: 一次召回最多注入几张卡。
#:
#: 3 张是"够用且不喧宾夺主"的量级：同类题的方法往往高度重合，
#: 给 10 张只会让 prompt 里塞满重复信息，反而稀释了当前题的重点。
MAX_NOTES_IN_PROMPT = 3

#: `tools` 字段最多留几个工具名。
MAX_TOOLS = 12


#: **文件扩展名**。用来把"长 hex 的附件名"从靶机 hostname 里区分开。
#:
#: ⚠️⚠️ 这是实测补的（2026-10-13）。`_TARGET_HOSTNAME` 的**形状**同时
#:      匹配两种东西，而它们的处理方式**必须相反**：
#:
#:        靶机：  dfc05019f1fa0d78efe8f583.tcp-ctf2.dasctf.com   ← 要掩
#:        附件名：9f0ee5dcb985a3ea70e474a8a0c6d3735ac879553a92af77771e4246c770adee.pcap
#:                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ ← 不能掩
#:
#:      两者都是"一长串 hex 开头"，唯一可靠的差别是**末标签**：
#:      靶机是域名后缀（`com` / `net`），附件是文件扩展名（`pcap` / `zip`）。
#:
#:      附件名是平台按内容 SHA256 生成的（见 `attachments.py`）。
#:      误掩的后果是卡片里出现：
#:
#:        unzip -o /work/attachments/<target>      ← 照抄会找不到文件
#:
#:      而**"这道题给的是个 zip"本身是可复用的信息**。
_FILE_EXTS = frozenset(
    {
        # 压缩/打包
        "zip", "gz", "tgz", "bz2", "xz", "7z", "rar", "tar", "jar", "war",
        # 可执行/二进制
        "exe", "elf", "bin", "so", "dll", "out", "o", "a", "class", "pyc",
        "wasm", "apk", "iso", "img", "dmp", "msi", "sys",
        # 代码/文本
        "py", "cs", "c", "cpp", "h", "hpp", "sh", "bat", "ps1", "pl", "rb",
        "go", "rs", "java", "php", "jsp", "asp", "html", "htm", "xml",
        "json", "yml", "yaml", "toml", "ini", "cfg", "conf", "log", "md",
        "txt", "csv", "tsv", "sql", "db", "sqlite", "pem", "key", "crt",
        "csr", "p12", "pfx", "asc", "gpg", "il",
        # 文档
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "rtf",
        # 媒体/取证
        "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "ico",
        "svg", "mp3", "mp4", "avi", "mov", "wav", "flac", "pcap", "pcapng",
        "cap", "evtx", "raw", "vmem", "ttf", "woff", "woff2", "eot",
    }
)


def _mask_target_host(match: re.Match[str]) -> str:
    """把 hostname 掩成 `<target>`，**除非它是文件名**。

    见 `_FILE_EXTS` 的说明：附件名与靶机 hostname 形状相同，
    只有"末标签是不是扩展名"能区分。
    """
    host = match.group(0)
    if host.rsplit(".", 1)[-1].lower() in _FILE_EXTS:
        return host
    return "<target>"


#: 靶机**裸 hostname** 的判据：首标签是一长串 hex，后面还跟着域名。
#:
#: 平台给每道题分配的环境是 `<24位hex>.tcp-ctf2.<赛事域>` 这种形状
#: （见 `sanitize_for_reuse` ②″ 里那份实测数据）。这里只认这个形状，
#: 因为更宽的判据会把 `Image.open` / `gui.il` / `easyre.exe` 一起掩掉。
#:
#: ⚠️ 首标签要求 ≥16 位 hex：短 hex 标签（`abc.example.com`）可能是
#:    正常的文档域名，掩掉会误伤；而 16 位以上几乎只可能是自动生成。
_TARGET_HOSTNAME = re.compile(
    r"\b[A-Fa-f0-9]{16,}(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}\b"
)

#: 长 hex 的**附件名词干**：`<64位hex>.pcap` → `<attachment>.pcap`。
#:
#: ⚠️⚠️ 这是实测踩出来的（2026-10-13）。平台按内容 SHA256 生成附件名
#:      （见 `attachments.py`），于是命令里到处是 64 位 hex：
#:
#:        tshark -r /work/attachments/9f0ee5dc…adee.pcap -q -z io,phs
#:
#:      它与靶机 hostname 的形状几乎一样，处理时连踩三个坑：
#:
#:        ① 被 `_TARGET_HOSTNAME` 当成靶机 → `unzip -o …/<target>`
#:           （照抄找不到文件）；
#:        ② 被 ③ 那条"32 位以上 hex 是凭据"删掉 →
#:           `tshark -r /work/attachments/.pcap`（文件没了）；
#:        ③ 留着不掩 → 占掉卡片 160 字预算的 **60 字**（37%），
#:           而且**它对复用毫无价值**：换个题目那个文件根本不存在。
#:
#: ★ 所以正确做法不是"留"也不是"删"，而是**把词干换成 `<attachment>`**：
#:
#:        tshark -r /work/attachments/<attachment>.pcap -q -z io,phs
#:
#:      一次解决三件事：命令完整可读、扩展名（"给的是 pcap"）这个
#:      **真正可复用的信息**保住了、长度回到正常。
#:
#: ⚠️ 与 `<target>` 的分工：`<target>` 是**网络目标**（要去连的），
#:    `<attachment>` 是**本地附件**（要去读的）。两者在解题里角色不同，
#:    合成一个占位符会让模型分不清"这个地址要去打"还是"这个文件要去读"。
#:
#: ⚠️ 它必须在 `_TARGET_HOSTNAME` **之后**跑：真靶机那时已经变成
#:    `<target>`，剩下的长 hex + 扩展名才确定是附件。反过来先跑的话，
#:    `dfc05019…f583.tcp-ctf2.dasctf.com` 会被它匹配成
#:    `<attachment>.tcp`（`.tcp` 看着像扩展名）—— 已推演过这个坑。
_LONG_HEX_FILE = re.compile(r"\b[A-Fa-f0-9]{16,}(\.(?:[A-Za-z0-9]{1,8}))\b")

#: 裸 IPv4。见 `sanitize_for_reuse` ②‴。
_IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


@lru_cache(maxsize=1)
def _inline_flag() -> re.Pattern[str]:
    """`FLAG_PATTERN` 的**子串版**（去掉 `^` / `$` 锚点）。

    ## 为什么需要它、为什么从 `FLAG_PATTERN` 推导

    `FLAG_PATTERN` 是整串匹配（`^前缀{正文}$`），只能判断"**一个词**
    是不是 flag"。但 flag 会藏在 URL 路径或参数里，那时它不是一个
    独立的词：

        <target>/?x=DASCTF{secret}
        ^^^^^^^^^^^^^^^^^^^^^^^^^^ 整串不匹配 → 逃过 ① 那一步

    所以需要一个不限位置的版本。

    ⚠️ **从 `FLAG_PATTERN.pattern` 推导而不是另写一条字面量**：
    两条正则各写一份必然漂移（`runner` 改了前缀长度、这边忘了跟），
    而漂移的后果是**泄漏**。去掉首尾锚点即可得到子串版 ——
    因为原式的锚点就在两端。

    ## 代价（明知而接受）

    子串匹配会误伤 `file{1..5}` 这类 shell 花括号展开。判据是
    "误伤的代价 vs 漏掉的代价"：误伤是少了一条可读性一般的内容，
    漏掉是**把 A 题的 flag 注进 B 题**。所以宁可误伤。

    ## ⚠️ 一个例外：**嵌在引号正则里的那一段不删**（2026-10-18 实测）

    唯一的例外在 `_strip_inline_flags` 里，不在本条正则上 —— 这里只是
    指个路，因为"为什么不删"的理由属于那条**调用**规则。
    """
    from app.kernel.runner import FLAG_PATTERN

    return re.compile(FLAG_PATTERN.pattern.strip("^$"))


#: 引号类字符 —— 出现它们说明"这一段是**字面量**，不是叙述正文"。
_QUOTE_CHARS = frozenset("'\"`")

#: 真 flag 正文里**基本不会**出现的字符（量出来的，见 `_strip_inline_flags`）。
#:
#: ⚠️ **故意不含** `.` `-` `\` `,` `_` `:` 这些：它们会出现在真 flag 正文
#:    或"被拼成真 flag 的片段"里（实测样本里就有 `flag{format_` 这种），
#:    把它们算成元字符会让判据宽松到**漏掉真 flag**。
_FLAG_BODY_META = frozenset("[]()|^$*+?")


def _is_unescaped_quote(text: str, index: int) -> bool:
    """`text[index]` 是不是一个**未被反斜杠转义**的引号。

    ⚠️ 这不是吹毛求疵，是实测撞出来的：`#1008` 的 flag 出现在
    `checker.c 中 a=\\"flag{...}` 里，那个 `"` 前面有反斜杠，是 JSON
    转义而不是"引号字面量的开头"。把它当成引号 → 整段被放过 → 泄漏。

    反斜杠必须**成对**消：`\\\\"`（两个真反斜杠）后面那个 `"` 是
    未转义的真引号，而 `\\"`（一个）后面的是转义引号。所以看奇偶。
    """
    if index < 0 or index >= len(text):
        return False
    if text[index] not in _QUOTE_CHARS:
        return False
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 0


def _body_looks_like_regex(matched: str) -> bool:
    """一个**整串匹配上** `FLAG_PATTERN` 的片段，其实是不是正则/命令？

    ## 为什么需要这个判断

    `FLAG_PATTERN` 只约束"前缀 + `{...}`"这个外形，正文里什么都收
    （除 `{}`）。所以下面这些**全都整串匹配**，但它们不是 flag：

        flag{[a-z]*}      ← grep 的正则
        flag{[^}]*}       ← 同上
        flag{(.+?)}       ← 同上
        flag{a|b}         ← 同上

    判据：正文里含正则元字符 `[ ] ( ) | ^ $ * + ?` 就不算真 flag。

    ## ⚠️ 这个判据是量出来的，且只在"整串匹配"语境用

    `_tmp/measure_flag_charset.py` 扫臂 A/B 全部 transcript 的 28 个
    去重形态：**17 个真 flag 无一含这些字符**，11 个含这些字符的
    **全是正则/代码片段**。所以在这条判据上零误伤。

    ⚠️ 但**不能推广到子串匹配**（`_strip_inline_flags` 那里）：
    那里的样本里就有"正文含 `[` 但拼起来是真 flag"的形状
    （`#1006` 的 `flag{format_'…b'[debug] token_part2 = …}`）。
    两个语境看到的证据不同，判据也就不同 —— 这是刻意的，不是不一致。
    """
    body = matched
    if "{" in body and "}" in body:
        body = body[body.index("{") + 1 : body.rindex("}")]
    return bool(_FLAG_BODY_META & set(body))


def _strip_inline_flags(text: str) -> str:
    """`_inline_flag()` 的**子串级**剔除，但放过"命令里当搜索针的 flag 形状"。

    ## 为什么需要这个例外（真 bug，不是假想）

    方法卡里最典型的 Web/取证做法，恰恰是**教人用正则去找 flag**：

        grep -ao 'flag{[^}]*}' /work/attachments/dump.bin

    而 `_inline_flag()` 的子串版会在这里匹配到 `flag{[^}`（正则是
    `[^{}]{1,200}`，`[^` 就是它眼里的"正文"）—— 它不要求那个 `}` 是
    真的右花括号。删掉之后命令变成：

        grep -ao ']*}' /work/attachments/dump.bin
                  ^^^^^ 照抄必然报错

    实测扫十张 A/B 方法卡，**两张被打成这样**（`arena-1004`、`arena-1010`），
    而那几条命令正是"这道题怎么做"的核心。所以这不是"少一条可读性
    一般的内容"，是**把卡片的主要知识删成了语法残骸**。

    同一类还有**代码里的搜索针** —— 删掉一样毁命令：

        python3 -c "... i=d.find(b'flag{'); print(d[i-60:i+40]) ..."

    ## 判据（位置，不是内容）

    只有"**紧前一个字符是未转义引号**"时才有机会保住，再看两条：

      · **紧后一个字符不是同一个引号** —— 说明我们不在 `echo 'flag{abc}'`
        那种"引号包住整段、后面就该收尾"的形状里，而是字符串/正则
        **还没写完**；
      · 否则（后面就是收尾引号）再看**正文里有没有正则元字符** ——
        覆盖 `grep 'flag{[a-z]*}'` 这种整条正则恰好被吃干净的情况。

    ## ⚠️ 为什么不能只看"正文含元字符"

    我最初就是这么写的，被实测否掉了（`_tmp/measure_flag_charset.py`
    扫臂 A/B 全部 transcript，28 个去重样本）。反例形如：

        flag{token_a'，偏移 1069 -> b'[debug] token_part2 = _b}

    正文里有 `[` `]`（元字符），但它**拼起来就是答案**
    `flag{token_a_b}` —— 放过 = 把这题的 flag 注进别的题。
    它前面是空格（不是引号），所以"位置"判据删得掉，"内容"判据删不掉。

    ⚠️ 这里刻意用**编造的** `flag{token_a_b}` 而不是某道真题的 flag：
    这段 docstring 会被打进日志和文档，写真题的 flag 等于把答案
    抄进了一个"教人怎么不泄露 flag"的函数里。

    ## ⚠️ 为什么不能只看"前面是引号"

    因为 `echo 'flag{abc}'`、`"answer": "flag{abc}"` 这些**正常写法**
    前面也是引号。它们后面紧跟同一个引号 → 走第二条分支 → 照删。

    ## 代价（明知而接受）

    hexdump 输出里恰好有反引号时会拼出 `abcdefghijklmnopqrstuvwxyz{|}`
    这种噪声，被判据当成"引号字面量"而保住。方向是对的：保住一串
    噪声只是卡片难看，删掉一段命令则是卡片失效 —— 不对称的错误代价
    决定了判据该往哪边偏。
    """
    pattern = _inline_flag()
    length = len(text)

    def _replace(match: re.Match[str]) -> str:
        if _keeps_search_needle(text, match.start(), match.end(), length):
            return match.group(0)
        return ""

    return pattern.sub(_replace, text)


def _keeps_search_needle(text: str, start: int, end: int, length: int) -> bool:
    """`text[start:end]` 这一处 flag 形状该**保住**（都是搜索针）还是删掉？

    这是整条脱敏链路上唯一一处"位置判据"，`_strip_inline_flags` 与
    `contains_unescaped_flag` 共用它 —— 两处若各写一份，改一处忘
    另一处就会退化成"脱敏保住了、闸门却仍判泄漏"（`#1004`/`#1010` 两张
    卡正是这么被误杀的）。

    ## 判据

      1. **紧前一个字符必须是未转义引号** —— 否则这就是叙述正文里裸着的
         flag（`#1008` 的 `a=\\"flag{...}` 里那个 `"` 有反斜杠，是 JSON
         转义而非引号字面量开头，`_is_unescaped_quote` 看奇偶挡掉）；
      2. 紧后一个字符**不是同一个引号** → 保住（正则/字符串还没写完）；
      3. 否则是 `echo 'flag{abc}'` 那种"引号包住整段"的形状 → 看正文
         有没有 `_FLAG_BODY_META`，含则保住、不含则删。

    ⚠️ `start - 1` 在 `start == 0` 时是 -1，会绕到字符串末尾（Python 的
       负索引），把末字符当成"前一个字符" —— 必须先挡 `start == 0`。
    """
    # ⚠️ `start - 1` 在 start==0 时是 -1，会绕到字符串末尾（Python
    #    的负索引），把末字符当成"前一个字符"—— 必须先挡住。
    if start == 0 or not _is_unescaped_quote(text, start - 1):
        return False
    after = text[end] if end < length else ""
    if after != text[start - 1]:
        return True
    body = text[start:end]
    body = body[body.index("{") + 1 : -1]
    return bool(_FLAG_BODY_META & set(body))


#: `flag{` 开头但**没有闭合花括号**的形状（`grep -abo 'flag{'`）。
#:
#: ⚠️ 必须单独一份，不能用 `_inline_flag()` 代替：那个要求闭合 `}`，
#:    对 `grep -abo 'flag{'` 一处都匹配不到。踩过 —— 闸门用 `\bflag\{`
#:    触发、却用 `_inline_flag()` 裁定，于是"触发看得见、裁定看不见"，
#:    `#1010` 那张卡被整张丢弃（实测 r4 轮）。
#:
#: 前缀只认 `flag`，**不跟** `_inline_flag()` 的 `[A-Za-z][A-Za-z0-9_]*`：
#: 放宽会把 `sed 's/a{2}/b/'` 这类正常代码当成残缺 flag（`a` + `{`），
#: 那会误杀整张卡。宁可漏掉 `CTF{` 开头的残缺形状（完整形状仍由 ① 覆盖）。
_FLAG_OPEN = re.compile(r"\bflag\{", re.IGNORECASE)


def _is_flag_prefixed(text: str, start: int, end: int) -> bool:
    """这个 `PREFIX{...}` 形状的前缀是不是就是 `flag` 这个词。

    `_inline_flag()` 是脱敏用的**广义**形状（任何 `[A-Za-z][A-Za-z0-9_]*`
    开头），对脱敏正合适：多删几个字只是卡里少点内容，代价低。但
    `contains_unescaped_flag` 的调用方是**判整张卡要不要丢**，用广义形状
    就会误杀 —— 实测 `sed 's/a{2}/b/' file.txt` 里的 `a{2}` 命中广义
    形状，被判泄漏、整张卡丢弃。所以这里再收一道前缀。
    """
    brace = text.find("{", start, end)
    if brace < 0:
        return False
    return text[start:brace].strip().lower() == "flag"


def contains_unescaped_flag(text: str) -> bool:
    """`text` 里有没有**会被当成答案的** flag 形状（有 = 不能落盘/不能注入）。

    这是给**外部调用方**（`scripts/ctf2_baseline.py` 的落盘闸门）用的
    公开判据。名字说的是它真正回答的问题 —— 早先叫
    `looks_like_flag_search_needle`，语义是反的（"没有 flag 形状"和
    "有 flag 形状但都是搜索针"都返回 `False`），调用方把 `False` 读成
    "拒收"，于是**纯搜索针的卡也被拒**，`grep -abo 'flag{'` 就是这么
    把 `#1010` 的卡整张干掉的。

    ## 判据（两段，顺序不能换）

      ① **有闭合花括号的完整 flag 形状**（`flag{[^}]*}`、`flag{abc}`）：
         交给与脱敏共用的 `_keeps_search_needle` —— 保住 = 搜索针，
         删掉 = 答案。
      ② **没有闭合花括号的残缺形状**（`grep -abo 'flag{'`）：脱敏根本
         不碰它（`_inline_flag()` 匹配不到），所以只能在这里单独判。
         判据用同一套位置判断的 ① 步：紧前一个字符是未转义引号 →
         它在引号里，是命令的搜索针；否则是叙述正文里裸着的 flag 开头。

    ⚠️ 两段都**只认 `flag` 前缀**，不用 `_inline_flag()` 的广义
       `[A-Za-z][A-Za-z0-9_]*`。那个广义是给脱敏用的（多删几个字代价低），
       而这里的调用方是**判整张卡要不要丢**，误判一次就少一张卡。实测
       踩过：`sed 's/a{2}/b/' file.txt` 里的 `a{2}` 命中广义形状，
       被判成泄漏、整卡丢弃。

    ⚠️ 与脱敏的关系只对**完整形状**成立（双向一致：脱敏保住 ⇔ 本函数
       放行）。对**残缺形状**（`found flag{ partially`）脱敏根本不碰它
       —— `_inline_flag()` 匹配不到 —— 本函数却必须判泄漏，否则正文里
       裸着的半个 flag 就漏过去了。所以不能说"本函数为真 ⇒ 脱敏删掉了"，
       只能说完整形状那一段两边是同一份判据。写错这个方向会得出
       `found flag{ partially` 该放行的结论（实测踩过）。

    ⚠️ 必须在**原文上下文**里判，不能把匹配片段切出来单独喂进来 ——
       切出来之后开头没有前一个字符，位置判据恒不成立，每处都会被判
       成"删除"。

    ⚠️ 空内容返回**假**：没有 flag 形状就谈不上泄漏，调用方的语义是
       "要不要拒收"，空内容不该被拒。

    ⚠️ 与落盘闸门里长串 hex/base64 那条正则分工不同：那个查的是
       `build_note_fields` 之外的独立一类（真 flag 常被切碎嵌进去），
       这里只管 flag 形状，因为只有它才和脱敏规则重叠。
    """
    if not text:
        return False
    length = len(text)
    # ① 完整形状：位置判据复用脱敏那一份，但前缀只认 flag。
    for match in _inline_flag().finditer(text):
        if not _is_flag_prefixed(text, match.start(), match.end()):
            continue
        if not _keeps_search_needle(text, match.start(), match.end(), length):
            return True
    # ② 残缺形状：脱敏不匹配它，所以单独用"前面是不是引号"判。
    for match in _FLAG_OPEN.finditer(text):
        if match.end() < length and text[match.end()] == "}":
            continue  # 完整的，① 已经判过
        if match.start() == 0 or not _is_unescaped_quote(text, match.start() - 1):
            return True
    return False


#: 方括号占位符：`[canary_token]`、`[secret_patterns]`、`[match_threshold=50]`……
#:
#: ## ★ 为什么这类东西必须**原样保留**（v0.40）
#:
#: 场景包里的判据是靠占位符写成的，例如 DL-01「植入 canary
#: `[canary_token]`」、DL-02「定义 `[secret_patterns]`（`sk-` 前缀、
#: AKIA 开头…）」。
#:
#: 这些占位符本身**不是秘密**，它们是"部署时会被替换成真值的位置名"。
#: 而 `sanitize_for_reuse` 的 ③ 步会删掉 32 位以上的连续 hex / 40 位以上
#: 的 base64 —— 如果占位符里恰好塞了一个用作示例的长串，那条判据就会被
#: 掏空成一句读不懂的话，而**空掉的正好是"该看什么"那一半**。
#:
#: ## 判据为什么用"方括号包裹"而不是"长度短一点"
#:
#: 不可以放宽 `{32,}` / `{40,}` 那两个阈值 —— 它们挡的是**真凭据**，
#: 放宽等于为真泄漏开一道后门（见那两行的说明）。
#:
#: 所以判据落在**形式上**：`[...]` 包裹的短标识符是**文档约定**，
#: 真凭据不会长成 `[canary_token]` 这样。这条边界很清楚：
#:   · `[canary_token]`          → 保留（形状名）
#:   · `7f3e9b2d1c84f60a5e8d…`   → 仍然删除（裸长 hex，可能是真值）
#:
#: 长度上限 64 是"这是个标识符不是内容"的护栏：`[` 后面跟一大段
#: base64 不算占位符，那反而更像真东西。
_PLACEHOLDER = re.compile(r"\[[A-Za-z0-9_][A-Za-z0-9_\-=./: ]{0,63}\]")


def _protect_placeholders(text: str) -> tuple[str, list[str]]:
    """把占位符换成哨兵，返回 `(换过的文本, 哨兵表)`。

    ## 为什么需要这一步（而不是给那两条正则加否定环视）

    两条剔除正则是 `\b[A-Fa-f0-9]{32,}\b` 与 `\b[A-Za-z0-9+/]{40,}={0,2}\b`。
    给它们加"别删方括号里的"环视是可行的，但那要改**脱敏主路径**的正则 ——
    而那两条是这个模块最不能出错的地方（它们挡的是真凭据）。

    换成"先把要保的摘出来、跑完再放回"之后：
      · 剔除正则一个字没改，挡真凭据的强度完全不变；
      · 保护范围一目了然，就是 `_PLACEHOLDER` 匹配到的那些。

    ## 哨兵为什么用 `\\x00` 包裹

    它不可能出现在正常文本里（JSON/命令/SQL 都不会带裸 NUL），
    所以不会和真实内容撞名，也就不需要处理"内容里本来就有哨兵"这种情况。
    """
    if not text:
        return text, []
    saved: list[str] = []

    def _swap(match: re.Match[str]) -> str:
        saved.append(match.group(0))
        return f"\x00{len(saved) - 1}\x00"

    return _PLACEHOLDER.sub(_swap, text), saved


def _restore_placeholders(text: str, saved: list[str]) -> str:
    """把哨兵换回原来的占位符（`_protect_placeholders` 的逆操作）。

    ⚠️ 用 `re.sub` + 回调而不是 `str.replace` 循环：哨兵是纯数字，
    逐个 `replace` 在占位符数量多时会把 `\x001\x00` 里的 `1` 撞进
    `\x0011\x00` 之类的编号里。一次扫描按编号取值没有这个歧义。
    """
    if not saved:
        return text

    def _swap(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        # 越界不该发生（哨兵只由 `_protect_placeholders` 生成），
        # 但真发生了也不该让整张卡报错 —— 退回空串即可。
        return saved[idx] if 0 <= idx < len(saved) else ""

    return re.sub(r"\x00(\d+)\x00", _swap, text)


def sanitize_for_reuse(text: str, *, limit: int = MAX_NOTE_CHARS) -> str:
    """把一段文本处理成**可以安全注入到另一道题**的形状。

    ## 白名单式，不是黑名单式

    这里做的事是**只留下确定的形状**，而不是"删掉已知的敏感词"：

      1. **剔除任何看起来像 flag 的片段**（`FLAG_PATTERN`）——
         这是最要紧的一条，理由见模块 docstring；
      2. **把靶机 URL 的 host 掩成 `<target>`**（题面/回显里常带靶机
         地址，而那是**这一道题**的地址，注入到别的题只会误导）——
         ⚠️ 注意是**只掩 host、保留路径**，不是整条删掉，理由见下面 ②；
      3. **剔除看起来像凭据的长随机串**（32 位以上的 hex / base64）；
      4. 折叠空白、截断到上限。

    ## 为什么 flag 用"剔除"而不是"替换成占位符"

    flag 是直接删掉的：替换会留下 `flag{***}` 这样的痕迹，而模型看到
    它会试图"还原"，或者把占位符当成格式提示。直接删掉更干净 ——
    损失的信息是"这里原本有个 flag"，而那本来就不该被复用。

    ⚠️ 但**靶机 URL 反过来**：那里替换（掩成 `<target>`）比删除好，
    因为删掉会把命令掏成空壳（见 ②）。两者的取舍不同，判据是
    "删掉之后这条信息还读不读得懂"。
    """
    if not text:
        return ""

    # 延迟 import：`runner` 依赖本模块（`service` 侧），而本模块
    # 顶层 import 它会形成环。
    from app.kernel.runner import FLAG_PATTERN

    cleaned = text

    # ① 剔除 flag 形状的片段。
    #
    # ⚠️ `FLAG_PATTERN` 是**整串匹配**（`^...$`），不能直接用来找子串。
    #    所以按空白切开逐个词判断 —— 那正好也是 flag 在文本里的
    #    常见形态（一个独立的词）。
    #
    # ⚠️⚠️ **必须逐行处理**（2026-10-13 修）：第一版是
    #      `cleaned = " ".join(cleaned.split())` —— 那会把**所有换行
    #      也压成空格**，于是 `approach` 里精心排的编号步骤挤成一行：
    #
    #        解出来了。关键步骤： 1. cd /work/ex && file ... 2. cd /work/ex && strings ...
    #
    #      而卡片的价值恰恰在"一条路径"的结构上 —— 挤成一行之后
    #      模型读到的是一坨命令，而不是"先做什么、再做什么"。
    #
    #      这个 bug 是**实测发现的**：把真实 transcript 跑一遍提炼，
    #      打印出来才看见 6 条步骤在一行里。
    lines: list[str] = []
    for line in cleaned.splitlines():
        kept = []
        for word in line.split():
            candidate = word.strip("`,.;:()[]'\"")
            # ⚠️ 整串匹配之外还要挡一次"其实是正则"：`flag{[a-z]*}`
            #    外形完全符合 `FLAG_PATTERN`，但它是 grep 的搜索针。
            #    删掉它 → 命令断成 `grep -ao /work/dump.bin`（实测）。
            if FLAG_PATTERN.match(candidate) and not _body_looks_like_regex(candidate):
                continue
            kept.append(word)
        lines.append(" ".join(kept))
    cleaned = "\n".join(lines)

    # ② 靶机 URL：**保留路径、掩掉 host**，而不是整条删掉。
    #
    # ⚠️⚠️ 第一版是 `re.sub(r"https?://\S+", "", cleaned)` —— 整条删光。
    #      实测（2026-10-13 dry-run）暴露出它把命令**掏成空壳**：
    #
    #        原始：curl -s -i http://xxx.dasctf.com:80/robots.txt --max-time 20
    #        结果：curl -s -i --max-time 20        ← 看着像做法，实际打不了
    #
    #      比"没有这条"更坏：模型会照着它去 curl 一个不存在的地址。
    #
    # ★ 而"访问了 /robots.txt"恰恰是**可复用的方法知识**，host 才是
    #   这一道题特有的东西。所以这里只掩 host、留路径：
    #
    #        curl -s -i <target>/robots.txt --max-time 20
    #
    #   这样命令仍然可读、可照搬形状，而靶机地址没有泄漏。
    #
    # ⚠️ 端口一起掩掉：`10.0.0.5:8080` 的端口有时就是服务特征，
    #    但保留它又会让人以为"端口是这个"。统一用 `<target>`。
    cleaned = re.sub(
        r"https?://[^\s/]+",
        "<target>",
        cleaned,
    )

    # ②′ ★ 补一刀：**URL 路径里的 flag**。
    #
    # ⚠️⚠️ 这是上面"保留路径"改动**自己引入的**风险（2026-10-13）：
    #
    #   第一版把整条 URL 删掉，所以 flag 藏在 URL 里也会被一起带走：
    #       curl 'http://10.0.0.5/?x=DASCTF{secret}'   →   curl ''
    #   （整条删光有别的毛病，见上；但"顺带删掉 flag"是它的副作用。）
    #
    #   现在保留路径之后，同一个 flag 就活下来了：
    #       curl '<target>/?x=DASCTF{secret}'          ← ★ 泄漏！
    #
    #   而 ① 那一步**挡不住它**：那里是按空白切词后做 `FLAG_PATTERN.match`
    #   整串匹配，而 `'<target>/?x=DASCTF{secret}'` 作为一个词并不匹配
    #   `^前缀{正文}$`。
    #
    # 所以这里补一遍**子串级**的剔除 —— 位置不再限定于词首词尾。
    #
    # ⚠️ 走 `_strip_inline_flags`（带"放过搜索针"的例外）而**不是**直接
    #    `_inline_flag().sub("", …)`：裸的 `.sub` 会把方法卡里
    #    `grep 'flag{[^}]*}'` 这类**教人找 flag 的命令**吃成语法残骸
    #    （2026-10-18 实测十张卡里两张中招）。判据与实测见该函数。
    #
    # ⚠️ 两者都走 `_inline_flag()`（带 `lru_cache`）而不是在模块顶层编译：
    #    它内部要 `from app.kernel.runner import FLAG_PATTERN`，而
    #    `runner` 也 import 本模块 —— 顶层调用会在 import 期撞环。
    #    这与本文件其它地方用延迟 import 是同一条纪律。
    cleaned = _strip_inline_flags(cleaned)

    # ②″ 靶机的**裸 hostname**（没有 `http://` 前缀的那些）。
    #
    # ⚠️⚠️ 上面那条只认 `http://` 开头的 URL，而实测（2026-10-13）里
    #      靶机地址经常以裸 hostname 出现，**全都漏了过去**：
    #
    #         timeout 15 nslookup dfc05019f1fa0d78efe8f583.tcp-ctf2.dasctf.com 192.168.44.2
    #                             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ 泄漏
    #
    # ## ★ 判据必须窄，否则会毁掉卡片（这是量出来的，不是猜的）
    #
    # 我原本想按"含点 + 末尾是字母"来认，于是先量了一遍**会误伤什么**
    # （`_tmp/measure_hosts.py`，29 道真题）—— 结果是 34 种，而且全是
    # **真正的方法知识**：
    #
    #     Image.open   re.findall   urllib.parse   olefile.OleFileIO
    #     gui.il（×8，是 .NET 的 IL 文件！）  easyre.exe   flag.txt
    #
    # 照那个判据脱敏，等于把我刚修好的"掏空命令"缺陷换个方向再犯一次。
    #
    # 而真实泄漏只有 5 处，形状高度一致：**平台临时分配的靶机**，
    # 首标签是一长串 hex（24 位）+ 赛事域名：
    #
    #     dfc05019f1fa0d78efe8f583.tcp-ctf2.dasctf.com
    #     c436664650adb5d2f6aa1b64.http-ctf2.dasctf.com
    #
    # 按"首标签 ≥16 位 hex 且后面还有域名部分"来判：**5 处命中、
    # 0 处误伤**。`Image.open` / `gui.il` / `easyre.exe` 全都活下来。
    #
    # ⚠️ 为什么不硬编码 `dasctf.com`：那是**一个平台**的域名，
    #    换赛事就失效了。而"长 hex 标签"是平台自动分配环境的通用形状。
    #
    # ⚠️ 用 `_mask_target_host` 而不是直接替换：它会把"末标签是文件
    #    扩展名"的那些**放过**（附件名与靶机形状相同，见 `_FILE_EXTS`），
    #    留给下面 ②⁗ 处理。
    cleaned = _TARGET_HOSTNAME.sub(_mask_target_host, cleaned)

    # ②‴ 靶机 IP。
    #
    # 裸 IP 同样是"这道题的环境"（实测里出现的是 `192.168.44.2`，
    # 本实验网的 DNS）。与 hostname 同理掩掉。
    #
    # ⚠️ `127.0.0.1` 也会被掩 —— 那是刻意的：本机回环在这个语境下
    #    也只是"某次调试连过的地方"，而区分 loopback 会让规则多一层
    #    判断却换不来更多可读性（`<target>` 同样读得懂）。
    cleaned = _IPV4.sub("<target>", cleaned)

    # ②⁗ 长 hex 的**附件名**：`<64位hex>.pcap` → `<attachment>.pcap`。
    #
    # ⚠️⚠️ 顺序：必须在 ②″ **之后**。上面用 `_mask_target_host` 已经把
    #      真靶机掩成 `<target>`、并**放过了**扩展名结尾的那些；到这里
    #      还在的长 hex + 扩展名就确定是附件名了。
    #      反过来先跑会把 `…f583.tcp-ctf2.dasctf.com` 匹配成
    #      `<attachment>.tcp`（`.tcp` 看着像扩展名）—— 已推演过。
    #
    # ★ 为什么是"换词干"而不是"保留"或"删掉"：见 `_LONG_HEX_FILE`
    #   的说明 —— 保扩展名、去随机串，一次解决三个坑。
    cleaned = _LONG_HEX_FILE.sub(r"<attachment>\1", cleaned)

    # ③ 剔除长随机串（可能是凭据、也可能是爆破出的口令）。
    #
    # 32 位是判据：短于它的随机串太多（哈希前缀、偏移量），
    # 剔除会误伤正常内容；而 32 位以上的连续 hex/base64
    # 在方法描述里几乎总是凭据或密钥。
    #
    # ⚠️ 到这一步附件名已经变成 `<attachment>.pcap`，不再是长 hex，
    #    所以这里**不需要**再加否定环视 —— 上面 ②⁗ 已经把它们摘走了。
    #    （早先试过用 `(?!\.[A-Za-z]{1,8}\b)` 在这里排除，但那只解决了
    #    "别删"、没解决"太占长度"和"被当 hostname"，所以改成前置处理。）
    # ★★ v0.40：占位符与"形状说明"必须先被保护起来再剔除。
    #    见 `_protect_placeholders` 的说明 —— 这不是放宽阈值，
    #    而是先认出"这些长串是**判据在描述形状**，不是真凭据"。
    cleaned, _placeholders = _protect_placeholders(cleaned)
    cleaned = re.sub(r"\b[A-Fa-f0-9]{32,}\b", "", cleaned)
    cleaned = re.sub(r"\b[A-Za-z0-9+/]{40,}={0,2}\b", "", cleaned)
    cleaned = _restore_placeholders(cleaned, _placeholders)

    # ④ 折叠空白（上面剔除后会留下多余空格与空行）。
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    # ⚠️ 行尾空格也去掉：不然卡片里会出现看不见的尾随空白。
    cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines())

    return cleaned.strip()[:limit]


def tools_from_transcript(transcript: str, *, limit: int = MAX_TOOLS) -> str:
    """从 transcript 里提取**真正用到的解题工具名**（去重、保序）。

    ## ★ 为什么不是直接取 `tool` 后面那个名字

    transcript 的行是 `[turn N] tool run_shell {"command": "..."}` ——
    而 `run_shell` / `http_request` / `read_attachment` 是**我们自己的
    包装层**工具名，不是解题工具。

    ⚠️ 实测（2026-10-13 dry-run）：第一版直接取那个名字，结果 29 张卡
    的 `tools` 字段几乎全是 `run_shell` 或 `run_shell, http_request` ——
    **零信息量**。模型看到"用到的工具：run_shell"学不到任何东西。

    所以这里剥开参数、从**命令正文**里认工具（`_tools_in_command`，
    按词边界）。认不出具体工具时（比如 `http_request` 只带一个 URL）
    退回包装层名字 —— 那时"做过一次 HTTP 请求"仍是个有用的事实。

    ⚠️ 用 `_TRANSCRIPT_TOOL` 那个正则（与 `digest_from_transcript`
       共用）：它是**我们自己的日志格式**，不是猜别人的。
    """
    from app.kernel.runner import _TRANSCRIPT_TOOL

    seen: list[str] = []
    for line in transcript.splitlines():
        match = _TRANSCRIPT_TOOL.match(line)
        if not match:
            continue
        wrapper = match.group(2).strip()
        # 从命令正文里认真工具；URL 先掩掉，免得 host 里的词被误认。
        raw = sanitize_for_reuse(_unwrap_args(match.group(3)), limit=400)
        found = _tools_in_command(raw) or ([wrapper] if wrapper else [])
        for name in found:
            if name not in seen:
                seen.append(name)
        if len(seen) >= limit:
            break
    return ", ".join(seen[:limit])


def key_steps_from_transcript(
    transcript: str, *, limit: int = 6, sanitize: bool = True, clip: int = 160
) -> list[str]:
    """从 transcript 里提取**成功路径上的关键命令**。

    ## ★ 为什么必须真的提取，而不是写一句模板

    第一版对"解出来了"的情况只输出一句固定文本：

        "这道题解出来了。可复用的要点：先看清楚手上有什么…再按题型选工具"

    而实测（2026-10-13，Kali 上回填 29 张卡）发现：**同类题召回出来的
    三张卡内容一模一样** —— 因为那句话是模板，跟这道题实际做了什么无关。
    三张一样的卡等于什么都没告诉模型。

    所以这里真的读 transcript，挑出值得复用的命令。

    ## ★★ `sanitize` 参数：同一个"挑哪几条"逻辑，两种去向（v0.36）

    判据（`_command_worth_reusing`）两边共用，但**正文要不要脱敏**
    取决于产物去哪：

    | 去向 | `sanitize` | 理由 |
    | --- | --- | --- |
    | 方法卡（`ctf2_solve_notes`） | `True`（默认） | 卡片进**另一道题**的 prompt，带靶机地址会误导 |
    | 题解（`ctf2_writeups`） | `False` | 题解是赛后提交/给人看的，靶机地址与 flag 正是**该有的内容** |

    ⚠️ **默认 `True`** 是刻意的：方法卡是更早、更广的用法。而"忘了传
    `sanitize=False`"的后果只是题解里少了地址（可读性下降）；
    反过来（默认 False）会让方法卡**泄漏靶机地址**，那是安全问题。
    默认值要偏向危险的那一侧。

    ## ★★ 顺序：**先脱敏、再判断**（不能反）

    ⚠️⚠️ 这是实测踩出来的（2026-10-13）。第一版是先
    `_command_worth_reusing(raw)` 再 `_clip_command` 之后统一脱敏，
    于是留下了一批**残骸命令**：

        curl -s -i http://xxx.dasctf.com/robots.txt --max-time 20
          ↓ 判据："含 curl" → 值得复用 ✅
          ↓ 脱敏：URL 被删光
        curl -s -i --max-time 20          ← 看着像做法，实际打不了

    模型会照着这条空壳去 curl 一个不存在的地址 —— 比"没这条"更坏。

    所以这里**先脱敏**，让判据看到的就是最终会写进卡片的形态。
    这也让 `_command_worth_reusing` 的注释里那条契约成立。

    ⚠️ `sanitize=False` 时**判据仍用脱敏版**，只是返回未脱敏的正文 ——
    否则那条空壳规则在题解这条路径上就失效了。

    ## ★★ `clip`：正文长度上限，**判据始终用 160**（v0.38 新增）

    题解要的是**完整命令**（人照着能重跑），而方法卡要的是**短**（进
    prompt，越长越挤）。所以正文上限可配：

    | 去向 | `sanitize` | `clip` |
    | --- | --- | --- |
    | 方法卡 | `True`（默认） | 160（默认） |
    | 题解 | `False` | 更大（见 `writeup.STEP_CLIP`） |

    ⚠️ **判据（`_command_worth_reusing`）永远看 160 那版**，与 `clip`
       无关。理由：判据要回答的是"这条命令值不值得复用"，而它的
       实测调参（词边界、空壳过滤）是在 160 的形态上做的。让判据
       随 `clip` 变会**同时改变方法卡的行为** —— 那正是这次要避开的
       （题解与卡片是两条去向，不能互相带偏）。
    """
    from app.kernel.runner import _TRANSCRIPT_TOOL

    steps: list[str] = []
    for line in transcript.splitlines():
        match = _TRANSCRIPT_TOOL.match(line)
        if not match:
            continue
        raw = _unwrap_args(match.group(3))
        # ★★ 顺序：脱敏 → 截断 → 判断。判据必须看**最终会写进卡片的
        #    那个形态**。截断也算在内：`_clip_command` 会把长命令切在
        #    中间，而"切一半的命令"和"被掏空的命令"一样照抄不了。
        safe = sanitize_for_reuse(raw, limit=400).strip()
        probe = _clip_command(safe, 160)
        if not _command_worth_reusing(probe):
            continue
        # ★ 判据用 160 那版（上面），**返回的正文按去向选**。
        snippet = probe if sanitize else _clip_command(raw, clip)
        if snippet not in steps:
            steps.append(snippet)
        if len(steps) >= limit:
            break
    return steps


def _unwrap_args(raw: str) -> str:
    """把 transcript 里那层参数 JSON 剥掉，只留命令本身。

    ## 为什么需要它

    `_TRANSCRIPT_TOOL` 捕获的是 `{"command": "ls -la /work"}` 这样的
    **参数 JSON**（见 `solve_in_sandbox` 里 `log.append` 的写法）。

    直接用的话卡片里会是一串带引号与花括号的 JSON：

        {"command": "cd /work/ex && strings -n 6 easyre.exe | head -80"}

    而它有两个坏处：
      · **占长度**（每条约多 15 个字符，6 条就多 90）；
      · **读起来像数据不像做法** —— 模型看到 JSON 会当结构化输入解析，
        而这里要传达的是"有人这么做过"。

    ## 剥不掉的怎么办

    `read_attachment` 那类工具的 args 是 `{"path": "x.zip"}`，剥完就是
    `x.zip` —— 那没什么指导意义，所以 `_command_worth_reusing` 会把它滤掉
    （不含工具名、也不是脚本）。
    """
    text = raw.strip()
    if not text.startswith("{"):
        return text
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # 截断过的 JSON（`_clip(..., 500)` 会切掉尾巴）解析不了 ——
        # 那就手工把 `"command": "` 那一段剥掉，剩下的仍然可读。
        match = re.match(r'\{\s*"command"\s*:\s*"(.*)$', text, re.DOTALL)
        if match:
            return match.group(1).rstrip('"}').replace("\\n", " ")
        return text
    if isinstance(parsed, dict):
        # `command` 优先；没有就退回第一个字符串值（比如 path）。
        for key in ("command", "path", "url"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return text


#: 值得复用的命令里通常含这些工具名。
#:
#: ⚠️ 它是**提示**不是白名单：`_command_worth_reusing` 还看命令形状 ——
#:    光靠关键词会漏掉自写脚本（`python3 - <<EOF`），光靠形状会捞进
#:    一堆 `ls` / `cat`。
_REUSABLE_TOOLS = (
    "objdump", "readelf", "strings", "nm", "gdb", "r2", "radare2", "rizin",
    "monodis", "ikdasm", "ilasm", "mcs", "mono", "de4dot",
    "binwalk", "foremost", "exiftool", "steghide", "zsteg", "pngcheck",
    "unzip", "7z", "tar", "file", "upx", "checksec",
    "hashcat", "john", "office2john", "zip2john", "rar2john",
    "openssl", "python3", "pycdc", "uncompyle6", "angr", "z3",
    "sqlmap", "ffuf", "gobuster", "curl", "nc", "socat", "tshark", "tcpdump",
    "xxd", "hexdump", "od",
    # ★ v0.46：P0 补进镜像的那批 pwn 工具，之前**一个都没在这里**。
    #
    # ⚠️ 后果不是"少认出几个工具名"：`_tools_in_command` 既是方法卡
    #    的 `tools` 字段来源，也是 `_command_worth_reusing` 的判据。
    #    它们不在名单里 ⇒ 一条**只含** `one_gadget libc.so.6` 的命令
    #    认不出任何工具 ⇒ 被当成"不值得复用"丢弃。
    #    于是那些真正需要补强才能用的技巧，恰好写不进任何一张卡。
    #    这与"镜像里装了工具但提示词没提"是同一类失配（见 v0.42 那次）。
    #
    # 另外 `gdb` 原本已在名单里，但 `_REUSABLE_TOOLS` 是**元组且逐项
    # `re.search`**，长名不会互相遮挡；`seccomp-tools` 带连字符，
    # 而词边界判据已经把 `-` 当分隔符（`(?<![\w-])…(?![\w-])`）——
    # 所以这个名字需要**写成完整形式**才能被认出（实测：
    # `seccomp-tools` 能命中，而单写一个 `tools` 会归到别的词）。
    # ⚠️ **必须小写**。`_tools_in_command` 先 `command.lower()` 再用
    #    `re.escape(tool)` 原样匹配 —— 写成 `ROPgadget` 就**永远命中不到**，
    #    而它不报错，只是静默地不认。实测踩过（第一版写的就是大写）。
    "ropgadget", "ropper", "cyclic", "one_gadget", "seccomp-tools",
    "patchelf", "pwninit", "libc-database",
)


def _command_worth_reusing(command: str) -> bool:
    """这条命令值不值得写进方法卡。

    ## ★ 这个函数接收的必须是**已脱敏**的命令

    ⚠️⚠️ 这是实测踩出来的契约（2026-10-13）。第一版的调用顺序是
    "先判断值不值得复用 → 再脱敏"，于是出现了一批**残骸命令**：

        原始：curl -s -i http://xxx.dasctf.com:80/robots.txt --max-time 20
        判据：含 `curl` → 值得复用 ✅
        脱敏：URL 被删 → curl -s -i --max-time 20     ← 空壳

    这条空壳**看起来像一条做法**，而模型会照着它去 curl 一个
    不存在的地址 —— 比"没有这条"更坏。所以判据必须作用在
    **最终会写进卡片的那个形态**上：先脱敏、再判断。

    ## 判据是"它对下一步有没有指导意义"

    **排除**（噪音，每道题都会跑）：
      · 纯查看类：`ls` / `cat /` / `pwd`；
      · 我们自己的探测：`command -v` / `which`；
      · 极短的命令（< 8 字符，通常是 `cd` 或脱敏剩下的 `<target>`）。

    **保留**：
      · 含 `_REUSABLE_TOOLS` 里任一工具名的（**按词边界**，见 `_tools_in_command`）；
      · 自写脚本（`<<'EOF'`、`<<PYEOF`、重定向到 .py/.cs）；
      · `sed -n` / `grep -n` / `awk` 这类**定位**命令（说明"去哪找"）。
    """
    cmd = command.strip()
    if len(cmd) < 8:
        return False

    # ★ 丢掉**语法不完整**的命令。
    #
    # ⚠️⚠️ 这是实测发现的（2026-10-13）：transcript 落盘时命令被
    #      `_clip(..., 500)` 截过，而**全库 604 条 tool 行里有 111 条
    #      被截断**（18%）。截断处的形状是"命令断在半句"：
    #
    #        ... print('total:',repr(buf)) s.close() EOF python3
    #        cd /work/gui && grep
    #
    #      这种命令**照着抄会直接报错**，而它看起来像一条做法。
    #      判据：以连接符收尾，或以一个"需要参数"的命令名裸着收尾。
    #
    #      ⚠️ 但 `| head` / `| sort` 是**完整**的（它们读 stdin），
    #         所以那些不算残骸 —— 见 `_OK_BARE_TAIL`。
    if _looks_truncated(cmd):
        return False

    low = cmd.lower()

    if low.startswith(("command -v", "which ", "ls ", "ls -", "cat /", "pwd")):
        return False

    # 自写脚本：保留（"模型怎么解决问题"最直接的体现）
    if "<<'EOF'" in cmd or "<<PYEOF" in cmd or "<<'PYEOF'" in cmd or "<<EOF" in cmd:
        return True
    if re.search(r">\s*\S+\.(py|cs|sh|c|cpp)\b", cmd):
        return True

    if _tools_in_command(cmd):
        return True

    return bool(re.search(r"\b(sed -n|grep -n|awk )", cmd))


#: 裸着出现就说明**命令不完整**的词 —— 它们都需要参数。
#:
#: ⚠️ 与 `_OK_BARE_TAIL` 的区别是实测出来的：`head` / `sort` / `cat`
#:    这类读 stdin，`cmd | head` 是完整命令；而 `grep` / `curl` /
#:    `python3` 裸着就是被截断了。
_NEEDS_ARG = frozenset(
    {
        "grep", "sed", "awk", "r2", "radare2", "rizin", "curl", "wget",
        "python", "python3", "unzip", "7z", "tar", "file", "strings",
        "objdump", "readelf", "nm", "monodis", "mcs", "mono", "hashcat",
        "john", "openssl", "tshark", "tcpdump", "nc", "ncat", "socat",
        "sqlmap", "ffuf", "gobuster", "binwalk", "foremost", "exiftool",
        "steghide", "zsteg", "pngcheck", "checksec", "upx", "angr", "z3",
        "pycdc", "uncompyle6", "ikdasm", "ilasm", "de4dot", "echo",
        "ls", "cd", "mkdir", "cp", "mv", "rm", "chmod", "find", "timeout",
    }
)

#: 裸着出现也**合法**的词 —— 它们从 stdin 读，`cmd | head` 是完整的。
#:
#: ⚠️ 这份名单是"宁可少判"的方向：把完整命令误判成残骸会白丢知识，
#:    而放过去一条残骸的代价小得多（模型看得出 `... python3` 不完整）。
#:
#: ⚠️ 与 `_NEEDS_ARG` **不重叠**（`_looks_truncated` 先查这张表）。
#:    分开写是为了让"哪些裸着合法"一眼可查，而不是靠两个集合的差集。
_OK_BARE_TAIL = frozenset(
    {"head", "tail", "sort", "uniq", "cat", "wc", "xxd", "tr", "cut"}
)


def _looks_truncated(command: str) -> bool:
    """命令是不是**被截断的半句**（见 `_NEEDS_ARG` 的说明）。

    两种残骸形状：
      · 以连接符/管道符/续行符收尾：`... && cd /work/gui &&`；
      · 以一个需要参数的命令名裸着收尾：`... EOF python3`。

    ⚠️ 也挡 `grep` 这类**脱敏掏空**的尾巴（URL 被掩掉后剩下的
       `curl -s -i`）—— 那种同样照抄不了。两个来源、同一个判据。

    ⚠️ **`;` 与 `,` 不算残骸标记**：`cmd1; cmd2;` 是合法的 shell
       （分号只是语句分隔符）。把它们当截断信号会误丢完整命令 ——
       而"误丢"是这份判据里代价更高的那个方向（见 `_OK_BARE_TAIL`）。

    ⚠️ **`\\;` 也不算**（实测踩到）：它是 `find … -exec … \\;` 的
       终止符，而单独的 `\\` 才是续行符。两者都"以反斜杠收尾"，
       差别在**前面有没有分号**：

         find . -type f -exec file {} \\;      ← 完整命令
         tshark -r x.pcap \\                    ← 被截断的续行

       只按 `endswith("\\\\")` 判会把前者误丢（实测里真有这条）。
    """
    stripped = command.rstrip()
    if stripped.endswith(("|", "&&", "||")):
        return True
    if stripped.endswith("\\") and not stripped.endswith("\\;"):
        return True
    words = stripped.split()
    if not words:
        return True
    tail = words[-1].strip("`'\"").lstrip("-")
    if not tail:
        # 以纯选项收尾（`curl -s -i` 那种被掏空的形状）。
        return True
    if tail in _OK_BARE_TAIL:
        return False
    if tail in _NEEDS_ARG:
        return True
    # ★ 被掏空的另一种形状：整条命令**只剩命令名 + 选项**，一个位置
    #   参数都没有。`curl -s -i`（URL 被掩掉后）就是这样 —— 它看着
    #   像做法，但没有目标，照抄只会报错。
    #
    # ⚠️ 判据必须看"有没有位置参数"，不能只看"结尾是不是选项"：
    #    `hashcat -b -m 9500 --force` 以选项收尾，但 `9500` 是位置参数，
    #    它是**完整**的命令（实测里真有这条）。只看结尾会误丢它。
    return _last_segment_has_no_positional(words)


def _last_segment_has_no_positional(words: list[str]) -> bool:
    """最后一段命令是不是"只有命令名 + 选项、没有位置参数"。

    只取**最后一段**（按 `&&` / `||` / `;` / `|` 切）：前面的段是铺垫
    （`cd /work && …`），要判断的是最后那个真正干活的命令。
    """
    segment: list[str] = []
    for w in words:
        if w in ("&&", "||", ";", "|"):
            segment = []
        else:
            segment.append(w)
    if len(segment) < 2:
        return False
    name = segment[0].strip("`'\"").rsplit("/", 1)[-1]
    if name not in _NEEDS_ARG:
        return False
    # 位置参数 = 任何不以 `-` 开头的词（含 `key=value` 这种赋值形式 ——
    # 它也是实打实的参数，所以算数）。
    return not any(not w.startswith("-") for w in segment[1:])


def _tools_in_command(command: str) -> list[str]:
    """从一条命令里认出**真正解题的工具名**（按词边界，不是子串）。

    ## ★ 为什么必须词边界

    ⚠️ 第一版是 `any(tool in low for tool in _REUSABLE_TOOLS)` ——
    **子串匹配**。而 `_REUSABLE_TOOLS` 里有 `nc`、`file`、`r2` 这种
    极短的词，子串匹配的误判率高得离谱：

      · `nc` 命中 `sync` / `func` / `encode`；
      · `file` 命中 `profile` / `filename` / `defile`；
      · `r2` 命中 `bar2` / `var2`。

    后果不是"多留了几条"这么轻：它让**几乎任何命令**都被判成
    "值得复用"，于是卡片里塞满 `cd /work && ls -la` 这种每道题都跑的
    噪音 —— 实测 5 张卡的首步都是 `cd /work/attachments && ls -la && file *`。

    ## 判据

    工具名前后不能紧邻 `\\w` 或 `-`（后者挡 `python3-venv` 这类
    连字符拼接），但**允许**后接 `.`（`python3.13`、`zsteg.py` 要能认出）。
    """
    low = command.lower()
    found: list[str] = []
    for tool in _REUSABLE_TOOLS:
        if re.search(rf"(?<![\w-]){re.escape(tool)}(?![\w-])", low):
            found.append(tool)
    return found


def _clip_command(command: str, limit: int) -> str:
    """把命令压成一行、截断。

    ⚠️ 换行会毁掉 prompt 里的列表结构（一条命令变成多行）。
    """
    one_line = " ".join(command.split())
    return one_line[:limit] + ("…" if len(one_line) > limit else "")


def _coverage_note(steps: list[str]) -> str:
    """把"跑到哪一轮、试了几条路径"写成一句覆盖范围说明（v0.40）。

    ## 为什么这一项**必须自动生成**而不能留空

    它是 §5.2 四项里唯一能区分"测遍了都没有"与"试了一下没反应"的
    那一项（见 `ruled_out_chain_complete` 的说明）。留空的话，
    所有失败的卡都会停在 `needs_follow_up` —— 那在效果上等于
    本次修复没有生效。

    ## ⚠️ 它描述的是**我们的观察**，不是"覆盖是充分的"

    写成"跑了 N 条命令"而不是"已覆盖全部变体"：后者是结论，
    而证明结论需要人复核。这里只如实记下规模，让读卡的人自己判断
    这个量够不够。
    """
    count = len(steps or [])
    if count <= 0:
        return "未记录（transcript 里没有可复用的命令）。"
    return f"本次共 {count} 条可复用命令，覆盖程度按此判断。"


def _agentsec_dead_ends(*, ruled_out: list[str], reason: str, covered: str) -> str:
    """把模型的排除清单与自动生成的观察拼成 §5.2 的四项格式（v0.40）。

    ## 数据来源的分工

    | 项 | 来源 | 拿不到时 |
    | --- | --- | --- |
    | ① 判据 | 模型 `ruled_out` 的条目 | `未指定` |
    | ② 测了什么 | 模型 `ruled_out` 的条目 | `未记录` |
    | ③ 观察到什么 | `error` 原话（脱敏后） | `未记录` |
    | ④ 覆盖范围 | `_coverage_note(steps)` | `未记录` |

    ## ★ 为什么模型写的东西能填前两项，但不能填第三项

    `ruled_out` 里每条大致是「排除了 X —— 因为试了 Y 得到 Z」。
    它天然覆盖了"针对哪个判据"与"做了什么测试"，也就是 ① 和 ②；
    而 ③ 要的是**观察到什么具体现象**，那与"错误原话"是同一件事，
    所以用 `reason` 更准确。

    ⚠️ 如果模型什么都没写（`ruled_out` 为空），前两项就写"未记录"，
    整条链路不齐全、verdict 落到 `needs_follow_up` —— **这是对的**。
    一次没有排除任何东西的解题，本来就不该被记成"已排除"。
    """
    items = [s for s in (ruled_out or []) if isinstance(s, str) and s.strip()]
    # 每一条都在这里过一遍脱敏：模型完全可能把目标地址甚至密钥原文
    # 写进 FINAL，而卡片会进另一道题的 prompt。
    cleaned = [sanitize_for_reuse(s, limit=200) for s in items]
    cleaned = [s for s in cleaned if s]

    if cleaned:
        # ② 用完整条目（"试了 Y 得到 Z"就在里面）。
        tested = "；".join(cleaned)
        # ① 取每条里"判据/考察点"那半句。模型写的形状不固定，所以
        # 只做**保守**切分：认得出分隔符就取前半，认不出就整条留下。
        # 拼错形状的代价只是这一项啰嗦，而丢掉内容的代价是判据缺失。
        heads = [_judge_head(s) for s in cleaned]
        judges = "；".join(h for h in heads if h) or tested
        # ③ 优先用 `error` 原话（那是"卡在哪一步"最精确的描述）；
        #    没有 error 但它确实写了排除清单时，**不能**写"未记录"——
        #    那等于把模型已经写下的观察抹掉，还会让整条链判不齐。
        observed = reason or "见「测了什么」里的逐条说明。"
    else:
        tested = "未记录"
        judges = "未指定"
        observed = reason or "未记录"

    return "\n".join(
        [
            f"判据：{judges}",
            f"测了什么：{tested}",
            f"观察到什么：{observed}",
            f"覆盖范围：{covered}",
        ]
    )


def _judge_head(item: str) -> str:
    """从一条排除记录里取出"针对什么判据"那半句（v0.40）。

    ## 为什么只做保守切分

    模型写 `ruled_out` 的形状不固定（实测有「排除了 X —— 因为 Y」
    「X：试了 Y」「X（原因：Y）」等多种）。这里只认几种明确的分隔符，
    认不出就**整条返回** —— 拼错形状的代价只是这一项啰嗦，
    而把内容切丢的代价是一条判据消失。
    """
    # 分隔符按"信息量"排：破折号通常分的是"结论——理由"，
    # 冒号分的是"标题：内容"。冒号放后面，避免把「X——因为 Y：Z」
    # 切成「X——因为 Y」。
    for sep in ("——", "：", ":"):
        if sep in item:
            head = item.split(sep, 1)[0].strip()
            # head 太短（比如只有"排除了"）就退回整条，避免切出无意义的碎片。
            if len(head) >= 4:
                return head
    return item


def build_note_fields(
    *,
    transcript: str,
    error: str | None,
    candidates: list[str],
    solved: bool,
    agentsec: bool = False,
    ruled_out: list[str] | None = None,
    ir: bool = False,
    domain: str | None = None,
) -> dict[str, str | None]:
    """从一次解题的产物里提炼出卡片的三个字段（**全部已脱敏**）。

    ## 输入是什么、能提炼出什么

    | 输入 | 提炼出的 |
    | --- | --- |
    | `transcript` | 用过的工具（`tools`）、**关键步骤**（`approach`） |
    | `error` | 失败原因 → 进 `dead_ends` |
    | `candidates` | **只用来判断"有没有产出"，绝不写进卡片** |

    ⚠️ `candidates` 是 flag 原文，**一个字都不进卡片**。它出现在签名里
    只是为了区分"解出来了"与"没解出来"两种措辞。

    ## ★ 为什么 `approach` 现在是"关键步骤"而不是一句结论

    第一版写的是一句模板（"先看清楚手上有什么…再按题型选工具"），
    而实测发现**同类题召回出来的卡内容一模一样** —— 那种卡毫无价值。

    现在它由 `key_steps_from_transcript` 真的从 transcript 里提取，
    所以每道题的卡是不同的。

    ## ★ `agentsec` 只影响 `dead_ends` 的**格式**，且默认关闭

    打开时 `dead_ends` 按场景包 §5.2 写成四项（判据/测了什么/观察到
    什么/覆盖范围），否则写一句失败原因。

    ⚠️ 默认必须是 `False`：`dead_ends` 会进**用户回合正文**，
    给五个现有分类换格式等于改掉它们每一次解题所见。

    ★ v0.41：`ir` 与 `agentsec` 走**同一条分支**（同一个四项格式）。
    见下面那段说明。默认同样 `False`。

    Args:
        transcript: 本次解题的工具调用记录。
        error: 收尾时的错误（没有就 None）。
        candidates: 候选答案原文（**只用于判断"有没有产出"**）。
        solved: 是否解出来了（`verdict == confirmed`）。
        agentsec: 本题是否属于智能体安全场景包。
        ir: 本题是否属于应急响应场景包（v0.41）。
        domain: 领域标签（v0.56，见 `vocab.NoteDomain`）；`generic` 会
            被**所有**分类召回，另两档只在本侧可见，`None` = 不标注。
    """
    tools = tools_from_transcript(transcript)
    steps = key_steps_from_transcript(transcript)

    from app.core.vocab import normalize_domain
    if solved:
        head = "解出来了。关键步骤（按时间序，前几步往往是识别与解包）："
    else:
        head = "**没解出来**。已试过的关键步骤："

    if steps:
        # 每条一行并编号 —— 模型读起来是"一条路径"而不是一坨文本。
        approach = head + "\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(steps, 1))
    else:
        approach = head + "（transcript 里没有可复用的命令）"

    if solved:
        dead_ends = ""
    elif agentsec or ir:
        # ★ v0.40 / v0.41：按场景包 §5.2 的**四项证据格式**写。
        #
        # 为什么这两族要改格式：`ruled_out`（排除）必须四项齐全
        # 才算数（见 `ruled_out_chain_complete`）。
        #
        # ## ★ v0.41：为什么 IR 也走这条路，而不是另写一套
        #
        # 应急响应里"查了这条线索但没找到"和"确认发生了"是两种都要
        # 被复用的结论，判据结构与对抗评测**逐条对应**：判据（要查
        # 什么）／测了什么（查了哪些日志）／观察到什么（结果）／
        # 覆盖范围（时间窗与日志源覆盖到哪）。四项缺任一项，那条
        # "排除"就是不可信的 —— 而不可信的排除比没有结论更危险。
        #
        # 所以共用同一个格式化函数。差异化留给提示词（那里说的是
        # "日志缺口"这类 IR 专有概念），格式层不重复实现。
        #
        # ## 四项的**真实**来源是模型的 FINAL 结论文档
        #
        # `SolveOutcome.ruled_out` 是模型逐条写的「排除了什么、凭什么」
        # （v0.38 起就有，见 `runner.SolveOutcome.ruled_out`）。那是
        # 唯一能让证据链真正齐全的信息源 —— 自动生成的四项里只有
        # 第 ③ 项（错误原话）是拿得到的。
        #
        # ## 拿不到的项写"未记录"，不省略、不编造
        #
        # 省略会让卡看起来像"四项都在、只是没内容"；"未记录"明确
        # 说明它残缺，`resolve_verdict` 会正确归到 `needs_follow_up`。
        #
        # ⚠️ 编造另三项 = "为了让 verdict 好看而伪造证据"，
        #    而证据链造假比没有证据链危险得多。
        reason = sanitize_for_reuse(error or "", limit=300)
        covered = _coverage_note(steps)
        dead_ends = _agentsec_dead_ends(
            ruled_out=ruled_out or [],
            reason=reason,
            covered=covered,
        )
    else:
        # ## ★ 为什么五个现有分类走这条老路，一个字都不改
        #
        # `dead_ends` 会被渲染进「走不通的方向：」并进入**用户回合正文**。
        # 把它换成四项格式，即便只是多了几个前缀，也改变了那五类题的
        # 每一次解题所见 —— 而那是本次改动的主要回归判据（"五类逐字节
        # 不变"）。
        #
        # 所以四项格式**只给 agentsec 四类**。副作用是那五类永远不会有
        # 四项齐全的 `dead_ends`，于是 `ruled_out` 对它们永不成立、
        # `solved` 语义与改动前完全一致 —— 这正是想要的。
        reason = sanitize_for_reuse(error or "", limit=300)
        dead_ends = reason or "未记录失败原因。"

    # ⚠️ 这一句必须留在**两个分支之外**：它只与 `candidates` 有关，
    #    与 `agentsec` 无关。曾经把它连同缩进一起放进了 else 分支，
    #    结果 agentsec 类的失败卡不再标注「全程没有产出候选答案」——
    #    而那句话是判断"这次到底有没有接近答案"的唯一线索。
    if not solved and not candidates:
        approach += "\n  全程没有产出候选答案。"

    return {
        # ⚠️ 上限放宽到两倍：`approach` 现在是多行步骤而不是一句话，
        #    用原来的 600 会把后半截步骤截掉（而那正是有价值的部分）。
        "approach": sanitize_for_reuse(approach, limit=MAX_NOTE_CHARS * 2),
        "tools": tools,
        "dead_ends": dead_ends,
        # ★ v0.56：领域标签，收敛到合法取值（认不出 → None，不猜成 generic）。
        "domain": normalize_domain(domain),
    }


# ── ★ v0.46：从工具清单反推细分题型 ──────────────────────────────
#
# 为什么是"从工具反推"而不是"从题目正文猜"：
#
# 实测过两条路径。题目正文的路径存在一个静默失败：平台
# `description` 经常为空、很多题只给一个附件名。那时"没认出来"与
# "不是那类题"长得一模一样 —— 而这个区分正是判断"要不要投 heap
# 工具"的全部依据。模型**真的跑了哪些命令**则是硬事实：它
# 不会因为题目没描述而变得模糊。
#
# ⚠️ 不能拿 `TOOL_NAMES`（只有 `run_shell`/`http_request`/
#    `read_attachment` 三个**包装层**名字）去数—— 这个坑
#    `tools_from_transcript` 的 docstring 里已经记过一次：
#    得到的清单几乎全是 `run_shell`，零信息量。
#    必须从命令正文里认工具（`_tools_in_command`）。

#: 每个细分题型的判定特征。
#:
#: 顺序即优先级：同一次解题可能同时命中多组（实测中
#: `pwn_baby` 那类题会同时出现 `checksec` 与 `cyclic`）。
#: 把更具体的放前面：用了 `cyclic`/`one_gadget` 说明已经
#: 走到了每一个堆溢出题都会走的 `checksec` 之外。
_SUBCATEGORY_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    # heap 向：这一组的工具只在真要挖堆时才会被拿起来。
    # `malloc`/`free` 是模型写利用脚本时的典型特征词。
    ("pwn-heap", frozenset({"one_gadget", "libc-database", "pwninit", "malloc", "tcache"})),
    # 格式串向：`%p`/`%s` 这类泄漏探针难以归入工具名，
    # 但 `pwntools` 的 `fmtstr_payload` 是专属的。
    ("pwn-fmt", frozenset({"fmtstr_payload"})),
    # 栈溢出 / ROP 向：`cyclic` 与 `ROPgadget` 基本只在这类题里出现。
    ("pwn-stack", frozenset({"cyclic", "ropgadget", "ropper", "ropstar"})),
)

#: 可能出现在 `tool_kinds` 里、但说明不了题型的工具。
#: 它们不参与判定，列出来是为了将来加规则时不要忘了
#: 这些已经在记录里的名字。
_TOOLS_NOT_DIAGNOSTIC = frozenset(
    {
        "file", "ls", "cat", "strings", "xxd", "hexdump", "od", "python3",
        "curl", "nc", "socat", "unzip", "tar", "7z", "checksec", "objdump",
        "readelf", "nm", "gdb", "r2", "radare2", "rizin", "binwalk",
        "exiftool", "steghide", "zsteg", "openssl", "z3", "angr", "sqlmap",
        "ffuf", "gobuster", "hashcat", "john", "patchelf", "seccomp-tools",
    }
)


def validate_note_category(
    category: str,
    *,
    agentsec_enabled: bool,
    ir_enabled: bool = False,
) -> str:
    """校验方法卡的 `category` 取值，返回**归一化后**的字符串（v0.40，v0.41）。

    ## 它守的是什么

    `KernelSolveNote.category` 是 `String(64)`、**没有数据库约束** ——
    原来它直接抄平台 payload 的分类名（`Pwn` / `Web` / …），
    爱是什么是什么。那在只有平台定义题型时是对的。

    但 `agentsec-*` 与 `ir-*` 这两族是**我们自己定义**的，而且它们不只是
    分类：它们决定 agent 拿到哪一套行为纪律（见 `vocab.AgentsecCategory`
    与 `vocab.IrCategory`）。让这种取值靠"调用方记得传对字符串"来保证，
    拼错一个字母就会静默退回攻击模式 —— 而模型不会报错，它只会照着
    原来的纪律去"拿 flag"。

    ## 开关关着时**拒绝**，而不是静默降级成空字符串

    静默降级会让"以为开了其实没开"变成一次没有信号的失效：卡片
    写进去了，但它不再影响 prompt、也不再有 verdict 语义，而写入方
    拿到的是成功。宁可抛错。

    ## ⚠️ 不校验现有五类

    那五类（以及平台将来可能加的任何分类）**原样放行** —— 它们来自
    平台 payload，我们不定义、也无权拒绝。这个函数的守备范围只有
    "以 `AGENTSEC_CATEGORY_PREFIX` 或 `IR_CATEGORY_PREFIX` 开头的取值"，
    其余一律原样返回。

    这条边界很重要：如果这里改成白名单，平台哪天新增一个题型，
    同步流程就会因为"分类不认识"而失败 —— 那是把我们的纪律
    强加给平台的接口，代价是丢题。

    ## ★ v0.41：为什么 `ir_enabled` 是**带默认值**的新参数

    与 `build_system_prompt` 同一条理由：不带默认值会让现有每个调用点
    都必须改，而它们的语义是"我不关心应急响应"。带默认值 `False` 之后，
    本函数在 v0.41 之前的调用行为**逐字节不变**。

    ⚠️ 但"`ir-*` 分类 + 开关默认关"会**抛错**，这是刻意的：一道 IR 题在
    开关关着时不该被静默降级成"未分类卡片"，那会让它的 verdict 语义
    无声消失（见上面第二段）。

    Args:
        category: 待校验的分类名（可以是空串 —— 空串永远合法）。
        agentsec_enabled: `KernelSettings.enable_agentsec_scenarios`。
        ir_enabled: `KernelSettings.enable_ir_scenarios`。

    Raises:
        ValueError: 取值属于某族但不合法、或该族的开关没打开。
    """
    from app.core.vocab import (
        AGENTSEC_CATEGORIES,
        AGENTSEC_CATEGORY_PREFIX,
        IR_CATEGORIES,
        IR_CATEGORY_PREFIX,
    )

    normalized = (category or "").strip()
    if not normalized:
        return ""
    if not normalized.startswith((AGENTSEC_CATEGORY_PREFIX, IR_CATEGORY_PREFIX)):
        # 平台定义的分类（pwn/web/reverse/crypto/misc 以及将来新增的）：
        # 一律放行，理由见 docstring 末尾那条边界。
        return normalized[:64]

    # ★ 两族各判各的开关与白名单。写成一对 if 而不是"判前缀 → 拼变量名"
    # 那种取巧写法：后者在加第三族时会把"开关名"变成一次字符串拼接，
    # 而拼错的名字不会报错，只会让 `getattr` 拿到一个不存在的属性。
    if normalized.startswith(AGENTSEC_CATEGORY_PREFIX):
        if not agentsec_enabled:
            raise ValueError(
                f"分类「{normalized}」属于智能体安全场景包，但该功能当前未启用。"
                "请在 CTF2 配置里打开 enableAgentsecScenarios 后再写入。"
            )
        if normalized not in AGENTSEC_CATEGORIES:
            raise ValueError(
                f"未知的智能体安全分类「{normalized}」。"
                f"合法取值：{' / '.join(sorted(AGENTSEC_CATEGORIES))}。"
            )
        return normalized

    if not ir_enabled:
        raise ValueError(
            f"分类「{normalized}」属于应急响应场景包，但该功能当前未启用。"
            "请在 CTF2 配置里打开 enableIrScenarios 后再写入。"
        )
    if normalized not in IR_CATEGORIES:
        raise ValueError(
            f"未知的应急响应分类「{normalized}」。"
            f"合法取值：{' / '.join(sorted(IR_CATEGORIES))}。"
        )
    return normalized


def is_agentsec_category(category: str) -> bool:
    """这个分类是否属于智能体安全场景包（**只看前缀，不看开关**）。

    ⚠️ 它不回答"该不该放行"，只回答"它是不是这类"。要不要放行由
    `validate_note_category` 结合开关判断 —— 拆成两个函数是因为
    调用点不同：prompt 注入那边只关心"是不是这类"，而写入路径那边
    还要再确认开关。
    """
    from app.core.vocab import AGENTSEC_CATEGORY_PREFIX

    return (category or "").strip().startswith(AGENTSEC_CATEGORY_PREFIX)


def is_ir_category(category: str) -> bool:
    """这个分类是否属于应急响应场景包（**只看前缀，不看开关**）（v0.41）。

    与 `is_agentsec_category` **完全同构**，包括"它不回答该不该放行"
    这条边界。两者分成两个函数而不是一个 `is_scenario_category`
    返回枚举，理由是调用点只关心"是不是我这一类"，而合并之后每个
    调用点都得再判一次返回值是哪一个 —— 那等于把前缀判断抄第二遍。
    """
    from app.core.vocab import IR_CATEGORY_PREFIX

    return (category or "").strip().startswith(IR_CATEGORY_PREFIX)


def is_pentest_category(category: str) -> bool:
    """这个分类是否属于渗透评估场景包（**只看前缀，不看开关**）（v0.47）。

    与 `is_ir_category` **完全同构**，包括"它不回答该不该放行"这条边界。
    """
    from app.core.vocab import PENTEST_CATEGORY_PREFIX

    return (category or "").strip().startswith(PENTEST_CATEGORY_PREFIX)


def domain_for_category(category: str) -> str | None:
    """从一个分类推导它的**侧**（`offensive` / `defensive`），推不出返回 `None`。

    ## ★ v0.56：这是"默认侧"，不是"跨场景通用"

    这里按题型前缀推的是**这张卡天然属于哪一侧**：

      · `pentest-*` → `offensive`（攻击评估侧的方法）；
      · `ir-*`      → `defensive`（应急响应/取证侧的方法）；
      · 其余（五个 CTF 题型、`agentsec-*`）→ `None`（不标侧）。

    ⚠️ **它绝不返回 `generic`**。`generic`（跨场景通用）是"这张卡的
    方法在攻防两侧都成立"的**人工判断**，靠前缀推不出来 —— 用前缀
    猜成 generic 会让一张只属于攻击侧的卡泄漏到应急响应上下文里，
    而模型会把它当成"防守经验"用。所以自动标注只敢标"属于哪一侧"，
    跨场景那一档留给显式的写入路径（见 `learn_from_solve` 的说明）。
    """
    from app.core.vocab import NoteDomain

    if is_pentest_category(category):
        return NoteDomain.OFFENSIVE.value
    if is_ir_category(category):
        return NoteDomain.DEFENSIVE.value
    return None


#: `ruled_out` 的四项证据要求（场景包 §5.2），以及**每项的残缺标记**。
#:
#: 形式是 `(标记词, 残缺标记)`：出现标记词只说明"写了这一项"，
#: 出现残缺标记说明"这一项其实是空的"。
#:
#: ## ★ 为什么必须有残缺标记这一层

#: 第一版只查"四项标记在不在"，而 `build_note_fields` 会自动写四个
#: 固定前缀 —— 于是**任何**失败卡都四项齐全，`ruled_out` 变成恒真，
#: 所有"试了一下没反应"都升级成"已排除"。那正是 §5.3 明令禁止的
#: 「禁止将『未发现异常』记为 `ruled_out`」。
#:
#: 实测发现的：一次 `error='timeout'`、`transcript` 里零条可复用命令的
#: 失败卡被判成了 `ruled_out`。所以判据必须落到**内容**上，不是前缀。
RULED_OUT_EVIDENCE_MARKERS: tuple[tuple[str, str], ...] = (
    ("判据：", "未指定"),      # ① 针对的具体判据编号
    ("测了什么：", "未记录"),  # ② 做过什么测试
    ("观察到什么：", "未记录"),  # ③ 观察到什么（为什么排除）
    ("覆盖范围：", "未记录"),  # ④ 测试覆盖范围（跑了哪些变体）
)


def ruled_out_chain_complete(dead_ends: str) -> bool:
    """`dead_ends` 里是否四项证据齐全（`ruled_out` 的**前提**）。

    ## 为什么 `ruled_out` 要有这个门槛

    场景包 §5.3 明令：「禁止将『未发现异常』记为 `ruled_out` ——
    排除需要**主动证伪**，不是『没测出来』」。

    而"没测出来"和"测遍了都没有"在文本上几乎长得一样：
    `dead_ends` 里都可能是"试了 X，没反应"。区别全在**有没有第四项
    （覆盖范围）** —— 它是一次排除与一次疏忽之间唯一的分界线。

    所以这条判据不是为了挑剔格式，是因为**没有它，`ruled_out` 就会
    被当成"我也没辙了"的垃圾桶**。而一旦那样，`solved=True` 就会
    覆盖一批其实没做完的卡，下次同类任务照着它跳过 —— 那比没有卡更糟。

    ## ★ 判的是"每项都有实质内容"，不是"四个前缀都在"

    见 `RULED_OUT_EVIDENCE_MARKERS` 的说明：只查前缀的话，
    `build_note_fields` 自动补的四项会让这个函数恒真。
    所以每一项都要求「标记词在」**且**「残缺标记不在那一行」。

    ## ⚠️ 它仍然只判"四项写了实质内容"，不判"内容说得对"

    判断内容质量需要人。但"每项都不是空的"至少能挡掉
    "试了一下没反应"这种，而那是最常见的一种。
    """
    text = dead_ends or ""
    if not text.strip():
        return False
    lines = text.splitlines()
    for marker, missing in RULED_OUT_EVIDENCE_MARKERS:
        # 找到写了这一项的那一行（标记词可能在行首）
        hit = next((ln for ln in lines if ln.startswith(marker)), None)
        if hit is None:
            return False
        # 该项必须不是"未指定/未记录"—— 那等于没写
        if missing in hit:
            return False
    return True


def resolve_verdict(*, solved: bool, dead_ends: str) -> str:
    """决定一张方法卡的 `verdict`（v0.40）。

    ## 三个取值，判据只有两条

    | 情况 | verdict | `solved` |
    | --- | --- | --- |
    | 解出来了 | `confirmed` | `True` |
    | 没解出，但 `dead_ends` 四项证据齐全 | `ruled_out` | `True` |
    | 其余 | `needs_follow_up` | `False` |

    ## ★ 中间那一行就是本次要修的那个缺陷

    原来 `solved` 只认"解出来了"。于是"跑完全套测试手法、确认这个
    考察点不存在"的卡被记成**未解决** —— 它在召回里排在后面，还可能
    因为"只给解出过的"而根本不被取到。

    而那张卡的获取成本与"命中"相当：对抗类判据要求跑完整个话术/技术
    全集才能判排除（§5.3）。把它当未解决，等于让下次同类任务**再跑
    一遍同样的全套排除** —— 那正是方法卡要消除的浪费。

    ## ★ 为什么不干脆 `ruled_out` 一律算 solved

    因为"排除"有真假之分。一次草率的"试了没反应"如果也算 solved，
    卡片就会以一个**错误的前提**被优先召回，下次任务照着它跳过 ——
    那比"没有这张卡"更糟：没有卡时模型会自己去测，有错卡时它不会。

    所以门槛落在"四项证据齐全"上（见 `ruled_out_chain_complete`）。

    Args:
        solved: 传统意义上的"解出来了"（有 confirmed 证据）。
        dead_ends: 提炼后的死路清单（`build_note_fields` 的产物）。
    """
    from app.core.vocab import AgentsecVerdict

    if solved:
        return AgentsecVerdict.CONFIRMED.value
    if ruled_out_chain_complete(dead_ends):
        return AgentsecVerdict.RULED_OUT.value
    return AgentsecVerdict.NEEDS_FOLLOW_UP.value


def counts_as_solved(verdict: str) -> bool:
    """这个 verdict 算不算"已解决"（用于 `KernelSolveNote.solved`）。

    `confirmed` 与 `ruled_out` 都算 —— 理由见 `resolve_verdict`。
    `needs_follow_up` 不算（它就是"还没定"）。
    """
    from app.core.vocab import AgentsecVerdict

    return verdict in (AgentsecVerdict.CONFIRMED.value, AgentsecVerdict.RULED_OUT.value)


def derive_subcategory(tool_kinds: str) -> str:
    """从 `tool_kinds`（逗号分隔的工具名）推出细分题型。

    返回空字符串表示**推不出来**，不是"普通 pwn"。这一区分是故意的：
    把"没证据"归成一个类会让分类统计里凭空多出一个不存在的类，
    而那正是要用这列数据回答问题时最怕的事。
    """
    if not tool_kinds:
        return ""
    lowered = {name.strip().lower() for name in tool_kinds.split(",") if name.strip()}
    if not lowered:
        return ""
    for label, needles in _SUBCATEGORY_RULES:
        if lowered & needles:
            return label
    return ""


#: `verdict` → 渲染时的标签（v0.40）。
#:
#: ## ⚠️ 只对**场景包分类**（`agentsec-*` / `ir-*`）生效 —— 这是刻意的
#:
#: 普通 CTF 题的 `verdict` 只有两种取值（解出 → `confirmed`、
#: 没解出 → `needs_follow_up`），而"没解出"这件事已经在 `approach`
#: 正文里写了「**没解出来**」。再挂一个标签就是同一句话说两遍。
#:
#: 更重要的是：**普通题那五类的 prompt 必须逐字节不变** ——
#: 那是本次改动的主要回归判据。所以标签只在场景包卡上出现，
#: 见 `_verdict_tag_for`。
_VERDICT_TAGS: dict[str, str] = {
    "confirmed": "【已命中】",
    "ruled_out": "【已排除】",
    "needs_follow_up": "【待跟进】",
}


def _verdict_tag_for(note: object) -> str:
    """这张卡该不该带 verdict 标签，带哪个。

    只有场景包分类（`agentsec-*` / `ir-*`）才带 —— 理由见 `_VERDICT_TAGS`。

    ⚠️ 判据用前缀函数，不用"verdict 是否非空"：`verdict` 列对**所有**
    卡都有值（默认 `needs_follow_up`），用它判断会让普通题也挂上标签。

    ## ★ v0.41：为什么 `ir-*` 也要带

    应急响应复盘卡片的价值分布与对抗评测**恰好一样**：「这条时间线查了
    但没找到」和「这条时间线确认了」是**两种都要被复用的知识**。
    不给 IR 卡挂标签，下次同类复盘会把一张"已排除"的卡当成待验证线索
    重新查一遍 —— 而那正是方法卡要消除的浪费（见 `AgentsecVerdict`
    的 docstring，那段推理对 IR 逐字适用）。

    ⚠️ 但普通题五类仍然不挂 —— 它们没有第三档语义，理由见 `_VERDICT_TAGS`。
    """
    category = getattr(note, "category", "") or ""
    if not (is_agentsec_category(category) or is_ir_category(category)):
        return ""
    return _VERDICT_TAGS.get(getattr(note, "verdict", "") or "", "")


def render_notes_for_prompt(notes: list[object], *, limit: int = MAX_NOTES_IN_PROMPT) -> str:
    """把几张卡片渲染成 prompt 里的一段（**只读、只渲染**）。

    ## 措辞上刻意强调"这是别的题的经验"

    不这么写的话，模型会把它当成**当前题**的已知条件 —— 而它是
    另一道题的方法，工具名可能对、参数一定不对。

    所以这一段：
      · 明确说"同类**其他**题的经验"；
      · 明确说"只借鉴思路，不要照搬命令"；
      · 附上题目标题，让模型能判断"是不是真的同类"。
    """
    if not notes:
        return ""

    lines = [
        "",
        "## 同类题的经验（★ 是**别的题**的，只借鉴思路）",
        "",
        "下面是本平台解过的同类题的方法记录。**它们不是当前题的已知条件** ——"
        "题目不同，命令与参数都不一样。",
        "",
        "用法：**看思路与工具选择，不要照搬命令**。"
        "如果某条「死路」和你正在试的方向相同，优先换方向。",
    ]
    for note in notes[:limit]:
        title = getattr(note, "_title", None) or getattr(note, "category", "") or "同类题"
        lines.append("")
        # ★ v0.40：把 verdict 写进渲染结果。
        #
        # ## 为什么模型**必须**知道这张卡是命中还是排除
        #
        # `solved=True` 现在覆盖两种卡，而它们的复用方式**相反**：
        #   · 命中（confirmed）的卡教"怎么打中"，照着它试是对的；
        #   · 排除（ruled_out）的卡教"这条路是死的"，照着它试是浪费。
        #
        # 不标出来的话，模型看到"做法：…"就当成可复制的成功路径 ——
        # 而那张卡可能是一张**失败**记录。那正是场景包 §6.3 说的
        # "dead_ends 是 high 价值知识"能不能真的被用上的前提。
        #
        # ⚠️ 只有 agentsec-* 那四类才有这三档语义。普通题的 verdict 是
        #    `confirmed`（解出）或 `needs_follow_up`（没解出），渲染出来
        #    与原来的"**没解出来**"措辞重复，所以空白以外不加标记 ——
        #    保持普通题那五类的 prompt **逐字节不变**。
        verdict_tag = _verdict_tag_for(note)
        lines.append(
            f"- **{title}**（{getattr(note, 'category', '') or '未分类'}）"
            + (f"　{verdict_tag}" if verdict_tag else "")
        )
        approach = getattr(note, "approach", "") or ""
        if approach:
            lines.append(f"  - 做法：{approach}")
        tools = getattr(note, "tools", "") or ""
        if tools:
            lines.append(f"  - 用到的工具：{tools}")
        dead = getattr(note, "dead_ends", "") or ""
        if dead:
            lines.append(f"  - 走不通的方向：{dead}")
    return "\n".join(lines)


def notes_for_challenge(db, challenge, *, limit: int = MAX_NOTES_IN_PROMPT) -> list:  # noqa: ANN001
    """找出与这道题**同类**的方法卡（召回）。

    ## 判据：同分类**或跨场景通用**，优先已解出的，排除自己

    1. **同 `category`** —— 这是当前唯一可靠的相似度信号。
       附件指纹相似度更准，但那需要先下载完附件（同步阶段），
       而召回发生在解题开始前 —— 那时附件可能还没下完。
       ★ v0.56 起再加一条 `OR domain == 'generic'`：跨场景通用卡
       （见 `vocab.NoteDomain`）对**任何**分类都可见。这正是「一个内核
       多场景」—— 在渗透侧学到的方法，应急响应侧也能召回。
    2. **排除自己** —— 注入自己的卡等于把"上次做到哪"当成"别的题的经验"，
       而那是 `resume` 该做的事，两者混淆会让模型分不清。
    3. **已解出的排前面** —— 成功的卡信息量更大；失败的卡（`dead_ends`）
       仍然给，但排后面。

    ## ⚠️ 为什么不做"标题相似度"

    标题相似度会误召回：`[FlareOn3]GUI` 与 `[FlareOn4]GUI` 标题极像，
    但可能一道是 .NET、一道是 Go。而**误召回比不召回更坏** ——
    它会让模型按错误的思路走，而它还以为是"经验"。

    ⚠️ `generic` 这条 `OR` 就是"主动扩大召回面"，与上一条纪律看似矛盾 ——
    所以 `domain` 只在**写入侧被判为跨场景通用**时才置 `generic`（见
    `learn_from_solve` + `normalize_domain`），不是随便哪张卡都能进。
    """
    from sqlalchemy import or_, select

    from app.core.vocab import GENERIC_NOTE_DOMAIN
    from app.kernel.models import KernelSolveNote

    category = (getattr(challenge, "category", "") or "").strip()
    if not category:
        # 没有分类就无法判断"同类" —— 返回空而不是"给全部"。
        # 给全部的后果是把无关题的方法灌进去，比不给更坏。
        return []

    rows = (
        db.execute(
            select(KernelSolveNote)
            .where(
                or_(
                    KernelSolveNote.category == category,
                    KernelSolveNote.domain == GENERIC_NOTE_DOMAIN,
                )
            )
            .where(KernelSolveNote.challenge_id != challenge.id)
            # 已解出的排前面（`solved` 是 bool，降序即 True 优先）。
            .order_by(KernelSolveNote.solved.desc(), KernelSolveNote.updated_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return list(rows)
