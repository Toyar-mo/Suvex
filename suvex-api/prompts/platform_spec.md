# 平台接入配置生成

你的任务：把一份 CTF 平台的接口文档，变成**一份 JSON 配置**。

系统会用这份配置驱动一个通用客户端去取题、提交答案、管理靶机环境。
你**不需要**写代码，也**不要**输出任何解释文字 —— 只输出 JSON。

## 输入

你会收到三样东西：

1. **接口文档**（可能是 Markdown、OpenAPI、或者一段 curl 示例）
2. **实测结果**：系统已经真的打过这个平台，报告了哪些端点活着、
   响应长什么样、认证方式是什么
3. **用户填写的 base_url**

## ★ 最重要的一条规则：实测优先于文档

实测是**硬证据**，文档可能过期、可能写的是网页端的接口、可能有个别错字。

两者冲突时，**以实测为准**。举例：

* 文档说端点叫 `/challenges`，但实测只有 `/ctf/exercise-list` 活着
  → 用实测的那个
* 文档说认证在 `X-Token` 头，但实测只有 `X-Agent-AccessKey` 返回 200
  → 用实测的那个

如果实测里某个端点没有出现，而文档写了它 —— 可以用，但在
`_notes` 里说明这是照文档填的、未经验证。

## ★ 第二条规则：不要发明

* 不要编造文档和实测里都没有的端点路径
* 不要猜认证头名 —— 用实测命中的那个
* 字段路径必须来自实测的字段骨架或文档的字段表

**宁可留空也不要猜。** 一份"取不到题"的配置会被系统的试跑发现并
让你重试；一份"看起来对但实际指向错误字段"的配置会安静地跑出错误的
结果 —— 那更糟。

## 输出格式

只输出一个 JSON 对象，不要包在 Markdown 代码块里（如果包了也没关系，
系统会剥掉），不要有任何前言后语。

```jsonc
{
  "display_name": "给人看的平台名",
  "base_url": "https://example.com/api/v1/agent",   // 含完整前缀，结尾不要斜杠

  "auth": {
    "style": "header",              // header | bearer | query
    "name": "X-Agent-AccessKey",    // header/query 的名字；bearer 时忽略
    "prefix": ""                    // 值的前缀，比如 "Bearer "（bearer 风格不用写）
  },

  "envelope": {                     // 统一响应包装；裸 JSON 就整个留空对象 {}
    "success_path": "code",         // 成功标志在哪个字段
    "success_value": "00000",       // 它等于什么才算成功
    "data_path": "data",            // 真正的数据在哪个字段
    "message_path": "message"       // 错误信息在哪个字段（可空）
  },

  "list_challenges": {
    "request": { "method": "GET", "path": "/ctf/exercise-list" },
    "items_path": "",               // 题目数组在响应里的位置；根就是数组时留空
    "children_path": "corpus",      // 两级列表时：子数组的字段名；一层就留空
    "category_from_group": true     // 两级时：用外层组的 name 当题目分类
  },

  "challenge_detail": {             // 取单题详情；没有就整段省略
    "method": "GET",
    "path": "/ctf/exercise",
    "params": { "exerciseId": "{id}" }
  },

  "submit": {
    "method": "POST",
    "path": "/answer-panel/answer",
    "body": { "exerciseId": "{id}", "flag": "{answer}" }
  },
  "submit_solved_path": "isCorrect",  // 响应里"对不对"的字段路径

  "start_env": {                    // 启动环境；没有就省略
    "method": "POST", "path": "/ctf/build-exercise-env",
    "body": { "exerciseId": "{id}" }
  },
  "stop_env": {                     // 回收环境；没有就省略
    "method": "POST", "path": "/ctf/recover-exercise-env",
    "body": { "exerciseId": "{id}" }
  },

  "environment": {                  // 环境信息从哪读；不支持环境就省略
    "from_detail": true,            // true = 环境藏在题目详情的响应里
    "endpoints_path": "endpoints",  // 靶机数组的路径
    "ready_path": "isNeedCheck",    // "是否仍在准备中"的字段
    "ready_negate": true,           // 该字段 true 表示**还没好** → 取反
    "can_refresh_path": "canRefreshEndpoint",
    "fields": {                     // 靶机内部字段名（默认值就是下面这些）
      "expose_ips": "exposeIps", "ports": "ports",
      "users": "users", "username": "username", "password": "password",
      "is_proxy": "isProxy", "proxy_ips": "proxyIps",
      "port_mappings": "portMappings", "expire_time": "expireTime"
    }
  },

  "answer_max_length": 256,         // 平台允许的答案最大长度；没写就 256

  "fields": {                       // 平台的字段名 → 我们的字段名
    "id": "id",                     // 必填：题目的唯一标识
    "title": "name",                // 必填：题目标题
    "description": "description",
    "category": "",                 // 题目分类（决定选哪个解题镜像）
    "score": "score",
    "solved": "hasSolved",          // 平台说"这题解没解出"
    "is_open": "isOpen",            // 平台说"这题开没开放"
    "difficulty": "difficulty",
    "attachments": "attachment.files",  // 附件数组的路径
    "attachment_url": "url",
    "attachment_name": "name",
    "attachment_ext": "ext"
  },

  "_notes": "给自己留的备注：哪些是照文档填的、哪些地方不确定"
}
```

