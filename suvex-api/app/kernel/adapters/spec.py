"""声明式平台配置：**一份 JSON 就能描述一个平台怎么对接**。

## 它为什么存在

"填个窗口 → agent 自动对接"这个需求的落点在这里。

大模型读完赛事方的接口文档之后，产出的是**这一份配置**，不是一段
Python 代码。三个理由：

1. **可审计** —— 人（和下一个 agent）能直接读懂它写了什么。
   `{"submit": {"path": "/answer-panel/answer"}}` 不需要任何解释。
2. **可复现** —— 同一份配置每次行为一致。生成的代码则可能在两次运行里
   因为一个随机细节而行为不同，而那种问题极难查。
3. **不执行任意代码** —— 大模型会幻觉。幻觉出来的配置最多是"打错了
   端点"，幻觉出来的代码可能在宿主机上干任何事。

表达式能力不够的畸形平台（CTF2 那种"没有列表端点、靠公开解法侧门
拿 id"的），仍然走手写驱动（`app/kernel/drivers/ctf2.py`）。**这里不追求
覆盖所有平台**，只覆盖"形状正常"的那些 —— 而正常形状恰恰是绝大多数。

## 字段路径的写法

一律用**点号路径**（`attachment.files`、`data.items`、`endpoints.0.exposeIps`），
数组下标直接写数字。空串表示"就是它自己"（比如列表响应根就是数组）。

不做 JSONPath / JMESPath：那两套的东西大模型写得出来，但**人读不懂**，
而"人能核对 agent 写了什么"是这套设计的前提之一。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 模板里代表"题目 id"的占位符。见 `RequestSpec` 的说明。
ID_PLACEHOLDER = "{id}"
#: 模板里代表"要提交的答案"的占位符。
ANSWER_PLACEHOLDER = "{answer}"


class SpecError(ValueError):
    """配置不合法。

    ★ 这个异常的 message **会被原样回喂给大模型**（见 `adapters.builder`），
    所以每一条都必须说清"哪一项、期望什么、收到什么" —— 写一句
    "配置格式错误"等于让模型盲猜，而它会猜同一个错。
    """


def _require(data: dict[str, Any], key: str, *, where: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise SpecError(f"{where} 缺少必填项 `{key}`")
    return data[key]


def _as_dict(value: Any, *, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"{where} 应该是一个对象，收到 {type(value).__name__}")
    return value


def _as_str(value: Any, *, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SpecError(f"{where} 应该是字符串，收到 {type(value).__name__}")
    return value


# ══════════════════════════════════════════════════════════
# 路径求值
# ══════════════════════════════════════════════════════════


def dig(payload: Any, path: str) -> Any:
    """按点号路径取值。取不到返回 `None`（**不抛**）。

    ⚠️ 为什么取不到不抛异常：一道题的某个可选字段缺失（比如没有
    `difficulty`）是**正常**的，不该让整次同步失败。真正需要报错的是
    "必填字段取不到"，那由调用方（`generic.py`）判断并给出上下文 ——
    它才知道"这个字段是标题还是备注"。
    """
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
            continue
        if isinstance(current, list):
            # 数组下标。非数字就是走不通（返回 None 而不是报错，同上面那条）。
            if not part.lstrip("-").isdigit():
                return None
            index = int(part)
            if -len(current) <= index < len(current):
                current = current[index]
                continue
            return None
        return None
    return current


def dig_str(payload: Any, path: str) -> str:
    value = dig(payload, path)
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def dig_int(payload: Any, path: str) -> int | None:
    """取整数。**兼容字符串数字**。

    ★ 这不是过度防御：比赛平台的文档里 `"score": "100"` 就是字符串。
    严格只认 int 会让"分值读不出来"，而那个错只影响"按分值排队"这个
    策略，表现成"高分题没排在前面" —— 极难归因。
    """
    value = dig(payload, path)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def dig_list(payload: Any, path: str) -> list[Any]:
    value = dig(payload, path)
    if isinstance(value, list):
        return value
    # 有些平台把单个附件也塞进 `{"files": {...}}`（对象而不是数组）。
    # 那是个真实的形状差异，而"少一个附件"的后果是题目做不了。
    if isinstance(value, dict):
        return [value]
    return []


# ══════════════════════════════════════════════════════════
# 配置的各个部分
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class AuthSpec:
    """认证怎么带。

    `style` 的取值刻意只有三个 —— 覆盖了两份真实文档里见到过的全部
    形状，而每多一种就要多一条测试和一个出错的机会：

    * `header` —— 自定义头，值 = `prefix + key`（比赛平台：`X-Agent-AccessKey`）
    * `bearer` —— `Authorization: Bearer <key>`（CTF2）
    * `query`  —— 挂在 URL 参数上（少数平台这么干）
    """

    style: str = "header"
    name: str = "Authorization"
    prefix: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AuthSpec:
        raw = _as_dict(data, where="auth")
        style = _as_str(raw.get("style"), where="auth.style") or "header"
        if style not in ("header", "bearer", "query"):
            raise SpecError(
                f"auth.style 只能是 header / bearer / query 之一，收到 {style!r}"
            )
        return cls(
            style=style,
            name=_as_str(raw.get("name"), where="auth.name") or "Authorization",
            prefix=_as_str(raw.get("prefix"), where="auth.prefix"),
        )

    def headers(self, key: str) -> dict[str, str]:
        if self.style == "query":
            return {}
        if self.style == "bearer":
            return {"Authorization": f"Bearer {key}"}
        return {self.name: f"{self.prefix}{key}"}

    def query(self, key: str) -> dict[str, str]:
        return {self.name: f"{self.prefix}{key}"} if self.style == "query" else {}


@dataclass(frozen=True, slots=True)
class EnvelopeSpec:
    """统一响应包装。

    ★ 两个平台在这里是**根本不同**的，而且差别会导致最隐蔽的一类 bug：

    * CTF2 —— 裸 JSON，成功与否看 HTTP 状态码（`success_path` 留空）
    * 比赛平台 —— **失败也返回 HTTP 200**，成功与否只看 `code`

    后者不做处理的话，"答案错误"会被读成成功，而 `data` 是空的 ——
    最终表现成一道题反复提交、永远不结束。那看起来像调度器的 bug。
    """

    success_path: str = ""
    success_value: str = ""
    data_path: str = ""
    message_path: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EnvelopeSpec:
        raw = _as_dict(data, where="envelope")
        spec = cls(
            success_path=_as_str(raw.get("success_path"), where="envelope.success_path"),
            success_value=_as_str(raw.get("success_value"), where="envelope.success_value"),
            data_path=_as_str(raw.get("data_path"), where="envelope.data_path"),
            message_path=_as_str(raw.get("message_path"), where="envelope.message_path"),
        )
        if spec.success_path and not spec.success_value:
            raise SpecError(
                "envelope 写了 success_path 就必须写 success_value —— "
                "只说「看哪个字段」而不说「等于什么」，等于没判成功"
            )
        return spec


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """一个请求怎么发。

    `params` / `body` 的值里可以用两个占位符：

    * `{id}`     —— 题目 id（来自 handle）
    * `{answer}` —— 要提交的答案

    ★ 只支持这两个，不做通用表达式。理由同模块 docstring：
    能表达的东西越少，大模型写错的空间就越小。
    """

    method: str
    path: str
    params: dict[str, str] = field(default_factory=dict)
    body: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, *, where: str) -> RequestSpec:
        raw = _as_dict(data, where=where)
        method = (_as_str(raw.get("method"), where=f"{where}.method") or "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise SpecError(f"{where}.method 不是合法的 HTTP 方法：{method!r}")
        path = _require(raw, "path", where=where)
        params = {
            str(k): str(v) for k, v in _as_dict(raw.get("params"), where=f"{where}.params").items()
        }
        body = {
            str(k): str(v) for k, v in _as_dict(raw.get("body"), where=f"{where}.body").items()
        }
        return cls(method=method, path=str(path), params=params, body=body)

    @staticmethod
    def render(template: str, *, item_id: str, answer: str = "") -> Any:
        """把占位符换成实际值。

        ★ 换完之后如果整串**正好等于**占位符，就返回**原始类型**而不是
        字符串 —— 因为比赛平台的 `exerciseId` 是**整数**，而
        `{"exerciseId": "1001"}` 会被平台的类型校验拒掉（或者更糟：
        被当成另一个 id）。
        """
        if template == ID_PLACEHOLDER:
            return _coerce_scalar(item_id)
        text = template.replace(ID_PLACEHOLDER, item_id).replace(ANSWER_PLACEHOLDER, answer)
        return text

    def render_params(self, *, item_id: str) -> dict[str, Any]:
        return {k: self.render(v, item_id=item_id) for k, v in self.params.items()}

    def render_body(self, *, item_id: str, answer: str = "") -> dict[str, Any]:
        return {k: self.render(v, item_id=item_id, answer=answer) for k, v in self.body.items()}


def _coerce_scalar(value: str) -> Any:
    """纯数字的字符串转成整数。"""
    text = value.strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return value


@dataclass(frozen=True, slots=True)
class FieldMap:
    """题目字段映射：平台的字段名 → 我们的字段名。

    ★ 每个字段都是**路径**而不是名字，因为题面在有些平台里嵌得很深
    （`data.challenge.description`）。
    """

    id: str = "id"
    title: str = "name"
    description: str = "description"
    category: str = ""
    score: str = ""
    solved: str = ""
    is_open: str = ""
    difficulty: str = ""
    attachments: str = ""
    attachment_url: str = "url"
    attachment_name: str = "name"
    attachment_ext: str = "ext"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, *, where: str = "fields") -> FieldMap:
        raw = _as_dict(data, where=where)
        values: dict[str, str] = {}
        for key in (
            "id",
            "title",
            "description",
            "category",
            "score",
            "solved",
            "is_open",
            "difficulty",
            "attachments",
            "attachment_url",
            "attachment_name",
            "attachment_ext",
        ):
            if key in raw:
                values[key] = _as_str(raw[key], where=f"{where}.{key}")
        # 默认值：只补"没写"的，写了空串是**明确要求关掉这一项**。
        defaults = cls()
        for key in (
            "id",
            "title",
            "description",
            "attachment_url",
            "attachment_name",
            "attachment_ext",
        ):
            values.setdefault(key, getattr(defaults, key))
        return cls(**values)


@dataclass(frozen=True, slots=True)
class ListSpec:
    """题目列表怎么取。

    `children_path` 处理**两级**列表：比赛平台的响应是
    `[{name: "Web", corpus: [题目...]}]` —— 外层是分类。不展平的话，
    分类名会被当成题目标题。

    `category_from_group` 为真时，展平出来的题会把外层的 `name` 当作
    分类 —— 而分类决定选哪个沙箱镜像（逆向题用 re 镜像），丢掉它会让
    逆向题落到基础镜像上。
    """

    request: RequestSpec
    items_path: str = ""
    children_path: str = ""
    category_from_group: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ListSpec:
        if data is None:
            raise SpecError("缺少 `list_challenges` —— 没有它就无法取题")
        raw = _as_dict(data, where="list_challenges")
        return cls(
            request=RequestSpec.from_dict(raw.get("request"), where="list_challenges.request"),
            items_path=_as_str(raw.get("items_path"), where="list_challenges.items_path"),
            children_path=_as_str(raw.get("children_path"), where="list_challenges.children_path"),
            category_from_group=bool(raw.get("category_from_group", False)),
        )


@dataclass(frozen=True, slots=True)
class EndpointFields:
    """一台靶机的字段名映射。默认值 = `api_doc.md` §5 里的名字。

    ★ 为什么要映射而不是写死：这些名字是**那个平台**的取名习惯
    （`exposeIps` 这种驼峰在 Java 系后端里很典型）。换个平台可能就是
    `ip_list` / `open_ports`。写死的话，配置能取到题、能提交，却在
    "把靶机讲给模型听"这一步默默给出一份空信息 —— 而模型会卡在
    "连不上"上，以为是自己命令写错了。
    """

    expose_ips: str = "exposeIps"
    ports: str = "ports"
    users: str = "users"
    username: str = "username"
    password: str = "password"
    is_proxy: str = "isProxy"
    proxy_ips: str = "proxyIps"
    port_mappings: str = "portMappings"
    expire_time: str = "expireTime"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EndpointFields:
        raw = _as_dict(data, where="environment.fields")
        values = {key: _as_str(raw[key], where=f"environment.fields.{key}") for key in raw}
        merged = {**{k: getattr(cls(), k) for k in cls.__dataclass_fields__}, **values}
        return cls(**merged)


@dataclass(frozen=True, slots=True)
class EnvironmentSpec:
    """环境怎么读。

    ## `from_detail` 是最重要的一项

    两个平台的形状**根本不同**：

    * CTF2 —— 有独立的环境状态端点（start 之后轮询它）
    * 比赛平台 —— **没有**状态端点，环境就在题目详情里
      （`isNeedCheck` / `endpoints`）

    硬套其中一个的形状，会写出一个永远查不到东西的函数，
    而症状是"环境一直显示未就绪"。
    """

    from_detail: bool = False
    endpoints_path: str = "endpoints"
    access_urls_path: str = ""
    #: "就绪"看哪个字段 + 是否取反。
    #: 比赛平台的 `isNeedCheck` 是**反的**（false 才是就绪），所以取反
    #: 必须能配，否则判据会整个反过来 —— 而那意味着"环境好了我们以为
    #: 没好、一直等到超时"。
    ready_path: str = ""
    ready_negate: bool = False
    can_refresh_path: str = ""
    fields: EndpointFields = field(default_factory=EndpointFields)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EnvironmentSpec:
        raw = _as_dict(data, where="environment")
        if not raw:
            return cls()
        return cls(
            from_detail=bool(raw.get("from_detail", False)),
            endpoints_path=_as_str(raw.get("endpoints_path"), where="environment.endpoints_path")
            or "endpoints",
            access_urls_path=_as_str(raw.get("access_urls_path"), where="environment.access_urls_path"),
            ready_path=_as_str(raw.get("ready_path"), where="environment.ready_path"),
            ready_negate=bool(raw.get("ready_negate", False)),
            can_refresh_path=_as_str(raw.get("can_refresh_path"), where="environment.can_refresh_path"),
            fields=EndpointFields.from_dict(raw.get("fields")),
        )


# ══════════════════════════════════════════════════════════
# 整份配置
# ══════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PlatformSpec:
    """一个平台的完整对接方式。"""

    display_name: str
    base_url: str
    auth: AuthSpec
    envelope: EnvelopeSpec
    list_challenges: ListSpec
    submit: RequestSpec
    #: 提交结果里"对不对"的字段路径（`isCorrect` / `accepted`）。
    submit_solved_path: str = "isCorrect"
    #: 提交结果里"已解过"的字段路径（CTF2 有 `ALREADY_SOLVED` 这种码）。
    submit_already_solved_path: str = ""
    challenge_detail: RequestSpec | None = None
    start_env: RequestSpec | None = None
    stop_env: RequestSpec | None = None
    #: 环境怎么读（藏在详情里 / 独立端点 / 不支持）。见 `EnvironmentSpec`。
    environment: EnvironmentSpec = field(default_factory=EnvironmentSpec)
    answer_max_length: int = 256
    fields: FieldMap = field(default_factory=FieldMap)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlatformSpec:
        if not isinstance(data, dict):
            raise SpecError(f"配置的顶层应该是一个对象，收到 {type(data).__name__}")
        base_url = _as_str(_require(data, "base_url", where="配置顶层"), where="base_url")
        if not base_url.startswith(("http://", "https://")):
            raise SpecError(f"base_url 必须是 http(s) 地址，收到 {base_url!r}")

        detail_raw = data.get("challenge_detail")
        return cls(
            display_name=_as_str(data.get("display_name"), where="display_name") or "新平台",
            base_url=base_url.rstrip("/"),
            auth=AuthSpec.from_dict(data.get("auth")),
            envelope=EnvelopeSpec.from_dict(data.get("envelope")),
            list_challenges=ListSpec.from_dict(data.get("list_challenges")),
            submit=RequestSpec.from_dict(
                _require(data, "submit", where="配置顶层"), where="submit"
            ),
            submit_solved_path=_as_str(
                data.get("submit_solved_path"), where="submit_solved_path"
            )
            or "isCorrect",
            submit_already_solved_path=_as_str(
                data.get("submit_already_solved_path"),
                where="submit_already_solved_path",
            ),
            challenge_detail=(
                RequestSpec.from_dict(detail_raw, where="challenge_detail")
                if detail_raw
                else None
            ),
            start_env=(
                RequestSpec.from_dict(data.get("start_env"), where="start_env")
                if data.get("start_env")
                else None
            ),
            stop_env=(
                RequestSpec.from_dict(data.get("stop_env"), where="stop_env")
                if data.get("stop_env")
                else None
            ),
            environment=EnvironmentSpec.from_dict(data.get("environment")),
            answer_max_length=_bounded_int(
                data.get("answer_max_length"), default=256, low=1, high=100_000,
                where="answer_max_length",
            ),
            fields=FieldMap.from_dict(data.get("fields")),
        )

    def slug_hint(self) -> str:
        """从 base_url 猜一个稳定的标识（给数据库的 slug 用）。"""
        host = self.base_url.split("//", 1)[-1].split("/", 1)[0]
        cleaned = "".join(ch if ch.isalnum() else "-" for ch in host).strip("-")
        return (cleaned or "platform")[:48]


def _bounded_int(value: Any, *, default: int, low: int, high: int, where: str) -> int:
    if value is None:
        return default
    number = _to_int(value)
    if number is None:
        raise SpecError(f"{where} 应该是整数，收到 {value!r}")
    if not (low <= number <= high):
        raise SpecError(f"{where} 应在 {low}-{high} 之间，收到 {number}")
    return number


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


# ══════════════════════════════════════════════════════════
# 提交时的"没受理"错误码
# ══════════════════════════════════════════════════════════

# 命中这些码 → 平台**没有受理**这次提交，**不计错误提交次数**。
#
# ★ 放在这里而不是某个平台驱动里：手写驱动与配置驱动的判定必须一致，
#   而复制两份的后果是"改了一处忘了另一处" —— 那种漂移的表现是某个
#   平台会拿同一个错答案**无限重试**，把不可再生的错误提交次数烧光。
#
# ⚠️ 这份名单是**猜的**：两份文档都没有给出具体的错误码取值。
#   真机第一次判错的原始响应会写进日志（见两个驱动的 `submit`），
#   那时用它来修正这里。
NOT_ACCEPTED_CODES: frozenset[str] = frozenset(
    {
        "TOO_MANY_REQUESTS",
        "RATE_LIMITED",
        "UNAUTHORIZED",
        "FORBIDDEN",
        "ANSWER_ATTEMPT_LIMIT",
    }
)


__all__ = [
    "ANSWER_PLACEHOLDER",
    "ID_PLACEHOLDER",
    "NOT_ACCEPTED_CODES",
    "AuthSpec",
    "EndpointFields",
    "EnvelopeSpec",
    "EnvironmentSpec",
    "FieldMap",
    "ListSpec",
    "PlatformSpec",
    "RequestSpec",
    "SpecError",
    "dig",
    "dig_int",
    "dig_list",
    "dig_str",
]
