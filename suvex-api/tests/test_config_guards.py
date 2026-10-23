"""启动配置守卫（`Settings.assert_bootable`）的行为锁。

## 为什么单独一个文件

`assert_bootable()` 守的是「配置错了却不会报错、只是悄悄不安全」那一类。
这种缺陷的特点：**开发态完全看不出来**（dev 环境本该宽松），只有把一个
真实部署弄到 prod 才暴露 —— 而那时候已经在生产上了。

所以每加一条守卫，都必须有一条能在**变异后变红**的测试。这个文件专放这些。

## 已有的守卫

1. prod + `cookie_secure=False` → 拒绝启动（v0.24）；
2. prod + 空 `secret_key` → 拒绝启动（v0.24）；
3. ★ prod + CORS 白名单里含本地开发前端源 → 拒绝启动（v0.73）。

第 3 条不是凭空加的：v0.73 的 P1 对照扫描（打自家平台前端）实测报出
「CORS 白名单含 http://localhost:5173 且 allow_credentials=True」，回代码
核对确认 `.env` 在部署副本里原样带着 Vite 开发端口。影响有限（要利用需
受害者本机开着该端口），但没有任何理由在生产保留它。
"""

from __future__ import annotations

import pytest

from app.core.config import Settings

#: 一个能通过其它所有守卫的 prod 基线 —— 单独测某条守卫时用它做底，免得
#: 因为别的守卫先抛导致断言打歪。
_PROD_OK = {
    "env": "prod",
    "cookie_secure": True,
    "secret_key": "x" * 48,
    "cors_origins": "https://suvex.example.com",
}


def _prod(**overrides) -> Settings:
    payload = dict(_PROD_OK)
    payload.update(overrides)
    return Settings(**payload)


# ══════════════════════════════════════════════════════════
# dev_cors_origins：识别口径
# ══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "origins,expected",
    [
        # 典型 Vite 源 —— 必须被认出来
        ("http://localhost:5173", ["http://localhost:5173"]),
        ("http://127.0.0.1:5173", ["http://127.0.0.1:5173"]),
        # 多个混在一起，只挑开发源，生产源原样留着
        (
            "https://suvex.example.com,http://localhost:5173,http://127.0.0.1:5173",
            ["http://localhost:5173", "http://127.0.0.1:5173"],
        ),
        # 末尾斜杠要先被 cors_origin_list 归一化掉
        ("http://localhost:5173/", ["http://localhost:5173"]),
        # 不带端口也算（http://localhost）
        ("http://localhost", ["http://localhost"]),
        # 常见 dev 端口
        ("http://localhost:3000", ["http://localhost:3000"]),
        ("http://localhost:4173", ["http://localhost:4173"]),
        # 不认识的端口不认 —— 别误杀一个真实生产源
        ("http://localhost:8443", []),
        # 非本地 host 不认（哪怕端口是 5173）
        ("https://suvex.example.com:5173", []),
        # 空配置
        ("", []),
    ],
)
def test_dev_cors_origins_detection(origins: str, expected: list[str]) -> None:
    assert Settings(cors_origins=origins).dev_cors_origins == expected


# ══════════════════════════════════════════════════════════
# assert_bootable：prod 拒绝开发源
# ══════════════════════════════════════════════════════════


def test_prod_rejects_dev_cors_origin() -> None:
    """★ 核心回归：prod + 开发源必须拒绝启动。

    ## 反面对照（变异会红）

    把 `assert_bootable` 里 `if self.is_prod and dev_origins:` 那段删掉
    （回到 v0.72），这条会因为「没抛」而失败。
    """
    settings = _prod(cors_origins="https://suvex.example.com,http://localhost:5173")
    with pytest.raises(RuntimeError) as exc:
        settings.assert_bootable()
    message = str(exc.value)
    assert "SUPEX_CORS_ORIGINS" in message
    # 错误信息里要**点名**是哪个源 —— 否则运维看到一条笼统报错还得自己猜
    assert "localhost:5173" in message


def test_prod_accepts_clean_cors_origins() -> None:
    """prod + 只有真实生产源 → 正常放行（别把守卫做成"一律拒绝"）。"""
    _prod(cors_origins="https://suvex.example.com,https://www.suvex.example.com").assert_bootable()


def test_dev_env_keeps_dev_cors_origin() -> None:
    """dev 环境带开发源是本该的 —— 守卫只对 prod 生效。"""
    Settings(env="dev", cors_origins="http://localhost:5173").assert_bootable()


# ══════════════════════════════════════════════════════════
# 既有两条守卫的回归（防止改这段代码时手滑打歪）
# ══════════════════════════════════════════════════════════


def test_prod_rejects_insecure_cookie() -> None:
    with pytest.raises(RuntimeError, match="SUPEX_COOKIE_SECURE"):
        _prod(cookie_secure=False).assert_bootable()


def test_prod_rejects_missing_secret_key() -> None:
    with pytest.raises(RuntimeError, match="SUPEX_SECRET_KEY"):
        _prod(secret_key="").assert_bootable()
