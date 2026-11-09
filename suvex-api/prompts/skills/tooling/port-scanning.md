---
name: port-scanning
description: 端口与服务扫描的选择与边界（本镜像有 nmap / rustscan / masscan）。需要对目标做端口发现、服务识别时用；在解题沙箱里 masscan 因无 NET_RAW 会直接失败，不要把它读成"没有开放端口"。
---

# 端口扫描：先选对工具，再谈结果

本镜像：nmap 7.x、rustscan 2.3、masscan 1.3。

## 这个沙箱里哪些能跑（实测）

| 工具 | 能不能跑 | 说明 |
| --- | --- | --- |
| `nmap -sT` | ✅ | TCP connect，普通用户可跑；**默认选它** |
| `nmap -sS/-O/-A` | ❌ | 需要 root / NET_RAW；沙箱是非 root |
| `rustscan` | ⚠️ | 依赖 nmap，速度扫描部分可能需要 raw socket；失败就退回 `nmap -sT` |
| `masscan` | ❌ | 实测 `[-] FAIL: permission denied`（无 NET_RAW）。**不是目标没开端口** |

## 起手

```bash
nmap -sT -Pn -n --top-ports 100 /work/target.txt        # 先扫常用端口
nmap -sT -Pn -n -p- --min-rate 1000 -T4 10.0.0.5        # 全端口（慢）
nmap -sT -sV -Pn -n -p 22,80,443,8080 10.0.0.5          # 服务/版本
nmap -sT -Pn -n --script http-title,http-headers -p80 10.0.0.5
```

`-sT` 比 `-sS` 慢，但在这个沙箱里是**唯一可靠**的 TCP 扫描方式。
`-Pn` 跳过主机发现（ICMP 常被过滤，跳过它不代表目标死了）。

## 结果怎么用

- 先按端口分优先级：22/80/443/8080/8443 → 再看非标端口（3000、5000、6379、9200…）。
- 拿到服务与版本后再决定下一步：`httpx` 探 Web、`nmap --script` 看已知信息、
  `searchsploit` 查版本。
- **"端口关着"只在扫描确实跑通时才成立**：masscan 权限错、rustscan 中途
  失败、`-sS` 被拒，都不产出结论。

## 授权与速率

- 只扫任务授权的目标；不要用高 `--min-rate` 打生产目标。
- CTF 题里目标通常单一，`-p-` 先全端口再 `-sV` 是常见顺序；但先看题面给没给端口。
- 不要做 SYN flood/压力测试；提示词禁止。

## 与其他工具的衔接

- 子域 → `subfinder`；存活/指纹 → `httpx`；Web 漏洞 → `ffuf` / `nuclei`。
- 端口上跑的是数据库/Redis 等：用对应客户端（`redis-cli` 等，如镜像内有），
  不要用 nmap 脚本硬猜。
