"""解题方法卡（v0.32）：跨题复用的知识提炼与召回。

## ★ 这组用例守的**第一件事**：注入内容绝不能带 flag

卡片会被拼进**另一道题**的 prompt。一旦带上旧 flag，就变成
"把 A 题的答案泄漏到 B 题的上下文里" —— 而 B 题的模型完全可能
把它当成 B 题的答案交上去。

所以下面"脱敏"那一节的用例比"功能"那一节更重要。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.vocab import KernelChallengeStatus
from app.kernel import notes as notes_mod
from app.kernel.models import KernelSolveNote


def _note_for(db: Session, challenge_id: str, **overrides) -> KernelSolveNote:  # noqa: ANN003
    """造一张方法卡。

    ⚠️ `challenge_id` **必须指向一条真实存在的题目行** ——
    `ctf2_solve_notes.challenge_id` 有外键，用假 id 会撞
    `ForeignKeyViolation`（实测撞过：`Key (challenge_id)=(ctf2chal_XXX)
    is not present in table "ctf2_challenges"`）。
    所以调用方要么用现成夹具的 id，要么先用 `_make_challenge` 造一行。
    """
    fields = {
        "id": new_id("ctf2note"),
        "challenge_id": challenge_id,
        "category": "REVERSE",
        "solved": True,
        "approach": "做法",
        "tools": "",
        "dead_ends": "",
    }
    fields.update(overrides)
    return KernelSolveNote(**fields)


# ══════════════════════════════════════════════════════════
# 一、★★ 脱敏：flag 绝不进卡片
# ══════════════════════════════════════════════════════════


def test_flag_shaped_text_is_stripped() -> None:
    """★★ 看起来像 flag 的片段必须被剔除。

    这是整个模块存在的**前提条件** —— 卡片会进另一道题的 prompt。
    """
    text = "最后拿到了 flag{th1s_1s_th3_4nsw3r} 然后提交成功了"
    cleaned = notes_mod.sanitize_for_reuse(text)

    assert "flag{" not in cleaned, "★ flag 没有被剔除"
    assert "th1s_1s_th3_4nsw3r" not in cleaned
    # 其余内容保留（剔除的是 flag，不是整句话）
    assert "提交成功" in cleaned


def test_various_flag_prefixes_are_stripped() -> None:
    """★ 各种前缀都要挡（不同赛事用不同前缀）。"""
    for text in (
        "答案是 DASCTF{abc123}",
        "拿到 CTF2{e1775a04-1234} 了",
        "提交 FLAG{upper_case_works}",
        "结果 CTF{simple}",
    ):
        cleaned = notes_mod.sanitize_for_reuse(text)
        assert "{" not in cleaned, f"★ 没剔除：{text!r} → {cleaned!r}"


def test_urls_are_stripped() -> None:
    """★ URL 要剔除 —— 那是**别的题**的靶机地址，注入只会误导。"""
    text = "靶机在 http://10.0.0.5:8080/api/v1 上，直接打它"
    cleaned = notes_mod.sanitize_for_reuse(text)

    assert "http://" not in cleaned
    assert "10.0.0.5" not in cleaned
    assert "直接打它" in cleaned


def test_url_path_is_kept_so_commands_survive() -> None:
    """★★ 掩 host、**保留路径** —— 不能把命令掏成空壳。

    ⚠️⚠️ 这条守的是一个实测缺陷（2026-10-13 dry-run）。第一版把 URL
    整条删光，于是卡片里出现了：

        curl -s -i --max-time 20        ← 看着像做法，实际打不了

    它比"没有这条"更坏：模型会照着它去 curl 一个不存在的地址。
    而"访问了 /robots.txt"恰恰是**可复用的方法知识** —— host 才是
    这一道题特有的东西。
    """
    raw = (
        "curl -s -i http://c436664650adb5d2f6aa1b64.http-ctf2.dasctf.com:80"
        "/robots.txt --max-time 20"
    )
    cleaned = notes_mod.sanitize_for_reuse(raw)

    # ① 靶机地址没有泄漏（host 与端口都要没了）
    assert "dasctf.com" not in cleaned, f"★ 靶机 host 泄漏：{cleaned!r}"
    assert "c436664650adb5d2f6aa1b64" not in cleaned
    # ② 路径保留 —— 这才是可复用的部分
    assert "/robots.txt" in cleaned, f"★ 路径被删，命令成了空壳：{cleaned!r}"
    # ③ 命令形状仍然完整（curl 与它的参数都还在）
    assert cleaned.startswith("curl -s -i")
    assert "--max-time 20" in cleaned


def test_url_only_argument_does_not_leave_a_hollow_command() -> None:
    """★★ 只有 URL 的参数（`http_request` 那种）不该留下空壳。

    这是上一条的另一面：`{"url": "http://..."}` 剥出来只有一个 URL，
    掩成 `<target>` 之后整条命令就只剩一个占位符 —— 那种"做法"没有
    任何指导意义，所以 `key_steps_from_transcript` 必须把它滤掉。
    """
    transcript = '[turn 1] tool http_request {"url": "http://10.0.0.5:8080/admin"}'
    steps = notes_mod.key_steps_from_transcript(transcript)

    assert steps == [], f"★ 只有 URL 的命令不该进卡片：{steps!r}"


def test_key_steps_sanitize_before_judging() -> None:
    """★★ 顺序契约：**先脱敏、再判断值不值得复用**（不能反）。

    ⚠️⚠️ 这是那个残骸缺陷的根因（2026-10-13）。第一版的顺序是
    "先判断 → 再脱敏"，于是：

        curl -s -i http://xxx.dasctf.com/robots.txt --max-time 20
          ↓ 判据：含 `curl` → 值得复用 ✅
          ↓ 脱敏：URL 被删光
        curl -s -i --max-time 20      ← 空壳进了卡片

    判据必须作用在**最终会写进卡片的那个形态**上。这条用例直接断言
    "产出的每一步都不是空壳"：命令不能以选项、管道符或连接符收尾 ——
    那正是被掏空之后的形状。
    """
    transcript = "\n".join(
        [
            '[turn 1] tool run_shell {"command": "curl -s -i '
            'http://c436664650adb5d2f6aa1b64.http-ctf2.dasctf.com:80/robots.txt"}',
            '[turn 2] tool http_request {"url": "http://10.0.0.5:8080/admin"}',
            '[turn 3] tool run_shell {"command": "curl -s http://10.0.0.5:8080/?id=1"}',
            '[turn 4] tool run_shell {"command": "sqlmap -u http://10.0.0.5:8080/?id=1 --batch"}',
        ]
    )
    steps = notes_mod.key_steps_from_transcript(transcript)

    assert steps, "应该至少留下一条可复用的做法"
    for step in steps:
        # 空壳的形状：以选项/管道/连接符收尾，或短到只剩一个占位符。
        assert not step.rstrip().endswith(("|", "&&", "||", "-", "--")), (
            f"★ 出现被掏空的残骸命令：{step!r}"
        )
        assert step.strip() != "<target>", f"★ 只剩占位符：{step!r}"

    # 路径保留下来（那才是可复用的部分），而靶机地址没有泄漏。
    joined = "\n".join(steps)
    assert "/robots.txt" in joined
    assert "dasctf.com" not in joined


def test_flag_inside_a_url_path_is_still_stripped() -> None:
    """★★★ flag 藏在 **URL 路径/参数**里也必须被剔除。

    ⚠️⚠️ 这条守的是"保留路径"那个改动**自己引入的**风险
    （2026-10-13）。第一版把整条 URL 删掉，所以藏在 URL 里的 flag
    会顺带被带走；改成保留路径之后，同一个 flag 就活下来了：

        curl 'http://10.0.0.5/?x=DASCTF{secret}'
          ↓ 掩 host、保留路径
        curl '<target>/?x=DASCTF{secret}'      ← ★ 泄漏！

    而按空白切词 + `FLAG_PATTERN.match`（整串匹配）那一步**挡不住**：
    `'<target>/?x=DASCTF{secret}'` 作为一个词并不匹配 `^前缀{正文}$`。

    所以脱敏里补了一遍子串级剔除。这条用例直接钉住它 ——
    因为这是**注入路径的安全底线**，漏一次就是把 A 题的答案
    送进 B 题的 prompt。
    """
    for raw in (
        "curl -s 'http://10.0.0.5:8080/?x=DASCTF{secret_value}'",
        "curl -s http://10.0.0.5:8080/flag{inner_path_leak}",
        "wget http://10.0.0.5/CTF2{another_one}.txt",
    ):
        cleaned = notes_mod.sanitize_for_reuse(raw)
        assert "{" not in cleaned, f"★★★ URL 里的 flag 泄漏了：{raw!r} → {cleaned!r}"
        assert "secret_value" not in cleaned
        assert "inner_path_leak" not in cleaned
        assert "another_one" not in cleaned


def test_long_random_strings_are_stripped() -> None:
    """★ 32 位以上的 hex/base64 要剔除（可能是凭据或爆破出的口令）。"""
    text = "爆出的口令是 f81ae6f5710cb1340f90cd80d9c33107a1469615bf299e6057dea7f4337f67a3"
    cleaned = notes_mod.sanitize_for_reuse(text)

    assert "f81ae6f5710cb1340f90cd80d9c33107a1469615bf299e6057dea7f4337f67a3" not in cleaned
    assert "爆出的口令是" in cleaned


def test_bare_hostname_is_masked() -> None:
    """★★ **裸 hostname**（没有 `http://` 前缀）也要掩掉。

    ⚠️⚠️ 这条守的是实测漏掉的一批（2026-10-13）：上面那条 URL 规则只认
    `http://` 开头的地址，而靶机经常以裸 hostname 出现：

        timeout 15 nslookup dfc05019f1fa0d78efe8f583.tcp-ctf2.dasctf.com 192.168.44.2
                            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ 泄漏

    实测 29 道真题里这样漏出去 5 处。
    """
    for raw in (
        "nslookup dfc05019f1fa0d78efe8f583.tcp-ctf2.dasctf.com 192.168.44.2",
        "getent hosts c436664650adb5d2f6aa1b64.http-ctf2.dasctf.com",
        "dig +short cb146342b031bdefc50d5935.http-ctf2.dasctf.com",
    ):
        cleaned = notes_mod.sanitize_for_reuse(raw)
        assert "dasctf.com" not in cleaned, f"★ 裸 hostname 泄漏：{cleaned!r}"
        assert "<target>" in cleaned


def test_bare_ipv4_is_masked() -> None:
    """★ 裸 IPv4 也要掩 —— 那是这道题的环境地址。"""
    cleaned = notes_mod.sanitize_for_reuse("nslookup x 192.168.44.2")
    assert "192.168.44.2" not in cleaned
    assert "<target>" in cleaned


def test_hostname_rule_does_not_destroy_method_knowledge() -> None:
    """★★★ 掩 hostname 的判据必须**窄** —— 不能误伤方法知识。

    ⚠️⚠️ 这条是"量出来的"约束，不是想当然（2026-10-13）。我原本打算
    按"含点 + 末尾是字母"来认 hostname，于是先量了一遍**会误伤什么**
    （29 道真题），结果是 **34 种**，而且全是真正的方法知识：

        Image.open    re.findall    urllib.parse    olefile.OleFileIO
        gui.il（×8，是 .NET 的 IL 文件！）   easyre.exe   flag.txt
        index.html    flag.php      bin.relocs.apply

    照那个宽判据脱敏，等于把"掏空命令"那个缺陷换个方向再犯一次 ——
    卡片会被削得读不懂。

    所以判据收窄成"首标签 ≥16 位 hex 且后面还有域名部分"：
    实测 **5 处命中、0 处误伤**。这条用例钉住"0 处误伤"那一面。
    """
    # 这些**必须原样保留** —— 它们不是 hostname，是方法/文件名/API。
    for keep in (
        "Image.open('gui.png')",
        "re.findall(r'x', s)",
        "urllib.parse.quote(s)",
        "olefile.OleFileIO(f)",
        "monodis gui.il > out.il",
        "file easyre.exe",
        "cat flag.txt",
        "bin.relocs.apply=true",
        "f.endswith('.py')",
        "http.request.uri",
    ):
        cleaned = notes_mod.sanitize_for_reuse(keep)
        assert "<target>" not in cleaned, (
            f"★ 误伤了方法知识（判据太宽）：{keep!r} → {cleaned!r}"
        )
        # 内容本身也要还在（不能只是没被掩、却被别的规则删了）。
        head = keep.split()[0].split("(")[0]
        assert head in cleaned, f"★ 内容被削掉了：{keep!r} → {cleaned!r}"


def test_short_hex_hostname_is_not_masked() -> None:
    """★ 短 hex 标签的域名**不该**被掩（判据要求 ≥16 位）。

    这是上一条的另一面：`abc.example.com` 可能是文档里的正常域名，
    掩掉会误伤；而 16 位以上几乎只可能是平台自动生成的环境名。
    """
    cleaned = notes_mod.sanitize_for_reuse("参考 https://docs.example.com/guide")
    assert "<target>" in cleaned  # host 部分仍然掩掉（它是 URL）
    # 但裸的短标签域名不掩：
    cleaned2 = notes_mod.sanitize_for_reuse("见 abc.example.com 的说明")
    assert "abc.example.com" in cleaned2, f"★ 短标签域名被误掩：{cleaned2!r}"


def test_attachment_filename_stem_becomes_a_placeholder() -> None:
    """★★★ 长 hex 的**附件名**：词干换成 `<attachment>`，扩展名保留。

    ⚠️⚠️ 这条守的是"掩裸 hostname"那个改动**自己引入的**误伤
    （2026-10-13）。平台按内容 SHA256 生成附件名，于是它和靶机
    hostname 的**形状完全一样**：

        靶机：  dfc05019f1fa0d78efe8f583.tcp-ctf2.dasctf.com   ← 掩成 <target>
        附件名：9f0ee5dc…adee.pcap                            ← 掩成 <attachment>.pcap

    连踩三个坑，所以判据不是"留"也不是"删"，而是**换词干**：

      ① 当成 hostname → `unzip -o /work/attachments/<target>`（照抄找不到文件）；
      ② 被"32 位以上 hex 是凭据"删掉 → `tshark -r …/.pcap`（文件没了）；
      ③ 留着不掩 → 占掉卡片 160 字预算的 60 字（37%），且对复用无价值
         （换个题目那个文件根本不存在）。

    ★ 保留扩展名是因为它**真的可复用**（"这道题给的是个 pcap"）；
      而 `<target>` 与 `<attachment>` 分工不同：前者是要连的网络目标，
      后者是要读的本地文件 —— 合成一个会让模型分不清。
    """
    raw = (
        "unzip -o /work/attachments/"
        "e06db57b2e9dea955fccb9679ba1c556de694cc1735c7eeb3ecfe0f5af871bae.zip"
    )
    cleaned = notes_mod.sanitize_for_reuse(raw)

    assert "<attachment>.zip" in cleaned, f"★ 附件名没换占位符：{cleaned!r}"
    assert "<target>" not in cleaned, f"★ 附件名被当成靶机：{cleaned!r}"
    assert "e06db57b2e9dea955fccb9679ba1c556de694cc1735c7eeb3ecfe0f5af871bae" not in cleaned

    # 而真正的靶机 hostname 仍然掩成 `<target>`（不能变成 <attachment>.tcp）。
    cleaned2 = notes_mod.sanitize_for_reuse(
        "nslookup dfc05019f1fa0d78efe8f583.tcp-ctf2.dasctf.com"
    )
    assert "<target>" in cleaned2
    assert "dasctf.com" not in cleaned2
    assert "<attachment>" not in cleaned2, (
        f"★ 靶机被误判成附件（`.tcp` 看着像扩展名）：{cleaned2!r}"
    )


def test_truncated_commands_are_dropped() -> None:
    """★★ 被截断的**半句命令**不能进卡片（照着抄会报错）。

    ⚠️⚠️ 这是实测发现的（2026-10-13）：transcript 落盘时命令被
    `_clip(..., 500)` 截过，而**全库 604 条 tool 行里有 111 条被截断**
    （18%）。截断处的形状就是"断在半句"：

        ... print('total:',repr(buf)) s.close() EOF python3
        cd /work/gui && grep

    它**看起来像一条做法**，但照着抄直接语法错误 —— 所以宁可丢掉。
    """
    for bad in (
        "cd /work/gui && grep",
        "python3 -c 'print(1)' &&",
        "cat /work/x.txt |",
        "cd /work && curl -s -i",  # 被掏空：只剩命令名 + 选项，没有目标
    ):
        assert not notes_mod._command_worth_reusing(bad), f"★ 残骸进了卡片：{bad!r}"


def test_complete_piped_commands_are_kept() -> None:
    """★★ 完整的**管道**命令要留下 —— 别把截断判据用过头。

    这是上一条的反面。`cmd | head` / `| sort` / `| uniq` 是**完整**的
    （它们从 stdin 读），实测里它们是卡片里最有价值的部分之一
    （`tshark … | sort | uniq -c | sort -rn | tail -60`）。

    ⚠️ 判据放宽的代价不对称：把完整命令误判成残骸会**白丢知识**，
    而放过一条残骸代价小得多（模型看得出 `... python3` 不完整）。
    """
    for good in (
        "tshark -r x.pcap -Y http -T fields -e http.request.uri | sort | uniq -c | sort -rn | tail -60",
        "strings -n 6 easyre.exe | head -80",
        "objdump -d x | grep -A3 call",
        "xxd x.bin | head -20",
        # ⚠️ 以选项收尾但**有位置参数**的命令是完整的（实测里真有）。
        "hashcat -b -m 9500 --force",
        # ⚠️ `\;` 是 find -exec 的终止符，不是续行符（实测里真有）。
        "find . -type f -exec file {} \\;",
    ):
        assert notes_mod._command_worth_reusing(good), f"★ 完整命令被误丢：{good!r}"


def test_short_hex_is_kept() -> None:
    """★ 短 hex **不该**被剔除（否则会误伤偏移量、短哈希这类正常内容）。

    这条挡的是"把阈值调得太低"：8 位 hex 在方法描述里很常见
    （`0x401000`、`deadbeef`），剔除它们会让描述变得读不懂。
    """
    text = "跳转到 0x401000，特征是 deadbeef"
    cleaned = notes_mod.sanitize_for_reuse(text)

    assert "0x401000" in cleaned
    assert "deadbeef" in cleaned


def test_sanitize_respects_the_length_limit() -> None:
    """★ 超过上限要截断（卡片不能撑爆 prompt 预算）。"""
    cleaned = notes_mod.sanitize_for_reuse("啊" * 5000, limit=100)
    assert len(cleaned) == 100


def test_sanitize_handles_empty_input() -> None:
    """★ 空输入返回空串（不抛）。"""
    assert notes_mod.sanitize_for_reuse("") == ""
    assert notes_mod.sanitize_for_reuse("   ") == ""


# ══════════════════════════════════════════════════════════
# 二、从解题产物提炼字段
# ══════════════════════════════════════════════════════════


def test_tools_are_extracted_from_transcript() -> None:
    """★ 从 transcript 里提取工具名（去重、保序）。

    ⚠️ 这里提取的是**命令里真正用到的解题工具**（`strings`），
    而不是包装层名字（`run_shell`）—— 理由见
    `test_tools_prefer_real_tool_names_over_wrappers`。
    """
    transcript = "\n".join(
        [
            "[turn 1] tool run_shell ls -la",
            "[turn 1] result: 一堆输出",
            "[turn 2] tool read_attachment x.zip",
            "[turn 2] result: 二进制",
            "[turn 3] tool run_shell strings x.zip",
            "[turn 3] result: 更多输出",
        ]
    )
    tools = notes_mod.tools_from_transcript(transcript)

    assert tools == "run_shell, read_attachment, strings", f"实际：{tools!r}"


def test_tools_prefer_real_tool_names_over_wrappers() -> None:
    """★★ `tools` 字段必须是**真工具名**，不能是包装层名字。

    ⚠️⚠️ 这条守的是一个实测缺陷（2026-10-13 dry-run）：第一版直接取
    `tool` 后面那个词，于是 29 张卡的 `tools` 几乎全是

        用到的工具：run_shell
        用到的工具：run_shell, http_request

    **零信息量** —— 那是我们自己的沙箱包装层，模型看到它学不到东西。

    现在从命令正文里认工具（按词边界）；认不出时（`http_request`
    只带一个 URL）才退回包装层名 —— 那时"做过一次 HTTP 请求"仍是
    个有用的事实。
    """
    transcript = "\n".join(
        [
            '[turn 1] tool run_shell {"command": "objdump -d easyre.exe | head"}',
            '[turn 2] tool run_shell {"command": "r2 -q -c \'izz~flag\' easyre.exe"}',
            '[turn 3] tool run_shell {"command": "python3 -c \'print(1)\'"}',
        ]
    )
    tools = notes_mod.tools_from_transcript(transcript)

    assert "run_shell" not in tools, f"★ 包装层名字进了卡片：{tools!r}"
    assert "objdump" in tools
    assert "r2" in tools
    assert "python3" in tools


def test_short_tool_names_do_not_match_as_substrings() -> None:
    """★★ 短工具名（`nc` / `file` / `r2`）不能按**子串**匹配。

    ⚠️⚠️ 第一版是 `any(tool in low for tool in _REUSABLE_TOOLS)`，而
    表里有 `nc` / `file` / `r2` 这种两三个字母的词，子串匹配的误判率
    高得离谱：

      · `nc` 命中 `sync` / `func` / `encode`；
      · `file` 命中 `profile` / `filename`；
      · `r2` 命中 `var2` / `bar2`。

    后果不是"多留几条"：它让**几乎任何命令**都被判成"值得复用"，
    于是卡片塞满 `cd /work && ls -la` 这种每道题都跑的噪音 ——
    实测 5 张卡的首步完全相同。

    ⚠️ 下面这些用例是**精心挑的**：它们里除了那个子串之外，
    **没有任何**真工具、也没有 `sed -n` / `grep -n` / 自写脚本 ——
    所以一旦判成 True，只可能是子串匹配造成的。
    （早先我用 `python3 -c '…sync()'` 当反例，那是**错的**：
    它确实含真工具 `python3`，判 True 本来就对。）
    """
    for noise in (
        "echo sync_done",       # 含 `nc` 子串（在 sync 里）
        "echo profile_loaded",  # 含 `file` 子串（在 profile 里）
        "echo var2_value",      # 含 `r2` 子串（在 var2 里）
        "echo encode_result",   # 含 `nc` 子串（在 encode 里）
    ):
        assert not notes_mod._command_worth_reusing(noise), (
            f"★ 子串误判为可复用：{noise!r}"
        )

    # 而真正的调用必须认出来（词边界不该把真工具也挡掉）。
    assert notes_mod._command_worth_reusing("nc -lvnp 4444")
    assert notes_mod._command_worth_reusing("file easyre.exe")
    assert notes_mod._command_worth_reusing("r2 -q -c izz x")
    # ⚠️ `python3.13` 这种带点的也要认（否定环视允许 `.`）。
    assert notes_mod._command_worth_reusing("python3.13 solve.py")


def test_tools_are_capped() -> None:
    """★ 工具名数量有上限（防止一条卡占太多 prompt）。"""
    transcript = "\n".join(f"[turn {i}] tool tool_{i} cmd" for i in range(50))
    tools = notes_mod.tools_from_transcript(transcript, limit=5)
    assert len(tools.split(", ")) == 5


def test_build_note_fields_never_includes_the_flag() -> None:
    """★★ 候选 flag **一个字都不进卡片**。

    `candidates` 出现在签名里只是为了区分"解出来了"与"没解出来"
    两种措辞 —— 它的**值**绝不落进任何字段。
    """
    fields = notes_mod.build_note_fields(
        transcript="[turn 1] tool run_shell cat /flag\n[turn 1] result: flag{secret}",
        error=None,
        candidates=["flag{secret_value}"],
        solved=True,
    )

    blob = " ".join(fields.values())
    assert "secret_value" not in blob, "★ flag 原文进了卡片"
    assert "flag{" not in blob


def test_failed_solve_records_the_dead_end() -> None:
    """★ 失败时把原因记进 `dead_ends` —— 那是卡片里最省时间的部分。"""
    fields = notes_mod.build_note_fields(
        transcript="[turn 1] tool run_shell objdump -d x\n[turn 1] result: 看不懂",
        error="回合用尽（80 轮）仍未给出结论。",
        candidates=[],
        solved=False,
    )

    assert "回合用尽" in fields["dead_ends"]
    assert "没解出来" in fields["approach"]


def test_solved_solve_has_no_dead_end() -> None:
    """★ 解出来时不写 `dead_ends`（没有"走不通"要记）。"""
    fields = notes_mod.build_note_fields(
        transcript="[turn 1] tool run_shell ls",
        error=None,
        candidates=["flag{x}"],
        solved=True,
    )
    assert fields["dead_ends"] == ""
    assert "解出来了" in fields["approach"]


def test_dead_end_is_sanitized_too() -> None:
    """★★ `dead_ends` 也要脱敏 —— 失败原因里可能带 flag 片段。

    实测里模型常在错误信息里引用它试过的候选（"flag{xxx} 被判错"），
    而那条信息会被写进卡片。
    """
    fields = notes_mod.build_note_fields(
        transcript="",
        error="平台判定 flag{wrong_guess} 不正确",
        candidates=[],
        solved=False,
    )
    assert "wrong_guess" not in fields["dead_ends"]
    assert "平台判定" in fields["dead_ends"]


# ══════════════════════════════════════════════════════════
# 三、召回
# ══════════════════════════════════════════════════════════


def test_recall_only_returns_the_same_category(
    db_session: Session, ctf2_challenge, ctf2_competition
) -> None:
    """★★ 只召回**同分类**的卡。

    不同分类的方法不能混用（逆向的经验对 crypto 题是噪音）。
    """
    from tests.kernel.conftest import _make_challenge  # noqa: PLC2701

    # ⚠️ 卡片的 challenge_id 有外键 —— 必须造**真实题目行**。
    same = _make_challenge(db_session, ctf2_competition, remote_id="same-cat")
    diff = _make_challenge(db_session, ctf2_competition, remote_id="diff-cat")

    db_session.add(
        _note_for(
            db_session,
            same.id,
            category=ctf2_challenge.category,
            approach="同类题的做法",
            tools="run_shell",
        )
    )
    db_session.add(
        _note_for(
            db_session,
            diff.id,
            category="完全不同的分类",
            approach="异类题的做法",
            tools="run_shell",
        )
    )
    db_session.commit()

    rows = notes_mod.notes_for_challenge(db_session, ctf2_challenge)
    approaches = [r.approach for r in rows]

    assert "同类题的做法" in approaches
    assert "异类题的做法" not in approaches, "★ 异类卡被召回了"


def test_recall_excludes_the_challenge_itself(
    db_session: Session, ctf2_challenge
) -> None:
    """★★ 不召回**自己**的卡。

    注入自己的卡等于把"上次做到哪"当成"别的题的经验" —— 而那是
    `resume` 该做的事。两者混淆会让模型分不清。
    """
    db_session.add(
        _note_for(
            db_session,
            ctf2_challenge.id,  # ← 自己
            category=ctf2_challenge.category,
            solved=False,
            approach="自己的上次记录",
        )
    )
    db_session.commit()

    rows = notes_mod.notes_for_challenge(db_session, ctf2_challenge)
    assert all(r.challenge_id != ctf2_challenge.id for r in rows), "★ 召回了自己"


def test_recall_puts_solved_notes_first(
    db_session: Session, ctf2_challenge, ctf2_competition
) -> None:
    """★ 已解出的卡排前面（成功的经验信息量更大）。"""
    from tests.kernel.conftest import _make_challenge  # noqa: PLC2701

    for solved, approach in ((False, "失败的经验"), (True, "成功的经验")):
        ch = _make_challenge(db_session, ctf2_competition, remote_id=f"order-{solved}")
        db_session.add(
            _note_for(
                db_session,
                ch.id,
                category=ctf2_challenge.category,
                solved=solved,
                approach=approach,
            )
        )
    db_session.commit()

    rows = notes_mod.notes_for_challenge(db_session, ctf2_challenge)
    assert rows, "应当召回至少一张"
    assert rows[0].solved is True, "★ 已解出的卡没排在前面"


def test_recall_puts_ruled_out_notes_with_the_solved_ones(
    db_session: Session, ctf2_competition
) -> None:
    """★★ `ruled_out` 的卡现在与已解出的卡一起排在 `needs_follow_up` 前面。

    ## 为什么这条值得单独守

    召回排序是 `solved.desc(), updated_at.desc()`（见
    `notes_for_challenge`）。v0.40 放宽了 `solved` 的语义 —— 一张
    「跑完全套手法、确认此路不通」的卡现在也 `solved=True`，于是
    **它开始进入前排**，而在此之前它根本挤不进 `limit=3` 的那三个位置。

    这是**有意**的二阶效应：一张写着「这条路不通，我试过 X/Y/Z」的卡，
    对下一道同类题的价值不低于一张「成功经验」—— 前者省掉的是整轮
    白跑。但如果没人把这条排序钉住，将来某次改动把 `solved` 的语义
    收紧回 `confirmed`，症状是"排除经验又悄悄不出现了"，而那时候
    没有任何测试会红。

    ## ★ 为什么必须端到端（走 `learn_from_solve`）而不是直接造行

    第一版直接 `_note_for(solved=True, verdict="ruled_out")` —— 那是把
    **结论**当输入写进去。于是把 `counts_as_solved` 里的 `RULED_OUT`
    摘掉，这条测试照样绿：它验证的是"我自己把 solved 填成了 True"。

    现在两张卡都由 `learn_from_solve` 生成，`solved` 由
    `counts_as_solved(resolve_verdict(...))` 自己算出来落库 ——
    摘掉那一个常量，`ruled_out` 那张就 `solved=False`，它会被
    `updated_at` 更晚的 `needs_follow_up` 挤到后面，断言随即变红。
    """
    from datetime import timedelta

    from sqlalchemy import update

    from app.core.vocab import AgentsecVerdict
    from tests.kernel.conftest import _make_challenge  # noqa: PLC2701

    _turn_agentsec_on(db_session)
    cat = "agentsec-prompt-injection"
    # 三道题行：两道用来产生卡片，一道用来召回（召回会排除自己）。
    recall_ch = _make_challenge(
        db_session, ctf2_competition, remote_id="order-recall", category=cat
    )
    ruled = _make_challenge(
        db_session, ctf2_competition, remote_id="order-ruled", category=cat
    )
    pending = _make_challenge(
        db_session, ctf2_competition, remote_id="order-pending", category=cat
    )

    transcript = "\n".join(
        [
            '[turn 1] tool run_shell {"command": "curl -s -i http://h/"}',
            '[turn 2] tool run_shell {"command": "ls -la /work"}',
        ]
    )
    # ① 跑完全套话术、模型写了排除理由 → 证据链齐全 → ruled_out。
    ruled_note = _learn(
        db_session,
        ruled,
        _FakeOutcome(
            transcript=transcript,
            error="",
            ruled_out=["排除了 PI-03 越狱 —— 三种话术全部被拒绝，无越界输出"],
        ),
    )
    assert ruled_note.verdict == AgentsecVerdict.RULED_OUT.value, (
        f"证据链应当齐全，实际 verdict={ruled_note.verdict}\n{ruled_note.dead_ends}"
    )
    assert ruled_note.solved is True, "★ produced 的卡没被算成已解决"

    # ② 只报了个错误、什么都没排除 → needs_follow_up。
    pending_note = _learn(
        db_session,
        pending,
        _FakeOutcome(transcript=transcript, error="命令超时，未得出结论"),
    )
    assert pending_note.verdict == AgentsecVerdict.NEEDS_FOLLOW_UP.value
    assert pending_note.solved is False

    # 把「还在跟进」那张的时间明确推后：否则两条只差几百微秒，
    # 库的精度一不够就成了同秒，`updated_at.desc()` 的次序不确定，
    # 这条测试会偶发飘红 —— 而偶发飘红的测试等于没有。
    db_session.execute(
        update(KernelSolveNote)
        .where(KernelSolveNote.challenge_id == pending.id)
        .values(updated_at=ruled_note.updated_at + timedelta(seconds=60))
    )
    db_session.commit()
    db_session.expire_all()

    rows = notes_mod.notes_for_challenge(db_session, recall_ch)
    assert [r.challenge_id for r in rows] == [ruled.id, pending.id], (
        "★ 已排除的卡没排在还在跟进的卡前面 —— "
        "「跑完全套确认不通」的经验被当作未解决挤出了召回"
    )


def test_recall_returns_empty_without_a_category(
    db_session: Session, ctf2_competition
) -> None:
    """★★ 题目没有分类时**返回空**，而不是"给全部"。

    给全部的后果是把无关题的方法灌进 prompt —— 比不给更坏。

    ## ⚠️ 这条测试的第一版是**假的**（变异验证抓到的）

    第一版只造了一个"无分类的假题目"对象，**没有在库里放任何卡片** ——
    于是即使把 `if not category: return []` 整条拆掉，查询也查不到东西、
    照样返回空，断言**照样通过**。

    它验证的是"恰好没有数据"，而不是"那条早退分支在起作用"。

    所以现在**先造一张必然会被查到的卡**（分类为空串），再断言
    "无分类的题目仍然返回空" —— 只有早退分支真的在，才会返回空。
    """
    from tests.kernel.conftest import _make_challenge  # noqa: PLC2701

    # 造一张**真实存在**的卡，分类故意设成空串 ——
    # 这样"无分类"的题目如果没走早退分支，就会查到它。
    ch = _make_challenge(db_session, ctf2_competition, remote_id="nocat-note")
    db_session.add(_note_for(db_session, ch.id, category="", approach="空分类的做法"))
    db_session.commit()

    class _NoCategory:
        id = "ctf2chal_NO_CATEGORY_AT_ALL"
        category = ""

    rows = notes_mod.notes_for_challenge(db_session, _NoCategory())
    assert rows == [], (
        "★ 无分类时返回了卡片 —— 早退分支没生效，"
        "而它挡的是「把无关题的方法灌进 prompt」"
    )


def test_recall_respects_the_limit(
    db_session: Session, ctf2_challenge, ctf2_competition
) -> None:
    """★ 召回数量有上限。"""
    from tests.kernel.conftest import _make_challenge  # noqa: PLC2701

    for i in range(10):
        ch = _make_challenge(db_session, ctf2_competition, remote_id=f"many-{i}")
        db_session.add(
            _note_for(
                db_session,
                ch.id,
                category=ctf2_challenge.category,
                approach=f"做法 {i}",
            )
        )
    db_session.commit()

    rows = notes_mod.notes_for_challenge(db_session, ctf2_challenge, limit=3)
    assert len(rows) == 3


# ══════════════════════════════════════════════════════════
# 四、渲染成 prompt 片段
# ══════════════════════════════════════════════════════════


def test_render_says_these_are_other_challenges(db_session: Session) -> None:
    """★★ 渲染时必须**明说**"这是别的题的经验"。

    不这么写的话，模型会把它当成当前题的已知条件 —— 而它是另一道题
    的方法，工具名可能对、参数一定不对。
    """
    note = KernelSolveNote(
        id=new_id("ctf2note"),
        challenge_id="ctf2chal_RENDER_TEST",
        category="REVERSE",
        solved=True,
        approach="用 monodis 反汇编",
        tools="monodis, mono",
        dead_ends="",
    )
    text = notes_mod.render_notes_for_prompt([note])

    assert "别的题" in text, "★ 没说清这些经验来自别的题"
    assert "不要照搬命令" in text, "★ 没提醒不要照搬"
    assert "monodis" in text


def test_render_includes_dead_ends(db_session: Session) -> None:
    """★ 走不通的方向也要渲染（那是卡片里最省时间的部分）。"""
    note = KernelSolveNote(
        id=new_id("ctf2note"),
        challenge_id="ctf2chal_DEADEND_TEST",
        category="REVERSE",
        solved=False,
        approach="没解出来",
        tools="",
        dead_ends="手工 patch 元数据走不通",
    )
    text = notes_mod.render_notes_for_prompt([note])
    assert "手工 patch 元数据走不通" in text


def test_render_empty_list_gives_empty_string() -> None:
    """★ 没有卡片时渲染成空串（而不是一段空标题）。"""
    assert notes_mod.render_notes_for_prompt([]) == ""


# ══════════════════════════════════════════════════════════
# 五、接进 prompt
# ══════════════════════════════════════════════════════════


def test_prompt_includes_notes_when_present() -> None:
    """★ 有卡片时 prompt 里能看到它。"""
    from app.kernel import runner

    spec = runner.SolveSpec(
        challenge_id="ctf2chal_X",
        title="某题",
        category="REVERSE",
        description="题面",
        profile=runner.IMAGE_BASE,
        access_urls=(),
        attachments=(),
        llm_model="m",
        # ⚠️ `llm_api_key` 是**必填**（无默认值）—— 它标了
        #    `field(repr=False)` 但仍是必填。这里给一个明显的假值。
        llm_api_key="test-key-not-real",
        notes_text="\n## 同类题的经验（★ 是**别的题**的）\n- 做法：用 monodis",
    )
    prompt = runner._initial_prompt(spec)
    assert "同类题的经验" in prompt
    assert "monodis" in prompt


def test_prompt_has_no_notes_section_when_empty() -> None:
    """★ 没卡片时 prompt 里**不该**出现那个标题（避免噪音）。"""
    from app.kernel import runner

    spec = runner.SolveSpec(
        challenge_id="ctf2chal_Y",
        title="某题",
        category="MISC",
        description="题面",
        profile=runner.IMAGE_BASE,
        access_urls=(),
        attachments=(),
        llm_model="m",
        llm_api_key="test-key-not-real",
        notes_text="",
    )
    prompt = runner._initial_prompt(spec)
    assert "同类题的经验" not in prompt

# ══════════════════════════════════════════════════════════
# ★★ 脱敏与"教人找 flag 的命令"的冲突（实测回归）
#
#   十道 mock 题跑方法卡 A/B 时发现：两张卡里的核心命令被脱敏
#   吃成了语法残骸 ——
#
#       grep -ao 'flag{[^}]*}' dump.bin
#     → grep -ao ']*}' dump.bin          ← 照抄必然报错
#
#   根因是 `_inline_flag()` 的子串版不要求那个 `}` 是真右花括号，
#   于是把 `flag{[^}` 当成 flag 删掉了。下面两条是对这个 bug 的
#   **双向**钉住：既不能因此漏掉真 flag，也不能再把命令砍断。
# ══════════════════════════════════════════════════════════


def test_grep_regex_for_flag_survives_sanitizing() -> None:
    """★★ 教人用正则找 flag 的命令必须活下来。

    这是"脱敏规则与方法卡内容重叠互相吃字符"的直接回归 —— 实测
    `arena-1004`/`arena-1010` 两张卡都被砍成这样。
    """
    for raw in (
        "1. grep -ao 'flag{[^}]*}' /work/attachments/dump.bin",
        "grep -ao 'flag{[a-z]*}' /work/dump.bin",
        'python3 -c "i=d.find(b\'flag{\'); print(d[i-60:i+40])"',
    ):
        cleaned = notes_mod.sanitize_for_reuse(raw)
        # 关掉空白差异：脱敏会折叠空白
        flat = " ".join(cleaned.split())
        flat_raw = " ".join(raw.split())
        assert flat == flat_raw, f"★ 命令被砍断了：{raw!r} → {cleaned!r}"


def test_real_flag_inside_quotes_is_still_stripped() -> None:
    """★ 引号里的**真 flag** 仍然要删 —— 别为了保命令把答案放出去。

    `echo 'flag{abc_def}'` 是"引号包住整段"的形状，正文没有正则元
    字符，所以判据走"删"这一支。
    """
    for raw in (
        "echo 'flag{abc_def}'",
        'curl -s "<target>/?x=DASCTF{secret}"',
    ):
        cleaned = notes_mod.sanitize_for_reuse(raw)
        assert "flag{abc_def}" not in cleaned
        assert "DASCTF{secret}" not in cleaned


def test_flag_assembled_from_two_chunked_pieces_is_stripped() -> None:
    """★★ 被切成两段、正文含 `[ ]` 的 flag 也不能漏。

    实测 `#1006`：`flag{format_'，偏移 1069 -> b'[debug] token_part2 =
    string_leak}` —— 正文里有 `[` 和 `]`。如果判据只看"正文有没有元
    字符"，这条会**被当成正则放过**，而那正是答案。
    """
    raw = (
        "flag{format_'，偏移 1069 -> b'[debug] token_part2 = string_leak}"
    )
    cleaned = notes_mod.sanitize_for_reuse(raw)
    assert "token_part2" not in cleaned or "flag{" not in cleaned


def test_escaped_quote_is_not_a_literal_opener() -> None:
    """★ JSON 转义出来的 `\"` 不是"引号字面量的开头"。

    实测 `#1008`：`checker.c 中 a=\"flag{...}` —— 那个 `"` 前面有反斜
    杠。把它当成引号开头 → 整段被放过 → 泄漏。

    ## ⚠️ 这条用例原先**测的不是这件事**（2026-10-19 修）

    原来写的是单引号字符串 `'...a=\"flag{...}'` —— 单引号里 `\"` 就是
    `"`，**反斜杠根本不存在**。于是它测的是"引号前没有反斜杠"这个相反
    的情形，断言恒失败：`_is_unescaped_quote` 看到干净的 `"`，判据正确
    地把它当搜索针放过了，而用例要它删掉。

    ⇒ 守着脱敏核心判据的用例，自己构造的输入是另一回事。这类错误比
    断言写错更隐蔽：它永远红，而红的原因看起来像是"判据坏了"。
    改成真正的反斜杠（双引号字符串里写 `\\"`），并把**前提**先断言出来，
    免得哪天又被某一层转义吃掉一次。
    """
    raw = "checker.c 中 a=\\\"flag{patch_me_if_you_can}"
    # 前提：这串里必须真的有一个反斜杠，否则测的就不是"转义引号"这件事。
    assert "\\" in raw
    cleaned = notes_mod.sanitize_for_reuse(raw)
    assert "patch_me_if_you_can" not in cleaned


# ── 从工具清单反推细分题型（v0.46）─────────────────────────
#
# 为什么要测：这两个函数是「平台只给大类、想回看 heap 题多不多」的唯一依据。
# 一旦它们静默地返回空字符串，统计里就会多出一个叫「推不出来」的假类，
# 而不是报错。


def test_derive_subcategory_picks_the_specific_branch() -> None:
    """工具清单能推出就必须推出，不能漏成空串。"""
    cases = {
        "cyclic,ropgadget,objdump": "pwn-stack",
        "gdb,cyclic": "pwn-stack",
        "one_gadget,python3": "pwn-heap",
        "pwninit,libc-database": "pwn-heap",
        "fmtstr_payload": "pwn-fmt",
    }
    for raw, want in cases.items():
        assert notes_mod.derive_subcategory(raw) == want, raw


def test_derive_subcategory_returns_empty_when_no_evidence() -> None:
    """没证据就是空串，**不能**归成一个叫「普通 pwn」的类。"""
    for raw in ("", "objdump,file", "gdb,patchelf,seccomp-tools", "checksec"):
        assert notes_mod.derive_subcategory(raw) == "", raw


def test_tools_from_transcript_recognises_the_p0_pwn_tools() -> None:
    """P0 补进镜像的那批工具必须被认出来。

    反向验证：`_REUSABLE_TOOLS` 里写成大写 `ROPgadget` 会永远命中不到
    且不报错（`_tools_in_command` 先 `lower()` 再原样匹配），
    所以这里专门用真实大小写。
    """
    transcript = (
        "[turn 1] tool run_shell {\"command\": \"ROPgadget --binary ./pwn_baby\"}\n"
        "[turn 2] tool run_shell {\"command\": \"one_gadget /lib/x86_64-linux-gnu/libc.so.6\"}\n"
        "[turn 3] tool run_shell {\"command\": \"cyclic 200\"}\n"
        "[turn 4] tool run_shell {\"command\": \"seccomp-tools dump ./pwn_baby\"}\n"
        "[turn 5] tool run_shell {\"command\": \"patchelf --print-interpreter ./pwn_baby\"}\n"
    )
    kinds = notes_mod.tools_from_transcript(transcript)
    for want in ("ropgadget", "one_gadget", "cyclic", "seccomp-tools", "patchelf"):
        assert want in kinds, f"{want} 没被认出来：{kinds!r}"


def test_tools_from_transcript_ignores_plain_commands() -> None:
    """普通命令不能凭空冒出具体工具名。

    这里的预期是退回包装层名字 —— `tools_from_transcript` docstring
    明写「认不出具体工具时退回包装层名字」，那时「做过一次命令行调用」
    仍是个有用的事实。写成 `== ""` 是把一个故意的设计当成 bug。
    """
    transcript = (
        "[turn 1] tool run_shell {\"command\": \"echo hello\"}\n"
        "[turn 2] tool run_shell {\"command\": \"sync\"}\n"
        "[turn 3] tool run_shell {\"command\": \"cargo build\"}\n"
    )
    assert notes_mod.tools_from_transcript(transcript) == "run_shell"
    # 关键是：不能把子串当成工具（"sync" 里有 "nc"，"cargo build" 里什么都没有）。
    assert notes_mod.derive_subcategory("run_shell") == ""


def test_flag_leak_predicate_matches_sanitizer() -> None:
    """★★ 落盘闸门与脱敏必须用**同一份**判据。

    `scripts/ctf2_baseline.py` 的 `_card_leaks` 原来见 `flag{` 就拒收整
    张卡；脱敏修好之后它反过来把刚保住的搜索针当泄漏，`#1004` 和
    `#1010` 两张卡被整张丢弃（实测）。两个判据分叉就会重演。

    ⚠️ 这道判据**改过名**：原来叫 `looks_like_flag_search_needle`，语义
       是反的（"没有 flag 形状"与"都是搜索针"都返回 False），而调用方把
       False 读成"拒收" ⇒ 纯搜索针的卡也被拒。名字换成它真正回答的问题。
    """
    # 搜索针（有闭合花括号）：不算泄漏
    assert not notes_mod.contains_unescaped_flag(
        "grep -ao 'flag{[^}]*}' /work/dump.bin"
    )
    # ★ 搜索针（**没有**闭合花括号）：`_inline_flag()` 匹配不到它，
    #   靠第二条判据放行。实测 `#1010` 的 r4 轮就是死在这一条上：
    #   触发用 `\bflag\{` 看得见、裁定用 `_inline_flag()` 看不见，
    #   于是整张卡被判泄漏丢弃。
    assert not notes_mod.contains_unescaped_flag(
        "grep -abo 'flag{' /work/attachments/dump.bin"
    )
    # 真答案：算泄漏
    assert notes_mod.contains_unescaped_flag("echo 'flag{abc_def}'")
    # 裸在正文里的：算泄漏
    assert notes_mod.contains_unescaped_flag("答案是 flag{th1s_1s_th3_4nsw3r}")
    # 残缺形状裸在正文里（不在引号内）：也算泄漏
    assert notes_mod.contains_unescaped_flag("found flag{ partially")
    # 空内容：不算泄漏（调用方语义是"要不要拒收"）
    assert not notes_mod.contains_unescaped_flag("")
    # 正常代码别误伤：`a{2}` 这类不是 flag
    assert not notes_mod.contains_unescaped_flag("sed 's/a{2}/b/' file.txt")


def test_sanitizer_and_flag_leak_predicate_agree() -> None:
    """★★ 交叉一致性：**完整形状**上两边必须是同一份判据。

    为什么只说"完整形状"：`found flag{ partially` 这种**残缺**形状脱敏
    根本不碰（`_inline_flag()` 匹配不到它），而闸门必须判泄漏 —— 否则
    正文里裸着的半个 flag 就漏过去了。所以不存在"闸门判泄漏 ⇒ 脱敏删了"
    这种单向蕴含，写成那样这条测试就会要求 `found flag{ partially` 放行
    （实测踩过，方向写错比不写更坏）。

    真正要钉死的是：**flag 家族里脱敏保住的，闸门必须放行** —— 否则就是
    `#1010` 那次"脱敏修好了、卡却学不到"的重演。
    """
    # 搜索针：脱敏保住 ⇔ 闸门放行（两边同一份判据）
    needles = [
        "grep -ao 'flag{[^}]*}' /work/dump.bin",
        "grep -ao 'flag{[a-z]*}' /work/dump.bin",
        "grep -abo 'flag{' /work/attachments/dump.bin",
    ]
    for raw in needles:
        assert "flag{" in notes_mod.sanitize_for_reuse(raw), (
            f"★ 该保住的搜索针被脱敏删了：{raw!r}"
        )
        assert not notes_mod.contains_unescaped_flag(raw), (
            f"★ 脱敏保住、闸门仍判泄漏：{raw!r}"
        )

    # 真答案：脱敏必删、闸门必判泄漏（两边同一份判据）
    answers = [
        "echo 'flag{abc_def}'",
        "答案是 flag{th1s_1s_th3_4nsw3r}",
    ]
    for raw in answers:
        assert "flag{" not in notes_mod.sanitize_for_reuse(raw), (
            f"★ 答案没被脱敏删掉：{raw!r}"
        )
        assert notes_mod.contains_unescaped_flag(raw), (
            f"★ 脱敏删了、闸门却不判泄漏：{raw!r}"
        )

    # 残缺形状裸在正文：脱敏不管它，闸门必须拦下（这一档只有一个方向）
    assert notes_mod.contains_unescaped_flag("found flag{ partially")


# ══════════════════════════════════════════════════════════
# ★ v0.40：`learn_from_solve` 的 verdict 落库
# ══════════════════════════════════════════════════════════


class _FakeOutcome:
    """`learn_from_solve` 读的就是这三个属性。

    ⚠️ `ruled_out` 是**必须**有的（v0.40）：`learn_from_solve` 用
    模型写在 FINAL 里的排除清单去补齐那四项证据链，缺了这个属性
    会 `AttributeError`。真实类型是 `runner.SolveOutcome`，那里的
    默认值也是空列表。
    """

    def __init__(
        self,
        transcript: str = "",
        error: str | None = None,
        ruled_out: list[str] | None = None,
    ) -> None:
        self.transcript = transcript
        self.error = error
        self.ruled_out: list[str] = list(ruled_out or [])
        self.candidates: list[object] = []


def _learn(db: Session, challenge, outcome) -> KernelSolveNote:  # noqa: ANN001
    """跑一次 `learn_from_solve` 并把落库的卡读回来。"""
    from app.kernel.service import learn_from_solve

    learn_from_solve(db, challenge, outcome)
    db.expire_all()
    return (
        db.query(KernelSolveNote)
        .filter(KernelSolveNote.challenge_id == challenge.id)
        .one()
    )


def test_learn_writes_confirmed_verdict_on_solve(
    db_session: Session, ctf2_competition
) -> None:
    """解出来了 → `verdict=confirmed`、`solved=True`。"""
    from app.core.vocab import AgentsecVerdict
    from tests.kernel.conftest import _make_challenge  # noqa: PLC2701

    ch = _make_challenge(
        db_session, ctf2_competition, remote_id="v-confirmed",
        status=KernelChallengeStatus.SOLVED.value,
        confirmed_flag="flag{ok}",
    )
    row = _learn(db_session, ch, _FakeOutcome(transcript="run_shell: ls -la"))

    assert row.verdict == AgentsecVerdict.CONFIRMED.value
    assert row.solved is True


def test_learn_writes_needs_follow_up_on_bare_failure(
    db_session: Session, ctf2_competition
) -> None:
    """★ 没解出来、且没有完整证据链 → `needs_follow_up`、`solved=False`。

    这一条守的是"**别**把随手的失败升级成已排除"：
    自动补的四项前缀不能冒充完整证据链（见
    `notes.RULED_OUT_EVIDENCE_MARKERS` 的说明）。
    """
    from app.core.vocab import AgentsecVerdict
    from tests.kernel.conftest import _make_challenge  # noqa: PLC2701

    ch = _make_challenge(db_session, ctf2_competition, remote_id="v-followup")
    row = _learn(db_session, ch, _FakeOutcome(error="timeout"))

    assert row.verdict == AgentsecVerdict.NEEDS_FOLLOW_UP.value
    assert row.solved is False, "★ 草率的失败被当成了已解决"


def test_learn_accepts_agentsec_category_when_switch_on(
    db_session: Session, ctf2_competition
) -> None:
    """开关打开 + agentsec 分类 → 卡能写成那个分类。"""
    from tests.kernel.conftest import _make_challenge  # noqa: PLC2701

    _turn_agentsec_on(db_session)
    ch = _make_challenge(
        db_session, ctf2_competition, remote_id="v-sec-on",
        category="agentsec-data-leak",
        status=KernelChallengeStatus.SOLVED.value,
        confirmed_flag="flag{ok}",
    )
    row = _learn(db_session, ch, _FakeOutcome(transcript="run_shell: ls"))

    assert row.category == "agentsec-data-leak"


def test_learn_degrades_to_blank_category_when_switch_off(
    db_session: Session, ctf2_competition
) -> None:
    """★ 开关关着 + agentsec 分类 → **写卡但不带分类**，不整条丢。

    ## 这条用例造的是一个"正常流程走不到"的状态

    正常同步时，开关关着就不会有 `agentsec-*` 的题被写进来
    （`validate_note_category` 在写入侧会拦）。但这里刻意造出那个
    状态，因为要守的是**兜底行为**：

    历史数据、手工改库、或者开关被关掉而题目早已入库 —— 这几种情况下
    库里确实会有 `agentsec-*` 的题。此时如果 `learn_from_solve` 因为
    分类不合法就整体失败，那道题的解题知识就永久丢了，而知识本身
    与开关无关。

    所以兜底是：**降级成无分类写卡**，不是丢卡。

    ⚠️ 这个取舍与"写入侧拒绝非法分类"不矛盾：那一条守的是
    新写入不产生脏数据，这一条守的是已有脏数据不会连带毁掉知识。
    两处判据不同是因为**风险方向不同**。
    """
    from tests.kernel.conftest import _make_challenge  # noqa: PLC2701

    _turn_agentsec_off(db_session)
    ch = _make_challenge(
        db_session, ctf2_competition, remote_id="v-sec-off",
        category="agentsec-prompt-injection",
        status=KernelChallengeStatus.SOLVED.value,
        confirmed_flag="flag{ok}",
    )
    row = _learn(db_session, ch, _FakeOutcome(transcript="run_shell: ls"))

    assert row.category == "", "开关关着时不该把 agentsec 分类写进卡"
    assert row.approach, "★ 分类不合法不该连知识一起丢掉"


def _set_agentsec_switch(db: Session, *, on: bool) -> None:
    """把配置行的开关设成指定值（走正常保存路径，不直接改列）。"""
    from app.kernel.service import get_ctf2_config, update_ctf2_settings

    row = get_ctf2_config(db)
    update_ctf2_settings(
        db,
        submit_mode=row.submit_mode,
        max_wrong_submissions=row.max_wrong_submissions,
        default_sandbox_profile=row.default_sandbox_profile,
        max_concurrent_solves=row.max_concurrent_solves,
        solve_poll_seconds=row.solve_poll_seconds,
        auto_sync_seconds=row.auto_sync_seconds,
        auto_priority_new=row.auto_priority_new,
        auto_start_new=row.auto_start_new,
        max_turns=row.max_turns,
        fallback_provider_id=row.fallback_provider_id,
        enable_agentsec_scenarios=on,
        # IR 那个开关原样透传 —— 它是必填关键字参数（v0.44），且
        # 这条 helper 只关心 agentsec 一个，不该顺手改另一个。
        enable_ir_scenarios=row.enable_ir_scenarios,
    )


def _turn_agentsec_on(db: Session) -> None:
    _set_agentsec_switch(db, on=True)


def _turn_agentsec_off(db: Session) -> None:
    """确保开关是关的（默认就是关，但别的用例可能改过）。"""
    _set_agentsec_switch(db, on=False)


def test_learn_marks_ruled_out_as_solved(
    db_session: Session, ctf2_competition
) -> None:
    """★★ 本次修复的**核心路径**：跑完全套判据后确认排除，算已解决。

    ## 为什么要端到端测而不是只测 `resolve_verdict`

    `resolve_verdict` 是个纯函数，测它只证明"判据写得对"。而缺陷 1
    的实际表现是：**卡片落库时 `solved` 被写成 False**，于是召回时
    排在后面、甚至被整轮排除后重跑。那条路径要经过
    `learn_from_solve` → `build_note_fields` → `resolve_verdict`
    → 落库，任何一环断了症状都一样（卡还是未解决）。

    所以这里造一次"真的跑完了三种话术、模型写了排除理由"的解题，
    然后读**数据库里的那一行**断言。
    """
    from app.core.vocab import AgentsecVerdict
    from tests.kernel.conftest import _make_challenge  # noqa: PLC2701

    _turn_agentsec_on(db_session)
    ch = _make_challenge(
        db_session, ctf2_competition, remote_id="v-ruled-out",
        category="agentsec-prompt-injection",
    )
    transcript = "\n".join(
        [
            '[turn 1] tool run_shell {"command": "file /work/a.bin"}',
            '[turn 2] tool run_shell {"command": "strings /work/a.bin | head -50"}',
        ]
    )
    outcome = _FakeOutcome(
        transcript=transcript,
        error="",
        ruled_out=[
            "排除了 PI-03 越狱 —— 三种话术全部被拒绝，无越界输出",
        ],
    )
    row = _learn(db_session, ch, outcome)

    assert row.verdict == AgentsecVerdict.RULED_OUT.value, (
        f"证据链应当齐全，实际 verdict={row.verdict}\n{row.dead_ends}"
    )
    assert row.solved is True, (
        "★ 跑完全套测试后确认排除，仍被记成未解决（缺陷 1 未修复）"
    )
