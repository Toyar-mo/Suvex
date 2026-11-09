"""新装工具的工具级技能（v1.79）。

## 为什么需要这一组

"工具装进镜像"只解决了一半：模型得知道**在这个镜像里**怎么用、有什么坑。
2026-11-09 把 wpscan / searchsploit / impacket / uncompyle6 / pycdc /
ghidra / vol 等装进镜像后，如果只留配方（参数表单），模型仍会按通用
教程去猜路径、猜参数，撞上与镜像不符的行为。

这组测试钉三件事：

1. 每篇技能都被发现、能 `load_skill`、出现在**模型看到的清单**里；
2. 正文里写的关键入口/路径是**这个镜像真的有的**（例如
   `ghidra-decompile`、`/usr/share/dirb/wordlists/common.txt`）；
3. 不再出现已被实测推翻的说法（`upx -d`、`rockyou.txt`、`sox`、把
   masscan 当默认扫描器）。
"""

from __future__ import annotations

import pytest

from app.kernel import skills as skills_mod

#: 技能名 → 正文里必须出现的关键事实。
#:
#: 只钉"镜像特有、且实测过"的那几条，不把整篇正文当快照。
TOOL_SKILLS: dict[str, tuple[str, ...]] = {
    "ghidra-headless": ("ghidra-decompile", "DecompileAll.java", "没有 GUI"),
    "pyc-decompile": ("pycdc", "uncompyle6", "Bad MAGIC"),
    "android-reverse": ("jadx", "apktool", "smali"),
    "pcap-analysis": ("tshark", "-Y", "--export-objects"),
    "volatility3": ("vol -f", "windows.pslist", "符号"),
    "stego-toolkit": ("stegoveritas", "-bruteLSB", "没有 zsteg"),
    "wpscan": ("--api-token", "WordPress"),
    "searchsploit": ("/usr/share/exploitdb", "搜到 ≠ 能打"),
    "impacket": ("secretsdump.py", "-just-dc", "敏感"),
    "crypto-toolkit": ("z3", "RsaCtfTool", "factordb"),
    "ropgadget": ("--binary", "one_gadget"),
    "port-scanning": ("masscan", "permission denied", "nmap -sT"),
}


@pytest.fixture(scope="module")
def by_name():
    return skills_mod.skills_by_name()


@pytest.mark.parametrize("name", sorted(TOOL_SKILLS))
def test_tool_skill_is_discovered_and_loadable(by_name, name: str) -> None:
    assert name in by_name, f"技能库里没有 {name} —— frontmatter/路径不对"
    text = skills_mod.load_skill_text(name, skills=list(by_name.values()))
    assert text.lstrip().startswith("#"), f"{name} 正文没有以标题起头"
    assert len(text) > 600, f"{name} 太短，像占位（{len(text)} 字符）"


@pytest.mark.parametrize("name", sorted(TOOL_SKILLS))
def test_tool_skill_states_image_specific_facts(by_name, name: str) -> None:
    text = skills_mod.load_skill_text(name, skills=list(by_name.values()))
    must = TOOL_SKILLS[name]
    missing = [needle for needle in must if needle not in text]
    assert not missing, f"{name} 缺少镜像特有事实：{missing}"


def test_tool_skills_appear_in_the_model_index() -> None:
    """进了技能库还不够 —— 必须出现在模型收到的清单里。"""
    from app.kernel.runner import load_system_prompt

    prompt = load_system_prompt()
    missing = [name for name in TOOL_SKILLS if name not in prompt]
    assert not missing, f"系统提示词技能清单里没有：{missing}"


@pytest.mark.parametrize("name", sorted(TOOL_SKILLS))
def test_tool_skill_does_not_teach_missing_tools(by_name, name: str) -> None:
    """已被实测推翻的说法不许**作为建议**出现。

    允许"镜像没有 upx / rockyou / sox"这种**否定说明**（那正是要教给模型
    的事实），但不许把它们写成可用命令。

    · `upx` 在镜像里**没有**（不要写 `upx -d` 当默认流程）
    · `rockyou.txt` / `/usr/share/wordlists` 是空的
    · `sox` 没有装
    """
    text = skills_mod.load_skill_text(name, skills=list(by_name.values()))
    for line in text.splitlines():
        if "upx -d" in line:
            assert "没有" in line or "不能" in line, f"{name} 把 upx 当可用工具：{line}"
        if "rockyou" in line:
            assert "没有" in line or "别" in line, f"{name} 把 rockyou 当已有字典：{line}"
        if "sox" in line:
            assert "没有" in line or "别" in line, f"{name} 把 sox 当可用工具：{line}"
