"""拦住「把真凭据写进了入库文件」。

## 为什么需要这一条（CI 里已有的两道不够）

`ci.yml` 的 `hygiene` job 已经有两道检查，但它们**只看文件名**：

    git ls-files | grep -E '(^|/)[.]env($|[.])'          # 文件名里有 .env
    git log --all --name-only | grep -E '(^|/)[.]env'   # 历史里的文件名

而真正会发生的泄漏往往**不在 `.env` 里**：有人把 key 写成代码里的默认值
（`api_key = "sk-…"`）、贴进 README、或者塞进一个测试夹具，文件名完全
正常。那两道检查对它一无所见。

## 判据：看**形状**，不看关键词

`api_key` 这个词在文档里天天出现，靠关键词判断全是误报。能区分真假的是
形状：**长、高熵、不含空格**。所以这里匹配的是 `sk-`/`AKIA`/`ghp_`/
私钥头这类**有固定前缀**的凭据，以及"赋值给 *key*/*token* 的长串"。

## ★ 为什么白名单必须存在（否则这条检查会被关掉）

测试里全是 `sk-test-…`、`sk-live-ACCEPTANCE-…`、`sk-not-a-real-key-…`。
如果它们全部报红，这条检查会在第一次推送时就被注释掉 —— 而那时它还没
拦下任何真东西。所以：**值里含下列词的，一律放过**。

代价如实记：一个真 key 恰好含有 `test` / `demo` 这类子串时会漏报。接受
这个代价，因为漏报的概率远低于误报，而误报的后果是整个检查失效。

## 扫描范围

只扫**已跟踪文件**（`git ls-files`）。理由：
  · 未跟踪文件（`.env`、`_tmp/`）本来就不入库，扫它们只会制造噪音；
  · 历史不可变，扫出来也修不了；那属于"发现泄漏之后怎么追查"，
    工具是 `_tmp/scan_secrets.py`（本地用，不在 CI 里跑）。

用法：
    python3 suvex-api/scripts/verify_no_secrets.py      # CI 与本地一致
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: 二进制/生成物不扫：读进来也只是噪音，而且 pattern 不会命中它们。
SKIP_SUFFIX = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".7z", ".whl", ".so", ".dll", ".exe", ".pyc", ".woff", ".woff2",
    ".ttf", ".eot", ".mp4", ".mp3", ".wav", ".bin", ".db", ".sqlite",
    ".lock", ".map",
}

#: (规则名, 正则, 掩码长度下限)。用 `group(1)` 取值；没有分组则取 `group(0)`。
PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("OpenAI/DeepSeek 风格的 sk-", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), 16),
    ("Anthropic sk-ant-", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"), 20),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 20),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"), 33),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), 50),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), 39),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), 15),
    ("私钥块", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), 0),
    (
        "比赛平台 agent access key",
        re.compile(r"X-Agent-AccessKey\s*[:=]\s*[\"']?([A-Za-z0-9_\-]{16,})", re.I),
        16,
    ),
    (
        "赋值给 *key*/*token* 的长串",
        re.compile(
            r"""(?ix)
            (?:api[_-]?key|access[_-]?key|secret[_-]?key|auth[_-]?token|
               access[_-]?token)
            \s*[:=]\s*["']?([A-Za-z0-9_\-./+=]{20,})["']?
            """
        ),
        20,
    ),
]

#: 见文件头「为什么白名单必须存在」。命中即放过。
#:
#: ⚠️ 这几条都不是凭空的：仓库里现有的测试夹具长这样 ——
#:
#:     sk-accept-SCAN-2f8a1c4e9b7d30625a8c1f4e   ← accept + SCAN
#:     sk-live-FIRSTKEY-2f8a1c4e9b7d30625a8c1f4e  ← FIRSTKEY
#:     sk-other-1234567890                        ← 递增数字
#:     sk-abcdefghijklmnop                        ← 递增字母
#:
#: 它们**后半段是高熵的**（`2f8a1c4e…` 看着就像真 token），所以靠熵
#: 区分不了 —— 唯一的信号是前半段那个**可读的词**。这也是为什么白名单
#: 要覆盖"递增序列"和"连续大写字母"，而不只是几个具体的单词。
FALSE_POSITIVE = re.compile(
    r"""(?ix)
    (?:
        test|example|sample|placeholder|dummy|fake|mock|stub|
        not[_-]?a[_-]?real|accept|probe|seed|demo|lorem|
        redact|mask|blur|your[_-]|change[_-]?me|changeme|
        \$\{|<[^>]*>|x{4,}|0{4,}|1{4,}|
        # 递增的字母/数字序列：abcdef / 123456
        abc|bcd|cde|def|efg|fgh|ghi|hij|ijk|jkl|klm|lmn|
        mno|nop|opq|pqr|qrs|rst|stu|tuv|uvw|vwx|wxy|xyz|
        123|234|345|456|567|678|789|890
    )
    """
)

#: ⚠️ 单独一条、**不带 `i` 标志**：上面那条是 `(?ix)`，`[A-Z]` 在忽略大小写
#: 的语境下等于 `[A-Za-z]`，那样任何三个连续字母都会命中，真 key 全被放过。
#: 连续大写字母是"人手写的常量"的典型特征（`FIRSTKEY` / `SCAN` / `ACCEPTANCE`）。
FALSE_POSITIVE_UPPER = re.compile(r"[A-Z]{3,}")

#: 形如 `sk-…` 但其实是**完整的英文句子**（文档里的示例）
LOOKS_HUMAN = re.compile(r"^[A-Za-z]+(?:[_-][A-Za-z]+){2,}$")


def mask(v: str) -> str:
    """只留头尾。真值不进 CI 日志 —— 日志是公开的。"""
    n = len(v)
    if n <= 8:
        return f"{'*' * n}（{n} 位）"
    return f"{v[:6]}…{v[-4:]}（{n} 位）"


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "-c", "core.quotepath=false", "ls-files"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return [f for f in out.stdout.splitlines() if f.strip()]


def main() -> int:
    files = tracked_files()
    if not files:
        print("读不到已跟踪文件列表（git 不可用？）")
        return 2

    hits: list[str] = []
    scanned = 0
    for rel in files:
        if Path(rel).suffix.lower() in SKIP_SUFFIX:
            continue
        path = REPO_ROOT / rel
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > 5000 or len(line) < 16:
                continue
            for name, rx, minlen in PATTERNS:
                for m in rx.finditer(line):
                    raw = m.group(1) if m.groups() else m.group(0)
                    if len(raw) < minlen:
                        continue
                    if FALSE_POSITIVE.search(raw) or FALSE_POSITIVE_UPPER.search(raw):
                        continue
                    if LOOKS_HUMAN.match(raw):
                        continue
                    hits.append(f"{rel}:{lineno}  [{name}]  {mask(raw)}")

    if not hits:
        print(f"✅ 已跟踪文件里没有形似凭据的字符串（扫描 {scanned} 个文件）")
        return 0

    print(f"❌ 发现 {len(hits)} 处形似凭据：\n")
    for h in hits:
        print("  " + h)
    print(
        "\n如果确认是真的：把它从文件里删掉、换用环境变量，然后**改掉那一个 key**"
        " —— 提交过的东西即使删掉也留在历史里。"
    )
    print("如果确认是假值：把它改成含 test/example/dummy 之类字样，或加进脚本的白名单。")
    return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
