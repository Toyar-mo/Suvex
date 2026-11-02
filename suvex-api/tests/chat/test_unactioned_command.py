"""正文里塞了 shell 命令、却没写 `ACTION:` —— 不许当结论收尾（v1.50）。

## 这条用例来自哪次线上会话

会话 `chat_01M6BWQ69MZJNBAB7C7QMVXPV2` 的 seq 660（2026-11-02 15:53，
用户截图「还是会停」）。模型的原话是：

    抱歉，上一条漏了 ACTION。我现在做一件从未检查过的事：**对全部图像做
    LSB 隐写提取**，这是 CTF 里最常见的置入 flag 方式。

    cd /work/persist/chisaki && python3 -c "
    ...

也就是说：它说了"我现在做"，然后**把 shell 命令原样贴进代码块**，没有
写 `ACTION:` 行。生产日志：

    chat.round round=0 raw_len=667 text_len=666 actions=0 said_final=False
               truncated=False tools=0 nudges=0 tool_nudges=0 ... finish='stop'
    chat.giveup no_action said_final=False plan_like=False plan_bounces=0
               nudges=0 ... text_len=666

三条判据全漏：

* `looks_like_plan(text)` → False（没有落在收尾三句窗口的「先/现在/接下来」）
* `looks_unfinished(text)` → False（`len >= MAX_UNFINISHED_CHARS`=200，被当成"长结论"）
* `tools_used == 0` → 那条"跑过工具就推"的分支够不着

于是 `should_nudge=False`、`nudges=0` → 直接 `giveup no_action`，整轮 `done`。
界面停在「继续」上，用户必须手点，点完又是新一轮空转 —— 就是"还是会停"。

## 修法与它的边界

新增 `nudge.has_unactioned_command(text)`：**围栏代码块里出现一行像
shell 命令的行、且正文没有 `ACTION:`/`FINAL:`** 就算"想动手但没走协议"。
这比任何长度/意图词都硬 —— 它明确表达了"我要执行这段"。

刻意**不**与 `looks_unfinished` 合并：那个函数守的是"问答型短半句"，
长度上界 200 是对的；这里守的是另一个形态（贴了命令不走协议），长度无关。
两病两药。

误判的代价 = 多一次模型调用（让它改用 ACTION 或说"做完了"）；
漏判的代价 = 用户又得手打「继续」、整轮白跑。后者更贵。
"""

from app.chat.nudge import has_unactioned_command, should_nudge

#: 线上真实那条（seq 660），逐字取自数据库。
LIVE_SEQ660 = """抱歉，上一条漏了 ACTION。我现在做一件从未检查过的事：**对全部图像做 LSB 隐写提取**，这是 CTF 里最常见的置入 flag 方式。

cd /work/persist/chisaki && python3 -c "
import numpy as np
from PIL import Image
for name in ['assembled_0-15.png','atlas.png']:
    try: im=Image.open(name).convert('RGB')
    except Exception as e: print(name,'ERR',e); continue
    a=np.asarray(im)
    print('===',name,im.size)
"
"""


def test_live_seq660_has_an_unactioned_command():
    """线上真实文本必须被判成"写了命令没走协议"。"""
    assert has_unactioned_command(LIVE_SEQ660) is True


def test_fenced_shell_block_is_a_command():
    text = "先把包解出来：\n\n```bash\ncd /work && tar -xzf a.tgz\n```\n"
    assert has_unactioned_command(text) is True


def test_the_page_stop_case_is_nudged_even_though_it_is_long():
    """666 字、零工具、非计划 —— 旧逻辑三判据全漏；新逻辑必须推一次。"""
    assert len(LIVE_SEQ660) >= 200  # 长度闸会挡掉 looks_unfinished
    nudged = should_nudge(
        at_limit=False,
        said_final=False,
        actions=[],
        truncated=False,
        tools_used=0,
        text=LIVE_SEQ660,
        nudges_used=0,
        max_nudges=2,
        truncations_used=0,
        max_truncations=12,
        tool_nudges_used=0,
        max_tool_nudges=8,
    )
    assert nudged is True


def test_a_real_conclusion_that_mentions_shell_in_prose_is_not_flagged():
    """正文里"提到"某个命令名，但不是一段可复制的命令块 —— 不该被判成要执行。"""
    text = (
        "我把整个流程走完了：用 objdump 看了反汇编，用 strings 找了常量，"
        "最后确认这道题没有 flag，是个误导项。结论如上。"
    )
    assert has_unactioned_command(text) is False


def test_plain_fenced_code_without_shell_verbs_is_not_flagged():
    text = "这里是配置示例：\n\n```\nname = demo\nvalue = 42\n```\n"
    assert has_unactioned_command(text) is False


def test_text_with_action_marker_is_not_a_command_case():
    """已经走了文本协议的不归这一类（另有分支处理）。"""
    text = '```\nls -la /work\n```\nACTION: {"tool": "run_shell", "args": {"command": "ls"}}'
    assert has_unactioned_command(text) is False


def test_empty_and_blank_texts_are_safe():
    assert has_unactioned_command("") is False
    assert has_unactioned_command("   \n\n  ") is False
