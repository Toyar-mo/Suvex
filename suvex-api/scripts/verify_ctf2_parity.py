"""CTF2 的前后端对账。

## 为什么需要它（以及为什么它比报告那份**更必要**）

报告那份对账（`verify_report_parity.py`）防的是"预览和下载不一致"，
而那是一个**看得见**的问题 —— 客户会发现。

CTF2 的状态枚举漂移**看不见**，因为它失败的方式是：

* 后端新增一个状态 → 前端 `CHALLENGE_STATUS_LABEL[status]` 得到
  `undefined`，界面上那个徽标**显示空白**（不报错、不崩）；
* 后端改一个取值 → 前端的状态筛选 chip 筛出来永远是空的，
  而用户会以为"这个状态下没有题"。

两者都不会有任何报错。本项目在 `FINDING_STATUS_LABEL` 上真栽过一次
（后端把状态从 4 个扩到 6 个，前端标签表没跟上），所以这条规矩有出处。

## 比什么

| 检查 | 为什么 |
| --- | --- |
| 五个枚举的**取值集合** | 见上。这是核心 |
| 前端调用的端点 ⊆ 后端声明的端点 | 一条 404 会被报成"平台接口变了"，排查方向全错 |
| flag 正则**逐字**一致 | 前端比后端严 → 真 flag 被自己挡住，且用户无补救路径 |
| 沙箱镜像名 / Dockerfile 目录 | 镜像名同时出现在四处，对不上就是运行时才炸 |
| 出参接口的**字段集合** | 后端加了字段而前端类型没跟上 → 那个值在界面上永远是 `undefined`（不报错） |

## 不比什么

**不比标签文案**。标签的权威在**前端**（后端只下发 status 字符串）——
与 `SCAN_STATUS_LABEL` 同一条分工。文案改了不该让对账红，
否则这条脚本会因为"改了个措辞"被绕过去。

## 它证明什么、不证明什么（如实说）

它证明「前端源码里的取值集合与后端 vocab 一致」。
它**不**证明「运行时行为一致」—— 那要靠 `tests/kernel/` 里的测试。

用法：

    python scripts/verify_ctf2_parity.py
"""

from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# 前端仓库是后端的兄弟目录（`../suvex-web`）。
FRONTEND = PROJECT_ROOT.parent / "suvex-web" / "src"

for stream in (sys.stdout, sys.stderr):
    # 中文输出在 Windows 控制台里默认会是乱码（cp936 编不出来就抛），
    # 而"对账脚本自己先炸了"比它对账失败更难查。
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

