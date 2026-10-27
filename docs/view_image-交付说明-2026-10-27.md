# view_image 工具接入 —— 交付说明与实证记录（2026-10-27）

> 用户指令（m02185）：「那就把strix 的浏览器和 view_image工具加入我们内核，把他们的skill用上」
> 三件事：① 浏览器工具 ② view_image 工具 ③ 上游技能搬入。本文记录 ②③ 的实现与**真机实证**，
> ① 记录其配套修复。

---

## 一、为什么原来的「截图」等于不存在

改造前的状态不是「不支持」，而是**支持但要命**：

- 消息 `content` 恒为 `str`（`runner.py:5385` 的 `base_message: dict[str, str]`、
  `recent: list[dict[str, str]]`、`_format_action_results` 返回 `str`）。
- 工具结果入对话的唯一入口是 `recent.append({"role": "user", "content": <纯文本>})`。
- `browser screenshot` 的返回值里写着：「这张图存成了文件，但**你无法看到图片内容**」。

代价：页面渲染错位、CSS 元素重叠、验证码、图表、UI 真实外观，全都只能靠 `snapshot`
的结构文本去猜。模型会据此下结论 —— 一个它从没看过的画面。

## 二、先证伪自己：模型真的支持视觉吗

在动手写代码之前先测端点，否则做出来的是假工具。

探针 `_tmp/probe_vision.py`（已从 Kali 清除）在**线上同一端点**发两次请求：

| 请求 | 结果 |
| --- | --- |
| 纯文本 `content: "ping"` | `200`，`prompt_tokens = 31` |
| `content` 为 `[{type:text}, {type:image_url, url:"data:image/png;base64,…"}]` | `200`，`prompt_tokens = **223**`（192 cache_creation tokens），模型描述了图片内容 |

**结论：端点支持视觉，`prompt_tokens` 从 31 涨到 223 证明图片真被 tokenize 了。**
所以阻塞点只在协议层，不在模型能力。

## 三、实现（`suvex-api/app/kernel/runner.py`）

| 部件 | 位置 | 作用 |
| --- | --- | --- |
| `MAX_IMAGE_BYTES` / `IMAGE_SHRINK_TARGET_BYTES` / `IMAGE_MAX_EDGE` / `IMAGE_MEDIA_TYPES` | 常量区 | 4 MB 硬上限、1.2 MB 缩图阈值、1568px 长边、png/jpg/jpeg/webp/gif |
| `class ImagePayload` | frozen dataclass | `path` / `media_type` / `data`(base64) / `size` |
| `_image_block(payload, *, anthropic)` | `:4179` | 双 provider 形状：OpenAI 用 `image_url`，Anthropic 用 `source.type=base64` |
| `_tool_view_image(args, *, sandbox, images)` | `:4261` | 路径白名单 → `copy_out` → 缩图 → 交给收集器 |
| `_shrink_image_in_sandbox(...)` | — | **容器内** `convert` 缩图（见下节为什么必须） |
| `copy_out(container_path, host_path)` | `:1990` | `docker exec` + base64 取回（不是 `docker cp`） |
| `turn_images: list[ImagePayload]` | 动作循环 | 旁路收集器，**只挂一轮、发完即清** |
| `_messages_with_images()` | `:5793` | 把图并进最后一条 user 消息，保持 user/assistant 交替 |

### 设计决定：图只挂一轮

不常驻历史。否则每一轮都重发一张 base64 图，几轮就把上下文吃光。
代价是模型要看第二次得再调一次 —— 文件还在原地，重取很便宜。
提示词里写明了这条纪律（`kernel_system.md`「关于看图」§）。

### 设计决定：为什么把图并进「最后一条 user 消息」

直接 append 一条带图的 user 消息会产生**两条连续 user 消息**，破坏
user/assistant 交替，部分 provider 会 400。所以把图并进已有那条。

## 四、三个真根因（都不是「写错了代码」，是「以为能用的东西其实不能用」）

这三个都是**单元测试绿、真机全挂**型，只有起真容器才暴露。

### 根因 1：宿主没有 Pillow → 缩图在线上是死路径

`_shrink_image` 在宿主调 Pillow。而线上 API 进程的 venv 里根本没有 Pillow
（`requirements.txt` 未声明，Kali 实测 `import PIL` → `ModuleNotFoundError`）。
于是这条路径**永远**走 `except ImportError`，症状是上下文悄悄被一张 4 MB
截图吃掉，而日志看起来一切正常。

修法：缩图**先在容器里做**（镜像里有 `imagemagick`，`Dockerfile:278`），
宿主 Pillow 只作兜底。

### 根因 2：`docker cp` 读不到 tmpfs 下的文件

容器内缩图成功，却取不回来。逐条手工对比三条通路后定案：

| 通路 | 读 `/work/persist`（bind mount） | 读 `/work/.browser/shots`（tmpfs） |
| --- | --- | --- |
| `docker cp` | ✅ | ❌ `Could not find the file` |
| `docker exec cat` | ✅ | ✅ |
| `docker exec base64` | ✅ | ✅ |

原因：`docker cp` 由 dockerd 执行，只看得见 rootfs 视图，看不见容器内 tmpfs。

修法：`copy_out` 内部改走 `docker exec … base64`，宿主侧解码。
实测缩图 1 580 946 B → 221 834 B 生效。

### 根因 3：容器 `--read-only` → `$HOME` 不可写 → Chromium 起不来

