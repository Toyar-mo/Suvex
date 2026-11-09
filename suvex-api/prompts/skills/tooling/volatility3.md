---
name: volatility3
description: 内存镜像取证（本镜像入口是 vol，Volatility 3）。遇到 .raw/.mem/.vmem/.dmp 内存镜像、要查进程/命令行/网络/文件/注册表时用；不要拿它分析 pcap（用 tshark）或磁盘镜像（用 sleuthkit）。
---

# Volatility 3（本镜像命令名是 `vol`）

镜像里 **没有** `volatility3` / `vol.py` 这两个命令名，只有 `vol`。
先确认镜像类型，再选插件族：

```bash
vol -f /work/mem.raw windows.info      # Windows
vol -f /work/mem.raw linux.pslist      # Linux（需要 dwarf 符号）
```

## 高频插件（按调查顺序）

```bash
# Windows
vol -f mem.raw windows.pslist
vol -f mem.raw windows.cmdline
vol -f mem.raw windows.netscan
vol -f mem.raw windows.filescan | grep -i 'flag\|secret'
vol -f mem.raw windows.dumpfiles --virtaddr 0x...
vol -f mem.raw windows.hashdump          # SAM 哈希（敏感）
vol -f mem.raw windows.registry.printkey --key 'Software\...'

# Linux
vol -f mem.raw linux.pslist
vol -f mem.raw linux.bash                # 命令历史（很常用）
vol -f mem.raw linux.lsof
```

输出很大时用 `-o /work/vol-out` 落盘再 `grep`，不要整段回上下文。

## 坑

- **符号表**：Windows 默认联网下载符号；解题沙箱 `--network none` 会失败。
  先看 `vol -f mem.raw windows.info` 是否报 symbol 错误；离线时用
  `-s /work/symbols` 指定预先放好的符号目录，不要反复重试。
- **镜像类型判错**：`windows.*` 跑 Linux 镜像会报 "Invalid profile" 之类的
  错误；先 `file mem.raw`、`strings | grep -i 'windows\|linux'` 粗判。
- **插件名**：Vol3 的插件是 `windows.pslist` 这种**带点**的名字；`pslist`
  单独写会找不到。`vol -h` 能看到全局参数，插件列表用
  `vol -f mem.raw --help`（或 `vol --help`）查看。
- **输出含敏感数据**（hashdump、凭据）：只用于解题结论，不要写进报告正文。
- 内存镜像通常几百 MB 到几 GB，单条命令可能要几分钟；timeout 给足。

## 与其他工具的衔接

- 字符串线索：`strings -el mem.raw | grep -i flag`（UTF-16 要 `-el`）。
- 提取出的文件再交给 `binwalk` / `exiftool` / `file`。
- 磁盘镜像：`sleuthkit` 的 `fls` / `icat` / `mactime`；pcap：`pcap-analysis`。