from app.core import vocab  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, title: str, detail: str = "") -> bool:
    RESULTS.append((ok, title, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {title}"
    if detail:
        line += f"\n         {detail}"
    print(line, flush=True)
    return ok


def read(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def parse_ts_record_keys(source: str, name: str) -> set[str]:
    """抠出一个 `Record<K, string> = { key: '...', ... }` 的**键集合**。

    ## ★ 只取键，不取值

    这是本脚本与报告那份对账最要紧的区别。报告那份比的是
    「键 → 文案」，因为报告是**交付物**，一句话的措辞是它的内容。

    而 CTF2 的状态标签是**界面文案**，改它不该让对账变红 ——
    否则某天有人改了个措辞、对账红了，他的第一反应是"把脚本改掉"
    而不是"检查语义"，于是这条脚本就废了。

    用正则而不是引 TS 解析器：这些表都是**扁平字面量**，
    而引一个 parser 只为了读 5 个表不值得。代价是它读不懂嵌套的表 ——
    真遇到时会返回空集合，而下面的检查会**当场发现**（不会静默通过）。
    """
    match = re.search(rf"{name}\s*(?::[^=]+)?=\s*\{{(.*?)\n\}}", source, re.S)
    if not match:
        return set()
    body = match.group(1)
    # 顶层的 `key:` —— 只认缩进两格的，避免把嵌套对象的键也收进来。
    return set(re.findall(r"^\s{2}'?([\w.-]+)'?\s*:", body, re.M))


def parse_ts_interface_fields(source: str, name: str) -> set[str]:
    """抠出一个 `export interface X { ... }` 的**字段名**集合。

    ⚠️ 只认缩进两格的 `字段:` —— 那正好是顶层字段的写法。
    docblock 里的行以 `   * ` 开头，不会被误收（与
    `parse_ts_record_keys` 同一个套路、同一条理由）。
    """
    match = re.search(rf"export interface {name}\s*\{{(.*?)\n\}}", source, re.S)
    if not match:
        return set()
    body = match.group(1)
    return set(re.findall(r"^\s{2}(\w+)\??\s*:", body, re.M))


def strip_comments(source: str) -> str:
    """去掉注释与字符串之外的东西，避免把**文档里的示例路径**当成调用。

    ⚠️ 这条是实测逼出来的：`ctf2.ts` 的文件头注释里写了一句
    "这里只写 `/ctf2/...`，不出现任何后端主机名或端口" ——
    而第一版脚本把那个 `/ctf2/...` 抠了出来，于是报
    "前端调用的端点后端没有：['/ctf2/...']"。
    那是**假警报**，而假警报的代价是下次真警报没人看。
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^[ \t]*//.*$", "", source, flags=re.M)


def normalize_path(path: str) -> str:
    """把路径参数归一成 `{}`，让两端的写法能对上。

    | 谁 | 写法 |
    | --- | --- |
    | 后端 router | `/ctf2/challenges/{challengeId}/solve` |
    | 前端调用 | `/ctf2/challenges/${encodeURIComponent(id)}/solve` |

    不归一的话它们永远不相等 —— 而"永远不相等"会被读成
    "前端调了一个不存在的端点"，方向完全错。

    ⚠️ 顺序要紧：先处理 `${...}`（它含花括号），再处理裸 `{...}`。
    反过来的话 `${id}` 会先被 `\\{[^}]*\\}` 吃掉成 `${}`，留下一个 `$`。
    """
    cleaned = re.sub(r"\$\{[^}]*\}", "{}", path)
    cleaned = re.sub(r"\{[^}]*\}", "{}", cleaned)
    cleaned = cleaned.split("?", 1)[0]
    return cleaned.rstrip("/") or "/"


def endpoint_paths(source: str) -> set[str]:
    """从 TS 里抠出**调用的** CTF2 端点路径（已归一化）。

    只认 `/ctf2/...` 字面量。这样不必理解 `api.get(...)` 的调用形状 ——
    而那个形状会变（有人包一层就失效）。

    ⚠️ 允许出现 `$` 与 `{}`：模板串里的路径参数正是那个形状，
    而把它们排除掉会让"前端确实调用了 solve"这件事看不见
    （提取到的是 `/ctf2/challenges/` 这种半截路径）。
    """
    found = set()
    for raw in re.findall(r"['\"`]/(ctf2/[^'\"`\n]*)", source):
        found.add(normalize_path("/" + raw))
    return found


def backend_endpoints() -> set[str]:
    """后端声明了哪些 CTF2 端点（从 OpenAPI 取，**不手抄**，已归一化）。

    ⚠️ 从 OpenAPI 取而不是从 router 源码正则：源码里路径可能拆成
    几段拼（`prefix + "/accounts"`），正则会漏。而 OpenAPI 是
    **运行时的真实结果** —— 它漏了就说明路由没挂上，而那本身就该报出来。
    """
    from app.main import create_app

    found = set()
    for path in create_app().openapi()["paths"]:
        if path.startswith("/api/v1/ctf2"):
            # 去掉 `/api/v1` 前缀，与前端那份对齐
            found.add(normalize_path(path[len("/api/v1") :]))
    return found


def main() -> int:
    print("CTF2 前后端对账", flush=True)
    print(f"  后端：{PROJECT_ROOT}", flush=True)
    print(f"  前端：{FRONTEND}", flush=True)

    if not FRONTEND.is_dir():
        print("\n⚠️ 找不到前端源码目录，无法对账。", flush=True)
        return 1

    ctf2_ts = read(FRONTEND / "data" / "ctf2.ts")
    if not ctf2_ts:
        print("\n⚠️ 读不到 src/data/ctf2.ts，无法对账。", flush=True)
        return 1
    # 抠端点路径之前先剥注释 —— 见 `strip_comments` 的说明（假警报）。
    code_only = strip_comments(ctf2_ts)

    # ══════════════════════════════════════════════════════
    # ① 枚举取值集合
    # ══════════════════════════════════════════════════════
    print("\n── 枚举取值集合（核心）──", flush=True)

    enum_pairs = [
        ("KernelChallengeStatus", "CHALLENGE_STATUS_LABEL", "题目状态"),
        ("KernelSubmissionStatus", "SUBMISSION_STATUS_LABEL", "提交结果"),
        ("KernelEnvironmentStatus", "ENVIRONMENT_STATUS_LABEL", "环境状态"),
        ("KernelSubmitMode", "SUBMIT_MODE_LABEL", "提交策略"),
        ("KernelSandboxProfile", "SANDBOX_PROFILE_LABEL", "沙箱镜像"),
    ]

    for enum_name, ts_table, label in enum_pairs:
        enum_cls = getattr(vocab, enum_name, None)
        if enum_cls is None:
            check(False, f"{label}：后端没有 {enum_name}", "vocab 里找不到这个枚举")
            continue
        backend_values = {member.value for member in enum_cls}
        frontend_values = parse_ts_record_keys(ctf2_ts, ts_table)
        missing = backend_values - frontend_values
        extra = frontend_values - backend_values
        check(
            not missing and not extra,
            f"{label}取值一致（{enum_name} ↔ {ts_table}）",
            (
                f"前端缺：{sorted(missing)}；前端多：{sorted(extra)}"
                if (missing or extra)
                else f"{len(backend_values)} 个取值"
            ),
        )

    # ══════════════════════════════════════════════════════
    # ② 端点
    # ══════════════════════════════════════════════════════
    print("\n── 端点 ──", flush=True)

    declared = backend_endpoints()
    used = endpoint_paths(code_only)

    check(
        bool(declared),
        "后端声明了 CTF2 端点",
        f"找到 {len(declared)} 条" if declared else "一条都没有 —— 路由没挂上？",
    )
    check(
        bool(used),
        "前端调用了 CTF2 端点",
        f"找到 {len(used)} 条" if used else "一条都没有 —— ctf2.ts 里没有 /ctf2 路径？",
    )

    # ★ 前端用到的**必须**都存在。这是硬失败：不存在的端点会 404，
    #   而 404 被 service 翻译成"平台接口可能变更了"—— 排查方向全错。
    undeclared = used - declared
    check(
        not undeclared,
        "前端调用的端点后端都有",
        f"后端没有这些：{sorted(undeclared)}" if undeclared else f"{len(used)} 条全部匹配",
    )

    # ⚠️ 反过来只**提示**，不算失败：后端有些端点可能只给运维/脚本用
    #    （比如 `/queue` 是排障看的）。把它当失败会逼着前端写一个
    #    没人用的调用，那是为了过检查而写代码。
    unused = declared - used
    if unused:
        print(f"  [INFO] 后端声明但前端未调用：{sorted(unused)}", flush=True)

    # ══════════════════════════════════════════════════════
    # ③ 答案格式
    # ══════════════════════════════════════════════════════
    print("\n── 答案格式 ──", flush=True)

    from app.kernel import runner as ctf2_runner

    backend_pattern = ctf2_runner.FLAG_PATTERN.pattern
    match = re.search(r"FLAG_PATTERN\s*=\s*/(.+)/\s*$", ctf2_ts, re.M)
    frontend_pattern = match.group(1) if match else ""

    check(
        bool(frontend_pattern),
        "前端声明了 FLAG_PATTERN",
        "没找到 —— ctf2.ts 里的即时校验会失效" if not frontend_pattern else "",
    )
    # ★ 逐字比。**前端不能比后端严**：被前端拦下的真答案
    #   用户完全没有补救路径（连试都没试），而反向（前端更松）
    #   只是多一次往返。
    check(
        frontend_pattern == backend_pattern,
        "flag 正则逐字一致",
        f"后端={backend_pattern!r}\n         前端={frontend_pattern!r}",
    )

    # ★★ v0.21 新增：答案长度上限是**跨语言常量**，同样会漂移。
    #
    # 漂移的后果与正则不同、但一样实际：前端上限**小于**后端时，
    # 用户在弹窗里能粘贴的答案会被前端拦下（可补救：改短即可）；
    # 前端上限**大于**后端时，用户以为没问题、提交到后端才被拒
    # （多一次往返，且提示出现在更远的地方）。
    # 两者都不该发生，所以照着正则那条的做法逐字比。
    backend_max = ctf2_runner.MAX_ANSWER_LENGTH
    match = re.search(r"MAX_ANSWER_LENGTH\s*=\s*(\d+)", ctf2_ts)
    frontend_max = int(match.group(1)) if match else None
    check(
        frontend_max == backend_max,
        f"答案长度上限一致（{backend_max}）",
        f"后端={backend_max}\n         前端={frontend_max}",
    )

    # ★★ v0.24 新增：优先级的**分档边界**同样是跨语言常量。
    #
    # 它比前两个更容易漂：这里有三个数，而且它们之间还有一条约束
    # （人工档起点必须 > 自动档封顶）。漂移的症状是"点『提前』跳到了
    # 2000，而徽标还显示『自动』"，或者"自动题压过了手工插队的题" ——
    # 两者看起来都像 UI 的 bug，实际是常量没跟着改。
    for const_name, backend_value in (
        ("AUTO_PRIORITY_BASE", vocab.CTF2_AUTO_PRIORITY_BASE),
        ("AUTO_PRIORITY_MAX", vocab.CTF2_AUTO_PRIORITY_MAX),
        ("PRIORITY_MANUAL_BASE", vocab.CTF2_PRIORITY_MANUAL_BASE),
    ):
        match = re.search(rf"\b{const_name}\s*=\s*(\d+)", ctf2_ts)
        frontend_value = int(match.group(1)) if match else None
        check(
            frontend_value == backend_value,
            f"优先级分档常量一致（{const_name}）",
            f"后端={backend_value} 前端={frontend_value}",
        )

    # 分档之间那条**约束**本身也要钉住：人工档起点必须大于自动档封顶。
    # 少了它，"人永远在环里"会静默失效 —— 自动题里分值最高的那道会压过
    # 用户手工插队的题，而赛事方明确允许"调整解题优先级"。
    check(
        vocab.CTF2_PRIORITY_MANUAL_BASE > vocab.CTF2_AUTO_PRIORITY_MAX,
        "人工插队档高于自动加成档（人永远能插到最前）",
        f"人工={vocab.CTF2_PRIORITY_MANUAL_BASE} > 自动封顶={vocab.CTF2_AUTO_PRIORITY_MAX}",
    )

    # ══════════════════════════════════════════════════════
    # ④ 沙箱镜像
    # ══════════════════════════════════════════════════════
    print("\n── 沙箱镜像 ──", flush=True)

    profile_values = {member.value for member in vocab.KernelSandboxProfile}
    check(
        profile_values == {"ctf2-sandbox", "ctf2-sandbox-re"},
        "镜像名与 Dockerfile 目录一致",
        f"vocab 里的取值：{sorted(profile_values)}",
    )
    # 镜像名同时是 vocab 的取值与前端标签表的键 —— 上面 ① 已经比过一次。
    # 这里再与 Dockerfile 的目录名比一次：它们必须能被一一找到，
    # 否则 `docker run` 会在运行时才报 "Unable to find image"。
    for name in sorted(profile_values):
        dockerfile = PROJECT_ROOT / "docker" / name / "Dockerfile"
        check(
            dockerfile.is_file(),
            f"docker/{name}/Dockerfile 存在",
            "" if dockerfile.is_file() else str(dockerfile),
        )

    # ══════════════════════════════════════════════════════
    # ⑤ 解题提示词
    # ══════════════════════════════════════════════════════
    print("\n── 解题提示词 ──", flush=True)

    prompt = PROJECT_ROOT / "prompts" / "kernel_system.md"
    text = read(prompt)
    check(prompt.is_file(), "prompts/kernel_system.md 存在", "" if text else str(prompt))
    if text:
        for keyword in ("ACTION:", "FINAL:", "不越界", "不编造"):
            check(keyword in text, f"提示词含纪律条款：{keyword}")

    # ══════════════════════════════════════════════════════
    # ⑥ 接口字段（含"候选 flag 不能又被合回一个字段"）
    # ══════════════════════════════════════════════════════
    print("\n── 接口字段 ──", flush=True)

    from app.kernel.schemas import KernelChallengeOut, KernelSettingsIn, KernelSettingsOut, KernelSyncOut

    field_pairs = [
        (KernelChallengeOut, "KernelChallenge", "题目出参"),
        (KernelSyncOut, "KernelSyncResult", "同步出参"),
        # ★ v0.24 新增：**配置出参**也要对账。
        #
        # 它是一处真实存在过的漏同步点：加一个配置项要同时动三个地方
        # （后端 schema、前端 interface、`saveKernelSettings` 的入参形状），
        # 而漏掉第二处的症状是"这一项在界面上根本不存在"，漏掉第三处的
        # 症状是"改了这一项，保存后没反应"—— 后者看起来像保存按钮坏了。
        (KernelSettingsOut, "KernelSettings", "配置出参"),
    ]
    for model, ts_name, label in field_pairs:
        backend_fields = set(model.model_fields)
        frontend_fields = parse_ts_interface_fields(ctf2_ts, ts_name)
        missing = backend_fields - frontend_fields
        extra = frontend_fields - backend_fields
        check(
            not missing and not extra and bool(frontend_fields),
            f"{label}字段一致（{model.__name__} ↔ {ts_name}）",
            (
                f"前端缺：{sorted(missing)}；前端多：{sorted(extra)}"
                if (missing or extra or not frontend_fields)
                else f"{len(backend_fields)} 个字段"
            ),
        )

    # ★ 配置的**入参与出参必须是同一组字段**（只差 `updatedAt`）。
    #
    # 为什么这值得单独一条：`KernelSettingsIn` 的字段**全部必填**
    # （漏传一项是调用方 bug，给默认值会让它静默改回默认）。所以后端
    # 加了字段而前端没跟着传时，PUT 会 422 —— 而那个报错说的是"字段
    # 缺失"，与用户真正遇到的现象（"我改的那一项没生效"）毫不相干。
    out_fields = set(KernelSettingsOut.model_fields)
    in_fields = set(KernelSettingsIn.model_fields)
    check(
        in_fields == out_fields - {"updatedAt"},
        "配置入参与出参是同一组字段（只差 updatedAt）",
        (
            f"只入参：{sorted(in_fields - out_fields)}；"
            f"只出参：{sorted(out_fields - in_fields - {'updatedAt'})}"
        ),
    )

    # ★ 单独把这一条钉住：**候选与已确认必须是两个字段**。
    #   合并回一个 `flag` 之后，界面无法回答"现在有没有东西等着我确认"，
    #   而在一道判错退回未解出的题上会显示一个看起来像答案的字符串
    #   （见 `KernelChallenge` 里那两列的说明）。这是一次真实修过的缺陷，
    #   所以它值得一条专门的检查，而不是只靠上面的集合比较顺带覆盖 ——
    #   集合比较会在"两边同时改回一个 flag"时通过。
    challenge_fields = set(KernelChallengeOut.model_fields)
    check(
        {"candidateFlag", "confirmedFlag"} <= challenge_fields and "flag" not in challenge_fields,
        "候选 flag 与已确认 flag 仍然是两个字段",
        f"当前字段：{sorted(f for f in challenge_fields if 'lag' in f)}",
    )

    # ══════════════════════════════════════════════════════
    # 摘要
    # ══════════════════════════════════════════════════════
    print("\n── 摘要 ──", flush=True)
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = [title for ok, title, _ in RESULTS if not ok]
    print(f"\n{'=' * 60}", flush=True)
    print(f"合计 {len(RESULTS)} 项：通过 {passed}，失败 {len(failed)}", flush=True)
    for title in failed:
        print(f"  ✗ {title}", flush=True)

    if not failed:
        print(
            "\n⚠️ 这个脚本证明的是「前端的取值集合与后端 vocab 一致」。\n"
            "   它**不**证明「运行时行为一致」—— 那要靠 tests/kernel/ 里的测试。\n"
            "   也**不**比标签文案：标签的权威在前端，改措辞不该让对账红。",
            flush=True,
        )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
