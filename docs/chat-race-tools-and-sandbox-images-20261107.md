# v1.71：对话页赛事自动托管（多模型赛跑）+ 沙箱镜像重建

日期：2026-11-07

## 一、这次解决的两个问题

用户报了两件事：

1. **「我希望在对话页解题时能用」赛事托管** —— 之前「多模型赛跑 / 整场托管」
   只在 CTF 页的 `RacePanel` 有（`/ctf-race/*` 端点），对话页没有。
2. **对话页解题直接报错** —— `Unable to find image 'suvex-sandbox:latest' locally`
   / `pull access denied`。根因：生产 Kali 上**根本没有 `suvex-sandbox` 镜像**
   （只有 `ctf-sandbox` 实验镜像和 postgres），所以任何起沙箱解题
   （对话/CTF/赛事托管）全都失败。

两件事一起做完了。

## 二、A 方案：对话页三工具（复用同一场 racer）

把「赛事自动托管」接进对话，模型可用三个新工具（对话工具从 16 → **19**）：

| 工具 | 作用 | scope | 有副作用 |
|---|---|---|---|
| `start_competition_race` | 起一场整场自动托管（多模型对每道题赛跑、卡住自动重试、跳过已解、自动交 flag） | scan | 是 → 走审批 |
| `competition_race_status` | 只读查进度 / 已解 N/M / 成本 | read | 否 |
| `stop_competition_race` | 停止正在进行的托管 | scan | 是 → 走审批 |

**关键设计：HTTP 面板与对话工具共用同一场单例。**
新增 `suvex-api/app/ctf_race/manager.py`（进程内单例，`_state{racer,task,report,error}` +
`_lock`）。`app/ctf_race/router.py` 的 `start/stop/status` 全部委托给 manager，
对话工具 `_tool_start/status/stop_competition_race`（`app/chat/engine.py`）也走它。
这样不会出现「面板起了一场、对话又起一场」的双实例双开赛跑。已在跑的再起会抛
`RaceAlreadyRunning` → 面板回 409 `RACE_ALREADY_RUNNING`、对话工具回一句人话。

**提示词**：`app/chat/prompts.py` 的 CTF 人格 guidance 末尾新增「赛跑 / 整场托管」
段，教模型：用户说「帮我全跑了 / 整场比赛交给你 / 多模型一起跑」就走
`start_competition_race`，而不是自己 `fetch_competition_challenges` + `run_shell`
一题题手搓。原生 tool-calling 下模型看到的是 `build_tools_schema()` 里的工具描述。

**审批卡**：`start/stop_competition_race` 是 consequential，会弹审批卡。因为
「起赛跑会花钱 + 自动交 flag」，`suvex-web/src/components/chat/ChatControls.tsx`
新增 `APPROVAL_TOOL_HINTS`，给它们各配一句人话说明，让用户能判断该不该点「执行」。

## 三、沙箱镜像重建（base + re + ir）

**重要更正**：早期排查误以为「仓库无 vendor 目录」，实为查错了路径。真位置是
`suvex-api/docker/suvex-sandbox/vendor/`（与 Dockerfile 同目录，13,812 文件，git 跟踪）。
`docker/README.md:41` 那句「两个 Dockerfile 都没有 COPY/ADD」是**过期错误**，实际
base Dockerfile 有 11 处 `COPY vendor/`。正确构建：

```
cd suvex-api/docker/suvex-sandbox
docker build -f Dockerfile -t suvex-sandbox:latest .     # context = 该目录
docker build -f ../suvex-sandbox-re/Dockerfile -t suvex-sandbox-re:latest <空ctx>
docker build -f ../suvex-sandbox-ir/Dockerfile -t suvex-sandbox-ir:latest <空ctx>
```

**踩到的两个真 bug（v1.63 引入、首次真机构建才暴露）**：

1. `fpylll` 的运行期依赖 **`cysignals`** 没装 → `import fpylll` 报
   `ModuleNotFoundError: No module named 'cysignals'`。修法：pip 段加 `cysignals`。
2. 自检脚本 `A[0]=[1,0,0]` 抛 `NotImplementedError`（fpylll 的 `IntegerMatrix`
   逐元素赋值 `A[i,j]=v` 才认，`A[i]=列表` 不认）。修法：自检改成
   `A=IntegerMatrix(3,3); LLL.reduction(A)`（零矩阵本既约，只为证明模块运行期可用）。

构建结果（Kali）：

| 镜像 | 大小 | 关键内容 |
|---|---|---|
| `suvex-sandbox:latest` | 5.29 GB | web/取证/pwn/misc 全套；capstone 6.0.0 + unicorn 2.1.2 |
| `suvex-sandbox-re:latest` | 6.59 GB | + angr / r2 / gdb（逆向专用） |
| `suvex-sandbox-ir:latest` | 5.39 GB | + 应急响应/取证（23 件） |

`scripts/verify_sandbox_tools.py --probe`（真问镜像）：**8 个分类全部 ✅**。

## 四、验证（真机，非摘要）

- **沙箱生命周期**：用 app 自身 `SandboxContainer` 真起容器 →
  `exec` 返回 `hello-from-sandbox` + `fpylll-in-sandbox`（exit 0）→ `destroy` 干净。
  证明镜像真能用，原「Unable to find image」根除。
- **对话页真解题**：session `chat_01M6RZ2RMT8BFQ4TAVZEA6ZWC5`，让它建 `test171.txt`
  写 `hello171` 再读回——正文 `run_shell cwd:/work` 真在容器里跑，`cat` 读回 + `xxd`
  验证 `6865 6c6c 6f31 3731 0a`，干净 `answer` 收尾，turn `done / round 9 / tools 3`。
  截图 `artifacts/v171-solve.png`（read_image 复核）。
- **对话工具注册**：prod `build_tools_schema()` = **19** 工具，三个 race 工具都在
  `_TOOL_IMPLS`，manager 可 import，`/ctf-race/status` 未登录 401 / admin 登录 200 idle。
- **单测**：`tests/chat/test_chat_race_tools.py` 新建 10 passed；`tests/ctf_race/`
  50 passed；全量 `tests/chat/` **498 passed, 2 skipped**（12 分 18 秒）；
  前端 chat 组件 **120 passed**，`tsc -b` 0 错，`eslint` 0 告警，`vite build` ✓
  （`index-tou3VrrM.js`）。

## 五、部署（Kali 生产）

- 后端：`app/chat/engine.py`、`app/chat/prompts.py`、`app/ctf_race/router.py`、
  `app/ctf_race/manager.py`（新增）——prod sha 与本地逐字节一致；重启
  `Application startup complete`，health 200。
- 前端：dist 同步（`index-tou3VrrM.js`），nginx bind mount。
- 镜像：三个 tag 已在 Kali（上面第一张表）。

## 六、诚实记录的遗留

1. **平台侧仍不可达**：`ycb-test.dasctf.com` 的 HTTPS 证书 hostname 不匹配
   （`CERTIFICATE_VERIFY_FAILED: Hostname mismatch`），且此前 AccessKey 也曾
   失去 agent 接口权限。所以「真跑一整场」的 happy path 这次**没法端到端证明**——
   代码链路（工具注册 / manager / 端点 / 沙箱启动）都已验证，只差平台可达。
2. 模型对「整场托管」在平台失败时会退回手动 `fetch_competition_challenges` 探索
   （这次就是，因为平台 SSL 直接崩），属模型在错误分支的行为，不是工具没接上。
3. 简单任务模型偶有多余的再验证（round 9 / tools 3，非旧 38 轮病态循环），可接受。
