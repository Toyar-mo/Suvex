"""比赛平台 API 的**本地模拟服务端**（照着 `api_doc.md` 实现）。

## 为什么需要它

比赛还没开始，`api_doc.md` 里的 `{serverHost}` 是占位符 —— 没有真实服务端，
"拉题 → 起环境 → 解题 → 提交"这条链路就没法端到端跑。

这个 mock 完整实现了文档里的 9 个端点，并且**故意把几种真实世界的麻烦造出来**：

* **异步环境**：`build-exercise-env` 之后 `isNeedCheck` 先为 `true`，
  查两次详情才变成 `false` —— 逼着调用方真的去轮询（而不是调一次就当好了）；
* **代理与非代理两种靶机**：`endpoints[].isProxy` 有 true 有 false，
  让"优先走代理"那段逻辑能被真的走到；
* **附件**：题目带附件 URL，指向本服务自己的 `/files/...`，于是下载链路也能测；
* **提交判定三态**：正确 / 错误 / 重复提交，都要有确定的返回；
* **鉴权**：`X-Agent-AccessKey` 不对时按文档返回非 `00000` 的 code。

## 跑法

    python scripts/mock_arena_server.py --port 8901 --key ak_test_local

然后把它当成 `{serverHost}` 用：

    http://127.0.0.1:8901/slab-match/api/v1/agent/...

⚠️ 它是**测试工具**，不参与生产路径；真实比赛时把 host/key 换成赛事方给的即可。
"""

from __future__ import annotations

import argparse
import base64
import io
import time
import zipfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, Header, Query
from fastapi.responses import JSONResponse, Response

# ══════════════════════════════════════════════════════════
# 假数据（够跑通链路，且覆盖几种分支）
# ══════════════════════════════════════════════════════════

