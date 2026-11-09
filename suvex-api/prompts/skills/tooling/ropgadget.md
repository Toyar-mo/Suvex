---
name: ropgadget
description: ROP gadget 搜索（本镜像有 ROPgadget 7.7、one_gadget、pwntools、GEF）。需要构造 ROP 链、找 pop/ret、算 libc 偏移时用；不要把它当"利用生成器"——它只给候选，约束要自己核。
---

# ROPgadget（gadget 搜索）

本镜像：ROPgadget 7.7、`one_gadget`、pwntools（`pwn`）、GEF（gdb 内）。

## 起手

```bash
ROPgadget --binary /work/chall --only 'pop|ret' | head -30
ROPgadget --binary /work/chall --only 'pop|ret' --depth 6
ROPgadget --binary /work/libc.so.6 --only 'pop rdi|ret'
```

- `--binary` 是必需的；位置参数不会生效。
- `--only` 用正则缩小指令集合；大二进制全量搜索很慢，先收窄。
- `--depth` 控制 gadget 最大指令数（默认 6）；越大越慢、越难用。

## 使用纪律

- **候选 ≠ 可用**：每个 gadget 都有前置约束（栈布局、寄存器值、`rsp` 对齐）。
  找到 `pop rdi; ret` 只说明"能弹一个值"，不说明你的链能走到它。
- **PIE/ASLR**：静态地址不是运行时地址；先 `checksec`（`pwn checksec` 或 gdb
  里 GEF 的 `checksec`），PIE 要泄漏基址，libc 要泄漏版本/偏移。
- **one_gadget**：给的也是候选，带 `rsp+0x...` 等约束；直接跳大多不成立。
- **pwntools**：`ROP(elf)` / `ELF('/work/chall')` 能自动找 gadget 并拼链，
  比手抄地址可靠；复杂链写脚本。

## 本镜像的相关事实

- `checksec` 是 pwntools 的 `pwn checksec`（会先打一段 terminfo 警告，属噪音）。
- gdb 通过 `/etc/gdb/gdbinit` 加载 GEF：`checksec` / `vmmap` / `pattern` 可用；
  用 `-nx` 会关掉 GEF。
- `pwn disasm` 在这个镜像里是坏的（Python 3.14 的 safeeval 问题）；反汇编用
  `objdump` / `radare2` / `ghidra-decompile`。

## 与其他工具的衔接

- 反汇编/伪代码：`objdump -d`、`radare2`、`ghidra-headless`。
- 防护：`checksec`；调试：`gdb`（GEF）。
- 利用脚本：`python3` + pwntools（`from pwn import *`）。
