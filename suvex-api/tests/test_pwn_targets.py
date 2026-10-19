"""四道 pwn 靶机题的**账本一致性**测试。

这些东西本身没法在单测里跑（要 gcc、要 gdb），能锁住的是"清单写没写全"
这一类错误 —— 而正是这类错误会静悄悄地生效：

* `CATEGORIES` 漏加 id → 题目定义躺在 `EXERCISES` 里看起来写好了，
  但 `/ctf/exercise-list` 不返回它，**等于没上架**，A/B 跑起来只是少两道题；
* `SOURCES` 漏加源码 → `/files/<name>` 端点 `build()` 时抛 `BuildError`；
* `EXTRA_FLAGS` 漏加 `-Wl,--dynamic-linker` → `pwn_patch` 编出来能正常跑，
  **整道题失去意义**，而它照样能编译、照样能下载、照样"看起来是道题"；
* `ENC[]` 与被掩码的 flag 不一致 → 解出来的是一串乱码，
  而这条只有真跑到 `win()` 才会暴露。

掩码表直接从 `.c` 源码里读出来校验，因此不需要 gcc。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

# `scripts/mock_arena_server.py` 是**按脚本方式**跑的（`import pwn_targets`，
# 依赖 cwd 就是 `scripts/`）。测试里要 import 它，得把那个目录放进 sys.path
# —— 否则 `ModuleNotFoundError: No module named 'pwn_targets'`。
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from scripts import mock_arena_server as mock  # noqa: E402
from scripts import pwn_targets  # noqa: E402

SRC_DIR = Path(pwn_targets.__file__).resolve().parent

#: 每道题的明文 flag 与掩码常量名。
#: 新增题目时**必须**在这里登记，否则下面的账本测试覆盖不到它。
EXPECTED_FLAGS = {
    "pwn_patch": ("ENC", "flag{patchelf_before_you_run}"),
    "pwn_debug": ("ENC", "flag{gdb_shows_the_runtime}"),
}

MASK = 0x5A


def _read_enc(source: str, name: str) -> bytes:
    """从 `.c` 源码里把 `static const unsigned char <name>[] = {...}` 读出来。"""
    text = (SRC_DIR / source).read_text(encoding="utf-8")
    match = re.search(
        rf"static const unsigned char {name}\[\]\s*=\s*\{{(.*?)\}};",
        text,
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"{source} 里找不到 {name}[]")
    values = [int(tok, 16) for tok in re.findall(r"0x([0-9a-fA-F]{2})", match.group(1))]
    return bytes(values)


def test_每道题都有源码() -> None:
    assert set(pwn_targets.SOURCES) == {"pwn_baby", "pwn_rop", "pwn_patch", "pwn_debug"}
    for name, source in pwn_targets.SOURCES.items():
        assert (SRC_DIR / source).is_file(), f"{name} 的源码 {source} 不在"


def test_只有_patch_那道题改了解释器() -> None:
    """`EXTRA_FLAGS` 是这道题的**全部内容**，漏了它题目就退化成普通栈溢出。"""
    assert set(pwn_targets.EXTRA_FLAGS) == {"pwn_patch"}
    flags = pwn_targets.EXTRA_FLAGS["pwn_patch"]
    assert f"-Wl,--dynamic-linker={pwn_targets.BROKEN_INTERP}" in flags
    # 坏路径必须真的不存在，否则内核能加载，题目不成立。
    assert not Path(pwn_targets.BROKEN_INTERP).exists()


def test_掩码表能解回预期的_flag() -> None:
    for name, (enc_name, want) in EXPECTED_FLAGS.items():
        enc = _read_enc(pwn_targets.SOURCES[name], enc_name)
        got = bytes(b ^ MASK for b in enc).decode("utf-8")
        assert got == want, f"{name} 的 {enc_name}[] 解出来是 {got!r}，不是 {want!r}"


def test_附带两道题都上架了() -> None:
    """写好了但没进 `CATEGORIES` = 没上架，而这一点不会报任何错。"""
    listed = {ex_id for cat in mock.CATEGORIES for ex_id in cat["corpus"]}
    for ex_id in (1011, 1012):
        assert ex_id in listed, f"#{ex_id} 不在任何分类的 corpus 里 —— 等于没上架"
        assert ex_id in mock.EXERCISES, f"#{ex_id} 没有题目定义"


def test_每个_binary_附件都有源码并且上架了() -> None:
    """`/files/<name>` 只对 `BINARY_ATTACHMENTS` 走编译分支。"""
    for name in mock.BINARY_ATTACHMENTS:
        assert name in pwn_targets.SOURCES, f"{name} 没有源码，下载附件会抛 BuildError"

    listed = {ex_id for cat in mock.CATEGORIES for ex_id in cat["corpus"]}
    attached = {
        ex_id
        for ex_id, item in mock.EXERCISES.items()
        for file in (item.get("attachment") or {}).get("files") or []
        if Path(file["url"]).name in mock.BINARY_ATTACHMENTS
    }
    assert attached <= listed, f"这些带二进制附件的题没上架：{sorted(attached - listed)}"


def test_两道纯附件题不占用远程端口() -> None:
    """纯附件题若被塞进 `PWN_TARGETS`，会多起两个没人连的进程。"""
    assert set(mock.PWN_TARGETS) == {1003, 1006}
    assert not (set(pwn_targets.SOURCES) & {"pwn_patch", "pwn_debug"}) & set(
        name for name, _ in mock.PWN_TARGETS.values()
    )


def test_没装_gcc_时报错而不是静默跳过(monkeypatch: pytest.MonkeyPatch) -> None:
    """编译不出来必须吵 —— 静默跳过会让"靶机没起来"变成"模型没解出"。"""
    monkeypatch.setattr(pwn_targets.shutil, "which", lambda _name: None)
    monkeypatch.setattr(pwn_targets, "BUILD_DIR", Path("/nonexistent-build-dir"))
    with pytest.raises(pwn_targets.BuildError, match="gcc"):
        pwn_targets.build("pwn_baby", force=True)


def test_未知题目名报错时列出全部可用名字() -> None:
    with pytest.raises(pwn_targets.BuildError) as excinfo:
        pwn_targets.build("pwn_not_a_thing")
    for name in pwn_targets.SOURCES:
        assert name in str(excinfo.value)


def test_题目定义里的_flag_与源码一致() -> None:
    """`EXERCISES` 里登记的 flag 是**判分依据**，和二进制里藏的对不上就永远判错。"""
    for name, (_, want) in EXPECTED_FLAGS.items():
        owners = [
            item
            for item in mock.EXERCISES.values()
            for file in (item.get("attachment") or {}).get("files") or []
            if Path(file["url"]).name == name
        ]
        assert owners, f"没有题目挂 {name} 这个附件"
        for item in owners:
            assert item["flag"] == want, f"#{item['id']} 的 flag 是 {item['flag']!r}，二进制里是 {want!r}"


def test_源码里没有残留的待办标记() -> None:
    """`※` / `TODO` / `FIXME` 留在题目源码里，说明这题还没写完。"""
    for source in pwn_targets.SOURCES.values():
        text = (SRC_DIR / source).read_text(encoding="utf-8")
        for marker in ("TODO", "FIXME", "XXX"):
            assert marker not in text, f"{source} 里还留着 {marker}"


def test_源码里的偏移注释与_exploit_一致() -> None:
    """两道栈题都用 88 这个偏移；注释和代码对不上会把人带偏。"""
    for source in ("pwn_baby.c", "pwn_patch.c"):
        text = (SRC_DIR / source).read_text(encoding="utf-8")
        assert "88" in text, f"{source} 的注释里没写偏移 88"
        # 栈帧布局必须是 sub $0x50（80 字节）+ 保存的 rbp（8） = 88。
        assert "0x50" in text, f"{source} 的注释里没写 sub $0x50,%rsp"


def test_源码都是可编译的_c_文件() -> None:
    """轻量语法体检：至少括号配平、没有未闭合的注释。"""
    for source in pwn_targets.SOURCES.values():
        text = (SRC_DIR / source).read_text(encoding="utf-8")
        assert text.count("{") == text.count("}"), f"{source} 花括号不配平"
        assert text.count("/*") == text.count("*/"), f"{source} 注释不配平"
        assert "int main(" in text, f"{source} 里没有 main"


def _strip_comments(text: str) -> str:
    """去掉 `/* ... */` 与 `// ...`，只留真正的代码。

    注释里写出明文 flag 是**有意**的（`ENC[]` 旁边那行说明掩的是什么），
    那不是漏洞；要抓的是 flag 作为字符串字面量出现在代码里。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def test_掩码常量本身不是明文() -> None:
    """flag 直接躺在字符串常量里就等于送分题。"""
    for source in pwn_targets.SOURCES.values():
        code = _strip_comments((SRC_DIR / source).read_text(encoding="utf-8"))
        literals = [m.group(1) for m in re.finditer(r'"((?:flag|FLAG)\{[^"]*)"', code)]
        assert not literals, f"{source} 的代码里出现了明文 flag 字面量：{literals}"


def test_模块可以被_ast_解析() -> None:
    """`__init__.py` 自己也要能过一遍语法检查（不依赖运行环境）。"""
    ast.parse((SRC_DIR / "__init__.py").read_text(encoding="utf-8"))
