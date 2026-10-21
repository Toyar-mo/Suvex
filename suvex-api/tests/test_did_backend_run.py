"""`did_backend_run` 按后端分派"真的启动过吗"。

## 为什么要有这个东西

`finalize` 的 `strix_ran` 参数本来只纠一条具体的谎报：strix 因 argparse
用法错误退 `2`，而 `2` 同时又是"发现了漏洞"，于是什么都没干的一次记录
被记成「已完成」。判据是 `strix_runs/` 目录在不在。

但自研引擎（agent 后端）的退出码语义**不同**，它也不建 `strix_runs/`。
如果一律用 `did_strix_run`，自研引擎的**每一次**成功都会被判成"没跑起来"，
再被写成失败。所以必须按后端分派：agent 看 `agent_run/run.json`。

认不出的后端名一律按 True —— 不是"放行"，而是"不拿一个我不认识的判据
去否定一次记录"（那个二义性只在 strix 的退出码 `2` 上存在）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.scans.runner import did_agent_run, did_backend_run, did_strix_run


def test_did_agent_run_true_when_run_json_present(tmp_path: Path) -> None:
    run_dir = tmp_path / "scan"
    (run_dir / "agent_run").mkdir(parents=True)
    (run_dir / "agent_run" / "run.json").write_text("{}", encoding="utf-8")

    assert did_agent_run(str(run_dir)) is True


def test_did_agent_run_false_when_run_json_missing(tmp_path: Path) -> None:
    """目录在但 `run.json` 不在 = 引擎没进 solve 循环。"""
    run_dir = tmp_path / "scan"
    (run_dir / "agent_run").mkdir(parents=True)

    assert did_agent_run(str(run_dir)) is False


def test_did_agent_run_false_when_run_dir_absent(tmp_path: Path) -> None:
    assert did_agent_run(str(tmp_path / "nope")) is False


def test_did_agent_run_none_is_false() -> None:
    assert did_agent_run(None) is False


def test_did_strix_run_only_looks_at_strix_runs(tmp_path: Path) -> None:
    """交叉验证：agent 的产物目录不会被 strix 判据认成"跑过"。"""
    run_dir = tmp_path / "scan"
    (run_dir / "agent_run").mkdir(parents=True)
    (run_dir / "agent_run" / "run.json").write_text("{}", encoding="utf-8")

    assert did_strix_run(str(run_dir)) is False


@pytest.mark.parametrize("name", ["strix", "agent", "fake", None, "future-backend"])
def test_did_backend_run_never_raises(tmp_path: Path, name: str | None) -> None:
    """任何后端名都不该炸 —— 它跑在收尾路径上，抛异常会把终态写坏。"""
    run_dir = tmp_path / "scan"
    run_dir.mkdir(parents=True)

    assert isinstance(did_backend_run(str(run_dir), runner_name=name), bool)


def test_did_backend_run_dispatches_agent_to_agent_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "scan"
    (run_dir / "agent_run").mkdir(parents=True)
    (run_dir / "agent_run" / "run.json").write_text("{}", encoding="utf-8")

    assert did_backend_run(str(run_dir), runner_name="agent") is True
    # 同一个目录，strix 判据看不到 strix_runs → False
    assert did_backend_run(str(run_dir), runner_name="strix") is False


def test_did_backend_run_trusts_unknown_backend(tmp_path: Path) -> None:
    """认不出的后端名按 True：不拿陌生判据否认一次记录。"""
    run_dir = tmp_path / "scan"
    run_dir.mkdir(parents=True)

    assert did_backend_run(str(run_dir), runner_name="fake") is True
    assert did_backend_run(str(run_dir), runner_name=None) is True
    assert did_backend_run(str(run_dir), runner_name="future-backend") is True
