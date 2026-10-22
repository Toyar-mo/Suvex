"""`ctf2` 领域的数据模型。

## 五张表为什么是五张

它们各自回答一个**生命周期不同**的问题，合并任何两张都会制造
"什么时候清空"的争执：

| 表 | 生命周期 | 谁产生 |
| --- | --- | --- |
| `ctf2_accounts` | 跟着**人**（解绑才没） | 用户粘贴 PAT |
| `ctf2_competitions` | 跟着**平台**（赛事结束也留着，是历史） | 同步动作 |
| `ctf2_challenges` | 跟着**赛事/练习集** | 同步动作 |
| `ctf2_environments` | **最短**：环境停掉就是一段历史 | 启动环境 |
| `ctf2_submissions` | **只增不改**（提交记录是证据链） | 提交动作 |

把 `environments` 并进 `challenges` 是最诱人的一步（一对一嘛），
但那会丢掉"同一道题起过三次环境"这件事 —— 而排查"为什么一直起不来"
靠的正是这三次尝试的时间线。

`submissions` 并进去更糟：它会变成"只存最后一次提交"，于是
"这道题我试过哪几个 flag"永远查不出来了。

## ★ 凭据只在一处，且是密文

`ctf2_accounts.token_encrypted` 是**全模块唯一**持有 PAT 的地方，
与 `provider_accounts.api_key_encrypted` 同一套做法（`core/crypto.py`
的 Fernet）。这里刻意**不重复**那段"为什么不哈希"的论证 —— 结论一样、
代码路径一样，重复一遍只会多一处要同步维护的文字。

⚠️ 同样刻意**不提供** `account.token` 属性：让 ORM 对象根本没有一条
通往明文的路，比"提供一个会返回明文/掩码的属性"更安全。明文只能通过
`service.decrypt_account_token()` 拿到，那个函数的名字就说明了代价。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.columns import JsonColumn, UtcDateTime
from app.core.db import Base
from app.core.timeutil import now_utc
from app.core.vocab import (
    DEFAULT_AGENTSEC_VERDICT,
    Ctf2ChallengeStatus,
    Ctf2EnvironmentStatus,
    Ctf2SandboxProfile,
    Ctf2SubmissionStatus,
    Ctf2SubmitMode,
    values,
)

# CTF2 官方域名。**硬编码**（已定设计决策 5）：
# 它不是一个"可配置的部署参数"，而是一条安全边界 ——
# 任何让 PAT 被发往非官方地址的路径都是凭据外泄。
#
# ⚠️ 允许运维加白名单是**刻意不做**的：本模块的唯一用途就是对接
# ctf2.dasctf.com，"自建 CTF2 兼容平台"是一个不存在的东西。
# 如果将来真出现，正确做法是加一条 ADR 并重新评估威胁模型，
# 而不是给一个环境变量让人随手改掉这条边界。
CTF2_OFFICIAL_HOSTS: tuple[str, ...] = (
    "ctf2.dasctf.com",
    # 平台的文件 CDN —— 题目的附件 `download_url` 全部指向这里
    # （实测：https://ctf2-files.dasctf.com/ctf-files/uploads/…）。
    #
    # ⚠️ 它**不是** `ctf2.dasctf.com` 的子域，所以必须单独列出来。
    # 漏了它的症状不是"少下几个文件"，而是**每一次附件下载都被自己的
    # 白名单拒掉** —— 而报错写着"不在官方域名下"，看起来像平台给了
    # 一个野地址，实际是我们自己的名单不全。实测（2026-09-29）：
    # 所有题目的附件一个都没落盘过。
    #
    # 加它的代价要说清楚：这份名单同时在守"PAT 只发往官方地址"那条
    # 边界，多一个条目就多一个可被配成 `base_url` 的取值。这里接受它，
    # 是因为该域名同属平台方、且只承载静态文件；而且
    # `assert_allowed_target` 的匹配是"精确或**真**子域"
    # （`host == x or host.endswith("." + x)`），并没有为了省事写成
    # `endswith("dasctf.com")` —— 那种写法会放进 `evil-dasctf.com`。
    "ctf2-files.dasctf.com",
)

# API 前缀。所有端点都在它之下（见 `client.py`）。
CTF2_API_PREFIX = "/api/open/v1/user"

# 账号状态。三个取值，**不放进 vocab**：它们只在 `service.bind_account` /
# `unbind_account` 两处被写，没有任何跨模块消费方，也没有前端标签表 ——
# 而 vocab 里那些枚举之所以集中，是因为它们是前后端的共同语言。
_ACCOUNT_STATUSES = ("active", "expired", "revoked")


class Ctf2Account(Base):
    """一个绑定的 CTF2 账号。

    ## 为什么 token 是密文列 + 掩码列（两列）

    与 `provider_accounts` 完全同构，理由也一样：读接口（"我绑了哪个号"）
    **根本不需要明文**，把掩码预先算好存下来，读路径上就永远不出现明文。
    这样"明文只在真的要调 CTF2 的那一刻才被解开"是可验证的，不只是承诺。

    代价同样要说清楚：两列必须**同时**更新，所以它们只允许在
    `service.bind_account` 一个函数里被写入。
    """

    __tablename__ = "ctf2_accounts"
    __table_args__ = (
        # 一个实例只允许一个**活跃**绑定。用部分唯一索引在数据库层强制 ——
        # 与 `uq_provider_accounts_default` 同一条理由：只在 service 里判
        # "有没有别的活跃绑定"，两个并发请求会各自看到"没有"然后都写下去，
        # 于是"用哪个 PAT"变成不确定的。
        #
        # ⚠️ 部分索引（`WHERE status = 'active'`）而不是全表唯一：
        # 解绑后的历史记录要留着（审计需要"这个号什么时候绑过"），
        # 而全表唯一会让"解绑再重绑"直接撞约束。
        Index(
            "uq_ctf2_accounts_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        CheckConstraint(
            "status IN ('active', 'expired', 'revoked')", name="status_valid"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 显示名。用户可改，用来区分"我的号 / 队里的号"。
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="")

    # ── 凭据（★ 密文）───────────────────────────────────────
    # 可空**是刻意的**，虽然"活跃绑定"在语义上必然有 token：
    # 解绑动作会把密文置 NULL（真删掉，而不是留一份没用的密文），
    # 而那时这一行还在（历史）。NOT NULL 会让解绑只能靠删行表达，
    # 而那会连带删掉它下面的赛事与提交记录（CASCADE）。
    token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 可展示掩码，形如 `pat_****9f2a`。手写这一列会被
    # `tests/test_secret_hygiene.py` 那一类测试抓出来。
    token_hint: Mapped[str | None] = mapped_column(String(40), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")

    # ── 这个账号属于哪个平台（v0.25）────────────────────────
    #
    # ⚠️ 可空，且 **NULL 有确切含义**：用内置的 CTF2 驱动。
    #
    # 为什么**不**建外键：外键会让"删掉一个平台配置"牵连到账号（或者反过来
    # 拦住删除），而那两件事在语义上无关 —— 删掉一份自动生成的配置，不该
    # 让一个已经绑好的账号跟着消失。存在性校验放在 service 层做。
    platform_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── CTF2 侧的账号身份 ───────────────────────────────────
    #
    # 绑定成功后从平台回读。**可空**：首次绑定可能刚好撞上平台抖动，
    # 那不该让绑定失败 —— 能拿到 token 就算绑上了，身份下一次同步再补。
    remote_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remote_username: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # 这条 PAT 被平台授予的 scope 列表（JSON 数组）。
    #
    # ★ 存它不是为了鉴权（我们不做本地判权），而是为了**提前告诉用户**
    # "你少一个 scope，点了启动环境会 403"。403 的现场报错虽然带得出
    # 缺失的 scope（见 client.py），但那是**用户已经点了按钮之后**才知道的。
    scopes: Mapped[list | None] = mapped_column(JsonColumn, nullable=True)

    # 最近一次成功与本平台通信的时刻。**必须可空**：从没成功过就是 NULL，
    # 而不是"绑定时刻"—— 后者会让一个从未连通的绑定看起来是健康的。
    last_synced_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # 最近一次失败的原因（中文，可直接展示）。成功时清空。
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    @property
    def has_token(self) -> bool:
        """有没有可用凭据。与 `ProviderAccount.has_key` 同一个模式：
        没有它，"绑了但没 token"与"没绑过"在响应里长得一样。"""
        return bool(self.token_encrypted)

    @property
    def token_mask(self) -> str:
        return self.token_hint or ""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        # ⚠️ 不含凭据、也不含密文。密文不算明文，但它出现在日志里
        # 同样没有好处，且会诱导别人"反正只是密文"—— 那是一条会滑坡的边。
        return f"<Ctf2Account id={self.id!r} label={self.label!r} status={self.status!r}>"


class Ctf2Platform(Base):
    """一个"已接入的平台"：要么是内置驱动，要么是自动生成的配置。

    ## ★ 为什么"平台"要单独成一张表

    在这之前，CTF2 的地址是**写死在代码里**的（`client.DEFAULT_BASE_URL`），
    所以账号表里只存一个 PAT 就够了。而比赛要接的那个平台，地址、认证头、
    端点路径、字段名**全都不同**，且由用户在界面上填 —— 那些东西必须有
    地方放。

    ## `spec` 为 NULL 表示"用内置驱动"

    内置的 CTF2 驱动带着 CTF2 那种畸形逻辑（没有列表端点、要靠公开解法
    侧门去捞题目 id），**表达不成配置**。所以它的 `spec` 是 NULL，由
    `driver` 列指明用哪个驱动。

    ## ⚠️ 凭据**不在这张表里**

    它仍然只住在 `ctf2_accounts.token_encrypted`（模块 docstring 第 25 行
    立的规矩：全模块只有那一处持有可解密的凭据）。

    代价是"生成配置"和"绑定账号"变成**两个动作**：生成时 key 只在内存里
    过一遍；生成好之后用户再点一次启用，那时才把 key 落到账号表。
    多一次点击换的是"凭据只有一个家"—— 这个交换是值得的，因为"到底哪个
    字段是密码"这个问题一旦有了第二个答案，以后每次审计都要重新答一遍。
    """

    __tablename__ = "ctf2_platforms"
    __table_args__ = (
        CheckConstraint("source IN ('builtin', 'generated')", name="source_valid"),
        CheckConstraint(
            "status IN ('building', 'draft', 'verified', 'failed')", name="status_valid"
        ),
        CheckConstraint("driver IN ('ctf2', 'generic')", name="driver_valid"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 稳定标识，唯一。给人看的，也是"是不是同一个平台"的判断依据。
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    # 用哪个驱动：内置的 `ctf2`，还是解释配置的 `generic`。
    driver: Mapped[str] = mapped_column(String(16), nullable=False, default="generic")
    # 声明式配置（`PlatformSpec` 的 dict）。**内置驱动为 NULL** —— 见类 docstring。
    spec: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)

    source: Mapped[str] = mapped_column(String(16), nullable=False, default="generated")
    # building = 后台正在生成（前端轮询这个字段）。
    #
    # ★ 为什么生成要放后台：一轮生成最多三次"探测 + 大模型 + 试跑"，
    #   耗时可能到两分钟。挂在一次 HTTP 请求上必然超时，而超时之后用户
    #   看到的是"失败"，实际后台还在跑 —— 那种不一致最难查。
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    # 最近一次失败原因（中文，可直接展示）。成功时清空。
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 模型最后一次的**原始输出**。留着它：排查"它为什么总也生成不对"时，
    # 唯一能看的证据就是这段原文。
    last_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 生成跑了几轮（1 = 一次就成）。界面显示"试了 2 次"用。
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<Ctf2Platform slug={self.slug!r} driver={self.driver!r} "
            f"status={self.status!r}>"
        )


class Ctf2Competition(Base):
    """一场赛事（或一个练习集）。

    ## `remote_id` 与 `id` 是两件事

    `remote_id` 是 CTF2 侧的 id（形如 `1234`），所有 API 调用都用它；
    `id` 是我们自己的 `ctf2comp_<ULID>`。不合并的理由是**本地主键必须与
    外部系统的 id 空间无关** —— 合并之后，"CTF2 改了 id 规则"或
    "两个平台 id 撞车"都会变成主键冲突，而那是要重建数据的级别。

    唯一约束落在 `(account_id, remote_id)`：**不同账号**可能绑到
    同一个 `remote_id`（比如两个人都绑了同一场公开赛），那是两条记录。

    ## `kind` 为什么分赛事与练习

    CTF2 的赛事（`competitions/`）与练习（`practice/`）**是两套端点、
    两套语义**：赛事有阶段（stages）与提交列表，练习直接是
    "练习集 → 题目"。把两者合成一种会逼着每个调用点都去判断
    "这个 id 是哪种"，而那种判断散在十处就是十次出错机会。

    ⚠️ 已知缺口：环境与提交端点**只挂在 practice 上**，所以赛事类的
    题目目前提交会 404。见 `service._load_challenge_brief` 的说明。
    """

    __tablename__ = "ctf2_competitions"
    __table_args__ = (
        # ⚠️ 这条唯一约束在 `account_id` 改成可空之后**仍然有效但不覆盖
        #    `local`**：PostgreSQL 里 NULL 不等于 NULL，所以两行
        #    (NULL, 'ir-case-001') 不会撞上它。本地案例的唯一性由
        #    `remote_id` 自己保证（建题时用 `new_id()` 现造，见
        #    `service.create_local_competition`）。
        #
        #    这不是"漏了个约束"，而是刻意的：把 `remote_id` 的全局唯一
        #    写进数据库会让"两个账号同步到同一个 remote_id"这件事从
        #    "合法但奇怪"变成"数据库拒绝"，而平台侧的 remote_id 只在
        #    单个账号内有意义。
        UniqueConstraint(
            "account_id", "remote_id", name="uq_ctf2_competitions_account_remote"
        ),
        CheckConstraint(
            "kind IN ('competition', 'practice', 'local')", name="kind_valid"
        ),
        Index("ix_ctf2_competitions_account_kind", "account_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # ★ v0.43：改可空。`kind == 'local'` 的批次（本地复盘案例）不属于
    # 任何平台账号 —— 它没有远端、没有凭据、也不是拉下来的。
    #
    # 为什么不用"挂一个占位的 account 行"绕开 NOT NULL：
    # `uq_ctf2_accounts_active` 是**部分唯一索引**（`WHERE status='active'`），
    # 造占位账号要么占掉"唯一的活跃绑定"，要么造一个 status 不是 active
    # 的假行 —— 两种都是让数据为了满足约束而撒谎。
    #
    # ⚠️ `ondelete="CASCADE"` 保留：解绑账号时，那个账号同步下来的
    #    赛事与题目确实该跟着消失。NULL 的行不受外键影响，本地案例
    #    因此天然免疫解绑操作 —— 这是想要的行为。
    account_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("ctf2_accounts.id", ondelete="CASCADE"), nullable=True
    )
    # CTF2 侧的 id。存字符串而不是整数：平台不同端点的 id 形态不一致
    # （赛事是整数、stage 是字符串），统一成字符串省掉每次调用的转换。
    remote_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="competition")

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    # 平台侧的状态原文（如 `ongoing` / `ended`）。**不枚举**：
    # 这是别人家的取值空间，我们猜不全，硬枚举会让一个没见过的值
    # 把整次同步打回。真正驱动本地行为的是下面的时间窗。
    remote_status: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    start_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # 题目总数。给列表页用，避免"N+1 次 count"。
    challenge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    synced_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    # 平台返回的原始对象。**留一份**是刻意的：CTF2 的响应里有
    # 我们没建模的字段（规则、评分方式、公告），排查"为什么某个行为不对"
    # 时，能看到当时的原始响应比重新调一次 API 可靠（数据可能已经变了）。
    raw: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Ctf2Competition id={self.id!r} remote={self.remote_id!r} name={self.name!r}>"


class Ctf2Challenge(Base):
    """一道题。

    ## ★ 这是整个模块**状态最复杂**的一张表

    `status` 的 7 个取值与它们的理由写在 `vocab.Ctf2ChallengeStatus`
    的 docstring 里，这里不重复。要在这里记的是**两个计数字段的分工**：

    · `wrong_submission_count` —— **外部**后果的计数，只统计真正
      被平台判错的提交（见 `vocab.CTF2_CREDITED_WRONG_STATUSES`）。
      它是自动提交的刹车片，也是"还剩几次机会"的唯一来源。
    · `candidate_count` —— **内部**产出计数，统计我们一共产出了几个
      候选 flag。它回答的是另一个问题："这个 agent 是没解出来，
      还是解出来一堆垃圾"。两个数字都有人看，合并不了。

    ⚠️ `wrong_submission_count` **不能**由"数 submissions 表"算出来。
    理由：提交记录会被保留策略清理（将来），而**额度是平台侧的、
    不会因为本地清理而恢复**。把它存成独立列，意味着清理历史记录
    不会让刹车片失效 —— 那是一处会静默导致超额提交的设计缺陷。
    """

    __tablename__ = "ctf2_challenges"
    __table_args__ = (
        UniqueConstraint(
            "competition_id", "remote_id", name="uq_ctf2_challenges_competition_remote"
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(Ctf2ChallengeStatus))})",
            name="status_valid",
        ),
        CheckConstraint(
            f"sandbox_profile IN ({', '.join(repr(v) for v in values(Ctf2SandboxProfile))})",
            name="sandbox_profile_valid",
        ),
        CheckConstraint("wrong_submission_count >= 0", name="wrong_count_non_negative"),
        CheckConstraint("max_wrong_submissions >= 0", name="max_wrong_non_negative"),
        # ★ 不变量：解出的题必然有**确认过的** flag。放在数据库层而不是
        # service 里，因为它是「数据不可能长成什么样」的定义 —— 与
        # `ck_scans_finished_at_matches_terminal_status` 同一条理由。
        #
        # ⚠️ 刻意写成**单向**蕴含，不是双向等价：
        # `(status = 'solved') = (confirmed_flag IS NOT NULL)` 会拒绝
        # "解出之后有人手工清空 flag"这个**清理动作** —— 而拒绝清理
        # 不是我们要的。反向不成立（存着 flag 但状态退回 unsolved
        # 是合法的："解出来了但我想重跑一遍验证"）。
        CheckConstraint(
            "status <> 'solved' OR confirmed_flag IS NOT NULL", name="solved_implies_flag"
        ),
        # ★ 拆分带来的**新**保证（见 `candidate_flag` 的说明）：
        # 「有一列装着我们并不确定的东西，却表现得像答案」这件事
        # 在数据层就不可能发生。
        CheckConstraint(
            "candidate_flag IS NULL OR confirmed_flag IS NULL",
            name="candidate_never_with_confirmed",
        ),
        # 确认过的 flag 只属于 solved。这条比 `solved_implies_flag` 更强：
        # 它挡住的是"判对了但状态没走到 solved"（比如将来有人补了一个
        # 只写 confirmed_flag 的代码路径）。
        CheckConstraint(
            "confirmed_flag IS NULL OR status = 'solved'", name="confirmed_only_when_solved"
        ),
        # 同步列表与"按状态筛"是主要查询形状。
        Index("ix_ctf2_challenges_competition_status", "competition_id", "status"),
        # 阶段查询（赛事的题目按 stage 分组）。
        Index("ix_ctf2_challenges_stage", "stage_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    competition_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ctf2_competitions.id", ondelete="CASCADE"), nullable=False
    )
    remote_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 赛事才有的阶段 id（`stages/{stageId}/challenges/`）。练习题为 NULL。
    # 可空而不是空串：空串是"一个叫空字符串的阶段"，而 NULL 是"没有阶段"。
    stage_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    # 分类原文（`web` / `crypto` / `reverse` / `misc` / `pwn` …）。
    #
    # ★ **不枚举、不做大小写归一化**：它是"选哪个沙箱镜像"的输入
    # （见 `sandbox_profile`），而镜像的选择应当在**同步时**决定一次并
    # 落库，不该在每次启动时重新解释一遍平台给的字符串（那会让
    # "同一道题昨天用 base、今天用 re"这种不可复现的事发生）。
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 附件清单（JSON 数组，元素形如 `{"name": ..., "url": ...}`）。
    # 存 JSON 而不是关联表：附件**没有独立生命周期**（题目更新就整体替换），
    # 单独一张表会带来"什么时候清理孤儿附件"这种没人想回答的问题。
    attachments: Mapped[list | None] = mapped_column(JsonColumn, nullable=True)

    # 分值 / 难度。可空：练习集常常不给分。
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── 套题支持（已定设计决策里的 `sub_flag_id`）────────────
    #
    # ★ 为什么必须**持久化**这个 id，而不是每次提交时现取：
    #
    # CTF2 的套题（一道题里多个 flag）要求提交时带上 `sub_flag_id`。
    # 如果每次提交都重新调一次详情端点拿它，那么两次调用之间题目配置
    # 一旦被平台改过，第二次会拿到不同的 id —— 而那次提交会被计为
    # **错误提交**（消耗额度）。现取等于把一次外部变更的代价转嫁到
    # 用户的额度上。
    #
    # 存下来的语义是"我们打算提交的那一个位置"，它稳定、可追溯。
    sub_flag_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── 状态机 ────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=Ctf2ChallengeStatus.UNSOLVED.value
    )
    # ── flag：候选与已确认**分两列** ──────────────────────
    #
    # ★ 为什么必须分开（v0.20 的修正）。一个列装两种语义时，
    # 「界面上显示的那个 flag 到底是答案，还是只是它猜的」无法回答。
    # 会真出事的那条路径：
    #
    #   safe 模式落候选 A（界面显示 A）→ 用户确认提交 → 平台判错 →
    #   状态退回未解出，而 A 还挂在题目行上 —— 于是这道题
    #   「没解出却有一个 flag」，而那个字符串看起来完全像答案。
    #
    # · `candidate_flag` —— LLM 提出的、**还没被平台判定**的那一个。
    #   只在 `candidate_ready` 有意义；一旦有了判定（对或错）就清空。
    # · `confirmed_flag` —— **平台判过对**的那一个。只有 `solved` 时存在。
    #
    # ⚠️ 「我们最后试的是哪一个」不再由这里回答 —— `ctf2_submissions`
    #   有每一条提交的完整记录（flag、判定、时间、回执）。那才是权威，
    #   而题目行上留一个字符串只会在下一次提交时过期。
    #   这也是为什么判错时可以放心清空候选（与旧行为相反）。
    candidate_flag: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_flag: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── 提交额度（见类 docstring 的说明）────────────────────
    wrong_submission_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 每题可配。放在题目行上而不是全局设置：不同赛事的额度不一样，
    # 而"这道题我只想试一次"是用户的真实诉求。
    max_wrong_submissions: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── 沙箱与解题 ────────────────────────────────────────
    sandbox_profile: Mapped[str] = mapped_column(
        String(32), nullable=False, default=Ctf2SandboxProfile.BASE.value
    )
    # 解题那一次对应的扫描 id（可空：还没解过）。
    #
    # ⚠️ 这是**跨模块引用**，所以它是裸列、**没有外键** ——
    # 与 `scans` 丢掉 provider 外键那次（迁移 `698537531e16`）同一条理由：
    # 外键会让"删掉一次扫描"变成"删掉这道题的解题记录"，而后者是
    # CTF2 的资产、前者是日常产物。两者的保留期本来就不同。
    scan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    solved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── 调度优先级（v0.23）──────────────────────────────────
    #
    # ## 为什么需要它：比赛的计分规则直接奖励"先做哪道题"
    #
    # 规则原文：
    #
    # > 每道题目前三名成功解题的团队将额外获得本题分值的
    # > **15%、10%、5%** 的奖励分
    # > 参赛队伍可以持续与自行构建的 Agent 进行交互，包括设定任务目标、
    # > **调整解题优先级**、查看运行状态、优化提示词……
    #
    # 前半句意味着"速度直接换分"，后半句说明**调整优先级是赛事方
    # 明确允许并期待的操作**。而原来的认领顺序是纯 FIFO
    # （按 `updated_at`），用户唯一能做的干预是"取消再重排"——
    # 那既不精确，也会把那道题的状态搅乱。
    #
    # ## 语义
    #
    # 越大越先做，**默认 0**。排序是 `priority DESC, updated_at ASC`：
    #   · 同级之内仍然是 FIFO（现有行为不变）；
    #   · 只有用户真的调过优先级的题才会插队。
    #
    # ⚠️ 不设"+10 表示紧急"这类魔法档位，也不自动按分值排：
    #    "先做高分题还是先做容易题"是**战术选择**，取决于当前比分与
    #    队伍策略，系统替用户猜只会猜错。所以它是明确的手动动作，
    #    另附一个"按分值批量设优先级"的便利入口 —— **由用户主动触发**。
    #
    # ⚠️ 不给 `CheckConstraint` 框区间：它不是安全边界，而是纯排序权重，
    #    任何整数都有意义。框住只会让"我想让这题排最前"变成
    #    "先查一下上限是多少"。
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    @property
    def remaining_wrong_submissions(self) -> int:
        """还剩几次错误提交的机会。给界面显示"还能试几次"。

        ★ 由**服务端**算好下发，不让前端做减法：`max - count` 这条规则
        有两份实现迟早会分叉，而分叉的症状是"界面显示还剩 2 次、
        实际提交时被拒"。
        """
        return max(0, int(self.max_wrong_submissions or 0) - int(self.wrong_submission_count or 0))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Ctf2Challenge id={self.id!r} title={self.title!r} status={self.status!r}>"


class Ctf2Environment(Base):
    """一次"启动题目环境"的尝试及其结果（已定设计决策 1）。

    见模块 docstring：它**不被合并进 challenges**，因为"同一道题起过
    三次环境"这件事必须留得下来 —— 排查"为什么一直起不来"靠的正是
    这三次的时间线。

    ## `access_urls` 与 `access_url` 是两个字段

    CTF2 的环境可能给**多个**访问地址（比如同时有 web 入口与一个
    单独的 TCP 服务端口，或者多个副本）。`access_url` 是**首选**那个
    （单值，给"点一下就能打开"用），`access_urls` 是全部（给"要在
    沙箱里逐个试"用）。

    只存复数会让每个用它的地方都得自己写一遍"取第一个"；
    只存单数会丢掉其余入口 —— 而多入口题目恰好是那些"web 打不动、
    真正的洞在另一个端口"的题。
    """

    __tablename__ = "ctf2_environments"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(Ctf2EnvironmentStatus))})",
            name="status_valid",
        ),
        CheckConstraint(
            f"sandbox_profile IN ({', '.join(repr(v) for v in values(Ctf2SandboxProfile))})",
            name="sandbox_profile_valid",
        ),
        # ★ 不变量：只有 `ready` 才允许有 access_url。
        #
        # 这条挡的是一个非常具体的错误：轮询超时后把"最后一次看到的
        # access_url"留在行上，于是界面显示一个可点的链接，而那个环境
        # 根本没起来 —— 用户点进去是连接断开，然后来问"你们的链接坏了"。
        # 宁可显示"环境未就绪、无地址"。
        CheckConstraint(
            "status = 'ready' OR access_url IS NULL", name="access_url_only_when_ready"
        ),
        CheckConstraint("poll_count >= 0", name="poll_count_non_negative"),
        # 查"这道题当前有没有活着环境"是最热的查询（每次启动前都要判）。
        Index("ix_ctf2_environments_challenge_status", "challenge_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    challenge_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ctf2_challenges.id", ondelete="CASCADE"), nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Ctf2EnvironmentStatus.STOPPED.value
    )
    sandbox_profile: Mapped[str] = mapped_column(
        String(32), nullable=False, default=Ctf2SandboxProfile.BASE.value
    )

    access_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_urls: Mapped[list | None] = mapped_column(JsonColumn, nullable=True)

    # 我们侧沙箱容器的 id（Docker）。可空：环境启动了但沙箱还没起
    # （或者用的是宿主直连模式）。
    #
    # ★ 存它是为了**能清理**：进程崩了之后，一个没人记得 id 的容器
    # 会一直占着资源，而"僵尸容器"没有界面能看到。
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 轮询统计。它是排查"启动慢"的唯一数据来源 ——
    # 没有它，只能看到"用了 40 秒"，而不知道是轮了 3 次还是 80 次。
    poll_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # 平台侧给的到期时刻（有租期时）。可空 = 不知道什么时候到期。
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Ctf2Environment id={self.id!r} status={self.status!r}>"


class Ctf2SolveAttempt(Base):
    """**一次解题尝试的不可变策略快照。**

    ## ★ 为什么必须有这张表（2026-10-11 补）

    在这之前，`POST /ctf2/challenges/{id}/solve` 的 `mode` 参数
    **被静默丢弃** —— `service.start_solve` 收下它之后直接 `del mode`
    （那里有注释解释"策略不存进 challenge"），而 worker 直到真正开跑
    才现读**当时的全局设置**。

    后果不是"参数没生效"这么轻。它是一条真实的时序漏洞：

      · 用户点解题时选 `safe`（明确表示"我要人工确认"）；
      · 题目排队等待（并发上限 3，前面有题在跑）；
      · 这期间**任何人**把全局设置改成 `auto`；
      · worker 启动时读到的就是 `auto` —— 于是**系统替用户做了一个
        他明确拒绝过的对外提交**，消耗的是**不可再生**的错误次数额度。

    同一个坑还吃掉了另外两个受理期输入：
      · `brief`（人工补充的线索）—— schema 收了、router 没转交、
        service 没字段、prompt 里没有位置，**一个字都进不去 solver**；
      · `max_turns` —— 与 `mode` 在 `build_spec` 里是相邻两行、
        共用同一个 `settings_row`，所以排队期间改预算同样会漂移。

    ## 为什么是"快照"而不是"把字段加到 challenge 上"

    这些值是**这一次尝试**的输入，不是题目的属性：
      · 同一道题第二次解可能换 `mode`（上次 safe 没确认，这次想 auto）；
      · 换 `brief`（补了新线索）；
      · 换模型（上次那个模型没解出来）。

    存到 challenge 上会让"上一次用什么策略"被下一次覆盖 ——
    而事后复盘"它当时是按什么策略跑的"正是最需要能回答的问题。
    与 `Ctf2Environment` 存"同一道题起过三次环境"是同一条理由。

    ## ⚠️ 落库之后**不可变**

    写进去就不再改。worker 读它、不再读全局设置 —— 这就是这个类
    存在的全部意义。要换策略就再受理一次（新的一行）。

    与 `Ctf2Submission` 的"只增不改"是同一条纪律，但方向相反：
    提交记录记的是**对外动作**，本表记的是**内部输入**。
    """

    __tablename__ = "ctf2_solve_attempts"
    __table_args__ = (
        # 取值合法性。与 `Ctf2Submission.mode_valid` 同一个理由：
        # 这两列会进审计与复盘，写进一个拼错的字符串会让
        # "这次到底按什么策略跑的"变成读不出来的东西。
        CheckConstraint(
            f"mode IN ({', '.join(repr(v) for v in values(Ctf2SubmitMode))})",
            name="solve_attempt_mode_valid",
        ),
        CheckConstraint(
            f"sandbox_profile IN ({', '.join(repr(v) for v in values(Ctf2SandboxProfile))})",
            name="solve_attempt_profile_valid",
        ),
        # 回合数必须有下界：0 或负数意味着"受理了但一轮都不跑"，
        # 那是配置错误而不是预算选择（下界与 `MIN_CTF2_MAX_TURNS` 一致）。
        CheckConstraint("max_turns >= 1", name="solve_attempt_turns_positive"),
        # "这道题最近几次尝试"是最常见的查询（界面要显示上次用了什么策略）。
        Index("ix_ctf2_solve_attempts_challenge_at", "challenge_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    challenge_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ctf2_challenges.id", ondelete="CASCADE"), nullable=False
    )

    # ── 受理时定下的策略（worker 只读这里）──────────────────
    #
    # ⚠️ 默认值只用于"历史行/内部调用没给值"的兜底，**不是**运行时
    #    默认 —— 真正的默认来自受理时的 `effective_ctf2_settings`。
    #    这与 `models.Scan.goal` 那条"NOT NULL 但不给数据库默认"的
    #    取舍不同：那里漏填会静默变成某个值，这里漏填会被下面
    #    `service.start_solve` 的显式解析挡住。
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Ctf2SubmitMode.SAFE.value
    )
    max_turns: Mapped[int] = mapped_column(Integer, nullable=False)
    sandbox_profile: Mapped[str] = mapped_column(String(32), nullable=False)

    # 用户补充的线索（"考点在反调试"）。上限与 `scans.brief` 一致（8000）。
    #
    # ⚠️ 它**可能含敏感信息**（用户会写"口令是 xxx"）。但与 flag 同理：
    #    它本来就要进 prompt、被模型读到；加密它只会让"这次带了什么线索"
    #    在复盘时读不出来。所以明文，且**不进活动流的 summary** ——
    #    与 `events.emit_candidate_ready` 把 flag 放进 detail 而非 summary
    #    是同一条纪律（summary 会显示在列表里）。
    brief: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── 这次尝试用的是哪个模型 ────────────────────────────────
    #
    # ★ 存下来而不是"用时再查 provider"：provider 配置会被改
    #   （换模型、删掉再建一个），而"这次尝试用的到底是哪个"必须与
    #   当时一致 —— 否则复盘时"为什么这次没解出来"永远说不清，
    #   因为看到的模型可能已经不是当时那个了。
    #   与 `Ctf2Submission.sub_flag_id` 复制而非引用是同一条理由。
    #
    # 可空：受理时若还没配 provider，`build_spec` 会抛错并把题目置
    # failed —— 那时这行仍然是"受理过的证据"，不该因为没模型就不落库。
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # 是不是续解（用户在界面上明确选的）。它同样是**这一次**的输入，
    # 所以和策略放在一起 —— 与 `_resume` 那个内存集合不同，落库之后
    # 重启也不会丢（见 `scheduler` 的说明：重启把孤儿置 unsolved 后，
    # 那条"这次是续解"的信息原本会随进程一起消失）。
    resume: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Ctf2SolveAttempt id={self.id!r} challenge={self.challenge_id!r} "
            f"mode={self.mode!r} turns={self.max_turns}>"
        )


class Ctf2Submission(Base):
    """一条 flag 提交记录。**只增不改。**

    ## 为什么"只增不改"是一条硬规矩

    它是这件事的**证据链**："这个 flag 是什么时候、由谁（人还是自动）、
    以什么模式提交的，平台怎么回的"。任何对历史行的修改都会让这条链
    失去意义 —— 而它恰恰是事后争议（"我没提交过这个"、
    "额度怎么用掉的"）时唯一能拿出来的东西。

    状态流转（`pending` → `correct`/`wrong`）是**追加语义**：
    它是在把一条已经发出的请求补上它的结果，不是在改历史。
    所以 `pending` 行只能被**推进到终态**，不能改回 `pending`、
    也不能改 flag 内容。

    ## ★ `mode` 必须落库

    同一次提交是"人点了确认"还是"auto 模式自动发的"，责任归属完全不同。
    不记它，事后就只能靠猜 —— 而"这个 flag 是系统自己发出去的"这件事
    在有争议时是关键事实。
    """

    __tablename__ = "ctf2_submissions"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(Ctf2SubmissionStatus))})",
            name="status_valid",
        ),
        CheckConstraint(
            f"mode IN ({', '.join(repr(v) for v in values(Ctf2SubmitMode))})",
            name="mode_valid",
        ),
        # ★ 不变量：`correct` 的提交必然有平台回执 id。
        #
        # 这条挡的是"把一次请求成功当成判题成功"。提交请求返回 200
        # 只说明**请求收到了**，判题结果在响应体里（或另一个端点）。
        # 没有回执就标成 `correct`，等于凭空宣称解出了一道题 ——
        # 而那道题下次同步会变回未解出，用户会觉得"系统在骗我"。
        CheckConstraint(
            "status <> 'correct' OR remote_submission_id IS NOT NULL",
            name="correct_implies_receipt",
        ),
        # "这道题提交过什么"是最常见的查询。
        Index("ix_ctf2_submissions_challenge_at", "challenge_id", "submitted_at"),
        # 查待确认的（`pending`）—— 重试逻辑与对账都要用它。
        Index("ix_ctf2_submissions_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    challenge_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ctf2_challenges.id", ondelete="CASCADE"), nullable=False
    )

    # ⚠️ **明文 flag 落库**，这是刻意的，且与凭据是**相反**的取舍。
    #
    # 凭据必须加密（泄漏后果是"替用户发包"）；而 flag 是**这次任务的结果**，
    # 它本来就要显示在界面上、写进报告里、被用户复制走。加密它只会让
    # "这个提交发的是什么"在排查时读不出来 —— 而付出的是零安全收益：
    # 任何能看到这条记录的人，本来就能看到那道题的结果页。
    flag: Mapped[str] = mapped_column(Text, nullable=False)
    # 套题位置（提交时用的那个，从 challenge 复制过来）。
    # 复制而不是引用：题目配置会被改，而**当时提交的是哪个位置**
    # 必须与当时一致 —— 这是证据链的一部分。
    sub_flag_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Ctf2SubmissionStatus.PENDING.value
    )
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Ctf2SubmitMode.SAFE.value
    )

    # 平台回执。见 `correct_implies_receipt` 约束。
    remote_submission_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 平台原话（判错时常常给一句提示，比如"flag 格式应为 flag{...}"）。
    # 它是**给用户看的**，所以原样保留、不翻译。
    remote_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 我们自己的失败说明（网络、权限、本地拦截）。与 `remote_message` 分开，
    # 因为"谁说的话"决定了用户该找谁 —— 一句混在一起的错误会让人
    # 去找平台客服，而问题其实在本地。
    local_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    def __repr__(self) -> str:  # pragma: no cover
        # ⚠️ repr 不含 flag 本身：它会进日志与异常消息，而
        # "提交过什么"在日志里没有价值 —— 记录里有，且那才是有权限的读路径。
        return f"<Ctf2Submission id={self.id!r} status={self.status!r} mode={self.mode!r}>"


class Ctf2SolveNote(Base):
    """一道题的**解题方法卡**（v0.32）—— 跨题复用的最小单位。

    ## 为什么需要它

    在这之前，每次解题都从零开始：

      · `resume` 只读**同一道题**的 transcript；
      · CTF2 的 `/writeups/` 端点只被用来提取 challenge_id 做同步，
        解法内容本身**没有被用于解题**；
      · 于是"同类题第二次遇到"仍然要重新摸索一遍。

    ## ★ 它存什么、不存什么（这条比字段更重要）

    **存方法，不存答案。**

    | 存 | 不存 |
    | --- | --- |
    | 题型与关键工具（`tools`） | **flag / 答案原文** |
    | 有效的步骤梗概（`approach`） | 完整的题面 |
    | 试过但走不通的方向（`dead_ends`） | 未脱敏的原始 transcript |

    ⚠️ **flag 绝不入库**。理由不是"flag 是秘密"（它本来就在
    `ctf2_challenges.confirmed_flag` 里、也进报告），而是**注入路径的
    安全性**：这些卡片会被拼进**另一道题**的 prompt。一旦带上旧 flag，
    就变成"把 A 题的答案泄漏到 B 题的上下文里"—— 而 B 题的模型
    完全可能把它当成 B 题的答案交上去。

    所以写入时**逐字段脱敏**（见 `service.learn_from_solve`），
    而不是"相信上游传进来的东西已经干净"。

    ## 为什么用"方法卡"而不是直接塞 transcript

    transcript 动辄 60–115 KB，绝大部分是工具原始输出（见
    `digest_from_transcript` 的说明）。塞回去会把预算吃光，
    然后模型又开始"忘记题目要求" —— 那把"复用"变成了"更糟的重来"。
    """

    __tablename__ = "ctf2_solve_notes"
    __table_args__ = (
        # 一道题一张卡。重解时**更新**而不是新增 —— 与
        # `ctf2_solve_attempts`（每次尝试一行）是相反的设计，因为
        # 它们回答的问题不同：
        #   · attempt 回答"这一次按什么策略跑的"（历史，只增）；
        #   · note    回答"这道题怎么解"（知识，取最新）。
        UniqueConstraint("challenge_id", name="uq_ctf2_solve_notes_challenge"),
        # 召回时按分类 + 解出状态筛，所以这两列要能走索引。
        Index("ix_ctf2_solve_notes_category", "category"),
        Index("ix_ctf2_solve_notes_solved", "solved"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    challenge_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ctf2_challenges.id", ondelete="CASCADE"), nullable=False
    )

    #: 题型（从 challenge 复制，便于跨题召回时不必 join）。
    #:
    #: ⚠️ 复制而不是引用：题目行会被删（同步会清理），而"这道题的方法"
    #: 在它被删之后仍然有参考价值 —— 只是那时我们靠 challenge_id 的
    #: CASCADE 一起删掉。复制一份让召回查询不必 join。
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    #: 这道题最后**有没有解出**。召回时优先给"解出过"的卡。
    #:
    #: ⚠️ 失败的卡也有价值（`dead_ends` 能让新题少走弯路），
    #: 所以它们不删，只是排在后面。
    #:
    #: ## ★ v0.40：它的含义**变宽**了，但仍然是"值不值得优先召回"
    #:
    #: 原来它等于 `challenge.status == SOLVED`。在智能体安全评测里，
    #: "跑完全套测试手法后确认不存在"（`ruled_out`）的获取成本与
    #: "命中"（`confirmed`）相当 —— 见 `verdict` 列的说明。
    #: 所以现在两者都算 `solved=True`（都值得优先召回）。
    #:
    #: ⚠️ 判**性质**请读 `verdict`，不要从这个布尔值倒推 —— 它已经被
    #:    故意合并了两种情况。
    solved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: 这张卡是**怎么解决的**（v0.40）。取值见 `vocab.AgentsecVerdict`。
    #:
    #: ## ★ 为什么不能只靠 `solved: bool`
    #:
    #: `solved` 回答"值不值得优先召回"（排序用），而这一列回答
    #: "是哪一种解决"。两者**不能互相推导**：
    #:
    #:   · `confirmed`    —— 命中。卡教人"怎么打中"；
    #:   · `ruled_out`    —— 排除。卡教人"这条路是死的"，附具体反证；
    #:   · `needs_follow_up` —— 待跟进（默认档）。
    #:
    #: 前两者都是 `solved=True`，但复用方式完全相反：一张讲"怎么命中"
    #: 的卡拿去当"已经排除"用，会让模型跳过本该做的试探 —— 那正是
    #: 场景包 5.3 节列的"分级禁区"里最严重的一种。
    #:
    #: ⚠️ 默认 `needs_follow_up` 而不是 `confirmed`：拿不准就说拿不准。
    verdict: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DEFAULT_AGENTSEC_VERDICT
    )

    #: 方法梗概（**已脱敏**）。几句话，不是全文。
    approach: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: 用到的关键工具/命令名（逗号分隔）。给"这道题要什么工具"用。
    tools: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: 试过但走不通的方向（**已脱敏**）。这是卡片里最省时间的部分 ——
    #: 它让新题不必重走同一条死路。
    dead_ends: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    def __repr__(self) -> str:  # pragma: no cover
        # ⚠️ 不含 approach/dead_ends 正文：它们会进日志，
        # 而卡片内容可能很长。id 与 category 足够定位。
        return (
            f"<Ctf2SolveNote challenge={self.challenge_id!r} "
            f"category={self.category!r} solved={self.solved}>"
        )


class Ctf2SolveUsage(Base):
    """**一次 LLM 调用的 token 用量**（v0.36）。

    ## ★ 为什么是"每次调用一行"而不是在尝试上加累计列

    需求要的是"细分到每一次任务的消耗"，而"每一次任务"有两种读法：
    每次**解题尝试**、每次**LLM 调用**。

    独立表**同时满足两者** —— 按 `attempt_id` 聚合即得前者。
    反过来，只存累计列就**永久丢失了明细**，而明细能回答的问题
    累计值一个都答不了：

      · 哪一轮最贵（是不是在某一轮烧掉了一半预算）；
      · 是不是在**兜圈子烧钱**（连续多轮 token 高但没进展）；
      · 换模前后各花了多少（v0.35 的 fallback 会让同一道题用两个模型）。

    代价是每轮多一次 INSERT。相对一次几百毫秒的 LLM 调用可以忽略。

    ## ★★ `usage_reported` 这一列是必须的（别删）

    接入点**可能不给 `usage`**（第三方代理、某些本地模型）。那时
    token 记 0 —— 而"真的是 0 token"与"没记录到"在界面上长得一模一样。

    所以单开一列布尔值区分。这与 `providers` 的 `hasKey` / `apiKey`
    分两列、`Ctf2Submission` 的 `candidateFlag` / `confirmedFlag`
    分两列是同一条纪律：**两种语义不能共用一个"空"**。

    不这么做的后果很具体：用户看到"某次解题消耗 0"，要么以为它没跑过，
    要么以为那次特别省 —— 两种都是错的。

    ## 实测的字段口径（2026-10-14，写在这里免得再测一遍）

    | 字段 | 简单问答 | 推理型 | 能用吗 |
    | --- | --- | --- | --- |
    | `prompt_tokens` | 37 | 75 | ✅ |
    | `completion_tokens` | 44 | 904 | ✅ |
    | `total_tokens` | 81 | 979 | ✅ |
    | `input_tokens` | 37 | 75 | ⚠️ 与 prompt 重复 |
    | **`output_tokens`** | **0** | **0** | ★ **恒为 0，不可用** |
    | `reasoning_tokens` | 0 | **0** | ⚠️ 实测不给，不可依赖 |

    ⚠️ 那个网关同时给 `usage_semantic: "openai"` 与
    `usage_source: "anthropic"`（自建网关转译的特征）。照 Anthropic
    那套读 `input_tokens`/`output_tokens` 会**静默少算全部输出**。

    ⚠️ 推理型那次 `completion_tokens=904` 而 `content` 只有 280 字
    —— 说明**推理开销已计入 completion**，不必单加 `reasoning_tokens`。
    仍然留一列备查（别的接入点可能会给）。
    """

    __tablename__ = "ctf2_solve_usages"
    __table_args__ = (
        # 轮数从 1 起。0 轮意味着"一次不存在的调用"。
        CheckConstraint("turn >= 1", name="solve_usage_turn_positive"),
        # 负数只可能是解析错误（或上游给了脏数据）。
        # 与其让它污染统计，不如让写库直接失败。
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
            name="solve_usage_tokens_non_negative",
        ),
        # "这次尝试的逐轮明细"是最热的查询（详情页展开）。
        Index("ix_ctf2_solve_usages_attempt_turn", "attempt_id", "turn"),
        # 按题聚合（题解里要写"这道题一共花了多少"）。
        Index("ix_ctf2_solve_usages_challenge_at", "challenge_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ctf2_solve_attempts.id", ondelete="CASCADE"), nullable=False
    )
    # ⚠️ 冗余存 challenge_id（attempt 已经能关联到题）：题解与总览页
    #    都要"按题聚合 token"，而只走 attempt 需要多一次 JOIN。
    #    与 `Ctf2SolveNote.category` 复制而非引用是同一条取舍。
    challenge_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ctf2_challenges.id", ondelete="CASCADE"), nullable=False
    )

    turn: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── 这次调用是谁 ────────────────────────────────────────
    #
    # ⚠️ 存 model 而不是只存 provider_id：v0.35 的换模 fallback 会让
    #    **同一次尝试**用两个模型，而"换模前后各花多少"必须能算出来。
    model: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    provider_label: Mapped[str] = mapped_column(String(120), nullable=False, default="")

    # ── token 数（接入点给的，见类 docstring 的口径表）────────
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 推理 token。**实测该接入点恒为 0**，留列备查。
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: ★ 接入点**到底给没给** usage。见类 docstring 的说明。
    #:
    #: False 时上面四个 token 列全是 0，而那个 0 **不代表没消耗**。
    usage_reported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Ctf2SolveUsage attempt={self.attempt_id!r} turn={self.turn} "
            f"total={self.total_tokens} reported={self.usage_reported}>"
        )


class Ctf2Writeup(Base):
    """一道题的**题解（Writeup）**（v0.36）—— 赛后提交的依据。

    ## 为什么 CTF2 需要自己一份（而不是复用 scans 的）

    `scans/writeup.py` 拼的是**strix 报告**的重排版。而 CTF2 解题
    **根本不走 strix** —— 它由 `app/kernel/runner.py` 自己的循环驱动，
    从不产生 `penetration_test_report.md`。

    所以扫描的题解端点对 CTF2 的题**永远**找不到东西，而它给出的
    错误信息（"产物目录可能被手工清理过"）在误导用户去找一个
    根本不该存在的文件。详见契约 §3.19.5。

    ## 内容从哪来

    `SolveOutcome.transcript`（每轮的命令 + 输出摘要）+ 题目信息
    + flag + 轮数/耗时/token 统计。拼装逻辑在 `ctf2/writeup.py`。

    ## ★ 一题一份（`UNIQUE`），不是每次尝试一份

    题解是**给赛后提交用的**，一份就够。多次尝试产多份会让"提交哪一份"
    变成新问题。重解时**更新**（与 `Ctf2SolveNote` 的 upsert 同理）。

    ## ★ 未解出也产出

    需求原话是"这是赛后需要提交的依据"。未解出的题同样需要一份记录
    ——"我们试过什么、卡在哪"本身就是可提交的材料，也是复盘依据。
    所以 `solved` 是一个**字段**而不是"要不要建这一行"的判据。

    ## ★★ flag **不脱敏** —— 与 `Ctf2SolveNote` 恰好相反

    这个差别必须写下来，否则后来者会"顺手统一"而把其中一边改坏：

    | | `Ctf2SolveNote`（方法卡） | `Ctf2Writeup`（题解） |
    | --- | --- | --- |
    | 去哪 | **另一道题**的 prompt | 赛后提交 / 给人看 |
    | 带 flag 的后果 | **泄漏**（B 题可能把 A 题的答案交上去） | 正确 —— flag 正是要展示的 |
    | 所以 | 白名单式脱敏 | **原样保留** |

    判据是**这份内容的去向**，不是"flag 是不是秘密"。
    """

    __tablename__ = "ctf2_writeups"
    __table_args__ = (
        # 一道题一份。重解时**更新**而不是新增。
        UniqueConstraint("challenge_id", name="uq_ctf2_writeups_challenge"),
        Index("ix_ctf2_writeups_generated_at", "generated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    challenge_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ctf2_challenges.id", ondelete="CASCADE"), nullable=False
    )

    #: 这份题解出自哪一次尝试。
    #:
    #: ⚠️ `SET NULL` 而不是 CASCADE：尝试记录被清掉时，题解**仍然有价值**
    #:    （它是给人看的成品），不该跟着消失。而"出自哪次尝试"丢了大不了。
    attempt_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("ctf2_solve_attempts.id", ondelete="SET NULL"), nullable=True
    )

    #: 题解全文（markdown）。**含 flag 原文**，理由见类 docstring。
    markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: 产出时的状态。未解出的题解同样保留（见类 docstring）。
    solved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: 产出时的轮数与耗时 —— 题解头部要写"跑了多少轮、花了多久"。
    #:
    #: ⚠️ 冗余存（attempt 与 scan 都有类似信息）：题解是**一份独立成品**，
    #:    它应当自带这些数字，而不是渲染时再去别处查 —— 否则那些行
    #:    一旦被清理，题解头部就出现空白。
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    elapsed_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)

    generated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=now_utc, onupdate=now_utc
    )

    def __repr__(self) -> str:  # pragma: no cover
        # ⚠️ 不含 markdown 正文：它会进日志，而题解可能有几千行。
        return (
            f"<Ctf2Writeup challenge={self.challenge_id!r} solved={self.solved} "
            f"len={len(self.markdown or '')}>"
        )


class Ctf2SolveTelemetry(Base):
    """**一次解题的横向指标快照**（v0.45）—— 生产遥测。

    ## 为什么需要这张表（这是它存在的唯一理由）

    `ctf2_writeups` 已经有了 `turns` 与 `elapsed_sec`，`ctf2_solve_usages`
    已经有了逐轮 token。缺的是**回答"这次补强有没有用"所必需的那三个数**：

      · 回合数（`turns`）—— 对话轮了几次；
      · **工具调用数**（`tool_calls`）—— 一回合可以下发多条命令，
        所以回合数只反映了一半的"兜圈子"；
      · **方法卡张数**（`notes_count`）—— 这次到底参考到了几张别题的解法。

    后两个数原先**哪里都没有**：`SolveOutcome` 里有 `tool_outputs`
    （完整输出原文，为了做答案锚定）却没人数个数，`_recall_notes` 只把
    张数打进一行 `logger.info` 就扔了。于是"方法卡值不值"这个问题
    永远只能靠**人工造基准题**去答 —— 而实测那样做测不出效应
    （见 README「跨题方法卡」那一节）。

    真实比赛里这三个数本来就会自然产生。落下来之后，"模型明显在兜圈子"
    的题会自己浮出来，不必等到有人手工去翻 transcript。

    ## ★ 为什么是独立表，而不是往 `ctf2_writeups` 加列

    | | `Ctf2Writeup` | 本表 |
    | --- | --- | --- |
    | 行数/题 | **一题一行**（`UNIQUE`，重解时更新） | **一次解题一行** |
    | 是谁的产物 | 赛后提交的**成品** | 运行的**观测样本** |

    关键在行数：统计要的是**样本**。"这道题解了 5 次"在题解表里永远只有
    1 行，5 次尝试的 4 次被覆盖掉 —— 而那 4 次恰恰是"后来者为什么更快"
    的证据。写进题解表等于把样本数永久压成 1。

    ## ★ 为什么 `attempt_id` 是 `SET NULL` 而 `challenge_id` 是 `CASCADE`

    与 `Ctf2Writeup` 的同名取舍是**同一个理由**但结论相反，值得写清：

    - attempt 快照被清理时，这条观测**仍然有价值**（它说的是"这道题解了
      几次、各花多少轮"），所以 `SET NULL` 让它活下来；
    - 题被删掉时，它的观测也跟着没意义了，所以 `CASCADE`。

    ## ★ `category` 为什么冗余存一份

    统计要按分类切 —— 方法卡本来就是**同分类**召回的，混着算会把
    "Web 题不需要卡"和"Crypto 题有卡也没用"搅成一团。而
    `challenge.category` 是同步时会改的字段（平台侧改名/改分类），
    一旦被改，历史观测的分类就跟着漂了。冗余一份，让"当时的分类"
    和"当时的张数"来自同一个快照。

    ## 不存什么

    不存 `elapsed_sec`：它的来源是**借壳的 `Scan.duration_sec`**
    （`Ctf2Challenge` 自己没有耗时列），而那个值在 `finalize_solve_scan`
    之后才存在 —— 本表在 `_record_outcome` 里写，那时它还是 `None`。
    耗时列在 `ctf2_writeups` 里已有，且实测没有分辨力
    （宿主负载能把它放大三倍），不为它在这里再造一处不一致的读数。
    """

    __tablename__ = "ctf2_solve_telemetry"
    __table_args__ = (
        CheckConstraint(
            "turns >= 0 AND tool_calls >= 0 AND notes_count >= 0",
            name="solve_telemetry_non_negative",
        ),
        # "这道题的历次观测"（详情页/复盘按题看）。
        Index("ix_ctf2_solve_telemetry_challenge_at", "challenge_id", "created_at"),
        # "按分类算均值"（回答"方法卡在哪类题上才有用"）。
        Index("ix_ctf2_solve_telemetry_category_at", "category", "created_at"),
        # "按细分类型算均值"（回答"带 heap 题的遥测只占多少"）。
        Index("ix_ctf2_solve_telemetry_subcategory_at", "subcategory", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    challenge_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("ctf2_challenges.id", ondelete="CASCADE"), nullable=False
    )
    #: 出自哪一次尝试。`SET NULL`：快照被清理后观测仍然算数。
    attempt_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("ctf2_solve_attempts.id", ondelete="SET NULL"), nullable=True
    )

    #: 当时的分类（见类 docstring：防止同步改分类让历史统计漂移）。
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    #: 这次解出没解出 —— 所有统计都要能按它分组。
    solved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 真正执行过的工具调用次数（不含被 `MAX_ACTIONS_PER_TURN` 丢掉的动作）。
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 注入的同类题方法卡张数。0 表示"没有召回"（分类为空/库里没有/召回失败）。
    notes_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: ★ v0.46：这次解题**真正用到的工具名**（去重、保序、逗号分隔）。
    #:
    #: ## 为什么要存工具名，而不只存 `subcategory`
    #:
    #: 回答"要不要上 heap 向工具"需要的是"真实比赛里 heap 题多不多、
    #: 模型在上面卡不卡"。而 `subcategory` 是**我们自己推导**
    #: 出来的结论 —— 推导规则一定会改。只存结论的话，规则改了就得
    #: 重跑生产才能重算；而存了工具名，新规则可以直接在历史数据上
    #: 重新跑一遍。这正是 `turns`/`tool_calls` 当初没记、后来无法回填
    #: 的同一个坑。
    #:
    #: ## 只记**工具名**，不记命令与参数
    #:
    #: 命令里有 flag、有目标 URL、有完整路径。遥测不是事故记录；
    #: 而"用了 gdb/cyclic 还是用了 objdump/nc"已经足够区分技术路线。
    #: 反向从 transcript 提取时走的是生产判据
    #: `notes.tools_from_transcript`（按词边界从命令正文里认）。
    tool_kinds: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    #: ★ v0.46：从 `tool_kinds` **推导**出的细分题型（`pwn-stack` / `pwn-heap` / `pwn-fmt` / ``）。
    #:
    #: ## 为什么平台给不出这个值
    #:
    #: 实测（`app/kernel/service.py` 同步段）：`_pick(payload, "category", "type")` 是
    #: **唯一**分类来源，平台 payload 里没有 `subcategory`/`subtype` 之类的键。
    #: 所以细分只能从"模型实际做了什么"反推 —— 也只有那个能回答
    #: "要不要投 heap 工具"。
    #:
    #: 空串 = 推不出来（不是"普通 pwn"）。这一区分很重要：
    #: 把"没证据"当成"一小类"会让分类统计里多出一个不存在的类。
    subcategory: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    #: ★ v0.47：这次解题**有没有带**越权与注入手法块（生产遥测）。
    #:
    #: ## 为什么必须单独记一列
    #:
    #: 回答"那个开关到底有没有用"要靠**按开关切分**历史遥测，而
    #: `solved` / `turns` / `tool_calls` 只说"结果"，不说"当时是什么条件"。
    #: 开关是全局设置、随时可能被改，所以事后无法按时间窗倒推 ——
    #: 一旦中途改过，倒推出来的两组就会互相污染，且没有任何信号提示污染。
    #:
    #: ## 记的是"块真的进了提示词"，不是"开关当时是开的"
    #:
    #: 两者不等：`agentsec-*` / `ir-*` 七类即使开关开着也**按前缀排除**，
    #: 提示词里没有这个块。真正决定"带不带"的是 `SolveSpec.offensive`
    #: （开关 ∧ 非场景题，见 `runner.build_system_prompt`），所以这里记的就是
    #: 它 —— 只有这样切出来的两组才互斥、没有歧义。
    offensive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=now_utc)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Ctf2SolveTelemetry challenge={self.challenge_id!r} "
            f"solved={self.solved} turns={self.turns} "
            f"tools={self.tool_calls} notes={self.notes_count} "
            f"sub={self.subcategory!r}>"
        )


__all__ = [
    "CTF2_API_PREFIX",
    "CTF2_OFFICIAL_HOSTS",
    "Ctf2Account",
    "Ctf2Challenge",
    "Ctf2Competition",
    "Ctf2Environment",
    "Ctf2SolveAttempt",
    "Ctf2SolveNote",
    "Ctf2SolveTelemetry",
    "Ctf2SolveUsage",
    "Ctf2Submission",
    "Ctf2Writeup",
]
