"""`scripts/verify_sandbox_tools.py` 的两道筛子。

## 这个脚本在守什么

`verify_sandbox_tools.py` 是「沙箱能力契约」：它读 `docker/*/Dockerfile`，
确认提示词里承诺给模型的工具**确实写在了镜像的安装列表里**。

## 为什么会有第二个文件里的第二道筛子

第一道只读文本。历史事故里最贵的一次是：`patchelf` / `gdb` 在提交
`2de97ce`（2026-10-18）才写进 Dockerfile，而线上镜像是 2026-10-12 构建的
—— 6 天里脚本全报 ✅，真实镜像里两个命令都不存在。这类"文本对了、产物没跟上"
静态解析**永远**看不见，只能去问镜像本身。

## 所以这里锁死两条

1. **两步结果绝不合并。** 「名单写错」（包在、命令名不对 → 改翻译表）和
   「镜像没跟上」（包没装 → 必须重建镜像）修法相反、成本差一个数量级。
   合成一个 ❌ 的结果，读的人只能猜，而猜错的默认动作是白重建一次镜像。
   实测中 30 项里 28 项是前者，重建多少次都不会好。

2. **没验 ≠ 通过。** 没有 docker / 镜像没构建 / 探测超时，第二道就跑不了。
   这种情况下必须明说"未跑"，绝不能打印「声明与产物一致」。
   改动前它真的这么打了 —— 一个专抓"镜像没跟上"的脚本，自己在没有 docker
   的机器上谎报通过，是同一个错误在脚本自己身上又发生了一次。

## 为什么测试不碰真实 docker

第二道的判据只有在"命令缺失"时才触发，而真实镜像里制造一次缺失就得重建
镜像（几分钟 + 影响在跑的容器）。所以 `check()` 留了 `probe_fn` 注入点，
测试用假探测函数精确构造两种现场。注入点**没有**命令行开关或环境变量，
免得有人拿它去跑出一个绿的 CI。
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from scripts import verify_sandbox_tools as vst  # noqa: E402

# ══════════════════════════════════════════════════════════
# 造一个"镜像返回了什么"的假探测函数
# ══════════════════════════════════════════════════════════


def fake_probe(
    *,
    missing_cmds: Iterable[str] = (),
    uninstalled: Iterable[tuple[str, str]] = (),
    absent_images: Iterable[str] = (),
    status: vst.ProbeStatus | None = None,
):
    """构造一个假的 `probe_image_full`。

    · `missing_cmds`  —— 这些命令报"镜像里没有"（其余全报有）；
    · `uninstalled`   —— 这些包报"没装"（其余全报装了）；
    · `absent_images` —— 这些镜像报"拿不到证据"（返回 `ProbeStatus.NO_EVIDENCE`）；
    · `status`        —— 给 `absent_images` 里的镜像指定**具体**的没证据原因
      （`NO_DOCKER` / `IMAGE_MISSING` / `TIMEOUT` …）。不指定时用
      `NO_EVIDENCE`，即"原因不明"那条兼容路径。

    默认「命令都在、包都装了」，即一个健康镜像 —— 这样每个测试只需要
    声明它关心的那一个变量，其余全是已知的好状态。
    """
    missing = set(missing_cmds)
    not_installed = set(uninstalled)
    no_evidence = set(absent_images)
    failure = status or vst.ProbeStatus.NO_EVIDENCE
    calls: list[str] = []

    def _probe(image, commands, packages):
        calls.append(image)
        if image in no_evidence:
            return vst.ProbeResult(status=failure, image=image)
        cmd_result = {cmd: (cmd not in missing) for cmd in commands}
        pkg_result = {(pkg, kind): ((pkg, kind) not in not_installed) for pkg, kind in packages}
        return vst.ProbeResult(
            status=vst.ProbeStatus.OK, image=image, commands=cmd_result, packages=pkg_result
        )

    _probe.calls = calls  # type: ignore[attr-defined]
    return _probe


@pytest.fixture()
def no_docker(monkeypatch):
    """把 `shutil.which("docker")` 变成 `None` —— 模拟本机没有 docker。

    这台开发机（Windows）确实是这个状态，但测试不依赖这一点：
    显式打桩才能在装了 docker 的 CI 上得到同样的结论。
    """
    monkeypatch.setattr(vst.shutil, "which", lambda _name: None)


# ══════════════════════════════════════════════════════════
# ① 两步结果绝不合并
# ══════════════════════════════════════════════════════════


def test_naming_error_and_image_lag_stay_in_separate_buckets(monkeypatch):
    """同一个命令，成因不同 → 必须落进**不同**的桶。

    这是整个改动的核心断言。两栏如果被任何一个重构合成一栏，
    这条就会红 —— 而它红的样子（"两者被合并了"）比"报错说缺工具"
    重要得多：合并之后修法就丢了。

    这里用 `monkeypatch.setitem` 往翻译表里塞**只在这个测试里存在**的条目，
    而不是去改真表：真表里现在没有"声明了但不存在的命令"了（那正是
    这次修掉的东西），而这条测试要验的恰恰是那个现场。
    """
    # 现场 A：包装好了，但命令名不对
    monkeypatch.setitem(vst.PACKAGE_COMMANDS, "demo-pkg", ("demo-cmd", "demo-real"))
    image = vst.ImagePackages(name="ctf2-sandbox", apt=["demo-pkg"], pip=[])
    drift = vst.classify_drift(
        "ctf2-sandbox",
        image,
        {"demo-cmd": False, "demo-real": True},
        {("demo-pkg", "apt"): True},
    )
    assert [cmd for cmd, _pkg in drift.naming_errors] == ["demo-cmd"]
    assert drift.image_lagging == [], "包已装，不该报成镜像没跟上"
    assert not drift.clean

    # 现场 B：包根本没装（历史上的 `patchelf` / `gdb`）
    image_b = vst.ImagePackages(name="ctf2-sandbox", apt=["gdb"], pip=[])
    drift_b = vst.classify_drift(
        "ctf2-sandbox",
        image_b,
        {"gdb": False},
        {("gdb", "apt"): False},
    )
    assert [cmd for cmd, _pkg in drift_b.image_lagging] == ["gdb"]
    assert drift_b.naming_errors == [], "包没装，不该报成名单写错"


def test_second_screen_prints_two_labelled_sections(capsys):
    """报告面上 A / B 必须各占一段，带各自的修法。"""
    naming = vst.ImageDrift(
        image="ctf2-sandbox-re",
        naming_errors=[("csc", "mono-devel")],
    )
    lagging = vst.ImageDrift(
        image="ctf2-sandbox",
        image_lagging=[("gdb", "gdb")],
    )
    vst.print_second_screen({"ctf2-sandbox": lagging, "ctf2-sandbox-re": naming}, "")
    out = capsys.readouterr().out

    # 两个具体条目都要出现，且各自带镜像名
    assert "csc" in out and "ctf2-sandbox-re" in out
    assert "gdb" in out and "ctf2-sandbox" in out
    # 两栏标题分开
    assert "A 名单写错" in out
    assert "B 镜像没跟上" in out
    # 两种修法必须都写出来，且方向相反
    assert "PACKAGE_COMMANDS" in out  # A 只改文本
    assert "docker build" in out  # B 才重建镜像
    # A 的段落必须明说不用重建 —— 否则读的人还是会去重建
    a_line = out.split("A 名单写错", 1)[1].split("B 镜像没跟上", 1)[0]
    assert "不用重建" in a_line


def test_unknown_is_its_own_bucket_not_a_pass():
    """包状态问不到时 → `unknown`，既不是通过，也不许猜成 A 或 B。"""
    image = vst.ImagePackages(name="ctf2-sandbox", apt=["gdb"], pip=[])
    drift = vst.classify_drift(
        "ctf2-sandbox",
        image,
        {"gdb": False},
        {},  # 没有任何包状态 —— 探测那一半失败了
    )
    assert drift.unknown == ["gdb"]
    assert drift.naming_errors == []
    assert drift.image_lagging == []
    assert not drift.clean, "unknown 不许当成通过"


def test_commands_that_exist_are_not_reported():
    """命令都在 → 两栏都空。宁可漏报也不能天天假报（假报会让脚本被无视）。"""
    image = vst.ImagePackages(name="ctf2-sandbox", apt=["gdb", "vim-tiny"], pip=[])
    drift = vst.classify_drift(
        "ctf2-sandbox",
        image,
        {"gdb": True, "vi": True, "vim.tiny": True},
        {("gdb", "apt"): True, ("vim-tiny", "apt"): True},
    )
    assert drift.clean
    assert drift.naming_errors == [] and drift.image_lagging == [] and drift.unknown == []


# ══════════════════════════════════════════════════════════
# ② docker 缺失时不是静默成功
# ══════════════════════════════════════════════════════════


def test_no_docker_says_not_run_instead_of_passing(no_docker, capsys):
    """没有 docker → 必须明说「第二道未跑」，且**不许**说"一致"。"""
    outcome = vst.check(probe=True)
    out = capsys.readouterr().out

    assert outcome.probe_attempted, "请求了 --probe"
    assert not outcome.probe_checked, "但没有拿到任何证据"
    assert outcome.probe_not_run_reason, "必须给出没跑的原因"
    assert "docker" in outcome.probe_not_run_reason.lower()

    # 说清"没跑"
    assert "第二道未跑" in out
    assert "没验" in out or "没有证据" in out

    # ★ 最要紧的一条：不许出现"一致"这种结论式措辞
    assert "声明与产物一致" not in out


def test_main_exit_code_stays_zero_without_docker(no_docker, monkeypatch, capsys):
    """跳过第二道**不是失败** —— CI 上没有 docker 是常态，不能把 CI 弄红。"""
    monkeypatch.setattr(sys, "argv", ["verify_sandbox_tools.py", "--probe"])
    code = vst.main()
    out = capsys.readouterr().out

    assert code == 0
    assert "第二道未跑" in out
    assert "声明与产物一致" not in out


def test_missing_image_is_also_not_a_pass(monkeypatch, capsys):
    """docker 在、但镜像没构建 → 同样是"没证据"，不是通过。

    这一条防的是：有人把"拿不到结果"当成"结果是好的"。
    """
    monkeypatch.setattr(vst.shutil, "which", lambda _name: "/usr/bin/docker")
    probe = fake_probe(absent_images=["ctf2-sandbox", "ctf2-sandbox-re", "ctf2-sandbox-ir"])
    outcome = vst.check(probe=True, probe_fn=probe)
    out = capsys.readouterr().out

    assert not outcome.probe_checked
    assert "第二道未跑" in out
    assert "声明与产物一致" not in out


# ══════════════════════════════════════════════════════════
# 漂移真的能被抓到（端到端：走 check + 报告面）
# ══════════════════════════════════════════════════════════


def test_drift_is_detected_and_attributed(monkeypatch, capsys):
    """故意让镜像里"少"一个已声明的工具 → 报出**哪个工具、哪个镜像**。

    这里复刻的是历史事故的形状：`patchelf` / `gdb` 写进了 Dockerfile，
    镜像里却没有。用假探测函数模拟，不改任何真文件。
    """
    monkeypatch.setattr(vst.shutil, "which", lambda _name: "/usr/bin/docker")
    probe = fake_probe(
        missing_cmds=["gdb"],
        uninstalled=[("gdb", "apt")],
    )
    outcome = vst.check(probe=True, probe_fn=probe)
    out = capsys.readouterr().out

    assert outcome.probe_checked, "这次拿到证据了"
    confirmed = outcome.drift_confirmed
    assert confirmed, "缺了 gdb，必须报出来"

    # 必须能说出**哪个镜像**缺**哪个工具**
    reported = [(d.image, cmd) for d in confirmed for cmd, _pkg in d.image_lagging]
    assert reported, f"没有指名道姓地报出缺失，实际输出：\n{out}"
    assert any(cmd == "gdb" for _image, cmd in reported)
    assert all(image.startswith("ctf2-sandbox") for image, _cmd in reported)
    assert "gdb" in out


def test_drift_changes_exit_code(monkeypatch, capsys):
    """`--probe` 抓到漂移时必须返回非 0。

    否则 CI 加不加 `--probe` 都一样绿 —— 那这个开关等于没接上。
    """
    monkeypatch.setattr(vst.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        vst,
        "check",
        lambda **kwargs: _outcome_with_lag("ctf2-sandbox", "gdb", "gdb"),
    )
    monkeypatch.setattr(sys, "argv", ["verify_sandbox_tools.py", "--probe"])
    assert vst.main() == 1
    out = capsys.readouterr().out
    assert "gdb" in out
    assert "docker build" in out  # 修法指向重建


def test_clean_probe_is_the_only_way_to_say_consistent(monkeypatch, capsys):
    """"一致"这句话只在**真的验过且干净**时出现。"""
    monkeypatch.setattr(vst.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        vst,
        "check",
        lambda **kwargs: _outcome_with_lag(None, None, None),
    )
    monkeypatch.setattr(sys, "argv", ["verify_sandbox_tools.py", "--probe"])
    assert vst.main() == 0
    assert "声明与产物一致" in capsys.readouterr().out


def _outcome_with_lag(image: str | None, cmd: str | None, pkg: str | None):
    """造一个 `CheckOutcome`：`image=None` 表示"验过且干净"。"""
    outcome = vst.CheckOutcome([])
    outcome.probe_attempted = True
    if image is None:
        outcome.drifts = {"ctf2-sandbox": vst.ImageDrift(image="ctf2-sandbox")}
    else:
        outcome.drifts = {
            image: vst.ImageDrift(image=image, image_lagging=[(cmd or "", pkg or "")])
        }
    return outcome


# ══════════════════════════════════════════════════════════
# 向后兼容：第一道行为与退出码不许变
# ══════════════════════════════════════════════════════════


def test_check_return_value_is_still_a_list():
    """`check()` 的返回值仍当列表用 —— 老调用点不用改。

    `CheckOutcome` 继承 `list` 就是为了这个：第一道的语义一个字不动，
    第二道只是多挂几个属性。换成 dataclass 会逼所有调用点同步改，
    漏一处就是"检查永远报绿"。
    """
    outcome = vst.check()
    assert isinstance(outcome, list)
    assert len(outcome) == len(list(outcome))


def test_static_only_run_is_green_and_exits_zero(monkeypatch, capsys):
    """不带 `--probe`（CI 的调用方式）→ 与改动前一致：全绿、退出码 0。"""
    monkeypatch.setattr(sys, "argv", ["verify_sandbox_tools.py"])
    assert vst.main() == 0
    out = capsys.readouterr().out
    assert "每个分类需要的工具" in out
    assert "声明与产物一致" not in out, "没跑第二道时不能这么说"
    # 静态局限说明必须还在 —— 它提醒读者这个 ✅ 覆盖不到产物层
    assert "镜像没重建" in out


def test_static_layer_has_no_gap_right_now():
    """当前仓库的 Dockerfile 满足能力契约（第一道全绿）。"""
    findings = vst.check()
    assert findings == [], f"静态层出现缺口：{[(f.category, f.missing) for f in findings]}"


# ══════════════════════════════════════════════════════════
# 翻译表：包名 ≠ 命令名（实测驱动，防止再写回车库）
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("pkg", "must_not_claim"),
    [
        # `dpkg -L vim-tiny` 只给 `/usr/bin/vim.tiny` 与 `/usr/bin/vi`
        ("vim-tiny", "vim"),
        # Kali rolling 的 `mono-devel` 给 `mcs` / `mono-csc` / `monodis`，没有 `csc`
        ("mono-devel", "csc"),
        # oletools 0.60.2 上游已移除 `oledump.py`
        ("oletools", "oledump.py"),
    ],
)
def test_translation_table_does_not_claim_nonexistent_commands(pkg, must_not_claim):
    """这三条都曾把"不存在的命令"当成会有的命令。

    写错时的表现是：脚本说镜像里有 `vim`，而模型在沙箱里 `vim` 一声
    command not found —— 静默失败，只能靠人肉发现。
    """
    assert must_not_claim not in vst.PACKAGE_COMMANDS[pkg]


def test_translation_table_has_no_package_names_as_commands():
    """包名本身不该被当成命令名报出去。

    `python3-pip` / `ruby-dev` / `ocl-icd-libopencl1` 都不提供同名命令
    （实测 `dpkg -L` 取证）。留它们在命令集合里，第二道就会每次都报
    "镜像里没有 python3-pip"，把真漂移淹掉。
    """
    declared = vst.ImagePackages(
        name="ctf2-sandbox",
        apt=["python3-pip", "ruby-dev", "ocl-icd-libopencl1"],
        pip=[],
    ).declared()
    assert "python3-pip" not in declared
    assert "ruby-dev" not in declared
    assert "ocl-icd-libopencl1" not in declared
    # 但它们确实提供的东西要在
    assert "pip" in declared


def test_owners_maps_commands_back_to_packages():
    """反查表：第二道要问"这个命令是谁承诺的"，才能二分。"""
    image = vst.ImagePackages(name="x", apt=["vim-tiny", "gdb"], pip=["demo-lib"])
    owners = image.owners()
    assert ("vim-tiny", "apt") in owners["vi"]
    assert ("gdb", "apt") in owners["gdb"]
    # pip 包走另一条查询路径，标记不能丢
    assert owners["demo-lib"] == [("demo-lib", "pip")]


def test_pip_lookup_is_case_insensitive_but_apt_is_not():
    """pip 包名大小写不敏感，apt 包名敏感。

    `pip list` 打的是 `PyYAML`，而 Dockerfile 里可能写 `pyyaml` —— 同一次
    查表必须两种写法都能命中，否则纯库会被报成"有 CLI 但镜像里没有"。
    反向不能顺手 apply 到 apt：apt 包名本来就全小写，多一层 lower 只会
    让某个将来真叫 `Foo` 的包被静默匹配到 `foo`。
    """
    assert vst.ImagePackages(name="x", apt=[], pip=["PyYAML"]).declared() == []
    assert vst.ImagePackages(name="x", apt=[], pip=["pyyaml"]).declared() == []
    # apt 侧另有一条 `Xvfb` 这样的真命令，它的大小写必须原样保留
    image = vst.ImagePackages(name="x", apt=["xvfb"], pip=[])
    assert "Xvfb" in image.declared()


def test_declared_covers_every_declared_package():
    """`declared()` 是第二道要逐个 `command -v` 的清单，不能漏包。"""
    image = vst.ImagePackages(name="x", apt=["gdb", "vim-tiny"], pip=["demo-lib"])
    declared = image.declared()
    assert "gdb" in declared
    assert "vi" in declared
    assert "demo-lib" in declared
    # 保序，报告才稳定
    assert declared == list(dict.fromkeys(declared))


def test_declared_skips_packages_known_to_offer_no_command():
    """纯库（pandas / pyyaml / lief…）不贡献命令，别塞进 probe 名单。

    它们确实装了、也确实没有 CLI。当成命令去问，第二道就会拿一堆
    "镜像里没有 pandas" 的假缺失把真漂移淹掉 —— 而淹没之后这个脚本
    又回到"都报红、于是没人看"。
    """
    image = vst.ImagePackages(
        name="x",
        apt=[],
        pip=["pandas", "PyYAML", "lief", "python-dateutil", "openpyxl"],
    )
    assert image.declared() == []
    assert image.commands() == set()
