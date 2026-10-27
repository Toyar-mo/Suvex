"""`view_image` 工具的守卫测试。

## 这个文件守的是什么

`view_image` 是本项目**唯一**能把视觉信息送进模型的手段。它有几处
一旦写错就变成"假能力"的地方，这些测试逐条钉住：

1. **假装成功** —— 取到字节就说"已挂到这一轮"，但调用方没挂收集器，
   图其实没进去。模型于是基于"我看过了"开始编。那比工具不存在更糟：
   它会把自己没验证过的东西写成结论。
2. **越界读文件** —— `docker cp` 能把整机文件搬出来。"能读 /etc/shadow
   再送给外部 LLM"是一条不该存在的通道。
3. **图常驻历史** —— 一张图 base64 后 1.6 MB。留三轮就吃光上下文，
   而且每轮都要重发一遍。所以图**只挂一轮**。
4. **两条执行路径分叉** —— 单循环（`_solve`）与多智能体
   （`multiagent.run_agent_loop`）是两条独立的循环，各有一个消息列表。
   只接一条的话，症状是"单循环能看图、多智能体看不见"，而多智能体
   在开关打开时是**默认**路径 —— 这种缺口最难发现，因为单循环的
   测试全绿。

## 为什么不用真 docker

宿主没有 docker（本项目一直如此）。`SandboxContainer` 的契约在这条
路径上只有 `copy_out()`，所以用假对象能精确控制"容器里那个文件到底
是什么字节"，这比跑通一个真容器更容易造出边界条件。
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from app.kernel import runner as runner_mod
from app.kernel.runner import (
    IMAGE_MAX_EDGE,
    IMAGE_MEDIA_TYPES,
    IMAGE_SHRINK_TARGET_BYTES,
    MAX_IMAGE_BYTES,
    TOOL_NAMES,
    WORK_DIR,
    ImagePayload,
    _image_block,
    _tool_view_image,
)
from app.kernel.tools import TOOL_BY_NAME, TOOL_SPECS

#: 一个真的 1x1 PNG（67 字节）。用真 PNG 而不是 b"PNG" 之类的假字节：
#: 缩图分支要真的能被 Pillow 解码/拒绝，假字节会让那两条断言失去意义。
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# ══════════════════════════════════════════════════════════
# 假沙箱：只需要 copy_out
# ══════════════════════════════════════════════════════════


class _FakeCopy:
    def __init__(self, ok: bool, stdout: str = "", stderr: str = "") -> None:
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = 0 if ok else 1

    def combined(self, *, limit: int = 800) -> str:
        return (self.stdout + self.stderr)[:limit]


class _FakeSandbox:
    """只实现 `copy_out`。

    `files` 是"容器里有哪些文件"：路径 → 字节。不在里面的路径
    模拟 `docker cp` 失败（那正是模型把路径写错时的现象）。
    """

    def __init__(self, files: dict[str, bytes], *, fail: bool = False) -> None:
        self.files = files
        self.fail = fail
        self.copied: list[tuple[str, str]] = []

    def copy_out(self, container_path: str, host_path):
        self.copied.append((container_path, str(host_path)))
        if self.fail or container_path not in self.files:
            return _FakeCopy(False, stderr=f"Error: No such container:path: {container_path}")
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_bytes(self.files[container_path])
        return _FakeCopy(True)


def _run(sandbox, args: dict, *, images: list | None) -> str:
    return asyncio.run(_tool_view_image(args, sandbox=sandbox, images=images))


# ══════════════════════════════════════════════════════════
# ① 工具确实注册进了内核
# ══════════════════════════════════════════════════════════


def test_view_image_is_a_real_kernel_tool():
    """三处登记缺一不可：`TOOL_NAMES`（dispatch 认它）、`TOOL_SPECS`
    （前端工具页列它）、`TOOL_BY_NAME`（用它查元数据）。

    只登记其中一处是最常见的不完整接线：模型调得到、界面上看不见，
    或者反过来。
    """
    assert "view_image" in TOOL_NAMES
    assert "view_image" in TOOL_BY_NAME
    assert any(s.name == "view_image" for s in TOOL_SPECS)


def test_view_image_spec_does_not_claim_it_is_blind():
    """`default_limitation` 必须是**这个工具真实的限制**，不能写"看不到图"。

    这条是从 `browser` 那条同源守卫改出来的：v0.106 时 `browser` 的
    limitation 写着"看不了图片"，那是当时的事实；接入 `view_image` 之后
    同一句话变成**反向误导**（模型明明能看却被告知看不了）。

    对 `view_image` 自己，真实限制是**时机**（下一轮才可见、只挂一轮）
    和**范围**（只能读 /work 之下）。
    """
    spec = TOOL_BY_NAME["view_image"]
    lim = spec.default_limitation
    assert "下一轮" in lim, f"没说清图什么时候可见（当前：{lim}）"
    assert "一轮" in lim, f"没说清图只挂一轮（当前：{lim}）"
    assert "/work" in lim, f"没说清路径范围（当前：{lim}）"
    assert "看不到图" not in lim
    assert "看不了" not in lim


def test_system_prompt_lists_view_image():
    """系统提示词的工具表里必须有它。

    ★ 这条是补一个**已发生的漏洞**：`browser` 在 v1.0 就做进内核了，
    但 `prompts/kernel_system.md` 的工具表一直是 11 行 —— 模型从来没在
    系统提示词里见过 `browser` 这个名字。工具注册了不等于模型知道它存在。
    """
    from app.kernel.runner import PROMPT_PATH  # noqa: PLC0415

    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "view_image" in text, "系统提示词的工具表里没有 view_image，模型不会想到用它"
    assert "browser" in text, "系统提示词的工具表里没有 browser（v1.0 就漏了，别再漏回去）"


# ══════════════════════════════════════════════════════════
# ② 路径白名单：不能把整机文件送给外部 LLM
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/shadow",
        "/root/.ssh/id_rsa",
        "/tmp/x.png",
        "work/x.png",  # 少了前导斜杠
        "/work/../etc/shadow",
    ],
)
def test_rejects_paths_outside_work(bad: str):
    sandbox = _FakeSandbox({bad: PNG_1PX})
    images: list[ImagePayload] = []
    out = _run(sandbox, {"path": bad}, images=images)
    assert "拒绝" in out, f"`{bad}` 应当被拒（当前：{out}）"
    assert images == [], "被拒的路径不该产生任何 ImagePayload"
    assert sandbox.copied == [], "被拒的路径不该真的去 docker cp（那已经晚了）"


def test_rejects_traversal_explicitly():
    sandbox = _FakeSandbox({})
    out = _run(sandbox, {"path": "/work/../../etc/passwd"}, images=[])
    assert ".." in out
    assert "拒绝" in out


def test_rejects_unknown_suffix():
    """`/work/a.txt` 不是图片 —— 明确说支持哪些，而不是让端点回 400。"""
    sandbox = _FakeSandbox({"/work/a.txt": b"hello"})
    images: list[ImagePayload] = []
    out = _run(sandbox, {"path": "/work/a.txt"}, images=images)
    assert "拒绝" in out
    assert images == []
    # 说清支持哪些格式，模型下次才知道换什么后缀
    for suffix in IMAGE_MEDIA_TYPES:
        assert suffix in out, f"没告诉模型支持 `{suffix}`（当前：{out}）"


def test_missing_path_returns_usage_not_crash():
    out = _run(_FakeSandbox({}), {}, images=[])
    assert "path" in out
    assert "参数错误" in out


# ══════════════════════════════════════════════════════════
# ③ ★ 核心：图真的进了收集器；没有收集器时**不许假装成功**
# ══════════════════════════════════════════════════════════


def test_valid_image_lands_in_the_collector():
    sandbox = _FakeSandbox({f"{WORK_DIR}/shot.png": PNG_1PX})
    images: list[ImagePayload] = []
    out = _run(sandbox, {"path": f"{WORK_DIR}/shot.png"}, images=images)

    assert len(images) == 1, f"图没进收集器（当前返回：{out}）"
    payload = images[0]
    assert payload.path == f"{WORK_DIR}/shot.png"
    assert payload.media_type == "image/png"
    assert payload.size == len(PNG_1PX)
    # data 是**裸 base64**，不含 `data:` 前缀 —— 前缀由 `_image_block` 加，
    # 两处都加会得到 `data:image/png;base64,data:image/png;base64,...`
    assert not payload.data.startswith("data:")
    assert base64.b64decode(payload.data) == PNG_1PX
    # 返回值要告诉模型"下一轮才看得到"，否则它会在同一条消息里编描述
    assert "下一轮" in out


def test_no_collector_means_it_says_so_instead_of_pretending():
    """★ 这条是整个工具最容易变成"假能力"的地方。

    取到了字节、解码成功、完全可以返回一句"已挂到这一轮" —— 但如果
    调用方没挂收集器，那句话就是**假的**：模型下一轮什么也看不到，
    却会以为自己验证过页面。

    宁可在这种情况下退化成"你还是看不到它 + 改用文本手段"。
    """
    sandbox = _FakeSandbox({f"{WORK_DIR}/shot.png": PNG_1PX})
    out = _run(sandbox, {"path": f"{WORK_DIR}/shot.png"}, images=None)

    assert "看不到" in out, f"没有收集器时必须明说看不到（当前：{out}）"
    assert "已把" not in out, "取到字节不等于模型看到了 —— 不能返回「已挂到这一轮」"
    # 并且要给替代手段，否则模型卡死
    assert "snapshot" in out
    assert "get text" in out


# ══════════════════════════════════════════════════════════
# ④ 图片形状：两种 provider 的 content block 都要对
# ══════════════════════════════════════════════════════════


def test_image_block_uses_openai_shape_by_default():
    payload = ImagePayload(path="/work/a.png", media_type="image/png", data="AAAA", size=3)
    block = _image_block(payload, anthropic=False)
    assert block["type"] == "image_url"
    assert block["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_image_block_uses_anthropic_shape_when_asked():
    payload = ImagePayload(path="/work/a.png", media_type="image/png", data="AAAA", size=3)
    block = _image_block(payload, anthropic=True)
    assert block["type"] == "image"
    assert block["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}


# ══════════════════════════════════════════════════════════
# ⑤ 大小上限：拒绝时给可执行的下一步，不是一句"太大了"
# ══════════════════════════════════════════════════════════


def test_oversized_image_is_rejected_with_a_convert_command():
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 1)
    sandbox = _FakeSandbox({f"{WORK_DIR}/big.png": big})
    images: list[ImagePayload] = []
    out = _run(sandbox, {"path": f"{WORK_DIR}/big.png"}, images=images)

    assert "拒绝" in out
    assert images == []
    assert str(MAX_IMAGE_BYTES) in out, "要告诉模型上限是多少，否则它只是再撞一次"
    assert "convert" in out, "要给一条能直接抄的缩图命令"
    assert str(IMAGE_MAX_EDGE) in out


def test_empty_file_is_named_as_empty_not_as_a_decode_error():
    sandbox = _FakeSandbox({f"{WORK_DIR}/empty.png": b""})
    images: list[ImagePayload] = []
    out = _run(sandbox, {"path": f"{WORK_DIR}/empty.png"}, images=images)
    assert "0 字节" in out or "空文件" in out
    assert images == []


def test_copy_out_failure_explains_where_screenshots_live():
    """模型把路径写错时，最有用的一句话是"截图默认在哪儿"。"""
    sandbox = _FakeSandbox({})
    out = _run(sandbox, {"path": f"{WORK_DIR}/nope.png"}, images=[])
    assert "读不到" in out
    assert "/work/.browser/shots" in out


def test_shrink_target_is_below_the_hard_limit():
    """缩图阈值必须**低于**硬上限，否则那个分支永远进不去（死代码）。"""
    assert IMAGE_SHRINK_TARGET_BYTES < MAX_IMAGE_BYTES


# ══════════════════════════════════════════════════════════
# ⑥ ★ 缩图必须真的在**容器里**发生
# ══════════════════════════════════════════════════════════


class _FakeShellResult:
    def __init__(self, ok: bool, stdout: str = "", stderr: str = "") -> None:
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = 0 if ok else 1

    def combined(self, *, limit: int = 800) -> str:
        return (self.stdout + self.stderr)[:limit]


class _ShellSandbox(_FakeSandbox):
    """多实现一个 `shell()`：模拟在容器里跑 `convert`。

    `shrunk` 是"容器缩完之后那个文件有多大"。`shell_ok=False` 模拟
    镜像里没有 imagemagick（`convert: not found`，退出码 127）。
    """

    def __init__(
        self,
        files: dict[str, bytes],
        *,
        shrunk: bytes | None = None,
        shell_ok: bool = True,
    ) -> None:
        super().__init__(files)
        self.shrunk = shrunk
        self.shell_ok = shell_ok
        self.commands: list[str] = []
        self.cleanups: list[str] = []

    def shell(self, command: str, *, timeout: float = 60.0):
        self.commands.append(command)
        if command.startswith("rm -rf "):
            self.cleanups.append(command)
            return _FakeShellResult(True)
        if not self.shell_ok:
            return _FakeShellResult(False, stderr="convert: not found")
        # `convert ... <out>` —— 把缩好的字节塞进"容器文件表"，
        # 这样紧接着的 copy_out 能取到它。
        out_path = command.split()[-1]
        if self.shrunk is not None:
            self.files[out_path] = self.shrunk
        return _FakeShellResult(True)


def test_shrink_happens_inside_the_sandbox_not_on_the_host():
    """★ 缩图必须走容器里的 `convert`。

    这条守的是一个**已发生的线上缺陷**：原实现只在宿主调 Pillow，而
    API 进程的 venv 里没有 Pillow（`requirements.txt` 没声明；Kali 实测
    `import PIL` → `ModuleNotFoundError`）。那条路径在线上永远走
    `except ImportError`，于是"缩图"这个承诺从未生效过 —— 一张 1.5 MB
    的整页截图会被原样送进对话，几轮之后模型开始遗忘。

    所以断言三件事：① 调了 `convert` ② note 说清是**沙箱内**缩的
    ③ 送出去的字节是缩过的那份，不是原始那份。
    """
    original = b"\x89PNG\r\n\x1a\n" + b"\x00" * (IMAGE_SHRINK_TARGET_BYTES + 1)
    shrunk = b"\x89PNG\r\n\x1a\nsmall"
    sandbox = _ShellSandbox({f"{WORK_DIR}/big.png": original}, shrunk=shrunk)
    images: list[ImagePayload] = []

    out = _run(sandbox, {"path": f"{WORK_DIR}/big.png"}, images=images)

    assert sandbox.commands, "一次 convert 都没跑 —— 缩图没有发生"
    assert "convert" in sandbox.commands[0], f"没调 convert（跑了：{sandbox.commands[0]}）"
    assert f"{WORK_DIR}/" in sandbox.commands[0], "缩图的临时文件要落在可写的 /work 里"
    assert len(images) == 1
    assert base64.b64decode(images[0].data) == shrunk, "送进去的还是原图，缩了个寂寞"
    assert "沙箱内" in out, f"没告诉模型缩图是在容器里做的（当前：{out}）"
    # 临时目录要清掉：/work 是 tmpfs，留着就是白占容器内存
    assert any("rm -rf" in c for c in sandbox.cleanups), "用完没清沙箱里的临时文件"


def test_shrink_falls_back_to_raw_when_no_tool_is_available_anywhere():
    """容器没 `convert`、宿主也没 Pillow 时，**说真话**并按原样送。

    这条挡的是另一种沉默失败：如果不明说，模型会以为图被缩过了，
    而实际上它的上下文里塞着一份 1.5 MB 的 base64。
    """
    original = b"\x89PNG\r\n\x1a\n" + b"\x00" * (IMAGE_SHRINK_TARGET_BYTES + 1)
    sandbox = _ShellSandbox({f"{WORK_DIR}/big.png": original}, shell_ok=False)
    images: list[ImagePayload] = []

    out = _run(sandbox, {"path": f"{WORK_DIR}/big.png"}, images=images)

    # 宿主这边确实没有 Pillow，这条分支在开发机和线上行为一致。
    assert "缩图工具" in out or "按原样送出" in out, f"没说明为什么没缩（当前：{out}）"
    assert len(images) == 1, "缩不了不等于不给 —— 原图仍要送进去"
    assert base64.b64decode(images[0].data) == original


def test_image_still_too_big_after_shrink_is_rejected_not_slipped_in():
    """缩完仍然超限 → 明确拒绝。

    纯噪声 PNG 缩到 1568px 仍可能上 MB，base64 后再 ×4/3。悄悄塞进
    对话的后果不是报错，是模型在几十轮之后开始遗忘，而那看起来
    像"模型变笨了"，没人会去怀疑一张图。
    """
    original = b"\x89PNG\r\n\x1a\n" + b"\x00" * (IMAGE_SHRINK_TARGET_BYTES + 1)
    still_big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 1)
    sandbox = _ShellSandbox({f"{WORK_DIR}/noise.png": original}, shrunk=still_big)
    images: list[ImagePayload] = []

    out = _run(sandbox, {"path": f"{WORK_DIR}/noise.png"}, images=images)

    assert images == [], "缩完还超限的图不该进收集器"
    assert "拒绝" in out
    assert "crop" in out, "要给一条可执行的下一步，而不是一句「太大了」"
