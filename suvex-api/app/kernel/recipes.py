"""工具配方（tool recipes）—— 学 CyberStrikeAI 的 `tools/*.yaml`。

## 这个模块补的是什么

`tools.py` 的 `TOOL_SPECS` 回答的是「**智能体**能调哪几个工具」—— 12 个，
是 ACTION 协议里的动作（`run_shell` / `http_request` / `call_mcp` …）。

它**不**回答另一个问题：「`run_shell` 里我能跑哪些安全工具，怎么跑才不是
白跑一趟？」

后者是配方要回答的。一个配方 = 一个外部 CLI 的名字 + 默认参数 + 参数怎么
拼 + **默认参数下它做不到什么**。举一个真实例子：`nuclei` 在默认参数下
如果模板目录是空的，它会直接 `[FTL] Could not run nuclei: no templates
provided for scan` 退出 —— 而模型看到的是「运行完了、没命中」。同一个目标、
同一工具、只补上 `-t /path/to/templates` 就命中 2 条。这个差别不写在配方
里，模型永远学不到。

## ★ 核心约束：配方必须带「本机有没有这个二进制」的实测

这是本模块与 CyberStrikeAI 那份 YAML **最重要的差别**，理由是实测出来的：

    2026-10-25 实测 `suvex-sandbox:latest`：CyberStrikeAI 的 90 个配方里
    只有 28 个的 command 能在镜像里 `command -v` 到。其余 62 个（amass /
    katana / subfinder / wpscan / metasploit / zap / ghidra / checkov /
    trivy / volatility3 …）在这个镜像里根本不存在。

如果照搬那 90 条，结果是一份漂亮的能力清单 + 62 个「素材库里没有的菜」：
模型照着配方写命令，沙箱回 `sh: 1: amass: not found`，白烧一个回合。
更糟的是它**看起来像「工具跑了但没结果」**，这与「目标确实没有」无法区分 ——
正是本项目反复在抓的那类静默失真。

所以：配方是**数据**，是否可用是**探测结果**，两者分开存。探测走
`probe_command()` + `parse_probe()`（一次 `sh -c` 批量 `command -v`），
由调用方缓存 —— 它在每个任务开始时都要用，而每次去 exec 一遍是纯浪费。

## 参数拼接：三种 format

CyberStrikeAI 的 YAML 用 `format: flag | positional | template` 三种：

  - `flag`         `-p 80`（`flag` 字段写开关名）
  - `positional`   按 `position` 排序后直接摆进命令行
  - `template`     `-T{value}`（模板里 `{value}` 替换成参数值）

我们照抄这三种，因为它们是**够用的最小集合**：实测 90 个配方里 269 个
`flag` + 184 个 `positional` + 3 个 `template`，没有第四种。

## 为什么不直接读 CyberStrikeAI 的 YAML 文件

它是 Apache-2.0，允许派生但要求署名。而我们的部署里**没有那个仓库** ——
运行时去读一个不存在的目录，等于把「能跑」绑在别人的 git 检出上。
所以这里是一份**按我们的沙箱实测筛过的**子集：只保留能在
`suvex-sandbox:latest` 里跑起来的那 28 个，加上 `exec`。出处与署名见文件
末尾 `_ATTRIBUTION`。

## 为什么 `exec` 也在里面

用户点名要它。它在我们这里不是新工具 —— `run_shell` 已经是它了，而且
执行位置更严（独立容器、`--network none`、`--read-only`、`--cap-drop ALL`）。
把它列成配方的作用是：让界面上「能跑什么」清单里出现它，且
`default_limitation` 写清「我们是容器不是宿主」，避免模型以为能像
CyberStrikeAI 那样摸宿主机。
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════


#: 参数的拼法。取值集合刻意只有三种（见文件头「参数拼接」）。
PARAM_FORMATS: Final[frozenset[str]] = frozenset({'flag', 'positional', 'template'})


@dataclass(frozen=True)
class RecipeParam:
    """一个配方的参数。

    ## `default` 与 `required` 的差别

    `required=True` 是「不给我就不该跑」（`nmap` 没有 target 没意义）。
    `default='4'` 是「不给我就用这个值」（`nmap` 的时序默认 `-T4`）。

    两者都会真的拼进命令行 —— 差别只在「省略时是否还算一次合法调用」。
    混用它们会出现「什么参数都不给，工具照样跑了、跑的是默认值、模型以为
    自己配过了」，所以这里分开表达。
    """

    name: str
    #: `flag` / `positional` / `template`。见 `PARAM_FORMATS`。
    format: str
    #: 一句话说明（中文）。给界面与提示词用。
    description: str = ''
    required: bool = False
    #: `flag` 格式的开关名（如 `-p`）。
    flag: str = ''
    #: `positional` 格式的摆放顺序（小的在前）。
    position: int = 0
    #: `template` 格式的模板串，含 `{value}`。
    template: str = ''
    #: 省略时的取值。拼进命令行时与显式给出等价。
    default: str = ''


@dataclass(frozen=True)
class ToolRecipe:
    """一个外部 CLI 的调用配方。

    ## `default_limitation` 是本模块存在的主要理由

    见 `tools.py::ToolSpec.default_limitation` 那一大段。这里再说一遍
    因为它在这个模块里更尖锐：外部 CLI 的默认参数**几乎总是在缩水**
    （nmap 默认只扫约 1000 个端口、ffuf 没字典就退化成空跑、nuclei 没模板
    直接 FTL 退出）。不写清，模型会把「缩水的输出」读成「目标就是这样」。
    """

    #: 配方名（= 二进制名，`exec` 除外）。模型与界面都按它索引。
    name: str
    #: 一句话说明（中文）。
    summary: str
    #: 分组。见 `RECIPE_GROUPS`。
    group: str
    #: 真正要执行的命令。通常等于 `name`，`exec` 是 `sh`。
    command: str
    #: 无条件前置参数（如 nmap 的 `-sT -sV`）。
    args: tuple[str, ...] = ()
    #: 参数定义。顺序即界面顺序。
    params: tuple[RecipeParam, ...] = ()
    #: ★ 默认参数下它**做不到**什么（空串 = 默认就是全功能）。
    default_limitation: str = ''
    #: 这些退出码不算失败。
    #:
    #: 为什么需要：`ffuf` 没命中时退出码是 1（`-mc` 不含任何匹配），
    #: `gobuster` 没找到也是非 0。若把非 0 一律当失败，模型会看到
    #: 「命令失败」而不知道「它其实跑对了、只是没东西」。
    allowed_exit_codes: tuple[int, ...] = (0,)


#: 分组名。顺序即界面显示顺序 —— 从「开局最常用」到「最专用」。
RECIPE_GROUPS: Final[tuple[str, ...]] = (
    '执行',
    '侦察',
    'Web 探测',
    '漏洞验证',
    '凭据与破解',
    '逆向与取证',
)


def _p(  # noqa: PLR0913 - 参数名就是配方 DSL，拆开反而更难读
    name: str,
    fmt: str,
    description: str = '',
    *,
    required: bool = False,
    flag: str = '',
    position: int = 0,
    template: str = '',
    default: str = '',
) -> RecipeParam:
    """构造一个参数。`fmt` 是位置参数，因为它是最常写的那个。

    ⚠️ 这里不校验 `fmt` 的合法性 —— 由模块末尾的
    `_assert_recipes_consistent()` 一次校验全部，比逐个构造时校验快得多，
    也让拼错的那个在 import 时就炸（而不是等到模型调它）。
    """
    return RecipeParam(
        name=name,
        format=fmt,
        description=description,
        required=required,
        flag=flag,
        position=position,
        template=template,
        default=default,
    )


# ══════════════════════════════════════════════════════════
# 配方清单
# ══════════════════════════════════════════════════════════
#
# 收录标准（三条，缺一不可）：
#   1. 二进制在 `suvex-sandbox:latest` 里 `command -v` 得到（2026-10-25 实测）
#   2. 在 CTF / 靶场里有明确用途（不是「也许某天用得上」）
#   3. 能写出「默认参数下做不到什么」—— 写不出这条说明我没真正用它跑过

RECIPE_SPECS: Final[tuple[ToolRecipe, ...]] = (
    # ── 执行 ──────────────────────────────────────────────
    ToolRecipe(
        name='exec',
        summary='在沙箱容器里执行一条 shell 命令（就是 run_shell 的配方形态）。',
        group='执行',
        command='sh',
        args=('-c',),
        params=(
            _p('command', 'positional', '要执行的命令。', required=True, position=0),
        ),
        # ★ 这一条是与 CyberStrikeAI 最大的不同，必须写明：它们的 exec 是
        #   宿主机 `sh -c`，我们是独立容器。不写清，模型会去摸宿主机路径。
        default_limitation=(
            '执行位置是**独立沙箱容器**（无网络、只读根、无特权），不是宿主机。'
            '宿主机的文件、进程、网络一概看不到；能写的只有 /work。'
        ),
    ),
    # ── 侦察 ──────────────────────────────────────────────
    ToolRecipe(
        name='nmap',
        summary='端口与服务扫描：TCP 连接扫描 + 版本检测 + 默认 NSE 脚本。',
        group='侦察',
        command='nmap',
        args=('-sT', '-sV', '-sC'),
        params=(
            _p('target', 'positional', 'IP / 主机名 / CIDR / 域名。', required=True, position=1),
            _p('ports', 'flag', '端口范围，如 80 / 1-1000 / 80,443。', flag='-p'),
            _p('timing', 'template', '时序 T0–T5，数字越大越快。', template='-T{value}'),
            _p('nse_scripts', 'flag', '自定义 NSE 脚本，如 vuln / http-*。', flag='--script'),
        ),
        default_limitation=(
            '默认只扫 nmap 的常用端口表（约 1000 个），**不是 65535 全端口**；'
            '要全扫必须显式给 `ports=1-65535`，而那会慢几个数量级。'
            '另外 `-O`/`-A`（OS 检测）需要 root，容器里是非 root，会被 nmap 拒绝。'
        ),
    ),
    ToolRecipe(
        name='masscan',
        summary='超高速端口扫描（异步发包），适合大网段先摸开口。',
        group='侦察',
        command='masscan',
        params=(
            _p('target', 'positional', 'CIDR 或 IP 范围。', required=True, position=1),
            _p('ports', 'flag', '端口范围，必填（masscan 不给就什么都不扫）。', required=True, flag='-p'),
            _p('rate', 'flag', '发包速率 packets/s。', flag='--rate', default='1000'),
        ),
        default_limitation=(
            'masscan 只回答「这个端口开着」，**不做服务识别、不做版本判断**。'
            '拿到开口之后必须再用 nmap -sV 或 curl 去看那是什么。'
            '速率给太高会丢包并给出假阴性（看起来端口关着）。'
        ),
    ),
    ToolRecipe(
        name='rustscan',
        summary='先用极快速度找开放端口，再自动交给 nmap 做识别。',
        group='侦察',
        command='rustscan',
        params=(
            _p('target', 'positional', '目标 IP / 主机名。', required=True, position=1),
            _p('ports', 'flag', '端口范围，如 1-65535。', flag='-p'),
        ),
        default_limitation=(
            '它默认会调高文件描述符上限并并发扫全部端口，在容器里可能'
            '因为 ulimit 上限而中途报错（不是目标问题）。失败时退回 nmap。'
        ),
    ),
    # ── Web 探测 ──────────────────────────────────────────
    ToolRecipe(
        name='ffuf',
        summary='高速 Web 目录/参数/子域模糊测试（要自带字典）。',
        group='Web 探测',
        command='ffuf',
        params=(
            _p('url', 'flag', '目标 URL，用 FUZZ 标注入点，如 http://t/FUZZ。', required=True, flag='-u'),
            _p('wordlist', 'flag', '字典路径，如 /usr/share/wordlists/...。', required=True, flag='-w'),
            _p('match_codes', 'flag', '匹配的状态码，如 200,301,403。', flag='-mc'),
            _p('filter_size', 'flag', '过滤掉这个响应长度（去掉默认页）。', flag='-fs'),
            _p('threads', 'flag', '并发数。', flag='-t', default='40'),
        ),
        default_limitation=(
            '**必须给字典**。不给 `-w` 它直接报错退出，不是「扫了没结果」。'
            '没命中时 ffuf 的退出码非 0 —— 那是「跑对了但没东西」，不是执行失败。'
        ),
        allowed_exit_codes=(0, 1),
    ),
    ToolRecipe(
        name='gobuster',
        summary='目录/子域/虚拟主机暴力枚举（Go 写的，稳定）。',
        group='Web 探测',
        command='gobuster',
        params=(
            _p('mode', 'positional', '模式：dir / dns / vhost。', required=True, position=1),
            _p('url', 'flag', '目标 URL（dir/vhost）或域名（dns）。', required=True, flag='-u'),
            _p('wordlist', 'flag', '字典路径。', required=True, flag='-w'),
            _p('extensions', 'flag', '追加扩展名，如 php,html,txt。', flag='-x'),
        ),
        default_limitation=(
            '同样**必须给字典**。默认并发与超时较保守，大字典会跑很久；'
            '要更快得显式加 `--threads`（它比 ffuf 慢，因为是单线程队列模型）。'
        ),
        allowed_exit_codes=(0, 1),
    ),
    ToolRecipe(
        name='feroxbuster',
        summary='递归式内容发现（自动往命中的目录里继续扫）。',
        group='Web 探测',
        command='feroxbuster',
        params=(
            _p('url', 'flag', '目标 URL。', required=True, flag='-u'),
            _p('wordlist', 'flag', '字典路径。', flag='-w'),
            _p('depth', 'flag', '递归深度。', flag='-d', default='4'),
        ),
        default_limitation=(
            '默认开递归，容易在「无限生成的路径」上失控（比如日历、分页）。'
            '碰到这种目标要显式 `-d 1` 关掉递归，否则它会把配额烧光。'
        ),
        allowed_exit_codes=(0, 1),
    ),
    ToolRecipe(
        name='dirsearch',
        summary='中文圈常用的目录扫描器（内置字典，开箱能跑）。',
        group='Web 探测',
        command='dirsearch',
        params=(
            _p('url', 'flag', '目标 URL。', required=True, flag='-u'),
            _p('extensions', 'flag', '扩展名，如 php,asp,zip。', flag='-e'),
            _p('threads', 'flag', '并发数。', flag='-t', default='30'),
        ),
        default_limitation=(
            '它**自带**字典（这是它比 ffuf 好上手的地方），但内置字典只有约 1 万条，'
            '不如 dirbuster/raft 的大字典全。想要更全要显式 `-w /path/to/wordlist`。'
        ),
        allowed_exit_codes=(0, 1),
    ),
    ToolRecipe(
        name='nikto',
        summary='Web 服务器配置缺陷扫描（过期软件、危险文件、默认页）。',
        group='Web 探测',
        command='nikto',
        params=(
            _p('host', 'flag', '目标 URL 或主机。', required=True, flag='-h'),
            _p('port', 'flag', '端口（URL 里没写时用）。', flag='-p'),
            _p('tuning', 'flag', '扫描类型：1=有趣文件 2=配置 3=泄露 9=SQL。', flag='-T'),
        ),
        default_limitation=(
            '它以**已知特征库**为准，对现代框架（SPA、纯 API）基本无话可说 ——'
            '回零条不等于安全，只等于「它不认识这个目标」。它还会主动请求几百个'
            '常见路径，对生产目标是明显的扫描噪音。'
        ),
        allowed_exit_codes=(0, 1),
    ),
    ToolRecipe(
        name='wafw00f',
        summary='识别目标前面有没有 WAF、是哪一家。',
        group='Web 探测',
        command='wafw00f',
        params=(
            _p('url', 'positional', '目标 URL。', required=True, position=1),
        ),
        default_limitation=(
            '只识别**它认识的那几十家** WAF。回「没有 WAF」的准确含义是'
            '「我这些指纹都没匹配上」，可能是自研 WAF、可能是 CDN 隐藏。'
            '判断 WAF 存在更可靠的办法是打一个明显恶意 payload 看响应差异。'
        ),
    ),
    ToolRecipe(
        name='arjun',
        summary='发现隐藏的 HTTP 查询参数。',
        group='Web 探测',
        command='arjun',
        params=(
            _p('url', 'flag', '目标 URL（不要带 ? 参数）。', required=True, flag='-u'),
            _p('method', 'flag', '请求方法 GET/POST。', flag='-m', default='GET'),
            _p('wordlist', 'flag', '自定义参数字典。', flag='-w'),
        ),
        default_limitation=(
            '默认字典约 2.5 万个常见参数名，能发现「常规命名」的参数；'
            '自研接口里的业务参数（如 orderNo、thirdToken）基本找不到。'
            '它的判断依据是响应差异，所以对「参数错了也返回 200」的目标不灵。'
        ),
    ),
    ToolRecipe(
        name='dalfox',
        summary='XSS 扫描器（DOM/反射/存储，自带 payload 集）。',
        group='漏洞验证',
        command='dalfox',
        params=(
            _p('url', 'positional', '目标 URL（可带参数）。', position=1),
            _p('input_file', 'flag', '从文件读 URL 列表。', flag='--input-file'),
            _p('blind', 'flag', '带外回调地址（XSS 盲打）。', flag='-b'),
        ),
        default_limitation=(
            '它的命中是**机审候选**：报「XSS found」往往只是「payload 原样出现在」'
            '「响应里」。要确认必须自己看那个响应、确认它落在可执行上下文（标签内、'
            '属性外）、且被浏览器真的执行。别把扫描器的 finding 当已验证漏洞。'
        ),
    ),
    ToolRecipe(
        name='xsser',
        summary='老牌 XSS 探测工具，覆盖面广但噪音大。',
        group='漏洞验证',
        command='xsser',
        params=(
            _p('url', 'flag', '目标 URL。', required=True, flag='-u'),
        ),
        default_limitation=(
            '它很老（Python 2 时代的 payload 集），现代浏览器的 CSP、'
            '`X-Content-Type-Options` 会让它的多数 payload 无效，而它不感知这些。'
            '优先用 dalfox，这个作为补充。'
        ),
        allowed_exit_codes=(0, 1),
    ),
    # ── 漏洞验证 ──────────────────────────────────────────
    ToolRecipe(
        name='sqlmap',
        summary='SQL 注入检测与利用（探测 → 确认 → 拖库）。',
        group='漏洞验证',
        command='sqlmap',
        params=(
            _p('url', 'flag', '目标 URL；用 * 标注入点，如 http://t/p?id=1*。', required=True, flag='-u'),
            _p('data', 'flag', 'POST 数据，如 id=1&submit=go。', flag='--data'),
            _p('batch', 'flag', '非交互（不给就等你输入 y/n，在容器里会挂住）。', flag='--batch', default='1'),
            _p('level', 'flag', '探测强度 1-5。', flag='--level', default='1'),
            _p('risk', 'flag', '风险等级 1-3。', flag='--risk', default='1'),
            _p('tamper', 'flag', 'WAF 绕过脚本，如 space2comment。', flag='--tamper'),
        ),
        # ★ 这一条是最贵的一条：不给 --batch，它会停下来问 y/n，然后超时。
        #   模型看到「命令超时」会以为是网络慢，实际是在等输入。
        default_limitation=(
            '**默认 `--level=1 --risk=1` 只做最浅的一层探测**：很多注入点（尤其是'
            '盲注、需要特定 payload 位置、需要绕 WAF 的）在这个级别下回'
            '「not injectable」。那个结论不可信 —— 加大到 `--level=3 --risk=2` '
            '并补 `--tamper` 再看，才谈得上「排除」。'
            '另外它必须 `--batch`，否则会停下来等交互输入而超时。'
        ),
        allowed_exit_codes=(0, 1),
    ),
    ToolRecipe(
        name='nuclei',
        summary='基于 YAML 模板的漏洞扫描（社区模板库，覆盖面最广）。',
        group='漏洞验证',
        command='nuclei',
        params=(
            _p('url', 'flag', '目标 URL。', flag='-u'),
            _p('list', 'flag', '目标列表文件。', flag='-l'),
            _p('templates', 'flag', '模板目录或单个模板路径。', flag='-t'),
            _p('tags', 'flag', '按标签选模板，如 cve,rce,exposure。', flag='-tags'),
            _p('severity', 'flag', '按严重度选，如 critical,high。', flag='-severity'),
        ),
        # ★★ 这条是整个模块里最要紧的一条 —— 实测过。
        default_limitation=(
            '**默认参数下它连一个模板都没有**，会直接 '
            '`[FTL] Could not run nuclei: no templates provided for scan` 退出。'
            '镜像里装了 nuclei 二进制但**没装模板库**（2026-10-25 实测 '
            '/home/solver、/root、/usr/share、/opt 下都没有 nuclei-templates）。'
            '所以：要么显式给 `templates=/path/to/templates`（那里必须真有 '
            'yaml），要么别用它。★ 它「退出码 0 但什么都没扫」的样子与'
            '「目标没有漏洞」无法区分 —— 看到没输出先确认模板加载了。'
        ),
    ),
    ToolRecipe(
        name='hydra',
        summary='在线口令爆破（ssh/ftp/http-* 等 50+ 协议）。',
        group='凭据与破解',
        command='hydra',
        params=(
            _p('target', 'positional', '目标 IP / 主机。', required=True, position=1),
            _p('service', 'positional', '协议，如 ssh / ftp / http-post-form。', required=True, position=2),
            _p('user', 'flag', '单个用户名。', flag='-l'),
            _p('userlist', 'flag', '用户名字典。', flag='-L'),
            _p('passlist', 'flag', '口令字典。', flag='-P'),
            _p('threads', 'flag', '并发数。', flag='-t', default='16'),
        ),
        default_limitation=(
            '**必须有字典**（`-P`）。它不做用户名枚举，也不做口令策略推断 ——'
            '它就是把字典逐条打上去。字典里没有那个口令就永远爆破不出来，'
            '而回「0 valid passwords」会被误读成「口令很强」。'
            '另外 `http-post-form` 需要精确的失败特征串，写错会全部报成功。'
        ),
        allowed_exit_codes=(0, 1, 255),
    ),
    # ── 逆向与取证 ────────────────────────────────────────
    ToolRecipe(
        name='binwalk',
        summary='固件/镜像里的嵌入文件与文件系统识别、提取。',
        group='逆向与取证',
        command='binwalk',
        params=(
            _p('file', 'positional', '要分析的文件。', required=True, position=1),
            _p('extract', 'flag', '自动提取识别到的内容（`-e`）。', flag='-e'),
        ),
        default_limitation=(
            '只做**签名匹配**，不保证提取出来的是完整可挂载的文件系统'
            '（被裁剪、加密、压缩过的常见）。`-e` 提取失败时它会安静地'
            '只留一个空目录，看起来像「提取成功了但里面没东西」。'
        ),
    ),
    ToolRecipe(
        name='foremost',
        summary='按文件头尾雕刻（carving）恢复文件，适合磁盘镜像。',
        group='逆向与取证',
        command='foremost',
        params=(
            _p('file', 'flag', '输入文件/镜像。', required=True, flag='-i'),
            _p('output', 'flag', '输出目录。', flag='-o', default='/work/foremost_out'),
            _p('types', 'flag', '要恢复的类型，如 jpg,png,pdf,zip。', flag='-t'),
        ),
        default_limitation=(
            '它按**文件头尾特征**雕刻，不认识文件系统结构 —— 碎片化的文件'
            '会恢复出损坏内容，而它不会告诉你哪些是坏的。恢复出来的'
            '每个文件都要自己验证能不能打开。'
        ),
    ),
    ToolRecipe(
        name='exiftool',
        summary='读写图片/文档/媒体的元数据（GPS、相机、软件版本）。',
        group='逆向与取证',
        command='exiftool',
        params=(
            _p('file', 'positional', '文件路径。', required=True, position=1),
        ),
        default_limitation=(
            '只读元数据，**不做隐写分析**。图片里藏了数据它看不到'
            '（那是 steghide/zsteg/binwalk 的事）。'
            '元数据可能被抹掉 —— 没有 GPS 不等于不是在现场拍的。'
        ),
    ),
    ToolRecipe(
        name='steghide',
        summary='JPEG/BMP/WAV 隐写提取（需要口令，可空口令）。',
        group='逆向与取证',
        command='steghide',
        params=(
            _p('extract', 'flag', '提取模式（`-x`）。', flag='-x', default='1'),
            _p('file', 'flag', '载体文件。', flag='-sf'),
            _p('passphrase', 'flag', '口令；空口令要显式给空串。', flag='-p', default=''),
        ),
        default_limitation=(
            '只支持 JPEG/BMP/WAV/AU，**不认 PNG**。口令错时它会返回'
            '「could not extract any data with that passphrase」—— 那句话'
            '既可能是「口令错」，也可能是「这里根本没有隐写」。要区分得先用'
            '`steghide info` 确认载体里真有嵌入数据。'
        ),
        allowed_exit_codes=(0, 1),
    ),
    ToolRecipe(
        name='strings',
        summary='从二进制里抽出可打印字符串（找硬编码口令、路径、URL）。',
        group='逆向与取证',
        command='strings',
        params=(
            _p('file', 'positional', '文件路径。', required=True, position=1),
            _p('min_len', 'flag', '最短长度。', flag='-n', default='4'),
            _p('encoding', 'flag', '编码：s=单字节 l=16位小端 b=16位大端。', flag='-e'),
        ),
        default_limitation=(
            '默认**只抽 ASCII 且长度 >=4**。UTF-16 字符串（Windows PE 里到处都是）'
            '要显式 `-e l`；更短的（如 3 字符口令）要 `-n 3`。'
            '没看到不等于没有 —— 被压缩/加密的节区抽不出可读字符串。'
        ),
    ),
    ToolRecipe(
        name='objdump',
        summary='反汇编与 ELF/PE 结构解析（节区、符号、重定位）。',
        group='逆向与取证',
        command='objdump',
        params=(
            _p('mode', 'flag', '动作：`-d` 反汇编 / `-t` 符号表 / `-h` 节区。', flag='-d', default='1'),
            _p('file', 'positional', '文件路径。', required=True, position=1),
        ),
        default_limitation=(
            '默认 `-d` **只反汇编可执行节区**，数据节区里的代码（常见于 shellcode、'
            '加壳程序）看不到。要看全部用 `-D`。另外它不做交叉引用、不做控制流 ——'
            '要「看懂逻辑」得上反编译器（radare2/ghidra）。'
        ),
    ),
    ToolRecipe(
        name='radare2',
        summary='逆向工程框架：反汇编、调试、打补丁、脚本化分析。',
        group='逆向与取证',
        command='radare2',
        params=(
            _p('file', 'positional', '二进制路径。', required=True, position=1),
            _p('commands', 'flag', '要执行的 r2 命令（`-c`），多条用 ; 分隔。', flag='-c'),
            _p('analysis', 'flag', '打开就先做分析（`-A`）。', flag='-A', default='1'),
        ),
        default_limitation=(
            '必须用 `-c` 给命令，否则它会进交互式 shell 然后挂住等输入（与 '
            'sqlmap 不给 --batch 同类事故）。'
            '`-A` 分析对加壳/混淆样本经常失败且不报错 —— 输出里没有函数不代表没有函数。'
        ),
    ),
    ToolRecipe(
        name='gdb',
        summary='GNU 调试器（PWN 题的运行时分析、寄存器/栈观察）。',
        group='逆向与取证',
        command='gdb',
        params=(
            _p('program', 'positional', '要调试的程序。', position=1),
            _p('commands', 'flag', '要执行的 gdb 命令（`-ex`），可重复。', flag='-ex'),
            _p('batch', 'flag', '批处理模式，跑完就退（`-batch`）。', flag='-batch', default='1'),
        ),
        default_limitation=(
            '容器里**没有 pwndbg/gef 插件**（实测 `gdb -batch -ex checksec` 回 '
            '「Undefined command」，因为 /etc/gdb/gdbinit 没加载任何插件），所以 '
            'checksec/vmmap/cyclic 这些命令在 gdb 里不存在，别写。'
            '★ 但 **pwntools 装了**（`pwn` 命令可用，`pwn cyclic 20` 实测正常）—— '
            '要在 gdb 外用 `pwn checksec` 或写 pwntools 脚本，不要在 gdb 里找它们。'
            '必须带 `-batch -ex`，否则进交互式挂住。'
            '没有 ptrace 权限时 attach 一个已在跑的进程会失败。'
        ),
    ),
    ToolRecipe(
        name='checksec',
        summary='看二进制的防护：NX / PIE / Canary / RELRO / Fortify。',
        group='逆向与取证',
        # ★ 实测：镜像里的 `checksec` 是 **pwntools 的 `pwn checksec`**
        #   （`/opt/venv/bin/checksec`，`--help` 打的是 `usage: pwn checksec`），
        #   不是独立的 checksec.sh。它的参数是 `--file=` 或直接位置参数。
        command='checksec',
        params=(
            _p('file', 'flag', '二进制路径。', required=True, flag='--file'),
        ),
        default_limitation=(
            '只读 ELF 头与节区标记，**不做实际验证**。比如它报「Canary found」'
            '只代表有 `__stack_chk_fail` 引用；报「NX enabled」不代表每一段都不可执行。'
            '它也不告诉你漏洞在哪 —— 它只回答「这道题的防护组合是什么」。'
            '★ 它依赖 pwntools，启动时会打一行 `_curses.error: setupterm` 警告'
            '（容器里没有 terminfo）—— 那是**噪音不是失败**，解析时忽略第一段。'
        ),
    ),
    ToolRecipe(
        name='hashcat',
        summary='GPU/CPU 口令破解（支持 300+ 哈希类型）。',
        group='凭据与破解',
        command='hashcat',
        params=(
            _p('hashfile', 'positional', '哈希文件路径。', required=True, position=1),
            _p('wordlist', 'positional', '字典路径。', position=2),
            _p('hash_type', 'flag', '哈希类型编号（`-m`），如 0=MD5 1000=NTLM。', required=True, flag='-m'),
            _p('attack_mode', 'flag', '攻击模式（`-a`），0=字典。', flag='-a', default='0'),
        ),
        default_limitation=(
            '容器里**没有 GPU**，纯 CPU 跑，速度比 GPU 慢三到四个数量级。'
            '这意味着只适合「字典里有就能秒出」的情况，不适合暴力枚举空间。'
            '`-m` 编号写错会让它报 token length exception 而不是「哈希类型错」，容易误判。'
        ),
        allowed_exit_codes=(0, 1),
    ),
    ToolRecipe(
        name='john',
        summary='John the Ripper：自动识别哈希类型并破解。',
        group='凭据与破解',
        command='john',
        params=(
            _p('hashfile', 'positional', '哈希文件路径。', required=True, position=1),
            _p('wordlist', 'flag', '字典路径。', flag='--wordlist'),
            _p('format', 'flag', '强制指定哈希格式（识别错时用）。', flag='--format'),
        ),
        default_limitation=(
            '不给 `--wordlist` 会用**自带的极小字典 + 增量模式**，跑很久才出结果，'
            '看起来像「卡住」。它自动识别格式经常失败（尤其带盐的、非标准前缀的），'
            '失败时它会拿整个文件当「一个哈希」去试，然后报 0 —— 这时要显式 `--format`。'
        ),
        allowed_exit_codes=(0, 1),
    ),
    ToolRecipe(
        name='xxd',
        summary='十六进制查看/还原（快速看文件头、手工改字节）。',
        group='逆向与取证',
        command='xxd',
        params=(
            _p('file', 'positional', '文件路径。', required=True, position=1),
            _p('revert', 'flag', '把 hexdump 还原成二进制（`-r`）。', flag='-r'),
        ),
        default_limitation=(
            '只是 hexdump，**不做任何结构解析**。看一个陌生文件先 `xxd | head` '
            '确认魔数（`file` 命令更直接），不要指望它告诉你「这是什么格式」。'
        ),
    ),
    ToolRecipe(
        name='seccomp-tools',
        summary='读/改 seccomp BPF 过滤器：dump 一个程序的沙箱规则，或评估它能不能逃出去。',
        group='逆向与取证',
        # ★ 实测：`/usr/local/bin/seccomp-tools`，Ruby 写的，子命令式
        #   （`dump <file>` / `disasm` / `emu` / `asm` / `audit`）。
        #   没有默认子命令 —— 不给子命令只会打 usage。
        command='seccomp-tools',
        params=(
            _p('action', 'positional', '子命令：`dump`（读规则）/ `emu`（模拟）/ `audit`（找逃逸）。',
               required=True, position=0),
            _p('target', 'positional', '目标二进制路径。', position=1),
        ),
        default_limitation=(
            '它**只读静态规则**，不证明规则真的生效：dump 出来的信息来自 '
            '`prctl` 与 BPF 字节码的字面分析，程序若在运行时才安装过滤器，'
            'dump 看到的是「没装」。要看真实生效的规则，得在**已启动的进程**上 '
            'attach（`dump -p <pid>`），而那需要 ptrace —— 容器里通常没有。'
            '`audit` 子命令给的是「可疑」清单，不是可利用结论。'
        ),
    ),
    ToolRecipe(
        name='one_gadget',
        summary='在 libc 里找「一个地址就能拿到 shell」的 gadget（省掉 ROP 链的活）。',
        group='逆向与取证',
        command='one_gadget',
        params=(
            _p('file', 'positional', 'libc.so 路径。', required=True, position=0),
            _p('level', 'flag', '输出详细级别（`-l`），0/1/2，2 会给约束条件的拆解。', flag='-l'),
        ),
        default_limitation=(
            'gadget 都带**前置约束**（寄存器/栈必须是什么），它默认只打印约束、'
            '不验证你的实际状态满足不满足。直接跳上去大多数时候不成立，'
            '所以它输出的是「候选」不是「能用」。'
            '另外它按 build-id 先去查线上库；容器无网，会退回就地扫文件'
            '（可能慢），必要时显式给 `-f`。它只对 glibc 有效，musl 没有这套。'
        ),
    ),
    ToolRecipe(
        name='patchelf',
        summary='改 ELF 的 interpreter / rpath / soname（本地复现远程 libc 的标准做法）。',
        group='逆向与取证',
        command='patchelf',
        params=(
            _p('target', 'positional', '要改的 ELF 路径。', required=True, position=0),
            _p('set_interpreter', 'flag', '改解释器（`--set-interpreter`），如指向题目自带 ld。',
               flag='--set-interpreter'),
            _p('set_rpath', 'flag', '改运行时库搜索路径（`--set-rpath`），指向题目自带 libc。',
               flag='--set-rpath'),
        ),
        default_limitation=(
            '**就地改写文件**，改坏了只能重新下载/还原 —— 动之前先 `cp` 一份。'
            '它改的是 ELF 头与节区，不校验新解释器是否真能加载这个二进制；'
            '解释器与架构不匹配时，症状是运行时报 `Exec format error` 或直接 segfault，'
            '那看起来像「二进制坏了」而不是「解释器配错了」。'
        ),
    ),
    ToolRecipe(
        name='socat',
        summary='建立任意两个端点之间的双向管道（拿 shell、转端口、起了监听等靶机连）。',
        group='执行',
        command='socat',
        params=(
            _p('addresses', 'positional', '两个地址，如 `TCP-LISTEN:4444,reuseaddr -`。',
               required=True, position=0),
        ),
        default_limitation=(
            '容器**无网络**（`--network none`），所以任何 TCP/UNIX 连接都会失败 —— '
            '它不是拿来打远程的，主要用途是**本地起一个监听**给另一个进程连'
            '（如把 pwntools 脚本的 stdin/stdout 接到一个 socket 上）。'
            '要真联网请用 `http_request` 工具（它走宿主侧代理），不是这个。'
        ),
    ),
    ToolRecipe(
        name='nc',
        summary='最简 TCP 收发（版本探测、手工发一个 payload、看原始响应）。',
        group='侦察',
        command='nc',
        params=(
            _p('host', 'positional', '目标主机。', required=True, position=1),
            _p('port', 'positional', '目标端口。', required=True, position=2),
            _p('verbose', 'flag', '打印连接细节（`-v`）。', flag='-v'),
            _p('timeout', 'flag', '连接超时秒数（`-w`）。', flag='-w'),
        ),
        default_limitation=(
            '容器**无网络**，对外连接一定失败（`Network is unreachable`）—— '
            '这不是「目标没开端口」，别把它读成结论。'
            '真正对外发请求要用 `http_request` 工具。'
            '另外它**不加密、不解析**：拿到的就是裸字节，HTTP/TLS 都不会自动处理。'
        ),
        allowed_exit_codes=(0, 1),
    ),
    ToolRecipe(
        name='pwn',
        summary='pwntools 命令行：`cyclic` 生成/查偏移、`asm`/`disasm` 汇编互转、`checksec` 看防护。',
        group='逆向与取证',
        # ★ 实测结论（2026-10-26，在真实容器参数下逐条验过）：
        #   · `pwn cyclic 16` → `aaaabaaacaaadaaa`            ✓ 可用
        #   · `pwn asm "nop; ret"` → 字节 `90c3`（二进制输出）  ✓ 可用
        #   · `pwn disasm 90c3` → **崩**：Python 3.14 的
        #     `pwnlib.util.safeeval` 用 `dis.opname` 查不到 `LOAD_SMALL_INT`，
        #     抛 `ValueError: opcode LOAD_SMALL_INT not allowed`。
        #     Python API（`from pwnlib.asm import disasm`）反而正常 ——
        #     是 CLI 那条路径特有。所以下面的说明里必须写「别用 pwn disasm」。
        command='pwn',
        params=(
            _p('subcommand', 'positional',
               '子命令：`cyclic` / `asm` / `checksec`（**不要用 `disasm`**，见局限）。',
               required=True, position=0),
            _p('argument', 'positional', '子命令的参数（长度 / 汇编串 / 文件路径）。', position=1),
        ),
        default_limitation=(
            '★ `pwn disasm` 在这个镜像里**是坏的**（Python 3.14 的 safeeval 不认 '
            '`LOAD_SMALL_INT`，抛 ValueError）—— 要反汇编用 `objdump` 或 `radare2`。'
            '`pwn asm` 输出的是**裸字节**（不是 hex），看结果要接 `| xxd -p`。'
            '容器无 terminfo，每条命令都会先打一段 `_curses.error: setupterm` 警告，'
            '那是噪音、不是失败。'
            '★ pwntools 的 Python API **齐全可用**（`from pwn import *`，含 ROP 类），'
            '复杂利用请写脚本而不是硬拼命令行。'
        ),
    ),
)


#: 名字 → 配方。
RECIPE_BY_NAME: Final[dict[str, ToolRecipe]] = {r.name: r for r in RECIPE_SPECS}


# ══════════════════════════════════════════════════════════
# 参数拼接
# ══════════════════════════════════════════════════════════


def build_argv(recipe: ToolRecipe, values: dict[str, str]) -> list[str]:
    """把一个配方 + 调用方给的值拼成 argv。

    ## 为什么返回 list 而不是字符串

    这是**唯一**能防住命令注入的形状：`shlex.quote` 拼出来的字符串在
    `sandbox.shell()` 里还要再过一次 shell 解析，一旦哪里漏了引号就执行了。
    返回 argv 让调用方走直接 exec 式的传参，并且让「参数值里带空格/分号」
    变成一件正常的事而不是事故。

    ## `required` 缺失时抛，而不是拼一个残缺命令

    残缺命令的后果是「跑了一个参数不全的扫描器」，它往往**成功退出**
    （退出码 0）并给出无意义输出。那比报错糟得多 —— 报错模型会改，
    无意义输出模型会当结论。所以这里直接 `ValueError`。

    ## 空值的处理

    `values` 里给了空串 —— 当作「没给」。理由：模型很常写
    `{"ports": ""}` 表示「我不指定」，若拼成 `-p ''`，nmap 会报
    `Failed to resolve ""`，又是一次白跑。
    """
    for param in recipe.params:
        if not param.required:
            continue
        if not str(values.get(param.name) or '').strip():
            raise ValueError(
                f'配方 `{recipe.name}` 缺少必填参数 `{param.name}`'
                f'（{param.description or "无说明"}）。'
            )

    argv: list[str] = [recipe.command, *recipe.args]

    # `positional` 按 position 排序后依次追加。`default` 参与 —— 一个
    # 位置参数有默认值意味着「不给也该出现」。
    positional = sorted(
        (p for p in recipe.params if p.format == 'positional'),
        key=lambda p: p.position,
    )
    for param in positional:
        value = str(values.get(param.name) or '').strip() or param.default
        if value:
            argv.append(value)

    # 其余两种保持声明顺序（flag 之间的先后不影响语义，但稳定顺序让
    # 日志之间可比 —— 与 `tools.py::enabled_tool_names` 同一条理由）。
    for param in recipe.params:
        if param.format == 'positional':
            continue
        value = str(values.get(param.name) or '').strip() or param.default
        if not value:
            continue
        if param.format == 'flag':
            if not param.flag:
                raise ValueError(
                    f'配方 `{recipe.name}` 的参数 `{param.name}` 是 flag 格式但没写 flag 名。'
                )
            argv.extend([param.flag, value])
        elif param.format == 'template':
            if '{value}' not in param.template:
                raise ValueError(
                    f'配方 `{recipe.name}` 的参数 `{param.name}` 模板里没有 {{value}}：'
                    f'{param.template!r}'
                )
            argv.append(param.template.replace('{value}', value))
        else:
            raise ValueError(
                f'配方 `{recipe.name}` 的参数 `{param.name}` 格式未知：{param.format!r}'
                f'（只支持 {sorted(PARAM_FORMATS)}）。'
            )
    return argv


def render_argv(recipe: ToolRecipe, values: dict[str, str]) -> str:
    """`build_argv` 的字符串形态，用于展示给模型看「这条命令长什么样」。

    ⚠️ 只用于**展示**。真执行请用 `build_argv` 并自己决定怎么传参 ——
    这里的 `shlex.join` 是为了让人读得懂，不是为了安全边界。
    """
    return shlex.join(build_argv(recipe, values))


# ══════════════════════════════════════════════════════════
# 可用性探测
# ══════════════════════════════════════════════════════════

#: 探测脚本里的哨兵。**故意用不常见的串**，避免与二进制名或输出撞。
_PROBE_SENTINEL = '__SUPEX_RECIPE_PROBE__'


def probe_command(names: list[str] | tuple[str, ...]) -> str:
    """拼一条 `sh -c` 脚本，一次问清这些二进制在不在。

    ## 为什么一次批量问

    29 个配方各 exec 一次 = 29 次容器 round-trip（每次几十毫秒），
    在**每个任务开始**都跑一遍就是纯浪费。一次拼接只花一次往返。

    ## 为什么输出用哨兵包围

    `command -v` 在有 PATH 命中时打印**路径**（`/usr/bin/nmap`），没有时
    打印空行。直接按行读会把「空行」与「没执行」混起来。所以每条结果都写成
    `哨兵 名字=路径或空 哨兵`，解析时只认两个哨兵之间的内容 —— 宿主机
    或镜像里任何其他 stdout 噪音都不会被误读成结果。

    ## 为什么要 `2>/dev/null`

    `command -v` 在 POSIX shell 里是内建命令，本不会污染 stderr；但有些
    镜像的 `/bin/sh` 是 busybox，某些版本对不存在命令会写 stderr。留着这条
    重定向让「没找到」干脆是空的，省得解析时要滤。
    """
    if not names:
        return 'true'
    # 名字只允许 [A-Za-z0-9._+-]，否则一次性拼进脚本会有注入面。
    # 这条白名单不是「相信调用方」，而是让将来有人从配置里读名字时也安全。
    for name in names:
        if not re.fullmatch(r'[A-Za-z0-9._+-]+', name):
            raise ValueError(f'配方名里只能是字母数字与 ._+-（收到 {name!r}）')
    parts = []
    for name in names:
        parts.append(
            f"printf '%s{name}=%s%s\\n' '{_PROBE_SENTINEL}' "
            f'"$(command -v {name} 2>/dev/null)" \'{_PROBE_SENTINEL}\''
        )
    return ' ; '.join(parts)


def parse_probe(output: str) -> dict[str, str]:
    """解析 `probe_command` 的输出 → `{名字: 路径}`（没装的值为空串）。

    ★ 找不到的名字**也要出现在结果里**（值为空串），而不是「不在字典里」。
    调用方要能区分「我问过、它没有」与「我没问」。前者该在界面上画成
    「镜像里没装」，后者是调用方的 bug。
    """
    found: dict[str, str] = {}
    for line in output.splitlines():
        start = line.find(_PROBE_SENTINEL)
        if start < 0:
            continue
        end = line.find(_PROBE_SENTINEL, start + len(_PROBE_SENTINEL))
        if end < 0:
            continue
        body = line[start + len(_PROBE_SENTINEL) : end]
        name, _, path = body.partition('=')
        if name:
            found[name] = path.strip()
    return found


# ══════════════════════════════════════════════════════════
# 渲染（进提示词 / 给 load_recipe 回全文）
# ══════════════════════════════════════════════════════════
#
# ★★ 这一节补的是 v0.98 留下的**真缺口**，必须写清为什么。
#
# v0.98 建好了这一整套配方（34 条、6 组、参数拼法、探测函数），
# 也接上了 `GET /kernel/recipes`，界面上能看。但**模型从头到尾看不到它**：
# `probe_command()` / `parse_probe()` 在生产代码里零调用方，`default_limitation`
# 只出现在 API 响应里。而这个模块存在的**全部理由**就是"让模型知道默认参数
# 下会漏掉什么"（见文件头）。配方只进界面 = 知识只给了人看，
# 而人是照着模型给的结论下判断的 —— 等于没修。
#
# 这正是本项目反复抓的那类形状：**配好了却静默失效**。


#: 进提示词的配方清单里，每条局限最多显示多少字符。
#:
#: ## 为什么要截断，而不是全给
#:
#: 系统提示词**每一轮都要重发**（`runner.solve_in_sandbox` 里
#: `gateway.chat(_messages(), system=system_prompt)`）。34 条配方的局限
#: 全文约 6.2KB，跑 40 轮就是 248KB 的重复传输 —— 而其中大部分细节
#: （某个 flag 具体怎么写）**只在真的要调那个工具时才有用**。
#:
#: 所以：清单里给**够判断"该不该用这个工具、要不要加参数"**的一句话，
#: 全文交给 `load_recipe`。截断留 60 字符 ≈ 一句中文短句。
INDEX_LIMITATION_CHARS: Final[int] = 60


def render_index(*, probe: dict[str, str] | None = None) -> str:
    """渲染「命令行工具配方清单」—— 进系统提示词。

    ## ★ 为什么清单里必须带一句局限，而不是只列工具名

    因为**选择发生在调用之前**。模型决定"用 nmap 扫一下"的时候，
    如果那时它不知道"nmap 默认只扫约 1000 个端口"，它就会拿着默认结果
    写下"目标只开了这几个端口"。等它 `load_recipe` 再看到局限，
    那个结论**已经写进上一轮的推理里了**。

    所以清单 = 名字 + 这一句最贵的局限。全文（参数怎么拼、完整局限）
    才靠 `load_recipe` 按需拉。

    ## `probe`：已探测到的可用性

    `probe` 是 `parse_probe(probe_command(...))` 的结果，可以不给
    （不给 = 不知道装没装）。给了就**如实标注没装的那些** ——
    这是本模块与 CyberStrikeAI 那份 YAML 最重要的差别：
    照抄 90 条会让模型对着不存在的二进制写命令，沙箱回
    `sh: amap: not found`，而它看起来像"工具跑了但没结果"。

    ⚠️ `probe` 里**有**某名字但值为空串 = 问过、没装。
    `probe` 里**没有**该名字 = 没问过，不作标注（不能诬告它没装）。
    """
    if not RECIPE_SPECS:
        return ''

    lines: list[str] = []
    for group in RECIPE_GROUPS:
        items = [r for r in RECIPE_SPECS if r.group == group]
        if not items:
            continue
        lines.append(f'\n【{group}】')
        for recipe in items:
            mark = ''
            if probe is not None and recipe.name in probe:
                mark = '' if probe[recipe.name] else '（★ 本镜像没装，别调）'
            summary = recipe.summary
            limitation = recipe.default_limitation
            line = f'- `{recipe.name}`{mark}：{summary}'
            if limitation:
                clipped = limitation
                if len(clipped) > INDEX_LIMITATION_CHARS:
                    clipped = clipped[:INDEX_LIMITATION_CHARS] + '…'
                line += f' **默认会漏**：{clipped}'
            lines.append(line)
    return '\n'.join(lines)


def render_recipe(name: str, *, probe: dict[str, str] | None = None) -> str:
    """渲染**一条**配方的全文（给 `load_recipe` 工具用）。

    ## 为什么全文要单独一次调用，而不是塞进清单

    见 `INDEX_LIMITATION_CHARS` 的说明：清单每轮重发，全文只在需要时看一次。

    ## 名字未命中时返回**可用清单**，不是空串

    与 `skills.load_skill_text` 同一条纪律：模型拼错名字时需要看到
    "到底有哪些"，否则它会反复猜（每次猜都烧一个回合）。
    """
    recipe = RECIPE_BY_NAME.get((name or '').strip())
    if recipe is None:
        available = '、'.join(r.name for r in RECIPE_SPECS) or '（无）'
        return f'没有名为 `{name}` 的工具配方。可用配方：{available}'

    out: list[str] = [f'# `{recipe.name}`（{recipe.group}）', '', recipe.summary, '']

    if probe is not None and recipe.name in probe:
        where = probe[recipe.name]
        out.append(
            f'本镜像里**装了**：`{where}`' if where else '★ 本镜像里**没装这个二进制**，不要调它。'
        )
        out.append('')

    # 命令行怎么拼 —— 这是"照着做"的部分，必须给完整可复制的形态。
    out.append('## 怎么调')
    out.append('')
    out.append('```')
    out.append(render_argv(recipe, _sample_values(recipe)))
    out.append('```')
    out.append('')

    if recipe.params:
        out.append('## 参数')
        out.append('')
        for param in recipe.params:
            bits: list[str] = []
            if param.format == 'flag':
                bits.append(f'`{param.flag}`')
            elif param.format == 'template':
                bits.append(f'`{param.template}`')
            else:
                bits.append('（位置参数）')
            need = '必填' if param.required else ('有默认值' if param.default else '可选')
            detail = f'- `{param.name}` {bits[0]}　{need}'
            if param.default:
                detail += f'（省略时用 `{param.default}`）'
            if param.description:
                detail += f'：{param.description}'
            out.append(detail)
        out.append('')

    if recipe.default_limitation:
        # ★ 不折叠、不缩写成小字：这一节是本配方存在的理由。
        out.append('## ★ 默认参数下它做不到什么')
        out.append('')
        out.append(recipe.default_limitation)
        out.append('')

    if recipe.allowed_exit_codes:
        codes = '、'.join(str(c) for c in recipe.allowed_exit_codes)
        out.append(
            f'## 退出码\n\n下面这些退出码**不算失败**（没命中也是正常结果）：{codes}\n'
        )
    return '\n'.join(out)


def _sample_values(recipe: ToolRecipe) -> dict[str, str]:
    """给 `render_argv` 造一组**样例值**，用来展示"这条命令长什么样"。

    ## 为什么必填参数填成 `<名字>` 占位符，而不是填一个像样的值

    填 `example.com` 会让样例看起来**已经可以跑了** —— 模型容易把它整条
    复制走，然后对着真实目标用了 `example.com`。而 `<target>` 明确写着
    "这里要换成你的目标"，不可能被误读成成品。

    ## 为什么可选参数不填

    `build_argv` 的语义是"给了就拼、没给用默认值"。样例里全都给上，
    会让"哪些是必须的"看不出来 —— 而那正是模型最容易搞错的地方。
    """
    return {p.name: f'<{p.name}>' for p in recipe.params if p.required}


def recipe_names() -> list[str]:
    """全部配方名（给"可用清单"与测试用）。"""
    return [r.name for r in RECIPE_SPECS]


# ══════════════════════════════════════════════════════════
# 一致性断言（import 时执行）
# ══════════════════════════════════════════════════════════


def _assert_recipes_consistent() -> None:
    """在 import 时校验整份清单。

    ## 为什么要在 import 时炸而不是写在测试里

    这几条错误的**症状都是静默的**：格式拼错 → 模型调它时抛 ValueError
    （那时已经在解题中途，白烧一个回合）；分组写错 → 界面上那一栏永远是空的
    （没人会去查为什么）。放在 import 时校验，错了当场起不来，
    比「部署之后某天发现」便宜得多。

    与 `tools.py::assert_catalog_consistent` 是同一个思路，只是那份是
    跨模块对账（要和 `runner.TOOL_NAMES` 比），这份是自洽校验。
    """
    seen: set[str] = set()
    for recipe in RECIPE_SPECS:
        if recipe.name in seen:
            raise AssertionError(f'配方名重复：{recipe.name}')
        seen.add(recipe.name)

        if recipe.group not in RECIPE_GROUPS:
            raise AssertionError(
                f'配方 {recipe.name} 的分组 {recipe.group!r} 不在 RECIPE_GROUPS 里：'
                f'{RECIPE_GROUPS}'
            )
        if not recipe.command:
            raise AssertionError(f'配方 {recipe.name} 没写 command')

        param_names: set[str] = set()
        for param in recipe.params:
            if param.name in param_names:
                raise AssertionError(f'配方 {recipe.name} 的参数名重复：{param.name}')
            param_names.add(param.name)

            if param.format not in PARAM_FORMATS:
                raise AssertionError(
                    f'配方 {recipe.name} 的参数 {param.name} 格式非法：'
                    f'{param.format!r}（只支持 {sorted(PARAM_FORMATS)}）'
                )
            if param.format == 'flag' and not param.flag:
                raise AssertionError(
                    f'配方 {recipe.name} 的参数 {param.name} 是 flag 但没写 flag 名'
                )
            if param.format == 'template' and '{value}' not in param.template:
                raise AssertionError(
                    f'配方 {recipe.name} 的参数 {param.name} 模板缺 {{value}}：'
                    f'{param.template!r}'
                )

    # 每一条都必须写 default_limitation？不强求 —— 有些工具默认就是全功能
    # （`exiftool` 就是全量读）。但「默认会缩水」的那几个必须有，否则
    # 这个模块就白建了。这里只钉最贵的几条。
    must_explain = {'nuclei', 'sqlmap', 'ffuf', 'gobuster', 'hydra'}
    missing = sorted(n for n in must_explain if not RECIPE_BY_NAME[n].default_limitation)
    if missing:
        raise AssertionError(
            f'这些配方必须写清「默认参数下做不到什么」：{missing}'
            '（它们的默认行为会静默缩水，见各条目的注释）'
        )


_assert_recipes_consistent()


# ══════════════════════════════════════════════════════════
# 出处
# ══════════════════════════════════════════════════════════

_ATTRIBUTION: Final[str] = '''\
参数拼法（flag / positional / template 三种 format）、以及把默认行为的
局限写进配方本身这个做法，参考自 CyberStrikeAI
（https://github.com/AIPentest/CyberStrikeAI ，Apache-2.0）的 tools/*.yaml。

本文件不是它的拷贝：清单按 suvex-sandbox 的实测可用性筛过（90 → 28+1），
schema 重写成 frozen dataclass，参数拼接与探测逻辑为本项目自写。署名按
Apache-2.0 第 4 条的要求保留。
'''

__all__ = [
    'PARAM_FORMATS',
    'RECIPE_BY_NAME',
    'RECIPE_GROUPS',
    'RECIPE_SPECS',
    'RecipeParam',
    'ToolRecipe',
    'build_argv',
    'parse_probe',
    'probe_command',
    'render_argv',
]