#: 题目：分类 → corpus。id 用整数（与文档一致）。
#:
#: ★ 每类**两道**。这是给"回归基线"用的（见 scripts/ctf2_baseline.py）：
#:   一道"照理该过"的、一道"要绕一下"的。只放一道的话，一次失败分不清是
#:   "模型不行"还是"这道题就是不适合它"。
EXERCISES: dict[int, dict[str, Any]] = {
    # ── Web ────────────────────────────────────────────
    1001: {
        "id": 1001,
        "name": "easy-web",
        "category": "Web",
        "description": "登录口有弱口令。附件里是源码包。",
        "hasSolved": False,
        "score": "100",
        "difficulty": "EASY",
        "flag": "flag{welcome_to_the_arena}",
        "isNeedInit": True,
        "endpointType": "monopoly",
        "attachment": {
            "files": [
                {"name": "source.zip", "url": "/files/source.zip", "ext": "zip"},
            ]
        },
    },
    1004: {
        "id": 1004,
        "name": "web-lfi",
        "category": "Web",
        "description": "下载接口的 file 参数没有过滤，试试能不能读到不该读的文件。附件是站点源码包。",
        "hasSolved": False,
        "score": "200",
        "difficulty": "MEDIUM",
        "flag": "flag{lfi_gets_the_file}",
        "isNeedInit": True,
        "endpointType": "monopoly",
        "attachment": {
            "files": [{"name": "wwwroot.zip", "url": "/files/wwwroot.zip", "ext": "zip"}]
        },
    },
    # ── Crypto ─────────────────────────────────────────
    1002: {
        "id": 1002,
        "name": "crypto-warmup",
        "category": "Crypto",
        "description": "一段密文，密钥很短。",
        "hasSolved": False,
        "score": "150",
        "difficulty": "EASY",
        "flag": "flag{short_key_is_not_a_key}",
        "isNeedInit": False,          # ★ 不需要起环境的分支
        "endpointType": "monopoly",
        "attachment": {
            "files": [{"name": "cipher.txt", "url": "/files/cipher.txt", "ext": "txt"}]
        },
    },
    1005: {
        "id": 1005,
        "name": "crypto-rsa",
        "category": "Crypto",
        "description": "RSA 的所有参数都在附件里。",
        "hasSolved": False,
        "score": "250",
        "difficulty": "MEDIUM",
        "flag": "flag{rsa_is_not_hard}",
        "isNeedInit": False,
        "endpointType": "monopoly",
        "attachment": {
            "files": [{"name": "rsa.txt", "url": "/files/rsa.txt", "ext": "txt"}]
        },
    },
    # ── Pwn ────────────────────────────────────────────
    1003: {
        "id": 1003,
        "name": "pwn-baby",
        "category": "Pwn",
        "description": "栈溢出，靶机已经在跑了。",
        "hasSolved": False,
        "score": "300",
        "difficulty": "MEDIUM",
        "flag": "flag{ret2win_is_classic}",
        "isNeedInit": True,
        "endpointType": "monopoly",
        "attachment": {
            "files": [{"name": "baby", "url": "/files/baby", "ext": "bin"}]
        },
    },
    1006: {
        "id": 1006,
        "name": "pwn-fmt",
        "category": "Pwn",
        "description": "格式化字符串漏洞，能把栈上的东西打印出来。flag 被拆散了，得拼。",
        "hasSolved": False,
        "score": "350",
        "difficulty": "HARD",
        "flag": "flag{format_string_leak}",
        "isNeedInit": True,
        "endpointType": "monopoly",
        "attachment": {
            "files": [{"name": "fmt", "url": "/files/fmt", "ext": "bin"}]
        },
    },
    # ── Reverse ────────────────────────────────────────
    1007: {
        "id": 1007,
        "name": "rev-crackme",
        "category": "Reverse",
        "description": "输入正确的密码才会打印 flag。",
        "hasSolved": False,
        "score": "250",
        "difficulty": "MEDIUM",
        "flag": "flag{strings_is_underrated}",
        "isNeedInit": False,
        "endpointType": "monopoly",
        "attachment": {
            "files": [{"name": "crackme", "url": "/files/crackme", "ext": "bin"}]
        },
    },
    1008: {
        "id": 1008,
        "name": "rev-patchme",
        "category": "Reverse",
        "description": "附件是校验逻辑的源码，找出它认可的那个 flag。",
        "hasSolved": False,
        "score": "300",
        "difficulty": "MEDIUM",
        "flag": "flag{patch_me_if_you_can}",
        "isNeedInit": False,
        "endpointType": "monopoly",
        "attachment": {
            "files": [{"name": "checker.c", "url": "/files/checker.c", "ext": "c"}]
        },
    },
    # ── Misc ───────────────────────────────────────────
    1009: {
        "id": 1009,
        "name": "misc-hex",
        "category": "Misc",
        "description": "一串十六进制，解出来就是 flag。",
        "hasSolved": False,
        "score": "100",
        "difficulty": "EASY",
        "flag": "flag{hex_is_a_language}",
        "isNeedInit": False,
        "endpointType": "monopoly",
        "attachment": {
            "files": [{"name": "data.txt", "url": "/files/data.txt", "ext": "txt"}]
        },
    },
    1010: {
        "id": 1010,
        "name": "misc-dump",
        "category": "Misc",
        "description": "从一段内存 dump 里把 flag 捞出来。",
        "hasSolved": False,
        "score": "200",
        "difficulty": "MEDIUM",
        "flag": "flag{carve_the_evidence}",
        "isNeedInit": True,
        "endpointType": "monopoly",
        "attachment": {
            "files": [{"name": "dump.bin", "url": "/files/dump.bin", "ext": "bin"}]
        },
    },
}

#: 分类树（`/ctf/exercise-list` 的形状：分类在前，题目挂在 corpus 下）
#:
#: ★ 五个分类要和 `app/ctf2/runner.py` 里 `CATEGORY_REQUIREMENTS` 对得上 ——
#:   Web / Crypto / Pwn / Reverse / Misc。Reverse 是唯一路由到另一个镜像的。
CATEGORIES = [
    {"id": 10, "name": "Web", "order": 1, "corpus": [1001, 1004]},
    {"id": 11, "name": "Crypto", "order": 2, "corpus": [1002, 1005]},
    {"id": 12, "name": "Pwn", "order": 3, "corpus": [1003, 1006]},
    {"id": 13, "name": "Reverse", "order": 4, "corpus": [1007, 1008]},
    {"id": 14, "name": "Misc", "order": 5, "corpus": [1009, 1010]},
]

