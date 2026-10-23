"""`ctf2` 领域路由。**只做薄封装**（业务在 `service.py`）。

## 端点与鉴权级别

| 方法 | 路径 | scope | 为什么是这个级别 |
| --- | --- | --- | --- |
| GET | `/ctf2/accounts` | read | 看"绑了哪个号"不含凭据（只有掩码） |
| POST | `/ctf2/accounts` | **admin** | 写凭据。与 provider 同级 |
| DELETE | `/ctf2/accounts/{id}` | **admin** | 销毁凭据 |
| GET | `/ctf2/settings` | **admin** | 它是**安全策略**（见下） |
| PUT | `/ctf2/settings` | **admin** | 决定"机器能否自动对外提交 flag" |
| GET | `/ctf2/competitions` | read | 只读列表 |
| GET | `/ctf2/challenges` | read | 只读列表 |
| GET | `/ctf2/challenges/{id}` | read | 只读详情 |
| GET | `/ctf2/snapshot` | read | 一次拿三层，全是只读 |
| GET | `/ctf2/queue` | read | 与 `/scans/queue` 同级 |
| POST | `/ctf2/sync` | **scan** | 改数据（写库），且是一串外部请求 |
| POST | `/ctf2/challenges/{id}/solve` | **scan** | 起环境 + 调度解题 = 对外发包 |
| POST | `/ctf2/challenges/{id}/submit` | **scan** | **对外提交 flag，消耗不可再生额度** |
| POST | `/ctf2/challenges/{id}/cancel` | **scan** | 改状态 |
| DELETE | `/ctf2/challenges/{id}/environment` | **scan** | 释放平台资源 |
| GET | `/ctf2/challenges/{id}/writeup` | read | 题解（v0.36）。只读拼装好的 markdown |
| GET | `/ctf2/challenges/{id}/usage` | read | 该题的**逐次** token 明细（v0.36）|
| GET | `/ctf2/usage` | read | 全局 token 统计（v0.36）|

### 三条「为什么」

**① 绑定账号是 admin，不是 scan。**
它写的是**凭据**。契约 §1.2 的理由在这里同样成立：一个能发起扫描的
机器令牌不该顺带能替换平台的 PAT —— 那等于让它能拿别人的账号去提交。
按 `providers` 的同级处理。

**② 切换 safe/auto 是 admin，而不是"用户偏好"。**
`auto` 意味着**机器会在无人确认的情况下对外提交 flag**，而每次
错误提交都消耗一个不可再生的额度。这不是界面偏好，是安全策略 ——
改它的人必须是有权改配置的人。这也是为什么它落在 `/ctf2/settings`
而不是某个"个人设置"上。

**③ `submit` 是 scan 而不是 read。**
它**对外发出一个不可撤销的动作**，而且有配额后果。`read` 的定义是
只读 —— 一个只读令牌能替用户扣掉答题机会，显然不成立。

## ⚠️ 这些端点**不能改成 `async def`**

见 `service.py` 模块 docstring：service 用的是"同步外观 + `asyncio.run`
内核"，而 `asyncio.run` 在正在运行的 event loop 里会直接抛
`RuntimeError`。改成 async 会同时打挂每一个 CTF2 端点。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import Identity, require_admin, require_read, require_scan
from app.core.db import get_db
from app.core.errors import ApiError, ErrorCode
from app.core.vocab import AuditAction
from app.kernel import platforms, service
from app.kernel import workorder as workorder_mod
from app.kernel import writeup as writeup_mod
from app.kernel.models import KernelChallenge, KernelCompetition, KernelEnvironment
from app.kernel.schemas import (
    IrTaskIn,
    IrTaskOut,
    KernelAccountBindIn,
    KernelAccountOut,
    KernelChallengeOut,
    KernelCompetitionOut,
    KernelEnvironmentOut,
    KernelPlatformActivateIn,
    KernelPlatformDraftIn,
    KernelPlatformOut,
    KernelPrioritiesByScoreIn,
    KernelPriorityBatchOut,
    KernelPriorityIn,
    KernelPullIn,
    KernelPullOut,
    KernelQueueOut,
    KernelRemoteCompetitionOut,
    KernelSettingsIn,
    KernelSettingsOut,
    KernelSnapshotOut,
    KernelSolveIn,
    KernelSolveOut,
    KernelSubmitIn,
    KernelSubmitOut,
    KernelSyncIn,
    KernelSyncOut,
    KernelTokenUsageOut,
    KernelToolOut,
    KernelToolsIn,
    KernelToolsOut,
    KernelUsageByAttemptOut,
    KernelUsageByChallengeOut,
    KernelUsageByModelOut,
    KernelUsageEntryOut,
    KernelWorkOrderOut,
    KernelWriteupOut,
    McpServerOut,
    McpServersIn,
    McpServersOut,
    McpServerTestIn,
    McpServerTestOut,
)

router = APIRouter(prefix="/kernel", tags=["Kernel"])

ReadDep = Annotated[Identity, Depends(require_read)]
ScanDep = Annotated[Identity, Depends(require_scan)]
AdminDep = Annotated[Identity, Depends(require_admin)]
DbDep = Annotated[Session, Depends(get_db)]


# ══════════════════════════════════════════════════════════
# 出参组装（纯映射，不含业务）
# ══════════════════════════════════════════════════════════


def _environment_out(environment: KernelEnvironment | None) -> KernelEnvironmentOut | None:
    if environment is None:
        return None
    return KernelEnvironmentOut(
        id=environment.id,
        challengeId=environment.challenge_id,
        status=environment.status,
        sandboxProfile=environment.sandbox_profile,
        accessUrl=environment.access_url,
        accessUrls=list(environment.access_urls or []),
        containerId=environment.container_id,
        pollCount=environment.poll_count,
        startedAt=environment.started_at,
        readyAt=environment.ready_at,
        expiresAt=environment.expires_at,
        lastError=environment.last_error,
    )


def _challenge_out(
    challenge: KernelChallenge, environment: KernelEnvironmentOut | None
) -> KernelChallengeOut:
    return KernelChallengeOut(
        id=challenge.id,
        competitionId=challenge.competition_id,
        remoteId=challenge.remote_id,
        stageId=challenge.stage_id,
        title=challenge.title,
        category=challenge.category,
        description=challenge.description,
        attachments=list(challenge.attachments or []),
        score=challenge.score,
        status=challenge.status,
        # ★ 两列各归各位。前端据此判断"有没有东西等着我确认"——
        #   见 `KernelChallengeOut` 的说明：一个字段装不下两种意思。
        candidateFlag=challenge.candidate_flag,
        confirmedFlag=challenge.confirmed_flag,
        # ⚠️ 由**服务端**算好，不让前端做减法：`max - count` 这条规则
        #    有两份实现迟早会分叉（比如将来加入平台侧额度时算法会变）。
        remainingWrongSubmissions=challenge.remaining_wrong_submissions,
        wrongSubmissionCount=challenge.wrong_submission_count,
        maxWrongSubmissions=challenge.max_wrong_submissions,
        candidateCount=challenge.candidate_count,
        sandboxProfile=challenge.sandbox_profile,
        scanId=challenge.scan_id,
        solvedAt=challenge.solved_at,
        lastError=challenge.last_error,
        environment=environment,
        priority=challenge.priority,
        createdAt=challenge.created_at,
        updatedAt=challenge.updated_at,
    )


def _account_out(account) -> KernelAccountOut | None:  # noqa: ANN001
    """⚠️ 这里**没有** token 字段，也永远不会有 —— 只有掩码。

    与 `ProviderAccount` 的 `apiKey` 同一个处理：读接口不回填凭据，
    否则它会进 DOM、进截图、进剪贴板历史。
    """
    if account is None:
        return None
    return KernelAccountOut(
        id=account.id,
        label=account.label,
        status=account.status,
        remoteUserId=account.remote_user_id,
        remoteUsername=account.remote_username,
        scopes=list(account.scopes or []),
        tokenMask=account.token_mask,
        hasToken=account.has_token,
        lastSyncedAt=account.last_synced_at,
        lastError=account.last_error,
        createdAt=account.created_at,
        updatedAt=account.updated_at,
    )


def _competition_out(competition) -> KernelCompetitionOut:  # noqa: ANN001
    return KernelCompetitionOut(
        id=competition.id,
        accountId=competition.account_id,
        remoteId=competition.remote_id,
        kind=competition.kind,
        name=competition.name,
        remoteStatus=competition.remote_status,
        startAt=competition.start_at,
        endAt=competition.end_at,
        challengeCount=competition.challenge_count,
        syncedAt=competition.synced_at,
    )


def _challenges_with_environments(
    db: Session, challenges: list[KernelChallenge]
) -> list[KernelChallengeOut]:
    """一次查询补齐每道题的最新环境。

    ★ 为什么不逐题查：列表上几十道题就是几十个请求打在同一秒里
    （N+1）。`list_environments` 用一次 `IN` 查询取回全部，
    而下面的遍历只保留每道题的**最新**那条（查询已按 created_at 倒序）。
    """
    environments = service.list_environments(db, challenge_ids=[c.id for c in challenges])
    latest: dict[str, KernelEnvironmentOut] = {}
    for environment in environments:
        if environment.challenge_id in latest:
            continue
        out = _environment_out(environment)
        if out is not None:
            latest[environment.challenge_id] = out
    return [_challenge_out(c, latest.get(c.id)) for c in challenges]


# ══════════════════════════════════════════════════════════
# 账号
# ══════════════════════════════════════════════════════════


@router.get("/accounts", response_model=list[KernelAccountOut], summary="CTF2 账号绑定列表")
def list_accounts(_identity: ReadDep, db: DbDep) -> list[KernelAccountOut]:
    """只返回掩码。**没有 `token` 字段**（见 `_account_out`）。"""
    return [out for out in (_account_out(a) for a in service.list_accounts(db)) if out]


@router.post(
    "/accounts",
    response_model=KernelAccountOut,
    status_code=status.HTTP_201_CREATED,
    summary="绑定（或换绑）CTF2 账号",
)
def bind_account(
    payload: KernelAccountBindIn,
    request: Request,
    identity: AdminDep,
    db: DbDep,
) -> KernelAccountOut:
    """★ 会**真的调一次 CTF2** 验证凭据。

    不验的话用户会得到"绑定成功"，然后在第一次点解题时才吃到 401 ——
    那时他已经以为绑定没问题了，而排查方向会跑到"是不是题目环境有问题"。

    验证失败时**不落库**：把无效凭据存下来会让界面显示一个
    "已绑定"的账号，而它什么都做不了。

    ⚠️ 审计里**只写标签与 id**。PAT 明文在这一层根本拿不到第二次机会
    —— `payload.token` 被直接交给 service，本函数不读它、不记它。
    """
    account = service.bind_account(db, token=payload.token, label=payload.label)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_ACCOUNT_BIND,
        summary=f"绑定 CTF2 账号「{account.label}」",
        target=account.id,
    )
    out = _account_out(account)
    assert out is not None
    return out


@router.delete(
    "/accounts/{accountId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="解绑（清空凭据，保留历史记录）",
)
def unbind_account(accountId: str, request: Request, identity: AdminDep, db: DbDep) -> None:
    """**清空密文**而不是删行 —— 删行会级联带走赛事与提交记录
    （`ondelete=CASCADE`），而那是 CTF2 的资产，不该因为换绑而消失。

    ★ 留痕：撤掉一把代表用户操作外部平台的钥匙，与放进它是同一级的
    安全事件（见 `AuditAction.CTF2_ACCOUNT_BIND` 的说明）。
    """
    # 标签由 service 在清空密文的**同一个事务里**取出来（见那里的说明）：
    # 解绑之后这一行就没有可读的名字了，而审计里只写一个 id
    # 等于让人事后无从判断"被撤掉的是哪个号"。
    label = service.unbind_account(db, accountId)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_ACCOUNT_UNBIND,
        summary=f"解绑 CTF2 账号「{label}」",
        target=accountId,
    )


# ══════════════════════════════════════════════════════════
# 配置（提交策略）
# ══════════════════════════════════════════════════════════


def _settings_out(row) -> KernelSettingsOut:  # noqa: ANN001
    return KernelSettingsOut(
        submitMode=row.submit_mode,
        maxWrongSubmissions=row.max_wrong_submissions,
        defaultSandboxProfile=row.default_sandbox_profile,
        maxConcurrentSolves=row.max_concurrent_solves,
        solvePollSeconds=row.solve_poll_seconds,
        autoSyncSeconds=row.auto_sync_seconds,
        autoPriorityNew=row.auto_priority_new,
        autoStartNew=row.auto_start_new,
        maxTurns=row.max_turns,
        fallbackProviderId=row.fallback_provider_id,
        enableAgentsecScenarios=row.enable_agentsec_scenarios,
        enableIrScenarios=row.enable_ir_scenarios,
        enableMultiAgent=row.enable_multi_agent,
        updatedAt=row.updated_at,
    )


@router.get("/settings", response_model=KernelSettingsOut, summary="CTF2 配置（提交策略）")
def read_ctf2_settings(_identity: AdminDep, db: DbDep) -> KernelSettingsOut:
    """admin 级：`submitMode` 决定机器能否**无人确认**地对外提交 flag。"""
    return _settings_out(service.get_ctf2_config(db))


@router.put("/settings", response_model=KernelSettingsOut, summary="改 CTF2 配置")
def update_ctf2_settings(
    payload: KernelSettingsIn, request: Request, identity: AdminDep, db: DbDep
) -> KernelSettingsOut:
    """保存后**立刻生效**（worker 每次读生效值，不需要重启）。

    ★ 从 `auto` 切回 `safe` 是**即时**的，反过来也一样 ——
    不给"正在跑的解题"任何豁免：一次已受理的解题会在它的下一个
    提交决策点读到新值。这比"启动时快照策略"更符合直觉
    （用户按下 safe 就是希望**立刻**不再有自动提交）。

    ★ 留痕，而且**记下改成了什么**：`safe → auto` 是拆掉"提交前必须
    人工确认"这块刹车片。审计里只写"改了配置"没有意义 ——
    事后要回答的是"什么时候起机器开始能自己交了"。
    """
    row = service.update_ctf2_settings(
        db,
        submit_mode=payload.submitMode,
        max_wrong_submissions=payload.maxWrongSubmissions,
        default_sandbox_profile=payload.defaultSandboxProfile,
        max_concurrent_solves=payload.maxConcurrentSolves,
        solve_poll_seconds=payload.solvePollSeconds,
        auto_sync_seconds=payload.autoSyncSeconds,
        auto_priority_new=payload.autoPriorityNew,
        auto_start_new=payload.autoStartNew,
        max_turns=payload.maxTurns,
        fallback_provider_id=payload.fallbackProviderId,
        enable_agentsec_scenarios=payload.enableAgentsecScenarios,
        enable_ir_scenarios=payload.enableIrScenarios,
        enable_multi_agent=payload.enableMultiAgent,
    )
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_SETTINGS_UPDATE,
        summary=f"更新 CTF2 配置（提交模式：{row.submit_mode}）",
        target=row.id,
        detail={
            "submitMode": row.submit_mode,
            "maxWrongSubmissions": row.max_wrong_submissions,
            "defaultSandboxProfile": row.default_sandbox_profile,
            # ★ 记下并发：比赛现场"什么时候把并发调到了几"是要复查的事，
            #   而它与提交模式一样属于会改变机器对外行为的配置。
            "maxConcurrentSolves": row.max_concurrent_solves,
            # ★ v0.24：自动作业的三项也留痕。`autoStartNew` 尤其 ——
            #   打开它意味着"机器可以自己开始解题并消耗额度"，
            #   与 `submitMode` 同属"改变了机器对外行为"的配置。
            "solvePollSeconds": row.solve_poll_seconds,
            "autoSyncSeconds": row.auto_sync_seconds,
            "autoPriorityNew": row.auto_priority_new,
            "autoStartNew": row.auto_start_new,
            # ★ 回合预算也留痕：它决定"一道题最多烧多少次 LLM 调用"，
            #   属于会改变机器对外行为的配置（与并发、提交模式同一类）。
            "maxTurns": row.max_turns,
            # ★ v0.40：场景包开关也留痕，且理由比上面几项更硬 ——
            #   打开它会把 agent 的行为纪律从"攻破系统拿 flag"换成
            #   "判定安全考察点是否成立"（证据分三级、多智能体结论只能
            #   标疑似）。事后复盘"当时模型为什么不去拿 flag"时，
            #   这一项是唯一能回答的线索。
            "enableAgentsecScenarios": row.enable_agentsec_scenarios,
            # ★ v0.41：应急响应场景包开关。理由同上且更强：
            #   打开它会把 agent 的行为纪律换成「**只读**复盘、处置只出
            #   建议不执行」。事后复盘"当时模型为什么不去改目标"时，
            #   这一项是唯一能回答的线索 —— 它与 agentsec 是两个独立问题，
            #   所以分开记，不合并成"场景包状态"一项。
            "enableIrScenarios": row.enable_ir_scenarios,
            # ★ v0.58：多智能体编排开关。它改变执行拓扑（单循环 vs
            #   root+子 agent），会把一次任务的 LLM 调用量放大约 N 倍 ——
            #   事后"这次为什么花了这么多 token"要看这一项。
            "enableMultiAgent": row.enable_multi_agent,
        },
    )
    return _settings_out(row)


# ══════════════════════════════════════════════════════════
# MCP 连接（v0.81）
# ══════════════════════════════════════════════════════════
#
# ## 为什么是独立端点，不并进 `/kernel/settings`
#
# `PUT /kernel/settings` 是**全量提交**语义（`KernelSettingsIn` 每个字段
# 必填，漏传 = 静默改回默认）。把 MCP 清单并进去，会变成"加一条 MCP
# 连接必须同时把提交策略、并发、回合预算全部重发一遍" —— 而任何一次
# 前端疏忽都会顺手改掉别的配置且不留痕。
#
# 它们本来也是两件事：那一组回答"解题怎么跑"，这一组回答"智能体除了
# 沙箱工具还能调到哪些外部能力"。设置页的纪律是**每块独立保存**，
# 这里照同一条办。
#
# ⚠️ 鉴权是 admin。与 `/settings` 同级 —— 配一条 MCP 连接等于允许智能体
#    拉起任意子进程并与之通信。这是**扩大攻击面**的操作，不是普通偏好。


def _mcp_servers_out(db: DbDep) -> McpServersOut:  # noqa: ARG001
    """组装读取回执：清单（去 env）+ gogo 三工具的就绪判据。"""
    servers = service.public_mcp_servers(db)
    ready, reason = service.gogo_readiness(servers)
    return McpServersOut(
        servers=[McpServerOut(**item) for item in servers],
        gogoReady=ready,
        gogoReason=reason,
    )


@router.get(
    "/mcp-servers",
    response_model=McpServersOut,
    summary="MCP 连接清单（含 gogo 工具就绪判据）",
)
def list_mcp_servers(_identity: AdminDep, db: DbDep) -> McpServersOut:
    """读已保存的 MCP 连接。

    ★ 回执里**没有** `env`，只有 `hasEnv`。凭据不进浏览器、不进前端
    store、不进任何截图 —— 与 `KernelAccountOut` 只回 `tokenMask`
    是同一条纪律。
    """
    return _mcp_servers_out(db)


@router.put(
    "/mcp-servers",
    response_model=McpServersOut,
    summary="整份替换 MCP 连接清单",
)
def replace_mcp_servers(
    payload: McpServersIn, request: Request, identity: AdminDep, db: DbDep
) -> McpServersOut:
    """保存整份清单（**全量替换**，不是增量）。

    为什么全量：清单只有几条，逐个 PATCH 会引入"顺序""删除"两类新状态。
    前端把整份提交上来、后端整份写回，与设置页其他面板的"这一块整体保存"
    是同一个心智模型。

    ★ 留痕，且**记下每条连接的 name 与 command**：它决定智能体能拉起
    哪些外部进程。事后"这个 agent 为什么能调到 X"要看这一条。
    `env`（可能含 token）**不进审计**。
    """
    servers = [
        {
            "name": item.name,
            "command": item.command,
            "args": list(item.args),
            "env": dict(item.env),
            "notes": item.notes,
            "allowed_tools": list(item.allowed_tools) if item.allowed_tools else None,
            # ★ v0.82：前端编辑一条已有连接时**拿不到**它的 `env`（只进不出），
            #    所以它用这个标记说"这次别动 env"。没有它，用户只改一下
            #    notes 就会静默清掉凭据 —— 详见 `McpServerIn.preserveEnv`。
            "preserve_env": bool(item.preserveEnv),
        }
        for item in payload.servers
    ]
    service.save_mcp_servers(db, servers)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.KERNEL_MCP_SERVERS_UPDATE,
        summary=f"更新 MCP 连接（{len(servers)} 条）",
        detail={
            "servers": [
                {
                    "name": item["name"],
                    "command": item["command"],
                    "args": item["args"],
                    "hasEnv": bool(item["env"]),
                    "allowedTools": item["allowed_tools"],
                }
                for item in servers
            ]
        },
    )
    return _mcp_servers_out(db)


@router.post(
    "/mcp-servers/test",
    response_model=McpServerTestOut,
    summary="试连一条尚未保存的 MCP 连接",
)
async def test_mcp_server(
    payload: McpServerTestIn, _identity: AdminDep
) -> McpServerTestOut:
    """真拉起一次、握手一次、取一次工具清单。

    ★ **不保存**：这是"验证配置"，不是"提交配置"。用户填完就想知道
    对不对，让他先保存再试连会把验证变成"先污染配置再回滚"。

    ★ 为什么必须真连而不是静态检查 command 存在：MCP 的失败大多在
    协议层（不响应 initialize、stdout 混了日志、tools/list 报错），
    静态检查一个都发现不了。而这类配置错误的暴露点本来是"跑一次
    真实解题后的模型工具报错"—— 既说不清原因，又污染一次已受理的解题。
    """
    result = await service.test_mcp_server(
        {
            "name": payload.server.name,
            "command": payload.server.command,
            "args": list(payload.server.args),
            "env": dict(payload.server.env),
            "notes": payload.server.notes,
            "allowed_tools": (
                list(payload.server.allowed_tools) if payload.server.allowed_tools else None
            ),
        }
    )
    return McpServerTestOut(**result)


# ══════════════════════════════════════════════════════════
# 内置工具清单与开关（v0.82）
# ══════════════════════════════════════════════════════════
#
# ## 这两个端点补的是哪一处
#
# `runner.TOOL_NAMES` 的 13 个工具**从来没有过 HTTP 入口**：部署者既
# 看不见智能体能调什么，也不能按任务收窄。而提示词里已经在教模型用
# `recon_scan` / `run_poc` —— 界面上却一片空白。
#
# ## 鉴权为什么一读一写不同级
#
# `GET` 是 read：回的是静态清单 + 开关值，不含凭据、不涉安全策略。
# `PUT` 是 admin：改的是**能力边界**（关掉 `run_shell` 会让之后每一次
# 任务都变差，而现场看起来像"模型变笨"）。判据与 `PUT /kernel/settings`
# 一致。两处分级都登记在 `tests/test_authorization_matrix.py`。


def _tools_out(db: DbDep) -> KernelToolsOut:  # noqa: ARG001
    """组装清单回执（内部只有这一个形状，避免两处各拼一次）。"""
    payload = service.tools_catalog(db)
    return KernelToolsOut(
        tools=[KernelToolOut(**item) for item in payload["tools"]],
        groups=list(payload["groups"]),
        disabled=list(payload["disabled"]),
        gogoReady=payload["gogo_ready"],
        gogoReason=payload["gogo_reason"],
    )


@router.get(
    "/tools",
    response_model=KernelToolsOut,
    summary="内置工具清单（含开关与 MCP 就绪判据）",
)
def list_kernel_tools(_identity: ReadDep, db: DbDep) -> KernelToolsOut:
    """列全部内置工具，每个带 `enabled` 与 `ready` 两个**独立**状态。

    ⚠️ 界面上这两个不能合成一个"可用/不可用"：
      · `enabled=false` → 你去开关里打开它；
      · `enabled=true` 但 `ready=false` → 你去 MCP 连接里配好它。
    合成一个，用户就不知道该去哪修。
    """
    return _tools_out(db)


@router.put(
    "/tools",
    response_model=KernelToolsOut,
    summary="整份替换被关掉的内置工具清单",
)
def replace_kernel_tools(
    payload: KernelToolsIn, request: Request, identity: AdminDep, db: DbDep
) -> KernelToolsOut:
    """保存"关掉哪些工具"（**全量替换**，空列表 = 全开）。

    ★ 留痕：关掉一个工具会静默改变智能体的能力边界，而失败现场
    （模型突然不会用某个工具了）看起来像模型变笨，不像配置被改过。
    所以"谁在什么时候关了哪个"必须是能回答的问题。
    """
    unknown_before = service.get_disabled_tools(db)
    saved = service.save_disabled_tools(db, list(payload.disabled))
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.KERNEL_TOOLS_UPDATE,
        summary=f"更新工具开关（关掉 {len(saved['disabled'])} 个）",
        detail={
            "disabledBefore": list(unknown_before),
            "disabledAfter": list(saved["disabled"]),
        },
    )
    return _tools_out(db)


# ══════════════════════════════════════════════════════════
# 同步与列表
# ══════════════════════════════════════════════════════════


@router.get(
    "/competitions/remote",
    response_model=list[KernelRemoteCompetitionOut],
    summary="平台上有哪些赛事（即时查询，不写库）",
)
def list_remote_competitions(_identity: ScanDep) -> list[KernelRemoteCompetitionOut]:
    """scan 级，虽然它**只读**。

    ★ 为什么一个只读端点要 scan 级：它每次都真的打一次平台接口。
    给 read 级的话，一个只读令牌就能反复触发外部请求 ——
    与 `POST /ctf2/sync` 是同一条理由（"用我们的凭据去刷别人接口"）。

    ## 为什么必须有这个端点

    "同步赛事题目"要先让用户**选**赛事，而可选的赛事不能从本地库来：
    本地库里的赛事正是上一次同步的结果，第一次用的时候它是空的 ——
    那是个死结。所以这里绕过本地直接问平台。

    ⚠️ 它**不写库**：拉回来的东西只用于给用户选，落库由 `/ctf2/sync` 做。
    """
    return [
        KernelRemoteCompetitionOut(**item) for item in service.list_remote_competitions()
    ]


@router.post(
    "/challenges/pull",
    response_model=KernelPullOut,
    summary="按题目链接/ID 拉取一道练习集题目",
)
def pull_challenge(
    payload: KernelPullIn, request: Request, identity: ScanDep, db: DbDep
) -> KernelPullOut:
    """scan 级：它**写库**且会打平台接口。

    ## 它存在的理由

    CTF2 的开放 API **没有**"列出练习集题目"的接口 —— 实测 11 条候选路径
    全部返回 `404 {"params": {"resource": "route"}}`（路由不存在，不是权限）。
    而"按 id 取单题详情"**是活的**（编造 id 时返回 `resource=practice_challenge`，
    两个 resource 值不同，区别明确）。

    所以拿到题目的可行路径只有一条：**用户把平台上的题目链接粘过来**，
    我们抠出 id、在已知练习集里试出它属于哪个，然后落库。
    一次一道，但它确定能成 —— 比继续等平台开放列表接口实在。
    """
    challenge, created = service.pull_challenge(db, raw_input=payload.input)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_SYNC,
        summary=f"按链接拉取题目：{challenge.title}"
        + ("" if created else "（更新已有）"),
        target=challenge.id,
        detail={"remoteId": challenge.remote_id, "created": created},
    )
    competition = db.get(KernelCompetition, challenge.competition_id)
    return KernelPullOut(
        challenge=_challenge_out(challenge, None),
        created=created,
        challengeId=challenge.remote_id,
        competitionName=competition.name if competition else "",
    )


@router.post("/sync", response_model=KernelSyncOut, summary="从 CTF2 同步赛事与题目")
def sync_ctf2(payload: KernelSyncIn, request: Request, identity: ScanDep, db: DbDep) -> KernelSyncOut:
    """scan 级：它**改数据**（写库），且会发出去一串外部请求。

    给 read 级的话，一个只读令牌就能反复触发同步 —— 而每次同步都是
    对平台的几十次请求。那是一条"用我们的凭据去刷别人接口"的路径。

    ★ 留痕：一次同步以**我们的身份**对平台发几十次请求，并写本地库。
    审计记的是"这次同步动了多少东西"，而不是逐题一条（那会把审计表
    灌满噪音，而噪音会让真正的记录难找）。
    """
    result = service.sync(db, competition_ids=payload.competitionIds)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_SYNC,
        summary=f"同步 CTF2：{result.summary()}",
        target=None,
        detail={
            "competitions": result.competitions,
            "challengesCreated": result.challenges_created,
            "challengesUpdated": result.challenges_updated,
            "attachmentsDownloaded": result.attachments.downloaded,
            "attachmentFailures": len(result.attachments.failed),
        },
    )
    return KernelSyncOut(
        competitions=result.competitions,
        challengesCreated=result.challenges_created,
        challengesUpdated=result.challenges_updated,
        errors=result.errors,
        attachmentsDownloaded=result.attachments.downloaded,
        attachmentsSkipped=result.attachments.skipped,
        attachmentsFailed=len(result.attachments.failed),
        message=result.message(),
    )


@router.post(
    "/ir/tasks",
    response_model=IrTaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="建一个本地应急响应复盘任务（v0.43）",
)
def create_ir_task(
    payload: IrTaskIn, request: Request, identity: ScanDep, db: DbDep
) -> IrTaskOut:
    """把一个**本机证据包**变成一个可开跑的复盘任务。

    ## ★ 这个端点是「应急响应」入口能不能用的关键

    在此之前，本地复盘案例只能由 `scripts/run_ir_closed_loop.py` 直接
    写库创建 —— 侧栏放一个「应急响应」入口，点进去却建不出任务，
    那个入口就只是一句"这里本该有功能"。

    它复用 CTF2 的整条流水线（ReAct 循环 / 三类事件 / 沙箱 /
    三级证据分级 / 处置工单），但**不需要**绑 CTF2 账号 —— 证据包是
    本机上传的，整个流程一次都不碰平台的域名。

    ## scope 为什么是 scan 而不是 admin

    它不写凭据、不改安全策略（`/ctf2/settings` 那种才是 admin）。
    它做的事与"起一次扫描"同类：创建一个会消耗 LLM 额度、会起沙箱
    容器的任务。按 `POST /scans` 的同级处理。

    ## ⚠️ 附件走暂存，不在这里传

    任务一创建就会被调度器毫秒级认领，"建完再传附件"的窗口不存在
    （这条在扫描附件上已经被实测抓过一次）。所以调用顺序是：

        ① POST /scans/attachments/staging    → stagingId
        ② POST /ctf2/ir/tasks                → challengeId + 认领附件
        ③ POST /ctf2/challenges/{id}/solve   → 真正开跑

    第 ② 步内部把暂存批次里的文件搬到 `attachments.attachment_root()`,
    这是 worker 唯一会读的目录。批次过期或没传时照建不误，回执里
    `notice` 会说明 —— 拒绝创建会让"先想起来再补附件"这条常见流程
    走不通，而附件丢了是次要问题。
    """
    challenge = service.create_local_review(
        db, title=payload.title, brief=payload.brief
    )

    # 把暂存批次里的附件认领到这道题名下。
    #
    # ⚠️ 认领（`claim`）是**移动**：源目录会被清掉，所以它必须发生在
    #    题建好之后（那时 challenge.id 才存在），并且**只调一次**。
    #    幂等语义在 `uploads.claim` 里：批次不存在就返回空列表，不报错。
    claimed: list[dict] = []
    notice: str | None = None
    if payload.attachmentStagingId:
        claimed = _claim_ir_attachments(payload.attachmentStagingId, challenge.id)
        if not claimed:
            notice = "附件暂存批次不存在或已过期（暂存保留 2 小时），本次任务没有证据包。"

    # 回写附件元数据，让界面能显示"这次带了几份证据"。
    if claimed:
        challenge.attachments = [
            {"name": str(item.get("name") or ""), "url": None} for item in claimed
        ]

    # ★★ 必须先 commit，再写审计 —— 顺序反了会**静默丢掉整个任务**。
    #
    # `audit.record()` 自己会 `db.commit()`，而它的异常兜底里有一条
    # `db.rollback()`（会话级的）。这里 `create_local_review()` 只做了
    # `flush()`，也就是说此刻这道题还**没落盘**。于是：
    #
    #   audit.record() 内部抛异常（任何原因）
    #     → except 分支 rollback()
    #     → 把这道题一起回滚掉
    #     → 审计丢了（那是它的设计：审计可缺条）
    #     → 但任务也没了，而端点继续往下走，返回一个已脱离会话的对象
    #     → 最终用户在界面上得到 500，或（更糟）一个"看起来建成了"
    #       但库里不存在的 id
    #
    # 这个组合不是理论推演：第一次跑 `test_ir_task_router.py` 时它是红的，
    # 报的错是 `Instance <KernelChallenge> is not persistent within this Session`
    # 加一句 `AttributeError: 'Request' object has no attribute 'identity'`
    # —— 后者才是第一现场（见下面那行的说明），前者是它的后果。
    db.commit()
    db.refresh(challenge)

    audit.record(
        db,
        request=audit.from_http(request, identity),
        # ★ 专用动作而不是复用 `CTF2_SOLVE_START`：它的标签是
        #   「发起 CTF2 解题」，而这次动作是"把一批生产日志交给智能体"。
        #   审计日志给事后追责的人看，两者是两个不同的问题。
        action=AuditAction.IR_TASK_CREATE,
        summary=f"建了一个本地复盘任务：{challenge.title}",
        target=challenge.id,
        detail={
            "kind": "local",
            "category": challenge.category,
            "sandboxProfile": challenge.sandbox_profile,
            "attachments": [str(item.get("name") or "") for item in claimed],
        },
    )
    db.refresh(challenge)

    return IrTaskOut(
        challenge=_challenge_out(challenge, None),
        attachments=claimed,
        notice=notice,
    )


def _claim_ir_attachments(staging_id: str, challenge_id: str) -> list[dict]:
    """把暂存的证据包搬到这道题自己的附件目录。返回认得的那几个。

    ## 为什么要单独搬一次，不能用 `uploads.claim`

    `uploads.claim` 搬进的是**扫描**的附件目录（`scan_upload_dir(scan_id)`），
    而 worker 读的是 `attachments.attachment_root(challenge_id)`
    （`_scan_runs/_ctf2_attachments/<challenge_id>/`）。两个路径属于
    两个不同的领域，复用前者的函数会得到"文件搬到了，但 worker 看不见"。

    ## ⚠️ 必须排除指纹文件

    `attachments.FINGERPRINT_SUFFIX`（`.sha256`）是同步逻辑写下的旁挂
    文件。不排除的话 `_attachment_paths` 会把它当成一份证据交给模型 ——
    而模型会去读一个只含一行哈希的文件，然后报告"这份证据是空的"。
    这条坑在 `runner._attachment_paths` 里有同样的说明。
    """
    from app.kernel import attachments as attachments_mod
    from app.scans import uploads as uploads_mod

    source = uploads_mod.staging_dir(staging_id)
    if not source.is_dir():
        return []

    target = attachments_mod.attachment_root(challenge_id)
    target.mkdir(parents=True, exist_ok=True)

    claimed: list[dict] = []
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.name.endswith(attachments_mod.FINGERPRINT_SUFFIX):
            continue
        name = uploads_mod.unique_name(target, path.name)
        (target / name).write_bytes(path.read_bytes())
        claimed.append({"name": name, "sizeBytes": (target / name).stat().st_size})

    # 认领 = 移动。清掉暂存目录，让"已认领"由文件系统本身表达
    # （与 `uploads.claim` 同一纪律）。
    import shutil

    shutil.rmtree(source, ignore_errors=True)
    return claimed


@router.get("/competitions", response_model=list[KernelCompetitionOut], summary="赛事列表")
def list_competitions(
    _identity: ReadDep,
    db: DbDep,
    accountId: Annotated[str | None, Query(description="只看某个绑定下的赛事")] = None,
) -> list[KernelCompetitionOut]:
    return [
        _competition_out(item) for item in service.list_competitions(db, account_id=accountId)
    ]


@router.get("/challenges", response_model=list[KernelChallengeOut], summary="题目列表")
def list_challenges(
    _identity: ReadDep,
    db: DbDep,
    competitionId: Annotated[str | None, Query(description="只看某场赛事")] = None,
    status: Annotated[str | None, Query(description="按状态筛")] = None,
) -> list[KernelChallengeOut]:
    """一次把**环境**也带出去（见 `_challenges_with_environments` 的说明）。"""
    challenges = service.list_challenges(db, competition_id=competitionId, status=status)
    return _challenges_with_environments(db, challenges)


@router.get("/challenges/{challengeId}", response_model=KernelChallengeOut, summary="题目详情")
def get_challenge(challengeId: str, _identity: ReadDep, db: DbDep) -> KernelChallengeOut:
    challenge = service.get_challenge_or_404(db, challengeId)
    environment = service.latest_environment(db, challengeId)
    return _challenge_out(challenge, _environment_out(environment))


@router.get("/snapshot", response_model=KernelSnapshotOut, summary="一次拿到账号+赛事+题目")
def get_snapshot(
    _identity: ReadDep,
    db: DbDep,
    competitionId: Annotated[str | None, Query()] = None,
) -> KernelSnapshotOut:
    """一次性把三层给全，而不是让前端三次往返。

    这三层在任何一次有意义的展示里都会同时被需要，而分三次请求
    会让界面出现"题目出来了、账号还没出来"的中间闪烁。
    """
    competition = (
        service.get_competition_or_404(db, competitionId) if competitionId else None
    )
    challenges = service.list_challenges(db, competition_id=competitionId or None)
    return KernelSnapshotOut(
        account=_account_out(service.get_account(db)),
        competition=_competition_out(competition) if competition is not None else None,
        challenges=_challenges_with_environments(db, challenges),
    )


@router.get("/queue", response_model=KernelQueueOut, summary="解题队列状态")
def read_queue(_identity: ReadDep, db: DbDep) -> KernelQueueOut:
    """与 `GET /scans/queue` 同一条思路：它回答的是
    「为什么我点了开始解题，却一直排在队列里」——
    答案通常是"并发上限满了"，而那需要能看到当前在跑几个、上限几个。
    """
    from app.kernel.scheduler import get_scheduler

    stats = get_scheduler().stats(db)
    return KernelQueueOut(
        running=stats.running,
        limit=stats.limit,
        queued=stats.queued,
        challenges=stats.challenges,
    )


# ══════════════════════════════════════════════════════════
# 解题 / 提交 / 取消
# ══════════════════════════════════════════════════════════


@router.post(
    "/challenges/{challengeId}/solve", response_model=KernelSolveOut, summary="开始解题"
)
def solve_challenge(
    challengeId: str, payload: KernelSolveIn, request: Request, identity: ScanDep, db: DbDep
) -> KernelSolveOut:
    """受理一次解题：起环境 → 沙箱 → 解题 → 产生候选 flag。

    ## ★ 它**不同步等解题结束**

    解题要跑几分钟到几十分钟（LLM 在沙箱里干活）。同步等它意味着一个
    几十分钟的 HTTP 请求 —— 中间任何一层（浏览器、反代、uvicorn 的
    keep-alive）都会先超时，而超时之后用户**不知道任务到底起没起**。

    所以它只做"受理"：状态落库、丢进队列、立刻返回。
    真正的进展走 **events 流**（已定设计决策 7：前端 Activity 页面
    不改代码就能看到）。

    ⚠️ 这也意味着本端点**不会**返回 flag。它返回的是"受理凭据"。
    """
    challenge = service.get_challenge_or_404(db, challengeId)
    service.start_solve(
        db,
        challenge,
        mode=payload.mode,
        sandbox_profile=payload.sandboxProfile,
        max_wrong_submissions=payload.maxWrongSubmissions,
        # ★ v0.28：这个字段在 schema 里一直有，而**从来没被转交过** ——
        #   `KernelSolveIn.brief` 的注释写着"传给 agent 的额外提示"，
        #   实际上一路断在这里：router 不传 → service 没参数 →
        #   prompt 里没有位置。用户填了它，什么都不会发生，
        #   而且没有任何反馈告诉他这一点。
        brief=payload.brief,
        resume=payload.resume,
    )
    db.refresh(challenge)
    environment = service.latest_environment(db, challengeId)
    # ★ 留痕：起环境 + 在容器里跑 LLM 是有真实代价的动作（平台额度、CPU），
    # 而且解题用的沙箱镜像与额度上限都是**这一次**的决定，事后要能查。
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_SOLVE_START,
        summary=f"发起解题：{challenge.title}",
        target=challenge.id,
        detail={
            "competitionId": challenge.competition_id,
            "remoteId": challenge.remote_id,
            "sandboxProfile": challenge.sandbox_profile,
            "maxWrongSubmissions": challenge.max_wrong_submissions,
        },
    )
    return KernelSolveOut(
        challenge=_challenge_out(challenge, _environment_out(environment)),
        environmentId=environment.id if environment else None,
        scanId=challenge.scan_id,
        message=(
            "已受理，正在启动题目环境。进展会出现在活动流里 —— "
            "候选 flag 产出后会等你确认（safe 模式）。"
        ),
    )


@router.put(
    "/challenges/{challengeId}/priority",
    response_model=KernelChallengeOut,
    summary="设置解题优先级",
)
def set_challenge_priority(
    challengeId: str, payload: KernelPriorityIn, identity: ScanDep, db: DbDep
) -> KernelChallengeOut:
    """调一道题在**排队**中的先后。越大越先做，默认 0。

    ## 为什么比赛需要它

    规则里写着「每道题目前三名成功解题的团队将额外获得本题分值的
    15%、10%、5%」—— **速度直接换分**；而同一份规则也明确允许
    「调整解题优先级」。

    原来的认领顺序是纯 FIFO，用户想插队只能"取消再重新点解题"：
    那会把状态打回 `unsolved`、清掉候选，而且重排后 `updated_at`
    又变了 —— 用起来既不精确又有副作用。

    ## ⚠️ 改优先级**不影响**已经在跑的题

    只有还在排队、尚未被 worker 认领的题会被重新排序。中断一道
    跑到一半的题会浪费掉已经消耗的平台环境与 LLM 调用，而
    "让它跑完"的代价只是排队的题晚一会儿开始。

    审核级别：`ScanDep`（与"开始解题"同级）。它不是危险动作 ——
    改一个排序权重不会让任何东西被发到外部。
    """
    challenge = service.set_challenge_priority(
        db, challengeId, priority=payload.priority
    )
    environment = service.latest_environment(db, challengeId)
    return _challenge_out(challenge, _environment_out(environment))


@router.post(
    "/priorities/by-score",
    summary="按分值批量设置解题优先级",
    response_model=KernelPriorityBatchOut,
)
def set_priorities_by_score(
    payload: KernelPrioritiesByScoreIn, identity: ScanDep, db: DbDep
) -> KernelPriorityBatchOut:
    """把排队中的题**按分值从高到低**批量设优先级。返回改了几道。

    ## ★ 它是便利入口，**不是默认策略**

    "先做高分题"听起来显然，但不总是对的：高分题往往更难，开赛初期
    把并发全压在几道硬题上可能一道都拿不下；而"前三名加分"意味着
    **先解出一道容易题**有时比死磕难题更划算。真正的取舍取决于当前
    比分、剩余时间与队伍能力 —— 那是一道战术题，系统替用户猜只会猜错。

    所以调度的默认行为仍然是 FIFO（`priority` 全 0），而这个端点
    必须**由人主动点**。

    ## 分值相同或缺失

    同分按 `updated_at`（FIFO）。**没有分值的题排在最后** ——
    PostgreSQL 的 `DESC` 默认把 NULL 排最前，那会让所有没分值的题
    插到队首，而它们恰恰是最不值得优先的。
    """
    changed = service.set_priorities_by_score(
        db, competition_id=payload.competitionId, limit=payload.limit
    )
    return KernelPriorityBatchOut(changed=changed)


@router.post(
    "/challenges/{challengeId}/submit",
    response_model=KernelSubmitOut,
    summary="确认提交候选 flag（safe 模式的人工确认点）",
)
def submit_flag(
    challengeId: str, payload: KernelSubmitIn, request: Request, identity: ScanDep, db: DbDep
) -> KernelSubmitOut:
    """★ **这个端点是 safe 模式的全部意义所在。**

    safe 模式下解题只产出候选、不提交，而"要不要提交"停在
    **必须由人推一下**的位置。这一推就是这里。

    为什么需要人来推：flag 的提交是**不可撤销的外部动作**，
    且有**错误次数上限** —— 烧完额度这道题就只能等下一场。
    让一个 LLM 在无人确认的情况下连续对外提交，赌的是"它每次都对"，
    而猜错是常态（见 `runner.decide_submission` 的说明）。

    ⚠️ 三条硬约束都在 `service.confirm_submit` 里，不在这里：
    本地格式校验、去重、额度。放在 service 是因为**自动提交那条路径
    必须遵守同一套**（两边共用同一个函数，见那里的 docstring）。
    """
    challenge = service.get_challenge_or_404(db, challengeId)
    status_value, message = service.confirm_submit(
        db, challenge, flag=payload.flag, mode=payload.mode
    )
    db.refresh(challenge)
    # ★ 留痕：这是**对外发出的、不可撤销的**动作，而且消耗不可再生的
    # 错误提交额度。"谁在什么时候把哪个 flag 交出去"必须可回答。
    #
    # ⛔ detail 里**不写 flag 明文、也不写平台的回复文案**：
    #   · 审计的保留期（365 天）比 `ctf2_submissions` 长，把答案复制到
    #     第二张表等于让它多活一段更久的时间；
    #   · 平台的回复文案可能**回显**我们提交的内容，而经过 `redact`
    #     不等于安全（它挡的是 token 形状，不是 flag）。
    #   所以只记指纹：它对得上账（"这次提交是不是我那个"），
    #   但反推不出内容。
    from app.kernel import runner

    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_SUBMIT,
        summary=f"提交 flag（{challenge.title}）→ {status_value}",
        target=challenge.id,
        detail={
            "status": status_value,
            "flagFingerprint": runner.flag_fingerprint(payload.flag),
        },
    )
    return KernelSubmitOut(
        status=status_value,
        message=message,
        challenge=_challenge_out(challenge, None),
    )


@router.post(
    "/challenges/{challengeId}/cancel", response_model=KernelChallengeOut, summary="取消解题"
)
def cancel_challenge(
    challengeId: str, request: Request, identity: ScanDep, db: DbDep
) -> KernelChallengeOut:
    """取消。

    ⚠️ **它不能杀死已经在跑的 worker**（线程杀不掉，见 scheduler 的
    docstring）。所以"取消"对**已经在跑**的那一次只是标记 ——
    那一轮会跑完，然后不落候选。前端必须说清这个区别，
    否则用户点完取消看到进度还在动，会以为按钮坏了。

    ★ 留痕：它会**丢掉已经产生的结果**（候选 flag）并中止一次
    有代价的运行，而"这道题怎么突然没结果了"正是事后会问的。
    """
    challenge = service.get_challenge_or_404(db, challengeId)
    previous = challenge.status
    service.cancel_challenge(db, challenge)
    db.refresh(challenge)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_SOLVE_CANCEL,
        summary=f"取消解题：{challenge.title}",
        target=challenge.id,
        detail={"fromStatus": previous, "toStatus": challenge.status},
    )
    return _challenge_out(challenge, None)


@router.delete(
    "/challenges/{challengeId}/environment",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="释放题目环境",
)
def release_environment(challengeId: str, _identity: ScanDep, db: DbDep) -> None:
    """释放平台环境（+ 把本地环境行标成 stopped）。

    scan 级：它**改状态**且调平台接口。给 read 级的话，一个只读令牌
    就能把别人正在解题的环境停掉 —— 那会让解题在半路失败。

    ⚠️ 走 `release_environment_sync` 而不是自己 `asyncio.run`：
    "怎么在同步上下文里跑协程"这件事的使用面越小，那条
    "端点不能改成 async def"的约束就越难被无意破坏。
    """
    service.get_challenge_or_404(db, challengeId)  # 不存在就 404
    service.release_environment_sync(db, challengeId)


# ══════════════════════════════════════════════════════════
# 题解与 token 统计（v0.36）
# ══════════════════════════════════════════════════════════


@router.get(
    "/challenges/{challengeId}/writeup",
    response_model=KernelWriteupOut,
    summary="CTF 题解（Writeup）",
)
def read_writeup(challengeId: str, _identity: ReadDep, db: DbDep) -> KernelWriteupOut:
    """读这道题的题解（赛后提交的依据）。

    ## ★ 为什么不复用 `GET /scans/{id}/writeup`

    那条路拼的是 **strix 报告**的版式重排。而 CTF2 解题**不走 strix**
    （由 `runner.solve_in_sandbox` 自己的循环驱动），从不产生
    `penetration_test_report.md`。

    所以扫描的题解端点对 CTF2 的题**永远**报"没有留下报告文件"，
    而那句话在误导用户去找一个不该存在的文件 —— 详见契约 §3.19.5。

    ## 三种成因走同一个 409，但**话不一样**

    判据是"这道题**发起过**解题没有"（`service.was_solve_started`），
    **不是** `status == unsolved`、**也不是**只看受理快照：

      · 题不存在 → 404（由 `get_challenge_or_404` 抛）；
      · 从没发起过 → "点「解题」跑一次，结束后会自动生成"；
      · 发起过但没留下题解行 → "这次没有生成题解"，并给出下一步。

    ⚠️ 判据**不能**用状态。`environment_ready` / `solving` /
    `submitting` / `candidate_ready` / `failed` 都不是 `unsolved`，
    按状态二分会把它们全部归进第二档 —— 于是**一道正在跑的题**
    会得到"解题记录早于题解功能上线（v0.36）"这句**假话**。
    用户刚点完「解题」，却被告知"重解一次" —— 那是最不该发生的误导。

    ⚠️ 判据**也不能**只看 `ctf2_solve_attempts`。那张表是 v0.28 才建的，
    之前解的题没有 attempt 行 —— 实测有 **20 道平台判过对的题**
    （`hashcat` 等）会因此得到"还没有解过"这句假话。所以判据加上了
    壳 scan（`scan_id` 非空）这条更早的证据线，详见
    `service.was_solve_started` 的说明。

    分开三个码只会让前端写三段其实一样的分支 —— 补救动作都不是
    在界面上能做的。而 `message` 本来就是写给用户看的中文原话。
    """
    challenge = service.get_challenge_or_404(db, challengeId)

    row = service.get_writeup(db, challengeId)
    if row is None:
        # ⚠️ 区分"从没发起过"与"发起过但没留下题解" —— 两者下一步不同。
        if not service.was_solve_started(db, challengeId):
            raise ApiError(
                409,
                ErrorCode.WRITEUP_UNAVAILABLE,
                "这道题还没有解过，所以没有题解。点「解题」跑一次，"
                "结束后会自动生成。",
            )
        raise ApiError(
            409,
            ErrorCode.WRITEUP_UNAVAILABLE,
            "这次解题没有生成题解（题解功能自 v0.36 起才产出，"
            "也可能那一次没有留下解题过程）。重新解一次就会生成。",
        )

    return KernelWriteupOut(
        challengeId=challenge.id,
        title=challenge.title,
        category=challenge.category or "",
        solved=row.solved,
        markdown=row.markdown,
        filename=writeup_mod.filename_for(challenge),
        attemptId=row.attempt_id,
        turns=row.turns,
        elapsedSec=row.elapsed_sec,
        generatedAt=row.generated_at,
        updatedAt=row.updated_at,
    )


@router.get(
    "/challenges/{challengeId}/workorder",
    response_model=KernelWorkOrderOut,
    summary="处置建议工单（应急响应复盘产物，v0.41）",
)
def read_workorder(
    challengeId: str, _identity: ReadDep, db: DbDep
) -> KernelWorkOrderOut:
    """读这道题的**处置建议工单**（会话 C 工单的产物）。

    ## 这为什么是一个独立端点（而不是题解里的一节）

    题解是叙事文档，工单是**待办清单**。工单会被复制进工单系统、
    打印出来批、当附件发出去 —— 它得能**单独下载**。题解里只放索引
    （第几条、什么级别、去哪找），正文不重复写（见 `writeup.build_writeup`
    第六节）。

    ## ★ 409 的判据是"有没有产出过工单"，与题解那三个成因**不同**

    题解对每道解过的题都有；工单**只有应急响应任务**才有 ——
    普通 CTF 题的提示词里没让模型写 `workorders`，所以它恒为空。
    这不是失败，是这类任务根本不产出工单。

    所以这里**不**复用 `WRITEUP_UNAVAILABLE`：它对应的补救动作是
    "重解一次"，而一条 CTF 题重解一百次也不会有工单 —— 那会让用户
    在一个永远做不到的动作上反复尝试。

    ## ⚠️ 空工单是一份**文件**，不是 404

    「本次复盘没有需要处置的事项」本身是个结论（见
    `workorder.render_workorders` 的空列表分支），它会被渲染成一份
    带说明的 markdown。只有**压根没产出过文件**才是 409。
    """
    challenge = service.get_challenge_or_404(db, challengeId)

    markdown = workorder_mod.read_order_file(challenge.id)
    if markdown is None:
        raise ApiError(
            409,
            ErrorCode.WRITEUP_UNAVAILABLE,
            "这道题还没有处置建议工单。工单是应急响应复盘任务的产物 —— "
            "普通解题任务不产出工单。如果这是一道应急响应题，"
            "等本次复盘跑完再看。",
        )

    return KernelWorkOrderOut(
        challengeId=challenge.id,
        title=challenge.title,
        markdown=markdown,
        filename=workorder_mod.filename_for_order(challenge.title),
        count=workorder_mod.count_orders(markdown),
    )


@router.get(
    "/challenges/{challengeId}/usage",
    response_model=list[KernelUsageEntryOut],
    summary="单题的逐次 token 消耗",
)
def read_challenge_usage(
    challengeId: str, _identity: ReadDep, db: DbDep
) -> list[KernelUsageEntryOut]:
    """这道题的**每一次 LLM 调用**花了多少（按时间与轮次升序）。

    这是"细分到每一次任务的消耗"最直接的视图 —— 能看出
    哪一轮最贵、是不是在兜圈子烧钱。

    ⚠️ 返回的每一项都带 `usageReported`：接入点没给用量时 token 是 0，
    而那个 0 **不代表没消耗**。前端必须分开显示"0"与"未记录"。
    """
    service.get_challenge_or_404(db, challengeId)  # 不存在就 404
    rows = service.challenge_usage_entries(db, challengeId)
    return [
        KernelUsageEntryOut(
            turn=r.turn,
            model=r.model,
            providerLabel=r.provider_label,
            promptTokens=r.prompt_tokens,
            completionTokens=r.completion_tokens,
            totalTokens=r.total_tokens,
            reasoningTokens=r.reasoning_tokens,
            usageReported=r.usage_reported,
            at=r.created_at,
        )
        for r in rows
    ]


@router.get("/usage", response_model=KernelTokenUsageOut, summary="token 消耗统计")
def read_usage(
    _identity: ReadDep,
    db: DbDep,
    challengeId: Annotated[str | None, Query(alias="challengeId")] = None,
    competitionId: Annotated[str | None, Query(alias="competitionId")] = None,
) -> KernelTokenUsageOut:
    """token 消耗统计（总览页与题目详情共用）。

    ## ★★ `calls` 与 `recordedCalls` 分开看

    `calls` 是调用次数（永远准确）；`recordedCalls` 是其中**接入点
    真的返回了用量**的次数。两者不等时 `totalTokens` 是**下限**。

    把"没记录"显示成"消耗 0"会与"没跑过"混淆 —— 见
    `schemas.KernelTokenUsageOut` 的说明。

    ## ⚠️ 不做 token → 金额换算

    单价随模型、供应商、时段变化，写死在代码里会误导。只给 token 数。

    ## ⚠️ `since` 说明统计从哪一刻开始

    v0.36 之前的解题没有 usage 记录。前端应显示它，否则用户会以为
    "总共就花了这么点"，而实际是**没记**。
    """
    data = service.token_usage_summary(
        db, challenge_id=challengeId, competition_id=competitionId
    )
    return KernelTokenUsageOut(
        calls=data["calls"],
        recordedCalls=data["recordedCalls"],
        promptTokens=data["promptTokens"],
        completionTokens=data["completionTokens"],
        totalTokens=data["totalTokens"],
        byModel=[KernelUsageByModelOut(**m) for m in data["byModel"]],
        byChallenge=[KernelUsageByChallengeOut(**c) for c in data["byChallenge"]],
        # ★ v0.37：按每次解题分组（需求要的"细分到每个任务"）。
        byAttempt=[KernelUsageByAttemptOut(**a) for a in data["byAttempt"]],
        since=data["since"],
    )


# ══════════════════════════════════════════════════════════
# 平台接入（v0.25）
# ══════════════════════════════════════════════════════════
#
# ## 权限：**admin 级**，与「改 CTF2 配置」同级
#
# 这几个端点收的是"服务端要去打哪个地址"：用户填一个 baseUrl，服务端就
# 真的去请求它。这与"开始解题"（scan 级）不是一回事 —— 那个是把已有的题
# 丢给沙箱，这个是**让服务端发起一次到任意主机的请求**。
#
# ⚠️ 内网地址**不拦**（比赛平台常常就部署在内网，拦了等于功能白做），
#    只拦云厂商的实例元数据服务 —— 见 `platforms.BLOCKED_HOSTS`。


def _platform_out(row, *, active_id: str | None) -> KernelPlatformOut:  # noqa: ANN001
    return KernelPlatformOut(
        id=row.id,
        slug=row.slug,
        displayName=row.display_name,
        baseUrl=row.base_url,
        driver=row.driver,
        source=row.source,
        status=row.status,
        lastError=row.last_error,
        attempts=row.attempts,
        isBuilding=row.status == "building",
        lastRaw=row.last_raw,
        active=row.id == active_id,
        createdAt=row.created_at,
        updatedAt=row.updated_at,
    )


def _builtin_platform_out(*, active_id: str | None) -> KernelPlatformOut:
    """内置 CTF2 的**合成条目** —— 它不在数据库里。

    见 `models.KernelPlatform` 那段"为什么不插初始数据"：让"账号用哪个平台"
    只有一种表示法（`platform_id IS NULL` = 内置），列表在展示层补上它。
    """
    return KernelPlatformOut(
        id=None,
        slug=platforms.BUILTIN_SLUG,
        displayName=platforms.BUILTIN_DISPLAY,
        baseUrl="",
        driver="ctf2",
        source="builtin",
        status="verified",
        lastError=None,
        attempts=0,
        isBuilding=False,
        lastRaw=None,
        active=active_id is None,
        createdAt=None,
        updatedAt=None,
    )


def _active_platform_id(db: Session) -> str | None:
    account = service.get_account(db)
    return account.platform_id if account is not None else None


@router.get("/platforms", response_model=list[KernelPlatformOut], summary="已接入的平台")
def list_platforms(_identity: ReadDep, db: DbDep) -> list[KernelPlatformOut]:
    """内置的 CTF2 排在第一个（它是默认的那个）。"""
    active_id = _active_platform_id(db)
    return [
        _builtin_platform_out(active_id=active_id),
        *[_platform_out(row, active_id=active_id) for row in platforms.list_platforms(db)],
    ]


@router.get(
    "/platforms/{platformId}",
    response_model=KernelPlatformOut,
    summary="平台详情（生成期间轮询它）",
)
def read_platform(platformId: str, _identity: ReadDep, db: DbDep) -> KernelPlatformOut:
    """★ 前端在生成期间**轮询这个端点**，直到 `isBuilding` 变假。

    为什么不让生成挂在 POST 上同步返回：一轮生成最坏两分钟（探测几十个
    候选端点 + 最多三次大模型调用 + 试跑），挂在 HTTP 请求上必然超时 ——
    而超时之后后台还在跑，用户看到"失败"、重试一次却"成功"，
    看起来像个偶发 bug。
    """
    row = platforms.get_platform_or_404(db, platformId)
    return _platform_out(row, active_id=_active_platform_id(db))


@router.post(
    "/platforms/draft",
    response_model=KernelPlatformOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="让 agent 识别一个平台（后台生成）",
)
def draft_platform(
    payload: KernelPlatformDraftIn, request: Request, identity: AdminDep, db: DbDep
) -> KernelPlatformOut:
    """受理并**立刻返回**（202）。真正的生成在后台跑。

    ⚠️ 明文 key 只在内存里过一遍，**不落库** —— 要真正启用这个平台，
    用户还得再点一次「启用」，那时才写进账号表的密文列。
    """
    row = platforms.create_building(db, base_url=payload.baseUrl)
    started = platforms.start_build(
        row.id,
        base_url=row.base_url,
        access_key=payload.accessKey,
        document=payload.document,
    )
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_PLATFORM_DRAFT,
        summary=f"发起平台自动识别（{row.base_url}）",
        target=row.id,
        detail={
            "baseUrl": row.base_url,
            "slug": row.slug,
            "started": started,
            "hasDocument": bool((payload.document or "").strip()),
            # ⚠️ 只记**长度**，不记内容：文档可能有几百 KB，而审计表
            #    不是放它的地方（它的作用是回答"谁在什么时候做了什么"）。
            "documentChars": len(payload.document or ""),
        },
    )
    db.refresh(row)
    return _platform_out(row, active_id=_active_platform_id(db))


@router.post(
    "/platforms/{platformId}/activate",
    response_model=KernelPlatformOut,
    summary="启用一个平台（校验并写入凭据）",
)
def activate_platform(
    platformId: str,
    payload: KernelPlatformActivateIn,
    request: Request,
    identity: AdminDep,
    db: DbDep,
) -> KernelPlatformOut:
    """验证凭据 → 写进账号表 → 把账号指向这个平台。"""
    account = platforms.activate_platform(
        db,
        platform_id=platformId,
        access_key=payload.accessKey,
        label=payload.label,
    )
    row = platforms.get_platform_or_404(db, platformId)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_PLATFORM_ACTIVATE,
        summary=f"启用平台 {row.display_name}",
        target=row.id,
        detail={"slug": row.slug, "baseUrl": row.base_url, "label": account.label},
    )
    return _platform_out(row, active_id=account.platform_id)


@router.post(
    "/platforms/use-builtin",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="切回内置的 CTF2 驱动",
)
def use_builtin_platform(request: Request, identity: AdminDep, db: DbDep) -> None:
    """把账号退回内置驱动。

    ★ 它**不删**任何配置 —— 生成的平台还留在列表里，随时能再启用。
    这个动作只改"账号指向谁"。
    """
    platforms.use_builtin_platform(db)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_PLATFORM_USE_BUILTIN,
        summary="切回内置 CTF2 驱动",
        target="ctf2",
        detail={},
    )


@router.delete(
    "/platforms/{platformId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一份平台配置",
)
def delete_platform(platformId: str, request: Request, identity: AdminDep, db: DbDep) -> None:
    """删配置**不动账号的凭据**，但会把账号退回内置驱动（如果它正指向
    被删的这个）—— 否则账号会留下一个悬空的 `platform_id`。"""
    row = platforms.get_platform_or_404(db, platformId)
    slug, base_url = row.slug, row.base_url
    platforms.delete_platform(db, platformId)
    audit.record(
        db,
        request=audit.from_http(request, identity),
        action=AuditAction.CTF2_PLATFORM_DELETE,
        summary=f"删除平台配置 {slug}",
        target=platformId,
        detail={"slug": slug, "baseUrl": base_url},
    )


__all__ = ["router"]
