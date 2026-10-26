"""v0.99 新增技能（从 CyberStrikeAI 移植的三篇）的质量与纪律测试。

## 这一组在钉什么

「搬技能库」这个动作有两种坏法，都不会报错：

1. **搬进来的技能讲了镜像里没有的工具** —— 模型照着写命令，沙箱回
   `sh: amap: not found`，而那看起来像「工具跑了但没结果」。
   v0.98 的 `recipes.py` 文件头记过这条教训（CyberStrikeAI 的 90 个配方
   在我们镜像里只有 28 个能跑）。实测也推翻了初判：它的 AD 攻击、
   云攻击、移动逆向三篇，提到的工具在 `suvex-sandbox` 里**全部不存在**
   （certipy / mitm6 / ntlmrelayx / responder / secretsdump / aws /
   gcloud / kubectl / apktool / jadx / frida 逐个 `command -v` 全 MISSING）。
   所以那三篇没有搬。

2. **搬进来的技能与我们的真实机制脱节** —— 它会说「用 upsert_project_fact
   落库」，而我们没有那个工具；或者它教「用代理绕封禁」，而我们的
   crypto/逆向容器是 `--network none`、压根没网。这种技能比没有更糟：
   模型会照它做，然后困惑为什么每条路都不通。

所以下面三条分别钉：存在性、与我们机制的一致性、以及"提到的工具真的在镜像里"。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.kernel import skills as skills_mod

#: v0.99 从 CyberStrikeAI 移植的三篇（按我们的沙箱与机制重写过，不是拷贝）。
PORTED_SKILLS = ("redteam-opsec", "security-compliance", "ai-llm-app-attack")


@pytest.fixture(scope="module")
def by_name():
    return skills_mod.skills_by_name()


@pytest.mark.parametrize("name", PORTED_SKILLS)
def test_ported_skill_is_loadable(by_name, name: str) -> None:
    """三篇都必须被技能库解析到（front matter 合法、进索引、能 load）。"""
    assert name in by_name, f"技能库里没有 {name} —— front matter 或路径不对"

    text = skills_mod.load_skill_text(name, skills=list(by_name.values()))
    assert len(text) > 500, f"{name} 的正文太短（{len(text)} 字符），像是个占位"
    # 正文里不能残留未解析的 front matter 分隔符之后的内容错位
    assert text.lstrip().startswith("#"), f"{name} 的正文没有以标题起头"


def test_ported_skills_appear_in_the_prompt_index() -> None:
    """★ 进了技能库还不够 —— 还必须出现在**模型收到的清单**里。

    这是 v0.98 那个洞的同形状检查：`render_index()` 的输出要真的被
    `load_system_prompt()` 用上，否则技能存在但模型不知道它存在。
    """
    from app.kernel.runner import load_system_prompt

    prompt = load_system_prompt()
    for name in PORTED_SKILLS:
        assert name in prompt, (
            f"系统提示词的技能清单里没有 {name} —— "
            "技能写了但模型看不到，等于不存在"
        )


def test_ported_skills_do_not_promise_tools_we_do_not_have(by_name) -> None:
    """★ 不能出现"我们沙箱里没有的工具"。

    ## 判据怎么来的（实测，不是猜）

    2026-10-26 在 `suvex-sandbox:latest` 里按生产启动参数
    （`--network none --read-only --cap-drop ALL`）逐个 `command -v`：

        FOUND   nmap ffuf gobuster dirb nikto whatweb wafw00f sqlmap
                curl wget dig nslookup radare2 r2 objdump gdb checksec
                binwalk foremost exiftool steghide pngcheck strings tcpdump
                python3 pip3 hashcat john hydra socat
        MISSING apktool jadx frida objection ghidra node npm rg mitmproxy
                whois subfinder amass theHarvester mono dotnet
                certipy mitm6 ntlmrelayx responder secretsdump kerbrute
                aws az gcloud kubectl minio pacu trivy
                msfconsole medusa crackmapexec nxc netexec chisel proxychains4

    下面只钉那些**会被当成命令写出来**的名字。提到它们在"我们缺什么"
    的语境里（如 redteam-opsec 讲"无网容器里没有代理这条路"）是允许的吗？
    —— 不允许。那种说明仍然会让模型去写 `--socks5`。
    真要讲"什么不存在"，说能力不说命令名。
    """
    forbidden = [
        # 逆向 / 移动（第二批判定为 MISSING）
        "apktool",
        "jadx",
        "frida",
        "objection",
        "ghidra",
        "mitmproxy",
        "rg ",
        "subfinder",
        "amass",
        "theHarvester",
        "whois",
        # 内网 / 云（第一批判定为 MISSING）
        "certipy",
        "mitm6",
        "ntlmrelayx",
        "responder",
        "secretsdump",
        "kerbrute",
        "bloodhound",
        "crackmapexec",
        "netexec",
        "nxc ",
        "chisel",
        "proxychains",
        "metasploit",
        "msfconsole",
        "aws ",
        "gcloud",
        "kubectl",
        "trivy",
        "pacu",
        "scoutsuite",
        # 语言运行时（MISSING）
        "node ",
        "npm ",
        "mono ",
        "dotnet",
    ]
    for name in PORTED_SKILLS:
        skill = by_name[name]
        body = skill.read().lower()
        hits = sorted({token.strip() for token in forbidden if token in body})
        assert not hits, (
            f"{name} 提到了镜像里不存在的工具：{hits}。"
            "模型会照着写命令，然后拿到 `not found` —— 那看起来像"
            "「工具跑了但没结果」，是 v0.98 已经踩过的那类静默失真。"
        )


def test_opsec_skill_states_our_network_none_limitation(by_name) -> None:
    """★ `redteam-opsec` 必须写清我们的容器可能**完全没网**。

    ## 为什么这条单独钉

    CyberStrikeAI 那篇 opsec 的核心建议之一是「被拦就换代理」——
    而我们的 `crypto` / `取证` / `逆向` 容器是 `--network none`，
    代理这条路**物理上不存在**。照搬会让模型在一个没有网的容器里
    反复试代理，每次失败都被理解成"代理不可用"而不是"这里没有网"。

    这是"搬技能"最典型的失真：原文在它的环境里是对的。
    """
    body = by_name["redteam-opsec"].read()
    assert "--network none" in body, "没提无网容器这条硬限制"
    assert "Network is unreachable" in body or "is unreachable" in body, (
        "没写清无网时的报错长什么样 —— 那一句是模型把"
        "「环境没网」误读成「目标不可达」的分界线"
    )
    assert "curl" in body, "没给「如何一条命令判断有没有网」的可执行判据"


def test_compliance_skill_name_matches_its_content(by_name) -> None:
    """`security-compliance` 讲的是**动作前核查**，不是"合规报告生成"。

    名字歧义会让模型在错误时机加载它。这条钉住 description 里那层语义。
    """
    skill = by_name["security-compliance"]
    assert "核查" in skill.description or "授权" in skill.description, (
        f"description 没表达「动作前核查」这层语义：{skill.description[:80]}"
    )


def test_ported_skills_are_written_in_chinese() -> None:
    """正文必须是中文（本项目全站中文，英文正文会让风格断裂）。"""
    root = Path(skills_mod.SKILLS_ROOT)
    for name in PORTED_SKILLS:
        path = next(root.rglob(f"{name}.md"))
        text = path.read_text(encoding="utf-8")
        cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
        assert cjk > 200, f"{name} 里中文字符只有 {cjk} 个，像是没重写的原文"