### 字段路径的写法

一律用**点号路径**，数组下标写数字：

* `attachment.files` —— 嵌套对象
* `data.0.name` —— 数组第 0 个元素的 name
* 空串 `""` —— 就是它自己（比如响应根就是数组）

★ **所有路径都相对于 `envelope.data_path` 里面那一层。**

系统会先按 `envelope` 把外层包装拆掉，再把路径作用在**拆剩下的数据**上。
所以如果响应长这样：

```json
{"code": "00000", "message": "", "data": [{"id": 1001, "name": "easy-web"}]}
```

那么 `items_path` 应该填**空串**（拆掉 `data` 之后，根就是那个数组），
而**不是** `"data"` —— 填 `"data"` 会让它在数组上找 `data` 字段，
永远找不到，然后表现为"题目列表是空的"。

### 占位符

只有两个，用在 `params` / `body` 的**值**里：

* `{id}` —— 题目 id（来自取题时读到的 `fields.id`）
* `{answer}` —— 要提交的答案

值**整个**等于 `{id}` 时会保持原始类型（数字还是数字）。所以
`{"exerciseId": "{id}"}` 会发出 `{"exerciseId": 1001}` 而不是 `"1001"`。

### 几个容易搞错的地方

1. **两级列表**：如果题目列表的响应是
   `[{"name": "Web", "corpus": [题目...]}]`，那外层是**分类**不是题目 ——
   要填 `children_path: "corpus"` 和 `category_from_group: true`。
   不填的话，分类名会被当成题目标题。

2. **反着的布尔值**：`isNeedCheck` 这种字段，`true` 表示"**还**没好"。
   要填 `ready_negate: true`。填错的表现是"环境好了我们以为没好，
   一直等到超时"。

3. **分值可能是字符串**：`"score": "100"` 很常见。直接填 `score` 即可，
   系统会转成整数。

4. **答案长度**：平台文档里如果写了长度限制（比如"最长 256 字符"），
   填进 `answer_max_length`。填大了的后果是长答案在提交那一刻才失败。

## 一次完整的例子

下面是**照一份真实文档**写出来的配置，供你参考格式与详细程度：

```json
{
  "display_name": "比赛平台（slab-match）",
  "base_url": "https://example.com/slab-match/api/v1/agent",
  "auth": { "style": "header", "name": "X-Agent-AccessKey", "prefix": "" },
  "envelope": {
    "success_path": "code",
    "success_value": "00000",
    "data_path": "data",
    "message_path": "message"
  },
  "list_challenges": {
    "request": { "method": "GET", "path": "/ctf/exercise-list" },
    "items_path": "",
    "children_path": "corpus",
    "category_from_group": true
  },
  "challenge_detail": {
    "method": "GET",
    "path": "/ctf/exercise",
    "params": { "exerciseId": "{id}" }
  },
  "submit": {
    "method": "POST",
    "path": "/answer-panel/answer",
    "body": { "exerciseId": "{id}", "flag": "{answer}" }
  },
  "submit_solved_path": "isCorrect",
  "start_env": {
    "method": "POST",
    "path": "/ctf/build-exercise-env",
    "body": { "exerciseId": "{id}" }
  },
  "stop_env": {
    "method": "POST",
    "path": "/ctf/recover-exercise-env",
    "body": { "exerciseId": "{id}" }
  },
  "environment": {
    "from_detail": true,
    "endpoints_path": "endpoints",
    "ready_path": "isNeedCheck",
    "ready_negate": true,
    "can_refresh_path": "canRefreshEndpoint",
    "fields": {
      "expose_ips": "exposeIps",
      "ports": "ports",
      "users": "users",
      "username": "username",
      "password": "password",
      "is_proxy": "isProxy",
      "proxy_ips": "proxyIps",
      "port_mappings": "portMappings",
      "expire_time": "expireTime"
    }
  },
  "answer_max_length": 256,
  "fields": {
    "id": "id",
    "title": "name",
    "description": "description",
    "category": "",
    "score": "score",
    "solved": "hasSolved",
    "is_open": "isOpen",
    "difficulty": "difficulty",
    "attachments": "attachment.files",
    "attachment_url": "url",
    "attachment_name": "name",
    "attachment_ext": "ext"
  },
  "_notes": "环境状态没有独立端点，就在题目详情里；题目列表是两级的（分类 Web/Pwn/... → corpus）"
}
```
