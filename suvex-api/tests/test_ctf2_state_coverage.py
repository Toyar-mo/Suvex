"""CTF2 状态收尾与沙箱配额（任务 21）。

## 这个文件防的是同一类错误：**"别处解决过、这里漏了"**

2026-10-10 的一次只读审查发现四处同型问题，它们的形状完全一样：
某个机制在一个地方建立了，在另一个地方没有同步过去。

| 机制 | 建在哪里 | 漏在哪里 | 后果 |
| --- | --- | --- | --- |
| 沙箱配额下发 | `scans/service.py` 的 `sandbox_cpus=...` | `ctf2/runner.build_spec` | 解题容器**没有任何 CPU/内存上限** |
| 收尾认 `environment_ready` | `recover_orphans` 的状态列表 | `_record_outcome` / `run_worker` | 题目永久卡在"环境已就绪"，而环境已释放 |
| 回收 `submitting` | `_assert_resolvable` 把它算作"在跑" | `recover_orphans` | 重启后留下"点不动"的僵尸题 |

**这类"半修"比完全没修更危险** —— 因为看起来已经处理过了。
所以每一条都配一个断言，而不是靠注释提醒（注释已经被证明挡不住：
`service` 漏挂技能那个坑，`artifact` 的注释警告就在同一个文件里，照样踩了第二次）。
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from app.core.vocab import Ctf2ChallengeStatus
from app.ctf2 import runner, scheduler


def _code_of(func) -> str:  # noqa: ANN001
    """取出一个函数的**代码**，去掉注释与文档字符串。

    ## ★ 为什么必须去掉注释（这是我自己踩的坑）

    第一版这几条测试直接对 `inspect.getsource()` 做子串匹配，
    结果**全部误报** —— 因为它们匹配到了我自己写的注释：

      · 断言"不该出现 `== SOLVING`"，而注释里正在引用它解释"改了什么"；
      · 断言"不该出现 `Ctf2Submission`"，而注释里提到了
        `Ctf2SubmissionStatus.PENDING`。

    也就是说：**注释写得越详细，这种测试越容易假红** ——
    而详细注释恰恰是这个项目的风格。所以必须解析成 AST，
    只看真正的代码节点。

    ⚠️ 用 AST 而不是正则去注释：字符串字面量里也可能有 `#`，
        正则会把它们误伤（比如 `"# 未产出候选"` 这样的消息文本）。
    """
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)

    # 丢掉 docstring（它是 Expr/Constant 节点，不在注释里但同样会误伤）
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]

    return ast.unparse(tree)


def _all_code_text(func) -> str:  # noqa: ANN001
    """函数的代码 + 它调用的同模块函数的代码（一层）。

    有些判据在辅助函数里（例如 `recover_orphans` 的状态列表可能被
    抽成一个常量或小函数）。只看单个函数体会漏掉。
    """
    return _code_of(func)



# ══════════════════════════════════════════════════════════
# 一、★ 沙箱配额：整条链路必须接通
# ══════════════════════════════════════════════════════════


def test_build_spec_passes_the_sandbox_quota() -> None:
    """★★ `build_spec` 必须把配额填进 `SolveSpec`。

    ## 为什么这条测试是"看源码"而不是"跑一遍"

    这个函数的其余输入（provider 凭据、环境记录、附件路径）都要真实数据库，
    而**配额这两个参数不需要** —— 它们是纯配置。所以用一个源码级断言
    把"这四个环节是否接上"钉死，比造一整套 fixture 更直接，也更快。

    ## 它防的是什么

    2026-10-10 之前，这条链路上**四个环节三个是对的**：

        _argv（消费）   ✅ if self.memory_mb: argv += ["--memory", ...]
        _run_worker     ✅ memory_mb=spec.memory_mb
        SolveSpec       ✅ memory_mb: int | None = None
        build_spec      ❌ 没有赋值          ← 断在这里

    于是默认值 `None` 一路传到 `if` 里恒为假，**解题容器从来没有配额**。
    而 `_argv` 的参数表白纸黑字写着这两个挡的是
    「LLM 让它跑 `hashcat` 会吃满机器」—— 防护写了，没接上电。

    ⚠️ 用源码断言而不是"导入后检查默认值"：默认值本来就是 `None`，
        查它查不出任何东西 —— 必须确认**赋值语句存在**。
    """
    source = _code_of(runner.build_spec)

    assert "cpus=" in source, (
        "`build_spec` 没有把 cpus 填进 SolveSpec —— "
        "解题容器将没有 CPU 上限（hashcat 能占满所有核）。\n"
        "修法：照 `scans/service.py` 读 `settings_service.read_sandbox_config(db)`。"
    )
    assert "memory_mb=" in source, (
        "`build_spec` 没有把 memory_mb 填进 SolveSpec —— "
        "解题容器将没有内存上限（一条命令就能把宿主机 OOM）。"
    )
    assert "read_sandbox_config" in source, (
        "配额不是从 `read_sandbox_config` 读的 —— 那么设置页改了配额对 CTF2 无效，"
        "而用户会以为改的是全局配置。"
    )


def test_quota_actually_reaches_the_docker_argv() -> None:
    """★ 配额真的会变成 docker 参数（消费端）。

    这条与上一条是一对：上一条保证**值送到了**，这一条保证**送到的值有用**。
    只测一边的话，"字段存在但 `_argv` 忘了拼"这种漏法照样能过。

    ⚠️ 断言里**不要写引号**：`ast.unparse()` 会把所有字符串统一成单引号，
        而源码里写的是双引号 —— 带上引号就会假红（我第一版就是这么错的）。
        匹配裸的参数名即可，`--memory` 这个 token 只可能来自那条 docker 参数。
    """
    spec_source = _code_of(runner.SandboxContainer._argv)

    assert "--memory" in spec_source, "`_argv` 不再拼 --memory —— 配额到不了容器"
    assert "--cpus" in spec_source, "`_argv` 不再拼 --cpus"


def test_spec_defaults_are_none_meaning_no_limit() -> None:
    """`SolveSpec` 的配额默认值是 `None`（= 不下发参数 = Docker 默认不限额）。

    ⚠️ 这条**不是**在测"默认值是 None 所以没问题" —— 恰恰相反：
    它记录的是"为什么这个 bug 能藏这么久"。默认值 `None` 让整条链路
    在**不赋值时也完全合法**，没有任何类型错误、没有任何警告。
    所以上一条源码断言才是必要的。
    """
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(runner.SolveSpec)}
    assert "cpus" in fields and "memory_mb" in fields
    assert fields["cpus"].default is None
    assert fields["memory_mb"].default is None


# ══════════════════════════════════════════════════════════
# 二、★ 收尾必须认 `environment_ready`
# ══════════════════════════════════════════════════════════


def test_record_outcome_covers_environment_ready() -> None:
    """★★ 收尾判据不能只认 `solving`。

    ## 背景：`environment_ready` 是**主状态**

    `scheduler.recover_orphans` 的 docstring 早就写明了这件事
    （"它是解题过程的主状态，不是瞬间中间态"），但同一份认知
    没有同步到 `_record_outcome`。

    后果是一条**很常见**的路径：模型照 `_final_stretch_note` 的要求
    把 candidates 写成空数组（"没拿到就写空，不要编"），于是
    `outcome.error` 保持 `None`（没有候选 ≠ 出错），两个分支都不进 ——
    题目**永久留在 `environment_ready`**，而 `finally` 里的
    `release_environment` 已经把环境停掉、`access_url` 清空。
    数据库说"环境已就绪"，实际环境早没了。

    ## 为什么断言的是"用排除法"而不是"包含某个值"

    用 `not in (...)` 的排除法时，**将来加新状态默认算作需要收尾**；
    用 `== SOLVING` 的穷举法时，**将来加新状态默认漏掉** ——
    而后者正是这次 bug 的成因。所以这条测试盯的是"写法方向"，
    不只是"当前值对不对"。
    """
    source = _code_of(runner._record_outcome)

    assert "== Ctf2ChallengeStatus.SOLVING.value" not in source, (
        "`_record_outcome` 又用 `== SOLVING` 穷举了 —— worker 起来之后"
        "主状态是 `environment_ready`，那个分支几乎永远不命中，"
        "题目会永久卡在'环境已就绪'。\n"
        "修法：改成排除法 `not in (CANDIDATE_READY, SOLVED, FAILED, SUBMITTING)`。"
    )
    assert "not in (" in source, "收尾判据应当是排除法（见 docstring）"


def test_worker_crash_fallback_covers_environment_ready() -> None:
    """★ worker 崩溃兜底同样要认 `environment_ready`。

    这是同一个洞的另一半：`_run_worker` 抛异常时，兜底只认
    `SOLVING` / `SUBMITTING`，于是崩在解题过程中间的题会永久停在
    "环境已就绪"。
    """
    source = _code_of(runner.run_worker)

    assert "ENVIRONMENT_READY.value" in source, (
        "`run_worker` 的崩溃兜底漏了 `environment_ready` —— "
        "worker 崩在解题过程中间时，题目会永久停在'环境已就绪'。"
    )


# ══════════════════════════════════════════════════════════
# 三、★ 孤儿回收必须覆盖 `submitting`
# ══════════════════════════════════════════════════════════


def test_recover_orphans_covers_submitting() -> None:
    """★★ 重启回收要认 `submitting`，否则留下"点不动"的僵尸题。

    `submit_candidate` 在**发出请求之前**就把状态写成 `submitting`
    （刻意的：界面要能显示"正在提交"）。进程若死在
    "状态已写、响应未回"之间，这道题重启后：

      · `recover_orphans` 不回收它；
      · `_assert_resolvable` 对 `submitting` 抛 409「正在处理中」
        —— 而**没有任何东西在处理**；
      · 用户唯一的出路是手动点「取消」。

    这正是 `recover_orphans` docstring 里描述过的那种僵尸
    （"没有错误、没有在跑、也点不动的题"），只是换了个状态值。
    """
    source = _code_of(scheduler.Ctf2Scheduler.recover_orphans)

    assert "SUBMITTING.value" in source, (
        "`recover_orphans` 没有回收 `submitting` —— 服务重启若落在"
        "一次 flag 提交的往返窗口内，那道题会变成点不动的僵尸。"
    )
    # 已有的两个不能被顺手删掉
    assert "SOLVING.value" in source
    assert "ENVIRONMENT_READY.value" in source


def test_recover_orphans_keeps_the_pending_submission_row() -> None:
    """★ 回收 `submitting` 时**不能**删掉那条 `pending` 提交记录。

    它的语义是"请求已发出、结果未知"（见
    `vocab.Ctf2SubmissionStatus.PENDING` 的注释）—— 那是**一张需要
    人工对账的欠条**。删掉它等于把"我们可能已经提交过这个 flag"
    这件事抹掉，而平台那边可能已经记账。
    """
    source = _code_of(scheduler.Ctf2Scheduler.recover_orphans)

    assert "Ctf2Submission" not in source, (
        "`recover_orphans` 动了提交记录 —— `pending` 那张欠条必须留着，"
        "它记录的是'可能已经提交过'，删了就再也对不上账。"
    )


# ══════════════════════════════════════════════════════════
# 四、状态集合的完整性（防止将来又漏）
# ══════════════════════════════════════════════════════════


def test_every_non_terminal_status_is_recoverable() -> None:
    """★★ 每一个"非终态"的题目状态都必须能被重启回收。

    ## 这条是这次修复的**推广形式**

    前三条测试各自盯一个具体状态。而它们背后的规律是：
    **凡是"表示有东西正在跑"的状态，重启后都必须能被回收** ——
    因为重启意味着那些"东西"已经不存在了。

    判据：`Ctf2ChallengeStatus` 里除终态外的每一个取值，
    都要出现在 `recover_orphans` 的状态列表里。

    这条测试的价值在于**面向未来**：将来加一个新状态时，
    它会强制作者想清楚"这个状态重启后要不要回收"。
    如果答案是"不要"（比如它本来就是终态），那就该加进下面的
    `_TERMINAL`；如果是"要"，就必须加进回收列表。
    """
    # 终态：重启后**不该**被重置 —— 它们是已经定下来的结论。
    # 加新状态时，如果它不属于这里，就会被下面那条断言逼着加进回收列表。
    terminal = {
        Ctf2ChallengeStatus.SOLVED.value,
        Ctf2ChallengeStatus.CANDIDATE_READY.value,
        Ctf2ChallengeStatus.FAILED.value,
        Ctf2ChallengeStatus.UNSOLVED.value,
    }

    source = _code_of(scheduler.Ctf2Scheduler.recover_orphans)
    all_statuses = {member.value for member in Ctf2ChallengeStatus}
    non_terminal = all_statuses - terminal

    # ⚠️ 按**枚举成员名**匹配，不按小写的值匹配。
    #
    # 代码里写的是 `Ctf2ChallengeStatus.SOLVING.value`，而 AST 还原出来
    # 也是这个形状 —— 小写的 `solving` **根本不出现在源码里**。
    # 第一版我按小写值匹配，于是三条明明已经回收的状态全被判成"缺失"
    # （假红），而假红的代价是让人去改本来正确的代码。
    #
    # 顺带：这个写法还顺带挡住了"值写错成字符串字面量"的情况 ——
    # 那种写法绕过了枚举，改枚举时不会被编译器发现。
    missing = [
        value
        for value in sorted(non_terminal)
        if f"Ctf2ChallengeStatus.{Ctf2ChallengeStatus(value).name}.value" not in source
    ]
    assert not missing, (
        f"这些**非终态**没有被 `recover_orphans` 回收：{missing}\n"
        "重启意味着'正在跑的东西'已经不存在了，所以每一个非终态都必须被回收 —— "
        "否则那道题会永久卡在那个状态，而用户点不动它。\n"
        "（如果某个状态确实不该回收，把它加进这条测试的 `terminal` 集合并说明理由。）"
    )
