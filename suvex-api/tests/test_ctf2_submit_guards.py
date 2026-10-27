"""CTF2 提交路径与认领的守卫（任务 21 第二批）。

## 这批修的是"边界上的漏" —— 每一处都有一个共同特征

**注释里已经承认了，但代码没跟上。**

| 位置 | 注释怎么说的 | 代码实际怎么做 |
| --- | --- | --- |
| `confirm_submit` | `runner` 里写着「产品路径本该拦住它…**没有**调 `_assert_resolvable` —— 所以那条防线是漏的」 | 真的没调 |
| `submit_candidate` | 「先写 pending 再发请求」的纪律 | `client_factory()` 抛的异常捕不到，留下假 pending |
| `_claim` | 「排除在跑的」靠内存 `continue` | 跳过的名额不补取，并发静默退化 |
| `release_environment` | 「承诺是绝不抛，捕获面就得与它一致：`Exception`」 | 首行在 `try` 之外 |

所以这个文件的断言**不是**"某个值对不对"，而是"那条纪律有没有真的落实"。
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from app.kernel import runner, scheduler, service


def _code_of(func) -> str:  # noqa: ANN001
    """取函数的代码，**去掉注释与 docstring**。

    ⚠️ 必须去注释：这个项目的注释会**引用**被修掉的写法来解释"改了什么"，
        直接做子串匹配会把注释当成代码，造成假红（上一批测试踩过两次）。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
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


# ══════════════════════════════════════════════════════════
# 一、★ 提交前必须有状态断言
# ══════════════════════════════════════════════════════════


def test_confirm_submit_asserts_the_state() -> None:
    """★★ `confirm_submit` 必须调 `_assert_resolvable`。

    ## 不调的后果：扣掉一次**不可再生**的机会

    每道题的错误提交次数是有限的。对一道**已解出**的题再提交一个错误的
    flag，平台会回 `WRONG`，而 `_advance_challenge` 会实打实地
    `wrong_submission_count += 1`。

    `runner.submit_candidate` 里的兜底只是"不写坏状态"（避免撞 CHECK 约束
    得到 500），它**没有阻止请求发出去** —— 而那段注释自己写着
    「产品路径本该拦住它…**没有**调 `_assert_resolvable` —— 所以那条防线是漏的」。

    ⚠️ 断言的是"调了这个函数"，不是"写了某个字符串"：函数名是唯一的，
        而字符串可能出现在注释或错误消息里。
    """
    source = _code_of(service.confirm_submit)

    assert "_assert_resolvable" in source, (
        "`confirm_submit` 没有做状态断言 —— 可以对一道**已解出**的题重复提交，"
        "而每道题的错误提交次数是不可再生的额度。\n"
        "修法：`_assert_submission_supported` 之后补 `_assert_resolvable(challenge)`。"
    )


def test_confirm_submit_still_asserts_platform_support() -> None:
    """平台能力断言**不能**被状态断言挤掉。

    两者回答不同的问题：前者是「这个接口在平台上存不存在」（与状态无关），
    后者是「现在能不能做」。少任何一个都会让某一类题拿到错误的拒绝理由。
    """
    source = _code_of(service.confirm_submit)

    assert "_assert_submission_supported" in source, (
        "`confirm_submit` 丢了平台能力断言 —— 赛事题会走到一个不存在的端点上，"
        "而用户看到的是「格式不对」之类误导性的拒绝理由。"
    )


# ══════════════════════════════════════════════════════════
# 二、★ 假 `pending`：请求没发出去就不能留欠条
# ══════════════════════════════════════════════════════════


