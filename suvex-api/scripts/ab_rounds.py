#!/usr/bin/env python3
"""A/B 对照执行器：**一条命令跑完所有臂**，并在开跑前证明两臂只差一个变量。

## 为什么要有这个脚本（而不是留在 `_tmp/` 里）

"两臂只差一个环境变量"是这类对照实验**唯一的可复现依据**。
在此之前，它由两份手抄的 shell 脚本维持 —— 而下次要重跑时，最容易被
顺手改错、又最容易被忽略的，恰恰就是这条前提（多设一个 `CTF2_LLM_MODEL`
试试、把某个臂的 `--max-turns` 从 30 改成 40、"反正都是跑一遍"）。
重写脚本时凭记忆恢复，等于把结论交给运气。

所以这里做三件事：

1. **一条命令跑完全部臂**（而不是"先跑 A、再跑 B"）——
   两臂的配置在同一处构造，才有东西可以 diff。
2. **开跑前自检**：把每个臂**真正会收到的环境变量**逐键比对，
   差异集合必须恰好是允许的那几个（默认只有 `CTF2_IMAGE_BASE`），
   否则打印出到底哪几个键不同、各是什么值，然后**退出**。
   不往下跑 —— 跑出来的数字一旦"两臂还差了别的"，越好看越有害。
3. **写盘留证**：`<out>/summary.json` 里记下这次的 env diff 结果、
   两臂的镜像 id 与创建时间、git commit。事后有人问"这是不是干净的对照"，
   答案在文件里，不在记性里。

## 用法

    # 两个臂各跑 5 轮，两臂只差镜像（P0 pwn 补强的标准跑法）
    python3 scripts/ab_rounds.py \\
        --arm pre=ctf2-sandbox:pre-gems \\
        --arm post=ctf2-sandbox:latest \\
        --ids 1003 1006 --rounds 5 --out _tmp/ab-p0

    # 只做自检、不跑（改完脚本先跑这个）
    python3 scripts/ab_rounds.py --arm a=x --arm b=y --check-only

    # 显式声明允许的差异（默认只有 CTF2_IMAGE_BASE）
    python3 scripts/ab_rounds.py --arm a=imgA --arm b=imgB --may-differ CTF2_IMAGE_BASE

## 环境要求

* **Docker**（要起沙箱容器）与已构建好的各臂镜像；
* 一个可用的 LLM：环境变量 `CTF2_LLM_MODEL` + `CTF2_LLM_KEY`
  （可选 `CTF2_LLM_BASE_URL` / `CTF2_LLM_LABEL` / `CTF2_LLM_KIND`）。
  没配的话 `arena_e2e.load_provider()` 会退到 providers 表，
  那条路还要 `SUPEX_SECRET_KEY` —— 这个脚本**不替你猜**，
  缺什么就明确说缺什么然后退出。

## 为什么每轮都重启 mock

`scripts/mock_arena_server.py` 的 `hasSolved` 在**进程内存**里，且没有
reset 端点。不重启的话第 2 轮起每题都判 `A0409 该答案已提交过`，
把"已解"测成"没解"。每轮重启前还会查一遍热身状态，非 0 就退出。

## 为什么每轮独立目录

transcript 落在 `<--out 的目录>/transcripts/`（见 `ctf2_baseline.py` 里
`transcript_dir = Path(args.out).parent / "transcripts"`），
共用目录会让后一轮覆盖前一轮 —— 而 transcript 是事后归因的唯一材料。

## 不进 CI

要 Docker、要真实 LLM 调用、一轮几分钟。它是度量工具，不是门禁。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MOCK_SCRIPT = REPO_ROOT / "scripts" / "mock_arena_server.py"
BASELINE_SCRIPT = REPO_ROOT / "scripts" / "ctf2_baseline.py"

#: 默认允许两臂不同的环境变量 —— 只有镜像。
#:
#: 想换别的（例如对比两个模型）必须**显式** `--may-differ CTF2_LLM_MODEL`，
#: 让"这次对照到底在比什么"变成一个被人写下来的决定，而不是脚本里的猜测。
DEFAULT_MAY_DIFFER = ("CTF2_IMAGE_BASE",)

#: 只影响"跑多快/跑多少"、不影响被测对象的变量。它们在两臂之间**允许相同**
#: （本来就相同），列出来只是为了报错时能说清"这不是差异"。
_RUN_KNOBS = ("CTF2_GIT_COMMIT",)


class AbError(RuntimeError):
    """预检没过。**一律不静默继续** —— 对照实验跑歪了比没跑更糟。"""


# ══════════════════════════════════════════════════════════
# 臂与自检
# ══════════════════════════════════════════════════════════


@dataclass
class Arm:
    """一个实验臂：标签 + 它相对基线环境额外设置的变量。"""

    label: str
    overrides: dict[str, str] = field(default_factory=dict)

    def env(self, base: dict[str, str]) -> dict[str, str]:
        """这个臂**真正会收到**的环境变量（基线 + 覆盖）。"""
        merged = dict(base)
        merged.update(self.overrides)
        return merged


def parse_arm(raw: str) -> Arm:
    """`label=IMAGE` → Arm，覆盖 `CTF2_IMAGE_BASE`。"""
    label, sep, image = raw.partition("=")
    label, image = label.strip(), image.strip()
    if not sep or not label or not image:
        raise AbError(
            f"--arm 要写成 `<标签>=<镜像>`，收到 {raw!r}。"
            "例如 `--arm pre=ctf2-sandbox:pre-gems`"
        )
    return Arm(label=label, overrides={"CTF2_IMAGE_BASE": image})


def diff_environments(
    arms: list[Arm], base: dict[str, str]
) -> dict[str, dict[str, str | None]]:
    """逐键比对各臂最终环境，返回 `{变量名: {臂标签: 值}}`，只含**有差异**的键。

    ★ 比的是"每个臂真正会收到的那份环境"，不是"我打算改的那几个变量"。
    两者差得很远：有人给某个臂多设一个 `CTF2_LLM_MODEL`、或者直接在
    外层 shell 里 `export` 了只该给一个臂用的东西，前者会被这里抓到，
    后者也会（因为 base 是进程自己的 `os.environ`）。
    """
    envs = {arm.label: arm.env(base) for arm in arms}
    keys: set[str] = set()
    for env in envs.values():
        keys.update(env)
    differing: dict[str, dict[str, str | None]] = {}
    for key in sorted(keys):
        values = {label: env.get(key) for label, env in envs.items()}
        if len(set(values.values())) > 1:
            differing[key] = values
    return differing


def assert_only_expected_differences(
    arms: list[Arm], base: dict[str, str], may_differ: tuple[str, ...]
) -> dict[str, dict[str, str | None]]:
    """自检的核心。**不满足就抛 `AbError`**，并且把差异原样打出来。

    三道题，缺一不可：

    * **多出来的差异** —— 除了 `may_differ` 之外还有键不同 ⇒ 不是干净对照；
    * **该差的不差** —— `may_differ` 里的键在两臂上取值相同 ⇒ 两臂其实是
      同一个配置（例如镜像名写重了），跑出来的数字会被当成"没差异"，
      比跑错更危险；
    * **臂太少** —— 一个臂没有对照物。
    """
    if len(arms) < 2:
        raise AbError(
            f"至少要两个臂才能做对照，现在只有 {len(arms)} 个。"
            "（一个臂能做的只有「跑一遍看看」，那是基线，不是 A/B）"
        )
    labels = [arm.label for arm in arms]
    if len(set(labels)) != len(labels):
        raise AbError(f"臂标签重复了：{labels} —— 结果目录会互相覆盖")

    differing = diff_environments(arms, base)
    unexpected = {k: v for k, v in differing.items() if k not in may_differ}
    if unexpected:
        lines = ["两臂之间有**没有声明过**的配置差异，这不是干净对照："]
        for key, values in unexpected.items():
            rendered = " vs ".join(f"{label}={value!r}" for label, value in values.items())
            lines.append(f"    {key}: {rendered}")
        lines.append("")
        lines.append(
            "要么把它消掉（让两臂只差被测的那一个），"
            "要么用 `--may-differ <变量名>` 显式声明"
            "——「这次对照在比什么」应当是一个被人写下来的决定。"
        )
        raise AbError("\n".join(lines))

    silent = [key for key in may_differ if key not in differing]
    if silent:
        detail = {key: {arm.label: arm.env(base).get(key) for arm in arms} for key in silent}
        lines = ["声明为「应当不同」的变量在两臂上取值相同 —— 这对照没有对照物："]
        for key, values in detail.items():
            rendered = " / ".join(f"{label}={value!r}" for label, value in values.items())
            lines.append(f"    {key}: {rendered}")
        lines.append("")
        lines.append("最常见的原因是镜像名写重了。查一下 `--arm` 的参数。")
        raise AbError("\n".join(lines))

    return differing


# ══════════════════════════════════════════════════════════
# 预检（不涉及 LLM 的硬条件）
# ══════════════════════════════════════════════════════════


def require_files() -> None:
    for path in (MOCK_SCRIPT, BASELINE_SCRIPT):
        if not path.is_file():
            raise AbError(f"找不到 {path} —— 这个脚本要在仓库里跑（scripts/ 同目录）。")


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise AbError(
            "找不到 docker —— 基线要在有 Docker 的机器上跑（沙箱是容器）。"
            "这个脚本刻意不提供「没有容器时的降级跑法」：那测的就不是同一条链路。"
        )


def image_facts(image: str) -> dict[str, str]:
    """镜像在不在、是哪一份。**不存在就抛** —— 否则每轮都会在「起沙箱」处失败。"""
    completed = subprocess.run(  # noqa: S603 - 常量 + 用户给的镜像名，无 shell
        ["docker", "image", "inspect", image, "--format", "{{.Id}}|{{.Created}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AbError(
            f"本地没有镜像 {image}（`docker image inspect` 失败）。\n"
            f"{completed.stderr.strip()}\n"
            "先构建它，或把 --arm 改成实际存在的 tag。"
        )
    image_id, _, created = completed.stdout.strip().partition("|")
    return {"image": image, "id": image_id, "created": created}


def require_llm() -> None:
    """LLM 凭据必须在环境里 —— 不在就明确说要什么，不让它跑到一半才炸。

    为什么要求环境变量而不是让它回落到 providers 表：那条路要连数据库、
    还要 `SUPEX_SECRET_KEY`，而"用哪个模型"恰恰是这类对照里最需要被
    看见、被记下来的变量。放在环境里，`--may-differ` 才管得到它。
    """
    model = (os.environ.get("CTF2_LLM_MODEL") or "").strip()
    key = (os.environ.get("CTF2_LLM_KEY") or "").strip()
    missing = [name for name, value in (("CTF2_LLM_MODEL", model), ("CTF2_LLM_KEY", key)) if not value]
    if missing:
        raise AbError(
            "缺 LLM 凭据：没有设置 " + " / ".join(missing) + "。\n"
            "这类对照要显式指定模型（否则回落到 providers 表，还要 SUPEX_SECRET_KEY，"
            "而且「用哪个模型」就没被记下来了）。例：\n"
            "    export CTF2_LLM_MODEL=...  CTF2_LLM_KEY=...  CTF2_LLM_BASE_URL=..."
        )


def resolve_arena_key(cli_value: str | None) -> str:
    """拿到 mock 的 X-Agent-AccessKey —— **脚本里不写死任何凭据**。

    为什么不留一个默认假值：`ak_test_local` 确实是 mock 自己的默认值、
    不是真凭据，但把它写进实验脚本会养成一个坏习惯 —— 下一个要跑
    A/B 的人会以为"这脚本自带一份能跑的配置"，于是在缺凭据时**默默
    用默认值跑起来**，跑完发现 mock 一直回 `A0231 AccessKey 无效`，
    所有题都"没解出"，然后把这个数字当成结论。

    所以这里把顺序写死：命令行 > 环境变量 > 明确报错退出。
    缺什么就说什么，不猜。
    """
    # `source` 只用于下面那句报错文案里的"从哪来的"，循环体不读它 ——
    # 用 `_` 前缀明说"这里不打算用"，而不是删掉元组里的那一列。
    for _source, value in (
        ("--key", cli_value),
        ("CTF2_ARENA_KEY", os.environ.get("CTF2_ARENA_KEY")),
    ):
        text = (value or "").strip()
        if text:
            return text
    raise AbError(
        "缺 mock 的 X-Agent-AccessKey：既没给 --key，也没有 CTF2_ARENA_KEY。"
        " 这不是真凭据，是 mock 自己的约定值；但它必须由你显式给出，"
        "脚本不自带默认值。例："
        "    export CTF2_ARENA_KEY=<跟 mock 启动时 --key 一致的那个值>"
    )


def git_commit() -> str:
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = completed.stdout.strip()
    return commit or "unknown"


# ══════════════════════════════════════════════════════════
# mock 生命周期
# ══════════════════════════════════════════════════════════


def port_is_open(host: str, port: int, *, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _stale_mock_pids() -> list[int]:
    """扫 `/proc` 找还活着的 mock 进程（Linux）。非 Linux 返回空。

    ★ 不用 `pkill -f`：那个 pattern 会命中**调用它的那个 shell**（命令行里
    带着同样的字符串），在 ssh 里跑时表现为"ssh 自己被杀了，exit 255"。
    这里按 `/proc/<pid>/cmdline` 精确比对脚本路径，不会误伤。
    """
    pids: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return pids
    target = str(MOCK_SCRIPT)
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        if target in argv:
            pids.append(int(entry.name))
    return pids


def stop_process(proc: subprocess.Popen[bytes]) -> None:
    """先 SIGTERM 整组、再补 SIGKILL。mock 会派生 pwn 靶机线程，必须整组收。"""
    if proc.poll() is not None:
        return
    for sig in (15, 9):  # SIGTERM, SIGKILL
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


class MockServer:
    """一轮一个 mock：起 → 跑 → 停。每次都是干净进程，`hasSolved` 归零。"""

    def __init__(self, *, host: str, port: int, key: str, log_path: Path) -> None:
        self.host, self.port, self.key = host, port, key
        self.log_path = log_path
        self.proc: subprocess.Popen[bytes] | None = None
        self._log = None

    def __enter__(self) -> MockServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        if port_is_open(self.host, self.port):
            stale = _stale_mock_pids()
            hint = (
                f"占用者是 mock 进程 PID={stale}，先 `kill {' '.join(map(str, stale))}`"
                if stale
                else "占用者不是本仓库的 mock（自己起的服务？），先停掉它"
            )
            raise AbError(
                f"{self.host}:{self.port} 已经有东西在监听 —— {hint}。\n"
                "不复用已有实例：它的 hasSolved 可能不是干净的，"
                "那会把「已解」测成「没解」。"
            )
        self._log = self.log_path.open("wb")
        self.proc = subprocess.Popen(  # noqa: S603 - 常量 + 本地脚本
            [
                sys.executable,
                str(MOCK_SCRIPT),
                "--host", self.host,
                "--port", str(self.port),
                "--key", self.key,
            ],
            cwd=str(REPO_ROOT),
            stdout=self._log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                tail = self.log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
                raise AbError(f"mock 起不来（退出码 {self.proc.returncode}）：\n{tail}")
            if port_is_open(self.host, self.port):
                return
            time.sleep(0.25)
        raise AbError("mock 60s 内没监听上 —— 看 " + str(self.log_path))

    def stop(self) -> None:
        if self.proc is not None:
            stop_process(self.proc)
            self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def solved_count(self) -> int:
        """热身检查：现在有几道题已经是 `hasSolved=true`。**应为 0**。"""
        import urllib.error
        import urllib.request

        url = f"http://{self.host}:{self.port}/slab-match/api/v1/agent/ctf/exercise-list"
        request = urllib.request.Request(url, headers={"x-agent-accesskey": self.key})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - 本机
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise AbError(f"查热身状态失败（{url}）：{exc}") from exc
        return _count_solved(payload)


def _count_solved(payload: object) -> int:
    """数 `hasSolved: true` 的题。递归走，不假设固定的嵌套层数。"""
    if isinstance(payload, dict):
        return (1 if payload.get("hasSolved") is True else 0) + sum(
            _count_solved(value) for value in payload.values()
        )
    if isinstance(payload, list):
        return sum(_count_solved(item) for item in payload)
    return 0


# ══════════════════════════════════════════════════════════
# 跑一轮
# ══════════════════════════════════════════════════════════


def cleanup_containers() -> None:
    """清掉上一轮留下的沙箱容器。不清会累积到几百个，且端口/名字会撞。"""
    listed = subprocess.run(  # noqa: S603
        ["docker", "ps", "-aq", "--filter", "name=arena-e2e"],
        capture_output=True,
        text=True,
        check=False,
    )
    ids = listed.stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, check=False)  # noqa: S603


def run_round(
    *,
    arm: Arm,
    round_index: int,
    rounds: int,
    base_env: dict[str, str],
    args: argparse.Namespace,
    out_dir: Path,
) -> dict[str, object]:
    round_dir = out_dir / f"r{round_index}"
    round_dir.mkdir(parents=True, exist_ok=True)
    result_path = round_dir / "result.json"

    print(f"======== 臂 {arm.label} 第 {round_index}/{rounds} 轮 "
          f"{time.strftime('%H:%M:%S')} ========", flush=True)

    # 附件目录跨运行残留：旧文件会被整目录只读挂进容器当成本题附件，
    # 模型会去分析题目根本没给的垃圾（踩过，见 arena_e2e.py 的清理逻辑）。
    attachments = REPO_ROOT / "_tmp" / "arena-attachments"
    shutil.rmtree(attachments, ignore_errors=True)
    cleanup_containers()

    env = arm.env(base_env)
    started = time.time()
    with MockServer(
        host=args.base_url.split("//", 1)[-1].split(":")[0],
        port=args.port,
        key=args.key,
        log_path=round_dir / "mock.log",
    ) as mock:
        solved = mock.solved_count()
        print(f"  热身检查：hasSolved=true 的题 {solved} 道（应为 0）", flush=True)
        if solved:
            raise AbError(
                f"mock 状态不干净（已有 {solved} 道 isSolved）—— "
                "这轮会把「已解」测成「没解」，停下来。"
            )

        cmd = [
            sys.executable, "-u", str(BASELINE_SCRIPT), "run",
            "--base-url", args.base_url,
            "--key", args.key,
            "--ids", *[str(i) for i in args.ids],
            "--max-turns", str(args.max_turns),
            "--timeout", str(args.timeout),
            "--out", str(result_path),
        ]
        if args.notes_cards:
            cmd += ["--notes-cards", args.notes_cards]
        print("  $ " + " ".join(cmd[1:]), flush=True)
        completed = subprocess.run(  # noqa: S603 - 常量 + 本地脚本
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        (round_dir / "run.log").write_text(
            completed.stdout + ("\n[stderr]\n" + completed.stderr if completed.stderr else ""),
            encoding="utf-8",
        )
        # 摘要行给等在外面的人看；完整日志在 run.log。
        for line in completed.stdout.splitlines():
            if "→" in line or line.startswith(("结果", "平均")):
                print("  " + line.strip(), flush=True)

    elapsed = time.time() - started
    print(f"======== 臂 {arm.label} 第 {round_index} 轮结束 "
          f"rc={completed.returncode} {elapsed:.0f}s {time.strftime('%H:%M:%S')} ========",
          flush=True)
    return {
        "arm": arm.label,
        "round": round_index,
        "rc": completed.returncode,
        "elapsed_s": round(elapsed, 1),
        "result": str(result_path.relative_to(REPO_ROOT)) if result_path.is_file() else None,
        "log": str((round_dir / "run.log").relative_to(REPO_ROOT)),
    }


# ══════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A/B 对照执行器：一条命令跑完所有臂，开跑前自检两臂只差声明过的变量",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            "  python3 scripts/ab_rounds.py \\\n"
            "      --arm pre=ctf2-sandbox:pre-gems --arm post=ctf2-sandbox:latest \\\n"
            "      --ids 1003 1006 --rounds 5 --out _tmp/ab-p0\n"
        ),
    )
    parser.add_argument(
        "--arm",
        action="append",
        required=True,
        metavar="标签=镜像",
        help="一个实验臂，可重复给多个。只改 CTF2_IMAGE_BASE，其余配置全部共用",
    )
    parser.add_argument("--ids", type=int, nargs="*", default=None, help="只跑这些题（默认全部）")
    parser.add_argument("--rounds", type=int, default=5, help="每臂跑几轮（默认 5）")
    parser.add_argument("--out", default="_tmp/ab", help="结果根目录（每臂每轮一个子目录）")
    parser.add_argument("--max-turns", type=int, default=30, help="单题最大回合（默认 30）")
    parser.add_argument("--timeout", type=float, default=1800.0, help="单题超时秒（默认 1800）")
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8901", help="mock 的地址（默认本机 8901）"
    )
    parser.add_argument("--port", type=int, default=8901, help="mock 监听端口（默认 8901）")
    parser.add_argument(
        "--key",
        default=None,
        help=(
            "mock 的 X-Agent-AccessKey。默认读环境变量 CTF2_ARENA_KEY；"
            "两边都没给就退出（不在脚本里写死一个测试凭据）"
        ),
    )
    parser.add_argument("--notes-cards", default="", help="方法卡 JSON（给两臂都注入，与 A/B 无关）")
    parser.add_argument(
        "--may-differ",
        action="append",
        default=None,
        metavar="变量名",
        help=(
            "显式声明「允许两臂不同」的环境变量，可重复。"
            f"默认 {list(DEFAULT_MAY_DIFFER)}（只差镜像）"
        ),
    )
    parser.add_argument(
        "--check-only", action="store_true", help="只跑预检与自检，不真的跑（改完脚本先跑这个）"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    may_differ = tuple(args.may_differ) if args.may_differ else DEFAULT_MAY_DIFFER
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir

    try:
        arms = [parse_arm(raw) for raw in args.arm]
        require_files()
        require_docker()

        base_env = dict(os.environ)
        base_env.setdefault("CTF2_GIT_COMMIT", git_commit())
        arena_key = resolve_arena_key(args.key)

        print("=" * 72, flush=True)
        print(f"A/B 对照 · {len(arms)} 个臂 × {args.rounds} 轮 "
              f"· 题 {args.ids or '全部'} · commit {base_env['CTF2_GIT_COMMIT']}", flush=True)
        print("=" * 72, flush=True)

        # ── 自检：先证"两臂只差声明过的那些"，再谈跑 ──
        differing = assert_only_expected_differences(arms, base_env, may_differ)
        print("✓ 两臂配置自检通过，差异恰好是：", flush=True)
        for key, values in differing.items():
            rendered = " | ".join(f"{label}={value}" for label, value in values.items())
            print(f"    {key}: {rendered}", flush=True)

        facts = {arm.label: image_facts(arm.overrides["CTF2_IMAGE_BASE"]) for arm in arms}
        for label, fact in facts.items():
            print(f"    {label}: {fact['image']} → {fact['id'][:19]} (created {fact['created']})",
                  flush=True)

        require_llm()
        args.key = arena_key
        print(f"    LLM: {os.environ['CTF2_LLM_MODEL']}"
              f" @ {os.environ.get('CTF2_LLM_BASE_URL') or '(默认)'}", flush=True)

        if args.check_only:
            print("\n--check-only：预检全过，没有真的跑。", flush=True)
            return 0

        out_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, object]] = []
        summary: dict[str, object] = {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "git_commit": base_env["CTF2_GIT_COMMIT"],
            "arms": {arm.label: arm.overrides for arm in arms},
            "images": facts,
            "may_differ": list(may_differ),
            "differing_env": differing,
            "ids": args.ids,
            "rounds": args.rounds,
            "max_turns": args.max_turns,
            "timeout": args.timeout,
            "llm_model": os.environ.get("CTF2_LLM_MODEL"),
            "runs": records,
        }
        try:
            for arm in arms:
                for round_index in range(1, args.rounds + 1):
                    records.append(
                        run_round(
                            arm=arm,
                            round_index=round_index,
                            rounds=args.rounds,
                            base_env=base_env,
                            args=args,
                            out_dir=out_dir / f"arm-{arm.label}",
                        )
                    )
        finally:
            summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            cleanup_containers()
            (out_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"\n汇总写进 {out_dir / 'summary.json'}", flush=True)

    except AbError as exc:
        print(f"\n✗ {exc}", file=sys.stderr, flush=True)
        return 2

    failures = [record for record in records if record["rc"] != 0]
    print(f"全部完成：{len(records)} 轮，{len(failures)} 轮返回非 0。", flush=True)
    for record in failures:
        print(f"    ✗ 臂 {record['arm']} 第 {record['round']} 轮 rc={record['rc']} "
              f"（看 {record['log']}）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
