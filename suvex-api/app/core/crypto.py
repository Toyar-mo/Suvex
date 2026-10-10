"""对称加密：provider 凭据落库前的封装。

## 为什么是 Fernet（对称）而不是哈希

`api_key` 与口令、会话令牌都不一样 —— **它必须能被还原**：
服务端要拿它去调 LLM。所以哈希这条路从一开始就不通，
只能是可逆加密。

Fernet 是 `cryptography` 库提供的 AES-128-CBC + HMAC-SHA256 组合
（"encrypt-then-MAC"），带版本号与时间戳，且**自带密钥格式校验**。
选它而不是自己拼 AES 的理由很直接：自拼的加密代码里，
IV 复用、MAC 校验顺序、填充预言这几类错误不会在正常使用中暴露，
只在被攻击时暴露。

## 密钥从哪来

单独的 `SUPEX_SECRET_KEY` 环境变量，**不复用任何其他密钥**。

> 任务里写的是「不要和 JWT 密钥复用」。本项目没有 JWT 密钥
> （见 `docs/adr-001-auth-credentials.md`：契约否掉了 JWT，用的是不透明令牌，
> 根本不产生签名密钥）。所以这条约束在这里的等价物是：
> **加密密钥只用于加密，不参与任何别的东西**（不签名、不派生会话、不校验）。
> 一条明文密钥只承担一个职责，泄漏时影响面才是可界定的。

## 密钥丢了会怎样（必须知道）

`SUPEX_SECRET_KEY` 丢失或更换 ⇒ **已存的 provider 凭据全部无法解密**。
这不是可以"优雅降级"的事：凭据无法解密时，那个 provider 就是不可用的，
而界面必须**如实说这件事**，而不是显示成"未配置"让用户以为是自己没填。

所以 `decrypt_secret` 失败会抛 `SecretDecryptError`，
调用方必须把它变成一个明确的错误信息（见 `providers/service.py`），
而不是吞掉。

代价是运维上必须把这一条密钥和数据库一起备份。这句话写在 README 里。
"""

from __future__ import annotations

import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

# 掩码里的固定标记。真实 API key 里出现连续四个星号的可能性可以忽略，
# 所以它同时被用作"这个值已经是掩码"的判据（见 looks_like_mask）。
MASK_MARKER = "****"

# 掩码保留的位数。与前端 `lib/secret.ts` 的 `secretPreview` 同一个约定：
# 前 3 位让人认出是哪一家（sk- / AIz / gsk-），后 4 位才是真正能区分的部分 ——
# 同一家的多个 key 前缀往往完全相同，只给前缀等于没给信息。
MASK_PREFIX_CHARS = 3
MASK_SUFFIX_CHARS = 4

# 短于这个长度就整体隐掉。照抄前端 `secretPreview` 的阈值（12）：
# 一个 11 位的值用「前 3 + 后 4」会暴露 7 位，那不是掩码，是缩写。
MASK_MIN_LENGTH = 12

# 已经是掩码的形状：前后各最多 4 个非星号字符，中间恰好四个星号。
# 用形状判断而不是 `"****" in value`，是为了让"恰好长得像掩码的真实 key"
# 也没法被误判成掩码（虽然实际不可能）。
_MASK_PATTERN = re.compile(r"^\S{0,4}\*{4}\S{0,4}$")


class SecretKeyMissing(RuntimeError):
    """没有配置 `SUPEX_SECRET_KEY`。"""


class SecretDecryptError(RuntimeError):
    """解密失败 —— 密钥变了，或者密文被改过。"""


def generate_key() -> str:
    """生成一条新的 Fernet 密钥（44 字符 URL-safe base64）。

    给运维用：`python -m app.core.crypto` 会打印一条。
    """
    return Fernet.generate_key().decode("ascii")


