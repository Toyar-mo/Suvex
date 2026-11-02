# 羊城杯「AI Agent 解题赛道」对赛自查与能力升级评估

> 日期：2026-11-02　｜　适用赛事：2026 羊城杯决赛 · AI Agent 解题挑战赛（11/7 12:00–15:00）+ 测试赛（11/3 14:00–20:00）
> 依据：`artifacts/yangcheng_env.u8.txt`（赛事环境规范）、`artifacts/yangcheng_manual.u8.txt`（决赛手册）
> 说明：赛事方 `api_doc.md` 未在仓库内找到；但 `app/kernel/drivers/slabmatch.py` 就是照那份文档写的驱动，以下以代码为准。
> 本文件是**评估**，不含代码改动。改哪些由你决定。

---

## 一、结论先行

**好消息：系统已经有一整套为这种比赛写好的对接层，不是从零开始。** 平台驱动、批量并发调度、flag 格式校验、对话记录落盘、错误提交次数计量、自动同步放题——都在 `app/kernel/` 里，且有测试覆盖。

**但有 3 个「会直接影响比赛能不能跑起来」的缺口，和 4 个「跑起来也不吃亏，但值得补」的短板。** 按严重度排：

| # | 等级 | 缺口 | 后果 |
|---|---|---|---|
| G1 | 🔴 阻断 | **配置驱动的平台接入（`driver_for`）没有接线** —— 代码里自己写着"这是下一轮接线的入口，现在先放着"，全库只有测试调用它 | 想接 `ycb-test.dasctf.com` 只能走内置 CTF2 老路；若测试赛平台与 CTF2 接口不同，**根本连不上** |
| G2 | 🔴 阻断 | **错误提交上限被硬限死 ≤10**（`MAX_WRONG_SUBMISSIONS_CEILING = 10`），赛事规则是**每题 50 次** | 配置层无法表达"50 次"，自动提交会提前耗尽额度判断（或误判 failed） |
| G3 | 🟠 高 | **提交的是「答案」而非「花括号内内容」**：`validate_answer` 只做归一化，不做 `DASCTF{}`/`flag{}` 的**花括号剥离** | 若模型交出 `DASCTF{abc}`，平台（要求只交 `abc`）会判错，白扣额度 |
| G4 | 🟢 低 | ~~并发上限复用扫描配额~~ **实为已修**：`max_concurrent()` 自 v0.22 起读 `KernelConfig.max_concurrent_solves`（独立配额），只是文件头 docstring 仍写旧方案 | 无功能影响；纯文档债，比赛直接调 `maxConcurrentSolves`（上限 16）即可 |
| G5 | 🟡 中 | **每题回合预算默认 80 回合**（上限 500） | 难题/需要长时间爆破的题容易在预算内解不出 |
| G6 | 🟡 中 | **对话记录是「每题的 transcript 文件」**（`/work` 之外的磁盘），不是赛事要求的 JSONL 导出 | 赛后审计要求"完整 AI 对话记录"，现在要自己找文件、没现成导出按钮 |
| G7 | 🟢 低 | **`drivers/__init__.py` 文档与实现不符**（说要在 `registry.py` 注册，但那个文件不存在） | 纯文档误导，不影响运行 |

---

## 二、逐条对照赛制

### 2.1 flag 格式与提交内容（赛制 §三.1）

> 赛事原文：「flag 格式均为 `DASCTF{}` 或者 `flag{}`，提交时**仅需提交 `{}` 内内容**即可。若有特殊格式要求将在题目内注明。」

- **现状**：`app/kernel/runner.py:3197 validate_answer()` —— v0.21 起判据从"像不像 flag"放宽成"只拦明显不可能"（空/超长/占位符）。它**会** `normalize_flag` 去引号反引号，**不会**剥 `DASCTF{...}` 的外壳。
- **差距**：若模型把整个 `DASCTF{abc}` 交上来，平台要的是 `abc` → 判错。
- **注意**：`validate_answer` 的 docstring 特意写明"比赛允许提交命令行/IP/时间戳"（那是另一个赛道的规则）。本次赛事**明确只交花括号内内容**——两者冲突时以本次赛事为准。所以这里需要**新增一个可配置的「flag 外壳剥离」步骤**，而不是改掉通用的宽松策略。