def test_client_factory_is_outside_the_submit_try() -> None:
    """★★ `client_factory()` 必须在发请求的 `try` **之外**。

    ## 为什么

    它在"账号不存在 / 凭据解不开"时抛的是 **`ApiError`**，而 `ApiError`
    与 `KernelError` **没有继承关系** —— `except KernelError` 捕不到它。
    异常穿透时，上面**已经写下了一条 `pending` 提交行**。

    ## 那条假 `pending` 的代价

    `pending` 的语义是「请求已发出、结果未知」—— 一张要人工对账的欠条。
    但事实上请求**根本没发出去**。更贵的是 `already_submitted()` 把 `pending`
    计入「已提交」，于是**同一个 flag 以后永远不会被再次提交** ——
    一道本来能解的题，因为一次凭据抖动而永久失去那个候选。

    ## 怎么断言

    关键是 `client_factory()` **不能和 `submit_flag(...)` 在同一个 `try` 里**
    —— 因为那个 try 的 `except` 只捕 `KernelError`。

    ⚠️ 不能笼统地断言"它不在任何 try 里"：修复后的写法是**给它单独一个
        `try`**（`except Exception`），而那恰恰是正确做法。
        第一版我这么写过，结果把已经修好的代码判成错的（假红）。

    判据：遍历每个 `Try` 节点，**只要某个 try 的 body 里同时出现
    `client_factory` 与 `submit_flag`**，就是没修好。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(runner.submit_candidate)))

    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        segment = "\n".join(ast.unparse(stmt) for stmt in node.body)
        if "client_factory" in segment and "submit_flag" in segment:
            offenders.append(node.lineno)

    assert not offenders, (
        f"`client_factory()` 与 `submit_flag()` 在同一个 try 里（行 {offenders}）—— "
        "那个 try 只捕 `KernelError`，而建客户端失败抛的是 `ApiError`"
        "（两者无继承关系），会留下一条「请求从未发出」的假 `pending`，"
        "那个 flag 以后永不重试。\n"
        "修法：把 `client_factory()` 挪进它自己的 try（`except Exception`）。"
    )


def test_failed_client_build_marks_the_row_failed_not_pending() -> None:
    """建不出客户端时，那条提交行要记成 `failed`，**不能**留 `pending`。

    只有"真的发出去了、结果不知道"才配叫 pending。
    """
    source = _code_of(runner.submit_candidate)

    assert "KernelSubmissionStatus.FAILED.value" in source, (
        "建客户端失败时没有把提交行改成 `failed` —— 留着 `pending` 会让"
        "`already_submitted` 认为这个 flag 交过了，于是永不重试。"
    )


# ══════════════════════════════════════════════════════════
# 三、★ 认领要排除"在跑的"，而且名额要补
# ══════════════════════════════════════════════════════════


def test_claim_excludes_running_challenges_in_sql() -> None:
    """★★ 认领的 SQL 必须排除已在跑的题。

    ## 不排除会怎样：并发**静默退化**

    「在跑」的题状态仍是 `solving`（环境启动期间还没走到 `environment_ready`），
    而排序是 `updated_at ASC`，于是它们**稳定地占据查询结果的前几行**。
    内存里 `continue` 跳过后，**名额不补取**。

    实测推演：上限 2，先跑起 A（正在起环境），再点 B、C —— 每次 `slots=1`，
    取 1 行永远是 A，A 被跳过，B、C 在 A 的环境起来之前一道都不会被认领。
    **实际并发退化成 1，而界面上显示上限 2。**

    ⚠️ 这条测试盯的是"排除发生在 SQL 里"而不是"内存里 continue 了" ——
        后者正是这次要修掉的写法，它看起来也对，只是会浪费名额。
    """
    source = _code_of(scheduler.KernelScheduler._claim)

    assert "notin_" in source, (
        "认领没有在 SQL 里排除已在跑的题 —— 被跳过的名额不会补取，"
        "并发会静默退化（界面上显示上限 3，实际只跑 1）。"
    )


def test_claim_still_has_the_per_tick_safety_valve() -> None:
    """`_MAX_CLAIMS_PER_TICK` 是安全阀，不能因为这次改动被去掉。

    它是"一次最多认领几道"，防止"上限调大之后一次抢 50 个把宿主打爆"。
    """
    source = _code_of(scheduler.KernelScheduler._claim)

    assert "_MAX_CLAIMS_PER_TICK" in source, (
        "认领丢了每轮上限 —— 上限调大时会一次抢一大批，"
        "而那正是那个安全阀要防的（它是安全阀，不是性能参数）。"
    )


# ══════════════════════════════════════════════════════════
# 四、★ 「绝不抛」的承诺要覆盖整个函数体
# ══════════════════════════════════════════════════════════


def test_release_environment_wraps_its_whole_body() -> None:
    """★★ `release_environment` 的整段都要在捕获面之内。

    ## 原来漏在哪

    首行 `_remote_ids_fresh(challenge_id)` 在 `try` 之外，而它内部开了一个
    `session_scope()` —— 数据库抖动时抛的是 SQLAlchemy 异常，
    **穿透**了这个承诺「绝不抛」的函数。

    ## 后果有两层，第二层更贵

    1. 它在 `finally` 里被调用 → 这个异常会**盖住 worker 的原始异常**
       （正是 docstring 第一段要防的形态）；
    2. `_run_worker` 的收尾段（`_record_outcome` + `finalize_solve_scan`）
       **永远不会执行** → 题目与解题 scan 都停在运行态。

    也就是说：**一次数据库抖动会让一次已经跑完的解题丢掉结果。**

    ## 怎么断言

    函数的 `body` 里如果只有 `Try`（加可能的 return），说明整段都被包住了。
    如果 `body` 里还有别的语句（例如一次函数调用），那就是裸在 try 外面。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(service.release_environment)))
    func = tree.body[0]
    assert isinstance(func, ast.AsyncFunctionDef)

    # 去掉 docstring 后看剩下几条语句
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]

    non_try = [ast.unparse(stmt)[:60] for stmt in body if not isinstance(stmt, ast.Try)]
    assert not non_try, (
        f"`release_environment` 里这些语句**在 try 之外**：{non_try}\n"
        "它承诺「收尾绝不抛」，而调用点在 `finally` 里 —— 抛出的异常会盖住"
        "worker 的原始异常，并让 `_record_outcome` 永远不执行（丢掉解题结果）。"
    )


