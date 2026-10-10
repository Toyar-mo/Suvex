"""凭据：口令哈希、会话令牌、机器令牌。

## 为什么不是 JWT

任务 1 的原文要求「JWT（access token + refresh token）」，而契约 §1.2 明确
写了 **不要用 JWT**，理由是「本地单机部署不需要无状态扩展，而 JWT
**无法即时吊销**（改密码后旧令牌仍有效）」。

**按契约实现，用不透明令牌**，理由不是「契约更大」而是它给的那条是技术事实：

* 本平台的凭据泄漏后果是**替攻击者发包**（契约 §0.3），不是读到数据。
  所以「改密码后旧会话立刻失效」是安全要求，不是体验优化。
  JWT 要做到这一点必须维护一张吊销表 —— 那就已经不是无状态了，
  等于付了 JWT 的复杂度（密钥轮换、时钟偏移、算法混淆）却没拿到它的好处。
* access + refresh 的**语义**在这里保留了：会话有 12 小时有效期，
  且每次使用会续期（滚动过期）—— 这等价于 refresh 的效果，
  但不用把凭据切成两段、也不用处理「refresh 过期了但 access 还没」的边界态。
* 机器通道的 `svx_…` 令牌同样是哈希入库、可即时吊销、可限定 scope。

令牌格式被封装在本模块里。将来若真要换 JWT，换的是 `token_digest` +
会话/令牌校验这两处，业务代码一行不动。决策记录见 `docs/adr-001`。

## 存哈希，不存明文

会话令牌和机器令牌都只存 SHA-256 摘要。这里**可以用裸 SHA-256**（不用 argon2），
因为它们是 256 位的高熵随机串 —— 口令需要慢哈希来抵抗字典攻击，
而随机令牌没有字典可查。用 argon2 反而会让每个请求多花几十毫秒。
"""

from __future__ import annotations

import hashlib
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# argon2id 的默认参数（argon2-cffi 25.x：time_cost=3, memory_cost=64MiB, parallelism=4）。
# 不手工调参：默认值是当前推荐档，手调的「我觉得更快」通常是错的。
_hasher = PasswordHasher()

# ── 口令强度（与前端 `checkPassword` 对齐）────────────────
# 契约 §3.1 要求服务端复用前端的规则（≥12 字符 + 大小写 + 数字）。
# 前端还给了「符号（可选）」一条 —— 那是**加分项不是硬要求**，
# 所以这里不拦它，只影响强度分档。
MIN_PASSWORD_LENGTH = 12
_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_DIGIT = re.compile(r"\d")

# ── 机器令牌 ──────────────────────────────────────────────
MACHINE_TOKEN_PREFIX = "svx_"
# 前端 `ApiToken.prefix` 是 `plaintext.slice(0, 8)`，服务端必须用同一个切片长度，
# 否则界面上显示的前缀和实际令牌对不上，人会拿它去比对日志然后困惑。
MACHINE_TOKEN_PREFIX_LENGTH = 8
_MACHINE_TOKEN_BYTES = 32
_SESSION_TOKEN_BYTES = 32


# ── 口令 ──────────────────────────────────────────────────


def hash_password(raw: str) -> str:
    """argon2id 哈希。每次调用都用新的随机盐（argon2 内部处理）。"""
    return _hasher.hash(raw)


def verify_password(stored_hash: str, raw: str) -> bool:
    """校验口令。

    **吞掉所有异常只回 False**：调用方拿到的应该是「对不对」，
    不是「哈希格式坏了还是口令错了」。后者会把内部状态泄露到响应里。
    哈希串损坏（`InvalidHashError`）也归为 False —— 那种情况只可能是
    数据库被手工改过，此时拒绝登录是正确行为。
    """
    try:
        _hasher.verify(stored_hash, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(stored_hash: str) -> bool:
    """参数升级后是否该重算。登录成功时顺手调用，让老哈希自然淘汰。"""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def password_problems(raw: str) -> list[str]:
    """未满足的口令要求，中文。空列表 = 通过。

    返回**全部**未满足项而不是第一条：契约 §1.3 要求一次说清，
    而「补了长度又说不含数字」是典型的来回三次。
    """
    problems: list[str] = []
    if len(raw) < MIN_PASSWORD_LENGTH:
        problems.append(f"至少 {MIN_PASSWORD_LENGTH} 个字符")
    if not (_LOWER.search(raw) and _UPPER.search(raw)):
        problems.append("同时包含大写与小写字母")
    if not _DIGIT.search(raw):
        problems.append("包含数字")
    return problems


def password_error_message(problems: list[str]) -> str:
    return "口令强度不足，需要：" + "、".join(problems) + "。"


# ── 令牌 ──────────────────────────────────────────────────


def new_session_token() -> str:
    """会话令牌明文。43 字符 URL-safe base64，256 位熵。

    用 `token_urlsafe`（base64）而不是 `token_hex`：前者短 25%，
    而 Cookie 长度是有实际上限的（浏览器普遍 4KB，但代理的头部限制更小）。
    """
    return secrets.token_urlsafe(_SESSION_TOKEN_BYTES)


def new_machine_token() -> tuple[str, str]:
    """返回 `(明文, 前缀)`。明文只在创建响应里出现这一次。"""
    raw = MACHINE_TOKEN_PREFIX + secrets.token_urlsafe(_MACHINE_TOKEN_BYTES)
    return raw, raw[:MACHINE_TOKEN_PREFIX_LENGTH]


def token_digest(raw: str) -> str:
    """入库用的摘要。64 位小写十六进制，可直接建唯一索引。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def looks_like_machine_token(raw: str) -> bool:
    """先看形状再查库。

    没有这一步，任何人往 `Authorization` 头里塞点东西都会触发一次
    带索引的数据库查询 —— 一个不需要认证的放大面。
    """
    return raw.startswith(MACHINE_TOKEN_PREFIX) and len(raw) > MACHINE_TOKEN_PREFIX_LENGTH


def scope_allows(granted: list[str], required: str) -> bool:
    """scope 是否够用。

    **三档是累进的**（契约 §1.2 的措辞是「`read`+`scan` scope 令牌」，
    前端 `TOKEN_SCOPE_NOTE.scan` 也写着「在只读基础上」）：
    `admin` ⊃ `scan` ⊃ `read`。

    所以持 `scan` 令牌的 CI 既能发扫描也能读结果，不必同时申请两个 scope ——
    那才是符合直觉的。反过来 `read` 令牌想发扫描会被 403，
    这正是 scope 存在的意义。
    """
    granted_set = set(granted)
    if required == "read":
        return bool(granted_set & {"read", "scan", "admin"})
    if required == "scan":
        return bool(granted_set & {"scan", "admin"})
    if required == "admin":
        return "admin" in granted_set
    return False


# scope 的中文名，用在 403 的文案里。与前端 `TOKEN_SCOPE_LABEL` 一致。
SCOPE_LABELS = {"read": "只读", "scan": "发起扫描", "admin": "管理权限"}
