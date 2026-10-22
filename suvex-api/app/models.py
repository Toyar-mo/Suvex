"""所有 ORM 模型的汇合点。

存在的理由只有一个：**Alembic 的 autogenerate 靠 `Base.metadata`，
而 metadata 只包含「已经被导入过」的模型类。**

模型分散在各领域包里，如果没人导入它们，`Base.metadata` 就是空的 ——
于是 `alembic revision --autogenerate` 会兴高采烈地生成一个
**把全部表都删掉的迁移**。这不是假想的风险：它是 autogenerate 最常见的
事故之一，而且表现是「迁移成功执行了，数据没了」。

所以：env.py 与测试都从这里导入，不逐个 import 领域包。
加新模型时在下面加一行 —— 漏了的话，`scripts/verify_schema.py` 会报出来
（它比对 metadata 里的表数与迁移文件里出现的表名）。
"""

from __future__ import annotations

from app.audit.models import AuditLog  # noqa: F401

# noqa: F401 是必要的：这些名字只为了副作用（注册到 Base.metadata）而导入，
# 静态检查会认为它们是未使用。
from app.auth.models import AdminAccount, ApiToken, AuthSession  # noqa: F401
from app.events.models import ActivityEvent, Agent  # noqa: F401
from app.findings.models import Finding, FindingTimelineEntry  # noqa: F401
from app.kernel.models import (  # noqa: F401
    Ctf2Account,
    Ctf2Challenge,
    Ctf2Competition,
    Ctf2Environment,
    Ctf2Submission,
)
from app.members.models import Member  # noqa: F401
from app.providers.models import ProviderAccount  # noqa: F401
from app.reports.models import Report, ReportFinding  # noqa: F401
from app.scans.models import Scan  # noqa: F401
from app.settings.models import (  # noqa: F401
    Ctf2Config,
    FindingSlaConfig,
    NoticeRule,
    NotifyChannel,
    SandboxConfig,
    ScanRetentionConfig,
    ScanView,
)

__all__ = [
    "AdminAccount",
    "AuthSession",
    "ApiToken",
    "AuditLog",
    "ProviderAccount",
    "Scan",
    "Finding",
    "FindingTimelineEntry",
    "Report",
    "ReportFinding",
    "Agent",
    "ActivityEvent",
    "SandboxConfig",
    "NoticeRule",
    "NotifyChannel",
    "ScanRetentionConfig",
    "ScanView",
    "Member",
    "FindingSlaConfig",
    # ★ v0.20：CTF2 领域（五张 + 一张配置表）
    "Ctf2Account",
    "Ctf2Competition",
    "Ctf2Challenge",
    "Ctf2Config",
    "Ctf2Environment",
    "Ctf2Submission",
]
