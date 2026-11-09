---
name: ghidra-headless
description: Ghidra 无头反编译（本镜像入口 ghidra-decompile）—— 需要伪代码级理解二进制、radare2/objdump 的反汇编不够用时用它；不用于动态调试或内存观察（用 gdb），也不用于加壳样本的第一轮（先 upx/binwalk 看壳）。
---

# Ghidra（headless）

本镜像：Ghidra 12.1.4（apt 安装），Java 25，**没有 GUI**。
入口是包装器 `/usr/local/bin/ghidra-decompile`，不是裸 `analyzeHeadless`。

## 起手

```bash
# 一个参数：二进制路径。输出伪代码到文件（直接打印会非常大）
ghidra-decompile /work/chall > /work/chall.c 2> /work/chall.ghidra.log
wc -l /work/chall.c
```

包装器做的事：建临时项目 → `-import` → 跑 `DecompileAll.java`（遍历所有
函数打印 C 风格伪代码）→ `-deleteProject` 清理。**不要**自己拼
`analyzeHeadless` 的五个参数：项目目录不存在时报的是
`Directory not found: /tmp/...`，很容易被误读成"ghidra 坏了"。

## 读输出：先定位，再展开

整份输出可能有几千行。**不要**一次 `cat` 回上下文：

```bash
grep -n '^// ==== ' /work/chall.c | head -40        # 函数清单
grep -n '^// ==== main' /work/chall.c                # 入口
grep -n 'flag\|secret\|password\|admin' /work/chall.c | head
sed -n '120,220p' /work/chall.c                      # 只取一个函数
```

判断优先级：`main` → 引用了可疑字符串的函数 → 导出/被导入的函数 →
其余 `FUN_...`。自动分析给的名字（`FUN_00101234`）是**地址**，不是语义。

## 坑

- **首次导入慢**：大二进制 + 自动分析要几十秒到几分钟。`run_shell` 的
  `timeout_seconds` 给足（例如 300），不要把超时读成"分析失败"。
- **加壳**：先用 `binwalk` / `strings | grep -i upx` 判断壳；本镜像**没有 upx**，
  不能直接 `upx -d`。脱壳要么用题目附带的工具，要么先从壳代码里找 OEP。
  直接反编译加壳样本得到的是壳的代码，函数清单看起来"很少"不代表程序简单。
- **架构**：ELF/PE/Mach-O 会自动识别；固件/裸二进制需要在 Ghidra 里指定
  processor，当前包装器不支持传 `-processor` —— 这种情况先用 `radare2`
  （`r2 -a arm -b 32`）或 `objdump`。
- **无头 ≠ 交互**：没有 GUI、没有反编译器面板；只能看脚本打印的伪代码。
  需要交叉引用/图形视图时，用 `radare2` 的 `axt` / `agf`。
- 脚本是 Java（`DecompileAll.java`）。不要写 `-postScript *.py`：
  PyGhidra 提供器在 headless 包里不保证存在，会报 `Script not found`。

## 与其他工具的衔接

- 动态行为：`gdb`（本镜像带 GEF；`checksec` / `vmmap` / `pattern` 可用）。
- 快速反汇编/字符串：`objdump -d`、`strings -n 6`、`radare2 -A`。
- Python 字节码：`pycdc` / `uncompyle6`（见 `pyc-decompile`），不要用 Ghidra 硬啃 pyc。
- Android：`jadx` / `apktool`（见 `android-reverse`）。