### 2.2 分批放题（赛制 §三.2）

> 「赛题将根据赛程情况分批放题，请注意平台公告。」

- **现状**：`platform.RemoteChallenge.is_open` 已有「是否已放题」字段；`autosync.py` 定时拉题（`_interval` 每轮读设置，0=关闭）。`slabmatch.fetch_challenges` 按 corpus 分组返回。
- **结论**：✅ 覆盖。只要把 autosync 间隔调到合适（如 60–120s），分批放题会被自动同步进来。

### 2.3 递减计分 / 实时排名（赛制 §三.3–4）

> 「每道题初始分…每多一人降 1%，下限 80%…赛事排名按分数高低实时排名，同分看时间。」

- **现状**：`slabmatch.overview()` 打 `GET /answer-panel/overview`，**返回里含 `stageRank`**（排名）。`match_info()` 打 `/match/notice/match-info`。
- **结论**：✅ 数据可拿到。但**评分/排名的展示前端**需要确认是否已渲染 `stageRank`——这是"看板"层面的活，不影响能不能提交，影响的是"你要不要盯榜"。**建议**：赛前确认总览页有没有把 `stageRank` 显示出来。

### 2.4 每题 50 次提交上限 / 禁爆破（赛制 §三.5、§四.3）

- **现状（关键差距 G2）**：`app/kernel/service.py:163 MAX_WRONG_SUBMISSIONS_CEILING = 10`，`_assert_wrong_ceiling()` 拒绝 >10；`MIN_WRONG_SUBMISSIONS = 0`（service.py:162）；默认 `DEFAULT_CTF2_MAX_WRONG_SUBMISSIONS = 3`（`vocab.py:1276`）。语义上这是"我们本地允许试几次"的保守阀（省额度），不是平台真上限。
- **差距**：赛事给 50 次。现在最多只能配到 10。虽然没有"必须用满"的道理，但**如果一题需要多次试探**（例如 flag 里含大小写变体），10 次可能不够而 50 次才合规。**这不是 bug，是一个过紧的本地保护阀**——需要放开到 50（并保持"不爆破"纪律：不做穷举，只交有依据的候选）。
- **改动面**：只需 `MAX_WRONG_SUBMISSIONS_CEILING` 一个常量 10→50（连同 `settings/models.py:469` 与 `schemas.py:1138` 的注释同步）；默认值 3 可保留（保守是好事），比赛按题调高即可。
- **验证**：`_advance_challenge`（`runner.py:7113`）在 `remaining_wrong_submissions <= 0` 时置 `failed`——上限放开后这条仍成立，只是阈值变 50。

### 2.5 Agent 环境自部署 / API 接入（赛制 §二.1、§五）

- **现状（关键差距 G1）**：`app/kernel/platforms.py:466 driver_for()` 明写「这个函数是**下一轮接线**的入口（`service.sync`/`runner` 会改走它）。现在先把它放在这里」——全库 grep 只有 `tests/kernel/test_platforms.py` 调用，**生产路径没接**。也就是说：
  - 账号 `platform_id IS NULL` → 走内置 `KernelClient`（CTF2 老接口 `/slab-match/api/v1/agent` 那套）。
  - 有 `platform_id` → 会造 `GenericDriver`，但**没人调它**。
  - 结果：**配置驱动的通用适配器（含 `probe.py` 里已内置的 `("dasctf","DASCTF/CTF2")` 指纹）是一套"写好了、测试了、但没用上"的能力。**
