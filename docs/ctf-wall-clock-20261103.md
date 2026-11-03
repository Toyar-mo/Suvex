# CTF 每题墙钟预算（v1.61 项目1）

> 承接用户 2026-11-03 的诉求：「看看项目对于解题速度和工具使用上还有没有可优化的……遇到难的也要有应对的办法」。
> 审计结论的关键缺口：**CTF 求解路径没有墙钟预算**。本文件记录该缺口的病根、修法与验证。

## 病根

`SolveSpec.wall_clock_seconds` 在 CTF 求解路径上**恒为 `None`**。

- 墙钟三档催促（`WALL_WARN_BANDS = (0.60, 0.80, 0.92)`，`suvex-api/app/kernel/runner.py:410`）是唯一按**时间轴**推进的收尾机制，也是唯一会**硬杀**的维度（到点 `task.cancel()`）。
- 但 CTF 走 `runner._run_worker` → `solve_in_sandbox`（runner.py:8360），全程没有把 `wall_clock_seconds` 设上值，也没有设 `wall_clock_started_at` → 这三档**从未触发**。
- 结果：只剩「回合催促」（70/85/95% 轮次）兜底。可它治不了「一轮花很久」的题。真库实测：有题跑满 80 轮、单次求解墙钟烧到 **9.5 小时**（借壳 `Scan.duration_sec` 观测）。
- 对照：渗透路径 `app/scans/agent_runner.py:751` 附近早就有 `solve_spec.wall_clock_seconds = timeout_seconds; wall_clock_started_at = started_at` —— CTF 路径漏了这条接线。

## 设计

- **默认墙钟 = `max_turns × 45 秒`**（`DEFAULT_CTF2_SECONDS_PER_TURN = 45`）。
  - 80 轮 → 3600s（60 分钟）；200 轮 → 9000s；夹取到 `[120, 86400]`（2 分钟 ~ 24 小时）。
  - 随回合联动：调大回合，墙钟自动跟着放宽，不会出现「回合够但墙钟先杀」的错配。
- **`NULL` = 自动推导，不是关闭**。这一点在 schema / model / 前端三处都点了名，避免被误读成「不设 = 不限时」。
- **显式值覆盖**：管理员可在设置页填具体秒数；越界（<120 或 >86400）→ 400 校验错，**不静默夹取**（与 `_assert_wall_clock_seconds` 纪律一致）。
- 不给 `MAX_CTF2_WALL_CLOCK_SECONDS` 之外的「无限」选项：墙钟是保护（防跑飞烧钱/烧时间），不是惩罚。

## 改动（后端）

| 文件 | 改动 |
| --- | --- |
| `suvex-api/app/core/vocab.py` | `DEFAULT_CTF2_SECONDS_PER_TURN=45`、`MIN/MAX_CTF2_WALL_CLOCK_SECONDS=120/86400`、`default_ctf2_wall_clock_seconds(max_turns)`（`max(max_turns*45, MIN)` 夹 MAX） |
| `suvex-api/app/settings/models.py` | `KernelConfig.wall_clock_seconds: Mapped[int|None]`（nullable，NULL=自动） |
| `suvex-api/app/kernel/service.py` | `KernelSettings.wall_clock_seconds`；`effective_ctf2_settings` 带出；`update_ctf2_settings(wall_clock_seconds=...)`；`_assert_wall_clock_seconds`（[120,86400] 越界 400） |
| `suvex-api/app/kernel/schemas.py` | `KernelSettingsOut.wallClockSeconds`；`KernelSettingsIn.wallClockSeconds = Field(default=None, ge=120, le=86400)` |
| `suvex-api/app/kernel/router.py` | `_settings_out` 带出；PUT 透传；audit detail 记 `wallClockSeconds` |
| `suvex-api/app/kernel/runner.py` | `build_spec` 解析墙钟（配置非 None 用之，否则自动推导）；`solve_in_sandbox` 开跑时打 `wall_clock_started_at = time.monotonic()`（在 `outcome=SolveOutcome()` 之后，因为 build_spec 到真正开跑之间还有建容器耗时，不该算进模型预算） |
| `suvex-api/alembic/versions/20261103_2255_a7e3c9d24b61_wall_clock_seconds.py` | 新迁移 `a7e3c9d24b61`（down_revision `f2c8a71d9b64`），`add_column("suvex_config", "wall_clock_seconds", Integer, nullable=True)` |

