"""覆盖度缺口标注（v1.82）。"""

from __future__ import annotations

from app.assets.service import coverage_gaps


def test_gaps_mark_recorded_categories() -> None:
    tested = [
        {"factKey": "surface/login-form", "summary": "登录接口接受表单提交", "detail": ""},
        {"factKey": "surface/upload-avatar", "summary": "头像上传无类型校验", "detail": ""},
    ]

    gaps = {gap["key"]: gap for gap in coverage_gaps(tested)}

    assert gaps["auth"]["recorded"] is True
    assert gaps["input"]["recorded"] is True
    assert gaps["cloud"]["recorded"] is False
    assert gaps["cloud"]["matches"] == []


def test_gaps_are_cognition_not_vulnerability_judgement() -> None:
    # 没有记录 = 没有认知；函数只返回 recorded=False，不推断"没测过"。
    gaps = coverage_gaps([])

    assert len(gaps) == 8
    assert all(gap["recorded"] is False for gap in gaps)
