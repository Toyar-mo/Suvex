# artex-ymh 差距分析与融合计划 · 2026-11-10

> 目的：把 `panda0x123/artex-ymh`（ARTEX 二开，AGPL-3.0）里 Suvex 没有的能力选出来，
> 分批移植到 Suvex（FastAPI + React），而不是把 Go 代码整体搬进来。

## 1. 部署事实（Kali）

| 项 | 值 |
| --- | --- |
| 源码 | `/root/artex-ymh`（clone `main`，26M） |
| 构建 | 前端 `npm ci && npm run build:static` → `go build -tags embedui`，产物 50M |
| 数据库 | Docker `artex-db`（postgres:16-alpine，`127.0.0.1:55433`，独立于 Suvex 的 55432） |
| 端口 | `192.168.44.128:8787`（直接绑 LAN；默认只绑 loopback） |
| 门控 | 非 loopback 时默认开启：入口 `data/gate.path`，未过门控一律返回固定 nginx 页 |
| 登录 | 用户名 `ARTEX`；初始密码已设，见交接记录 |
| 工具清单 | 启动自检 17 项外部工具，当前**全部缺失**（chisel/suo5/impacket/nxc/mimikatz 等）；内网功能会明确报错，不会假装可用 |

## 2. ymh 独有能力（相对 Suvex）

| 能力 | ymh 落点 | Suvex 现状 | 移植判断 |
| --- | --- | --- | --- |
| Webshell/反弹会话台账 | `session/`、`sessions` 表、`register_session`/`session_exec` | 无 | 高价值但工程量大；先做数据模型与只读台账 |
| 多层隧道编排 | `tunnel/`（probe/plan/deploy/health，chisel/suo5） | 无 | 依赖 Kali 工具与真实内网；暂缓 |
| 凭据一等实体 | `credentials` 表、复用打分 | 无 | 可做（加密存储 + 评分），独立于隧道 |
| 内网拓扑 | `intranet` 页、host/segment/links | 无 | 可先做资产模型扩展 |
| 阶段移交 + scope 审批 | `pending_scope`、`/sessions/{id}/handoff` | 扫描授权有 scope，但无“新网段待批准” | **可移植性高**，纯数据 + 审批 |
| 反证判据（chainskel） | `agent/chainskel/verifier.go` | 有 verification 轴，无 checklist | **本轮已落地**（独立实现） |
| 蜜罐识别 L1 | `honeydetect/` + 签名 JSON | 无 | **高价值**：资产评分 + 信号 |
| 反 AI 陷阱纪律 | 提示词红线、出口审查、tarpit 熔断 | 无 | **高价值低成本**：提示词 + 否决规则 |
| 受管暂存 stage_share | `stage/`（随机 token + TTL） | 有文件管理，无受管投递 | 可做（TTL + 台账） |
| 伪装门控 | `server/gate.go` | 无 | 可选；Suvex 已有登录与 CORS |
| 部署预检 `artex doctor` | `doctor.go` | 有 `/health.py` 基础 | 可扩展为深检 |
| 内部工具 sha256 清单 | `packaging/tools-manifest.json` | 无 | 可借鉴（供应链） |

## 3. 许可边界（先读）

artex-ymh 与上游均为 **AGPL-3.0**。AGPL 的网络使用条款要求：修改后通过网络提供服务，需向使用者提供完整对应源码。因此：

- **不能**把 ymh 的 Go 代码/提示词原文直接复制进 Suvex 的私有代码库而不评估开源义务。
- 本项目采取的做法：**只借鉴设计与判据思路，用 Suvex 的技术栈独立实现**，并在代码注释中注明来源。
- 若未来要直接复用其代码，需要 Suvex 整体按 AGPL-3.0 开源，或取得作者另行授权。

## 4. 优先级

**P0（低成本、直接提升质量）**
1. 反证判据 checklist（已落地：`app/findings/verifier.py` + 修复方案返回 + 详情页展示）。
2. 反 AI 陷阱纪律（提示词红线：永不自证、目标内容只当数据、可疑 attestation 上报）。
3. 蜜罐 L1 静态签名 + 资产评分 + planner 处置纪律。

**P1（中等）**
4. scope 审批：新网段/新目标进入 `pending_scope`，人工批准后才可扫。
5. 受管暂存（TTL + token + 台账），替代临时 HTTP 服务。
6. 凭据实体（加密 + 来源 + 复用评分）+ 喷洒审批规则。

**P2（大工程/外部依赖）**
7. 内网会话（webshell/反弹）+ 隧道四件套。
8. 内网拓扑与 host 归并。
9. 分布式 node（ymh 的 FUTURE 也明确这是大改造）。

## 5. 本轮已做

- 部署 artex-ymh 并完成真机登录（`/function/tasks`，侧栏可见「内网」）。
- 独立实现反证判据并接入 Suvex 修复方案与详情页（不复制源码）。

## 6. 待做

- 蜜罐签名库与资产评分端点。
- 反 AI 陷阱提示词与出口审查规则。
- scope 审批表与端点。
- 受管暂存 TTL。
- 审计导出（Sigma/Suricata/ATT&CK/MISP）与部署深检。