- **影响判断**：如果 `ycb-test.dasctf.com` 与现有内置 CTF2 客户端（`app/kernel/client.py`，1497 行，`/slab-match/api/v1/agent` 路径）**接口一致**，那么不接 `driver_for` 也能跑 —— 这是最可能的实际情况（`slabmatch.py` 的路径前缀就是 `/slab-match/api/v1/agent`，且 `probe.py` 把 `dasctf` 映射到这里）。**若接口一致，G1 降级为"技术债，不阻断"；若测试赛接口有出入，G1 升级为"必须先接线"。**
- **必须做的确认动作**：测试赛（11/3）当天，登录 `ycb-test.dasctf.com` 拿【文档中心】的 API 接入说明，与 `slabmatch.py` 逐字段核对（尤其：认证头 `X-Agent-AccessKey`、成功码 `00000`、提交体 `{exerciseId, flag}`、起环境 `/ctf/build-exercise-env`）。**这是赛前 1 号待办。**

### 2.6 必须用本人账号 + 团队 accesskey（赛制 §四.1）

- **现状**：`KernelAccount`（`settings/models.py`）有 `token_encrypted`/`token_hint`/`platform_id`/`status`/`last_error`；`slabmatch.AuthSpec(style="header", name="X-Agent-AccessKey")` 走 header 认证；`activate_platform()` 会加密存 accesskey 并验证可见题数。
- **结论**：✅ 覆盖。赛前只需在设置页把 accesskey 填进去并「启用平台」，验证能看到题。

### 2.7 保留完整 AI 对话记录备审（赛制 §四.5）

- **现状（差距 G6）**：`runner.py:7977 _save_transcript()` 把 `SolveOutcome.transcript`（每轮命令+输出摘要）写到磁盘；`writeup.py` 生成题解（含 flag 原文）。**但**：
  - 这是"每题一份 transcript 文本"，**不是**赛事举例的「一会话一 .jsonl」那种完整 LLM 对话记录（含模型原始输出）；
  - **没有导出按钮/端点**把它打包成审计材料。
- **差距**：赛后若被要求"提交 AI 对话记录"，需要有人手动去 `<transcript 目录>` 找文件。**建议**：加一个「审计导出」端点/按钮，把某场比赛所有题的 transcript + 模型原始交互打成一个 zip。工作量中等，不影响比赛能不能提交，影响的是**合规审计**。

### 2.8 批量并发多题（AI Agent 赛道核心玩法）

- **现状**：`app/kernel/scheduler.py`（684 行）线程池调度：`tick()` = 回收 + 算名额 + 认领；`_claim` 按 `priority DESC, updated_at ASC`，名额 = `max_concurrent(db)` = `KernelConfig.max_concurrent_solves`（默认 3，`settings/models.py:532`，上下限 1–16，`vocab.py:1293-1298`）。`main.py:174-226` 启动即起调度器。`_MAX_CLAIMS_PER_TICK = 4`（每 tick 最多认领 4 道）。
- **结论**：✅ 批量并发能力**完整可用**。比赛时把 `maxConcurrentSolves` 调到 8–16 即可（受机器 CPU/内存与 LLM 速率约束）。
- **注意（G4，已更正）**：并发上限**已是独立配额**——`max_concurrent()`（`scheduler.py:86-119`）自 v0.22 起读 `KernelConfig.max_concurrent_solves`（默认 3，上下限 1–16），**不再**借用 `max_concurrent_scans`。文件头 docstring（第 9、23 行）仍写"故意复用 max_concurrent_scans"，是**过时描述**，与实际实现相反——不要照那段注释调，直接改 `maxConcurrentSolves`。决赛两个赛道（靶场评估 + Agent 解题）同一场进行时，两者的并发**是各自独立的**（分层配额），不会互相挤占。

### 2.9 题目优先级 / 前三名加分（对应赛制 §三.3 递减计分）

- **现状**：`KernelChallenge.priority` 列，`_claim` 排序 `priority DESC` —— 高优先级先做；docstring 明确提到"前三名额外加分"场景下选题顺序直接换分。
- **结论**：✅ 覆盖。赛前可对高分题设高 priority。

### 2.10 flag 最多 50 次 / 禁爆破 / 反作弊（赛制 §四.2–4）

- 见 2.4（50 次上限需放开）与 2.1（花括号剥离）。反作弊是赛事侧机制，系统侧只需保证"不做穷举爆破"——当前设计本来就是"只交有依据的候选"，✅ 合规。

---

## 三、升级方案（按优先级，等你点头再动手）

