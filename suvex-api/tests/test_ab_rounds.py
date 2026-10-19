"""`scripts/ab_rounds.py` 的配置自检：两臂只差声明过的那个变量。

## 为什么这些测试值得单独存在

这个脚本存在的理由只有一条 —— 让"两臂只差一个环境变量"这件事
**由代码保证**，而不是由"下次重跑的人记得"保证。所以它自己必须被证明：

* 多出一个差异（哪怕只是 `CTF2_LLM_MODEL`）⇒ **必须报错退出**；
* 声明为"应当不同"的变量结果相同（镜像名写重了）⇒ **必须报错退出**，
  否则跑出来的数字会被当成"没差异"，比跑错更危险；
* 干净的对照 ⇒ 必须通过，且返回的差异集合恰好是声明的那一个。

第三条同样重要：一个"永远报错"的自检在功能上等于没有自检，
只会被人加个 `|| true` 绕过去。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ab_rounds.py"


def _load():
    spec = importlib.util.spec_from_file_location("ab_rounds_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ★ 必须先登记进 sys.modules 再 exec：被加载的文件里有 @dataclass，
    # 而 Python 3.13 的 dataclasses._is_type 会用
    # `sys.modules.get(cls.__module__).__dict__` 反查定义模块 ——
    # 没登记时拿到 None，抛
    # `AttributeError: 'NoneType' object has no attribute '__dict__'`。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


ab = _load()


def _base(**extra: str) -> dict[str, str]:
    """一份最小的基线环境：像真的一样，两臂共用的东西都在这儿。"""
    env = {"PATH": "/usr/bin", "CTF2_LLM_MODEL": "test-model", "CTF2_LLM_KEY": "k"}
    env.update(extra)
    return env


def test_only_the_image_differs_is_accepted() -> None:
    """标准跑法：只改镜像 ⇒ 通过，且差异集合恰好是 CTF2_IMAGE_BASE。"""
    arms = [ab.parse_arm("pre=img:pre"), ab.parse_arm("post=img:post")]
    differing = ab.assert_only_expected_differences(arms, _base(), ab.DEFAULT_MAY_DIFFER)
    assert list(differing) == ["CTF2_IMAGE_BASE"]
    assert differing["CTF2_IMAGE_BASE"] == {"pre": "img:pre", "post": "img:post"}


def test_an_extra_difference_is_rejected() -> None:
    """★ 核心：两臂还差了个 CTF2_LLM_MODEL ⇒ 报错，并点名是哪个变量。

    这正是"顺手多设一个变量试试"会造成的局面 —— 跑出来的数字再好看，
    它也不是"只差镜像"的对照。
    """
    arms = [ab.parse_arm("pre=img:pre"), ab.parse_arm("post=img:post")]
    arms[1].overrides["CTF2_LLM_MODEL"] = "other-model"
    with pytest.raises(ab.AbError) as raised:
        ab.assert_only_expected_differences(arms, _base(), ab.DEFAULT_MAY_DIFFER)
    message = str(raised.value)
    assert "CTF2_LLM_MODEL" in message
    assert "没有声明过" in message
    # 报错必须把两边的值都写出来，否则读的人还得自己去查。
    # 值用 repr 形态（带引号），这样"空串"和"没设置"看起来不一样。
    assert "pre='test-model'" in message
    assert "post='other-model'" in message


def test_extra_difference_can_be_declared_on_purpose() -> None:
    """想对比两个模型当然可以 —— 但必须**显式**声明，不是在脚本里猜。"""
    arms = [ab.parse_arm("a=img:a"), ab.parse_arm("b=img:b")]
    arms[1].overrides["CTF2_LLM_MODEL"] = "other-model"
    differing = ab.assert_only_expected_differences(
        arms, _base(), ("CTF2_IMAGE_BASE", "CTF2_LLM_MODEL")
    )
    assert set(differing) == {"CTF2_IMAGE_BASE", "CTF2_LLM_MODEL"}


def test_identical_images_are_rejected() -> None:
    """声明了"应当不同"却相同 ⇒ 报错。

    不加这一条的话，把 `--arm pre=X --arm post=X`（镜像名写重）会安静地
    跑出一份"两臂一模一样"的结果，读的人会以为"这改动没有影响"。
    """
    arms = [ab.parse_arm("pre=same:tag"), ab.parse_arm("post=same:tag")]
    with pytest.raises(ab.AbError) as raised:
        ab.assert_only_expected_differences(arms, _base(), ab.DEFAULT_MAY_DIFFER)
    message = str(raised.value)
    assert "没有对照物" in message
    assert "same:tag" in message


def test_a_single_arm_is_rejected() -> None:
    """一个臂跑出来的东西是基线，不是 A/B —— 名字必须先说清。"""
    with pytest.raises(ab.AbError) as raised:
        ab.assert_only_expected_differences([ab.parse_arm("only=x")], _base(), ab.DEFAULT_MAY_DIFFER)
    assert "至少要两个臂" in str(raised.value)


def test_duplicate_arm_labels_are_rejected() -> None:
    """同名臂的结果目录会互相覆盖，提前拦住。"""
    arms = [ab.parse_arm("dup=img:a"), ab.parse_arm("dup=img:b")]
    with pytest.raises(ab.AbError) as raised:
        ab.assert_only_expected_differences(arms, _base(), ab.DEFAULT_MAY_DIFFER)
    assert "重复" in str(raised.value)


def test_malformed_arm_is_rejected() -> None:
    for raw in ("justalabel", "=img", "label="):
        with pytest.raises(ab.AbError):
            ab.parse_arm(raw)


def test_difference_detection_sees_the_whole_environment() -> None:
    """比的是「每个臂真正会收到的那份环境」，不是「我打算改的那几个变量」。

    这里把差异塞进一个**没人会想到要改**的变量里，检验的是
    `diff_environments` 真的逐键扫了整份环境。
    """
    arms = [ab.parse_arm("pre=img:pre"), ab.parse_arm("post=img:post")]
    arms[1].overrides["HTTPS_PROXY"] = "http://127.0.0.1:8080"
    differing = ab.diff_environments(arms, _base())
    assert "HTTPS_PROXY" in differing
    assert differing["HTTPS_PROXY"]["pre"] is None


def test_count_solved_walks_nested_payloads() -> None:
    """热身检查的计数：mock 的列表按分类嵌套，不能假设层数。"""
    payload = {
        "data": [
            {"corpus": [{"hasSolved": True}, {"hasSolved": False}]},
            {"corpus": [{"hasSolved": True}, {"hasSolved": True}]},
        ]
    }
    assert ab._count_solved(payload) == 3
    assert ab._count_solved({"data": []}) == 0
    # 只有严格的 True 才算：JSON 里的 "true" 字符串、1、None 都不算。
    assert ab._count_solved({"hasSolved": "true"}) == 0
    assert ab._count_solved({"hasSolved": 1}) == 0

def test_arena_key_comes_from_the_command_line_first() -> None:
    """命令行给的优先 —— 想临时换一个值不该去改环境变量。"""
    assert ab.resolve_arena_key("from-cli") == "from-cli"


def test_arena_key_falls_back_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 --key 时读 CTF2_ARENA_KEY。

    注意断言的是"读到了环境里的那个值"，不是"等于某个硬编码常量" ——
    否则这个测试会变成把凭据钉死在测试里。
    """
    monkeypatch.setenv("CTF2_ARENA_KEY", "from-env")
    assert ab.resolve_arena_key(None) == "from-env"
    # 空白也算没给：`export CTF2_ARENA_KEY=` 是很常见的手滑。
    monkeypatch.setenv("CTF2_ARENA_KEY", "   ")
    with pytest.raises(ab.AbError):
        ab.resolve_arena_key(None)


def test_missing_arena_key_names_both_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 两个来源都没有 ⇒ 报错退出，而不是退到某个默认假值往下跑。

    留默认值的后果具体是：mock 一直回 `A0231 AccessKey 无效`，十道题
    全部"没解出"，而这份结果会被当成"工具补强没用"的结论。
    所以错误信息必须同时点名 --key 和 CTF2_ARENA_KEY。
    """
    monkeypatch.delenv("CTF2_ARENA_KEY", raising=False)
    with pytest.raises(ab.AbError) as raised:
        ab.resolve_arena_key(None)
    message = str(raised.value)
    assert "--key" in message
    assert "CTF2_ARENA_KEY" in message
    # 脚本里不能还留着一个能跑通的默认值。
    assert "ak_test_local" not in message