NOTICES = [
    {
        "id": 501,
        "title": "比赛开始公告",
        "content": "本次比赛仅允许使用平台下发的 AccessKey 调用接口。",
        "createdAt": "2026-06-26T10:00:00.000+08:00",
        "createdTime": 1780000000000,
        "userName": "系统公告",
    }
]

#: 附件名 → 该附件里藏的 flag。
#:
#: ★ 这几条让"解题"这一步**真的可解**。如果 flag 只放在服务端，
#: 模型不可能猜到它，端到端就退化成"下载附件 + 提交一个必然错误的答案"。
#: 藏在附件里之后，模型得真的用工具（unzip / base64 / strings）去挖。
#: 三份附件刻意用三种形态，覆盖三类工具。
ATTACHMENT_FLAGS: dict[str, str] = {
    "source.zip": "flag{welcome_to_the_arena}",       # zip → unzip 后读 flag.txt
    "wwwroot.zip": "flag{lfi_gets_the_file}",         # zip → 还得进子目录才找得到
    "cipher.txt": "flag{short_key_is_not_a_key}",     # base64 包裹 → 解码
    "rsa.txt": "flag{rsa_is_not_hard}",               # 给 p q e c → 得真的算
    "baby": "flag{ret2win_is_classic}",               # 混在二进制噪声里 → strings
    "fmt": "flag{format_string_leak}",                # 分两段埋 → 还得拼起来
    "crackme": "flag{strings_is_underrated}",         # 二进制 → strings
    "checker.c": "flag{patch_me_if_you_can}",         # 源码 → 读代码
    "data.txt": "flag{hex_is_a_language}",            # hex → xxd -r -p
    "dump.bin": "flag{carve_the_evidence}",           # 二进制 dump → strings/binwalk
}

#: RSA 题用的一整组参数（离线算好，**故意写死**）。
#:
#: 为什么写死而不是运行时生成：基线要可复现 —— 两次跑必须是同一道题，
#: 否则"回合数变了"分不清是模型变了还是题目变了。
#: n 是 256 bit，装得下 21 字节的 flag；p、q 直接给了，所以**是可解的**
#: （不用分解，只要会 RSA 解密那一套）。
RSA_PARAMS: dict[str, int] = {
    "p": 254304710727048405345333162861081917581,
    "q": 233804115147935605901268780423745796773,
    "e": 65537,
    "n": 59457487869489280438879681724913602613640635277851254156997750658183561766113,
    "c": 39545473729203954364549730459779541103937583965343310964099541771412337486461,
}


