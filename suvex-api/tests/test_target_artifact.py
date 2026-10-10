"""`artifact`（只有附件）的目标规则（任务 17）。

## 这个文件里最该保住的是**别的分支没被顺手放宽**

为了让 `artifact` 能提交空目标，`ScanCreate.target` 从
`Field(min_length=1)` 改成了 `Field(default="")` —— 那是**一处全局放宽**。

于是真正的问题不是"artifact 能不能过"，而是
**"另外四种是不是还要求必填"**。这类改动的典型事故就是：
新功能测试全绿，而 URL 扫描从此可以不带目标建出来 —— 然后预检去探一个
空字符串，失败原因莫名其妙。

所以这里的 `test_other_kinds_still_require_a_target` 是**回归钉子**，
不是凑数的参数化。
"""

from __future__ import annotations

from typing import get_args

import pytest
from sqlalchemy.orm import Session

from app.core.vocab import TargetKind
from app.scans import targets
from app.scans.schemas import TargetKindLiteral
from tests.factories import make_scan


# ══════════════════════════════════════════════════════════
# 一、artifact：空目标是**正确状态**
# ══════════════════════════════════════════════════════════


def test_empty_target_is_valid_for_artifact() -> None:
    """★ 空串对 `artifact` 是合法的 —— 这是全项目唯一一个这样的分支。

    实现上它必须在"空值早退"**之前**判（见 `targets.validate`）：
    顺序反了的话，逆向题会在建扫描的第一步被判「请填写目标」，
    而那个报错完全说不出该怎么办。
    """
    check = targets.validate(TargetKind.ARTIFACT, "")

    assert check.ok is True
    assert check.error is None
    # hint 要指向**下一步该做什么**（去传附件），而不是解释为什么不用填
    assert "附件" in (check.hint or "")


def test_whitespace_only_is_treated_as_empty() -> None:
    """只有空格也算空 —— 用户从别处复制粘贴时很容易带上空白。"""
    assert targets.validate(TargetKind.ARTIFACT, "   \n ").ok is True


def test_artifact_with_an_address_is_refused() -> None:
    """★ 填了地址就报错，**不能静默忽略**。

    这个模式下我们不会把任何地址交给执行后端（target 是解压出来的目录），
    所以用户填的地址会被丢掉。而"我明明填了地址它却没去扫"是最难归因的
    一类问题 —— 用户会以为扫过了、只是没发现问题。

    报错还要给出**替代动作**：想扫地址就换一种目标类型。
    """
    check = targets.validate(TargetKind.ARTIFACT, "http://example.com")

    assert check.ok is False
    assert "不需要目标地址" in (check.error or "")
    assert "目标类型" in (check.hint or ""), "没说那该怎么办"


def test_normalize_of_artifact_is_always_empty() -> None:
    """归一化必须清成空串 —— `scans.target` 里的附件名由 `create_scan` 填。"""
    assert targets.normalize(TargetKind.ARTIFACT, "http://example.com") == ""
    assert targets.normalize(TargetKind.ARTIFACT, "") == ""


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # 常见形态：去掉扩展名就是题目名
        ("tryre.zip", "tryre"),
        ("chall.tar.gz", "chall.tar"),  # 只去最后一段，见 docstring 的取舍
        ("试试.rar", "试试"),
        # 没有扩展名：原样（CTF 里叫 `chall` 的附件很常见）
        ("chall", "chall"),
        # 以点开头：`rsplit` 会得到空串，必须有兜底
        (".hidden", ".hidden"),
    ],
)
def test_default_name_comes_from_the_attachment_name(filename: str, expected: str) -> None:
    """名字从**附件名**推导 —— 列表里那一行要能让人认出是哪道题。"""
    assert targets.default_name(TargetKind.ARTIFACT, filename) == expected


# ══════════════════════════════════════════════════════════
# 二、★ 回归钉子：另外四种**仍然**要求必填
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "kind",
    [TargetKind.URL, TargetKind.REPO, TargetKind.PATH, TargetKind.SERVICE],
)
def test_other_kinds_still_require_a_target(kind: str) -> None:
    """★★ 放宽 schema 的 `min_length=1` 之后，这四种**必须照旧**拒绝空目标。

    这是整个 `artifact` 改动里**唯一一处全局放宽** —— 而它的代价
    全落在这四种身上。少了这条测试，"URL 扫描可以不填目标"会一路绿到线上，
    症状是预检去探一个空字符串、然后给出一句莫名其妙的失败原因。
    """
    check = targets.validate(kind, "")

    assert check.ok is False, f"{kind} 竟然接受了空目标"
    assert check.error == "请填写目标"


@pytest.mark.parametrize(
    "kind",
    [TargetKind.URL, TargetKind.REPO, TargetKind.PATH, TargetKind.SERVICE],
)
def test_whitespace_only_is_still_refused_for_other_kinds(kind: str) -> None:
    """只有空格同样不算填了 —— 与上面那条同一条边界。"""
    assert targets.validate(kind, "   ").ok is False


# ══════════════════════════════════════════════════════════
# 三、对账：字面量与枚举不许分叉
# ══════════════════════════════════════════════════════════


def test_target_kind_literal_matches_the_enum() -> None:
    """★ `TargetKindLiteral` 必须与 `TargetKind` **一个不多一个不少**。

    `schemas.py` 里那个字面量是**抄了一份**的枚举，而这份拷贝没有任何
    机制保证同步 —— 与本项目在 `findings.status` 上真踩过的
    「改了枚举、漏了 schema」是同一类隐患（那次漏掉两个状态）。

    这条测试不靠人眼对账：直接从两处取出取值范围比集合。
    """
    assert set(get_args(TargetKindLiteral)) == {kind.value for kind in TargetKind}


def test_database_accepts_every_target_kind(db_session: Session) -> None:
    """★★ 库里那条 CHECK 约束必须放行**每一个** `TargetKind` 取值。

    这条盯的是一个**工具不会提醒**的坑：`--autogenerate` **不比对 CHECK
    约束的内容**。所以"给 TargetKind 加了取值、也写了迁移"这件事，
    光看代码是看不出来的 —— 只有真的往库里插一行才知道做没做全。

    而漏掉的症状是「建某种目标的扫描时接口报错了」，看起来像代码 bug，
    排查方向完全错了。（这个项目在 `findings.status` 上真踩过一次。）
    """
    for kind in TargetKind:
        scan = make_scan(db_session, scan_id=f"scan_probe_{kind.value}")
        scan.target_kind = kind.value
        scan.target = "x"
        # 撞约束就在这里抛 —— 不需要额外断言：抛了就红
        db_session.commit()