## 改动（前端）

| 文件 | 改动 |
| --- | --- |
| `suvex-web/src/data/kernel.ts` | `KernelSettings` / `saveKernelSettings` 参数加 `wallClockSeconds: number | null`；新增测试专用 `__resetKernelForTest()`（清模块级缓存） |
| `suvex-web/src/components/settings/KernelPanel.tsx` | 新增「每题墙钟预算（秒）」字段（留空 = 自动，placeholder 显示推导值 `自动（{maxTurns*45}s）`）；`wallClockError` 只在填了非法值时红；dirty 检测 + 保存体 + 校验守卫都接上 |

## 测试

- **后端** `suvex-api/tests/kernel/test_wall_clock.py`（14 条）：推导边界（80→3600 / 200→9000 / 1→120 下界 / 10000→86400 上界）、HTTP NULL=自动往返、显式值往返、越界 400、漏传=自动、service 默认/持久化、effective 带字段、`build_spec` 真出非 None 墙钟且 `started_at` 为 None。**14 passed**。
- **前端** `suvex-web/src/data/kernel.test.ts`（+2）：显式值原样进 PUT 请求体、自动（null）也把键带出去且是 `null` 不是缺字段。**全 25 passed**。
- **前端渲染** `suvex-web/src/components/settings/KernelPanel.render.test.tsx`（新，4 条）：字段渲染出来（label/help 对）、`null` 显示空串且 placeholder=`自动（3600s）`、显式值原样显示、换回合 placeholder 跟着变（200→9000）。**4 passed**。
- 全前端回归 **76 files / 1138 passed**；`tsc -b --force` / `eslint --max-warnings 0` / `npm run build` 全绿。
- 后端回归 `tests/kernel/test_settings_validation.py` + `test_solve_attempt.py` **28 passed**。

## 部署与线上验证（Kali 192.168.44.128）

- 后端 7 文件 prod sha == 本地；`alembic upgrade head` → `a7e3c9d24b61 (head)`；DB `suvex_config.wall_clock_seconds integer nullable=YES`；重启 `Application startup complete` + health 200。
- **prod 端到端铁证**（部署代码 + 真库）：`runner.build_spec(s, 真题 [ZJCTF 2019]NiZhuanSiWei)` → `max_turns=500, wall_clock_seconds=22500 (=500×45 自动), started_at=None` —— 墙钟已真接上电（`effective_ctf2_settings` 当前 `wall_clock_seconds=None, max_turns=500`）。
- 前端 dist 已部署（`index-BaUM42WQ.js`，prod == 本地）；字段在真浏览器 + 真 prod CSS 下渲染，「每题墙钟预算（秒）」+ placeholder「自动（3600s）」验证通过。

### 视觉复核说明（诚实边界）

设置页 `/settings` 是 **admin 级**（需「管理权限」）。只读账号 `zzviewer88` 打开会看到 `INSUFFICIENT_SCOPE` 的整页错误条，**所有面板表单都不渲染** —— 这是真实的访问控制，不是 bug。因此视觉复核走的是**真组件 + 真 CSS**：从 `KernelPanel` 的真实渲染（render test）导出 DOM，用 prod 线上样式表在浏览器里显示并截图（`artifacts/v161-wallclock-field.png`）。它证明的是「组件真实渲染出的字段与样式」，不能冒充「以 admin 身份打开整页设置」的截图。

## 影响的既有机制

墙钟接上电后，`runner.py` 里**早已存在**的 `_wall_clock_band` / `_wall_clock_seconds_left` / `_wall_clock_note` / `_wall_warn_stage` 从「全程沉默」变为真正参与：到 60%/80%/92% 会向模型注入整理证据、准备收尾的提示，到点硬杀本轮。这意味着**长跑题现在有了时间维度的兜底**，不再只能靠回合数。
