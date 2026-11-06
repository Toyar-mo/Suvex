"""CTF 专项技能包（ljagiello/ctf-skills）导入 + CTF 人格默认挂载（v1.71）。

## 这个文件在防什么

用户要求「安装专项技能包 ljagiello/ctf-skills 到项目，配置好在 CTF 方向默认调用」。
三件事任何一件坏了都会**静默失效**：

1. **技能包没被加载器发现** —— `discover_skills()` 找不到 → 清单里没有 →
   模型根本不知道有这套手册。没有一个地方会报错，模型只是「变笨了」。
2. **目录包附件被误当成独立技能** —— `ctf-pwn/` 下有 70 个附件 .md，若被
   当成 70 篇独立技能塞进清单，清单会被撑爆（`MAX_INDEX_ENTRIES=200`），
   真正的入口被淹没。
3. **CTF 人格没默认注入 solve-challenge** —— 用户选了「CTF 解题」却还是要
   自己手动去勾技能，那「默认调用」就没做到。

这三条都不该靠人肉记，钉成测试。
"""

from __future__ import annotations

from app.chat import prompts as P
from app.kernel import skills as S


def test_ctf_skills_are_discovered_and_grouped_under_ctf() -> None:
    """12 篇 ctf-skills（含 solve-challenge）都被发现，且 category 归到 ctf。"""
    all_skills = S.discover_skills()
    ctf = [s for s in all_skills if s.category == "ctf"]
    names = {s.name for s in ctf}
    expected = {
        "ctf-ai-ml",
        "ctf-crypto",
        "ctf-forensics",
        "ctf-malware",
        "ctf-misc",
        "ctf-osint",
        "ctf-pwn",
        "ctf-reverse",
        "ctf-similar-search",
        "ctf-web",
        "ctf-writeup",
        "solve-challenge",
    }
    assert expected <= names, f"缺少 CTF 技能：{expected - names}"


def test_ctf_package_attachments_are_not_counted_as_skills() -> None:
    """目录包内部附件不能被当成独立技能（否则清单爆掉）。"""
    all_skills = S.discover_skills()
    # ctf-pwn 有 70 个附件，但它们都该挂在 ctf-pwn 这篇下面，不是 70 篇技能
    pwn = next(s for s in all_skills if s.name == "ctf-pwn")
    assert len(pwn.files) >= 50  # 附件确实被收进来了
    # 附件不能各自成为一个 Skill：不存在名为 "rsa-attacks" 这类附件的技能
    names = {s.name for s in all_skills}
    assert "rsa-attacks" not in names
    assert "classic-ciphers" not in names


def test_solve_challenge_body_is_readable() -> None:
    """solve-challenge 正文能读到，且确实是 CTF 派发/分诊入口。"""
    text = S.load_skill_text("solve-challenge")
    assert not text.startswith("没有名为")
    low = text.lower()
    # 派发入口的关键词：分诊 + 按题型路由
    assert "triage" in low or "三" in text or "分诊" in text
    assert "ctf-web" in low or "ctf-pwn" in low or "route" in low


def test_persona_default_skill_texts_only_for_registered_persona() -> None:
    """只有登记过默认技能的人格（ctf）才有默认注入，其它人格为空。"""
    ctf = P.persona_default_skill_texts("ctf")
    assert [n for n, _ in ctf] == ["solve-challenge"]
    assert P.persona_default_skill_texts("default") == []
    assert P.persona_default_skill_texts(None) == []
    assert P.persona_default_skill_texts("aillm") == []


def test_persona_default_skill_dedup_when_already_mounted() -> None:
    """用户已显式挂载同名技能时，默认注入不重复 —— 正文只出现一次。"""
    mounted = [("solve-challenge", "USER BODY")]
    out = P.persona_default_skill_texts("ctf", mounted)
    assert out == []


def test_ctf_persona_prompt_contains_solve_challenge_body() -> None:
    """构建 CTF 人格提示词时，solve-challenge 正文确实进了 system prompt。"""
    prompt = P.build_system_prompt(persona="ctf", native_tools=True)
    # 正文里独有的字样（frontmatter 会被剥掉，用正文里的标题）
    assert "CTF Challenge Solver" in prompt
    # 派发入口该讲到的核心内容也在
    assert "triage" in prompt.lower() or "分诊" in prompt


def test_default_persona_prompt_does_not_inject_ctf_skill() -> None:
    """默认人格不注入 CTF 技能 —— 不要把无关打法塞给普通对话。"""
    prompt = P.build_system_prompt(persona="default", native_tools=True)
    assert "CTF Challenge Solver" not in prompt


def test_ctf_skill_index_lists_ctf_entries() -> None:
    """技能清单（会进提示词）里能看到 ctf-* 与 solve-challenge。"""
    idx = S.render_index()
    assert "solve-challenge" in idx
    assert "ctf-web" in idx
    assert "ctf-pwn" in idx