### P0 —— 赛前必须做（否则可能连不上/被判错）

1. **对照测试赛文档核对 `slabmatch.py`**（不是改代码，是核对）：11/3 当天拿 API 文档，逐字段核对认证头、成功码、提交体、起环境/收环境路径。**若一致 → 什么都不用改；若不一致 → 按差异最小改驱动。**
2. **放开错误提交上限到 50**：`service.py:163 MAX_WRONG_SUBMISSIONS_CEILING` 10 → 50；`vocab.py` 的 `MAX_CTF2_MAX_WRONG_SUBMISSIONS` 若有独立常量同步。测试补一条"可配 50"。
3. **新增「flag 外壳剥离」**：在 `validate_answer` 之前（或在提交前）加一步：若答案匹配 `^(DASCTF|flag|FLAG)\{(.*)\}$`，剥成组 2。**必须做成可配置**，因为 2.1 提到的另一个赛道允许命令行/IP 答案——不能全局剥。测试覆盖 `DASCTF{abc}→abc`、`flag{abc}→abc`、`abc→abc`（不变）、`DASCTF{}→拒绝（空正文）`。

### P1 —— 强烈建议（影响成绩上限）

4. **接 `driver_for` 到生产路径**：若 P0-1 发现接口有差异，就把 `service.sync`/`runner` 改走 `driver_for(db, account)`，让配置驱动真正生效（`GenericDriver` 已实现、`probe` 已认 DASCTF 指纹）。若接口完全一致，则作为技术债记录，赛后再接。
5. **调并发与放题节奏**：赛前把 `maxConcurrentSolves` 设到机器能扛的值（先压测 4/8，看 CPU/内存/LLM 429）；把 `autosync` 间隔调到 60–120s 跟放题节奏。（无需动扫描配额——两套配额已是独立的。）

### P2 —— 值得做（合规与体验）

6. **审计导出**：加「导出本场对话记录」端点，把 transcript + 模型交互打包，满足 §四.5 备审。
7. **确认总览页渲染 `stageRank`**（排名看板）与 `match_info` 公告展示。
8. **文档修正**：`drivers/__init__.py` 说"在 `registry.py` 注册"（文件不存在）；`scheduler.py` 文件头 docstring 说复用 `max_concurrent_scans`（实际读 `max_concurrent_solves`，v0.22 已改）。两处都改成与实现一致。

### P3 —— 可选优化

9. **回合预算**：`DEFAULT_CTF2_MAX_TURNS=80`（上限 500）。若历史数据显示难题常在 80 回合内解不出，赛前调到 120–200。
10. **多 agent**：`app/kernel/multiagent.py`（2806 行）已有子 agent 并行拆解能力（`MAX_CHILDREN=6`）——可作为"难题多人并攻"的开关，需评估 token 成本。

---

## 四、需要你在测试赛当天确认的事项（清单）

- [ ] `ycb-test.dasctf.com` 登录 →【AI Agent 夺旗赛】→【文档中心】导出 API 接入说明（markdown）
- [ ] 与 `slabmatch.py` 核对：认证头名、成功码、"获取题目/开关靶机/提交 flag/看公告"四个端点路径与请求体
- [ ] 用团队 accesskey 在设置页「启用平台」，确认能拉到题、能看到题数
- [ ] 手动跑通一道简单题：起环境 → 解题 → 提交 → 平台确认 correct
- [ ] 确认 flag 提交格式：故意交一次 `DASCTF{正确内容}` 看是否被判错（验证是否需要剥离）

---

## 五、一句话总结

现有系统**已经具备**跑这个赛道的绝大部分能力（批量并发、平台对接框架、flag 校验、额度计量、自动同步都在）；真正的风险集中在**一处硬闸门 + 两处细节**——① 配置驱动平台接入没接线（是否阻断取决于测试赛接口与现有 CTF2 是否一致，赛前一天拿文档核对即可定性）、② 错误提交上限被限死在 10（赛事给 50，改一个常量）、③ 花括号未剥离（会白扣额度）。这三项赛前处理掉，剩下的都是锦上添花。
