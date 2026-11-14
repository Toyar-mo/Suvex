---
name: ghidra-reverse
description: Ghidra headless 反编译与交叉引用：无 IDA 时的批量二进制分析、函数还原和证据输出。
---

<!--
  Adapted from zhaoxuya520/reverse-skill (MIT License).
  Source: https://github.com/zhaoxuya520/reverse-skill/tree/main/skills/ghidra-reverse
  Changes: removed router/bootstrap/tool-index/Ghidra MCP; mapped to the
  `ghidra-decompile` wrapper and `analyzeHeadless` present in the Suvex image.
-->

# Ghidra 逆向

## 适用

- 没有 IDA 许可证时的主反编译入口；
- 批量 headless 分析；
- 需要伪代码、交叉引用、函数边界与数据类型恢复；
- 与 radare2 快速侦察互补。

## 环境

Suvex 镜像已装 Ghidra，并提供包装器：

```bash
ghidra-decompile /path/to/sample > /work/persist/ghidra/sample.c
analyzeHeadless <project_dir> <project_name> -import /path/to/sample \
  -postScript <script> 2>&1 | tee /work/persist/ghidra/headless.log
```

`ghidra-decompile` 没有 GUI；需要交互式分析时在报告里说明，不要把
GUI 步骤写成已执行。**没有** Ghidra MCP；不要调用不存在的端口/工具。

## 工作流

### 1. 自动分析

```text
□ 识别架构/位数/编译器
□ 记录入口点、导出表、导入 API、字符串
□ 先跑 Strings / 导入表，再决定是否全量反编译
```

### 2. 关键函数

- 从字符串、错误文案、导入 API、协议常量反查 xref；
- 重命名函数/变量，写 plate comment；
- 先还原输入解析与校验，再看加密/网络；
- 需要动态验证时交给 `run_shell`（gdb/strace）或 `js-reverse`。

### 3. 批量与产物

- 项目目录、脚本、反编译输出全部放 `/work/persist/ghidra/`；
- 至少留下：样本 hash、Ghidra 版本、关键函数地址、反编译片段、
  一条可复现命令；
- 结论区分“反编译看到”与“动态验证过”。

## 常见坑

- 自动分析失败不代表文件不可分析：先看架构识别与基址；
- 剥离符号时不要靠函数名猜语义，靠 xref、字符串和调用约定；
- 优化/混淆代码里伪代码可能很脏；用汇编和动态调试验证关键分支；
- 大文件全量反编译很慢：先定位函数再导出。