def test_release_environment_catches_broad_exceptions() -> None:
    """捕获面必须是 `Exception`，不能只捕 `KernelError`。

    这条与上一条配套：包住了整段、但只捕 `KernelError` 的话，
    数据库异常照样穿透。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(service.release_environment)))

    broad = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                # `except Exception` 或 `except Exception as exc`
                if isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
                    broad = True
    assert broad, (
        "`release_environment` 没有捕 `Exception` —— 数据库层的异常"
        "（SQLAlchemy）不是 `KernelError`，会穿透这个「绝不抛」的函数。"
    )


# ══════════════════════════════════════════════════════════
# 五、可用性与可观测性
# ══════════════════════════════════════════════════════════


def test_sandbox_gets_a_writable_tmp() -> None:
    """★ 容器是 `--read-only`，所以要给 `/tmp` 一个 tmpfs。

    不给的话 `cmake` / `gcc` / `john` / `hashcat` 这些默认写 `/tmp` 的工具
    会报 `Permission denied` —— 那看起来像**镜像里缺东西**，
    而不是"挂载少给了一个目录"，排查方向会跑偏。

    ## ⚠️ 断言的是**拼出来的 argv**，不是 `_argv` 的源码文本

    原来这条读 `_argv` 的源码做 `"/tmp" in source`。v0.102 的
    `3c0437b` 把 `_argv` 拆成了 `_restricted_argv` / `_unrestricted_argv`
    两个 helper，`/tmp` 的 tmpfs 落在前者里 —— 于是源码文本里不再有
    `/tmp`，这条**假红**了（容器行为一点没变，实测 argv 里两个参数都在）。

    源码文本断言本来就是这个毛病：谁把代码挪个位置就红一次，而红的
    时候你无法区分"行为退化了"和"代码搬家了"。改成调用 `_argv()`
    看**真正会传给 docker 的参数**，搬家不再影响它，而一旦真把
    `--tmpfs /tmp` 删掉，它仍然会红 —— 这才是这条测试想守的东西。
    """
    argv = runner.SandboxContainer(name="tmpfs_probe", image="suvex-sandbox:latest")._argv()

    # `--read-only` 是前提：没有它，容器本来就能写任意路径，这条测试
    # 会变成"什么都没验证"。一起断言，防止将来有人顺手删掉只读根、
    # 而 /tmp 的 tmpfs 变成一句没有意义的残留参数。
    assert "--read-only" in argv, "容器不再是只读根 —— 那 /tmp 的 tmpfs 就没有意义了"

    assert "--tmpfs" in argv, "容器没有挂任何 tmpfs —— `--read-only` 下工具写临时文件会失败"
    tmpfs_values = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs" and i + 1 < len(argv)]
    tmp_entries = [v for v in tmpfs_values if v.split(":")[0] == "/tmp"]
    assert tmp_entries, (
        "容器没有可写的 /tmp —— `--read-only` 下工具写临时文件会失败，"
        f"而报错看起来像镜像缺东西。实挂的 tmpfs：{tmpfs_values}"
    )
    # 可写（`rw`）是这条的全部意义。只写 `/tmp` 而不带 `rw` 时 Docker
    # 的默认行为恰好也是可写，但显式写出来才能防止有人照抄别的项目
    # 改成 `:ro` 还以为是同一个东西。
    #
    # ⚠️ `rw` 是**粘在路径后面**的：`/tmp:rw,exec,size=...`。
    #   所以按 `,` 切开后第一段是 `/tmp:rw` 而不是 `rw` —— 要再把
    #   路径与选项用 `:` 分开才取得到。这里曾经写成 `"rw" in parts`
    #   而直接假红（`parts` 里只有 `/tmp:rw`），别改回去。
    options = tmp_entries[0].split(":", 1)[1].split(",")
    assert "rw" in options, f"/tmp 的 tmpfs 不是可写的：{tmp_entries[0]}"


def test_autosync_records_its_last_error() -> None:
    """★ 自动同步失败时要记下原因，不能只写日志。

    `_last_error` 是给 `stats()` 用的（比赛规则里「查看运行状态」那部分）。
    原来全文件没有任何一处给它赋过错误值 —— 于是自动同步连续失败十轮，
    界面上仍显示一切正常。
    """
    source = _code_of(scheduler.KernelScheduler._claim)  # 占位，真正看的是 autosync
    del source

    from app.kernel import autosync

    tree = ast.parse(textwrap.dedent(inspect.getsource(autosync)))
    assigns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == "_last_error" for t in node.targets
        )
    ]
    # 至少两处：一处置 None（成功时清空），一处赋错误信息
    assert len(assigns) >= 2, (
        f"`_last_error` 只有 {len(assigns)} 处赋值 —— 它恒为 None，"
        "自动同步连续失败也不会在界面上体现。"
    )
