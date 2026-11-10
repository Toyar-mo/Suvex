"""漏洞的修复方案（v1.81，CodeMender 闭环的第一步）。

## 它是什么，不是什么

它把一条**已入库的发现**翻译成一份可审阅的修复计划：改什么、怎么改、
改完怎么验证。规则库按 CWE 给最小正确做法，未识别的 CWE 给通用的
「复核 → 定位 → 修复 → 验证」四步。

**不生成补丁、不提交代码**：与 `workorder.py` 同一条纪律 —— 执行永远是
人的事。补丁生成需要仓库上下文与回归验证，那是下一步，不是这一层。

## 为什么 readiness 要看 `verification`

一条 `verified`（L2）的发现可以直接进入修复；`partial`/`none` 的
只能先补验证。否则会出现"按建议改了半天，原来那条是误报" ——
这与 `evidence.py` 把 L0 挡在 findings 之外是同一条纪律。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 规则库。键是 CWE 编号（大写），值是"最小正确做法 + 验证方式"。
#:
#: 刻意只收**最常见、有确定做法**的几类；其余走通用四步，而不是编一条
#: 看起来具体、实际无法执行的建议。
_RULES: dict[str, RemedyRule] = {}


@dataclass(frozen=True)
class RemedyRule:
    title: str
    steps: tuple[str, ...]
    verification: tuple[str, ...]
    #: 统一 diff **草案**（`-` 旧行 / `+` 新行）。空 = 该类没有可给出的
    #: 示例；即使有，也永远不能直接 apply —— 见 `plan()` 的 caveats。
    patch: tuple[str, ...] = ()


def _rule(
    title: str,
    steps: tuple[str, ...],
    verification: tuple[str, ...],
    patch: tuple[str, ...] = (),
) -> RemedyRule:
    return RemedyRule(title=title, steps=steps, verification=verification, patch=patch)


_RULES.update(
    {
        "CWE-89": _rule(
            "SQL 注入：参数化查询",
            (
                "把拼接 SQL 改成参数化查询（驱动层的占位符 / ORM 绑定参数）。",
                "不要用字符串转义或黑名单替代参数化；它们挡不住类型转换与编码差异。",
                "如果必须动态拼接表名/列名，改为服务端白名单映射，不接受原始输入。",
            ),
            (
                "用同一请求的 replay 验证：注入 payload 与正常值返回结构一致。",
                "用 differential 对照：布尔/时间盲注的响应差异消失。",
            ),
            (
                '- cursor.execute("SELECT * FROM users WHERE id = " + user_id)',
                '+ cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))',
            ),
        ),
        "CWE-79": _rule(
            "XSS：上下文输出编码 + CSP",
            (
                "按输出上下文编码（HTML 正文 / 属性 / JS / URL 各有不同编码）。",
                "避免 innerHTML/文档写入；必须富文本时用成熟 sanitizer 并限定允许标签。",
                "加 CSP（至少禁止 inline script）作为纵深防御。",
            ),
            (
                "用无头浏览器执行 payload，确认脚本不再执行（XBOW 同款 validator）。",
                "replay 确认响应中的 payload 已被编码，而不是原样回显。",
            ),
            (
                "- element.innerHTML = userInput",
                "+ element.textContent = userInput  // 或按上下文做输出编码",
            ),
        ),
        "CWE-22": _rule(
            "路径穿越：规范化 + 根目录约束",
            (
                "对用户提供的路径做规范化（resolve）后，确认仍在允许的根目录内。",
                "拒绝绝对路径、`..` 段与盘符/UNC 形式；不要只做字符串替换。",
                "用服务端生成的文件 id 代替直接暴露文件名。",
            ),
            (
                "differential：`../`、编码变体、绝对路径都应返回拒绝，而不是文件内容。",
                "replay 确认合法文件仍可正常读取（修复没有误伤功能）。",
            ),
            (
                "- path = os.path.join(base, user_path)",
                "+ path = os.path.realpath(os.path.join(base, user_path))",
                "+ if os.path.commonpath([os.path.realpath(base), path]) != os.path.realpath(base):",
                '+     raise ValueError("outside root")',
            ),
        ),
        "CWE-918": _rule(
            "SSRF：出站白名单 + 解析后校验",
            (
                "对出站目标做协议 + 主机白名单；不要只依赖字符串黑名单。",
                "在 DNS 解析之后校验实际连接地址（防 DNS 重绑定），并禁用环境代理。",
                "对重定向逐跳校验，不能只校验第一个 URL。",
            ),
            (
                "differential：内网/metadata/等价 IP 写法都应被拒绝，公网白名单仍可用。",
                "检查错误信息不回显内网地址或响应内容。",
            ),
            (
                "- return requests.get(user_url)",
                "+ ip = socket.gethostbyname(urlsplit(user_url).hostname)",
                "+ if not is_public(ip) or host not in ALLOWED_HOSTS:",
                '+     raise ValueError("blocked")',
                "+ return requests.get(user_url, allow_redirects=False)",
            ),
        ),
        "CWE-306": _rule(
            "缺失鉴权：统一鉴权中间件",
            (
                "把鉴权收敛到路由/中间件层的统一装饰器，而不是每个 handler 自己判。",
                "默认拒绝：新增端点必须显式声明公开或所需 scope。",
                "按资源归属过滤（created_by/tenant），不要只判\"已登录\"。",
            ),
            (
                "未认证 / 只读 / 越权三种身份各跑一遍，断言 401/403 边界。",
                "对照：合法身份仍能完成原操作。",
            ),
        ),
        "CWE-798": _rule(
            "硬编码凭据：外置到密钥管理",
            (
                "把凭据从源码/配置默认值移到环境变量或密钥管理服务。",
                "立即轮换已泄露的凭据；删掉文件不等于从历史移除。",
                "加 CI 扫描（形状匹配）防止再次入库。",
            ),
            (
                "确认代码与镜像里不再出现该值；旧凭据已失效。",
                "启动自检：缺少必要环境变量时明确失败，而不是回退到默认值。",
            ),
            (
                '- API_KEY = "sk-live-..."',
                '+ API_KEY = os.environ["API_KEY"]  # 缺失时启动失败',
            ),
        ),
        "CWE-200": _rule(
            "信息泄露：统一错误页与响应裁剪",
            (
                "生产环境关闭堆栈/调试信息，错误响应只给稳定错误码与请求 id。",
                "按角色裁剪响应字段（密钥、内部路径、其他用户数据）。",
                "检查缓存与日志：同一份敏感内容不能只在前端隐藏。",
            ),
            (
                "未认证/低权限身份请求，确认响应不再含敏感字段。",
                "replay 确认正常功能仍返回必要字段。",
            ),
        ),
    }
)

_DEFAULT = _rule(
    "通用修复：复核 → 定位 → 修复 → 验证",
    (
        "先用可重放验证器（replay / differential）确认这条发现可复现，再动手。",
        "定位到产生该行为的代码路径（请求参数 → 处理函数 → 数据/系统调用）。",
        "在根因处修复，不要在输出端做一次性过滤。",
        "修复后跑同一验证器，并补一条回归测试（同一输入不再触发）。",
    ),
    (
        "replay：修复前后同一请求的响应差异符合预期。",
        "differential：攻击输入与对照输入的差异消失，合法输入不受影响。",
    ),
)


def plan(finding: Any) -> dict[str, Any]:
    """把一条 finding 翻译成修复计划。纯函数，不碰 DB/网络。

    接受 ORM `Finding` 或任何有 `id/cwe/category/verification` 属性的对象
    （测试用 `SimpleNamespace`）。
    """
    cwe = str(getattr(finding, "cwe", "") or "").strip().upper()
    category = str(getattr(finding, "category", "") or "").strip()
    verification = str(getattr(finding, "verification", "") or "").strip()
    rule = _RULES.get(cwe, _DEFAULT)
    verified = verification == "verified"

    steps = list(rule.steps)
    if not verified:
        steps.insert(
            0,
            "这条目前不是 L2（独立验证）：先用 verify_http 做 replay/differential，"
            "把证据补到可重放，再按下面步骤修复。",
        )
    return {
        "findingId": str(getattr(finding, "id", "") or ""),
        "cwe": cwe,
        "category": category,
        "readiness": "ready" if verified else "needs_verification",
        "title": rule.title,
        "steps": steps,
        "verification": list(rule.verification),
        "patch": (
            {
                "format": "unified-diff-draft",
                "appliesDirectly": False,
                "diff": "\n".join(rule.patch),
                "caveats": [
                    "这是结构示意，不是针对本仓库的补丁。",
                    "应用前必须替换成真实的文件路径、框架与类型。",
                    "改完必须跑仓库测试与该发现的 replay/differential。",
                ],
            }
            if rule.patch
            else None
        ),
        "note": (
            "补丁是草案，不能直接 apply；计划与代码修改都由人完成。"
            if verified
            else "先补验证：未达到 L2 的发现按建议修复，可能是在修一条误报。"
        ),
    }


__all__ = ["RemedyRule", "plan"]