def build_attachment(name: str, flag: str) -> bytes:
    """按附件名造出**真的能解出 flag** 的内容。

    ★ 每种附件对应一种"挖法"，这是故意的：基线要能看出模型卡在**哪一步** ——
      是不会解压，还是不会 base64，还是不会 strings。都做成"cat 一下就有"
      的话，十道题测的是同一件事。
    """
    if name == "source.zip":
        # 第 1 层：解压 → 读 flag.txt
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("flag.txt", flag + "\n")
            archive.writestr("app.py", "# 登录逻辑（占位）\nprint('hello')\n")
        return buffer.getvalue()

    if name == "wwwroot.zip":
        # 第 2 层：解压还不够，flag 埋在子目录的备份文件里
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "index.php",
                "<?php\n// 下载接口：include $_GET['file']; —— 这里没做路径过滤\n",
            )
            archive.writestr(".env", "DB_HOST=127.0.0.1\nDB_PASS=not_the_flag\n")
            archive.writestr(
                "backup/flag.php.bak",
                f"<?php\n// 备份文件忘了删：{flag}\n",
            )
        return buffer.getvalue()

    if name == "cipher.txt":
        # 第 3 层：base64 包一层
        token = base64.b64encode(flag.encode()).decode()
        return f"解一下这串 base64，得到的字符串就是 flag：\n{token}\n".encode()

    if name == "rsa.txt":
        # 第 4 层：得真的算（给齐 p q e c，标准解密）
        lines = [
            "RSA。参数都在下面，解出来的明文就是 flag。",
            "",
            *(f"{k} = {v}" for k, v in RSA_PARAMS.items()),
            "",
        ]
        return "\n".join(lines).encode()

    if name == "data.txt":
        # 第 5 层：hex → xxd -r -p（或 python bytes.fromhex）
        return f"十六进制，解出来就是 flag：\n{flag.encode().hex()}\n".encode()

    if name == "checker.c":
        # 第 6 层：读源码 —— flag 被拆成三段，得自己拼
        return (
            "/* 校验逻辑：把三段拼起来才是那个密码 */\n"
            "#include <stdio.h>\n"
            "#include <string.h>\n"
            "\n"
            "int main(void) {\n"
            f'    const char *a = "{flag[:5]}";\n'
            f'    const char *b = "{flag[5:13]}";\n'
            f'    const char *c = "{flag[13:]}";\n'
            "    char buf[64];\n"
            '    snprintf(buf, sizeof buf, "%s%s%s", a, b, c);\n'
            '    printf("password: %s\\n", buf);\n'
            "    return 0;\n"
            "}\n"
        ).encode()

    if name == "fmt":
        # 第 7 层：flag 被拆成两段埋在不同位置 —— 只捞到一段是交不对的
        noise = b"\x7fELF" + bytes(range(256)) * 2
        half = len(flag) // 2
        return (
            noise
            + f"\n[debug] token_part1 = {flag[:half]}\n".encode()
            + noise
            + f"\n[debug] token_part2 = {flag[half:]}\n".encode()
            + noise
        )

    if name in ("baby", "crackme", "dump.bin"):
        # 第 8 层：混在二进制噪声里 → strings 能捞出来
        noise = b"\x7fELF" + bytes(range(256)) * 2
        return noise + f"\n...strings 结果里有一行: {flag}\n".encode() + noise

    return f"mock attachment: {name}".encode()



#: 运行期状态（进程内，重启即清空 —— 这是 mock，不需要持久化）
ENV_STATE: dict[int, dict[str, Any]] = {}      # exerciseId → 环境状态
SUBMITTED: dict[int, list[str]] = {}           # exerciseId → 提交过的 flag
BUILT_AT: dict[int, float] = {}                # exerciseId → 起环境时刻


def ok(data: Any) -> JSONResponse:
    """文档里的成功信封。"""
    return JSONResponse({"code": "00000", "message": "", "data": data})


def fail(code: str, message: str, *, status: int = 200) -> JSONResponse:
    """文档里失败也是 200 + 非 00000 的 code（这一点很要紧：调用方不能只看 HTTP 状态）。"""
    return JSONResponse({"code": code, "message": message, "data": None}, status_code=status)


