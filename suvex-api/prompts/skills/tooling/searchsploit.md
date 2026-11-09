---
name: searchsploit
description: 本地 ExploitDB 检索（本镜像有 searchsploit + exploitdb 快照）。知道组件与版本、要找公开 PoC/利用代码时用；不要用它替代实际验证，也不要把"搜到 PoC"当成"目标可利用"。
---

# SearchSploit（本地 ExploitDB）

本镜像：`searchsploit` + 本地 ExploitDB 快照（构建时打包，**不会联网更新**）。

## 起手

```bash
searchsploit apache 2.4.49
searchsploit --nmap /work/nmap.xml        # 把 nmap -sV 的结果直接喂进去
searchsploit -j wordpress 6.4 | jq '.RESULTS_EXPLOIT[] | {Title, Path}'
```

- `-w` 显示 Exploit-DB 的网页链接（不是本地路径）。
- `-m <EDB-ID>` 把 PoC 复制到当前目录；`-x` 直接用 `$PAGER` 打开。
- 结果里的 `Path` 是 `/usr/share/exploitdb/...` 下的相对路径，**要自己读**：

```bash
searchsploit -p 50383        # 显示完整路径
sed -n '1,120p' /usr/share/exploitdb/exploits/linux/remote/50383.py
```

## 判据：搜到 ≠ 能打

- 版本范围、补丁级别、配置、认证前提都要核对；ExploitDB 的条目经常
  只对某个小版本、某个模块、某个非默认配置成立。
- 语言/平台要对：PHP 的 PoC 不能直接打 Java 服务。
- 依赖要对：PoC 里 `import requests` 之类的库在镜像 venv 里通常有；
  缺库就装或用 `http_request` 手工复现关键请求。
- 有 PoC 但目标是生产/未授权：**不要执行**。本平台的授权纪律优先。

## 坑

- 数据库是**快照**，新 CVE 可能没有；`-u` 更新需要联网，沙箱里通常不可用。
- 搜版本号时用 `-s` 做精确版本匹配，减少噪音。
- `--nmap` 需要 nmap 的 XML 输出（`-oX`），普通文本不行。

## 与其他工具的衔接

- 组件识别：`whatweb` / `httpx -td` / `nmap -sV`。
- PoC 验证：`http_request` / `run_shell`（受授权与范围约束）。
- WordPress 专项：`wpscan`（见 `wpscan` 技能）。
