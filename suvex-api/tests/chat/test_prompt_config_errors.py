"""配置类错误的处置口径（2026-11-08 实测后补）。

## 事故

账号解绑后，对话模型调 `start_ctf_solve` 拿到「尚未绑定 CTF2 账号」。
它没有停，而是：

1. 又重试了一次（以为是并发名额问题）；
2. 从沙箱里 `getent hosts ctf2`、逐个 `/dev/tcp` 探端口，想"直接连靶机"
   绕开平台 —— 而 `198.18.x.x` 是代理的 fake-ip，那些 "open" 什么都不代表。

整轮烧掉几十个工具调用，结论是"平台接口用不了"。

所以提示词里要有一条明确的处置方式，工具回执也要说明"重试无用"。
"""

from __future__ import annotations

from app.chat.prompts import build_system_prompt


def test_prompt_tells_the_model_not_to_retry_config_errors() -> None:
    prompt = build_system_prompt()

    assert "配置类错误" in prompt
    assert "重试和换参数都不会让它变好" in prompt


def test_prompt_warns_about_fake_ip_direct_connections() -> None:
    """★ 直连靶机是这条事故里最贵的一步：既拿不到题，也证明不了任何事。"""
    prompt = build_system_prompt()

    assert "fake-ip" in prompt
    assert "不要" in prompt
