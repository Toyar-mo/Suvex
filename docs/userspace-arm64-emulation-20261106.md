# Userspace ARM64 模拟（unicorn）—— 无 KVM 环境下做 Android `.so` 动态分析

日期：2026-11-06

## 背景

会话 `chat_01M6P1TAYTASS6P64P85PED8MB`（「千千的咲」Android UE5 逆向）跑了 12 小时没出
结果。查下来模型把「动态分析」窄化成「起 Android 模拟器」，发现没有 `/dev/kvm` 就断定
「动态分析做不了」，于是反复换对称算法穷尽（12 亿+ 次全 0 命中）。**这是把唯一一条路
当成全部**：动态分析还有第二条路 —— **userspace 指令级模拟**，不需要 KVM。

## 环境事实

- Kali（`ssh kali`）本身是 VMware VM、宿主未开嵌套虚拟化：`/dev/kvm` 不存在、
  `/proc/cpuinfo` 的 `vmx/svm` 计数 = 0 → **整机模拟器起不来**。
- 但 userspace 模拟工具具备：
  - **chat 沙箱镜像 `suvex-sandbox` 里已装 `unicorn 2.1.2` + `capstone 6.0.0`（`/opt/venv/bin/python3`）**，
    有 `java`（unidbg 可用），**没有** `qemu-aarch64`（需要 `apt install qemu-user-static`）。
  - Kali 宿主上 `unicorn 2.1.4`（系统 python3）、`/usr/bin/qemu-aarch64`（qemu-user 11.1.0）、java 21 都有。

## 可行性实测（决定性）

用 unicorn 直接 `mem_map` 真实 `libUnreal.so`（arm64-v8a，96MB）的 PT_LOAD 可执行段并
`emu_start` 执行真实函数，**成功**：

```
=== disasm of real libUnreal.so exec start (vaddr 0x13e1000) ===
  0x13e1000: bti c
  0x13e1004: adrp x0, #0x53c5000
  0x13e1008: add x0, x0, #0xfb0
  0x13e100c: b #0x53bfce0
  0x13e1010: bti c
  0x13e1014: ret
  ...
=== executed real ARM64 instructions inside unicorn; X0 readable = 0x53c5fb0 ===
```

→ 用 unicorn 把 arm64 `.so` 的代码段映射进来、`emu_start` 跑目标函数、hook 读寄存器
拿返回值，**完全可行，不需要 KVM**。要做完整「喂密文→读明文」，把 `.so` 所有 PT_LOAD
段（含 `.rodata`/`.data.rel.ro`）都 `mem_map` 进去即可。

## 可复用模板（`docs/unicorn-arm64-probe.py`）

见同目录 `unicorn-arm64-probe.py`：解析 ELF64 程序头 → `mem_map` 全部 PT_LOAD 段 →
`uc.emu_start(函数地址)` → hook `UC_HOOK_CODE` 反汇编 + hook 读返回值。改 `TARGET` 为
你要模拟的函数地址（用 capstone 从 `strings` 命中的引用往上找函数边界）即可。

## 一个重要前提：先确认「到底有没有要模拟的函数」

**别假设「一定有解密函数」**。做之前先确认二进制真的引用了目标数据：

```
strings -n 6 libUnreal.so | grep -iE 'p\.bin|Data/|Decrypt|AES|Submit|IsComplete'
```

本例实测：`p.bin` / `Data/` / `Chisaki` / `Puzzle` 在 arm64/x86_64 两套 `.so` 里**全 0 命中**
（只有 `/Script/CTFGame` 5 处）—— 说明**这个 p.bin 根本没被游戏读取**，不存在「解密
p.bin 的函数」可模拟。这种情况下正确方向是分析**判题/校验逻辑**（`SubmitCell`/`IsComplete`
这类函数怎么判对错），而不是硬啃一个不存在的解密。另一个现实阻塞是**缺 flag 格式**
（有 `DASCTF{...}` 格式就能把密钥空间压到可爆破）。

## 已落地

- `suvex-api/app/chat/prompts.py`：新增「### 需要「动态分析」不等于「必须起 Android
  模拟器」——userspace 模拟不走 KVM」一节，明确 unicorn / qemu-user / unidbg 三条
  userspace 路，并要求「先 `strings` 确认目标数据真被引用、别假设有解密函数」。
- `suvex-api/tests/chat/test_download_guidance.py`：
  `test_userspace_emulation_guidance_present` 钉住这段指引不被删。

## 诚实边界

- userspace 模拟能跑**单个纯计算函数**（解密/校验/变换）；如果目标函数依赖未实现的
  JNI/系统调用且无法 stub，或需要真实 GPU/输入，才可能跑不通 —— 那时再如实说。
- 「没有 KVM」**不是**「做不了动态分析」。整机模拟走不了，userspace 走得了。