@lru_cache(maxsize=1)
def _fernet(key: str) -> Fernet:
    """构造 Fernet 实例。

    带缓存是因为 `Fernet.__init__` 每次都要做一次 base64 解码与长度校验，
    而这个函数在每次加密/解密时都会被调用。
    缓存键是密钥本身 —— 密钥变了自然就是新实例，不需要手动失效。
    """
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise SecretKeyMissing(
            "SUPEX_SECRET_KEY 格式不正确。它必须是 Fernet 密钥："
            "44 个字符的 URL-safe base64（32 字节原始密钥）。\n"
            "生成一条新的：\n"
            "    python -m app.core.crypto\n"
            "或：\n"
            '    python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


def current_key() -> str:
    """读当前配置的密钥。没有就抛 `SecretKeyMissing`（消息里带生成命令）。"""
    from app.core.config import get_settings

    key = (get_settings().secret_key or "").strip()
    if not key:
        raise SecretKeyMissing(
            "尚未配置 SUPEX_SECRET_KEY，无法加密或解密 provider 凭据。\n"
            "生成一条并写进 .env：\n"
            "    python -m app.core.crypto\n"
            "⚠️ 这条密钥必须与数据库一起备份 —— 丢了它，已存的 provider 凭据就再也解不开。"
        )
    return key


def is_configured() -> bool:
    """密钥配了没有。给启动自检与接口做前置判断用（不抛异常）。"""
    from app.core.config import get_settings

    return bool((get_settings().secret_key or "").strip())


# ── 加解密 ────────────────────────────────────────────────


def encrypt_secret(plaintext: str) -> str:
    """明文 → 密文（Fernet token，URL-safe base64 字符串）。

    ⚠️ **不接受空串**。"没有 key" 在数据模型里用 NULL 表达，
    而不是"存了一段加密后的空串"—— 后者会让 `hasKey` 变成
    "密文列非空"这种间接判断，而它会在密钥轮换后出错。
    """
    if not plaintext:
        raise ValueError("encrypt_secret 不接受空串：没有凭据时应当存 NULL")
    return _fernet(current_key()).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """密文 → 明文。失败抛 `SecretDecryptError`。"""
    if not token:
        raise SecretDecryptError("密文为空，无法解密。")
    try:
        return _fernet(current_key()).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptError(
            "无法解密这条 provider 凭据。最可能的原因是 SUPEX_SECRET_KEY "
            "被更换或丢失 —— 换回原来那条密钥即可恢复；"
            "若原密钥确实已丢失，只能重新填写该 provider 的 API key。"
        ) from exc
    except (ValueError, TypeError) as exc:
        raise SecretDecryptError("密文格式不正确，无法解密。") from exc


# ── 掩码 ──────────────────────────────────────────────────


def mask_secret(value: str) -> str:
    """明文 → 可展示的掩码，形如 `sk-****abcd`。

    这是**唯一**允许离开服务端、出现在响应里的凭据相关信息。
    它不参与任何校验，只用于"让用户确认这里确实有一把钥匙，以及是哪一把"。
    """
    if not value:
        return ""
    if len(value) < MASK_MIN_LENGTH:
        return "*" * 8
    return (
        value[:MASK_PREFIX_CHARS]
        + MASK_MARKER
        + value[-MASK_SUFFIX_CHARS:]
    )


def looks_like_mask(value: str) -> bool:
    """这个值是不是一个掩码？

    用途只有一个，但是关键的一个：**防止掩码被当成新密钥写回库里**。

    场景是真的：客户端 GET 到 `sk-****abcd` 之后，如果它把这个值原样放进
    更新请求（很多"读出来改一个字段再整体提交"的表单会这么干），
    服务端就会把掩码存成新的 API key —— 而用户看到的反馈是"保存成功"。
    真凭据被静默毁掉，且没有任何报错。

    所以更新路径上：值等于掩码 → 视为"不改凭据"。
    """
    return bool(value) and bool(_MASK_PATTERN.match(value))


def redact(value: str) -> str:
    """把一段文本里**看起来像凭据**的部分替换成掩码。给日志用。

    这是兜底，不是"可以放心打日志"的许可证。见 `core/logging.py`。
    """
    if not value:
        return value
    redacted = _SK_KEY.sub(lambda m: mask_secret(m.group(0)), value)
    redacted = _SVX_KEY.sub(lambda m: m.group(0)[:8] + "****", redacted)
    # 连接串里的口令。任务 8 要求"数据库连接串不被打出来"，而这句话
    # 最容易在被违反的地方就是**连库失败的异常**里 ——
    # SQLAlchemy 的报错会把整个 URL 原样带上（含口令）。
    redacted = _DB_URL_PASSWORD.sub(lambda m: f"{m.group(1)}:****@", redacted)
    # JWT。本项目**不用** JWT 做自己的凭据（见 docs/adr-001），
    # 但扫描目标可能返回一个，而我们要把它原样写进证据里 ——
    # 那段证据会进数据库、进报告、进日志。
    redacted = _JWT.sub(lambda m: m.group(0)[:8] + "****", redacted)
    # `Authorization: Bearer xxx` 形式。放最后：前面几条可能已经把
    # 凭据变成掩码，但那不影响这里的匹配（掩码里没有足够长的 token 字符）。
    redacted = _BEARER.sub(lambda m: f"{m.group(1)} ****", redacted)
    return redacted


# 形如 sk-xxx / sk-proj-xxx / gsk_xxx 的密钥。长度下限 8 是为了不误伤
# 普通文本里的 "sk-"（比如 URL 里的 slug）。
_SK_KEY = re.compile(r"\b(?:sk|gsk|xai|pk)[-_][A-Za-z0-9_\-]{8,}")
# 本平台自己的令牌前缀
_SVX_KEY = re.compile(r"\bsvx_[A-Za-z0-9_\-]{8,}")
# ⚠️ 连接串口令：**必须**按 `scheme://user:pass@` 的形状匹配，
# 而不是"匹配 :// 之后的所有东西" —— 后者会把 `postgresql://host/db?sslmode=x`
# 里的正常部分也吃掉。要求出现 `user:pass@` 三段才动它。
# 只保留 scheme://user 与 @，中间那段换成掩码。
_DB_URL_PASSWORD = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/@\s]+):[^@/\s]+@")
# JWT：三段 base64url，第一段固定以 `eyJ` 开头（`{"` 的 base64）。
# 这个前缀让误伤率极低 —— 普通文本里不会自然出现它。
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}")
# `Bearer xxx` 头。大小写都认（HTTP 头字段名不区分大小写）。
_BEARER = re.compile(r"\b(Bearer)\s+[A-Za-z0-9_\-.]{8,}", re.IGNORECASE)


if __name__ == "__main__":  # pragma: no cover - 运维用的一次性入口
    print(generate_key())
