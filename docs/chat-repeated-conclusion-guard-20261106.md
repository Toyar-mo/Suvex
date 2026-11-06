# 同一结论复读闸（v1.69）—— 卡死的长任务不再换个算法无限穷尽

日期：2026-11-06

## 用户问题

会话「千千的咲.zip解题」（`chat_01M6P1TAYTASS6P64P85PED8MB`）单会话跑了
**12+ 小时 / 429 次 run_shell / 127 轮**，最后仍没解出。用户问：

> 查一下什么问题跑了 60 分钟没解出来，题目太难了吗，怎么提升效率？
> 项目不是跑在 kali 吗，直连 kali 真机不就行了，A 也要做。

## 环境事实（先纠正一个误解）

「直连 kali 真机」并不能解锁动态分析 —— **Kali 本身就是一台 VMware 虚拟机，且宿主
没开嵌套虚拟化**：

```
$ ssh kali 'ls /dev/kvm; grep -c -E "vmx|svm" /proc/cpuinfo; systemd-detect-virt'
ls: cannot access '/dev/kvm': No such file or directory
0
vmware
```

- `/dev/kvm` 不存在；`/proc/cpuinfo` 的 `vmx/svm` 计数 = 0（宿主 i5-13490F 支持 VT-x，但没透传进这台 VM）。
- 结论：Android 模拟器起不来（需要 `/dev/kvm`），与沙箱里模型实测到的硬阻塞是同一个。

## 真正的效率损失（不是题太难、也不是没努力）

模型对这个挑战做了 **11 类对称算法全空间穷尽（12 亿+ 次，0 命中）**，又自查纠正了
6 处错误，静态分析非常扎实。它自己也得出了正确判断：**密钥只能运行时代码生成，必须动态分析**。
但问题在于：

1. **模型把「动态分析」窄化成「起 Android 模拟器」**，漏了 userspace CPU 模拟这条路
   （unidbg / unicorn / qemu-user 不需要 KVM，可以直接模拟 arm64 `.so` 里的解密函数）。
   于是它反复在静态侧换算法穷尽同一个不可行结论。
2. **没有「同一结论复读就强制收尾」的机制** —— 每步都是**不同**的真实命令，
   `MAX_REPEATED_ACTIONS`（连续相同命令）永不触发；`TURN_WALL_SECONDS` 单轮 60 分钟到点
   停下，用户一点「继续」又续 60 分钟 ×N，于是一个"已经想清楚做不了"的活把算力和
   用户耐心一起烧光。

## 本次修复：A —— 同一结论复读闸（v1.69）

### 判据

`nudge.is_repeated_conclusion(texts)`：在最近 `window` 条 assistant 正文里，
**两两成簇**——任意一个"簇"内彼此 2-gram Jaccard ≥ 0.7 且簇大小 ≥ 3，即判"同一结论复读"。

> 为什么是"成簇"而不是"锚最新一条"：线上冒烟发现模型会
> 「结论 A → 换句独特推理 → 结论 A → 换句独特推理」地**振荡**，锚最新一条会漏判。
> 成簇法在振荡序列里也能抓到（`test_is_repeated_conclusion_detects_oscillation` 钉住）。

### 上下文门槛（防误杀正常长任务）

只在**已经干了足够多活**时才判复读：
- `REPEATED_CONCLUSION_MIN_TOOLS = 20`（已执行 ≥20 次工具）
- `REPEATED_CONCLUSION_MIN_ROUNDS = 12`（已跑 ≥12 轮）

真正卡死的会话是几百次工具、上百轮；正常任务远低于此。这两道门槛避免了把
「刚开始干活时正常地重复描述现状」误判成"结论复读"（否则会误杀正常会话与既有测试）。

### 命中后行为

在 `run_turn` 与 `resume_approved` 两处循环里（与墙钟预算闸同一位置），命中即：
落一句 `render_repeated_conclusion_note()`（"我注意到刚才已经多次给出同一个结论，
就不再重复跑下去了——这不是「还差一点没试到」，而是当前环境/信息确实卡住了。
如果要继续，请先补齐缺失的条件…"），收尾返回。**不再自动续跑**。

## 改动文件

- `suvex-api/app/chat/nudge.py`：新增 `_fingerprint` / `_jaccard` / `is_repeated_conclusion`
  与两个常量。
- `suvex-api/app/chat/prompts.py`：新增 `render_repeated_conclusion_note()`。
- `suvex-api/app/chat/engine.py`：新增 `_recent_assistant_texts()`（取最近若干条
  assistant 正文，**不限 surface**——带动作的叙述是 `surface="process"`，复读恰恰发生在
  这些叙述里）；两处循环接入复读闸 + 两个上下文门槛常量。
- `suvex-api/tests/chat/test_native_tool_calls.py`：新增 6 条测试（成簇复读/振荡/不同进展不误判/
  少于阈值/提示文案/run_turn 命中即收尾）。

## 验证

- 后端 `tests/chat/` 全量 **479 passed, 2 skipped**（绿）。其中既有 3 条曾因复读闸误判
  而红（`test_narrative…`/`test_repeated_plan_finals…`/`test_repeated_finals…`），加
  `MIN_TOOLS/MIN_ROUNDS` 门槛后恢复绿——这也正是门槛存在的理由。
- 部署 Kali prod：`engine.py` / `nudge.py` / `prompts.py` 三个文件 sha256 与本地逐字节一致；
  重启后 `Application startup complete`、`/api/v1/health` 200。
- prod 直接 import 验证：`REPEATED_CONCLUSION_MIN_TOOLS=20`、`MIN_ROUNDS=12`、
  `render_repeated_conclusion_note()` 正常、`is_repeated_conclusion` 对复读/振荡返回 True。
- 线上冒烟（admin，新会话 `chat_01M6QRC7WPDGQ3VVK4EC29CWE0`，简单 echo 任务）：
  正常 6 轮 / 2 工具 / `done` 单条干净答案，**复读闸未误触发**（健康短任务不受影响）。

## 遗留（诚实记录，非本次范围）

- **userspace 动态分析能力**：Kali 已装 `unicorn 2.1.4` 与 `qemu-aarch64`（user-static）。
  下一步可评估用 unicorn 直接模拟 arm64 `libUnreal.so` 的解密函数（不需 KVM），
  并把这条方案写进提示词，避免模型再把「动态分析」窄化成「起模拟器」。
- 本闸只处理"结论复读"，不处理"每一步都不同但持续在做无意义验证"的另一种空转；
  那类仍受 `TURN_WALL_SECONDS`（单轮 60 分钟）与 `MAX_REPEATED_ACTIONS` 兜底。