这条最隐蔽：`browser` 工具写出来之后**从来没有在容器里成功跑通过**。

容器是 `--read-only` 起的（`runner._restricted_argv`），`$HOME=/home/solver`
整个不可写。Kali 实测：

```
$ touch $HOME/.wtest        → Read-only file system
$ chromium --headless --dump-dom …
  → Failed to create headless user data directory container
  → Chrome exited early without writing DevToolsActivePort
```

`Dockerfile` 里**已经**设了 `AGENT_BROWSER_PROFILE=/work/.browser/profile`，
但那只管 agent-browser 自己的 profile，管不到 Chromium 的 `$HOME/.config/chromium`、
`$HOME/.cache` 与 crashpad 数据库。

修法：`browser.py` 新增 `HOME_ENV: Final = f"HOME={WORK_ROOT}/home"`，
`_invoke()` 每条命令前置它，`_ensure_dirs()` 建该目录。

⚠️ 为什么不写进 Dockerfile：`/work` 是**运行时**挂的 tmpfs，构建时它还不是那个
目录；而且改掉所有进程的 `HOME` 会影响别的工具（`pip install --user` 落点、
`~/.local/bin` 的 PATH 约定）。只在调 `agent-browser` 这一条命令上覆盖。

## 五、真机实证（Kali，非 mock）

探针 `_tmp/probe_e2e_full.py` 起**真容器**、跑**真 agent-browser**、走**真
`dispatch_tool`**，最后把拿到的图交给**真模型端点**问一句「图上写了什么」。

```
[e2e] open:       '✓ http://127.0.0.1:18081/'
[e2e] screenshot: '✓ Screenshot saved to /work/.browser/shots/e2e.png'
[e2e] ls:         -rw-r--r-- 1 solver solver 12946 … /work/.browser/shots/e2e.png
[e2e] view_image: '已把 `/work/.browser/shots/e2e.png` 挂到这一轮（原始 12946 字节）'
[e2e] payload:    12946B (容器内 12946B) png=True
[e2e] vision:     'VIEW IMAGE OK'
```

判据全部满足：

- `payload_bytes == container_bytes`（12 946）→ 取回的是**逐字一致**的原图
- `is_png == true` → PNG magic 正确
- `vision_sees_text == true` → 模型读出的文字与页面上画的文字**逐字相同**

证据图：`docs/evidence/view_image-e2e-kali.png`（1280×633，深蓝底白字
`VIEW IMAGE OK`）—— 已 `read_image` 人工复核，与模型回答一致。

## 六、守卫测试（防回归）

| 文件 | 条数 | 守的是什么 |
| --- | --- | --- |
| `tests/kernel/test_view_image.py` | 22 | 路径白名单（`/work` 之下、拒 `..`）、格式白名单、超限拒绝、`copy_out` 走 base64、缩图、无收集器时明说「你还是看不到」 |
| `tests/kernel/test_image_channel.py` | — | 旁路收集器只挂一轮、`_messages_with_images` 保持 user/assistant 交替、**无图时提前返回**（见下）、双 provider 形状 |
| `tests/kernel/test_browser_tool.py` | 52 | 含新增 3 条：`HOME_ENV` 前置、`mkdir` 建 home、两个常量逐字一致 |

### 变异验证（都真的会红）

- `test_image_channel.py`：把 `_messages_with_images` 里 `if not turn_images: return _messages()`
  提前返回删掉 → **38 条同时红**（`AttributeError: '_FakeGateway' object has no attribute 'anthropic'`）。
  这条提前返回不是优化，是**必需**：无图时不该求值 `gateway.anthropic`。
- `test_browser_tool.py` 三条 HOME 守卫：3/3 变异有效（去掉 `HOME_ENV` 前缀 / HOME 指到别处 /
  `mkdir` 漏掉 home 都能抓住），基线复查全绿。

## 七、技能

- 总数 **77 篇**（原 34 → 搬入 42 → 补 `agent_browser.md` 1 篇）。
- `prompts/skills/tooling/agent_browser.md` 是**改写**的（不是原样搬入）：上游版本教
  `exec_command`、Caido 代理、`view_image` SDK 调用、`/workspace/.agent-browser-screenshots/`
  —— 全部与我们运行时不符。改写后对齐真实实现（`/work/.browser/shots`、真 `view_image` 工具名）。
- 顺带修掉一个真缺陷：`prompts/skills/analysis/source_aware_discovery.md` 残留未渲染的
  Jinja（`{% if coverage_tools %}`），而我们的加载器**没有模板引擎** → 标记会原样漏给模型。
  已摊平 + 新增全文守卫（`test_real_skills_have_no_unrendered_template_syntax`）。
- 工具对账守卫 `test_real_skills_no_skill_for_uninstalled_tools` 从**硬编码黑名单**改为
  从 `Dockerfile` 推导单一事实源（含注释剔除与包名别名 `httpx-toolkit`），两次变异验证均可红。

## 八、部署

`suvex-api/deploy/update.sh`（整包覆盖 + 逐文件 md5 回验）：
**305 个文件全部 md5 一致**，`alembic upgrade head` 通过，服务重启后 `health=200`。

Kali 侧核验：`TOOL_SPECS` 13 项、含 `view_image`；`browser.py` 含 2 处 `HOME_ENV`；
`runner.py` 14 处 `view_image`；`prompts/skills` **77 篇**。

探针与临时文件已从部署目录清除（`_probe_e2e.py`、`_tmp_e2e_shot.png`、tar 包）。
