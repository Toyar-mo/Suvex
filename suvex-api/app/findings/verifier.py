"""落 finding 之前的反证清单（v1.82）。

## 来源与边界

做法借鉴自 ARTEX 二开分支的 `chainskel` 反证判据（AGPL-3.0）：**按类别
给出「先证伪再下结论」的检查项**。这里不复制其代码或文案，只按同一
方法论独立撰写；判据本身是通用工程事实，不是实现细节。

## 为什么放在 finding 侧

`verification` 轴已经有 `verified/partial/none`，但它只表达**结果**，
不表达**凭什么**。这张清单让评审者能看到"该做的对照做了没有"，而不是
只看一个 `verified` 标签。与 `remediation` 一样，它是纯函数、不碰 DB。
"""

from __future__ import annotations

from typing import Any

#: 通用反证层：任何类别都先过这几条。
_GENERAL = (
    "200 不等于打通：拦截页/WAF 会伪造 200。必须有良性/恶意对照，只有差异化响应才算信号。",
    "结论必须锚定一个「只有成功才会出现」的特征（独有数据、线性延时、回连、文件落地），"
    "响应看起来正常或没有报错都不构成证据。",
    "复现失败时先排除环境、防护与时机因素，再判误报；无法复现又无法排除时标 inconclusive。",
)

#: 类别判据。键是标准化的类别名（见 `CLASS_ALIASES`）。
_CLASS_CHECKS: dict[str, tuple[str, ...]] = {
    "sqli": (
        "布尔/时间盲注都要有可复现的稳定差异；单次报错只说明输入进了 SQL 解析器，不证明可控。",
        "时间盲注先测无 payload 的基线耗时；延时须明显超出基线并重复出现。",
        "报错回显要能构造出受控输出（如版本函数、字面量）出现在响应里。",
    ),
    "xss": (
        "回显不等于可执行：确认 payload 所在上下文（HTML/属性/JS/URL），关键字符未被编码。",
        "存储型要由独立会话重新读取，确认已落库并按脚本渲染。",
        "仅扫描器特征命中、无实际回显时不得判 verified。",
    ),
    "ssrf": (
        "回连源 IP 必须是目标服务端出口，排除 CDN、浏览器端渲染造成的伪 SSRF。",
        "证明出网不等于影响成立：需触达内网/元数据/可改状态接口，或在报告中明确影响面后定级。",
    ),
    "cmdi": (
        "延时须与设定值线性对应才排除网络抖动；单次或非线性延时不可判。",
        "有回连或命令输出回显时优先作为证据，时间盲只作降级手段。",
    ),
    "upload": (
        "落地与解析要分别验证：文件可访问，且访问时按脚本解析执行；返回源码/直接下载即未解析。",
        "随机名或路径不可见时，确认落地点与解析行为之前不得判 verified。",
    ),
    "auth-bypass": (
        "至少用未认证/低权限/越权三种身份各跑一遍，明确 401/403 边界。",
        "只判「已登录」不够：要验证资源归属过滤（他人 id、跨租户）。",
    ),
    "info-leak": (
        "确认泄露字段来自服务端响应/日志/缓存，而不是前端展示层已经隐藏的内容。",
        "对照低权限身份：同一请求不应返回该字段；正常功能仍应返回必要字段。",
    ),
}

#: 把 `cwe` / `category` / 标题归一化到上面的类别。
CLASS_ALIASES: dict[str, str] = {
    "CWE-89": "sqli",
    "CWE-79": "xss",
    "CWE-918": "ssrf",
    "CWE-78": "cmdi",
    "CWE-77": "cmdi",
    "CWE-22": "upload",
    "CWE-434": "upload",
    "CWE-306": "auth-bypass",
    "CWE-284": "auth-bypass",
    "CWE-639": "auth-bypass",
    "CWE-200": "info-leak",
}

_CATEGORY_ALIASES: dict[str, str] = {
    "injection": "sqli",
    "xss": "xss",
    "ssrf": "ssrf",
    "broken_access_control": "auth-bypass",
    "auth": "auth-bypass",
    "info_disclosure": "info-leak",
}


def classify(cwe: str | None, category: str | None) -> str:
    """返回类别键；认不出时返回 `generic`。"""
    key = (cwe or "").strip().upper()
    if key in CLASS_ALIASES:
        return CLASS_ALIASES[key]
    return _CATEGORY_ALIASES.get((category or "").strip(), "generic")


def checks_for(cwe: str | None, category: str | None) -> dict[str, Any]:
    """返回 `{class, general, specific}`，给修复/评审界面直接渲染。"""
    klass = classify(cwe, category)
    return {
        "class": klass,
        "general": list(_GENERAL),
        "specific": list(_CLASS_CHECKS.get(klass, ())),
    }


__all__ = ["checks_for", "classify"]