def build_app(access_key: str) -> FastAPI:
    app = FastAPI(title="Mock Arena API", docs_url=None, redoc_url=None)

    def check_key(key: str | None) -> JSONResponse | None:
        if not key:
            return fail("A0230", "缺少 X-Agent-AccessKey")
        if key != access_key:
            return fail("A0231", "AccessKey 无效")
        return None

    # ── 竞赛信息与公告 ──────────────────────────────────
    @app.get("/slab-match/api/v1/agent/match/notice/match-info")
    def match_info(x_agent_accesskey: str | None = Header(default=None)) -> JSONResponse:
        if (bad := check_key(x_agent_accesskey)) is not None:
            return bad
        return ok({"note": "不要攻击比赛平台本身。", "rule": "仅可使用下发接口。"})

    @app.get("/slab-match/api/v1/agent/answer-panel/overview")
    def overview(x_agent_accesskey: str | None = Header(default=None)) -> JSONResponse:
        if (bad := check_key(x_agent_accesskey)) is not None:
            return bad
        solved = sum(1 for item in EXERCISES.values() if item["hasSolved"])
        return ok({"stagePoint": float(solved) * 100.0, "stageRank": max(1, 20 - solved)})

    @app.get("/slab-match/api/v1/agent/match/notice/now-list")
    def notice_list(x_agent_accesskey: str | None = Header(default=None)) -> JSONResponse:
        if (bad := check_key(x_agent_accesskey)) is not None:
            return bad
        return ok(NOTICES)

    @app.get("/slab-match/api/v1/agent/match/notice/detail")
    def notice_detail(
        id: int = Query(...),  # noqa: A002 - 文档里就叫 id
        x_agent_accesskey: str | None = Header(default=None),
    ) -> JSONResponse:
        if (bad := check_key(x_agent_accesskey)) is not None:
            return bad
        item = next((n for n in NOTICES if n["id"] == id), None)
        if item is None:
            return fail("A0404", "公告不存在")
        return ok({**item, "isFile": False, "file": None, "url": None})

    # ── 题目 ────────────────────────────────────────────
    @app.get("/slab-match/api/v1/agent/ctf/exercise-list")
    def exercise_list(x_agent_accesskey: str | None = Header(default=None)) -> JSONResponse:
        if (bad := check_key(x_agent_accesskey)) is not None:
            return bad
        tree = []
        for category in CATEGORIES:
            corpus = []
            for exercise_id in category["corpus"]:
                item = EXERCISES[exercise_id]
                corpus.append(
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "order": len(corpus) + 1,
                        "isOpen": True,
                        "hasSolved": item["hasSolved"],
                    }
                )
            tree.append(
                {"id": category["id"], "name": category["name"], "order": category["order"], "corpus": corpus}
            )
        return ok(tree)

    @app.get("/slab-match/api/v1/agent/ctf/exercise")
    def exercise(
        exerciseId: int = Query(...),  # noqa: N803 - 文档里的参数名
        x_agent_accesskey: str | None = Header(default=None),
    ) -> JSONResponse:
        if (bad := check_key(x_agent_accesskey)) is not None:
            return bad
        item = EXERCISES.get(exerciseId)
        if item is None:
            return fail("A0404", "题目不存在")

        state = ENV_STATE.get(exerciseId, {})
        # ★ 环境异步：起过之后先"准备中"，两次查询后才给 endpoints
        building = state.get("polls", 0) < 2 if state.get("building") else False

        endpoints: list[dict[str, Any]] = []
        if state.get("ready"):
            endpoints = [
                # 非代理靶机：直接给 IP + 端口
                {
                    "exposeIps": ["10.20.30.40"],
                    "ports": ["80", "22"],
                    "users": [{"username": "admin", "password": "admin123"}],
                    "portMappings": [],
                    "proxyIps": [],
                    "isProxy": False,
                    "expireTime": int(time.time() * 1000) + 3600_000,
                },
                # 代理靶机：★ 优先用代理（proxyIps + portMappings）
                {
                    "exposeIps": ["10.20.30.41"],
                    "ports": ["9999"],
                    "users": [],
                    "portMappings": [{"type": "tcp", "port": "9999", "proxy": "31001"}],
                    "proxyIps": ["127.0.0.1"],
                    "isProxy": True,
                    "expireTime": int(time.time() * 1000) + 3600_000,
                },
            ]

        payload = {
            "id": item["id"],
            "name": item["name"],
            "description": item["description"],
            "hasSolved": item["hasSolved"],
            "score": item["score"],
            "difficulty": item["difficulty"],
            "attachment": item["attachment"],      # ★ 附件在 attachment.files 下
            "endpoints": endpoints,
            "canRefreshEndpoint": True,
            "isNeedInit": item["isNeedInit"],
            "endpointType": item["endpointType"],
            "currentTime": int(time.time() * 1000),
            "isNeedCheck": bool(building),
        }
        if state.get("building"):
            state["polls"] = state.get("polls", 0) + 1
            if state["polls"] >= 2:
                state["building"] = False
                state["ready"] = True
        return ok(payload)

    # ── 环境 ────────────────────────────────────────────
    @app.post("/slab-match/api/v1/agent/ctf/build-exercise-env")
    def build_env(
        # ⚠️ 用 `Annotated[...]` 而不是 `payload: dict = Body(...)`：
        #    后者会让 ruff 的 B008 报"不要在默认值里调用函数"，
        #    而这个写法既避开它、也是新版 FastAPI 推荐的写法。
        payload: Annotated[dict[str, Any], Body()],
        x_agent_accesskey: str | None = Header(default=None),
    ) -> JSONResponse:
        if (bad := check_key(x_agent_accesskey)) is not None:
            return bad
        exercise_id = payload.get("exerciseId")
        if exercise_id not in EXERCISES:
            return fail("A0404", "题目不存在")
        ENV_STATE[exercise_id] = {"building": True, "ready": False, "polls": 0}
        BUILT_AT[exercise_id] = time.time()
        return ok(None)

    @app.post("/slab-match/api/v1/agent/ctf/recover-exercise-env")
    def recover_env(
        payload: Annotated[dict[str, Any], Body()],
        x_agent_accesskey: str | None = Header(default=None),
    ) -> JSONResponse:
        if (bad := check_key(x_agent_accesskey)) is not None:
            return bad
        ENV_STATE.pop(payload.get("exerciseId"), None)
        return ok(None)

    # ── 提交 ────────────────────────────────────────────
    @app.post("/slab-match/api/v1/agent/answer-panel/answer")
    def answer(
        payload: Annotated[dict[str, Any], Body()],
        x_agent_accesskey: str | None = Header(default=None),
    ) -> JSONResponse:
        if (bad := check_key(x_agent_accesskey)) is not None:
            return bad
        exercise_id = payload.get("exerciseId")
        flag = str(payload.get("flag") or "")
        item = EXERCISES.get(exercise_id)
        if item is None:
            return fail("A0404", "题目不存在")
        if len(flag) > 256:
            return fail("A0400", "flag 最长 256 字符")

        history = SUBMITTED.setdefault(exercise_id, [])
        if flag in history:
            # 重复提交要有确定的返回 —— 调用方靠它判断"是不是已经交过"
            return fail("A0409", "该答案已提交过")
        history.append(flag)

        if flag == item["flag"]:
            item["hasSolved"] = True
            return ok({"isCorrect": True})
        return fail("A0401", "答案错误")

    # ── 附件（让下载与**解题**链路都能测）──────────────
    #
    # ★ 这些附件是**可解**的 —— 每道题的 flag 线索就藏在对应附件里。
    #
    # 为什么不把 flag 只放在服务端：那样"解题"这一步永远解不出来
    # （模型不可能猜到一个硬编码的字符串），端到端就退化成"下载附件 +
    # 提交一个必然错误的答案"。把线索放进附件，模型才真的要用工具
    # （解压 / base64 / strings）去把它挖出来 —— 那才是要测的东西。
    @app.get("/files/{name}")
    def files(name: str) -> Any:
        safe = Path(name).name
        flag = ATTACHMENT_FLAGS.get(safe)
        if flag is None:
            return JSONResponse({"code": "A0404", "message": "附件不存在", "data": None})

        # 具体造法在 build_attachment()（按"挖法"分层，见那个函数的 docstring）
        if safe.endswith(".zip"):
            media = "application/zip"
        elif safe.endswith((".txt", ".c")):
            media = "text/plain; charset=utf-8"
        else:
            media = "application/octet-stream"
        return Response(
            build_attachment(safe, flag),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )

    return app


FILES_DIR = Path(__file__).resolve().parent.parent / "_tmp" / "mock-arena-files"


def main() -> int:
    parser = argparse.ArgumentParser(description="比赛平台 API 的本地 mock 服务端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8901)
    parser.add_argument("--key", default="ak_test_local", help="X-Agent-AccessKey 的期望值")
    args = parser.parse_args()

    import uvicorn

    print(f"Mock Arena API 启动：http://{args.host}:{args.port}")
    print(f"  AccessKey：{args.key}")
    print(f"  Base URL ：http://{args.host}:{args.port}/slab-match/api/v1/agent")
    print("  题目：" + ", ".join(f"{item['id']}({item['name']})" for item in EXERCISES.values()))
    uvicorn.run(build_app(args.key), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
