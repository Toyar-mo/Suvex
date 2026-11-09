---
name: wpscan
description: WordPress 专项扫描（本镜像有 wpscan 4.1）。目标确认是 WordPress、需要枚举版本/插件/主题/用户时用；不要拿它扫非 WordPress 站点，也不要用它做口令爆破（提示词明令禁止）。
---

# WPScan（WordPress 专项）

本镜像：wpscan 4.1.0。先确认目标真的是 WordPress：

```bash
curl -sI https://target/ | head
curl -s https://target/wp-login.php | head
curl -s https://target/readme.html | grep -i version
```

## 起手

```bash
wpscan --url https://target \
  --enumerate vp,vt,u \
  --random-agent \
  --format json -o /work/wpscan.json
```

- `vp` 插件 / `vt` 主题 / `u` 用户；`--enumerate ap,at` 是全量（慢）。
- **漏洞数据需要 API token**：

```bash
wpscan --url https://target --enumerate vp --api-token "$WPSCAN_TOKEN"
```

没有 token 时它仍会列出插件/主题/版本，但**不会**告诉你哪些有 CVE ——
不要把"没报漏洞"读成"没漏洞"。

## 读结果

- `version`：版本号 + 是否最新；旧版本再去 `searchsploit wordpress <ver>`。
- `plugins` / `themes`：名称、版本、是否可访问；有版本的再查 CVE。
- `users`：枚举出的用户名；只作为后续**授权**测试的输入，不做爆破。

## 坑

- `--url` 必须是站点根（带 `/wp-json` 也行）；给具体页面会得到 404/空结果。
- 默认会跟随 `--random-agent`；生产目标上控制速率（`--throttle`），
  不要把它当压测工具。
- 插件枚举会发很多请求；`--plugins-detection passive` 更安静，但结果更少。
- 口令爆破（`-P` / `--passwords`）属于提示词禁止的动作：本平台的授权
  范围里不做爆破。

## 与其他工具的衔接

- 版本 → `searchsploit wordpress <version>` 找公开 PoC。
- 发现的上传点/参数 → `http_request` 手工验证，不要直接把扫描器的
  "vulnerable" 当结论。
